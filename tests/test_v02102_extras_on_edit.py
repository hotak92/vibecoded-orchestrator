# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Extra code folders and edits (v0.2.102 S16-7, NB-H5; reshaped in v0.2.107 CG-3).

v0.2.107 (CG-3, owner rulings 2026-11-17 / 2026-11-19): the code graph follows
COMMITS, so an edit — under the project root or under an ENABLED extra code
folder — queues NOTHING for the code graph. An extra folder is re-indexed when
ITS HEAD moves (``vco_lib.codegraph_extras_refresh``, spawned by the Stop hook).
What remains of S16-7 is the membership list: ``--list-enabled-extra-paths``
feeds the hooks' cache (``.claude/state/codegraph_extra_paths_cache.txt``),
which the end-of-turn edit reminder reads so an edit in an enabled extra
folder is listed as this project's work.

NB-H5: ``stop-codegraph-drain.sh``'s extras-refresh spawn passes every path to
``sh -c`` as a POSITIONAL PARAMETER — a project root containing ``$``,
backticks or ``"`` is never re-parsed by the shell.

Covered here:
  * the ``--list-enabled-extra-paths`` CLI surface (and the retired
    ``--queued`` mode is refused);
  * bash + pwsh routing: an edit under an enabled extra (direct, through a
    symlink, against a trailing-slash cache line) is listed in the reminder
    and writes NO code-graph queue (RED before CG-3: a per-extra queue file);
    an edit under a disabled extra is not listed; a stale cache refreshes
    through the lister;
  * the Stop hook never spawns a queued drain (leftover queue files included);
  * NB-H5: an evil project root runs no command substitution;
  * R20 L1: a future extras-check stamp never suppresses the check.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import codegraph_extras_refresh as cer
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAIN_SH = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.sh"
DRAIN_PS1 = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.ps1"
ROUTE_LIB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "route-touched-path.sh"
ROUTE_LIB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "route-touched-path.ps1"
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"


@dataclass(frozen=True)
class _Extra:
    path: str
    enabled: bool
    last_indexed_commit: Optional[str] = None


@dataclass(frozen=True)
class _Cfg:
    project_id: str
    code_graph_collection_prefix: str
    code_graph_extra_paths: tuple


def _cfg(extras) -> _Cfg:
    return _Cfg("pid-1", "Acme", tuple(extras))


# ── the CLI surface ────────────────────────────────────────────────────────


def test_list_enabled_extra_paths_filters_disabled(monkeypatch) -> None:
    cfg = _cfg([_Extra("/srv/on", True), _Extra("/srv/off", False)])
    monkeypatch.setattr(
        "vco_lib.project_config.resolve", lambda _root: cfg, raising=True,
    )
    assert cer.list_enabled_extra_paths("/proj") == ["/srv/on"]


def test_cli_list_enabled_prints_and_exits_0(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cer, "list_enabled_extra_paths", lambda root: ["/srv/on"])
    rc = cer.main(["--project-root", "/proj", "--list-enabled-extra-paths"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["/srv/on"]


def test_cli_list_enabled_failure_exits_3(monkeypatch, capsys) -> None:
    def _boom(_root):
        raise RuntimeError("hub down")

    monkeypatch.setattr(cer, "list_enabled_extra_paths", _boom)
    rc = cer.main(["--project-root", "/proj", "--list-enabled-extra-paths"])
    assert rc == 3, "a failed list must exit non-zero so the caller keeps its stale cache"
    assert "config unavailable" in capsys.readouterr().err


def test_cli_refresh_requires_analyzer() -> None:
    with pytest.raises(SystemExit):
        cer.main(["--project-root", "/proj"])


def test_cli_retired_queued_mode_is_refused() -> None:
    """CG-3: the per-edit ``--queued`` drain is retired — the flag is unknown."""
    with pytest.raises(SystemExit):
        cer.main(["--project-root", "/proj", "--queued", "--analyzer", "/x/a.py"])
    assert not hasattr(cer, "refresh_queued")


# ── bash + pwsh fixtures: the routing decision ──────────────────────

_BASH_REQUIRED = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3")), reason="bash required"
)

_PS1_REQUIRED = pytest.mark.skipif(
    not shutil.which("pwsh") or os.name == "nt", reason="pwsh (POSIX) required"
)


def _route_env(tmp_path: Path, **extra: str) -> dict:
    """Env that makes the extras cache deterministic: no launcher.db in the
    state root (no mtime-triggered refresh) and every venv tier missing (a
    TTL-triggered refresh is a soft no-op that keeps the seeded cache)."""
    env = {
        **os.environ,
        "VCT_STATE_DIR": str(tmp_path / "vct-state"),
        "VCT_VENV": "",
        "VCT_INSTALL_ROOT": "",
        "VCO_VENV_PYTHON": "",
    }
    env.update(extra)
    return env


def _run_route_sh(project_root: Path, touched: Path, session: str, env: dict) -> subprocess.CompletedProcess:
    script = (
        f'. "{ROUTE_LIB_SH}"\n'
        f'vco_route_init "{HOOKS_DIR}" "{project_root}" "{shutil.which("python3")}"\n'
        f'vco_route_touched_path "{touched}" "{session}"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=60, env=env)


def _sh_route_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Scratch project + extra path + seeded membership cache (enabled extra)."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    cache = proj / ".claude" / "state" / "codegraph_extra_paths_cache.txt"
    cache.write_text(f"{extra}\n")
    return proj, extra, cache


def _no_codegraph_queue(state: Path) -> None:
    """CG-3: no edit writes a code-graph queue of any kind."""
    left = sorted(p.name for p in state.glob("codegraph_drain_*"))
    assert left == [], f"an edit must queue nothing for the code graph: {left}"


def _reminder(state: Path, session: str = "sess1") -> str:
    f = state / f"edit_reminder_{session}.txt"
    return f.read_text() if f.exists() else ""


@_BASH_REQUIRED
def test_sh_route_extra_edit_is_listed_and_queues_nothing(tmp_path: Path) -> None:
    """RED before CG-3: the edit landed in codegraph_drain_extra_<md5>_<sid>.txt."""
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) in _reminder(state)


@_BASH_REQUIRED
def test_sh_route_primary_edit_is_listed_and_queues_nothing(tmp_path: Path) -> None:
    """RED before CG-3: the edit landed in codegraph_drain_<sid>.txt."""
    proj, _extra, _ = _sh_route_project(tmp_path)
    touched = proj / "main.py"
    touched.write_text("y = 2\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) in _reminder(state)


@_BASH_REQUIRED
def test_sh_route_path_under_disabled_extra_is_not_listed(tmp_path: Path) -> None:
    """LEAVE-ALONE twin: a DISABLED extra row never reaches the cache, so a
    path under it (outside the project root) is not this project's work."""
    proj, _extra, _ = _sh_route_project(tmp_path)
    disabled_extra = tmp_path / "extra-disabled"
    (disabled_extra / "src").mkdir(parents=True)
    touched = disabled_extra / "src" / "off.py"
    touched.write_text("z = 3\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) not in _reminder(state)


@_BASH_REQUIRED
def test_sh_route_stale_cache_refreshes_through_the_lister(tmp_path: Path) -> None:
    """TTL=0 forces a refresh; the venv shim (VCT_VENV tier 1) answers the
    --list-enabled-extra-paths CLI with the extra path, and the edit is then
    listed as an extra-folder edit."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)

    venv = tmp_path / "fakevenv"
    (venv / "bin").mkdir(parents=True)
    shim = venv / "bin" / "python"
    shim.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "-m" ] && [ "$2" = "vco_lib.codegraph_extras_refresh" ]; then
            printf '%s\\n' '{extra}'
            exit 0
        fi
        exec {shutil.which("python3")} "$@"
    """))
    shim.chmod(0o755)

    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    env = _route_env(tmp_path, VCT_VENV=str(venv), VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS="0")
    r = _run_route_sh(proj, touched, "sess1", env)
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    cache = state / "codegraph_extra_paths_cache.txt"
    assert cache.exists() and cache.read_text().splitlines() == [str(extra)]
    assert str(touched) in _reminder(state)
    _no_codegraph_queue(state)


# ── L-A2 symlinks, L-A3 trailing slash (bash + pwsh) ──────────────────────


def _real(p: Path) -> str:
    """pytest's tmp may itself sit behind a symlink (/tmp on some OSes); the
    routing resolves physical paths, so fixtures must name the physical root."""
    return os.path.realpath(p)


def _route_project_with_cache(tmp_path: Path, cache_lines: list[str],
                              rel_proj: str = "proj") -> tuple[Path, Path]:
    """Scratch project whose extras cache holds exactly ``cache_lines``.
    Returns (physical project root, state dir)."""
    proj = tmp_path / rel_proj
    state = proj / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "codegraph_extra_paths_cache.txt").write_text("\n".join(cache_lines) + "\n")
    return Path(_real(proj)), state


def _run_route_ps1(project_root: Path, touched: Path, session: str, env: dict) -> subprocess.CompletedProcess:
    cmd = (
        f". '{ROUTE_LIB_PS1}'; "
        f"Initialize-VcoRoute -HooksDir '{HOOKS_DIR}' -ProjectRoot '{project_root}'; "
        f"Invoke-VcoRouteTouchedPath -Path '{touched}' -SessionId '{session}'"
    )
    return subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", cmd],
                          capture_output=True, text=True, timeout=120, env=env)


@_BASH_REQUIRED
def test_sh_route_symlinked_edit_path_is_listed(tmp_path: Path) -> None:
    """L-A2: an edit THROUGH a symlinked directory still matches the
    canonical extra."""
    extra = tmp_path / "real-extra"
    (extra / "src").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(extra, link)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra)])
    (extra / "src" / "foo.py").write_text("x = 1\n")
    touched_via_link = link / "src" / "foo.py"
    r = _run_route_sh(proj, touched_via_link, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched_via_link) in _reminder(state)
    _no_codegraph_queue(state)


@_BASH_REQUIRED
def test_sh_route_trailing_slash_cache_line_matches(tmp_path: Path) -> None:
    """L-A3: a cache line with a trailing separator matches on both siblings."""
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra) + "/"])
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched) in _reminder(state)
    _no_codegraph_queue(state)


@_PS1_REQUIRED
def test_ps1_route_extra_edit_is_listed_and_queues_nothing(tmp_path: Path) -> None:
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) in _reminder(state)


@_PS1_REQUIRED
def test_ps1_route_primary_edit_is_listed_and_queues_nothing(tmp_path: Path) -> None:
    proj, _extra, _ = _sh_route_project(tmp_path)
    touched = proj / "main.py"
    touched.write_text("y = 2\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) in _reminder(state)


@_PS1_REQUIRED
def test_ps1_route_path_under_disabled_extra_is_not_listed(tmp_path: Path) -> None:
    proj, _extra, _ = _sh_route_project(tmp_path)
    disabled_extra = tmp_path / "extra-disabled"
    (disabled_extra / "src").mkdir(parents=True)
    touched = disabled_extra / "src" / "off.py"
    touched.write_text("z = 3\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    _no_codegraph_queue(state)
    assert str(touched) not in _reminder(state)


@_PS1_REQUIRED
def test_ps1_route_symlinked_edit_path_is_listed(tmp_path: Path) -> None:
    extra = tmp_path / "real-extra"
    (extra / "src").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(extra, link)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra)])
    (extra / "src" / "foo.py").write_text("x = 1\n")
    touched_via_link = link / "src" / "foo.py"
    r = _run_route_ps1(proj, touched_via_link, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched_via_link) in _reminder(state)
    _no_codegraph_queue(state)


@_PS1_REQUIRED
def test_ps1_route_trailing_slash_cache_line_matches(tmp_path: Path) -> None:
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra) + "/"])
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched) in _reminder(state)
    _no_codegraph_queue(state)


# ── the Stop hook: extras check + catch-up only ─────────────────────────


def _shim_python_per_arg(path: Path, log: Path) -> None:
    """A stand-in interpreter: records every argv element on its own line."""
    path.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done >> '{log}'\n"
        "printf '\\n' >> '" + str(log) + "'\n"
    )
    path.chmod(0o755)


def _wait_lines(p: Path, want: int, tries: int = 100) -> list:
    for _ in range(tries):
        if p.exists():
            lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
            if len(lines) >= want:
                return lines
        time.sleep(0.05)
    return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _drain_fixture(tmp_path: Path, root: Path, *, suppress_check: bool = True) -> dict:
    """State dir, shim interpreter + analyzer stub. ``suppress_check`` stamps
    a fresh extras-check ts so the extras spawn cannot fire; the NB-H5 test
    disables it because that spawn IS the surface under test.

    v0.2.105 (#512A): the Stop hook ALSO spawns the HEAD-diff catch-up
    (``-m vco_lib.codegraph_head_catchup``). EVERY assertion in this file is
    about the extras surface, so the catch-up's own throttle stamp is always
    written fresh here — unconditionally, including when ``suppress_check``
    is False (that flag is about the EXTRAS check only). Covered by
    ``tests/test_v02105_codegraph_head_catchup.py`` instead.
    """
    state = root / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    spawn_log = tmp_path / "spawned.log"
    shim = tmp_path / "py.sh"
    _shim_python_per_arg(shim, spawn_log)
    stub = tmp_path / "analyze.py"
    stub.write_text("")
    now = str(int(time.time()))
    (state / "codegraph_head_catchup.ts").write_text(now)
    if suppress_check:
        (state / "codegraph_extras_check.ts").write_text(now)
    return {
        "state": state, "shim": shim, "stub": stub, "spawn_log": spawn_log, "now": now,
    }


def _spawn_blocks(log: Path) -> list[list[str]]:
    """The shim logs one argv element per line + a blank separator; return one
    block per invocation so the FIRST element (the script/interpreter arg)
    identifies what ran."""
    if not log.exists():
        return []
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in log.read_text().splitlines():
        if ln.strip():
            cur.append(ln)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def _wait_blocks(log: Path, want: int, tries: int = 100) -> list[list[str]]:
    for _ in range(tries):
        blocks = _spawn_blocks(log)
        if len(blocks) >= want:
            return blocks
        time.sleep(0.05)
    return _spawn_blocks(log)


def _drain_env(root: Path, fx: dict, **extra: str) -> dict:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(root),
        "VCT_PYTHON": str(fx["shim"]),
        "VCT_ANALYZER_SCRIPT": str(fx["stub"]),
        "VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS": "600",
    }
    env.update(extra)
    return env


# ── NB-H5: an evil project root is never re-parsed by sh -c ────────────────


@_BASH_REQUIRED
def test_sh_drain_evil_project_root_runs_no_command_substitution(tmp_path: Path) -> None:
    """RED pre-fix: the extras-refresh spawn interpolated \"$PROJECT_ROOT\"
    into the double-quoted sh -c string — `$(touch …)` in the path executed.
    """
    pwned = tmp_path / "PWNED"
    evil = tmp_path / f'x $(touch {pwned}) "y"'
    # suppress_check=False: the commit-stamp extras spawn (the NB-H5 surface)
    # must actually fire.
    fx = _drain_fixture(tmp_path, evil, suppress_check=False)

    r = subprocess.run(["bash", str(DRAIN_SH)],
                       input=json.dumps({"session_id": "sessE"}),
                       capture_output=True, text=True, timeout=60,
                       env=_drain_env(evil, fx))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    assert not pwned.exists(), (
        "command substitution in the project path executed — the spawn "
        "re-parsed $PROJECT_ROOT (NB-H5)"
    )
    # The literal path reached the interpreter as ONE argument.
    flat = [ln for b in blocks for ln in b]
    assert str(evil) in flat, (
        f"the literal evil path must arrive as a single argv element: {blocks}"
    )


# ── R20 L1: a FUTURE stamp (pre-v0.2.103 local-time stamp) never throttles ──
# Windows PowerShell 5.1 wrote LOCAL-time epoch stamps before v0.2.103; at
# UTC+2 such a stamp sits 7200 s in the future of the new true-UTC "now", and
# `now - last < interval` then suppressed every check for hours. Both
# siblings read a future stamp as "no stamp".

_FUTURE_S = 7200


def _shell_argv(shell: str) -> list[str]:
    if shell == "bash":
        return ["bash", str(DRAIN_SH)]
    return ["pwsh", *PWSH_BASE_FLAGS, "-File", str(DRAIN_PS1)]


def _shell_available(shell: str) -> bool:
    if shell == "bash":
        return shutil.which("bash") is not None
    # Same reach as _PS1_REQUIRED: the stand-in interpreter is a /bin/sh shim.
    return shutil.which("pwsh") is not None and os.name != "nt"


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_future_extras_check_stamp_does_not_suppress_the_check(
        tmp_path: Path, shell: str) -> None:
    if not _shell_available(shell):
        pytest.skip(f"{shell} required")
    root = tmp_path / "proj"
    root.mkdir()
    fx = _drain_fixture(tmp_path, root, suppress_check=False)
    stamp = fx["state"] / "codegraph_extras_check.ts"
    stamp.write_text(str(int(fx["now"]) + _FUTURE_S))

    r = subprocess.run(_shell_argv(shell), input=json.dumps({"session_id": "sessX"}),
                       capture_output=True, text=True, timeout=120,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    refresh = [b for b in blocks if "vco_lib.codegraph_extras_refresh" in b]
    assert refresh, f"{shell}: a future stamp must not suppress the check: {blocks}"
    assert int(stamp.read_text()) <= int(time.time()), "the stamp is rewritten to now"


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_recent_real_stamp_still_suppresses_the_check(tmp_path: Path, shell: str) -> None:
    """LEAVE-ALONE: a genuine recent (past) stamp keeps throttling."""
    if not _shell_available(shell):
        pytest.skip(f"{shell} required")
    root = tmp_path / "proj"
    root.mkdir()
    fx = _drain_fixture(tmp_path, root, suppress_check=False)
    (fx["state"] / "codegraph_extras_check.ts").write_text(str(int(fx["now"]) - 5))
    r = subprocess.run(_shell_argv(shell), input=json.dumps({"session_id": "sessR"}),
                       capture_output=True, text=True, timeout=120,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)
    assert _spawn_blocks(fx["spawn_log"]) == []


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_stop_hook_never_drains_a_queue(tmp_path: Path, shell: str) -> None:
    """CG-3: leftover queue files from an older release (session, shared,
    per-extra) start NO analyzer and NO queued drain; with both throttles
    fresh the hook spawns nothing at all. RED before CG-3: the analyzer ran
    on the primary queue and ``--queued`` on the extra queue."""
    if not _shell_available(shell):
        pytest.skip(f"{shell} required")
    root = tmp_path / "proj"
    root.mkdir()
    fx = _drain_fixture(tmp_path, root)
    state = fx["state"]
    (state / "codegraph_drain_sessQ.txt").write_text(f"{root}/m.py\n")
    (state / "codegraph_drain_shared.txt").write_text(f"{root}/n.py\n")
    (state / "codegraph_drain_extra_0123456789abcdef0123456789abcdef_sessQ.txt").write_text(
        "/srv/clone/a.py\n")
    r = subprocess.run(_shell_argv(shell), input=json.dumps({"session_id": "sessQ"}),
                       capture_output=True, text=True, timeout=120,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)  # give a (wrongly) spawned child a beat to appear
    assert _spawn_blocks(fx["spawn_log"]) == [], _spawn_blocks(fx["spawn_log"])
