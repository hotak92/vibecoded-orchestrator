# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — three defects from a real dogfood update (2026-10-16).

G1  the retry cap counted LIFETIME attempts: a condition fixed three times
    over the months (each by a completed retry) came back and was reported
    "ran 3 time(s) and has STOPPED" before VCO had retried it once. The cap
    is now scoped to the CURRENT episode (``deferral_retry.retry_history``).
G2  the update's resync probed the code_embed service ONCE, five seconds
    after the same update restarted its container, and deferred the resync
    while the model was still loading. A running-but-silent container is now
    waited for (``codegraph_resync.code_backend_down_waiting``).
G5  ``codegraph_embed_resync_pending`` printed the bare analyzer walk, which
    never clears that entry; it now prints the resync DRIVER, whose post-walk
    probe is the clear.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest import mock

import pytest

from vco_lib import codegraph_resync as cr
from vco_lib import deferral_retry as dr
from vco_lib.deferral_emit import DeferralEntry, emit, resolve_conditions
from vco_lib.deferral_report import DeferralReport

CID = "codegraph_embed_resync_pending"


# ─────────────────────────────── helpers ───────────────────────────────


def _trail(folder: Path, *rows: "tuple[str, str]") -> None:
    path = dr.attempts_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i, (status, detail) in enumerate(rows):
            fh.write(json.dumps({
                "ts": f"2026-09-{(i % 28) + 1:02d}T10:00:00Z",
                "condition_id": CID, "status": status, "detail": detail,
            }) + "\n")


def _completed_episode() -> "list[tuple[str, str]]":
    return [(dr.STARTED, "handler codegraph_resync"),
            (dr.RETRIED, "code-graph resync driver completed")]


def _failed_attempt() -> "list[tuple[str, str]]":
    return [(dr.STARTED, "handler codegraph_resync"),
            (dr.INCONCLUSIVE, "the condition is STILL in the ledger")]


def _trail_statuses(folder: Path) -> "list[str]":
    path = dr.attempts_path(folder)
    if not path.is_file():
        return []
    return [json.loads(ln)["status"] for ln in path.read_text().splitlines() if ln.strip()]


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        return 0


def _dispatch(folder: Path, monkeypatch) -> "tuple[list, _Runner]":
    scripts = folder / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        results = dr.dispatch(folder, condition_ids=[CID],
                              backend_probe=lambda *_a: True, runner=runner,
                              single_instance=False)
    return results, runner


def _entry() -> DeferralEntry:
    entry = cr.build_resync_deferral("MyProj", "true")
    assert entry is not None
    return entry


def _command(result) -> str:
    assert isinstance(result.deferral, DeferralEntry)
    return result.deferral.command_to_apply


# ─────────────────────────── G1: per-episode cap ───────────────────────────


def test_completed_earlier_episodes_do_not_count_against_a_new_one(tmp_path, monkeypatch):
    """The field trail: three COMPLETED episodes, then a fresh occurrence."""
    _trail(tmp_path, *(_completed_episode() * 3))
    history = dr.retry_history(tmp_path, CID)
    assert history.attempts == 0 and not history.cap_reached
    assert dr.attempt_count(tmp_path, CID) == 0
    assert "STOPPED" not in dr.retry_disposition_note(tmp_path, CID)
    # The dispatcher's gate agrees: the handler runs.
    results, runner = _dispatch(tmp_path, monkeypatch)
    assert len(runner.calls) == 1, [r.detail for r in results]


def test_a_genuinely_stuck_current_episode_still_stops_at_the_cap(tmp_path, monkeypatch):
    _trail(tmp_path, *(_completed_episode() * 3), *(_failed_attempt() * dr.MAX_ATTEMPTS))
    history = dr.retry_history(tmp_path, CID)
    assert history.attempts == dr.MAX_ATTEMPTS and history.cap_reached
    assert "STOPPED" in dr.retry_disposition_note(tmp_path, CID)
    results, runner = _dispatch(tmp_path, monkeypatch)
    assert runner.calls == []
    assert [r.status for r in results] == [dr.SKIPPED]
    assert "cap" in results[0].detail


def test_an_external_clear_ends_the_episode(tmp_path):
    _trail(tmp_path, *(_failed_attempt() * dr.MAX_ATTEMPTS),
           (dr.RESOLVED, "ledger entry cleared"))
    assert dr.attempt_count(tmp_path, CID) == 0


def test_a_clear_inside_an_open_attempt_does_not_reset_the_cap(tmp_path):
    """A handler's child that clears and re-emits must not loop forever."""
    attempt = [(dr.STARTED, "handler codegraph_resync"),
               (dr.RESOLVED, "ledger entry cleared"),
               (dr.INCONCLUSIVE, "the condition is STILL in the ledger")]
    _trail(tmp_path, *(attempt * dr.MAX_ATTEMPTS))
    assert dr.retry_history(tmp_path, CID).cap_reached


def test_the_blocked_streak_is_scoped_to_the_episode_too(tmp_path):
    blocked = (dr.BLOCKED, dr.backend_block_detail("code", False))
    _trail(tmp_path, *([blocked] * 6), *_completed_episode())
    history = dr.retry_history(tmp_path, CID)
    assert history.blocked == 0 and history.blocked_streak == 0
    # ...while a current episode's streak is still reported.
    _trail(tmp_path, *([blocked] * 6))
    history = dr.retry_history(tmp_path, CID)
    assert history.blocked == 6 and history.blocked_streak == 6


def test_resolve_conditions_records_the_episode_end(tmp_path):
    emit(tmp_path, _entry())
    assert resolve_conditions(tmp_path, (CID,)) == 1
    assert _trail_statuses(tmp_path) == [dr.RESOLVED]


def test_resolving_an_absent_or_unretryable_condition_writes_no_row(tmp_path):
    assert resolve_conditions(tmp_path, (CID,)) == 0
    assert _trail_statuses(tmp_path) == []
    dr.record_episode_end(tmp_path, "bundle_user_modified_preserved", "x")
    assert _trail_statuses(tmp_path) == []


def test_update_not_owed_clear_ends_the_episode_only_when_the_entry_was_present(tmp_path):
    result = cr.ResyncTriggerResult(status="not_owed", message="none owed")
    empty = DeferralReport()
    cr.report_trigger_result(result, "MyProj", empty, lambda *a: None, tmp_path)
    assert _trail_statuses(tmp_path) == []
    report = DeferralReport()
    report.add_entry(_entry())
    cr.report_trigger_result(result, "MyProj", report, lambda *a: None, tmp_path)
    assert _trail_statuses(tmp_path) == [dr.RESOLVED]
    assert not report.has_condition(CID)


# ──────────────────── G2: wait for a starting backend ────────────────────


@pytest.fixture
def backend(monkeypatch):
    """A container-served graph whose health answers from a script."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")  # bounded, no sleeps
    monkeypatch.setattr(cr, "_walk_code_models", lambda *_a: [])
    state = {"answers": [], "probes": 0, "running": True}

    def _healthy(*_a, **_k):
        state["probes"] += 1
        return state["answers"].pop(0) if state["answers"] else False

    monkeypatch.setattr(cr, "code_embed_service_healthy", _healthy)
    monkeypatch.setattr(cr, "_backend_container_running", lambda _svc: state["running"])
    return state


def test_a_running_container_that_finishes_loading_is_waited_for(backend):
    backend["answers"] = [False, False, True]
    down, used = cr.code_backend_down_waiting("MyProj")
    assert down is None and used is True
    assert backend["probes"] == 3


def test_a_stopped_or_absent_container_is_not_waited_for(backend):
    backend["running"] = False
    backend["answers"] = [False, True]
    down, _used = cr.code_backend_down_waiting("MyProj")
    assert down is not None and "11440" in down
    assert backend["probes"] == 1, "no re-probe when nothing is starting"


def test_a_running_container_that_never_answers_is_bounded(backend):
    down, _used = cr.code_backend_down_waiting("MyProj")
    assert down is not None
    assert 1 < backend["probes"] < 20


def _spawn(tmp_path, monkeypatch, **kw):
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    spawned = []
    monkeypatch.setattr(cr.subprocess, "Popen",
                        lambda argv, **k: spawned.append(argv) or mock.Mock(pid=4321))
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    result = cr.spawn_background_resync(tmp_path, "MyProj", python_exe=sys.executable,
                                        check_owed=False, **kw)
    return result, spawned


def test_the_update_trigger_launches_once_the_restarted_service_answers(
    tmp_path, monkeypatch, backend,
):
    backend["answers"] = [False, True]
    result, spawned = _spawn(tmp_path, monkeypatch, wait_for_starting_backend=True)
    assert result.status == "launched", result.message
    assert any("--run-resync" in argv for argv in spawned)


def test_without_the_flag_the_instant_probe_still_defers(tmp_path, monkeypatch, backend):
    backend["answers"] = [False, True]
    result, spawned = _spawn(tmp_path, monkeypatch)
    assert result.status == "deferred" and spawned == []
    assert backend["probes"] == 1


def test_install_trigger_passes_the_wait_flag_and_the_ledger_folder():
    src = (Path(__file__).resolve().parents[1] / "install.py").read_text(encoding="utf-8")
    start = src.index("def _trigger_codegraph_embed_resync(")
    body = src[start:src.index("\ndef ", start + 10)]
    seen = {}

    def _spawn_stub(*a, **k):
        seen["spawn"] = k
        return cr.ResyncTriggerResult(status="skipped", message="stub")

    def _report_stub(*a, **k):
        seen["report"] = a

    ns = {
        "PROJECT_ROOT": Path("/nonexistent-root"), "DeferralReport": DeferralReport,
        "_log_install_event": lambda *a: None,
        "_derive_orchestrator_project_name": lambda: "MyProj",
    }
    exec(compile(body, "install.py", "exec"), ns)  # noqa: S102 — the real shim body
    with mock.patch.object(cr, "spawn_background_resync", _spawn_stub), \
            mock.patch.object(cr, "report_trigger_result", _report_stub):
        ns["_trigger_codegraph_embed_resync"](DeferralReport())
    assert seen["spawn"].get("wait_for_starting_backend") is True
    assert seen["report"][-1] == Path("/nonexistent-root")


# ──────────────── G5: the printed command clears its entry ────────────────


def _parse_driver(cmd: str) -> dict:
    """Run the printed command's argv through the driver's REAL entry point,
    with the driver body stubbed — what would execute, and with which args."""
    parts = shlex.split(cmd)
    assert parts[1:3] == ["-m", "vco_lib.codegraph_resync"], parts
    seen = {}

    def _driver(project, repo_root, analyzer, **kw):
        seen.update(project=project, repo_root=repo_root, analyzer=analyzer, **kw)
        return 0

    with mock.patch.object(cr, "run_resync_and_verify", _driver), \
            mock.patch.object(cr, "configure_logging", lambda *a, **k: None):
        rc = cr._main(parts[3:])
    assert rc == 0
    return seen


def test_the_service_down_entry_prints_the_clearing_driver(tmp_path, monkeypatch, backend):
    repo = tmp_path / "a repo"
    repo.mkdir()
    result, _spawned = _spawn(repo, monkeypatch)
    assert result.status == "deferred"
    seen = _parse_driver(_command(result))
    assert seen["project"] == "MyProj" and seen["repo_root"] == repo
    assert seen["analyzer"].name == "analyze_code_graph.py"


def test_the_stale_image_entry_prints_a_driver_command_that_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(cr, "_walk_code_models", lambda *_a: [])
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "stale")
    result = cr.spawn_background_resync(tmp_path, "MyProj", python_exe=sys.executable,
                                        check_owed=False)
    assert result.status == "deferred"
    cmd = _command(result)
    seen = _parse_driver(cmd.split("re-run the resync: ", 1)[1])
    assert seen["repo_root"] == tmp_path


def test_the_drivers_converged_run_clears_the_entry_it_printed(tmp_path):
    """The other half of the promise: the driver's own clear removes it."""
    emit(tmp_path, _entry())
    cr._resolve_persisted_resync_deferral(tmp_path)
    assert not DeferralReport.read(tmp_path).has_condition(CID)
