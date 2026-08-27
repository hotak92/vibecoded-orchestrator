# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-H — registry-driven retry of transient deferral conditions.

The field failure: ``kg_sync_no_embedding_backend`` records that the embedding
backend was down for the seconds the KG seed ran. It is transient by
definition, yet nothing ever re-ran the seed when the backend came back — two
field projects sat with an empty knowledge graph while the ledger told their
Claude to fix it by hand.

The gate ORDER is the safety property, and every leg of it is pinned here with
BOTH sides (acts / leaves alone):

    no other driver holds this folder's lock
      → registry says auto_retryable + names a handler
        → attempts < cap
          → the probe for THAT handler's backend returns True
            (False AND None both skip)
              → the attempt is recorded, THEN the handler runs
                → ONLY the child's own paired clear (re-read from the
                  ledger) resolves the condition — never the exit code

Fully hermetic: the backend probe and the child-process runner are injected,
so no service is contacted and no subprocess is spawned.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_registry, deferral_retry  # noqa: E402

KG_CID = "kg_sync_no_embedding_backend"
CG_CID = "code_graph_no_embedding_backend"
CG_CODE_CID = "code_graph_code_backend_unreachable"


class _Runner:
    """Records argv instead of spawning."""

    def __init__(self, rc=0):
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        return self.rc


def _project(td: str, *, scripts=("sync_knowledge_graph.py", "analyze_code_graph.py")):
    folder = Path(td)
    scripts_dir = folder / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in scripts:
        (scripts_dir / name).write_text("# stub\n", encoding="utf-8")
    return folder


class RegistryContractTests(unittest.TestCase):
    """The registry rows this WP made TRUE."""

    def test_the_three_tenants_declare_handlers_that_exist(self):
        for cid, handler in (
            (KG_CID, "kg_seed"),
            (CG_CID, "code_graph_walk"),
            (CG_CODE_CID, "code_graph_walk"),
        ):
            self.assertEqual(deferral_registry.retry_handler_for(cid), handler, cid)
            self.assertIn(handler, deferral_retry.HANDLERS, cid)

    def test_tenants_are_auto_retryable_and_paired(self):
        for cid in (KG_CID, CG_CID, CG_CODE_CID):
            spec = deferral_registry.condition(cid)
            self.assertEqual(spec.condition_class, "auto_retryable", cid)
            self.assertEqual(
                spec.clear_probe, "paired-resolution",
                f"{cid} claims a clear it must actually have — WP-H pairs it "
                f"at the retry's success path",
            )

    def test_every_declared_retry_handler_exists(self):
        """A registry row naming a handler nobody implements is the
        documented-protocol-never-implemented class, in a new place."""
        for spec in deferral_registry.all_specs():
            name = spec.retry_handler
            if name is None:
                continue
            self.assertIn(
                name, deferral_retry.HANDLERS,
                f"{spec.pattern} declares retry:py:{name} which does not exist",
            )

    def test_non_retryable_conditions_are_invisible_to_the_dispatcher(self):
        for cid in ("dual_ollama_detected", "mcp_registration_failed",
                    "kg_access_phantom_repaired", "not_registered_at_all"):
            self.assertIsNone(deferral_retry.handler_name_for(cid), cid)

    def test_retry_action_requires_the_auto_retryable_class(self):
        """The class IS the consent record — the loader must refuse a
        retry_action on any other tier rather than silently honouring it."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(
                'format_version = 1\n'
                '[conditions.x]\nclass = "action_required"\nowner = "t"\n'
                'clear_probe = "manual-dismiss"\nemit_surfaces = ["ledger"]\n'
                'retry_action = "retry:py:kg_seed"\n'
            )
            path = Path(fh.name)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                deferral_registry.load_registry(path)
            self.assertIn("auto_retryable", str(ctx.exception))
        finally:
            path.unlink()

    def test_malformed_retry_action_is_rejected(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(
                'format_version = 1\n'
                '[conditions.x]\nclass = "auto_retryable"\nowner = "t"\n'
                'clear_probe = "manual-dismiss"\nemit_surfaces = ["ledger"]\n'
                'retry_action = "kg_seed"\n'
            )
            path = Path(fh.name)
        try:
            with self.assertRaises(RuntimeError):
                deferral_registry.load_registry(path)
        finally:
            path.unlink()


class DispatchGateTests(unittest.TestCase):
    def test_backend_up_retries_and_resolves(self):
        """ACT leg. RED on c67ef888: no dispatcher existed, so this entry was
        immortal even after the user fixed their backend."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner(rc=0)
            resolved: list[str] = []
            with mock.patch.object(deferral_retry, "_record_resolution",
                                   side_effect=lambda f, c, d: resolved.append(c)):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.RETRIED])
        self.assertEqual(resolved, [KG_CID])
        self.assertEqual(len(runner.calls), 1)

    def test_backend_down_leaves_everything_alone(self):
        """LEAVE-ALONE leg: no retry, no resolve, nothing spawned."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            with mock.patch.object(deferral_retry, "_record_resolution") as resolve:
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: False, runner=runner,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("no text embedding backend", results[0].detail)
        self.assertEqual(runner.calls, [])
        resolve.assert_not_called()

    def test_backend_unknown_is_treated_as_down(self):
        """Positive evidence only: a probe that could not run must never be
        read as "the backend is up"."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: None, runner=runner,
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("unknown", results[0].detail)
        self.assertEqual(runner.calls, [])

    def test_a_raising_backend_probe_does_not_retry(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()

            def _boom(_folder, _kind):
                raise RuntimeError("probe blew up")

            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=_boom, runner=runner,
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertEqual(runner.calls, [])

    def test_failed_handler_does_not_resolve(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner(rc=3)
            with mock.patch.object(deferral_retry, "_record_resolution") as resolve:
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.FAILED])
        resolve.assert_not_called()

    def test_attempt_cap_stops_a_permanently_failing_retry(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner(rc=1)
            for _ in range(deferral_retry.MAX_ATTEMPTS):
                deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
            self.assertEqual(len(runner.calls), deferral_retry.MAX_ATTEMPTS)
            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=runner,
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("attempt cap", results[0].detail)
        self.assertEqual(len(runner.calls), deferral_retry.MAX_ATTEMPTS)

    def test_skips_do_not_burn_the_cap(self):
        """A machine whose backend stays down for a week must still retry on
        the day it comes back."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner(rc=0)
            for _ in range(10):
                deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: False, runner=runner,
                )
            with mock.patch.object(deferral_retry, "_record_resolution"):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.RETRIED])

    def test_backend_is_probed_once_per_kind_per_dispatch(self):
        """Cached per BACKEND KIND, not once for the whole pass: the KG seed
        and the analyzer walk ask different questions, and one answer cannot
        stand for both."""
        calls = []

        def _probe(folder, kind):
            calls.append((folder, kind))
            return True

        with TemporaryDirectory() as td:
            folder = _project(td)
            with mock.patch.object(deferral_retry, "_record_resolution"):
                deferral_retry.dispatch(
                    folder,
                    # two code-graph cids + one kg cid → still ONE probe each
                    condition_ids=[KG_CID, CG_CID, CG_CODE_CID],
                    backend_probe=_probe, runner=_Runner(),
                )
        self.assertEqual(
            sorted(k for _f, k in calls),
            [deferral_retry.CODE_BACKEND, deferral_retry.TEXT_BACKEND],
        )

    def test_each_handler_is_gated_on_the_backend_its_work_needs(self):
        """MAJOR-1b / MINOR-3, RED pre-fix: the gate constructed an
        EmbeddingService (an EITHER-backend question), which passes in exactly
        the state `code_graph_code_backend_unreachable` was emitted in — so the
        analyzer retry was dispatched into its own skip path and burned an
        attempt every session."""
        seen = {}

        def _probe(_folder, kind):
            seen[kind] = True
            return kind == deferral_retry.TEXT_BACKEND  # code backend DOWN

        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner(rc=0)
            with mock.patch.object(deferral_retry, "_record_resolution"):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID, CG_CODE_CID],
                    backend_probe=_probe, runner=runner,
                )
            by_cid = {r.condition_id: r for r in results}
            self.assertEqual(by_cid[KG_CID].status, deferral_retry.RETRIED)
            self.assertEqual(by_cid[CG_CODE_CID].status, deferral_retry.SKIPPED)
            self.assertIn("code embedding backend", by_cid[CG_CODE_CID].detail)
            # ONE child spawned — the analyzer was never run.
            self.assertEqual(len(runner.calls), 1)
            self.assertIn("sync_knowledge_graph.py", runner.calls[0][1])
            # …and the hopeless run did not burn the analyzer's cap.
            self.assertEqual(
                deferral_retry.attempt_count(folder, CG_CODE_CID), 0
            )

    def test_unknown_handler_name_skips_rather_than_crashing(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            with mock.patch.object(deferral_retry, "handler_name_for",
                                   return_value="nope"):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=_Runner(),
                )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("unknown handler", results[0].detail)


class HandlerArgvTests(unittest.TestCase):
    def test_kg_seed_reuses_the_shipped_hash_gated_sync(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            ctx = deferral_retry.RetryContext(
                folder=folder, condition_id=KG_CID, runner=runner, python="/py",
            )
            result = deferral_retry.retry_kg_seed(ctx)
        self.assertEqual(result.status, deferral_retry.RETRIED)
        argv = runner.calls[0]
        self.assertEqual(argv[0], "/py")
        self.assertTrue(argv[1].endswith(".claude/scripts/sync_knowledge_graph.py"))
        self.assertEqual(argv[2], "--all")

    def test_kg_seed_prefers_the_bundled_copy_over_the_template(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".claude" / "scripts").mkdir(parents=True)
            (folder / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("x")
            (folder / "templates" / "scripts").mkdir(parents=True)
            (folder / "templates" / "scripts" / "sync_knowledge_graph.py").write_text("x")
            runner = _Runner()
            deferral_retry.retry_kg_seed(deferral_retry.RetryContext(
                folder=folder, condition_id=KG_CID, runner=runner, python="/py",
            ))
        self.assertIn(".claude", runner.calls[0][1])

    def test_kg_seed_skips_when_no_script_exists(self):
        with TemporaryDirectory() as td:
            runner = _Runner()
            result = deferral_retry.retry_kg_seed(deferral_retry.RetryContext(
                folder=Path(td), condition_id=KG_CID, runner=runner, python="/py",
            ))
        self.assertEqual(result.status, deferral_retry.SKIPPED)
        self.assertEqual(runner.calls, [])

    def test_kg_seed_restores_the_env_it_pinned(self):
        import os

        sentinel = "/previous/root"
        os.environ["KG_SYNC_PROJECT_ROOT"] = sentinel
        try:
            with TemporaryDirectory() as td:
                folder = _project(td)
                deferral_retry.retry_kg_seed(deferral_retry.RetryContext(
                    folder=folder, condition_id=KG_CID, runner=_Runner(),
                    python="/py",
                ))
            self.assertEqual(os.environ["KG_SYNC_PROJECT_ROOT"], sentinel)
        finally:
            os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

    def test_code_graph_walk_is_incremental_and_never_destructive(self):
        """--force-recreate DROPS the collections; --prune-stale deletes every
        row of a hash-skipped file. A retry has consent for neither."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            with mock.patch.object(deferral_retry, "_project_name",
                                   return_value="Proj"):
                result = deferral_retry.retry_code_graph_walk(
                    deferral_retry.RetryContext(
                        folder=folder, condition_id=CG_CID, runner=runner,
                        python="/py",
                    )
                )
        self.assertEqual(result.status, deferral_retry.RETRIED)
        argv = runner.calls[0]
        self.assertIn("--incremental", argv)
        self.assertIn("--project", argv)
        self.assertEqual(argv[argv.index("--project") + 1], "Proj")
        self.assertNotIn("--force-recreate", argv)
        self.assertNotIn("--prune-stale", argv)

    def test_code_graph_walk_omits_project_when_unresolvable(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            with mock.patch.object(deferral_retry, "_project_name",
                                   return_value=None):
                deferral_retry.retry_code_graph_walk(
                    deferral_retry.RetryContext(
                        folder=folder, condition_id=CG_CID, runner=runner,
                        python="/py",
                    )
                )
        self.assertNotIn("--project", runner.calls[0])


class AttemptLedgerTests(unittest.TestCase):
    def test_rows_are_jsonl_and_name_the_decision(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            deferral_retry.record_attempt(
                folder, deferral_retry.RetryResult(KG_CID, deferral_retry.FAILED, "why"),
            )
            rows = [
                json.loads(line)
                for line in deferral_retry.attempts_path(folder)
                .read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(rows[0]["condition_id"], KG_CID)
        self.assertEqual(rows[0]["status"], deferral_retry.FAILED)
        self.assertEqual(rows[0]["detail"], "why")
        self.assertIn("ts", rows[0])

    def test_absent_trail_counts_as_zero_attempts(self):
        with TemporaryDirectory() as td:
            self.assertEqual(deferral_retry.attempt_count(Path(td), KG_CID), 0)

    def test_corrupt_lines_are_ignored(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            path = deferral_retry.attempts_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                'not json\n{"condition_id": "%s", "status": "started"}\n' % KG_CID,
                encoding="utf-8",
            )
            self.assertEqual(deferral_retry.attempt_count(folder, KG_CID), 1)

    def test_only_started_rows_count_toward_the_cap(self):
        """One STARTED row per invocation. Counting OUTCOME rows (pre-fix)
        double-counted a completed attempt and — worse — counted a CRASHED
        handler as zero, so the cap could never engage on the failure mode it
        exists for."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            for status in (deferral_retry.STARTED, deferral_retry.FAILED,
                           deferral_retry.SKIPPED, deferral_retry.INCONCLUSIVE):
                deferral_retry.record_attempt(
                    folder, deferral_retry.RetryResult(KG_CID, status, "x"),
                )
            self.assertEqual(deferral_retry.attempt_count(folder, KG_CID), 1)

    def test_record_attempt_never_raises_on_an_unwritable_folder(self):
        deferral_retry.record_attempt(
            Path("/proc/definitely/not/writable"),
            deferral_retry.RetryResult(KG_CID, deferral_retry.FAILED, "x"),
        )


class OwedWorkTests(unittest.TestCase):
    def test_owed_ids_filter_the_ledger_through_the_registry(self):
        self.assertEqual(
            deferral_retry.retryable_condition_ids(
                ["mcp_registration_failed", KG_CID, CG_CID, KG_CID],
            ),
            [KG_CID, CG_CID],
        )

    def test_owed_condition_ids_reads_the_real_ledger(self):
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        with TemporaryDirectory() as td:
            folder = Path(td)
            emit(folder, DeferralEntry(
                condition_id=KG_CID, title="t", detected="d",
                why_deferred="w", command_to_apply="c", severity="warning",
            ))
            self.assertEqual(deferral_retry.owed_condition_ids(folder), [KG_CID])

    def test_owed_condition_ids_on_an_empty_folder(self):
        with TemporaryDirectory() as td:
            self.assertEqual(deferral_retry.owed_condition_ids(Path(td)), [])


def _emit(folder: Path, cid: str) -> None:
    """Write ONE real ledger entry through the locked emitter."""
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    emit(folder, DeferralEntry(
        condition_id=cid, title="t", detected="d",
        why_deferred="w", command_to_apply="c", severity="warning",
    ))


def _auto_resolution_rows(folder: Path) -> list[dict]:
    path = folder / ".claude" / "logs" / "auto-resolutions.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


class ExitZeroIsNotProofTests(unittest.TestCase):
    """MAJOR-1(a): the CHILD's own paired clear is the only evidence.

    Both tenants ``return 0`` on their backend SKIP paths AFTER re-emitting
    the very entry being retried (``analyze_code_graph.py`` twice,
    ``sync_knowledge_graph.py`` once). Resolving on ``rc == 0`` therefore
    deleted the row the child had just written — and, because
    ``resolve_conditions`` tombstones the id for the run, nothing could put it
    back — while ``auto-resolutions.jsonl`` recorded "completed" for a seed
    that never ran.
    """

    def test_a_skip_path_child_leaves_the_entry_alone(self):
        """RED pre-fix: the entry was DELETED and a success row was logged."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            _emit(folder, KG_CID)

            def _skip_path_runner(argv, cwd):
                # What the shipped script does when the backend is still down:
                # re-emit, then exit 0.
                _emit(folder, KG_CID)
                return 0

            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=_skip_path_runner,
            )

            self.assertEqual(
                [r.status for r in results], [deferral_retry.INCONCLUSIVE],
            )
            self.assertIn("STILL in the ledger", results[0].detail)
            self.assertEqual(
                deferral_retry.owed_condition_ids(folder), [KG_CID],
                "the entry the child just re-wrote must SURVIVE",
            )
            self.assertEqual(
                _auto_resolution_rows(folder), [],
                "no 'completed' row may be logged for skipped work",
            )

    def test_a_child_that_clears_its_own_condition_resolves_it(self):
        """LEAVE-ALONE half — genuine success still resolves + logs."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            _emit(folder, KG_CID)

            def _success_runner(argv, cwd):
                # What the shipped script does on a fully-successful sync:
                # its own narrow clear.
                from vco_lib.deferral_emit import resolve_conditions
                resolve_conditions(folder, (KG_CID,))
                return 0

            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=_success_runner,
            )

            self.assertEqual(
                [r.status for r in results], [deferral_retry.RETRIED],
            )
            self.assertEqual(deferral_retry.owed_condition_ids(folder), [])
            rows = _auto_resolution_rows(folder)
            self.assertTrue(
                any(r.get("condition_id") == KG_CID for r in rows), rows,
            )

    def test_an_unreadable_ledger_is_not_evidence_of_a_clear(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            with mock.patch.object(deferral_retry, "condition_cleared",
                                   return_value=None):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=_Runner(rc=0),
                )
        self.assertEqual(
            [r.status for r in results], [deferral_retry.INCONCLUSIVE],
        )
        self.assertIn("could not be re-read", results[0].detail)

    def test_the_dispatcher_never_calls_resolve_conditions_itself(self):
        """It must not resolve OR tombstone: the child already did, and a
        second resolve is exactly how a re-emitted entry got erased."""
        src = (REPO_ROOT / "vco_lib" / "deferral_retry.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("resolve_conditions(", src)

    def test_both_tenants_carry_the_narrow_clear_the_dispatcher_relies_on(self):
        """The gate is only meaningful if the children actually clear."""
        sync = (REPO_ROOT / "templates" / "scripts"
                / "sync_knowledge_graph.py").read_text(encoding="utf-8")
        self.assertIn("_clear_sync_deferral_no_backend(PROJECT_ROOT)", sync)
        analyzer = (REPO_ROOT / "templates" / "scripts"
                    / "analyze_code_graph.py").read_text(encoding="utf-8")
        self.assertIn(
            '_deferral_op("clear_backend_deferrals", deferral_root)', analyzer,
            "the analyzer must CALL its clear on the success path",
        )
        # …and the tenancy itself (both cids + the clear) lives in the one
        # vco_lib home the analyzer's thin wrappers delegate to.
        from vco_lib import codegraph_deferrals

        self.assertEqual(
            set(codegraph_deferrals.CODE_GRAPH_BACKEND_CIDS),
            {CG_CID, CG_CODE_CID},
        )
        self.assertIn("clear_backend_deferrals", analyzer)
        self.assertTrue(callable(codegraph_deferrals.clear_backend_deferrals))


def _analyzer_child(*, walk_succeeds: bool):
    """Stand-in for ``analyze_code_graph.py``'s LEDGER-root behaviour.

    Resolves its root exactly as the shipped script does — ``--deferral-root``
    wins, else ``$VCT_ORCHESTRATOR_ROOT``, else ``repo_path`` — and then runs
    the REAL :mod:`vco_lib.codegraph_deferrals` emit / clear against it. Only
    the walk itself is faked; every ledger write here is the shipped one.
    """
    def _run(argv, cwd):
        from vco_lib import codegraph_deferrals as cd

        if "--deferral-root" in argv:
            root = Path(argv[argv.index("--deferral-root") + 1])
        elif os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip():
            root = Path(os.environ["VCT_ORCHESTRATOR_ROOT"])
        else:
            root = Path(argv[2])
        if walk_succeeds:
            cd.clear_backend_deferrals(root)
        else:
            cd.emit_no_backend(root, RuntimeError("backend still down"))
        return 0

    return _run


def _ledger_cids(folder: Path) -> list[str]:
    from vco_lib.deferral_report import DeferralReport

    report = DeferralReport.read(Path(folder))
    return [e.condition_id for e in report.entries] if report else []


class LedgerRootPinTests(unittest.TestCase):
    """MAJOR-A (wave-3 re-review): the child must write the ledger we READ.

    On a launcher-/bundle-managed USER project the analyzer's two backend
    entries land in the PROJECT's ledger — the launcher deliberately leaves
    ``VCT_ORCHESTRATOR_ROOT`` unset (``codegraph.rs``), so the analyzer's
    ``install_root`` IS the project. At RETRY time the session-start
    environment DOES carry ``VCT_ORCHESTRATOR_ROOT`` (projected via
    ``.claude/env``) and the child inherits it, so without an explicit pin:

    * the child's success-path clear resolves in the ORCHESTRATOR clone — a
      cross-ledger resolve on the PROJECT's evidence, and
    * ``condition_cleared(folder=P)`` still finds the entry ⇒ INCONCLUSIVE on
      every attempt until the cap burns and the entry is immortal in P.

    ``retry_kg_seed`` already pins its equivalent (``KG_SYNC_PROJECT_ROOT``);
    this is the analyzer's missing half, carried on argv instead of env.
    """

    def test_code_graph_walk_pins_the_ledger_root_to_the_dispatchers_folder(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            runner = _Runner()
            with mock.patch.object(deferral_retry, "_project_name",
                                   return_value="Proj"):
                deferral_retry.retry_code_graph_walk(
                    deferral_retry.RetryContext(
                        folder=folder, condition_id=CG_CID, runner=runner,
                        python="/py",
                    )
                )
        argv = runner.calls[0]
        self.assertIn("--deferral-root", argv)
        self.assertEqual(
            argv[argv.index("--deferral-root") + 1], str(folder),
            "the pinned root must be the folder whose ledger dispatch re-reads",
        )

    def test_a_retry_clears_the_ledger_the_dispatcher_reads(self):
        """RED pre-fix: the child cleared the ORCH clone, dispatch re-read P,
        found the entry ⇒ INCONCLUSIVE with an attempt burned."""
        with TemporaryDirectory() as td, TemporaryDirectory() as orch:
            folder, orch_root = _project(td), Path(orch)
            _emit(folder, CG_CID)
            with mock.patch.dict(
                os.environ, {"VCT_ORCHESTRATOR_ROOT": str(orch_root)},
            ), mock.patch.object(
                deferral_retry, "_project_name", return_value="Proj",
            ):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[CG_CID],
                    backend_probe=lambda f, k: True,
                    runner=_analyzer_child(walk_succeeds=True),
                )

            self.assertEqual(
                [r.status for r in results], [deferral_retry.RETRIED],
                f"expected RETRIED, got {[(r.status, r.detail) for r in results]}",
            )
            self.assertEqual(deferral_retry.owed_condition_ids(folder), [])
            self.assertTrue(
                any(r.get("condition_id") == CG_CID
                    for r in _auto_resolution_rows(folder)),
            )

    def test_the_orchestrator_ledger_is_untouched_by_a_project_walk(self):
        """No cross-ledger resolve: P's walk is evidence about P only."""
        with TemporaryDirectory() as td, TemporaryDirectory() as orch:
            folder, orch_root = _project(td), Path(orch)
            _emit(folder, CG_CID)
            _emit(orch_root, CG_CID)
            with mock.patch.dict(
                os.environ, {"VCT_ORCHESTRATOR_ROOT": str(orch_root)},
            ), mock.patch.object(
                deferral_retry, "_project_name", return_value="Proj",
            ):
                deferral_retry.dispatch(
                    folder, condition_ids=[CG_CID],
                    backend_probe=lambda f, k: True,
                    runner=_analyzer_child(walk_succeeds=True),
                )

            self.assertEqual(_ledger_cids(folder), [])
            self.assertEqual(
                _ledger_cids(orch_root), [CG_CID],
                "the orchestrator clone's own entry must SURVIVE a project walk",
            )

    def test_a_still_down_retry_re_emits_into_the_folder_not_the_clone(self):
        """Emit-root == clear-root == dispatcher-read-root: ONE variable.

        A retry that runs while the backend is still down re-emits — that
        re-emit has to land where the reader looks, or the ledger the user
        sees and the ledger VCO reasons about diverge.
        """
        with TemporaryDirectory() as td, TemporaryDirectory() as orch:
            folder, orch_root = _project(td), Path(orch)
            _emit(folder, CG_CID)
            with mock.patch.dict(
                os.environ, {"VCT_ORCHESTRATOR_ROOT": str(orch_root)},
            ), mock.patch.object(
                deferral_retry, "_project_name", return_value="Proj",
            ):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[CG_CID],
                    backend_probe=lambda f, k: True,
                    runner=_analyzer_child(walk_succeeds=False),
                )

            self.assertEqual(
                [r.status for r in results], [deferral_retry.INCONCLUSIVE],
            )
            self.assertEqual(_ledger_cids(folder), [CG_CID])
            self.assertEqual(
                _ledger_cids(orch_root), [],
                "a project retry must never write into the orchestrator clone",
            )


class DriverHardeningTests(unittest.TestCase):
    """MINOR-4: the attempt is recorded BEFORE the handler, and two drivers
    cannot run over the same folder at once."""

    def test_a_crashing_handler_still_consumes_its_attempt(self):
        """RED pre-fix: `record_attempt` ran AFTER the handler, so a handler
        that raised left no row — the cap could never engage."""
        with TemporaryDirectory() as td:
            folder = _project(td)

            def _boom(argv, cwd):
                raise RuntimeError("child blew up")

            for _ in range(deferral_retry.MAX_ATTEMPTS):
                with self.assertRaises(RuntimeError):
                    deferral_retry.dispatch(
                        folder, condition_ids=[KG_CID],
                        backend_probe=lambda f, k: True, runner=_boom,
                    )
            self.assertEqual(
                deferral_retry.attempt_count(folder, KG_CID),
                deferral_retry.MAX_ATTEMPTS,
            )
            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=_Runner(rc=0),
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("attempt cap", results[0].detail)

    def test_a_second_driver_declines_while_one_is_running(self):
        """ACT leg of the single-instance guard: a live pidfile blocks."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            path = deferral_retry.pidfile_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            # A pid that is alive but is NOT us: our own parent.
            import os as _os
            path.write_text(f"{_os.getppid()}\n", encoding="utf-8")
            runner = _Runner(rc=0)
            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=runner,
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertIn("already running", results[0].detail)
        self.assertEqual(runner.calls, [], "nothing may be spawned")

    def test_a_stale_pidfile_does_not_block_forever(self):
        """LEAVE-ALONE leg: a dead pid's lock is taken over."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            path = deferral_retry.pidfile_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("4194304\n", encoding="utf-8")  # provably gone
            runner = _Runner(rc=0)
            with mock.patch.object(deferral_retry, "_record_resolution"):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
            self.assertEqual(
                [r.status for r in results], [deferral_retry.RETRIED],
            )
            self.assertEqual(len(runner.calls), 1)
            self.assertFalse(path.exists(), "the lock is released on exit")

    def test_an_ancient_lock_is_abandoned_even_if_its_pid_looks_alive(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            path = deferral_retry.pidfile_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            import os as _os
            path.write_text(f"{_os.getppid()}\n", encoding="utf-8")
            old = time.time() - deferral_retry.PIDFILE_STALE_SECONDS - 60
            _os.utime(path, (old, old))
            runner = _Runner(rc=0)
            with mock.patch.object(deferral_retry, "_record_resolution"):
                results = deferral_retry.dispatch(
                    folder, condition_ids=[KG_CID],
                    backend_probe=lambda f, k: True, runner=runner,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.RETRIED])

    def test_the_liveness_probe_has_one_home(self):
        """install.py's copy and the driver's guard must ask the same
        function — the Windows os.kill footgun gets exactly one handling."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        body = src[src.index("def _pid_is_alive_for_deferral"):]
        body = body[: body.index("\ndef ", 10)]
        self.assertIn("from vco_lib.deferral_probes import pid_is_alive", body)
        self.assertNotIn("OpenProcess", body)


class SessionStartTriggerTests(unittest.TestCase):
    def test_spawns_only_when_something_is_owed(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch.object(deferral_retry, "owed_condition_ids",
                                   return_value=[]), \
                 mock.patch.object(deferral_retry, "spawn_detached") as spawn:
                self.assertEqual(
                    deferral_retry.session_start_owed_check(folder), [],
                )
                spawn.assert_not_called()

            with mock.patch.object(deferral_retry, "owed_condition_ids",
                                   return_value=[KG_CID]), \
                 mock.patch.object(deferral_retry, "spawn_detached") as spawn:
                self.assertEqual(
                    deferral_retry.session_start_owed_check(folder), [KG_CID],
                )
                spawn.assert_called_once()

    def test_a_failed_spawn_never_raises_into_the_hook(self):
        with TemporaryDirectory() as td:
            with mock.patch.object(deferral_retry, "owed_condition_ids",
                                   return_value=[KG_CID]), \
                 mock.patch.object(deferral_retry, "spawn_detached",
                                   side_effect=OSError("boom")):
                self.assertEqual(
                    deferral_retry.session_start_owed_check(Path(td)), [KG_CID],
                )

    def test_detached_spawn_is_a_new_session_on_posix(self):
        """Mechanism test, no reactor and no real child: the driver must
        outlive the hook that started it (the codegraph-resync precedent)."""
        import os

        captured = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs

        with TemporaryDirectory() as td:
            with mock.patch("subprocess.Popen", _FakePopen):
                ok = deferral_retry.spawn_detached(Path(td), python="/py")
        self.assertTrue(ok)
        self.assertEqual(
            captured["argv"][:4], ["/py", "-m", "vco_lib.deferral_retry", "--folder"],
        )
        if os.name == "posix":
            self.assertTrue(captured["kwargs"]["start_new_session"])

    def test_both_session_start_hooks_call_the_shared_helper(self):
        """The bash and PowerShell siblings must CALL the one home rather than
        each carrying a copy of the owed-work rule."""
        for name in ("session-start-deferral-surface.sh",
                     "session-start-deferral-surface.ps1"):
            text = (REPO_ROOT / "templates" / "hooks" / name).read_text(
                encoding="utf-8",
            )
            self.assertIn("session_start_owed_check", text, name)
            self.assertIn("from vco_lib.deferral_retry import", text, name)
            self.assertIn("except Exception:", text, name)


class CliTests(unittest.TestCase):
    def test_list_mode_is_read_only(self):
        with TemporaryDirectory() as td:
            folder = _project(td)
            with mock.patch.object(deferral_retry, "owed_condition_ids",
                                   return_value=[KG_CID]), \
                 mock.patch.object(deferral_retry, "dispatch") as dispatch:
                rc = deferral_retry.main(["--folder", str(folder), "--list", "--json"])
        self.assertEqual(rc, 0)
        dispatch.assert_not_called()

    def test_exit_zero_even_when_nothing_could_be_retried(self):
        """A best-effort background driver must not turn a legitimate skip
        into a visible failure."""
        with TemporaryDirectory() as td:
            folder = _project(td)
            with mock.patch.object(
                deferral_retry, "dispatch",
                return_value=[deferral_retry.RetryResult(
                    KG_CID, deferral_retry.SKIPPED, "backend down")],
            ):
                self.assertEqual(
                    deferral_retry.main(["--folder", str(folder)]), 0,
                )


if __name__ == "__main__":
    unittest.main()
