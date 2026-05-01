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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
