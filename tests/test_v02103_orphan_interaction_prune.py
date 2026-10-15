# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (defect 7 + R12 F1/F2/F3): unanchored orphan CodeInteraction rows
become prunable and stop hiding behind "0 stale rows" — with ONE predicate,
narrowed proof, and no argument-type mismatch.

R12 fixes under test:
  * F1: the prune hands the predicate the row's UUID STRING (the analyzer
    previously passed the row OBJECT, so the real weaviate client raised and
    every verdict was "unknown" — reaping dead while the probe counted).
    Every test here drives the REAL predicate
    (``is_provable_codegraph_orphan`` → ``classify_codegraph_orphan``) through
    ``prune_collection``; the fake ``query`` validates the uuid the way the
    real client does (non-str/UUID → TypeError), so the mismatch class is
    caught for good.
  * F2: "provable orphan" = no reference at all, or every reference DANGLING
    (zero target objects, verified by a successful query). A target that
    EXISTS but has no path is UNCERTAIN → keep.
  * F3: probe and purge route through the ONE predicate (same project /
    refused-extra filters) — a parity test runs both over one fixture and
    asserts count == reaped.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vco_lib import codegraph_resync as cr
from vco_lib.codegraph_prune import prune_collection

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _lang_id(v):
    return str(v or "").lower() or ""


class _RefObj:
    def __init__(self, properties):
        self.properties = dict(properties)


def _refs(*entries):
    """entries: (ref_prop, [target-props...]) — a missing entry means the
    reference property is ABSENT from the row."""
    out = {}
    for ref_prop, targets in entries:
        out[ref_prop] = types.SimpleNamespace(
            objects=[_RefObj(t) for t in targets])
    return out


def _valid_uuid(value):
    """Mirror the real client's ``get_valid_uuid`` strictness (R12-F1): a
    non-str/UUID argument raises, exactly like weaviate's util does."""
    if isinstance(value, str):
        return value
    if isinstance(value, _uuid.UUID):
        return str(value)
    raise TypeError(f"Cannot parse {value!r} as an UUID")


class _Coll:
    """CodeInteraction-shaped collection fake. ``fetch`` maps a uuid STRING to
    the object the reference fetch returns; the fetch itself validates the
    uuid like the real client (non-str → TypeError)."""

    def __init__(self, name="P_CodeInteraction", rows=None, props=None,
                 iter_raises_after=None, fetch=None, fetch_raises=()):
        self.name = name
        self._rows = rows or []
        self.deleted = []
        self._iter_raises_after = iter_raises_after
        self._fetch = dict(fetch or {})
        self._fetch_raises = set(fetch_raises)
        self.fetch_calls = []
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[SimpleNamespace(name=p)
                            for p in (props if props is not None
                                      else ["project", "file_path",
                                            "project_source"])]
            )
        )
        self.data = types.SimpleNamespace(
            delete_by_id=lambda uuid: self.deleted.append(uuid)
        )
        self.query = types.SimpleNamespace(
            fetch_object_by_id=self._fetch_by_id)

    def _fetch_by_id(self, uuid, return_references=None):
        uid = _valid_uuid(uuid)  # R12-F1: the real client's strictness
        self.fetch_calls.append(uid)
        if uid in self._fetch_raises:
            raise RuntimeError("503 unavailable")
        return self._fetch.get(uid)

    def iterator(self, return_properties=None):
        for i, row in enumerate(self._rows):
            yield row
            if self._iter_raises_after is not None and i >= self._iter_raises_after:
                raise RuntimeError("enumeration failed mid-scan")


def _row(uid, file_path=None, project="P", language=None, project_source=None):
    props = {"project": project}
    if file_path is not None:
        props["file_path"] = file_path
    if language is not None:
        props["language"] = language
    if project_source is not None:
        props["project_source"] = project_source
    return SimpleNamespace(uuid=uid, properties=props)


def _fetch_obj(uid, refs):
    o = SimpleNamespace(uuid=uid, properties={"file_path": None})
    o.references = refs
    return o


# ── prune: the act arms (REAL predicate, uuid-validating fake) ─────────────

def test_provably_orphaned_row_is_reaped_on_full_walk():
    """ACT: anchor-less row + skips in play (every real walk has skips — the
    pre-fix preserve rule made these rows immortal) + NO reference at all →
    deleted through the real predicate (R12-F1: uuid string, not the row)."""
    coll = _Coll(rows=[_row("i-0")], fetch={"i-0": _fetch_obj("i-0", {})})
    pruned, failures = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert failures == 0
    assert pruned == 1 and coll.deleted == ["i-0"]
    assert coll.fetch_calls == ["i-0"], "the predicate must fetch BY UUID"


def test_dangling_only_references_are_reaped():
    """ACT (F2's narrowed proof): every reference the row has is DANGLING
    (zero target objects, verified by a successful query) → provably dead."""
    coll = _Coll(rows=[_row("i-0")], fetch={
        "i-0": _fetch_obj("i-0", _refs(
            ("source_function", []),
            ("source_module", []),
        )),
    })
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 1 and coll.deleted == ["i-0"]


def test_orphan_reaping_logs_id_and_reason(caplog):
    coll = _Coll(rows=[_row("i-0")], fetch={"i-0": _fetch_obj("i-0", {})})
    with caplog.at_level("INFO", logger="vco_lib.codegraph_prune"):
        prune_collection(
            coll, set(), project_name="P", canonical_lang_id=_lang_id,
            preserve_paths={"src/skipped.py"},
            reap_orphans=True,
        )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "i-0" in joined and "no file_path" in joined, (
        f"each removed row must be logged (id + why); got: {joined!r}"
    )


# ── prune: the leave-alone arms ─────────────────────────────────────────────

def test_row_with_resolvable_reference_is_left_alone():
    """LEAVE-ALONE: a reference resolving to a path is ANCHORED — the anchor
    backfill converges it; never deleted on this evidence."""
    coll = _Coll(rows=[_row("i-ref")], fetch={
        "i-ref": _fetch_obj("i-ref", _refs(
            ("source_function", [{"file_path": "src/caller.py"}]))),
    })
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == []


def test_live_target_without_path_is_uncertain_and_kept():
    """LEAVE-ALONE (R12-F2): the reference target EXISTS but carries no path
    (a legacy CodeFunction that itself predates file_path) — NOT provably
    dead. Pre-fix this classified orphan and deleted the row."""
    coll = _Coll(rows=[_row("i-live")], fetch={
        "i-live": _fetch_obj("i-live", _refs(
            ("source_function", [{"file_path": None, "full_name": "legacy"}]))),
    })
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == [], (
        "a live target without a path is UNCERTAIN — never a delete authorisation"
    )


def test_unknown_fetch_verdict_preserves():
    """LEAVE-ALONE: a failed fetch (or a concurrently-vanished row) is
    UNKNOWN → preserved, never deleted."""
    coll = _Coll(rows=[_row("i-err"), _row("i-gone")],
                 fetch={"i-gone": None}, fetch_raises=("i-err",))
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == []


def test_row_with_file_path_never_reaches_the_predicate():
    """LEAVE-ALONE: an ANCHORED row (file_path set, in the preserve set) is
    preserved and the predicate is never consulted for it."""
    coll = _Coll(rows=[_row("i-anchored", file_path="src/skipped.py")])
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == []
    assert coll.fetch_calls == [], "anchored rows must not reach the predicate"


def test_foreign_project_row_is_never_reaped():
    """LEAVE-ALONE (R12-F3): the predicate applies the purge's project filter
    — a foreign-project row is out of scope, whatever its references."""
    coll = _Coll(rows=[_row("i-foreign", project="Other")], fetch={})
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == []


def test_refused_extra_source_row_is_never_reaped():
    """LEAVE-ALONE (R12-F3): a row stamped with a preserve-source (a refused
    extra root the analyzer keeps) is never reaped, reference-less or not."""
    coll = _Coll(rows=[_row("i-extra", project_source="/repo/nested/extra")],
                 fetch={})
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        preserve_sources={"/repo/nested/extra"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == []
    assert coll.fetch_calls == [], "filtered-out rows must not pay a fetch"


def test_scoped_prune_judges_no_unknown_language_row():
    """LEAVE-ALONE (partial walk): a language-scoped prune never judges a
    pre-migration (unknown-language) row — preserved before the anchor logic."""
    coll = _Coll(rows=[_row("i-0")])
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        language_scope="python",
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,  # even if a caller mis-wires it
    )
    assert pruned == 0 and coll.deleted == []


def test_enumeration_failure_deletes_nothing():
    """LEAVE-ALONE (enumeration failure): the scan dies AFTER the orphan
    candidate was seen — deferred deletion must authorise nothing."""
    coll = _Coll(rows=[_row("i-0")], fetch={"i-0": _fetch_obj("i-0", {})},
                 iter_raises_after=0)
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and coll.deleted == [], (
        "an enumeration failure mid-scan must drop the orphan candidates"
    )


def test_orphan_delete_failure_counts_as_prune_failure():
    """Accounting: a failed orphan delete feeds the same failures counter as
    any stale-row delete (success→partial signalling)."""
    coll = _Coll(rows=[_row("i-0")], fetch={"i-0": _fetch_obj("i-0", {})})

    def _boom(uuid):
        raise RuntimeError("Weaviate-500 subtract prop lengths")

    coll.data = types.SimpleNamespace(delete_by_id=_boom)
    pruned, failures = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        reap_orphans=True,
    )
    assert pruned == 0 and failures == 1


def test_reaping_off_keeps_pre_fix_preserve_semantics():
    """Back-compat: with ``reap_orphans`` off (the default — every partial or
    unproven walk) the anchor-less preserve rule is unchanged."""
    coll = _Coll(rows=[_row("i-0")], fetch={})
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
    )
    assert pruned == 0 and coll.deleted == []
    assert coll.fetch_calls == []


# ── the classifier (three-way verdict, R12-F2) ─────────────────────────────

def test_classifier_three_way_verdicts():
    good = _Coll(fetch={
        "r-path": _fetch_obj("r-path", _refs(
            ("source_function", [{"file_path": "src/caller.py"}]))),
        "r-none": _fetch_obj("r-none", {}),
        "r-dangling": _fetch_obj("r-dangling", _refs(
            ("source_function", []))),
        "r-live-nopath": _fetch_obj("r-live-nopath", _refs(
            ("source_module", [{"path": None}]))),
        "r-mixed": _fetch_obj("r-mixed", _refs(
            ("source_function", []),
            ("source_module", [{"path": "src/m.py"}]))),
    }, fetch_raises=("r-err",))
    assert cr.classify_codegraph_orphan(good, "r-path") == "anchored"
    assert cr.classify_codegraph_orphan(good, "r-none") == "orphan"
    assert cr.classify_codegraph_orphan(good, "r-dangling") == "orphan"
    assert cr.classify_codegraph_orphan(good, "r-live-nopath") == "unknown"
    assert cr.classify_codegraph_orphan(good, "r-mixed") == "anchored"
    assert cr.classify_codegraph_orphan(good, "r-err") == "unknown"
    assert cr.classify_codegraph_orphan(good, "r-gone") == "unknown"


def test_classifier_rejects_a_row_object_the_way_the_real_client_does():
    """R12-F1's regression lock: feeding the classifier a row OBJECT (the
    shipped mismatch) must surface as UNKNOWN — never as an orphan verdict —
    because the uuid-validation raises exactly like weaviate's util."""
    coll = _Coll(fetch={"i-0": _fetch_obj("i-0", {})})
    row_obj = _row("i-0")
    assert cr.classify_codegraph_orphan(coll, row_obj) == "unknown"


def test_predicate_filters_match_the_purge():
    """R12-F3: the ONE predicate's filters — project, preserve_sources, and
    the structural refused-extra (nested-under-root, non-primary) rule the
    driver-side probe uses when it cannot know the per-walk set."""
    coll = _Coll(fetch={
        "a": _fetch_obj("a", {}),                       # plain orphan
        "f": _fetch_obj("f", {}),                       # foreign project
        "n": _fetch_obj("n", {}),                       # nested-extra tenant
    })
    assert cr.is_provable_codegraph_orphan(
        coll, "a", {"file_path": None, "project": "P"},
        project_name="P") is True
    assert cr.is_provable_codegraph_orphan(
        coll, "f", {"file_path": None, "project": "Other"},
        project_name="P") is False
    assert cr.is_provable_codegraph_orphan(
        coll, "n", {"file_path": None, "project": "P",
                    "project_source": "/repo/nested/extra"},
        project_name="P",
        preserve_sources={"/repo/nested/extra"}) is False
    # The probe's structural twin: no preserve_sources, but the repo root
    # says the source is a nested (refused-by-construction) extra tenant.
    assert cr.is_provable_codegraph_orphan(
        coll, "n", {"file_path": None, "project": "P",
                    "project_source": "/repo/nested/extra"},
        project_name="P", repo_root=Path("/repo")) is False
    # A PRIMARY-source row is still judged.
    assert cr.is_provable_codegraph_orphan(
        coll, "a", {"file_path": None, "project": "P",
                    "project_source": "/repo"},
        project_name="P", repo_root=Path("/repo")) is True


# ── probe ↔ purge parity (R12-F3) ───────────────────────────────────────────

PARITY_ROWS = [
    _row("p-orphan"),                                       # reaped + counted
    _row("p-anchored", file_path="src/skipped.py"),         # kept, not counted
    _row("p-ref"),                                          # anchored by ref
    _row("p-live-nopath"),                                  # F2: uncertain
    _row("p-foreign", project="Other"),                     # out of project
    _row("p-extra", project_source="/repo/nested/extra"),   # refused tenant
]

PARITY_FETCH = {
    "p-orphan": _fetch_obj("p-orphan", {}),
    "p-ref": _fetch_obj("p-ref", _refs(
        ("source_function", [{"file_path": "src/x.py"}]))),
    "p-live-nopath": _fetch_obj("p-live-nopath", _refs(
        ("source_function", [{"file_path": None}]))),
    "p-foreign": _fetch_obj("p-foreign", {}),
    "p-extra": _fetch_obj("p-extra", {}),
}


def _parity_coll():
    return _Coll(rows=list(PARITY_ROWS), fetch=dict(PARITY_FETCH))


def test_probe_and_purge_agree_on_the_same_fixture(monkeypatch):
    """R12-F3: ONE predicate, one answer — the probe's count equals the
    purge's reaped set over identical rows (the pre-fix probe counted rows
    the purge preserved, so convergence never happened)."""
    purge_coll = _parity_coll()
    pruned, _ = prune_collection(
        purge_coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        preserve_sources={"/repo/nested/extra"},
        reap_orphans=True,
    )
    assert purge_coll.deleted == ["p-orphan"], "sanity: exactly the provable row"

    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    probe_coll = _parity_coll()
    count = cr.count_orphan_interaction_rows(
        "P", repo_root=Path("/repo"), client=types.SimpleNamespace(
            collections=types.SimpleNamespace(
                exists=lambda n: n == probe_coll.name,
                get=lambda n: probe_coll,
            ),
            close=lambda: None,
        ))
    assert count == 1, (
        f"probe must count exactly what the purge reaped; got {count}"
    )


def test_probe_zero_when_collection_absent(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    empty = types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda n: False, get=lambda n: None,
        ),
        close=lambda: None,
    )
    assert cr.count_orphan_interaction_rows("P", client=empty) == 0


def test_probe_none_on_enumeration_failure(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")

    class _IterBoom(_Coll):
        def iterator(self, return_properties=None):
            raise RuntimeError("scan failed")
            yield  # pragma: no cover

    coll = _IterBoom()
    assert cr.count_orphan_interaction_rows(
        "P", client=types.SimpleNamespace(
            collections=types.SimpleNamespace(
                exists=lambda n: True, get=lambda n: coll),
            close=lambda: None,
        )) is None


# ── the analyzer-side gating (R12-F2: full, unscoped, all-extras walk) ─────

def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v02103_orphan_prune_analyzer", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module missing: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


def _gated_analyzer(analyzer_mod):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(
        analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst.visited_uuids = set()
    inst._track_visited = True
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    inst._prune_language = ""
    inst._refused_extra_sources = set()
    inst._dropped_extra_sources = set()
    inst._strict_source = False
    inst._analyze_repo_root_raw = ""
    inst.modules_collection = None
    inst.classes_collection = None
    inst.functions_collection = None
    inst.apis_collection = None
    inst.interactions_collection = _Coll("P_CodeInteraction")
    return inst


def _captured_reap_kwargs(analyzer_mod, monkeypatch, analyzer):
    captured = []

    def _capture(collection, visited_uuids, **kw):
        captured.append((getattr(collection, "name", "?"), kw.get("reap_orphans")))
        return (0, 0)

    monkeypatch.setattr(analyzer_mod, "_prune_collection_impl", _capture)
    analyzer._prune_stale_objects()
    return captured


def test_analyzer_gates_reaping_on_scope_and_extra_coverage(
        analyzer_mod, monkeypatch):
    """R12-F2: ``reap_orphans`` is passed ONLY for CodeInteraction on a full,
    unscoped walk that refused/dropped NO extra folder — a refused extra, an
    offline (dropped) extra, or a language scope each disable it entirely."""
    base = _gated_analyzer(analyzer_mod)
    assert dict(_captured_reap_kwargs(analyzer_mod, monkeypatch, base)
                )["P_CodeInteraction"] is True

    scoped = _gated_analyzer(analyzer_mod)
    scoped._prune_language = "python"
    assert dict(_captured_reap_kwargs(analyzer_mod, monkeypatch, scoped)
                )["P_CodeInteraction"] is False

    refused = _gated_analyzer(analyzer_mod)
    refused._refused_extra_sources = {"/repo/nested"}
    assert dict(_captured_reap_kwargs(analyzer_mod, monkeypatch, refused)
                )["P_CodeInteraction"] is False

    dropped = _gated_analyzer(analyzer_mod)
    dropped._dropped_extra_sources = {"/mnt/offline-extra"}
    assert dict(_captured_reap_kwargs(analyzer_mod, monkeypatch, dropped)
                )["P_CodeInteraction"] is False


def test_analyzer_records_dropped_extras(analyzer_mod, tmp_path):
    """R12-F2: an extra folder that is offline/unmounted (does not resolve or
    does not exist) is RECORDED, so the run's reaping is disabled instead of
    silently assuming full coverage."""
    analyzer = analyzer_mod.CodeGraphAnalyzer("P")
    missing = tmp_path / "unmounted-extra"
    analyzer.analyze_repository(tmp_path, extra_paths=[missing])
    assert analyzer._dropped_extra_sources == {missing.as_posix()}, (
        "an offline extra must be recorded as dropped coverage"
    )


# ── the driver's converged verdict (R12-F4) and the deferral (F5) ──────────

def _patch_driver_guts(monkeypatch, tmp_path, *, stale_counts, orphans):
    orphan_recorded = []

    def _fake_orphan_record(repo_root, project_name, count, command_to_apply):
        orphan_recorded.append(
            {"count": count, "command": command_to_apply})

    orphan_resolved = []
    unconverged = []

    def _fake_unconverged(repo_root, project_name, counts, resume_cmd,
                          stuck_identities=None):
        unconverged.append({"counts": counts, "resume_cmd": resume_cmd})

    import vco_lib.code_embed_exposure as _exposure_mod

    exposure_cleared = []
    monkeypatch.setattr(_exposure_mod, "clear_exposure_bump",
                        lambda p: exposure_cleared.append(p) or True)
    monkeypatch.setattr(cr, "walk_uses_container", lambda p: False)
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *a, **k: True)
    monkeypatch.setattr(cr, "_maybe_run_vectorless_audit", lambda *a, **k: None)
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: dict(stale_counts))
    monkeypatch.setattr(cr, "count_orphan_interaction_rows",
                        lambda *a, **k: orphans)
    monkeypatch.setattr(cr, "_record_unconverged_deferral", _fake_unconverged)
    monkeypatch.setattr(cr, "_record_orphan_interactions_deferral",
                        _fake_orphan_record)
    resolved = []
    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral",
                        lambda root: resolved.append(root))
    monkeypatch.setattr(cr, "_resolve_orphan_interactions_deferral",
                        lambda root: orphan_resolved.append(root))
    monkeypatch.setattr(cr, "spawn_code_summary_after_walk", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_report_terminal_to_hub", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_collect_terminal_stats", lambda lp: ({}, ""))
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(cr, "_python_exe",
                        SimpleNamespace(resolve_or_current=lambda: sys.executable))
    return dict(orphan_recorded=orphan_recorded, orphan_resolved=orphan_resolved,
                unconverged=unconverged, resolved=resolved,
                exposure_cleared=exposure_cleared)


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_discharges_then_records_orphans(monkeypatch, tmp_path, capsys):
    """R12-F4: with zero stale rows and orphans remaining, the CONVERGED
    discharge (exposure heal clear + persisted-entry clear) runs FIRST — the
    orphan condition must never block it — and the orphan entry is recorded
    under its OWN condition id with a --prune-stale remedy."""
    st = _patch_driver_guts(monkeypatch, tmp_path, stale_counts={}, orphans=2)
    rc = cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "converged: 0 stale rows" in out, (
        "the stale verdict is TRUE zero — the discharge must run"
    )
    assert st["exposure_cleared"] == ["Acme"], (
        "R12-F4: the orphan branch must not skip the exposure-heal discharge"
    )
    assert st["resolved"] == [tmp_path]
    assert st["unconverged"] == [], "no stale rows → no unconverged entry"
    assert len(st["orphan_recorded"]) == 1
    assert st["orphan_recorded"][0]["count"] == 2
    assert st["orphan_recorded"][0]["command"].endswith("--prune-stale")
    assert st["orphan_resolved"] == []
    assert "2 provable orphan" in out and "--prune-stale" in out


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_resolves_the_orphan_entry_on_a_positive_zero(
        monkeypatch, tmp_path):
    """Paired clear: a later run whose orphan probe is POSITIVELY zero
    resolves the orphan entry (the consented prune walk removed the rows)."""
    st = _patch_driver_guts(monkeypatch, tmp_path, stale_counts={}, orphans=0)
    rc = cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    assert rc == 0
    assert st["orphan_recorded"] == []
    assert st["orphan_resolved"] == [tmp_path]


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_undeterminable_orphans_neither_record_nor_clear(
        monkeypatch, tmp_path):
    st = _patch_driver_guts(monkeypatch, tmp_path, stale_counts={},
                            orphans=None)
    cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    assert st["orphan_recorded"] == [] and st["orphan_resolved"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_stale_path_does_not_record_orphans(monkeypatch, tmp_path, capsys):
    """Stale rows dominate: the ordinary unconverged entry is recorded and no
    orphan entry rides along (the next walk re-probes)."""
    st = _patch_driver_guts(monkeypatch, tmp_path,
                            stale_counts={"P_CodeFunction": 3}, orphans=2)
    capsys.readouterr()
    rc = cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT converged" in out and "3" in out
    assert len(st["unconverged"]) == 1
    assert st["unconverged"][0]["counts"] == {"P_CodeFunction": 3}
    assert st["orphan_recorded"] == []


def test_orphan_deferral_flavour_text_is_true():
    """R12-F5: the orphan flavour's prose must be TRUE — the command DELETES
    rows a full walk does not visit; the old 're-embeds only the remaining
    stale rows' claim must not appear in it."""
    entry = cr.build_unconverged_deferral(
        "Acme", None, "cmd --prune-stale", orphan_interactions=2)
    assert entry is not None
    assert entry.condition_id == cr._ORPHAN_CONDITION_ID
    assert entry.condition_id != cr._CONDITION_ID
    assert "no file anchors" in entry.detected or "file anchors" in entry.detected
    assert "DELETES" in entry.why_deferred
    assert "--prune-stale" in entry.why_deferred or "prune" in entry.why_deferred
    assert "re-embeds only" not in entry.why_deferred
    assert "never" in entry.why_deferred and "automatic" in entry.why_deferred


def test_ordinary_unconverged_flavour_is_unchanged():
    """The parameterisation must not disturb the ordinary (stale-rows)
    flavour: same condition id, same re-embed prose."""
    entry = cr.build_unconverged_deferral(
        "Acme", {"P_CodeFunction": 3}, "cmd")
    assert entry is not None
    assert entry.condition_id == cr._CONDITION_ID
    assert "re-embeds only the remaining stale rows" in entry.why_deferred


def test_orphan_condition_is_registered_action_required():
    """R12-F4: the orphan condition is declared in the registry as
    action_required with NO retry action — the auto-retry handler forwards no
    --prune-stale and must never be selected for it."""
    from vco_lib.deferral_registry import condition

    spec = condition(cr._ORPHAN_CONDITION_ID)
    assert spec is not None, "condition must be declared in the registry"
    assert spec.condition_class == "action_required"
    assert not spec.retry_action
    assert spec.clear_probe == "paired-resolution"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
