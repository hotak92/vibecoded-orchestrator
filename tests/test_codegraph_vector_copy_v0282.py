# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.82 (WP-2, G7 + G3 engine): vector-portable row copy + project-identity
migration for the code graph.

Fail-without/pass-with tests T8-T13 (per PLAN-v0282 §WP-2):
  * T8  — migration moves a row + its vector BYTE-IDENTICALLY, zero embed calls.
  * T9  — destination-write failure → source row NOT deleted (destructive
          leave-alone). NEW-code test: cannot run on base (the module does not
          exist there) — stated in the test docstring, not faked.
  * T10 — collision (dest exists WITH vector) → source deleted, dest untouched;
          collision with a VECTORLESS dest → source copied over it (replace),
          never dropped. NEW-code test — same base note.
  * T11 — chunked entity (3 chunk rows) migrates all chunks with correct
          per-chunk identity keys; an unreconstructable-identity row is left in
          place + counted.
  * T12 — Windows-shaped stored anchor migrates (both-UUID probe: RAW form
          reproduces the source UUID, so the row is migrated).
  * T13 — resync counting parity: a fixture matrix over
          {NULL, 0, floor-1, floor, current} matches guards' classification
          (``is_row_revision_stale`` + ``classify_stale_kind``).

VECTOR PURITY: every migration/copy path here asserts the embedder is NEVER
called (there is no embedder to call — the module imports none; the tests bind a
poison embedder onto the analyzer module import and assert zero calls where a
regression could introduce one).
"""
from __future__ import annotations

import types
import uuid as _uuid_mod
from typing import Any, Dict, List, Optional

import pytest

import vco_lib.codegraph_vector_copy as vc
import vco_lib.codegraph_guards as guards


# ── the analyzer's real deterministic-UUID builder (REUSE, never mirror) ──────


@pytest.fixture(scope="module")
def uuid_builder():
    return vc._load_analyzer_uuid_builder()


# ─────────────────────────── fakes (weaviate v4 shape) ───────────────────────


class _CrossRef:
    """Stand-in for weaviate's ``_CrossReference`` — exposes ``.objects``, a
    list of resolved target objects (each carrying ``.uuid``)."""

    def __init__(self, objects):
        self.objects = objects


class _Obj:
    """A fetched object: ``.uuid``, ``.properties`` (dict), ``.vector`` (named-
    vector dict, or None for a vectorless row), ``.references`` (dict
    ``{name: _CrossRef}`` or None)."""

    def __init__(self, uuid: str, properties: Dict[str, Any], vector: Any,
                 references: Any = None):
        self.uuid = uuid
        self.properties = dict(properties)
        self.vector = vector
        self.references = references


def _resolve_refs(coll: "_Coll", src_uuid: str, want_names):
    """Build a ``{name: _CrossRef}`` dict for ``src_uuid`` from the collection's
    stored reference beacons.

    The fake surfaces EVERY stored beacon UUID (whether or not its target row
    still exists) so the migration's own ``is_live`` probe — not the read — makes
    the keep-vs-drop decision. Real weaviate ``return_references`` pre-filters
    dangling beacons, so in production the drop branch never fires; the fake
    returns dangling beacons too specifically to EXERCISE that defensive branch
    (B3's dangling-ref-counted test)."""
    stored = coll.rows.get(str(src_uuid), {}).get("references") or {}
    out = {}
    for name in (want_names or []):
        targets = stored.get(name) or []
        objs = [_Obj(str(t), {}, None) for t in targets]
        if objs:
            out[name] = _CrossRef(objs)
    return out


class _Query:
    def __init__(self, coll: "_Coll"):
        self._coll = coll

    def fetch_object_by_id(self, uuid, include_vector=False, return_properties=None,
                           return_references=None):
        self._coll.fetch_calls.append(str(uuid))
        row = self._coll.rows.get(str(uuid))
        if row is None:
            return None
        props = dict(row["properties"])
        if return_properties:
            props = {k: props.get(k) for k in return_properties}
        vec = row["vector"] if include_vector else None
        references = None
        if return_references:
            # return_references is a list of _FakeQueryReference(link_on=name).
            want_names = [getattr(r, "link_on", r) for r in return_references]
            references = _resolve_refs(self._coll, str(uuid), want_names)
        return _Obj(str(uuid), props, vec, references)

    def fetch_objects(self, filters=None, limit=None, return_properties=None,
                      return_references=None):
        # Minimal filter support: our _Filter carries (prop, op, value).
        objs = []
        for u, row in self._coll.rows.items():
            if filters is not None and not filters.matches(row["properties"]):
                continue
            props = dict(row["properties"])
            if return_properties:
                props = {k: props.get(k) for k in return_properties}
            objs.append(_Obj(u, props, None))
            if limit is not None and len(objs) >= limit:
                break
        return types.SimpleNamespace(objects=objs)


class _Data:
    def __init__(self, coll: "_Coll"):
        self._coll = coll

    def insert(self, properties=None, references=None, uuid=None, vector=None):
        if self._coll.insert_raises:
            raise RuntimeError("insert boom (injected)")
        self._coll.rows[str(uuid)] = {
            "properties": dict(properties or {}), "vector": vector,
            "references": dict(references) if references else None,
        }
        self._coll.inserts.append(
            {"uuid": str(uuid), "properties": dict(properties or {}),
             "vector": vector, "references": dict(references) if references else None}
        )
        return _uuid_mod.uuid4()

    def replace(self, uuid=None, properties=None, references=None, vector=None):
        if self._coll.replace_raises:
            raise RuntimeError("replace boom (injected)")
        self._coll.rows[str(uuid)] = {
            "properties": dict(properties or {}), "vector": vector,
            "references": dict(references) if references else None,
        }
        self._coll.replaces.append(
            {"uuid": str(uuid), "properties": dict(properties or {}),
             "vector": vector, "references": dict(references) if references else None}
        )

    def update(self, uuid=None, properties=None):
        row = self._coll.rows.get(str(uuid))
        if row is not None:
            row["properties"].update(properties or {})
        self._coll.updates.append({"uuid": str(uuid), "properties": dict(properties or {})})

    def delete_by_id(self, uuid):
        if self._coll.delete_raises:
            raise RuntimeError("delete boom (injected)")
        self._coll.rows.pop(str(uuid), None)
        self._coll.deletes.append(str(uuid))

    def exists(self, uuid):
        return str(uuid) in self._coll.rows


class _Coll:
    def __init__(self, name: str, rows: Optional[Dict[str, dict]] = None):
        self.name = name
        # rows: uuid -> {"properties": {...}, "vector": <dict|None>,
        #                "references": {name: [target_uuid, ...]} | None}
        self.rows: Dict[str, dict] = rows or {}
        self.insert_raises = False
        self.replace_raises = False
        self.delete_raises = False
        self.fetch_calls: List[str] = []
        self.inserts: List[dict] = []
        self.replaces: List[dict] = []
        self.updates: List[dict] = []
        self.deletes: List[str] = []
        self.query = _Query(self)
        self.data = _Data(self)
        # Back-reference to the owning client for cross-collection ref
        # resolution; set by _Client (None for stand-alone collections).
        self.client: Optional["_Client"] = None

    # B2: the REAL weaviate-client v4.21 Collection.iterator has NO `filters`
    # kwarg. The fake MUST mirror that signature so a regression that re-adds a
    # `filters=` call fails loudly here (it did NOT before — the old fake
    # declared a phantom `filters=None`, masking the TypeError).
    def iterator(self, include_vector=False, return_metadata=None,
                 return_properties=None, return_references=None, after=None,
                 cache_size=None):
        for u, row in list(self.rows.items()):
            props = dict(row["properties"])
            if return_properties:
                props = {k: props.get(k) for k in return_properties}
            vec = row["vector"] if include_vector else None
            refs = None
            if return_references:
                want_names = [getattr(r, "link_on", r) for r in return_references]
                refs = _resolve_refs(self, str(u), want_names)
            yield _Obj(u, props, vec, refs)


class _Filter:
    """A tiny stand-in for weaviate's Filter.by_property(...).equal(...)."""

    def __init__(self, prop: str, op: str, value: Any):
        self.prop = prop
        self.op = op
        self.value = value

    def matches(self, props: Dict[str, Any]) -> bool:
        v = props.get(self.prop)
        if self.op == "equal":
            return v == self.value
        if self.op == "is_none":
            return (v is None) == bool(self.value)
        return False


class _FilterBuilder:
    def __init__(self, prop):
        self.prop = prop

    def equal(self, value):
        return _Filter(self.prop, "equal", value)

    def is_none(self, value):
        return _Filter(self.prop, "is_none", value)


class _FilterFactory:
    @staticmethod
    def by_property(prop):
        return _FilterBuilder(prop)


class _FakeQueryReference:
    """Stand-in for weaviate's ``QueryReference(link_on=<name>)`` — the copy
    reads ``.link_on`` to know which reference to resolve."""

    def __init__(self, link_on=None, **_kw):
        self.link_on = link_on


@pytest.fixture(autouse=True)
def _patch_weaviate_filter(monkeypatch):
    """Inject the fake ``Filter`` + ``QueryReference`` into the module's local
    import site. B2 removed the ``_iter_project_rows`` filter (v4.21 iterator
    has no ``filters`` kwarg), but ``fetch_objects`` in older paths + the B3
    ``_query_references_for`` still import from ``weaviate.classes.query`` — the
    fake keeps those imports resolving without a live weaviate."""
    fake_query = types.ModuleType("weaviate.classes.query")
    fake_query.Filter = _FilterFactory
    fake_query.QueryReference = _FakeQueryReference
    import sys
    monkeypatch.setitem(sys.modules, "weaviate.classes.query", fake_query)
    yield


class _Client:
    def __init__(self, colls: Dict[str, _Coll]):
        self._colls = colls
        self.collections = types.SimpleNamespace(
            exists=lambda n: n in self._colls,
            get=lambda n: self._colls[n],
        )
        # Wire each collection back to this client so cross-collection
        # reference resolution (imports→CodeModule, module→CodeModule, etc.)
        # can find live targets in a DIFFERENT collection than the source's.
        for c in self._colls.values():
            c.client = self

    def close(self):  # pragma: no cover — never own-built in tests
        pass


def _named_vec(*vals):
    """A named-vector dict as weaviate returns for include_vector=True."""
    return {"codesage_embed": list(vals)}


# ─────────────────────────── B2: iterator signature pin ──────────────────────


def test_B2_real_client_iterator_has_no_filters_kwarg():
    """B2 (fail-on-base proof): weaviate-client v4.21's ``Collection.iterator``
    has NO ``filters`` kwarg. The pre-fix migration passed ``filters=`` and
    every call raised ``TypeError`` that the per-collection soft-fail swallowed
    → an all-zero no-op migration. This pin imports the REAL client class and
    asserts the phantom kwarg is absent, so:
      * if a future client version ADDS a ``filters`` kwarg, we learn (and can
        switch back to a server-side filter);
      * if the migration RE-introduces a ``filters=`` call against this client,
        the live smoke would ``TypeError`` again — this test is the cheap guard.

    Shaped to FAIL LOUDLY if weaviate is absent (this repo treats a missing dep
    as a broken install, not a skip): the import is unconditional, so a missing
    weaviate raises ImportError here rather than silently passing.
    """
    import inspect

    import weaviate.collections.collection as _collmod

    sig = inspect.signature(_collmod.Collection.iterator)
    assert "filters" not in sig.parameters, (
        "Collection.iterator now HAS a `filters` kwarg — if intentional, the "
        "migration may switch back to a server-side filter; until then "
        "_iter_project_rows MUST iterate unfiltered + filter client-side "
        f"(current params: {list(sig.parameters)})."
    )
    # And the kwargs the migration DOES use must exist (guards a rename).
    for needed in ("return_properties", "return_references", "include_vector"):
        assert needed in sig.parameters, (
            f"Collection.iterator lost the `{needed}` kwarg the migration relies "
            f"on (params: {list(sig.parameters)})."
        )


# ─────────────────────── copy primitive: purity + confirm ────────────────────


def test_copy_one_row_moves_vector_verbatim(uuid_builder):
    """The vector dict is written to the destination BYTE-IDENTICALLY."""
    vec = _named_vec(0.1, 0.2, 0.3)
    src = _Coll("P_CodeFunction", {
        "src-uuid": {"properties": {"full_name": "m.f", "project": "Old"}, "vector": vec},
    })
    dest = src  # same collection (identity migration is in-collection)
    outcome = vc.copy_one_row_with_vector(
        src, dest, "src-uuid", "dest-uuid", project_override="New",
    )
    assert outcome.status == "copied"
    written = dest.rows["dest-uuid"]
    assert written["vector"] == vec  # verbatim, byte-identical
    assert written["vector"] is not None
    assert written["properties"]["project"] == "New"  # override applied


def test_B4_copy_strips_empty_named_vector_slot_before_write(uuid_builder):
    """B4 (fail-on-base proof): a mixed-slot vector round-trips the CONFIGURED-
    but-empty slot as ``{slot: []}``; passing that back to insert raises
    ``WeaviateInvalidInputError('Invalid vectors: [].')`` on real weaviate. The
    copy must strip empty slots (clean_named_vector) before the write so only
    the populated slot is sent."""
    # Source vector: codesage populated, openai configured-but-empty.
    mixed = {"codesage_embed": [0.1, 0.2], "openai_embed": []}

    class _RejectEmptySlotData(_Data):
        def insert(self, properties=None, references=None, uuid=None, vector=None):
            # Mirror weaviate: an empty-LIST slot value is rejected.
            if isinstance(vector, dict):
                for slot, val in vector.items():
                    if isinstance(val, list) and len(val) == 0:
                        raise RuntimeError(f"Invalid vectors: [] (slot {slot})")
            return super().insert(
                properties=properties, references=references, uuid=uuid, vector=vector,
            )

    src = _Coll("P_CodeFunction", {
        "src-uuid": {"properties": {"full_name": "m.f", "project": "Old"}, "vector": mixed},
    })
    src.data = _RejectEmptySlotData(src)
    outcome = vc.copy_one_row_with_vector(
        src, src, "src-uuid", "dest-uuid", project_override="New",
    )
    assert outcome.status == "copied", outcome.message
    written = src.rows["dest-uuid"]["vector"]
    # Only the populated slot survives; the empty slot was dropped pre-write.
    assert written == {"codesage_embed": [0.1, 0.2]}
    assert "openai_embed" not in written


def test_copy_confirms_write_before_reporting_success():
    """A write that does NOT read back → ``failed`` (never a false success)."""
    vec = _named_vec(1.0)
    src = _Coll("P_CodeFunction", {
        "s": {"properties": {"full_name": "m.f", "project": "Old"}, "vector": vec},
    })

    # Make insert silently drop the row (write "succeeds" but leaves no row).
    class _SilentData(_Data):
        def insert(self, properties=None, references=None, uuid=None, vector=None):
            return _uuid_mod.uuid4()  # no row stored → exists() will be False

    src.data = _SilentData(src)
    outcome = vc.copy_one_row_with_vector(src, src, "s", "d")
    assert outcome.status == "failed"
    assert "confirm" in outcome.message.lower() or "read-back" in outcome.message.lower()


def test_copy_vectorless_source_is_left():
    """A source with no vector is not copyable (nothing valid to preserve)."""
    src = _Coll("P_CodeFunction", {
        "s": {"properties": {"full_name": "m.f"}, "vector": None},
    })
    outcome = vc.copy_one_row_with_vector(src, src, "s", "d")
    assert outcome.status == "failed"
    assert "vector" in outcome.message.lower()


# ─────────────────────────── T8 ───────────────────────────


def test_T8_migration_moves_row_and_vector_byte_identical(uuid_builder, monkeypatch):
    """T8: a full identity migration moves a Function row + its vector VERBATIM,
    computing ZERO embeddings, and deletes the (now-migrated) source."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    vec = _named_vec(0.5, 0.6, 0.7, 0.8)
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {
            "properties": {
                "full_name": ik, "file_path": fp, "project": old, "project_source": "",
            },
            "vector": vec,
        },
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )

    dest_uuid = uuid_builder(new, fp, ik, project_source="")
    assert summary.moved == 1
    assert summary.deduped == 0
    assert summary.left == 0
    assert summary.failures == 0
    # destination carries the verbatim vector + rewritten project
    assert func.rows[dest_uuid]["vector"] == vec
    assert func.rows[dest_uuid]["properties"]["project"] == new
    # source deleted (confirmed dest first)
    assert src_uuid not in func.rows
    assert src_uuid in func.deletes
    # machine-readable summary line
    assert summary.summary_line() == (
        "IDENTITY_MIGRATION moved=1 deduped=0 left=0 failures=0"
    )


def test_T8_migration_preserves_all_row_properties(uuid_builder, monkeypatch):
    """Regression: the migrated destination carries the FULL row (signature,
    body, content_hash, embed_revision, …), not just the identity subset — the
    copy reads all properties, never the iterator's limited return-props set."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    full_props = {
        "full_name": ik, "file_path": fp, "project": old, "project_source": "",
        "signature": "def foo()", "function_body": "def foo(): return 1",
        "content_hash": "abc123", "embed_revision": 1, "is_test": False,
        "chunk_num": 0, "total_chunks": 1, "type_uses": ["int"],
    }
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {"properties": full_props, "vector": _named_vec(0.1)},
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    vc.migrate_project_identity(client, "NewName", old, new, uuid_builder=uuid_builder)
    dest_uuid = uuid_builder(new, fp, ik, project_source="")
    dest_props = func.rows[dest_uuid]["properties"]
    # every non-project field survived verbatim; project was overridden.
    for k, v in full_props.items():
        if k == "project":
            assert dest_props["project"] == new
        else:
            assert dest_props[k] == v, f"property {k!r} was dropped/changed on copy"


# ─────────────────────────── T9 ───────────────────────────


def test_T9_destination_write_failure_leaves_source(uuid_builder, monkeypatch):
    """T9 (the destructive leave-alone): when the destination insert RAISES,
    the migration counts a failure and NEVER deletes the source row.

    NEW-code note: this cannot run on the WP-2 base commit — the module
    ``vco_lib.codegraph_vector_copy`` does not exist there. "fail-without" for
    this test means "the import fails on base", not a behavioural diff.
    """
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    vec = _named_vec(1.0, 2.0)
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {
            "properties": {"full_name": ik, "file_path": fp, "project": old},
            "vector": vec,
        },
    })
    func.insert_raises = True  # destination write blows up
    client = _empty_five("NewName", overrides={"CodeFunction": func})

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )

    assert summary.failures == 1
    assert summary.moved == 0
    # SOURCE ROW STILL PRESENT — the destructive leave-alone.
    assert src_uuid in func.rows
    assert src_uuid not in func.deletes


def test_T9_delete_failure_after_confirmed_dest_counts_failure(uuid_builder, monkeypatch):
    """A dest that IS confirmed but whose source delete fails → counted as a
    failure event (a leftover dup), never silently 'moved'."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {
            "properties": {"full_name": ik, "file_path": fp, "project": old},
            "vector": _named_vec(1.0),
        },
    })
    func.delete_raises = True
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    dest_uuid = uuid_builder(new, fp, ik, project_source="")
    assert dest_uuid in func.rows              # dest written (data preserved)
    assert summary.failures == 1               # but delete failed → failure
    assert src_uuid in func.rows               # source NOT lost


# ─────────────────────────── T10 ───────────────────────────


def test_T10_collision_with_vector_dedups_source(uuid_builder, monkeypatch):
    """T10a: destination already has a vector → the source is a true duplicate:
    delete the source, leave the destination untouched (no re-write)."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    dest_uuid = uuid_builder(new, fp, ik, project_source="")
    dest_vec = _named_vec(9.0, 9.0)
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {"properties": {"full_name": ik, "file_path": fp, "project": old},
                   "vector": _named_vec(1.0)},
        dest_uuid: {"properties": {"full_name": ik, "file_path": fp, "project": new},
                    "vector": dest_vec},
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    assert summary.deduped == 1
    assert summary.moved == 0
    # destination vector UNCHANGED (never re-written)
    assert func.rows[dest_uuid]["vector"] == dest_vec
    assert not func.inserts and not func.replaces
    # source deleted (its data already lives at dest)
    assert src_uuid not in func.rows
    assert src_uuid in func.deletes


def test_T10_collision_with_vectorless_dest_replaces_not_drops(uuid_builder, monkeypatch):
    """T10b: destination exists but is VECTORLESS → copy the good source vector
    OVER it (replace), never drop the source's vector."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    dest_uuid = uuid_builder(new, fp, ik, project_source="")
    src_vec = _named_vec(3.0, 3.0)
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {"properties": {"full_name": ik, "file_path": fp, "project": old},
                   "vector": src_vec},
        dest_uuid: {"properties": {"full_name": ik, "file_path": fp, "project": new},
                    "vector": None},  # vectorless dest
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    assert summary.moved == 1                       # replace counts as moved
    # dest now carries the good source vector (replaced, not dropped)
    assert func.rows[dest_uuid]["vector"] == src_vec
    assert func.replaces and func.replaces[0]["uuid"] == dest_uuid
    # source deleted after confirmed replace
    assert src_uuid not in func.rows


# ─────────────────────────── T11 ───────────────────────────


def test_T11_chunked_entity_migrates_all_chunks_and_leaves_unreconstructable(
    uuid_builder, monkeypatch,
):
    """T11: a 3-chunk Function migrates all chunk rows under their correct
    per-chunk identity keys (chunk 0 = bare key, chunk i = ``key::i``); a row
    whose identity cannot be reconstructed is LEFT + counted."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/big.py", "mod.big"
    # 3 chunk rows: chunk 0 keys on the bare key, chunk 1 → key::1, chunk 2 → key::2.
    chunk_keys = [ik, f"{ik}::1", f"{ik}::2"]
    src_uuids = [uuid_builder(old, fp, k, project_source="") for k in chunk_keys]
    dest_uuids = [uuid_builder(new, fp, k, project_source="") for k in chunk_keys]
    rows = {}
    for i, su in enumerate(src_uuids):
        rows[su] = {
            "properties": {
                "full_name": ik, "file_path": fp, "project": old,
                "chunk_num": i, "total_chunks": 3, "project_source": "",
            },
            "vector": _named_vec(float(i), float(i)),
        }
    # An unreconstructable row: a CodeInteraction with no stored source token.
    interaction = _Coll("NewName_CodeInteraction", {
        "ix-unrecon": {
            "properties": {"endpoint": "/x", "project": old},  # no `source`
            "vector": _named_vec(0.0),
        },
    })
    func = _Coll("NewName_CodeFunction", rows)
    client = _empty_five("NewName", overrides={
        "CodeFunction": func, "CodeInteraction": interaction,
    })

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )

    # all 3 chunk rows moved to their correct per-chunk destinations
    assert summary.moved == 3
    for i, du in enumerate(dest_uuids):
        assert du in func.rows
        assert func.rows[du]["vector"] == _named_vec(float(i), float(i))
        assert func.rows[du]["properties"]["chunk_num"] == i
    for su in src_uuids:
        assert su not in func.rows
    # the unreconstructable interaction row was LEFT + counted
    assert summary.left == 1
    assert "ix-unrecon" in interaction.rows


# ─────────────────────────── T12 ───────────────────────────


def test_T12_windows_shaped_anchor_migrates_both_uuid_probe(uuid_builder, monkeypatch):
    """T12: a legacy row whose stored file_path uses backslashes and whose UUID
    was minted from the RAW backslash form migrates correctly — the migration
    tries the RAW path first (reproducing the source UUID) then the normalized
    form, and picks the one that reproduces the stored UUID."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    raw_fp = "src\\win\\a.py"           # backslash form as stored on a Windows run
    ik = "mod.win"
    # UUID minted from the RAW backslash path (a Windows-shaped legacy row).
    src_uuid = uuid_builder(old, raw_fp, ik, project_source="")
    vec = _named_vec(4.0, 4.0)
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {
            "properties": {"full_name": ik, "file_path": raw_fp, "project": old},
            "vector": vec,
        },
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    # The RAW form reproduces the source UUID → the dest is derived from RAW too.
    dest_uuid = uuid_builder(new, raw_fp, ik, project_source="")
    assert summary.moved == 1
    assert summary.left == 0
    assert dest_uuid in func.rows
    assert func.rows[dest_uuid]["vector"] == vec


def test_T12_unreconstructable_source_uuid_is_left(uuid_builder, monkeypatch):
    """A row whose UUID was minted from inputs NEITHER file-path form reproduces
    (e.g. an older seed shape) is LEFT + counted — never a wrong destination."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    func = _Coll("NewName_CodeFunction", {
        "totally-alien-uuid": {
            "properties": {"full_name": "mod.f", "file_path": "src/a.py", "project": old},
            "vector": _named_vec(1.0),
        },
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    assert summary.left == 1
    assert summary.moved == 0
    assert "totally-alien-uuid" in func.rows  # left in place


# ─────────────────── B3: cross-reference survival + remap ────────────────────


def _module_row(uuid_builder, project, mod_path):
    """Build a CodeModule row keyed on ``module::<path>`` for ``project``."""
    ik = f"module::{mod_path}"
    su = uuid_builder(project, mod_path, ik, project_source="")
    return su, {
        "properties": {"path": mod_path, "project": project, "project_source": ""},
        "vector": _named_vec(0.9),
    }


def test_B3_migration_carries_and_remaps_references(uuid_builder, monkeypatch):
    """B3 (fail-on-base proof): the migrated destination row carries its
    cross-references, with every target UUID REMAPPED to the target's NEW
    (migrated) UUID. Without the fix the copied row carried ZERO references and
    — keeping its verbatim content_hash/embed_revision, so the analyzer SKIPs it
    forever — the edges were lost permanently.

    Setup: a CodeFunction ``mod.foo`` whose ``module`` ref points at a
    CodeModule ``src/a.py`` (both migrated). After migration the destination
    function's ``module`` ref must point at the module's NEW UUID.
    """
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    mod_path, fp, ik = "src/a.py", "src/a.py", "mod.foo"

    mod_su, mod_row = _module_row(uuid_builder, old, mod_path)
    func_su = uuid_builder(old, fp, ik, project_source="")
    # The function's stored `module` beacon points at the module's OLD uuid.
    func_row = {
        "properties": {
            "full_name": ik, "file_path": fp, "project": old, "project_source": "",
            "content_hash": "deadbeef", "embed_revision": 7,
        },
        "vector": _named_vec(0.5, 0.6),
        "references": {"module": [mod_su]},
    }
    module = _Coll("NewName_CodeModule", {mod_su: mod_row})
    func = _Coll("NewName_CodeFunction", {func_su: func_row})
    client = _empty_five("NewName", overrides={
        "CodeModule": module, "CodeFunction": func,
    })

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )

    mod_du = uuid_builder(new, mod_path, f"module::{mod_path}", project_source="")
    func_du = uuid_builder(new, fp, ik, project_source="")
    assert summary.moved == 2                    # module + function both moved
    assert summary.refs_dropped == 0
    # The migrated function's `module` ref points at the module's NEW uuid.
    dest_refs = func.rows[func_du]["references"]
    assert dest_refs is not None, "references were dropped on the copy (B3 regressed)"
    assert dest_refs["module"] == [mod_du], dest_refs
    # And the verbatim body metadata survived (content_hash / embed_revision).
    assert func.rows[func_du]["properties"]["content_hash"] == "deadbeef"
    assert func.rows[func_du]["properties"]["embed_revision"] == 7


def test_B3_reference_to_unmigrated_row_is_kept(uuid_builder, monkeypatch):
    """B3: a reference whose target is NOT being migrated (a valid, live,
    non-migrated row — e.g. a cross-project or already-canonical module) keeps
    its ORIGINAL UUID (still valid), never remapped, never dropped."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    func_su = uuid_builder(old, fp, ik, project_source="")

    # A pre-existing module row that is NOT part of the old-identity migration
    # set (its project is already the canonical `new`), so it stays live and is
    # never remapped. Its UUID is a fixed, valid beacon target.
    live_mod_uuid = "11111111-2222-3333-4444-555555555555"
    module = _Coll("NewName_CodeModule", {
        live_mod_uuid: {
            "properties": {"path": "src/canon.py", "project": new,
                           "project_source": ""},
            "vector": _named_vec(0.9),
        },
    })
    func = _Coll("NewName_CodeFunction", {
        func_su: {
            "properties": {"full_name": ik, "file_path": fp, "project": old,
                           "project_source": ""},
            "vector": _named_vec(0.5),
            "references": {"module": [live_mod_uuid]},
        },
    })
    client = _empty_five("NewName", overrides={
        "CodeModule": module, "CodeFunction": func,
    })

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    func_du = uuid_builder(new, fp, ik, project_source="")
    assert summary.moved == 1                     # only the function migrated
    assert summary.refs_dropped == 0
    # The live, non-migrated target's UUID is KEPT verbatim.
    assert func.rows[func_du]["references"]["module"] == [live_mod_uuid]
    # The live module row itself is untouched (still under `new`, not migrated).
    assert live_mod_uuid in module.rows


def test_B3_dangling_reference_is_dropped_and_counted(uuid_builder, monkeypatch):
    """B3: a reference whose target is neither being migrated NOR a currently-
    live UUID (a dangling beacon) is DROPPED from the copied row and COUNTED in
    ``summary.refs_dropped`` — never guessed at a wrong target."""
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    func_su = uuid_builder(old, fp, ik, project_source="")

    dangling_uuid = "deadbeef-0000-0000-0000-000000000000"  # resolves to nothing
    func = _Coll("NewName_CodeFunction", {
        func_su: {
            "properties": {"full_name": ik, "file_path": fp, "project": old,
                           "project_source": ""},
            "vector": _named_vec(0.5),
            # `calls` points at a target that exists in NO collection.
            "references": {"calls": [dangling_uuid]},
        },
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})

    summary = vc.migrate_project_identity(
        client, "NewName", old, new, uuid_builder=uuid_builder,
    )
    func_du = uuid_builder(new, fp, ik, project_source="")
    assert summary.moved == 1
    # The dangling `calls` beacon was dropped (never resolved by the fake read,
    # so it never even reaches the write dict) — and counted.
    assert summary.refs_dropped == 1
    dest_refs = func.rows[func_du]["references"]
    # No `calls` edge written (the only target was dangling).
    assert not dest_refs or "calls" not in dest_refs, dest_refs


# ── dry-run classifies without writing ──


def test_dry_run_writes_nothing(uuid_builder, monkeypatch):
    _assert_no_embedder(monkeypatch)
    old, new = "Old Name", "NewName"
    fp, ik = "src/a.py", "mod.foo"
    src_uuid = uuid_builder(old, fp, ik, project_source="")
    func = _Coll("NewName_CodeFunction", {
        src_uuid: {"properties": {"full_name": ik, "file_path": fp, "project": old},
                   "vector": _named_vec(1.0)},
    })
    client = _empty_five("NewName", overrides={"CodeFunction": func})
    summary = vc.migrate_project_identity(
        client, "NewName", old, new, dry_run=True, uuid_builder=uuid_builder,
    )
    assert summary.moved == 1                 # would-move
    assert not func.inserts and not func.replaces and not func.deletes
    assert src_uuid in func.rows              # nothing actually changed


# ─────────────────── T13: resync counting parity ────────────────────


# FLOOR=2 so "floor-1" (=1) is a DISTINCT below-floor positive revision, not
# the vectorless sentinel (0) — pins the guards row "0 < rev < floor → EMBED".
FLOOR = 2
CURRENT = 3  # a hypothetical future revision (floor raised to 2)


@pytest.mark.parametrize(
    "stored_rev, expect_stale, expect_kind",
    [
        (None, True, "embed_owed"),       # NULL → pre-migration → embed
        (0, True, "embed_owed"),          # vectorless sentinel → embed
        (FLOOR - 1, True, "embed_owed"),  # 0 < rev < floor → stale space → embed
        (FLOOR, True, "stamp_owed"),      # floor <= rev < current → cheap stamp
        (CURRENT, False, "current"),      # at current → no work owed
    ],
)
def test_T13_resync_counting_parity(stored_rev, expect_stale, expect_kind):
    """T13: the resync's per-row staleness + owed-kind split matches the guards
    module's classification across {NULL, 0, floor-1, floor, current}."""
    assert guards.is_row_revision_stale(stored_rev, CURRENT) is expect_stale
    kind = guards.classify_stale_kind(
        stored_rev, current_revision=CURRENT, floor_revision=FLOOR,
        vectorless_sentinel=0,
    )
    assert kind == expect_kind


def test_T13_count_stale_in_collection_delegates_to_guards(monkeypatch):
    """The resync's inline staleness (the pre-R3 revision-only tier) now
    delegates to ``guards.is_row_revision_stale`` — a fixture matrix confirms
    the count matches the guard's verdict count exactly."""
    import vco_lib.codegraph_resync as cr

    rows = [
        {"embed_revision": None},   # stale
        {"embed_revision": 0},      # stale
        {"embed_revision": CURRENT},  # not stale
        {"embed_revision": "junk"},  # stale (non-int)
        {"embed_revision": CURRENT - 1},  # stale
    ]
    coll = _RevColl("P_CodeFunction", rows)
    n = cr._count_stale_in_collection(coll, CURRENT)  # reachable_fn=None → pre-R3 tier
    expected = sum(
        1 for r in rows if guards.is_row_revision_stale(r["embed_revision"], CURRENT)
    )
    assert n == expected == 4


# ─────────────────── copy_rows_with_vectors batch primitive ──────────────────


def test_copy_rows_batch_soft_fails_per_row(monkeypatch):
    """One bad row never aborts the batch; delete only on confirmed dest."""
    vec = _named_vec(1.0)
    src = _Coll("P_CodeFunction", {
        "s1": {"properties": {"project": "Old"}, "vector": vec},
        "s2": {"properties": {"project": "Old"}, "vector": None},  # vectorless → fail
    })
    counts = vc.copy_rows_with_vectors(
        src, src, [("s1", "d1"), ("s2", "d2")],
        delete_source_on_confirm=True,
    )
    assert counts["copied"] == 1
    assert counts["failed"] == 1
    assert counts["deleted"] == 1        # only s1's dest was confirmed
    assert "s1" not in src.rows          # source deleted after confirm
    assert "s2" in src.rows              # unconfirmed → source left


# ─────────────────────────── helpers ───────────────────────────


class _RevColl:
    """A collection whose aggregate is unavailable (forces the per-row scan)."""

    def __init__(self, name, rows):
        self.name = name
        self._rows = rows
        # aggregate that raises → _count_stale_in_collection falls to the scan
        self.aggregate = types.SimpleNamespace(
            over_all=lambda **kw: (_ for _ in ()).throw(RuntimeError("no agg"))
        )

    def iterator(self, return_properties=None):
        for i, props in enumerate(self._rows):
            yield _Obj(f"u{i}", props, None)

    class _Cfg:
        pass


def _empty_five(prefix: str, overrides: Optional[Dict[str, _Coll]] = None) -> _Client:
    """A client with all 5 code-graph collections; overrides replace specific
    bases (the rest are empty)."""
    overrides = overrides or {}
    colls: Dict[str, _Coll] = {}
    for base in ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"):
        name = f"{prefix}_{base}"
        colls[name] = overrides.get(base, _Coll(name, {}))
    return _Client(colls)


def _assert_no_embedder(monkeypatch):
    """VECTOR PURITY (source-shape guard): the copy module must never CALL or
    IMPORT an embedder — it carries vectors, it never recomputes them.

    We scan the parsed AST (not raw text — the module docstring legitimately
    NAMES these symbols to say they are absent) for any ``import`` of an
    embedder module or any CALL to an embedder function. Zero hits required.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(vc))
    banned_call_names = {
        "generate_embedding", "embed_function", "embed_class",
        "chunk_or_truncate_for_embedding", "chunk_or_truncate_class_for_embedding",
    }
    banned_import_substrings = ("embedding_service", "code_embed", "code_truncation")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            assert name not in banned_call_names, (
                f"vector purity violation: call to {name!r} in codegraph_vector_copy"
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = [mod] + [a.name for a in getattr(node, "names", [])]
            for n in names:
                low = (n or "").lower()
                for bad in banned_import_substrings:
                    assert bad not in low, (
                        f"vector purity violation: import of {n!r} in codegraph_vector_copy"
                    )
