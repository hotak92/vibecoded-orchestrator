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
from pathlib import Path, PurePosixPath
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


def derive_project_diagrams_name(project_name: str) -> str:
    """Public: derive a per-project Diagrams collection name from a
    project name string.

    Mirrors the naming convention of the KG / Development collections —
    `<sanitized>_Diagrams`. Used by `vco_lib.diagram_indexer` (Phase
    1.5) when upserting Mermaid + Excalidraw entries into Weaviate.
    """
    return f"{sanitize_for_weaviate_class(project_name)}_Diagrams"


def derive_project_collection_names(project_name: str) -> dict:
    """Canonical collection-name dict for a project.

    Returns:
        {
          "kg_collection":              "<sanitized>_KnowledgeGraph",
          "development_collection":     "<sanitized>_Development",   # uppercase D
          "diagrams_collection":        "<sanitized>_Diagrams",      # uppercase D
          "project_name":               <raw, not sanitized>,
          "shared_kg_collection":       "VibeCodedOrchestrator_KnowledgeGraph",
          "shared_kg_write_disabled":   "false",
          "kg_basename":                "<sanitized>",
        }

    `shared_kg_write_disabled` is the per-project WRITE gate (asymmetric
    model since 2026-05-01: all projects always READ the shared KG; only
    writes are gated). Default "false" (writes allowed). Stored as a
    string so all 4 env surfaces can pass it through unchanged.

    `diagrams_collection` is the per-project Weaviate class that holds
    Mermaid/Excalidraw diagrams indexed by `vco_lib.diagram_indexer`
    (Phase 1.5 of the Diagrams Integration). Auto-paired with the KG
    collection on project init by `derive_project_diagrams_name`
    callers; reads are unconditional, writes flow through the indexer
    module only.
    """
    basename = sanitize_for_weaviate_class(project_name)
    # Canonical shared KG class name. Aligned across:
    #   - this file (vco_lib/project_init.py)
    #   - launcher/src/lib/project-state/IdentityTab.svelte fallback
    #   - all migration scripts (scripts/migrate-shared-kg-schema.{sh,ps1})
    # Renamed from VibeCodedTools_KnowledgeGraph in v0.2.12 (PR-26 / Group E).
    # v0.2.23 B1 (2026-05-21) flipped the casing from lowercase-c "Vibecoded"
    # back to capital-C "VibeCoded" to match the brand spelling. Existing
    # installs with the lowercase-c class are adopted in place via the
    # case-insensitive lookup in `install.py::_ensure_collections` and the
    # binding-row self-heal step in `install.py::_self_heal_kg_bindings_on_update`.
    # Users with data under the legacy `VibeCodedTools_KnowledgeGraph` name
    # can still migrate via the launcher's Shared KG picker
    # (Settings -> Identity -> "Manage shared KG collection").
    return {
        "kg_collection": f"{basename}_KnowledgeGraph",
        "development_collection": f"{basename}_Development",
        "diagrams_collection": f"{basename}_Diagrams",
        "project_name": project_name,
        "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
        "shared_kg_write_disabled": "false",
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
    """KG-shaped multi-slot named-vector config (v0.2.18 catalog).

    Sources the slot catalog from `vco_lib.weaviate_schema.KG_NAMED_VECTORS`
    — see that module for the LOCKED slot list + rationale.

    Pre-v0.2.18 this function returned a hard-coded 3-slot config
    (`qwen3_embed`, `ollama_embed`, `openai_embed`). v0.2.18 extends to
    5 slots — the legacy 3 are RETAINED so v0.2.17 -> v0.2.18 upgrades
    preserve data, plus 2 new slots (`arctic2_embed`, `openai_text_embed`)
    that the EmbeddingService (Commit 2) and GUI dropdowns (Commit 8)
    use as additional embedding-provider targets.

    Each slot has `vectorizer: none` so we feed pre-computed embeddings
    from the MCP server. Index type stays HNSW (Weaviate default for ANN).

    The MCP server's `sync_knowledge_graph.py` writes objects with at
    least one named vector populated; the others are filled lazily as the
    user pulls more embedding backends. Without this multi-vector config
    seeding fails with HTTP 422 ("collection configured without multiple
    named vectors, but received named vectors").
    """
    # Late import to avoid a circular dep — weaviate_schema imports
    # project_init lazily inside `_default_collection_rebuilder`.
    from vco_lib.weaviate_schema import KG_NAMED_VECTORS
    return {slot.name: slot.to_weaviate_config() for slot in KG_NAMED_VECTORS}


def code_class_definitions(project_prefix: str = "") -> dict[str, dict]:
    """Return Weaviate class definitions for the 5 code-graph collections.

    Args:
        project_prefix: Optional prefix added before each canonical name
            (e.g. `project_prefix="MyProj_"` -> `"MyProj_CodeFunction"`).
            Empty string returns bare-name definitions; callers using
            per-project prefixing prepend it.

    Used by analyzers + the v0.2.18 schema migration. The actual
    code-graph create site is `templates/scripts/analyze_code_graph.py`
    which has its own per-collection property list (richer than the KG
    schema). This function provides the MINIMAL VECTOR-CONFIG-ONLY shape
    that the v0.2.18 migrate-collections helper uses for new-class
    creation (when a code collection doesn't yet exist and the helper
    needs to bootstrap it with the multi-slot config). Property-rich
    creation still flows through analyze_code_graph.py's
    `create_collections` so the bare definitions returned here only
    carry the vector slots + the inverted-index invariant.

    v0.2.18 (Plan C): the minimal property surface now declares the
    `language` property (canonical lowercase ID per analyzed file). This
    enables the smart-dispatch's patch_props action to add the missing
    property on existing v0.2.17 code collections when the migrate path
    routes them through. analyze_code_graph.py's `_ensure_language_property`
    is the primary path; the patch_props branch is a defense-in-depth
    redundancy for callers that exercise migrate-collections directly
    without re-running analyze.

    The returned dict is keyed by basename (no prefix); composition with
    `project_prefix` is the caller's responsibility for canonical naming.
    """
    from vco_lib.weaviate_schema import CODE_NAMED_VECTORS, _CODE_COLLECTION_SUFFIXES

    vc = {slot.name: slot.to_weaviate_config() for slot in CODE_NAMED_VECTORS}

    # v0.2.18 (Plan C) — minimal property declaration. Same shape used by
    # analyze_code_graph.py's `create_collections` so the smart-dispatch
    # patch_props action recognises it as "already there" on freshly-
    # created collections.
    _language_prop = {
        "name": "language",
        "dataType": ["text"],
        "description": (
            "Canonical-lowercase language ID (python, javascript, ...) — "
            "Plan C scoped prune"
        ),
    }

    return {
        basename: {
            "class": f"{project_prefix}{basename}",
            "description": f"Code-graph {basename} collection (v0.2.18 multi-slot)",
            "vectorConfig": vc,
            "invertedIndexConfig": {"indexNullState": True},
            # Plan C: `language` is the only minimal property declared
            # here; analyze_code_graph.py owns the full property surface
            # for each collection. Both classes (Module/Class/Function/
            # API/Interaction) carry the same `language` prop — for
            # CodeInteraction it represents the SOURCE-SIDE language
            # (caller's), not the target's.
            "properties": [_language_prop],
        }
        for basename in sorted(_CODE_COLLECTION_SUFFIXES)
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
            # v0.2.17 (Reviewer B v0.2.18 nit, folded into v0.2.17):
            # SHA-256 over (frontmatter-minus-updated + body), used by
            # `templates/scripts/sync_knowledge_graph.py`'s embed-skip
            # fast path. Including it at create-time means fresh
            # per-project KGs ship with the property from day 0 — no
            # warm-up sync that re-embeds everything just to populate
            # this field. The orchestrator-self template also has it
            # via the `temporal_props` migration loop for upgrades; this
            # entry covers create-from-scratch on per-project init.
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }


def development_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project Development collection.

    Same `indexNullState=True` invariant as the KG schema (see
    `kg_class_definition`).

    Temporal properties (`created`, `updated`, `valid_from`, `valid_until`)
    mirror the KG schema. The MCP server's `hybrid_search` applies a
    stale-data filter (``valid_until is_none(True) | valid_until > now``)
    on every search target; without these properties Development queries
    fail at the GraphQL layer with "no such prop with name 'valid_until'
    found in class '<X>_Development'". Date dataType matches the KG
    definition (see ``kg_class_definition`` properties).

    v0.2.18 (2026-05-19): adds two properties for KG parity:

    * ``content_hash`` (text) — SHA-256 over the source file body, used by
      ``templates/scripts/sync_knowledge_graph.py::sync_doc``'s embed-skip
      fast-path. Mirrors the v0.2.17 KG addition (see
      `kg_class_definition`) so re-syncing an unchanged docs/ tree drops
      from "re-embed every file" to "compare hashes; skip everything in
      milliseconds." Existing v0.2.17 Dev collections gain the property
      via `migrate_collections`' additive patch_props action — additive,
      non-destructive. The first re-sync after the upgrade backfills
      values; subsequent re-syncs hit the fast path.

    * ``status`` (text) — lets docs that get superseded by newer versions
      be marked `archived` so `hybrid_search`'s stale-filter skips them.
      Same shape as the KG `status` field.

    EXPLICITLY NOT MIRRORED from the KG schema (user direction 2026-05-19):

    * ``tags`` / ``links`` / ``typed_links`` — KG-only graph metadata;
      Dev rows have no WikiLink resolution, no typed relationships, no
      tag-from-typed-link inference. The corresponding fields in
      ``parse_doc_file`` synthesize empty values for symmetry, but they
      are never written into the Dev collection.
    * ``node_type`` — redundant: every row in a Development collection is
      unambiguously a "doc" by virtue of the collection name itself
      (``<Project>_Development``). No need for a per-row constant column.
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
            # Temporal metadata — mirrors the KG schema so MCP filters
            # (`valid_until is_none(True) | valid_until > now`) work
            # against Development collections too. Added 2026-05-16
            # (PR-24).
            {"name": "created", "dataType": ["date"]},
            {"name": "updated", "dataType": ["date"]},
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
            # v0.2.18 (2026-05-19): KG parity for archived-doc filtering
            # (`status`) and the embed-skip fast-path (`content_hash`).
            # Migration into existing v0.2.17 Dev collections is handled
            # by `migrate_collections`' additive patch_props action —
            # the diff between `_fetch_schema()` and this target picks up
            # any missing prop automatically (see `_schema_delta`).
            {"name": "status", "dataType": ["text"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }


def diagrams_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project Diagrams collection.

    Phase 1.5 of the Diagrams Integration (2026-05-24). Mirrors the
    `indexNullState=True` invariant of the KG / Dev schemas and uses
    the same multi-slot named-vector config so KG-style search ranking
    works without translation.

    Property surface (matches `vco_lib/diagram_indexer.py::_weaviate_upsert`):

    * ``title``           — `inferred_title` (mermaid frontmatter title,
                            excalidraw scene name, or humanised filename)
    * ``content``         — Mermaid source OR concatenated Excalidraw
                            text labels (the embedding target)
    * ``path_tags``       — split of `category_path` ("gui/auth" →
                            ["gui","auth"]) — primary tag axis
    * ``diagram_kind``    — "flowchart" / "classDiagram" / ... / "excalidraw"
    * ``chat_id``         — Claude Code session UUID when the wrapper-MCP
                            saved the diagram; nullable when user saved
                            outside a Claude session
    * ``linked_session_summary`` — first 200 chars of the chat's
                            summary file (best-effort)
    * ``file_path``       — absolute path on disk (used for dedup +
                            click-through from search results)
    * ``created_at`` / ``updated_at`` — unix epoch ints (NOT date strings
                            — the indexer writes Python ints from
                            `int(time.time())` to keep the SQLite +
                            Weaviate shapes identical)

    Explicitly NOT included (rationale documented inline):

    * ``status`` — diagrams have no archival workflow yet; if a Phase-3
      need emerges, add via the additive patch_props migration path.
    * ``content_hash`` — content_text already serves as the dedup key
      for now; adding hash later is an additive migration.
    * ``tags`` / ``links`` / ``typed_links`` — diagrams use `path_tags`
      as their sole tag axis (the path IS the tag). No WikiLink graph.
    """
    return {
        "class": name,
        "description": (
            "VibeCoded Tools per-project diagrams collection "
            "(Mermaid + Excalidraw)"
        ),
        "vectorConfig": named_vector_config(),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "path_tags", "dataType": ["text[]"]},
            {"name": "diagram_kind", "dataType": ["text"]},
            {"name": "chat_id", "dataType": ["text"]},
            {"name": "linked_session_summary", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "created_at", "dataType": ["int"]},
            {"name": "updated_at", "dataType": ["int"]},
        ],
    }


# Internal aliases preserving install.py's underscored names.
_named_vector_config = named_vector_config
_kg_class_definition = kg_class_definition
_development_class_definition = development_class_definition
_diagrams_class_definition = diagrams_class_definition


# ---------------------------------------------------------------------------
# Schema-drift detection (relocated from install.py:4562)
# ---------------------------------------------------------------------------


def detect_kg_schema_drift(weaviate_url: str, kg_collection: str) -> tuple[bool, list[str]]:
    """Probe a running KG collection for today's required schema invariants.

    Returns (drift_detected, missing_features). drift_detected=True means
    the collection exists but lacks one or more invariants that the
    current code requires.

    Invariants checked (today's set; grow this list when new ones land):
      - The CORE named-vector slot subset is present (the legacy v0.2.17
        triple qwen3_embed + ollama_embed + openai_embed). v0.2.18 added
        two more slots (`arctic2_embed`, `openai_text_embed`) but their
        absence is NOT classified as drift — they're idempotently added
        by `python -m vco_lib.project_init migrate-collections` and the
        running MCP / sync scripts gracefully handle their absence
        (falling back to qwen3_embed). The v0.2.18 plan's deferral
        mechanism (Commit 9 enrichment migration) is the proper surface
        for "missing optional slots"; drift-detection still triggers
        only on absent core slots that would break basic search.
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

    # Check named-vector slots. v0.2.18: the catalog now lists 5 slots
    # but drift only fires on the CORE legacy-v0.2.17 subset — the new
    # v0.2.18 slots (`arctic2_embed`, `openai_text_embed`) are optional
    # extras handled by the migrate-collections idempotent helper and
    # are not blockers for basic KG operation. This keeps drift-
    # triggered destructive rebuilds rare; the explicit migrate path
    # adds the new slots additively via copy-with-vectors.
    vec_config = schema.get("vectorConfig") or {}
    core_expected_slots = {"qwen3_embed", "ollama_embed", "openai_embed"}
    actual_slots = set(vec_config.keys())
    if not core_expected_slots.issubset(actual_slots):
        gap = sorted(core_expected_slots - actual_slots)
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
#   1. If Weaviate unreachable, attempt `podman start <name>` (or
#      `docker start` fallback) once and wait up to 10s for healthy.
#      Container name is discovered via
#      `vco_lib.containers.find_existing_container("weaviate")`;
#      falls back to the canonical `vco_weaviate` if none of the
#      historical aliases are present on the host.
#   2. If still unreachable, write a `weaviate_unreachable_at_bootstrap`
#      deferral entry to `<project_folder>/.claude/context/UPDATE_DEFERRED.md`
#      and return success — NEVER block project creation. The hook
#      `ensure-containers.sh` is the second-line backstop for the next
#      Claude Code session.
#
# Shared KG (`VibeCodedOrchestrator_KnowledgeGraph` since v0.2.23 B1,
# previously `VibecodedOrchestrator_KnowledgeGraph` since v0.2.12 PR-26
# which renamed from the pre-v0.2.12 `VibeCodedTools_KnowledgeGraph` — see
# `_LEGACY_SHARED_KG_NAME` and `_LEGACY_SHARED_KG_NAME_LOWERCASE_C` below
# for the legacy aliases still used by migration-detection paths):
# created when missing regardless of any per-project SHARED_KG_WRITE_DISABLED
# toggle (or its legacy SHARED_KG_OPT_OUT alias). Per the coordinator's
# 2026-05-01 directive: every project ALWAYS reads the shared KG; the toggle
# is purely a runtime write-gate. Creation is not gated on it.
# ---------------------------------------------------------------------------


# Canonical shared-KG class name. Must stay in lockstep with:
#   * `derive_project_collection_names()` above (which returns this value
#     as `shared_kg_collection`),
#   * `launcher/src-tauri/src/commands/project_env_settings.rs::DEFAULT_SHARED_KG_COLLECTION`,
#   * `claude_mcp_servers/weaviate_mcp/server.py::_SHARED_KG_DEFAULT`,
#   * `scripts/migrate-shared-kg-schema.{sh,ps1}` defaults.
# The cross-language invariant test `tests/test_shared_kg_constant_consistency.py`
# pins these in lockstep so any drift fails CI loudly.
#
# v0.2.23 B1 (2026-05-21): canonical casing flipped from "Vibecoded" (lowercase
# c) to "VibeCoded" (capital C) to match the brand spelling. New installs
# create classes with capital C; case-insensitive adoption in install.py
# means an existing lowercase-c class is adopted in-place (no rename, no
# data loss). The lowercase-c name is kept as a legacy alias for
# detection-only purposes (see _LEGACY_SHARED_KG_NAME_LOWERCASE_C).
_SHARED_KG_NAME = "VibeCodedOrchestrator_KnowledgeGraph"

# Legacy shared-KG class name (pre-v0.2.12 PR-26 rename). Migration-detection
# code that recognizes user installs still carrying the old class uses THIS
# constant rather than scattering the literal string across the codebase.
# DO NOT use this as a default for new writes — only for "did the user
# install this before the rename?" detection logic. Picker-driven migration
# (launcher Settings → Identity → "Manage shared KG collection") is the
# consent mechanism for renaming the on-disk class; this code never
# auto-renames or auto-drops.
_LEGACY_SHARED_KG_NAME = "VibeCodedTools_KnowledgeGraph"

# Lowercase-c variant of the canonical name (PR-34 / v0.2.12 default
# through v0.2.22). v0.2.23 B1 flipped the canonical to capital-C to
# match the brand spelling; this constant pins the prior default as a
# legacy alias so case-insensitive-adoption code recognises a user
# Weaviate that still carries the lowercase-c class.
#
# Same DO-NOT-USE-FOR-WRITES contract as _LEGACY_SHARED_KG_NAME: detection
# only. Install.py's case-insensitive adoption logic rebinds the resolved
# `SHARED_KG_COLLECTION` env value to whatever the live class actually is,
# so downstream writes always target the on-disk casing.
_LEGACY_SHARED_KG_NAME_LOWERCASE_C = "VibecodedOrchestrator_KnowledgeGraph"

def _default_restart_container() -> str:
    """Resolve the Weaviate container name to attempt restarting.

    v0.2.15: replaced the hardcoded literal `weaviate_claude` (which was
    a maintainer-machine leak — see vco_lib/containers.py) with a
    lazy lookup that probes for the actual container on the host. Falls
    back to the canonical `vco_weaviate` when nothing matches, so callers
    always get a usable name (`podman start vco_weaviate` will fail
    gracefully with "no such container" if the user has truly nothing).

    Called lazily by `_attempt_container_restart` rather than evaluated at
    module import — `podman container exists` shells out and we don't
    want to pay that on every `import vco_lib.project_init` in test code.
    """
    from vco_lib.containers import canonical_name, find_existing_container

    found = find_existing_container("weaviate")
    if found is not None:
        return found
    return canonical_name("weaviate")


# Sentinel that callers can pass instead of a literal container name to
# request lazy lookup. Resolves to the actual name at call time via
# `_default_restart_container()`. The old import-time constant pattern
# was a snapshot of the maintainer's machine — see vco_lib/containers.py.
_DEFAULT_RESTART_CONTAINER: str = "__resolve_lazily__"


# ---------------------------------------------------------------------------
# Bug-1 v0.2.4 (2026-05-12): schema-incompatibility regeneration helpers.
#
# Pre-v0.2.0 orchestrators created KG/Dev collections with different schema
# shapes than the current code expects:
#
#   * Case-only name conflicts: old `SD15_development` (lowercase d) vs new
#     `SD15_Development` (capital D). Weaviate stores class names case-
#     sensitively but rejects POSTs of "similar" classes with HTTP 422
#     `class already exists: found similar class "<actual>"`.
#
#   * Multi-named-vector legacy schemas with `ollama_embed`+`qwen3_embed`
#     where new code expects 3 slots (`qwen3_embed`+`ollama_embed`+
#     `openai_embed`) AND legitimately accepts a single named vector per
#     object — sync_knowledge_graph.py writes one vector per object,
#     Weaviate's older multi-vector configs reject that with HTTP 422
#     "configured with multiple named vectors, but received a single vector".
#
# Both cases are losslessly fixable by drop + recreate from disk: the on-
# disk knowledge/**/*.md is the source of truth and the post-bootstrap
# kg-sync step re-ingests everything.
#
# No version tracking is needed; the actual schema fields tell us
# everything. If the diff between actual and target is non-trivial, the
# collection is incompatible.
# ---------------------------------------------------------------------------


# Pattern Weaviate emits in HTTP 422 responses when a POST /v1/schema hits
# a case-insensitive class-name collision. The actual name is captured in
# the first group.
#
# We accept multiple escaping variants — Weaviate's error body is JSON,
# and what we see depends on how it propagates up to Python:
#   - Plain JSON-decoded:  `similar class "X"`
#   - Single-escaped:      `similar class \"X\"` (one layer of escape)
#   - Double-escaped:      `similar class \\"X\\"` (from `str(bytes_repr)`,
#     which is what RuntimeError(str) of a bytes-wrapped response produces)
# The wrapping quote may also be a single ASCII apostrophe in some legacy
# error variants, so the regex is permissive about the delimiter.
_SIMILAR_CLASS_RE = re.compile(
    r'similar\s+class\s+\\{0,2}["\']([^"\'\\]+)\\{0,2}["\']',
    re.IGNORECASE,
)


def _extract_similar_class_name(error_body: Optional[str]) -> Optional[str]:
    """Parse Weaviate's `class already exists: found similar class "X"` 422
    response. Returns the actual server-side name if present, else None.

    Used by bootstrap_collections to recover from case-only name conflicts
    by dropping the actual-named class and re-creating with the target name.

    Handles both the unescaped form (`similar class "X"`) and the JSON-
    escaped form (`similar class \\"X\\"`) — Weaviate's REST error body is
    JSON, so the bytes we see in the response usually contain backslash-
    escaped quotes around the name.
    """
    if not error_body:
        return None
    m = _SIMILAR_CLASS_RE.search(error_body)
    return m.group(1) if m else None


def _schema_incompatible(
    actual: dict,
    target_def_fn: Callable[[str], dict],
    name: str,
) -> tuple[bool, str]:
    """Compare an actual Weaviate schema dict against the canonical target.

    Returns ``(incompatible, reason)`` where ``reason`` is a short
    human-readable explanation (used for forensic logging and the
    regenerated[] envelope entry). When ``incompatible`` is False the
    schema is close enough for current code; minor additive property
    drift is tolerated because the smart-migrate path patches those
    in-place during `migrate_collections` and the post-bootstrap
    sync re-ingests anyway.

    Detection rules (intentionally NARROWER than `_schema_delta`):
      * legacy single-vector (no vectorConfig) → REGEN
      * CORE legacy-v0.2.17 named-vector slot(s) missing → REGEN
        (`qwen3_embed` + `ollama_embed` + `openai_embed` — the triple
        that v0.2.17 MCP writes depend on). Optional newer slots
        (`arctic2_embed`, `openai_text_embed` from v0.2.18) being
        absent is NOT REGEN — they're added idempotently by
        `migrate-collections`.
      * Extra slots (slots in actual but not in target) → NOT REGEN.
        Pre-v0.2.18 the rule was "any set mismatch triggers regen";
        v0.2.18 retains LEGACY slots in the catalog precisely so that
        v0.2.17 collections don't trip this, but if a future cleanup
        narrows the catalog, we still want extra slots to be benign
        (data preservation > schema strictness).
      * indexNullState invariant missing → REGEN
      * properties missing/extra → NOT REGEN (sync re-ingests; smart
        migrate's patch_props handles the additive case; the destructive
        regen path is reserved for the changes that can't be fixed any
        other way).

    The case-only naming conflict is NOT handled here — it's surfaced via
    the 422 response from _create_class, not via schema inspection (the
    collision class isn't visible to a `_fetch_schema(target_name)` since
    we ask for the wrong-cased name in the first place).
    """
    target = target_def_fn(name)
    target_vec = target.get("vectorConfig") or {}
    actual_vec = actual.get("vectorConfig")

    if not actual_vec:
        return (True, "legacy single-vector schema (no vectorConfig)")

    # v0.2.18: only flag REGEN when CORE legacy-v0.2.17 slots are
    # missing. The catalog's new v0.2.18 slots (e.g. `arctic2_embed`)
    # missing is fine — they're added later via migrate-collections.
    # Extra slots are always tolerated (data preservation principle).
    actual_slots = set(actual_vec.keys())
    core_slots = {"qwen3_embed", "ollama_embed", "openai_embed"}
    # The target's slot set must be a superset of core_slots (else the
    # catalog itself is broken). If a downstream config narrowed it,
    # fall back to the target slots directly.
    target_slots = set(target_vec.keys())
    effective_required = core_slots & target_slots if core_slots & target_slots else target_slots
    missing_core = sorted(effective_required - actual_slots)
    if missing_core:
        return (True, f"named-vector mismatch (missing core slots: {','.join(missing_core)})")

    target_inv = target.get("invertedIndexConfig") or {}
    actual_inv = actual.get("invertedIndexConfig") or {}
    if target_inv.get("indexNullState", False) and not actual_inv.get(
        "indexNullState", False
    ):
        return (True, "indexNullState=True required but not set")

    return (False, "")


def _drop_and_recreate(
    name: str,
    definition: dict,
    *,
    weaviate_url: Optional[str],
    log_event: Optional[Callable[..., None]],
    reason: str,
) -> None:
    """Drop ``name`` if present, then POST the canonical definition.

    Used by bootstrap_collections when an existing collection's schema
    has diverged from the current spec in a non-additive way (different
    named-vector set, indexNullState missing, legacy single-vector).

    Lossless from the user's perspective: the on-disk `knowledge/**/*.md`
    is the source of truth and the subsequent kg-sync re-ingests.
    Forensic snapshot is captured BEFORE the drop so a mid-drop crash
    leaves a trail.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            log_event(step, phase, detail)

    # Snapshot before destroying — same forensics hook used by
    # migrate_collections's rebuild branch (HIGH-4, 2026-05-01).
    snap = _snapshot_collection_for_rebuild(name, weaviate_url=weaviate_url)
    _log(
        "7b.bootstrap.regen",
        "snapshot",
        f"{name}: pre-drop snapshot ({reason})",
        data={
            "collection": name,
            "reason": reason,
            "object_count": snap["object_count"],
            "sample_uuids": snap["sample_uuids"],
        },
    )

    _delete_class(name, weaviate_url=weaviate_url)
    # Important: definition's `class` field may not match `name` (e.g.
    # case-conflict path where we drop the wrong-cased existing then
    # create with the canonical name). Trust `definition["class"]` for
    # the POST.
    _create_class(definition, weaviate_url=weaviate_url)


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

    ``container_name`` defaults to the lazy-lookup sentinel
    `_DEFAULT_RESTART_CONTAINER`; when passed (or unset), the actual
    name is resolved via `_default_restart_container()` which probes
    `vco_lib.containers` for any matching container on the host.
    """
    import shutil
    import subprocess as _sp

    if container_name == _DEFAULT_RESTART_CONTAINER:
        container_name = _default_restart_container()

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
        "actions": [{"collection": str, "action": "create"|"exists"|"would-create"|"regenerated", "ok": bool}],
        "regenerated": [{"collection": str, "reason": "case-conflict"|"multi-vector"|"legacy-single-vector"|"index-null-state"|"named-vector-mismatch", "dropped_name": str}],
        "errors": [{"collection": str, "error": str}],
      }

    Bug-1 v0.2.4 (2026-05-12): when an existing collection's schema is
    incompatible with the current spec (case-only name conflict, legacy
    multi-vector config, missing indexNullState, etc.) the function
    drops the old collection and recreates with the target schema. The
    Rust caller parses ``regenerated[]`` to drive the banner's
    "Migrating Weaviate schema for X..." state. Lossless: knowledge/**/*.md
    on disk is the source of truth and the subsequent kg-sync step
    re-populates Weaviate.

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
        # Bug-1 v0.2.4 (2026-05-12): collections regenerated due to schema
        # incompatibility. Empty under normal first-install conditions.
        # See _schema_incompatible for the regen trigger conditions.
        "regenerated": [],
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

    # 2. Build the target list. Each tuple carries the canonical target
    # name AND the def-fn (so we can re-derive the spec when regenerating
    # under a case-conflict alias). Order matters: KG first, Dev second,
    # shared KG last.
    targets: list[tuple[str, Callable[[str], dict]]] = [
        (derived["kg_collection"], kg_class_definition),
    ]
    if not kg_only:
        targets.append(
            (derived["development_collection"], development_class_definition),
        )
    # Shared KG: always created when missing (per coordinator: shared KG is
    # READ by every project regardless of per-project opt-out, so creation
    # is unconditional). The opt-out toggle is purely a write-gate enforced
    # at MCP-call time, not a creation gate.
    targets.append((_SHARED_KG_NAME, kg_class_definition))

    # 3. Iterate: existence check + schema probe + POST.
    for name, target_def_fn in targets:
        definition = target_def_fn(name)
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
            # Bug-1 v0.2.4: schema-incompatibility regen. Compare actual
            # schema fields against the canonical target; if non-trivially
            # divergent (different named-vector set, missing
            # indexNullState, legacy single-vector), drop + recreate.
            incompatible, reason = _schema_incompatible(
                existing, target_def_fn, name,
            )
            if not incompatible:
                result["actions"].append({
                    "collection": name, "action": "exists", "ok": True,
                })
                _log("7b.bootstrap", "ok",
                     f"{name}: already exists with compatible schema",
                     data={"collection": name, "action": "exists"})
                continue

            # Regen path. In dry-run mode we report the intent without
            # mutating; the Rust caller surfaces the banner state.
            if dry_run:
                result["regenerated"].append({
                    "collection": name,
                    "reason": _regen_reason_tag(reason),
                    "dropped_name": name,
                    "detail": reason,
                })
                result["actions"].append({
                    "collection": name, "action": "would-regenerate", "ok": True,
                })
                _log("7b.bootstrap", "ok",
                     f"{name}: WOULD regenerate ({reason})",
                     data={"collection": name, "action": "would-regenerate",
                           "reason": reason})
                continue

            try:
                _drop_and_recreate(
                    name, definition,
                    weaviate_url=weaviate_url,
                    log_event=log_event,
                    reason=reason,
                )
                result["regenerated"].append({
                    "collection": name,
                    "reason": _regen_reason_tag(reason),
                    "dropped_name": name,
                    "detail": reason,
                })
                result["actions"].append({
                    "collection": name, "action": "regenerated", "ok": True,
                })
                _log("7b.bootstrap", "ok",
                     f"{name}: regenerated ({reason})",
                     data={"collection": name, "action": "regenerated",
                           "reason": reason})
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("7b.bootstrap", "error",
                     f"{name}: regenerate failed: {err}",
                     data={"collection": name, "error": err,
                           "reason": reason})
                result["actions"].append({
                    "collection": name, "action": "regenerated", "ok": False,
                })
                result["errors"].append({
                    "collection": name,
                    "error": f"regenerate failed ({reason}): {err}",
                })
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
            # Bug-1 v0.2.4: case-only name conflict path. Weaviate POSTs
            # are case-sensitive (so our existence-probe missed the old
            # `<Project>_development` lowercase variant), but the server-
            # side dedup rejects creating `<Project>_Development` with
            # HTTP 422 `class already exists: found similar class "<actual>"`.
            # Extract the actual existing name, drop it, retry the POST.
            err_str = str(e)
            actual_name = _extract_similar_class_name(err_str)
            if actual_name and actual_name != name:
                _log("7b.bootstrap", "warn",
                     f"{name}: case-conflict with existing {actual_name!r}; "
                     f"dropping old and recreating with target name",
                     data={"collection": name,
                           "conflicting_name": actual_name,
                           "branch": "case-conflict"})
                try:
                    _drop_and_recreate(
                        actual_name, definition,
                        weaviate_url=weaviate_url,
                        log_event=log_event,
                        reason=f"case-only name conflict ({actual_name!r} → {name!r})",
                    )
                    result["regenerated"].append({
                        "collection": name,
                        "reason": "case-conflict",
                        "dropped_name": actual_name,
                        "detail": f"existing {actual_name!r} replaced with target name {name!r}",
                    })
                    result["actions"].append({
                        "collection": name, "action": "regenerated", "ok": True,
                    })
                    _log("7b.bootstrap", "ok",
                         f"{name}: regenerated from case-conflict {actual_name!r}",
                         data={"collection": name, "action": "regenerated",
                               "dropped_name": actual_name,
                               "branch": "case-conflict"})
                    continue
                except Exception as e2:
                    err2 = f"{type(e2).__name__}: {e2}"
                    _log("7b.bootstrap", "error",
                         f"{name}: case-conflict recovery failed: {err2}",
                         data={"collection": name, "error": err2,
                               "dropped_name": actual_name,
                               "branch": "case-conflict-failed"})
                    result["actions"].append({
                        "collection": name, "action": "create", "ok": False,
                    })
                    result["errors"].append({
                        "collection": name,
                        "error": (f"case-conflict recovery failed (existing "
                                  f"{actual_name!r}): {err2}"),
                    })
                    continue

            # Generic create-failure path.
            err = f"{type(e).__name__}: {e}"
            _log("7b.bootstrap", "error",
                 f"{name}: create failed: {err}",
                 data={"collection": name, "error": err})
            result["actions"].append({
                "collection": name, "action": "create", "ok": False,
            })
            result["errors"].append({"collection": name, "error": err})

    return result


def _regen_reason_tag(reason: str) -> str:
    """Map a free-form regen reason string to a stable tag the Rust
    caller can dispatch on. Keeps the JSON envelope's ``reason`` field
    finite for UI banner text.
    """
    r = reason.lower()
    if "case" in r:
        return "case-conflict"
    if "single-vector" in r or "no vectorconfig" in r:
        return "legacy-single-vector"
    if "named-vector" in r or "slot" in r:
        return "multi-vector"
    if "indexnullstate" in r:
        return "index-null-state"
    return "schema-mismatch"


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

    # v0.2.15: drive the restart command off the actual host container
    # name (canonical `vco_weaviate`, but `weaviate` or `weaviate_claude`
    # on legacy installs). See vco_lib/containers.py.
    from vco_lib.containers import (
        all_known_names as _all_known_names,
        find_existing_container as _find_existing_container,
    )
    _restart_target = (
        _find_existing_container("weaviate")
        or _all_known_names("weaviate")[0]  # canonical (vco_weaviate)
    )
    _cmd_hint = (
        f"podman start {_restart_target}  # or: docker start {_restart_target}"
    )
    if _find_existing_container("weaviate") is None:
        # No container yet on host. Show the full candidate list so the
        # user can pick whichever one matches their install era.
        _cmd_hint += (
            "  # legacy installs may use one of: "
            + " | ".join(_all_known_names("weaviate")[1:])
        )

    cmd_lines = [
        "# 1. Bring Weaviate up.",
        _cmd_hint,
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
#     "schema_version": 2,
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
#     "preserved_files": {              # schema v2 (2026-05-13): tracks
#                                       # files VCO chose NOT to overwrite.
#       "<rel-path-from-folder>": {
#         "shipped_sha256": "<hex>",    # what VCO would have written.
#         "preserved_at": "ISO-8601",   # most-recent install time the file
#                                       # was preserved.
#         "shipped_source": "<rel>",    # path within orchestrator root.
#         "reason": "preserve|skip-existing",  # update-mode vs first-install.
#       },
#     },
#   }
#
# Schema-version compatibility: readers default `preserved_files` to `{}` when
# absent (v1 manifests). No migration step is required — the next install run
# upgrades the file in place by writing schema_version=2.
# ---------------------------------------------------------------------------


_MANIFEST_REL = Path(".claude") / ".vco-manifest.json"
_MANIFEST_SCHEMA_VERSION = 2


# Placeholder substitutions applied to agent .md files (mirrors
# install.py:5564). Skill .md files use the same map.
#
# PR-2 portability (2026-05-06):
#
# `{{ORCHESTRATOR_ROOT}}` resolves to an ABSOLUTE PATH at install time.
# This is necessary because Claude Code's agent .md frontmatter parses
# YAML mcpServers `command:` fields straight to `execvp()` — shell-style
# `${VAR}` expansion does NOT happen there. Baking the absolute path is
# the only mechanism that lets Claude Code spawn the orchestrator-tools
# MCP. Trade-off: moving the orchestrator clone breaks every project's
# MCP wiring until `install-bundle --update` is rerun (which the launcher
# triggers on rename and adoption). The manifest-driven hash compare in
# `_file_action` already heals stale baked paths when the prior-shipped
# hash matches the installed file (i.e. user hasn't customised it).
#
# `{{VCT_ORCHESTRATOR_ROOT}}` resolves to the LITERAL string
# `${VCT_ORCHESTRATOR_ROOT}` so it can be expanded by shell or by Python
# `os.environ` lookups at run time. Use this placeholder in agent .md
# bodies, hook scripts, or any context where a runtime-relocatable path
# is acceptable.
#
# `{{PROJECT_ROOT}}` (added 2026-05-07, follow-up #9) resolves to the
# project folder being installed into — the directory containing
# `.claude/`, `CLAUDE.md`, the user's source. Use this for agent .md
# bodies that need to reference project-relative paths cleanly without
# hardcoding the full absolute path. `project_root` is None on
# orchestrator self-install (where there's no separate project folder);
# in that case `{{PROJECT_ROOT}}` resolves to the orchestrator root
# itself, since the orchestrator IS its own project at install time.
def _agent_subs(
    orchestrator_root: Path,
    project_root: Path | None = None,
) -> dict[str, str]:
    return {
        "{{ORCHESTRATOR_ROOT}}": str(orchestrator_root),
        "{{PROJECT_ROOT}}": str(project_root if project_root else orchestrator_root),
        "{{PROJECTS_ROOT}}": str(orchestrator_root.parent),
        "{{HOME}}": str(Path.home()),
        # Runtime-resolvable form for shell / Python contexts. The literal
        # ${VCT_ORCHESTRATOR_ROOT} string survives substitution as-is so the
        # consumer expands it at use time. Templates SHOULD prefer this
        # placeholder unless the consumer is a YAML execvp boundary (see
        # the {{ORCHESTRATOR_ROOT}} note above).
        "{{VCT_ORCHESTRATOR_ROOT}}": "${VCT_ORCHESTRATOR_ROOT}",
    }


def _hook_globs_for_os() -> tuple[str, ...]:
    """Return ALL hook flavours to ship — both `.sh` and `.ps1` flavours,
    on every OS.

    History (v0.2.14 fix, audit Concern #2 2026-05-17): we used to ship only
    the host-OS flavour (`.sh` on Linux/macOS; `.ps1` on Windows). That
    broke cross-OS workflows where the same project folder is opened from
    both POSIX and Windows shells (dual-boot, WSL crossover, network-
    mounted projects): orphan stale hooks of the OTHER flavour lingered
    and could be invoked by the unexpected shell. They're text files
    (~few KB each); the runtime picks which extension matches its shell,
    so shipping both is cheap + correct.
    """
    return ("*.sh", "*.ps1")


def _hook_glob_for_os() -> str:
    """Legacy single-glob accessor kept for backward-compat in other
    callsites. Prefer `_hook_globs_for_os()`. Mirrors install.py:5641.
    """
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
    `{"schema_version": ..., "files": {}, "preserved_files": {}}` on
    missing / unparseable file so callers can treat it uniformly.

    Forward-compat: v1 manifests (no `preserved_files` key) read back with
    an empty dict for that section — no migration needed."""
    target = folder / _MANIFEST_REL
    empty = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "files": {},
        "preserved_files": {},
    }
    if not target.exists():
        return dict(empty)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(empty)
        if "files" not in data or not isinstance(data["files"], dict):
            data["files"] = {}
        # preserved_files added in schema v2; default to empty for v1 readers.
        if "preserved_files" not in data or not isinstance(data["preserved_files"], dict):
            data["preserved_files"] = {}
        return data
    except Exception:
        # Corrupt manifest — treat as missing to avoid blocking the install.
        return dict(empty)


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


def _enumerate_bundle_files(
    orchestrator_root: Path,
    project_root: Path | None = None,
) -> list[_BundleFileOp]:
    """Build the list of files to install. OS-aware (hooks pick .sh vs .ps1).

    `project_root` (optional) is the install target folder. When given,
    `{{PROJECT_ROOT}}` placeholder substitution in agent / skill .md
    bodies resolves to that folder. Defaults to the orchestrator root
    (matches old behaviour for orchestrator self-installs).

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
    hook_globs = _hook_globs_for_os()

    # Hooks (top-level only — _lib handled below). Ship BOTH .sh + .ps1
    # flavours so the same .claude/ tree works regardless of which shell
    # the user invokes hooks from (v0.2.14 Concern #2 fix).
    hooks_src = templates / "hooks"
    if hooks_src.exists():
        for glob in hook_globs:
            for hook_file in sorted(hooks_src.glob(glob)):
                if hook_file.parent.name == "_lib":
                    continue
                ops.append(_BundleFileOp(
                    dest_rel=str(Path(".claude") / "hooks" / hook_file.name),
                    source_abs=hook_file,
                    source_rel=str(hook_file.relative_to(orchestrator_root)),
                    transform=None,
                    always_overwrite=False,
                ))

    # Hooks _lib (always overwrite — not user-customisable). Both flavours.
    lib_src = hooks_src / "_lib"
    if lib_src.exists():
        for glob in hook_globs:
            for lib_file in sorted(lib_src.glob(glob)):
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
    subs = _agent_subs(orchestrator_root, project_root)

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

    # v0.2.21 Step 8f: `.vscode/tasks.json` — VS Code folderOpen task
    # that starts vct-hub when the project is opened. Belt-and-braces
    # companion to the Claude Code SessionStart hook (which runs on
    # `claude` invocation). Both invocations are idempotent
    # (`vct-hub --start-if-not-running` exits 0 either way). The
    # manifest-driven hash compare preserves user-customised
    # tasks.json on `--update` so users who maintain their own VS
    # Code tasks won't lose them; new installs get the template.
    vscode_tasks_src = templates / ".vscode" / "tasks.json"
    if vscode_tasks_src.is_file():
        ops.append(_BundleFileOp(
            dest_rel=str(Path(".vscode") / "tasks.json"),
            source_abs=vscode_tasks_src,
            source_rel=str(vscode_tasks_src.relative_to(orchestrator_root)),
            transform=None,
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


def _stale_orchestrator_root_heal_match(
    raw: bytes,
    target_path: Path,
    orchestrator_root: Path,
) -> bool:
    """PR-2 portability heal (2026-05-06).

    Detect the case where an agent .md was install-stamped against an
    OLD orchestrator-clone path (e.g. user moved/renamed the clone) and
    is now stale. If we substitute the SAME placeholders against an
    `old_root` extracted from the installed file and reproduce the
    installed bytes, then the user did NOT customise — they just have
    a stale baked path. Return True in that case so the caller can
    overwrite safely.

    Conservative: returns False on any ambiguity. Only matches when
    the installed file contains a path-shaped string of the form
    `<old_root>/claude_mcp_servers/...` and round-tripping with that
    `old_root` reproduces the file byte-for-byte. False on Windows
    paths (case-insensitive FS makes the round-trip unreliable).
    """
    try:
        installed = target_path.read_bytes()
        installed_text = installed.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return False

    # Look for a baked path of the form `<root>/claude_mcp_servers/`. Use
    # a byte-anchor + back-walk so we don't try to grep arbitrary regex
    # over the whole file. The first match wins; we tolerate at most one
    # candidate orchestrator-root prefix per file.
    needle = "/claude_mcp_servers/"
    idx = installed_text.find(needle)
    if idx <= 0:
        return False
    # Walk back to the start of the path. Acceptable path chars: anything
    # that's not whitespace, quote, colon (YAML separator), or comma.
    end = idx
    start = end
    while start > 0:
        c = installed_text[start - 1]
        if c.isspace() or c in ('"', "'", ":", ",", "(", ")", "<", ">"):
            break
        start -= 1
    if start == end:
        return False
    candidate = installed_text[start:end]
    if not candidate.startswith("/"):
        # POSIX absolute paths only. Skip Windows / relative.
        return False
    old_root = Path(candidate).resolve()
    if old_root == orchestrator_root.resolve():
        # Same path → not a stale-root case (the hash compare would have
        # caught it as noop).
        return False

    # Round-trip: build subs map for the OLD root, transform the source,
    # compare to the installed bytes. If they match, the user didn't
    # touch it; the stale path is the only difference.
    subs = {
        "{{ORCHESTRATOR_ROOT}}": str(old_root),
        "{{PROJECTS_ROOT}}": str(old_root.parent),
        "{{HOME}}": str(Path.home()),
        "{{VCT_ORCHESTRATOR_ROOT}}": "${VCT_ORCHESTRATOR_ROOT}",
    }
    try:
        text = raw.decode("utf-8", errors="replace")
        for k, v in subs.items():
            text = text.replace(k, v)
        round_trip = text.encode("utf-8")
    except Exception:
        return False
    return round_trip == installed


def _agent_or_skill_already_present(
    project_dir: Path, name: str, kind: str,
) -> bool:
    """Return True if the agent/skill is already installed at either the
    enabled or disabled location, so install-bundle should skip copying.

    Mirrors `resolve_kind_paths()` in the Rust launcher-core
    (`vct-launcher-core::db::project_state`). `kind` is 'agent' or
    'skill'. Used by `_file_action` to honour the FS-disable contract:
    a user-disabled file (moved to `.claude/{agents,skills}.disabled/`
    by the launcher GUI) must NOT be resurrected by a bundle update.

    Path math is intentionally pure (no I/O beyond `.exists()`) so the
    helper is cheap to call once per bundle op.
    """
    claude = project_dir / ".claude"
    if kind == "agent":
        # Agents are individual .md files.
        leaf = f"{name}.md"
        return (
            (claude / "agents" / leaf).exists()
            or (claude / "agents.disabled" / leaf).exists()
        )
    if kind == "skill":
        # Skills are whole directories — the name IS the leaf.
        return (
            (claude / "skills" / name).exists()
            or (claude / "skills.disabled" / name).exists()
        )
    return False


def _classify_bundle_op_kind(dest_rel: str) -> Optional[tuple[str, str]]:
    """If `dest_rel` is an agent .md or skill file/dir, return the
    (kind, name) tuple suitable for `_agent_or_skill_already_present`.

    Returns None for hooks, scripts, settings, infra — anything not
    subject to the FS-disable rule.

    Cross-OS: `_BundleFileOp.dest_rel` is built via `str(Path(...))`
    whose separator depends on the host OS (`/` on POSIX, `\\` on
    Windows). Normalise both flavours via `pathlib.PurePosixPath`
    after a backslash-to-slash swap so the classifier works uniformly
    regardless of where the bundle was enumerated.
    """
    # PurePosixPath alone treats `\\` as a literal character, so a
    # Windows-shaped dest_rel ('.claude\\agents\\foo.md') would not split
    # into the expected parts. Normalise to `/` first.
    normalised = dest_rel.replace("\\", "/")
    parts = PurePosixPath(normalised).parts
    # All FS-disable-relevant ops live under .claude/<bucket>/...
    if len(parts) < 3 or parts[0] != ".claude":
        return None
    bucket = parts[1]
    if bucket == "agents" and len(parts) == 3 and parts[2].endswith(".md"):
        # `.claude/agents/<name>.md` — name is the stem (sans `.md`).
        return ("agent", parts[2][:-3])
    if bucket == "skills" and len(parts) >= 3:
        # Skills are recursive; every shipped file lives under
        # `.claude/skills/<name>/...`. Skip the whole skill when its
        # directory has a `.disabled/` counterpart.
        return ("skill", parts[2])
    return None


def _installed_matches_template_history(
    template_source: Path,
    installed_hash: str,
    orchestrator_root: Path,
    *,
    max_commits: int = 50,
) -> bool:
    """v0.2.31 heal: did this file's installed sha match ANY historical
    version of the template under `templates/`? If yes, the file was
    shipped by VCO at some point — the user hasn't edited it, it's just
    stale. Safe to overwrite.

    Bounded git-log walk on the template path. Looks at `git log -p`
    for the path, hashes each historical blob's content, and compares.

    Returns False (= preserve as user-modified) on any error path:
      - orchestrator_root isn't a git repo (tarball install)
      - git isn't on PATH
      - template path not under orchestrator_root
      - git log returns no history (new file not yet committed)

    `max_commits` caps the walk depth (~6 months at typical release
    cadence for this repo). Adjust upward if false-preserves happen.

    Note: this helper covers the `_file_action` "no prior_hash in
    manifest but file exists on disk" case introduced by adding new
    files to the bundle without retro-actively updating manifests on
    existing installs. The discipline for genuinely user-modified
    files (= file content never matched any shipped version) is
    unchanged — those still take the preserve path.
    """
    import subprocess as _sp

    if not orchestrator_root.is_dir():
        return False
    git_dir = orchestrator_root / ".git"
    if not git_dir.exists():
        # Tarball install or non-git source tree. Can't walk history.
        return False
    try:
        rel = template_source.resolve().relative_to(orchestrator_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    rel_str = str(rel).replace("\\", "/")
    # `git log --format=%H` over the path → list of commits touching it.
    # We then `git show <sha>:<path>` for each and sha-256 the bytes.
    try:
        result = _sp.run(
            [
                "git", "-C", str(orchestrator_root),
                "log", f"-{max_commits}", "--pretty=format:%H", "--", rel_str,
            ],
            capture_output=True, text=True, timeout=5.0,
        )
    except (FileNotFoundError, _sp.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    commits = [c.strip() for c in result.stdout.splitlines() if c.strip()]
    if not commits:
        # File never had a commit touching it under this path. Could be
        # legitimately new (uncommitted) or moved/renamed; fall through
        # to default-preserve.
        return False
    for sha in commits:
        try:
            blob = _sp.run(
                [
                    "git", "-C", str(orchestrator_root),
                    "show", f"{sha}:{rel_str}",
                ],
                capture_output=True, timeout=2.0,
            )
        except (FileNotFoundError, _sp.SubprocessError):
            continue
        if blob.returncode != 0:
            continue
        if _bytes_sha256(blob.stdout) == installed_hash:
            return True
    return False


def _file_action(
    op: _BundleFileOp,
    target_path: Path,
    *,
    update_mode: bool,
    manifest: dict,
    orchestrator_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
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
      "skip-disabled"   — FS-disable plan (Wave 2 D, 2026-05-22): the agent
                          or skill exists at the user-disabled location
                          (`.claude/{agents,skills}.disabled/<name>`). Skip
                          copying so the user's disable choice survives
                          bundle updates.

    PR-2 heal (2026-05-06): if `orchestrator_root` is supplied and the
    file was produced via `_apply_subs` (transform present), an installed
    file that round-trips to the source bytes under a DIFFERENT (stale)
    orchestrator root is treated as overwritable — the user moved the
    clone, didn't edit the file. See `_stale_orchestrator_root_heal_match`.
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

    # FS-disable guard (Wave 2 D contract). Before touching disk, check
    # the `.disabled/` companion location for agents and skills. If the
    # user has explicitly disabled this entry via the launcher GUI, the
    # bundle install MUST NOT recreate the enabled-side file — that
    # would silently undo the disable. The guard runs BEFORE the
    # target-existence branches so it works in both first-install and
    # update modes uniformly.
    if project_root is not None:
        kind_name = _classify_bundle_op_kind(op.dest_rel)
        if kind_name is not None:
            kind, name = kind_name
            # The enabled-side check is implicit in "exists" elsewhere;
            # only the disabled-side check is novel. Check it explicitly
            # to surface the skip-disabled action in actions["skip-disabled"].
            disabled_dir = (
                "agents.disabled" if kind == "agent" else "skills.disabled"
            )
            disabled_leaf = (
                f"{name}.md" if kind == "agent" else name
            )
            disabled_path = (
                project_root / ".claude" / disabled_dir / disabled_leaf
            )
            if disabled_path.exists():
                return ("skip-disabled", source_bytes)

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

    # PR-2 heal: stale-orchestrator-root scenario. Only kick in when the
    # file is `_apply_subs`-transformed AND we have a current
    # orchestrator_root to compare against. Doesn't fire for
    # non-substituted files (hooks, scripts, compose) because their
    # transform is None.
    if (
        op.transform is not None
        and orchestrator_root is not None
        and _stale_orchestrator_root_heal_match(raw, target_path, orchestrator_root)
    ):
        return ("overwrite", source_bytes)

    # v0.2.31 heal: manifest-untracked-but-shipped scenario. Pre-v0.2.31
    # the install-bundle manifest only tracked a subset of bundled files
    # (mostly hooks/_lib/* and scripts). Files NOT in the manifest had
    # `prior_hash == ""`, and the default-to-preserve path treated them
    # as user-modified — silently freezing stale shipped versions for
    # release after release. This was THE bug behind v0.2.27→v0.2.29's
    # invisible-keyword-suggest-failure on VCO_dev (PR #259's
    # `keywords:` frontmatter on 97 agents/skills never reached
    # orchestrator-root-style projects).
    #
    # Heuristic: when the manifest has no prior_hash for this path, walk
    # the template file's git history. If the installed sha matches ANY
    # historical shipped sha (current template, any prior commit on the
    # template path), the file is "VCO-shipped, possibly stale" — safe
    # to overwrite. If no match across history → genuinely user-edited
    # → preserve as before.
    #
    # Bounded: walks at most the last 50 commits touching the template
    # path (covers ~6 months of orchestrator history at typical release
    # cadence). Cheap on cold cache (~10-50ms per file); warm git index
    # is faster. Skips gracefully if orchestrator_root isn't a git repo
    # (e.g. tarball install) — falls through to default-to-preserve.
    if (
        not prior_hash
        and orchestrator_root is not None
        and _installed_matches_template_history(
            op.source_abs, installed_hash, orchestrator_root
        )
    ):
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


def _format_file_list_md(paths: list[str], cap: int = 100) -> str:
    """Render a bullet-list of file paths for inclusion in a deferral entry.

    Caps at `cap` entries with a "... and N more" trailer when oversize so
    the deferral .md doesn't grow unbounded for large preserve / skip lists.

    Item 3 (Gap 2, 2026-05-13): cap bumped from 20 to 100. The SD15
    smoking-gun case had 36 preserved files; the old 20-cap silently hid
    the tail. A 100-cap covers every realistic install (the entire
    orchestrator bundle is currently ~114 files) while still bounding
    pathological writes.
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
    # Item 4 (Gap 7, 2026-05-13): emit $VCT_ORCHESTRATOR_ROOT instead of a
    # baked literal path so the command stays portable across machines and
    # surviving orchestrator-clone relocations. The env var is set by
    # `.claude/env` (sourced by every VCO-installed project's tooling); if
    # the user runs from a shell without it, the prose tells them how to
    # set it manually.
    # v0.2.23 B5 (D18 short-term): when a preserved file is likely
    # CLAUDE.md (the common case — the user adds project-specific Dev
    # Constraints / KG conventions to it), the highest-leverage action is
    # NOT "diff manually" but "ask Claude to merge in this project session".
    # Claude has the orchestrator's intent (this CLAUDE.md text) AND the
    # user's project context loaded — it can produce a merged file in
    # seconds that preserves both. We surface that as the FIRST option
    # because it's the only one that scales when CLAUDE.md grows past the
    # 100-line mark and per-file `diff -u` becomes impractical.
    has_claude_md = any(
        Path(p).name.lower() in ("claude.md", "claude.local.md")
        for p in modified_files
    )
    claude_merge_hint = (
        f"# RECOMMENDED for CLAUDE.md / CLAUDE.local.md (the common case):\n"
        f"# open this folder in Claude Code and ask:\n"
        f"#   \"Merge the orchestrator's shipped CLAUDE.md against my local\n"
        f"#    one. Preserve project-specific Dev Constraints / KG conventions\n"
        f"#    but adopt new orchestrator-shipped guidance. Show me the diff\n"
        f"#    before writing.\"\n"
        f"# Claude reads $VCT_ORCHESTRATOR_ROOT/CLAUDE.md and your local one,\n"
        f"# proposes a 3-way merge, and writes the result with your approval.\n"
        f"#\n"
        if has_claude_md else ""
    )
    cmd = (
        f"{claude_merge_hint}"
        f"# Inspect the differences (per file, if you prefer the manual path):\n"
        f"#   diff -u <orchestrator>/<source-rel> {folder}/<dest-rel>\n"
        f"# Run from a shell where `.claude/env` has been sourced (or\n"
        f"# prepend VCT_ORCHESTRATOR_ROOT=/path/to/VCO_dev). Then either\n"
        f"# accept shipped versions (forces overwrite — destroys local edits):\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"\"$VCT_ORCHESTRATOR_ROOT\" --update --force --json\n"
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


def _emit_orphan_preserved_deferral(
    folder: Path, orphan_files: list[str], orchestrator_root: Path,
) -> None:
    """v0.2.24 §A0 audit (2026-05-22): emit
    `bundle_user_modified_deletion_preserved` when files the orchestrator
    PREVIOUSLY shipped (recorded in manifest["files"]) are now absent
    from the new shipped enumeration AND the user has customized them
    vs the prior shipped hash.

    Behavior contract:
    - Files NOT user-modified (hash matches prior shipped) are deleted
      silently — they were always the orchestrator's content, and the
      orchestrator no longer ships them. No deferral.
    - Files user-modified are PRESERVED on disk (we don't destroy
      user content), the manifest entry is kept so a future re-ship
      would recognize the baseline, and a deferral is emitted so a
      Claude session sees that VCO no longer ships these files.

    Severity is `info` — the project is functional; this is purely
    "FYI, you have files VCO no longer manages".
    """
    if not orphan_files:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    files_md = _format_file_list_md(sorted(orphan_files))
    cmd = (
        f"# These files are no longer shipped by the orchestrator but you\n"
        f"# customized them, so VCO preserved them on disk. Options:\n"
        f"#\n"
        f"# (1) Keep them as-is — they're yours now (most common choice):\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id bundle_user_modified_deletion_preserved\n"
        f"#\n"
        f"# (2) Delete them if you no longer need them:\n"
        f"#   rm <path>     # POSIX\n"
        f"#   del <path>    # Windows cmd.exe\n"
        f"# Then run --update again so VCO drops the manifest entry."
    )
    entry = DeferralEntry(
        condition_id="bundle_user_modified_deletion_preserved",
        title="User-modified bundle files preserved after upstream deletion",
        detected=(
            f"During an `install-bundle --update` run, "
            f"{len(orphan_files)} file(s) that VCO previously shipped were "
            f"NOT re-shipped (upstream removed them) AND your local copy "
            f"differs from what VCO originally shipped. They were "
            f"preserved on disk rather than auto-deleted:\n"
            f"{files_md}"
        ),
        why_deferred=(
            "Default-to-safety: VCO does not auto-delete files the user "
            "has modified, even when upstream no longer ships them. You "
            "may have customized these for project-specific use. Use the "
            "options below to either keep them indefinitely or remove "
            "them manually."
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
    # Item 4 (Gap 7, 2026-05-13): emit $VCT_ORCHESTRATOR_ROOT (set by
    # `.claude/env`) instead of a baked literal path so the command is
    # portable across machines / orchestrator clone relocations.
    cmd = (
        f"# Run from a shell where `.claude/env` has been sourced, or\n"
        f"# prepend VCT_ORCHESTRATOR_ROOT=/path/to/VCO_dev. Then accept\n"
        f"# the orchestrator's shipped versions for ALL skipped files:\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"\"$VCT_ORCHESTRATOR_ROOT\" --update --force --json"
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


# ---------------------------------------------------------------------------
# Project-level templates (Item 7 / Observation 7, 2026-05-13)
#
# VCO ships minimal stubs for the three project-level files that aren't
# bundled like hooks/scripts/agents because they're per-project bespoke:
#   - CLAUDE.md             — project-instructions (Claude Code loads on session start)
#   - .claude/CONTEXT_STATE.md — active working memory
#   - MEMORY.md (template)  — auto-memory index (LIVE file is under ~/.claude/projects/...)
#
# Install-time semantics:
#   - File missing in project → write the substituted template as the
#     real file (gives fresh projects a sensible starting point).
#   - File exists → write the substituted template to a sibling reference
#     path under .claude/context/templates/<NAME>.reference.md so the
#     user / Claude can diff. If the existing file meaningfully differs
#     from the reference, emit a `template_review_pending` deferral so
#     future sessions are nudged to review.
#
# Schema-bump compatibility: this is purely additive — projects with no
# CLAUDE.md and no MEMORY.md get fresh stubs; existing projects get a
# `.reference.md` sidecar but their on-disk files are never touched.
# ---------------------------------------------------------------------------

# Each entry: (template filename under templates/, project-relative destination
# for the LIVE file, project-relative destination for the .reference.md sidecar).
_PROJECT_LEVEL_TEMPLATES = (
    (
        "CLAUDE.md.template",
        Path("CLAUDE.md"),
        Path(".claude") / "context" / "templates" / "CLAUDE.md.reference.md",
    ),
    (
        "CONTEXT_STATE.md.template",
        Path(".claude") / "CONTEXT_STATE.md",
        Path(".claude") / "context" / "templates" / "CONTEXT_STATE.md.reference.md",
    ),
    (
        "MEMORY.md.template",
        Path("MEMORY.md"),
        Path(".claude") / "context" / "templates" / "MEMORY.md.reference.md",
    ),
)


def _project_template_subs(
    orchestrator_root: Path,
    project_root: Path,
    project_name: str,
) -> dict[str, str]:
    """Placeholder map for project-level templates. Superset of
    ``_agent_subs`` plus ``{{PROJECT_NAME}}``.

    Plain-text substitution via ``str.replace`` (per coordinator: no fancy
    templating engine). Keep keys delimited so partial matches don't
    accidentally substitute. The orchestrator root is included so the
    auto-generated `--orchestrator-root` lines in CLAUDE.md point at the
    user's actual clone.
    """
    base = _agent_subs(orchestrator_root, project_root)
    base["{{PROJECT_NAME}}"] = project_name
    return base


def _apply_template_subs(buf: bytes, subs: dict[str, str]) -> bytes:
    """Apply placeholder substitutions; UTF-8 in / UTF-8 out (emoji-safe)."""
    text = buf.decode("utf-8", errors="replace")
    for k, v in subs.items():
        text = text.replace(k, v)
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Conditional template primitive (Phase 1.5.B, 2026-05-25)
#
# Mustache-style sections that the project-template renderer strips BEFORE
# the dict-based `{{KEY}}` placeholder pass. Designed for the "is this
# module active for this project?" question — diagrams ships as default-on
# with a user-toggleable opt-out in the launcher's DiagramsTab; future
# modules (RL, MAO, paid packs) reuse the same primitive.
#
# Syntax (whole-line tags):
#
#   {{#if_module_active diagrams}}
#   ... body ...
#   {{/if_module_active}}
#
#   {{#if_module_inactive diagrams}}
#   ... alternate body ...
#   {{/if_module_inactive}}
#
# Rules:
#   - <name> matches [a-z_][a-z0-9_]* (DB module_name convention).
#   - Opening + closing tags must each occupy their own line; the line is
#     removed entirely (not just the directive token), so the rendered
#     output has no orphan blank line where the tag used to be.
#   - When a block is DROPPED, one trailing newline immediately after the
#     closing tag is also consumed so there is no blank-line scar.
#   - Nesting is NOT supported in Phase 1.5.B — opening a new block while
#     already inside one raises TemplateError. (Document this restriction
#     in the docstring + tests; the parser asserts at the point of error.)
#   - Variable substitution inside a kept block is the downstream pass's
#     concern; this primitive only strips/keeps whole blocks.
#   - Unknown opening tags (typo: `{{#if_modul_active ...}}`) → TemplateError.
#   - Unmatched opening or unmatched closing → TemplateError.
#
# All TemplateError messages include the offending 1-based line number so
# callers can point at the broken template directly.
# ---------------------------------------------------------------------------


class TemplateError(ValueError):
    """Raised when a conditional template block is malformed.

    Carries the offending 1-based line number in ``line_no`` so callers
    can quote the location alongside the message.
    """

    def __init__(self, message: str, *, line_no: int | None = None) -> None:
        super().__init__(message)
        self.line_no = line_no


# Regex anchors: a whole-line conditional tag (leading/trailing whitespace
# tolerated, no other content on the line). The `<name>` capture is the
# strict module-name rule from the plan: lowercase identifier.
_TAG_OPEN_ACTIVE_RE = re.compile(
    r"^\s*\{\{#if_module_active\s+([a-z_][a-z0-9_]*)\s*\}\}\s*$"
)
_TAG_OPEN_INACTIVE_RE = re.compile(
    r"^\s*\{\{#if_module_inactive\s+([a-z_][a-z0-9_]*)\s*\}\}\s*$"
)
_TAG_CLOSE_ACTIVE_RE = re.compile(
    r"^\s*\{\{/if_module_active\s*\}\}\s*$"
)
_TAG_CLOSE_INACTIVE_RE = re.compile(
    r"^\s*\{\{/if_module_inactive\s*\}\}\s*$"
)
# Catch-all for typos / unknown directives: any line that looks like a
# mustache-style block tag (`{{#...}}` or `{{/...}}`) and didn't match one
# of the four canonical patterns above. Used for error reporting.
_TAG_UNKNOWN_RE = re.compile(
    r"^\s*\{\{[#/]\s*[A-Za-z][A-Za-z0-9_]*.*\}\}\s*$"
)


def render_conditional_blocks(
    template: str,
    *,
    active_modules: set[str],
) -> str:
    """Strip ``{{#if_module_active <name>}}...{{/if_module_active}}`` blocks
    where ``<name>`` is NOT in ``active_modules``. Strip the
    ``{{#if_module_inactive ...}}`` mirror where ``<name>`` IS in
    ``active_modules``. Remaining ``{{KEY}}`` placeholders are left
    untouched (the downstream dict-substitution pass handles those).

    Args:
        template: Raw template text (UTF-8 decoded already).
        active_modules: Set of module-name strings considered "on" for
            this project (e.g. ``{"diagrams"}``).

    Returns:
        The template with conditional blocks resolved. When a block is
        dropped, one trailing newline immediately after the closing tag
        is also consumed so the output has no blank-line scar.

    Raises:
        TemplateError: malformed tag, mismatched open/close, attempt to
            nest a block, or an unknown ``{{#...}}`` directive. The
            exception's ``line_no`` attribute is the 1-based line of
            the offending tag.
    """
    # Preserve the input's trailing-newline status: splitlines() drops the
    # terminator and we rebuild with "\n".join, so capture whether the
    # original ended in a newline so the round-trip is faithful for
    # template files that conventionally end with one.
    had_trailing_newline = template.endswith("\n")
    lines = template.splitlines()

    # Pass 1: parse into a list of tag events
    # kind in {"open_active", "open_inactive", "close_active", "close_inactive"}.
    events: list[tuple[str, int, str | None]] = []
    for idx, ln in enumerate(lines):
        m = _TAG_OPEN_ACTIVE_RE.match(ln)
        if m:
            events.append(("open_active", idx, m.group(1)))
            continue
        m = _TAG_OPEN_INACTIVE_RE.match(ln)
        if m:
            events.append(("open_inactive", idx, m.group(1)))
            continue
        if _TAG_CLOSE_ACTIVE_RE.match(ln):
            events.append(("close_active", idx, None))
            continue
        if _TAG_CLOSE_INACTIVE_RE.match(ln):
            events.append(("close_inactive", idx, None))
            continue
        # Catch-all: line looks like a mustache block tag but didn't
        # match any canonical pattern. Likely a typo. Report at parse
        # time so the user gets a clear line number.
        if _TAG_UNKNOWN_RE.match(ln):
            raise TemplateError(
                f"Unknown conditional template tag on line {idx + 1}: "
                f"{ln.strip()!r}. Valid tags: "
                f"'{{{{#if_module_active <name>}}}}', "
                f"'{{{{/if_module_active}}}}', "
                f"'{{{{#if_module_inactive <name>}}}}', "
                f"'{{{{/if_module_inactive}}}}'.",
                line_no=idx + 1,
            )

    # Pass 2: walk events, build (start_line, end_line, kind, module_name)
    # blocks. Enforce: no nesting, matched open/close pairs, matching kind.
    blocks: list[tuple[int, int, str, str]] = []
    open_stack: list[tuple[str, int, str]] = []
    for kind, idx, mod_name in events:
        if kind in ("open_active", "open_inactive"):
            if open_stack:
                prev_kind, prev_idx, _ = open_stack[-1]
                raise TemplateError(
                    f"Nested conditional template blocks are not supported "
                    f"in Phase 1.5.B. Line {idx + 1} opens a {kind!r} block "
                    f"while line {prev_idx + 1} ({prev_kind!r}) is still "
                    f"open.",
                    line_no=idx + 1,
                )
            assert mod_name is not None
            open_stack.append((kind, idx, mod_name))
            continue
        # close_active / close_inactive
        if not open_stack:
            raise TemplateError(
                f"Unmatched closing tag on line {idx + 1}: no open "
                f"conditional block to close.",
                line_no=idx + 1,
            )
        prev_kind, prev_idx, prev_mod = open_stack.pop()
        expected_close = (
            "close_active" if prev_kind == "open_active" else "close_inactive"
        )
        if kind != expected_close:
            raise TemplateError(
                f"Mismatched conditional tags: line {prev_idx + 1} opens "
                f"a {prev_kind!r} block but line {idx + 1} closes with "
                f"{kind!r}.",
                line_no=idx + 1,
            )
        blocks.append((prev_idx, idx, prev_kind, prev_mod))

    if open_stack:
        leftover_kind, leftover_idx, _ = open_stack[-1]
        raise TemplateError(
            f"Unmatched opening tag on line {leftover_idx + 1}: "
            f"{leftover_kind!r} block never closed.",
            line_no=leftover_idx + 1,
        )

    # Pass 3: decide keep/drop per line, then materialise the output.
    # A line is dropped if (a) it's an opening/closing tag line, OR
    # (b) it falls inside a block whose body we're dropping. Additionally,
    # when a block is dropped, the single immediately-following line is
    # also consumed if it's blank — avoids the blank-line scar.
    drop = [False] * len(lines)
    for start, end, kind, mod_name in blocks:
        # Tag lines are ALWAYS removed (the user never wants to see
        # literal `{{#if_module_active ...}}` in rendered output, even
        # when the body is kept).
        drop[start] = True
        drop[end] = True
        if kind == "open_active":
            keep_body = mod_name in active_modules
        else:  # open_inactive
            keep_body = mod_name not in active_modules
        if not keep_body:
            for i in range(start + 1, end):
                drop[i] = True
            # Consume one trailing blank line after the closing tag.
            trailing = end + 1
            if trailing < len(lines) and lines[trailing].strip() == "":
                drop[trailing] = True

    out_lines = [ln for ln, d in zip(lines, drop) if not d]
    out = "\n".join(out_lines)
    if had_trailing_newline and (not out.endswith("\n")):
        out += "\n"
    return out


# ---------------------------------------------------------------------------
# Managed-region merge for project-level Markdown files (Phase 1.5.B)
#
# The CLAUDE.md template is rendered into the project's `CLAUDE.md` at
# install time and re-rendered on module-toggle events. Anything OUTSIDE
# the bracketed managed-region (added freely by the user) must survive the
# re-render verbatim — same Bug-4 discipline as the `.claude/settings.json
# env` deep-merge handling (see knowledge/concepts/config-projection-
# contract-2026-05-24.md). The wrapper markers are HTML comments so they
# render invisibly in markdown viewers.
# ---------------------------------------------------------------------------

MANAGED_REGION_OPEN = "<!-- >>>VCO_MANAGED>>> -->"
MANAGED_REGION_CLOSE = "<!-- <<<VCO_MANAGED<<< -->"


def merge_managed_region(
    existing_claude_md: str,
    new_managed_body: str,
) -> str:
    """Merge a freshly-rendered managed body into an existing CLAUDE.md,
    preserving any user-added content outside the markers verbatim.

    Three cases:
      1. Both markers present → replace the body between them. Content
         before the opening marker and after the closing marker is
         preserved verbatim.
      2. Markers absent (older project / fresh empty file) → wrap
         ``new_managed_body`` in markers and prepend it to the existing
         content, separated by a blank line. The prior content is treated
         as below-the-managed-region user material.
      3. Existing content empty → emit only the wrapped managed body.

    Idempotent: feeding the output of one call back in as
    ``existing_claude_md`` with the same ``new_managed_body`` produces the
    same string.

    ``new_managed_body`` is inserted between the marker lines WITHOUT the
    markers themselves — pass the rendered template body, not a
    pre-wrapped string.

    Args:
        existing_claude_md: Current on-disk CLAUDE.md content (UTF-8
            str). May be empty.
        new_managed_body: Newly-rendered template body, WITHOUT the
            wrapping marker lines.

    Returns:
        The merged CLAUDE.md content as a single UTF-8 string. The
        caller is responsible for writing atomically (use
        ``_write_file_atomic`` or equivalent — partial writes to a
        user-readable file would be visible).

    Raises:
        TemplateError: opening marker present but closing marker absent
            (or vice-versa), or closing marker appears before opening.
    """
    # Defensive normalisation: a body the caller built with mixed CRLF
    # would leak \r into the output. Normalise to LF here.
    body = new_managed_body.replace("\r\n", "\n").replace("\r", "\n")
    # No leading/trailing blank lines inside the markers.
    body = body.strip("\n")

    open_idx = existing_claude_md.find(MANAGED_REGION_OPEN)
    close_idx = existing_claude_md.find(MANAGED_REGION_CLOSE)

    if open_idx == -1 and close_idx == -1:
        # Case 2 / 3: no markers present. Wrap the body, prepend.
        wrapped = f"{MANAGED_REGION_OPEN}\n{body}\n{MANAGED_REGION_CLOSE}"
        if existing_claude_md.strip() == "":
            return wrapped + "\n"
        # Existing content becomes "below the managed region" user
        # material. Separate with a blank line; preserve trailing-newline.
        return wrapped + "\n\n" + existing_claude_md

    if open_idx == -1 or close_idx == -1:
        present = "opening" if open_idx != -1 else "closing"
        missing = "closing" if open_idx != -1 else "opening"
        raise TemplateError(
            f"CLAUDE.md has a {present} VCO-managed-region marker but no "
            f"{missing} marker. Refusing to clobber: fix the file by hand "
            f"or restore the missing marker."
        )

    if close_idx < open_idx:
        raise TemplateError(
            "CLAUDE.md's VCO-managed-region markers are out of order "
            "(closing appears before opening). Refusing to clobber."
        )

    # Case 1: both markers present. Replace body in-place.
    # `prefix` = everything up to & including the opening marker.
    # `suffix` = everything from the closing marker onwards.
    prefix = existing_claude_md[: open_idx + len(MANAGED_REGION_OPEN)]
    suffix = existing_claude_md[close_idx:]
    return f"{prefix}\n{body}\n{suffix}"


# ---------------------------------------------------------------------------
# Module-active resolver (Phase 1.5.B)
#
# Reads the launcher SQLite DB's ``project_modules`` table — Phase 1.1
# (sibling agent) lands the actual schema. Until then we ship a STUB
# fallback so render tests can run in isolation: when the DB or table is
# absent, return the default-on module set. ``diagrams`` is included to
# match Phase 1.5's "default-on with opt-out" design.
# ---------------------------------------------------------------------------

# Default-on modules: any module with no row in `project_modules`, OR with
# `enabled=1`, is considered active. The constant lives here so the stub
# fallback and the live-DB path share a single source of truth.
_DEFAULT_ACTIVE_MODULES: frozenset[str] = frozenset({"diagrams"})


def _launcher_db_path() -> Path:
    """Return the path to the launcher SQLite DB.

    Same resolution rule as ``vco_lib.paths.vct_root_dir`` /
    ``templates/scripts/generate-kg-summary.py``:

      1. ``$VCT_STATE_DIR/launcher.db`` if the env var is set.
      2. ``~/.vct/launcher.db`` otherwise.
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom) / "launcher.db"
    return Path.home() / ".vct" / "launcher.db"


def resolve_active_modules(
    project_id: str,
    *,
    db_path: Path | None = None,
) -> set[str]:
    """Return the set of active module names for ``project_id``.

    Reads ``project_modules`` rows from the launcher SQLite DB and treats
    a module as active when ``enabled=1`` OR when no row exists for that
    project + module combination (default-on policy per Phase 1.5).

    STUB BEHAVIOUR (until Phase 1.1's DB migration lands): when the DB
    file or the ``project_modules`` table is absent, returns
    ``_DEFAULT_ACTIVE_MODULES`` so the render pipeline still produces
    sensible output in isolated test environments and on fresh installs.

    Args:
        project_id: The project's UUID-or-slug as stored in
            ``project_modules.project_id``.
        db_path: Override the default ``~/.vct/launcher.db`` resolution
            (used by tests to point at a fixture DB).

    Returns:
        Set of module name strings considered active for this project.
        Always includes default-on modules unless a row explicitly
        disables them (``enabled=0``).
    """
    import sqlite3

    target = db_path if db_path is not None else _launcher_db_path()
    if not target.is_file():
        # Phase 1.1 not yet integrated, or fresh install before launcher
        # has touched the DB. Return defaults.
        return set(_DEFAULT_ACTIVE_MODULES)

    try:
        conn = sqlite3.connect(str(target))
    except sqlite3.Error:
        return set(_DEFAULT_ACTIVE_MODULES)
    try:
        # Probe for the table — Phase 1.1 owns the schema; until it lands
        # the table doesn't exist and we fall back to defaults.
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='project_modules'"
            )
            if cur.fetchone() is None:
                return set(_DEFAULT_ACTIVE_MODULES)
        except sqlite3.Error:
            return set(_DEFAULT_ACTIVE_MODULES)

        try:
            cur = conn.execute(
                "SELECT module_name, enabled FROM project_modules "
                "WHERE project_id = ?",
                (project_id,),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            return set(_DEFAULT_ACTIVE_MODULES)
    finally:
        conn.close()

    # Build the active-set: start with defaults, then apply explicit rows.
    # A row with enabled=0 REMOVES a default-on module from the set; a
    # row with enabled=1 adds (or keeps) the module.
    active = set(_DEFAULT_ACTIVE_MODULES)
    for module_name, enabled in rows:
        if not isinstance(module_name, str):
            continue
        if enabled:
            active.add(module_name)
        else:
            active.discard(module_name)
    return active


def _normalise_for_diff(text: str) -> list[str]:
    """Normalise a file for the "meaningfully differs" check.

    Strips trailing whitespace per line and trims trailing blank lines
    so a one-line whitespace change doesn't flag the file for review.
    Anything beyond whitespace + EOL normalisation counts as a real diff.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Trim trailing all-empty lines.
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _emit_template_review_pending_deferral(
    folder: Path,
    *,
    diverged_files: list[str],
) -> None:
    """Emit `template_review_pending` when project-level template stubs
    differ from the existing on-disk versions.

    Per-project single entry listing all diverged files (mirrors the
    bundle deferral pattern). The user resolves by either updating the
    on-disk file to match the reference, dismissing the deferral, or
    simply ignoring it (severity is `info` — the project is functional;
    this is a "you might want to look at this" nudge, not a blocker).
    """
    if not diverged_files:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    files_md = _format_file_list_md(sorted(diverged_files))
    cmd = (
        f"# Compare each file against VCO's reference template:\n"
        f"#   diff -u {folder}/<file> "
        f"{folder}/.claude/context/templates/<NAME>.reference.md\n"
        f"# Adopt structure/sections you want; keep your project-specific\n"
        f"# content. The reference files refresh on every install run, so\n"
        f"# they always reflect VCO's current shipping shape.\n"
        f"# To silence this nudge without changing anything:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id template_review_pending"
    )
    entry = DeferralEntry(
        condition_id="template_review_pending",
        title="Project-level template review pending",
        detected=(
            f"VCO ships minimal-stub templates for CLAUDE.md, "
            f"CONTEXT_STATE.md, and MEMORY.md to give fresh projects a "
            f"starting point. {len(diverged_files)} file(s) in this "
            f"project meaningfully differ from the current shipping "
            f"reference — that's expected for established projects, but "
            f"you may want to review whether any new sections (e.g. "
            f"`Session Start Discipline`, `KG-First Search Policy`) are "
            f"worth pulling in:\n"
            f"{files_md}"
        ),
        why_deferred=(
            "Project-level files are bespoke (CLAUDE.md sections, "
            "CONTEXT_STATE.md state) — VCO never overwrites them. The "
            "reference templates ship as `.reference.md` sidecars under "
            "`.claude/context/templates/` so you can diff and selectively "
            "adopt."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def _install_project_level_templates(
    folder: Path,
    *,
    orchestrator_root: Path,
    project_name: str,
    dry_run: bool,
) -> dict:
    """Install (or refresh) the three project-level template stubs.

    Returns a result dict for the install_project_bundle response::

        {
          "live_created":  [<rel>...],  # template stub installed as the
                                        # actual project file (was missing).
          "reference_written": [<rel>...],  # .reference.md sidecar refreshed.
          "diverged":      [<rel>...],  # existing file ≠ reference template.
        }

    Idempotent. On every run the reference sidecars are rewritten with
    the current shipping shape (atomic write, no-op when bytes match).
    """
    out = {
        "live_created": [],
        "reference_written": [],
        "diverged": [],
    }

    templates_dir = orchestrator_root / "templates"
    subs = _project_template_subs(orchestrator_root, folder, project_name)

    for template_name, live_rel, ref_rel in _PROJECT_LEVEL_TEMPLATES:
        src = templates_dir / template_name
        if not src.exists():
            # Templates not shipped on this orchestrator clone — skip
            # silently. The bundle pre-install gate (`orchestrator_root`
            # validation) covers the catastrophic case.
            continue

        try:
            raw = src.read_bytes()
        except OSError:
            continue
        # Phase 1.5.B: run the conditional-blocks pre-pass BEFORE the
        # dict-substitution pass. CLAUDE.md.template is the primary
        # consumer (per-module sections); the pre-pass is a no-op on
        # templates that don't contain any conditional tags, so applying
        # it uniformly is safe. Use the project folder as the resolver's
        # project_id — Phase 1.1's launcher DB uses the project folder
        # path (sanitised) as the slug key.
        try:
            active = resolve_active_modules(str(folder))
        except Exception:
            # Defensive: any unexpected resolver failure falls back to
            # defaults so install never breaks.
            active = set(_DEFAULT_ACTIVE_MODULES)
        try:
            raw_text = raw.decode("utf-8", errors="replace")
            rendered_text = render_conditional_blocks(raw_text, active_modules=active)
            raw = rendered_text.encode("utf-8")
        except TemplateError:
            # If the template itself is malformed, skip the conditional
            # pass and let the original bytes flow through. The reference
            # sidecar will surface the issue on diff.
            pass
        substituted = _apply_template_subs(raw, subs)

        live_target = folder / live_rel
        if not live_target.exists():
            # Missing project-level file → install the stub.
            # For CLAUDE.md specifically, wrap the substituted body in
            # the VCO-managed-region markers so future re-renders can
            # safely replace only the managed body (preserving any
            # user-added content below the closing marker).
            if live_rel == Path("CLAUDE.md"):
                wrapped = merge_managed_region(
                    existing_claude_md="",
                    new_managed_body=substituted.decode("utf-8", errors="replace"),
                )
                substituted = wrapped.encode("utf-8")
            if not dry_run:
                try:
                    _write_file_atomic(live_target, substituted)
                except OSError:
                    # Best-effort: skip this template if the write fails;
                    # don't fail the whole install.
                    continue
            out["live_created"].append(str(live_rel))
            # Don't write the reference sidecar in this case — the live
            # file IS the reference at this moment, so a sidecar is
            # redundant. A future install run (after the user edits the
            # live file) will create the sidecar then.
            continue

        # Live file already exists → refresh the reference sidecar.
        ref_target = folder / ref_rel
        if not dry_run:
            try:
                _write_file_atomic(ref_target, substituted)
            except OSError:
                continue
        out["reference_written"].append(str(ref_rel))

        # Compare existing vs reference. "Meaningfully differs" =
        # anything beyond whitespace + trailing-newline normalisation
        # (per coordinator: keep the check simple).
        try:
            existing_text = live_target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Can't read — don't flag for review; the user has a bigger
            # problem than a template diff.
            continue
        reference_text = substituted.decode("utf-8", errors="replace")
        if _normalise_for_diff(existing_text) != _normalise_for_diff(reference_text):
            out["diverged"].append(str(live_rel))

    return out


# ---------------------------------------------------------------------------
# CLAUDE.md re-render entrypoint (Phase 1.5.B)
#
# Wired into the DiagramsTab toggle (Phase 1.3 — sibling): when the user
# flips a module toggle, the launcher's Tauri command
# ``set_project_module_enabled`` calls this CLI via subprocess (Option A
# pattern from Phase 0.B's config_projection — Rust shells out to Python
# for byte-layout authority over template rendering).
#
# Pipeline:
#   1. Read ``templates/CLAUDE.md.template`` from the orchestrator clone.
#   2. ``render_conditional_blocks`` strips per-module sections.
#   3. ``_apply_template_subs`` resolves ``{{PROJECT_NAME}}`` etc.
#   4. ``merge_managed_region`` replaces the body inside the markers
#      while preserving any user-added content outside.
#   5. Atomic write via ``_write_file_atomic``.
# ---------------------------------------------------------------------------


def render_claude_md(
    folder: Path,
    *,
    orchestrator_root: Path,
    project_name: str,
    project_id: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Re-render ``<folder>/CLAUDE.md`` from the orchestrator template,
    preserving any user content outside the VCO-managed-region markers.

    Used by the launcher's ``set_project_module_enabled`` Tauri command
    (via the ``re-render-claude-md`` CLI subcommand) when the user
    toggles a module on/off in DiagramsTab or any future per-module
    settings UI.

    Idempotent on the managed body: feeding the same active-modules set
    in twice produces byte-identical output.

    Args:
        folder: Target project folder containing (or about to contain)
            ``CLAUDE.md``.
        orchestrator_root: Orchestrator clone root (source of the
            ``templates/CLAUDE.md.template`` file).
        project_name: Display name used to resolve ``{{PROJECT_NAME}}``.
        project_id: Project id/slug used to look up
            ``project_modules`` rows. Defaults to ``str(folder)`` so the
            stub resolver (no DB) returns default-on modules — matches
            the install-time behaviour.
        db_path: Override the default ``~/.vct/launcher.db`` resolution
            (used by tests).

    Returns:
        A result dict::

            {
              "wrote_path": "<abs path>",
              "active_modules": [<sorted module names>],
              "managed_region_present_before": bool,
              "rendered_bytes": <int>,
            }

    Raises:
        FileNotFoundError: ``templates/CLAUDE.md.template`` missing on
            the orchestrator clone.
        TemplateError: malformed conditional tag or out-of-order markers
            in the existing CLAUDE.md.
        OSError: write failure (atomic-write: no partial file on disk).
    """
    template_path = orchestrator_root / "templates" / "CLAUDE.md.template"
    if not template_path.is_file():
        raise FileNotFoundError(
            f"CLAUDE.md template not found at {template_path}. The "
            f"orchestrator clone may be incomplete; re-run install.py "
            f"--update."
        )

    raw_bytes = template_path.read_bytes()
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    # Resolve active modules. Default project_id to the folder path so
    # the stub resolver (no DB) returns the default-on set — matches
    # install-time behaviour.
    effective_project_id = project_id if project_id is not None else str(folder)
    active = resolve_active_modules(effective_project_id, db_path=db_path)

    # Pipeline: conditional → substitution.
    rendered = render_conditional_blocks(raw_text, active_modules=active)
    subs = _project_template_subs(orchestrator_root, folder, project_name)
    rendered_bytes = _apply_template_subs(rendered.encode("utf-8"), subs)
    rendered_body = rendered_bytes.decode("utf-8", errors="replace")

    # Read existing CLAUDE.md (may not exist).
    target = folder / "CLAUDE.md"
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
    else:
        existing = ""

    had_markers = (
        MANAGED_REGION_OPEN in existing and MANAGED_REGION_CLOSE in existing
    )

    merged = merge_managed_region(
        existing_claude_md=existing,
        new_managed_body=rendered_body,
    )

    merged_bytes = merged.encode("utf-8")
    _write_file_atomic(target, merged_bytes)

    return {
        "wrote_path": str(target),
        "active_modules": sorted(active),
        "managed_region_present_before": had_markers,
        "rendered_bytes": len(merged_bytes),
    }


def _run_rl_client_setup(folder: Path) -> dict:
    """Run the per-project ``rl_client_setup`` script for the platform.

    Picks ``rl_client_setup.sh`` on POSIX, ``rl_client_setup.ps1`` on
    Windows. The script lives in ``<folder>/.claude/scripts/`` (copied
    earlier in the bundle install via the script-glob loop). When the
    script is missing or the platform shell isn't available, this is a
    no-op and returns ``{}``.

    Soft-fail: any subprocess failure is captured into the returned
    dict (``{"ok": false, "stderr": ...}``); we never raise out so the
    rest of the install completes.

    Returns:
        Empty dict when no script was run.
        ``{"ok": True, "script": <abs>, "stdout": <str>, "stderr": <str>}``
        on success. ``ok: False`` on failure (still soft).
    """
    import subprocess
    import sys as _sys

    scripts_dir = folder / ".claude" / "scripts"
    if not scripts_dir.exists():
        return {}

    is_windows = _sys.platform.startswith("win") or _sys.platform == "cygwin"
    if is_windows:
        candidate = scripts_dir / "rl_client_setup.ps1"
        if not candidate.exists():
            return {}
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(candidate)]
    else:
        candidate = scripts_dir / "rl_client_setup.sh"
        if not candidate.exists():
            return {}
        cmd = ["bash", str(candidate)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(folder),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"ok": False, "script": str(candidate), "error": str(exc)}

    return {
        "ok": proc.returncode == 0,
        "script": str(candidate),
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip()[:500],
        "stderr": (proc.stderr or "").strip()[:500],
    }


def _emit_migrate_required_deferral(
    folder: Path,
    *,
    project_name: str,
    weaviate_url: str,
    plan_entries: list[dict],
) -> None:
    """Emit `schema_migration_required`: a Weaviate dry-run plan revealed
    one or more collections need `copy` or `rebuild` to reach the target
    schema. Both are destructive (drop+recreate the collection),
    so we DO NOT auto-apply them — we surface a deferral entry that names
    each collection + its required action and tells the user the explicit
    command to consent.

    Args:
        folder: target user-project folder.
        project_name: raw project name (the user-facing label).
        weaviate_url: the URL the dry-run probed (echoed in the command_to_apply).
        plan_entries: list of `{"collection", "action"}` dicts where action is
            in {"copy", "rebuild"}. Anything filtered before this call.

    Severity is `warning`: the project is functional with the existing schema
    (read paths still work), but new schema features (e.g. `index_null_state`)
    are missing until the user explicitly consents to migrate.
    """
    if not plan_entries:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    # Render the per-collection action plan as a bullet list. Sorted for
    # determinism so deferral .md doesn't churn between runs that produce
    # the same plan in different order.
    detected_lines = []
    has_rebuild = False
    for entry in sorted(plan_entries, key=lambda e: (e.get("collection") or "", e.get("action") or "")):
        coll = entry.get("collection") or "?"
        action = entry.get("action") or "?"
        if action == "rebuild":
            has_rebuild = True
            detected_lines.append(
                f"  - `{coll}` → **rebuild** (drop + re-embed; legacy single-vector format)"
            )
        else:
            detected_lines.append(
                f"  - `{coll}` → **copy-with-vectors** (atomic swap; ~30s per collection)"
            )

    # Build the suggested command. `vco_lib` lives in the ORCHESTRATOR
    # clone's venv (NOT this project's venv) — running `python -m
    # vco_lib.project_init ...` from the project directory fails with
    # ModuleNotFoundError. The command below uses an explicit
    # `cd $VCT_ORCHESTRATOR_ROOT && .venv/bin/python -m ...` invocation
    # so the user (or an LLM agent reading this) doesn't have to figure
    # out the venv plumbing. `--name '<project>'` scopes the migration
    # to THIS project's collections regardless of where the orchestrator
    # clone lives. (v0.2.18 doc fix 2026-05-19: prior wording assumed
    # the user knew to run from VCT_ORCHESTRATOR_ROOT.)
    #
    # `--force-rebuild` is only mentioned when the plan actually has a
    # rebuild entry — otherwise the smart copy path handles it without
    # the escape hatch.
    if has_rebuild:
        cmd = (
            f"# Run the migration from the orchestrator clone (vco_lib lives there,\n"
            f"# NOT in this project's venv). The --name flag scopes the work to\n"
            f"# THIS project's collections. Preserves vectors via copy where possible,\n"
            f"# falls back to drop+re-embed for legacy single-vector collections.\n"
            f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
            f"--name {project_name!r} --weaviate-url {weaviate_url!r} --json\n"
            f"# OR force the destructive drop+re-embed for ALL collections (slower,\n"
            f"# requires Ollama embedding service to be healthy; ~3-5 min):\n"
            f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
            f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
            f"--force-rebuild --json"
        )
    else:
        cmd = (
            f"# Run the migration from the orchestrator clone (vco_lib lives there,\n"
            f"# NOT in this project's venv). The --name flag scopes the work to\n"
            f"# THIS project's collections. Atomic copy-with-vectors swap;\n"
            f"# preserves all UUIDs + vectors + WikiLink cross-references.\n"
            f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
            f"--name {project_name!r} --weaviate-url {weaviate_url!r} --json"
        )

    entry = DeferralEntry(
        condition_id="schema_migration_required",
        title="Schema migration required",
        detected=(
            f"A pre-update dry-run of `migrate-collections` against "
            f"`{weaviate_url}` reported one or more per-project Weaviate "
            f"collections need a destructive migration to reach the current "
            f"target schema:\n"
            + "\n".join(detected_lines)
        ),
        why_deferred=(
            "Schema drift detected. `copy` and `rebuild` actions modify "
            "Weaviate state in ways that are not silently reversible "
            "(`copy` drops the collection mid-swap; `rebuild` re-embeds "
            "every object via Ollama). PR 5's update flow defers this so "
            "the user explicitly consents — the bundle install (hooks, "
            "agents, scripts) still proceeds and is unaffected."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def _cleanup_legacy_bash_env_in_project(folder: Path) -> dict:
    """Idempotent cleanup of pre-0.2.11 BASH_ENV lean-ctx shim in a user project.

    Parallel of install.py:_cleanup_legacy_bash_env_shim for the orchestrator
    self-update path. Called from `install_project_bundle` during update_mode
    so the launcher's "Update bundle" button on existing user projects also
    strips the fork-bomb fuse left over from pre-0.2.11 installs.

    Pre-0.2.11 installs of user projects could end up with `BASH_ENV` wired
    in `<project>/.claude/settings.json` (either propagated by an old
    settings template that we no longer ship, or pasted by a user copying
    from the orchestrator's own settings.json). Independently, the
    `<project>/.claude/scripts/leanctx-bash-env.sh` file may exist as a
    leftover from a previous bundle copy. Both are fork-bomb-prone on
    lean-ctx 3.x (see knowledge/concepts/lean-ctx-shim-disabled.md) and
    must be neutralized.

    What this function does:
    - Strips `env.BASH_ENV` from `<project>/.claude/settings.json` if the
      value points at the project-local shim path. Keys pointing at
      unrelated paths (user tooling) are left alone.
    - Does NOT rewrite the shim file itself; that's already handled by the
      regular bundle copy step, which overwrites
      `<project>/.claude/scripts/leanctx-bash-env.sh` with the disabled
      template body via `_enumerate_bundle_files`.
    - Emits a deferral entry only when the cleanup cannot be applied (file
      readonly, JSON parse error, unrecognized BASH_ENV value pointing
      elsewhere). The normal success path is silent (the caller logs from
      its own context).

    Soft-fail throughout: any error returns a dict describing what
    happened so the caller can record it in `result["warnings"]` /
    `result["errors"]` without raising.

    Returns:
        ``{"action": "removed"|"absent"|"left-alone"|"unparseable"|"write-failed",
           "detail": <free text>}``
    """
    settings_file = folder / ".claude" / "settings.json"
    shim_rel = folder / ".claude" / "scripts" / "leanctx-bash-env.sh"

    if not settings_file.exists():
        return {"action": "absent", "detail": "settings.json not present"}

    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {
            "action": "unparseable",
            "detail": f"{type(e).__name__}: {e}",
        }

    if not isinstance(settings, dict):
        return {"action": "unparseable", "detail": "settings.json root not a dict"}

    env_block = settings.get("env")
    if not isinstance(env_block, dict) or "BASH_ENV" not in env_block:
        return {"action": "absent", "detail": "no BASH_ENV in settings.env"}

    raw_val = str(env_block.get("BASH_ENV", ""))
    points_at_shim = (
        "leanctx-bash-env.sh" in raw_val
        or raw_val.endswith(str(shim_rel))
    )
    if not points_at_shim:
        return {
            "action": "left-alone",
            "detail": (
                f"BASH_ENV={raw_val!r} points elsewhere (user tooling); "
                "not touching"
            ),
        }

    env_block.pop("BASH_ENV", None)
    try:
        _write_file_atomic(
            settings_file,
            (json.dumps(settings, indent=2) + "\n").encode("utf-8"),
        )
    except OSError as e:
        return {
            "action": "write-failed",
            "detail": f"{type(e).__name__}: {e}",
        }

    return {
        "action": "removed",
        "detail": (
            "stripped BASH_ENV (was pointing at the disabled "
            "leanctx-bash-env.sh shim)"
        ),
    }


# ---------------------------------------------------------------------------
# v0.2.24 RL-defect-2026-05-22 Fix 2 (cleanup hygiene):
#
# Pre-v0.2.12 the launcher's Rust `write_project_env_files` wrote a
# `claude-code.env` sub-object inside `.vscode/settings.json` containing
# MCP_WEAVIATE_SERVER / MCP_PYTHON / MCP_OLLAMA_SERVER / MCP_PYTHONPATH
# absolute paths. PR-27 (v0.2.12, 2026-05-16) removed that write because
# the `claude-code.env` channel did NOT propagate to MCP subprocesses on
# Linux Claude Code 2.1.143 — the canonical channel since then is
# `.claude/settings.json` `env`. But existing projects (SD15, VCO_dev,
# any pre-v0.2.12 install) still have the legacy MCP_* keys in their
# `.vscode/settings.json`. The keys are INERT (don't propagate), but
# they:
#   1. Confuse audits — anyone grepping for MCP path config finds a
#      stale absolute path pointing at an orchestrator clone the user
#      may have moved, renamed, or deleted.
#   2. Have the user's username + on-disk layout baked in, which is a
#      minor privacy / disclosure consideration if the `.vscode/`
#      directory is shared via git or screenshot.
#
# Detection-only with deferral. Per user policy 2026-05-22: never
# auto-overwrite user-edited files — emit a deferral entry recommending
# cleanup, let the user decide. The legacy MCP_* keys are inert so
# there's no urgency; deferral severity is `info`.
# ---------------------------------------------------------------------------

_LEGACY_VSCODE_MCP_ENV_KEY_PREFIXES: tuple[str, ...] = (
    "MCP_WEAVIATE_SERVER",
    "MCP_PYTHON",
    "MCP_OLLAMA_SERVER",
    "MCP_PYTHONPATH",
)


def _detect_legacy_vscode_mcp_env_keys(folder: Path) -> dict:
    """Detect pre-v0.2.12 MCP_* keys lingering in a project's
    `.vscode/settings.json` `claude-code.env` block.

    Returns:
        dict with:
          - action: "none" (no .vscode dir / file / parseable JSON / no
                   keys) | "detected" (≥1 legacy key found) | "unparseable"
          - keys: list[str] of detected key names (empty when action != "detected")
          - file: relative path string (for the deferral message)
    """
    settings_file = folder / ".vscode" / "settings.json"
    if not settings_file.exists():
        return {"action": "none", "keys": [], "file": ".vscode/settings.json"}

    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Don't surface a deferral on unparseable JSON — the file may
        # have trailing-comma user edits, and our cleanup logic should
        # never push the user toward fixing JSON syntax just so we can
        # check for hygiene-only keys.
        return {"action": "unparseable", "keys": [], "file": ".vscode/settings.json"}

    if not isinstance(data, dict):
        return {"action": "unparseable", "keys": [], "file": ".vscode/settings.json"}

    env_block = data.get("claude-code.env")
    if not isinstance(env_block, dict):
        return {"action": "none", "keys": [], "file": ".vscode/settings.json"}

    detected: list[str] = []
    for key in env_block.keys():
        if not isinstance(key, str):
            continue
        if key in _LEGACY_VSCODE_MCP_ENV_KEY_PREFIXES:
            detected.append(key)

    if not detected:
        return {"action": "none", "keys": [], "file": ".vscode/settings.json"}

    return {
        "action": "detected",
        "keys": sorted(detected),
        "file": ".vscode/settings.json",
    }


def _emit_legacy_vscode_mcp_env_deferral(
    folder: Path, detection: dict,
) -> None:
    """Emit `legacy_vscode_mcp_env_keys_present`: pre-v0.2.12
    `.vscode/settings.json claude-code.env` block contains stale
    absolute-path MCP_* keys that are inert (don't propagate to MCP
    subprocesses on Linux Claude Code) but bake the user's on-disk
    layout into the project tree.

    Per user policy (2026-05-22): never auto-overwrite user-edited
    files — emit a deferral, let the user decide.

    Severity is `info`: the keys are functionally inert. Cleanup is
    hygiene, not a correctness fix.
    """
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    keys = detection.get("keys", [])
    settings_rel = detection.get("file", ".vscode/settings.json")

    detected_msg = (
        f"`{settings_rel}` contains a pre-v0.2.12 `claude-code.env` block "
        f"with {len(keys)} legacy MCP path key(s) "
        f"({', '.join(keys)}). Since v0.2.12 (PR-27) this channel no "
        "longer propagates to MCP subprocesses on Linux Claude Code; the "
        "canonical per-project MCP env channel is `.claude/settings.json` "
        "`env`. The legacy keys are functionally inert but bake the "
        "user's on-disk layout into the project tree (potential "
        "confusion for audits and a minor disclosure consideration if "
        "the `.vscode/` directory is shared via git or screenshot)."
    )

    # Cleanup recipe — operator-driven, NOT auto. The recipe uses jq to
    # surgically delete just the offending keys, preserving the rest of
    # the file (in case the user customised it).
    settings_path_str = f"{folder}/{settings_rel}"
    jq_filter = (
        '.["claude-code.env"] |= (with_entries(select('
        + " and ".join(f'.key != "{k}"' for k in keys)
        + ")))"
    )
    cmd = (
        f"# Inspect first:\n"
        f"cat {settings_path_str!r} | jq '.[\"claude-code.env\"]'\n"
        f"# Then prune just the legacy MCP_* keys (preserves the rest):\n"
        f"jq {jq_filter!r} {settings_path_str!r} > {settings_path_str!r}.tmp \\\n"
        f"  && mv {settings_path_str!r}.tmp {settings_path_str!r}\n"
        f"# Then dismiss this deferral via the launcher GUI OR:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id legacy_vscode_mcp_env_keys_present"
    )

    entry = DeferralEntry(
        condition_id="legacy_vscode_mcp_env_keys_present",
        title="Legacy .vscode/settings.json MCP_* env keys (inert, cleanup recommended)",
        detected=detected_msg,
        why_deferred=(
            "Per the v0.2.24 RL-defect investigation (2026-05-22), "
            "these keys are confirmed INERT — Claude Code spawns MCPs "
            "from ~/.claude.json registrations, which already point at "
            "the active orchestrator clone. The legacy `claude-code.env` "
            "channel was removed from the Rust launcher's writer in "
            "PR-27 (v0.2.12, 2026-05-16) because empirical sentinel "
            "testing confirmed it did NOT reach MCP subprocesses on "
            "Linux. Cleanup is hygiene-only — never auto-applied to "
            "respect the user-edited-files policy."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[
            "knowledge/concepts/rl-telemetry-silent-suppression-on-schema-failure.md",
        ],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def _emit_bash_env_cleanup_deferral(
    folder: Path, cleanup_result: dict,
) -> None:
    """Emit `legacy_bash_env_cleanup_pending`: the 0.2.11 cleanup couldn't
    finish (settings.json unparseable, write blocked by file perms, etc.).

    Severity is `warning`: the project is functionally OK as long as the
    BASH_ENV pointer remains in settings.json (it'd only cause harm if the
    .claude/scripts/leanctx-bash-env.sh shim was still active, which the
    bundle copy step disables in the same run). But Claude Code sessions
    should be nudged to resolve this so a future scenario — manual edit
    re-enabling the shim, or a shim restored from git — doesn't fork-bomb.

    Per-project grouping: a single entry covers any failed cleanup state.
    The action field in `cleanup_result` is encoded into the detected
    block so the operator can tell at a glance what went wrong.
    """
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    action = cleanup_result.get("action", "unknown")
    detail = cleanup_result.get("detail", "")
    settings_rel = ".claude/settings.json"
    shim_rel = ".claude/scripts/leanctx-bash-env.sh"

    if action == "unparseable":
        detected = (
            f"During the 0.2.11 legacy BASH_ENV cleanup, "
            f"`{settings_rel}` could not be parsed as JSON "
            f"({detail}). The cleanup was skipped to avoid corrupting "
            f"user state."
        )
        cmd = (
            f"# Inspect / fix the JSON, then re-run the bundle update:\n"
            f"cat {folder}/{settings_rel} | python -m json.tool\n"
            f"python -m vco_lib.project_init install-bundle "
            f"--folder {str(folder)!r} --update --json"
        )
    elif action == "write-failed":
        detected = (
            f"During the 0.2.11 legacy BASH_ENV cleanup, the write to "
            f"`{settings_rel}` failed ({detail}). Most common cause is "
            f"a read-only file system or restrictive permissions."
        )
        cmd = (
            f"# Fix permissions and re-run the bundle update:\n"
            f"chmod u+w {folder}/{settings_rel}\n"
            f"python -m vco_lib.project_init install-bundle "
            f"--folder {str(folder)!r} --update --json"
        )
    else:
        detected = (
            f"During the 0.2.11 legacy BASH_ENV cleanup, an unexpected "
            f"state was reached (action={action!r}, detail={detail!r}). "
            f"Manual inspection recommended."
        )
        cmd = (
            f"# Inspect the BASH_ENV state by hand:\n"
            f"python -c \"import json; "
            f"print(json.load(open({str(folder / settings_rel)!r}))"
            f".get('env', {{}}).get('BASH_ENV'))\"\n"
            f"# Then re-run after resolving:\n"
            f"python -m vco_lib.project_init install-bundle "
            f"--folder {str(folder)!r} --update --json"
        )

    entry = DeferralEntry(
        condition_id="legacy_bash_env_cleanup_pending",
        title="Legacy BASH_ENV lean-ctx shim cleanup pending",
        detected=detected,
        why_deferred=(
            "0.2.11 disabled the BASH_ENV → leanctx-bash-env.sh shim "
            "(fork-bomb risk on lean-ctx 3.x — incident 2026-04-30 + "
            "recidiva 2026-05-15, see knowledge/concepts/"
            "lean-ctx-shim-disabled.md). The orchestrator tried to "
            f"strip the legacy `BASH_ENV` key from `{settings_rel}` "
            "during this update and was blocked. The shim file at "
            f"`{shim_rel}` was still disabled in-place by the bundle "
            "copy step, so the immediate fork-bomb risk is contained — "
            "but the dangling BASH_ENV reference should be removed "
            "before a future change reactivates the shim."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[
            "knowledge/concepts/lean-ctx-shim-disabled.md",
        ],
    )
    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


# ---------------------------------------------------------------------------
# PR-22 (v0.2.12, 2026-05-16): legacy `docker-compose.override.yml` rename
#
# PR-10A (v0.2.11) shipped writing the launcher-managed compose override at
# `infrastructure/docker-compose.override.yml`, but that filename is NOT
# auto-loaded by podman-compose (it only auto-loads
# `compose.override.yaml`/`.yml`). The companion boot-script change in
# `scripts/launch-claude-mcp-stack.sh` now emits `-f compose.override.yaml`
# explicitly; this helper handles existing on-disk legacy files so an
# `install.py --update` from v0.2.11 migrates them in place.
#
# Scope:
#   - `<install_root>/infrastructure/docker-compose.override.yml`
#   - `<install_root>/claude_mcp_servers/docker-compose.override.yml`
#     (hand-edited legacy location some users have).
#
# Behavior (per directory):
#   - Legacy file absent → no-op.
#   - Legacy file present + target `compose.override.yaml` absent → rename
#     in place, emit a `compose_override_renamed` deferral so the operator
#     can see the migration on next session.
#   - Both present → conflict; do NOT rename, emit
#     `compose_override_filename_conflict` so the operator resolves
#     manually.
#
# Idempotent + soft-fail: subsequent runs find the legacy file gone and
# silently no-op. Permission / disk errors are caught and surfaced as a
# warning-severity deferral, never raised.
# ---------------------------------------------------------------------------


_LEGACY_COMPOSE_OVERRIDE_NAME = "docker-compose.override.yml"
_CANONICAL_COMPOSE_OVERRIDE_NAME = "compose.override.yaml"
_COMPOSE_OVERRIDE_SEARCH_SUBDIRS = ("infrastructure", "claude_mcp_servers")


def _detect_and_rename_legacy_compose_override(install_root: Path) -> Optional[dict]:
    """Detect any legacy `docker-compose.override.yml` files under
    `install_root` and rename them to `compose.override.yaml` so
    podman-compose's auto-loader recognizes them.

    Searches the directories listed in `_COMPOSE_OVERRIDE_SEARCH_SUBDIRS`
    (currently `infrastructure/` and `claude_mcp_servers/`). For each
    legacy file found:

    - If the target `compose.override.yaml` already exists in the same
      directory: emit a `compose_override_filename_conflict` deferral
      entry listing both paths. Do NOT rename — the operator resolves
      manually (the legacy and canonical files may have diverged).
    - Else: rename via `Path.rename()`. Emit a `compose_override_renamed`
      deferral entry naming both the old and new absolute paths so the
      operator can see the migration in the next-session report.

    Idempotent: calling this on a tree with no legacy files is a no-op
    (returns `None`). Calling it after a successful rename also returns
    `None` on the next run.

    Soft-fail: `PermissionError`, `OSError` (disk full, FS read-only,
    cross-device link), and any other rename failure is caught and
    converted into a `compose_override_rename_failed` deferral. The
    install must still complete.

    Args:
        install_root: Absolute path to the orchestrator install root
            (typically `Path(__file__).resolve().parent.parent` from
            `install.py`).

    Returns:
        A dict shaped like ``{"action": "<...>", "renamed": [paths...],
        "conflicts": [(old, new), ...], "errors": [(path, err), ...]}``
        when at least one legacy file was detected, else ``None``. The
        caller is expected to log + emit deferrals based on this; this
        function itself emits the deferral entries directly so
        callers can stay terse.

    PR-22 (2026-05-16). See:
    - knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md
    - .claude/context/PUBLIC_REPO_FIXES_REPORT_2026-05-16.md (Fixes 1, 2, 11)
    """
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    install_root = Path(install_root)
    renamed: list[tuple[Path, Path]] = []
    conflicts: list[tuple[Path, Path]] = []
    errors: list[tuple[Path, str]] = []

    for subdir in _COMPOSE_OVERRIDE_SEARCH_SUBDIRS:
        legacy_path = install_root / subdir / _LEGACY_COMPOSE_OVERRIDE_NAME
        if not legacy_path.is_file():
            continue
        target_path = install_root / subdir / _CANONICAL_COMPOSE_OVERRIDE_NAME
        if target_path.exists():
            # Conflict: user has both. Don't overwrite their canonical
            # file with the legacy one (or vice versa) — emit a
            # deferral and let the operator resolve manually.
            conflicts.append((legacy_path, target_path))
            continue
        try:
            legacy_path.rename(target_path)
            renamed.append((legacy_path, target_path))
        except (OSError, PermissionError) as exc:
            # Soft-fail: log + record. Most common cause is read-only FS
            # (e.g. user mounted the install root noexec/ro for hardening).
            errors.append((legacy_path, f"{type(exc).__name__}: {exc}"))

    if not renamed and not conflicts and not errors:
        return None

    # Emit deferral entries (separate condition_id per outcome class so
    # the operator can see at a glance what happened).
    report = DeferralReport.read(install_root)

    if renamed:
        renamed_lines = "\n".join(
            f"- `{old}` → `{new}`" for old, new in renamed
        )
        report.add_entry(DeferralEntry(
            condition_id="compose_override_renamed",
            title="Legacy compose override renamed to podman-compose auto-load name",
            detected=(
                "One or more `docker-compose.override.yml` files were "
                "detected at `install_root` subdirectories and renamed "
                "in place to `compose.override.yaml` so podman-compose's "
                "auto-loader recognizes them. Renames:\n"
                f"{renamed_lines}"
            ),
            why_deferred=(
                "podman-compose only auto-loads override files named "
                "`compose.override.yaml` / `compose.override.yml`; the "
                "legacy Docker-Compose-v1 name `docker-compose.override.yml` "
                "is NOT auto-loaded. PR-10A (v0.2.11) shipped writing the "
                "wrong filename; users picking 'bind mount' mode in the "
                "Storage Settings GUI got a confirmation but the override "
                "was silently ignored at boot."
            ),
            command_to_apply=(
                "# No action required — the rename has already been applied.\n"
                "# Verify the canonical files exist:\n"
                + "\n".join(f"ls -la {new}" for _old, new in renamed)
            ),
            severity="info",
            kg_node_refs=[
                "knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md",
            ],
        ))

    if conflicts:
        conflict_lines = "\n".join(
            f"- legacy: `{old}` -- canonical: `{new}`"
            for old, new in conflicts
        )
        report.add_entry(DeferralEntry(
            condition_id="compose_override_filename_conflict",
            title="Both legacy and canonical compose override files present",
            detected=(
                "Detected a legacy `docker-compose.override.yml` AND a "
                "canonical `compose.override.yaml` in the same directory. "
                "The rename was NOT applied (the canonical file may already "
                "carry user changes that differ from the legacy file). "
                "Conflicting pairs:\n"
                f"{conflict_lines}"
            ),
            why_deferred=(
                "Auto-merging override YAML is unsafe — the two files may "
                "encode different volume sources, ports, or service "
                "additions. The operator must compare them and pick one."
            ),
            command_to_apply=(
                "# Compare each conflicting pair, then delete whichever "
                "is stale:\n"
                + "\n".join(
                    f"diff -u {old} {new}\n"
                    f"# Then `rm` the one you do NOT want to keep."
                    for old, new in conflicts
                )
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md",
            ],
        ))

    if errors:
        error_lines = "\n".join(
            f"- `{path}`: {err}" for path, err in errors
        )
        report.add_entry(DeferralEntry(
            condition_id="compose_override_rename_failed",
            title="Legacy compose override rename failed",
            detected=(
                "One or more legacy `docker-compose.override.yml` files "
                "could not be renamed to `compose.override.yaml`:\n"
                f"{error_lines}"
            ),
            why_deferred=(
                "Most common cause is a read-only filesystem, restrictive "
                "permissions, or a cross-device boundary. The install can "
                "still complete; podman-compose's auto-load just won't "
                "pick up the override until the rename is applied."
            ),
            command_to_apply=(
                "# Resolve the underlying cause and rename by hand:\n"
                + "\n".join(
                    f"mv {path} {path.parent / _CANONICAL_COMPOSE_OVERRIDE_NAME}"
                    for path, _err in errors
                )
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md",
            ],
        ))

    report.write(install_root)

    return {
        "action": (
            "renamed" if renamed and not conflicts and not errors
            else "conflict" if conflicts and not renamed and not errors
            else "error" if errors and not renamed and not conflicts
            else "mixed"
        ),
        "renamed": [(str(o), str(n)) for o, n in renamed],
        "conflicts": [(str(o), str(n)) for o, n in conflicts],
        "errors": [(str(p), e) for p, e in errors],
    }


# ---------------------------------------------------------------------------
# PR-10B (v0.2.11): legacy KG / code-graph collection detection on Add Project
#
# When a user adds a pre-existing project that has accumulated KG or code-graph
# data under a DIFFERENT collection name (legacy naming, manual rename,
# imported from another machine), the install-bundle step creates a fresh
# empty canonical collection while the legacy data sits orphaned.
#
# These helpers DETECT such candidates conservatively (prefix-similarity to
# THIS project only — never collections from other projects) and emit
# deferral entries so Claude Code surfaces them on next session.  No
# destructive action is taken without explicit user consent.
# ---------------------------------------------------------------------------


# KG-family suffixes considered for legacy detection.
_KG_SUFFIXES = ("_KnowledgeGraph", "_Development")

# Code-graph entity suffixes — regenerable from source.
_CODEGRAPH_SUFFIXES = (
    "_CodeFunction",
    "_CodeModule",
    "_CodeClass",
    "_CodeAPI",
    "_CodeInteraction",
)


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance.  Pure Python — no third-party
    dependency.  Used only for short prefix-similarity scoring (project
    basenames are typically <30 chars) so the O(n*m) cost is negligible.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Two-row DP.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,        # insertion
                prev[j] + 1,            # deletion
                prev[j - 1] + cost,     # substitution
            )
        prev = curr
    return prev[-1]


def _http_count_objects(class_name: str, weaviate_url: str) -> Optional[int]:
    """Count objects in `class_name` via the Weaviate GraphQL Aggregate
    endpoint.

    Returns:
        int   — object count when the request succeeds.
        None  — Weaviate unreachable, malformed response, or class missing.
                Caller treats `None` as "unknown" (not zero) — we don't want
                to claim a legacy collection is empty when we couldn't reach
                the server.

    Soft-fails throughout: never raises into the caller.
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    query = (
        "{ Aggregate { "
        f"{class_name} {{ meta {{ count }} }}"
        " } }"
    )
    try:
        status, body = _http_request(
            "POST", f"{base}/v1/graphql", body={"query": query}, timeout=10.0,
        )
    except Exception:
        return None
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    try:
        agg = payload.get("data", {}).get("Aggregate", {}) or {}
        rows = agg.get(class_name) or []
        if not rows:
            # Aggregate returns empty list for missing class.
            return 0
        meta = rows[0].get("meta") or {}
        count = meta.get("count")
        if isinstance(count, int):
            return count
    except Exception:
        return None
    return None


def _embedding_dim_from_schema(class_def: dict) -> Optional[int]:
    """Extract a representative embedding dimension from a fetched schema
    dict, if discoverable from `vectorIndexConfig.dimensions` or similar.

    Returns None when not knowable from the schema alone (the typical case
    on Weaviate 1.28.x — dimensions are inferred at first ingest).  The
    deferral message prefers a concrete number when available but renders
    a "(dim unknown)" placeholder otherwise.
    """
    try:
        # Multi-named-vector schema: look at the first slot's index config.
        vec_cfg = class_def.get("vectorConfig") or {}
        for _slot, slot_cfg in vec_cfg.items():
            idx_cfg = (slot_cfg or {}).get("vectorIndexConfig") or {}
            dim = idx_cfg.get("dimensions")
            if isinstance(dim, int) and dim > 0:
                return dim
        # Legacy single-vector format.
        idx_cfg = class_def.get("vectorIndexConfig") or {}
        dim = idx_cfg.get("dimensions")
        if isinstance(dim, int) and dim > 0:
            return dim
    except Exception:
        return None
    return None


def _strip_known_suffix(class_name: str, suffixes: tuple) -> Optional[tuple]:
    """If `class_name` ends with one of `suffixes`, return (prefix, suffix);
    else None."""
    for sfx in suffixes:
        if class_name.endswith(sfx) and len(class_name) > len(sfx):
            return (class_name[: -len(sfx)], sfx)
    return None


def _is_similar_prefix(
    candidate_prefix: str,
    canonical_prefix: str,
    *,
    levenshtein_threshold: int = 3,
) -> bool:
    """Conservative similarity heuristic between a legacy class's prefix
    and THIS project's canonical (sanitized) prefix.

    Match rules (any one is sufficient — case-insensitive throughout):
      1. Exact match (after lowercasing).
      2. One is a substring of the other.
      3. Levenshtein distance ≤ `levenshtein_threshold` (default 3).

    The substring rule catches the common "VCO" → "VCODev" case (project
    renamed by appending "Dev"). The Levenshtein rule catches small typos
    or capitalisation drift ("Artup" vs "ARTup", "Foo" vs "FoO").

    Returns False on identical match (caller filters that case separately
    — the canonical name is NOT a "legacy" candidate).
    """
    if not candidate_prefix or not canonical_prefix:
        return False
    a = candidate_prefix.lower()
    b = canonical_prefix.lower()
    if a == b:
        # Exact match on prefix → not a legacy candidate (caller's
        # responsibility to skip the canonical class itself).
        return True
    if a in b or b in a:
        return True
    if _levenshtein(a, b) <= levenshtein_threshold:
        return True
    return False


def _detect_legacy_collections_with_suffixes(
    project_name: str,
    weaviate_url: str,
    suffixes: tuple,
) -> list[dict]:
    """Shared core for legacy KG + legacy code-graph detection.

    Args:
        project_name: raw project name as registered with the launcher.
        weaviate_url: Weaviate REST endpoint.
        suffixes: tuple of class-name suffixes to inspect (KG family or
            code-graph family).

    Returns a list of candidate dicts, each with:
        {
          "class_name":     "<old class name>",
          "suffix":         "_KnowledgeGraph" (etc.),
          "object_count":   int | None,    # None when Weaviate unreachable
          "embedding_dim":  int | None,    # None when not discoverable
          "canonical_name": "<canonical class for this project + suffix>",
        }

    Returns [] in any of these conditions (treated as "nothing to migrate"):
      - Weaviate unreachable.
      - No classes match the suffix family.
      - All matching classes have a different prefix than THIS project's
        canonical prefix (i.e., they belong to OTHER projects — never auto-
        suggest migrating someone else's data).
      - The only matching class IS the canonical name (fresh-install path).
    """
    canonical_prefix = sanitize_for_weaviate_class(project_name)
    if not canonical_prefix:
        return []
    # Conservative: if the project name didn't yield a real prefix and we
    # fell back to `_FALLBACK_PREFIX` ("vct"), do NOT scan — the fallback
    # is too generic and would match many unrelated classes.
    if canonical_prefix == _FALLBACK_PREFIX and project_name.strip().lower() != _FALLBACK_PREFIX:
        return []

    # Schema fetch — soft-fail to empty list if Weaviate is unreachable.
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    try:
        status, body = _http_request("GET", f"{base}/v1/schema", timeout=10.0)
    except Exception:
        return []
    if status != 200:
        return []
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return []

    classes = payload.get("classes") or []
    if not isinstance(classes, list):
        return []

    candidates: list[dict] = []
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        class_name = cls.get("class") or ""
        if not class_name:
            continue

        decomp = _strip_known_suffix(class_name, suffixes)
        if decomp is None:
            continue
        cand_prefix, sfx = decomp

        canonical_name = f"{canonical_prefix}{sfx}"
        # Skip the canonical class itself — it's NOT legacy.
        if class_name == canonical_name:
            continue

        # Conservative prefix-similarity check.  Without this we'd
        # mistakenly suggest migrating Agape_KnowledgeGraph just because
        # the user added a project called "Foo".
        if not _is_similar_prefix(cand_prefix, canonical_prefix):
            continue

        # Object count via GraphQL Aggregate (lightweight; no v4 client).
        count = _http_count_objects(class_name, weaviate_url)
        emb_dim = _embedding_dim_from_schema(cls)

        candidates.append({
            "class_name": class_name,
            "suffix": sfx,
            "object_count": count,
            "embedding_dim": emb_dim,
            "canonical_name": canonical_name,
        })

    # Stable order: by suffix then class_name (deterministic deferral .md).
    candidates.sort(key=lambda c: (c["suffix"], c["class_name"]))
    return candidates


def _detect_legacy_kg_collections(
    project_name: str, weaviate_url: str,
) -> list[dict]:
    """Detect KG-family classes (KnowledgeGraph + Development) that look
    like THIS project's data under a different prefix.

    See `_detect_legacy_collections_with_suffixes` for the full contract.
    """
    return _detect_legacy_collections_with_suffixes(
        project_name, weaviate_url, _KG_SUFFIXES,
    )


def _detect_legacy_codegraph_collections(
    project_name: str, weaviate_url: str,
) -> list[dict]:
    """Detect code-graph-family classes (CodeFunction / CodeModule /
    CodeClass / CodeAPI / CodeInteraction) that look like THIS project's
    data under a different prefix.

    Code-graph data is regenerable from source — the deferral entry
    suggests `code-graph-analyze` re-run rather than copy-with-vectors.
    """
    return _detect_legacy_collections_with_suffixes(
        project_name, weaviate_url, _CODEGRAPH_SUFFIXES,
    )


def _format_legacy_kg_detected(candidates: list[dict]) -> str:
    """Render the bullet-list detected block for the KG deferral."""
    lines = []
    for c in candidates:
        cnt = c.get("object_count")
        dim = c.get("embedding_dim")
        cnt_txt = f"{cnt} object{'s' if cnt != 1 else ''}" if isinstance(cnt, int) else "object count unknown"
        dim_txt = f", {dim}-dim" if isinstance(dim, int) else ""
        lines.append(
            f"  - `{c['class_name']}` ({cnt_txt}{dim_txt}) "
            f"→ canonical: `{c['canonical_name']}`"
        )
    return "\n".join(lines)


def _format_legacy_kg_command(
    project_name: str,
    weaviate_url: str,
    candidates: list[dict],
) -> str:
    """Render the suggested migration commands for the KG deferral.

    Each candidate gets a one-line python-c command that copies objects
    from the legacy class to the canonical class via
    `_copy_collection_with_vectors` (vectors + UUIDs preserved), then
    drops the legacy class.  This is the safe rename idiom — irreversible
    once the drop succeeds, hence the explicit-consent gating.

    The canonical name MUST already exist (created during install_bundle's
    bootstrap step).  If for some reason it doesn't, the user should re-run
    `bootstrap-collections --name <project>` first.
    """
    lines = [
        "# Per-candidate migration: copy objects (vectors + UUIDs preserved)",
        "# from the legacy class into the canonical class, then drop the",
        "# legacy class.  Inspect the dry-run plan first.  The canonical",
        "# class is created by install-bundle's bootstrap step — verify it",
        f"# exists before running:  curl -s {weaviate_url}/v1/schema | python -m json.tool",
        "",
    ]
    for c in candidates:
        old = c["class_name"]
        new = c["canonical_name"]
        lines.append(
            f"# {old} → {new}  ({c.get('object_count', '?')} objects)"
        )
        lines.append(
            "python -c \"from vco_lib.project_init import "
            "_copy_collection_with_vectors, _delete_class; "
            f"n = _copy_collection_with_vectors({old!r}, {new!r}, "
            f"weaviate_url={weaviate_url!r}); "
            f"print(f'copied {{n}} objects'); "
            f"_delete_class({old!r}, weaviate_url={weaviate_url!r}); "
            f"print('dropped {old}')\""
        )
        lines.append("")
    lines.append(
        "# Once migration succeeds, the canonical class holds the data and"
    )
    lines.append(
        "# the legacy class is gone.  Re-running install-bundle will see no"
    )
    lines.append(
        "# remaining candidates and clear this deferral entry."
    )
    return "\n".join(lines)


def _format_legacy_codegraph_command(
    project_name: str,
    weaviate_url: str,
    candidates: list[dict],
) -> str:
    """Render the suggested cleanup commands for the code-graph deferral.

    Code-graph data is regenerable from source — the safe path is
    drop legacy + re-run `code-graph-analyze` against the project root.
    """
    lines = [
        "# Code-graph collections are REGENERATED from source — drop the",
        "# legacy classes and re-run code-graph-analyze on the project.",
        "",
    ]
    for c in candidates:
        old = c["class_name"]
        lines.append(
            f"# Drop {old}  ({c.get('object_count', '?')} objects)"
        )
        lines.append(
            "python -c \"from vco_lib.project_init import _delete_class; "
            f"_delete_class({old!r}, weaviate_url={weaviate_url!r}); "
            f"print('dropped {old}')\""
        )
        lines.append("")
    lines.append(
        "# Then regenerate the canonical code-graph collections from source:"
    )
    lines.append(
        f".claude/scripts/code-graph-analyze . --project {project_name!r}"
    )
    return "\n".join(lines)


def _emit_legacy_kg_deferral(
    folder: Path,
    project_name: str,
    weaviate_url: str,
    candidates: list[dict],
) -> None:
    """Emit `kg_collection_legacy_candidates`: one or more KG-family classes
    in Weaviate look like THIS project's data under a non-canonical prefix.

    Severity is `warning`: the project will function with the (empty)
    canonical class, but the user's accumulated knowledge is orphaned
    until they consent to migrate.

    Single entry per install run — the body lists every candidate so the
    user sees the full picture in one place.
    """
    if not candidates:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    detected_lines = _format_legacy_kg_detected(candidates)
    cmd = _format_legacy_kg_command(project_name, weaviate_url, candidates)

    detected = (
        f"During the per-project install, Weaviate at `{weaviate_url}` was "
        f"inspected for legacy knowledge-graph collections that match "
        f"this project's name under a non-canonical prefix.  The "
        f"following candidates were detected:\n\n"
        f"{detected_lines}\n\n"
        f"The canonical collection(s) for this project (auto-created by "
        f"install-bundle) are now empty — queries return no results until "
        f"the legacy data is either migrated into the canonical class or "
        f"dropped."
    )

    entry = DeferralEntry(
        condition_id="kg_collection_legacy_candidates",
        title="Legacy KG collections detected for this project",
        detected=detected,
        why_deferred=(
            "The migration is destructive (drops the legacy class after "
            "copy) and the prefix-similarity heuristic, while conservative, "
            "can in principle return false positives.  Auto-applying it "
            "without consent could destroy data that belongs to a "
            "differently-named project the user is keeping intentionally.  "
            "PR-10B detects + reports; the user (or a future Tauri "
            "command) runs the migration explicitly."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )

    report = DeferralReport.read(folder)
    report.add_entry(entry)
    report.write(folder)


def _emit_legacy_codegraph_deferral(
    folder: Path,
    project_name: str,
    weaviate_url: str,
    candidates: list[dict],
) -> None:
    """Emit `codegraph_collection_legacy_candidates`: one or more code-graph-
    family classes in Weaviate look like THIS project's data under a non-
    canonical prefix.

    Severity is `info`: code-graph data is REGENERATED from source on every
    `code-graph-analyze` run, so even orphaned legacy collections cause no
    data loss — they're just wasted Weaviate storage.  The deferral nudges
    the user to drop them + re-analyze.
    """
    if not candidates:
        return
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    detected_lines = _format_legacy_kg_detected(candidates)  # same renderer
    cmd = _format_legacy_codegraph_command(project_name, weaviate_url, candidates)

    detected = (
        f"During the per-project install, Weaviate at `{weaviate_url}` was "
        f"inspected for legacy code-graph collections that match this "
        f"project's name under a non-canonical prefix.  The following "
        f"candidates were detected:\n\n"
        f"{detected_lines}\n\n"
        f"Code-graph collections are regenerated from source — drop the "
        f"legacy classes and re-run code-graph-analyze on the project "
        f"after install."
    )

    entry = DeferralEntry(
        condition_id="codegraph_collection_legacy_candidates",
        title="Legacy code-graph collections detected for this project",
        detected=detected,
        why_deferred=(
            "Even though code-graph data is regenerable, dropping a "
            "Weaviate class is irreversible — and the prefix-similarity "
            "heuristic can in principle return false positives.  PR-10B "
            "detects + reports; the user explicitly drops the legacy "
            "classes and re-runs code-graph-analyze to repopulate the "
            "canonical collections."
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
            "skip-disabled": [<rel>...],        # Wave 2 D, 2026-05-22
            "orphan-deleted": [<rel>...],       # v0.2.24 §A0
            "orphan-preserved": [<rel>...],     # v0.2.24 §A0
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
                         "noop", "preserve", "skip-existing",
                         "skip-disabled",
                         "orphan-deleted", "orphan-preserved")},
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
        # v0.2.24 §A0 audit (2026-05-22): added `orphan-deleted` /
        # `orphan-preserved` to the action buckets. Orphans are files
        # in manifest["files"] that this run did NOT re-ship (e.g.
        # upstream deleted them). `orphan-deleted` = removed on disk
        # because hash matched what we previously shipped (user
        # untouched); `orphan-preserved` = kept on disk because user
        # modified vs the prior shipped hash.
        "actions": {k: [] for k in
                    ("create", "overwrite", "always-overwrite",
                     "noop", "preserve", "skip-existing",
                     "skip-disabled",
                     "orphan-deleted", "orphan-preserved")},
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
    # Schema v2: preserved_files records every file VCO chose not to
    # overwrite during this run (`preserve` in update mode + `skip-existing`
    # in first-install mode). Rebuilt from scratch each run so converged
    # files (no longer diverged) automatically fall off.
    new_preserved: dict[str, dict] = {}
    user_modified_paths: list[str] = []
    skipped_existing_paths: list[str] = []

    ops = _enumerate_bundle_files(orchestrator_root, project_root=folder)
    _log("4.bundle", "start",
         f"enumerate: {len(ops)} ops",
         data={"folder": str(folder), "ops": len(ops)})

    for op in ops:
        target_path = folder / op.dest_rel
        try:
            action, source_bytes = _file_action(
                op, target_path, update_mode=update_mode, manifest=manifest,
                orchestrator_root=orchestrator_root,
                project_root=folder,
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
            # Schema v2: record the preservation so a future install (or
            # auditor) can answer "did VCO ever try to install file X here?".
            new_preserved[op.dest_rel] = {
                "shipped_sha256": shipped_hash,
                "preserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "shipped_source": op.source_rel,
                "reason": "preserve",
            }

        elif action == "skip-existing":
            # First-install with pre-existing file: do not overwrite, but
            # also don't claim ownership in the manifest (it's not ours).
            # Track for the per-project deferral so Claude Code knows the
            # bundle install was incomplete (user has stale customizations
            # that won't track future orchestrator improvements).
            skipped_existing_paths.append(op.dest_rel)
            # Schema v2: record the preservation under reason="skip-existing".
            new_preserved[op.dest_rel] = {
                "shipped_sha256": shipped_hash,
                "preserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "shipped_source": op.source_rel,
                "reason": "skip-existing",
            }

        elif action == "skip-disabled":
            # FS-disable contract (Wave 2 D, 2026-05-22): the agent or
            # skill exists at the user-disabled location
            # (`.claude/{agents,skills}.disabled/<name>`). The user
            # explicitly disabled it via the launcher GUI; bundle
            # updates MUST NOT undo that choice. We do NOT touch the
            # filesystem, do NOT claim ownership in the manifest
            # (the enabled-side file isn't ours — it doesn't exist),
            # and do NOT record a preservation entry (the .disabled/
            # location is the source-of-truth, not a divergent edit).
            # Pure no-op other than the result["actions"]["skip-disabled"]
            # append below for caller introspection.
            pass

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
                        # .claude/scripts/ + *.sh as executable. 0o700
                        # (owner-only rwx) — CodeQL py/overly-permissive-file
                        # flagged both 0o755 (world) and 0o750 (group) as
                        # overly permissive. The project folder belongs to
                        # the user; group/world access is unnecessary.
                        mode = 0o700
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

    # v0.2.24 §A0 audit item #1 (2026-05-22): detect orphans — files
    # the orchestrator previously shipped (recorded in manifest["files"])
    # but DID NOT ship this run (not in `ops`). Three sub-cases:
    #
    #   (a) Orphan no longer on disk → silently drop from manifest.
    #       Already deleted (likely by a previous --force or the user).
    #   (b) Orphan present, matches prior_shipped_hash → user never
    #       touched it. Safe to delete; record under
    #       `bundle_orphan_deleted` for visibility (info severity).
    #   (c) Orphan present, hash DIFFERS from prior_shipped_hash → user
    #       customized it. PRESERVE on disk + emit
    #       `bundle_user_modified_deletion_preserved` deferral so the
    #       user knows VCO no longer ships this file but they may
    #       still want their copy. They can delete it manually after.
    #
    # We populate result["actions"]["orphan-deleted"] /
    # ["orphan-preserved"] for caller introspection. The orphan deletion
    # in case (b) is a SAFE delete — file matches what VCO shipped
    # exactly, so the user can never have meaningful customizations on
    # it.
    orphan_deleted: list[str] = []
    orphan_preserved: list[str] = []
    prior_files: dict = manifest.get("files", {}) or {}
    new_files_keys = set(new_files.keys())
    # Compute orphans BEFORE we also include user-modified preserves —
    # the preserved files write their prior entry into new_files (line
    # ~4456), so subtracting new_files_keys from prior_files gives the
    # paths the new run truly did NOT see.
    seen_in_ops = {op.dest_rel for op in ops}
    for prior_rel, prior_entry in prior_files.items():
        if prior_rel in seen_in_ops:
            # Re-shipped this run; not an orphan.
            continue
        prior_hash = (prior_entry or {}).get("sha256", "")
        target_path = folder / prior_rel
        if not target_path.exists():
            # Case (a) — already gone; just don't carry forward.
            continue
        try:
            installed_hash = _file_sha256(target_path)
        except Exception:
            # Read error → treat as preserved (default to safety).
            orphan_preserved.append(prior_rel)
            new_files[prior_rel] = prior_entry  # Keep manifest entry.
            continue
        if prior_hash and installed_hash == prior_hash:
            # Case (b) — safe delete. Hash matches what we shipped,
            # so the user never edited it. Delete from disk; drop
            # from manifest (already not in new_files).
            if not dry_run:
                try:
                    target_path.unlink()
                    orphan_deleted.append(prior_rel)
                except OSError as e:
                    _log("4.bundle.orphan", "warn",
                         f"could not delete orphan {prior_rel}: {e}",
                         data={"path": prior_rel, "error": str(e)})
                    # Couldn't delete → treat as preserved + warn.
                    orphan_preserved.append(prior_rel)
                    new_files[prior_rel] = prior_entry
            else:
                # Dry-run: still report the would-be deletion.
                orphan_deleted.append(prior_rel)
        else:
            # Case (c) — user-modified. Preserve on disk; keep manifest
            # entry so a FUTURE shipped version of this file (should it
            # come back) can recognize the prior baseline.
            orphan_preserved.append(prior_rel)
            new_files[prior_rel] = prior_entry

    result["actions"]["orphan-deleted"] = orphan_deleted
    result["actions"]["orphan-preserved"] = orphan_preserved

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

    # PR-1 (v0.2.11): legacy BASH_ENV cleanup. Pre-0.2.11 installs
    # (orchestrator or user-project) wired BASH_ENV in
    # .claude/settings.json pointing at .claude/scripts/leanctx-bash-env.sh —
    # a fork-bomb-prone pattern on lean-ctx 3.x (see
    # knowledge/concepts/lean-ctx-shim-disabled.md).
    # `_smart_merge_for_bundle` is user-wins on top-level scalars, so the
    # merge above does NOT strip a pre-existing BASH_ENV key. This explicit
    # post-merge step removes it (idempotent — no-op on installs that never
    # had BASH_ENV, or on installs already cleaned by a previous --update).
    # Runs in update_mode only; first-install never sees the legacy state.
    if update_mode and not dry_run:
        try:
            cleanup_result = _cleanup_legacy_bash_env_in_project(folder)
            action = cleanup_result.get("action", "unknown")
            detail = cleanup_result.get("detail", "")
            if action == "removed":
                _log("4.bundle.bashenv-cleanup", "ok",
                     f"legacy BASH_ENV stripped: {detail}",
                     data=cleanup_result)
            elif action in ("write-failed", "unparseable"):
                # Surfacing via warnings (not errors): the rest of the bundle
                # install is still useful. The deferral entry below also
                # tells the operator to re-run after fixing the cause.
                _log("4.bundle.bashenv-cleanup", "warn",
                     f"legacy BASH_ENV cleanup deferred: {action}: {detail}",
                     data=cleanup_result)
                result["warnings"].append(
                    f"legacy BASH_ENV cleanup deferred ({action}): {detail}"
                )
                try:
                    _emit_bash_env_cleanup_deferral(folder, cleanup_result)
                except Exception as defer_err:
                    _log("4.bundle.bashenv-cleanup", "error",
                         f"deferral write failed: "
                         f"{type(defer_err).__name__}: {defer_err}",
                         data={"error": str(defer_err)})
            # `absent` / `left-alone` paths are silent — they're the normal
            # no-op case (clean project, or user has unrelated BASH_ENV).
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.bashenv-cleanup", "error",
                 f"legacy BASH_ENV cleanup crashed: {err}",
                 data={"error": err})
            result["warnings"].append(
                f"legacy BASH_ENV cleanup crashed: {err}"
            )

    # PR-7 (v0.2.11): backfill PROJECT_NAME + CODE_GRAPH_PROJECT into the
    # project's `.claude/settings.json::env` block. Idempotent — runs on
    # every install-bundle pass (first-install AND --update) so that
    # pre-v0.2.11 projects pick up the keys without requiring the launcher
    # to re-run env-write. Dry-run paths skip the write but still log the
    # planned action via `_backfill_code_graph_project_env_in_project`'s
    # action field (which we filter to no-op on missing/unparseable files).
    if not dry_run:
        try:
            backfill = _backfill_code_graph_project_env_in_project(folder)
            result["backfill_code_graph_project"] = backfill
            _log("4.bundle.backfill", "ok",
                 f"code_graph_project_env: {backfill['action']}",
                 data=backfill)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.backfill", "error",
                 f"backfill failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"code_graph_project_env backfill failed: {err}")

    # v0.2.28 (2026-05-23): backfill KG_COLLECTION / SHARED_KG_COLLECTION
    # / DEVELOPMENT_COLLECTION into the project's
    # `.claude/settings.json::env` block. Source of truth is the launcher
    # DB's `project_kg_bindings` table (DB = canonical, per the v0.2.21
    # hub architecture). User-set values are preserved. Idempotent.
    # Diagnostic context: KG searches were silently returning 0 results
    # for projects where these keys never made it into the canonical env
    # channel — the MCP would resolve to the orchestrator-root default
    # collection rather than the project's actual collection.
    if not dry_run:
        try:
            kg_backfill = _backfill_kg_collection_env_in_project(folder)
            result["backfill_kg_collection"] = kg_backfill
            _log("4.bundle.backfill_kg", "ok",
                 f"kg_collection_env: {kg_backfill['action']}",
                 data=kg_backfill)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.backfill_kg", "error",
                 f"kg_collection backfill failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"kg_collection_env backfill failed: {err}")

    # PR-7 (v0.2.11, addendum-4): backfill VS Code watcher / search /
    # Pylance exclude blocks into the project's `.vscode/settings.json`.
    # Without these excludes, large workspaces (>10 GB / >50k files —
    # typical ML projects with venvs + cargo target/) OOM-kill VS Code
    # via systemd-oomd during initial indexing. Idempotent: skipped
    # entirely if every canonical key is already present (user-wins).
    if not dry_run:
        try:
            vscode_backfill = _backfill_vscode_excludes_in_project(folder)
            result["backfill_vscode_excludes"] = vscode_backfill
            _log("4.bundle.vscode_excludes", "ok",
                 f"vscode_excludes: {vscode_backfill['action']}",
                 data=vscode_backfill)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.vscode_excludes", "error",
                 f"backfill failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"vscode_excludes backfill failed: {err}")

    # v0.2.24 RL-defect-2026-05-22 Fix 2 (cleanup hygiene): detect legacy
    # MCP_* keys in `.vscode/settings.json claude-code.env` (pre-v0.2.12
    # writes; inert post-PR-27 but bake the user's on-disk layout into
    # the project tree). Detection-only — per user policy 2026-05-22
    # we never auto-overwrite user-edited files; emit a deferral entry
    # recommending cleanup and let the user decide.
    legacy_vscode_mcp_detected = False
    if not dry_run:
        try:
            vscode_mcp_detect = _detect_legacy_vscode_mcp_env_keys(folder)
            result["legacy_vscode_mcp_env"] = vscode_mcp_detect
            if vscode_mcp_detect["action"] == "detected":
                legacy_vscode_mcp_detected = True
                _emit_legacy_vscode_mcp_env_deferral(folder, vscode_mcp_detect)
                _log("4.bundle.legacy_vscode_mcp", "ok",
                     f"legacy_vscode_mcp_env: detected {len(vscode_mcp_detect['keys'])} key(s)",
                     data=vscode_mcp_detect)
            else:
                _log("4.bundle.legacy_vscode_mcp", "ok",
                     f"legacy_vscode_mcp_env: {vscode_mcp_detect['action']}",
                     data=vscode_mcp_detect)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.legacy_vscode_mcp", "error",
                 f"legacy MCP env detection failed: {err}",
                 data={"error": err})
            result["warnings"].append(
                f"legacy_vscode_mcp_env detection failed: {err}"
            )

    # Project-level templates (item 7 / Obs 7, 2026-05-13). Minimal stubs
    # for CLAUDE.md, CONTEXT_STATE.md, MEMORY.md. Missing → install stub
    # as the live file; present → refresh the `.reference.md` sidecar and
    # flag for review when meaningfully diverged.
    template_review_diverged: list[str] = []
    try:
        # Project name derived from folder basename — kept simple per
        # coord ("no fancy templating engine"). Callers that want a
        # different display name can edit CLAUDE.md after install.
        derived_project_name = folder.name or "Project"
        templates_result = _install_project_level_templates(
            folder,
            orchestrator_root=orchestrator_root,
            project_name=derived_project_name,
            dry_run=dry_run,
        )
        result["templates"] = templates_result
        template_review_diverged = list(templates_result.get("diverged", []))
        _log("4.bundle.templates", "ok",
             f"templates: live_created={len(templates_result['live_created'])}, "
             f"reference_written={len(templates_result['reference_written'])}, "
             f"diverged={len(template_review_diverged)}",
             data=templates_result)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _log("4.bundle.templates", "error",
             f"project-level templates failed: {err}",
             data={"error": err})
        result["warnings"].append(f"project-level templates failed: {err}")

    # RL client per-project setup (Stream 1, v0.2.20). Creates the local
    # data directories used by the rl_client logger so the free-tier
    # retrieval data collection has somewhere to write. Soft-fail: the
    # rest of install must succeed even if this script is missing or
    # errors out (it's a convenience step, not a hard install gate).
    if not dry_run:
        try:
            _rl_setup_result = _run_rl_client_setup(folder)
            if _rl_setup_result:
                result.setdefault("rl_client_setup", _rl_setup_result)
                _log("4.bundle.rl_client_setup", "ok",
                     f"rl_client_setup ran: {_rl_setup_result.get('script', '?')}",
                     data=_rl_setup_result)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.rl_client_setup", "warn",
                 f"rl_client_setup failed (non-fatal): {err}",
                 data={"error": err})
            result["warnings"].append(f"rl_client_setup failed: {err}")

    # Manifest write (always after a successful pass — even dry-run skips).
    if not dry_run:
        try:
            manifest_payload = {
                "schema_version": _MANIFEST_SCHEMA_VERSION,
                "vco_version": result["vco_version"],
                "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": dict(sorted(new_files.items())),
                # Schema v2 (2026-05-13): foundation for audit/diff tooling
                # (item 5 of deferral-ux-polish sprint). See the schema
                # docstring above the constants for the per-entry shape.
                "preserved_files": dict(sorted(new_preserved.items())),
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
    #
    # Reconcile pass (Gap 11 fix, 2026-05-13): after emitting any
    # still-applicable entries, walk the on-disk deferral and DROP entries
    # for conditions this install fully resolved. Without this, an
    # `--update --force` run that overwrites every preserved file leaves a
    # stale `bundle_skipped_existing_files` entry behind because the emit
    # functions are guarded behind non-empty lists.
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

        # v0.2.24 §A0 audit (2026-05-22): user-modified files the
        # orchestrator no longer ships (upstream deletion) → emit
        # `bundle_user_modified_deletion_preserved`. Files that
        # matched the prior shipped hash were already SAFE-deleted in
        # the orphan loop and don't need a deferral.
        if update_mode and orphan_preserved:
            try:
                _emit_orphan_preserved_deferral(
                    folder, orphan_preserved, orchestrator_root,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"orphan-preserved deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"orphan-preserved deferral write failed: {err}"
                )

        # Item 7 (2026-05-13): emit `template_review_pending` when any of
        # the three project-level templates meaningfully differ from the
        # current shipping reference. Severity is info — purely a nudge.
        if template_review_diverged:
            try:
                _emit_template_review_pending_deferral(
                    folder, diverged_files=template_review_diverged,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"template-review deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"template-review deferral write failed: {err}"
                )

        # PR-10B (v0.2.11): legacy KG / code-graph collection detection.
        # When a user adds a pre-existing project that already has KG or
        # code-graph data under a non-canonical prefix, the canonical
        # collections (just bootstrapped) are empty while the legacy data
        # is orphaned.  Detect those candidates conservatively and emit
        # deferral entries — never auto-migrate (destructive).
        weaviate_url = os.environ.get(
            "WEAVIATE_URL", _weaviate_url_default(),
        )
        legacy_kg_candidates: list[dict] = []
        legacy_codegraph_candidates: list[dict] = []
        try:
            legacy_kg_candidates = _detect_legacy_kg_collections(
                derived_project_name, weaviate_url,
            )
            _log("4.bundle.legacy-kg", "ok",
                 f"legacy KG candidates: {len(legacy_kg_candidates)}",
                 data={"count": len(legacy_kg_candidates),
                       "candidates": [c["class_name"] for c in legacy_kg_candidates]})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.legacy-kg", "error",
                 f"legacy-KG detection failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"legacy-KG detection failed: {err}")

        try:
            legacy_codegraph_candidates = _detect_legacy_codegraph_collections(
                derived_project_name, weaviate_url,
            )
            _log("4.bundle.legacy-codegraph", "ok",
                 f"legacy code-graph candidates: {len(legacy_codegraph_candidates)}",
                 data={"count": len(legacy_codegraph_candidates),
                       "candidates": [c["class_name"] for c in legacy_codegraph_candidates]})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.legacy-codegraph", "error",
                 f"legacy-codegraph detection failed: {err}",
                 data={"error": err})
            result["warnings"].append(f"legacy-codegraph detection failed: {err}")

        if legacy_kg_candidates:
            try:
                _emit_legacy_kg_deferral(
                    folder,
                    project_name=derived_project_name,
                    weaviate_url=weaviate_url,
                    candidates=legacy_kg_candidates,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"legacy-KG deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"legacy-KG deferral write failed: {err}"
                )

        if legacy_codegraph_candidates:
            try:
                _emit_legacy_codegraph_deferral(
                    folder,
                    project_name=derived_project_name,
                    weaviate_url=weaviate_url,
                    candidates=legacy_codegraph_candidates,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"legacy-codegraph deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"legacy-codegraph deferral write failed: {err}"
                )

        # Surface in result for caller introspection / Tauri visibility.
        result["legacy_kg_candidates"] = legacy_kg_candidates
        result["legacy_codegraph_candidates"] = legacy_codegraph_candidates

        # Reconcile + trim: drop entries this install resolved.
        try:
            _reconcile_bundle_deferrals(
                folder,
                still_user_modified=bool(user_modified_paths) and not force,
                still_skipped_existing=bool(skipped_existing_paths),
                still_template_review_pending=bool(template_review_diverged),
                still_legacy_kg=bool(legacy_kg_candidates),
                still_legacy_codegraph=bool(legacy_codegraph_candidates),
                # v0.2.24 §A0 (2026-05-22): include the new orphan-
                # preserved condition so a future run that has no
                # remaining orphans (user deleted them, or upstream
                # re-added) clears the stale deferral.
                still_orphan_preserved=bool(orphan_preserved),
                # v0.2.24 RL-defect Fix 2 (2026-05-22): include the
                # legacy .vscode MCP_* env detection so a future run
                # where the user has cleaned the keys clears the entry.
                still_legacy_vscode_mcp=legacy_vscode_mcp_detected,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.deferral", "error",
                 f"deferral reconcile failed: {err}",
                 data={"error": err})
            result["warnings"].append(
                f"deferral reconcile failed: {err}"
            )

    return result


def _reconcile_bundle_deferrals(
    folder: Path,
    *,
    still_user_modified: bool,
    still_skipped_existing: bool,
    still_template_review_pending: bool = False,
    still_legacy_kg: bool = False,
    still_legacy_codegraph: bool = False,
    still_orphan_preserved: bool = False,
    still_legacy_vscode_mcp: bool = False,
) -> None:
    """Trim bundle-specific deferral entries that this install resolved.

    Walks the on-disk UPDATE_DEFERRED.md and removes any entry whose
    `condition_id` corresponds to a state the current install fully
    cleared (no surviving preserved files in that bucket). Other
    condition_ids (schema_migration_required, weaviate_unreachable, etc.)
    are left untouched — they're owned by separate code paths.

    `DeferralReport.write` already deletes the file when the entry list
    becomes empty, so this function is the single place where "force-
    resolved → stale deferral cleanup" happens.
    """
    from vco_lib.deferral_report import DeferralReport

    bundle_conditions = {
        "bundle_user_modified_preserved": still_user_modified,
        "bundle_skipped_existing_files": still_skipped_existing,
        # Item 7 (2026-05-13): template_review_pending is also owned by
        # install_bundle — every run recomputes the diverged set, so when
        # the user updates their CLAUDE.md to match the reference (or
        # vice versa) the next install clears the stale entry.
        "template_review_pending": still_template_review_pending,
        # PR-10B (v0.2.11): legacy collection deferrals are recomputed
        # every install — when the user resolves them (migrates or drops
        # the legacy class) the next install sees no candidates and clears
        # the stale entry.
        "kg_collection_legacy_candidates": still_legacy_kg,
        "codegraph_collection_legacy_candidates": still_legacy_codegraph,
        # v0.2.24 §A0 (2026-05-22): orphan-preserved bookkeeping. When
        # the user deletes the orphan file manually (or upstream
        # re-adds it), `still_orphan_preserved` becomes False and the
        # next install clears the stale deferral entry.
        "bundle_user_modified_deletion_preserved": still_orphan_preserved,
        # v0.2.24 RL-defect Fix 2 (2026-05-22): legacy .vscode MCP_*
        # detection is recomputed every install — when the user removes
        # the inert keys (via the deferral's command) the next install
        # sees `action=none` and clears the stale entry.
        "legacy_vscode_mcp_env_keys_present": still_legacy_vscode_mcp,
    }

    report = DeferralReport.read(folder)
    initial_ids = {e.condition_id for e in report.entries}
    if not initial_ids & set(bundle_conditions):
        # Nothing on-disk we own → no reconciliation to do.
        return

    changed = False
    for condition_id, still_applicable in bundle_conditions.items():
        if not still_applicable and report.has_condition(condition_id):
            report.mark_resolved(condition_id)
            changed = True

    if changed:
        # write() unlinks the file if the entry list is now empty,
        # otherwise atomic-writes the trimmed report.
        report.write(folder)


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


def _backfill_code_graph_project_env_in_project(
    folder: Path,
    project_name: Optional[str] = None,
) -> dict:
    """Idempotent: add `PROJECT_NAME` + `CODE_GRAPH_PROJECT` to a per-project
    `.claude/settings.json::env` block when either key is missing.

    PR-7 (v0.2.11): pre-v0.2.11 the launcher wrote `KG_COLLECTION` and
    `DEVELOPMENT_COLLECTION` into the per-project env block but omitted
    `PROJECT_NAME` and `CODE_GRAPH_PROJECT`. The Orchestrator Project's
    own `post-file-edit` hook then fell back to the hardcoded
    "ClaudeOrchestrator" literal, polluting the legacy code-graph
    collection. This helper runs during
    `install-bundle --update` to repair existing installs in place.

    Idempotency contract:
      - Missing settings file → no-op (`action="missing"`).
      - File unparseable JSON → no-op (`action="unparseable"`) so a hand-
        edited file doesn't get clobbered.
      - Missing `env` block → create it with both keys.
      - `env` present, both keys present → no-op (`action="noop"`).
        User-set values are preserved verbatim — this function only ADDS
        missing keys, never overwrites.
      - `env` present, one or both keys missing → fill in the missing
        keys (`action="backfilled"`).

    Project-name resolution (used only when the key is missing):
      1. Explicit `project_name` argument (preferred — caller-supplied,
         typically derived from the Rust launcher's project record).
      2. Existing `env.KG_COLLECTION` minus the `_KnowledgeGraph` suffix
         (matches the launcher-derived per-project basename).
      3. Existing `env.PROJECT_NAME` (if PROJECT_NAME is present but
         CODE_GRAPH_PROJECT is missing — sync the two).
      4. `folder.name` as last resort, sanitized via
         `sanitize_for_weaviate_class` for consistency with the launcher's
         derivation rules.

    Args:
        folder: target user-project folder.
        project_name: optional explicit project name. When None, resolved
            via the chain above.

    Returns:
        `{"action": str, "added_keys": [str, ...], "path": str,
          "resolved_name": str}` — `resolved_name` is the value actually
        written for the missing key(s); empty when the action is noop.
    """
    settings_file = folder / ".claude" / "settings.json"
    result: dict = {
        "action": "missing",
        "added_keys": [],
        "path": str(settings_file),
        "resolved_name": "",
    }

    if not settings_file.exists():
        return result

    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        result["action"] = "unparseable"
        return result

    if not isinstance(data, dict):
        result["action"] = "unparseable"
        return result

    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
        env_was_missing = True
    else:
        env_was_missing = False

    # Resolve the name to write for any missing keys. We do this lazily so
    # the "both keys present" path skips the resolution work entirely.
    def _resolve_name() -> str:
        if project_name:
            return str(project_name)
        kg = env.get("KG_COLLECTION") if isinstance(env, dict) else None
        if isinstance(kg, str) and kg.endswith("_KnowledgeGraph"):
            return kg[: -len("_KnowledgeGraph")]
        existing_pn = env.get("PROJECT_NAME") if isinstance(env, dict) else None
        if isinstance(existing_pn, str) and existing_pn:
            return existing_pn
        return sanitize_for_weaviate_class(folder.name or "")

    # v0.2.31: same manual_override-correction pattern the KG-side
    # backfill (`_backfill_kg_collection_env_in_project`) uses since
    # v0.2.30. When launcher.db's `project_codegraph_bindings.config_json`
    # carries a `manual_override` sentinel, treat the DB row's
    # `collection_prefix` as the source of truth and correct
    # `env.CODE_GRAPH_PROJECT` even if it's already set to a stale value.
    # Without this, install.py --update + the legacy auto-seeded default
    # could silently revert the user's customized code-graph collection
    # prefix on every update — same shape of bug as the v0.2.28 KG-binding
    # clobber. No correction without manual_override = respects user edits.
    _cg_override = _read_codegraph_binding_override(folder)
    cg_prefix = _cg_override.get("collection_prefix")
    cg_has_manual_override = _cg_override.get("has_manual_override", False)

    added: list[str] = []
    resolved = ""
    if "PROJECT_NAME" not in env:
        # v0.2.31: prefer manual-override binding when present.
        if cg_has_manual_override and cg_prefix:
            resolved = cg_prefix
        else:
            resolved = resolved or _resolve_name()
        env["PROJECT_NAME"] = resolved
        added.append("PROJECT_NAME")
    elif cg_has_manual_override and cg_prefix and env["PROJECT_NAME"] != cg_prefix:
        # Correct existing-but-stale value (matches v0.2.30 KG behaviour).
        env["PROJECT_NAME"] = cg_prefix
        added.append("PROJECT_NAME (corrected)")
        resolved = cg_prefix
    if "CODE_GRAPH_PROJECT" not in env:
        if cg_has_manual_override and cg_prefix:
            env["CODE_GRAPH_PROJECT"] = cg_prefix
        else:
            resolved = resolved or _resolve_name()
            env["CODE_GRAPH_PROJECT"] = resolved
        added.append("CODE_GRAPH_PROJECT")
    elif (
        cg_has_manual_override
        and cg_prefix
        and env["CODE_GRAPH_PROJECT"] != cg_prefix
    ):
        env["CODE_GRAPH_PROJECT"] = cg_prefix
        added.append("CODE_GRAPH_PROJECT (corrected)")

    if not added and not env_was_missing:
        result["action"] = "noop"
        return result

    # Atomic write via tempfile + rename, mirroring `_write_file_atomic`.
    # Soft-fail: best-effort backfill, surface error via action field
    # rather than propagating — the rest of `install-bundle --update`
    # must continue regardless.
    try:
        payload = json.dumps(data, indent=2) + "\n"
        _write_file_atomic(settings_file, payload.encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "backfilled"
    result["added_keys"] = added
    result["resolved_name"] = resolved
    return result


# ---------------------------------------------------------------------------
# v0.2.28 — KG_COLLECTION / SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION
# backfill in `.claude/settings.json::env`
# ---------------------------------------------------------------------------
#
# Forensic context (2026-05-23):
#   The MCP weaviate-kg subprocess reads collection names from env vars
#   `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`.
#   The canonical per-project env channel that propagates to MCP
#   subprocesses (since v0.2.12 / PR-27) is `.claude/settings.json::env`,
#   not `.vscode/settings.json::claude-code.env`.
#
#   Pre-v0.2.28, only `PROJECT_NAME` + `CODE_GRAPH_PROJECT` were
#   backfilled into that channel (see `_backfill_code_graph_project_env`).
#   The three KG-collection keys were never propagated — projects that
#   relied on the legacy `.vscode/settings.json` channel had their KG
#   searches silently resolve via `~/.claude.json` defaults or via the
#   v0.2.27 empty-env safety fallback (which lands on the
#   orchestrator-root literal `VibeCodedOrchestrator_KnowledgeGraph`).
#   The result: hybrid_search returned 0 results across the board
#   because the MCP was searching a different collection than the one
#   the project's nodes actually lived in.
#
#   v0.2.28 fix: extend the install-bundle backfill to also write the
#   three missing KG-collection keys, sourced from the launcher.db's
#   `project_kg_bindings` table (DB = source of truth, per the v0.2.21
#   hub architecture). User-set values are NEVER overwritten — we only
#   ADD missing keys, matching the discipline established by
#   `_backfill_code_graph_project_env_in_project`.


def _read_codegraph_binding_override(folder: Path) -> dict:
    """v0.2.31 (staged for next substantial release): codegraph analogue
    of `_read_kg_binding_override`. Returns the launcher.db
    `project_codegraph_bindings` row's `collection_prefix` + whether the
    row's `config_json.manual_override` sentinel is set, so the caller
    can decide whether to CORRECT an existing-but-stale
    `env.CODE_GRAPH_PROJECT` (manual_override = yes) or leave it alone
    (manual_override = no, = auto-seeded default the user hasn't
    customized).

    Same path-resolution + Windows-aware comparison rules as
    `_read_kg_binding_override`. Soft-fails to empty defaults on any
    error path.

    Returns:
        {"collection_prefix": str | None, "has_manual_override": bool}
    """
    import json as _json
    import os as _os
    import platform as _platform
    import sqlite3 as _sqlite3

    out: dict = {"collection_prefix": None, "has_manual_override": False}

    state_dir_env = _os.environ.get("VCT_STATE_DIR", "").strip()
    if state_dir_env:
        db_path = Path(state_dir_env) / "launcher.db"
    else:
        db_path = Path.home() / ".vct" / "launcher.db"

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

    is_windows = _platform.system().lower().startswith("win")

    def _path_eq(a: str, b: Path) -> bool:
        try:
            ap = Path(a).resolve()
        except (OSError, RuntimeError):
            return False
        if is_windows:
            return str(ap).lower() == str(b).lower()
        return ap == b

    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except _sqlite3.Error:
        return out

    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, folder_path FROM projects")
            rows = cur.fetchall()
        except _sqlite3.Error:
            return out
        project_id = None
        for row_id, row_folder in rows:
            if _path_eq(row_folder or "", folder_canonical):
                project_id = row_id
                break
        if project_id is None:
            return out
        try:
            cur.execute(
                "SELECT collection_prefix, config_json "
                "FROM project_codegraph_bindings WHERE project_id = ?",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                return out
            prefix, config_json = row
            try:
                cfg = _json.loads(config_json or "{}")
            except (_json.JSONDecodeError, TypeError):
                cfg = {}
            out["collection_prefix"] = prefix if prefix else None
            out["has_manual_override"] = (
                bool(cfg.get("manual_override"))
                if isinstance(cfg, dict)
                else False
            )
        except _sqlite3.Error:
            return out
    finally:
        try:
            conn.close()
        except _sqlite3.Error:
            pass

    return out


def _read_kg_binding_override(folder: Path) -> dict:
    """v0.2.30: same launcher.db read as `_read_kg_collection_from_launcher_db`,
    but ALSO returns whether each binding carries a `manual_override`
    sentinel in its `config_json`. The caller uses this to decide
    whether an existing-but-stale settings.json env value should be
    corrected (manual_override = yes → correct) or preserved
    (manual_override = no → leave alone, the user might have edited
    settings.json directly).

    Returns a dict with keys:
        primary_kg_collection: str | None
        primary_has_manual_override: bool
        shared_kg_collection: str | None
        shared_has_manual_override: bool

    Soft-fails to an empty/defaults dict on any error path. Path
    resolution + Windows-aware comparison match
    `_read_kg_collection_from_launcher_db`.
    """
    import json as _json
    import os as _os
    import platform as _platform
    import sqlite3 as _sqlite3

    out: dict = {
        "primary_kg_collection": None,
        "primary_has_manual_override": False,
        "shared_kg_collection": None,
        "shared_has_manual_override": False,
    }

    state_dir_env = _os.environ.get("VCT_STATE_DIR", "").strip()
    if state_dir_env:
        db_path = Path(state_dir_env) / "launcher.db"
    else:
        db_path = Path.home() / ".vct" / "launcher.db"

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

    is_windows = _platform.system().lower().startswith("win")

    def _path_eq(a: str, b: Path) -> bool:
        try:
            ap = Path(a).resolve()
        except (OSError, RuntimeError):
            return False
        if is_windows:
            return str(ap).lower() == str(b).lower()
        return ap == b

    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except _sqlite3.Error:
        return out

    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, folder_path FROM projects")
            rows = cur.fetchall()
        except _sqlite3.Error:
            return out
        project_id = None
        for row_id, row_folder in rows:
            if _path_eq(row_folder or "", folder_canonical):
                project_id = row_id
                break
        if project_id is None:
            return out
        try:
            cur.execute(
                "SELECT role, collection_name, config_json FROM project_kg_bindings "
                "WHERE project_id = ?",
                (project_id,),
            )
            for role, name, config_json in cur.fetchall():
                if not name:
                    continue
                try:
                    cfg = _json.loads(config_json or "{}")
                except (_json.JSONDecodeError, TypeError):
                    cfg = {}
                has_override = bool(
                    cfg.get("manual_override")
                ) if isinstance(cfg, dict) else False
                if role == "primary":
                    out["primary_kg_collection"] = name
                    out["primary_has_manual_override"] = has_override
                elif role == "shared":
                    out["shared_kg_collection"] = name
                    out["shared_has_manual_override"] = has_override
        except _sqlite3.Error:
            return out
    finally:
        try:
            conn.close()
        except _sqlite3.Error:
            pass

    return out


def _read_kg_collection_from_launcher_db(folder: Path) -> dict:
    """Look up `(primary_kg_collection, shared_kg_collection)` for the
    project at `folder` by reading the launcher.db `project_kg_bindings`
    table directly. Returns an empty dict on any soft-fail path (DB not
    found, project not registered, query error) — caller falls through
    to the derivation chain in `_backfill_kg_collection_env_in_project`.

    The shared-binding `role='shared'` collection name is returned as
    `shared_kg_collection`; the `role='primary'` collection name as
    `primary_kg_collection`. Either may be absent if only one role has
    been seeded.

    Path resolution rules: matches `_discover_app_state_db_path` in
    install.py — `$VCT_STATE_DIR/launcher.db` if set, else
    `~/.vct/launcher.db`. Cross-OS via `Path.home()`.

    The folder match uses absolute-path equality after `resolve()` on
    both sides, with a Windows-aware compare (case-insensitive on
    Windows, case-sensitive elsewhere) to handle launcher.db rows
    written from a different drive-letter casing on the same OS.
    """
    import os as _os
    import platform as _platform
    import sqlite3 as _sqlite3

    out: dict = {}

    # DB path resolution — mirror install.py._discover_app_state_db_path
    # rather than calling it (avoid the install.py → project_init →
    # install.py reverse-import which doesn't exist today but would
    # bite the test fixtures).
    state_dir_env = _os.environ.get("VCT_STATE_DIR", "").strip()
    if state_dir_env:
        db_path = Path(state_dir_env) / "launcher.db"
    else:
        db_path = Path.home() / ".vct" / "launcher.db"

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

    is_windows = _platform.system().lower().startswith("win")

    def _path_eq(a: str, b: Path) -> bool:
        try:
            ap = Path(a).resolve()
        except (OSError, RuntimeError):
            return False
        if is_windows:
            return str(ap).lower() == str(b).lower()
        return ap == b

    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except _sqlite3.Error:
        return out

    try:
        cur = conn.cursor()
        # Find the project row that matches our folder.
        try:
            cur.execute("SELECT id, folder_path FROM projects")
            rows = cur.fetchall()
        except _sqlite3.Error:
            return out
        project_id = None
        for row_id, row_folder in rows:
            if _path_eq(row_folder or "", folder_canonical):
                project_id = row_id
                break
        if project_id is None:
            return out
        try:
            cur.execute(
                "SELECT role, collection_name FROM project_kg_bindings "
                "WHERE project_id = ?",
                (project_id,),
            )
            for role, name in cur.fetchall():
                if not name:
                    continue
                if role == "primary":
                    out["primary_kg_collection"] = name
                elif role == "shared":
                    out["shared_kg_collection"] = name
        except _sqlite3.Error:
            return out
    finally:
        try:
            conn.close()
        except _sqlite3.Error:
            pass

    return out


def _backfill_kg_collection_env_in_project(
    folder: Path,
    project_name: Optional[str] = None,
) -> dict:
    """Idempotent: add `KG_COLLECTION` / `SHARED_KG_COLLECTION` /
    `DEVELOPMENT_COLLECTION` to a per-project `.claude/settings.json::env`
    block when missing. Source of truth = launcher.db's
    `project_kg_bindings` table. Fall-back derivation when DB is
    unavailable.

    Discipline (matching `_backfill_code_graph_project_env_in_project`):
      - Missing settings file → `action="missing"`, no-op.
      - File unparseable JSON → `action="unparseable"`, no-op.
      - Missing `env` block → create it with all 3 keys (when
        derivable).
      - All 3 keys present → `action="noop"`. User-set values are
        preserved verbatim — this function only ADDS, never overwrites.
      - One or two keys missing → fill the missing ones
        (`action="backfilled"`).

    Resolution chain (used only for keys that are missing):
      1. launcher.db `project_kg_bindings` (primary + shared roles).
         Read-only; soft-fails to step 2 on any DB error / not-registered.
      2. Existing `env.KG_COLLECTION` minus `_KnowledgeGraph` (derive
         development_collection by suffix swap to `_Development`).
      3. Explicit `project_name` argument or existing `env.PROJECT_NAME`.
      4. `folder.name` sanitized via `sanitize_for_weaviate_class`.

    `SHARED_KG_COLLECTION` semantic: writing an empty string is
    legitimate (it means "don't fan-out to shared KG"). We treat a
    `role='shared'` binding with a non-empty collection_name as a
    write-target, and write `""` when the DB has no shared binding.
    This matches the v0.2.21 launcher behavior where the shared role
    is optional.

    Args:
        folder: target user-project folder.
        project_name: optional explicit project name override for
            derivation chain step 3.

    Returns:
        Same shape as `_backfill_code_graph_project_env_in_project`:
        `{"action": str, "added_keys": [str, ...], "path": str,
          "resolved_values": {key: value, ...}}`.
    """
    settings_file = folder / ".claude" / "settings.json"
    result: dict = {
        "action": "missing",
        "added_keys": [],
        "path": str(settings_file),
        "resolved_values": {},
    }

    if not settings_file.exists():
        return result

    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        result["action"] = "unparseable"
        return result

    if not isinstance(data, dict):
        result["action"] = "unparseable"
        return result

    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
        env_was_missing = True
    else:
        env_was_missing = False

    # Cache DB lookup — we may read it twice during derivation.
    _db_cache: dict = {}
    _db_loaded = False

    def _from_db() -> dict:
        nonlocal _db_loaded
        if not _db_loaded:
            _db_cache.update(_read_kg_collection_from_launcher_db(folder))
            _db_loaded = True
        return _db_cache

    def _derive_basename() -> str:
        if project_name:
            return sanitize_for_weaviate_class(str(project_name))
        kg = env.get("KG_COLLECTION") if isinstance(env, dict) else None
        if isinstance(kg, str) and kg.endswith("_KnowledgeGraph"):
            return kg[: -len("_KnowledgeGraph")]
        existing_pn = env.get("PROJECT_NAME") if isinstance(env, dict) else None
        if isinstance(existing_pn, str) and existing_pn:
            return sanitize_for_weaviate_class(existing_pn)
        return sanitize_for_weaviate_class(folder.name or "")

    added: list[str] = []
    resolved: dict = {}

    # v0.2.30 fix: launcher.db's `project_kg_bindings` row is the
    # canonical source of truth for KG_COLLECTION. When the user (or a
    # prior migration) put a `manual_override` sentinel in the binding's
    # config_json, that's a signal that the launcher's auto-seed default
    # was overridden deliberately. In that case, ALSO correct an
    # existing-but-stale env value — not just add missing keys. Without
    # this, `install.py --update` can leave `env.KG_COLLECTION` pinned
    # to the orchestrator-root default literal even when launcher.db
    # says "the user picked something else", causing silent KG-search
    # misroute. The Rust seed-guard already preserves the binding on
    # boot; this completes the loop on the Python install side.
    _db_override = _read_kg_binding_override(folder)

    # KG_COLLECTION (primary) — fill if missing OR correct if launcher.db
    # has a manual_override that differs.
    db_primary = _db_override.get("primary_kg_collection")
    has_manual_override_primary = _db_override.get("primary_has_manual_override", False)
    if "KG_COLLECTION" not in env:
        if db_primary:
            env["KG_COLLECTION"] = db_primary
        else:
            db = _from_db()
            if "primary_kg_collection" in db:
                env["KG_COLLECTION"] = db["primary_kg_collection"]
            else:
                env["KG_COLLECTION"] = f"{_derive_basename()}_KnowledgeGraph"
        added.append("KG_COLLECTION")
        resolved["KG_COLLECTION"] = env["KG_COLLECTION"]
    elif has_manual_override_primary and db_primary and env["KG_COLLECTION"] != db_primary:
        # Correct an existing wrong value: launcher.db has an explicit
        # manual_override, settings.json env disagrees. Trust the DB.
        env["KG_COLLECTION"] = db_primary
        added.append("KG_COLLECTION (corrected)")
        resolved["KG_COLLECTION"] = db_primary

    # SHARED_KG_COLLECTION — empty-string is a legitimate user choice.
    # Only add when the key is absent, not when it's "" (empty means
    # "intentionally disabled cross-project fan-out"). Same
    # manual-override correction logic as primary.
    db_shared = _db_override.get("shared_kg_collection")
    has_manual_override_shared = _db_override.get("shared_has_manual_override", False)
    if "SHARED_KG_COLLECTION" not in env:
        if db_shared:
            env["SHARED_KG_COLLECTION"] = db_shared
        else:
            db = _from_db()
            if "shared_kg_collection" in db:
                env["SHARED_KG_COLLECTION"] = db["shared_kg_collection"]
            else:
                # No shared binding seeded — leave the cross-project gate
                # closed by default. The user can flip it via the launcher's
                # Identity tab → Manage shared KG collection.
                env["SHARED_KG_COLLECTION"] = ""
        added.append("SHARED_KG_COLLECTION")
        resolved["SHARED_KG_COLLECTION"] = env["SHARED_KG_COLLECTION"]
    elif (
        has_manual_override_shared
        and db_shared
        and env["SHARED_KG_COLLECTION"] != db_shared
        and env["SHARED_KG_COLLECTION"] != ""  # respect user's explicit-disable
    ):
        env["SHARED_KG_COLLECTION"] = db_shared
        added.append("SHARED_KG_COLLECTION (corrected)")
        resolved["SHARED_KG_COLLECTION"] = db_shared

    # DEVELOPMENT_COLLECTION — derived by suffix swap from the primary.
    if "DEVELOPMENT_COLLECTION" not in env:
        primary = env.get("KG_COLLECTION", "")
        if isinstance(primary, str) and primary.endswith("_KnowledgeGraph"):
            dev_name = primary[: -len("_KnowledgeGraph")] + "_Development"
        else:
            dev_name = f"{_derive_basename()}_Development"
        env["DEVELOPMENT_COLLECTION"] = dev_name
        added.append("DEVELOPMENT_COLLECTION")
        resolved["DEVELOPMENT_COLLECTION"] = dev_name
    elif (
        has_manual_override_primary
        and db_primary
        and env.get("KG_COLLECTION") == db_primary
    ):
        # KG_COLLECTION just got corrected from override; recompute the
        # paired DEVELOPMENT_COLLECTION via suffix swap if it doesn't
        # match the new primary's basename.
        expected_dev = (
            db_primary[: -len("_KnowledgeGraph")] + "_Development"
            if db_primary.endswith("_KnowledgeGraph")
            else None
        )
        if expected_dev and env.get("DEVELOPMENT_COLLECTION") != expected_dev:
            env["DEVELOPMENT_COLLECTION"] = expected_dev
            added.append("DEVELOPMENT_COLLECTION (corrected)")
            resolved["DEVELOPMENT_COLLECTION"] = expected_dev

    if not added and not env_was_missing:
        result["action"] = "noop"
        return result

    try:
        payload = json.dumps(data, indent=2) + "\n"
        _write_file_atomic(settings_file, payload.encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "backfilled"
    result["added_keys"] = added
    result["resolved_values"] = resolved
    return result


# ---------------------------------------------------------------------------
# .vscode/settings.json exclude-block backfill (PR-7 / v0.2.11)
# ---------------------------------------------------------------------------
#
# Forensic context (00:56 live OOM-kill incident, 2026-05-16):
#   Opening a large workspace (>10 GB / >50k files — common for ML
#   projects with venvs, model weights, cargo target/) in VS Code with
#   the Pylance extension active triggered:
#     - file watcher scanning every file under the workspace root
#     - Pylance indexing every Python file it discovered (including
#       .venv/lib/python3.x/site-packages which is multi-GB of stdlib
#       wheels)
#     - Chromium renderer holding the full DOM for the file tree
#   Result: systemd-oomd killed VS Code's Chromium scope at 86.46%
#   memory pressure. Reproduced across SD15 (47 GB), Claude (32 GB,
#   76k files), VCO_dev (cargo target/ 33 GB). Local fix in each case
#   was to add the canonical files.watcherExclude + python.analysis.*
#   blocks. PR-7 ships those as launcher-managed defaults so every
#   project the launcher registers (and `install-bundle --update`s)
#   gets them automatically.
#
# Cross-OS notes:
#   - JSON path patterns use forward slashes; VS Code normalizes on
#     Windows, no per-OS branch needed.
#   - `python.analysis.indexing: false` disables Pylance's persistent
#     index — the in-memory analysis still works, just doesn't write
#     a multi-GB cache under ~/.cache/. Users who want indexing back
#     can override per-project.
#
# Coordination with the Rust writer:
#   Pre-PR-27, the launcher's `write_project_env_files` (Rust, at
#   commands/projects_v2.rs) wrote a `claude-code.env` sub-object
#   inside `.vscode/settings.json`. That write was removed in PR-27
#   (v0.2.12, 2026-05-16) because the key did NOT propagate to MCP
#   subprocesses on Linux as of Claude Code 2.1.143 — empirical
#   sentinel testing confirmed (.claude/settings.json env is the
#   channel that actually reaches MCPs). See PR-27 commit message and
#   docs/CLAUDE_CODE_COMPATIBILITY.md → "Per-project env files" for
#   the full trace.
#
#   After PR-27 the Rust writer does not touch `.vscode/settings.json`
#   at all from the env-write code path, so this Python helper is the
#   ONLY writer of that file across the launcher's project-init flow.
#   It still adds only top-level keys (files.watcherExclude, etc.) so
#   any pre-existing `claude-code.env` block authored by the user (or
#   by a pre-PR-27 launcher) is preserved verbatim — the by-key
#   backfill never touches a key it doesn't own.

_VSCODE_EXCLUDE_DEFAULTS: dict[str, object] = {
    # Watcher: prevent inotify / FSEvents / ReadDirectoryChangesW from
    # firing for these dirs. Heavy churn (cargo target/, node_modules/)
    # otherwise saturates the watcher queue.
    "files.watcherExclude": {
        "**/.git/objects/**": True,
        "**/.git/subtree-cache/**": True,
        "**/node_modules/**": True,
        "**/__pycache__/**": True,
        "**/.pytest_cache/**": True,
        "**/.ruff_cache/**": True,
        "**/.mypy_cache/**": True,
        "**/.venv/**": True,
        "**/venv/**": True,
        "**/site-packages/**": True,
        "**/dist/**": True,
        "**/build/**": True,
        "**/target/**": True,
        "**/state/**": True,
        "**/.claude/logs/**": True,
        "**/.claude/worktrees/**": True,
    },
    # File tree: hide noise from the explorer (still searchable via
    # `search.exclude` carve-out below if user removes it).
    "files.exclude": {
        "**/.git": True,
        "**/node_modules": True,
        "**/__pycache__": True,
        "**/.pytest_cache": True,
        "**/.ruff_cache": True,
        "**/.mypy_cache": True,
        "**/.venv": True,
        "**/dist": True,
        "**/build": True,
        "**/target": True,
    },
    # Quick-search exclude (Cmd/Ctrl+P, full-text find): skip the heavy
    # build / cache / log dirs so search latency stays sub-second.
    "search.exclude": {
        "**/node_modules": True,
        "**/__pycache__": True,
        "**/.venv": True,
        "**/dist": True,
        "**/build": True,
        "**/target": True,
        "**/state": True,
        "**/.claude/logs": True,
        "**/.claude/worktrees": True,
        "**/*.lock": True,
    },
    # Pylance: skip these dirs from type-analysis. Indexing OFF avoids
    # the persistent multi-GB cache under ~/.cache.
    "python.analysis.exclude": [
        "**/.venv/**",
        "**/venv/**",
        "**/__pycache__/**",
        "**/.pytest_cache/**",
        "**/.mypy_cache/**",
        "**/.claude/worktrees/**",
    ],
    "python.analysis.indexing": False,
}

# Keys the backfill helpers consider — exposed for tests + the
# orchestrator-side mirror in install.py.
_VSCODE_EXCLUDE_KEYS: tuple[str, ...] = (
    "files.watcherExclude",
    "files.exclude",
    "search.exclude",
    "python.analysis.exclude",
    "python.analysis.indexing",
)


def _backfill_vscode_excludes_in_project(folder: Path) -> dict:
    """Idempotent: add VS Code watcher/search/Pylance exclude blocks to
    a per-project `.vscode/settings.json` when keys are missing.

    PR-7 (v0.2.11): without these excludes, opening a large workspace
    (>10 GB / >50k files — typical for ML projects with venvs, cargo
    target/, model weights) in VS Code triggers OOM kills (verified
    live on Claude/, SD15/, VCO_dev/ on 2026-05-16). The launcher now
    ships the canonical exclude block as a backfill — existing projects
    catch up on `install-bundle --update`.

    Idempotency contract:
      - Missing settings file → create it with just the exclude block
        + a marker comment. `_template_origin: "vibecoded-orchestrator
        v0.2.11+ — vscode-excludes backfill"` so the file is identifiable.
      - File unparseable JSON → action="unparseable" (no-op, preserves
        user file untouched). Hand-edited JSON with trailing commas is
        a common case — we don't want to clobber that.
      - Top-level key already present → user-wins, leave alone (covers
        the "user set `files.watcherExclude: {}` to explicitly disable
        the feature" case the addendum calls out).
      - Top-level key missing → add it with the canonical value.

    Args:
        folder: target user-project folder.

    Returns:
        `{"action": str, "added_keys": [str, ...], "path": str}`. Action
        is one of:
          - "created"     — file didn't exist, written from canonical defaults
          - "backfilled"  — file existed; added one or more missing keys
          - "noop"        — file existed; every canonical key already present
          - "unparseable" — file existed but couldn't be parsed; left alone
          - "write_failed:<ErrorClass>" — atomic write raised
    """
    settings_file = folder / ".vscode" / "settings.json"
    result: dict = {
        "action": "noop",
        "added_keys": [],
        "path": str(settings_file),
    }

    if not settings_file.exists():
        # Fresh write: include just the exclude block (no claude-code.env
        # — PR-27 (v0.2.12, 2026-05-16) removed the Rust launcher's
        # write of that block from `.vscode/settings.json` because it
        # didn't propagate to MCP subprocesses on Linux. The canonical
        # channel for per-project MCP env is `.claude/settings.json`
        # env, written by the Rust launcher's `write_project_env_files`.)
        payload: dict = {
            "_template_origin": (
                "vibecoded-orchestrator v0.2.11+ — vscode-excludes backfill"
            ),
        }
        for key, value in _VSCODE_EXCLUDE_DEFAULTS.items():
            payload[key] = value
        try:
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, indent=2) + "\n"
            _write_file_atomic(settings_file, text.encode("utf-8"))
        except OSError as e:
            result["action"] = f"write_failed:{type(e).__name__}"
            return result
        result["action"] = "created"
        result["added_keys"] = list(_VSCODE_EXCLUDE_DEFAULTS.keys())
        return result

    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        result["action"] = "unparseable"
        return result

    if not isinstance(data, dict):
        result["action"] = "unparseable"
        return result

    added: list[str] = []
    for key, value in _VSCODE_EXCLUDE_DEFAULTS.items():
        if key not in data:
            data[key] = value
            added.append(key)

    if not added:
        return result  # action stays "noop"

    try:
        payload_text = json.dumps(data, indent=2) + "\n"
        _write_file_atomic(settings_file, payload_text.encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "backfilled"
    result["added_keys"] = added
    return result


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
    [--weaviate-url <url>] [--project-folder <path>] [--include-code]
    [--all-projects] --json`

    Sets KG_COLLECTION + DEVELOPMENT_COLLECTION env vars from --name
    (using canonical derivation), then runs the dispatcher.

    v0.2.18 (Commit 4) — additional behavior:
      * KG + Dev collections continue through the existing
        `migrate_collections` smart-dispatch (noop/patch_props/copy/
        rebuild). The target schema now has the v0.2.18 5-slot vector
        config; existing v0.2.17 collections gain the 2 new slots
        (`arctic2_embed`, `openai_text_embed`) via the `copy` action.
      * NEW: code-graph collections (`<Project>_CodeFunction` etc.) are
        walked via the additive helper from `vco_lib.weaviate_schema`
        (`migrate_collections_to_v0218_schema`). Each missing v0.2.18
        slot is added idempotently. Code-graph results are surfaced in
        the JSON envelope under `v0218_schema_reports`.
      * `--all-projects` skips the `--name`-scoped path entirely and
        walks every KG-shaped and Code-shaped collection on the server.
        Useful for orchestrator-wide post-update migrations.

    JSON stdout schema:
      {"plan": [{"collection", "action", "objects_copied", "elapsed_ms"}],
       "dry_run": bool,
       "deferral_emitted": bool,
       "errors": [{"collection", "action", "error"}],
       "v0218_schema_reports": [{"collection", "added_slots", "skipped_slots",
                                  "errors", "objects_copied"}]}

    PR 5 (2026-05-01): when --dry-run AND --project-folder are both set
    AND the plan contains any `copy` or `rebuild` action, a
    `schema_migration_required` deferral entry is written to
    `<project-folder>/.claude/context/UPDATE_DEFERRED.md`. This is the
    pre-update path used by Rust `update_project_v2` to surface destructive
    schema migrations for explicit user consent (rather than auto-applying
    them mid-bundle-install).

    Exit 0 on success; 1 if any errors[] entry exists.
    """
    all_projects = bool(getattr(args, "all_projects", False))

    # Validation: --name is required unless --all-projects is set. argparse
    # can't express "X required unless Y" cleanly so we hand-roll it here.
    if not all_projects and not getattr(args, "name", None):
        print(
            "error: --name is required (or pass --all-projects to walk "
            "every collection)",
            file=sys.stderr,
        )
        return 2

    if not all_projects:
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
    else:
        # --all-projects path: skip the per-project KG/Dev env-driven
        # dispatch; the v0.2.18 helper below walks every KG-shaped
        # collection on the server directly.
        result = {"plan": [], "dry_run": bool(args.dry_run), "errors": []}

    result.setdefault("deferral_emitted", False)
    result.setdefault("v0218_schema_reports", [])

    # v0.2.18: additive migration to the new 5-slot KG + 6-slot Code
    # catalog. This handles three things:
    #   1. KG/Dev: the existing migrate_collections smart-dispatch
    #      already handles named-vector slot additions via the `copy`
    #      action. For dry-run we don't re-walk these (they're in
    #      `result["plan"]`). For wet-run we still re-run the additive
    #      helper to handle the shared KG (which the existing path
    #      doesn't visit) and to surface a unified report.
    #   2. Code-graph: the existing path never touched these. v0.2.18
    #      adds them via the additive helper.
    #   3. --all-projects: walks every collection.
    #
    # Always-on by default (matches the v0.2.18 acceptance criteria);
    # `--no-include-code` opts out for callers who want pre-v0.2.18
    # KG-only behavior (kept for bisectability).
    include_code = getattr(args, "include_code", True)
    if include_code:
        try:
            from vco_lib.weaviate_schema import (
                CODE_NAMED_VECTORS,
                KG_NAMED_VECTORS,
                enumerate_code_collections,
                enumerate_kg_collections,
                migrate_collection_to_target,
            )

            project_name_arg = None if all_projects else args.name
            weaviate_url = args.weaviate_url
            dry_run = bool(args.dry_run)

            # KG-shaped collections (per-project + shared KG when relevant).
            for coll in enumerate_kg_collections(
                project_name=project_name_arg, weaviate_url=weaviate_url,
            ):
                if dry_run:
                    # Dry-run: planned-only entry; no Weaviate writes.
                    result["v0218_schema_reports"].append({
                        "collection": coll,
                        "action": "v0218_schema_check",
                        "added_slots": [],
                        "skipped_slots": [],
                        "errors": [],
                        "objects_copied": 0,
                        "dry_run": True,
                    })
                else:
                    report = migrate_collection_to_target(
                        coll, KG_NAMED_VECTORS,
                        weaviate_url=weaviate_url,
                    )
                    result["v0218_schema_reports"].append({
                        "collection": report.collection,
                        "added_slots": report.added_slots,
                        "skipped_slots": report.skipped_slots,
                        "errors": report.errors,
                        "objects_copied": report.objects_copied,
                    })
                    if report.errors:
                        for e in report.errors:
                            result["errors"].append({
                                "collection": report.collection,
                                "action": "v0218_schema",
                                "error": f"slot {e['slot']}: {e['reason']}",
                            })

            # Code-graph collections (per-project Code* OR all server-wide).
            for coll in enumerate_code_collections(
                project_name=project_name_arg, weaviate_url=weaviate_url,
            ):
                if dry_run:
                    result["v0218_schema_reports"].append({
                        "collection": coll,
                        "action": "v0218_schema_check",
                        "added_slots": [],
                        "skipped_slots": [],
                        "errors": [],
                        "objects_copied": 0,
                        "dry_run": True,
                    })
                else:
                    report = migrate_collection_to_target(
                        coll, CODE_NAMED_VECTORS,
                        weaviate_url=weaviate_url,
                    )
                    result["v0218_schema_reports"].append({
                        "collection": report.collection,
                        "added_slots": report.added_slots,
                        "skipped_slots": report.skipped_slots,
                        "errors": report.errors,
                        "objects_copied": report.objects_copied,
                    })
                    if report.errors:
                        for e in report.errors:
                            result["errors"].append({
                                "collection": report.collection,
                                "action": "v0218_schema",
                                "error": f"slot {e['slot']}: {e['reason']}",
                            })
        except Exception as e:
            # v0.2.18 helper should not break the existing migrate flow.
            # If the helper fails (Weaviate down, import-time issue, etc.),
            # report it in errors[] and continue. The legacy KG/Dev path
            # has already run and its result["plan"] is intact.
            result["errors"].append({
                "collection": None,
                "action": "v0218_schema",
                "error": f"v0.2.18 schema helper failed: "
                         f"{type(e).__name__}: {e}",
            })

    # PR 5: drift-detection deferral (pre-update path).
    project_folder = getattr(args, "project_folder", None)
    if project_folder and bool(args.dry_run) and not result["errors"] and not all_projects:
        destructive = [
            e for e in result.get("plan", [])
            if e.get("action") in ("copy", "rebuild")
        ]
        if destructive:
            try:
                _emit_migrate_required_deferral(
                    Path(project_folder).resolve(),
                    project_name=args.name,
                    weaviate_url=args.weaviate_url or _weaviate_url_default(),
                    plan_entries=destructive,
                )
                result["deferral_emitted"] = True
            except Exception as e:
                # Soft-fail: a deferral write failure must not abort the
                # whole update flow. Report via errors[] so the Rust caller
                # surfaces it as a warning toast.
                result["errors"].append({
                    "collection": None,
                    "action": "deferral",
                    "error": f"migrate-required deferral write failed: "
                             f"{type(e).__name__}: {e}",
                })

    if args.json:
        print(json.dumps(result))
    else:
        print(f"dry_run: {result['dry_run']}  deferral_emitted: {result['deferral_emitted']}")
        for entry in result["plan"]:
            print(
                f"  {entry['action']:13s} {entry['collection']}  "
                f"objects_copied={entry['objects_copied']}  "
                f"elapsed_ms={entry['elapsed_ms']}"
            )
        if result["v0218_schema_reports"]:
            print()
            print("v0.2.18 multi-slot schema migration:")
            print(f"  {'COLLECTION':45s}  ADDED  SKIPPED  ERRORS  COPIED")
            for r in result["v0218_schema_reports"]:
                added = len(r.get("added_slots") or [])
                skipped = len(r.get("skipped_slots") or [])
                errors = len(r.get("errors") or [])
                copied = r.get("objects_copied", 0)
                print(
                    f"  {r['collection']:45s}  "
                    f"{added:5d}  {skipped:7d}  {errors:6d}  {copied:6d}"
                )
                if r.get("added_slots"):
                    print(f"      + added: {', '.join(r['added_slots'])}")
                if r.get("errors"):
                    for e in r["errors"]:
                        print(f"      ! {e['slot']}: {e['reason']}")
        for err in result["errors"]:
            print(f"  ERROR {err.get('collection') or '(global)'}: {err['error']}")
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


def _cmd_drop_collections(args: argparse.Namespace) -> int:
    """`drop-collections --name <project_name> [--weaviate-url <url>] --json`

    Drop the project's OWN Weaviate collections (`<sanitized>_KnowledgeGraph`,
    `<sanitized>_Development`). The shared KG (`VibeCodedOrchestrator_KnowledgeGraph`
    since v0.2.23 B1, was `VibecodedOrchestrator_KnowledgeGraph` v0.2.12–v0.2.22,
    or whatever `_SHARED_KG_NAME` resolves to — was `VibeCodedTools_KnowledgeGraph`
    pre-v0.2.12 PR-26) is NEVER touched — every project depends on read
    access to it, and it's owned by the orchestrator install, not by any
    individual project.

    Used by the launcher's `delete_project_v2` when the user opts in to
    `purge_collections: true` on unregister. Soft-fails idempotently:
    a 404 from Weaviate (collection already gone) counts as a successful
    drop. Connection errors land in `errors[]`, never raise, exit 1.

    JSON stdout schema:
      {"dropped": ["<Project>_KnowledgeGraph", "<Project>_Development"],
       "skipped_shared": "VibeCodedOrchestrator_KnowledgeGraph",
       "errors": [{"collection": <name>, "error": <str>}]}

    Exit 0 on clean drop (incl. 404). Exit 1 when at least one drop
    request hit a non-404 HTTP error.
    """
    derived = derive_project_collection_names(args.name)
    targets = [derived["kg_collection"], derived["development_collection"]]
    weaviate_url = args.weaviate_url

    result: dict = {
        "dropped": [],
        "skipped_shared": _SHARED_KG_NAME,
        "errors": [],
    }

    for name in targets:
        # Defense in depth: refuse to drop anything that looks like a
        # shared collection. The shared name is fixed at install time,
        # but a future config knob could let users override it; if we
        # ever ship that knob, the override has to flow through here so
        # this guard stays correct.
        if name == _SHARED_KG_NAME:
            result["errors"].append({
                "collection": name,
                "error": (
                    f"refusing to drop shared collection {name!r} — "
                    f"shared KG is install-owned, not project-owned"
                ),
            })
            continue
        try:
            _delete_class(name, weaviate_url=weaviate_url)
            result["dropped"].append(name)
        except Exception as e:
            # Connection refused, timeout, malformed URL — surface as
            # a per-collection error rather than crashing the whole
            # JSON envelope. Rust caller wraps these as warnings.
            result["errors"].append({
                "collection": name,
                "error": f"{type(e).__name__}: {e}",
            })

    if args.json:
        print(json.dumps(result))
    else:
        for n in result["dropped"]:
            print(f"dropped: {n}")
        print(f"skipped (shared): {result['skipped_shared']}")
        for err in result["errors"]:
            print(f"  ERROR {err['collection']}: {err['error']}")
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


def _cmd_dismiss_deferral(args: argparse.Namespace) -> int:
    """`dismiss-deferral --folder <path> --condition-id <id> [--json]`

    Remove the deferral entry whose `condition_id` matches `<id>` from
    `<folder>/.claude/context/UPDATE_DEFERRED.md`. Idempotent: re-running
    on an already-dismissed entry (or against a non-existent deferrals
    file) silently succeeds with exit 0.

    Used by the user (or a future launcher GUI button) to silence a
    deferral whose condition cannot be auto-resolved by re-running
    `install-bundle --update --force`. The four emission sites in this
    module (`bundle_user_modified_preserved`, `bundle_skipped_existing_files`,
    `template_review_pending`, plus any future ones) all reference this
    subcommand in the `command_to_apply` text they print.

    Exit codes:
      0 — happy path (entry removed) OR idempotent no-op (no file / no
          matching entry).
      1 — file exists but is structurally malformed (cannot parse).
      2 — argparse / invalid input (handled by argparse itself).

    JSON stdout schema (when `--json` is set):
      {"dismissed": true|false,
       "condition_id": "<id>",
       "remaining": <int — entries still on disk after the call>,
       "reason": "<optional context: no_deferrals_file | no_match | dismissed>"}

    Stderr is reserved for human-readable status lines:
      "dismissed <condition_id>" — entry was present and removed.
      "no matching deferral"     — entry not present (idempotent path).
    """
    folder = Path(args.folder).resolve()
    condition_id = args.condition_id

    from vco_lib.deferral_report import DeferralReport, _DEFERRED_REL

    target = folder / _DEFERRED_REL

    # Edge case: file doesn't exist. Idempotent no-op.
    if not target.exists():
        payload = {
            "dismissed": False,
            "condition_id": condition_id,
            "remaining": 0,
            "reason": "no_deferrals_file",
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print("no matching deferral", file=sys.stderr)
        return 0

    # Edge case: file exists but is unreadable (permissions, non-UTF-8,
    # OS-level I/O error). Surface as exit 1 rather than swallowing.
    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(
            f"error: failed to read {target}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    # Detect structural malformation: a non-empty file with frontmatter
    # claiming condition_ids exist, yet the body parses to zero entries.
    # `DeferralReport.read()` itself is defensive (returns an empty
    # report on garbage input), so we hand-roll this drift check —
    # otherwise a corrupted file would silently appear "already
    # dismissed" and the user would lose data without warning.
    try:
        report = DeferralReport.read(folder)
    except Exception as e:
        # Belt-and-braces: defensive parser shouldn't raise, but catch
        # anyway so a future regression doesn't bring down the caller.
        print(
            f"error: failed to parse {target}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    if raw_text.strip() and not report.entries:
        from vco_lib.deferral_report import _parse_frontmatter

        fm = _parse_frontmatter(raw_text)
        fm_ids = fm.get("condition_ids") or []
        if fm_ids:
            print(
                f"error: malformed deferral file {target}: frontmatter "
                f"lists condition_ids={fm_ids!r} but no parseable entry "
                f"sections were found",
                file=sys.stderr,
            )
            return 1

    # Happy path or no-match (both exit 0).
    had_entry = report.has_condition(condition_id)
    if not had_entry:
        payload = {
            "dismissed": False,
            "condition_id": condition_id,
            "remaining": len(report.entries),
            "reason": "no_match",
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print("no matching deferral", file=sys.stderr)
        return 0

    report.mark_resolved(condition_id)
    # `report.write` deletes the file when the entry list is empty and
    # strips the CLAUDE.md reminder block — both desired here.
    report.write(folder)

    payload = {
        "dismissed": True,
        "condition_id": condition_id,
        "remaining": len(report.entries),
        "reason": "dismissed",
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"dismissed {condition_id}", file=sys.stderr)
    return 0


def _cmd_re_render_claude_md(args: argparse.Namespace) -> int:
    """``re-render-claude-md --folder <path> --project-name <name>
    [--orchestrator-root <path>] [--project-id <id>] [--db-path <path>]
    [--json]``

    Re-render ``<folder>/CLAUDE.md`` from the orchestrator template after a
    module toggle (Phase 1.5.B). User content outside the
    ``<!-- >>>VCO_MANAGED>>> -->`` / ``<!-- <<<VCO_MANAGED<<< -->`` markers
    is preserved verbatim.

    Used by the launcher's ``set_project_module_enabled`` Tauri command
    (Phase 1.1 sibling) — Rust shells out to Python for byte-layout
    authority over template rendering, same Option A pattern as the
    Phase 0.B ``config_projection`` subcommand.

    JSON stdout schema (when ``--json`` is set)::

      {"wrote_path": "<abs>",
       "active_modules": [<sorted module names>],
       "managed_region_present_before": bool,
       "rendered_bytes": <int>}

    Exit codes:
      0 — success.
      1 — render failed (template missing, malformed conditional block,
          out-of-order markers in existing file, or write failure).
    """
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        msg = f"folder does not exist or is not a directory: {folder}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    # Resolve orchestrator_root: explicit arg wins, else walk up looking
    # for the templates/ dir (mirrors install-bundle's discovery rule).
    if args.orchestrator_root:
        orchestrator_root = Path(args.orchestrator_root).resolve()
    else:
        orchestrator_root = _find_orchestrator_root_from_module()

    project_id = args.project_id if args.project_id else None
    db_path = Path(args.db_path).resolve() if args.db_path else None

    try:
        result = render_claude_md(
            folder,
            orchestrator_root=orchestrator_root,
            project_name=args.project_name,
            project_id=project_id,
            db_path=db_path,
        )
    except (FileNotFoundError, TemplateError, OSError) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"render failed: {exc}", file=sys.stderr)
        return 1

    payload = {"ok": True, **result}
    if args.json:
        print(json.dumps(payload))
    else:
        print(
            f"re-rendered {result['wrote_path']} "
            f"({result['rendered_bytes']} bytes, "
            f"active_modules={result['active_modules']})",
            file=sys.stderr,
        )
    return 0


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
            "copy-with-vectors / rebuild. Replaces drop-and-re-embed. "
            "v0.2.18: also adds new named-vector slots (arctic2_embed, "
            "openai_text_embed, jina_embed, etc.) to existing KG + code "
            "collections via additive copy-with-vectors."
        ),
    )
    p_migrate.add_argument(
        "--name", default=None,
        help="Project name (raw, e.g. 'VideoFrames'). KG/Dev collection "
             "names are derived via the canonical sanitizer. Required "
             "unless --all-projects is set.",
    )
    p_migrate.add_argument(
        "--all-projects", action="store_true",
        help="(v0.2.18) Walk every KG-shaped and Code-shaped collection "
             "on the server. Skips the per-project KG/Dev env-driven "
             "smart-dispatch — uses only the additive v0.2.18 multi-slot "
             "migration. Useful for orchestrator-wide post-upgrade runs.",
    )
    p_migrate.add_argument(
        "--include-code", dest="include_code", action="store_true",
        default=True,
        help="(v0.2.18, DEFAULT) Also migrate code-graph collections "
             "(CodeModule / CodeClass / CodeFunction / CodeAPI / "
             "CodeInteraction) to the v0.2.18 multi-slot schema.",
    )
    p_migrate.add_argument(
        "--no-include-code", dest="include_code", action="store_false",
        help="(v0.2.18 escape hatch) Skip the code-graph multi-slot "
             "migration step. KG/Dev still run through the existing "
             "smart-dispatch. Use only for bisecting v0.2.18 regressions.",
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
        "--project-folder", default=None,
        help="Path to the user-project folder. PR 5: when set with "
             "--dry-run, a `schema_migration_required` deferral entry is "
             "written to <folder>/.claude/context/UPDATE_DEFERRED.md when "
             "the plan contains any `copy` or `rebuild` action.",
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

    # drop-collections (2026-05-06 unregister) ---------------------------
    p_drop = sub.add_parser(
        "drop-collections",
        help=(
            "Drop the project's OWN Weaviate collections "
            "(<Project>_KnowledgeGraph, <Project>_Development). Shared "
            "KG is NEVER touched. Used by launcher delete_project_v2 "
            "when --purge-collections is opted in."
        ),
    )
    p_drop.add_argument(
        "--name", required=True,
        help="Project name (raw; sanitization applied internally).",
    )
    p_drop.add_argument(
        "--weaviate-url", default=None,
        help="Override Weaviate URL (default: WEAVIATE_URL env or "
             "http://localhost:8081).",
    )
    p_drop.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout.",
    )
    p_drop.set_defaults(func=_cmd_drop_collections)

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

    # dismiss-deferral (v0.2.31 #21) -------------------------------------
    p_dismiss = sub.add_parser(
        "dismiss-deferral",
        help=(
            "Remove a single deferral entry from "
            "<folder>/.claude/context/UPDATE_DEFERRED.md. Idempotent: "
            "missing file or no-matching-entry exit 0. Referenced by the "
            "four deferral-emission sites in this module."
        ),
    )
    p_dismiss.add_argument(
        "--folder", required=True,
        help="Target user-project folder (must contain the .claude/ tree).",
    )
    p_dismiss.add_argument(
        "--condition-id", required=True, dest="condition_id",
        help="The deferral entry's condition_id field — e.g. "
             "'bundle_user_modified_preserved', 'template_review_pending'.",
    )
    p_dismiss.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (default: human prose "
             "on stderr).",
    )
    p_dismiss.set_defaults(func=_cmd_dismiss_deferral)

    # re-render-claude-md (Phase 1.5.B 2026-05-25) -----------------------
    p_render = sub.add_parser(
        "re-render-claude-md",
        help=(
            "Re-render <folder>/CLAUDE.md from the orchestrator template "
            "after a module toggle. Conditional blocks resolved against "
            "the launcher DB's project_modules table; user content "
            "outside the VCO-managed-region markers is preserved verbatim."
        ),
    )
    p_render.add_argument(
        "--folder", required=True,
        help="Target user-project folder (must exist).",
    )
    p_render.add_argument(
        "--project-name", required=True, dest="project_name",
        help="Display name used to resolve {{PROJECT_NAME}} in the "
             "template.",
    )
    p_render.add_argument(
        "--orchestrator-root", default=None,
        help="Orchestrator clone root (source of "
             "templates/CLAUDE.md.template). Default: walk up from this "
             "module looking for vct-module.json (same rule as "
             "install-bundle).",
    )
    p_render.add_argument(
        "--project-id", default=None, dest="project_id",
        help="Project id/slug used to look up project_modules rows. "
             "Default: the resolved folder path (matches the stub "
             "resolver's contract when no DB is available).",
    )
    p_render.add_argument(
        "--db-path", default=None, dest="db_path",
        help="Override the default ~/.vct/launcher.db resolution "
             "(used by tests / non-default state dirs).",
    )
    p_render.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (default: human-prose "
             "summary on stderr).",
    )
    p_render.set_defaults(func=_cmd_re_render_claude_md)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
