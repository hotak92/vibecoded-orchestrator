# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 AD-9 / L1-F09 / L1-F18 — every install exit path writes the
deferral report, and ONE gate stands in front of every Weaviate write.

No real Weaviate, Ollama, podman or install run: the step-5 tail runs against
the scripted ``World`` of ``tests/test_v02100_install_services_up.py`` and the
Weaviate steps against fakes.
"""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from tests.test_v02100_install_services_up import CORPUS, World  # noqa: E402
from vco_lib import install_deferral_flow as idf  # noqa: E402
from vco_lib import install_services_up as isu  # noqa: E402
from vco_lib import install_weaviate as iw  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402

STEP_5B_CID = "service_registry_unavailable"  # a row step [5b] owes (install-owned)


def _row(cid: str) -> DeferralEntry:
    return DeferralEntry(condition_id=cid, title=cid, detected="d", why_deferred="w",
                         command_to_apply="c", severity="warning")


def _flow(folder: Path) -> idf.InstallDeferralFlow:
    return idf.InstallDeferralFlow(folder, owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
                                   owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES)


def _ids(folder: Path) -> list[str]:
    return [e.condition_id for e in DeferralReport.read(folder).entries]


# ── step 5 hard stop → the step-5b rows + the compose failure row on disk ───


def test_sys_exit_at_step_5_leaves_the_5b_rows_and_the_compose_failure_row(tmp_path):
    (tmp_path / "w").mkdir()
    w = World(tmp_path / "w", compose_results=((1, CORPUS["field_2026_09_29_network_label"]),),
              net_labels={})
    ledger_root = tmp_path / "root"
    (ledger_root / ".claude" / "context").mkdir(parents=True)

    def main() -> int:  # install.main()'s shape: arm, 5b rows, step 5, exit 1
        flow = _flow(ledger_root)
        flow.arm()
        flow.report.add_entry(_row(STEP_5B_CID))
        w.plan.deferral_report = flow.report
        if w.go()[0] == isu.FAIL:
            sys.exit(1)  # _start_services' hard stop
        return 0

    with pytest.raises(SystemExit) as ei:
        idf.run_main(main)
    assert ei.value.code == 1
    on_disk = _ids(ledger_root)
    assert STEP_5B_CID in on_disk
    assert "services_compose_up_failed" in on_disk
    assert "compose_network_label_mismatch_attached" in on_disk


def test_uncaught_exception_and_early_return_are_flushed_too(tmp_path):
    def raising() -> int:
        flow = _flow(tmp_path)
        flow.arm()
        flow.report.add_entry(_row("ollama_model_pull_failed"))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        idf.run_main(raising)
    assert _ids(tmp_path) == ["ollama_model_pull_failed"]

    def early() -> int:
        flow = _flow(tmp_path / "b")
        flow.arm()
        flow.report.add_entry(_row(STEP_5B_CID))
        return 1

    assert idf.run_main(early) == 1
    assert _ids(tmp_path / "b") == [STEP_5B_CID]


def test_partial_flush_keeps_owned_rows_this_run_never_reached(tmp_path):
    """ACT: an owned row on disk that the stopped run did not re-detect is
    KEPT by the partial flush. LEAVE-ALONE: a completed run's finalize still
    drops it (drop-when-absent is a completed-run rule)."""
    DeferralReport().write(tmp_path)  # empty; then plant one owned row
    seeded = DeferralReport()
    seeded.add_entry(_row("weaviate_unreachable_at_update"))
    seeded.write(tmp_path)

    def stopped() -> int:
        flow = _flow(tmp_path)
        flow.seed()
        flow.arm()
        flow.report.add_entry(_row(STEP_5B_CID))
        sys.exit(1)

    with pytest.raises(SystemExit):
        idf.run_main(stopped)
    assert sorted(_ids(tmp_path)) == sorted(["weaviate_unreachable_at_update", STEP_5B_CID])

    flow = _flow(tmp_path)
    flow.seed()
    flow.finalize()  # a COMPLETED run that no longer sees either condition
    assert _ids(tmp_path) == []


def test_unarmed_exits_write_nothing_and_a_finalized_run_is_not_rewritten(tmp_path):
    def dry_run() -> int:  # --adopt-project-dry-run: returns before arming
        _flow(tmp_path).report.add_entry(_row(STEP_5B_CID))
        return 0

    assert idf.run_main(dry_run) == 0
    assert not (tmp_path / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()

    writes = []

    def completed() -> int:
        flow = _flow(tmp_path)
        flow.arm()
        flow.report.add_entry(_row(STEP_5B_CID))
        flow.finalize()
        with mock.patch.object(DeferralReport, "write", side_effect=lambda *a: writes.append(a)):
            return 0

    assert idf.run_main(completed) == 0
    assert writes == []


def test_install_py_entry_point_runs_main_through_the_flush(monkeypatch):
    """The ``__main__`` block must enter :func:`run_main` (behavioural: run the
    module as a script with ``run_main`` spied — main itself never runs)."""
    seen = []
    monkeypatch.setattr(idf, "run_main", lambda fn: seen.append(fn.__name__) or 7)
    monkeypatch.setattr(sys, "argv", ["install.py", "--help"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(str(REPO_ROOT / "install.py"), run_name="__main__")
    assert ei.value.code == 7 and seen == ["main"]


# ── the one Weaviate write gate (L1-F18) ────────────────────────────────────


@pytest.fixture
def pending(monkeypatch):
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "weaviate_pending", True)
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)


def test_gate_refuses_while_confirmation_is_pending_and_allows_otherwise():
    events = []
    assert iw.weaviate_write_allowed({"weaviate_pending": True}, step="seed",
                                      log_event=lambda *a, **k: events.append(a))[0] is False
    assert events and events[0][1] == iw.SKIPPED
    assert iw.weaviate_write_allowed({"weaviate_pending": False}, step="seed") == (True, "")


def test_yes_and_rebuild_collections_never_approve_a_rebuild_past_the_gate(pending):
    args = argparse.Namespace(update=True, rebuild_collections=True, skip_rebuild_prompt=False,
                              yes=True)
    with mock.patch.object(install, "_detect_kg_schema_drift") as drift:
        assert install._maybe_prompt_rebuild_collections(args) is False
    drift.assert_not_called()


def test_leave_alone_rebuild_opt_in_still_works_without_a_pending_question(monkeypatch):
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "weaviate_pending", False)
    args = argparse.Namespace(update=True, rebuild_collections=True, skip_rebuild_prompt=False)
    assert install._maybe_prompt_rebuild_collections(args) is True


def test_every_weaviate_writing_step_refuses_while_pending(pending):
    with mock.patch.object(install._wh, "weaviate_url_default") as url:
        install._ensure_collections({}, args=argparse.Namespace())
    url.assert_not_called()
    assert install._seed_weaviate(argparse.Namespace()) == iw.SKIPPED
    with mock.patch.object(install, "_run_additive_temporal_props_migration") as mig:
        install._run_schema_migration_scripts(DeferralReport())
    mig.assert_not_called()
    with mock.patch.object(install, "_trigger_codegraph_identity_sweep") as sweep:
        install._trigger_codegraph_maintenance(DeferralReport())
    sweep.assert_not_called()


def test_a_gated_seed_is_skipped_not_a_success():
    """L1-F18: ``_seed_succeeded`` was True when the gate had skipped the seed."""
    assert iw.collections_and_seed(lambda: None, lambda: iw.SKIPPED, report=DeferralReport(),
                                   rebuild_performed=False, runtime=lambda: "podman") == iw.SKIPPED
    assert iw.collections_and_seed(lambda: None, lambda: None, report=DeferralReport(),
                                   rebuild_performed=False, runtime=lambda: "podman") == iw.SEEDED


def test_weaviate_down_gets_one_restart_then_a_deferral(monkeypatch):
    calls = []

    def ensure():
        raise TimeoutError("not ready")

    monkeypatch.setattr("vco_lib.containers.find_existing_container", lambda *a, **k: "vco_weaviate")
    report = DeferralReport()
    status = iw.collections_and_seed(ensure, lambda: None, report=report, rebuild_performed=True,
                                     runtime=lambda: "podman",
                                     run=lambda argv, **k: calls.append(argv), sleep=lambda _s: None)
    assert status == iw.FAILED
    assert calls == [["podman", "start", "vco_weaviate"]]
    assert {e.condition_id for e in report.entries} == {"weaviate_unreachable_at_update",
                                                        "rebuild_pending_seed"}
