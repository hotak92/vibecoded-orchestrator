# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — a resync whose analysis SKIPPED (no embedding backend
reachable) must not report terminal ``success`` to the hub, and the owed
work must stay owed.

Field 2026-10-23 (``~/.vct/logs/resync-VibeCodedOrchestrator-20261023-
140259.log``): the analyzer soft-skips with EXIT 0 when the code backend
does not answer ("Code-graph analysis skipped: active code backend
(slot=codesage_embed) is not reachable"), the driver relayed that as
``reported terminal 'success' to hub``, and the post-walk probe could
even clear ``codegraph_embed_resync_pending`` for work that never ran.
"""

import subprocess

from vco_lib import codegraph_resync as cr

_CONDITION_ID = "codegraph_embed_resync_pending"
_DOWN = "the code_embed container (:11440) serving codesage-large-v2"


def _stub_driver_gates(monkeypatch, *, backend_down):
    """Neutralise every gate of the driver's slotted body except the
    backend-reachability one under test (no Weaviate, no hub, no spawn).
    Returns the recorder dicts."""
    from types import SimpleNamespace

    monkeypatch.setattr(cr, "walk_uses_container", lambda *_a, **_k: False)
    monkeypatch.setattr(cr, "code_backend_down_waiting",
                        lambda *_a, **_k: (_DOWN, True) if backend_down
                        else (None, False))
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *_a, **_k: False)
    monkeypatch.setattr(cr, "_maybe_run_vectorless_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *_a, **_k: {})
    monkeypatch.setattr(cr, "spawn_code_summary_after_walk",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "_collect_terminal_stats",
                        lambda *_a, **_k: ({}, None))
    seen = {"runs": [], "terminal": []}

    def _record_terminal(*args, **kwargs):
        seen["terminal"].append((args, kwargs))
        return None

    monkeypatch.setattr(cr, "_report_terminal_to_hub", _record_terminal)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: seen["runs"].append(a) or SimpleNamespace(returncode=0))
    return seen


def _persisted(folder) -> set:
    from vco_lib.deferral_report import DeferralReport

    return {e.condition_id for e in DeferralReport.read(folder).entries}


def test_backend_down_refuses_before_the_analyzer_and_reports_failed(
        monkeypatch, tmp_path, capsys):
    """ACT side: with the backend unreachable the driver must NOT spawn the
    analyzer, must post a NON-success terminal status (the hub's /terminal
    route accepts only success/partial/failed — "failed" is the honest one
    for a run that did not complete), and must keep/record the
    ``codegraph_embed_resync_pending`` deferral so the retry driver re-runs
    the resync once the backend answers. RED-PROOF TARGET: deleting the
    backend-down gate (or its terminal report) turns these assertions red.
    """
    seen = _stub_driver_gates(monkeypatch, backend_down=True)
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py")
    assert rc == 0
    assert seen["runs"] == [], "a skipped analysis must never spawn the analyzer"
    out = capsys.readouterr().out
    assert "NOT running the analyzer" in out
    assert "stays owed" in out
    # Exactly ONE terminal report, and it is NOT success.
    assert len(seen["terminal"]) == 1, seen["terminal"]
    args, kwargs = seen["terminal"][0]
    assert args[0] == "MyProj"
    assert args[1] != "success"
    assert args[1] == "failed"
    assert kwargs.get("files_analyzed") == 0
    assert _CONDITION_ID in _persisted(tmp_path), (
        "the owed resync work must stay owed (kept or recorded)")


def test_backend_up_runs_the_analyzer_and_reports_success(
        monkeypatch, tmp_path):
    """LEAVE-ALONE side: a reachable backend changes nothing — the analyzer
    runs and the ordinary completion path reports its own terminal status
    (never a backend-down refusal, never the backend-down deferral)."""
    seen = _stub_driver_gates(monkeypatch, backend_down=False)
    (tmp_path / ".claude" / "context").mkdir(parents=True, exist_ok=True)
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py")
    assert rc == 0
    assert len(seen["runs"]) == 1, "a reachable backend must reach the analyzer"
    assert len(seen["terminal"]) == 1
    _args, kwargs = seen["terminal"][0]
    assert kwargs.get("error_message") in (None, ""), kwargs
    assert _CONDITION_ID not in _persisted(tmp_path)


def test_backend_down_keeps_an_existing_pending_entry_recorded(
        monkeypatch, tmp_path):
    """The entry the spawn may have recorded is REFRESHED (kept), not
    resolved: a driver that cannot run must not clear the ledger row that
    says its work is owed."""
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    rep = DeferralReport.read(tmp_path)
    rep.add_entry(DeferralEntry(
        condition_id=_CONDITION_ID, title="pending", detected="d",
        why_deferred="w", command_to_apply="cmd", severity="warning"))
    rep.write(tmp_path)
    _stub_driver_gates(monkeypatch, backend_down=True)
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py")
    assert rc == 0
    assert _CONDITION_ID in _persisted(tmp_path)


def test_the_driver_side_probe_is_the_shared_waiting_one(monkeypatch):
    """The gate must ask :func:`code_backend_down_waiting` — the ONE shared
    probe that also WAITS OUT a running-but-starting container (the v0.2.104
    tolerance) instead of the instant probe, so a model still loading is not
    refused. Regression pin on the wiring, not the behaviour."""
    import inspect

    src = inspect.getsource(cr._run_resync_and_verify_slotted)
    assert "code_backend_down_waiting(" in src
    assert "code_backend_down(" not in src.replace(
        "code_backend_down_waiting(", "")


# ─── v0.2.105 fix round: the POST-WALK soft-skip convergence (the race) ──


def test_post_walk_soft_skip_converges_on_the_refusal(monkeypatch, tmp_path,
                                                       capsys):
    """ACT side of the closed race (review LOW): the backend answered the
    PRE-check but went down before the analyzer's own probe, so the analyzer
    SOFT-SKIPPED — exit 0 with "Code-graph analysis skipped" in the shared
    log. The driver must NOT relay that as success: the deferral is
    recorded (driver-time text), the hub row is closed "failed" with
    files_analyzed 0, and the code-summary rider never spawns for a walk
    that analyzed nothing. RED-PROOF TARGET: deleting the post-walk
    soft-skip check turns these assertions red."""
    seen = _stub_driver_gates(monkeypatch, backend_down=False)
    seen["summary"] = []
    monkeypatch.setattr(cr, "spawn_code_summary_after_walk",
                        lambda *a, **k: seen["summary"].append(a))
    log = tmp_path / "resync.log"
    log.write_text(
        "⚠️  Code-graph analysis skipped: active code backend "
        "(slot=codesage_embed) is not reachable.\n", encoding="utf-8")
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log)
    assert rc == 0
    assert len(seen["runs"]) == 1, "the analyzer DID run (and soft-skipped)"
    assert seen["summary"] == []
    out = capsys.readouterr().out
    assert "SOFT-SKIPPED" in out and "stays owed" in out
    # Exactly ONE terminal report, and it is NOT success.
    assert len(seen["terminal"]) == 1, seen["terminal"]
    args, kwargs = seen["terminal"][0]
    assert args[0] == "MyProj" and args[1] == "failed"
    assert kwargs.get("files_analyzed") == 0
    assert "soft-skipped" in (kwargs.get("error_message") or "")
    assert _CONDITION_ID in _persisted(tmp_path)


def test_post_walk_no_marker_is_the_ordinary_completion(monkeypatch, tmp_path):
    """LEAVE-ALONE side: an analyzer that RAN (no skip marker in the log)
    keeps the ordinary completion path — its own terminal report, no
    backend-down deferral."""
    seen = _stub_driver_gates(monkeypatch, backend_down=False)
    log = tmp_path / "resync.log"
    log.write_text("Code-graph analysis: 42 files analyzed\n",
                   encoding="utf-8")
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log)
    assert rc == 0
    assert len(seen["runs"]) == 1
    assert len(seen["terminal"]) == 1
    _args, kwargs = seen["terminal"][0]
    assert kwargs.get("error_message") in (None, ""), kwargs
    assert _CONDITION_ID not in _persisted(tmp_path)


# ─── v0.2.105 fix round: the driver-time deferral text ───────────────────


def test_backend_down_builder_text_is_driver_time():
    """The DRIVER-TIME flavour must say WHEN the backend was down (the resync
    run), never reuse the update-time wording."""
    entry = cr.build_backend_down_deferral("MyProj", "cmd", backend=_DOWN)
    assert entry is not None
    assert entry.condition_id == _CONDITION_ID
    for stale in ("--update", "pre-chunking"):
        assert stale not in entry.detected, entry.detected
        assert stale not in entry.why_deferred, entry.why_deferred
    assert _DOWN.lower() in entry.detected.lower()  # capitalised first letter
    assert "unreachable" in entry.detected


def test_recorded_backend_down_deferral_uses_driver_time_text(
        monkeypatch, tmp_path):
    """End to end: the entry the DRIVER records (both arms) carries the
    driver-time text — review LOW: the update-time wording ("was
    unreachable during --update ... pre-chunking scheme") is false for a
    manual or driver-time refusal. RED-PROOF TARGET: pointing
    `_record_backend_down_deferral` back at `build_resync_deferral` turns
    this red."""
    _stub_driver_gates(monkeypatch, backend_down=True)
    rc = cr._run_resync_and_verify_slotted(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py")
    assert rc == 0
    from vco_lib.deferral_report import DeferralReport

    entry = next(e for e in DeferralReport.read(tmp_path).entries
                 if e.condition_id == _CONDITION_ID)
    for stale in ("--update", "pre-chunking"):
        assert stale not in entry.detected, entry.detected
        assert stale not in entry.why_deferred, entry.why_deferred
