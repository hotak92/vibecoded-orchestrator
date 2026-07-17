# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 (WP-1 / D4 / P1) — code-graph stale-identity SWEEP.

Covers:
  * ``codegraph_vector_copy.sweep_stale_identities`` + ``_distinct_stale_identities``
    (Python engine) — act (stale identities migrated), leave-alone (all
    canonical ⇒ zero migration; unreadable class ⇒ soft no-op; foreign
    prefixes untouched), and the ``--sweep`` CLI (mutually exclusive with
    ``--from``).
  * ``codegraph_resync.identity_sweep_if_stale`` — the probe-first shim +
    ``record_auto_resolution`` audit row on a real migration.
    FAIL-WITHOUT-FIX PIN (P1 root-migration-runs): a stale identity present ⇒
    ``migrate_project_identity`` invoked with (from=stale, to=canonical).
  * install.py ``_trigger_codegraph_maintenance`` reaches the sweep even when
    the R-6 embed-resync probe says NOT owed (structural/flow PIN).

Non-root (A3): the ``non_root`` fixtures run the sweep on a NON-ROOT prefix
(``ClientAlpha`` — a project folder distinct from the orchestrator root).

Every gate constructs its own in-memory fixtures — nothing depends on the
incident machine's post-local-fix state.
"""

from __future__ import annotations

import types

import pytest

from vco_lib import codegraph_vector_copy as vc
from vco_lib import codegraph_resync as cr


_BASES = ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction")


# ── Minimal weaviate-client fakes ─────────────────────────────────────────────
class _SweepColl:
    """A code-graph collection fake supporting the two reads the sweep uses:
    ``iterator(return_properties=["project"])`` (identity enumeration) and
    ``aggregate.over_all(filters=..., total_count=True)`` (the cheap probe)."""

    def __init__(self, name, project_values, *, agg_raises=False,
                 iter_raises=False):
        self.name = name
        # project_values: list of the ``project`` property per stored row.
        self._project_values = list(project_values)
        self._iter_raises = iter_raises
        self._canonical_for_agg = None  # set by the probe filter capture

        def _over_all(filters=None, total_count=False, **_kw):
            if agg_raises:
                raise RuntimeError("aggregate unsupported (injected)")
            # The probe filters project != canonical; the fake reads the
            # canonical from the captured filter value.
            canonical = getattr(filters, "value", None)
            n = sum(1 for v in self._project_values if v != canonical)
            return types.SimpleNamespace(total_count=n)

        self.aggregate = types.SimpleNamespace(over_all=_over_all)

    def iterator(self, return_properties=None, **_kw):
        if self._iter_raises:
            raise RuntimeError("iterate failed (injected)")
        for v in self._project_values:
            yield types.SimpleNamespace(properties={"project": v})


class _SweepClient:
    def __init__(self, colls):
        self._colls = colls
        self.collections = types.SimpleNamespace(
            exists=lambda name: name in self._colls,
            get=lambda name: self._colls[name],
        )
        self.closed = False

    def close(self):
        self.closed = True


class _FakeFilterBuilder:
    def __init__(self, prop):
        self.prop = prop

    def not_equal(self, value):
        return types.SimpleNamespace(prop=self.prop, op="not_equal", value=value)


class _FakeFilterFactory:
    @staticmethod
    def by_property(prop):
        return _FakeFilterBuilder(prop)


@pytest.fixture(autouse=True)
def _patch_weaviate_filter(monkeypatch):
    """Make ``from weaviate.classes.query import Filter`` resolve in the probe
    without a live weaviate."""
    fake_query = types.ModuleType("weaviate.classes.query")
    fake_query.Filter = _FakeFilterFactory
    import sys
    monkeypatch.setitem(sys.modules, "weaviate.classes.query", fake_query)
    yield


def _client_with_identities(prefix, per_base_projects):
    """Build a 5-collection fake where each ``<prefix>_<base>`` carries the
    given list of ``project`` values."""
    colls = {
        f"{prefix}_{base}": _SweepColl(f"{prefix}_{base}", per_base_projects.get(base, []))
        for base in _BASES
    }
    return _SweepClient(colls)


# ── _distinct_stale_identities ────────────────────────────────────────────────
def test_distinct_stale_identities_discovers_non_canonical_sorted():
    # Root-shaped prefix with a spaced legacy identity mixed with canonical.
    client = _client_with_identities("VibeCodedOrchestrator", {
        "CodeModule": ["VibeCodedOrchestrator", "VibeCoded Orchestrator"],
        "CodeFunction": ["VibeCoded Orchestrator", "Older Name"],
    })
    stale = vc._distinct_stale_identities(
        client, "VibeCodedOrchestrator", "VibeCodedOrchestrator",
    )
    # sorted, deduped, canonical excluded
    assert stale == ["Older Name", "VibeCoded Orchestrator"]


def test_distinct_stale_identities_all_canonical_is_empty():
    client = _client_with_identities("VibeCodedOrchestrator", {
        "CodeModule": ["VibeCodedOrchestrator"],
        "CodeFunction": ["VibeCodedOrchestrator"],
    })
    assert vc._distinct_stale_identities(
        client, "VibeCodedOrchestrator", "VibeCodedOrchestrator",
    ) == []


def test_distinct_stale_identities_unreadable_class_soft_fails():
    # One class raises on iterate → contributes nothing, others still scanned.
    good = _SweepColl("P_CodeModule", ["Stale One"])
    bad = _SweepColl("P_CodeFunction", ["ignored"], iter_raises=True)
    colls = {f"P_{b}": _SweepColl(f"P_{b}", []) for b in _BASES}
    colls["P_CodeModule"] = good
    colls["P_CodeFunction"] = bad
    client = _SweepClient(colls)
    assert vc._distinct_stale_identities(client, "P", "Canonical") == ["Stale One"]


# ── sweep_stale_identities (engine) ───────────────────────────────────────────
def test_sweep_act_migrates_each_stale_identity(monkeypatch):
    """ACT: two stale identities ⇒ migrate_project_identity called once per
    identity with (from=stale, to=canonical)."""
    client = _client_with_identities("ClientAlpha", {  # NON-ROOT prefix (A3)
        "CodeModule": ["ClientAlpha", "Alpha Client", "alpha-old"],
    })
    calls = []

    def _fake_migrate(cl, prefix, old, new, *, dry_run=False, uuid_builder=None):
        calls.append((prefix, old, new, dry_run))
        s = vc.MigrationSummary()
        s.moved = 3
        return s

    monkeypatch.setattr(vc, "migrate_project_identity", _fake_migrate)
    summaries = vc.sweep_stale_identities("ClientAlpha", "ClientAlpha", client=client)
    # Two distinct stale identities, sorted.
    assert [c[1] for c in calls] == ["Alpha Client", "alpha-old"]
    assert all(c[0] == "ClientAlpha" and c[2] == "ClientAlpha" for c in calls)
    assert all(c[3] is False for c in calls)  # real run, not dry
    assert len(summaries) == 2
    assert sum(s.moved for s in summaries) == 6


def test_sweep_leave_alone_all_canonical_zero_migrations(monkeypatch):
    """LEAVE-ALONE: all rows canonical ⇒ zero migration calls."""
    client = _client_with_identities("ClientAlpha", {
        "CodeModule": ["ClientAlpha", "ClientAlpha"],
    })
    calls = []
    monkeypatch.setattr(
        vc, "migrate_project_identity",
        lambda *a, **k: calls.append(a) or vc.MigrationSummary(),
    )
    summaries = vc.sweep_stale_identities("ClientAlpha", "ClientAlpha", client=client)
    assert summaries == []
    assert calls == []


def test_sweep_dry_run_threads_flag(monkeypatch):
    client = _client_with_identities("P", {"CodeModule": ["P", "Old"]})
    seen = {}

    def _fake_migrate(cl, prefix, old, new, *, dry_run=False, uuid_builder=None):
        seen["dry_run"] = dry_run
        return vc.MigrationSummary()

    monkeypatch.setattr(vc, "migrate_project_identity", _fake_migrate)
    vc.sweep_stale_identities("P", "P", client=client, dry_run=True)
    assert seen["dry_run"] is True


def test_sweep_one_identity_failure_does_not_abort(monkeypatch):
    """A per-identity migration exception is recorded as a failure-signal
    summary; the sweep continues to the next identity."""
    client = _client_with_identities("P", {"CodeModule": ["P", "A Name", "B Name"]})

    def _fake_migrate(cl, prefix, old, new, *, dry_run=False, uuid_builder=None):
        if old == "A Name":
            raise RuntimeError("boom on A")
        s = vc.MigrationSummary()
        s.moved = 1
        return s

    monkeypatch.setattr(vc, "migrate_project_identity", _fake_migrate)
    summaries = vc.sweep_stale_identities("P", "P", client=client)
    assert len(summaries) == 2
    # One failure-signal summary + one real summary.
    assert sum(s.failures for s in summaries) == 1
    assert sum(s.moved for s in summaries) == 1


def test_sweep_missing_prefix_is_noop():
    assert vc.sweep_stale_identities("", "Canon") == []
    assert vc.sweep_stale_identities("P", "") == []


# ── CLI --sweep ───────────────────────────────────────────────────────────────
def test_cli_sweep_mutually_exclusive_with_from():
    with pytest.raises(SystemExit):
        vc._cli(["--migrate-identity", "--prefix", "P", "--to", "P",
                 "--from", "Old", "--sweep"])


def test_cli_sweep_requires_prefix_and_to():
    with pytest.raises(SystemExit):
        vc._cli(["--migrate-identity", "--sweep", "--prefix", "P"])  # no --to


def test_cli_sweep_runs_engine_and_prints_aggregate(monkeypatch, capsys):
    client = _client_with_identities("P", {"CodeModule": ["P", "Old"]})
    monkeypatch.setattr(vc, "_build_client", lambda *a, **k: client)

    def _fake_sweep(prefix, canonical, *, client=None, dry_run=False):
        s = vc.MigrationSummary()
        s.moved = 4
        s.deduped = 1
        return [s]

    monkeypatch.setattr(vc, "sweep_stale_identities", _fake_sweep)
    rc = vc._cli(["--migrate-identity", "--sweep", "--prefix", "P", "--to", "P"])
    assert rc == 0
    out = capsys.readouterr().out
    # Final line is the aggregate the launcher parser keys on.
    lines = [ln for ln in out.splitlines() if ln.startswith("IDENTITY_MIGRATION")]
    assert lines[-1] == "IDENTITY_MIGRATION moved=4 deduped=1 left=0 failures=0"


# ── identity_sweep_if_stale (resync shim) ─────────────────────────────────────
def _install_client_stub(monkeypatch, client):
    monkeypatch.setattr(cr, "_build_client", lambda *a, **k: client)


def test_identity_sweep_if_stale_PIN_migrates_from_stale_to_canonical(
    monkeypatch, tmp_path,
):
    """FAIL-WITHOUT-FIX PIN (P1 root-migration-runs): a stale identity present
    ⇒ migrate_project_identity invoked with (from=stale, to=canonical) AND an
    auto-resolution audit row recorded."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "VibeCodedOrchestrator")
    client = _client_with_identities("VibeCodedOrchestrator", {
        "CodeModule": ["VibeCodedOrchestrator", "VibeCoded Orchestrator"],
    })
    _install_client_stub(monkeypatch, client)

    migrate_calls = []

    def _fake_migrate(cl, prefix, old, new, *, dry_run=False, uuid_builder=None):
        migrate_calls.append((old, new))
        s = vc.MigrationSummary()
        s.moved = 2
        s.deduped = 2834
        return s

    monkeypatch.setattr(vc, "migrate_project_identity", _fake_migrate)

    audit = []
    import vco_lib.deferral_emit as de
    monkeypatch.setattr(
        de, "record_auto_resolution",
        lambda folder, cid, action, detail, **k: audit.append((cid, action, detail)),
    )

    moved = cr.identity_sweep_if_stale(tmp_path, "VibeCoded Orchestrator")

    # The stale display name was migrated onto the canonical prefix.
    assert ("VibeCoded Orchestrator", "VibeCodedOrchestrator") in migrate_calls
    assert moved == 2 + 2834
    # Audit trail recorded (B-F9).
    assert audit and audit[0][0] == "codegraph_identity_migrated"


def test_identity_sweep_if_stale_leave_alone_all_canonical(monkeypatch, tmp_path):
    """LEAVE-ALONE: probe reports 0 stale ⇒ probe-only, no migration, no audit."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "ClientAlpha")
    client = _client_with_identities("ClientAlpha", {  # NON-ROOT (A3)
        "CodeModule": ["ClientAlpha", "ClientAlpha"],
    })
    _install_client_stub(monkeypatch, client)

    calls = []
    monkeypatch.setattr(
        vc, "migrate_project_identity",
        lambda *a, **k: calls.append(a) or vc.MigrationSummary(),
    )
    audit = []
    import vco_lib.deferral_emit as de
    monkeypatch.setattr(
        de, "record_auto_resolution",
        lambda *a, **k: audit.append(a),
    )
    moved = cr.identity_sweep_if_stale(tmp_path, "ClientAlpha")
    assert moved == 0
    assert calls == []
    assert audit == []


def test_identity_sweep_if_stale_prefix_unresolvable_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: None)
    # _build_client must never even be called.
    monkeypatch.setattr(
        cr, "_build_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not connect")),
    )
    assert cr.identity_sweep_if_stale(tmp_path, "Whatever") == 0


def test_identity_sweep_if_stale_weaviate_down_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    monkeypatch.setattr(cr, "_build_client", lambda *a, **k: None)
    assert cr.identity_sweep_if_stale(tmp_path, "P") == 0


def test_identity_sweep_probe_aggregate_error_falls_through(monkeypatch, tmp_path):
    """Aggregate raises on every class ⇒ probe undeterminable (None) ⇒ the
    engine's own enumerate scan is authoritative (conservative: proceed)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    colls = {
        f"P_{b}": _SweepColl(f"P_{b}", ["Stale"], agg_raises=True) for b in _BASES
    }
    client = _SweepClient(colls)
    _install_client_stub(monkeypatch, client)

    calls = []

    def _fake_migrate(cl, prefix, old, new, *, dry_run=False, uuid_builder=None):
        calls.append(old)
        s = vc.MigrationSummary()
        s.moved = 1
        return s

    monkeypatch.setattr(vc, "migrate_project_identity", _fake_migrate)
    monkeypatch.setattr(
        __import__("vco_lib.deferral_emit", fromlist=["record_auto_resolution"]),
        "record_auto_resolution", lambda *a, **k: None,
    )
    moved = cr.identity_sweep_if_stale(tmp_path, "P")
    # Undeterminable probe fell through to the engine, which migrated the stale
    # identity. ONE distinct stale identity ("Stale") ⇒ migrate called once.
    assert calls == ["Stale"]
    assert moved == 1


def test_probe_stale_identity_count_positive_and_zero():
    client = _client_with_identities("P", {
        "CodeModule": ["P", "Old"],
        "CodeFunction": ["P"],
    })
    # 1 stale in CodeModule, 0 elsewhere → total 1.
    assert cr._probe_stale_identity_count(client, "P", "P") == 1

    clean = _client_with_identities("P", {"CodeModule": ["P"]})
    assert cr._probe_stale_identity_count(clean, "P", "P") == 0


def test_probe_undeterminable_when_all_aggregates_raise():
    colls = {f"P_{b}": _SweepColl(f"P_{b}", ["x"], agg_raises=True) for b in _BASES}
    client = _SweepClient(colls)
    assert cr._probe_stale_identity_count(client, "P", "P") is None


# ── install.py --update FLOW pin: the sweep runs even when resync is NOT owed ──
import sys as _sys  # noqa: E402
import unittest as _unittest  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from unittest import mock as _mock  # noqa: E402

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

import install  # type: ignore  # noqa: E402


class TestInstallFlowReachesSweep(_unittest.TestCase):
    """FAIL-WITHOUT-FIX PIN (P1 root-migration-runs, flow half): the install.py
    --update maintenance path invokes the identity sweep UNCONDITIONALLY — even
    when the R-6 embed-resync probe reports NOT owed. The sweep is deliberately
    NOT gated by the owed-probe (identity-stale rows can be embed-revision
    current)."""

    def test_maintenance_runs_sweep_before_resync_even_when_not_owed(self):
        order = []

        class _NotOwed:
            status = "not_owed"
            message = "all rows at current embed revision"
            pid = None
            deferral = None

        with _mock.patch.object(
            install, "_derive_orchestrator_project_name", return_value="Proj"
        ), _mock.patch(
            "vco_lib.codegraph_resync.identity_sweep_if_stale",
        ) as sweep_mock, _mock.patch(
            "vco_lib.codegraph_resync.spawn_background_resync",
            return_value=_NotOwed(),
        ):
            sweep_mock.side_effect = lambda *a, **k: order.append("sweep") or 0
            # Wrap mark_resolved so the resync half runs its not_owed branch.
            report = _mock.MagicMock()
            report.mark_resolved.side_effect = lambda *a, **k: order.append("resync_not_owed")
            install._trigger_codegraph_maintenance(report)

        # The sweep ran, and it ran BEFORE the resync half — even though the
        # resync probe reported not_owed (proving no owed-gate on the sweep).
        self.assertEqual(order[0], "sweep",
                         "identity sweep must run first, unconditionally")
        self.assertIn("resync_not_owed", order,
                      "the resync half still ran its not_owed branch after the sweep")
        sweep_mock.assert_called_once()
        # Called with the root PROJECT_ROOT + resolved project name.
        args, _kw = sweep_mock.call_args
        self.assertEqual(args[0], install.PROJECT_ROOT)
        self.assertEqual(args[1], "Proj")

    def test_sweep_shim_soft_fails_on_helper_error(self):
        """The shim never crashes the update: a helper exception is swallowed."""
        with _mock.patch.object(
            install, "_derive_orchestrator_project_name", return_value="Proj"
        ), _mock.patch(
            "vco_lib.codegraph_resync.identity_sweep_if_stale",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            install._trigger_codegraph_identity_sweep(_mock.MagicMock())
