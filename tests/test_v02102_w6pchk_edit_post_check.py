# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-PCHK: the edit_outcome row's ``post_check`` is real.

The ``post-edit-outcome`` header promised since v0.2.52 that a forked
background poller fills ``post_check`` from status records written by the
PostToolUse checkers; nothing wrote them and every row carried ``None``.
``vco_lib/edit_post_check.py`` makes the promise true. These tests pin:

* a passing compile fills ``post_check`` ok, a failing one fail;
* an edit no checker covers gets ``no_signal`` at once (no window wait);
* a poller that crashes / cannot start / dies outright never drops the row;
* two same-named files in different directories never share a status;
* a subagent's check is never credited to the main chat's edit;
* both hook siblings (``.sh`` and ``.ps1``), end to end.

Red-proof (2026-10-13, against the pre-lane hooks restored from
/tmp/w6pchk_aside): every hook-level case below fails — no status file is
ever written and every emitted row has ``post_check: None``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.pwsh import PWSH_BASE_FLAGS  # noqa: E402
from vco_lib import edit_post_check as epc  # noqa: E402

HOOKS = REPO_ROOT / "templates" / "hooks"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
FLAVOURS = ["sh", "ps1"]


# ── helpers ────────────────────────────────────────────────────────────────


class Recorder:
    """An ``emit_outcome_event`` stand-in that keeps every call."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, **kw):
        self.rows.append(kw)
        return True


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".claude" / "state").mkdir(parents=True)
    (p / "src").mkdir()
    return p


def _record(project: Path, fp: Path, *, tool="Write", emit=None, spawn=None,
            agent="", session="sessP", env=None) -> tuple[str, Recorder]:
    rec = emit or Recorder()
    how = epc.record_edit_outcome(
        project_root=str(project), session_id=session, agent_id=agent,
        tool_name=tool, file_path=str(fp), diff_size=3, file_existed_before=True,
        ts_ms=int(time.time() * 1000), emit=rec,
        spawn=spawn or (lambda pending, root: None), env=env or {})
    return how, rec


def _pending(project: Path) -> list[Path]:
    d = epc.pending_dir(str(project))
    return sorted(d.iterdir()) if d.is_dir() else []


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


# ── coverage, keys, window ─────────────────────────────────────────────────


def test_coverage_table(tmp_path):
    project = _project(tmp_path)
    inside = str(project / "src" / "m.py")
    assert epc.covering_checker("Write", inside, str(project)) == "py_compile"
    assert epc.covering_checker("Edit", inside, str(project)) is None, (
        "py-compile-check is registered on Write only")
    assert epc.covering_checker("Write", str(project / "src" / "m.md"), str(project)) is None
    assert epc.covering_checker("Write", str(project / "src" / "m.PY"), str(project)) is None, (
        "coverage must be exactly the hook's case-sensitive .py gate")
    outside = tmp_path / "elsewhere.py"
    assert epc.covering_checker("Write", str(outside), str(project)) is None, (
        "a file outside the project must not write into this project's state")


def test_coverage_matches_the_shipped_checker_registration():
    """CHECKERS must name exactly the checkers the settings templates
    register: py-compile-check, PostToolUse, matcher ``Write``."""
    for tmpl in ("linux", "windows"):
        data = json.loads((REPO_ROOT / "templates" / f"settings.json.{tmpl}.template")
                          .read_text(encoding="utf-8"))
        matchers = [g.get("matcher") for g in data["hooks"]["PostToolUse"]
                    for h in g["hooks"] if "py-compile-check" in h.get("command", "")]
        assert matchers == ["Write"], (tmpl, matchers)
    assert [(c.name, c.tools, c.suffixes) for c in epc.CHECKERS] == [
        ("py_compile", ("Write",), (".py",))]
    assert 'endswith(".py")' in (HOOKS / "py-compile-check.sh").read_text(encoding="utf-8")
    assert "EndsWith('.py')" in (HOOKS / "py-compile-check.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("raw,want", [(None, 20), ("5", 5), ("abc", 20), ("9999", 300),
                                      ("-1", 0), ("0", 0)])
def test_window_secs(raw, want):
    env = {} if raw is None else {epc.ENV_WINDOW: raw}
    assert epc.window_secs(env) == want


def test_same_named_files_in_different_dirs_do_not_collide(tmp_path):
    project = _project(tmp_path)
    a = project / "a" / "mod.py"
    b = project / "b" / "mod.py"
    for f in (a, b):
        f.parent.mkdir()
        f.write_text("x = 1\n")
    pa = epc.write_status(project_root=str(project), file_path=str(a), checker="py_compile",
                          tool_name="Write", exit_code=0, session_id="s", agent_id="")
    pb = epc.write_status(project_root=str(project), file_path=str(b), checker="py_compile",
                          tool_name="Write", exit_code=1, session_id="s", agent_id="")
    assert pa is not None and pb is not None and pa != pb
    assert epc.read_status(str(project), str(a), "s", "")["ok"] is True
    assert epc.read_status(str(project), str(b), "s", "")["ok"] is False


def test_status_record_is_short_and_carries_no_contents(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "secret.py"
    f.write_text("TOKEN = 'do-not-copy'\n")
    p = epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                         tool_name="Write", exit_code=0, session_id="s", agent_id="")
    data = json.loads(p.read_text())
    assert set(data) == {"v", "file_path", "checker", "ok", "exit_code", "ts_ms"}
    assert "do-not-copy" not in p.read_text()


def test_uncovered_status_is_not_written(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "notes.md"
    assert epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                            tool_name="Write", exit_code=0, session_id="s", agent_id="") is None
    assert not list((project / ".claude" / "state").glob(f"{epc.STATUS_PREFIX}*"))


def test_status_is_keyed_per_agent(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                     tool_name="Write", exit_code=0, session_id="s", agent_id="abc123")
    assert epc.read_status(str(project), str(f), "s", "abc123") is not None
    assert epc.read_status(str(project), str(f), "s", "") is None, (
        "a subagent's check must not be credited to the main chat's edit")


# ── the wait ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code,status", [(0, "ok"), (1, "fail")])
def test_await_fills_ok_and_fail(tmp_path, code, status):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    since = epc.edit_reference_ms(str(f), int(time.time() * 1000))
    epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                     tool_name="Write", exit_code=code, session_id="s", agent_id="")
    pc = epc.await_post_check(project_root=str(project), file_path=str(f), session_id="s",
                              agent_id="", checker="py_compile", since_ms=since, window_s=5)
    assert pc["status"] == status and pc["exit_code"] == code and pc["checker"] == "py_compile"


def test_a_status_older_than_the_edit_is_not_this_edits_result(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                     tool_name="Write", exit_code=0, session_id="s", agent_id="",
                     now_ms=1000)
    clock = FakeClock()
    pc = epc.await_post_check(project_root=str(project), file_path=str(f), session_id="s",
                              agent_id="", checker="py_compile", since_ms=5000, window_s=3,
                              sleep=clock.sleep, clock=clock)
    assert pc == {"status": "no_signal", "reason": "timeout", "checker": "py_compile",
                  "window_s": 3}
    assert clock.t >= 3


# ── record / poller / sweeper ──────────────────────────────────────────────


def test_unchecked_file_type_is_no_signal_at_once_without_a_poller(tmp_path):
    project = _project(tmp_path)
    spawned: list = []
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    t0 = time.monotonic()
    how, rec = _record(project, f, tool="Edit", spawn=lambda p, r: spawned.append(p),
                       env={epc.ENV_WINDOW: "60"})
    assert how == "inline" and not spawned and not _pending(project)
    assert time.monotonic() - t0 < 5, "no_signal must not wait for the window"
    [row] = rec.rows
    assert row["event_type"] == "edit_outcome"
    assert row["payload"]["post_check"] == {"status": "no_signal", "reason": "no_checker"}


@pytest.mark.parametrize("code,status", [(0, "ok"), (1, "fail")])
def test_covered_edit_goes_to_a_poller_that_fills_post_check(tmp_path, code, status):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    spawned: list[Path] = []
    how, rec = _record(project, f, spawn=lambda p, r: spawned.append(p))
    assert how == "detached" and len(spawned) == 1 and not rec.rows, (
        "the hook must not emit a covered row itself: the poller owns it")
    epc.write_status(project_root=str(project), file_path=str(f), checker="py_compile",
                     tool_name="Write", exit_code=code, session_id="sessP", agent_id="")
    out = Recorder()
    assert epc.run_poller(spawned[0], emit=out) == "emitted"
    [row] = out.rows
    assert row["payload"]["post_check"]["status"] == status
    assert row["payload"]["file_path"] == str(f)
    assert not _pending(project), "the pending record is consumed"


def test_poller_crash_still_emits_the_row(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    spawned: list[Path] = []
    _record(project, f, spawn=lambda p, r: spawned.append(p))

    def boom(**_kw):
        raise RuntimeError("poller blew up")

    out = Recorder()
    assert epc.run_poller(spawned[0], emit=out, awaiter=boom) == "emitted_none"
    [row] = out.rows
    assert row["payload"]["post_check"] is None
    assert row["payload"]["agent_id"] == "" and row["task_id"].startswith("edit_outcome_")


def test_poller_that_cannot_start_emits_inline_once(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")

    def no_spawn(pending, root):
        raise OSError("fork failed")

    how, rec = _record(project, f, spawn=no_spawn)
    assert how == "inline_fallback"
    [row] = rec.rows
    assert row["payload"]["post_check"] is None
    assert not _pending(project)


def test_dead_poller_row_is_swept_once_and_a_late_poller_does_not_duplicate(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    spawned: list[Path] = []
    _record(project, f, spawn=lambda p, r: spawned.append(p), env={epc.ENV_WINDOW: "1"})
    swept = Recorder()
    # Leave-alone: inside window + grace the poller still owns the row.
    assert epc.sweep_stale_pending(str(project), swept) == 0 and not swept.rows
    later = int(time.time() * 1000) + (1 + epc.SWEEP_GRACE_S + 5) * 1000
    assert epc.sweep_stale_pending(str(project), swept, now_ms=later) == 1
    [row] = swept.rows
    assert row["payload"]["post_check"] is None
    late = Recorder()
    assert epc.run_poller(spawned[0], emit=late) in ("unreadable", "lost_claim")
    assert not late.rows, "exactly one process emits each row"


def test_claim_left_by_a_dead_process_is_swept(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "m.py"
    f.write_text("x = 1\n")
    spawned: list[Path] = []
    _record(project, f, spawn=lambda p, r: spawned.append(p))
    dead_pid = 2 ** 22 + 12345  # above the default pid_max: provably not alive
    claimed = spawned[0].with_name(f"{spawned[0].name}{epc.CLAIM_MARK}{dead_pid}")
    os.replace(spawned[0], claimed)
    out = Recorder()
    assert epc.sweep_stale_pending(str(project), out) == 1
    assert out.rows[0]["payload"]["post_check"] is None
    assert not _pending(project)


def test_old_status_records_are_pruned_and_fresh_ones_kept(tmp_path):
    project = _project(tmp_path)
    old, fresh = project / "src" / "old.py", project / "src" / "fresh.py"
    paths = []
    for f in (old, fresh):
        f.write_text("x = 1\n")
        paths.append(epc.write_status(project_root=str(project), file_path=str(f),
                                      checker="py_compile", tool_name="Write", exit_code=0,
                                      session_id="s", agent_id=""))
    unrelated = project / ".claude" / "state" / "something_else.json"
    unrelated.write_text("{}")
    stale = time.time() - epc.STATUS_TTL_S - 60
    os.utime(paths[0], (stale, stale))
    os.utime(unrelated, (stale, stale))
    assert epc.prune_old_status(str(project)) == 1
    assert not paths[0].exists(), "a stale status must be swept (act)"
    assert paths[1].exists(), "a fresh status must be kept (leave-alone)"
    assert unrelated.exists(), "only this module's records are touched"


def test_argv_entry_parses_key_value_items(tmp_path):
    project = _project(tmp_path)
    f = project / "src" / "it's here.md"  # a quote no longer breaks anything
    rec = Recorder()
    how = epc.record_edit_outcome_from_argv(
        ["tool=Write", f"file={f}", "diff=12", "existed=0", "ts=1700000000000",
         "agent=", "session=sessX"], project_root=str(project), emit=rec)
    assert how == "inline"
    p = rec.rows[0]["payload"]
    assert (p["tool_name"], p["file_path"], p["diff_size"], p["file_existed_before"],
            p["ts_ms"], p["agent_id"]) == ("Write", str(f), 12, False, 1700000000000, "")
    assert rec.rows[0]["session_id"] == "sessX"


# ── both hook siblings, end to end ─────────────────────────────────────────


def _need(flavour: str) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX fake venv")
    if flavour == "sh" and not BASH:
        pytest.skip("bash required")
    if flavour == "ps1" and not PWSH:
        pytest.skip("pwsh required")


def _fake_venv(tmp_path: Path, sink: Path) -> Path:
    """A VCT_VENV whose python sees THIS checkout's vco_lib but a fake
    ``emit_outcome_event`` that appends each row to ``sink``. The detached
    poller inherits the same PYTHONPATH, so it uses the fake too."""
    fake = tmp_path / "fakepkgs"
    mod = fake / "claude_mcp_servers" / "rl_client" / "outcome_emit.py"
    mod.parent.mkdir(parents=True)
    (fake / "claude_mcp_servers" / "__init__.py").touch()
    (mod.parent / "__init__.py").touch()
    mod.write_text("import json\n"
                   "def emit_outcome_event(**kw):\n"
                   f"    with open({str(sink)!r}, 'a') as fh:\n"
                   "        fh.write(json.dumps(kw) + '\\n')\n"
                   "    return True\n", encoding="utf-8")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f'#!/bin/sh\nPYTHONPATH="{fake}{os.pathsep}{REPO_ROOT}" '
                  f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return venv


def _env(tmp_path: Path, project: Path, venv: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT",
                        "PYTHONPATH", "KG_BASE_DIR")}
    env.update(CLAUDE_PROJECT_DIR=str(project), VCT_VENV=str(venv),
               VCT_INSTALL_ROOT=str(REPO_ROOT), VCT_STATE_DIR=str(tmp_path / "vct"),
               VCT_HUB_PORT="9", WEAVIATE_URL="http://127.0.0.1:9", **extra)
    return env


def _run(name: str, flavour: str, payload: dict, project: Path, env: dict):
    script = HOOKS / f"{name}.{flavour}"
    argv = (["bash", str(script)] if flavour == "sh"
            else [PWSH, *PWSH_BASE_FLAGS, "-File", str(script)])
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          env=env, cwd=str(project), timeout=120)


def _rows(sink: Path) -> list[dict]:
    if not sink.exists():
        return []
    return [json.loads(ln) for ln in sink.read_text().splitlines() if ln.strip()]


def _wait_rows(sink: Path, n: int, timeout: float = 45.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(sink)
        if len(rows) >= n:
            return rows
        time.sleep(0.2)
    return _rows(sink)


def _write_payload(fp: Path, tool: str = "Write") -> dict:
    ti = {"file_path": str(fp), "content": fp.read_text()} if tool == "Write" else \
        {"file_path": str(fp), "old_string": "x", "new_string": "yy"}
    return {"session_id": "sessH", "tool_name": tool, "tool_input": ti}


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("body,status", [("x = 1\n", "ok"), ("def (:\n", "fail")])
def test_hooks_fill_post_check_from_the_compile(tmp_path, flavour, body, status):
    _need(flavour)
    project = _project(tmp_path)
    sink = tmp_path / "rows.jsonl"
    env = _env(tmp_path, project, _fake_venv(tmp_path, sink), VCT_EDIT_POST_CHECK_SECS="40")
    fp = project / "src" / "mod.py"
    fp.write_text(body)
    payload = _write_payload(fp)
    # The outcome hook first: its poller must WAIT for the checker.
    r = _run("post-edit-outcome", flavour, payload, project, env)
    assert r.returncode == 0, r.stderr
    assert not _rows(sink), "a covered row is the poller's to emit, after the check"
    r = _run("py-compile-check", flavour, payload, project, env)
    assert r.returncode == 0, r.stderr
    assert ("py_compile:" in r.stdout) == (status == "fail"), r.stdout
    assert "compile status not recorded" not in r.stderr, r.stderr
    rows = _wait_rows(sink, 1)
    assert len(rows) == 1, (rows, r.stderr)
    pc = rows[0]["payload"]["post_check"]
    assert pc["status"] == status and pc["checker"] == "py_compile", pc
    assert not _pending(project)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hook_unchecked_edit_is_no_signal_before_the_hook_returns(tmp_path, flavour):
    _need(flavour)
    project = _project(tmp_path)
    sink = tmp_path / "rows.jsonl"
    env = _env(tmp_path, project, _fake_venv(tmp_path, sink), VCT_EDIT_POST_CHECK_SECS="120")
    fp = project / "src" / "mod.py"
    fp.write_text("x = 1\n")
    t0 = time.monotonic()
    r = _run("post-edit-outcome", flavour, _write_payload(fp, tool="Edit"), project, env)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 < 60
    rows = _rows(sink)
    assert len(rows) == 1, r.stderr
    assert rows[0]["payload"]["post_check"] == {"status": "no_signal", "reason": "no_checker"}


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hooks_same_named_files_in_two_dirs_get_their_own_result(tmp_path, flavour):
    _need(flavour)
    project = _project(tmp_path)
    sink = tmp_path / "rows.jsonl"
    env = _env(tmp_path, project, _fake_venv(tmp_path, sink), VCT_EDIT_POST_CHECK_SECS="40")
    good = project / "a" / "mod.py"
    bad = project / "b" / "mod.py"
    for f, body in ((good, "x = 1\n"), (bad, "def (:\n")):
        f.parent.mkdir()
        f.write_text(body)
        assert _run("post-edit-outcome", flavour, _write_payload(f), project, env).returncode == 0
    for f in (good, bad):
        assert _run("py-compile-check", flavour, _write_payload(f), project, env).returncode == 0
    statuses = sorted((project / ".claude" / "state").glob(f"{epc.STATUS_PREFIX}*.json"))
    assert len(statuses) == 2, statuses
    rows = _wait_rows(sink, 2)
    got = {r["payload"]["file_path"]: r["payload"]["post_check"]["status"] for r in rows}
    assert got == {str(good): "ok", str(bad): "fail"}, rows


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hook_run_sweeps_a_dead_pollers_row(tmp_path, flavour):
    """A poller that died outright left its pending record; the next hook
    run emits that row (post_check None) — the row is never dropped."""
    _need(flavour)
    project = _project(tmp_path)
    sink = tmp_path / "rows.jsonl"
    env = _env(tmp_path, project, _fake_venv(tmp_path, sink))
    orphan = project / "src" / "orphan.py"
    orphan.write_text("x = 1\n")
    spawned: list[Path] = []
    _record(project, orphan, spawn=lambda p, r: spawned.append(p), env={epc.ENV_WINDOW: "1"})
    rec = json.loads(spawned[0].read_text())
    rec["created_ms"] -= (1 + epc.SWEEP_GRACE_S + 5) * 1000
    spawned[0].write_text(json.dumps(rec))
    fp = project / "src" / "now.py"
    fp.write_text("x = 1\n")
    r = _run("post-edit-outcome", flavour, _write_payload(fp, tool="Edit"), project, env)
    assert r.returncode == 0, r.stderr
    rows = {row["payload"]["file_path"]: row["payload"]["post_check"] for row in _rows(sink)}
    assert rows == {str(orphan): None,
                    str(fp): {"status": "no_signal", "reason": "no_checker"}}, rows
    assert not _pending(project)
