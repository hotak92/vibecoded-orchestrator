# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (defect 7): unanchored orphan CodeInteraction rows become prunable
and stop hiding behind "0 stale rows".

Defect under test: rows written before `file_path` existed — file_path NULL,
embed_revision NULL, NO source_module/source_function reference (the anchor
backfill's "unresolvable" verdict) — survived EVERY prune, because every
prune keys on file_path / the walked file set and preserves anchor-less rows
while any file was skipped; the resync convergence probe classified pathless
rows "purgeable" (nothing counts them) and read "0 stale rows" over them.

Fix under test (all arms fake Weaviate; conservative default = delete
NOTHING on any uncertainty):
  * prune: an anchor-less row is reaped ONLY when a resolver POSITIVELY
    returns "" (no file_path AND no resolvable source reference), only on a
    full unscoped walk, and only after the enumeration completed;
  * convergence: count_orphan_interaction_rows counts exactly those rows, and
    the driver blocks the "converged" verdict on a positive count.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from vco_lib import codegraph_resync as cr
from vco_lib.codegraph_prune import prune_collection

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _lang_id(v):
    return str(v or "").lower() or ""


class _Coll:
    """CodeInteraction-shaped collection fake (schema-aware, records deletes)."""

    def __init__(self, name="P_CodeInteraction", rows=None, props=None,
                 iter_raises_after=None):
        self.name = name
        self._rows = rows or []
        self.deleted = []
        self._iter_raises_after = iter_raises_after
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[SimpleNamespace(name=p)
                            for p in (props if props is not None
                                      else ["project", "file_path"])]
            )
        )
        self.data = types.SimpleNamespace(
            delete_by_id=lambda uuid: self.deleted.append(uuid)
        )

    def iterator(self, return_properties=None):
        for i, row in enumerate(self._rows):
            yield row
            if self._iter_raises_after is not None and i >= self._iter_raises_after:
                raise RuntimeError("enumeration failed mid-scan")


def _row(uid, file_path=None, project="P", language=None):
    props = {"project": project}
    if file_path is not None:
        props["file_path"] = file_path
    if language is not None:
        props["language"] = language
    return SimpleNamespace(uuid=uid, properties=props)


# ── prune: the act arm ──────────────────────────────────────────────────────

def test_provably_orphaned_row_is_reaped_on_full_walk():
    """ACT: anchor-less row + skips in play (every real walk has skips — the
    pre-fix preserve rule made these rows immortal) + resolver POSITIVELY
    unresolvable → deleted, with the id + why logged."""
    coll = _Coll(rows=[_row("i-0", file_path=None)])
    pruned, failures = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: "",
    )
    assert failures == 0
    assert pruned == 1 and coll.deleted == ["i-0"]


def test_orphan_reaping_logs_id_and_reason(caplog):
    coll = _Coll(rows=[_row("i-0", file_path=None)])
    with caplog.at_level("INFO", logger="vco_lib.codegraph_prune"):
        prune_collection(
            coll, set(), project_name="P", canonical_lang_id=_lang_id,
            preserve_paths={"src/skipped.py"},
            orphan_resolver=lambda collection, obj: "",
        )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "i-0" in joined and "no file_path" in joined, (
        f"each removed row must be logged (id + why); got: {joined!r}"
    )


# ── prune: the leave-alone arms ─────────────────────────────────────────────

def test_row_with_resolvable_reference_is_left_alone():
    """LEAVE-ALONE: a row whose reference resolves to a path is preserved —
    the anchor backfill converges it; never deleted on this evidence."""
    coll = _Coll(rows=[_row("i-ref", file_path=None)])
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: "src/ref-target.py",
    )
    assert pruned == 0 and coll.deleted == []


def test_row_with_file_path_keeps_ordinary_semantics():
    """LEAVE-ALONE: an ANCHORED row (file_path set, in the preserve set) is
    preserved and the orphan resolver is never consulted for it."""
    calls = []

    def _resolver(collection, obj):
        calls.append(str(obj.uuid))
        return ""

    coll = _Coll(rows=[_row("i-anchored", file_path="src/skipped.py")])
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=_resolver,
    )
    assert pruned == 0 and coll.deleted == []
    assert calls == [], "anchored rows must not reach the orphan resolver"


def test_scoped_prune_is_a_partial_walk_and_never_reaps():
    """LEAVE-ALONE (partial walk): a language-scoped prune never judges an
    unknown-language (pre-migration) row — preserved before the anchor logic."""
    coll = _Coll(rows=[_row("i-0", file_path=None)])  # no `language` prop
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        language_scope="python",
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: "",
    )
    assert pruned == 0 and coll.deleted == []


def test_enumeration_failure_deletes_nothing():
    """LEAVE-ALONE (enumeration failure): the scan dies AFTER the orphan
    candidate was seen — deferred deletion must authorise nothing."""
    coll = _Coll(rows=[_row("i-0", file_path=None)], iter_raises_after=0)
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: "",
    )
    assert pruned == 0 and coll.deleted == [], (
        "an enumeration failure mid-scan must drop the orphan candidates"
    )


def test_unknown_resolver_verdict_preserves():
    """LEAVE-ALONE (backfill/query unknown): resolver returns None (could not
    determine) or raises → preserved, never deleted."""
    coll = _Coll(rows=[_row("i-unk", file_path=None),
                       _row("i-raise", file_path=None)])
    verdicts = {"i-unk": None, "i-raise": None}
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: (
            verdicts.get(str(obj.uuid))
            if str(obj.uuid) == "i-unk"
            else (_ for _ in ()).throw(RuntimeError("query failed"))
        ),
    )
    assert pruned == 0 and coll.deleted == []


def test_orphan_delete_failure_counts_as_prune_failure():
    """Accounting: a failed orphan delete feeds the same failures counter as
    any stale-row delete (success→partial signalling)."""
    coll = _Coll(rows=[_row("i-0", file_path=None)])

    def _boom(uuid):
        raise RuntimeError("Weaviate-500 subtract prop lengths")

    coll.data = types.SimpleNamespace(delete_by_id=_boom)
    pruned, failures = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
        orphan_resolver=lambda collection, obj: "",
    )
    assert pruned == 0 and failures == 1


def test_no_resolver_keeps_pre_fix_preserve_semantics():
    """Back-compat: without a resolver the anchor-less preserve rule is
    byte-for-byte the pre-v0.2.103 behaviour (preserved while skips in play)."""
    coll = _Coll(rows=[_row("i-0", file_path=None)])
    pruned, _ = prune_collection(
        coll, set(), project_name="P", canonical_lang_id=_lang_id,
        preserve_paths={"src/skipped.py"},
    )
    assert pruned == 0 and coll.deleted == []


# ── the provability primitive ───────────────────────────────────────────────

class _RefObj:
    def __init__(self, properties):
        self.properties = dict(properties)


class _FetchColl(_Coll):
    """Adds query.fetch_object_by_id backed by a per-uuid reference map."""

    def __init__(self, rows_by_uuid):
        super().__init__(rows=list(rows_by_uuid.values()))
        self._by_uuid = rows_by_uuid
        self.fetch_calls = []

        def _fetch(uuid, return_references=None):
            self.fetch_calls.append(uuid)
            return self._by_uuid[uuid]

        self.query = types.SimpleNamespace(fetch_object_by_id=_fetch)


def _obj(uuid, refs):
    o = SimpleNamespace(uuid=uuid, properties={"file_path": None})
    o.references = refs
    return o


def test_resolve_anchor_tri_state():
    """path / "" (positively unresolvable) / None (fetch failed — never a
    delete authorization)."""
    good = _FetchColl({
        "r-path": _obj("r-path", {
            "source_function": types.SimpleNamespace(
                objects=[_RefObj({"file_path": "src/caller.py"})])}),
        "r-none": _obj("r-none", {}),
    })
    assert cr.resolve_codegraph_anchor(good, "r-path") == "src/caller.py"
    assert cr.resolve_codegraph_anchor(good, "r-none") == ""

    class _Boom:
        query = types.SimpleNamespace(
            fetch_object_by_id=lambda uuid, return_references=None: (
                (_ for _ in ()).throw(RuntimeError("503"))
            )
        )

    assert cr.resolve_codegraph_anchor(_Boom(), "r-x") is None


# ── the convergence probe ───────────────────────────────────────────────────

def _probe_client(coll):
    return types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda n: n is not None and n == coll.name,
            get=lambda n: coll,
        ),
        close=lambda: None,
    )


def test_probe_counts_only_provable_orphans(monkeypatch):
    """Anchored rows and reference-bearing rows are NOT counted; only the
    positively-unresolvable ones are (probe and purge agree by construction)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    coll = _FetchColl({
        "a-anchored": SimpleNamespace(
            uuid="a-anchored", properties={"file_path": "src/a.py"}),
        "o-1": _obj("o-1", {}),
        "o-2": _obj("o-2", {
            "source_module": types.SimpleNamespace(objects=[])}),  # dangling
    })
    count = cr.count_orphan_interaction_rows("P", client=_probe_client(coll))
    assert count == 2
    assert set(coll.fetch_calls) == {"o-1", "o-2"}, (
        "anchored rows must not pay a reference fetch"
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

    class _IterBoom(_FetchColl):
        def iterator(self, return_properties=None):
            raise RuntimeError("scan failed")
            yield  # pragma: no cover

    assert cr.count_orphan_interaction_rows(
        "P", client=_probe_client(_IterBoom({}))) is None


# ── the driver's converged verdict ──────────────────────────────────────────

def _patch_driver_guts(monkeypatch, tmp_path, *, stale_counts, orphans):
    recorded = []

    def _fake_record(repo_root, project_name, counts, resume_cmd, stuck=None):
        recorded.append({"counts": counts, "resume_cmd": resume_cmd})

    resolved = []
    monkeypatch.setattr(cr, "walk_uses_container", lambda p: False)
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_maybe_run_vectorless_audit", lambda *a, **k: None)
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: dict(stale_counts))
    monkeypatch.setattr(cr, "count_orphan_interaction_rows",
                        lambda *a, **k: orphans)
    monkeypatch.setattr(cr, "_record_unconverged_deferral", _fake_record)
    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral",
                        lambda root: resolved.append(root))
    monkeypatch.setattr(cr, "spawn_code_summary_after_walk", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_report_terminal_to_hub", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_collect_terminal_stats", lambda lp: ({}, ""))
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(cr, "_python_exe",
                        SimpleNamespace(resolve_or_current=lambda: sys.executable))
    return recorded, resolved


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_blocks_converged_verdict_on_orphans(monkeypatch, tmp_path, capsys):
    """ACT: owed counts are all zero BUT orphan interactions remain → the
    driver must NOT print 'converged: 0 stale rows'; it records the one-time
    unconverged deferral whose resume command carries --prune-stale."""
    recorded, resolved = _patch_driver_guts(
        monkeypatch, tmp_path, stale_counts={}, orphans=2)
    rc = cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "converged: 0 stale rows" not in out
    assert "NOT converged" in out and "2" in out and "orphan" in out
    assert resolved == [], "no positive convergence → ledger entry NOT cleared"
    assert len(recorded) == 1
    assert recorded[0]["counts"] == {"CodeInteraction_orphans": 2}
    assert recorded[0]["resume_cmd"].endswith("--prune-stale"), (
        "the remedy must be the consented full-walk prune"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots")
def test_driver_still_converges_when_no_orphans(monkeypatch, tmp_path, capsys):
    """LEAVE-ALONE: zero owed AND zero (or undeterminable) orphans → the
    converged verdict and the ledger clear are unchanged."""
    for orphans in (0, None):
        recorded, resolved = _patch_driver_guts(
            monkeypatch, tmp_path, stale_counts={}, orphans=orphans)
        capsys.readouterr()
        rc = cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
        out = capsys.readouterr().out
        assert rc == 0
        assert "converged: 0 stale rows" in out
        assert recorded == [] and resolved == [tmp_path]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
