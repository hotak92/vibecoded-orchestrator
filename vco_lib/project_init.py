"""Project init helpers — single source of truth for sanitization, schema,
and collection-name derivation across Python and Rust.

This module was extracted from install.py in PR 2 of the project-init/update
overhaul (see `.claude/context/plans/project-init-and-update-overhaul-2026-05-01.md`
in claude-orchestrator).

Original locations in install.py (pre-extraction):
    _SAFE_CLASS_RE                  — install.py:4273
    _derive_project_kg_name         — install.py:4276
    _derive_project_dev_name        — install.py:4296
    _kg_class_definition            — install.py:4233
    _development_class_definition   — install.py:4260
    _named_vector_config            — install.py:4212
    _detect_kg_schema_drift         — install.py:4562
    _rebuild_collections            — install.py:4682

CLI usage (called by Rust via subprocess):
    python -m vco_lib.project_init derive --name <project_name> --json

    Output: JSON-only on stdout; logs to stderr.

Public API:
    sanitize_for_weaviate_class(name)         — single sanitizer (replaces
                                                Python _derive_project_kg_name
                                                AND Rust sanitize_kg_collection)
    derive_project_collection_names(name)     — canonical name dict
    derive_project_kg_name(name)              — name-based variant
    derive_project_dev_name(name)             — name-based variant
    kg_class_definition(name)                 — Weaviate KG schema dict
    development_class_definition(name)        — Weaviate Dev schema dict
    named_vector_config()                     — three named-vector slots
    detect_kg_schema_drift(url, kg_collection) — drift probe
    rebuild_collections(args)                  — drop+recreate (PR 3 will
                                                replace with migrate_collections)

Internal aliases (path-based, for back-compat with install.py callers):
    _derive_project_kg_name(project_root)
    _derive_project_dev_name(project_root)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Default Weaviate port (mirrors install.DEFAULT_WEAVIATE_PORT, kept here
# so this module is import-free of install.py).
DEFAULT_WEAVIATE_PORT = 8081

# Sanitizer regex: split on any non-alphanumeric run.
_SAFE_CLASS_RE = re.compile(r"[^A-Za-z0-9]+")

# Fallback prefix when a project name has no usable alphanumeric characters
# or starts with a digit. Lowercase `vct_` is intentional — Weaviate
# capitalizes the first letter on POST regardless, and the prefix flags
# the class as installer-managed.
_FALLBACK_PREFIX = "vct"


# ---------------------------------------------------------------------------
# Public sanitizer (single source of truth, replaces Rust + Python copies)
# ---------------------------------------------------------------------------


def sanitize_for_weaviate_class(project_name: str) -> str:
    """PascalCase a project name into a Weaviate class basename.

    Single source of truth across languages — replaces both the Python
    helper `_derive_project_kg_name` (path-based) and the Rust helper
    `sanitize_kg_collection`. Rust callers must subprocess this module
    via `python -m vco_lib.project_init derive --name <n> --json`.

    Rules (matching the existing install.py behavior):
      1. Split on any non-alphanumeric run (`-`, `_`, space, etc.).
      2. PascalCase each surviving part (uppercase first letter, keep rest).
      3. Concatenate.
      4. If nothing survives OR the result starts with a digit (invalid
         Weaviate class name), fall back to "vct".

    Note on non-ASCII: the regex `[^A-Za-z0-9]+` treats any non-ASCII
    character as a separator, so `étude` → `["tude"]` → `"Tude"` (the
    `é` is stripped, not preserved). This matches the existing
    install.py `_derive_project_kg_name` behavior exactly. Unicode-aware
    sanitization is out of scope for PR 2 — would require an explicit
    migration plan for any project that already exists with a stripped
    name.
    """
    base = project_name or ""
    parts = [p for p in _SAFE_CLASS_RE.split(base) if p]
    if not parts:
        return _FALLBACK_PREFIX
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or not pascal[0].isalpha():
        return _FALLBACK_PREFIX
    return pascal


def derive_project_kg_name(project_name: str) -> str:
    """Public: derive a per-project KG class name from a project name string.

    Equivalent to the path-based `_derive_project_kg_name(project_root)`
    but takes a name string directly — the form Rust subprocess callers
    need.
    """
    return f"{sanitize_for_weaviate_class(project_name)}_KnowledgeGraph"


def derive_project_dev_name(project_name: str) -> str:
    """Public: derive a per-project Development collection name from a
    project name string.
    """
    return f"{sanitize_for_weaviate_class(project_name)}_Development"


def derive_project_collection_names(project_name: str) -> dict:
    """Canonical collection-name dict for a project.

    Returns:
        {
          "kg_collection":            "<sanitized>_KnowledgeGraph",
          "development_collection":   "<sanitized>_Development",   # uppercase D
          "project_name":             <raw, not sanitized>,
          "shared_kg_collection":     "VibeCodedTools_KnowledgeGraph",
          "kg_basename":              "<sanitized>",
        }
    """
    basename = sanitize_for_weaviate_class(project_name)
    return {
        "kg_collection": f"{basename}_KnowledgeGraph",
        "development_collection": f"{basename}_Development",
        "project_name": project_name,
        "shared_kg_collection": "VibeCodedTools_KnowledgeGraph",
        "kg_basename": basename,
    }


# ---------------------------------------------------------------------------
# Internal aliases (path-based, kept for back-compat with install.py callers)
# ---------------------------------------------------------------------------


def _derive_project_kg_name(project_root: Path) -> str:
    """Internal alias — path-based KG name derivation.

    Mirrors the original install.py signature exactly so existing call sites
    don't break. Behavior matches the public `derive_project_kg_name`
    when fed `project_root.name`.
    """
    return derive_project_kg_name(project_root.name or "")


def _derive_project_dev_name(project_root: Path) -> str:
    """Internal alias — path-based Dev name derivation."""
    return derive_project_dev_name(project_root.name or "")


# ---------------------------------------------------------------------------
# Schema definitions (relocated from install.py:4212-4270)
# ---------------------------------------------------------------------------


def named_vector_config() -> dict:
    """Three named-vector slots: qwen3_embed (active default, 1024-dim),
    ollama_embed (legacy snowflake-arctic-embed2, 1024-dim, kept for back-
    compat), and openai_embed (1536-dim, for users who set OPENAI_API_KEY).

    Each slot has `vectorizer: none` so we feed pre-computed embeddings
    from the MCP server. Index type stays HNSW (Weaviate default for ANN).

    The MCP server's `sync_knowledge_graph.py` writes objects with at
    least one named vector populated; the others are filled lazily as the
    user pulls more embedding backends. Without this multi-vector config
    seeding fails with HTTP 422 ("collection configured without multiple
    named vectors, but received named vectors").
    """
    return {
        "qwen3_embed":  {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
        "ollama_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
        "openai_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
    }


def kg_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project KG collection.

    Sets `invertedIndexConfig.indexNullState=True` so the drift-detector
    in `detect_kg_schema_drift` sees a conformant schema on fresh
    installs. The drift detector requires it; previously the schema
    definition did not set it (silent drift on every fresh install).
    Adding it here closes that loop — see "Surprises" in the PR 2
    commit message.
    """
    return {
        "class": name,
        "description": "VibeCoded Tools knowledge graph collection",
        "vectorConfig": named_vector_config(),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "node_type", "dataType": ["text"]},
            {"name": "tags", "dataType": ["text[]"]},
            {"name": "links", "dataType": ["text[]"]},
            # WikiLink edges as nested objects: [[relationType::Target]]
            # → {relation_type: "uses", target_title: "Target"}.
            {
                "name": "typed_links",
                "dataType": ["object[]"],
                "nestedProperties": [
                    {"name": "relation_type", "dataType": ["text"]},
                    {"name": "target_title", "dataType": ["text"]},
                ],
            },
            {"name": "status", "dataType": ["text"]},
        ],
    }


def development_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project Development collection.

    Same `indexNullState=True` invariant as the KG schema (see
    `kg_class_definition`).
    """
    return {
        "class": name,
        "description": "VibeCoded Tools project documentation collection",
        "vectorConfig": named_vector_config(),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
        ],
    }


# Internal aliases preserving install.py's underscored names.
_named_vector_config = named_vector_config
_kg_class_definition = kg_class_definition
_development_class_definition = development_class_definition


# ---------------------------------------------------------------------------
# Schema-drift detection (relocated from install.py:4562)
# ---------------------------------------------------------------------------


def detect_kg_schema_drift(weaviate_url: str, kg_collection: str) -> tuple[bool, list[str]]:
    """Probe a running KG collection for today's required schema invariants.

    Returns (drift_detected, missing_features). drift_detected=True means
    the collection exists but lacks one or more invariants that the
    current code requires.

    Invariants checked (today's set; grow this list when new ones land):
      - 3 named-vector slots (qwen3_embed, ollama_embed, openai_embed)
      - inverted_index_config.index_null_state == True

    Both invariants CANNOT be retro-added on Weaviate ≤1.30 — the only
    fix is drop + re-ingest (or copy-with-vectors per PR 3).

    Failure-soft: if Weaviate is unreachable or the collection doesn't
    exist, returns (False, []).
    """
    try:
        import urllib.request
        # Weaviate v1 REST: GET /v1/schema/<class> returns the schema.
        req = urllib.request.Request(
            f"{weaviate_url.rstrip('/')}/v1/schema/{kg_collection}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return (False, [])
            schema = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return (False, [])

    missing: list[str] = []

    # Check named-vector slots
    vec_config = schema.get("vectorConfig") or {}
    expected_slots = {"qwen3_embed", "ollama_embed", "openai_embed"}
    actual_slots = set(vec_config.keys())
    if not expected_slots.issubset(actual_slots):
        gap = sorted(expected_slots - actual_slots)
        missing.append(f"named-vector slots (missing: {', '.join(gap)})")

    # Check index_null_state
    inv_idx = schema.get("invertedIndexConfig") or {}
    if not inv_idx.get("indexNullState", False):
        missing.append("index_null_state=True (required for stale-filter)")

    return (bool(missing), missing)


# Internal alias preserving install.py's underscored name.
_detect_kg_schema_drift = detect_kg_schema_drift


# ---------------------------------------------------------------------------
# Collection rebuild dispatch (relocated from install.py:4682)
#
# PR 3 will replace this with `migrate_collections` (smart copy/patch/
# rebuild dispatch — see weaviate-schema-port-research-2026-05-01.md).
# For PR 2, behavior is unchanged: drop the configured collections so
# the subsequent _ensure_collections + _seed_weaviate steps recreate
# them from scratch with today's schema.
# ---------------------------------------------------------------------------


def rebuild_collections(args, log_event=None) -> None:
    """Drop the KG and dev collections (when configured) so a subsequent
    seed step recreates them with today's schema and re-ingests from
    sources.

    Arguments:
        args:      argparse.Namespace from install.py — only `args` itself
                   is consumed by `weaviate.connect_to_custom`; we read
                   env vars for actual configuration.
        log_event: optional callable `(step, phase, detail, *, data=None)`
                   for forensic logging. install.py passes its
                   `_log_install_event`; CLI callers can pass None.

    Idempotent: silently skips collections that don't exist.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is not None:
            try:
                log_event(step, phase, detail, data=data)
            except TypeError:
                # Older log_event signatures may not accept `data` kwarg.
                log_event(step, phase, detail)

    print("[7b.1/10] Dropping KG + Dev collections for schema rebuild ...")
    _log("7b.1/10", "start", "schema-rebuild collection drop")

    try:
        import weaviate
        weaviate_url = os.environ.get("WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}")
        host = weaviate_url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(weaviate_url.rsplit(":", 1)[-1]) if ":" in weaviate_url else 8080
        client = weaviate.connect_to_custom(
            http_host=host,
            http_port=port,
            http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False,
            skip_init_checks=True,
        )
        try:
            for env_key, label in [
                ("KG_COLLECTION", "KG"),
                ("DEVELOPMENT_COLLECTION", "Dev"),
            ]:
                name = os.environ.get(env_key, "")
                if not name:
                    continue
                if client.collections.exists(name):
                    print(f"  Dropping {label}: {name} ...")
                    client.collections.delete(name)
                    _log(
                        "7b.1/10", "step",
                        f"dropped {label}: {name}",
                        data={"collection": name},
                    )
                else:
                    print(f"  {label} ({name}) does not exist — skipping drop")
        finally:
            client.close()
        _log("7b.1/10", "ok", "schema-rebuild drop complete")
    except Exception as e:
        print(f"  ! rebuild drop failed: {e}")
        print("    Update will continue but search may misbehave until")
        print("    you manually drop the collections and re-run --update.")
        _log("7b.1/10", "error", f"rebuild drop failed: {e}")


# Internal alias preserving install.py's underscored name.
_rebuild_collections = rebuild_collections


# ---------------------------------------------------------------------------
# Smart schema migration (PR 3) — copy-with-vectors instead of drop+re-embed.
#
# Verified against Weaviate 1.28.4 (see
# .claude/context/weaviate-schema-port-research-2026-05-01.md):
#   - vectorConfig slots:     PUT 422 "vector config is immutable"   → rebuild
#   - indexNullState:         PUT 422 "cannot be changed"            → rebuild
#   - properties:             POST /v1/schema/<class>/properties 200 → patch
#
# Solution: copy collection with iterator(include_vector=True) +
# batch.add_object(vector=<dict>, uuid=<orig>). Vectors round-trip
# byte-for-byte, no Ollama re-runs.
#
# Atomic-rename caveat: Weaviate has no class-rename endpoint. Use
# double-copy with stable name:
#   create <n>__staging w/target schema → copy old→staging → drop old →
#   recreate <n> w/target schema → copy staging→<n> → drop staging.
# Crash-recovery: detect orphan <n>__staging on next run and drop before
# replanning.
# ---------------------------------------------------------------------------


_STAGING_SUFFIX = "__staging"


@dataclass
class SchemaDelta:
    """Per-collection diff between actual and target schema.

    Attributes drive the migrate dispatch:
      legacy_single_vector → action=rebuild (no named vectors to copy)
      missing_vec_slots / indexNullState_needed → action=copy
      missing_props (only) → action=patch_props
      not_present → action=create
      none of the above → action=noop
    """
    not_present: bool = False
    legacy_single_vector: bool = False
    missing_vec_slots: list[str] = field(default_factory=list)
    indexNullState_needed: bool = False
    missing_props: list[dict] = field(default_factory=list)

    def any(self) -> bool:
        return (
            self.not_present
            or self.legacy_single_vector
            or bool(self.missing_vec_slots)
            or self.indexNullState_needed
            or bool(self.missing_props)
        )


def _weaviate_url_default() -> str:
    return os.environ.get("WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}")


def _http_request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """Thin urllib wrapper. Returns (status, body_bytes). Never raises on
    non-2xx — caller decides what to do.
    """
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        # Drain the error body so the caller can inspect Weaviate's reason.
        try:
            return (e.code, e.read())
        except Exception:
            return (e.code, b"")


def _fetch_schema(name: str, weaviate_url: Optional[str] = None) -> Optional[dict]:
    """GET /v1/schema/<name>. Returns dict on 200, None on 404, raises on
    network/transport error."""
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    status, body = _http_request("GET", f"{base}/v1/schema/{name}")
    if status == 200:
        return json.loads(body.decode("utf-8"))
    if status == 404:
        return None
    raise RuntimeError(f"GET /v1/schema/{name} → HTTP {status}: {body[:200]!r}")


def _list_classes(weaviate_url: Optional[str] = None) -> list[str]:
    """Return all class names currently defined on the server (for orphan
    detection). Returns [] on transport failure."""
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    try:
        status, body = _http_request("GET", f"{base}/v1/schema")
        if status != 200:
            return []
        payload = json.loads(body.decode("utf-8"))
        return [c.get("class", "") for c in payload.get("classes", []) if c.get("class")]
    except Exception:
        return []


def _expected_props_for(name: str, target_def_fn: Callable[[str], dict]) -> list[dict]:
    """Pull the target property list from the appropriate schema-def fn."""
    return list(target_def_fn(name).get("properties", []))


def _schema_delta(actual: dict, target: dict) -> SchemaDelta:
    """Compute per-collection action.

    Inputs are the raw schema dicts as returned by `GET /v1/schema/<class>`
    for `actual`, and as constructed by `kg_class_definition` /
    `development_class_definition` for `target`.
    """
    delta = SchemaDelta()

    actual_vec_config = actual.get("vectorConfig")
    target_vec_config = target.get("vectorConfig") or {}

    if not actual_vec_config:
        # Legacy single-vector format: schema has either vectorizer at
        # top level or no vectorConfig dict at all. We can't copy
        # individual named vectors → fall back to drop+re-ingest.
        delta.legacy_single_vector = True
        return delta

    expected_slots = set(target_vec_config.keys())
    actual_slots = set(actual_vec_config.keys())
    missing = sorted(expected_slots - actual_slots)
    if missing:
        delta.missing_vec_slots = missing

    inv_idx = actual.get("invertedIndexConfig") or {}
    target_inv = target.get("invertedIndexConfig") or {}
    if target_inv.get("indexNullState", False) and not inv_idx.get("indexNullState", False):
        delta.indexNullState_needed = True

    # Property check (additive only — Weaviate allows POST of new props).
    actual_prop_names = {p.get("name") for p in actual.get("properties", [])}
    missing_props: list[dict] = []
    for prop in target.get("properties", []):
        if prop.get("name") not in actual_prop_names:
            missing_props.append(prop)
    if missing_props:
        delta.missing_props = missing_props

    return delta


def _create_class(payload: dict, weaviate_url: Optional[str] = None) -> None:
    """POST /v1/schema. Idempotent: noop if class already exists with
    same name (we don't try to validate that the server-side def matches —
    callers should fetch first if they care).
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    name = payload.get("class")
    if not name:
        raise ValueError("class definition missing 'class' field")
    # Idempotency check.
    existing = _fetch_schema(name, weaviate_url=weaviate_url)
    if existing is not None:
        return
    status, body = _http_request("POST", f"{base}/v1/schema", body=payload, timeout=30)
    if status not in (200, 201):
        raise RuntimeError(f"POST /v1/schema ({name}) → HTTP {status}: {body[:300]!r}")


def _post_property(class_name: str, prop: dict, weaviate_url: Optional[str] = None) -> None:
    """POST /v1/schema/<class>/properties. Empirically confirmed mutable on
    Weaviate 1.28.4."""
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    status, body = _http_request(
        "POST", f"{base}/v1/schema/{class_name}/properties", body=prop, timeout=30,
    )
    if status not in (200, 201):
        raise RuntimeError(
            f"POST /v1/schema/{class_name}/properties ({prop.get('name')!r}) "
            f"→ HTTP {status}: {body[:300]!r}"
        )


def _delete_class(name: str, weaviate_url: Optional[str] = None) -> None:
    """DELETE /v1/schema/<name>. Idempotent (404 treated as success)."""
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    status, body = _http_request("DELETE", f"{base}/v1/schema/{name}", timeout=30)
    if status not in (200, 204, 404):
        raise RuntimeError(f"DELETE /v1/schema/{name} → HTTP {status}: {body[:300]!r}")


def _count_objects(name: str, weaviate_url: Optional[str] = None) -> int:
    """Count objects in a collection via v4 iterator. Lightweight: yields
    object metadata only (no vectors) so it scales for the recovery
    classification check. Returns 0 if collection missing or empty.
    """
    try:
        client = _connect_v4_client(weaviate_url=weaviate_url)
    except Exception:
        # Connection failure → can't classify; treat as 0 (caller will
        # log + bail rather than risk a destructive decision).
        return 0
    try:
        col = client.collections.get(name)
        n = 0
        # include_vector=False keeps payloads small.
        for _ in col.iterator(include_vector=False):
            n += 1
        return n
    except Exception:
        # Collection might not exist yet, or v4 client raises on missing
        # class. Let _fetch_schema be the existence oracle elsewhere;
        # here, missing → 0.
        return 0
    finally:
        client.close()


def _recover_or_drop_orphan_staging(
    name: str,
    target_def: Optional[dict] = None,
    *,
    weaviate_url: Optional[str] = None,
    log_event: Optional[Callable[..., None]] = None,
) -> str:
    """Crash-recovery for `<name>__staging` left by a prior failed migrate.

    Three branches based on the relative state of `<name>` vs
    `<name>__staging`:

    * RECOVER — `<name>` is missing or empty AND staging is populated.
      The staging holds the only surviving copy of user data (prior run
      died between `_delete_class(name)` at step 3 and the staging→new
      copy at step 5). We re-create `<name>` with `target_def` (target
      schema), copy staging→new, then drop staging. Returns "recovered".
      See `test_crash_recovery_recovers_data_when_name_deleted_mid_copy`.

    * SAFE-DROP — `<name>` exists with object count >= staging's count.
      Staging is genuinely orphaned (mid-step-2 crash where the source
      still held the canonical data). Safe to drop staging. Returns
      "dropped". See `test_crash_recovery_drops_orphan_when_name_already_intact`.

    * AMBIGUOUS — `<name>` exists with FEWER objects than staging. We
      cannot tell whether the staging is a partial copy mid-flight or
      contains data that `<name>` lost. Do NOT drop staging. Emit a loud
      forensic log and return "ambiguous"; the caller surfaces this as
      a deferral entry per HIGH-1 (see deferral integration sibling fix).

    Returns one of {"none", "recovered", "dropped", "ambiguous"}.

    DATA-LOSS WARNING (history): the prior `_drop_orphan_staging`
    unconditionally deleted `<name>__staging`, which destroyed the only
    surviving copy when a prior run died mid-step-5. Never re-introduce
    the unconditional path — see BLOCKER-1 in
    `.claude/context/pr3-6-7-integration-review-2026-05-01.md`.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            log_event(step, phase, detail)

    staging = f"{name}{_STAGING_SUFFIX}"
    if _fetch_schema(staging, weaviate_url=weaviate_url) is None:
        return "none"

    name_present = _fetch_schema(name, weaviate_url=weaviate_url) is not None
    name_count = _count_objects(name, weaviate_url=weaviate_url) if name_present else 0
    staging_count = _count_objects(staging, weaviate_url=weaviate_url)

    # RECOVER — staging is the only surviving copy.
    if (not name_present) or name_count == 0:
        if staging_count == 0:
            # Both empty — nothing to recover, just drop the orphan.
            _delete_class(staging, weaviate_url=weaviate_url)
            return "dropped"
        if target_def is None:
            # Caller didn't give us a target schema; we can't safely
            # recreate `<name>`. Loudly leave staging alone.
            _log(
                "7b.recover",
                "error",
                f"RECOVER NEEDED but no target schema supplied for {name}; "
                f"staging {staging} retains {staging_count} objects — "
                f"manual recovery: copy_collection_with_vectors("
                f"{staging!r}, {name!r}); delete_class({staging!r})",
                data={"collection": name, "staging": staging,
                      "staging_count": staging_count,
                      "branch": "ambiguous_no_target"},
            )
            return "ambiguous"
        # Build a copy of target_def with the canonical name in case the
        # caller passed a staging-flavoured definition.
        recover_def = dict(target_def)
        recover_def["class"] = name
        if name_present:
            # `<name>` exists but is empty — drop it so _create_class
            # (idempotent on existing) actually applies the target
            # schema fresh.
            _delete_class(name, weaviate_url=weaviate_url)
        _create_class(recover_def, weaviate_url=weaviate_url)
        copied = _copy_collection_with_vectors(
            staging, name, weaviate_url=weaviate_url,
        )
        if copied != staging_count:
            # Round-trip mismatch — keep staging in place for forensics.
            _log(
                "7b.recover",
                "error",
                f"recovery copy {staging}→{name} mismatch: "
                f"staging had {staging_count}, copied {copied}; "
                f"staging RETAINED for manual review",
                data={"collection": name, "staging": staging,
                      "staging_count": staging_count, "copied": copied,
                      "branch": "recover_mismatch"},
            )
            return "ambiguous"
        _delete_class(staging, weaviate_url=weaviate_url)
        _log(
            "7b.recover",
            "ok",
            f"recovered {copied} objects from {staging} → {name}",
            data={"collection": name, "staging": staging,
                  "objects_copied": copied, "branch": "recover"},
        )
        return "recovered"

    # SAFE-DROP — `<name>` has at least as many objects as staging.
    if name_count >= staging_count:
        _delete_class(staging, weaviate_url=weaviate_url)
        _log(
            "7b.recover",
            "ok",
            f"safe-drop orphan {staging} (name={name_count} >= staging={staging_count})",
            data={"collection": name, "staging": staging,
                  "name_count": name_count, "staging_count": staging_count,
                  "branch": "safe_drop"},
        )
        return "dropped"

    # AMBIGUOUS — `<name>` has fewer objects than staging.
    _log(
        "7b.recover",
        "error",
        f"AMBIGUOUS: {name} has {name_count} objects but staging "
        f"{staging} has {staging_count}; staging RETAINED — manual "
        f"recovery may be needed: inspect both, then "
        f"copy_collection_with_vectors({staging!r}, {name!r}) if staging "
        f"is canonical, else delete_class({staging!r})",
        data={"collection": name, "staging": staging,
              "name_count": name_count, "staging_count": staging_count,
              "branch": "ambiguous"},
    )
    return "ambiguous"


def _drop_orphan_staging(name: str, weaviate_url: Optional[str] = None) -> bool:
    """Backward-compat shim for the pre-BLOCKER-1 API. Calls
    `_recover_or_drop_orphan_staging` without a target schema, so it
    cannot recover — only the SAFE-DROP / no-staging branches are
    reachable. New code should call the recover-aware function with the
    target schema. Returns True iff staging was actually dropped (or
    recovered+dropped).
    """
    outcome = _recover_or_drop_orphan_staging(
        name, target_def=None, weaviate_url=weaviate_url,
    )
    return outcome in ("dropped", "recovered")


def _snapshot_collection_for_rebuild(
    name: str, weaviate_url: Optional[str] = None, sample_limit: int = 10,
) -> dict:
    """HIGH-4 (2026-05-01): snapshot object count + sample UUIDs BEFORE a
    rebuild action drops the collection. Used by ``migrate_collections``'s
    rebuild branch so a mid-rebuild Weaviate crash leaves a forensic trail
    in the install log + the deferral entry.

    Returns ``{"object_count": int|None, "sample_uuids": list[str]}``. Never
    raises — Weaviate already being unreachable means we have nothing to
    snapshot, and the caller proceeds with the drop+recreate semantic.
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    snapshot: dict = {"object_count": None, "sample_uuids": []}

    # Sample UUIDs via REST (simpler than GraphQL, no class-vs-quoted-name
    # escaping headaches).
    try:
        status, body = _http_request(
            "GET",
            f"{base}/v1/objects?class={name}&limit={sample_limit}",
            timeout=10,
        )
        if status == 200:
            payload = json.loads(body.decode("utf-8"))
            snapshot["sample_uuids"] = [
                obj.get("id", "") for obj in payload.get("objects", [])
                if obj.get("id")
            ]
    except Exception:
        pass

    # Object count via GraphQL Aggregate.
    try:
        gql = {"query": "{ Aggregate { %s { meta { count } } } }" % name}
        status, body = _http_request(
            "POST", f"{base}/v1/graphql", body=gql, timeout=10,
        )
        if status == 200:
            payload = json.loads(body.decode("utf-8"))
            agg = (
                payload.get("data", {})
                .get("Aggregate", {})
                .get(name, [])
            )
            if agg and isinstance(agg, list):
                count = agg[0].get("meta", {}).get("count")
                if isinstance(count, int):
                    snapshot["object_count"] = count
    except Exception:
        pass

    return snapshot


def _connect_v4_client(weaviate_url: Optional[str] = None):
    """Late-import weaviate-client v4 so non-migrate code paths don't pull
    the dependency. Returns a connected client."""
    import weaviate  # noqa: WPS433  (intentional lazy import)

    url = weaviate_url or _weaviate_url_default()
    host = url.replace("http://", "").replace("https://", "").split(":")[0]
    # Defensive port parse (works for "http://localhost:8081" or "https://x:9999/").
    try:
        port = int(url.rsplit(":", 1)[-1].split("/")[0])
    except ValueError:
        port = 8080
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=url.startswith("https://"),
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=False,
        skip_init_checks=True,
    )


def _copy_collection_with_vectors(
    src: str,
    dst: str,
    *,
    batch_size: int = 200,
    weaviate_url: Optional[str] = None,
) -> int:
    """Copy all objects from `src` to `dst` preserving UUIDs + named vectors.

    Uses weaviate-client v4 because raw HTTP doesn't support batch object
    writes with named vectors easily. Returns object count copied.

    Round-trip semantics (verified by research report):
      iterator(include_vector=True) yields obj.vector as dict[str, list[float]]
      batch.add_object(vector=<dict>, uuid=<orig>) re-imports byte-for-byte.
    """
    client = _connect_v4_client(weaviate_url=weaviate_url)
    try:
        src_col = client.collections.get(src)
        dst_col = client.collections.get(dst)
        copied = 0
        with dst_col.batch.dynamic() as bw:
            for obj in src_col.iterator(include_vector=True):
                # `obj.vector` is dict[str, list[float]] for named-vector
                # collections, list[float] for legacy single-vector. We
                # only copy if it's a dict (named-vector); single-vector
                # was already gated out by the legacy_single_vector
                # delta path.
                vec = obj.vector
                if isinstance(vec, list):
                    # Defensive: shouldn't happen because dispatcher
                    # routes legacy_single_vector → rebuild, not copy.
                    raise RuntimeError(
                        f"copy refused: src={src} returned legacy single "
                        "vector — should have been routed to rebuild"
                    )
                # Filter out unpopulated slots: when a named-vector slot
                # is configured but no vector was ever stored for that
                # object, Weaviate's iterator returns it as `[]`. Passing
                # `[]` back to add_object triggers
                # `WeaviateInvalidInputError('Invalid vectors: [].')`.
                # Drop the empty entries so only populated slots round-
                # trip; the destination's missing slots stay empty (same
                # observable state as the source).
                vec_clean = {k: v for k, v in vec.items() if v}
                bw.add_object(
                    properties=obj.properties,
                    uuid=obj.uuid,
                    vector=vec_clean,
                )
                copied += 1
                # Manual flush every batch_size to bound memory + give
                # Weaviate predictable backpressure.
                if copied % batch_size == 0:
                    # batch.dynamic auto-flushes; explicit flush is
                    # informational only. Continue.
                    pass
        # Surface any failed objects from the batch (rare but possible).
        failed = dst_col.batch.failed_objects
        if failed:
            raise RuntimeError(
                f"copy {src}→{dst}: {len(failed)} failed objects, "
                f"first error: {failed[0].message!r}"
            )
        return copied
    finally:
        client.close()


def _classify_action(delta: SchemaDelta) -> str:
    """Translate a SchemaDelta into one of: noop / create / rebuild / copy / patch_props.

    Order matters — same as the algorithm in the research report.
    """
    if delta.not_present:
        return "create"
    if not delta.any():
        return "noop"
    if delta.legacy_single_vector:
        return "rebuild"
    if delta.missing_vec_slots or delta.indexNullState_needed:
        return "copy"
    if delta.missing_props:
        return "patch_props"
    return "rebuild"  # unhandled — escape to drop+re-embed


def _build_plan(
    args,
    *,
    weaviate_url: Optional[str] = None,
    schema_fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[dict]:
    """Compute the action plan for KG + Dev collections from env vars.

    schema_fetcher injection point exists for unit tests that want to
    feed fake schema responses.
    """
    fetcher = schema_fetcher or (lambda n: _fetch_schema(n, weaviate_url=weaviate_url))
    plan: list[dict] = []
    pairs = [
        ("KG_COLLECTION", kg_class_definition),
        ("DEVELOPMENT_COLLECTION", development_class_definition),
    ]
    for env_key, target_def_fn in pairs:
        name = os.environ.get(env_key, "")
        if not name:
            continue
        actual = fetcher(name)
        target = target_def_fn(name)
        if actual is None:
            delta = SchemaDelta(not_present=True)
        else:
            delta = _schema_delta(actual, target)
        action = _classify_action(delta)
        # Force-rebuild override: skip smart path entirely.
        if getattr(args, "force_rebuild", False) and action in ("copy", "patch_props", "noop"):
            action = "rebuild"
        plan.append({
            "env_key": env_key,
            "collection": name,
            "action": action,
            "target": target,
            "delta": delta,
        })
    return plan


def migrate_collections(
    args,
    *,
    dry_run: bool = False,
    weaviate_url: Optional[str] = None,
    log_event: Optional[Callable[..., None]] = None,
    schema_fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Smart per-collection schema migration. Replaces `rebuild_collections`'s
    drop-and-re-embed with: noop / patch_props / copy-with-vectors / rebuild.

    Caller contract: `args` must expose at minimum `force_rebuild` (bool).
    install.py callers pass the argparse Namespace; CLI callers construct
    a Namespace from --force-rebuild / --dry-run.

    Returns a result dict:
      {"plan": [{"collection", "action", "objects_copied", "elapsed_ms"}],
       "dry_run": bool,
       "errors": [{"collection", "action", "error"}]}
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is not None:
            try:
                log_event(step, phase, detail, data=data)
            except TypeError:
                log_event(step, phase, detail)

    weaviate_url = weaviate_url or _weaviate_url_default()
    result: dict = {"plan": [], "dry_run": dry_run, "errors": []}

    # Crash-recovery: classify and recover/drop orphan staging classes from
    # prior failed runs. We inspect the env-configured collections only;
    # that's enough for the common case (KG + Dev). A foreign orphan
    # (different project) is left alone — nothing to do with us.
    #
    # Pass the target schema so that if `<name>` is gone but staging holds
    # the only surviving copy, we can recover it (BLOCKER-1 fix).
    _recover_targets = {
        "KG_COLLECTION": kg_class_definition,
        "DEVELOPMENT_COLLECTION": development_class_definition,
    }
    for env_key, target_fn in _recover_targets.items():
        name = os.environ.get(env_key, "")
        if not name:
            continue
        try:
            target_def = target_fn(name)
            outcome = _recover_or_drop_orphan_staging(
                name, target_def=target_def,
                weaviate_url=weaviate_url, log_event=log_event,
            )
            if outcome == "recovered":
                _log("7b.recover", "ok",
                     f"recovered data from orphan staging: {name}{_STAGING_SUFFIX} → {name}",
                     data={"collection": name, "staging": name + _STAGING_SUFFIX,
                           "branch": "recover"})
            elif outcome == "dropped":
                _log("7b.recover", "ok",
                     f"dropped orphan staging: {name}{_STAGING_SUFFIX}",
                     data={"collection": name + _STAGING_SUFFIX, "branch": "safe_drop"})
            elif outcome == "ambiguous":
                # AMBIGUOUS staging — surface as an error so the caller's
                # deferral integration (HIGH-1 sibling fix) treats it as a
                # blocker requiring human review. Don't fail the whole
                # migrate; the orphan is retained for inspection.
                result["errors"].append({
                    "collection": name,
                    "action": "recover",
                    "error": (f"ambiguous orphan staging {name}{_STAGING_SUFFIX} — "
                              "manual recovery required; staging RETAINED"),
                })
            # outcome == "none" → no staging present, nothing to log.
        except Exception as e:
            _log("7b.recover", "error",
                 f"orphan-staging recovery failed for {name}: {e}",
                 data={"collection": name + _STAGING_SUFFIX, "error": str(e)})
            result["errors"].append({
                "collection": name,
                "action": "recover",
                "error": str(e),
            })

    # Build the plan.
    try:
        plan = _build_plan(
            args, weaviate_url=weaviate_url, schema_fetcher=schema_fetcher,
        )
    except Exception as e:
        _log("7b.plan", "error", f"plan build failed: {e}",
             data={"error": str(e)})
        result["errors"].append({
            "collection": None, "action": "plan", "error": str(e),
        })
        return result

    for entry in plan:
        _log("7b.plan", "ok",
             f"{entry['collection']}: {entry['action']}",
             data={"collection": entry["collection"], "action": entry["action"]})

    if dry_run:
        for entry in plan:
            # Human log → stderr; structured plan → returned dict (the CLI
            # caller writes JSON to stdout).
            print(f"  WOULD {entry['action']:13s} {entry['collection']}",
                  file=sys.stderr)
            result["plan"].append({
                "collection": entry["collection"],
                "action": entry["action"],
                "objects_copied": 0,
                "elapsed_ms": 0,
            })
        return result

    # Execute.
    for entry in plan:
        name = entry["collection"]
        action = entry["action"]
        target = entry["target"]
        delta = entry["delta"]
        t_start = time.monotonic()
        objects_copied = 0

        try:
            _log(f"7b.{action}", "start", f"{name}: {action}",
                 data={"collection": name, "action": action})
            print(f"  {action:13s} {name}", file=sys.stderr)

            if action == "noop":
                pass

            elif action == "create":
                _create_class(target, weaviate_url=weaviate_url)

            elif action == "patch_props":
                for prop in delta.missing_props:
                    _post_property(name, prop, weaviate_url=weaviate_url)

            elif action == "copy":
                staging = f"{name}{_STAGING_SUFFIX}"
                staging_def = dict(target)
                staging_def["class"] = staging
                # 1. create staging w/target schema
                _create_class(staging_def, weaviate_url=weaviate_url)
                # 2. copy old → staging
                copied_a = _copy_collection_with_vectors(
                    name, staging, weaviate_url=weaviate_url,
                )
                # 3. drop old
                _delete_class(name, weaviate_url=weaviate_url)
                # 4. recreate name w/target schema
                _create_class(target, weaviate_url=weaviate_url)
                # 5. copy staging → name
                copied_b = _copy_collection_with_vectors(
                    staging, name, weaviate_url=weaviate_url,
                )
                if copied_a != copied_b:
                    raise RuntimeError(
                        f"copy round-trip mismatch: old→staging={copied_a}, "
                        f"staging→new={copied_b}"
                    )
                # 6. drop staging
                _delete_class(staging, weaviate_url=weaviate_url)
                objects_copied = copied_b

            elif action == "rebuild":
                # Fall back to today's drop+re-embed path. We delete the
                # collection here; the caller's _ensure_collections +
                # _seed_weaviate handle recreate + re-ingest.
                if _fetch_schema(name, weaviate_url=weaviate_url) is not None:
                    # HIGH-4 (2026-05-01): snapshot BEFORE the destructive
                    # _delete_class so a mid-rebuild crash leaves a forensic
                    # trail (object count + sample UUIDs) in install.jsonl.
                    _snap = _snapshot_collection_for_rebuild(
                        name, weaviate_url=weaviate_url,
                    )
                    _log("7b.rebuild", "snapshot",
                         f"{name}: pre-drop snapshot",
                         data={"collection": name,
                               "object_count": _snap["object_count"],
                               "sample_uuids": _snap["sample_uuids"]})
                    _delete_class(name, weaviate_url=weaviate_url)
            else:
                raise RuntimeError(f"unknown action: {action}")

            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            _log(f"7b.{action}", "ok", f"{name}: {action}",
                 data={"collection": name, "action": action,
                       "objects_copied": objects_copied,
                       "elapsed_ms": elapsed_ms})
            result["plan"].append({
                "collection": name,
                "action": action,
                "objects_copied": objects_copied,
                "elapsed_ms": elapsed_ms,
            })

        except Exception as e:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            err_msg = f"{type(e).__name__}: {e}"
            _log(f"7b.{action}", "error", f"{name}: {action}: {err_msg}",
                 data={"collection": name, "action": action,
                       "error": err_msg, "elapsed_ms": elapsed_ms})
            print(f"    ! migrate failed: {err_msg}", file=sys.stderr)

            # HIGH-5: best-effort rollback for the copy action. If the
            # failure occurred between step 3 (delete `<name>`) and step 5
            # (staging→new copy completes), `<name>` is gone but staging
            # holds the data. Try to recover; if recovery fails, leave
            # staging in place + log explicit manual recovery instructions.
            if action == "copy":
                staging = f"{name}{_STAGING_SUFFIX}"
                staging_present = False
                name_present = True
                try:
                    staging_present = _fetch_schema(
                        staging, weaviate_url=weaviate_url,
                    ) is not None
                    name_present = _fetch_schema(
                        name, weaviate_url=weaviate_url,
                    ) is not None
                except Exception:
                    # Existence probe failed — fall through to log only.
                    pass

                if staging_present and not name_present:
                    # Try inline recovery using the same target schema.
                    try:
                        recovery_def = dict(target)
                        recovery_def["class"] = name
                        _create_class(
                            recovery_def, weaviate_url=weaviate_url,
                        )
                        recovered = _copy_collection_with_vectors(
                            staging, name, weaviate_url=weaviate_url,
                        )
                        _delete_class(staging, weaviate_url=weaviate_url)
                        _log(
                            "7b.copy.rollback",
                            "ok",
                            f"recovered {recovered} objects from {staging} → {name} after copy failure",
                            data={"collection": name, "staging": staging,
                                  "objects_copied": recovered,
                                  "branch": "rollback_recovered"},
                        )
                        objects_copied = recovered
                    except Exception as e2:
                        # Recovery itself failed — leave staging in place.
                        _log(
                            "7b.copy.rollback",
                            "error",
                            (f"DATA IN STAGING; original error: {err_msg}; "
                             f"recovery error: {type(e2).__name__}: {e2}; "
                             f"manual recovery: copy_collection_with_vectors("
                             f"{staging!r}, {name!r}); delete_class({staging!r})"),
                            data={"collection": name, "staging": staging,
                                  "original_error": err_msg,
                                  "recovery_error": f"{type(e2).__name__}: {e2}",
                                  "branch": "rollback_failed"},
                        )
                elif staging_present and name_present:
                    # Both alive — staging may hold a partial or full copy.
                    # Don't auto-drop; surface manual instructions.
                    _log(
                        "7b.copy.rollback",
                        "error",
                        (f"DATA IN STAGING (both {name} and {staging} present); "
                         f"manual recovery: inspect both, then if staging is "
                         f"canonical run copy_collection_with_vectors("
                         f"{staging!r}, {name!r}) and delete_class({staging!r})"),
                        data={"collection": name, "staging": staging,
                              "original_error": err_msg,
                              "branch": "rollback_both_present"},
                    )

            result["errors"].append({
                "collection": name,
                "action": action,
                "error": err_msg,
            })
            result["plan"].append({
                "collection": name,
                "action": action,
                "objects_copied": objects_copied,
                "elapsed_ms": elapsed_ms,
            })

    return result


# Internal alias preserving install.py's underscored convention.
_migrate_collections = migrate_collections


# ---------------------------------------------------------------------------
# Collection bootstrap (PR 4) — POST schema with podman-restart soft-fail.
#
# Used by:
#   - launcher `create_project_v2` (subprocess via `bootstrap-collections`)
#   - install.py first-install / adopt-mode (eventually — currently
#     install.py has its own `_ensure_collections`; PR 5+ may dedupe).
#
# Idempotent: existence-checks each target before POST, so a re-run on a
# project whose collections already exist is a no-op (no errors).
#
# Soft-fail policy (per PR 4 spec):
#   1. If Weaviate unreachable, attempt `podman start weaviate_claude` (or
#      `docker start` fallback) once and wait up to 10s for healthy.
#   2. If still unreachable, write a `weaviate_unreachable_at_bootstrap`
#      deferral entry to `<project_folder>/.claude/context/UPDATE_DEFERRED.md`
#      and return success — NEVER block project creation. The hook
#      `ensure-containers.sh` is the second-line backstop for the next
#      Claude Code session.
#
# Shared KG (`VibeCodedTools_KnowledgeGraph`): created when missing
# regardless of any per-project SHARED_KG_OPT_OUT toggle. Per the
# coordinator's 2026-05-01 directive: every project ALWAYS reads the
# shared KG; the toggle is purely a runtime write-gate. Creation is not
# gated on it.
# ---------------------------------------------------------------------------


_SHARED_KG_NAME = "VibeCodedTools_KnowledgeGraph"
_DEFAULT_RESTART_CONTAINER = "weaviate_claude"


def _is_weaviate_reachable(weaviate_url: str, *, timeout: float = 5.0) -> bool:
    """Probe `/v1/.well-known/ready`. Returns True only on HTTP 200."""
    base = weaviate_url.rstrip("/")
    try:
        status, _body = _http_request(
            "GET", f"{base}/v1/.well-known/ready", timeout=timeout,
        )
        return status == 200
    except Exception:
        return False


def _attempt_container_restart(
    container_name: str = _DEFAULT_RESTART_CONTAINER,
    *,
    log_event: Optional[Callable[..., None]] = None,
) -> bool:
    """Try `podman start <name>` first, fall back to `docker start`.

    Returns True if the start command succeeded (which doesn't guarantee
    the service is HEALTHY yet — caller should follow up with a readiness
    probe). Returns False if both runtimes are missing or the start fails.
    """
    import shutil
    import subprocess as _sp

    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            log_event(step, phase, detail)

    for runtime in ("podman", "docker"):
        if shutil.which(runtime) is None:
            continue
        try:
            res = _sp.run(
                [runtime, "start", container_name],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode == 0:
                _log(
                    "7b.bootstrap.restart", "ok",
                    f"{runtime} start {container_name} succeeded",
                    data={"runtime": runtime, "container": container_name},
                )
                return True
            else:
                _log(
                    "7b.bootstrap.restart", "warn",
                    f"{runtime} start {container_name}: rc={res.returncode}: {res.stderr.strip()[:200]}",
                    data={"runtime": runtime, "container": container_name,
                          "stderr": res.stderr.strip()[:200]},
                )
        except Exception as e:
            _log(
                "7b.bootstrap.restart", "warn",
                f"{runtime} start {container_name} raised: {type(e).__name__}: {e}",
                data={"runtime": runtime, "error": str(e)[:200]},
            )
    return False


def _wait_for_weaviate_ready(
    weaviate_url: str, *, timeout: float = 10.0, interval: float = 0.5,
) -> bool:
    """Poll `_is_weaviate_reachable` until it returns True or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_weaviate_reachable(weaviate_url, timeout=2.0):
            return True
        time.sleep(interval)
    return False


def bootstrap_collections(
    project_name: str,
    weaviate_url: Optional[str] = None,
    *,
    dry_run: bool = False,
    kg_only: bool = False,
    project_folder: Optional[Path] = None,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """POST Weaviate schema for the per-project KG, Dev, and shared KG
    collections. Idempotent — existing classes are left untouched.

    Args:
        project_name: Raw project name (sanitization applied internally).
        weaviate_url: Override; defaults to WEAVIATE_URL env or
            http://localhost:8081.
        dry_run: Plan only — no Weaviate mutations.
        kg_only: Skip the per-project Development collection (used by
            tests and minimal-bootstrap scenarios). The shared KG is
            still created either way (per the coordinator directive: all
            projects always need read access to the shared KG).
        project_folder: When set, deferral entries (Weaviate-unreachable)
            land in `<project_folder>/.claude/context/UPDATE_DEFERRED.md`.
            When None, deferral writes are skipped (the caller is expected
            to handle the no-folder case — e.g. CLI run from a shell
            unrelated to any user project).
        log_event: Optional forensic logger compatible with install.py's
            `_log_install_event`.

    Returns a JSON-serialisable dict:
      {
        "weaviate_reachable": bool,
        "restart_attempted": bool,
        "restart_succeeded": bool,
        "deferred": bool,
        "dry_run": bool,
        "actions": [{"collection": str, "action": "create"|"exists"|"would-create", "ok": bool}],
        "errors": [{"collection": str, "error": str}],
      }

    Soft-fail contract: the function NEVER raises for transport errors or
    for individual collection creation failures. A non-empty `errors`
    array signals partial failure that the caller should surface (e.g.
    via `CreateProjectResult.warnings`), but the project create can still
    proceed.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            log_event(step, phase, detail)

    weaviate_url = weaviate_url or _weaviate_url_default()
    derived = derive_project_collection_names(project_name)
    result: dict = {
        "weaviate_reachable": False,
        "restart_attempted": False,
        "restart_succeeded": False,
        "deferred": False,
        "dry_run": bool(dry_run),
        "actions": [],
        "errors": [],
    }

    # 1. Reachability probe + soft restart on failure.
    reachable = _is_weaviate_reachable(weaviate_url)
    if not reachable and not dry_run:
        result["restart_attempted"] = True
        _log("7b.bootstrap", "warn",
             f"weaviate unreachable at {weaviate_url}, attempting restart",
             data={"weaviate_url": weaviate_url})
        if _attempt_container_restart(log_event=log_event):
            result["restart_succeeded"] = True
            reachable = _wait_for_weaviate_ready(weaviate_url, timeout=10.0)
    result["weaviate_reachable"] = reachable

    if not reachable:
        # Defer + return cleanly. Dry-run skips both the restart attempt
        # and the deferral write — it's a planning preview only.
        if dry_run:
            _log("7b.bootstrap", "warn",
                 "weaviate unreachable; would defer (dry-run, no write)",
                 data={"weaviate_url": weaviate_url})
            return result
        result["deferred"] = True
        _log("7b.bootstrap", "warn",
             "weaviate unreachable after restart attempt; writing deferral",
             data={"weaviate_url": weaviate_url})
        if project_folder is not None:
            try:
                _write_bootstrap_deferral(
                    Path(project_folder),
                    project_name=project_name,
                    weaviate_url=weaviate_url,
                    derived=derived,
                    kg_only=kg_only,
                )
            except Exception as e:
                _log("7b.bootstrap", "error",
                     f"deferral write failed: {type(e).__name__}: {e}",
                     data={"error": str(e)[:200]})
                result["errors"].append({
                    "collection": None,
                    "error": f"deferral write failed: {e}",
                })
        return result

    # 2. Build the target list.
    targets: list[tuple[str, dict]] = [
        (derived["kg_collection"],     kg_class_definition(derived["kg_collection"])),
    ]
    if not kg_only:
        targets.append(
            (derived["development_collection"],
             development_class_definition(derived["development_collection"])),
        )
    # Shared KG: always created when missing (per coordinator: shared KG is
    # READ by every project regardless of per-project opt-out, so creation
    # is unconditional). The opt-out toggle is purely a write-gate enforced
    # at MCP-call time, not a creation gate.
    targets.append((_SHARED_KG_NAME, kg_class_definition(_SHARED_KG_NAME)))

    # 3. Iterate: existence check + POST when missing.
    for name, definition in targets:
        try:
            existing = _fetch_schema(name, weaviate_url=weaviate_url)
        except Exception as e:
            _log("7b.bootstrap", "error",
                 f"schema probe for {name} failed: {type(e).__name__}: {e}",
                 data={"collection": name, "error": str(e)[:200]})
            result["errors"].append({
                "collection": name,
                "error": f"schema probe failed: {e}",
            })
            continue

        if existing is not None:
            result["actions"].append({
                "collection": name, "action": "exists", "ok": True,
            })
            _log("7b.bootstrap", "ok",
                 f"{name}: already exists",
                 data={"collection": name, "action": "exists"})
            continue

        if dry_run:
            result["actions"].append({
                "collection": name, "action": "would-create", "ok": True,
            })
            continue

        try:
            _create_class(definition, weaviate_url=weaviate_url)
            result["actions"].append({
                "collection": name, "action": "create", "ok": True,
            })
            _log("7b.bootstrap", "ok",
                 f"{name}: created with target schema",
                 data={"collection": name, "action": "create"})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("7b.bootstrap", "error",
                 f"{name}: create failed: {err}",
                 data={"collection": name, "error": err})
            result["actions"].append({
                "collection": name, "action": "create", "ok": False,
            })
            result["errors"].append({"collection": name, "error": err})

    return result


def _write_bootstrap_deferral(
    project_folder: Path,
    *,
    project_name: str,
    weaviate_url: str,
    derived: dict,
    kg_only: bool,
) -> None:
    """Emit a `weaviate_unreachable_at_bootstrap` deferral entry. Used by
    `bootstrap_collections` when Weaviate is down + restart fails.

    Lazy import of `vco_lib.deferral_report` so non-bootstrap code paths
    don't pull the module.
    """
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    cmd_lines = [
        "# 1. Bring Weaviate up.",
        "podman start weaviate_claude  # or: docker start weaviate_claude",
        "",
        "# 2. Re-run bootstrap (idempotent).",
        f"python -m vco_lib.project_init bootstrap-collections "
        f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
        f"--project-folder {str(project_folder)!r}",
    ]
    if kg_only:
        cmd_lines[-1] += " --kg-only"

    entry = DeferralEntry(
        condition_id="weaviate_unreachable_at_bootstrap",
        title="Weaviate collection bootstrap deferred",
        detected=(
            f"Weaviate at {weaviate_url} was unreachable during project "
            f"creation, and the auto-restart attempt did not bring it back. "
            f"The project's KG collections "
            f"({derived['kg_collection']}, "
            f"{derived['development_collection']}, {_SHARED_KG_NAME}) "
            f"have not been created. Knowledge-graph search and writes will "
            f"fail until Weaviate is up and bootstrap is re-run."
        ),
        why_deferred=(
            "Soft-fail policy: project creation must never block on "
            "Weaviate availability. The collections are created lazily on "
            "next bootstrap once Weaviate is healthy."
        ),
        command_to_apply="\n".join(cmd_lines),
        severity="warning",
        kg_node_refs=[
            "knowledge/concepts/weaviate-schema-evolution.md",
        ],
    )
    report = DeferralReport.read(project_folder)
    report.add_entry(entry)
    report.write(project_folder)


# ---------------------------------------------------------------------------
# Bundle install (PR 4) — copy hooks/scripts/agents/skills/settings/
# infrastructure into a user project folder.
#
# Single source of truth for the per-project bundle. `install.py` keeps
# its own copy logic for the orchestrator-clone case (in-place install);
# launcher `create_project_v2` calls THIS via subprocess for user-project
# bootstrap.
#
# Manifest-based update (PR 5 territory; PR 4 lays the foundation):
#   - First-install: skip-if-exists; preserves user customizations on
#     pre-existing folders that already had hand-rolled hooks.
#   - Update mode: hash-based drift detection. If installed file matches
#     a hash recorded in the manifest, OVERWRITE. Otherwise PRESERVE +
#     emit deferral. "Default to safety": when the manifest is missing
#     or the source/installed hashes both differ from manifest, treat as
#     user-modified and preserve.
#
# Manifest schema (`<folder>/.claude/.vco-manifest.json`):
#   {
#     "schema_version": 1,
#     "vco_version": "<orchestrator HEAD or release tag>",
#     "installed_at": "ISO-8601",
#     "files": {
#       "<rel-path-from-folder>": {
#         "sha256": "<hex>",            # hash of the SHIPPED source at
#                                       # install time (NOT the on-disk
#                                       # copy after user edits)
#         "source": "<rel-path>",       # path within orchestrator root
#       },
#     },
#   }
# ---------------------------------------------------------------------------


_MANIFEST_REL = Path(".claude") / ".vco-manifest.json"
_MANIFEST_SCHEMA_VERSION = 1


# Placeholder substitutions applied to agent .md files (mirrors
# install.py:5564). Skill .md files use the same map.
def _agent_subs(orchestrator_root: Path) -> dict[str, str]:
    return {
        "{{ORCHESTRATOR_ROOT}}": str(orchestrator_root),
        "{{PROJECTS_ROOT}}": str(orchestrator_root.parent),
        "{{HOME}}": str(Path.home()),
    }


def _hook_glob_for_os() -> str:
    """`*.sh` on Linux/macOS, `*.ps1` on Windows. Mirrors install.py:5641."""
    import platform
    return "*.ps1" if platform.system() == "Windows" else "*.sh"


def _settings_template_path(orchestrator_root: Path) -> Path:
    """Pick the OS-specific settings.json template file."""
    import platform
    name = (
        "settings.json.windows.template"
        if platform.system() == "Windows"
        else "settings.json.linux.template"
    )
    return orchestrator_root / "templates" / name


def _file_sha256(path: Path) -> str:
    """SHA256 hex digest of a file's bytes. Returns empty string if the
    file is missing."""
    import hashlib
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _bytes_sha256(data: bytes) -> str:
    """SHA256 hex digest of an in-memory byte string."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _read_manifest(folder: Path) -> dict:
    """Parse `.claude/.vco-manifest.json` if present. Returns
    `{"schema_version": ..., "files": {}}` on missing / unparseable file
    so callers can treat it uniformly."""
    target = folder / _MANIFEST_REL
    if not target.exists():
        return {"schema_version": _MANIFEST_SCHEMA_VERSION, "files": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"schema_version": _MANIFEST_SCHEMA_VERSION, "files": {}}
        if "files" not in data or not isinstance(data["files"], dict):
            data["files"] = {}
        return data
    except Exception:
        # Corrupt manifest — treat as missing to avoid blocking the install.
        return {"schema_version": _MANIFEST_SCHEMA_VERSION, "files": {}}


def _write_manifest_atomic(folder: Path, manifest: dict) -> None:
    """Atomic-write the manifest via tempfile + os.replace. Same pattern as
    `deferral_report.write`."""
    target = folder / _MANIFEST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), suffix=".tmp", prefix=".vco-manifest-",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _resolve_vco_version(orchestrator_root: Path) -> str:
    """Best-effort orchestrator version string for the manifest. Uses
    `git rev-parse --short HEAD` when available, falls back to "unknown".
    Never raises.
    """
    import subprocess as _sp
    try:
        res = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(orchestrator_root),
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            sha = res.stdout.strip()
            if sha:
                return sha
    except Exception:
        pass
    return "unknown"


@dataclass
class _BundleFileOp:
    """One copy unit for `install_project_bundle`.

    `dest_rel` is relative to `<folder>`. `source_abs` is the absolute path
    of the shipped file. `transform` is None for byte-copy or a callable
    `(bytes) -> bytes` for substitution (e.g. agent placeholder rewrites).
    `always_overwrite=True` for files that aren't user-customisable
    (e.g. hooks/_lib).
    """
    dest_rel: str
    source_abs: Path
    source_rel: str = ""  # rel to orchestrator_root, for manifest
    transform: Optional[Callable[[bytes], bytes]] = None
    always_overwrite: bool = False


def _enumerate_bundle_files(orchestrator_root: Path) -> list[_BundleFileOp]:
    """Build the list of files to install. OS-aware (hooks pick .sh vs .ps1).

    Layout:
      .claude/hooks/<name>.{sh,ps1}        from templates/hooks/  (skip _lib)
      .claude/hooks/_lib/<name>.{sh,ps1}   from templates/hooks/_lib/  (always overwrite)
      .claude/scripts/<name>               from templates/scripts/  (all flavours)
      .claude/agents/<name>.md             from templates/agents/free/  (with substitutions)
      .claude/skills/<rel>                 from templates/skills/<rel>  (recursive; .md substituted)
      infrastructure/<name>                from infrastructure/<name>   (only docker/podman compose)
    Settings template handled separately (smart-merge, not a plain copy).
    """
    ops: list[_BundleFileOp] = []
    templates = orchestrator_root / "templates"
    hook_glob = _hook_glob_for_os()

    # Hooks (top-level only — _lib handled below).
    hooks_src = templates / "hooks"
    if hooks_src.exists():
        for hook_file in sorted(hooks_src.glob(hook_glob)):
            if hook_file.parent.name == "_lib":
                continue
            ops.append(_BundleFileOp(
                dest_rel=str(Path(".claude") / "hooks" / hook_file.name),
                source_abs=hook_file,
                source_rel=str(hook_file.relative_to(orchestrator_root)),
                transform=None,
                always_overwrite=False,
            ))

    # Hooks _lib (always overwrite — not user-customisable).
    lib_src = hooks_src / "_lib"
    if lib_src.exists():
        for lib_file in sorted(lib_src.glob(hook_glob)):
            ops.append(_BundleFileOp(
                dest_rel=str(Path(".claude") / "hooks" / "_lib" / lib_file.name),
                source_abs=lib_file,
                source_rel=str(lib_file.relative_to(orchestrator_root)),
                transform=None,
                always_overwrite=True,
            ))

    # Scripts: copy ALL recognized flavours (mirrors install.py:5729).
    scripts_src = templates / "scripts"
    if scripts_src.exists():
        script_patterns = ["*.py", "*.sh", "*.ps1", "kg-*", "code-graph-*", "cost-summary"]
        seen: set[str] = set()
        for pat in script_patterns:
            for script_file in sorted(scripts_src.glob(pat)):
                if script_file.is_dir() or script_file.name in seen:
                    continue
                seen.add(script_file.name)
                ops.append(_BundleFileOp(
                    dest_rel=str(Path(".claude") / "scripts" / script_file.name),
                    source_abs=script_file,
                    source_rel=str(script_file.relative_to(orchestrator_root)),
                    transform=None,
                    always_overwrite=False,
                ))

    # Agents (with placeholder substitution).
    agents_src = templates / "agents" / "free"
    subs = _agent_subs(orchestrator_root)

    def _apply_subs(buf: bytes) -> bytes:
        text = buf.decode("utf-8", errors="replace")
        for k, v in subs.items():
            text = text.replace(k, v)
        return text.encode("utf-8")

    if agents_src.exists():
        for agent_file in sorted(agents_src.glob("*.md")):
            ops.append(_BundleFileOp(
                dest_rel=str(Path(".claude") / "agents" / agent_file.name),
                source_abs=agent_file,
                source_rel=str(agent_file.relative_to(orchestrator_root)),
                transform=_apply_subs,
                always_overwrite=False,
            ))

    # Skills (recursive; .md gets substitutions, others byte-copy).
    skills_src = templates / "skills"
    if skills_src.exists():
        for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
            for f in sorted(skill_dir.rglob("*")):
                if f.is_dir():
                    continue
                rel_in_skills = f.relative_to(skills_src)
                dest_rel = str(Path(".claude") / "skills" / rel_in_skills)
                ops.append(_BundleFileOp(
                    dest_rel=dest_rel,
                    source_abs=f,
                    source_rel=str(f.relative_to(orchestrator_root)),
                    transform=_apply_subs if f.suffix == ".md" else None,
                    always_overwrite=False,
                ))

    # Infrastructure compose files. Copy all docker-* / podman-* yml at
    # the top level of `infrastructure/`. The hook `ensure-containers.sh`
    # picks the right overlay at runtime; we just need the files present.
    infra_src = orchestrator_root / "infrastructure"
    if infra_src.exists():
        for compose_file in sorted(infra_src.iterdir()):
            if not compose_file.is_file():
                continue
            n = compose_file.name
            if not (
                (n.startswith("docker-compose") or n.startswith("podman-compose"))
                and (n.endswith(".yml") or n.endswith(".yaml"))
            ):
                continue
            ops.append(_BundleFileOp(
                dest_rel=str(Path("infrastructure") / n),
                source_abs=compose_file,
                source_rel=str(compose_file.relative_to(orchestrator_root)),
                transform=None,
                always_overwrite=False,
            ))

    return ops


def _file_action(
    op: _BundleFileOp,
    target_path: Path,
    *,
    update_mode: bool,
    manifest: dict,
) -> tuple[str, bytes]:
    """Decide the per-file action and return (action, source_bytes).

    Actions:
      "create"          — target missing, write source.
      "overwrite"       — file exists, content matches manifest's prior-shipped
                          hash → safe to update with new shipped content.
      "preserve"        — file exists, user-modified vs manifest. Skip; emit
                          deferral.
      "noop"            — file exists, source identical to installed (no-op).
      "always-overwrite"— `op.always_overwrite=True` (e.g. hooks/_lib).
      "skip-existing"   — first-install (update_mode=False) and target exists.
    """
    # Compute the source bytes (after transform if any). We always need
    # the bytes to compute hashes; reading is cheap relative to the rest.
    raw = op.source_abs.read_bytes()
    if op.transform is not None:
        source_bytes = op.transform(raw)
    else:
        source_bytes = raw

    if op.always_overwrite:
        return ("always-overwrite", source_bytes)

    if not target_path.exists():
        return ("create", source_bytes)

    # File exists — compare.
    installed_hash = _file_sha256(target_path)
    new_source_hash = _bytes_sha256(source_bytes)

    if installed_hash == new_source_hash:
        # Already up to date.
        return ("noop", source_bytes)

    if not update_mode:
        # First-install semantics: never touch existing files (preserves
        # any user customizations on pre-existing folders).
        return ("skip-existing", source_bytes)

    # Update mode: consult the manifest. If installed_hash matches what
    # we previously shipped, the user hasn't touched it → safe to
    # overwrite. Otherwise the user has modified it → preserve + defer.
    prior = manifest.get("files", {}).get(op.dest_rel, {})
    prior_hash = prior.get("sha256", "")
    if prior_hash and installed_hash == prior_hash:
        return ("overwrite", source_bytes)
    # Default to safety: user-modified (or unknown provenance).
    return ("preserve", source_bytes)


def _write_file_atomic(target: Path, data: bytes, *, mode: Optional[int] = None) -> None:
    """Atomic file write: temp file in same dir + os.replace. Optionally
    sets a unix mode bit (0o755 for shell scripts to preserve executable).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        suffix=".tmp",
        prefix=f".{target.name}.",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, str(target))
        if mode is not None:
            try:
                os.chmod(str(target), mode)
            except OSError:
                # chmod is a no-op on Windows; don't fail.
                pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _format_file_list_md(paths: list[str], cap: int = 20) -> str:
    """Render a bullet-list of file paths for inclusion in a deferral entry.

    Caps at `cap` entries with a "... and N more" trailer when oversize so
    the deferral .md doesn't grow unbounded for large preserve / skip lists
    (a fresh-folder install over a previously-installed orchestrator could
    plausibly produce dozens of skipped paths).
    """
    if len(paths) <= cap:
        return "\n".join(f"  - `{p}`" for p in paths)
    head = "\n".join(f"  - `{p}`" for p in paths[:cap])
    return f"{head}\n  - ... and {len(paths) - cap} more"


def _emit_user_modified_deferral(
    folder: Path, modified_files: list[str], orchestrator_root: Path,
) -> None:
    """Emit `bundle_user_modified_preserved`: one deferral entry per project
    listing every file that diverged from the prior-shipped hash during an
    `--update` run.

    The user has three options:
    1. Accept shipped versions wholesale: `--update --force`.
    2. Keep customizations and dismiss the deferral via `dismiss-deferral`
       (PR 5+ command — placeholder in the message for now).
    3. Manually merge per-file.

    Per-project grouping (single entry, file list inside) is intentional —
    one entry per file would generate dozens of deferrals that all
    duplicate the same actionable command.
    """
    if not modified_files:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    files_md = _format_file_list_md(sorted(modified_files))
    cmd = (
        f"# Inspect the differences (per file):\n"
        f"#   diff -u <orchestrator>/<source-rel> {folder}/<dest-rel>\n"
        f"# Then either accept shipped versions (forces overwrite):\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"{str(orchestrator_root)!r} --update --force --json\n"
        f"# OR keep your customizations and dismiss this deferral:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id bundle_user_modified_preserved"
    )
    entry = DeferralEntry(
        condition_id="bundle_user_modified_preserved",
        title="User-modified bundle files preserved during update",
        detected=(
            f"During an `install-bundle --update` run, "
            f"{len(modified_files)} file(s) under the project's `.claude/` "
            f"tree were found to differ from the version this orchestrator "
            f"originally shipped. They were preserved (not overwritten):\n"
            f"{files_md}"
        ),
        why_deferred=(
            "Default-to-safety: when an installed file's hash differs "
            "from the prior-shipped hash recorded in .vco-manifest.json, "
            "we preserve the on-disk version. If your edits are "
            "intentional, dismiss the deferral; if you'd rather take the "
            "shipped version, re-run with `--force`."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def _emit_skipped_existing_deferral(
    folder: Path, skipped_files: list[str], orchestrator_root: Path,
) -> None:
    """Emit `bundle_skipped_existing_files`: one deferral entry per project
    listing pre-existing files that the first-install path SKIPPED because
    their content differs from the orchestrator's shipped version.

    Why: a Claude Code session opening this folder needs to know the bundle
    install was incomplete — the user may have a stale custom hook that
    will silently miss new orchestrator-side improvements until they
    explicitly run `--update --force`.

    Severity is `info` (not `warning`) — the project is functional, just
    not 100% in lockstep with the orchestrator's defaults.

    Per-project grouping (single entry, file list inside): one entry per
    file would be noisy and harder to action. The single entry's command
    fixes ALL of them in one go.
    """
    if not skipped_files:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    files_md = _format_file_list_md(sorted(skipped_files))
    cmd = (
        f"# Accept the orchestrator's shipped versions for ALL skipped files:\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"{str(orchestrator_root)!r} --update --force --json"
    )
    entry = DeferralEntry(
        condition_id="bundle_skipped_existing_files",
        title="Pre-existing files preserved during first-install",
        detected=(
            f"During the first-install of this project's bundle, "
            f"{len(skipped_files)} file(s) under `.claude/` and "
            f"`infrastructure/` already existed AND differed from the "
            f"orchestrator's shipped versions. They were preserved to "
            f"avoid overwriting user customizations:\n"
            f"{files_md}"
        ),
        why_deferred=(
            "These files already existed when the bundle was first "
            "installed and differ from the orchestrator's shipped "
            "versions. We preserved them to avoid overwriting user "
            "customizations. If you intended to use the orchestrator's "
            "defaults, run "
            "`python -m vco_lib.project_init install-bundle --folder "
            "<path> --update --force` to overwrite."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def install_project_bundle(
    folder: Path,
    orchestrator_root: Optional[Path] = None,
    *,
    update_mode: bool = False,
    force: bool = False,
    dry_run: bool = False,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """Install (or update) the per-project Claude bundle in `folder`.

    Args:
        folder: target user-project folder (must exist).
        orchestrator_root: source of truth — the vibecoded-orchestrator
            clone. Default: walk up from this module looking for
            `vct-module.json`.
        update_mode: True → manifest-driven hash diff for overwrites;
            False → first-install skip-if-exists.
        force: in update mode, treat user-modified files as overwritable
            (still respects the no-changes "noop" case).
        dry_run: enumerate + classify but make no filesystem mutations.
        log_event: optional forensic logger.

    Returns a JSON-serialisable dict:
      {
        "folder": str,
        "orchestrator_root": str,
        "update_mode": bool,
        "force": bool,
        "dry_run": bool,
        "actions": {
            "create": [<rel>...],
            "overwrite": [<rel>...],
            "always-overwrite": [<rel>...],
            "noop": [<rel>...],
            "preserve": [<rel>...],
            "skip-existing": [<rel>...],
        },
        "settings_action": "created"|"merged"|"unchanged"|"unchanged (user file unparseable)"|"" ,
        "manifest_written": bool,
        "vco_version": str,
        "warnings": [...],
        "errors": [...],
      }

    Soft-fail: per-file errors land in `errors[]`; the function never
    raises for individual file failures. A missing template tree (e.g.
    `templates/skills/` absent) just means fewer entries in `actions`.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            log_event(step, phase, detail)

    folder = Path(folder).resolve()
    if not folder.exists() or not folder.is_dir():
        return {
            "folder": str(folder),
            "orchestrator_root": "",
            "update_mode": bool(update_mode),
            "force": bool(force),
            "dry_run": bool(dry_run),
            "actions": {k: [] for k in
                        ("create", "overwrite", "always-overwrite",
                         "noop", "preserve", "skip-existing")},
            "settings_action": "",
            "manifest_written": False,
            "vco_version": "unknown",
            "warnings": [],
            "errors": [{"path": str(folder), "error": "folder does not exist or is not a directory"}],
        }

    orchestrator_root = (
        Path(orchestrator_root).resolve()
        if orchestrator_root is not None
        else _find_orchestrator_root_from_module()
    )

    result: dict = {
        "folder": str(folder),
        "orchestrator_root": str(orchestrator_root),
        "update_mode": bool(update_mode),
        "force": bool(force),
        "dry_run": bool(dry_run),
        "actions": {k: [] for k in
                    ("create", "overwrite", "always-overwrite",
                     "noop", "preserve", "skip-existing")},
        "settings_action": "",
        "manifest_written": False,
        "vco_version": _resolve_vco_version(orchestrator_root),
        "warnings": [],
        "errors": [],
    }

    if not orchestrator_root.exists():
        result["errors"].append({
            "path": str(orchestrator_root),
            "error": "orchestrator_root does not exist",
        })
        return result

    manifest = _read_manifest(folder)
    new_files: dict[str, dict] = {}
    user_modified_paths: list[str] = []
    skipped_existing_paths: list[str] = []

    ops = _enumerate_bundle_files(orchestrator_root)
    _log("4.bundle", "start",
         f"enumerate: {len(ops)} ops",
         data={"folder": str(folder), "ops": len(ops)})

    for op in ops:
        target_path = folder / op.dest_rel
        try:
            action, source_bytes = _file_action(
                op, target_path, update_mode=update_mode, manifest=manifest,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle", "error",
                 f"{op.dest_rel}: classify failed: {err}",
                 data={"path": op.dest_rel, "error": err})
            result["errors"].append({"path": op.dest_rel, "error": err})
            continue

        # Honour --force: in update mode, treat preserve as overwrite.
        if force and update_mode and action == "preserve":
            action = "overwrite"

        # Compute the new shipped hash regardless of action — needed for
        # manifest update on every file we recognize.
        shipped_hash = _bytes_sha256(source_bytes)

        # Record into manifest only when we actually deposited the
        # shipped content (or when we previously did and it's still
        # what's on disk — noop / always-overwrite cases).
        record_in_manifest = False

        if action == "preserve":
            user_modified_paths.append(op.dest_rel)
            # Keep the manifest's prior entry (don't update hash) so the
            # next update still recognizes the prior baseline.
            existing = manifest.get("files", {}).get(op.dest_rel)
            if existing is not None:
                new_files[op.dest_rel] = existing

        elif action == "skip-existing":
            # First-install with pre-existing file: do not overwrite, but
            # also don't claim ownership in the manifest (it's not ours).
            # Track for the per-project deferral so Claude Code knows the
            # bundle install was incomplete (user has stale customizations
            # that won't track future orchestrator improvements).
            skipped_existing_paths.append(op.dest_rel)

        elif action == "noop":
            # File matches what we'd write. Manifest entry should reflect
            # the shipped hash (in case a previous install pre-dated the
            # manifest mechanism).
            record_in_manifest = True

        elif action in ("create", "overwrite", "always-overwrite"):
            if not dry_run:
                try:
                    mode: Optional[int] = None
                    # Preserve executable bit for shell scripts on POSIX.
                    if op.dest_rel.endswith((".sh",)) or "/scripts/" in op.dest_rel.replace("\\", "/"):
                        # Many launcher scripts have no extension (kg-search,
                        # code-graph-query, cost-summary). Mark all of
                        # .claude/scripts/ + *.sh as executable.
                        mode = 0o755
                    _write_file_atomic(target_path, source_bytes, mode=mode)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    _log("4.bundle", "error",
                         f"{op.dest_rel}: write failed: {err}",
                         data={"path": op.dest_rel, "error": err})
                    result["errors"].append({"path": op.dest_rel, "error": err})
                    continue
            record_in_manifest = True

        if record_in_manifest:
            new_files[op.dest_rel] = {
                "sha256": shipped_hash,
                "source": op.source_rel,
            }

        result["actions"][action].append(op.dest_rel)

    # Smart-merge settings.json template separately. The template carries
    # the orchestrator's hooks block + permissions defaults. The merge
    # logic mirrors install.py:_merge_settings_template + _smart_merge_settings.
    settings_template = _settings_template_path(orchestrator_root)
    if settings_template.exists():
        try:
            settings_action = _merge_settings_template_for_bundle(
                settings_template, folder / ".claude" / "settings.json",
                dry_run=dry_run,
            )
            result["settings_action"] = settings_action
            _log("4.bundle.settings", "ok",
                 f"settings.json: {settings_action}",
                 data={"action": settings_action})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.settings", "error",
                 f"settings.json merge failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"settings.json merge failed: {err}")

    # Manifest write (always after a successful pass — even dry-run skips).
    if not dry_run:
        try:
            manifest_payload = {
                "schema_version": _MANIFEST_SCHEMA_VERSION,
                "vco_version": result["vco_version"],
                "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": dict(sorted(new_files.items())),
            }
            _write_manifest_atomic(folder, manifest_payload)
            result["manifest_written"] = True
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.manifest", "error",
                 f"manifest write failed: {err}",
                 data={"error": err})
            result["errors"].append({"path": str(_MANIFEST_REL), "error": err})

    # Per-project deferral entries — single entry per case, listing all
    # affected files. Two distinct cases are tracked:
    #
    # 1. update-mode `preserve`: files the user modified, diverging from
    #    the prior-shipped manifest hash. Emitted unless --force was used.
    # 2. first-install `skip-existing`: files that pre-existed AND differ
    #    from what we would have shipped. Emitted regardless of mode (the
    #    user has stale customizations that won't auto-update).
    #
    # Both deferrals share the same UPDATE_DEFERRED.md file via PR 6's
    # `DeferralReport.add_entry` (last-write-wins per condition_id, so a
    # subsequent install run that resolves the condition will overwrite
    # the entry with the new state, or remove it when the list is empty).
    if not dry_run:
        if update_mode and user_modified_paths and not force:
            try:
                _emit_user_modified_deferral(
                    folder, user_modified_paths, orchestrator_root,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"user-modified deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"user-modified deferral write failed: {err}"
                )

        if skipped_existing_paths:
            try:
                _emit_skipped_existing_deferral(
                    folder, skipped_existing_paths, orchestrator_root,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"skipped-existing deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"skipped-existing deferral write failed: {err}"
                )

    return result


def _find_orchestrator_root_from_module() -> Path:
    """Walk up from this module's location looking for `vct-module.json`.
    Used when callers don't pass `--orchestrator-root` explicitly."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "vct-module.json").exists():
            return parent
    # Fallback: parent of this module's parent (vco_lib/) — best effort.
    return here.parent.parent


def _merge_settings_template_for_bundle(
    template_path: Path, target_path: Path, *, dry_run: bool,
) -> str:
    """Mirror of install.py:_merge_settings_template + _smart_merge_settings.

    Inlined here (rather than importing from install.py) so vco_lib stays
    import-free of install.py — install.py imports vco_lib, not the other
    way around.
    """
    template_data = json.loads(template_path.read_text(encoding="utf-8"))

    if not target_path.exists():
        if dry_run:
            return "would-create"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_file_atomic(
            target_path,
            (json.dumps(template_data, indent=2) + "\n").encode("utf-8"),
        )
        return "created"

    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unchanged (user file unparseable)"

    merged = _smart_merge_for_bundle(existing, template_data)
    if merged == existing:
        return "unchanged"

    if dry_run:
        return "would-merge"

    _write_file_atomic(
        target_path,
        (json.dumps(merged, indent=2) + "\n").encode("utf-8"),
    )
    return "merged"


def _smart_merge_for_bundle(user: dict, template: dict) -> dict:
    """Recursive dict merge with hooks-block special-case (mirror of
    install.py:_smart_merge_settings)."""
    out = dict(user)
    for key, tval in template.items():
        if key not in out:
            out[key] = tval
            continue
        uval = out[key]
        if key == "hooks" and isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = _merge_hooks_for_bundle(uval, tval)
        elif isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = _smart_merge_for_bundle(uval, tval)
        # else: user wins.
    return out


def _merge_hooks_for_bundle(user_hooks: dict, template_hooks: dict) -> dict:
    """Per-event hook array merge — append template entries whose inner
    `command` strings aren't already present (mirror of install.py)."""
    out = dict(user_hooks)
    for event, t_entries in template_hooks.items():
        if event not in out:
            out[event] = list(t_entries)
            continue
        u_entries = out[event] if isinstance(out[event], list) else []
        existing_cmds: set = set()
        for entry in u_entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and h.get("command"):
                    existing_cmds.add(h["command"])
        merged_entries = list(u_entries)
        for t_entry in t_entries:
            if not isinstance(t_entry, dict):
                continue
            t_cmds = [
                h.get("command")
                for h in t_entry.get("hooks", [])
                if isinstance(h, dict) and h.get("command")
            ]
            if t_cmds and all(c in existing_cmds for c in t_cmds):
                continue
            merged_entries.append(t_entry)
        out[event] = merged_entries
    return out


# ---------------------------------------------------------------------------
# CLI entry point (Rust subprocess interface)
# ---------------------------------------------------------------------------


def _cmd_derive(args: argparse.Namespace) -> int:
    """`derive --name <project_name> --json` → emit canonical name dict."""
    payload = derive_project_collection_names(args.name)
    if args.json:
        # JSON-only on stdout; Rust does serde_json::from_str on this.
        print(json.dumps(payload))
    else:
        for k, v in payload.items():
            print(f"{k}={v}")
    return 0


def _cmd_migrate_collections(args: argparse.Namespace) -> int:
    """`migrate-collections --name <name> [--dry-run] [--force-rebuild]
    [--weaviate-url <url>] --json`

    Sets KG_COLLECTION + DEVELOPMENT_COLLECTION env vars from --name
    (using canonical derivation), then runs the dispatcher.

    JSON stdout schema:
      {"plan": [{"collection", "action", "objects_copied", "elapsed_ms"}],
       "dry_run": bool,
       "errors": [{"collection", "action", "error"}]}

    Exit 0 on success; 1 if any errors[] entry exists.
    """
    derived = derive_project_collection_names(args.name)
    # Inject env so migrate_collections picks them up. We don't mutate
    # the caller's environment beyond this process — argparse callers
    # are typically the Rust subprocess or a CLI invocation, not a long-
    # lived shell.
    os.environ["KG_COLLECTION"] = derived["kg_collection"]
    os.environ["DEVELOPMENT_COLLECTION"] = derived["development_collection"]

    # Build a minimal Namespace-like for migrate_collections dispatch.
    ns = argparse.Namespace(force_rebuild=bool(args.force_rebuild))
    result = migrate_collections(
        ns,
        dry_run=bool(args.dry_run),
        weaviate_url=args.weaviate_url,
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"dry_run: {result['dry_run']}")
        for entry in result["plan"]:
            print(
                f"  {entry['action']:13s} {entry['collection']}  "
                f"objects_copied={entry['objects_copied']}  "
                f"elapsed_ms={entry['elapsed_ms']}"
            )
        for err in result["errors"]:
            print(f"  ERROR {err['collection']}: {err['error']}")
    return 1 if result["errors"] else 0


def _cmd_bootstrap_collections(args: argparse.Namespace) -> int:
    """`bootstrap-collections --name <n> [--weaviate-url <url>] [--dry-run]
    [--kg-only] [--project-folder <path>] --json`

    POSTs Weaviate schema for `<sanitized>_KnowledgeGraph` and
    `<sanitized>_Development` (and the shared KG). Idempotent.

    Exit 0 on success (including soft-fail with deferral). Exit 1 only
    when individual collection POSTs failed AND the function couldn't
    soft-recover. Weaviate-down + restart-fail is treated as a deferred
    success (exit 0 with `deferred: true`) so launcher project-create
    never blocks.
    """
    project_folder = (
        Path(args.project_folder).resolve() if args.project_folder else None
    )
    result = bootstrap_collections(
        args.name,
        weaviate_url=args.weaviate_url,
        dry_run=bool(args.dry_run),
        kg_only=bool(args.kg_only),
        project_folder=project_folder,
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"weaviate_reachable: {result['weaviate_reachable']}")
        print(f"deferred: {result['deferred']}")
        for a in result["actions"]:
            print(f"  {a['action']:13s} {a['collection']}  ok={a['ok']}")
        for err in result["errors"]:
            print(f"  ERROR {err['collection']}: {err['error']}")
    # Soft-fail policy: deferred path returns success so create_project_v2
    # doesn't propagate it as a hard error. Hard errors only when there
    # were per-collection failures we couldn't defer.
    return 1 if result["errors"] else 0


def _cmd_install_bundle(args: argparse.Namespace) -> int:
    """`install-bundle --folder <path> [--orchestrator-root <path>]
    [--update] [--force] [--dry-run] [--project-folder <path>] --json`

    Copies `templates/` + `infrastructure/` into the user project folder.
    See `install_project_bundle` for full semantics.

    Exit 0 on clean install (including update with deferred entries).
    Exit 1 when at least one file failed to write or the manifest write
    failed.

    `--project-folder` is accepted as an alias / explicit form of
    `--folder` for symmetry with the bootstrap subcommand. If both are
    given they must match.
    """
    folder = Path(args.folder).resolve()
    if args.project_folder:
        explicit = Path(args.project_folder).resolve()
        if explicit != folder:
            print(
                f"error: --folder ({folder}) and --project-folder ({explicit}) "
                "must refer to the same path",
                file=sys.stderr,
            )
            return 2

    orchestrator_root = (
        Path(args.orchestrator_root).resolve() if args.orchestrator_root else None
    )
    result = install_project_bundle(
        folder,
        orchestrator_root=orchestrator_root,
        update_mode=bool(args.update),
        force=bool(args.force),
        dry_run=bool(args.dry_run),
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"folder: {result['folder']}")
        print(f"orchestrator_root: {result['orchestrator_root']}")
        print(f"update_mode: {result['update_mode']}  dry_run: {result['dry_run']}")
        for category, paths in result["actions"].items():
            if not paths:
                continue
            print(f"  {category} ({len(paths)}):")
            for p in paths[:8]:
                print(f"    {p}")
            if len(paths) > 8:
                print(f"    ... +{len(paths) - 8} more")
        if result["settings_action"]:
            print(f"  settings.json: {result['settings_action']}")
        if result["manifest_written"]:
            print(f"  manifest written: .claude/.vco-manifest.json")
        for w in result["warnings"]:
            print(f"  WARNING {w}")
        for err in result["errors"]:
            print(f"  ERROR {err.get('path', '?')}: {err['error']}")
    return 1 if result["errors"] else 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.project_init",
        description=(
            "Project init helpers — Rust subprocess interface. "
            "All subcommands accept --json for clean stdout/stderr "
            "separation (stdout: JSON, stderr: logs)."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_derive = sub.add_parser(
        "derive",
        help="Emit canonical collection-name dict for a project name.",
    )
    p_derive.add_argument("--name", required=True, help="Project name (raw, e.g. 'VideoFrames').")
    p_derive.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (default if invoked from Rust).",
    )
    p_derive.set_defaults(func=_cmd_derive)

    p_migrate = sub.add_parser(
        "migrate-collections",
        help=(
            "Smart per-collection schema migration: noop / patch_props / "
            "copy-with-vectors / rebuild. Replaces drop-and-re-embed."
        ),
    )
    p_migrate.add_argument(
        "--name", required=True,
        help="Project name (raw, e.g. 'VideoFrames'). KG/Dev collection "
             "names are derived via the canonical sanitizer.",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Plan only, no Weaviate mutations.",
    )
    p_migrate.add_argument(
        "--force-rebuild", action="store_true",
        help="Bypass smart path, always drop+re-embed (escape hatch).",
    )
    p_migrate.add_argument(
        "--weaviate-url", default=None,
        help="Override Weaviate URL (default: WEAVIATE_URL env or "
             "http://localhost:8081).",
    )
    p_migrate.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout.",
    )
    p_migrate.set_defaults(func=_cmd_migrate_collections)

    # bootstrap-collections (PR 4) ---------------------------------------
    p_bootstrap = sub.add_parser(
        "bootstrap-collections",
        help=(
            "POST Weaviate schema for the per-project KG/Dev/Shared "
            "collections. Idempotent. Soft-fails on Weaviate-down "
            "(podman start retry, then deferral .md). Used by launcher "
            "create_project_v2."
        ),
    )
    p_bootstrap.add_argument(
        "--name", required=True,
        help="Project name (raw; sanitization applied internally).",
    )
    p_bootstrap.add_argument(
        "--weaviate-url", default=None,
        help="Override Weaviate URL (default: WEAVIATE_URL env or "
             "http://localhost:8081).",
    )
    p_bootstrap.add_argument(
        "--dry-run", action="store_true",
        help="Plan only, no Weaviate mutations.",
    )
    p_bootstrap.add_argument(
        "--kg-only", action="store_true",
        help="Skip the per-project Development collection. Shared KG is "
             "still created (every project depends on read access).",
    )
    p_bootstrap.add_argument(
        "--project-folder", default=None,
        help="Path to the user-project folder. When set, "
             "Weaviate-unreachable conditions emit a deferral entry to "
             "<folder>/.claude/context/UPDATE_DEFERRED.md.",
    )
    p_bootstrap.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout.",
    )
    p_bootstrap.set_defaults(func=_cmd_bootstrap_collections)

    # install-bundle (PR 4) ----------------------------------------------
    p_bundle = sub.add_parser(
        "install-bundle",
        help=(
            "Copy hooks/scripts/agents/skills/settings/infrastructure "
            "into a user-project folder. Manifest-driven on --update. "
            "Used by launcher create_project_v2 + (PR 5) update_project_v2."
        ),
    )
    p_bundle.add_argument(
        "--folder", required=True,
        help="Target user-project folder (must exist).",
    )
    p_bundle.add_argument(
        "--orchestrator-root", default=None,
        help="Orchestrator clone root (source of truth for templates/ + "
             "infrastructure/). Default: walk up from this module looking "
             "for vct-module.json.",
    )
    p_bundle.add_argument(
        "--update", action="store_true",
        help="Manifest-driven update mode: hash-based drift detection, "
             "preserves user-modified files, emits deferral entries.",
    )
    p_bundle.add_argument(
        "--force", action="store_true",
        help="In update mode: overwrite user-modified files anyway. "
             "No-op without --update.",
    )
    p_bundle.add_argument(
        "--dry-run", action="store_true",
        help="Enumerate + classify without filesystem mutations.",
    )
    p_bundle.add_argument(
        "--project-folder", default=None,
        help="Alias / explicit form of --folder (kept for symmetry with "
             "bootstrap-collections). If both are given they must match.",
    )
    p_bundle.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout.",
    )
    p_bundle.set_defaults(func=_cmd_install_bundle)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
