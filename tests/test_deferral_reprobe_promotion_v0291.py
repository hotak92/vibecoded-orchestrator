# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-B item 3 (decision #5) — RE-PROBE PROMOTION.

The systemic finding behind "deferral entries never auto-clean": the ONLY
generic auto-resolve machinery ran behind ``--update --apply-deferred``, and the
launcher's GUI update path spawns a plain ``install.py --update``. So in the
FIELD, every re-probe handler written since v0.2.54 was dead code for everyone
who updates through the GUI — which is everyone.

The pass is now unconditional on ``--update``. What keeps that safe is the
``side_effects`` gate: probes are read-only by construction, so they always run;
the ONE handler that acts on the system (``weaviate_unreachable_at_update`` →
``podman start``) still requires the flag. Both legs are tested, because the
gate protects an action taken on the user's machine without them asking.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


def _load_install():
    """Import install.py without running main() (it guards on __main__)."""
    spec = importlib.util.spec_from_file_location(
        "install_for_reprobe_tests", REPO_ROOT / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


install = _load_install()


def _entry(cid: str, **kw) -> DeferralEntry:
    base = dict(
        condition_id=cid, title=f"t {cid}", detected="d",
        why_deferred="w", command_to_apply="c", severity="warning",
    )
    base.update(kw)
    return DeferralEntry(**base)


def _persist(folder: Path, *entries: DeferralEntry) -> None:
    report = DeferralReport.read(folder)
    for e in entries:
        report.add_entry(e)
    report.write(folder)


class CallSiteShapeTests(unittest.TestCase):
    """The promotion itself: the pass is no longer flag-gated."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_pass_runs_on_every_update(self):
        """The pass runs on every ``--update``, with no flag in front of it.

        v0.2.91 WP-D moved the call site out of ``main()`` and into
        ``_post_install_probe_phase`` (main() sits at a hard line ratchet, and
        the phase now also runs the doctor). The INVARIANT is unchanged and is
        pinned here at its new home: main() calls the phase unconditionally,
        and inside the phase the only gate on the re-probe is ``args.update``.
        """
        self.assertIn(
            "_post_install_probe_phase(_deferral_report, _deferral_folder, args=args)",
            self.source,
        )
        phase = self.source[self.source.index("def _post_install_probe_phase"):]
        phase = phase[: phase.index("def _run_doctor_phase")]
        self.assertIn(
            'if getattr(args, "update", False):\n'
            "        _apply_deferred_entries(report, folder, args=args)",
            phase,
        )
        # No `--apply-deferred` flag check anywhere in the phase body: the
        # flag survives only INSIDE `_apply_deferred_entries`, as the
        # side-effect selector.
        body = phase.split('"""', 2)[-1]
        self.assertNotIn('getattr(args, "apply_deferred"', body)

    def test_flag_no_longer_gates_the_pass(self):
        """The old shape `if args.update and getattr(args, "apply_deferred", ...)`
        must be GONE — its survival anywhere would restore the field gap."""
        self.assertNotIn(
            'if args.update and getattr(args, "apply_deferred", False):',
            self.source,
        )

    def test_flag_now_selects_side_effects_only(self):
        """The flag survives ONLY as the side-effect selector. It is resolved
        inside `_apply_deferred_entries` (one home for reading it) rather than
        at the call site, so `side_effects=None` means "ask args"."""
        self.assertIn(
            'side_effects = bool(getattr(args, "apply_deferred", False))',
            self.source,
        )
        self.assertIn('side_effects: "bool | None" = None,', self.source)

    def test_only_one_handler_is_side_effect_gated(self):
        """`side_effects` must gate exactly one branch UNCONDITIONALLY. If a
        second handler starts acting on the system on the bare flag, that is a
        decision to take explicitly, not a side effect of adding an `if`.

        The orphan-collection DROP is the one COMPOUND gate (wave-2 MINOR-1):
        it needs `side_effects` AND its own `--apply-orphan-deletes` consent,
        so it is counted separately below rather than folded in here."""
        # Only gates INSIDE the per-entry loop count — the one at 4-space
        # indentation merely picks the pass's header line.
        gated = [
            ln for ln in self.source.splitlines()
            if re.match(r"^ {8,}if side_effects:\s*$", ln)
        ]
        self.assertEqual(len(gated), 1, gated)
        header = [
            ln for ln in self.source.splitlines()
            if re.match(r"^ {4}if side_effects:\s*$", ln)
        ]
        self.assertEqual(len(header), 1, header)

    def test_orphan_delete_requires_both_the_pass_flag_and_its_own_consent(self):
        """wave-2 MINOR-1 (RED-PROOF): the Weaviate DROP must be reachable ONLY
        under `--apply-deferred --apply-orphan-deletes`.

        The re-probe promotion made the pass run on EVERY `--update`. The
        orphan handler's gate read `args.apply_orphan_deletes` alone, so a user
        who had once passed the orphan flag (or any caller constructing args
        with it set) would get an irreversible Weaviate DROP on a plain,
        unattended `--update` — while the argparse help still promises
        "During --update --apply-deferred". Side-effectful remediation stays
        consent-gated: both flags, or nothing."""
        compound = [
            ln for ln in self.source.splitlines()
            if re.match(
                r"^ {8,}if side_effects and getattr\(\s*args,\s*"
                r'"apply_orphan_deletes",\s*False\s*\):\s*$',
                ln,
            )
        ]
        self.assertEqual(
            len(compound), 1,
            "the orphan DROP must be gated on `side_effects and "
            "args.apply_orphan_deletes`; found: %r" % (compound,),
        )
        self.assertNotIn(
            '            if getattr(args, "apply_orphan_deletes", False):',
            self.source,
            "the flag-only gate would let a plain --update drop a collection",
        )


class ReadOnlyPassBehaviourTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *, side_effects: bool) -> DeferralReport:
        current = DeferralReport()
        install._apply_deferred_entries(
            current, self.folder, args=None, side_effects=side_effects,
        )
        return current

    def _cids(self, report: DeferralReport):
        return {e.condition_id for e in report.entries}

    def test_record_entry_resolves_without_the_flag(self):
        """RED-PROOF for the promotion: `generated_files_reconciled` is a
        one-shot audit record whose handler has existed since v0.2.89 and never
        ran for a GUI updater. Under the promoted pass it clears on a plain
        `--update`."""
        _persist(self.folder, _entry("generated_files_reconciled", severity="info"))
        result = self._run(side_effects=False)
        self.assertNotIn("generated_files_reconciled", self._cids(result))

    def test_registry_probe_resolves_without_the_flag(self):
        """The sidecar probe clears an `orchestrator_user_modified_preserved`
        whose parked upstream copies are gone — read-only, so no flag needed."""
        entry = _entry(
            "orchestrator_user_modified_preserved",
            severity="info",
            detected=(
                "…upstream saved as `docs/A.md.from-upstream-5a9ae53` "
                "(base=aaa theirs=bbb)"
            ),
        )
        _persist(self.folder, entry)
        result = self._run(side_effects=False)
        self.assertNotIn(
            "orchestrator_user_modified_preserved", self._cids(result)
        )

    def test_registry_probe_keeps_entry_when_the_sidecar_survives(self):
        """LEAVE-ALONE leg of the same branch."""
        sidecar = self.folder / "docs" / "A.md.from-upstream-5a9ae53"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("upstream", encoding="utf-8")
        entry = _entry(
            "orchestrator_user_modified_preserved",
            severity="info",
            detected="…upstream saved as `docs/A.md.from-upstream-5a9ae53` (x)",
        )
        _persist(self.folder, entry)
        result = self._run(side_effects=False)
        self.assertIn("orchestrator_user_modified_preserved", self._cids(result))

    def test_unknown_condition_is_preserved(self):
        _persist(self.folder, _entry("some_future_condition"))
        result = self._run(side_effects=False)
        self.assertIn("some_future_condition", self._cids(result))

    def test_probe_driven_clear_leaves_an_auto_resolution_trail(self):
        """B-F9: no silent mutations. A probe-driven clear must be auditable."""
        entry = _entry(
            "orchestrator_user_modified_preserved",
            severity="info",
            detected="…upstream saved as `docs/A.md.from-upstream-5a9ae53` (x)",
        )
        _persist(self.folder, entry)
        self._run(side_effects=False)
        trail = self.folder / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(trail.is_file())
        self.assertIn(
            "orchestrator_user_modified_preserved",
            trail.read_text(encoding="utf-8"),
        )


class SideEffectGateTests(unittest.TestCase):
    """The one branch that acts on the user's machine — both legs."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._calls = []
        self._orig_run = install.subprocess.run

        def _spy(cmd, *a, **kw):
            self._calls.append(cmd)
            return self._orig_run(
                [sys.executable, "-c", "pass"], capture_output=True,
            )

        install.subprocess.run = _spy
        # Force the reachability probe to fail so the branch takes its
        # "keep the entry" tail deterministically, offline.
        self._orig_urlopen = install.urllib.request.urlopen

        def _boom(*a, **kw):
            raise OSError("weaviate unreachable in test")

        install.urllib.request.urlopen = _boom

    def tearDown(self):
        install.subprocess.run = self._orig_run
        install.urllib.request.urlopen = self._orig_urlopen
        self._tmp.cleanup()

    def _run(self, *, side_effects: bool) -> DeferralReport:
        current = DeferralReport()
        install._apply_deferred_entries(
            current, self.folder, args=None, side_effects=side_effects,
        )
        return current

    def test_read_only_pass_never_starts_a_container(self):
        """LEAVE-ALONE leg: a plain `--update` probes but never runs
        `podman start`. Starting a container nobody asked for is exactly the
        unrequested action the no-auto-heal ruling forbids."""
        _persist(self.folder, _entry("weaviate_unreachable_at_update"))
        result = self._run(side_effects=False)
        starts = [c for c in self._calls if isinstance(c, list) and "start" in c]
        self.assertEqual(starts, [], f"unexpected side effect: {self._calls}")
        self.assertIn(
            "weaviate_unreachable_at_update",
            {e.condition_id for e in result.entries},
        )

    def test_apply_deferred_still_attempts_the_container_start(self):
        """ACT leg: the flag keeps its original meaning — you may also act."""
        _persist(self.folder, _entry("weaviate_unreachable_at_update"))
        self._run(side_effects=True)
        starts = [c for c in self._calls if isinstance(c, list) and "start" in c]
        self.assertTrue(starts, f"expected a container start; got {self._calls}")


class _Args:
    """Minimal argparse.Namespace stand-in for the handler's flag reads."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class OrphanDeleteConsentTests(unittest.TestCase):
    """wave-2 MINOR-1 — the Weaviate DROP is the second destructive branch.

    A DROP cannot be undone, so it gets the destructive-branch treatment: a
    test for the ACT leg AND one for the leave-alone leg. The leave-alone leg
    is the one the re-probe promotion put at risk — the pass now runs
    unattended on every `--update`, so a gate that reads only the orphan flag
    would delete a collection on a run the user never consented to.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._requests = []
        self._orig_urlopen = install.urllib.request.urlopen

        class _Resp:
            def __init__(self, payload: bytes):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _spy(req, *a, **kw):
            # `req` is either a str URL (the schema GET) or a Request.
            url = req if isinstance(req, str) else req.full_url
            method = "GET" if isinstance(req, str) else req.get_method()
            self._requests.append((method, url))
            if url.endswith("/graphql"):
                return _Resp(
                    b'{"data":{"Aggregate":'
                    b'{"VibeCodedOrchestrator_Development":[{"meta":'
                    b'{"count":0}}]}}}'
                )
            return _Resp(b"{}")

        install.urllib.request.urlopen = _spy

    def tearDown(self):
        install.urllib.request.urlopen = self._orig_urlopen
        self._tmp.cleanup()

    def _run(self, *, side_effects: bool, orphan_flag: bool) -> DeferralReport:
        current = DeferralReport()
        install._apply_deferred_entries(
            current,
            self.folder,
            args=_Args(apply_orphan_deletes=orphan_flag),
            side_effects=side_effects,
        )
        return current

    def _deletes(self):
        return [r for r in self._requests if r[0] == "DELETE"]

    def test_plain_update_never_drops_even_with_the_orphan_flag(self):
        """LEAVE-ALONE leg (RED-PROOF): `--update` WITHOUT `--apply-deferred`,
        but with `apply_orphan_deletes` set, must issue NO DELETE and must keep
        the entry. Pre-fix the gate read the orphan flag alone, so the promoted
        (always-on) pass would have dropped the collection unasked."""
        _persist(self.folder, _entry("orphan_orchestrator_development_collection"))
        result = self._run(side_effects=False, orphan_flag=True)
        self.assertEqual(
            self._deletes(), [],
            f"a plain --update issued a Weaviate DROP: {self._requests}",
        )
        self.assertIn(
            "orphan_orchestrator_development_collection",
            {e.condition_id for e in result.entries},
        )

    def test_apply_deferred_plus_orphan_flag_still_drops(self):
        """ACT leg: both consents present ⇒ the documented behaviour is intact
        (the entry is dropped and resolved)."""
        _persist(self.folder, _entry("orphan_orchestrator_development_collection"))
        result = self._run(side_effects=True, orphan_flag=True)
        self.assertTrue(
            self._deletes(), f"expected a DROP; got {self._requests}",
        )
        self.assertNotIn(
            "orphan_orchestrator_development_collection",
            {e.condition_id for e in result.entries},
        )

    def test_apply_deferred_without_the_orphan_flag_does_not_drop(self):
        """The pre-existing consent gate is unchanged by the fix."""
        _persist(self.folder, _entry("orphan_orchestrator_development_collection"))
        result = self._run(side_effects=True, orphan_flag=False)
        self.assertEqual(self._deletes(), [], f"{self._requests}")
        self.assertIn(
            "orphan_orchestrator_development_collection",
            {e.condition_id for e in result.entries},
        )


class RecordAutoExpiryTests(unittest.TestCase):
    """Record-class cids expire one-shot through registry OWNERSHIP.

    install.py never emits them, so the single end-of-run write simply does not
    carry them forward — the `stale_unit_retired_` precedent, now extended to
    the launcher's boot-time records.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _finalize(self) -> set:
        from vco_lib.install_deferral_flow import InstallDeferralFlow

        flow = InstallDeferralFlow(
            folder=self.folder,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()
        flow.finalize()
        return {e.condition_id for e in DeferralReport.read(self.folder).entries}

    def test_boot_time_records_expire_on_the_next_update(self):
        _persist(
            self.folder,
            _entry("kg_access_phantom_repaired", severity="info"),
            _entry("codegraph_binding_repaired", severity="info"),
            _entry("launcher_binary_clobber_averted", severity="info"),
        )
        self.assertEqual(self._finalize(), set())

    def test_owed_work_entries_are_never_dropped(self):
        """LEAVE-ALONE leg: a FOREIGN owed-work entry must survive the same
        finalize. Owning it would be the A-2 data-loss bug."""
        _persist(
            self.folder,
            _entry("codegraph_embed_resync_pending"),
            _entry("bundle_user_modified_preserved"),
            _entry("kg_sync_no_embedding_backend"),
        )
        self.assertEqual(
            self._finalize(),
            {
                "codegraph_embed_resync_pending",
                "bundle_user_modified_preserved",
                "kg_sync_no_embedding_backend",
            },
        )

    def test_record_emit_also_writes_the_auto_resolution_trail(self):
        """Registry-declared `auto_resolutions_jsonl` must actually fire, or
        expiring the ledger row would erase the only trace of the event."""
        from vco_lib.deferral_emit import emit

        emit(self.folder, _entry("kg_access_phantom_repaired", severity="info"))
        trail = self.folder / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(trail.is_file())
        self.assertIn("kg_access_phantom_repaired", trail.read_text(encoding="utf-8"))

    def test_recurring_nudges_do_not_spam_the_trail(self):
        """`template_review_pending` re-emits on every bundle update; mirroring
        it would turn the trail into a log of nothing happening."""
        from vco_lib.deferral_emit import emit

        emit(self.folder, _entry("template_review_pending", severity="info"))
        trail = self.folder / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertFalse(trail.exists())


if __name__ == "__main__":
    unittest.main()
