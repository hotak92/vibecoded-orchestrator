# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The spawn seam: no doomed detached children, and a retry that is not blocked.

THE FIELD DEFECT (2026-09-09, Linux). "Update all bundles" updated 8 projects
and reported for each: *"code-graph re-index started in the background …
Progress: <vct_root>/logs/resync-*.log"*. Every non-root project's log was ~1 KB
and contained only::

    ModuleNotFoundError: No module named 'vco_lib'
      code-summary: Weaviate unreachable: No module named 'weaviate'

Nothing was wrong with the walk; the CHILD could not import its own package.
The launcher spawned the bundle update with a bare PATH `python3`, so
`sys.executable` inside `project_init` was `/usr/bin/python3`, and
`spawn_background_resync` passed that plus a FILE path
(`str(Path(__file__).resolve())`) to a child whose cwd was the USER project.
`Popen` succeeded, so the function returned `launched`, so the GUI said success.

Four properties keep that from recurring, and this file pins all four:

  1. the child is launched as ``-m vco_lib.codegraph_resync`` — a MODULE, so
     the import does not depend on the child's cwd;
  2. the interpreter comes from ``vco_lib.python_exe``, never ``sys.executable``;
  3. an interpreter that cannot import the child's own package produces
     ``status="failed"`` — NOT ``launched``, and NOT the softer ``skipped``
     (which the caller logs at phase "ok" and never shows the user);
  4. the failure propagates through ``ensure_extractor_generation`` and leaves
     the completion stamp UNWRITTEN, so the next bundle update retries.
"""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_extractor_generation as ceg  # noqa: E402
from vco_lib import codegraph_resync as cr  # noqa: E402
from vco_lib import python_exe as px  # noqa: E402


class _FakeProc:
    pid = 4321


def _stub_analyzer_tree(root: Path) -> None:
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")


class SpawnGuardBase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = self.root / "user-project"
        self.repo.mkdir()
        _stub_analyzer_tree(self.repo)
        os.environ["VCT_STATE_DIR"] = str(self.root / "vct-state")
        self._saved_state = os.environ.get("VCT_STATE_DIR")
        self.addCleanup(os.environ.pop, "VCT_STATE_DIR", None)
        px.clear_preflight_cache()
        self.addCleanup(px.clear_preflight_cache)
        # Never touch a real code-embed service or Weaviate.
        patcher = mock.patch.object(cr, "code_embed_service_healthy", lambda *a, **k: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        counts = mock.patch.object(cr, "count_stale_rows", lambda *a, **k: None)
        counts.start()
        self.addCleanup(counts.stop)

    def warm(self, python_exe) -> tuple[bool, str]:
        """Run the REAL preflight once, before `Popen` is faked.

        The probe is a subprocess, and `subprocess.run` goes through
        `subprocess.Popen` — the very symbol these tests replace. Warming the
        per-interpreter memo first keeps the verdict REAL (it is measured
        against the actual interpreter) while leaving the fake free to record
        the spawn argvs. Production warms the same memo the same way: once per
        process, which is what makes an 8-project "Update all" pay for one probe.

        When the interpreter being warmed is THIS runner's own and it cannot
        import the stack from a neutral cwd (a CI runner without the editable
        install), the tests that assume "launched" have no honest verdict:
        skip with the reason rather than fail on the runner or pass vacuously.
        A deliberately broken interpreter (the refusal tests) is never skipped.
        """
        ok, why = px.preflight(python_exe)
        if not ok and str(python_exe) == sys.executable:
            self.skipTest(
                f"runner interpreter cannot preflight the stack ({why}); "
                "CI installs the package editable so this does not skip there"
            )
        return ok, why

    def spawned_argvs(self):
        """Patch Popen, returning the list of argvs it was handed."""
        seen: list[list[str]] = []

        def _fake_popen(argv, **kwargs):
            seen.append(list(argv))
            return _FakeProc()

        p = mock.patch.object(cr.subprocess, "Popen", _fake_popen)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def broken_interpreter(self) -> Path:
        """A real, executable `python3` that cannot import `vco_lib`.

        Its `-c` prints the probe's own JSON contract naming the missing module
        — the observable behaviour of the `/usr/bin/python3` the field machine
        handed to all 8 detached children.
        """
        fake = self.root / "fakebin" / "python3"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(
            "#!/bin/sh\n"
            "cat <<'EOF'\n"
            "[\"vco_lib: ModuleNotFoundError: No module named 'vco_lib'\"]\n"
            "EOF\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        return fake


class TheChildIsLaunchedAsAModule(SpawnGuardBase):
    def test_the_driver_argv_is_dash_m_not_a_file_path(self):
        self.warm(sys.executable)
        seen = self.spawned_argvs()
        result = cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
        )
        self.assertEqual(result.status, "launched", result.message)
        driver = next(a for a in seen if "--run-resync" in a)
        self.assertEqual(
            driver[1:3], ["-m", "vco_lib.codegraph_resync"],
            "the child must be launched as a MODULE — a file path makes its "
            "`import vco_lib` depend on a cwd that is the USER PROJECT",
        )
        self.assertFalse(
            any(a.endswith("codegraph_resync.py") for a in driver),
            "no file path to this module may appear in the child's argv",
        )

    def test_every_rider_child_is_also_launched_as_a_module(self):
        self.warm(sys.executable)
        seen = self.spawned_argvs()
        cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
        )
        riders = [a for a in seen if "--prune-ignored" in a or "--backfill-metadata" in a]
        self.assertEqual(len(riders), 2, "prune + backfill riders must both spawn")
        for argv in riders:
            self.assertEqual(argv[1:3], ["-m", "vco_lib.codegraph_resync"])

    def test_every_child_runs_the_same_interpreter(self):
        self.warm(sys.executable)
        seen = self.spawned_argvs()
        cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
        )
        self.assertTrue(seen)
        self.assertEqual(
            {a[0] for a in seen}, {sys.executable},
            "the driver and its riders must share one resolved interpreter",
        )

    def test_the_interpreter_comes_from_the_resolver_not_sys_executable(self):
        seen = self.spawned_argvs()
        chosen = self.root / "resolved" / "bin" / "python"
        chosen.parent.mkdir(parents=True)
        chosen.write_text("", encoding="utf-8")
        with mock.patch.object(
            px, "resolve_vco_lib_python_or_none", return_value=chosen
        ), mock.patch.object(px, "preflight", return_value=(True, "")):
            result = cr.spawn_background_resync(self.repo, "MyProj", check_owed=False)
        self.assertEqual(result.status, "launched", result.message)
        self.assertEqual({a[0] for a in seen}, {str(chosen)})
        self.assertNotEqual(
            str(chosen), sys.executable,
            "sanity: the fixture must differ from sys.executable",
        )


class TheSpawnRefusesADoomedChild(SpawnGuardBase):
    def test_a_python_that_cannot_import_vco_lib_yields_failed_not_launched(self):
        """RED-PROOF. Under the old code this spawned; the walk then died in a
        ~1 KB log while the caller reported success."""
        broken = self.broken_interpreter()
        # Measure the (real) verdict BEFORE the Popen fake is installed.
        ok, detail = self.warm(broken)
        self.assertFalse(ok, "fixture sanity: this interpreter must be broken")
        self.assertIn("vco_lib", detail)
        seen = self.spawned_argvs()
        result = cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=str(broken), check_owed=False,
        )
        self.assertEqual(
            result.status, "failed",
            "an interpreter that cannot import the child's own package must "
            "NOT produce a 'launched' result",
        )
        self.assertIn(str(broken), result.message,
                      "the failure must name the interpreter")
        self.assertIn("vco_lib", result.message,
                      "the failure must name the missing module")
        self.assertEqual(seen, [], "NOTHING may be spawned once the guard fires")
        self.assertIsNone(result.pid)

    def test_failed_is_distinct_from_skipped(self):
        """`skipped` is logged at phase "ok" by `project_init` and never shown
        to the user. A broken install must not hide there."""
        broken = self.broken_interpreter()
        self.warm(broken)
        failed = cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=str(broken), check_owed=False,
        )
        empty_name = cr.spawn_background_resync(self.repo, "", check_owed=False)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(empty_name.status, "skipped")

    def test_an_unresolvable_interpreter_also_fails_loudly(self):
        seen = self.spawned_argvs()
        with mock.patch.object(px, "resolve_vco_lib_python_or_none", return_value=None):
            result = cr.spawn_background_resync(self.repo, "MyProj", check_owed=False)
        self.assertEqual(result.status, "failed")
        self.assertIn("install.py --update", result.message)
        self.assertEqual(seen, [])

    def test_the_log_header_names_the_interpreter_and_the_argv(self):
        """Deliverable 7: the next incident must be diagnosable from the log."""
        self.warm(sys.executable)
        self.spawned_argvs()
        result = cr.spawn_background_resync(
            self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
        )
        self.assertEqual(result.status, "launched", result.message)
        logs = sorted((self.root / "vct-state" / "logs").glob("resync-MyProj-*.log"))
        self.assertEqual(len(logs), 1)
        body = logs[0].read_text(encoding="utf-8")
        self.assertIn(f"# interpreter: {sys.executable}", body)
        self.assertIn(f"# cwd: {self.repo}", body)
        self.assertIn("# argv: ", body)
        self.assertIn("-m vco_lib.codegraph_resync", body)
        self.assertIn("--log-path", body,
                      "the LOGGED argv must be the FINAL one that is executed")


class TheLogHandleIsNeverLeaked(SpawnGuardBase):
    """R7/3: the degrade-to-DEVNULL path must CLOSE the handle it abandons.

    `_write_spawn_header` can raise on an already-open handle (that is the
    degrade this `except` exists for), and setting the local to `None` is not
    closing it — the fd would survive until GC, inside a function whose entire
    discipline is that the parent owns nothing once the children are spawned.
    """

    def test_a_failing_header_write_closes_the_handle(self):
        import builtins

        real_open = builtins.open
        opened: list = []

        def _recording_open(*a, **k):
            fh = real_open(*a, **k)
            opened.append(fh)
            return fh

        self.warm(sys.executable)
        seen = self.spawned_argvs()
        with mock.patch.object(
            cr, "_write_spawn_header", side_effect=OSError("disk full")
        ), mock.patch.object(builtins, "open", _recording_open):
            result = cr.spawn_background_resync(
                self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
            )

        # The spawn still succeeds — logging never blocks it (v0.2.73 R-5).
        self.assertEqual(result.status, "launched", result.message)
        self.assertTrue(seen, "the children must still be spawned")
        logs = [f for f in opened if "resync-MyProj" in str(getattr(f, "name", ""))]
        self.assertEqual(len(logs), 1, "the per-spawn log file must have been opened")
        self.assertTrue(
            logs[0].closed,
            "the abandoned log handle must be CLOSED, not just dereferenced — "
            "dropping the reference leaks the fd until GC",
        )

    def test_the_degraded_spawn_uses_devnull(self):
        """The other half of the same degrade: with no usable handle the
        children must be DEVNULL'd, never handed a closed file."""
        import subprocess as _sp

        self.warm(sys.executable)
        kwargs_seen: list[dict] = []

        def _fake_popen(argv, **kwargs):
            kwargs_seen.append(kwargs)
            return _FakeProc()

        p = mock.patch.object(cr.subprocess, "Popen", _fake_popen)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(
            cr, "_write_spawn_header", side_effect=OSError("disk full")
        ):
            result = cr.spawn_background_resync(
                self.repo, "MyProj", python_exe=sys.executable, check_owed=False,
            )
        self.assertEqual(result.status, "launched", result.message)
        self.assertTrue(kwargs_seen)
        for kw in kwargs_seen:
            self.assertIs(kw["stdout"], _sp.DEVNULL)
            self.assertIs(kw["stderr"], _sp.DEVNULL)


class TheFailurePropagatesAndTheRetryIsNotBlocked(SpawnGuardBase):
    """`ensure_extractor_generation` is the caller the launcher actually drives."""

    def _plan_says_owed(self):
        return mock.patch.object(
            ceg, "plan",
            return_value=ceg.Verdict(True, ceg.REASON_CROSSES_BUMP),
        )

    def test_a_failed_spawn_is_reported_as_failed_not_skipped(self):
        with self._plan_says_owed():
            outcome = ceg.ensure_extractor_generation(
                self.repo,
                prev_version="0.2.91",
                running_version="0.2.94",
                project_name="MyProj",
                spawn=lambda *a, **k: cr.ResyncTriggerResult(
                    status="failed", message="broken interpreter /usr/bin/python3",
                ),
            )
        self.assertEqual(
            outcome.status, "failed",
            "a broken install must not be flattened into `skipped` — "
            "`project_init` logs skipped at phase 'ok' and never surfaces it",
        )
        self.assertIn("broken interpreter", outcome.message)

    def test_a_failed_attempt_writes_no_completion_stamp(self):
        with self._plan_says_owed():
            ceg.ensure_extractor_generation(
                self.repo,
                prev_version="0.2.91",
                running_version="0.2.94",
                project_name="MyProj",
                spawn=lambda *a, **k: cr.ResyncTriggerResult(status="failed", message="x"),
            )
        self.assertIsNone(
            ceg.read_stamp_generation(self.repo),
            "an un-run walk must never look like a completed one",
        )

    def test_the_next_update_retries_after_a_failed_attempt(self):
        """RECOVERY. The 8 field projects are in exactly this state: a walk was
        triggered, the child died, no stamp was written."""
        calls = {"n": 0}

        def _spawn(*a, **k):
            calls["n"] += 1
            return cr.ResyncTriggerResult(status="failed", message="broken")

        for _ in range(2):
            with self._plan_says_owed():
                outcome = ceg.ensure_extractor_generation(
                    self.repo,
                    prev_version="0.2.91",
                    running_version="0.2.94",
                    project_name="MyProj",
                    spawn=_spawn,
                )
            self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            calls["n"], 2,
            "a prior FAILED attempt must not block the retry — nothing in the "
            "failure path may look like completion",
        )

    def test_a_successful_retry_then_launches(self):
        with self._plan_says_owed():
            outcome = ceg.ensure_extractor_generation(
                self.repo,
                prev_version="0.2.91",
                running_version="0.2.94",
                project_name="MyProj",
                spawn=lambda *a, **k: cr.ResyncTriggerResult(status="launched", pid=99),
            )
        self.assertEqual(outcome.status, "launched")
        self.assertEqual(outcome.pid, 99)


class TheLadderReOwesTheWalkThatNeverRan(unittest.TestCase):
    """The OTHER half of recovery, and the subtler one.

    The failed 0.2.92 / 0.2.93 re-index attempts wrote no stamp — correct — but
    they DID advance each project's recorded manifest version to 0.2.93. Rule 3
    of `decide` ("prev_version at/past the newest bump ⇒ the graph was produced
    by an analyzer that already had the fixes") would then STAMP those projects
    as done without ever walking them: a fix that repairs the spawn but leaves
    the ladder alone would silently strand exactly the eight projects it was
    written for. Appending 0.2.94 to the ladder is what re-owes the work.
    """

    def test_the_ladder_covers_the_release_being_prepared(self):
        self.assertEqual(ceg.EXTRACTOR_GENERATION_BUMPS[-1], "0.2.94")
        self.assertEqual(ceg.CURRENT_EXTRACTOR_GENERATION, "0.2.94")

    def test_a_project_stranded_by_the_field_defect_is_owed_again(self):
        verdict = ceg.decide(
            prev_version="0.2.93",      # what the failed 0.2.93 update recorded
            running_version="0.2.94",
            stamp_generation=None,      # the doomed child never stamped
            graph_exists=True,
        )
        self.assertTrue(
            verdict.needs_reindex,
            "a project whose re-index was triggered but never ran must be owed "
            "it again, not stamped on the strength of its manifest version",
        )
        self.assertFalse(verdict.stamp_now)

    def test_a_project_that_completed_the_0293_walk_pays_one_more_pass(self):
        """The COST, asserted rather than wished away (review R6/3).

        Appending to the ladder moves its head, so a project holding a
        `0.2.93` stamp — one that really did complete the previous walk — is no
        longer `generation_is_current` and re-walks too. It is NOT left alone,
        and an earlier version of this comment claimed otherwise.

        (The old form of this test stamped `"0.2.94"`, a value nothing can hold
        before the release that declares it — so it asserted a state that cannot
        occur in the field and proved nothing.)

        What the pass actually costs: one forced re-extraction with the
        per-file skip gate bypassed; embeds stay content-hash gated, so
        unchanged entities are not re-embedded.
        """
        verdict = ceg.decide(
            prev_version="0.2.93",
            running_version="0.2.94",
            stamp_generation="0.2.93",
            graph_exists=True,
        )
        self.assertTrue(
            verdict.needs_reindex,
            "moving the ladder head re-owes the walk for every stamped project "
            "too — that is the accepted price of not stranding the eight",
        )
        self.assertFalse(verdict.stamp_now)

    def test_a_project_at_the_new_head_is_left_alone(self):
        """...and once a project HAS walked under the new head, it stops."""
        verdict = ceg.decide(
            prev_version="0.2.94",
            running_version="0.2.95",
            stamp_generation=ceg.CURRENT_EXTRACTOR_GENERATION,
            graph_exists=True,
        )
        self.assertFalse(verdict.needs_reindex)
        self.assertEqual(verdict.reason, ceg.REASON_STAMP_CURRENT)

    def test_a_project_with_no_graph_is_still_not_walked(self):
        verdict = ceg.decide(
            prev_version="0.2.93", running_version="0.2.94",
            stamp_generation=None, graph_exists=False,
        )
        self.assertFalse(verdict.needs_reindex)
        self.assertTrue(verdict.stamp_now)


if __name__ == "__main__":
    unittest.main()
