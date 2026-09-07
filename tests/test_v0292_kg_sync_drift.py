# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.kg_sync_drift — the v0.2.92 silent-KG-sync-drop detector.

Covers the decision matrix requested for this work package: both the "act"
(drift reported) and "leave-alone" (no drift) case for every branch that
gates the finding, plus the reachability failure mode that must never
degrade into a false "everything is missing" report.

Also covers the 2026-09-01 addendum from a field report (a module that
creates VCO projects programmatically): a project that is REGISTERED but
never had its bundle installed has no `.claude/` tree at all, so a Claude
Code write to `knowledge/*.md` succeeds, the write envelope reports
"complete", and NO component reports an error — the symptom is "it says it
wrote 40 nodes, search returns nothing", every individual signal green. See
`test_all_individual_signals_healthy_but_aggregate_is_unsynced` below for
the test that encodes exactly that shape, and the `check_kg_binding*` tests
for the distinct "no binding at all" state this requires (separate from
node-level drift, which needs a binding to exist in the first place).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import kg_sync_drift as drift
from vco_lib.knowledge_residue import content_signature_excluding_updated

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _hash_of(content: str) -> str:
    return content_signature_excluding_updated(content)


# ---------------------------------------------------------------------------
# Fake collections — a dict of {rel_posix_with_knowledge_prefix: content_hash}
# stands in for the real Weaviate GraphQL round-trip.
# ---------------------------------------------------------------------------

def _fake_query_hashes(collections: dict):
    """Build a query_hashes_fn that serves *collections* keyed by collection name.

    Signature matches vco_lib.kg_sync.batch_query_content_hashes:
    (weaviate_url, collection_name, on_warn=None) -> dict.
    """

    def _fn(weaviate_url, collection_name, on_warn=None):
        return dict(collections.get(collection_name, {}))

    return _fn


def _failing_query_hashes(*, transport: bool = True):
    """query_hashes_fn that always reports a transport/graphql failure
    (returns {} AND fires on_warn) — the shape a real transport error takes."""

    def _fn(weaviate_url, collection_name, on_warn=None):
        if on_warn is not None:
            kind = "transport_failure" if transport else "graphql_errors"
            on_warn(kind, {"collection": collection_name, "errors": ["boom"]})
        return {}

    return _fn


def _always_reachable(url: str) -> bool:
    return True


def _never_reachable(url: str) -> bool:
    return False


# ---------------------------------------------------------------------------
# Core matrix
# ---------------------------------------------------------------------------

def test_matching_hash_reports_no_drift(tmp_path: Path):
    content = "---\ntitle: Foo\ntype: concept\n---\n\nbody text\n"
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", content)
    hashes = {"MyKG": {"knowledge/concepts/foo.md": _hash_of(content)}}

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes(hashes),
    )
    assert report.status == "ok"
    assert report.missing == ()
    assert report.stale == ()
    assert report.scanned == 1


def test_missing_from_collection_reports_drift(tmp_path: Path):
    content = "---\ntitle: Bar\ntype: concept\n---\n\nbody text\n"
    _write(tmp_path / "knowledge" / "concepts" / "bar.md", content)
    hashes = {"MyKG": {}}  # collection has nothing for this file

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes(hashes),
    )
    assert report.status == "drift"
    assert report.missing == ("knowledge/concepts/bar.md",)
    assert report.stale == ()


def test_stale_hash_reports_drift(tmp_path: Path):
    content = "---\ntitle: Baz\ntype: concept\n---\n\ncurrent body\n"
    _write(tmp_path / "knowledge" / "concepts" / "baz.md", content)
    hashes = {"MyKG": {"knowledge/concepts/baz.md": "0" * 64}}  # deliberately wrong

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes(hashes),
    )
    assert report.status == "drift"
    assert report.stale == ("knowledge/concepts/baz.md",)
    assert report.missing == ()


def test_legacy_empty_content_hash_counts_as_stale_not_missing(tmp_path: Path):
    """A pre-v0.2.17 row with an empty content_hash IS present (the file_path
    key exists) but cannot be verified — batch_query_content_hashes's own
    documented convention treats this as "always stale", not "missing"."""
    content = "---\ntitle: Legacy\ntype: concept\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "legacy.md", content)
    hashes = {"MyKG": {"knowledge/concepts/legacy.md": ""}}

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes(hashes),
    )
    assert report.status == "drift"
    assert report.stale == ("knowledge/concepts/legacy.md",)
    assert report.missing == ()


def test_weaviate_unreachable_reports_unknown_not_missing(tmp_path: Path):
    """Reachability failure must never masquerade as 'everything is drifted'."""
    content = "---\ntitle: Foo\ntype: concept\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_never_reachable,
        query_hashes_fn=_fake_query_hashes({}),  # would report "missing" if reached
    )
    assert report.status == "unknown"
    assert report.missing == ()
    assert report.stale == ()


def test_transport_failure_during_query_reports_unknown(tmp_path: Path):
    """Reachable=True but the actual hash query fails (transport lost mid-
    query, GraphQL errors[]) must ALSO report 'unknown', not 'missing'."""
    content = "---\ntitle: Foo\ntype: concept\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_failing_query_hashes(),
    )
    assert report.status == "unknown"
    assert report.missing == ()


def test_empty_knowledge_dir_no_drift_no_crash(tmp_path: Path):
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()

    report = drift.scan_drift(
        knowledge_root,
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_never_reachable,  # must not even be consulted
        query_hashes_fn=_failing_query_hashes(),
    )
    assert report.status == "ok"
    assert report.scanned == 0


def test_absent_knowledge_dir_no_drift_no_crash(tmp_path: Path):
    """knowledge/ not created at all — same contract as an empty one."""
    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_never_reachable,
        query_hashes_fn=_failing_query_hashes(),
    )
    assert report.status == "ok"
    assert report.scanned == 0


# ---------------------------------------------------------------------------
# Exclusion buckets — archived, schema files, scope:shared
# ---------------------------------------------------------------------------

def test_archived_by_path_never_reported_as_missing(tmp_path: Path):
    content = "---\ntitle: Old\ntype: concept\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "archive" / "old.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"MyKG": {}}),
    )
    assert report.status == "ok"
    assert report.archived_skipped == 1
    assert report.missing == ()


def test_archived_by_frontmatter_status_never_reported_as_missing(tmp_path: Path):
    content = "---\ntitle: Old2\ntype: concept\nstatus: deprecated\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "old2.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"MyKG": {}}),
    )
    assert report.status == "ok"
    assert report.archived_skipped == 1


def test_architecture_dir_not_mistaken_for_archive(tmp_path: Path):
    """Exact-segment match only — 'architecture/' must NOT trip the archive
    path check (the v0.2.70 FIX #5 substring-vs-segment lesson)."""
    content = "---\ntitle: Arch\ntype: concept\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "architecture" / "arch.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"MyKG": {}}),  # not present -> drift
    )
    assert report.archived_skipped == 0
    assert report.status == "drift"
    assert report.missing == ("knowledge/architecture/arch.md",)


def test_tag_hierarchy_and_vocabulary_excluded(tmp_path: Path):
    _write(tmp_path / "knowledge" / "TAG_HIERARCHY.md", "# tags\n")
    _write(tmp_path / "knowledge" / "VOCABULARY.md", "# vocab\n")

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"MyKG": {}}),
    )
    assert report.status == "ok"
    assert report.scanned == 0
    assert report.excluded_skipped == 2


def test_scope_shared_checked_against_shared_collection(tmp_path: Path):
    content = "---\ntitle: Shared\ntype: concept\nscope: shared\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "shared.md", content)
    hashes = {"SharedKG": {"knowledge/concepts/shared.md": _hash_of(content)}}

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        shared_kg_collection="SharedKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes(hashes),
    )
    assert report.status == "ok"
    assert report.shared_scope_skipped == 0


def test_scope_shared_missing_from_shared_collection_is_drift(tmp_path: Path):
    content = "---\ntitle: Shared2\ntype: concept\nscope: shared\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "shared2.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        shared_kg_collection="SharedKG",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"SharedKG": {}}),
    )
    assert report.status == "drift"
    assert report.missing == ("knowledge/concepts/shared2.md",)


def test_scope_shared_without_configured_shared_collection_is_skipped_not_missing(tmp_path: Path):
    """No SHARED_KG_COLLECTION configured -> checking the project collection
    for a scope:shared node would be WRONG (its rows were migrated out).
    Must be skipped, never flagged as drifted."""
    content = "---\ntitle: Shared3\ntype: concept\nscope: shared\n---\n\nbody\n"
    _write(tmp_path / "knowledge" / "concepts" / "shared3.md", content)

    report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="MyKG",
        shared_kg_collection="",
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({"MyKG": {}}),
    )
    assert report.status == "ok"
    assert report.shared_scope_skipped == 1
    assert report.missing == ()


# ---------------------------------------------------------------------------
# Archived-check / exclusion parity with sync_knowledge_graph.py (SSOT)
# ---------------------------------------------------------------------------

SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


def test_archive_segments_match_sync_script_source():
    text = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert '_ARCHIVE_DIR_SEGMENTS = {"archive", ".archive", "_archive"}' in text, (
        "sync_knowledge_graph.py's _ARCHIVE_DIR_SEGMENTS literal changed — "
        "update vco_lib.kg_sync_drift.ARCHIVE_DIR_SEGMENTS to match"
    )
    assert drift.ARCHIVE_DIR_SEGMENTS == frozenset({"archive", ".archive", "_archive"})


def test_archived_status_values_match_sync_script_source():
    text = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert 'status in ("archived", "deprecated", "superseded")' in text, (
        "sync_knowledge_graph.py's archived-status tuple changed — update "
        "vco_lib.kg_sync_drift.ARCHIVED_STATUS_VALUES to match"
    )
    assert drift.ARCHIVED_STATUS_VALUES == frozenset(
        {"archived", "deprecated", "superseded"}
    )


def test_excluded_sync_basenames_match_sync_script_source():
    text = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "EXCLUDED_FILES = {'TAG_HIERARCHY.md', 'VOCABULARY.md'}" in text, (
        "sync_knowledge_graph.py's EXCLUDED_FILES literal changed — update "
        "vco_lib.kg_sync_drift.EXCLUDED_SYNC_BASENAMES to match"
    )
    assert drift.EXCLUDED_SYNC_BASENAMES == frozenset(
        {"TAG_HIERARCHY.md", "VOCABULARY.md"}
    )


# ---------------------------------------------------------------------------
# Surfacing — deferral ledger integration
# ---------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".claude" / "context").mkdir(parents=True)
    return root


def _has_condition(folder: Path, cid: str) -> bool:
    from vco_lib.deferral_report import DeferralReport

    report = DeferralReport.read(folder)
    if not report:
        return False
    return any(getattr(e, "condition_id", "") == cid for e in report.entries)


def test_surface_drift_emits_entry_on_drift(project: Path):
    report = drift.DriftReport(
        status="drift",
        scanned=1,
        missing=("knowledge/concepts/foo.md",),
        stale=(),
        detail="1 missing, 0 stale out of 1 checked",
    )
    drift.surface_drift(project, report)
    assert _has_condition(project, drift.CID_DRIFT)


def test_surface_drift_entry_is_action_required_not_auto_retryable(project: Path):
    """This module cannot wire a real auto_retryable handler (outside its
    file boundary) — claiming that disposition without one would tell the
    reader 'this fixes itself' when nothing does. Must be explicit."""
    from vco_lib.deferral_report import DeferralReport

    report = drift.DriftReport(
        status="drift", scanned=1, missing=("knowledge/x.md",), stale=(),
    )
    drift.surface_drift(project, report)
    parsed = DeferralReport.read(project)
    entry = next(e for e in parsed.entries if e.condition_id == drift.CID_DRIFT)
    assert entry.disposition == "action_required"


def test_surface_drift_resolves_entry_on_ok(project: Path):
    # First emit a drift entry, then a clean scan must clear it.
    drift.surface_drift(
        project,
        drift.DriftReport(status="drift", scanned=1, missing=("x.md",), stale=()),
    )
    assert _has_condition(project, drift.CID_DRIFT)

    drift.surface_drift(project, drift.DriftReport(status="ok", scanned=1))
    assert not _has_condition(project, drift.CID_DRIFT)


def test_surface_drift_unknown_status_does_not_touch_ledger(project: Path):
    # A prior drift entry must survive an inconclusive probe untouched.
    drift.surface_drift(
        project,
        drift.DriftReport(status="drift", scanned=1, missing=("x.md",), stale=()),
    )
    assert _has_condition(project, drift.CID_DRIFT)

    drift.surface_drift(
        project,
        drift.DriftReport(status="unknown", scanned=1, detail="weaviate down"),
    )
    # Still present — an inconclusive probe must not erase a real finding.
    assert _has_condition(project, drift.CID_DRIFT)


def test_surface_drift_never_raises_on_bad_folder(tmp_path: Path):
    # A folder with no .claude/context and no write permission assumptions —
    # surfacing must be best-effort and never propagate.
    bogus = tmp_path / "does_not_exist_and_is_not_created"
    report = drift.DriftReport(status="drift", scanned=1, missing=("x.md",), stale=())
    drift.surface_drift(bogus, report)  # must not raise


# ---------------------------------------------------------------------------
# check_kg_binding — the DISTINCT setup-gap check (2026-09-01 addendum)
# ---------------------------------------------------------------------------

def _no_hub_resolver(folder: Path):
    """A resolve_cfg_fn stand-in that always finds nothing (mirrors a hub
    that is unreachable, or a project unknown to it)."""
    class _Cfg:
        kg_collection = ""

    return _Cfg()


def _raising_hub_resolver(folder: Path):
    raise ConnectionError("hub unreachable")


def _hub_resolver_returning(name: str):
    class _Cfg:
        kg_collection = name

    def _fn(folder: Path):
        return _Cfg()

    return _fn


def test_check_kg_binding_unbound_when_nothing_resolves(tmp_path: Path):
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", "---\ntitle: Foo\n---\n\nbody\n")

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_no_hub_resolver,
    )
    assert result.status == "unbound"
    assert result.kg_collection == ""


def test_check_kg_binding_bound_via_explicit_argument(tmp_path: Path):
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", "---\ntitle: Foo\n---\n\nbody\n")

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        kg_collection="MyRealKG",
        # A resolver that would raise if it were ever CALLED — proves the
        # explicit argument short-circuits before any hub round-trip.
        resolve_cfg_fn=_raising_hub_resolver,
    )
    assert result.status == "bound"
    assert result.kg_collection == "MyRealKG"
    assert "explicit" in result.detail


def test_check_kg_binding_bound_via_local_settings_json(tmp_path: Path):
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", "---\ntitle: Foo\n---\n\nbody\n")
    (tmp_path / ".claude").mkdir(parents=True)
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"env": {"KG_COLLECTION": "LocalConfigKG"}}', encoding="utf-8",
    )

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_no_hub_resolver,
    )
    assert result.status == "bound"
    assert result.kg_collection == "LocalConfigKG"


def test_check_kg_binding_bound_via_hub_resolver(tmp_path: Path):
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", "---\ntitle: Foo\n---\n\nbody\n")

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_hub_resolver_returning("HubKG"),
    )
    assert result.status == "bound"
    assert result.kg_collection == "HubKG"
    assert "hub" in result.detail.lower()


def test_check_kg_binding_hub_failure_falls_through_not_crashes(tmp_path: Path):
    """A hub probe that raises must fall through to 'unbound' (fail-open,
    same contract as every other hub-resolver caller), never propagate."""
    _write(tmp_path / "knowledge" / "concepts" / "foo.md", "---\ntitle: Foo\n---\n\nbody\n")

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_raising_hub_resolver,
    )
    assert result.status == "unbound"


def test_check_kg_binding_ok_when_no_content(tmp_path: Path):
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()

    result = drift.check_kg_binding(
        tmp_path, knowledge_root,
        resolve_cfg_fn=_raising_hub_resolver,  # must not even be reached
    )
    assert result.status == "ok"


def test_check_kg_binding_archived_only_content_is_ok(tmp_path: Path):
    """Only archived content on disk -> nothing worth binding for."""
    _write(
        tmp_path / "knowledge" / "archive" / "old.md",
        "---\ntitle: Old\n---\n\nbody\n",
    )

    result = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_raising_hub_resolver,
    )
    assert result.status == "ok"


def test_surface_binding_gap_emits_and_clears(project: Path):
    unbound = drift.BindingCheck(status="unbound", detail="no binding anywhere")
    drift.surface_binding_gap(project, unbound)
    assert _has_condition(project, drift.CID_UNBOUND)

    bound = drift.BindingCheck(status="bound", kg_collection="X", detail="resolved")
    drift.surface_binding_gap(project, bound)
    assert not _has_condition(project, drift.CID_UNBOUND)


def test_surface_binding_gap_entry_is_action_required(project: Path):
    from vco_lib.deferral_report import DeferralReport

    unbound = drift.BindingCheck(status="unbound", detail="no binding anywhere")
    drift.surface_binding_gap(project, unbound)
    parsed = DeferralReport.read(project)
    entry = next(e for e in parsed.entries if e.condition_id == drift.CID_UNBOUND)
    assert entry.disposition == "action_required"


# ---------------------------------------------------------------------------
# The core field-report scenario: every individual signal is healthy, the
# aggregate is not, and no per-component check alone would have caught it.
# ---------------------------------------------------------------------------

def test_all_individual_signals_healthy_but_aggregate_is_unsynced(tmp_path: Path):
    """Models the exact reported symptom: a Claude Code session (file tools
    only) writes a knowledge/ node to a project that was registered but
    never bundled. Every individual signal is green:

      * the write itself succeeded (the file is readable on disk, valid
        frontmatter, valid content) — nothing here can detect a problem by
        inspecting the file alone;
      * there is no hook to fail, because there is no .claude/ tree at all;
      * there is no .claude/scripts/kg-sync to report an error, because it
        does not exist in this project;
      * a naive drift scan (scan_drift called with whatever the caller
        happened to have on hand, here an EMPTY collection name because
        nothing was ever bound) returns "unknown" — which is HONEST (never
        a false "ok"), but is not the clear, actionable diagnosis a reader
        needs.

    check_kg_binding is the check that actually names the problem: a
    project with real, valid knowledge/ content and a resolvable-nowhere
    binding is a DISTINCT, cheaply-detectable state from node-level drift.
    """
    node = tmp_path / "knowledge" / "concepts" / "success_looking_node.md"
    _write(
        node,
        "---\ntitle: Looks totally fine\ntype: concept\n---\n\n"
        "This node was written successfully. The write tool reported no "
        "error. Nothing about this file is wrong.\n",
    )
    # Sanity: the file really is readable, valid content — the "everything
    # individually healthy" premise.
    assert node.is_file()
    assert node.read_text(encoding="utf-8").startswith("---")

    # No .claude/ directory exists anywhere under tmp_path — the exact
    # "registered but unbundled" shape (no hook, no kg-sync, no kg-search).
    assert not (tmp_path / ".claude").exists()

    # A caller that ONLY ever runs the node-level drift scan, with whatever
    # collection name it happened to have (none, since nothing was ever
    # bound) gets an HONEST "cannot tell" — not a false "ok", but also not
    # the actionable diagnosis.
    naive_report = drift.scan_drift(
        tmp_path / "knowledge",
        weaviate_url="http://fake:8081",
        kg_collection="",  # nothing resolvable — the realistic value here
        reachable_fn=_always_reachable,
        query_hashes_fn=_fake_query_hashes({}),
    )
    assert naive_report.status == "unknown"
    assert naive_report.status != "drift"  # scan_drift cannot even ask the question

    # check_kg_binding is the check that actually NAMES this failure.
    binding = drift.check_kg_binding(
        tmp_path, tmp_path / "knowledge",
        resolve_cfg_fn=_no_hub_resolver,
    )
    assert binding.status == "unbound"
    assert "registered but never bundled" in binding.detail


# ---------------------------------------------------------------------------
# Standalone CLI — proves bundle independence with a REAL subprocess, not
# just direct-function calls. Answers the field report's core question
# ("does the check exist in the exact scenario where it is most needed?")
# with a genuine end-to-end run against a target with NO .claude/ tree.
# ---------------------------------------------------------------------------

def test_standalone_cli_runs_against_project_with_no_claude_tree(tmp_path: Path):
    target = tmp_path / "unbundled_project"
    _write(
        target / "knowledge" / "concepts" / "node.md",
        "---\ntitle: Node\ntype: concept\n---\n\nbody\n",
    )
    # The defining precondition: genuinely no .claude/ anywhere.
    assert not (target / ".claude").exists()

    proc = subprocess.run(
        [
            sys.executable, "-m", "vco_lib.kg_sync_drift",
            "--project-root", str(target),
            "--no-hub",
            "--json",
        ],
        # REPO_ROOT (defined above, next to SYNC_SCRIPT) is used only so
        # `vco_lib` resolves unambiguously for THIS subprocess — the check
        # itself never reads anything from REPO_ROOT; only the TARGET
        # project's own knowledge/ and .claude/ are touched.
        cwd=str(REPO_ROOT),
        env=child_env(os.environ, VCT_DISABLE_HUB_RESOLVER="1"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["binding"]["status"] == "unbound"

    # The surfaced finding lands in the TARGET project's ledger — proving
    # the whole pipeline (detect -> surface) works for a project that had
    # no .claude/ tree before this ran.
    deferred = target / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert deferred.is_file()
    assert "kg_binding_missing" in deferred.read_text(encoding="utf-8")
