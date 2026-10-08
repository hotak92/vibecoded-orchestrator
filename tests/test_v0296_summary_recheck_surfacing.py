# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-7b — the doctor's reading of the summary-degradation state.

WP-7a review MINOR-3: the ``kg_summaries_degraded`` entry carried the
pending counts in its ``detected`` text, but ``vco doctor`` — the
authoritative end-of-update report — never read them, so it could say
"1 actionable entry" without saying WHAT was owed. These tests pin the
probe that surfaces the count (WP-7a's ``scan_pending``, the ONE home for
"what is pending") plus the one-line recovery naming the recheck and the
5 h cadence.

Also pins the lifecycle boundary: the condition belongs to
``vco_lib.summary_health`` (paired-resolution), so the doctor REPORTS it —
never re-emits, never resolves, and an OK reading does not clear a live
entry (only ``summary_recheck``'s post-scan settles it).

All synthetic: no LLM call, no Weaviate, no network, no real user state.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor, summary_health as sh  # noqa: E402


class _FakeEntry:
    def __init__(self, cid):
        self.condition_id = cid


class _FakeReport:
    def __init__(self, cids):
        self.entries = [_FakeEntry(c) for c in cids]


def _pending(kg_stale=(), kg_missing=(), code_stale=()):
    return sh.PendingSet(
        kg_stale=list(kg_stale), kg_missing=list(kg_missing),
        code_stale=list(code_stale),
    )


_UNSET = object()


def _resolvers(*, pending=_UNSET, cids=()):
    """A COMPLETE fake machine (v0.2.102 W4-C flake fix).

    ``summary_pending`` + ``deferral_report`` are the probe under test. Every
    OTHER machine-reading resolver is injected too: pre-fix they stayed at
    their defaults, so ``run_doctor`` ran the whole full-scope probe set
    against the real machine — and against whatever an earlier test in the
    session had left behind (a reachable class listing, a leaked env var, a
    patched transport). On a machine with a populated unclaimed Weaviate
    class, ``kg_binding_evidence`` flipped ``report.ok`` and these tests
    went red in roughly one of three full runs, green alone. The doctor
    engine's own contract (``test_v0291_doctor_phase``: "Hermeticity is the
    design constraint — a complete fake machine is three lambdas") is what
    this fixture now actually builds. ``pending=None`` (a scan that could
    not look) is a VALID injection, distinct from the default (unset)
    resolver, so the sentinel is explicit.
    """
    return doctor.DoctorResolvers(
        summary_pending=(
            None if pending is _UNSET else (lambda folder: pending)
        ),
        deferral_report=lambda folder: _FakeReport(cids),
        # The rest of the machine, described — never measured.
        npx_probe=lambda names: {
            "schema_version": 1, "npx_present": True, "npm_present": True,
            "npx_path": "/fake/npx", "npm_path": "/fake/npm",
            "commands": {n: f"/fake/{n}" for n in names},
        },
        mcp_entries=lambda: {},
        disk_usage=lambda p: SimpleNamespace(
            total=10 ** 12, used=10 ** 9, free=500 * 2 ** 30,
        ),
        # "Could not look" shapes — each produces NO findings, never ok.
        kg_binding_evidence=lambda: None,
        gateway_state=lambda: None,
        agent_id_problems=lambda folder: [],
        environ=lambda: {},
        rl_loss_summary=lambda since_ms: {"total": 0, "by_kind": {}},
    )


class SummaryPendingProbeTests(unittest.TestCase):
    def test_pending_rows_report_the_count_and_the_recovery(self):
        pending = _pending(
            kg_stale=["knowledge/a.md", "knowledge/b.md"],
            kg_missing=["knowledge/c.md"],
            code_stale=["x.py::f", "x.py::g", "y.rs::h"],
        )
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(pending=pending),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        # The COUNT is the substance: total + the three-way split.
        self.assertIn("6 summary row(s) degraded", f.summary)
        self.assertIn("2 KG on a fallback backend", f.summary)
        self.assertIn("1 KG with no summary", f.summary)
        self.assertIn("3 code on a fallback backend", f.summary)
        self.assertEqual(f.detail["pending_total"], 6)
        self.assertEqual(f.detail["kg_stale"], 2)
        self.assertEqual(f.detail["kg_missing"], 1)
        self.assertEqual(f.detail["code_stale"], 3)
        self.assertFalse(f.detail["condition_live"])
        # The one-line recovery: the recheck command + the 5 h cadence note.
        self.assertIn("5 h breaker cooldown", f.command)
        self.assertIn("python -m vco_lib.summary_health summary-recheck",
                      f.command)
        self.assertIn("--project-root", f.command)
        self.assertFalse(report.ok, "pending rows must fail the report")

    def test_a_live_condition_with_an_empty_pending_set_stays_a_problem(self):
        """The paired-resolution contract: only ``summary_recheck``'s
        post-scan clears the entry, so the doctor names the recheck rather
        than silently blessing a live entry over a clean scan."""
        report = doctor.run_doctor(
            Path("/tmp/x"),
            resolvers=_resolvers(pending=_pending(), cids=[sh.CONDITION_ID]),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn(sh.CONDITION_ID, f.summary)
        self.assertIn("entry is live", f.summary)
        self.assertTrue(f.detail["condition_live"])
        self.assertIn("summary-recheck", f.command)

    def test_a_live_condition_adds_itself_to_a_nonempty_pending_set(self):
        pending = _pending(kg_stale=["knowledge/a.md"])
        report = doctor.run_doctor(
            Path("/tmp/x"),
            resolvers=_resolvers(pending=pending, cids=[sh.CONDITION_ID]),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn("entry is live", f.summary)
        self.assertEqual(f.detail["pending_total"], 1)

    def test_a_clean_machine_is_ok(self):
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(pending=_pending()),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertEqual(f.detail["pending_total"], 0)
        self.assertTrue(report.ok)

    def test_an_unscannable_tree_is_unknown_never_ok(self):
        """Positive evidence only: sidecars that cannot be read are not
        "nothing pending" (the ``mcp_commands_spawnable`` precedent)."""
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(pending=None),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        # Unknowns do not fail the report.
        self.assertTrue(report.ok)

    def test_reports_the_registered_cid_without_re_emitting_it(self):
        """``kg_summaries_degraded`` has an owner (WP-7a) with a
        paired-resolution lifecycle. Re-emitting from the doctor would fork
        it (the ``launcher_binary_stale`` precedent)."""
        pending = _pending(kg_missing=["knowledge/a.md"])
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(pending=pending),
        )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.fix, doctor.FIX_DEFER)
        self.assertEqual(f.condition_id, sh.CONDITION_ID)
        self.assertNotIn(sh.CONDITION_ID, doctor.DOCTOR_OWNED_CIDS)
        self.assertEqual(doctor.deferral_entries_for(report), [])

    def test_an_ok_reading_never_resolves_the_live_entry(self):
        """The symmetric trap: adding the cid to the self-resolving set
        would make an OK scan clear an entry whose contract says only the
        recheck's post-scan settles it (with its audit row). A live entry
        never produces an OK reading in the first place (see the problem
        test above); this pins the OK shape on the clean machine."""
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(pending=_pending()),
        )
        ok_reading = next(
            f for f in report.findings
            if f.probe == "summary_pending" and f.status == doctor.STATUS_OK
        )
        # No routing on OK: there is nothing this reading may clear.
        self.assertEqual(ok_reading.condition_id, "")
        self.assertNotIn(sh.CONDITION_ID, doctor.DOCTOR_SELF_RESOLVING_CIDS)
        self.assertNotIn(sh.CONDITION_ID, doctor.healthy_condition_ids(report))

    def test_full_only_not_a_boot_probe(self):
        """Local file reads + one sha256 per knowledge node: cheap per node,
        but the whole tree is walked and the answer changes when summaries
        generate, not at boot (the ``install_completeness`` cost precedent).
        The probe id is also the doctor's routing surface, so boot cannot
        name a condition the Updates panel then cannot show."""
        self.assertIn("summary_pending", doctor.PROBES)
        _fn, scopes = doctor.PROBES["summary_pending"]
        self.assertEqual(scopes, (doctor.SCOPE_FULL,))


class SessionPollutionImmunityTests(unittest.TestCase):
    """The flake this file shipped with a fix for (coordinator item,
    2026-10-08): the three ``report.ok`` tests flipped depending on test
    order and live machine state — a populated UNCLAIMED Weaviate class on
    this machine reached ``probe_kg_binding_evidence`` and turned the report
    red in roughly 1 of 3 full runs, green alone. A test must never read the
    live Weaviate instance. These pin the immunity: with the complete fake
    machine, a session-polluted binding scan (exactly what a live class list
    or an earlier test's patch leaves behind) cannot flip the verdict."""

    def test_a_polluted_binding_scan_cannot_flip_the_verdict(self):
        """Simulated polluting earlier test: the class listing answers with
        one populated class no registered project claims. The DEFAULT
        resolver would report it (unclaimed-class PROBLEM) — the injected
        machine must never consult it."""
        from vco_lib import kg_binding_doctor as kbd
        from vco_lib.kg_binding_doctor import BindingEvidenceScan, UnclaimedClass

        polluted = BindingEvidenceScan(
            verdicts=(),
            unclaimed=(UnclaimedClass(name="Ghost_KnowledgeGraph", count=3),),
        )
        with mock.patch.object(kbd, "scan_kg_binding_evidence",
                               return_value=polluted):
            report = doctor.run_doctor(
                Path("/tmp/x"), resolvers=_resolvers(pending=_pending()),
            )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertEqual(
            [x.probe for x in report.problems], [],
            "a session-polluted machine state must not fail the report",
        )
        self.assertTrue(report.ok)

    def test_no_probe_touches_a_real_weaviate_transport(self):
        """The complete fake machine leaves nothing to the defaults: running
        the doctor with a poisoned transport seam (any HTTP attempt raises
        loudly) still completes — nothing reached out."""
        from vco_lib import weaviate_helpers

        def _poisoned(*a, **kw):
            raise AssertionError(
                "a hermetic doctor run must not touch a Weaviate transport"
            )

        with mock.patch.object(weaviate_helpers, "http_request", _poisoned), \
                mock.patch.object(weaviate_helpers, "post_graphql_safe",
                                  _poisoned):
            report = doctor.run_doctor(
                Path("/tmp/x"), resolvers=_resolvers(pending=_pending()),
            )
        f = next(f for f in report.findings if f.probe == "summary_pending")
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertTrue(report.ok)


class SummaryPendingRealScanTests(unittest.TestCase):
    """The DEFAULT resolver is the real scan — no second counting."""

    def test_the_default_resolver_reads_the_real_sidecars(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kdir = root / "knowledge"
            kdir.mkdir()
            # One hash-frozen fallback row (stale backend, current hash)…
            stale = kdir / "x.md"
            stale.write_text("# x\nbody", encoding="utf-8")
            # …and one node with no summary entry at all.
            (kdir / "y.md").write_text("# y\nbody", encoding="utf-8")
            (kdir / ".node_formats.json").write_text(
                json.dumps({
                    "knowledge/x.md": {
                        "title": "x", "description": "d", "summary": "s",
                        "generated_at": "2026-09-21T00:00:00Z",
                        "content_hash": sh._kg_content_hash(
                            stale.read_text(encoding="utf-8")),
                        "backend": "ollama",
                    },
                }),
                encoding="utf-8",
            )
            # Only the summary scan runs for real against the fixture tree;
            # the REST of the machine comes from the same complete fake as
            # `_resolvers` — pre-fix these tests had the same machine-state
            # dependence the probe tests did (see `_resolvers`).
            res = _resolvers(pending=_UNSET)
            report = doctor.run_doctor(root, resolvers=res)
            f = next(f for f in report.findings if f.probe == "summary_pending")
            self.assertEqual(f.status, doctor.STATUS_PROBLEM)
            self.assertEqual(f.detail["pending_total"], 2)
            self.assertEqual(f.detail["kg_stale"], 1)
            self.assertEqual(f.detail["kg_missing"], 1)
            self.assertEqual(f.detail["code_stale"], 0)

    def test_the_default_resolver_says_ok_on_a_summary_complete_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kdir = root / "knowledge"
            kdir.mkdir()
            node = kdir / "x.md"
            node.write_text("# x\nbody", encoding="utf-8")
            (kdir / ".node_formats.json").write_text(
                json.dumps({
                    "knowledge/x.md": {
                        "title": "x", "description": "d", "summary": "s",
                        "generated_at": "2026-09-21T00:00:00Z",
                        "content_hash": sh._kg_content_hash(
                            node.read_text(encoding="utf-8")),
                        "backend": sh.PREFERRED_TIER,
                    },
                }),
                encoding="utf-8",
            )
            res = _resolvers(pending=_UNSET)
            report = doctor.run_doctor(root, resolvers=res)
            f = next(f for f in report.findings if f.probe == "summary_pending")
            self.assertEqual(f.status, doctor.STATUS_OK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
