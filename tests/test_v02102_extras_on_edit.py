# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 — S16-7 (extras refresh on edit), NB-H5 (sh -c injection), lane W2-A.

S16-7: a Write/Edit/Bash-written code file under an ENABLED extra code-graph
path is queued to a PER-EXTRA drain queue (``codegraph_drain_extra_<md5>_
<sid>.txt``) by ``_lib/route-touched-path.{sh,ps1}`` — never the primary
queue — and the Stop hook drains those queues on every Stop via
``vco_lib.codegraph_extras_refresh --queued`` (scoped ``--only-files-from``
with the extra's OWN ``--project`` prefix), even inside the 120 s primary
rate-limit window. The membership list is a cached table
(``.claude/state/codegraph_extra_paths_cache.txt``) refreshed through the
``--list-enabled-extra-paths`` CLI on a TTL or when launcher.db changes.

NB-H5: ``stop-codegraph-drain.sh``'s extras-refresh spawn passes every path
to ``sh -c`` as a POSITIONAL PARAMETER — a project root containing ``$``,
backticks or ``"`` is never re-parsed by the shell.

Covered here:
  * the queued-drain argv matches the committed fixture
    (``tests/fixtures/codegraph_extra_sync_argv.json::queued_cases``);
  * ``refresh_queued`` driver: drain / multi-session merge / disabled leave-
    alone / held lock / analyzer failure (queue restored) / foreign-path
    filter / hub-down keep / commit NOT recorded;
  * the ``--queued`` + ``--list-enabled-extra-paths`` CLI surface;
  * bash fixtures: route an extra edit into the extra queue (RED pre-fix),
    primary edit stays primary, disabled-extra path never extra-queued,
    stale cache refreshes through the lister;
  * the drain hook spawns ``--queued`` even inside the rate-limit window;
  * NB-H5: an evil project root runs no command substitution;
  * the pwsh runtime legs when pwsh is available.
"""
from __future__ import annotations

import hashlib
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
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "codegraph_extra_sync_argv.json"
DRAIN_SH = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.sh"
DRAIN_PS1 = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.ps1"
ROUTE_LIB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "route-touched-path.sh"
ROUTE_LIB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "route-touched-path.ps1"
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()  # noqa: S324 — a key


# ── the queued-drain argv matches the shared fixture ──────────────────────


def test_queued_argv_matches_shared_fixture() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["queued_cases"]
    assert cases, "the fixture must pin the queued-drain argv"
    for c in cases:
        got = cer.build_extra_sync_argv(c["path"], c["prefix"], False, None) + [
            "--only-files-from", c["list_file"],
        ]
        assert got == c["argv"], c["name"]
        # The analyzer rejects --incremental in scoped mode — the queued argv
        # must never carry it.
        assert "--incremental" not in got and "--since-commit" not in got


# ── refresh_queued with injected deps ──────────────────────────────────────


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


class _Rec:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.runs: list = []
        self.listed: list = []
        self.records: list = []

    def run_analyzer(self, python, analyzer, argv, timeout):
        argv = list(argv)
        self.runs.append(argv)
        listed = []
        if "--only-files-from" in argv:
            lf = Path(argv[argv.index("--only-files-from") + 1])
            try:
                listed = [ln for ln in lf.read_text().splitlines() if ln.strip()]
            except OSError as exc:
                listed = [f"<unreadable: {exc}>"]
        self.listed.append(listed)
        if self.ok:
            return cer.AnalyzerResult(True, "ok", len(listed), 5, 9)
        return cer.AnalyzerResult(False, "exit 1: boom")

    def record(self, *a):  # must NEVER be called by the queued drain
        raise AssertionError("refresh_queued must not record a commit")


def _cfg(extras) -> _Cfg:
    return _Cfg("pid-1", "Acme", tuple(extras))


def _drive_queued(tmp_path: Path, extras, rec: _Rec, **kw):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    lines: list = []
    deps = cer.Deps(
        resolve=lambda _root: _cfg(extras),
        run_analyzer=rec.run_analyzer,
        record=rec.record,
    )
    out = cer.refresh_queued(
        str(tmp_path), "/x/analyze.py", "/x/python", state, lines.append,
        deps=deps, **kw,
    )
    return out, lines, state


def _seed_queue(state: Path, extra: str, session: str, files: list[str]) -> Path:
    q = state / f"codegraph_drain_extra_{_md5(extra)}_{session}.txt"
    q.write_text("\n".join(files) + "\n")
    return q


def test_refresh_queued_drains_with_the_extras_own_args(tmp_path: Path) -> None:
    extra = "/srv/clone"
    rec = _Rec()
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [f"{extra}/src/a.py", f"{extra}/src/b.py"])
    out, lines, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "drained"}
    want_list = str(tmp_path / "state" / f"codegraph_drain_extra-files-{_md5(extra)}.txt")
    assert rec.runs == [
        cer.build_extra_sync_argv(extra, "Acme", False, None) + ["--only-files-from", want_list]
    ], rec.runs
    assert rec.listed == [[f"{extra}/src/a.py", f"{extra}/src/b.py"]]
    # Queue consumed, lock released, list file removed.
    assert not list(state.glob("codegraph_drain_extra_*.txt"))
    assert not list(state.glob("codegraph_drain_root_*.lock"))
    assert not (state / f"codegraph_drain_extra-files-{_md5(extra)}.txt").exists()
    assert any(ln.startswith(f"queued: done path={extra}") for ln in lines), lines


def test_refresh_queued_merges_all_sessions_into_one_run(tmp_path: Path) -> None:
    extra = "/srv/clone"
    rec = _Rec()
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [f"{extra}/a.py", f"{extra}/b.py"])
    _seed_queue(state, extra, "s2", [f"{extra}/b.py", f"{extra}/c.py"])
    out, _, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "drained"}
    assert len(rec.runs) == 1, rec.runs
    # Dedup keeps first-seen order across both sessions' queues.
    assert rec.listed == [[f"{extra}/a.py", f"{extra}/b.py", f"{extra}/c.py"]]


def test_refresh_queued_disabled_extra_is_never_drained(tmp_path: Path) -> None:
    """A DISABLED extra's queue is never analyzed (no write into a turned-off
    collection) — and since v0.2.102 L-A4 it is GC'd with a log line."""
    extra = "/srv/off"
    rec = _Rec()
    state = tmp_path / "state"
    state.mkdir()
    q = _seed_queue(state, extra, "s1", [f"{extra}/a.py"])
    out, lines, _ = _drive_queued(tmp_path, [_Extra(extra, False)], rec)
    assert out == {}
    assert rec.runs == []
    assert not q.exists(), "the orphan queue of a disabled extra is GC'd (L-A4)"
    assert any("gc orphan" in ln and _md5(extra) in ln for ln in lines), lines


def test_refresh_queued_gc_removes_orphans_but_keeps_enabled(tmp_path: Path) -> None:
    """L-A4 GC: queues whose key matches NO enabled extra are removed; a queue
    of a still-ENABLED extra is never deleted (here: its drain fails, the
    lines are restored, and GC must leave the restored queue in place). A
    leaked LEGACY transient list file (pre-fix-round-2 name) is GC'd too."""
    extra = "/srv/keep"
    rec = _Rec(ok=False)
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [f"{extra}/a.py"])
    orphan = state / f"codegraph_drain_extra_{_md5('/srv/gone')}_s9.txt"
    orphan.write_text("/srv/gone/x.py\n")
    legacy_list = state / f"codegraph_drain_extra_list_{_md5(extra)}.txt"
    legacy_list.write_text(f"{extra}/a.py\n")

    out, lines, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "analyzer_failed"}
    assert not orphan.exists(), "orphan queue must be GC'd"
    assert not legacy_list.exists(), "legacy transient list file must be GC'd"
    restored = state / f"codegraph_drain_extra_{_md5(extra)}_restored.txt"
    assert restored.exists(), "a STILL-ENABLED extra's queue is never GC'd"
    assert any("gc orphan" in ln for ln in lines), lines
    assert any("gc stale analyzer input" in ln for ln in lines), lines


def test_refresh_queued_held_lock_restores_the_queue(tmp_path: Path) -> None:
    extra = "/srv/clone"
    rec = _Rec()
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [f"{extra}/a.py"])
    (state / f"codegraph_drain_root_{_md5(extra)}.lock").mkdir()
    out, lines, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "busy"}
    assert rec.runs == []
    restored = state / f"codegraph_drain_extra_{_md5(extra)}_restored.txt"
    assert restored.exists() and f"{extra}/a.py" in restored.read_text()
    assert any("busy" in ln for ln in lines), lines


def test_refresh_queued_analyzer_failure_restores_the_queue(tmp_path: Path) -> None:
    extra = "/srv/clone"
    rec = _Rec(ok=False)
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [f"{extra}/a.py", f"{extra}/b.py"])
    out, lines, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "analyzer_failed"}
    restored = state / f"codegraph_drain_extra_{_md5(extra)}_restored.txt"
    assert restored.exists()
    assert restored.read_text().splitlines() == [f"{extra}/a.py", f"{extra}/b.py"]
    assert any("FAILED" in ln and "queue restored" in ln for ln in lines), lines
    # The lock is released even on failure.
    assert not list(state.glob("codegraph_drain_root_*.lock"))


def test_refresh_queued_filters_foreign_and_state_paths(tmp_path: Path) -> None:
    extra = "/srv/clone"
    rec = _Rec()
    state = tmp_path / "state"
    state.mkdir()
    _seed_queue(state, extra, "s1", [
        f"{extra}/a.py",
        "/srv/other/b.py",                       # not under the extra path
        f"{extra}/.claude/state/scratch.sh",     # transient scratch
        "/srv/cloned/x.py",                      # sibling prefix, NOT the extra
    ])
    out, _, _ = _drive_queued(tmp_path, [_Extra(extra, True)], rec)
    assert out == {extra: "drained"}
    assert rec.listed == [[f"{extra}/a.py"]]


def test_refresh_queued_hub_down_keeps_the_queues(tmp_path: Path) -> None:
    extra = "/srv/clone"
    state = tmp_path / "state"
    state.mkdir()
    q = _seed_queue(state, extra, "s1", [f"{extra}/a.py"])
    lines: list = []

    def _boom(_root):
        raise RuntimeError("hub down")

    out = cer.refresh_queued(
        str(tmp_path), "/x/a.py", "/x/py", state, lines.append,
        deps=cer.Deps(resolve=_boom),
    )
    assert out == {}
    assert q.exists(), "config unavailable must leave every queue in place"
    assert any("config unavailable" in ln for ln in lines)


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


def test_cli_queued_dispatches_refresh_queued(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    def _fake(root, analyzer, python, state_dir, log, **kw):
        seen.update(root=root, analyzer=analyzer, state_dir=state_dir)
        return {}

    monkeypatch.setattr(cer, "refresh_queued", _fake)
    rc = cer.main([
        "--project-root", str(tmp_path), "--queued",
        "--analyzer", "/x/analyze.py", "--state-dir", str(tmp_path / "st"),
    ])
    assert rc == 0
    assert seen["root"] == str(tmp_path) and seen["analyzer"] == "/x/analyze.py"
    assert seen["state_dir"] == tmp_path / "st"


def test_cli_run_modes_require_analyzer() -> None:
    with pytest.raises(SystemExit):
        cer.main(["--project-root", "/proj", "--queued"])


# ── bash fixtures: the routing decision (RED pre-fix) ──────────────────────

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


@_BASH_REQUIRED
def test_sh_route_extra_edit_lands_in_extra_queue(tmp_path: Path) -> None:
    """RED pre-fix: the edit used to land in the PRIMARY queue (or effectively
    nowhere useful); it must land in codegraph_drain_extra_<md5>_<sid>.txt."""
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    extra_queue = state / f"codegraph_drain_extra_{_md5(str(extra))}_sess1.txt"
    assert extra_queue.exists(), (
        f"the extra queue must exist; state dir holds: "
        f"{sorted(p.name for p in state.iterdir())}"
    )
    assert str(touched) in extra_queue.read_text()
    assert not (state / "codegraph_drain_sess1.txt").exists(), (
        "an extra-path edit must NEVER enter the primary queue"
    )
    # The session reminder accumulator still records the edit.
    assert str(touched) in (state / "edit_reminder_sess1.txt").read_text()


@_BASH_REQUIRED
def test_sh_route_primary_edit_stays_in_primary_queue(tmp_path: Path) -> None:
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = proj / "main.py"
    touched.write_text("y = 2\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    assert str(touched) in (state / "codegraph_drain_sess1.txt").read_text()
    assert not list(state.glob("codegraph_drain_extra_*.txt"))


@_BASH_REQUIRED
def test_sh_route_path_under_disabled_extra_is_not_extra_queued(tmp_path: Path) -> None:
    """A DISABLED extra row never reaches the cache (enabled-only list), so a
    path under it keeps the pre-existing primary-queue behaviour and is NOT
    queued to any extra queue."""
    proj, extra, _ = _sh_route_project(tmp_path)
    disabled_extra = tmp_path / "extra-disabled"
    (disabled_extra / "src").mkdir(parents=True)
    touched = disabled_extra / "src" / "off.py"
    touched.write_text("z = 3\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    assert not list(state.glob("codegraph_drain_extra_*.txt")), (
        "a path under a DISABLED extra row must not be extra-queued"
    )
    assert str(touched) in (state / "codegraph_drain_sess1.txt").read_text()


@_BASH_REQUIRED
def test_sh_route_stale_cache_refreshes_through_the_lister(tmp_path: Path) -> None:
    """TTL=0 forces a refresh; the venv shim (VCT_VENV tier 1) answers the
    --list-enabled-extra-paths CLI with the extra path. The refreshed cache
    then routes the edit into the extra queue."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    # No cache at all: the refresh must create it.

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
    extra_queue = state / f"codegraph_drain_extra_{_md5(str(extra))}_sess1.txt"
    assert extra_queue.exists() and str(touched) in extra_queue.read_text()


# ── M-A1 precedence, L-A2 symlinks, L-A3 trailing slash (bash + pwsh) ─────


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
def test_sh_route_primary_root_wins_over_ancestor_extra(tmp_path: Path) -> None:
    """M-A1 RED arm 1: an extra that is an ANCESTOR of the project root must
    NOT hijack primary edits — the edit lands in the primary queue."""
    parent = tmp_path / "parent"
    parent.mkdir()
    proj, state = _route_project_with_cache(tmp_path, [_real(parent)], rel_proj="parent/proj")
    touched = proj / "main.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched) in (state / "codegraph_drain_sess1.txt").read_text(), (
        "primary-root containment must win over an ancestor extra"
    )
    assert not list(state.glob("codegraph_drain_extra_*.txt")), (
        f"no extra queue may be written: {sorted(p.name for p in state.iterdir())}"
    )


@_BASH_REQUIRED
def test_sh_route_longest_extra_prefix_wins(tmp_path: Path) -> None:
    """M-A1 RED arm 2: nested extras — the INNER (longest) prefix gets the
    edit regardless of cache order (outer is listed first here)."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "src").mkdir(parents=True)
    proj, state = _route_project_with_cache(
        tmp_path, [_real(outer), _real(inner)])
    touched = inner / "src" / "x.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    inner_q = state / f"codegraph_drain_extra_{_md5(_real(inner))}_sess1.txt"
    outer_q = state / f"codegraph_drain_extra_{_md5(_real(outer))}_sess1.txt"
    assert inner_q.exists() and _real(touched) in inner_q.read_text(), (
        f"the longest prefix must win: {sorted(p.name for p in state.iterdir())}"
    )
    assert not outer_q.exists()


@_BASH_REQUIRED
def test_sh_route_symlinked_edit_path_is_resolved(tmp_path: Path) -> None:
    """L-A2 RED: an edit THROUGH a symlinked directory prefix-matches the
    canonical extra, and the RESOLVED path is what gets queued."""
    extra = tmp_path / "real-extra"
    (extra / "src").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(extra, link)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra)])
    (extra / "src" / "foo.py").write_text("x = 1\n")
    touched_via_link = link / "src" / "foo.py"
    r = _run_route_sh(proj, touched_via_link, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    extra_queue = state / f"codegraph_drain_extra_{_md5(_real(extra))}_sess1.txt"
    assert extra_queue.exists(), (
        f"a symlinked edit path must still route: {sorted(p.name for p in state.iterdir())}"
    )
    assert os.path.realpath(touched_via_link) in extra_queue.read_text(), (
        "the queue must carry the RESOLVED path (the analyzer's --only-files-from "
        "input must sit under the canonical extra path)"
    )
    assert not (state / "codegraph_drain_sess1.txt").exists()


@_BASH_REQUIRED
def test_sh_route_trailing_slash_cache_line_matches(tmp_path: Path) -> None:
    """L-A3 RED: a cache line with a trailing separator routes identically on
    both siblings; the queue is keyed by the RAW line (md5 with the slash —
    the Python drain's path_key(row.path) glob must find it)."""
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    raw_line = _real(extra) + "/"
    proj, state = _route_project_with_cache(tmp_path, [raw_line])
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_sh(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    extra_queue = state / f"codegraph_drain_extra_{_md5(raw_line)}_sess1.txt"
    assert extra_queue.exists(), (
        "a trailing-slash cache line must match (parity with the .ps1 sibling): "
        f"{sorted(p.name for p in state.iterdir())}"
    )


@_PS1_REQUIRED
def test_ps1_route_primary_root_wins_over_ancestor_extra(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    proj, state = _route_project_with_cache(tmp_path, [_real(parent)], rel_proj="parent/proj")
    touched = proj / "main.py"
    touched.write_text("x = 1\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert str(touched) in (state / "codegraph_drain_sess1.txt").read_text()
    assert not list(state.glob("codegraph_drain_extra_*.txt"))


@_PS1_REQUIRED
def test_ps1_route_longest_extra_prefix_wins(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "src").mkdir(parents=True)
    proj, state = _route_project_with_cache(
        tmp_path, [_real(outer), _real(inner)])
    touched = inner / "src" / "x.py"
    touched.write_text("x = 1\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    inner_q = state / f"codegraph_drain_extra_{_md5(_real(inner))}_sess1.txt"
    outer_q = state / f"codegraph_drain_extra_{_md5(_real(outer))}_sess1.txt"
    assert inner_q.exists() and _real(touched) in inner_q.read_text()
    assert not outer_q.exists()


@_PS1_REQUIRED
def test_ps1_route_symlinked_edit_path_is_resolved(tmp_path: Path) -> None:
    extra = tmp_path / "real-extra"
    (extra / "src").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(extra, link)
    proj, state = _route_project_with_cache(tmp_path, [_real(extra)])
    (extra / "src" / "foo.py").write_text("x = 1\n")
    touched_via_link = link / "src" / "foo.py"
    r = _run_route_ps1(proj, touched_via_link, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    extra_queue = state / f"codegraph_drain_extra_{_md5(_real(extra))}_sess1.txt"
    assert extra_queue.exists(), (
        f"a symlinked edit path must still route: {sorted(p.name for p in state.iterdir())}"
    )
    assert os.path.realpath(touched_via_link) in extra_queue.read_text()


@_PS1_REQUIRED
def test_ps1_route_trailing_slash_cache_line_matches(tmp_path: Path) -> None:
    extra = tmp_path / "extra-repo"
    (extra / "src").mkdir(parents=True)
    raw_line = _real(extra) + "/"
    proj, state = _route_project_with_cache(tmp_path, [raw_line])
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    r = _run_route_ps1(proj, touched, "sess1", _route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    extra_queue = state / f"codegraph_drain_extra_{_md5(raw_line)}_sess1.txt"
    assert extra_queue.exists(), (
        f"trailing-slash parity: {sorted(p.name for p in state.iterdir())}"
    )


# ── bash fixtures: the Stop drain spawns --queued ─────────────────────────


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
    a fresh commit-stamp check ts so ONLY the --queued spawn can fire; the
    NB-H5 test disables it because the commit-stamp spawn IS the surface
    under test."""
    state = root / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    spawn_log = tmp_path / "spawned.log"
    shim = tmp_path / "py.sh"
    _shim_python_per_arg(shim, spawn_log)
    stub = tmp_path / "analyze.py"
    stub.write_text("")
    now = str(int(time.time()))
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


@_BASH_REQUIRED
def test_sh_drain_spawns_queued_refresh_inside_the_rate_limit(tmp_path: Path) -> None:
    """RED pre-fix: an extra edit queue is drained on Stop even inside the
    120 s primary rate-limit window (the throttle guards FULL walks; a queued
    scoped drain is cheap)."""
    root = tmp_path / "proj"
    fx = _drain_fixture(tmp_path, root)
    state = fx["state"]
    session = "sessQ"
    extra = "/srv/clone"
    (state / f"codegraph_drain_extra_{_md5(extra)}_{session}.txt").write_text(
        f"{extra}/a.py\n"
    )
    # Primary queue present + fresh last-sync → the primary drain is
    # rate-limited; the extra drain must still spawn.
    primary = state / f"codegraph_drain_{session}.txt"
    primary.write_text(f"{root}/m.py\n")
    (state / "codegraph_drain_last_sync.ts").write_text(fx["now"])

    r = subprocess.run(["bash", str(DRAIN_SH)],
                       input=json.dumps({"session_id": session}),
                       capture_output=True, text=True, timeout=60,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    refresh = [b for b in blocks if "--queued" in b]
    assert refresh, f"the extra-queue drain must spawn --queued: {blocks}"
    b0 = refresh[0]
    assert b0[:2] == ["-m", "vco_lib.codegraph_extras_refresh"], b0
    assert "--project-root" in b0 and str(root) in b0, b0
    # The primary drain stayed rate-limited: the analyzer itself never ran
    # (its block would START with the stub path as the script arg; the
    # refresh spawn names the stub only as the --analyzer VALUE).
    analyzer_runs = [b for b in blocks if b[0] == str(fx["stub"])]
    assert analyzer_runs == [], (
        f"rate-limited primary drain must not run the analyzer: {blocks}"
    )
    assert primary.exists(), "the rate-limited primary queue is left for a later drain"
    # The extra queue persists until the (detached) refresh_queued consumes it.
    assert (state / f"codegraph_drain_extra_{_md5(extra)}_{session}.txt").exists()


@_BASH_REQUIRED
def test_sh_drain_without_extra_queues_never_spawns_queued(tmp_path: Path) -> None:
    """Leave-alone: a primary-only turn drains exactly as before (rate limit
    0 → the analyzer runs) and no --queued spawn appears."""
    if not shutil.which("git"):
        pytest.skip("git required for the canonical-root leg")
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True,
                   capture_output=True, env=genv)
    fx = _drain_fixture(tmp_path, repo)
    state = fx["state"]
    session = "sessP"
    f = repo / "a.py"
    f.write_text("a=1\n")
    (state / f"codegraph_drain_{session}.txt").write_text(f"{f}\n")

    r = subprocess.run(["bash", str(DRAIN_SH)],
                       input=json.dumps({"session_id": session}),
                       capture_output=True, text=True, timeout=60,
                       env=_drain_env(repo, fx, VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS="0"))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    assert blocks, "the primary drain must run the analyzer (via the shim)"
    assert blocks[0][0] == str(fx["stub"]), (
        f"the primary analyzer run leads with the analyzer script: {blocks}"
    )
    assert all("--queued" not in b for b in blocks), blocks


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


# ── NB-H5 round 2: the PRIMARY drain's _snip gets the same argv treatment ──


def _git_init(root: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True,
                   capture_output=True, env=env)


@_BASH_REQUIRED
def test_sh_drain_snip_evil_canonical_root_runs_no_command_substitution(tmp_path: Path) -> None:
    """RED pre-fix-round-2: the primary drain's `_snip` interpolated
    `$_canon`/`$_lock`/`$_list` into the double-quoted sh -c string — the
    NB-H5 class on the PRIMARY path, not just the extras spawn."""
    if not shutil.which("git"):
        pytest.skip("git required for the canonical-root leg")
    pwned = tmp_path / "PWNED2"
    evil = tmp_path / f'canon $(touch {pwned}) "z"'
    evil.mkdir(parents=True)
    _git_init(evil)
    fx = _drain_fixture(tmp_path, evil)  # commit-stamp spawn suppressed
    state = fx["state"]
    session = "sessS"
    f = evil / "a.py"
    f.write_text("a=1\n")
    (state / f"codegraph_drain_{session}.txt").write_text(f"{f}\n")

    r = subprocess.run(["bash", str(DRAIN_SH)],
                       input=json.dumps({"session_id": session}),
                       capture_output=True, text=True, timeout=60,
                       env=_drain_env(evil, fx, VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS="0"))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    assert not pwned.exists(), (
        "command substitution in the canonical root executed — the primary "
        "drain's _snip re-parsed $_canon (NB-H5, fix-round 2)"
    )
    flat = [ln for b in blocks for ln in b]
    assert str(evil) in flat, (
        f"the literal evil canonical root must arrive as single argv elements: {blocks}"
    )
    # The analyzer ran (via the shim) with the script as its first argument.
    assert blocks[0][0] == str(fx["stub"]), blocks


@_BASH_REQUIRED
def test_sh_drain_ignores_a_leaked_legacy_list_file(tmp_path: Path) -> None:
    """I-A5 RED: a leaked pre-fix-round-2 transient analyzer input
    (codegraph_drain_extra_list_<md5>.txt — from a killed mid-drain process)
    is NOT a queue and must not trigger the --queued spawn on every Stop."""
    root = tmp_path / "proj"
    root.mkdir()
    fx = _drain_fixture(tmp_path, root)
    (fx["state"] / f"codegraph_drain_extra_list_{_md5('/srv/clone')}.txt").write_text(
        "/srv/clone/a.py\n"
    )
    r = subprocess.run(["bash", str(DRAIN_SH)],
                       input=json.dumps({"session_id": "sessL"}),
                       capture_output=True, text=True, timeout=60,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)  # give a (wrongly) spawned child a beat to appear
    assert _spawn_blocks(fx["spawn_log"]) == [], (
        f"a leaked transient list file must not spawn the refresh: "
        f"{_spawn_blocks(fx['spawn_log'])}"
    )


@_PS1_REQUIRED
def test_ps1_drain_ignores_a_leaked_legacy_list_file(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    fx = _drain_fixture(tmp_path, root)
    (fx["state"] / f"codegraph_drain_extra_list_{_md5('/srv/clone')}.txt").write_text(
        "/srv/clone/a.py\n"
    )
    r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-File", str(DRAIN_PS1)],
                       input=json.dumps({"session_id": "sessL"}),
                       capture_output=True, text=True, timeout=120,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)
    assert _spawn_blocks(fx["spawn_log"]) == [], (
        f"a leaked transient list file must not spawn the refresh: "
        f"{_spawn_blocks(fx['spawn_log'])}"
    )


# ── pwsh runtime legs (the original S16-7 round-1 ones) ───────────────────


@_PS1_REQUIRED
def test_ps1_route_extra_edit_lands_in_extra_queue(tmp_path: Path) -> None:
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = extra / "src" / "foo.py"
    touched.write_text("x = 1\n")
    cmd = (
        f". '{ROUTE_LIB_PS1}'; "
        f"Initialize-VcoRoute -HooksDir '{HOOKS_DIR}' -ProjectRoot '{proj}'; "
        f"Invoke-VcoRouteTouchedPath -Path '{touched}' -SessionId 'sess1'"
    )
    r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", cmd],
                       capture_output=True, text=True, timeout=120,
                       env=_route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    extra_queue = state / f"codegraph_drain_extra_{_md5(str(extra))}_sess1.txt"
    assert extra_queue.exists(), (
        f"the extra queue must exist; state dir holds: "
        f"{sorted(p.name for p in state.iterdir())}"
    )
    assert str(touched) in extra_queue.read_text()
    assert not (state / "codegraph_drain_sess1.txt").exists()


@_PS1_REQUIRED
def test_ps1_route_primary_edit_stays_in_primary_queue(tmp_path: Path) -> None:
    proj, extra, _ = _sh_route_project(tmp_path)
    touched = proj / "main.py"
    touched.write_text("y = 2\n")
    cmd = (
        f". '{ROUTE_LIB_PS1}'; "
        f"Initialize-VcoRoute -HooksDir '{HOOKS_DIR}' -ProjectRoot '{proj}'; "
        f"Invoke-VcoRouteTouchedPath -Path '{touched}' -SessionId 'sess1'"
    )
    r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", cmd],
                       capture_output=True, text=True, timeout=120,
                       env=_route_env(tmp_path))
    assert r.returncode == 0, r.stderr
    state = proj / ".claude" / "state"
    assert str(touched) in (state / "codegraph_drain_sess1.txt").read_text()
    assert not list(state.glob("codegraph_drain_extra_*.txt"))


@_PS1_REQUIRED
def test_ps1_drain_spawns_queued_refresh_inside_the_rate_limit(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    fx = _drain_fixture(tmp_path, root)
    state = fx["state"]
    session = "sessQ"
    extra = "/srv/clone"
    (state / f"codegraph_drain_extra_{_md5(extra)}_{session}.txt").write_text(
        f"{extra}/a.py\n"
    )
    primary = state / f"codegraph_drain_{session}.txt"
    primary.write_text(f"{root}/m.py\n")
    (state / "codegraph_drain_last_sync.ts").write_text(fx["now"])

    r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-File", str(DRAIN_PS1)],
                       input=json.dumps({"session_id": session}),
                       capture_output=True, text=True, timeout=120,
                       env=_drain_env(root, fx))
    assert r.returncode == 0, r.stderr
    blocks = _wait_blocks(fx["spawn_log"], 1)
    refresh = [b for b in blocks if "--queued" in b]
    assert refresh, f"the extra-queue drain must spawn --queued: {blocks}"
    analyzer_runs = [b for b in blocks if b[0] == str(fx["stub"])]
    assert analyzer_runs == [], (
        f"rate-limited primary drain must not run the analyzer: {blocks}"
    )
    assert primary.exists()
