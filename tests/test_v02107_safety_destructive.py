# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 lane SAFETY — the three delete gates of the destructive audit.

DESTRUCTIVE-02  one count helper; a count that cannot be read is ``None``,
                never 0, and no caller drops / verifies / recovers / stamps
                on ``None``.
DESTRUCTIVE-01  a named-vector slot is added IN PLACE; the staging swap that
                remains drops the live class only after a count the copier did
                not produce matches.
DESTRUCTIVE-03  ``--prune-stale`` deletes nothing after a walk that is no
                evidence (no file / missing root / scoped) and keeps the old
                rows of a half-written file.

Every Weaviate call is faked; nothing here opens a socket.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from vco_lib import collection_rename as cr
from vco_lib import kg_binding_heal as kbh
from vco_lib import project_init as pi
from vco_lib import schema_migration_runner as smr
from vco_lib import weaviate_helpers as wh
from vco_lib import weaviate_schema as ws

URL = "http://127.0.0.1:9"
CLS = "Legacy_CodeFunction"


def _body(obj: Any) -> bytes:
    return json.dumps(obj).encode()


#: HTTP 200, GraphQL ``errors``, the class field null — a resolver error.
ERR_BODY = _body({
    "data": {"Aggregate": {CLS: None}},
    "errors": [{"message": "shard: not enough memory", "path": ["Aggregate", CLS]}],
})


def _count_body(n: int, cls: str = CLS) -> bytes:
    return _body({"data": {"Aggregate": {cls: [{"meta": {"count": n}}]}}})


def _transport(reply):
    """A fake ``http_request``: ``reply(method, url, body) -> (status, bytes)``."""
    calls: list = []

    def send(method, url, *, body=None, timeout=30.0):
        calls.append((method, url, body))
        return reply(method, url, body)

    send.calls = calls  # type: ignore[attr-defined]
    return send


def _patch_transport(monkeypatch, reply):
    send = _transport(reply)
    monkeypatch.setattr(wh, "http_request", send)
    monkeypatch.setattr(pi, "_http_request", send)
    monkeypatch.setattr(ws, "_http_request", send)
    return send


# ═══════════════════════════════════════════════════════════════════════════
# DESTRUCTIVE-02 — the ONE count helper
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status,body", [
    (200, ERR_BODY),                                                # errors + null field
    (200, _body({"data": {"Aggregate": {CLS: None}}})),             # null field
    (200, _body({"data": {"Aggregate": {CLS: []}}})),               # empty list
    (200, _body({"data": {"Aggregate": {}}})),                      # field absent
    (200, _body({"data": None, "errors": [{"message": "no such class"}]})),
    (200, _body({"data": {"Aggregate": {CLS: [{"meta": {"count": 7}}]}},
                 "errors": [{"message": "partial"}]})),             # errors beside a count
    (200, _body({"data": {"Aggregate": {CLS: [{"meta": {"count": None}}]}}})),
    (200, _body({"data": {"Aggregate": {CLS: [{"meta": {"count": True}}]}}})),
    (200, _body({"data": {"Aggregate": {CLS: [{"meta": {"count": -1}}]}}})),
    (200, b"not json"),
    (200, _body([1, 2])),
    (500, _count_body(3)),
    (503, b""),
])
def test_parse_reads_every_unreadable_reply_as_none(status, body):
    assert wh.parse_aggregate_count(status, body, CLS) is None


@pytest.mark.parametrize("n", [0, 1, 391545])
def test_parse_returns_an_integer_count(n):
    assert wh.parse_aggregate_count(200, _count_body(n), CLS) == n


def test_helper_none_on_exception_and_timeout(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(wh, "http_request", boom)
    assert wh.http_count_objects(CLS, URL) is None
    assert wh.http_count_objects(CLS, URL, absent_is_zero=True) is None


def test_helper_refuses_a_class_name_that_is_not_a_class_name():
    sent: list = []
    assert wh.http_count_objects("X { evil }", URL, request=lambda *a, **k: sent.append(a)) is None
    assert sent == []


def test_absent_is_zero_needs_a_positive_404(monkeypatch):
    def reply_with(schema_status):
        def reply(method, url, body):
            if method == "POST":
                return 200, ERR_BODY
            return schema_status, b""
        return reply

    # default: a missing class is "unreadable", never 0
    assert wh.http_count_objects(CLS, URL, request=_transport(reply_with(404))) is None
    # the server positively says the class is not there
    assert wh.http_count_objects(
        CLS, URL, absent_is_zero=True, request=_transport(reply_with(404))) == 0
    # the class EXISTS and its count errored: still unreadable
    assert wh.http_count_objects(
        CLS, URL, absent_is_zero=True, request=_transport(reply_with(200))) is None
    assert wh.http_count_objects(
        CLS, URL, absent_is_zero=True, request=_transport(reply_with(500))) is None


def test_every_entry_point_reads_the_error_body_as_none(monkeypatch):
    """The four former copies and every other count entry point."""
    _patch_transport(monkeypatch, lambda m, u, b: (200, ERR_BODY) if m == "POST" else (200, b"{}"))
    from vco_lib import chunker_revision, embed_slot_fill, embedding_enrichment

    assert wh.http_count_objects(CLS, URL) is None
    assert pi._http_count_objects(CLS, URL) is None
    assert pi._count_objects(CLS, URL) is None
    assert smr._weaviate_class_object_count(URL, CLS) is None
    assert kbh._count_weaviate_class_objects(URL, CLS) is None
    assert chunker_revision._default_kg_counter(CLS) is None
    assert cr.default_ops(URL).count(CLS) is None
    assert pi._snapshot_collection_for_rebuild(CLS, URL)["object_count"] is None
    with pytest.raises(RuntimeError):
        embed_slot_fill.object_count(CLS, URL)
    # the one display-only fallback: a progress estimate, never a gate
    monkeypatch.setattr(embedding_enrichment, "_weaviate_url", lambda: URL)
    assert embedding_enrichment._estimate_object_count(CLS) == 0


def test_every_entry_point_still_reads_a_real_count(monkeypatch):
    _patch_transport(monkeypatch, lambda m, u, b: (200, _count_body(42)))
    assert pi._http_count_objects(CLS, URL) == 42
    assert pi._count_objects(CLS, URL) == 42
    assert smr._weaviate_class_object_count(URL, CLS) == 42
    assert kbh._count_weaviate_class_objects(URL, CLS) == 42
    assert cr.default_ops(URL).count(CLS) == 42


# ── auto-drop of "empty" legacy code-graph classes ─────────────────────────


def _autodrop(monkeypatch, tmp_path, reply):
    _patch_transport(monkeypatch, reply)
    dropped: list = []
    logs: list = []
    monkeypatch.setattr(pi, "_delete_class", lambda n, weaviate_url=None: dropped.append(n))
    monkeypatch.setattr(pi, "_log_auto", logs.append)
    from vco_lib import deferral_emit as de
    monkeypatch.setattr(de, "record_auto_resolution", lambda *a, **k: None)
    cand = [{"class_name": CLS, "suffix": "_CodeFunction",
             "object_count": pi._http_count_objects(CLS, URL),
             "canonical_name": "Proj_CodeFunction", "case_only": False}]
    got, remaining = pi._autodrop_empty_codegraph_candidates(tmp_path, cand, URL)
    return got, remaining, dropped, logs


def test_autodrop_keeps_a_class_whose_count_errored(monkeypatch, tmp_path):
    got, remaining, dropped, _ = _autodrop(monkeypatch, tmp_path, lambda m, u, b: (200, ERR_BODY))
    assert dropped == [] and got == []
    assert [c["class_name"] for c in remaining] == [CLS]


def test_autodrop_refuses_when_the_reprobe_cannot_be_read_and_says_so(monkeypatch, tmp_path):
    """Detect-time said 0; the re-probe before the drop is unreadable."""
    replies = iter([(200, _count_body(0)), (200, ERR_BODY)])
    got, remaining, dropped, logs = _autodrop(
        monkeypatch, tmp_path, lambda m, u, b: next(replies))
    assert dropped == [] and got == [] and len(remaining) == 1
    assert any("could not be read" in line and CLS in line for line in logs)


def test_autodrop_still_drops_a_class_that_answers_zero(monkeypatch, tmp_path):
    got, remaining, dropped, _ = _autodrop(
        monkeypatch, tmp_path, lambda m, u, b: (200, _count_body(0)))
    assert dropped == [CLS] and got == [CLS] and remaining == []


# ── staging recovery ───────────────────────────────────────────────────────


class _EmptyListingClient:
    """v4 client whose listing yields nothing although the class holds rows
    (the A-002 shape) — what the staging classifier used to count with."""

    class collections:  # noqa: N801 - mirror the client attribute
        @staticmethod
        def get(name):
            return SimpleNamespace(iterator=lambda include_vector=False: iter(()))

    def close(self):
        pass


def _staging_world(monkeypatch, counts):
    """``counts``: class -> int, or None for an error-shaped reply."""
    def reply(method, url, body):
        if method == "POST":
            for name, n in counts.items():
                if f"{{ {name} {{" in body["query"]:
                    return (200, _count_body(n, name)) if n is not None else (200, ERR_BODY)
        return 404, b""

    _patch_transport(monkeypatch, reply)
    monkeypatch.setattr(wh, "connect_v4", lambda weaviate_url=None: _EmptyListingClient())
    deleted: list = []
    monkeypatch.setattr(pi, "_fetch_schema", lambda n, weaviate_url=None: {"class": n})
    monkeypatch.setattr(pi, "_delete_class", lambda n, weaviate_url=None: deleted.append(n))
    return deleted


def test_staging_recovery_keeps_a_staging_whose_listing_is_empty_but_count_is_not(monkeypatch):
    """Listing: 0 rows in both. Server count: staging holds 500, name holds 0.
    Counting by listing read "both empty" and dropped the only copy."""
    deleted = _staging_world(monkeypatch, {"K": 0, "K__staging": 500})
    assert pi._recover_or_drop_orphan_staging("K", weaviate_url=URL) == "ambiguous"
    assert deleted == []


def test_staging_recovery_touches_nothing_on_an_unreadable_count(monkeypatch):
    deleted = _staging_world(monkeypatch, {"K": 10, "K__staging": None})
    assert pi._recover_or_drop_orphan_staging("K", weaviate_url=URL) == "ambiguous"
    assert deleted == []


def test_staging_recovery_still_drops_a_proven_orphan(monkeypatch):
    deleted = _staging_world(monkeypatch, {"K": 10, "K__staging": 4})
    assert pi._recover_or_drop_orphan_staging("K", weaviate_url=URL) == "dropped"
    assert deleted == ["K__staging"]


# ── rename verify gate / drop_retired ──────────────────────────────────────


def _retired_ops(monkeypatch, reply):
    _patch_transport(monkeypatch, reply)
    deleted: list = []
    base = cr.default_ops(URL)
    ops = cr.WeaviateOps(
        probe=lambda names: {n: True for n in names}, count=base.count,
        create_class=lambda p: None, copy_class=lambda s, d: 0,
        sample_vectors=lambda n, k: {}, fetch_vectors=lambda n, u: {},
        delete_class=deleted.append, remint_identity=lambda *a, **k: {},
    )
    monkeypatch.setattr(cr, "_bound_names", lambda **k: (set(), set()))
    return ops, deleted


_RECORD = {"retired_classes": ["Old_KnowledgeGraph"],
           "replacements": {"Old_KnowledgeGraph": "Proj_KnowledgeGraph"}}


def test_drop_retired_refuses_when_the_counts_are_error_replies(monkeypatch, tmp_path):
    ops, deleted = _retired_ops(monkeypatch, lambda m, u, b: (200, ERR_BODY))
    out = cr.drop_retired(tmp_path, confirm=True, ops=ops, record=_RECORD)
    assert deleted == [] and out["dropped"] == []
    assert any("could not be read" in r for r in out["refused"])


def test_drop_retired_drops_when_the_replacement_provably_holds_the_data(monkeypatch, tmp_path):
    def reply(method, url, body):
        name = "Old_KnowledgeGraph" if "Old_KnowledgeGraph" in body["query"] else "Proj_KnowledgeGraph"
        return 200, _count_body(9, name)

    ops, deleted = _retired_ops(monkeypatch, reply)
    monkeypatch.setattr("vco_lib.deferral_emit.resolve_conditions", lambda *a, **k: None)
    out = cr.drop_retired(tmp_path, confirm=True, ops=ops, record=_RECORD)
    assert deleted == ["Old_KnowledgeGraph"] and out["dropped"] == ["Old_KnowledgeGraph"]


# ── migration runner: the "born fresh" stamp ───────────────────────────────


def test_has_rows_is_unknown_on_an_error_reply(monkeypatch):
    """An error reply must NOT read "definite 0" (False = stamp born-fresh)."""
    _patch_transport(monkeypatch, lambda m, u, b: (200, ERR_BODY) if m == "POST" else (200, b"{}"))
    assert smr._codegraph_collection_has_rows(URL, [CLS]) is None


def test_has_rows_false_for_an_empty_and_for_an_absent_class(monkeypatch):
    _patch_transport(monkeypatch, lambda m, u, b: (200, _count_body(0)))
    assert smr._codegraph_collection_has_rows(URL, [CLS]) is False
    # absent: the Aggregate errors, the schema endpoint says 404
    _patch_transport(monkeypatch, lambda m, u, b: (200, ERR_BODY) if m == "POST" else (404, b""))
    assert smr._codegraph_collection_has_rows(URL, [CLS]) is False


def test_has_rows_true_for_a_populated_class(monkeypatch):
    _patch_transport(monkeypatch, lambda m, u, b: (200, _count_body(3)))
    assert smr._codegraph_collection_has_rows(URL, [CLS]) is True


# ═══════════════════════════════════════════════════════════════════════════
# DESTRUCTIVE-01 — slot add in place; the guarded staging swap
# ═══════════════════════════════════════════════════════════════════════════


class _Server:
    """A fake Weaviate REST surface: schema GET/PUT + meta, recording calls."""

    def __init__(self, classes, *, version="1.37.18", put_status=200):
        self.classes = classes          # name -> schema dict
        self.version = version
        self.put_status = put_status
        self.calls: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):
        self.calls.append((method, url))
        tail = url.split("/v1/", 1)[1]
        if tail == "meta":
            return 200, _body({"version": self.version})
        name = tail.split("/", 1)[1]
        if method == "GET":
            if name in self.classes:
                return 200, _body(self.classes[name])
            return 404, b""
        if method == "PUT":
            if self.put_status == 200:
                self.classes[name] = body
                return 200, _body(body)
            return self.put_status, b'{"error":[{"message":"vector config is immutable"}]}'
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [m for m, _ in self.calls]


def _slot_server(monkeypatch, **kw):
    srv = _Server({"Proj_CodeFunction": {
        "class": "Proj_CodeFunction", "properties": [{"name": "name"}],
        "vectorConfig": {"codesage_embed": {"vectorizer": {"none": {}}}}}}, **kw)
    monkeypatch.setattr(ws, "_http_request", srv)
    monkeypatch.setattr(wh, "http_request", srv)
    monkeypatch.setattr(ws, "_existing_slot_dim", lambda *a, **k: None)
    dropped: list = []
    monkeypatch.setattr(pi, "_delete_class", lambda n, weaviate_url=None: dropped.append(n))
    monkeypatch.setattr(pi, "_create_class", lambda d, weaviate_url=None: dropped.append(("create", d)))
    monkeypatch.setattr(pi, "_copy_collection_with_vectors",
                        lambda *a, **k: dropped.append("copy") or 0)
    return srv, dropped


def test_slot_is_added_in_place_and_nothing_is_dropped_created_or_copied(monkeypatch):
    srv, touched = _slot_server(monkeypatch)
    out = ws.add_named_vector_slot(
        "Proj_CodeFunction", ws.NamedVectorSlot("qwen3_embed", 1024), weaviate_url=URL)
    assert out == ws.AddSlotResult.Created
    assert touched == []                                   # no drop / create / copy
    assert set(srv.methods()) == {"GET", "PUT"}            # never DELETE / POST
    vc = srv.classes["Proj_CodeFunction"]["vectorConfig"]
    assert set(vc) == {"codesage_embed", "qwen3_embed"}
    assert vc["codesage_embed"] == {"vectorizer": {"none": {}}}   # sent back verbatim
    assert srv.classes["Proj_CodeFunction"]["properties"] == [{"name": "name"}]


def test_second_add_is_a_no_op(monkeypatch):
    srv, _ = _slot_server(monkeypatch)
    slot = ws.NamedVectorSlot("qwen3_embed", 1024)
    assert ws.add_named_vector_slot("Proj_CodeFunction", slot, weaviate_url=URL) == ws.AddSlotResult.Created
    puts = srv.methods().count("PUT")
    assert ws.add_named_vector_slot("Proj_CodeFunction", slot, weaviate_url=URL) == ws.AddSlotResult.Skipped
    assert srv.methods().count("PUT") == puts


def test_old_server_gets_update_weaviate_and_never_a_drop(monkeypatch):
    srv, touched = _slot_server(monkeypatch, version="1.28.4", put_status=422)
    with pytest.raises(RuntimeError) as err:
        ws.add_named_vector_slot(
            "Proj_CodeFunction", ws.NamedVectorSlot("qwen3_embed", 1024), weaviate_url=URL)
    msg = str(err.value)
    assert "Update Weaviate" in msg and "1.28.4" in msg and "Nothing was dropped" in msg
    assert touched == [] and "DELETE" not in srv.methods()
    assert set(srv.classes["Proj_CodeFunction"]["vectorConfig"]) == {"codesage_embed"}


def test_single_vector_class_is_refused_not_rebuilt(monkeypatch):
    srv, touched = _slot_server(monkeypatch)
    srv.classes["Proj_CodeFunction"].pop("vectorConfig")
    with pytest.raises(ws.SlotAddUnsupported):
        ws.add_vector_slots_in_place(
            "Proj_CodeFunction", [ws.NamedVectorSlot("qwen3_embed", 1024)], weaviate_url=URL)
    assert touched == [] and "PUT" not in srv.methods()


def test_ensure_slot_reports_the_refusal_as_an_error_string(monkeypatch):
    """The model-switch / self-heal caller (`embed_slot_fill.ensure_slot`)."""
    from vco_lib import embed_slot_fill

    _, touched = _slot_server(monkeypatch, version="1.28.4", put_status=422)
    err = embed_slot_fill.ensure_slot(
        ["Proj_CodeFunction"], "qwen3_embed", 1024, [ws.NamedVectorSlot("qwen3_embed", 1024)],
        model="qwen3-embedding:0.6b", add=True, weaviate_url=URL,
        declared=lambda n, u: {"codesage_embed"})
    assert err and "Update Weaviate" in err and touched == []


def test_migrate_collection_to_target_records_the_refusal_per_slot(monkeypatch):
    """Install step 7d / `migrate-collections` caller."""
    _, touched = _slot_server(monkeypatch, version="1.28.4", put_status=422)
    report = ws.migrate_collection_to_target(
        "Proj_CodeFunction", [ws.NamedVectorSlot("codesage_embed", 2048),
                              ws.NamedVectorSlot("qwen3_embed", 1024)], weaviate_url=URL)
    assert report.skipped_slots == ["codesage_embed"] and report.added_slots == []
    assert [e["slot"] for e in report.errors] == ["qwen3_embed"]
    assert "Update Weaviate" in report.errors[0]["reason"] and touched == []


def test_the_drop_and_copy_rebuilder_is_gone():
    assert not hasattr(ws, "_default_collection_rebuilder")


def test_a_missing_slot_plans_add_slots_and_an_immutable_setting_plans_copy():
    assert pi._classify_action(pi.SchemaDelta(missing_vec_slots=["qwen3_embed"])) == "add_slots"
    assert pi._classify_action(pi.SchemaDelta(
        missing_vec_slots=["qwen3_embed"], missing_props=[{"name": "p"}])) == "add_slots"
    assert pi._classify_action(pi.SchemaDelta(
        missing_vec_slots=["qwen3_embed"], indexNullState_needed=True)) == "copy"
    assert pi._classify_action(pi.SchemaDelta(vector_index_type_change="hfresh")) == "copy"
    assert pi._classify_action(pi.SchemaDelta(missing_props=[{"name": "p"}])) == "patch_props"
    from vco_lib.migration_plan_classify import ADDITIVE_ACTIONS, LOSSY_ACTIONS

    assert "add_slots" in ADDITIVE_ACTIONS and "add_slots" not in LOSSY_ACTIONS


def test_migrate_collections_applies_add_slots_in_place(monkeypatch):
    """The bundle-update auto-apply path: `migrate_collections` with a plan
    whose only delta is a missing slot does one PUT and no drop."""
    srv, touched = _slot_server(monkeypatch)
    target = {"class": "Proj_CodeFunction", "properties": [{"name": "name"}],
              "vectorConfig": {"codesage_embed": {"vectorizer": {"none": {}}},
                               "qwen3_embed": {"vectorizer": {"none": {}}, "marker": 1}}}
    monkeypatch.setattr(pi, "_build_plan", lambda *a, **k: [{
        "env_key": None, "collection": "Proj_CodeFunction", "action": "add_slots",
        "target": target, "delta": pi.SchemaDelta(missing_vec_slots=["qwen3_embed"])}])
    monkeypatch.setattr(pi, "_list_classes", lambda weaviate_url=None: [])
    result = pi.migrate_collections(SimpleNamespace(force_rebuild=False), weaviate_url=URL)
    assert result["errors"] == []
    assert [e["action"] for e in result["plan"]] == ["add_slots"]
    assert touched == [] and "DELETE" not in srv.methods()
    assert srv.classes["Proj_CodeFunction"]["vectorConfig"]["qwen3_embed"]["marker"] == 1


# ── the guarded staging swap (what remains of `copy`) ──────────────────────


class _Store:
    """In-memory classes for `_swap_through_staging`."""

    def __init__(self, rows, *, broken_listing=(), count_override=None):
        self.rows = rows                       # name -> list of ids
        self.broken = set(broken_listing)      # names whose LISTING yields nothing
        self.count_override = count_override or {}
        self.deleted: list = []
        self.created: list = []

    def wire(self, monkeypatch):
        monkeypatch.setattr(pi, "_fetch_schema",
                            lambda n, weaviate_url=None: {"class": n} if n in self.rows else None)
        monkeypatch.setattr(pi, "_create_class", self._create)
        monkeypatch.setattr(pi, "_delete_class", self._delete)
        monkeypatch.setattr(pi, "_copy_collection_with_vectors", self._copy)
        monkeypatch.setattr(pi, "_count_objects", self._count)
        return self

    def _create(self, d, weaviate_url=None):
        self.created.append(d["class"])
        self.rows.setdefault(d["class"], [])

    def _delete(self, n, weaviate_url=None):
        self.deleted.append(n)
        self.rows.pop(n, None)

    def _copy(self, src, dst, *, weaviate_url=None, batch_size=200):
        listed = [] if src in self.broken else list(self.rows[src])
        self.rows[dst].extend(listed)
        return len(listed)

    def _count(self, n, weaviate_url=None):
        if n in self.count_override:
            return self.count_override[n]
        return len(self.rows[n]) if n in self.rows else None


NAME, STG = "Proj_KnowledgeGraph", "Proj_KnowledgeGraph__staging"
TARGET = {"class": NAME, "vectorConfig": {"qwen3_embed": {}}, "properties": []}


def test_swap_keeps_the_class_when_the_listing_yields_nothing(monkeypatch):
    """A-002: 500 rows stored, the listing returns 0 and does not raise."""
    st = _Store({NAME: [f"u{i}" for i in range(500)]}, broken_listing={NAME}).wire(monkeypatch)
    with pytest.raises(RuntimeError, match="NOT dropped"):
        pi._swap_through_staging(NAME, TARGET, weaviate_url=URL)
    assert len(st.rows[NAME]) == 500
    assert st.deleted == [STG]                 # only this run's own partial staging


def test_swap_refuses_before_creating_anything_when_the_count_is_unreadable(monkeypatch):
    st = _Store({NAME: ["a", "b"]}, count_override={NAME: None}).wire(monkeypatch)
    with pytest.raises(RuntimeError, match="could not be read"):
        pi._swap_through_staging(NAME, TARGET, weaviate_url=URL)
    assert st.created == [] and st.deleted == [] and st.rows[NAME] == ["a", "b"]


def test_swap_keeps_the_class_when_staging_cannot_be_counted(monkeypatch):
    st = _Store({NAME: ["a", "b"]}, count_override={STG: None}).wire(monkeypatch)
    with pytest.raises(RuntimeError, match="NOT dropped"):
        pi._swap_through_staging(NAME, TARGET, weaviate_url=URL)
    assert st.rows[NAME] == ["a", "b"] and NAME not in st.deleted


def test_swap_never_touches_a_staging_it_did_not_create(monkeypatch):
    st = _Store({NAME: ["a"], STG: ["only-copy-1", "only-copy-2"]}).wire(monkeypatch)
    with pytest.raises(RuntimeError, match="did not create"):
        pi._swap_through_staging(NAME, TARGET, weaviate_url=URL)
    assert st.deleted == [] and st.rows[STG] == ["only-copy-1", "only-copy-2"]
    assert st.rows[NAME] == ["a"]


def test_healthy_swap_round_trips_and_drops_staging_last(monkeypatch):
    st = _Store({NAME: [f"u{i}" for i in range(500)]}).wire(monkeypatch)
    assert pi._swap_through_staging(NAME, TARGET, weaviate_url=URL) == 500
    assert len(st.rows[NAME]) == 500 and STG not in st.rows
    assert st.deleted == [NAME, STG]           # live class first only AFTER the proof


def test_swap_retains_staging_when_the_copy_back_falls_short(monkeypatch):
    st = _Store({NAME: ["a", "b", "c"]}, broken_listing={STG}).wire(monkeypatch)
    with pytest.raises(RuntimeError, match="RETAINED"):
        pi._swap_through_staging(NAME, TARGET, weaviate_url=URL)
    assert st.rows[STG] == ["a", "b", "c"] and STG not in st.deleted


# ═══════════════════════════════════════════════════════════════════════════
# DESTRUCTIVE-03 — --prune-stale
# ═══════════════════════════════════════════════════════════════════════════

_ANALYZER_PATH = Path(__file__).resolve().parent.parent / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_v02107_safety_analyzer", str(_ANALYZER_PATH))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Coll:
    def __init__(self, name: str, rows: List[Any], props: List[str]) -> None:
        self.name, self._rows, self.deleted = name, rows, []
        self.data = SimpleNamespace(delete_by_id=lambda uuid: self.deleted.append(uuid))
        self.config = SimpleNamespace(
            get=lambda: SimpleNamespace(properties=[SimpleNamespace(name=p) for p in props]))

    def iterator(self, return_properties=None):
        return iter(self._rows)


def _row(uid, anchor_prop, anchor, source=None):
    props = {"project": "TestProject", anchor_prop: anchor}
    if source is not None:
        props["project_source"] = source
    return SimpleNamespace(uuid=uid, properties=props)


def _analyzer(analyzer_mod, *, with_source=False, src=""):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "TestProject"
    inst.visited_uuids = set()
    inst._track_visited = True
    inst._prune_language = ""
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    s = (lambda p: src) if with_source else (lambda p: None)
    cols = ["project", "path"] + (["project_source"] if with_source else [])
    fcols = ["project", "file_path"] + (["project_source"] if with_source else [])
    inst.modules_collection = _Coll("Test_CodeModule", [
        _row("m-a", "path", "src/a.py", s("a")), _row("m-b", "path", "src/b.py", s("b"))], cols)
    inst.functions_collection = _Coll("Test_CodeFunction", [
        _row("f-a1", "file_path", "src/a.py", s("a")), _row("f-a2", "file_path", "src/a.py", s("a")),
        _row("f-b1", "file_path", "src/b.py", s("b"))], fcols)
    inst.classes_collection = inst.apis_collection = inst.interactions_collection = None
    return inst


def _deleted(an):
    return sorted(an.modules_collection.deleted + an.functions_collection.deleted)


def test_a_walk_that_discovered_no_file_deletes_nothing(analyzer_mod, capsys):
    """Root exists but is empty (unmounted drive, wrong checkout)."""
    an = _analyzer(analyzer_mod)
    assert an._prune_stale_objects() == 0
    assert _deleted(an) == []
    assert "discovered no file" in capsys.readouterr().err


def test_a_walk_whose_root_is_missing_deletes_nothing(analyzer_mod, tmp_path, capsys, monkeypatch):
    import vco_lib.codegraph_source_scope as scope
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    an = _analyzer(analyzer_mod)
    an._analyze_repo_root_raw = (tmp_path / "gone").as_posix()
    an._prune_discovered_paths = {"src/a.py"}      # even with a stale discovery set
    assert an._prune_stale_objects() == 0
    assert _deleted(an) == []
    assert "missing" in capsys.readouterr().err


def test_a_scoped_walk_deletes_nothing(analyzer_mod, capsys):
    an = _analyzer(analyzer_mod)
    an._analyze_whole_repo = False                 # --only-file / --only-files-from
    an._prune_discovered_paths = {"src/a.py"}
    an._prune_walked_paths = {"src/a.py"}
    an.visited_uuids = {("Test_CodeModule", "m-a")}
    assert an._prune_stale_objects() == 0
    assert _deleted(an) == []
    assert "scoped" in capsys.readouterr().err


def test_a_full_walk_still_prunes_what_is_gone(analyzer_mod):
    """ACT: a.py re-walked completely (its two old functions are gone from the
    file), b.py deleted from disk -> every unvisited row goes."""
    an = _analyzer(analyzer_mod)
    an._prune_discovered_paths = {"src/a.py"}
    an._prune_walked_paths = {"src/a.py"}
    an.visited_uuids = {("Test_CodeModule", "m-a")}
    assert an._prune_stale_objects() == 4
    assert _deleted(an) == ["f-a1", "f-a2", "f-b1", "m-b"]


def _half_written(analyzer_mod, tmp_path, committed):
    root = tmp_path.as_posix()
    an = _analyzer(analyzer_mod, with_source=True, src=root)
    an._analyze_repo_root_raw = root
    an._prune_discovered_paths = {(root, "src/a.py"), (root, "src/b.py")}
    an._prune_walked_paths = {(root, "src/a.py")}          # b.py unchanged-skipped
    an.visited_uuids = {("Test_CodeModule", "m-a")}        # only the module write landed
    an._reconcile_walked = committed
    import vco_lib.codegraph_source_scope as scope
    return an, scope, root


def test_old_rows_of_a_half_written_file_survive(analyzer_mod, tmp_path, monkeypatch):
    """a.py: module row written (file marked walked), then the entity inserts
    failed, so its reconcile scope never committed. f-a1/f-a2 describe code
    that is still on disk."""
    an, scope, _ = _half_written(analyzer_mod, tmp_path, committed={})
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    assert an._prune_stale_objects() == 0
    assert _deleted(an) == []
    assert an._prune_guard.incomplete == {(tmp_path.as_posix(), "src/a.py")}


def test_stale_rows_of_a_completely_written_file_are_pruned(analyzer_mod, tmp_path, monkeypatch):
    """ACT twin: the same walk, but a.py's write committed -> its two old
    function rows really are stale."""
    root = tmp_path.as_posix()
    an, scope, _ = _half_written(
        analyzer_mod, tmp_path, committed={(root, "src/a.py"): {"Test_CodeModule": {"m-a"}}})
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    assert an._prune_stale_objects() == 2
    assert _deleted(an) == ["f-a1", "f-a2"]


def test_an_extra_root_that_yielded_no_file_keeps_its_rows(analyzer_mod, tmp_path, monkeypatch):
    """Primary walked fully; the extra folder is an empty mount point. Its
    rows stay, the primary's stale row goes."""
    primary, extra = tmp_path / "proj", tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    p, e = primary.as_posix(), extra.as_posix()
    an = _analyzer(analyzer_mod, with_source=True, src=p)
    an.functions_collection._rows.append(_row("x-1", "file_path", "lib/x.py", e))
    an._analyze_repo_root_raw = p
    an._walked_extra_roots = {e}
    an._prune_discovered_paths = {(p, "src/a.py")}
    an._prune_walked_paths = {(p, "src/a.py")}
    an._reconcile_walked = {(p, "src/a.py"): {}}
    an.visited_uuids = {("Test_CodeModule", "m-a"), ("Test_CodeFunction", "f-a1"),
                        ("Test_CodeFunction", "f-a2")}
    import vco_lib.codegraph_source_scope as scope
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: True)
    an._prune_stale_objects()
    assert _deleted(an) == ["f-b1", "m-b"]             # b.py is gone from the primary
    assert "x-1" not in an.functions_collection.deleted
    assert an._prune_guard.silent_roots == [e]


def test_no_file_under_the_primary_root_refuses_the_whole_prune(analyzer_mod, tmp_path):
    primary, extra = tmp_path / "proj", tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    p, e = primary.as_posix(), extra.as_posix()
    an = _analyzer(analyzer_mod, with_source=True, src=p)
    an._analyze_repo_root_raw = p
    an._walked_extra_roots = {e}
    an._prune_discovered_paths = {(e, "lib/x.py")}
    an._prune_walked_paths = {(e, "lib/x.py")}
    assert an._prune_stale_objects() == 0
    assert _deleted(an) == []


# ═══════════════════════════════════════════════════════════════════════════
# DESTRUCTIVE-03(a) — the end-of-walk deleted-file sweep (runs WITHOUT
# --prune-stale) must not read an empty / missing root as "every file deleted"
# ═══════════════════════════════════════════════════════════════════════════

from vco_lib import codegraph_row_classify as crc  # noqa: E402


def _sweep_rows():
    return _Coll("Test_CodeFunction", [
        _row("f-a", "file_path", "src/a.py"),
        _row("f-b", "file_path", "src/b.py"),
        _row("f-c", "file_path", "pkg/c.py"),
    ], ["project", "file_path", "embed_revision"])


def _sweep(analyzer_mod, root):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "TestProject"
    inst._strict_source = False
    coll = _sweep_rows()
    deleted, failures = inst._clear_deleted_primary_rows(coll, "file_path", root, set())
    return coll, deleted, failures


@pytest.mark.parametrize("kind", ["empty", "missing", "file"])
def test_the_sweep_deletes_nothing_under_an_empty_or_missing_root(analyzer_mod, tmp_path, kind):
    root = tmp_path / "mountpoint"
    if kind == "empty":
        root.mkdir()
    elif kind == "file":
        root.write_text("not a directory\n")
    coll, deleted, failures = _sweep(analyzer_mod, root)
    assert (deleted, failures, coll.deleted) == (0, 0, [])


@pytest.mark.parametrize("kind", ["empty", "missing"])
def test_the_predicates_keep_every_row_under_an_empty_or_missing_root(tmp_path, kind):
    root = tmp_path / "mountpoint"
    if kind == "empty":
        root.mkdir()
    assert crc.path_reachable_on_disk("src/a.py", root) is True
    assert crc.is_deleted_primary_row({"file_path": "src/a.py"}, root) is False
    assert crc.classify_row(
        {"file_path": "src/a.py", "embed_revision": 0}, root, path_prop="file_path",
        current_revision=999) != "purgeable"


def test_the_sweep_still_deletes_exactly_the_file_that_is_gone(analyzer_mod, tmp_path):
    """Leave-alone twin: a real root where one file was deleted."""
    root = tmp_path / "repo"
    for rel in ("src/a.py", "pkg/c.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x = 1\n")
    coll, deleted, failures = _sweep(analyzer_mod, root)
    assert (deleted, failures, coll.deleted) == (1, 0, ["f-b"])
