# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 (A-003) — owed work runs from ONE machine queue, one worker, gated.

Before: every session start of every project spawned its own detached
``python -m vco_lib.deferral_retry --folder <project>`` driver; nothing checked
that Weaviate was ready; each owed KG condition ran its own whole-tree
``sync_knowledge_graph.py --all``. After a crash, N projects' session starts
fired N drivers and up to 2N ``--all`` passes against a Weaviate still loading
its shards.

Pinned here, act AND leave-alone, all with fakes (no service, no real child):

* three session starts within 2 s → three queue entries, ONE worker spawn;
* Weaviate not ready → the job stays queued, NO attempt consumed;
* three conditions that need the same job → one run;
* a secondary backfill runs the missing-rows backfill, not ``--all``;
  a sync failure re-syncs only the failed paths;
* a worker that died mid-job → the attempt still counts (v0.2.91 rule);
* Weaviate's container restarted during the job → the attempt is excused
  (bounded), an unchanged machine → it counts;
* real order: readiness lost mid-job and regained → the job completes later,
  exactly one more run, no duplicate.

The measurement the report quotes comes from
``test_measurement_three_projects_after_a_restart``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_retry as dr  # noqa: E402

KG_CID = "kg_sync_no_embedding_backend"
FAIL_CID = "kg_sync_failures_pending"
BACKFILL_CID = "kg_secondary_backfill_pending"
FORMATS_CID = "kg_node_formats_refresh_failed"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A private VCT state root: the queue, the worker lock, the slots."""
    root = tmp_path / "vct-state"
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    monkeypatch.setenv("VCO_RESYNC_DRIVER_SLOTS", "2")
    return root


def _project(parent: Path, name: str) -> Path:
    folder = parent / name
    scripts = folder / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    for script in ("sync_knowledge_graph.py", "analyze_code_graph.py"):
        (scripts / script).write_text("# stub\n", encoding="utf-8")
    (folder / "knowledge").mkdir()
    return folder


def _owe(folder: Path, *cids: str) -> None:
    """Write real ledger entries for ``cids`` (the worker re-reads the ledger)."""
    from vco_lib.deferral_emit import DeferralEntry, emit_entries

    emit_entries(folder, [
        DeferralEntry(condition_id=cid, title=cid, detected="test", why_deferred="test",
                      command_to_apply="", severity="warning")
        for cid in cids
    ])


def _clear(folder: Path, *cids: str) -> None:
    from vco_lib.deferral_emit import resolve_conditions

    resolve_conditions(folder, cids)


class _Runner:
    """Records argv; optional side effect per call (clear the ledger, flip the
    machine state...)."""

    def __init__(self, rc=0, effect=None):
        self.rc = rc
        self.effect = effect
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        self.calls.append([str(a) for a in argv])
        if self.effect is not None:
            out = self.effect(len(self.calls), argv, cwd)
            if out is not None:
                return out
        return self.rc


def _ready(started_at: "float | None" = 1000.0):
    from vco_lib.owed_work import MachineState

    return MachineState(ready=True, started_at=started_at)


def _rows(folder: Path, cid: str) -> list[str]:
    path = dr.attempts_path(folder)
    if not path.exists():
        return []
    return [json.loads(line)["status"] for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("condition_id") == cid]


# ---------------------------------------------------------------------------
# 1. Session starts → one queue, one worker
# ---------------------------------------------------------------------------


def test_three_session_starts_within_two_seconds_start_one_worker(state, tmp_path):
    """ACT. Before: one ``deferral_retry --folder`` driver per project (3).
    After: three queue entries and ONE worker spawn."""
    projects = [_project(tmp_path, f"p{i}") for i in range(3)]
    spawned: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))

    with mock.patch.object(dr, "owed_condition_ids", return_value=[KG_CID]), \
         mock.patch.object(dr, "attempt_count", return_value=0), \
         mock.patch("subprocess.Popen", _FakePopen):
        for folder in projects:
            assert dr.session_start_owed_check(folder) == [KG_CID]

    assert len(spawned) == 1, spawned
    assert spawned[0][-3:] == ["-m", "vco_lib.owed_work", "worker"]
    from vco_lib import owed_work

    assert sorted(Path(e.folder).name for e in owed_work.read_queue()) == ["p0", "p1", "p2"]


def test_a_second_project_while_a_worker_runs_only_enqueues(state, tmp_path):
    """LEAVE-ALONE: a live worker (its lock held) is never joined by a second."""
    from vco_lib import owed_work
    from vco_lib.atomic import exclusive_file_lock

    folder = _project(tmp_path, "p")
    popen = mock.Mock()
    with exclusive_file_lock(owed_work.worker_lock_path(), timeout_s=0):
        assert dr.spawn_detached(folder) is True
        with mock.patch("subprocess.Popen", popen):
            assert owed_work.ensure_worker() is True
    popen.assert_not_called()
    assert [Path(e.folder) for e in owed_work.read_queue()] == [folder]


def test_an_empty_queue_starts_no_worker(state):
    from vco_lib import owed_work

    popen = mock.Mock()
    assert owed_work.ensure_worker(popen=popen) is False
    popen.assert_not_called()


def test_enqueue_is_one_entry_per_project_and_merges_env(state, tmp_path):
    from vco_lib import owed_work

    folder = _project(tmp_path, "p")
    owed_work.enqueue(folder, env={"A": "1"})
    owed_work.enqueue(folder, env={"B": "2"})
    owed_work.enqueue(folder, env={"A": "3"})
    [entry] = owed_work.read_queue()
    assert entry.env == {"A": "3", "B": "2"}


def test_the_revive_path_restarts_the_worker(monkeypatch):
    """The per-prompt hub revive is the second trigger (Python-only design)."""
    from vco_lib import hub_ensure, owed_work

    monkeypatch.setattr(hub_ensure, "revive_if_down", lambda **k: hub_ensure.ReviveResult(
        hub_ensure.ReviveState.ALREADY_RUNNING, "up"))
    calls: list[str] = []
    monkeypatch.setattr(owed_work, "ensure_worker", lambda **k: calls.append(str(k.get("origin"))))
    assert hub_ensure.main(["revive", "--source", "prompt", "--quiet"]) == 0
    assert calls == ["revive prompt"]


# ---------------------------------------------------------------------------
# 2. Weaviate gate — not ready ⇒ queued, no attempt
# ---------------------------------------------------------------------------


def test_weaviate_not_ready_keeps_the_job_queued_and_consumes_no_attempt(tmp_path):
    folder = _project(tmp_path, "p")
    runner = _Runner()
    from vco_lib.owed_work import MachineState

    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=runner, single_instance=False,
                          machine_watch=lambda: MachineState(ready=False))
    assert [r.status for r in results] == [dr.QUEUED]
    assert runner.calls == []
    assert dr.attempt_count(folder, KG_CID) == 0
    assert _rows(folder, KG_CID) == []


def test_weaviate_unknown_is_not_ready_either(tmp_path):
    folder = _project(tmp_path, "p")
    runner = _Runner()
    from vco_lib.owed_work import MachineState

    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=runner, single_instance=False,
                          machine_watch=lambda: MachineState(ready=None))
    assert [r.status for r in results] == [dr.QUEUED]
    assert runner.calls == []


def test_weaviate_ready_runs_the_job(tmp_path):
    """LEAVE-ALONE of the gate: ready → the job runs and is judged as before."""
    folder = _project(tmp_path, "p")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                              runner=runner, single_instance=False,
                              machine_watch=lambda: _ready())
    assert [r.status for r in results] == [dr.RETRIED]
    assert len(runner.calls) == 1


def test_worker_leaves_the_queue_alone_while_weaviate_is_not_ready(state, tmp_path):
    from vco_lib import owed_work
    from vco_lib.owed_work import MachineState, WorkerDeps

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    owed_work.enqueue(folder)
    dispatch = mock.Mock()
    clock = iter(range(0, 10_000, 10))
    report = owed_work.run_worker(WorkerDeps(
        state=lambda: MachineState(ready=False), dispatch=dispatch, sleep=lambda s: None,
        clock=lambda: float(next(clock)), idle_s=30, settle_s=0,
        slot_dir=state / "slots", slots=1, log=lambda line: None))
    dispatch.assert_not_called()
    assert "queue kept" in report.exit_reason
    assert len(owed_work.read_queue()) == 1
    assert dr.attempt_count(folder, KG_CID) == 0


def test_worker_waits_for_weaviate_to_settle(state, tmp_path):
    """Ready for less than the settle window → nothing runs yet."""
    from vco_lib import owed_work
    from vco_lib.owed_work import WorkerDeps

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    owed_work.enqueue(folder)
    dispatch = mock.Mock()
    now = [0.0]

    def _sleep(s):
        now[0] += 1.0

    report = owed_work.run_worker(WorkerDeps(
        state=lambda: _ready(), dispatch=dispatch, sleep=_sleep, clock=lambda: now[0],
        wall=lambda: 10_000.0, idle_s=5, settle_s=30, slot_dir=state / "slots", slots=1,
        log=lambda line: None))
    dispatch.assert_not_called()
    assert "settling" in report.exit_reason


def test_worker_leaves_the_queue_alone_while_every_slot_is_busy(state, tmp_path):
    """The worker shares the bulk-writer pool: a full pool → no run, no
    parked process (it exits after the idle window, the entry stays)."""
    from vco_lib import owed_work
    from vco_lib.atomic import exclusive_file_lock
    from vco_lib.owed_work import WorkerDeps

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    owed_work.enqueue(folder)
    dispatch = mock.Mock()
    clock = iter(range(0, 10_000, 10))
    slot_dir = state / "slots"
    with exclusive_file_lock(slot_dir / "slot-0.lock", timeout_s=0):
        report = owed_work.run_worker(WorkerDeps(
            state=lambda: _ready(), dispatch=dispatch, sleep=lambda s: None,
            clock=lambda: float(next(clock)), wall=lambda: 10_000.0, idle_s=30, settle_s=0,
            slot_dir=slot_dir, slots=1, log=lambda line: None))
    dispatch.assert_not_called()
    assert "busy" in report.exit_reason
    assert len(owed_work.read_queue()) == 1


def test_the_job_holds_a_slot_of_the_shared_pool(state, tmp_path):
    """While a job runs, a resync driver taking the same pool finds it full."""
    from vco_lib import owed_work
    from vco_lib.atomic import try_machine_slot_free
    from vco_lib.owed_work import WorkerDeps

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    owed_work.enqueue(folder)
    slot_dir = state / "slots"
    seen: list = []

    def _effect(n, argv, cwd):
        seen.append(try_machine_slot_free(slot_dir, slots=1))
        _clear(folder, KG_CID)

    runner = _Runner(effect=_effect)

    def _dispatch(f, **kw):
        return dr.dispatch(f, backend_probe=lambda *a: True, runner=runner, **kw)

    with mock.patch.object(dr, "_record_resolution"):
        owed_work.run_worker(WorkerDeps(
            state=lambda: _ready(), dispatch=_dispatch, sleep=lambda s: None,
            clock=lambda: 0.0, wall=lambda: 10_000.0, idle_s=0, settle_s=0,
            slot_dir=slot_dir, slots=1, job_log=lambda e: _null(), log=lambda line: None))
    assert seen == [None], "the slot must be held while the job runs"
    assert try_machine_slot_free(slot_dir, slots=1) == 0, "and released after"
    assert owed_work.read_queue() == []


def _null():
    import contextlib

    return contextlib.nullcontext()


# ---------------------------------------------------------------------------
# 3. One run per job; narrow jobs
# ---------------------------------------------------------------------------


def test_three_conditions_needing_the_same_job_run_it_once(tmp_path):
    """ACT. Before: three handler runs, three ``--all`` passes. After: the
    whole-tree job absorbs the backfill and the node-format refresh — one
    ``--all``, three verdicts."""
    folder = _project(tmp_path, "p")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        results = dr.dispatch(folder, condition_ids=[KG_CID, BACKFILL_CID, FORMATS_CID],
                              backend_probe=lambda f, k: True, runner=runner,
                              single_instance=False)
    assert len(runner.calls) == 1, runner.calls
    assert runner.calls[0][-1] == "--all"
    assert sorted(r.condition_id for r in results) == sorted([KG_CID, BACKFILL_CID, FORMATS_CID])
    for cid in (KG_CID, BACKFILL_CID, FORMATS_CID):
        assert _rows(folder, cid)[0] == dr.STARTED


def test_distinct_jobs_still_each_run(tmp_path):
    """LEAVE-ALONE: grouping never merges different work."""
    folder = _project(tmp_path, "p")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"), \
         mock.patch.object(dr, "_code_embed_image_verdict", return_value="current"):
        dr.dispatch(folder, condition_ids=[KG_CID, "code_graph_no_embedding_backend"],
                    backend_probe=lambda f, k: True, runner=runner, single_instance=False)
    assert len(runner.calls) == 2


def test_a_secondary_backfill_runs_the_missing_rows_backfill_not_a_tree_walk(tmp_path):
    """ACT. Before: ``sync_knowledge_graph.py --all``. After: the backfill
    child alone."""
    folder = _project(tmp_path, "p")
    runner = _Runner(rc=0)
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[BACKFILL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert runner.calls == [[runner.calls[0][0], "-m", "vco_lib.owed_work", "backfill",
                             "--folder", str(folder)]]
    assert not any("--all" in c for c in runner.calls)


def test_a_backfill_with_refused_rows_falls_back_to_the_tree_sync(tmp_path):
    """Only kg-sync's writer can re-lay rows the secondary refuses whole."""
    from vco_lib.owed_work import BACKFILL_NEEDS_WALK

    folder = _project(tmp_path, "p")
    runner = _Runner(effect=lambda n, argv, cwd: BACKFILL_NEEDS_WALK if n == 1 else 0)
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[BACKFILL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert len(runner.calls) == 2 and runner.calls[1][-1] == "--all"


def test_run_secondary_backfill_never_walks_and_settles_the_ledger(tmp_path):
    from vco_lib import kg_secondary_backfill as ksb
    from vco_lib import owed_work

    seen: dict = {}

    class _Svc:
        def close(self):
            seen["closed"] = True

    def _backfill(names, svc, folder, log=print):
        seen["names"] = list(names)
        return {("KG", "arctic2_embed"): ksb.COMPLETE}

    with mock.patch.object(ksb, "secondary_write_models", return_value={}), \
         mock.patch.object(ksb, "owed_pairs", return_value=[]):
        rc = owed_work.run_secondary_backfill(
            tmp_path, service_factory=lambda f: _Svc(), collections=lambda f: ["KG"],
            backfill=_backfill, settle=lambda f, owed, refresh: seen.setdefault("settled", owed),
            log=lambda line: None)
    assert rc == owed_work.BACKFILL_DONE
    assert seen == {"names": ["KG"], "settled": [], "closed": True}


def test_the_backfill_scan_selects_only_rows_missing_the_vector():
    """The rows the narrow job embeds: those WITHOUT the slot vector."""
    from vco_lib import kg_secondary_backfill as ksb

    class _Obj:
        def __init__(self, fp, vec):
            self.properties = {"file_path": fp, "content": f"text of {fp}"}
            self.vector = vec

    class _Coll:
        def iterator(self, **kw):
            return iter([_Obj("a.md", {"qwen3_embed": [1], "arctic2_embed": [1]}),
                         _Obj("b.md", {"qwen3_embed": [1]}),
                         _Obj("c.md", {"qwen3_embed": [1], "arctic2_embed": [2]})])

    class _Client:
        class collections:  # noqa: N801
            @staticmethod
            def exists(name):
                return True

            @staticmethod
            def get(name):
                return _Coll()

        def close(self):
            pass

    assert ksb.missing_rows("KG", "arctic2_embed", client_factory=_Client) == [
        ("b.md", "text of b.md")]


def test_a_sync_failure_resyncs_only_the_failed_paths(tmp_path):
    """ACT. Before: ``--all``. After: the recorded failed files only."""
    from vco_lib import owed_work

    folder = _project(tmp_path, "p")
    for name in ("a.md", "b.md", "c.md"):
        (folder / "knowledge" / name).write_text("# n\n", encoding="utf-8")
    prior = owed_work.failed_paths_state(folder)
    _owe(folder, FAIL_CID)
    owed_work.record_sync_failures(folder, ["knowledge/a.md", "knowledge/c.md"],
                                   whole_tree=True, prior=prior)
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    [call] = runner.calls
    assert "--all" not in call
    assert call[-2:] == [str(folder / "knowledge" / "a.md"), str(folder / "knowledge" / "c.md")]


def _owe_failed_paths(folder: Path, count: int, stem: str = "n", subdir: str = "") -> None:
    from vco_lib import owed_work

    rels = []
    for i in range(count):
        rel = f"knowledge/{subdir}{stem}{i}.md"
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text("# n\n", encoding="utf-8")
        rels.append(rel)
    prior = owed_work.failed_paths_state(folder)
    _owe(folder, FAIL_CID)
    owed_work.record_sync_failures(folder, rels, whole_tree=True, prior=prior)


def test_just_below_the_threshold_the_retry_stays_narrow(tmp_path):
    folder = _project(tmp_path, "p")
    _owe_failed_paths(folder, dr.MAX_NARROW_SYNC_PATHS)
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert "--all" not in runner.calls[0]
    assert len(runner.calls[0]) == 2 + 200


def test_above_the_path_count_the_retry_falls_back_to_the_whole_tree(tmp_path):
    folder = _project(tmp_path, "p")
    _owe_failed_paths(folder, dr.MAX_NARROW_SYNC_PATHS + 1)
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert runner.calls[0][-1] == "--all"
    assert len(runner.calls[0]) == 3


def test_above_the_argument_length_the_retry_falls_back_to_the_whole_tree(tmp_path):
    folder = _project(tmp_path, "p")
    _owe_failed_paths(folder, 30, subdir="".join(c * 240 + "/" for c in "dfgh"), stem="x")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert runner.calls[0][-1] == "--all"


def test_a_spawn_that_cannot_start_is_a_failed_attempt_not_a_crash(tmp_path):
    folder = _project(tmp_path, "p")
    _owe_failed_paths(folder, 3)

    def boom(argv, cwd):
        raise OSError("command line too long")

    results = dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                          runner=boom, single_instance=False)
    assert [r.status for r in results] == [dr.FAILED]
    assert dr.attempt_count(folder, FAIL_CID) == 1


def test_a_failure_entry_without_a_record_still_owes_the_whole_tree(tmp_path):
    """LEAVE-ALONE: install.py's enqueued seed re-emits the same condition for
    a WHOLE-TREE seed — a record left by an earlier entry must not narrow it."""
    from vco_lib import owed_work

    folder = _project(tmp_path, "p")
    (folder / "knowledge" / "a.md").write_text("# n\n", encoding="utf-8")
    _owe(folder, FAIL_CID)
    owed_work.record_sync_failures(folder, ["knowledge/a.md"], whole_tree=True,
                                   prior=(False, None))
    record = json.loads((folder / owed_work.FAILED_PATHS_REL).read_text())
    record["detected_at"] = "2000-01-01T00:00:00Z"  # an earlier entry's record
    (folder / owed_work.FAILED_PATHS_REL).write_text(json.dumps(record))
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FAIL_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert runner.calls[0][-1] == "--all"


def test_a_clean_list_over_every_failed_path_settles_the_entry(tmp_path):
    from vco_lib import owed_work
    from vco_lib.deferral_report import DeferralReport

    folder = _project(tmp_path, "p")
    for name in ("a.md", "b.md"):
        (folder / "knowledge" / name).write_text("# n\n", encoding="utf-8")
    _owe(folder, FAIL_CID)
    owed_work.record_sync_failures(folder, ["knowledge/a.md", "knowledge/b.md"],
                                   whole_tree=True, prior=(False, None))
    assert owed_work.settle_clean_sync_paths(folder, [str(folder / "knowledge/a.md")]) is False
    assert DeferralReport.read(folder).has_condition(FAIL_CID)
    assert owed_work.settle_clean_sync_paths(folder, [str(folder / "knowledge/b.md")]) is True
    report = DeferralReport.read(folder)
    assert not (report and report.has_condition(FAIL_CID))
    assert not (folder / owed_work.FAILED_PATHS_REL).exists()


def test_a_list_failure_after_a_whole_tree_entry_keeps_the_whole_tree_owed(tmp_path):
    from vco_lib import owed_work

    folder = _project(tmp_path, "p")
    (folder / "knowledge" / "a.md").write_text("# n\n", encoding="utf-8")
    _owe(folder, FAIL_CID)  # whole-tree owed: no record
    prior = owed_work.failed_paths_state(folder)
    assert prior == (True, None)
    _owe(folder, FAIL_CID)  # the list run's re-emit
    owed_work.record_sync_failures(folder, ["knowledge/a.md"], whole_tree=False, prior=prior)
    assert owed_work.owed_failed_sync_paths(folder) is None


def test_the_node_format_refresh_runs_alone(tmp_path):
    folder = _project(tmp_path, "p")
    runner = _Runner()
    with mock.patch.object(dr, "_record_resolution"):
        dr.dispatch(folder, condition_ids=[FORMATS_CID], backend_probe=lambda f, k: True,
                    runner=runner, single_instance=False)
    assert runner.calls[0][-1] == "--refresh-node-formats"


# ---------------------------------------------------------------------------
# 4. Attempt accounting
# ---------------------------------------------------------------------------


_DYING_WORKER = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from vco_lib import deferral_retry as dr
    folder = Path(sys.argv[1])

    def runner(argv, cwd):
        os._exit(9)  # the worker is killed mid-job (OOM, SIGKILL, reboot)

    dr.dispatch(folder, condition_ids=["kg_sync_no_embedding_backend"],
                backend_probe=lambda f, k: True, runner=runner, single_instance=False)
""")


def test_a_worker_that_died_mid_job_still_counts_the_attempt(tmp_path):
    """The v0.2.91 crash-loop rule: dying is not an excuse."""
    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    script = tmp_path / "dying_worker.py"
    script.write_text(_DYING_WORKER, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), WEAVIATE_URL="http://127.0.0.1:9")
    proc = subprocess.run([sys.executable, str(script), str(folder)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 9, proc.stderr
    assert _rows(folder, KG_CID) == [dr.STARTED]
    assert dr.attempt_count(folder, KG_CID) == 1
    assert dr.retry_history(folder, KG_CID).interrupted == 0


def test_a_weaviate_restart_during_the_job_excuses_the_attempt(tmp_path):
    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    states = iter([_ready(1000.0), _ready(2000.0)])  # container start time moved
    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=_Runner(rc=1), single_instance=False,
                          machine_watch=lambda: next(states))
    assert [r.status for r in results] == [dr.INTERRUPTED]
    assert _rows(folder, KG_CID) == [dr.STARTED, dr.INTERRUPTED]
    assert dr.attempt_count(folder, KG_CID) == 0


def test_readiness_lost_after_the_job_excuses_the_attempt(tmp_path):
    from vco_lib.owed_work import MachineState

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    states = iter([_ready(), MachineState(ready=False)])
    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=_Runner(rc=1), single_instance=False,
                          machine_watch=lambda: next(states))
    assert [r.status for r in results] == [dr.INTERRUPTED]
    assert dr.attempt_count(folder, KG_CID) == 0


def test_an_unchanged_machine_counts_the_failed_attempt(tmp_path):
    """LEAVE-ALONE: same container, still ready → the job failed, it counts."""
    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=_Runner(rc=1), single_instance=False,
                          machine_watch=lambda: _ready(1000.0))
    assert [r.status for r in results] == [dr.FAILED]
    assert dr.attempt_count(folder, KG_CID) == 1


def test_unknown_machine_readings_excuse_nothing(tmp_path):
    from vco_lib.owed_work import MachineState

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    states = iter([MachineState(ready=True, started_at=None), MachineState(ready=None)])
    results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                          runner=_Runner(rc=1), single_instance=False,
                          machine_watch=lambda: next(states))
    assert [r.status for r in results] == [dr.FAILED]


def test_excuses_are_bounded_so_a_job_that_kills_weaviate_still_caps(tmp_path):
    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    statuses = []
    t = [0.0]

    def _watch():
        t[0] += 100.0
        return _ready(t[0])  # every job "restarts" Weaviate

    for _ in range(dr.MAX_EXCUSED_INTERRUPTIONS + dr.MAX_ATTEMPTS + 2):
        results = dr.dispatch(folder, condition_ids=[KG_CID], backend_probe=lambda f, k: True,
                              runner=_Runner(rc=1), single_instance=False,
                              machine_watch=_watch)
        statuses.append(results[0].status)
    assert statuses.count(dr.INTERRUPTED) == dr.MAX_EXCUSED_INTERRUPTIONS
    assert statuses[-1] == dr.SKIPPED and dr.retry_history(folder, KG_CID).cap_reached


def test_machine_failed_between_reads_only_positive_evidence():
    from vco_lib.owed_work import MachineState, machine_failed_between

    assert machine_failed_between(_ready(1.0), MachineState(ready=False)) is True
    assert machine_failed_between(_ready(1.0), _ready(50.0)) is True
    assert machine_failed_between(_ready(1.0), _ready(1.0)) is False
    assert machine_failed_between(_ready(None), _ready(50.0)) is False
    assert machine_failed_between(_ready(1.0), MachineState(ready=None)) is False


# ---------------------------------------------------------------------------
# 5. Real order: readiness lost mid-job, then regained
# ---------------------------------------------------------------------------


def test_real_order_readiness_lost_mid_job_then_regained(state, tmp_path):
    """The worker runs the job; Weaviate dies during it (the run fails); the
    worker keeps the entry, waits while Weaviate is down — running nothing —
    and once Weaviate is back and settled runs the job ONE more time, which
    completes. Two runs in total, one excused attempt, no duplicate."""
    from vco_lib import owed_work
    from vco_lib.owed_work import MachineState, WorkerDeps

    folder = _project(tmp_path, "p")
    _owe(folder, KG_CID)
    owed_work.enqueue(folder)
    machine = {"ready": True, "started_at": 1000.0}
    now = [0.0]
    runs_while_down: list[int] = []

    def _effect(n, argv, cwd):
        if not machine["ready"]:
            runs_while_down.append(n)
        if n == 1:
            machine.update(ready=False)  # Weaviate crashes mid-job
            return 1
        _clear(folder, KG_CID)  # the second run completes: its paired clear
        return 0

    runner = _Runner(effect=_effect)

    def _state():
        return MachineState(ready=machine["ready"], started_at=machine["started_at"])

    def _sleep(s):
        now[0] += s
        if now[0] >= 40 and not machine["ready"]:
            machine.update(ready=True, started_at=1000.0 + now[0])  # back, new container

    def _dispatch(f, **kw):
        return dr.dispatch(f, backend_probe=lambda *a: True, runner=runner, **kw)

    with mock.patch.object(dr, "_record_resolution"):
        report = owed_work.run_worker(WorkerDeps(
            state=_state, dispatch=_dispatch, sleep=_sleep, clock=lambda: now[0],
            wall=lambda: 1000.0 + now[0] + 10_000, idle_s=600, settle_s=10, poll_s=5,
            slot_dir=state / "slots", slots=1, job_log=lambda e: _null(),
            log=lambda line: None))

    assert len(runner.calls) == 2, runner.calls
    assert runs_while_down == []
    assert _rows(folder, KG_CID) == [dr.STARTED, dr.INTERRUPTED, dr.STARTED,
                                     dr.RESOLVED, dr.RETRIED]
    assert report.exit_reason == "queue empty"
    assert owed_work.read_queue() == []


# ---------------------------------------------------------------------------
# 6. Measurement — three projects after a Weaviate restart
# ---------------------------------------------------------------------------


def test_measurement_three_projects_after_a_restart(state, tmp_path):
    """Three projects, each owing the KG seed, the secondary backfill and the
    node-format refresh, open together right after a Weaviate restart.

    Before (v0.2.106, per the audit): 3 drivers, and up to 2 ``--all`` per
    project (``--all`` per owed kg_seed condition until one clears them).
    After: 1 worker spawn; nothing runs until Weaviate is ready; then exactly
    one ``--all`` per project."""
    from vco_lib import owed_work
    from vco_lib.owed_work import MachineState, WorkerDeps

    projects = [_project(tmp_path, f"p{i}") for i in range(3)]
    for folder in projects:
        _owe(folder, KG_CID, BACKFILL_CID, FORMATS_CID)
    spawned: list = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append(argv)

    with mock.patch("subprocess.Popen", _FakePopen), \
         mock.patch.object(dr, "attempt_count", return_value=0):
        for folder in projects:
            dr.session_start_owed_check(folder)
    assert len(spawned) == 1

    machine = {"ready": False}
    now = [0.0]
    runner_calls: list = []

    def _effect(n, argv, cwd):
        runner_calls.append((str(cwd), list(argv), machine["ready"]))
        _clear(Path(cwd), KG_CID, BACKFILL_CID, FORMATS_CID)
        return 0

    runner = _Runner(effect=_effect)

    def _sleep(s):
        now[0] += s
        if now[0] >= 60:
            machine["ready"] = True

    def _dispatch(f, **kw):
        return dr.dispatch(f, backend_probe=lambda *a: True, runner=runner, **kw)

    with mock.patch.object(dr, "_record_resolution"):
        owed_work.run_worker(WorkerDeps(
            state=lambda: MachineState(ready=machine["ready"], started_at=None),
            dispatch=_dispatch, sleep=_sleep, clock=lambda: now[0], wall=lambda: 0.0,
            idle_s=600, settle_s=10, poll_s=5, slot_dir=state / "slots", slots=2,
            job_log=lambda e: _null(), log=lambda line: None))

    all_passes = [c for c in runner_calls if c[1][-1] == "--all"]
    per_project = {str(p): sum(1 for c in all_passes if c[0] == str(p)) for p in projects}
    assert all(c[2] for c in runner_calls), "nothing ran before Weaviate was ready"
    assert per_project == {str(p): 1 for p in projects}, per_project
    print(f"\nMEASUREMENT workers_spawned={len(spawned)} all_passes={len(all_passes)} "
          f"per_project={sorted(per_project.values())}")


# ---------------------------------------------------------------------------
# 7. Slot count from RAM, log names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ram,slots", [(8.0, 1), (16.0, 2), (31.9, 2), (48.0, 3),
                                       (128.0, 4), (0.0, 2), (None, None)])
def test_slot_count_follows_the_ram(ram, slots):
    from vco_lib.atomic import BULK_SLOTS_DEFAULT, machine_slot_count

    if ram is None:  # unreadable RAM probe ⇒ the pre-v0.2.107 default
        with mock.patch("vco_lib.host_probe.probe_system_ram_gb", side_effect=OSError):
            assert machine_slot_count(env={}) == BULK_SLOTS_DEFAULT
        return
    assert machine_slot_count(ram_gb=ram, env={}) == slots


def test_the_slot_env_override_wins():
    from vco_lib.atomic import machine_slot_count

    assert machine_slot_count(ram_gb=8.0, env={"VCO_RESYNC_DRIVER_SLOTS": "3"}) == 3
    assert machine_slot_count(ram_gb=8.0, env={"VCO_RESYNC_DRIVER_SLOTS": "x"}) == 1


def test_the_pool_is_the_resync_drivers_directory(state):
    from vco_lib.atomic import bulk_slot_dir

    assert bulk_slot_dir() == state / "state" / "resync-driver-slots"


def test_job_log_names_carry_the_project_key_and_the_pid(state, tmp_path, monkeypatch):
    from vco_lib import owed_work

    folder = _project(tmp_path, "my proj")
    owed_work.enqueue(folder)
    [entry] = owed_work.read_queue()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(owed_work, "logs_dir", lambda: logs)
    with owed_work._job_log(entry):
        os.write(1, b"child output\n")
    [log] = list(logs.iterdir())
    assert log.name.startswith(f"owed-work-{entry.key}-{os.getpid()}-")
    assert entry.key.startswith("my_proj-")
    assert "child output" in log.read_text()
