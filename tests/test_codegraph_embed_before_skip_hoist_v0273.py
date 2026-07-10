# SPDX-License-Identifier: AGPL-3.0-or-later
"""FIX-B2 (v0.2.73): hoist the content-hash skip BEFORE the embed.

Background — the ~50x Ollama waste this fix targets:
  The per-object content-hash tombstone-skip (v0.2.61) SAVED the Weaviate
  ``replace()`` on a byte-identical object, but it ran AFTER the walker had
  already called ``embed_function``/``embed_class`` (Ollama/CodeSage) and put
  the vector into ``insert_params``. So a 1-line edit to a 50-function file
  re-EMBEDDED all ~51 functions even though 50 were unchanged — the skip only
  avoided the write, not the embed compute.

Fix under test:
  Walkers now pass a zero-arg embed callable via
  ``insert_params['_deferred_embed']`` instead of an eager ``vector``.
  ``_dedup_insert`` → ``_resolve_deferred_embed`` point-reads the stored
  fingerprint and, when the object is byte-identical + at the current
  embed_revision, SKIPS the embed entirely. A changed / absent / uncertain
  object still embeds (fail-safe).

Contract pinned here:
  (a) unchanged object → deferred embedder NOT called, no vector set, no write.
  (b) changed object   → deferred embedder called once, vector set, write.
  (c) absent object    → deferred embedder called (fail-safe embed), write.
  (d) stale embed_revision (content matches) → deferred embedder called
      (revision gate forces re-embed), write.
  (e) N-func file, 1 changed → exactly 1 embed across N objects (the headline
      ~Nx cut).
  (f) the ``_deferred_embed`` key never leaks into the Weaviate replace/insert
      kwargs.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_fixb2_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module file missing: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


class _FakeObject:
    def __init__(self, properties: Dict[str, Any]) -> None:
        self.properties = properties


class _FakeQuery:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store
        self.fetch_calls: List[str] = []

    def fetch_object_by_id(self, uuid: str, return_properties=None) -> Optional[_FakeObject]:  # noqa: ARG002
        self.fetch_calls.append(str(uuid))
        props = self._store.get(str(uuid))
        return None if props is None else _FakeObject(props)


class _FakeCollectionData:
    def __init__(self) -> None:
        self.replace_calls: List[Dict[str, Any]] = []
        self.insert_calls: List[Dict[str, Any]] = []
        # v0.2.74 (D1): record metadata-PATCH calls (embed_revision stamp).
        self.update_calls: List[Dict[str, Any]] = []

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self.replace_calls.append({"uuid": str(uuid), **kwargs})
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self.insert_calls.append({"uuid": str(uuid), **kwargs})
        return str(uuid)

    def update(self, uuid: str, properties: Dict[str, Any]) -> None:
        self.update_calls.append({"uuid": str(uuid), "properties": properties})
        return None


class _FakeCollection:
    def __init__(self, name: str, store: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.name = name
        self.data = _FakeCollectionData()
        self.query = _FakeQuery(store if store is not None else {})


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "TestProject"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    return inst


class _EmbedCounter:
    """A deferred embedder that counts invocations and returns a fixed slot."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"codesage_embed": [0.1, 0.2, 0.3]}


def _func_params_with_deferred(embedder: _EmbedCounter, body: str = "def foo():\n    return 1\n") -> Dict[str, Any]:
    """A CodeFunction insert_params carrying a DEFERRED embedder (no eager vector)."""
    return {
        "properties": {
            "name": "foo",
            "full_name": "mod.foo",
            "function_body": body,
            "signature": "foo()",
            "type_uses": [],
        },
        "_deferred_embed": embedder,
    }


def _stored_hash_for(analyzer_mod, body: str) -> str:
    """The stored content_hash for the single-chunk steady state (chunk_num=0)."""
    props = {
        "name": "foo", "full_name": "mod.foo", "function_body": body,
        "signature": "foo()", "type_uses": [], 
        "chunk_num": 0, "total_chunks": 1,
    }
    return analyzer_mod._content_hash_for_object("T_CodeFunction", props)


# ---------------------------------------------------------------------------
# (a) unchanged → NO embed
# ---------------------------------------------------------------------------


def test_unchanged_object_skips_the_embed(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    params = _func_params_with_deferred(embedder)

    det_uuid = analyzer_mod._deterministic_uuid(analyzer.project_name, "src/mod.py", "mod.foo")
    stored_hash = _stored_hash_for(analyzer_mod, "def foo():\n    return 1\n")
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
            "total_chunks": 1,
        }},
    )

    out = analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert out == det_uuid
    assert embedder.calls == 0, "Unchanged object must NOT call the embedder (the ~50x cut)"
    assert coll.data.replace_calls == [], "Unchanged object must NOT be written"


# ---------------------------------------------------------------------------
# (b) changed → embed once + write
# ---------------------------------------------------------------------------


def test_changed_object_embeds_once_and_writes(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    params = _func_params_with_deferred(embedder, body="def foo():\n    return 2\n")

    det_uuid = analyzer_mod._deterministic_uuid(analyzer.project_name, "src/mod.py", "mod.foo")
    # Stored hash is for the OLD body → mismatch → embed + write.
    stored_hash = _stored_hash_for(analyzer_mod, "def foo():\n    return 1\n")
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
            "total_chunks": 1,
        }},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "Changed object must embed exactly once"
    assert len(coll.data.replace_calls) == 1, "Changed object must be written"
    written = coll.data.replace_calls[0]
    assert written.get("vector"), "The freshly-embedded vector must be attached"


# ---------------------------------------------------------------------------
# (c) absent → fail-safe embed + write
# ---------------------------------------------------------------------------


def test_absent_object_embeds_fail_safe(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    params = _func_params_with_deferred(embedder)
    coll = _FakeCollection("T_CodeFunction", store={})

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "Absent object must embed (fail-safe)"
    assert len(coll.data.replace_calls) == 1


# ---------------------------------------------------------------------------
# (d) stale embed_revision (content matches) → forced re-embed
# ---------------------------------------------------------------------------


def test_stale_revision_forces_reembed(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    params = _func_params_with_deferred(embedder)

    det_uuid = analyzer_mod._deterministic_uuid(analyzer.project_name, "src/mod.py", "mod.foo")
    stored_hash = _stored_hash_for(analyzer_mod, "def foo():\n    return 1\n")
    # Content matches, but stored at a STALE revision (0) → must re-embed.
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {"content_hash": stored_hash, "embed_revision": 0, "total_chunks": 1}},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "A stale embed_revision must force a re-embed even if content matches"
    assert len(coll.data.replace_calls) == 1


# ---------------------------------------------------------------------------
# (e) N funcs, 1 changed → exactly 1 embed across N objects
# ---------------------------------------------------------------------------


def test_n_funcs_one_changed_embeds_once(analyzer_mod: types.ModuleType) -> None:
    """The headline win: editing 1 of N functions embeds ONCE, not N times."""
    analyzer = _make_analyzer(analyzer_mod)
    N = 8
    bodies = {f"f{i}": f"def f{i}():\n    return {i}\n" for i in range(N)}

    # Seed the store as if all N were previously indexed + unchanged.
    store: Dict[str, Dict[str, Any]] = {}
    for i in range(N):
        ident = f"mod.f{i}"
        det_uuid = analyzer_mod._deterministic_uuid(analyzer.project_name, "src/mod.py", ident)
        props = {
            "name": f"f{i}", "full_name": ident, "function_body": bodies[f"f{i}"],
            "signature": f"f{i}()", "type_uses": [], 
            "chunk_num": 0, "total_chunks": 1,
        }
        h = analyzer_mod._content_hash_for_object("T_CodeFunction", props)
        store[det_uuid] = {
            "content_hash": h,
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
            "total_chunks": 1,
        }
    coll = _FakeCollection("T_CodeFunction", store=store)

    # Now edit exactly ONE function's body; re-run all N through _dedup_insert.
    total_embeds = 0
    for i in range(N):
        ident = f"mod.f{i}"
        body = bodies[f"f{i}"]
        if i == 3:
            body = "def f3():\n    return 999\n"  # the one edited function
        embedder = _EmbedCounter()
        params = {
            "properties": {
                "name": f"f{i}", "full_name": ident, "function_body": body,
                "signature": f"f{i}()", "type_uses": [], 
            },
            "_deferred_embed": embedder,
        }
        analyzer._dedup_insert(coll, params, ident, file_path_rel="src/mod.py")
        total_embeds += embedder.calls

    assert total_embeds == 1, (
        f"Only the 1 edited function should embed; got {total_embeds} embeds across {N} funcs"
    )
    assert len(coll.data.replace_calls) == 1, "Only the changed function is written"


# ---------------------------------------------------------------------------
# (f) the deferred-embed key never reaches Weaviate kwargs
# ---------------------------------------------------------------------------


def test_deferred_embed_key_never_leaks_to_weaviate(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    params = _func_params_with_deferred(embedder, body="def foo():\n    return 2\n")
    coll = _FakeCollection("T_CodeFunction", store={})

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1
    written = coll.data.replace_calls[0]
    assert "_deferred_embed" not in written, (
        "_deferred_embed must be popped before the Weaviate replace()/insert() splat"
    )
    # And the top-level insert_params dict must have had the key removed.
    assert "_deferred_embed" not in params


# ---------------------------------------------------------------------------
# (g) v0.2.74 D1 — PATCH-not-embed for content-unchanged stale-revision rows
# whose stored vector is still valid (embedding space unchanged).
# ---------------------------------------------------------------------------


def test_d1_stamp_only_for_unchanged_row_with_valid_vector(
    analyzer_mod: types.ModuleType, monkeypatch,
) -> None:
    """Content byte-identical, stored at a POSITIVE-but-stale revision whose
    vector is still in the current space → PATCH embed_revision, do NOT embed,
    do NOT tombstone (replace)."""
    # Simulate a future metadata-only revision bump: current = 2, compat floor
    # stays 1, so a stored rev-1 row is "old-but-valid" → stamp-only.
    monkeypatch.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 2)
    monkeypatch.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 1)

    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    body = "def foo():\n    return 1\n"
    params = _func_params_with_deferred(embedder, body=body)

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo")
    stored_hash = _stored_hash_for(analyzer_mod, body)
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": 1,       # positive (has vector) but stale (< 2)
            "total_chunks": 1,         # single-chunk
        }},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 0, "unchanged row with valid vector must NOT re-embed"
    assert coll.data.replace_calls == [], "no tombstone/replace — stamp only"
    assert len(coll.data.update_calls) == 1, "embed_revision must be PATCHED"
    patched = coll.data.update_calls[0]
    assert patched["uuid"] == det_uuid
    assert patched["properties"] == {"embed_revision": 2}


def test_d1_vectorless_rev0_still_reembeds(
    analyzer_mod: types.ModuleType, monkeypatch,
) -> None:
    """A rev-0 (VECTORLESS) row must RE-EMBED, never stamp — it has no valid
    vector to preserve (guards against a wrong-skip stranding a vectorless row)."""
    monkeypatch.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 2)
    monkeypatch.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 1)

    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    body = "def foo():\n    return 1\n"
    params = _func_params_with_deferred(embedder, body=body)

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo")
    stored_hash = _stored_hash_for(analyzer_mod, body)
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": 0,       # VECTORLESS → must re-embed
            "total_chunks": 1,
        }},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "vectorless rev-0 must re-embed, not stamp"
    assert coll.data.update_calls == [], "no stamp-only for a vectorless row"


def test_d1_stale_vector_below_compat_floor_reembeds(
    analyzer_mod: types.ModuleType, monkeypatch,
) -> None:
    """A row whose vector is BELOW the embedding-space-compat floor (its space
    genuinely changed, e.g. a chunking/model bump) must RE-EMBED, not stamp —
    the stored vector is in a stale space."""
    # current = 3, compat floor = 2 → a stored rev-1 vector is in a stale space.
    monkeypatch.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 3)
    monkeypatch.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 2)

    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    body = "def foo():\n    return 1\n"
    params = _func_params_with_deferred(embedder, body=body)

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo")
    stored_hash = _stored_hash_for(analyzer_mod, body)
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": 1,       # below the compat floor (2) → stale space
            "total_chunks": 1,
        }},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "vector below compat floor must re-embed"
    assert coll.data.update_calls == [], "no stamp-only across an embedding-space change"


def test_d1_over_budget_multichunk_reembeds_not_stamped(
    analyzer_mod: types.ModuleType, monkeypatch,
) -> None:
    """A chunkable entity stored as MULTI-chunk (over-budget) must re-embed on a
    revision bump (it needs a real chunk split), never a metadata stamp."""
    monkeypatch.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 2)
    monkeypatch.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 1)

    analyzer = _make_analyzer(analyzer_mod)
    embedder = _EmbedCounter()
    body = "def foo():\n    return 1\n"
    params = _func_params_with_deferred(embedder, body=body)

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo")
    # A multi-chunk stored row: the precheck hashes a single-chunk scratch copy,
    # so the content_hash will differ from a real multi-chunk canonical hash →
    # the fingerprint won't match AND the D1 classifier rejects total_chunks>1.
    # Store a hash that matches the single-chunk scratch so ONLY the multichunk
    # guard (not a hash mismatch) drives the re-embed.
    stored_hash = _stored_hash_for(analyzer_mod, body)
    coll = _FakeCollection(
        "T_CodeFunction",
        store={det_uuid: {
            "content_hash": stored_hash,
            "embed_revision": 1,
            "total_chunks": 3,         # over-budget → must re-embed
        }},
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert embedder.calls == 1, "over-budget multi-chunk must re-embed, not stamp"
    assert coll.data.update_calls == [], "no stamp-only for an over-budget entity"


# ─────────────────────────────────────────────────────────────────────
# v0.2.77 5c task 4: vectorless degrade emits ONE audit line + sets no vector
# ─────────────────────────────────────────────────────────────────────

class TestVectorlessDegradeLogging:
    def test_failed_embed_leaves_no_vector_and_logs_object(
        self, analyzer_mod, caplog
    ):
        cls = analyzer_mod.CodeGraphAnalyzer
        params = {"properties": {"full_name": "mod.doomed", "name": "doomed"}}

        def _boom():
            raise RuntimeError("CodeEmbed /embed returned HTTP 503: capacity")

        import logging
        with caplog.at_level(logging.WARNING):
            cls._run_deferred_embed_into(params, _boom)

        # No vector set → the write path will stamp _EMBED_REVISION_VECTORLESS.
        assert "vector" not in params
        # Exactly one audit line naming the object + the 503 failure.
        degraded = [r for r in caplog.records if "VECTORLESS" in r.getMessage()]
        assert len(degraded) == 1
        msg = degraded[0].getMessage()
        assert "mod.doomed" in msg
        assert "503" in msg

    def test_embedder_returns_none_logs_no_vector_reason(
        self, analyzer_mod, caplog
    ):
        cls = analyzer_mod.CodeGraphAnalyzer
        params = {"properties": {"path": "src/empty.py"}}
        import logging
        with caplog.at_level(logging.WARNING):
            cls._run_deferred_embed_into(params, lambda: None)
        assert "vector" not in params
        degraded = [r for r in caplog.records if "VECTORLESS" in r.getMessage()]
        assert len(degraded) == 1
        assert "no vector" in degraded[0].getMessage()

    def test_successful_embed_sets_vector_and_no_warning(
        self, analyzer_mod, caplog
    ):
        cls = analyzer_mod.CodeGraphAnalyzer
        params = {"properties": {"full_name": "mod.ok"}}
        import logging
        with caplog.at_level(logging.WARNING):
            cls._run_deferred_embed_into(params, lambda: [0.1, 0.2, 0.3])
        # Vector set (may be wrapped in a named-vector slot dict in dual mode).
        assert "vector" in params
        shaped = params["vector"]
        if isinstance(shaped, dict):
            assert [0.1, 0.2, 0.3] in shaped.values()
        else:
            assert shaped == [0.1, 0.2, 0.3]
        degraded = [r for r in caplog.records if "VECTORLESS" in r.getMessage()]
        assert degraded == []
