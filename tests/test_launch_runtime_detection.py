# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the new runtime-detection logic in
``scripts/launch-claude-mcp-stack.sh`` (PR-12 v0.2.11 Bug A + Bug B).

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
              timeout: float = 10.0) -> tuple[int, str, str]:
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
        f'set +e; source "{SCRIPT}"; '
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


def test_resolve_runtime_file_falls_through_when_runtime_unusable(tmp_path: Path):
    """Bug B core scenario: explicit runtime.txt names docker but docker
    daemon isn't reachable → fall through to the next candidate."""
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
    _, out, err = _run_bash('resolve_runtime_file', env=env)
    # Falls through to the WORKING_DIR candidate.
    assert out == str(fallback_path)
    # The fall-through is logged for diagnosability.
    assert "daemon is not reachable" in err or "falling through" in err


def test_resolve_runtime_file_uses_orchestrator_root(tmp_path: Path):
    """Third-priority candidate: VCT_ORCHESTRATOR_ROOT/state/install/runtime.txt."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman").chmod(0o755)

    orch_root = tmp_path / "orchestrator"
    orch_path = _make_runtime_txt(orch_root, "podman")

    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        # Working dir candidate doesn't exist.
        "VCT_STACK_WORKING_DIR": str(tmp_path / "missing"),
        "VCT_ORCHESTRATOR_ROOT": str(orch_root),
    }
    _, out, _ = _run_bash('resolve_runtime_file', env=env)
    assert out == str(orch_path)


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
    """runtime.txt names podman + podman is usable → detect_runtime
    short-circuits to podman without probing docker."""
    fake_bin = tmp_path / "bin"
    _seed_fake_bin(fake_bin)
    (fake_bin / "podman").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman").chmod(0o755)
    (fake_bin / "podman-compose").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "podman-compose").chmod(0o755)

    work_dir = tmp_path / "install"
    _make_runtime_txt(work_dir, "podman")
    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path),
        "VCT_STACK_WORKING_DIR": str(work_dir),
    }
    _, out, _ = _run_bash('detect_runtime', env=env)
    assert out == "podman-compose"
