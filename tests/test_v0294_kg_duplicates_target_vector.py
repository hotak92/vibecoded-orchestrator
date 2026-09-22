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


# The message Weaviate really returns for `near_object` against a uuid that no
# longer exists — captured verbatim from the live instance on 2026-09-22.
# NOTE the absent `for target: <slot>` tail: the SAME sentence with that tail
# means "this object has no vector in the slot you named". The scanner must
# not tell them apart by text (it asks whether the object still exists), and
# these doubles exercise both.
VANISHED_MSG = (
    "Query call with protocol GRPC search failed with message explorer: get "
    "class: concurrentTargetVectorSearch): explorer: get class: vectorize "
    "search vector: nearObject params: vector not found."
)
EMPTY_SLOT_MSG = VANISHED_MSG[:-1] + " for target: ollama_embed."


class _Query:
    def __init__(
        self,
        node: _Obj,
        recorder: list,
        *,
        nodes: "list[_Obj] | None" = None,
        vanished: "frozenset[str] | set[str]" = frozenset(),
        still_present_failures: "frozenset[str] | set[str]" = frozenset(),
        exists_probe_raises: bool = False,
    ) -> None:
        self._node = node
        self._recorder = recorder
        self._nodes = [node] if nodes is None else list(nodes)
        #: uuids DELETED between the snapshot and their comparison — the
        #: v0.2.96 race (kg-sync upserts delete-then-insert with a new uuid).
        self._vanished = set(vanished)
        #: uuids whose `near_object` fails while the object is STILL THERE —
        #: the structural failures (wrong slot, dead transport) that must keep
        #: aborting the scan.
        self._still_present_failures = set(still_present_failures)
        self._exists_probe_raises = exists_probe_raises

    def fetch_objects(self, **_kw):
        return _Page(list(self._nodes))

    def fetch_object_by_id(self, uid, **_kw):
        if self._exists_probe_raises:
            raise RuntimeError("schema/transport error during existence probe")
        key = str(uid)
        if key in self._vanished:
            return None
        for node in self._nodes:
            if str(node.uuid) == key:
                return node
        return None

    def near_object(self, **kwargs):
        self._recorder.append(kwargs)
        key = str(kwargs.get("near_object"))
        if key in self._vanished:
            raise RuntimeError(VANISHED_MSG)
        if key in self._still_present_failures:
            raise RuntimeError(EMPTY_SLOT_MSG)
        # The node itself — the scanner skips self, so the scan completes
        # with zero pairs and we get a clean look at the kwargs.
        for node in self._nodes:
            if str(node.uuid) == key:
                return _Page([node])
        return _Page([self._node])


class _Collection:
    """A collection whose SCHEMA shape is the variable under test."""

    def __init__(
        self,
        vector_config,
        *,
        with_config: bool = True,
        nodes: "list[_Obj] | None" = None,
        vanished: "frozenset[str] | set[str]" = frozenset(),
        still_present_failures: "frozenset[str] | set[str]" = frozenset(),
        exists_probe_raises: bool = False,
    ) -> None:
        self.node = _Obj(str(uuid.uuid4()), "A Node", "knowledge/a.md")
        self.recorded: list = []
        self.query = _Query(
            self.node,
            self.recorded,
            nodes=nodes,
            vanished=vanished,
            still_present_failures=still_present_failures,
            exists_probe_raises=exists_probe_raises,
        )
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


# ═══════════════════════════════════════════════════════════════════════════
# v0.2.96 — the SECOND field defect on this same mechanism (2026-09-22)
# ═══════════════════════════════════════════════════════════════════════════
#
# Observed live, via the hook path, on a maintainer machine::
#
#     📊 Found 761 nodes to analyze
#     ❌ Error during duplicate detection: Query call with protocol GRPC search
#        failed with message explorer: get class: concurrentTargetVectorSearch):
#        explorer: get class: vectorize search vector: nearObject params:
#        vector not found.
#     ⚠️  Scan did NOT complete — no verdict.
#
# …and the SAME scanner, run directly seconds later, completed: 761 nodes,
# 8 duplicates, report written. The scanner was not broken; one invocation
# PATH was.
#
# Root cause: the scan snapshots every node's uuid up front and then issues
# one `near_object(<that uuid>)` per node. `route-touched-path.sh` fires it
# from the knowledge-edit hook itself — into the kg-sync burst the same fire
# just scheduled — and `sync_knowledge_graph` upserts by DELETE + INSERT with
# no fixed uuid, so a node re-synced mid-scan loses the snapshotted uuid
# forever. Weaviate answers that with `nearObject params: vector not found`
# and the single outer `except` aborted the entire run.
#
# These tests drive the REAL `find_duplicates`. The property they protect is
# not "tolerate errors" — it is the pair: a vanished node is survivable AND
# says so; anything else still fails loudly.


def _nodes(count: int) -> list:
    return [
        _Obj(str(uuid.uuid4()), f"Node {i}", f"knowledge/n{i}.md")
        for i in range(count)
    ]


def test_a_node_resynced_mid_scan_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field defect, pinned: one vanished node must not kill the scan.

    Red-proof: delete the ``try/except`` around ``near_object`` in
    ``find_duplicates`` (or its ``continue``) and this fails — ``scan_error``
    is set and the 3-node scan produces no verdict, exactly as the live run
    did over 761 nodes.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    nodes = _nodes(3)
    coll = _Collection(MULTI_VECTOR, nodes=nodes, vanished={str(nodes[1].uuid)})

    duplicates, det = _scan(mod, coll)

    assert det.scan_error is None, (
        "a node re-synced under the scan is an expected condition on a live "
        f"collection, not a scan failure: {det.scan_error}"
    )
    assert det.nodes_skipped == 1
    assert duplicates == []
    # All three were attempted; the survivors were really compared.
    assert len(coll.recorded) == 3


def test_a_partial_scan_is_announced_not_silently_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tolerance must not become silence.

    The 2026-09-09 fix on this same scanner exists because "no verdict" read
    as "nothing found". A partial verdict must be as visible: the count is on
    the detector, and the terminal line carries ⚠️ — the marker the hook's
    report grep (``route-touched-path.sh``: ``✅|⚠️|📊|❌``) keys on, so it
    reaches the reader rather than dying in dropped stdout.

    Red-proof: drop the ``if self.nodes_skipped:`` progress block and this
    fails on the missing ⚠️ line.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    nodes = _nodes(4)
    gone = {str(nodes[0].uuid), str(nodes[2].uuid)}
    coll = _Collection(MULTI_VECTOR, nodes=nodes, vanished=gone)

    det = mod.DuplicateDetector.__new__(mod.DuplicateDetector)
    det.threshold = 0.95
    det.scan_error = None
    det.collection = coll
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        det.find_duplicates()

    assert det.nodes_skipped == 2
    printed = out.getvalue()
    assert "⚠️" in printed, f"a partial scan must announce itself: {printed!r}"
    assert "2 of 4" in printed


def test_a_failure_with_the_object_still_present_still_fails_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous direction stays closed.

    An EMPTY-SLOT failure carries nearly the same sentence as a vanished
    object (they differ only by ``for target: <slot>``), and it means the scan
    is querying a vector nothing ever wrote. If the tolerance swallowed that,
    the scanner would report "0 duplicates" for a graph it never searched —
    the precise lie v0.2.92 and v0.2.94 each closed once.

    Red-proof: replace the ``if not self._node_is_gone(node_uuid): raise``
    guard with an unconditional ``continue`` and this fails: the scan reports
    a clean graph with ``scan_error is None``.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    nodes = _nodes(3)
    coll = _Collection(
        MULTI_VECTOR, nodes=nodes, still_present_failures={str(nodes[1].uuid)}
    )

    duplicates, det = _scan(mod, coll)

    assert duplicates == []
    assert det.scan_error, "a failure on an object that is STILL THERE is fatal"
    assert "vector not found" in det.scan_error


def test_an_existence_probe_that_itself_fails_is_not_a_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cannot confirm ⇒ do nothing (the standing conservative-default rule).

    If the probe that would establish "this object is gone" raises, absence is
    NOT established. Guessing it is would turn a broken transport into a
    partial-but-plausible verdict.

    Red-proof: make ``_node_is_gone`` return True in its ``except`` arm and
    this fails — the scan completes with ``scan_error is None``.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    nodes = _nodes(2)
    coll = _Collection(
        MULTI_VECTOR,
        nodes=nodes,
        vanished={str(nodes[0].uuid)},
        exists_probe_raises=True,
    )

    _duplicates, det = _scan(mod, coll)

    assert det.scan_error, "an unconfirmable absence must not be read as absence"


def test_every_node_vanishing_is_no_verdict_not_a_clean_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing compared is the ABSENCE of a verdict, not a partial one.

    A full re-embed or a wholesale kg-sync invalidates every snapshotted uuid.
    Reporting "0 duplicates, 100% skipped" would be a verdict shaped like a
    clean bill of health.

    Red-proof: delete the ``nodes_skipped == len(nodes)`` raise and this fails
    — ``scan_error`` is None and ``main`` would exit 0.
    """
    _pin_active_embedding(monkeypatch, "qwen3")
    mod = _load(SCANNER)
    nodes = _nodes(3)
    coll = _Collection(
        MULTI_VECTOR, nodes=nodes, vanished={str(n.uuid) for n in nodes}
    )

    duplicates, det = _scan(mod, coll)

    assert duplicates == []
    assert det.scan_error, "zero nodes compared is no verdict"
    assert "disappeared" in det.scan_error


# ─── the verdict surfaces: a partial scan is never printed as "clean" ───────


class _StubDetector:
    """Stands in for the real detector so ``main`` can be driven end to end."""

    instances: list = []

    def __init__(self, similarity_threshold: float = 0.95) -> None:
        self.threshold = similarity_threshold
        self.scan_error = None
        self.nodes_skipped = 2
        _StubDetector.instances.append(self)

    def find_duplicates(self):
        return []

    def close(self):
        return None


def _run_main(mod, monkeypatch: pytest.MonkeyPatch, argv: list):
    monkeypatch.setattr(mod, "DuplicateDetector", _StubDetector)
    monkeypatch.setattr(sys, "argv", ["detect_duplicates.py", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main()
    return code, out.getvalue(), err.getvalue()


def test_main_never_prints_clean_after_a_partial_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The human verdict.

    Red-proof: delete the ``elif detector.nodes_skipped:`` arm in ``main`` and
    this fails — the run prints "knowledge graph is clean!" for a graph two of
    whose nodes were never looked at.
    """
    mod = _load(SCANNER)
    _StubDetector.instances = []

    code, out, _err = _run_main(mod, monkeypatch, [])

    assert code == 0, "a PARTIAL scan completed — it is not a failure"
    assert "clean" not in out.lower(), f"partial must not read as clean: {out!r}"
    assert "⚠️" in out and "PARTIAL" in out


def test_json_payload_carries_the_skip_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The machine verdict — the launcher modal needs the same distinction.

    Red-proof: drop ``"nodes_skipped"`` from the payload and this fails; the
    launcher would render "0 duplicates" for a partial scan.
    """
    import json as _json

    mod = _load(SCANNER)
    _StubDetector.instances = []

    code, out, _err = _run_main(mod, monkeypatch, ["--json"])

    payload = _json.loads(out)
    assert payload["nodes_skipped"] == 2
    assert payload["count"] == 0
    assert code == 0


# ─── the scanner addresses the ENV-RESOLVED Weaviate, not a hardcoded one ───


def test_the_scanner_connects_to_the_env_resolved_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WEAVIATE_PORT`` must steer the scan (v0.2.96).

    ``detect_duplicates.py`` computed ``WEAVIATE_URL`` at module level — and a
    parity test pins that computation against the shared home — while its
    ``connect_to_custom`` hardcoded ``localhost:8081``. The constant was
    credited and inert: on a relocated Weaviate the scan addressed a DIFFERENT
    instance (or nothing), and reported no verdict for a collection it never
    reached.

    Red-proof: restore ``http_host='localhost', http_port=8081`` and this
    fails on the recorded URL.
    """
    monkeypatch.delenv("WEAVIATE_URL", raising=False)
    monkeypatch.setenv("WEAVIATE_PORT", "19731")
    monkeypatch.setenv("GRPC_PORT", "50052")

    import vco_lib.weaviate_helpers as wh

    recorded: dict = {}

    class _FakeCollections:
        def get(self, name):
            return _Collection(MULTI_VECTOR)

    class _FakeClient:
        collections = _FakeCollections()

    def _fake_connect(url=None, **kwargs):
        recorded["url"] = url
        recorded.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(wh, "connect_v4", _fake_connect)

    mod = _load(SCANNER)
    assert mod.WEAVIATE_URL == "http://localhost:19731"

    det = mod.DuplicateDetector(similarity_threshold=0.95)
    try:
        assert recorded["url"] == "http://localhost:19731", (
            "the scan must address the env-resolved instance, not localhost:8081"
        )
        assert recorded["skip_init_checks"] is False, (
            "an unreachable Weaviate must still fail at connect, loudly"
        )
    finally:
        det.close()
