# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The duplicate scanner must NAME the vector slot it queries (v0.2.94).

Field defect, observed live 2026-09-09 on a maintainer machine (the
``post-file-edit`` hook runs this every 10th edit)::

    📊 Found 792 nodes to analyze
    ❌ Error during duplicate detection: Query call with protocol GRPC search
       failed with message extract target vectors: class
       VCODev_KnowledgeGraph has multiple vectors, but no target vectors were
       provided.
    ⚠️  Scan did NOT complete — no verdict.

EVERY VCO knowledge collection is created with several named vectors
(``qwen3_embed`` + ``arctic2_embed`` + ``openai_text_embed`` + the legacy
``ollama_embed`` / ``openai_embed``), and Weaviate refuses a vector query on
such a class unless the caller names the slot. ``detect_duplicates.py`` never
did, so it had produced NO verdict on every install since named vectors
shipped — the honest "Scan did NOT complete" line was the only trace.

These tests DRIVE the real ``find_duplicates`` against a fake collection that
RECORDS the query kwargs. They do not scan the source for ``target_vector``:
a name in a comment satisfies a source scan, and the whole point is that a
missing kwarg is invisible until a real multi-vector class rejects it.

Mutation check (red-proof): delete the two ``if target_vector:`` lines in
``detect_duplicates.py::find_duplicates`` and
``test_multi_vector_collection_targets_the_active_slot`` fails immediately.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCANNER = REPO / "templates" / "scripts" / "detect_duplicates.py"
SYNC = REPO / "templates" / "scripts" / "sync_knowledge_graph.py"


# ─── doubles ────────────────────────────────────────────────────────────────


class _Meta:
    def __init__(self, distance: float) -> None:
        self.distance = distance


class _Obj:
    """One Weaviate object as the scanner consumes it."""

    def __init__(self, uid: str, title: str, path: str) -> None:
        self.uuid = uid
        self.properties = {"title": title, "file_path": path, "node_type": "concept"}
        self.metadata = _Meta(0.0)


class _Page:
    def __init__(self, objects: list) -> None:
        self.objects = objects


class _Config:
    """``collection.config.get()`` result — only ``vector_config`` matters."""

    def __init__(self, vector_config) -> None:
        self.vector_config = vector_config


class _ConfigHandle:
    def __init__(self, vector_config) -> None:
        self._vector_config = vector_config

    def get(self) -> _Config:
        return _Config(self._vector_config)


class _Query:
    def __init__(self, node: _Obj, recorder: list) -> None:
        self._node = node
        self._recorder = recorder

    def fetch_objects(self, **_kw):
        return _Page([self._node])

    def near_object(self, **kwargs):
        self._recorder.append(kwargs)
        # The node itself — the scanner skips self, so the scan completes
        # with zero pairs and we get a clean look at the kwargs.
        return _Page([self._node])


class _Collection:
    """A collection whose SCHEMA shape is the variable under test."""

    def __init__(self, vector_config, *, with_config: bool = True) -> None:
        self.node = _Obj(str(uuid.uuid4()), "A Node", "knowledge/a.md")
        self.recorded: list = []
        self.query = _Query(self.node, self.recorded)
        if with_config:
            self.config = _ConfigHandle(vector_config)


# The real multi-vector shape VCO creates (verified live against
# ``VCODev_KnowledgeGraph``): weaviate-client returns a dict keyed by slot.
MULTI_VECTOR = {
    "arctic2_embed": object(),
    "ollama_embed": object(),
    "openai_embed": object(),
    "openai_text_embed": object(),
    "qwen3_embed": object(),
}


# ─── helpers ────────────────────────────────────────────────────────────────


def _load(path: Path):
    """Import a shipped script by path. Opens no connection."""
    name = f"_v0294_{path.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # pragma: no cover — partial install
        pytest.skip(f"{path.name} has runtime deps not installed ({exc})")
    return mod


def _pin_active_embedding(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    """Make the ACTIVE profile the only input to slot resolution.

    ``EMBEDDING_MODEL`` outranks ``ACTIVE_EMBEDDING`` in the shared resolver
    (``embedding_service.resolve_active_text_model_id``), and a dev shell
    routinely exports it — so it must be cleared, or this test would assert
    the machine's env instead of the mapping.
    """
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", profile)


def _scan(mod, collection) -> list:
    """Run the REAL ``find_duplicates`` over *collection*; return its result."""
    det = mod.DuplicateDetector.__new__(mod.DuplicateDetector)
    det.threshold = 0.95
    det.scan_error = None
    det.collection = collection
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        return det.find_duplicates(), det


# ─── (a) multi-vector schema → the ACTIVE slot is named ─────────────────────


@pytest.mark.parametrize(
    ("profile", "expected_slot"),
    [
        # The real mapping: `_model_id_for_active` (profile → model id) then
        # `TEXT_SLOT_MAP` (model id → slot), both in vco_lib/embedding_service.py.
        #   qwen3  → qwen3-embedding:0.6b          → qwen3_embed
        #   arctic → snowflake-arctic-embed2:latest → arctic2_embed
        #   openai → text-embedding-3-small         → openai_text_embed
        ("qwen3", "qwen3_embed"),
        ("arctic", "arctic2_embed"),
        ("openai", "openai_text_embed"),
    ],
)
def test_multi_vector_collection_targets_the_active_slot(
    monkeypatch: pytest.MonkeyPatch, profile: str, expected_slot: str
) -> None:
    """The field defect, pinned: the query NAMES the active slot.

    Red-proof: drop the ``if target_vector:`` guard's body in
    ``find_duplicates`` and this fails with ``target_vector`` absent — the
    exact state that made every real scan die on its first ``near_object``.
    """
    _pin_active_embedding(monkeypatch, profile)
    mod = _load(SCANNER)
    coll = _Collection(MULTI_VECTOR)

    duplicates, det = _scan(mod, coll)

    assert det.scan_error is None, f"scan must complete: {det.scan_error}"
    assert coll.recorded, "near_object was never called — the scan did not run"
    for kwargs in coll.recorded:
        assert kwargs.get("target_vector") == expected_slot, (
            "a multi-vector collection rejects an unnamed vector query; "
            f"expected target_vector={expected_slot!r}, got {kwargs!r}"
        )
    assert duplicates == []


def test_active_slot_matches_what_the_writer_would_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read/write symmetry, not just a literal.

    A scan that targets a slot ``kg-sync`` never populated is as useless as
    one that names none. Both sides go through ``active_text_vector_slot``,
    so this asserts the value the scanner sends IS that function's answer.
    """
    _pin_active_embedding(monkeypatch, "arctic")
    from vco_lib.kg_vector_slot import active_text_vector_slot

    mod = _load(SCANNER)
    coll = _Collection(MULTI_VECTOR)
    _scan(mod, coll)

    assert coll.recorded[0]["target_vector"] == active_text_vector_slot()


# ─── (b) legacy single UNNAMED vector → the kwarg is OMITTED ────────────────


def test_legacy_single_unnamed_vector_omits_target_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compatibility with pre-named-vector collections.

    weaviate-client reports ``vector_config = None`` for such a class
    (verified live against a legacy ``*_KnowledgeGraph`` still on this
    machine). Passing ANY name to it is an error, so the kwarg must be
    absent — not empty-string, absent.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    coll = _Collection(None)

    _duplicates, det = _scan(mod, coll)

    assert det.scan_error is None, f"scan must complete: {det.scan_error}"
    assert coll.recorded, "near_object was never called"
    for kwargs in coll.recorded:
        assert "target_vector" not in kwargs, (
            "a legacy single-unnamed-vector class rejects target_vector; "
            f"the kwarg must be omitted entirely, got {kwargs!r}"
        )


def test_single_named_vector_is_named_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One named vector is unambiguous — name it anyway, visibly.

    Collections like ``UnifiedMessages`` carry exactly one named slot; the
    active profile may not match it. Naming the one that EXISTS keeps the
    scan working (and prints which slot it used) instead of erroring on a
    slot this class never had.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    coll = _Collection({"ollama_embed": object()})

    _duplicates, det = _scan(mod, coll)

    assert det.scan_error is None
    assert coll.recorded[0].get("target_vector") == "ollama_embed"


def test_unreadable_schema_refuses_instead_of_guessing_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undeterminable schema → NO VERDICT. Cannot confirm ⇒ do nothing.

    v0.2.94 review item 4: this used to assume named vectors and query the
    ACTIVE slot — a guess about the very thing the probe failed to establish,
    and a guess in the dangerous direction: on a legacy single-unnamed-vector
    class the named kwarg is an ERROR, so the scan would die naming a slot
    instead of naming the schema read that failed.

    Mutation check: make `kg_query_target_vector` fall back to the active slot
    when `collection_vector_slots` returns None and this fails — a query is
    issued and the scan reports a clean graph it never actually checked.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    coll = _Collection(None, with_config=False)  # no `.config` at all

    duplicates, det = _scan(mod, coll)

    assert duplicates == []
    assert det.scan_error, "an unreadable schema must be recorded as a failure"
    assert "vector schema" in det.scan_error
    assert not coll.recorded, "no query may be issued with an unknown slot"


# ─── (c) ONE home: scanner AND kg-sync go through the shared resolver ───────


def test_scanner_resolves_through_the_shared_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVEN proof that the scanner calls ``vco_lib.kg_vector_slot``.

    Patching the shared module's function must change what the scanner sends.
    A private copy inside ``detect_duplicates.py`` would ignore the patch —
    which is exactly the divergence that let three consumers answer this same
    question differently until now.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    import vco_lib.kg_vector_slot as shared

    monkeypatch.setattr(
        shared, "kg_query_target_vector", lambda *_a, **_kw: "sentinel_slot"
    )
    mod = _load(SCANNER)
    coll = _Collection(MULTI_VECTOR)

    _scan(mod, coll)

    assert coll.recorded[0].get("target_vector") == "sentinel_slot"


def test_kg_sync_writer_resolves_through_the_same_shared_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WRITER shares the home too — asserted on the live binding + driven.

    ``sync_knowledge_graph.py`` binds the shared function at import time, so
    identity is the honest check that it is not a copy; the drive below then
    shows the property's answer comes from that function (the pre-v0.2.94 body
    ``return self.embedding_service.text_vector_slot`` would raise
    AttributeError for this service, never return the resolved slot).
    """
    _pin_active_embedding(monkeypatch, "arctic")
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    import vco_lib.kg_vector_slot as shared

    sync = _load(SYNC)

    assert sync.active_text_vector_slot is shared.active_text_vector_slot, (
        "kg-sync must use the shared resolver object, not a same-named copy"
    )

    class _ServiceWithoutTheAttribute:
        """A degenerate service: the copy raised here, the shared home resolves."""

    wrapper = sync.WeaviateWrapper.__new__(sync.WeaviateWrapper)
    wrapper.embedding_service = _ServiceWithoutTheAttribute()
    assert wrapper.text_vector_slot == shared.active_text_vector_slot()
    assert wrapper.text_vector_slot == "arctic2_embed"


def test_a_real_service_stays_authoritative_for_the_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service that will EMBED still wins over env derivation.

    Guards the migration: routing through the shared home must not start
    ignoring the constructed service (whose slot may differ from a stale env).
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    sync = _load(SYNC)

    class _Service:
        text_vector_slot = "openai_text_embed"

    wrapper = sync.WeaviateWrapper.__new__(sync.WeaviateWrapper)
    wrapper.embedding_service = _Service()
    assert wrapper.text_vector_slot == "openai_text_embed"


# ─── failure stays LOUD — never a silent "0 duplicates" ─────────────────────


def test_unresolvable_slot_is_a_failed_scan_not_a_clean_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken install cannot produce a verdict.

    If ``vco_lib`` is unreachable the slot cannot be resolved, and guessing
    one would be the silent-fallback the standing rule forbids. The scan must
    record ``scan_error`` (→ "Scan did NOT complete", exit 1), not report a
    clean graph.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    coll = _Collection(MULTI_VECTOR)

    # `None` in sys.modules makes the function-local import raise ImportError —
    # the shape of a partial install, without touching the filesystem.
    monkeypatch.setitem(sys.modules, "vco_lib.kg_vector_slot", None)

    duplicates, det = _scan(mod, coll)

    assert duplicates == []
    assert det.scan_error, "an unresolvable slot must be recorded as a failure"
    assert "vco_lib" in det.scan_error
    assert not coll.recorded, "no query may be issued with an unresolved slot"


def test_shared_resolver_reports_the_legacy_shape_as_no_named_vectors() -> None:
    """Unit-level pin on the probe's three-way answer.

    ``()`` (legacy, omit) and ``None`` (undeterminable) are DIFFERENT answers;
    collapsing them would either break legacy classes or re-open the defect.
    """
    from vco_lib.kg_vector_slot import collection_vector_slots

    assert collection_vector_slots(_Collection(None)) == ()
    assert collection_vector_slots(_Collection(MULTI_VECTOR)) == tuple(
        sorted(MULTI_VECTOR)
    )
    assert collection_vector_slots(_Collection(None, with_config=False)) is None

    # A config object that does not expose `vector_config` at all is
    # undeterminable too — NOT "no named vectors". The two need opposite
    # kwargs, so collapsing them would break one shape or the other.
    class _ConfigWithoutVectorConfig:
        def get(self):
            return object()

    class _Coll:
        config = _ConfigWithoutVectorConfig()

    assert collection_vector_slots(_Coll()) is None
