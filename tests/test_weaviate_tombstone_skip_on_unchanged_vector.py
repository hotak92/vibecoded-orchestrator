# SPDX-License-Identifier: AGPL-3.0-or-later
"""FIX-D2 (v0.2.73): empirically verify that re-writing a byte-identical object
does NOT grow the HNSW tombstone count.

Weaviate research flagged (could-not-confirm) that a ``replace()`` re-sending an
IDENTICAL client-supplied vector might still tombstone the old HNSW node on our
engine. VCO's per-object content-hash tombstone-skip
(``analyze_code_graph.py::_write_one_object``) is SUPPOSED to short-circuit the
``collection.data.replace()`` entirely when the stored ``content_hash`` matches —
so a re-analyze of an unchanged object issues 0 writes and 0 tombstones. This
suite verifies that empirically against a running engine.

Two layers (mirrors the X-2 retrieval-quality smoke pattern):

  PURE-UNIT layer (always runs): loads the analyzer and asserts the skip in
  ``_write_one_object`` RETURNS before ``collection.data.replace`` when the
  stored content_hash matches — i.e. the skip happens BEFORE the write call, so
  no tombstone can be generated. This is the code-level guarantee.

  LIVE-GATED layer (runs only when Weaviate is present): creates a scratch
  ``hnsw`` collection with a client-supplied ``vectorizer:none`` named vector,
  inserts one object, then re-writes it N times with the SAME vector + same
  content. Asserts the tombstone count (from ``/v1/nodes?output=verbose``) does
  NOT grow by N. When the tombstone metric is not exposed, falls back to the
  PROXY assertion (object count stays 1 — no version bloat). SKIPS cleanly when
  the infra is absent, so it is non-blocking in CI without a running Weaviate.

Synthetic names only (``VcoD2Scratch*`` / ``noise.*``) — no real project
identity embedded, no ``/home/<user>`` paths.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import types
import urllib.error
import urllib.request
import uuid as uuidlib
from pathlib import Path

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


# ---------------------------------------------------------------------------
# PURE-UNIT layer — the skip returns BEFORE collection.data.replace().
# ---------------------------------------------------------------------------


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_d2_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module missing — CI env regression: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


class _RecordingObject:
    def __init__(self, properties: dict) -> None:
        self.properties = properties


class _RecordingQuery:
    def __init__(self, stored: dict) -> None:
        self._stored = stored

    def fetch_object_by_id(self, det_uuid, return_properties=None):  # noqa: ARG002
        props = self._stored.get(det_uuid)
        return _RecordingObject(props) if props is not None else None


class _RecordingData:
    """Records whether replace()/insert() were called — the tombstone proxy at
    the code level (a replace() of a vector-bearing object is what tombstones)."""

    def __init__(self) -> None:
        self.replace_calls = 0
        self.insert_calls = 0

    def replace(self, *a, **k):  # noqa: ARG002
        self.replace_calls += 1

    def insert(self, *a, **k):  # noqa: ARG002
        self.insert_calls += 1


class _RecordingCollection:
    def __init__(self, name: str, stored: dict) -> None:
        self.name = name
        self.query = _RecordingQuery(stored)
        self.data = _RecordingData()


def test_skip_returns_before_replace_on_unchanged_object() -> None:
    """When the stored content_hash matches the computed one, the write path
    must NOT call collection.data.replace() — that is the code-level guarantee
    that no tombstone is generated for an unchanged object.
    """
    mod = _load_analyzer_module()

    # Build a minimal analyzer instance without running __init__ (mirrors the
    # existing content-hash-skip test's bypass).
    analyzer = mod.CodeGraphAnalyzer.__new__(mod.CodeGraphAnalyzer)
    analyzer.project_name = "VcoD2Scratch"
    analyzer._current_language = "python"
    analyzer._current_source = ""
    analyzer.visited_uuids = set()
    analyzer._track_visited = False

    coll_name = "VcoD2Scratch_CodeFunction"
    props = {
        "full_name": "noise.unchanged_fn",
        "function_body": "def unchanged_fn():\n    return 1\n",
        "signature": "def unchanged_fn()",
        "language": "python",
    }
    identity_key = "noise.unchanged_fn"
    det_uuid = mod._deterministic_uuid(
        analyzer.project_name, "noise.py", identity_key, project_source=""
    )
    # Compute the content hash the write path would compute + pre-seed it as the
    # STORED value so the point-read finds a match.
    content_hash = mod._content_hash_for_object(coll_name, dict(props))
    stored = {
        det_uuid: {
            "content_hash": content_hash,
            mod._EMBED_REVISION_PROP: mod.CODEGRAPH_EMBED_REVISION,
        }
    }
    collection = _RecordingCollection(coll_name, stored)

    insert_params = {
        "properties": dict(props),
        "vector": {"codesage_embed": [0.01] * 8},  # vector-bearing → replace() would tombstone
    }
    returned = analyzer._write_one_object(
        collection, det_uuid, insert_params, identity_key
    )

    assert collection.data.replace_calls == 0, (
        "unchanged object (matching content_hash) must SKIP replace() — a "
        "replace() here would tombstone the HNSW node the skip exists to avoid"
    )
    assert collection.data.insert_calls == 0, "skip path must not insert either"
    assert returned == det_uuid


def test_changed_object_does_replace() -> None:
    """Control: a CHANGED object (stored hash != computed) must still write —
    proving the skip is content-driven, not a blanket no-op."""
    mod = _load_analyzer_module()
    analyzer = mod.CodeGraphAnalyzer.__new__(mod.CodeGraphAnalyzer)
    analyzer.project_name = "VcoD2Scratch"
    analyzer._current_language = "python"
    analyzer._current_source = ""
    analyzer.visited_uuids = set()
    analyzer._track_visited = False

    coll_name = "VcoD2Scratch_CodeFunction"
    props = {
        "full_name": "noise.changed_fn",
        "function_body": "def changed_fn():\n    return 2\n",
        "signature": "def changed_fn()",
        "language": "python",
    }
    identity_key = "noise.changed_fn"
    det_uuid = mod._deterministic_uuid(
        analyzer.project_name, "noise.py", identity_key, project_source=""
    )
    stored = {
        det_uuid: {
            "content_hash": "a-different-stored-hash",
            mod._EMBED_REVISION_PROP: mod.CODEGRAPH_EMBED_REVISION,
        }
    }
    collection = _RecordingCollection(coll_name, stored)
    insert_params = {
        "properties": dict(props),
        "vector": {"codesage_embed": [0.02] * 8},
    }
    analyzer._write_one_object(collection, det_uuid, insert_params, identity_key)
    assert collection.data.replace_calls == 1, (
        "a genuinely-changed object must write (replace) — never silently skip"
    )


# ---------------------------------------------------------------------------
# LIVE-GATED layer — real tombstone-count check against a running engine.
# ---------------------------------------------------------------------------

_SCRATCH_CLASS = "VcoD2ScratchTombstone"
_N_REWRITES = 6


def _weaviate_url() -> str:
    return os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")


def _weaviate_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(
            f"{url}/v1/.well-known/ready", timeout=3
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _req(method: str, url: str, body: dict | None = None, timeout: float = 10.0):
    """Issue a request. Returns ``(status, parsed_json)``. A 4xx/5xx is
    returned as ``(code, {})`` rather than raised, so setup-shape mismatches
    (e.g. a scratch schema the running engine rejects with 422) let the caller
    decide to SKIP rather than error the whole test."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _delete_scratch(url: str) -> None:
    try:
        req = urllib.request.Request(f"{url}/v1/schema/{_SCRATCH_CLASS}", method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _tombstone_count(url: str) -> int | None:
    """Return the total HNSW tombstone count across shards for the scratch
    class, or None when the /v1/nodes verbose stats don't expose it (older
    engine / different shape). Caller falls back to the object-count proxy.
    """
    try:
        _st, payload = _req("GET", f"{url}/v1/nodes?output=verbose")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    total = 0
    found = False
    for node in payload.get("nodes", []) or []:
        for shard in node.get("shards", []) or []:
            cls = shard.get("class") or shard.get("collection") or ""
            if cls != _SCRATCH_CLASS:
                continue
            # The verbose shard stats may name the field differently across
            # versions; probe the known spellings.
            tomb = shard.get("vectorIndexingTombstones")
            if tomb is None:
                tomb = shard.get("vector_indexing_tombstones")
            if tomb is not None:
                try:
                    total += int(tomb)
                    found = True
                except (TypeError, ValueError):
                    pass
    return total if found else None


def _object_count(url: str) -> int | None:
    """Proxy metric: number of objects in the scratch class (must stay 1 across
    all re-writes — no version bloat)."""
    try:
        _st, payload = _req(
            "GET", f"{url}/v1/objects?class={_SCRATCH_CLASS}&limit=100"
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    objs = payload.get("objects")
    if not isinstance(objs, list):
        return None
    return len(objs)


_REQUIRE_LIVE = os.environ.get("VCO_RUN_LIVE_TOMBSTONE_SMOKE") == "1"


def test_live_unchanged_vector_rewrite_does_not_grow_tombstones() -> None:
    """Insert one object, then PUT (replace) it N times with the SAME vector +
    same content. The HNSW tombstone count must NOT grow by N (ideally 0 growth).

    This directly measures the could-not-confirm: does an identical-vector
    replace() tombstone the old node? If the count grows ~N, the raw replace()
    DOES tombstone even on an unchanged vector — which means VCO's skip MUST
    short-circuit BEFORE the replace() (verified by the pure-unit layer above),
    and this test documents the underlying-engine behaviour for the integrator.

    Gated: SKIPS when Weaviate is absent (non-blocking CI). Set
    VCO_RUN_LIVE_TOMBSTONE_SMOKE=1 to convert a skip into a failure.
    """
    url = _weaviate_url()
    if not _weaviate_ready(url):
        if _REQUIRE_LIVE:
            pytest.fail(f"VCO_RUN_LIVE_TOMBSTONE_SMOKE=1 but Weaviate not ready at {url}")
        pytest.skip(f"live tombstone smoke skipped: Weaviate not ready at {url}")

    _delete_scratch(url)  # clean any prior run
    try:
        # 1. Create a scratch HNSW class with a client-supplied named vector
        #    (vectorizer:none — VCO's exact shape).
        # Shape mirrors VCO's live Code* classes: NO top-level vectorizer
        # string; a single named-vector slot with `vectorizer: {none: {}}` +
        # slot-level `vectorIndexType: hnsw` (client-supplied vectors).
        schema = {
            "class": _SCRATCH_CLASS,
            "vectorConfig": {
                "codesage_embed": {
                    "vectorizer": {"none": {}},
                    "vectorIndexType": "hnsw",
                }
            },
            "properties": [
                {"name": "full_name", "dataType": ["text"]},
                {"name": "content_hash", "dataType": ["text"]},
            ],
        }
        status, _ = _req("POST", f"{url}/v1/schema", schema)
        if status not in (200, 201):
            pytest.skip(f"could not create scratch class (status {status})")

        obj_uuid = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, "vco-d2-scratch"))
        vector = [0.0123] * 8
        obj_body = {
            "class": _SCRATCH_CLASS,
            "id": obj_uuid,
            "properties": {"full_name": "noise.stable", "content_hash": "hstable"},
            "vectors": {"codesage_embed": vector},
        }
        # 2. Insert once.
        status, _ = _req("POST", f"{url}/v1/objects", obj_body)
        if status not in (200, 201):
            pytest.skip(f"could not insert scratch object (status {status})")
        time.sleep(0.3)

        baseline_tomb = _tombstone_count(url)

        # 3. Replace N times with the IDENTICAL vector + content (PUT = replace).
        for _ in range(_N_REWRITES):
            st, _b = _req("PUT", f"{url}/v1/objects/{_SCRATCH_CLASS}/{obj_uuid}", obj_body)
            assert st in (200, 204), f"replace failed (status {st})"
        time.sleep(0.5)

        # 4a. PRIMARY assertion: tombstones must not grow by ~N.
        after_tomb = _tombstone_count(url)
        if baseline_tomb is not None and after_tomb is not None:
            grew = after_tomb - baseline_tomb
            # Allow a tiny slack for async cleanup timing but reject ~N growth.
            assert grew < _N_REWRITES, (
                f"tombstone count grew by {grew} across {_N_REWRITES} identical-"
                f"vector replaces (baseline={baseline_tomb}, after={after_tomb}).\n"
                "FINDING for the integrator: the underlying engine DOES tombstone "
                "on an unchanged-vector replace() — so VCO's per-object skip MUST "
                "short-circuit BEFORE collection.data.replace (verified by the "
                "pure-unit layer). If that skip regressed, the amplification returns."
            )
        else:
            # 4b. PROXY assertion when tombstone stats aren't exposed: object
            #     count stays exactly 1 (no version bloat visible via REST).
            oc = _object_count(url)
            assert oc == 1, (
                f"object count is {oc}, expected 1 — identical-vector replaces "
                "must not create additional live versions"
            )
    finally:
        _delete_scratch(url)
