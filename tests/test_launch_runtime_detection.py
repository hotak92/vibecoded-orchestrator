# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the runtime-detection logic in
``scripts/launch-claude-mcp-stack.sh`` (PR-12 v0.2.11 Bug A + Bug C, and the
v0.2.97 pin rule that superseded Bug B).

PR-12 "Bug B" let a runtime.txt — or ``VCT_CONTAINER_RUNTIME`` — whose runtime
was down fall through to the OTHER runtime. v0.2.97 reverses that: the boot
wrapper follows THE pin rule every other VCO surface follows
(``vco_lib.containers.runtime_pin``: ``VCT_CONTAINER_RUNTIME`` →
``state/install/runtime.txt`` → auto-detect). Starting the stack under the
other runtime creates its containers on that runtime's EMPTY volumes next to
the real data (service-endpoints plan invariants I1/I2), so a pinned runtime
that is down starts nothing and logs one line naming the pin and the fix.
Unpinned auto-detection keeps its podman-then-docker fallback.

We exercise three pure helpers by sourcing the script:

  - ``_runtime_usable``      — daemon-access validation per runtime token
  - ``resolve_runtime_file`` — multi-candidate runtime.txt path resolution
  - ``detect_runtime``       — top-level dispatcher that wires the two

The script's ``main`` only runs when invoked as ``${0}``, so sourcing
exposes the helpers without side-effects. We stub `command`, `timeout`,
`docker`, `podman` via shell function overrides inside the spawned
subshell so the tests don't depend on real container runtimes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="bash not available in test environment (script is Linux/macOS only)",
)

# Coreutils the script invokes via PATH. We symlink each into per-test
# fake_bin dirs so tests can run with PATH=fake_bin only (no /usr/bin),
# which prevents the host's real podman/docker from leaking into the
# detect_runtime probe.
_NEEDED_COREUTILS = (
    # Coreutils invoked by the script's own helpers.
    "head", "tr", "timeout", "grep", "sleep", "date", "cat", "uname", "dirname",
    # main() (the exit-3 ledger test runs it whole): the refusal-reason pipe.
    "sed", "tail", "rm", "readlink",
    # `bash` and `env` are needed because the runtime stubs use a
    # `#!/usr/bin/env bash` shebang. Without symlinking these, the
    # shebang resolves /usr/bin/env (absolute path) but then env's
    # PATH-search for `bash` fails when /usr/bin isn't on the test PATH.
    "bash", "env",
)
_COREUTIL_PATHS = {name: shutil.which(name) for name in _NEEDED_COREUTILS}


def _seed_fake_bin(fake_bin: Path) -> None:
    """Symlink coreutils into ``fake_bin`` so a PATH=fake_bin-only run
    can still execute `head`, `timeout`, etc. Skips any utility not
    available on this host (the test would skip via downstream behaviour
    anyway)."""
    fake_bin.mkdir(parents=True, exist_ok=True)
    for name, real in _COREUTIL_PATHS.items():
        if real is None:
            continue
        link = fake_bin / name
        if not link.exists():
            link.symlink_to(real)


def _run_bash(snippet: str, env: dict | None = None,
              timeout: float = 10.0, script: Path = SCRIPT) -> tuple[int, str, str]:
    """Run a bash snippet that has the script already sourced. Return
    (rc, stdout, stderr) with trailing whitespace stripped.

    The script uses ``set -u`` and references ``HOME``, ``PATH``, etc.
    at source time — caller env MUST supply at minimum a HOME and PATH.
    We merge in defaults so per-test envs only need to declare what
    they're actually testing."""
    base = {
        "HOME": "/tmp",
        # Default PATH for tests that don't care about runtime probing.
        # Tests that DO care override PATH to a fake_bin that excludes
        # the host's real podman/docker.
        "PATH": "/usr/bin:/bin",
    }
    if env is not None:
        base.update(env)
    # Override `command -v` so it consults ONLY the per-test fake_bin
    # PATH for `docker`/`podman`/`podman-compose` — without this the
    # script picks up the host's real container runtimes (which on dev
    # boxes typically have working daemons) and the tests can't deny
    # specific runtimes. We override post-source so the script's own
    # source-time `command -v` calls (none currently) are unaffected.
    #
    # The override accepts any other arg verbatim (delegates to builtin).
    #
    # The script's log() function prints to BOTH stdout and the log
    # file — that pollutes pure-helper stdout (e.g. resolve_runtime_file
    # is supposed to print just a path). Override it post-source so the
    # diagnostic still lands on stderr (where tests can assert on it
    # via `err`) but stays out of stdout.
    full = (
        f'set +e; source "{script}"; '
        'log() { printf "%s\\n" "$*" 1>&2; }; '
        f'{snippet}'
    )
    proc = subprocess.run(
        [BASH or "bash", "-c", full],
        capture_output=True, text=True, timeout=timeout,
        env=base,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------------------
# _runtime_usable — daemon-access validation
# ---------------------------------------------------------------------------


def test_runtime_usable_unknown_token_returns_false():
    # Snippet inverts so we look at stdout instead of $?.
    _, out, _ = _run_bash('_runtime_usable nerdctl && echo OK || echo NO')
    assert out == "NO"


def test_runtime_usable_docker_with_no_server_returns_false(tmp_path: Path):
    """Bug A core scenario: docker binary on PATH but daemon
    unreachable (e.g. user not in `docker` group). `docker info` exits
    non-zero or its output lacks a `Server:` section. Must return false."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    docker_stub = fake_bin / "docker"
    # Stub: emit a Client-only `docker info` and exit 1 (mimics the
    # permission-denied case Docker's CLI shows).
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then\n'
        '  echo "Client:"\n'
        '  echo " Version: 20.10.0"\n'
        '  echo "ERROR: permission denied while trying to connect..." 1>&2\n'
        '  exit 1\n'
        'fi\n'
    )
    docker_stub.chmod(0o755)
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    _, out, _ = _run_bash(
        '_runtime_usable docker && echo OK || echo NO',
        env=env,
    )
    assert out == "NO"


def test_runtime_usable_docker_with_server_returns_true(tmp_path: Path):
    """Mirror image: stub docker that emits a Server: section → usable."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    docker_stub = fake_bin / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then\n'
        '  echo "Client:"\n'
        '  echo " Version: 20.10.0"\n'
        '  echo "Server:"\n'
        '  echo " Server Version: 20.10.0"\n'
        '  exit 0\n'
        'fi\n'
    )
    docker_stub.chmod(0o755)
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    _, out, _ = _run_bash(
        '_runtime_usable docker && echo OK || echo NO',
        env=env,
    )
    assert out == "OK"


def test_runtime_usable_podman_passes_when_info_succeeds(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text(
        "#!/usr/bin/env bash\nexit 0\n"
    )
    (fake_bin / "podman").chmod(0o755)
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    _, out, _ = _run_bash(
        '_runtime_usable podman && echo OK || echo NO',
        env=env,
    )
    assert out == "OK"


def test_runtime_usable_podman_fails_when_info_errors(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then exit 125; fi\n'
        "exit 0\n"
    )
    (fake_bin / "podman").chmod(0o755)
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    _, out, _ = _run_bash(
        '_runtime_usable podman && echo OK || echo NO',
        env=env,
    )
    assert out == "NO"


# ---------------------------------------------------------------------------
# resolve_runtime_file — multi-candidate path resolution
# ---------------------------------------------------------------------------


def _make_runtime_txt(root: Path, token: str) -> Path:
    """Materialise a runtime.txt at <root>/state/install/runtime.txt."""
    target_dir = root / "state" / "install"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "runtime.txt"
    target.write_text(token + "\n", encoding="utf-8")
    return target


def test_resolve_runtime_file_explicit_env_wins(tmp_path: Path):
    """When VCT_STACK_RUNTIME_FILE is explicitly set AND its content
    names a usable runtime, the explicit path wins."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman").chmod(0o755)

    explicit_dir = tmp_path / "explicit"
    explicit_path = _make_runtime_txt(explicit_dir, "podman")

    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_RUNTIME_FILE": str(explicit_path),
        "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist"),
    }
    _, out, _ = _run_bash('resolve_runtime_file', env=env)
    assert out == str(explicit_path)


def test_resolve_runtime_file_a_down_runtime_is_still_the_pin(tmp_path: Path):
    """Formerly PR-12 Bug B ("fall through to the next candidate when the
    recorded runtime is down") — SUPERSEDED by the v0.2.97 pin rule
    (``vco_lib.containers.runtime_pin``): the first runtime.txt that records a
    runtime IS the pin, reachable or not. detect_runtime refuses it (below);
    a second, differently-recorded file is never consulted."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    # docker stub fails the daemon-access check.
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then echo "Client:"; exit 1; fi\n'
    )
    (fake_bin / "docker").chmod(0o755)
    # podman stub passes.
    (fake_bin / "podman").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman").chmod(0o755)

    explicit_dir = tmp_path / "stale-install"
    explicit_path = _make_runtime_txt(explicit_dir, "docker")

    fallback_dir = tmp_path / "fresh-install"
    fallback_path = _make_runtime_txt(fallback_dir, "podman")

    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_RUNTIME_FILE": str(explicit_path),
        "VCT_STACK_WORKING_DIR": str(fallback_dir),
    }
    _, out, _err = _run_bash('resolve_runtime_file', env=env)
    # The explicit (docker) file is the pin; the fresh podman one is ignored.
    assert out == str(explicit_path)
    assert out != str(fallback_path)


def _clone(tmp_path: Path, name: str = "clone") -> tuple[Path, Path]:
    """An orchestrator clone layout the wrapper can belong to: a COPY of the
    script in ``<clone>/scripts/`` (so its own clone is ``<clone>``, not this
    checkout), ``vco_lib`` linked in, and an ``infrastructure/`` compose home.
    Returns ``(clone_root, script)``."""
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    (root / "vco_lib").symlink_to(REPO_ROOT / "vco_lib", target_is_directory=True)
    (root / "infrastructure").mkdir()
    (root / "infrastructure" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return root, script


def test_resolve_runtime_file_skips_a_file_that_names_no_runtime(tmp_path: Path):
    """PR-12 Bug C is kept: a candidate that is missing, empty or names no
    runtime is not a pin — the next candidate (the own clone's record) is."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    root, script = _clone(tmp_path)
    garbage = _make_runtime_txt(tmp_path / "garbage", "nerdctl")
    real = _make_runtime_txt(root, "podman")
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_RUNTIME_FILE": str(garbage),
    }
    _, out, err = _run_bash('resolve_runtime_file', env=env, script=script)
    assert out == str(real)
    assert "not podman or docker" in err


def test_the_wrapper_reads_its_own_clones_record_not_another_clones(tmp_path: Path):
    """R8 G5: the wrapper serves ITS OWN clone. A stale unit WorkingDirectory
    (PR-12 Bug C) or VCT_ORCHESTRATOR_ROOT naming ANOTHER clone used to come
    first, so that clone's docker record pinned this install's stack onto
    docker's empty volumes. Now the own clone's podman record is THE pin and
    the disagreement is logged."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    root, script = _clone(tmp_path)
    own = _make_runtime_txt(root, "podman")
    old = tmp_path / "old-clone"
    _make_runtime_txt(old, "docker")
    _make_runtime_txt(old / "infrastructure", "docker")
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(old / "infrastructure"),
        "VCT_ORCHESTRATOR_ROOT": str(old),
    }
    _, out, err = _run_bash('resolve_runtime_file', env=env, script=script)
    assert out == str(own)
    assert "ignoring the other clone's record" in err


def test_a_stale_working_dir_falls_back_to_the_wrappers_own_clone(tmp_path: Path):
    """R8 G5: a VCT_STACK_WORKING_DIR that is not a VCO compose home (moved /
    re-cloned install) is logged and replaced by the own clone's
    infrastructure/ — the same volume names, never an empty stack."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    root, script = _clone(tmp_path)
    stale = tmp_path / "moved-away"
    stale.mkdir()
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path), "VCT_STACK_WORKING_DIR": str(stale)}
    _, out, err = _run_bash('resolve_working_dir; printf "%s" "$VCT_STACK_WORKING_DIR"',
                            env=env, script=script)
    assert out == str(root / "infrastructure")
    assert "not a VCO compose directory" in err
    # A real compose home is left alone.
    env["VCT_STACK_WORKING_DIR"] = str(root / "infrastructure")
    _, out, _ = _run_bash('resolve_working_dir; printf "%s" "$VCT_STACK_WORKING_DIR"',
                          env=env, script=script)
    assert out == str(root / "infrastructure")


def test_resolve_runtime_file_returns_empty_when_no_candidate(tmp_path: Path):
    """All candidate paths missing → empty output (caller falls through
    to live probe)."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(tmp_path / "no1"),
        "VCT_ORCHESTRATOR_ROOT": str(tmp_path / "no2"),
    }
    _, out, _ = _run_bash('resolve_runtime_file', env=env)
    assert out == ""


# ---------------------------------------------------------------------------
# detect_runtime — top-level dispatcher with daemon validation
# ---------------------------------------------------------------------------


def test_detect_runtime_prefers_podman_over_docker(tmp_path: Path):
    """When BOTH podman and docker have usable daemons, podman wins.
    PR-12 Bug A: this is the new "preferred default" — flips the
    pre-PR-12 docker-first probe order."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman").chmod(0o755)
    (fake_bin / "podman-compose").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman-compose").chmod(0o755)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then echo "Server:"; echo " Server Version: 20.10.0"; exit 0; fi\n'
    )
    (fake_bin / "docker").chmod(0o755)
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist"),
    }
    _, out, _ = _run_bash('detect_runtime', env=env)
    assert out == "podman-compose"


def test_detect_runtime_skips_docker_without_daemon_access(tmp_path: Path):
    """Real-world Bug A scenario: docker binary on PATH but `docker info`
    fails (user not in `docker` group). detect_runtime must NOT pick
    docker — it must fall through to empty (no podman) or emit an
    accurate diagnostic."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then echo "Client:"; exit 1; fi\n'
    )
    (fake_bin / "docker").chmod(0o755)
    # No podman either.
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist"),
    }
    _, out, _ = _run_bash('detect_runtime', env=env)
    # No usable runtime → empty string.
    assert out == ""


def test_detect_runtime_picks_docker_when_only_docker_usable(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "info" ]; then echo "Server:"; echo " Server Version: 20.10.0"; exit 0; fi\n'
    )
    (fake_bin / "docker").chmod(0o755)
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist"),
    }
    _, out, _ = _run_bash('detect_runtime', env=env)
    assert out == "docker"


def test_detect_runtime_honors_runtime_txt_when_usable(tmp_path: Path):
    """The own clone's runtime.txt names docker + docker is usable → docker,
    although unpinned auto-detection would have picked the usable podman."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    for name, body in (("podman", "exit 0\n"), ("podman-compose", "exit 0\n"), ("docker", _DOCKER_UP)):
        (fake_bin / name).write_text("#!/usr/bin/env bash\n" + body)
        (fake_bin / name).chmod(0o755)
    root, script = _clone(tmp_path)
    _make_runtime_txt(root, "docker")
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    _, out, _ = _run_bash('detect_runtime', env=env, script=script)
    assert out == "docker"


# ---------------------------------------------------------------------------
# v0.2.97: THE pin rule (supersedes PR-12 Bug B's fall-through)
# ---------------------------------------------------------------------------


def _stub(fake_bin: Path, name: str, body: str) -> None:
    (fake_bin / name).write_text("#!/usr/bin/env bash\n" + body)
    (fake_bin / name).chmod(0o755)


_DOCKER_UP = 'if [ "$1" = "info" ]; then echo "Server:"; echo " Server Version: 20.10.0"; exit 0; fi\n'
_DOCKER_DOWN = 'if [ "$1" = "info" ]; then echo "Client:"; exit 1; fi\n'
_PODMAN_UP = "exit 0\n"
_PODMAN_DOWN = 'if [ "$1" = "info" ]; then exit 125; fi\nexit 0\n'
# podman up, holding VCO's containers/volumes (what `ps -a` / `volume ls` list).
_PODMAN_DATA = ('case "$1" in ps) echo vco_weaviate ;; volume) echo vco_weaviate_data ;; esac\n'
                "exit 0\n")


def _detect(env: dict, script: Path = SCRIPT) -> tuple[str, int, str]:
    """detect_runtime's (stdout, return code, stderr)."""
    _, out, err = _run_bash('out="$(detect_runtime)"; rc=$?; printf "%s|%s" "$out" "$rc"', env=env,
                            script=script)
    answer, _, rc = out.rpartition("|")
    return answer, int(rc), err


def _py_env(tmp_path: Path) -> dict:
    """What the wrapper needs to reach the Python reconcile: an interpreter
    (VCO_VENV_PYTHON) that imports vco_lib, and a sandboxed state/home."""
    return {"VCO_VENV_PYTHON": sys.executable, "PYTHONPATH": str(REPO_ROOT),
            "VCT_STATE_DIR": str(tmp_path / "vct-state"),
            "VCT_LAUNCHER_DB_PATH": str(tmp_path / "no-launcher.db"),
            "VCT_STACK_LOG_FILE": str(tmp_path / "stack.log"),
            # W-TOOL-DIRS for a child built from scratch: without it the
            # Python side would find this machine's REAL /usr/bin runtimes
            # through the usual-install-locations table (R9 H1(b)).
            "VCT_TOOL_SEARCH_DIRS": ""}


def test_a_runtime_txt_pin_whose_runtime_is_down_starts_nothing(tmp_path: Path):
    """The install recorded docker; docker is installed but DOWN; podman (with
    compose, holding VCO data) is up. Pre-v0.2.97 (PR-12 Bug B) the wrapper
    started the stack under podman — on podman's volumes, away from docker's
    data. A down record is never switched (R8 G1 case b, read-only here):
    nothing, rc 4, one line naming the pin (the runtime.txt path) and the fix."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "docker", _DOCKER_DOWN)
    _stub(fake_bin, "podman", _PODMAN_DATA)
    _stub(fake_bin, "podman-compose", "exit 0\n")
    root, script = _clone(tmp_path)
    pin = _make_runtime_txt(root, "docker")
    out, rc, err = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path), **_py_env(tmp_path)},
                           script=script)
    assert (out, rc) == ("", 4), err
    lines = [ln for ln in err.splitlines() if "pinned to docker" in ln]
    assert len(lines) == 1, err
    assert str(pin) in lines[0]
    assert "starting nothing" in lines[0]
    assert "Fix: start docker" in lines[0]
    assert "VCT_CONTAINER_RUNTIME=podman" in lines[0]


def test_an_env_pin_whose_runtime_is_down_starts_nothing(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "podman", _PODMAN_DOWN)
    _stub(fake_bin, "docker", _DOCKER_UP)
    out, rc, err = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path),
                            "VCT_CONTAINER_RUNTIME": "podman",
                            "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist")})
    assert (out, rc) == ("", 4), err
    assert "pinned to podman by VCT_CONTAINER_RUNTIME" in err
    assert "unset VCT_CONTAINER_RUNTIME" in err


def test_a_pinned_podman_without_a_compose_front_end_never_becomes_docker(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    # podman answers `info` but has no `compose` subcommand, and there is
    # no podman-compose on PATH.
    _stub(fake_bin, "podman", 'if [ "$1" = "compose" ]; then exit 125; fi\nexit 0\n')
    _stub(fake_bin, "docker", _DOCKER_UP)
    out, rc, err = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path),
                            "VCT_CONTAINER_RUNTIME": "podman",
                            "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist")})
    assert (out, rc) == ("", 4), err
    assert "podman compose" in err


def test_the_env_pin_outranks_runtime_txt(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "docker", _DOCKER_UP)
    _stub(fake_bin, "podman", _PODMAN_UP)
    _stub(fake_bin, "podman-compose", "exit 0\n")
    root, script = _clone(tmp_path)
    _make_runtime_txt(root, "podman")
    out, rc, _ = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path),
                          "VCT_CONTAINER_RUNTIME": "docker"}, script=script)
    assert (out, rc) == ("docker", 0)


def test_a_stale_record_whose_runtime_is_gone_boots_where_the_data_is(tmp_path: Path):
    """R8 G5 + the read-only G1 reconcile: the own clone recorded docker, docker
    is no longer installed, podman answers and holds VCO's containers. Before,
    the wrapper refused (rc 4 / exit 3) at every boot until someone edited the
    record by hand. Now it boots under podman for this boot and leaves the
    record alone (the next update re-records it)."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "podman", _PODMAN_DATA)
    _stub(fake_bin, "podman-compose", "exit 0\n")
    root, script = _clone(tmp_path)
    record = _make_runtime_txt(root, "docker")
    out, rc, err = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path), **_py_env(tmp_path)},
                           script=script)
    assert (out, rc) == ("podman-compose", 0), err
    assert "stale runtime record" in err
    assert record.read_text(encoding="utf-8").strip() == "docker", "the wrapper never rewrites state"


def test_an_exit_3_is_recorded_in_the_installed_clones_ledger(tmp_path: Path):
    """R8 G6: the boot wrapper's exit 3 used to surface only in its /tmp log.
    It now records `container_runtime_unusable` in its own clone's
    UPDATE_DEFERRED ledger (the one Python emitter), and still exits 3."""
    from vco_lib.deferral_report import DeferralReport

    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "docker", _DOCKER_DOWN)
    _stub(fake_bin, "podman", _PODMAN_DATA)
    root, script = _clone(tmp_path)
    _make_runtime_txt(root, "docker")
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path), **_py_env(tmp_path)}
    proc = subprocess.run([BASH or "bash", str(script)], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(tmp_path))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    entries = DeferralReport.read(root).entries
    assert [e.condition_id for e in entries] == ["container_runtime_unusable"], proc.stdout + proc.stderr
    assert "pinned to docker" in entries[0].detected
    assert entries[0].dismiss_fields["root"] == str(root)


def test_unpinned_auto_detection_keeps_its_fallback(tmp_path: Path):
    """No pin (no env, no runtime.txt): podman without a compose front-end
    still falls through to a usable docker — auto-detection is unchanged."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    _stub(fake_bin, "podman", 'if [ "$1" = "compose" ]; then exit 125; fi\nexit 0\n')
    _stub(fake_bin, "docker", _DOCKER_UP)
    out, rc, _ = _detect({"PATH": str(fake_bin), "HOME": str(tmp_path),
                          "VCT_CONTAINER_RUNTIME": "auto",
                          "VCT_STACK_WORKING_DIR": str(tmp_path / "noexist")})
    assert (out, rc) == ("docker", 0)
