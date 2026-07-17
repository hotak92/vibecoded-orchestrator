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

# NEW-8 / B3 (v0.2.53) — symlink-blocking defense used by
# ``_write_file_atomic``. Mirrors install.py's V47-B contract for the
# orchestrator-self path; the helpers live in ``vco_lib.symlink_handler``
# so both paths share the SSOT.
from vco_lib.symlink_handler import compute_vco_new_path, is_symlink_blocking
from vco_lib import weaviate_helpers as _wh
# v0.2.82 L4: the ONE home for named-vector round-trip cleaning (dropping
# configured-but-empty ``{slot: []}`` slots that weaviate rejects on re-insert).
# LOUD-FAIL import (no fallback): a broken vco_lib install must surface, never
# silently inline-degrade. MUST MATCH the sibling call site in
# vco_lib/codegraph_vector_copy.py.
from vco_lib.weaviate_vectors import clean_named_vector

# Default Weaviate port. Canonical value lives in
# ``vco_lib.weaviate_helpers`` (v0.2.77 Part 7a convergence); re-exported
# here for the many in-tree + test references to ``project_init.DEFAULT_WEAVIATE_PORT``.
DEFAULT_WEAVIATE_PORT = _wh.DEFAULT_WEAVIATE_PORT


def _log_auto(msg: str) -> None:
    """Loud, honest one-line log to stderr for v0.2.83 auto-resolution paths
    that have no ``log_event`` callback in scope (module-level producer
    helpers). Kept on stderr so ``--json`` CLI surfaces stay parseable on
    stdout. Never raises."""
    try:
        print(f"[vct] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never break the caller
        pass

# X-1 / v0.2.76: the underscore-DROPPING sanitizer moved to
# ``vco_lib.codegraph_naming`` — the ONE naming home. These aliases keep the
# historical private names resolving to the canonical source. ``_SAFE_CLASS_RE``
# is a RE-EXPORT (install.py re-exports it as install._SAFE_CLASS_RE, and
# tests/test_vco_lib_project_init.py asserts the two are the SAME object);
# ``_FALLBACK_PREFIX`` is used by an install-flow call site in this module.
from vco_lib.codegraph_naming import (  # noqa: E402,F401  (re-exports)
    _SAFE_CLASS_RE,
    FALLBACK_PREFIX as _FALLBACK_PREFIX,
)


# ---------------------------------------------------------------------------
# Public sanitizer — DEPRECATION RE-EXPORT.
#
# X-1 / v0.2.76: the underscore-DROPPING sanitizer now lives in the ONE
# naming home ``vco_lib.codegraph_naming`` (alongside the underscore-
# PRESERVING ``canonical_class_prefix``). This re-export keeps the historical
# import path ``from vco_lib.project_init import sanitize_for_weaviate_class``
# working for existing callers (install.py, config_projection, the weaviate
# MCP, rl_enrichment, tests). New code should import from
# ``vco_lib.codegraph_naming`` directly.
# ---------------------------------------------------------------------------
from vco_lib.codegraph_naming import (  # noqa: E402  (deprecation re-export)
    sanitize_for_weaviate_class,
)


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
        # v0.2.73 GAP-1: the 5 code-graph collection names, so a consented
        # project-unregister (`drop-collections`) reclaims them too instead
        # of minting fresh orphans. Keyed under a single sub-dict so callers
        # that only want KG/DEV/DIAGRAMS are unaffected. The code prefix is
        # the underscore-PRESERVING `canonical_class_prefix` (SSOT via
        # `derive_project_code_prefix`), NOT the KG `sanitize_for_weaviate_class`
        # basename — the two sanitizers diverge on underscored names.
        "code_collections": derive_project_code_collection_names(project_name),
    }


def _dev_diagrams_from_primary(primary: str, basename_fallback: str) -> tuple[str, str]:
    """The v0.2.84 PLAN-v0284 D1 one-rule dev/diagrams derivation.

    Given a resolved PRIMARY KG collection, return
    ``(development_collection, diagrams_collection)`` by suffix-swapping the
    ``_KnowledgeGraph`` tail (``VCODev_KnowledgeGraph`` → ``VCODev_Development``
    / ``VCODev_Diagrams``). When the primary does NOT end with
    ``_KnowledgeGraph`` (a user-renamed custom primary), fall back to the
    sanitized-name basename — mirroring config_projection's
    ``project_env_from_db`` and the hub's Decision C.

    This is the SAME rule config_projection's ``_derive_dev_diagrams_from_kg``
    realizes (D1). It is retained here for the two remaining project_init
    call-sites that resolve a primary from an ON-DISK settings.json env pin (the
    D3 resolver's settings-fallback tier + ``_backfill_kg_collection_env_in_project``),
    which the config_projection seam — a launcher.db resolver — does not cover.
    The launcher.db binding resolution itself now routes through the seam (see
    :func:`_resolve_bundle_collection_names_binding_first`), so this helper is no
    longer the launcher.db-derivation home — only the on-disk-env derivation.
    """
    if primary.endswith("_KnowledgeGraph"):
        stem = primary[: -len("_KnowledgeGraph")]
        return f"{stem}_Development", f"{stem}_Diagrams"
    return f"{basename_fallback}_Development", f"{basename_fallback}_Diagrams"


def _resolve_bundle_collection_names_binding_first(
    project_name: str,
    project_folder: Optional[Path],
    *,
    db_path: Optional[Path] = None,
) -> dict:
    """v0.2.84 PLAN-v0284 D3 (P2 / ruling R3): binding-first collection names.

    Returns the SAME dict shape as :func:`derive_project_collection_names` but
    resolves the KG/Dev/Diagrams names so bootstrap / migrate NEVER create a
    name-derived collection when a binding resolves a different primary (the R3
    re-creator fix — the incident's empty ``VibeCodedOrchestrator_Development``
    shells were re-created because these two flows name-derived from the launcher
    DISPLAY name):

      1. AUTHORITATIVE — the WP-2 ``config_projection`` one-rule seam
         :func:`~vco_lib.config_projection.resolve_collection_names_for_folder`
         (launcher.db ``project_kg_bindings(role='primary')`` binding-first, with
         the shared D1 dev/diagrams derivation). When the folder is registered
         its binding WINS — that is the R3 pin. Per the planner-approved D3
         contract, ``DbUnreachable`` (no launcher) OR ``ProjectNotFound``
         (unregistered folder) are BOTH the signal that "no binding is
         resolvable", NOT a fatal error — fall through to the on-disk pin / name
         derivation below.
      2. On-disk ``KG_COLLECTION`` pin in the target project's
         ``.claude/settings.json`` ``env`` block (a prior projection's value the
         seam can't see when the launcher DB is unreachable / the folder is
         unregistered — e.g. a standalone CLI bootstrap). Dev/Diagrams via the
         shared :func:`_dev_diagrams_from_primary` (== D1's rule).
      3. :func:`derive_project_collection_names` last resort — the genuinely
         binding-less fresh-create path (correct: the binding is then seeded to
         match). ``shared`` binding / shared-KG defaults are left to the caller's
         existing ``_SHARED_KG_NAME`` handling.

    Soft-fail throughout: any read/parse error falls through to the next tier.
    ``project_folder=None`` (no target folder) short-circuits to tier 3.

    Args:
        project_name: raw project display name (drives the name-derived base +
            last resort).
        project_folder: the target project folder (``None`` → tier 3).
        db_path: optional launcher.db override, threaded straight into the seam
            (tests pin this; the bootstrap/migrate call-sites leave it ``None``
            so the seam resolves the real ``~/.vct/launcher.db``).
    """
    base = derive_project_collection_names(project_name)
    if project_folder is None:
        return base
    folder = Path(project_folder)

    def _apply_primary(primary: str) -> dict:
        out = dict(base)
        out["kg_collection"] = primary
        dev, diagrams = _dev_diagrams_from_primary(primary, base["kg_basename"])
        out["development_collection"] = dev
        out["diagrams_collection"] = diagrams
        return out

    # Tier 1 (AUTHORITATIVE): the config_projection one-rule seam (launcher.db
    # binding-first). D3 integration (WP-2 landed): the local
    # `_read_kg_collection_from_launcher_db` tier is DELETED — the seam owns the
    # launcher.db read + the folder→id canonicalization (symlink/trailing-slash
    # pitfalls have ONE home there) + the shared dev/diagrams derivation.
    try:
        from vco_lib.config_projection import (
            DbUnreachable,
            ProjectNotFound,
            resolve_collection_names_for_folder,
        )
        names = resolve_collection_names_for_folder(folder, db_path=db_path)
        out = dict(base)
        out["kg_collection"] = names["kg_collection"]
        out["development_collection"] = names["development_collection"]
        out["diagrams_collection"] = names["diagrams_collection"]
        return out
    except (DbUnreachable, ProjectNotFound):
        # Both = "no binding resolvable" (no launcher / unregistered folder) →
        # fall through to the on-disk pin, then name derivation. NOT fatal.
        pass
    except Exception:  # noqa: BLE001 — any other seam error → conservative fallthrough
        pass

    # Tier 2: on-disk KG_COLLECTION pin in the project's settings.json env.
    try:
        settings_file = folder / ".claude" / "settings.json"
        if settings_file.is_file():
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                env = data.get("env")
                kg = env.get("KG_COLLECTION") if isinstance(env, dict) else None
                if isinstance(kg, str) and kg:
                    return _apply_primary(kg)
    except Exception:  # noqa: BLE001 — soft-fail to the last-resort derivation
        pass

    # Tier 3: name-derived last resort (fresh create — binding seeded to match).
    return base


def derive_project_code_prefix(project_name: str) -> str:
    """Return the canonical CODE-GRAPH collection prefix for a project.

    Code-graph collections use the underscore-PRESERVING sanitizer
    (``vco_lib.project_naming.canonical_class_prefix``) — the SAME rule the
    analyzers (`analyze_code_graph.py`) and the Rust binding
    (`project_codegraph_bindings.collection_prefix`) use — which DIFFERS
    from the KG-family ``sanitize_for_weaviate_class`` (KG collapses
    underscores). Consolidated here so the drop-collections / migrate /
    orphan-detector paths all agree on the code prefix.

    Delegates to the shared sanitizer in ``codegraph_to_mermaid`` (itself a
    thin wrapper over ``canonical_class_prefix`` with the legacy-fallback
    that never raises), so a single home owns the rule. Returns "" only when
    the sanitizer yields an empty/unusable string.
    """
    try:
        from vco_lib.codegraph_to_mermaid import _sanitize_collection_prefix
        return _sanitize_collection_prefix(project_name or "") or ""
    except Exception:
        # Last-resort: never raise into a caller building a drop plan.
        return ""


def derive_project_code_collection_names(project_name: str) -> list[str]:
    """Return the 5 canonical code-graph collection class names for a project.

    ``["<Prefix>_CodeModule", "<Prefix>_CodeClass", "<Prefix>_CodeFunction",
       "<Prefix>_CodeAPI", "<Prefix>_CodeInteraction"]`` where ``<Prefix>`` is
    :func:`derive_project_code_prefix`. Returns ``[]`` when the prefix cannot
    be derived (empty/unusable name) so drop/migrate callers add nothing
    rather than emitting bare ``_CodeFunction`` targets.

    Suffix order matches ``_CODEGRAPH_SUFFIXES`` (leading underscore stripped)
    so it's stable/testable.
    """
    prefix = derive_project_code_prefix(project_name)
    if not prefix:
        return []
    return [f"{prefix}{sfx}" for sfx in _CODEGRAPH_SUFFIXES]


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


def _derive_project_diagrams_name(project_root: Path) -> str:
    """Internal alias — path-based Diagrams name derivation.

    Added by fix/a1-indexing-pipeline (2026-05-25) so install.py's
    self-install bootstrap (``_ensure_collections``) can derive the
    diagrams collection name with the same path-based helper shape
    used for KG / Dev. Behavior matches ``derive_project_diagrams_name``
    fed ``project_root.name``.
    """
    return derive_project_diagrams_name(project_root.name or "")


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


def code_class_definitions(
    project_prefix: str = "",
    *,
    index_type: str = "hnsw",
) -> dict[str, dict]:
    """Return Weaviate class definitions for the 5 code-graph collections.

    Args:
        project_prefix: Optional prefix added before each canonical name
            (e.g. `project_prefix="MyProj_"` -> `"MyProj_CodeFunction"`).
            Empty string returns bare-name definitions; callers using
            per-project prefixing prepend it.
        index_type: v0.2.73 FIX-D4 — ``"hnsw"`` (default; the ONLY shipped
            default) or ``"hfresh"``. When ``"hfresh"``, each slot's
            ``vectorConfig`` is emitted with ``vectorIndexType:hfresh`` (which
            forces mandatory RQ compression — see
            ``NamedVectorSlot.to_weaviate_config``). GATED: default stays hnsw
            until the integrator's 1.37 scratch-test passes (HFresh is preview
            + forces RQ). A ``hfresh`` collection is only ever born via the
            `copy` migrate action (create-fresh-with-target-schema), never a
            mutation of a live collection (vectorIndexType is immutable live).

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

    # v0.2.73 FIX-D4: thread the target index type into every slot. Default
    # "hnsw" reproduces the pre-D4 shape byte-for-byte (so no spurious schema
    # delta on plain migrations); "hfresh" emits the preview index config
    # (mandatory RQ) — the copy migrate action recreates the collection fresh
    # with this target, which is the only legal way to change the index type.
    vc = {
        slot.name: slot.to_weaviate_config(index_type=index_type)
        for slot in CODE_NAMED_VECTORS
    }

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

    V52-I Fix B (2026-06-09): the 4 canonical temporal date props
    (`created_at`, `updated_at`, `valid_from`, `valid_until`) are now
    included at create-time so fresh KG collections — including the
    SHARED `VibeCodedOrchestrator_KnowledgeGraph` — pass the MCP's
    universal stale filter (``valid_until is_none(True) | valid_until > now``)
    without emitting `partial_fan_out_schema_missing` false-positives.

    Pre-V52-I gap matrix (audit 2026-06-09):
      - per-project KG: shipped without these props; sync_knowledge_graph
        additive-migrate added them lazily on first sync.
      - shared KG: shipped without these props AND nothing migrated it
        → universal stale filter emitted schema errors → 30 false-positive
        partial_fan_out events in our corpus.
    Including them here closes the gap on every fresh install; existing
    installs are reconciled by `migrate-development-temporal-props.sh`
    (regex extended to cover `_KnowledgeGraph` suffix — V52-I Fix B).
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
            # V52-I Fix B (2026-06-09): the 4 canonical temporal date
            # props the MCP's universal `_stale_filter` and `days=`
            # recency filter expect on every KG-shaped collection.
            # `valid_until` is the load-bearing one (driver of the bug);
            # the other three are added for completeness so future code
            # can rely on the full temporal-metadata quartet.
            {"name": "created_at", "dataType": ["date"]},
            {"name": "updated_at", "dataType": ["date"]},
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
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

    V52-I Fix B (2026-06-09): adds `valid_from` and `valid_until` as
    date-typed props so the MCP's universal `_stale_filter` doesn't
    schema-error on diagram collections. The indexer doesn't yet write
    these fields (no per-diagram archival workflow); they stay None on
    new rows, and the stale filter's `is_none(True) | > now` matcher
    treats None as "active by default" — exactly what we want for
    diagrams. The pre-existing INT `created_at` / `updated_at` columns
    are NOT renamed or retyped — the indexer
    (`vco_lib/diagram_indexer.py::_weaviate_upsert`) still writes them
    as `int(time.time())` and downstream search code reads them as ints.
    `valid_from` / `valid_until` are additive only.
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
            # V52-I Fix B (2026-06-09): date-typed validity window so the
            # MCP's universal stale filter
            # (`valid_until is_none(True) | valid_until > now`) doesn't
            # schema-error against diagram collections. Defaults to None
            # on every row the indexer writes — equivalent to "active by
            # default" under the stale filter's matcher.
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
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


def live_fingerprint_stale(
    weaviate_url: str, collection: str
) -> tuple[bool, list[str]]:
    """v0.2.60 (Piece 3): the re-embed gate. Returns (stale, changed_fields)
    where ``stale=True`` means the live collection's EMBEDDING-RELEVANT schema
    no longer matches the current catalog → a migration is needed.

    This is the probe the schema-migration runner uses to decide whether a
    derived (Weaviate) collection that is recorded at the canonical version
    nonetheless needs work. It gates re-embedding on an ACTUAL
    embedding-invalidating change — NOT on a version bump and NOT on a purely
    additive change:

      - Missing CORE named-vector slot OR missing ``indexNullState`` →
        stale (delegates to :func:`detect_kg_schema_drift`; these can't be
        retro-added on Weaviate ≤1.30, so they need a rebuild/copy).
      - A slot present BOTH live and in the catalog but at a DIFFERENT
        dimensionality → stale (stored vectors are invalid for that slot;
        this is the case ``detect_kg_schema_drift`` does NOT catch — it only
        checks slot *presence*, not *dim*).
      - A merely-additive difference (catalog has an OPTIONAL slot the live
        collection lacks) is NOT stale — the existing copy-with-vectors /
        patch_props path adds it without re-embedding. The catalog
        fingerprint (:func:`vco_lib.weaviate_schema.embedding_schema_fingerprint`)
        documents the embed-relevant identity; this probe is its live
        counterpart.

    Failure-soft: Weaviate unreachable / collection absent → (False, []),
    same contract as ``detect_kg_schema_drift``, so a down Weaviate never
    triggers a spurious migration.
    """
    # Invariant subset (core slots + indexNullState) — reuse the existing
    # detector verbatim so the two never drift apart.
    drift, missing = detect_kg_schema_drift(weaviate_url, collection)
    changed = list(missing)

    # Dim-mismatch check (the embedder-identity dimension the invariant
    # detector omits). Compare each slot that exists BOTH live and in the
    # catalog; a dim change invalidates that slot's stored vectors.
    try:
        from vco_lib.weaviate_schema import (
            CODE_NAMED_VECTORS,
            KG_NAMED_VECTORS,
            is_code_collection,
        )

        schema = _fetch_schema(collection, weaviate_url)
        if schema is not None:
            catalog = (
                CODE_NAMED_VECTORS if is_code_collection(collection) else KG_NAMED_VECTORS
            )
            catalog_dims = {slot.name: slot.dim for slot in catalog}
            for slot_name, cfg in (schema.get("vectorConfig") or {}).items():
                want = catalog_dims.get(slot_name)
                if want is None:
                    continue  # live slot not in catalog — not our concern here
                live_dim = _existing_vector_dim_for_slot(
                    weaviate_url, collection, slot_name
                )
                if live_dim is not None and live_dim != want:
                    changed.append(
                        f"slot '{slot_name}' dim {live_dim} != catalog {want} "
                        f"(stored vectors invalid → re-embed)"
                    )
    except Exception as exc:  # failure-soft — never block on a probe error
        logger = __import__("logging").getLogger(__name__)
        logger.debug("live_fingerprint_stale: dim probe failed (%s)", exc)

    return (bool(changed), changed)


def _existing_vector_dim_for_slot(
    weaviate_url: str, collection: str, slot_name: str
) -> Optional[int]:
    """Best-effort: the stored vector dim for ``slot_name`` on ``collection``.

    Delegates to the shared probe in ``vco_lib.weaviate_schema`` so the dim
    discovery logic lives in one place. Returns None when the slot is empty
    / unprobeable (treated as "no mismatch" by the caller — we never re-embed
    on an inconclusive probe).
    """
    try:
        from vco_lib.weaviate_schema import _existing_slot_dim

        return _existing_slot_dim(collection, slot_name, weaviate_url=weaviate_url)
    except Exception:
        return None


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
        weaviate_url = os.environ.get("WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}")
        # v0.2.77 Part 7a: use the shared connect_v4 factory. Force
        # http_secure=False to preserve this path's historical behaviour
        # (it always connected plaintext regardless of scheme).
        client = _wh.connect_v4(weaviate_url, http_secure=False)
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
      vector_index_type_change → action=copy  (v0.2.73 FIX-D4, hnsw→hfresh)
      missing_props (only) → action=patch_props
      not_present → action=create
      none of the above → action=noop
    """
    not_present: bool = False
    legacy_single_vector: bool = False
    missing_vec_slots: list[str] = field(default_factory=list)
    indexNullState_needed: bool = False
    missing_props: list[dict] = field(default_factory=list)
    # v0.2.73 FIX-D4: the target vectorIndexType (e.g. "hfresh") differs from
    # the live collection's ("hnsw"). vectorIndexType is IMMUTABLE on a live
    # collection, so the only way to change it is the `copy` action (create a
    # fresh collection born with the target index type + re-import the SAME
    # client vectors — no re-embed). None = no index-type change requested.
    vector_index_type_change: Optional[str] = None

    def any(self) -> bool:
        return (
            self.not_present
            or self.legacy_single_vector
            or bool(self.missing_vec_slots)
            or self.indexNullState_needed
            or bool(self.missing_props)
            or self.vector_index_type_change is not None
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

    Canonical implementation lives in :func:`vco_lib.weaviate_helpers.http_request`
    (v0.2.77 Part 7a convergence). Kept here as a module-level delegator so
    the many ``mock.patch.object(project_init, "_http_request", ...)`` test
    sites and the sibling helpers below (``_fetch_schema`` / ``_list_classes``)
    that call the module-local name continue to work unchanged.
    """
    return _wh.http_request(method, url, body=body, timeout=timeout)


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

    # v0.2.73 FIX-D4: detect a vectorIndexType change (hnsw → hfresh) on the
    # SHARED slots. vectorIndexType lives per-slot in the named-vector schema.
    # We only compare slots present in BOTH sides (a missing slot is already
    # handled by `missing_vec_slots` → copy). A target slot whose
    # `vectorIndexType` differs from the live one signals a copy-migrate
    # (immutable-on-live → recreate fresh). We record the TARGET type so the
    # dispatcher/logs can name it; take the first differing target as the
    # collection-wide target (all slots share one index type in practice).
    _target_index_type: Optional[str] = None
    for slot_name in sorted(expected_slots & actual_slots):
        tgt_it = ((target_vec_config.get(slot_name) or {})
                  .get("vectorIndexType") or "hnsw")
        act_it = ((actual_vec_config.get(slot_name) or {})
                  .get("vectorIndexType") or "hnsw")
        if str(tgt_it).strip().lower() != str(act_it).strip().lower():
            _target_index_type = str(tgt_it).strip().lower()
            break
    if _target_index_type is not None:
        delta.vector_index_type_change = _target_index_type

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


#: HFresh (Weaviate 1.37 preview) accepts only these distance metrics.
#: Doc-verified 2026-07-03 (see reviews/weaviate-doc-verification-1.37).
_HFRESH_ALLOWED_DISTANCES = frozenset({"cosine", "l2-squared"})


def _hfresh_incompatible_distance(
    name: str, *, weaviate_url: Optional[str] = None,
) -> Optional[str]:
    """Return the live collection's distance metric IF it is incompatible with
    HFresh (i.e. not cosine / l2-squared), else ``None``.

    Reads the actual per-slot ``vectorIndexConfig.distance`` from the live
    schema. A missing/unspecified distance means Weaviate's default
    (``cosine``) → compatible → returns ``None``. Soft-fail: if the schema
    can't be read (transient), returns ``None`` (do NOT block the copy on a
    probe failure — the POST would still surface a real incompatibility).

    Used as the FIX-D4 pre-flight guard on the hnsw→hfresh copy path.
    """
    try:
        schema = _fetch_schema(name, weaviate_url=weaviate_url)
    except Exception:
        return None
    if schema is None:
        return None
    vec_cfg = schema.get("vectorConfig") or {}
    for _slot, slot_cfg in vec_cfg.items():
        idx_cfg = (slot_cfg or {}).get("vectorIndexConfig") or {}
        dist = idx_cfg.get("distance")
        if dist is None:
            continue  # Weaviate default = cosine → compatible.
        if str(dist).strip().lower() not in _HFRESH_ALLOWED_DISTANCES:
            return str(dist)
    # Legacy single-vector shape (defensive; copy path shouldn't reach here).
    idx_cfg = schema.get("vectorIndexConfig") or {}
    dist = idx_cfg.get("distance")
    if dist is not None and str(dist).strip().lower() not in _HFRESH_ALLOWED_DISTANCES:
        return str(dist)
    return None


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

    Delegates to :func:`vco_lib.weaviate_helpers.count_objects_v4` (v0.2.77
    Part 7a convergence — the 0-on-failure variant). Kept as a module-level
    delegator so ``mock.patch.object(project_init, "_count_objects", ...)``
    keeps working.
    """
    return _wh.count_objects_v4(name, weaviate_url=weaviate_url)


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
    the dependency. Returns a connected client.

    Delegates to :func:`vco_lib.weaviate_helpers.connect_v4` (v0.2.77 Part 7a
    convergence — one ``connect_to_custom`` factory). Behaviour is identical
    to the pre-convergence inline (GRPC_PORT env default 50052,
    skip_init_checks=True, http_secure derived from the https:// scheme) EXCEPT
    the pathological "URL ends with a non-numeric port" fallback, which now
    lands on :data:`DEFAULT_WEAVIATE_PORT` (8081) instead of the stray 8080 the
    old inline used — 8081 is this module's actual default and 8080 never
    matched any real config.
    """
    return _wh.connect_v4(weaviate_url=weaviate_url)


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

    BUG-2 (v0.2.73) pre-flight shape guard: before the copy loop, probe the
    ``dst`` schema. This helper always sends a named-vector DICT (the source
    round-trip shape). If ``dst`` is single-vector (no ``vectorConfig``),
    Weaviate rejects the batch with an opaque HTTP 422 ("configured without
    multiple named vectors, but received named vectors") that only surfaces
    as a batch-failed error after the loop. We instead raise a CLEAR
    ValueError up-front telling the caller to re-embed from source (.md)
    rather than copy vectors. Same-shape (named→named) copies still work, so
    the migrate-collections internal staging path (weaviate_schema.py, which
    builds a matching staging schema) is unaffected.
    """
    # BUG-2: fail early and clearly on a named/single-vector shape mismatch,
    # rather than letting an opaque 422 batch failure surface later. A
    # single-vector destination cannot accept the named-vector dict this
    # helper sends — the correct remediation is re-embed-from-.md.
    try:
        dst_schema = _fetch_schema(dst, weaviate_url=weaviate_url)
    except Exception:
        # Soft-fail the probe only: if we can't read the schema (transient
        # network), fall through to the copy — the batch will still surface
        # any real failure. Never let the probe itself block a valid copy.
        dst_schema = None
    if dst_schema is not None and not dst_schema.get("vectorConfig"):
        raise ValueError(
            f"destination {dst!r} is single-vector; named-vector copy "
            f"impossible — re-embed from source (.md) instead "
            f"(run bootstrap-collections --name {dst!r} then "
            f".claude/scripts/kg-sync --all)"
        )
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
                # v0.2.82 L4: the rule now lives once in
                # vco_lib.weaviate_vectors.clean_named_vector. MUST MATCH the
                # sibling call site in vco_lib/codegraph_vector_copy.py (both
                # route through this helper — no second inline dict-comp).
                vec_clean = clean_named_vector(vec)
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
    # v0.2.73 FIX-D4: a vectorIndexType change (hnsw→hfresh) routes to the
    # SAME copy path as missing_vec_slots — the copy action recreates the
    # collection with the TARGET schema (born hfresh) + re-imports client
    # vectors (no re-embed). Ordered before patch_props: an index-type change
    # is structural (must recreate), a prop add is additive.
    if (
        delta.missing_vec_slots
        or delta.indexNullState_needed
        or delta.vector_index_type_change is not None
    ):
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
        # fix/a1-indexing-pipeline (2026-05-25): include the Diagrams
        # collection in the smart-migration plan so future schema bumps
        # (e.g. adding `status` / `content_hash` via the additive
        # patch_props action) flow through the same code path that
        # handles KG / Dev. Skipped silently when DIAGRAMS_COLLECTION
        # is unset in env (e.g. older projects pre-config_projection).
        ("DIAGRAMS_COLLECTION", diagrams_class_definition),
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

    # ── v0.2.73 FIX-D4: code-graph collections (GATED, default OFF) ──────
    # Pre-D4 the smart-migration machinery was KG-shaped ONLY (KG/DEV/
    # DIAGRAMS) — the 5 code collections were systematically excluded, which
    # is the SAME exclusion behind FIX-C GAP-1. We now enumerate them too so
    # an hnsw→hfresh index-type migration can reach the 87 GB CodeFunction via
    # the existing vector-preserving `copy` action (no re-embed).
    #
    # GATED: only enumerated when a code index-type target is explicitly
    # requested (via `--index-type hfresh` → args.index_type, or the
    # VCT_CODEGRAPH_INDEX_TYPE env). Default (unset / "hnsw") adds NOTHING —
    # the code collections stay out of the plan exactly as before, so no
    # existing migration behaviour changes. The default is NOT flipped: the
    # integrator runs the mandatory 1.37 HFresh × client-named-vector
    # scratch-test before hfresh ever becomes the default.
    code_index_type = _resolve_codegraph_index_type(args)
    if code_index_type == "hfresh":
        code_prefix = _resolve_codegraph_prefix_for_plan(args)
        if code_prefix:
            # code_class_definitions is keyed by BASENAME (CodeModule etc.);
            # compose the full per-project class name with the prefix and
            # build the hfresh target for each.
            code_defs = code_class_definitions(
                project_prefix=f"{code_prefix}_", index_type=code_index_type,
            )
            for _basename in sorted(code_defs.keys()):
                target = code_defs[_basename]
                cls_name = target["class"]
                actual = fetcher(cls_name)
                if actual is None:
                    # Code collection doesn't exist yet — creating it fresh
                    # is fine (it's born hfresh) but there's nothing to
                    # migrate. `create` here is a no-op-ish path; the
                    # analyzer normally owns code-collection creation with a
                    # richer property surface, so we SKIP absent code
                    # collections rather than mint an empty hfresh class the
                    # analyzer would then have to reconcile.
                    continue
                delta = _schema_delta(actual, target)
                action = _classify_action(delta)
                if getattr(args, "force_rebuild", False) and action in (
                    "copy", "patch_props", "noop",
                ):
                    action = "rebuild"
                plan.append({
                    "env_key": None,  # code collections are prefix-derived
                    "collection": cls_name,
                    "action": action,
                    "target": target,
                    "delta": delta,
                })

    return plan


def _resolve_codegraph_index_type(args) -> str:
    """Resolve the requested code-graph vectorIndexType target (GATED).

    Precedence: ``args.index_type`` (CLI ``--index-type``) → env
    ``VCT_CODEGRAPH_INDEX_TYPE`` → ``"hnsw"`` (default; code collections
    excluded from the plan, pre-D4 behaviour). Only ``"hfresh"`` opts a
    project's code collections INTO the migration plan.

    // GATED: default hnsw; hfresh is preview + forces RQ + needs 1.37
    // scratch-test (integrator) before default flip.
    """
    val = getattr(args, "index_type", None)
    if not val:
        val = os.environ.get("VCT_CODEGRAPH_INDEX_TYPE", "")
    return (val or "hnsw").strip().lower()


def _resolve_codegraph_prefix_for_plan(args) -> str:
    """Resolve the per-project code-graph prefix for the plan's code
    collections. Uses ``args.name`` (the migrate --name) → canonical code
    prefix. Returns "" when unresolvable (plan adds no code collections)."""
    name = getattr(args, "name", None)
    if not name:
        return ""
    return derive_project_code_prefix(name)


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
        # fix/a1-indexing-pipeline (2026-05-25): include Diagrams in the
        # orphan-staging recovery sweep so a crashed migration of the
        # diagrams collection doesn't leave a `<Project>_Diagrams_staging`
        # orphan that the bootstrap path then trips over on its next run.
        "DIAGRAMS_COLLECTION": diagrams_class_definition,
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

    # A-10 (v0.2.73): the env-configured sweep above only inspects the
    # CURRENT KG/DEV/DIAGRAMS collection names. A migration that crashed
    # leaving ``OldName_KnowledgeGraph__staging`` becomes INVISIBLE once the
    # project is renamed (or KG_COLLECTION is re-derived) before the next
    # run — the sweep now looks at ``NewName_*`` only, and the old staging
    # class (potentially the ONLY surviving copy per the BLOCKER-1 comment)
    # leaks forever. So additionally LIST every ``*__staging`` class on the
    # server and SURFACE any not already handled above. We NEVER auto-drop:
    # a staging class we can't tie to the current env may hold the last copy
    # of user data; dropping it blindly is exactly the destructive
    # operation VCO's consent pattern forbids. Surfacing it as an error
    # routes it through the caller's deferral integration for human review.
    try:
        _already_handled_staging = {
            os.environ.get(env_key, "") + _STAGING_SUFFIX
            for env_key in _recover_targets
            if os.environ.get(env_key, "")
        }
        # Two distinct staging families are reaped here:
        #
        #  * KG family (KnowledgeGraph / Development / Diagrams): a renamed KG,
        #    Development or Diagrams collection is exactly the A-10 leak. These
        #    are user-authored — a staging class may hold the ONLY surviving
        #    copy — so they are RETAINED + SURFACED for human review, never
        #    auto-dropped.
        #  * Code-graph family (``*_Code{Module,Class,Function,API,
        #    Interaction}__staging``): IS handled here too (v0.2.73, Q3),
        #    precisely because NOTHING else reaps it — the code-graph rebuild
        #    path (``weaviate_schema.rebuild_collection_via_staging``) leaves
        #    ``<Collection>__staging`` behind on a mismatch/crash and assumes
        #    this upstream sweep flushes it, but historically this sweep
        #    excluded it → an ownerless seam that orphaned code-graph staging
        #    forever. Because code graph is DERIVED/regenerable (unlike KG),
        #    a *provable* orphan can be SAFE-DROPPED — but only under the same
        #    count-aware guard as ``_recover_or_drop_orphan_staging``: drop
        #    ONLY when the base collection exists and holds >= as many objects
        #    as the staging (staging is then a pure orphan). If the base is
        #    MISSING or SMALLER, the staging might be the fuller/only copy, so
        #    RETAIN + SURFACE instead (never blind-drop).
        _KG_FAMILY_STAGING_SUFFIXES = (
            "_KnowledgeGraph" + _STAGING_SUFFIX,
            "_Development" + _STAGING_SUFFIX,
            "_Diagrams" + _STAGING_SUFFIX,
        )
        # Derive the code-graph staging suffixes from the ONE canonical list of
        # code-graph class base names (``schema_migration_runner._CODEGRAPH_
        # CLASS_SUFFIXES``) — do NOT re-list them here (a 5th copy would drift).
        from . import schema_migration_runner as _smr
        _CODEGRAPH_STAGING_SUFFIXES = tuple(
            f"_{_s}{_STAGING_SUFFIX}" for _s in _smr._CODEGRAPH_CLASS_SUFFIXES
        )
        _all_classes = _list_classes(weaviate_url)
        _orphan_staging = sorted(
            c for c in _all_classes
            if c.endswith(_KG_FAMILY_STAGING_SUFFIXES)
            and c not in _already_handled_staging
        )
        _codegraph_orphan_staging = sorted(
            c for c in _all_classes
            if c.endswith(_CODEGRAPH_STAGING_SUFFIXES)
            and c not in _already_handled_staging
        )
        for _orphan in _orphan_staging:
            _base = _orphan[: -len(_STAGING_SUFFIX)]
            _base_exists = _base in _all_classes
            _log(
                "7b.recover", "warn",
                f"orphan staging class not tied to current env: {_orphan} "
                f"(base {_base!r} {'present' if _base_exists else 'MISSING'}); "
                f"RETAINED for human review (never auto-dropped)",
                data={
                    "staging": _orphan,
                    "base": _base,
                    "base_present": _base_exists,
                    "branch": "unmatched_orphan_surface",
                },
            )
            result["errors"].append({
                "collection": _base,
                "action": "recover",
                "error": (
                    f"orphan staging class {_orphan} is not tied to this "
                    f"project's current KG/DEV/DIAGRAMS env "
                    f"(likely a pre-rename migration crash). Base collection "
                    f"{_base!r} is "
                    f"{'present' if _base_exists else 'MISSING (staging may be the only surviving copy)'}"
                    f". RETAINED — inspect and, if safe, drop manually via the "
                    f"launcher; VCO will not auto-drop it."
                ),
            })
        # Code-graph orphan staging (Q3): count-aware SAFE-DROP-or-SURFACE.
        # Code graph is DERIVED (regenerable), so a provable orphan is
        # self-heal, not destruction — but the count guard is what makes the
        # drop safe. We mirror the SAFE-DROP / AMBIGUOUS discipline of
        # ``_recover_or_drop_orphan_staging`` WITHOUT a KG target schema
        # (code-graph staging must NOT be recovered via the KG schema path —
        # only safe-dropped when provably redundant, or retained).
        #
        # CROSS-PROJECT SAFETY (v0.2.73): ``_list_classes`` is INSTANCE-WIDE, so
        # this sweep sees EVERY project's code-graph staging on the shared
        # Weaviate. Auto-dropping another project's ``*_Code*__staging`` would
        # be exactly the cross-tenant data-loss the legacy-KG detector was
        # hardened against — that project may have a rebuild IN FLIGHT (its
        # base momentarily >= staging) whose staging we'd delete out from under
        # it. So the SAFE-DROP is gated to THIS project's own code-graph prefix;
        # any code-graph staging belonging to a DIFFERENT project is only
        # SURFACED (retained) for that project's own migrate run to reconcile.
        #
        # Resolve the current project's code-graph prefix through the ONE
        # canonical resolver — ``schema_migration_runner._resolve_codegraph_prefix``
        # — so this sweep names the SAME collections the hub / analyzer / CLI do
        # (single source of truth; do NOT inline a parallel env cascade that
        # could drift from it). That resolver's chain is
        # ``CODE_GRAPH_PROJECT`` env → ``PROJECT_NAME`` (derived) — and on the
        # launcher-driven update path ``CODE_GRAPH_PROJECT`` is itself a
        # PROJECTION of ``project_codegraph_bindings.collection_prefix`` (the
        # launcher.db binding written by the hub before spawning install.py, see
        # ``config_projection._fetch_codegraph_binding_prefix``), so this traces
        # back to the DB binding, not an ad-hoc re-derivation. The CLI
        # ``migrate --name`` path (no env) falls back to ``args.name``. Empty
        # (unresolvable) → ownership can't be proven → NEVER auto-drop.
        # (_smr already imported above for _CODEGRAPH_CLASS_SUFFIXES.)
        _self_cg_prefix = (_smr._resolve_codegraph_prefix(os.environ) or "")
        if not _self_cg_prefix:
            _self_cg_prefix = _resolve_codegraph_prefix_for_plan(args)
        # OWNERSHIP is EXACT-SET membership over THIS project's 5 code-graph
        # class names (``<prefix>_CodeModule`` … ``<prefix>_CodeInteraction``) —
        # NOT a ``startswith(prefix + "_")`` prefix test. A prefix test
        # OVER-MATCHES a foreign tenant whose prefix is an underscore-delimited
        # SUPERSET of ours: with prefix ``Proj`` a bare startswith would treat
        # ``Proj_Backend_CodeFunction`` (project ``Proj_Backend``'s class) as
        # OURS and safe-drop its staging — the exact cross-tenant data-loss this
        # gate exists to prevent (``canonical_class_prefix`` PRESERVES explicit
        # underscores, so co-resident ``Proj`` + ``Proj_Backend`` is reachable).
        _own_cg_classes = frozenset(
            f"{_self_cg_prefix}_{_s}" for _s in _smr._CODEGRAPH_CLASS_SUFFIXES
        ) if _self_cg_prefix else frozenset()
        for _cg_orphan in _codegraph_orphan_staging:
            _cg_base = _cg_orphan[: -len(_STAGING_SUFFIX)]
            # Empty prefix (no --name / unresolvable) → can't prove ownership,
            # so NEVER auto-drop (the set is empty → nothing matches).
            _cg_is_ours = _cg_base in _own_cg_classes
            if not _cg_is_ours:
                # FOREIGN (or unprovable) — never touch another tenant's
                # staging; its own migrate run reconciles it. Surface only.
                _cg_base_exists = _cg_base in _all_classes
                _reason = (
                    f"belongs to a DIFFERENT project (base {_cg_base!r} is not "
                    f"under this project's code-graph prefix "
                    f"{_self_cg_prefix or '<unresolved>'!r})"
                )
                _log(
                    "7b.recover", "warn",
                    f"code-graph orphan staging {_cg_orphan} RETAINED: {_reason}"
                    f" — not auto-dropped",
                    data={"staging": _cg_orphan, "base": _cg_base,
                          "base_present": _cg_base_exists, "is_ours": False,
                          "branch": "codegraph_foreign_surface"},
                )
                result["errors"].append({
                    "collection": _cg_base,
                    "action": "recover",
                    "error": (
                        f"code-graph orphan staging {_cg_orphan} RETAINED for "
                        f"human review: {_reason}. Not auto-dropped — inspect "
                        f"and, if safe, drop manually via the launcher."
                    ),
                })
                continue
            # OURS — route the count-aware SAFE-DROP-or-SURFACE decision through
            # the ONE shared home ``_recover_or_drop_orphan_staging`` (single
            # source of truth for the count logic; do NOT re-implement it here).
            # ``target_def=None`` forbids the RECOVER branch — code-graph staging
            # must NEVER be recreated via a KG target schema; a missing/smaller
            # base returns "ambiguous" (surface), and base>=staging returns
            # "dropped" (self-heal, since code graph is derived/regenerable).
            _outcome = _recover_or_drop_orphan_staging(
                _cg_base, target_def=None,
                weaviate_url=weaviate_url, log_event=log_event,
            )
            if _outcome == "dropped":
                result["errors"].append({
                    "collection": _cg_base,
                    "action": "recover",
                    "error": (
                        f"code-graph orphan staging {_cg_orphan} SAFE-DROPPED: "
                        f"base {_cg_base!r} intact (>= staging). Code graph is "
                        f"derived; the redundant orphan was auto-dropped."
                    ),
                    "resolved": True,
                    "action_taken": "safe_dropped",
                })
            elif _outcome == "ambiguous":
                result["errors"].append({
                    "collection": _cg_base,
                    "action": "recover",
                    "error": (
                        f"code-graph orphan staging {_cg_orphan} RETAINED for "
                        f"human review: base {_cg_base!r} is MISSING or SMALLER "
                        f"than staging (staging may be the fuller/only copy). "
                        f"Not auto-dropped — inspect and, if safe, drop manually."
                    ),
                })
            # "none" (staging vanished mid-sweep) → nothing to report.
    except Exception as e:
        # Soft-fail: the extra sweep must never break migrate-collections.
        _log("7b.recover", "warn",
             f"A-10 unmatched-orphan-staging sweep failed: {e}",
             data={"error": str(e)})

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
                # v0.2.73 FIX-D4: HFresh × distance pre-flight guard. HFresh
                # supports ONLY cosine + l2-squared (doc-verified 1.37). If
                # this copy is an hnsw→hfresh index-type change AND the LIVE
                # collection uses `dot` distance, refuse up-front with a clear
                # error (route to the documented fallback: keep it on hnsw)
                # rather than a 422 at POST time. VCO code/KG use cosine so
                # the common case passes; the guard exists for correctness.
                if delta.vector_index_type_change == "hfresh":
                    _bad = _hfresh_incompatible_distance(name, weaviate_url=weaviate_url)
                    if _bad is not None:
                        raise ValueError(
                            f"{name}: cannot migrate to HFresh — live distance "
                            f"metric is {_bad!r}; HFresh supports only cosine + "
                            "l2-squared. Keep this collection on hnsw "
                            "(documented fallback); do not force the swap."
                        )
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
                # Drop + recreate with the target schema. v0.2.54 Track D
                # (P0-2): pre-fix this branch only DELETED — the comment
                # claimed "the caller's _ensure_collections + _seed_weaviate
                # handle recreate + re-ingest", which was true ONLY for the
                # install.py call path. The CLI handler
                # (_cmd_migrate_collections) never recreated, so the
                # `schema_migration_required` deferral's own
                # `command_to_apply` left the user's collection GONE until
                # the next full install.py run. Recreating here (empty,
                # target schema) closes that gap for every caller; the
                # data re-ingest still happens via _seed_weaviate
                # (install.py path) or the CLI handler's re-ingest step
                # (_cmd_migrate_collections, same Track D fix).
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
                    _create_class(target, weaviate_url=weaviate_url)
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
#   * `launcher/src-tauri/src/commands/project_env_settings.rs::LAST_RESORT_SHARED_KG_COLLECTION`
#     (renamed from `DEFAULT_SHARED_KG_COLLECTION` in v0.2.40 W40-C —
#     value unchanged, the rename signals "last-resort fallback" rather
#     than "first-choice default" since DB-read takes precedence),
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
#   * Case-only name conflicts: old `MyProject_development` (lowercase d) vs new
#     `MyProject_Development` (capital D). Weaviate stores class names case-
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
    # v0.2.84 PLAN-v0284 D3 (P2 / ruling R3): resolve the collection names
    # BINDING-FIRST (settings.json env → launcher.db primary binding →
    # name-derived last resort). Pre-.84 this name-derived from the DISPLAY
    # name on EVERY create AND update, so a project whose binding primary
    # (e.g. `VCODev_KnowledgeGraph`) differed from its display name
    # ("VibeCoded Orchestrator") got empty `VibeCodedOrchestrator_*` shells
    # created — and re-created after the operator dropped them (the R3
    # re-creator). With a `project_folder` we now honor the binding; without
    # one (or with no binding at all) this falls back to the identical
    # name-derived set (fresh-create — the binding is then seeded to match).
    derived = _resolve_bundle_collection_names_binding_first(
        project_name, project_folder,
    )
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
    # Diagrams third (when not kg_only), shared KG last.
    targets: list[tuple[str, Callable[[str], dict]]] = [
        (derived["kg_collection"], kg_class_definition),
    ]
    if not kg_only:
        targets.append(
            (derived["development_collection"], development_class_definition),
        )
        # Phase 1.5 — Diagrams collection (fix/a1-indexing-pipeline
        # 2026-05-25). Auto-paired with the KG collection on every
        # bootstrap so `vco_lib.diagram_indexer::_weaviate_upsert` has a
        # class to write into the first time the user saves a .mmd /
        # .excalidraw file (otherwise upsert fails with "no such class"
        # — Bug-3 of the wiring audit). The class is also unconditionally
        # bootstrapped on every existing project's next session because
        # the existence check + POST-on-absent path below makes the
        # bootstrap idempotent.
        targets.append(
            (derived["diagrams_collection"], diagrams_class_definition),
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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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

    # Compose the list of expected collections for the deferral message.
    # In kg_only mode we skip Dev and Diagrams (matching the bootstrap
    # target tuple); otherwise list all three per-project collections
    # plus the shared KG. fix/a1-indexing-pipeline (2026-05-25): added
    # `<Project>_Diagrams` so users see all collections that didn't get
    # created and can plan the re-run accordingly.
    if kg_only:
        _collections_human = f"{derived['kg_collection']}, {_SHARED_KG_NAME}"
    else:
        _collections_human = (
            f"{derived['kg_collection']}, "
            f"{derived['development_collection']}, "
            f"{derived['diagrams_collection']}, "
            f"{_SHARED_KG_NAME}"
        )
    entry = DeferralEntry(
        condition_id="weaviate_unreachable_at_bootstrap",
        title="Weaviate collection bootstrap deferred",
        detected=(
            f"Weaviate at {weaviate_url} was unreachable during project "
            f"creation, and the auto-restart attempt did not bring it back. "
            f"The project's KG collections "
            f"({_collections_human}) "
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
    # v0.2.83 PLAN-v0283 WP-B2: emission goes through the ONE locked emitter
    # home (vco_lib.deferral_emit) — read-modify-write under an exclusive
    # file lock so a concurrent detached writer (codegraph resync, install
    # finalize) can't drop this entry.
    _de.emit(project_folder, entry)


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

# v0.2.84 PLAN-v0284 D7 (P5/R2): shipped-file adoption backups. When an update
# ADOPTS a divergent bundle file, the CURRENT bytes are first copied here (one
# `<UTC-basic-ts>` sub-dir per install run) before the shipped bytes overwrite
# them — so the adoption never destroys the user's on-disk bytes without a
# captured copy. This tree is DELIBERATELY excluded from both the manifest
# ownership set (backups are never in `_enumerate_bundle_files` → never enter
# `new_files`) and the orphan scan (which walks only prior manifest entries), so
# it can never itself be classified, adopted, or deleted. Kept forever
# (small, user-prunable — see the adoption NOTICE text).
_ADOPT_BACKUPS_REL = Path(".claude") / "backups" / "bundle-adoptions"


# ---------------------------------------------------------------------------
# NEW-7 / B1 (v0.2.53) — bundle-update resume sentinel.
#
# Mirrors the v0.2.51 orchestrator-self pattern
# (`launcher/src-tauri/src/commands/installer.rs::write_update_resume_sentinel`)
# but scoped to per-project bundle updates rather than the
# orchestrator-self update. Same recovery shape: a JSON file lands on
# disk BEFORE any FS mutation; we delete it after the manifest write
# succeeds. If the run is killed mid-pass (Cmd-C, OOM, power loss), the
# sentinel survives and the next session-start detects it + prompts the
# user to resume (or warns them to re-run `install-bundle --update`).
#
# Without this, a mid-update interrupt leaves the manifest stale + files
# partially overwritten. The next `--update` run sees a manifest
# pointing at OLD shipped hashes for files we've already updated →
# `_file_action` returns `("preserve", ...)` for them → user-modified
# false-flagging → user-visible "5 files preserved" toast for files
# the user never touched.
#
# Audit:
# `.claude/context/audits/project-bundle-install-audit-2026-06-10.md`
# §6.6 / B1.
# ---------------------------------------------------------------------------

_BUNDLE_UPDATE_SENTINEL_REL = Path(".claude") / "state" / "bundle-update-resume-needed.json"
_BUNDLE_UPDATE_SENTINEL_SCHEMA = 1


def _bundle_sentinel_path(folder: Path) -> Path:
    """Absolute path to the bundle-update sentinel for ``folder``."""
    return folder / _BUNDLE_UPDATE_SENTINEL_REL


def read_bundle_update_resume_sentinel(folder: Path) -> Optional[dict]:
    """Read the bundle-update resume sentinel, if any.

    Returns ``None`` when:
      * the file is absent, or
      * the file is malformed JSON, or
      * the schema_version is unknown.

    Caller treats any None outcome as "no resume pending" so a broken
    sentinel never wedges the next install.
    """
    path = _bundle_sentinel_path(folder)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != _BUNDLE_UPDATE_SENTINEL_SCHEMA:
        return None
    return payload


def write_bundle_update_resume_sentinel(
    folder: Path,
    *,
    operation: str = "install-bundle-update",
    orchestrator_root: Optional[Path] = None,
    vco_version: str = "unknown",
    redirect_sink: Optional[list] = None,
) -> bool:
    """Atomic-write the bundle-update resume sentinel.

    Best-effort: any I/O failure logs to stderr + returns False rather
    than raising. The bundle install MUST proceed even when sentinel
    write fails (sentinel is a recovery aid, not a hard requirement).

    v0.2.70 (Bug B / B-1): the sentinel lives under `.claude/state/`, so when
    `.claude` is a symlink VCO refused to write through, the write redirects to
    a `.vco-new` sibling. When `redirect_sink` (a list) is provided, an
    `(original_target, vco_new)` pair is appended to it on redirect so the
    caller can fold it into the consolidated symlink deferral. Default `None`
    keeps the `bool`-return contract unchanged for all other callers.
    """
    payload = {
        "schema": _BUNDLE_UPDATE_SENTINEL_SCHEMA,
        "operation": operation,
        "folder": str(folder),
        "orchestrator_root": str(orchestrator_root) if orchestrator_root else "",
        "vco_version": vco_version,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
    }
    target = _bundle_sentinel_path(folder)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: mkdir {parent} failed: {e} — "
            f"skipping sentinel write\n"
        )
        return False
    # Tempfile + rename for atomicity. _write_file_atomic already does
    # this for arbitrary bytes; reuse it.
    try:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        _redirect = _write_file_atomic(target, body)
        if _redirect is not None and redirect_sink is not None:
            redirect_sink.append((target, _redirect))
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: write {target} failed: {e}\n"
        )
        return False
    return True


def clear_bundle_update_resume_sentinel(folder: Path) -> bool:
    """Best-effort: delete the bundle-update sentinel. Returns True on
    success or when the file was already absent; False on any other
    error. The caller never blocks on this — failing to delete a stale
    sentinel just leaves a warning surface for the next session."""
    target = _bundle_sentinel_path(folder)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: unlink {target} failed: {e}\n"
        )
        return False


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

    v0.2.54 Track G (G-4): thin alias over the single source of truth in
    `vco_lib.bundle_globs.hook_globs()` — install.py's Step 9b and the
    orchestrator-self materialize route through the same helper, ending
    the era of three divergent hook-flavour policies. See that module's
    docstring for the v0.2.14 Concern-#2 history of WHY both flavours.
    """
    from vco_lib.bundle_globs import hook_globs
    return hook_globs()


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

    `regenerated_data=True` (v0.2.57) for shipped files that are actually
    REGENERATED per-project at runtime (e.g. `knowledge/.node_formats.json`,
    the KG-summary cache rewritten by generate-kg-summary.py). Such a file
    diverges from the shipped seed the moment the project is used — that
    divergence is EXPECTED, not a user customization, so it must NOT trip
    the `bundle_user_modified_preserved` warning. On update it is silently
    kept-local (action `keep-regenerated`) UNLESS its schema version bumped,
    in which case it is re-generated/migrated (data ported forward, never
    blind-overwritten with the seed). See `_file_action` + `_schema_version`.
    """
    dest_rel: str
    source_abs: Path
    source_rel: str = ""  # rel to orchestrator_root, for manifest
    transform: Optional[Callable[[bytes], bytes]] = None
    always_overwrite: bool = False
    regenerated_data: bool = False


def _enumerate_bundle_files(
    orchestrator_root: Path,
    project_root: Path | None = None,
) -> list[_BundleFileOp]:
    """Build the list of files to install. Hooks ship BOTH .sh + .ps1
    flavours on every OS (vco_lib.bundle_globs policy, v0.2.54 Track G).

    `project_root` (optional) is the install target folder. When given,
    `{{PROJECT_ROOT}}` placeholder substitution in agent / skill .md
    bodies resolves to that folder. Defaults to the orchestrator root
    (matches old behaviour for orchestrator self-installs).

    v0.2.81: `project_root` ALSO gates curated `templates/knowledge/**`
    inclusion — only the orchestrator-root target (`project_root is None`
    or `project_root ≡ orchestrator_root`) receives the ~115 curated nodes;
    non-root projects get only the `_PER_PROJECT_KNOWLEDGE_FILES` allowlist
    and read the curated set via the shared-read fan-out. See
    `_enumerate_knowledge_ops` + `_is_root_bundle_target`.

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

    # Scripts: copy ALL recognized flavours. v0.2.54 Track G (G-4): the
    # pattern list is shared with install.py's Step 9b via
    # vco_lib.bundle_globs — the two inline copies had drifted (this one
    # was missing the extension-less detect-workflow-needs /
    # generate-workflow wrappers, so project bundles silently skipped them).
    from vco_lib.bundle_globs import script_patterns as _script_patterns
    scripts_src = templates / "scripts"
    if scripts_src.exists():
        seen: set[str] = set()
        for pat in _script_patterns():
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

    # v0.2.52 V52-C / v0.2.81: shipped KG nodes live under
    # `templates/knowledge/`. Curated nodes (concepts/tools/models/patterns)
    # now ship ROOT-ONLY — materialized once into the orchestrator root's
    # `knowledge/` (== the shared collection) and read by non-root projects
    # via the shared-read fan-out. See `_enumerate_knowledge_ops` for the
    # gate + the `_PER_PROJECT_KNOWLEDGE_FILES` allowlist that still ships
    # per-project. The gate keys on PATH identity (target ≡ orchestrator
    # root), never on collection names.
    ops.extend(_enumerate_knowledge_ops(
        orchestrator_root,
        include_curated=_is_root_bundle_target(orchestrator_root, project_root),
    ))

    return ops


def _enumerate_knowledge_ops(
    orchestrator_root: Path,
    *,
    include_curated: bool,
) -> list[_BundleFileOp]:
    """Enumerate the `templates/knowledge/**` → `knowledge/**` copy ops.

    Single enumerator with two consumers (one-concern-one-home):
      - `_enumerate_bundle_files` (gate) calls it with
        `include_curated=_is_root_bundle_target(...)`.
      - `materialize_root_knowledge` (install.py Step 4d) calls it with
        `include_curated=True`.

    When `include_curated=False` (a NON-root project), only the depth-1
    files in `_PER_PROJECT_KNOWLEDGE_FILES` are emitted — the ~115 curated
    nodes under concepts/ tools/ models/ patterns/ are skipped. Match is on
    `rel_in_knowledge` having exactly ONE path component AND the name being
    allowlisted, so a curated node accidentally placed at the top level in a
    future release is NOT silently shipped per-project.

    Pre-V52-C the curated set lived at `knowledge/` in the source tree and
    was copied through `ORCHESTRATOR_MANAGED_PATHS`; that mixed shipped +
    user-authored nodes in one directory and caused merge conflicts on
    update (modify-vs-delete races). V52-C moved it to `templates/knowledge/`
    + the bundle path; v0.2.81 makes the curated set root-only to stop
    duplicating it (disk + per-project embeddings) into every project.

    `always_overwrite=False` throughout: user customizations to a shipped
    node are PRESERVED across bundle updates via the manifest-driven hash
    compare (same V47-A pattern as agents / skills / hooks).
    """
    ops: list[_BundleFileOp] = []
    knowledge_src = orchestrator_root / "templates" / "knowledge"
    if not knowledge_src.exists():
        return ops
    for f in sorted(knowledge_src.rglob("*")):
        if f.is_dir():
            continue
        rel_in_knowledge = f.relative_to(knowledge_src)
        if not include_curated:
            # Non-root target: ship ONLY the depth-1 allowlisted files.
            is_top_level = len(rel_in_knowledge.parts) == 1
            if not (is_top_level and rel_in_knowledge.name in _PER_PROJECT_KNOWLEDGE_FILES):
                continue
        dest_rel = str(Path("knowledge") / rel_in_knowledge)
        ops.append(_BundleFileOp(
            dest_rel=dest_rel,
            source_abs=f,
            source_rel=str(f.relative_to(orchestrator_root)),
            transform=None,
            always_overwrite=False,
            # v0.2.57: regenerated per-project caches (e.g.
            # `.node_formats.json`, the KG-summary cache) diverge from the
            # shipped seed the moment the project is used — that is
            # EXPECTED, not a user customization. Flagging them
            # `regenerated_data` routes _file_action to `keep-regenerated`
            # (silent keep-local, NO bundle_user_modified_preserved warning)
            # instead of `preserve`. Schema-bump regeneration is gated by
            # the artifact_schema_versions DB registry.
            regenerated_data=_is_regenerated_data_file(dest_rel),
        ))
    return ops


# v0.2.57: filenames under templates/knowledge/ that are REGENERATED
# per-project at runtime rather than user-authored/curated. Kept as a
# narrow, explicit allowlist (not a broad glob) so a newly-shipped curated
# node can never be silently mis-classified as throwaway regenerated data.
_REGENERATED_DATA_BASENAMES: frozenset = frozenset({
    ".node_formats.json",   # KG-summary cache written by generate-kg-summary.py
})

#: Stable artifact_name for the kg_node_formats registry row. One cache per
#: project (identified by project_id), so a fixed name — NOT the folder
#: basename — keeps the row stable across project-folder renames (review N2).
_NODE_FORMATS_ARTIFACT_NAME = "default"


def _is_regenerated_data_file(dest_rel: str) -> bool:
    """True if `dest_rel` is a regenerated per-project data file (not a
    user-customizable bundle template). See `_REGENERATED_DATA_BASENAMES`."""
    return Path(dest_rel).name in _REGENERATED_DATA_BASENAMES


# v0.2.81: TOP-LEVEL files under templates/knowledge/ that KEEP shipping into
# EVERY project even after the curated node set became root-only. Narrow,
# explicit allowlist (matched only at depth 1 — see `_enumerate_knowledge_ops`)
# so a curated node accidentally placed at the top level in a future release is
# never silently shipped per-project. Rationale per entry:
#   - .node_formats.json         regenerated per-project cache SEED consumed by
#                                the kg_node_formats schema pipeline
#                                (`run_node_formats_schema_check`); NOT curated
#                                content. Gating it breaks new-project schema
#                                bootstrap.
#   - TAG_HIERARCHY.md /         per-project tag/vocabulary AUTHORING conventions
#     VOCABULARY.md              for the project's OWN nodes. Excluded from
#                                Weaviate sync anyway (sync_knowledge_graph.py
#                                EXCLUDED_FILES) → 2 disk files, ZERO embeddings.
#   - .node_embeddings.README.txt  sidecar docs for the project-local knowledge/.
_PER_PROJECT_KNOWLEDGE_FILES: frozenset = frozenset({
    "TAG_HIERARCHY.md",
    "VOCABULARY.md",
    ".node_formats.json",
    ".node_embeddings.README.txt",
})


def _is_root_bundle_target(
    orchestrator_root: Path,
    project_root: Path | None,
) -> bool:
    """True iff the bundle target IS the orchestrator clone itself.

    Drives the v0.2.81 root-only gate for curated `templates/knowledge/**`
    nodes: the curated set is materialized ONCE into the orchestrator
    root's `knowledge/` (== the shared collection, by definition — see
    install.py adopt-and-route) and NON-root projects read it via the
    shared-read fan-out, never a per-project copy.

    - `project_root is None` → True. Legacy self-install default: the
      `_enumerate_bundle_files` docstring already defines `None` as
      "defaults to the orchestrator root".
    - else → `_canonical_path_eq(project_root, orchestrator_root)` (symlink-
      + case-safe). Keyed on PATH identity, NOT on any collection-name
      comparison, so it can never disagree with the single root≡shared
      authority.

    Conservative failure mode inherited from `_canonical_path_eq`: a
    resolve error yields False → the target is treated as NON-root → curated
    knowledge is NOT shipped there (fails toward less materialization, never
    toward writing curated nodes into the wrong project's tree).
    """
    if project_root is None:
        return True
    return _canonical_path_eq(project_root, orchestrator_root)


def materialize_root_knowledge(
    install_root: Path,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """install.py Step 4d: materialize `templates/knowledge/**` → the
    orchestrator root's `knowledge/**` (v0.2.81).

    The curated bundled KG set now lives ONCE, in the orchestrator root's
    `knowledge/` (== the shared collection, by adopt-and-route definition).
    This function guarantees PRESENCE of that set on disk so the existing
    Step 7c KG-sync seeds the canonical/shared collection with it. Non-root
    projects then read the curated nodes via the shared-read fan-out, with
    no per-project copy.

    Deterministic seed (NOT a per-add "seed-if-empty"): runs on fresh AND
    `--update`, same as Step 4b/9b. Skip-existing so user-edited root nodes
    are never clobbered — full template refresh for the root remains the
    "Update bundle" / "Update all" channel (which runs `install_project_bundle`
    on the root, gate passes → knowledge included, with manifest + orphan
    semantics). Step 4d only guarantees presence for the seed.

    Prints its own `[4d/10]` step banner (same self-print precedent as Step
    9b `_install_agents_and_skills`) and — like the install.py step helpers
    that receive a callback — is fully SOFT-FAIL: any unexpected exception is
    caught, logged via `log_event`, and returned as an error rather than
    aborting the install. `log_event(step, phase, detail, data=...)` mirrors
    install.py's `_log_install_event` signature; None disables logging.

    Args:
        install_root: the orchestrator clone root (install.py PROJECT_ROOT).
        log_event: optional install.py `_log_install_event`-shaped callback.

    Returns:
        {"installed": int, "skipped": int, "errors": [str],
         "symlink_redirects": int}

    Semantics (parity with the Step 9b agents/skills idiom,
    install.py:23665-23695):
        - symlink guard: if `knowledge/`, a subdir, or a per-file target is
          a symlink VCO refuses to write through, content lands at a
          `.vco-new` sibling (V47-B) and `symlink_redirects` is incremented.
        - `os.path.lexists(dest)` on a non-symlink dest → skip (dangling
          symlink counts as occupied; never overwrite a present node).
        - else atomic write via `_write_file_atomic` (tempfile + os.replace;
          per-file AND full-ancestor symlink guard — a symlinked intermediate
          dir like knowledge/concepts/ is redirected too, not written
          through). A `.vco-new` redirect increments `symlink_redirects`
          AND `installed` (knowledge ops carry transform=None).
        - per-file Exception → collected in `errors`, loop continues
          (soft-fail; a partial seed is better than an aborted install).
        - `templates/knowledge/` missing → all-zero result, no raise.
        - idempotent: a second call → installed=0, skipped=N.
    """
    import os as _os

    def _emit(phase: str, detail: str, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event("4d/10", phase, detail, data=data)
        except TypeError:
            try:
                log_event("4d/10", phase, detail)
            except Exception:
                pass
        except Exception:
            pass

    out: dict = {
        "installed": 0, "skipped": 0, "errors": [], "symlink_redirects": 0,
    }

    print("[4d/10] Materializing curated knowledge/ (root-only) ... ",
          flush=True)
    try:
        return _materialize_root_knowledge_impl(
            install_root, out, _os=_os, emit=_emit,
        )
    except Exception as e:  # soft-fail: never abort the install
        err = f"{type(e).__name__}: {e}"
        out["errors"].append(err)
        _emit("warn", f"materialize_root_knowledge failed: {err}",
              data={"error": err})
        print(f"[4d/10] warning: {err} (continuing)", flush=True)
        return out


def _materialize_root_knowledge_impl(
    install_root: Path, out: dict, *, _os, emit,
) -> dict:
    """Core of `materialize_root_knowledge` (kept separate so the public
    entry point can wrap it in the soft-fail try)."""
    ops = _enumerate_knowledge_ops(install_root, include_curated=True)
    if not ops:
        emit("ok", "templates/knowledge/ absent — nothing to materialize",
             data={"installed": 0, "skipped": 0})
        return out

    # Redirect the whole knowledge/ subtree once if the dir itself is a
    # blocking symlink (same as Step 9b routes .claude/ to a sibling).
    knowledge_dir = install_root / "knowledge"
    if is_symlink_blocking(knowledge_dir):
        knowledge_dir = compute_vco_new_path(knowledge_dir)
        out["symlink_redirects"] += 1

    for op in ops:
        # op.dest_rel is "knowledge/<rel>"; rebase onto the (possibly
        # redirected) knowledge_dir so the subtree redirect is honoured.
        rel_below = Path(op.dest_rel).relative_to("knowledge")
        dest = knowledge_dir / rel_below
        try:
            # Skip-existing seed: never overwrite a present node (lexists
            # so a dangling non-symlink-blocked path counts as occupied).
            # A symlinked dest is NOT a skip — _write_file_atomic refuses
            # to write through it and lands the content at the `.vco-new`
            # sibling (V47-B), preserving the documented per-file redirect
            # contract (redirect counts as installed too).
            if (not is_symlink_blocking(dest)
                    and _os.path.lexists(_os.fspath(dest))):
                out["skipped"] += 1
                continue
            data = op.source_abs.read_bytes()
            if op.transform is not None:  # knowledge ops ship transform=None
                data = op.transform(data)
            # Atomic write + FULL ancestor symlink guard (walks intermediate
            # dirs — a symlinked knowledge/concepts/ redirects to
            # knowledge/concepts.vco-new/<name>, the gap the old inline
            # copyfile loop missed). Returns the redirect Path or None.
            redirect = _write_file_atomic(dest, data)
            if redirect is not None:
                out["symlink_redirects"] += 1
            out["installed"] += 1
        except Exception as e:
            out["errors"].append(f"{op.dest_rel}: {type(e).__name__}: {e}")
            continue

    emit("ok",
         f"materialized {out['installed']} / skipped {out['skipped']} "
         f"curated knowledge file(s)",
         data={k: (v if not isinstance(v, list) else len(v))
               for k, v in out.items()})
    print(f"[4d/10] done ({out['installed']} new, {out['skipped']} kept, "
          f"{len(out['errors'])} error(s)).", flush=True)
    return out


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
      "adopt"           — v0.2.84 D7 (P5/R2): file exists at a bundle-shipped
                          destination and diverges from the shipped bytes
                          (manifest-hash-mismatch OR manifest-less-no-history-
                          match). The loop backs up the CURRENT bytes then
                          writes the shipped bytes (users are not expected to
                          edit VCO codefiles). On backup-write failure the loop
                          FALLS BACK to "preserve" + deferral (never destroy
                          bytes without a captured copy).
      "preserve"        — file exists, user-modified vs manifest. Skip; emit
                          deferral. v0.2.84 D7: this action is no longer produced
                          by the default classification path — the loop only
                          reaches it via the adopt backup-failure fallback.
      "keep-regenerated"— v0.2.57: `op.regenerated_data=True` file (e.g.
                          `.node_formats.json`) that diverged because the
                          project REGENERATED it. Keep local, NO warning /
                          deferral (the divergence is expected, not a user
                          edit). Schema-bump regeneration gated separately
                          by the artifact_schema_versions DB registry.
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

    # v0.2.57: regenerated-data file (e.g. `.node_formats.json`). We reach
    # here only when the installed bytes differ from BOTH the new shipped
    # seed AND every prior-shipped/historical version — i.e. the file was
    # rewritten. For a NORMAL bundle file that means "user-modified →
    # preserve + warn". For a regenerated-data file it just means "the
    # project generated its own cache", which is EXPECTED. Return
    # `keep-regenerated` so we silently keep the local copy and DON'T emit
    # the bundle_user_modified_preserved warning. (Schema-bump-driven
    # regeneration is handled separately, gated by the
    # artifact_schema_versions DB registry — not by this hash compare.)
    if op.regenerated_data:
        return ("keep-regenerated", source_bytes)

    # v0.2.84 PLAN-v0284 D7 (P5, ruling R2): the two terminal "preserve"
    # outcomes we reach here — (a) manifest-hash-mismatch (KNOWN user-modified,
    # `prior_hash` present but `installed_hash != prior_hash`) and (b)
    # manifest-less-no-history-match (`prior_hash == ""` and the file matched
    # no template-git-history sha) — ADOPT the shipped bytes by default, EXCEPT
    # for user-owned `knowledge/**` KG nodes (see below).
    #
    # R2 verbatim: "we don't expect users to edit any VCO CODEFILE". The
    # bundle-shipped destination set that R2 covers is the CODE surface:
    # `.claude/hooks/*`, `.claude/scripts/*`, `.claude/agents/*.md`,
    # `.claude/skills/**`, `infrastructure/` compose (per the P5 anchor). For
    # those, a divergent copy is a STALE shipped version — the old `preserve`
    # outcome froze them forever + nagged with an eternal deferral (P5 incident:
    # 11 files stuck), so we adopt.
    #
    # `knowledge/**` is DIFFERENT: KG nodes are USER-OWNED state (v0.2.81 made
    # per-project knowledge user-owned — the orphan loop's knowledge-retirement
    # branch already treats it as such, and drop-collections re-syncs from the
    # on-disk `.md`). Adopting a user-modified KG node would DESTROY the user's
    # own knowledge content ("never destroy user data"). So a divergent
    # `knowledge/**` file stays `preserve` (the standing behavior, pinned by
    # test_v52_c_kg_as_user_state.py). Normalize the separator before the prefix
    # test (Windows dest_rel = `knowledge\...`) via the shared helper.
    from vco_lib.paths import to_posix_rel as _to_posix_rel
    if _to_posix_rel(op.dest_rel).startswith("knowledge/"):
        return ("preserve", source_bytes)
    #
    # `_file_action` only CLASSIFIES — the backup-then-write-shipped machinery
    # (and the backup-write-failure fallback to today's preserve + deferral)
    # lives in the loop, which has the `folder`/`target_path` context the backup
    # path needs. Other genuinely user-owned surfaces (CLAUDE.md,
    # CONTEXT_STATE.md, MEMORY.md, .env) are NOT in this `ops` set at all — they
    # flow through separate template/settings paths that are UNCHANGED. Both
    # heal paths above stay plain `overwrite` (provably VCO bytes, no backup).
    return ("adopt", source_bytes)


def _write_file_atomic(target: Path, data: bytes, *, mode: Optional[int] = None) -> Optional[Path]:
    """Atomic file write: temp file in same dir + os.replace. Optionally
    sets a unix mode bit (0o755 for shell scripts to preserve executable).

    NEW-8 / B3 (v0.2.53) — symlink-blocking defense ported from the
    orchestrator-self V47-B handling. When ``target`` itself is a
    symlink, or its parent (or any ancestor up to a sensible bound) is
    a symlink, we REFUSE to write through it. The orchestrator-self
    path handles this via ``is_symlink_blocking`` + ``compute_vco_new_path``
    (install.py:1286); per-project ``_write_file_atomic`` previously
    just did tempfile + os.replace, which on POSIX would replace the
    symlink TARGET (silent destruction of unrelated content).

    Behaviour
      * If the target itself is a symlink, redirect the write to the
        `.vco-new` sibling and emit a stderr warning so the run logs
        the redirect. The caller's expected file is gone — the new
        sibling is the new VCO-shipped content for the user to merge
        manually. Mirrors V47-B's contract.
      * If a parent directory is a symlink (e.g. user symlinked
        ``<project>/.claude`` to a shared location), same treatment:
        redirect to ``<canonical parent>.vco-new/<rest of path>``.
      * In both redirect cases we surface the redirect via stderr so
        the install log captures it, AND return the redirect target so
        the caller can surface a structured deferral (v0.2.70: Bug B —
        ``install_project_bundle`` accumulates these and emits ONE
        consolidated ``symlink_preserved_under_install_path`` deferral).

    Returns
      ``None`` on a normal (non-redirected) write. The ``redirect_target``
      ``Path`` when the write was redirected to a ``.vco-new`` sibling due
      to a symlink-blocking detection. Callers that need to surface this
      to users (``install_project_bundle`` and its settings-merge helper)
      capture it; callers that don't need deferral wiring ignore the
      return value (it's discarded as an expression statement).

    Reference:
    ``.claude/context/audits/project-bundle-install-audit-2026-06-10.md``
    §6.7 / B3.
    """
    # NEW-8 (v0.2.53) — symlink-blocking detection.
    #
    # Two cases to guard:
    #   1. `target` itself is a symlink (file/dir).
    #   2. An ancestor of `target` is a symlink — we'd silently write
    #      through it into the symlink's destination.
    # Both are redirected to the `.vco-new` sibling pattern.
    #
    # We bound the ancestor walk at the first "real" directory so we
    # don't spend time on absurd hierarchies; in practice the walk is
    # at most ~6 levels (project root → .claude → agents → ...).
    redirect_target: Optional[Path] = None
    if is_symlink_blocking(target):
        # Direct hit: target itself is a symlink.
        redirect_target = compute_vco_new_path(target)
    else:
        # Walk ancestors looking for a symlinked directory. We start
        # from target.parent (since target itself isn't a symlink) and
        # walk up. We stop walking once we leave target's chain of
        # ancestors that exist on disk.
        ancestor = target.parent
        seen: set[str] = set()
        while True:
            ancestor_str = str(ancestor)
            if ancestor_str in seen:
                break
            seen.add(ancestor_str)
            if is_symlink_blocking(ancestor):
                # Redirect the write to a `.vco-new` sibling of the
                # symlinked ancestor, replicating the rest of the path
                # inside the new directory. E.g.
                # target = .claude/agents/coder.md, ancestor = .claude
                # → redirect to `.claude.vco-new/agents/coder.md`.
                vco_new_anc = compute_vco_new_path(ancestor)
                # The path tail BELOW the symlinked ancestor.
                try:
                    rel = target.relative_to(ancestor)
                except ValueError:
                    # Defensive: if relative_to fails (shouldn't with
                    # the ancestor walk), fall back to using target's
                    # filename only.
                    rel = Path(target.name)
                redirect_target = vco_new_anc / rel
                break
            # Continue up. Stop at root or when parent doesn't change
            # (path normalisation root case).
            parent = ancestor.parent
            if parent == ancestor:
                break
            ancestor = parent

    if redirect_target is not None:
        sys.stderr.write(
            f"[vct] NEW-8 symlink-blocking: refusing to write through symlink "
            f"at {target}; redirecting to {redirect_target}. "
            f"The .vco-new sibling holds the orchestrator's intended content; "
            f"the symlink and its destination are untouched. Merge manually "
            f"when ready.\n"
        )
        target = redirect_target

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

    # v0.2.70 (Bug B): surface the redirect to the caller so it can emit a
    # structured `symlink_preserved_under_install_path` deferral. None on a
    # normal write.
    return redirect_target


def _adopt_backup_timestamp() -> str:
    """UTC basic-ISO timestamp for the per-run adoption-backup sub-dir.

    Basic ISO (``20260717T031500Z``) rather than extended (with ``:``) so the
    directory name is filesystem-safe on Windows (``:`` is illegal in NTFS
    path components). One value is computed per install run and reused for
    every file adopted in that run (a single ``<ts>`` dir per run per D7).
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_bytes_for_adoption(
    folder: Path, dest_rel: str, ts: str, current_bytes: bytes,
) -> str:
    """v0.2.84 PLAN-v0284 D7 (P5/R2): copy the CURRENT on-disk bytes of a file
    about to be ADOPTED into the per-run backup tree, atomically.

    Backup layout::

        <folder>/.claude/backups/bundle-adoptions/<ts>/<dest_rel>

    ``dest_rel`` is the bundle destination-relative path (e.g.
    ``.claude/hooks/foo.sh``), reused verbatim under the timestamp dir so the
    backup mirrors the project tree and is trivially discoverable. Uses the
    shared ``_write_file_atomic`` primitive (parents created, atomic replace,
    symlink guards apply).

    Returns the backup path RELATIVE to ``folder`` (POSIX-normalised, for the
    NOTICE / JSONL trail). Raises on any write failure — the caller MUST treat a
    raise as "do NOT adopt" and fall back to preserve + deferral (never destroy
    bytes without a captured copy).

    v0.2.84 PLAN-v0284 AMENDMENTS A4: ``dest_rel`` is host-OS-shaped (``_enumerate_bundle_
    files`` builds it via ``str(Path(...))`` → ``knowledge\\concepts\\foo.md`` on
    Windows). We normalize the separator to ``/`` via the shared
    ``vco_lib.paths.to_posix_rel`` helper (the v0.2.81 lesson — never inline a
    2nd copy) and JOIN via the individual POSIX parts so the backup mirror tree
    is byte-identical across OSes AND stays path-length-aware (component-wise
    join, no monolithic string that could overflow a Windows MAX_PATH check).
    """
    from vco_lib.paths import to_posix_rel
    rel_parts = PurePosixPath(to_posix_rel(dest_rel)).parts
    backup_abs = folder / _ADOPT_BACKUPS_REL / ts
    for part in rel_parts:
        backup_abs = backup_abs / part
    # `_write_file_atomic` may redirect through a symlink-blocking `.vco-new`
    # sibling; if it does, the ORIGINAL backup destination did not receive the
    # bytes. Treat a redirect as a backup failure (be conservative — we must
    # have the bytes at the documented path before we overwrite the original).
    redirect = _write_file_atomic(backup_abs, current_bytes)
    if redirect is not None:
        raise OSError(
            f"adoption backup for {dest_rel} was redirected to a .vco-new "
            f"sibling ({redirect}) — refusing to adopt without a captured copy "
            "at the documented backup path"
        )
    backup_rel = PurePosixPath(to_posix_rel(str(_ADOPT_BACKUPS_REL))) / ts
    for part in rel_parts:
        backup_rel = backup_rel / part
    return str(backup_rel)


def _format_file_list_md(paths: list[str], cap: int = 100) -> str:
    """Render a bullet-list of file paths for inclusion in a deferral entry.

    Caps at `cap` entries with a "... and N more" trailer when oversize so
    the deferral .md doesn't grow unbounded for large preserve / skip lists.

    Item 3 (Gap 2, 2026-05-13): cap bumped from 20 to 100. The
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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _emit_symlink_redirect_deferral(
    folder: Path,
    events: list[tuple[Path, Path]],
    install_root: Optional[Path],
) -> None:
    """Emit ONE `symlink_preserved_under_install_path` deferral covering ALL
    symlink-redirect events from a single `install_project_bundle` run.

    v0.2.70 (Bug B): wires the previously-orphaned
    `symlink_handler.emit_symlink_deferral_multi` into the project bundle's
    `.vco-new` redirect path. When `_write_file_atomic` refused to write
    through a symlinked target / ancestor (e.g. a symlinked `.claude` or
    `.claude/agents`), each redirect is accumulated as an
    `(original_target, vco_new)` pair; this helper builds the SINGLE
    consolidated `DeferralReport` entry listing every pair (NOT one entry per
    event — that would last-write-wins down to a single path, the W-F2 bug).

    Reads the existing on-disk deferral, appends the consolidated entry under
    the stable `SYMLINK_PRESERVED_CONDITION_ID` (so a re-run replaces rather
    than stacks), and writes it back.
    """
    if not events:
        return
    from vco_lib import deferral_emit as _de
    from vco_lib.symlink_handler import emit_symlink_deferral_multi

    # v0.2.83 PLAN-v0283 WP-B2: read-modify-write under the shared exclusive
    # lock. emit_symlink_deferral_multi mutates the report in place (multiple
    # (orig, vco_new) pairs fold into ONE consolidated entry), so we use the
    # locked_report context manager rather than the single-entry emit sugar.
    with _de.locked_report(folder) as report:
        emit_symlink_deferral_multi(report, events, install_root=install_root)


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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


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

    Thin wrapper around :func:`vco_lib.paths.launcher_db_path` — kept as a
    module-local symbol so existing callers (and any external imports of
    ``vco_lib.project_init._launcher_db_path``) keep working. v0.2.40 F5
    consolidation pointed every inline ``~/.vct`` reconstruction at the
    canonical resolver so future cross-OS convention changes (macOS /
    Windows) need a single fix-point.

    Resolution rules inherit from ``vct_root_dir``:

      1. ``$VCT_STATE_DIR/launcher.db`` if the env var is set.
      2. ``~/.vct/launcher.db`` otherwise.
    """
    from vco_lib.paths import launcher_db_path as _canonical
    return _canonical()


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


# ---------------------------------------------------------------------------
# v0.2.83 PLAN-v0283 B-F7 + D9: content-keyed `template_review_pending`
# dismissal memory.
#
# `template_review_pending` re-fires on EVERY bundle update for any project
# whose CLAUDE.md / CONTEXT_STATE.md / MEMORY.md meaningfully differ from the
# shipping reference — which is essentially every established project, forever.
# Most users dismiss it every time. B-F7 gives the dismissal a MEMORY: at
# dismissal time we snapshot the sha256 of each reference sidecar
# (`.claude/context/templates/<NAME>.reference.md`) into the manifest under
# `dismissals.template_review_pending.reference_hashes`. On the next run the
# producer suppresses re-emission WHILE every stored reference hash still
# matches the current sidecar — i.e. VCO has NOT shipped a genuinely new
# reference since the dismissal. The moment VCO ships a new reference (any
# stored hash mismatches, or a tracked sidecar is missing/newly appears) the
# nudge re-emits so the user learns about the new guidance. Deterministic and
# content-keyed — supersedes the companion file's 90-day timer proposal.
#
# schema_version STAYS 2 (additive optional key; readers default when absent,
# matching the v1->v2 precedent documented above the manifest constants).
# ---------------------------------------------------------------------------

# `template_review_pending` diverged-file rel-paths (live files) mapped to the
# base name used as the dismissal-hash key + the reference sidecar rel-path.
# Derived ONCE from `_PROJECT_LEVEL_TEMPLATES` so it can never drift from the
# template set the divergence check actually walks.
_TEMPLATE_REVIEW_DISMISSAL_KEY = "template_review_pending"


def _template_reference_sidecars() -> dict[str, Path]:
    """Map dismissal-hash key (template BASE name, e.g. ``"CLAUDE.md"``) to the
    reference-sidecar rel-path under the project folder.

    Single source of truth = ``_PROJECT_LEVEL_TEMPLATES``. The base name is the
    live file's ``.name`` (``CLAUDE.md`` / ``CONTEXT_STATE.md`` / ``MEMORY.md``)
    so the key is stable regardless of where the live file lives (root vs
    ``.claude/``).
    """
    out: dict[str, Path] = {}
    for _template_name, live_rel, ref_rel in _PROJECT_LEVEL_TEMPLATES:
        out[Path(live_rel).name] = Path(ref_rel)
    return out


def _current_template_reference_hashes(folder: Path) -> dict[str, str]:
    """sha256 of each EXISTING reference sidecar, keyed by template base name.

    A missing sidecar is OMITTED (its key is absent) — the divergence check
    only writes a sidecar when the live file already exists, so absence is a
    legitimate state. Read/hash errors also omit the key (conservative: an
    unhashable sidecar behaves like a changed one at compare time).
    """
    from vco_lib.hashing import sha256_file

    hashes: dict[str, str] = {}
    for base_name, ref_rel in _template_reference_sidecars().items():
        ref_path = folder / ref_rel
        if not ref_path.is_file():
            continue
        try:
            hashes[base_name] = sha256_file(ref_path)
        except Exception:  # noqa: BLE001 — unhashable sidecar → omit (treated as changed)
            continue
    return hashes


def _stored_template_dismissal_hashes(folder: Path) -> Optional[dict[str, str]]:
    """Return the reference-hash snapshot stored at the last dismissal, or
    ``None`` when there is no recorded dismissal.

    Reads ``dismissals.template_review_pending.reference_hashes`` from the
    manifest. ``None`` (no dismissal recorded) is distinct from ``{}`` (a
    dismissal recorded when NO sidecars existed) — both are handled by the
    suppression predicate.
    """
    manifest = _read_manifest(folder)
    dismissals = manifest.get("dismissals")
    if not isinstance(dismissals, dict):
        return None
    entry = dismissals.get(_TEMPLATE_REVIEW_DISMISSAL_KEY)
    if not isinstance(entry, dict):
        return None
    stored = entry.get("reference_hashes")
    if not isinstance(stored, dict):
        return None
    # Coerce to a clean str->str map (defensive against manifest tampering).
    return {str(k): str(v) for k, v in stored.items() if isinstance(v, str)}


def _template_review_dismissal_suppresses(folder: Path) -> bool:
    """True when a prior dismissal is still valid: EVERY stored reference hash
    equals the CURRENT sidecar hash (VCO shipped no new reference since the
    dismissal). Any missing/changed reference ⇒ False (re-emit).

    Suppression rule (D9):
      * No dismissal recorded (``stored is None``) ⇒ False (never suppress).
      * A stored key whose sidecar is now missing/unhashable (absent from
        ``current``) ⇒ changed ⇒ False.
      * A stored key whose current hash differs ⇒ changed ⇒ False.
      * All stored keys present AND equal ⇒ True (suppress). An empty stored
        map (dismissed when no sidecars existed) trivially satisfies "all
        equal" — but if sidecars EXIST now that weren't hashed at dismissal
        time, that's a genuinely new reference set, so those are treated as
        changed too (an empty stored map with any current sidecar ⇒ False).
    """
    stored = _stored_template_dismissal_hashes(folder)
    if stored is None:
        return False
    current = _current_template_reference_hashes(folder)
    if not stored:
        # Dismissed when no sidecars were hashed. If sidecars exist now, that's
        # new reference content the user hasn't reviewed → re-emit.
        return not current
    for base_name, stored_hash in stored.items():
        if current.get(base_name) != stored_hash:
            return False
    # Every recorded reference still matches. A NEW sidecar that wasn't part of
    # the dismissal snapshot is also "new content" → re-emit.
    for base_name in current:
        if base_name not in stored:
            return False
    return True


def _store_template_review_dismissal(folder: Path) -> None:
    """Snapshot the current reference-sidecar hashes into the manifest under
    ``dismissals.template_review_pending`` (D9 writer).

    Best-effort + SILENT: called from ``_cmd_dismiss_deferral`` AFTER the
    dismissal itself succeeds. Never raises into the caller and never writes to
    stdout/stderr (the dismiss command's JSON payload + stderr contract must
    stay byte-stable). A missing manifest is created (schema_version 2); a
    corrupt manifest is replaced with a fresh schema-2 shell carrying only the
    dismissal (the manifest's `files`/`preserved_files` will be rebuilt on the
    next install run).
    """
    try:
        manifest = _read_manifest(folder)
        dismissals = manifest.get("dismissals")
        if not isinstance(dismissals, dict):
            dismissals = {}
        dismissals[_TEMPLATE_REVIEW_DISMISSAL_KEY] = {
            "reference_hashes": _current_template_reference_hashes(folder),
            "dismissed_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        manifest["dismissals"] = dismissals
        # Preserve schema_version; _read_manifest already defaults it to 2.
        manifest.setdefault("schema_version", _MANIFEST_SCHEMA_VERSION)
        _write_manifest_atomic(folder, manifest)
    except Exception:  # noqa: BLE001 — dismissal memory is best-effort
        pass


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

    v0.2.83 PLAN-v0283 B-F7 + D9: content-keyed dismissal memory. When the
    user previously dismissed this nudge AND no reference sidecar has changed
    since (VCO shipped no new guidance), suppress re-emission. The moment VCO
    ships a new reference, the dismissal snapshot no longer matches and the
    nudge re-emits.
    """
    if not diverged_files:
        return
    # v0.2.83 B-F7/D9: honour a still-valid content-keyed dismissal.
    if _template_review_dismissal_suppresses(folder):
        _log_auto("template_review_pending suppressed (references unchanged "
                  "since dismissal)")
        return
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


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

    v0.2.70 (Bug B / B-1): the `.claude/CONTEXT_STATE.md` live file and the
    `.claude/context/templates/*.reference.md` sidecars are written via
    `_write_file_atomic`, so when `.claude` itself is a symlink VCO refused to
    write through, those writes redirect to `.vco-new`. We surface every such
    redirect via the `symlink_redirects` key so `install_project_bundle` folds
    them into the SAME consolidated symlink deferral as the main file loop.
    (CLAUDE.md / MEMORY.md live at the project ROOT — never under `.claude/` —
    so their live writes don't redirect; only their `.reference.md` sidecars,
    which live under `.claude/`, can.)
    """
    out: dict = {
        "live_created": [],
        "reference_written": [],
        "diverged": [],
        "symlink_redirects": [],  # list[tuple[Path, Path]] of (orig, vco_new)
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
                    _redirect = _write_file_atomic(live_target, substituted)
                    if _redirect is not None:
                        out["symlink_redirects"].append((live_target, _redirect))
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
                _redirect = _write_file_atomic(ref_target, substituted)
                if _redirect is not None:
                    out["symlink_redirects"].append((ref_target, _redirect))
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
    one or more collections need a LOSSY `rebuild` (drop + re-embed via
    Ollama) to reach the target schema. `rebuild` regenerates vectors
    rather than preserving them, so we DO NOT auto-apply it — we surface a
    deferral entry that names each collection + its required action and tells
    the user the explicit command to consent.

    v0.2.70: additive `copy` migrations are LOSSLESS (staging double-copy
    that round-trips every UUID + named vector + property byte-for-byte; copy
    never re-embeds and never drops the live collection) and are AUTO-APPLIED
    without a deferral. The caller (`_cmd_migrate_collections` gate) filters
    the plan to `action == "rebuild"` before calling this emitter, so it is
    only ever invoked with lossy rebuild entries.

    Args:
        folder: target user-project folder.
        project_name: raw project name (the user-facing label).
        weaviate_url: the URL the dry-run probed (echoed in the command_to_apply).
        plan_entries: list of `{"collection", "action"}` dicts where action is
            `rebuild` (legacy single-vector or unhandled escape). The gate
            filters out additive `copy` before this call.

    Severity is `warning`: the project is functional with the existing schema
    (read paths still work), but new schema features (e.g. `index_null_state`)
    are missing until the user explicitly consents to migrate.
    """
    if not plan_entries:
        return
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    # Render the per-collection action plan as a bullet list. Sorted for
    # determinism so deferral .md doesn't churn between runs that produce
    # the same plan in different order.
    detected_lines = []
    for entry in sorted(plan_entries, key=lambda e: (e.get("collection") or "", e.get("action") or "")):
        coll = entry.get("collection") or "?"
        # v0.2.70: the gate (`_cmd_migrate_collections`) filters the plan to
        # `action == "rebuild"` before calling this emitter, so every entry
        # here is a lossy rebuild (legacy single-vector or unhandled escape).
        # Additive `copy` is auto-applied, never deferred.
        detected_lines.append(
            f"  - `{coll}` → **rebuild** (drop + re-embed; legacy single-vector format)"
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
    # v0.2.54 Track D (P0-2): both commands now pass `--project-folder`
    # so the CLI's post-rebuild re-ingest step can locate the project's
    # `.claude/scripts/sync_knowledge_graph.py` and restore the dropped
    # data immediately. Pre-fix the command promised "falls back to
    # drop+re-embed" while the CLI path never re-embedded — the user's
    # collection stayed empty until the next full install.py run.
    #
    # v0.2.70: this emitter only fires for lossy `rebuild` now (additive
    # `copy` is auto-applied), so the command always documents the
    # drop + recreate + re-ingest path. The smart `migrate-collections`
    # call preserves vectors via copy where possible and only rebuilds the
    # legacy collections; `--force-rebuild` is the all-collections escape.
    folder_arg = f"--project-folder {str(folder)!r} "
    cmd = (
        f"# Run the migration from the orchestrator clone (vco_lib lives there,\n"
        f"# NOT in this project's venv). The --name flag scopes the work to\n"
        f"# THIS project's collections. Preserves vectors via copy where possible;\n"
        f"# for legacy single-vector collections it drops, recreates with the\n"
        f"# target schema, and re-ingests from knowledge/ + docs/ (requires the\n"
        f"# embedding backend to be healthy; ~3-5 min).\n"
        f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
        f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
        f"{folder_arg}--json\n"
        f"# OR force the destructive drop+recreate+re-ingest for ALL collections\n"
        f"# (slower; same embedding-backend requirement):\n"
        f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
        f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
        f"{folder_arg}--force-rebuild --json"
    )

    entry = DeferralEntry(
        condition_id="schema_migration_required",
        title="Schema migration required",
        detected=(
            f"A pre-update dry-run of `migrate-collections` against "
            f"`{weaviate_url}` reported one or more per-project Weaviate "
            f"collections need a data-rebuilding migration (drop + re-embed) "
            f"to reach the current target schema:\n"
            + "\n".join(detected_lines)
        ),
        # must match projects_v2.rs run_migrate_dry_run warning (cross-language
        # mirror — see launcher/src-tauri/src/commands/projects_v2.rs). Keep the
        # framing semantically identical: rebuild re-embeds (vectors regenerated,
        # not preserved) so it is consent-gated; additive copy is lossless and
        # auto-applied without a deferral.
        why_deferred=(
            "Schema drift detected. `rebuild` re-embeds every object via Ollama "
            "(vectors are regenerated, not preserved), so it is deferred for "
            "explicit consent. Additive `copy` migrations preserve all data "
            "(UUIDs + named vectors + properties round-trip byte-for-byte) and "
            "are auto-applied without a deferral. The bundle install (hooks, "
            "agents, scripts) still proceeds and is unaffected."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _cleanup_legacy_bash_env_in_project(
    folder: Path, *, redirect_sink: Optional[list] = None,
) -> dict:
    """Idempotent cleanup of pre-0.2.11 BASH_ENV lean-ctx shim in a user project.

    Parallel of install.py:_cleanup_legacy_bash_env_shim for the orchestrator
    self-update path. Called from `install_project_bundle` during update_mode
    so the launcher's "Update bundle" button on existing user projects also
    strips the fork-bomb fuse left over from pre-0.2.11 installs.

    v0.2.70 (Bug B / B-1, completeness): the BASH_ENV strip rewrites
    `.claude/settings.json` via `_write_file_atomic`, so when `.claude` is a
    symlink VCO refused to write through, that write redirects. When
    `redirect_sink` (a list) is provided, the `(target, vco_new)` pair is
    appended to it. (The settings-merge step usually captures the same
    `.claude/settings.json` redirect already; the consolidated emitter dedups
    duplicate pairs, so threading here is for completeness, not double-listing.)

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
        _redirect = _write_file_atomic(
            settings_file,
            (json.dumps(settings, indent=2) + "\n").encode("utf-8"),
        )
        if _redirect is not None and redirect_sink is not None:
            redirect_sink.append((settings_file, _redirect))
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
# `.claude/settings.json` `env`. But existing pre-v0.2.12 installs
# still have the legacy MCP_* keys in their
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

    v0.2.83 PLAN-v0283 B-F4 note: this emitter is now the FALLBACK path. The
    default flow auto-prunes the inert keys (see
    ``_autoprune_legacy_vscode_mcp_env_keys`` + the call site in
    ``install_project_bundle``); this deferral is only emitted when the
    auto-prune could not run (unexpected shape / write error). Kept intact so
    direct callers + the fallback still produce the historical entry.
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _autoprune_legacy_vscode_mcp_env_keys(folder: Path, detection: dict) -> bool:
    """v0.2.83 PLAN-v0283 B-F4: auto-prune the 4 inert legacy MCP_* keys from
    ``.vscode/settings.json``'s ``claude-code.env`` block.

    The keys (``MCP_WEAVIATE_SERVER`` / ``MCP_PYTHON`` / ``MCP_OLLAMA_SERVER`` /
    ``MCP_PYTHONPATH``) have been functionally inert since v0.2.12 (PR-27) and
    only bake the user's on-disk layout into the tree — deleting exactly those
    four keys is provably non-destructive to project behaviour, so it is a
    DEFAULT-ON automation (D8): no env gate.

    Guard (D8/B-F4): proceed ONLY when ``settings.json`` parses via
    ``json.loads`` to a dict AND ``"claude-code.env"`` is a dict. Otherwise
    return ``False`` (caller falls back to the deferral). An empty
    ``claude-code.env`` dict is LEFT in place (conservative — we prune keys, we
    don't restructure the file). Any write error also returns ``False`` so the
    caller emits today's deferral instead.

    Returns:
        ``True``  — the keys were pruned + written; ``record_auto_resolution``
                    was recorded; NO deferral should be emitted.
        ``False`` — could not safely prune (unexpected shape / read / write
                    error); the caller MUST fall back to the deferral.
    """
    from vco_lib.atomic import atomic_write_text
    from vco_lib import deferral_emit as _de

    settings_rel = detection.get("file", ".vscode/settings.json")
    keys = list(detection.get("keys", []) or [])
    if not keys:
        return False

    settings_file = folder / settings_rel
    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Unparseable (JSONC / trailing-comma user edits) → keep today's
        # no-op-detection behaviour; do NOT push the user toward fixing JSON.
        return False
    if not isinstance(data, dict):
        return False
    env_block = data.get("claude-code.env")
    if not isinstance(env_block, dict):
        return False

    # Prune exactly the 4 canonical legacy keys that are present.
    to_delete = [
        k for k in _LEGACY_VSCODE_MCP_ENV_KEY_PREFIXES if k in env_block
    ]
    if not to_delete:
        return False
    for k in to_delete:
        del env_block[k]
    data["claude-code.env"] = env_block  # empty dict left in place if now empty

    # Re-serialise with a stable indent + trailing newline (match VS Code's
    # 4-space convention used elsewhere for these settings files).
    try:
        serialised = json.dumps(data, indent=4) + "\n"
        atomic_write_text(settings_file, serialised)
    except (OSError, TypeError, ValueError):
        # Write / serialisation failure → fall back to the deferral.
        return False

    _de.record_auto_resolution(
        folder,
        "legacy_vscode_mcp_env_keys_present",
        "pruned_inert_vscode_mcp_keys",
        f"removed {len(to_delete)} inert legacy MCP_* key(s) "
        f"({', '.join(sorted(to_delete))}) from {settings_rel}",
        log=_log_auto,
    )
    return True


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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


# ---------------------------------------------------------------------------
# v0.2.47 RL-7.5 (2026-06-04): chunker-preset overhaul
# ---------------------------------------------------------------------------


# Version (inclusive lower bound on the NEW side) at which the chunker-preset
# overhaul lands. Anyone whose prior manifest's `vco_version` was strictly
# less than this needs to re-sync KG + codegraph against the new presets.
# Mirrors `CHUNKER_BUMP_VERSION` in
# `launcher/src-tauri/src/commands/chunker_revision_deferral.rs`.
_CHUNKER_BUMP_VERSION = "0.2.46"


def _parse_semver(version: str) -> "tuple[int, int, int] | None":
    """Parse "X.Y.Z" into (major, minor, patch). None on malformed input.

    Doesn't pull in `packaging` — orchestrator version strings are always
    plain semver without pre-release tags.
    """
    parts = version.split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _crosses_chunker_boundary(prev_version: str, running_version: str) -> bool:
    """True iff this upgrade crosses the v0.2.46 chunker-preset boundary."""
    prev = _parse_semver(prev_version)
    running = _parse_semver(running_version)
    bump = _parse_semver(_CHUNKER_BUMP_VERSION)
    if prev is None or running is None or bump is None:
        return False
    return prev < bump <= running


def _emit_chunker_resync_deferral(
    folder: Path,
    prev_version: str,
    running_version: str,
) -> None:
    """Emit `chunker_preset_overhaul_pending`: KG + codegraph need re-syncing
    after an upgrade across the v0.2.46 chunker-preset boundary.

    Post-v0.2.46 the chunker uses MUCH larger chunks for qwen3-embedding
    (target_tokens 9500 vs. legacy 1000) and a five-tier preset routing
    (xsmall/small/medium/large/xlarge). Existing Weaviate rows synced under
    the legacy presets have stale chunk boundaries: relevant content lives
    in chunk N+1 that the new preset would have folded into chunk N. Search
    recall degrades on long answers until the user re-syncs.

    Per-project entry: each project's KG + codegraph are independent
    Weaviate collections, so each gets its own deferral entry.

    Severity is `info` — searches still WORK, they just return less
    relevant top-k results. The user can defer the re-sync indefinitely.
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    detected = (
        f"This project's `.vco-manifest.json` recorded `vco_version="
        f"{prev_version}`; the current orchestrator is `{running_version}`. "
        f"The chunker presets in `claude_mcp_servers/weaviate_mcp/chunking.py` "
        f"changed at v{_CHUNKER_BUMP_VERSION} (target chunk size for qwen3 "
        f"jumped from ~1000 tokens to ~9500). Existing Weaviate rows in this "
        f"project's KG + code graph were chunked under the LEGACY presets — "
        f"search recall degrades on long answers because relevant content "
        f"lives in chunk N+1 that the new preset would have folded into "
        f"chunk N."
    )
    # v0.2.75 (C-10 family fix): the previous commands named flags the target
    # CLIs reject or ignore — `kg-sync --force` does not exist (kg-sync's
    # manual argv loop silently ignored it) and `code-graph-analyze --force`
    # is rejected by argparse ("unrecognized arguments"), so the remediation
    # could never run as written. `--force-recreate` is the real drop+rebuild
    # flag, which is exactly the "re-chunk everything" this deferral wants.
    # Guarded by tests/test_deferral_command_argparse_sweep.py.
    cmd = (
        "# Re-chunk this project's KG under the new presets:\n"
        f"cd {folder}\n"
        ".claude/scripts/kg-sync --all\n"
        "\n"
        "# Re-chunk this project's code graph under the new presets\n"
        "# (drop + rebuild the 5 Code* classes so every entity re-embeds):\n"
        ".claude/scripts/code-graph-analyze . --force-recreate\n"
        "\n"
        "# Both commands are heavy I/O (re-embeds every chunk via Ollama).\n"
        "# Consider running them when you're not actively coding."
    )

    entry = DeferralEntry(
        condition_id="chunker_preset_overhaul_pending",
        title="KG + codegraph re-sync recommended (chunker presets changed)",
        detected=detected,
        why_deferred=(
            "Auto-rechunking every KG row and code-graph entity at install "
            "time would block the bundle update for minutes and consume "
            "significant Ollama GPU time. We defer the decision so the user "
            "can pick a quiet moment. Searches WORK in the meantime — they "
            "just return less relevant top-k results than they would under "
            "the new presets. Once you run the re-sync commands below, "
            "this deferral self-resolves on the next bundle update."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[
            "knowledge/concepts/parallel-pr-coordination-gotchas-2026-05-10.md",
        ],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


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

# v0.2.83 PLAN-v0283 B-F2 + N-2: the compose-override conditions the
# reconciliation clears when they NO LONGER HOLD this run.
#
# B-F2 (original scope): `compose_override_filename_conflict` — the
# HUMAN-JUDGEMENT deferral that pre-.83 persisted FOREVER after the user
# resolved the pair (the incident this fix targets).
#
# N-2 (v0.2.83, user "fix-everything" directive): ALSO reconcile the two
# informational one-shot records. B2 originally excluded them citing (a)
# "their condition doesn't recur" and (b) the idempotency contract — but a
# stale one-shot notice STILL lingers on disk forever once its action settled,
# which is the same class of never-clearing cruft the conflict fix targets:
#   * `compose_override_renamed`  — cleared when the rename is COMPLETE (the
#     legacy path is gone this run, so nothing is re-emitted → the record is a
#     settled notice of a past migration).
#   * `compose_override_rename_failed` — cleared when the failure NO LONGER
#     recurs (the conflict/FS error is gone this run → nothing re-emitted).
# The "condition no longer holds" test is the SAME `not in active_condition_ids`
# gate the conflict uses: `active_ids` is computed FROM the producer's own
# path walk (`renamed`/`errors` populated this run), so a record only clears
# when the producer positively re-detected NO recurrence. Idempotency is
# preserved DIFFERENTLY than B2 assumed: the producer's RETURN VALUE is
# orthogonal to disk reconciliation — a second run that clears a stale record
# returns a non-None `auto_resolved` dict THAT RUN, then converges to None on
# the third run (record already gone). See `test_idempotent_after_rename`
# (updated with an N-2 comment).
_COMPOSE_OVERRIDE_RECONCILE_CONDITION_IDS = (
    "compose_override_filename_conflict",
    "compose_override_renamed",
    "compose_override_rename_failed",
)


def _classify_compose_override_conflict(legacy_path: Path, canonical_path: Path) -> str:
    """v0.2.83 PLAN-v0283 B-F2: classify a coexisting legacy+canonical compose
    override pair for auto-resolution.

    Returns one of:
      * ``"identical"``     — byte-for-byte identical. The v0.2.54 C-RT-5 mirror
        (``volumes.rs`` writes the SAME body to BOTH names by design) so this is
        the SANCTIONED pair. B-F2(i): suppress the deferral, KEEP BOTH FILES.
      * ``"semantic_equal"`` — bytes differ but ``yaml.safe_load`` of each parses
        cleanly AND compares equal (comment/whitespace drift only). B-F2(ii):
        re-mirror the legacy file to the canonical bytes (canonical wins per the
        user's "update to use the new one" ruling), NO deferral.
      * ``"divergent"``     — genuinely different (parse failure on either side,
        yaml unavailable, or parsed-unequal). B-F2(iii): keep today's
        ``compose_override_filename_conflict`` deferral verbatim.

    Conservative on every uncertainty: unreadable file, import-yaml failure, or
    a parse error anywhere ⇒ ``"divergent"`` (defer to human judgement).
    """
    try:
        legacy_bytes = legacy_path.read_bytes()
        canonical_bytes = canonical_path.read_bytes()
    except OSError:
        return "divergent"
    if legacy_bytes == canonical_bytes:
        return "identical"
    # Byte-different → try a semantic (YAML-structure) comparison.
    try:
        import yaml  # PyYAML — a hard dep of the orchestrator venv.
    except ImportError:
        # yaml unavailable → cannot prove semantic equality → conservative.
        return "divergent"
    try:
        legacy_doc = yaml.safe_load(legacy_bytes.decode("utf-8"))
        canonical_doc = yaml.safe_load(canonical_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, ValueError):
        return "divergent"
    # Only claim semantic equality for a MEANINGFUL parsed structure. A
    # comment-only / empty override parses to ``None`` (or a bare scalar), and
    # two byte-different comment-only files must NOT be read as "identical
    # config" (that would re-mirror away genuine hand-edits with no config to
    # prove equivalence). Require BOTH sides to be a non-empty mapping/sequence
    # (a real compose document is a mapping) before treating them as equal.
    if not isinstance(legacy_doc, (dict, list)) or not legacy_doc:
        return "divergent"
    if not isinstance(canonical_doc, (dict, list)) or not canonical_doc:
        return "divergent"
    if legacy_doc == canonical_doc:
        return "semantic_equal"
    return "divergent"


def _reconcile_compose_override_deferrals(
    install_root: Path,
    *,
    active_condition_ids: set[str],
) -> list[str]:
    """v0.2.83 PLAN-v0283 B-F2 + N-2: drop STALE compose-override deferrals.

    When a previously-deferred compose-override condition no longer holds this
    run — i.e. it is NOT in ``active_condition_ids``, the set this run re-emitted
    from its own path walk — resolve the on-disk entry. Scope
    (``_COMPOSE_OVERRIDE_RECONCILE_CONDITION_IDS``):
      * ``compose_override_filename_conflict`` — the pair became absent /
        identical / re-mirrored (B-F2).
      * ``compose_override_renamed`` — the rename is complete (legacy path gone
        this run → not re-emitted) (N-2).
      * ``compose_override_rename_failed`` — the failure no longer recurs
        (conflict / FS error gone this run → not re-emitted) (N-2).

    Returns the list of condition_ids actually resolved (for the additive
    ``auto_resolved`` / ``auto_resolved_condition_ids`` return keys + the
    ``record_auto_resolution`` bookkeeping). Because these IDs are FOREIGN to
    install.py (project_init emits them), the caller MUST replay each into the
    run-scoped ``DeferralReport`` via ``mark_resolved`` (see B-1 /
    ``install._replay_compose_override_resolutions``) or ``finalize()``'s
    late-merge would resurrect a record we just cleared on disk. Soft-fail:
    never raises.
    """
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralReport

    try:
        on_disk = DeferralReport.read(install_root)
    except Exception:  # noqa: BLE001 — unparseable report → nothing to reconcile
        return []
    present = {e.condition_id for e in on_disk.entries}
    stale = [
        cid for cid in _COMPOSE_OVERRIDE_RECONCILE_CONDITION_IDS
        if cid in present and cid not in active_condition_ids
    ]
    if not stale:
        return []
    _de.resolve_conditions(install_root, stale, log=_log_auto)
    # v0.2.83 B-1: leave a visible trail for each reconciled condition. Before
    # this the reconcile branch cleared the on-disk entry via resolve_conditions
    # but never wrote an auto-resolutions.jsonl row (the caller appended to a
    # list nobody replayed), so a self-clear was invisible in the audit log.
    for cid in stale:
        _de.record_auto_resolution(
            install_root,
            cid,
            "reconciled_stale_compose_override",
            f"cleared stale {cid} deferral (condition no longer holds this run)",
            log=_log_auto,
        )
    return stale


def _detect_and_rename_legacy_compose_override(install_root: Path) -> Optional[dict]:
    """Detect any legacy `docker-compose.override.yml` files under
    `install_root` and rename them to `compose.override.yaml` so
    podman-compose's auto-loader recognizes them.

    Searches the directories listed in `_COMPOSE_OVERRIDE_SEARCH_SUBDIRS`
    (currently `infrastructure/` and `claude_mcp_servers/`). For each
    legacy file found:

    - If the target `compose.override.yaml` already exists in the same
      directory: CLASSIFY the pair (v0.2.83 PLAN-v0283 B-F2):
        * byte-identical (the v0.2.54 C-RT-5 mirror) → auto-resolve by
          SUPPRESSION: no deferral, KEEP BOTH FILES, record an auto-resolution.
        * yaml-semantically-equal (comment/whitespace drift only) → re-mirror
          the legacy file to the canonical bytes (canonical wins), no deferral,
          record an auto-resolution.
        * genuinely divergent (parse failure / parsed-unequal / yaml missing)
          → keep today's `compose_override_filename_conflict` deferral verbatim.
      Do NOT rename in any conflict case.
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
        "conflicts": [(old, new), ...], "errors": [(path, err), ...],
        "auto_resolved": [...]}`` when at least one legacy file was detected
        OR at least one stale compose deferral was reconciled, else ``None``.
        The ``auto_resolved`` key (v0.2.83 additive — install.py:6497 reads
        only ``action``/``renamed``/``conflicts``/``errors`` so its shape is
        unchanged) lists a human-readable summary per auto-resolution. The
        caller logs based on this; this function emits the deferral entries
        directly so callers can stay terse.

    PR-22 (2026-05-16). See:
    - knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md
    - .claude/context/PUBLIC_REPO_FIXES_REPORT_2026-05-16.md (Fixes 1, 2, 11)
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de
    from vco_lib.atomic import atomic_write_bytes

    install_root = Path(install_root)
    renamed: list[tuple[Path, Path]] = []
    conflicts: list[tuple[Path, Path]] = []
    errors: list[tuple[Path, str]] = []
    auto_resolved: list[str] = []
    # v0.2.83 B-1: the CONDITION IDs this run auto-resolved on disk, for the
    # additive ``auto_resolved_condition_ids`` return key. install.py's caller
    # replays each into the RUN-scoped DeferralReport via ``mark_resolved`` so
    # the A-2 seed → finalize P1 late-merge cannot resurrect an entry we just
    # cleared on disk (the resolve_conditions tombstone is per-instance — it
    # lives on the throwaway report inside locked_report, not on the run report).
    auto_resolved_condition_ids: set[str] = set()

    for subdir in _COMPOSE_OVERRIDE_SEARCH_SUBDIRS:
        legacy_path = install_root / subdir / _LEGACY_COMPOSE_OVERRIDE_NAME
        if not legacy_path.is_file():
            continue
        target_path = install_root / subdir / _CANONICAL_COMPOSE_OVERRIDE_NAME
        if target_path.exists():
            # v0.2.83 B-F2: both exist — classify before deferring.
            classification = _classify_compose_override_conflict(
                legacy_path, target_path,
            )
            if classification == "identical":
                # B-F2(i): the sanctioned v0.2.54 C-RT-5 mirror. Suppress the
                # deferral, KEEP BOTH FILES (deleting the legacy name breaks
                # docker-compose-v1 auto-load AND loops the mirror-writer).
                detail = (
                    f"byte-identical compose override pair (sanctioned "
                    f"C-RT-5 mirror) — kept both `{legacy_path}` and "
                    f"`{target_path}`"
                )
                _de.record_auto_resolution(
                    install_root,
                    "compose_override_filename_conflict",
                    "kept_identical_mirror_pair",
                    detail,
                    log=_log_auto,
                )
                auto_resolved.append(detail)
                auto_resolved_condition_ids.add("compose_override_filename_conflict")
                continue
            if classification == "semantic_equal":
                # B-F2(ii): comment/whitespace drift only. Canonical content
                # wins — re-mirror the legacy file to the canonical bytes so
                # the pair is byte-identical again (a no-op mirror on the next
                # run). Semantics are provably preserved (yaml.safe_load equal).
                try:
                    canonical_bytes = target_path.read_bytes()
                    atomic_write_bytes(legacy_path, canonical_bytes)
                except OSError as exc:
                    # Re-mirror failed → fall back to the human deferral so the
                    # drift is still surfaced (never silently drop it).
                    conflicts.append((legacy_path, target_path))
                    _log_auto(
                        f"compose override re-mirror failed for "
                        f"{legacy_path}: {type(exc).__name__}: {exc} — "
                        "kept conflict deferral"
                    )
                    continue
                detail = (
                    f"re-mirrored semantically-identical compose override "
                    f"`{legacy_path}` to canonical bytes from `{target_path}` "
                    "(comment/whitespace drift only)"
                )
                _de.record_auto_resolution(
                    install_root,
                    "compose_override_filename_conflict",
                    "remirrored_semantic_equal_override",
                    detail,
                    log=_log_auto,
                )
                auto_resolved.append(detail)
                auto_resolved_condition_ids.add("compose_override_filename_conflict")
                continue
            # B-F2(iii): genuinely divergent — keep today's deferral. Don't
            # overwrite the canonical file with the legacy one (or vice versa).
            conflicts.append((legacy_path, target_path))
            continue
        try:
            legacy_path.rename(target_path)
            renamed.append((legacy_path, target_path))
        except (OSError, PermissionError) as exc:
            # Soft-fail: log + record. Most common cause is read-only FS
            # (e.g. user mounted the install root noexec/ro for hardening).
            errors.append((legacy_path, f"{type(exc).__name__}: {exc}"))

    # v0.2.83 B-F2 reconciliation: compute the condition_ids this run
    # (re-)emits, then clear any STALE compose-override deferral (a
    # previously-deferred pair now absent/identical/re-mirrored, or a settled
    # rename/rename-failed). This is the first reconciliation the compose
    # producer has ever had — pre-.83 a resolved conflict persisted forever.
    active_ids: set[str] = set()
    if renamed:
        active_ids.add("compose_override_renamed")
    if conflicts:
        active_ids.add("compose_override_filename_conflict")
    if errors:
        active_ids.add("compose_override_rename_failed")
    reconciled = _reconcile_compose_override_deferrals(
        install_root, active_condition_ids=active_ids,
    )
    for cid in reconciled:
        auto_resolved.append(f"cleared stale {cid} deferral (no longer applies)")
        auto_resolved_condition_ids.add(cid)

    if not renamed and not conflicts and not errors:
        # Nothing to emit. Return a dict ONLY when we auto-resolved / reconciled
        # something (so the caller can log it); else None (true no-op).
        if auto_resolved:
            return {
                "action": "auto_resolved",
                "renamed": [],
                "conflicts": [],
                "errors": [],
                "auto_resolved": auto_resolved,
                # v0.2.83 B-1 additive: the caller replays these into the
                # run-scoped DeferralReport (mark_resolved) so seed→finalize
                # cannot resurrect an entry we just cleared on disk.
                # M-NEW-1 symmetry note: no active-cid subtraction needed on THIS
            # return path — it is only reachable when renamed/conflicts/errors
            # are all empty, so active_ids is empty by construction.
            "auto_resolved_condition_ids": sorted(auto_resolved_condition_ids),
            }
        return None

    # Emit deferral entries via the ONE locked emitter home (separate
    # condition_id per outcome class so the operator can see at a glance what
    # happened). Batched so foreign entries are preserved + the write is atomic.
    _pending_entries: list[DeferralEntry] = []

    if renamed:
        renamed_lines = "\n".join(
            f"- `{old}` → `{new}`" for old, new in renamed
        )
        _pending_entries.append(DeferralEntry(
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
        _pending_entries.append(DeferralEntry(
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
        _pending_entries.append(DeferralEntry(
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

    # v0.2.83 PLAN-v0283 WP-B2: batch-emit via the ONE locked emitter home
    # (read-modify-write under the exclusive lock; foreign entries preserved).
    if _pending_entries:
        _de.emit_entries(install_root, _pending_entries, log=_log_auto)

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
        # v0.2.83 additive key (install.py:6497 does not read it — frozen
        # contract preserved).
        "auto_resolved": auto_resolved,
        # v0.2.83 B-1 additive: condition IDs this run cleared on disk. The
        # install.py caller replays them into the run-scoped DeferralReport
        # (mark_resolved) so seed→finalize cannot resurrect them.
        #
        # v0.2.83 re-review M-NEW-1: subtract cids that are ALSO ACTIVE this
        # run. condition_ids are shared across directory pairs — with a
        # byte-identical pair in one subdir (auto-resolved) AND a divergent
        # pair in another (fresh conflict emitted THIS run under the SAME
        # cid), replaying the cid would tombstone the run report and finalize
        # would clobber the fresh human-judgement deferral. A cid that is
        # simultaneously auto-resolved (for one pair) and active (for
        # another) must stay alive; the resolved pair's cleanup is complete
        # on disk regardless.
        "auto_resolved_condition_ids": sorted(
            auto_resolved_condition_ids - active_ids
        ),
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
    or capitalisation drift ("Quux" vs "QUUX", "Foo" vs "FoO").

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


def _live_kg_collection_binding() -> Optional[str]:
    """Return the project's LIVE knowledge-graph collection binding — the
    Weaviate class the project's MCP / hooks actually read from — or None.

    BUG-1 (v0.2.73): the sanitizer (`sanitize_for_weaviate_class`) is
    case-LOSSY — an all-lowercase project name like ``vibecoded-orchestrator``
    yields the lowercase-c ``VibecodedOrchestrator_KnowledgeGraph`` which does
    NOT exist, while the real active class is the uppercase-C
    ``VibeCodedOrchestrator_KnowledgeGraph`` (populated). The active binding is
    the authoritative "canonical" — the sanitizer's guess is subordinate to it.

    The canonical channel that reaches this install-time process is the
    ``KG_COLLECTION`` env var (set by ``.claude/settings.json env`` /
    ``.claude/env`` / hub resolution — the same value MCP subprocesses see).
    An empty string is treated as "no binding" (v0.2.27 empty-env coercion).
    Returns the raw class name (case preserved) so callers can compare it
    against the live Weaviate schema exactly.
    """
    val = os.environ.get("KG_COLLECTION", "")
    return val.strip() or None


def _kg_binding_keep_set_normalised() -> tuple[set[str], bool]:
    """Return ``(normalised_kg_class_names, resolvable)`` — EVERY project's live
    KG collection binding (all roles), normalised for case-insensitive match.

    SEV-2 #2 (v0.2.73): the legacy-KG drop detector must NEVER emit a drop
    command against ANOTHER active project's KG class. BUG-1 only protected the
    SELF collection; a different project's ``Foobar_KnowledgeGraph`` can
    substring/Levenshtein-match this project's ``Foo`` prefix via
    ``_is_similar_prefix`` and be proposed as a drop target — but the consented
    command re-embeds from THIS project's ``.md`` (never the other project's),
    so running it drops the other project's populated KG unrecoverably.

    This mirrors ``_codegraph_keep_set_normalised``: the launcher.db binding
    table is the ground truth. ``resolvable`` is False only when launcher.db is
    unreachable — callers MUST refuse to emit any drop command in that case
    (conservative data-safety), never treat an empty set as "nothing is live".
    """
    try:
        from vco_lib.launcher_db_reader import kg_binding_keep_set
        names, resolvable = kg_binding_keep_set()
    except Exception:
        return (set(), False)
    normed = {_normalise_prefix_for_match(n) for n in names if n}
    normed.discard("")
    return (normed, resolvable)


def _detect_legacy_collections_with_suffixes(
    project_name: str,
    weaviate_url: str,
    suffixes: tuple,
    *,
    live_binding: Optional[str] = None,
    cross_project_keep_set: Optional[set[str]] = None,
    cross_project_keep_resolvable: Optional[bool] = None,
) -> list[dict]:
    """Shared core for legacy KG + legacy code-graph detection.

    Args:
        project_name: raw project name as registered with the launcher.
        weaviate_url: Weaviate REST endpoint.
        suffixes: tuple of class-name suffixes to inspect (KG family or
            code-graph family).
        live_binding: the project's LIVE knowledge-graph collection (the
            class it actually reads — env ``KG_COLLECTION`` / hub-resolved).
            When provided, it is the AUTHORITATIVE canonical: a candidate
            equal to it (case-insensitively) is NEVER a drop target. Defaults
            to `_live_kg_collection_binding()` for the KG suffix family; for
            code-graph the caller passes None (code-graph has no single env
            binding — it's regenerable, and its skip stays sanitizer-based).

    Returns a list of candidate dicts, each with:
        {
          "class_name":     "<old class name>",
          "suffix":         "_KnowledgeGraph" (etc.),
          "object_count":   int | None,    # None when Weaviate unreachable
          "embedding_dim":  int | None,    # None when not discoverable
          "canonical_name": "<canonical class for this project + suffix>",
          "case_only":      bool,          # BUG-1: candidate differs from the
                                           # canonical ONLY by case → a
                                           # case-REBIND, never copy+drop.
        }

        cross_project_keep_set: SEV-2 #2 (KG family only). The normalised set
            of EVERY project's live KG collection binding (all roles), from
            launcher.db. A candidate whose full class name (normalised) is in
            this set is ANOTHER project's live KG — NEVER a drop target, even
            when ``_is_similar_prefix`` matches this project's prefix. Code-graph
            callers pass None (their exclusion is prefix-based via the code
            keep-set, applied elsewhere).
        cross_project_keep_resolvable: pairs with ``cross_project_keep_set``.
            When False (launcher.db unreachable), the keep-set is NOT applied to
            detection (historic substring/Levenshtein behavior is retained), and
            the emitted DROP command's RUN-TIME re-validation
            (``_legacy_kg_drop_revalidated``) is the conservative gate — it
            refuses any drop when the keep-set can't be confirmed. So detection
            never depends on ambient launcher.db state, yet an unverifiable
            candidate can never be dropped.

    Returns [] in any of these conditions (treated as "nothing to migrate"):
      - Weaviate unreachable.
      - No classes match the suffix family.
      - All matching classes have a different prefix than THIS project's
        canonical prefix (i.e., they belong to OTHER projects — never auto-
        suggest migrating someone else's data).
      - The only matching class IS the canonical name (fresh-install path),
        matched CASE-INSENSITIVELY (BUG-1) or equal to the live binding.
      - (KG family) the candidate class is ANY project's live KG binding
        (SEV-2 #2 cross-project exclusion).
    """
    canonical_prefix = sanitize_for_weaviate_class(project_name)
    if not canonical_prefix:
        return []
    # BUG-1: the live KG binding (env KG_COLLECTION) is the authoritative
    # canonical — the class the project actually reads. A candidate equal
    # to it is BY DEFINITION not a legacy drop target. Lowercased once here
    # for O(1) case-insensitive comparison in the loop.
    live_binding_lc = (live_binding or "").strip().lower() or None

    # SEV-2 #2: cross-project KG-binding exclusion. When a RESOLVABLE keep-set is
    # supplied (KG family), any candidate whose normalised class name is ANY
    # project's live KG binding is excluded from detection — it's active data,
    # never a drop target. When the keep-set is UNRESOLVABLE (launcher.db down),
    # detection keeps its historic substring/Levenshtein behavior (deterministic,
    # informational), and the emitted DROP command is the conservative gate: it
    # RE-VALIDATES against the live binding table at RUN time via
    # `_legacy_kg_drop_revalidated`, which REFUSES the drop when the keep-set is
    # unresolvable. So an unverifiable candidate can be surfaced but can NEVER be
    # dropped — the data-safety invariant holds without making detection depend
    # on ambient launcher.db state.
    cross_keep = (
        cross_project_keep_set
        if (cross_project_keep_set is not None
            and cross_project_keep_resolvable is not False)
        else None
    )
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
        # BUG-1 (v0.2.73): skip the canonical class itself — it's NOT legacy.
        # Two authoritative skip rules, both CASE-INSENSITIVE:
        #   (a) class matches the sanitizer-derived canonical name (case-
        #       insensitively). Historically this was case-SENSITIVE `==`,
        #       which let the real uppercase-C active class slip past the
        #       skip when the sanitizer produced a lowercase-c guess — and
        #       be emitted as a legacy candidate whose proposed destination
        #       (the nonexistent lowercase-c class) turned the migration
        #       command into a self-destruct on the real 2590-node KG.
        #   (b) class equals the project's LIVE `KG_COLLECTION` binding
        #       (the class the project actually reads). The live binding is
        #       BY DEFINITION not a drop target, regardless of the
        #       sanitizer's casing guess.
        class_name_lc = class_name.lower()
        if class_name_lc == canonical_name.lower():
            continue
        if live_binding_lc is not None and class_name_lc == live_binding_lc:
            continue

        # SEV-2 #2: cross-project KG-binding exclusion. A candidate whose FULL
        # class name (normalised) is ANY project's live KG binding is that
        # project's active data — NEVER a drop target. Mirrors the code-graph
        # binding-exclusion (`_detect_orphan_code_collections`). This closes the
        # substring false-match hole: `Foobar_KnowledgeGraph` (a different
        # project's live 2590-node class) would otherwise be emitted for project
        # `Foo` because "foo" is a substring of "foobar".
        if cross_keep is not None:
            norm_class = _normalise_prefix_for_match(class_name)
            if norm_class and norm_class in cross_keep:
                continue

        # Conservative prefix-similarity check.  Without this we'd
        # mistakenly suggest migrating Quux_KnowledgeGraph just because
        # the user added a project called "Foo".
        if not _is_similar_prefix(cand_prefix, canonical_prefix):
            continue

        # Object count via GraphQL Aggregate (lightweight; no v4 client).
        count = _http_count_objects(class_name, weaviate_url)
        emb_dim = _embedding_dim_from_schema(cls)

        # BUG-1: `case_only` marks a candidate that differs from the
        # canonical ONLY by case → a case-REBIND (metadata rename of the
        # launcher binding), NEVER a copy+drop of vectors. With the
        # case-insensitive skip above, a candidate that is a case-variant of
        # the *sanitizer* canonical is already skipped, so this is normally
        # False for emitted candidates. It is kept as an explicit,
        # authoritative field so `_format_legacy_kg_command` can route + a
        # future detector regression (e.g. a candidate re-surfaced through a
        # different path) is still caught by the same-name guard downstream.
        case_only = class_name_lc == canonical_name.lower()
        candidates.append({
            "class_name": class_name,
            "suffix": sfx,
            "object_count": count,
            "embedding_dim": emb_dim,
            "canonical_name": canonical_name,
            "case_only": case_only,
        })

    # Stable order: by suffix then class_name (deterministic deferral .md).
    candidates.sort(key=lambda c: (c["suffix"], c["class_name"]))
    return candidates


def _detect_legacy_kg_collections(
    project_name: str,
    weaviate_url: str,
    *,
    live_binding: Optional[str] = None,
) -> list[dict]:
    """Detect KG-family classes (KnowledgeGraph + Development) that look
    like THIS project's data under a different prefix.

    See `_detect_legacy_collections_with_suffixes` for the full contract.

    `live_binding` defaults to the project's live `KG_COLLECTION` env
    binding (`_live_kg_collection_binding()`) — the AUTHORITATIVE canonical
    that must never be proposed as a drop target (BUG-1). Tests inject it
    explicitly; the install caller relies on the env default.
    """
    if live_binding is None:
        live_binding = _live_kg_collection_binding()
    # SEV-2 #2: resolve the cross-project KG-binding keep-set so a DIFFERENT
    # active project's live KG class can never be emitted as a drop target.
    cross_keep, cross_resolvable = _kg_binding_keep_set_normalised()
    return _detect_legacy_collections_with_suffixes(
        project_name, weaviate_url, _KG_SUFFIXES,
        live_binding=live_binding,
        cross_project_keep_set=cross_keep,
        cross_project_keep_resolvable=cross_resolvable,
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


def _legacy_kg_drop_revalidated(class_name: str) -> bool:
    """RUN-TIME re-validation guard for a consented legacy-KG drop (SEV-2 #2).

    Returns True ONLY when ``class_name`` (normalised) is confirmed to be NOT a
    live KG binding of ANY project AND the keep-set is resolvable. Mirrors
    ``_revalidated_orphan_live_classes`` for code: the deferral command re-checks
    the CURRENT launcher bindings at the moment the user runs it (a project may
    have been re-added between detect-time and run-time, recreating the binding),
    never trusting the detect-time snapshot.

    Conservative: returns False (refuse the drop) when the keep-set is
    UNRESOLVABLE (launcher.db down) — we never drop a populated collection we
    cannot positively confirm is dead. This function is embedded verbatim into
    the emitted deferral command so the guard runs on the user's machine at
    execution time.
    """
    keep_set, resolvable = _kg_binding_keep_set_normalised()
    if not resolvable:
        return False
    norm = _normalise_prefix_for_match(class_name)
    if not norm:
        return False
    return norm not in keep_set


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


def _format_case_rebind_instruction(old: str, new: str) -> list[str]:
    """Render a NON-destructive case-rebind instruction for a legacy
    candidate that differs from the canonical ONLY by case (BUG-1).

    A case-only pair (``old.lower() == new.lower()``) is NEVER a legitimate
    copy+drop — the two names refer to the SAME logical collection, and the
    stored data lives under whichever casing Weaviate actually created. The
    correct migration is a metadata case-rebind of the launcher binding
    (make the project read the actual-cased class), which
    ``bootstrap-collections`` already performs via its case-conflict
    recovery path (``regenerated[].reason == "case-conflict"``). Emitting a
    ``_delete_class(old)`` here would DROP the populated collection.
    """
    return [
        f"# CASE-ONLY difference detected: {old!r} vs {new!r} refer to the",
        "# SAME collection under different casing. This is a metadata",
        "# case-REBIND, NOT a data migration — do NOT copy+drop (that would",
        "# destroy the populated collection). Re-run bootstrap-collections;",
        "# its case-conflict recovery rebinds the launcher to the actual-",
        "# cased class:",
        "python -m vco_lib.project_init bootstrap-collections "
        f"--name {new!r}",
        "# Verify the project now reads the populated class:",
        "#   .claude/scripts/kg-search list",
        "",
    ]


def _format_legacy_kg_command(
    project_name: str,
    weaviate_url: str,
    candidates: list[dict],
) -> str:
    """Render the suggested migration commands for the KG deferral.

    Two disjoint remediation shapes, selected per candidate:

      * CASE-ONLY pair (``old.lower() == new.lower()``, BUG-1): a metadata
        case-REBIND, never a copy+drop. Rendered by
        ``_format_case_rebind_instruction``. A HARD GUARD here refuses to
        emit any ``_delete_class(old)`` / ``_copy_collection_with_vectors``
        for such a pair — belt-and-suspenders so even a future detector
        regression can't produce a self-destruct command on a collection the
        project is actively bound to.

      * GENUINE legacy (different prefix, real drop target — BUG-2): the
        remediation is RE-EMBED-FROM-`.md`, NOT copy-vectors. When the
        schema/model changed, the legacy vectors are the wrong shape (a
        named-vector copy into a single-vector destination 422s) and often
        the wrong model; the on-disk ``knowledge/**/*.md`` is the source of
        truth. Sequence: ensure the canonical exists with the current
        named-vector schema (``bootstrap-collections``), re-embed from
        ``.md`` (``kg-sync --all``), then drop the legacy class.
    """
    lines = [
        "# Per-candidate migration. Two shapes below:",
        "#   * CASE-ONLY name difference → metadata case-REBIND (no data op).",
        "#   * GENUINE legacy prefix     → RE-EMBED from source .md, then drop.",
        "# The canonical class is created by install-bundle's bootstrap step;",
        f"# verify it before running:  curl -s {weaviate_url}/v1/schema | python -m json.tool",
        "",
    ]
    for c in candidates:
        old = c["class_name"]
        new = c["canonical_name"]
        # HARD GUARD (BUG-1): a case-only pair is NEVER a copy+drop. Route
        # to a case-rebind instruction regardless of the detector's flag —
        # the name comparison here is authoritative and independent, so a
        # future detector regression that mis-flags `case_only` still cannot
        # produce a destructive command.
        if old.lower() == new.lower():
            lines.extend(_format_case_rebind_instruction(old, new))
            continue

        # GENUINE legacy (different prefix): re-embed from source .md
        # (BUG-2). No _copy_collection_with_vectors — cross-schema copies
        # 422 on named-vector/single-vector shape mismatch, and stale
        # vectors may carry the wrong embedding model.
        cnt = c.get("object_count", "?")
        lines.append(f"# {old} → {new}  ({cnt} objects) — re-embed from source .md")
        lines.append("# 1. Ensure the canonical exists with the current named-vector schema:")
        lines.append(
            "python -m vco_lib.project_init bootstrap-collections "
            f"--name {new!r}"
        )
        lines.append("# 2. Re-embed the canonical from the on-disk knowledge/**/*.md (source of truth):")
        lines.append(".claude/scripts/kg-sync --all")
        lines.append("# 3. Drop the legacy class (vectors were the wrong shape/model; .md already re-embedded).")
        lines.append("#    RUN-TIME re-validation (SEV-2 #2): the guard refuses the drop if this")
        lines.append("#    class is a LIVE KG binding of ANY project (re-checked NOW, not at detect")
        lines.append("#    time), or if launcher.db can't be read (conservative — never drop a")
        lines.append("#    populated collection we can't confirm is dead):")
        lines.append(
            "python -c \"from vco_lib.project_init import _delete_class, "
            "_legacy_kg_drop_revalidated; "
            f"n={old!r}; "
            f"(_delete_class(n, weaviate_url={weaviate_url!r}) "
            "or print('dropped ' + n)) if _legacy_kg_drop_revalidated(n) "
            "else print('REFUSED (live KG binding of a project, or launcher.db "
            "unreadable): ' + n)\""
        )
        lines.append("")
    lines.append(
        "# Once migration succeeds, the canonical class holds the data and"
    )
    lines.append(
        "# any dropped legacy class is gone.  Re-running install-bundle will"
    )
    lines.append(
        "# see no remaining candidates and clear this deferral entry."
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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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

    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _autodrop_empty_codegraph_candidates(
    folder: Path,
    candidates: list[dict],
    weaviate_url: str,
) -> tuple[list[str], list[dict]]:
    """v0.2.83 PLAN-v0283 B-F6: auto-drop EMPTY legacy code-graph candidates.

    Code-graph collections are regenerated from source, so an EMPTY legacy
    class is a zero-cost orphaned shell — dropping it is provably
    non-destructive (default-ON, no env gate). The KG-family is NEVER touched
    here (this is only ever called with code-graph candidates; KG data is not
    regenerable — out of scope by design).

    Per candidate, drop ONLY when ALL of:
      * ``object_count == 0`` at detect time, AND
      * ``case_only`` is False (a case-only variant refers to the SAME logical
        collection — dropping it would destroy live data; BUG-1 invariant), AND
      * a RE-PROBE via ``_http_count_objects`` IMMEDIATELY before the drop still
        returns exactly 0 (guards a row that filled in between detect and drop).
    After a successful ``_delete_class`` a POST-DROP re-probe confirms the class
    is gone (count is None/0 → absent). Any deviation (count>0, count unknown
    None, re-probe mismatch, delete error, post-probe still populated) ⇒ the
    candidate STAYS in the deferred remainder (conservative: defer to human).

    Weaviate unreachable ⇒ re-probe returns None ⇒ nothing dropped (existing
    soft-fail). Never raises.

    Returns ``(dropped_class_names, remaining_candidates)``.
    """
    from vco_lib import deferral_emit as _de

    dropped: list[str] = []
    remaining: list[dict] = []
    for cand in candidates:
        class_name = cand.get("class_name") or ""
        count = cand.get("object_count")
        case_only = bool(cand.get("case_only"))
        # Only empty, non-case-only candidates are drop-eligible.
        if not class_name or case_only or count != 0:
            remaining.append(cand)
            continue
        # Re-probe IMMEDIATELY before the drop (re-probe-before-acting).
        try:
            live_count = _http_count_objects(class_name, weaviate_url)
        except Exception:  # noqa: BLE001 — treat probe failure as unknown
            live_count = None
        if live_count != 0:
            # Filled in since detect, or unknown (None) → do NOT drop.
            remaining.append(cand)
            continue
        # Drop, then POST-DROP re-probe to confirm absence.
        try:
            _delete_class(class_name, weaviate_url=weaviate_url)
        except Exception as exc:  # noqa: BLE001 — drop failed → keep deferred
            _log_auto(
                f"codegraph auto-drop failed for {class_name}: "
                f"{type(exc).__name__}: {exc} — kept in deferral"
            )
            remaining.append(cand)
            continue
        try:
            post_count = _http_count_objects(class_name, weaviate_url)
        except Exception:  # noqa: BLE001
            post_count = None
        # A dropped class aggregates to 0 (empty) or None (class missing) —
        # both mean "gone". A positive count means the drop didn't take.
        if isinstance(post_count, int) and post_count > 0:
            _log_auto(
                f"codegraph auto-drop of {class_name} did not take "
                f"(post-probe count={post_count}) — kept in deferral"
            )
            remaining.append(cand)
            continue
        dropped.append(class_name)
        _de.record_auto_resolution(
            folder,
            "codegraph_collection_legacy_candidates",
            "dropped_empty_legacy_codegraph_class",
            f"dropped empty legacy code-graph class `{class_name}` "
            "(re-probed 0 before + absent after) — regenerable from source",
            log=_log_auto,
        )
    return dropped, remaining


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
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

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

    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


# ═══════════════════════════════════════════════════════════════════════════
# v0.2.73 FIX-C — Orphan CODE-collection cleanup detector (CONSENTED, never
# auto-drop) + FIX-C-RECUR — prefix-drift forward-guard.
#
# H3 found ~4.6 GB of orphan CODE collections the existing wizard cannot
# reclaim (4 gaps). This detector closes them with a BINDING-EXCLUSION seed —
# the AUTHORITATIVE keep-set is `project_codegraph_bindings.collection_prefix`
# (launcher.db) + `project_codegraph_extra_paths` owners — NOT the BUG-1
# `_is_similar_prefix` heuristic (which R1 proved is the data-loss vector).
# This mirrors the Rust wizard's `normalise_prefix_for_match` binding-exclusion
# (project_identity.rs:716), the SOUND design.
#
# DATA-SAFETY (R1 BLOCKER-1, non-negotiable):
#   * NEVER flag a prefix that matches (case-insensitively) ANY live binding.
#   * When the binding keep-set is UNRESOLVABLE (launcher.db down), flag
#     NOTHING (an empty keep-set must never mean "everything is an orphan").
#   * The consented command RE-VALIDATES against the live binding table + live
#     schema AT RUN TIME (re-probe-before-acting) — never trusts the
#     detect-time snapshot.
#   * On-disk stranded-dir reclaim = STOP-WEAVIATE-FIRST, behind a SECOND flag.
#   * NEVER auto-drop; emit an `orphan_code_collections_detected` deferral.
# ═══════════════════════════════════════════════════════════════════════════


def _normalise_prefix_for_match(s: str) -> str:
    """Python mirror of the Rust `normalise_prefix_for_match`
    (project_identity.rs:791): strip every non-ASCII-alphanumeric char and
    lowercase. Must match the Rust rule so the Python orphan-detector and the
    Rust wizard agree on which prefixes are "the same". "VibeCoded_Orchestrator",
    "vibecodedorchestrator", "VibeCoded Orchestrator" all normalise equal.
    """
    return "".join(c.lower() for c in (s or "") if c.isascii() and c.isalnum())


def _codegraph_keep_set_normalised() -> tuple[set[str], bool]:
    """Return ``(normalised_keep_prefixes, resolvable)`` for the orphan
    detector.

    ``normalised_keep_prefixes`` = every `project_codegraph_bindings`
    collection_prefix + every extra-path owner prefix, run through
    :func:`_normalise_prefix_for_match`. ``resolvable`` is False when
    launcher.db could not be opened at all — the CRITICAL guard: the detector
    MUST refuse to flag anything when it cannot positively confirm the
    keep-set (conservative data-safety).
    """
    try:
        from vco_lib.launcher_db_reader import codegraph_binding_keep_set
        prefixes, resolvable = codegraph_binding_keep_set()
    except Exception:
        return (set(), False)
    normed = {_normalise_prefix_for_match(p) for p in prefixes if p}
    normed.discard("")
    return (normed, resolvable)


def _list_ondisk_weaviate_dirs(volume_dir: str) -> list[tuple[str, int]]:
    """List immediate subdirectories of a Weaviate volume dir with their
    apparent size in bytes. Returns ``[(dirname, size_bytes), ...]``.

    Soft-fail: returns [] if the dir is unreadable / doesn't exist. Sizes are
    ``os.walk`` apparent bytes (== du here; no sparse files on Weaviate LSM
    segments). Used by the on-disk-stranded-segment branch (H3 Population-1,
    the 4.6 GB reclaim invisible to schema-only scans).
    """
    out: list[tuple[str, int]] = []
    try:
        entries = list(os.scandir(volume_dir))
    except Exception:
        return []
    for ent in entries:
        try:
            if not ent.is_dir():
                continue
        except Exception:
            continue
        total = 0
        try:
            for root, _dirs, files in os.walk(ent.path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        continue
        except Exception:
            total = 0
        out.append((ent.name, total))
    return out


def _detect_orphan_code_collections(
    weaviate_url: str,
    *,
    volume_dir: Optional[str] = None,
    keep_set: Optional[set[str]] = None,
    keep_resolvable: Optional[bool] = None,
    schema_fetcher: Optional[Callable[[], list[str]]] = None,
    ondisk_lister: Optional[Callable[[str], list[tuple[str, int]]]] = None,
) -> dict:
    """Two-source orphan CODE-collection detector (READ-ONLY, never drops).

    Sources (H3):
      (a) LIVE-schema code classes whose prefix is in NO current binding.
      (b) ON-DISK stranded segment dirs whose class is GONE from the live
          schema (the biggest reclaim — invisible to schema-only scans).

    Seed = BINDING-EXCLUSION (the authoritative keep-set), case-insensitive,
    exactly like the Rust wizard's `normalise_prefix_for_match` skip — NOT the
    BUG-1 `_is_similar_prefix` heuristic.

    Returns::

        {
          "live_orphans":   [{"class_name", "prefix", "suffix",
                              "object_count"}],
          "ondisk_orphans": [{"dir", "size_bytes"}],
          "keep_resolvable": bool,
          "total_reclaim_bytes": int,
        }

    HARD GUARD: when the keep-set is UNRESOLVABLE (launcher.db down), returns
    EMPTY orphan lists — we never flag anything we can't positively attribute.

    Injection points (``keep_set``, ``schema_fetcher``, ``ondisk_lister``)
    exist for unit tests; production callers pass none.
    """
    result: dict = {
        "live_orphans": [],
        "ondisk_orphans": [],
        "keep_resolvable": True,
        "total_reclaim_bytes": 0,
        # SEV-3 #1: detect-time snapshot of normalised prefixes that have ANY
        # live code class (populated below while Weaviate is UP). Empty when
        # Weaviate is unreachable at detect time.
        "live_prefixes_normalised": [],
    }

    # Resolve the authoritative keep-set (case-insensitive).
    if keep_set is None or keep_resolvable is None:
        _normed, _resolvable = _codegraph_keep_set_normalised()
        keep_set = _normed if keep_set is None else keep_set
        keep_resolvable = _resolvable if keep_resolvable is None else keep_resolvable
    result["keep_resolvable"] = bool(keep_resolvable)

    # DATA-SAFETY: cannot confirm the keep-set → flag NOTHING.
    if not keep_resolvable:
        return result

    # ── (a) live-schema orphans ─────────────────────────────────────────
    if schema_fetcher is not None:
        live_classes = list(schema_fetcher())
    else:
        live_classes = _list_classes(weaviate_url)
    live_class_set_lc = {c.lower() for c in live_classes}

    # SEV-3 #1: capture the DETECT-TIME live-prefix snapshot while Weaviate is
    # UP. Every normalised prefix that has ANY live code class right now is a
    # prefix whose on-disk segment dirs must NOT be fs-reclaimed later (the
    # reclaim runs with Weaviate DOWN and cannot re-fetch). This guards the
    # "active code-graph but momentarily-absent binding row" degenerate the code
    # acknowledges at launcher_db_reader.py — a live class for prefix P proves P
    # is active even if the keep-set (bindings) doesn't list it. Persisted into
    # the deferral so the fs-level reclaim can honour it.
    live_prefixes_normalised: set[str] = set()
    for _cls in live_classes:
        _decomp = _strip_known_suffix(_cls, _CODEGRAPH_SUFFIXES)
        if _decomp is None:
            continue
        _norm = _normalise_prefix_for_match(_decomp[0])
        if _norm:
            live_prefixes_normalised.add(_norm)
    result["live_prefixes_normalised"] = sorted(live_prefixes_normalised)

    for cls in sorted(live_classes):
        decomp = _strip_known_suffix(cls, _CODEGRAPH_SUFFIXES)
        if decomp is None:
            continue
        prefix, sfx = decomp
        norm = _normalise_prefix_for_match(prefix)
        if not norm:
            continue
        # BINDING-EXCLUSION: a prefix in the keep-set (case-insensitively) is
        # ACTIVE — never an orphan. This is the hard guard against dropping a
        # live binding (incl. the 87 GB CodeFunction).
        if norm in keep_set:
            continue
        count = _http_count_objects(cls, weaviate_url)
        result["live_orphans"].append({
            "class_name": cls,
            "prefix": prefix,
            "suffix": sfx,
            # GAP-3: include count==0 classes (a zero-object class still holds
            # a schema slot + dir). None = Weaviate unreachable for the count.
            "object_count": count,
        })

    # ── (b) on-disk stranded segment dirs (H3 GAP-4, the big reclaim) ────
    # A dir whose name has NO case-insensitive match in the live schema is a
    # class-less segment dir. We ONLY consider dirs that LOOK like a code
    # collection (end with a code suffix, normalised) so we never touch
    # Weaviate-internal dirs (`raft`, etc.). Weaviate lowercases class names
    # for its on-disk dir names.
    if volume_dir:
        lister = ondisk_lister or _list_ondisk_weaviate_dirs
        for dirname, size in lister(volume_dir):
            # Skip if a live class matches this dir (case-insensitively) — it
            # is a live collection's segment dir, NOT stranded.
            if dirname.lower() in live_class_set_lc:
                continue
            # Only treat it as a code orphan if the dir name ends with a code
            # suffix (case-insensitive). Otherwise leave it (KG orphan / raft
            # / internal).
            low = dirname.lower()
            if not any(low.endswith(sfx.lower()) for sfx in _CODEGRAPH_SUFFIXES):
                continue
            # Its class is absent from the live schema (checked above) → the
            # keep-set can't protect a class that doesn't exist, but we STILL
            # guard: a dir whose prefix matches a live binding is suspicious
            # (should have a live class) — skip it to be safe.
            _pfx = None
            for sfx in _CODEGRAPH_SUFFIXES:
                if low.endswith(sfx.lower()):
                    _pfx = dirname[: -len(sfx)]
                    break
            _pfx_norm = _normalise_prefix_for_match(_pfx) if _pfx else ""
            if _pfx_norm and _pfx_norm in keep_set:
                continue
            # SEV-3 #1: even without a binding row, a dir whose prefix has ANY
            # LIVE class (a different suffix of the same prefix) is ACTIVE — skip
            # it. This is the detect-time-snapshot guard against reclaiming an
            # active project's segment dir when its binding row is momentarily
            # absent (Weaviate is DOWN at reclaim, so this is the only chance).
            if _pfx_norm and _pfx_norm in live_prefixes_normalised:
                continue
            result["ondisk_orphans"].append({
                "dir": dirname,
                "size_bytes": int(size),
                # Persist the normalised prefix so the fs-reclaim can re-check it
                # against the persisted live-prefix snapshot before rm.
                "prefix_normalised": _pfx_norm,
            })
            result["total_reclaim_bytes"] += int(size)

    return result


def _format_orphan_code_command(
    weaviate_url: str,
    live_orphans: list[dict],
    ondisk_orphans: list[dict],
    volume_dir: Optional[str],
    project_folder: Optional[str] = None,
) -> str:
    """Render the two CONSENTED, RE-VALIDATING cleanup commands for the
    orphan-code deferral. Never renders a drop that isn't re-validated at run
    time against the live binding table + live schema.
    """
    lines = [
        "# ORPHAN CODE-COLLECTION CLEANUP — CONSENTED, re-validated at run time.",
        "# Both commands re-probe the LIVE launcher.db bindings + live Weaviate",
        "# schema BEFORE any drop (re-probe-before-acting) — a case-only variant",
        "# of a live binding is NEVER dropped. Nothing here auto-runs.",
        "",
    ]
    if live_orphans:
        lines.append("# (a) LIVE-schema orphan classes (no current binding):")
        for o in live_orphans:
            cnt = o.get("object_count")
            cnt_txt = f"{cnt} objects" if isinstance(cnt, int) else "count unknown"
            lines.append(f"#     - {o['class_name']}  ({cnt_txt})")
        lines.append(
            "python -m vco_lib.project_init drop-orphan-code-collections "
            f"--weaviate-url {weaviate_url!r} --confirm"
        )
        lines.append("")
    if ondisk_orphans:
        lines.append(
            "# (b) ON-DISK stranded segment dirs (class ALREADY gone from the"
        )
        lines.append(
            "#     live schema — Weaviate's schema DELETE cannot reclaim these)."
        )
        lines.append(
            "#     FILESYSTEM-LEVEL reclaim. INVARIANT: Weaviate MUST be STOPPED"
        )
        lines.append(
            "#     first — deleting a segment dir under a RUNNING Weaviate can"
        )
        lines.append(
            "#     CORRUPT the volume (Weaviate holds the dir in its shard map)."
        )
        lines.append(
            "#     The command REFUSES to run while Weaviate answers /v1/meta;"
        )
        lines.append(
            "#     stop the weaviate container (launcher Services tab, or"
        )
        lines.append(
            "#     `podman stop weaviate_claude` / `docker compose stop weaviate`),"
        )
        lines.append(
            "#     run it, then restart Weaviate. GUARD (with Weaviate DOWN it"
        )
        lines.append(
            "#     CANNOT re-fetch the schema): it removes ONLY dirs whose"
        )
        lines.append(
            "#     normalised prefix is (a) NOT a live launcher.db code-graph"
        )
        lines.append(
            "#     binding AND (b) NOT in the DETECT-TIME live-prefix snapshot"
        )
        lines.append(
            "#     (captured while Weaviate was UP). It REFUSES everything when"
        )
        lines.append(
            "#     the keep-set is unresolvable OR the snapshot is missing."
        )
        for o in ondisk_orphans:
            mb = o["size_bytes"] / (1024 * 1024)
            lines.append(f"#     - {o['dir']}  ({mb:.1f} MB)")
        vd = volume_dir or "<weaviate-volume-dir>"
        recl = (
            "python -m vco_lib.project_init reclaim-stranded-code-segments "
            f"--volume-dir {vd!r} --weaviate-url {weaviate_url!r} "
            "--confirm --i-understand-filesystem-level"
        )
        # SEV-3 #1: pass the project folder so the reclaim can locate the
        # detect-time live-prefix snapshot in `.claude/state/`.
        if project_folder:
            recl += f" --project-folder {str(project_folder)!r}"
        lines.append(recl)
        lines.append("")
    return "\n".join(lines)


# SEV-3 #1: detect-time live-prefix snapshot state file. The fs-level reclaim
# runs with Weaviate DOWN and cannot re-fetch the schema, so the DETECT step
# (Weaviate UP) persists which normalised prefixes had ANY live code class. The
# reclaim reads this and REFUSES to rm a dir whose prefix was live at detect
# time (the "active code-graph but momentarily-absent binding row" degenerate).
_ORPHAN_LIVE_PREFIX_SNAPSHOT_FILENAME = "codegraph-orphan-live-prefixes.json"
_ORPHAN_LIVE_PREFIX_SNAPSHOT_SCHEMA = "vco.codegraph_orphan_live_prefixes.v1"


def _orphan_live_prefix_snapshot_path(folder: Path) -> Path:
    """Path of the detect-time live-prefix snapshot state file."""
    return (
        Path(folder) / ".claude" / "state"
        / _ORPHAN_LIVE_PREFIX_SNAPSHOT_FILENAME
    )


def _write_orphan_live_prefix_snapshot(
    folder: Path, live_prefixes_normalised: list[str],
) -> bool:
    """Persist the detect-time normalised live-prefix set. Best-effort:
    returns False on I/O failure rather than raising."""
    p = _orphan_live_prefix_snapshot_path(folder)
    payload = {
        "schema": _ORPHAN_LIVE_PREFIX_SNAPSHOT_SCHEMA,
        "live_prefixes_normalised": sorted(
            {s for s in (live_prefixes_normalised or []) if s}
        ),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_text
        atomic_write_text(p, json.dumps(payload, indent=2) + "\n")
        return True
    except Exception:
        try:
            p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False


def _read_orphan_live_prefix_snapshot(
    folder: Path,
) -> tuple[set[str], bool]:
    """Read the detect-time live-prefix snapshot.

    Returns ``(normalised_prefixes, present)``. ``present`` is False when the
    file is missing/malformed — the reclaim treats a MISSING snapshot as
    "cannot confirm which prefixes were live → refuse the fs reclaim entirely"
    (conservative data-safety; the file is written whenever a deferral is
    emitted, so its absence at reclaim time is an anomaly worth refusing on)."""
    p = _orphan_live_prefix_snapshot_path(folder)
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return (set(), False)
    try:
        data = json.loads(raw)
    except Exception:
        return (set(), False)
    vals = data.get("live_prefixes_normalised")
    if not isinstance(vals, list):
        return (set(), False)
    return ({str(v) for v in vals if isinstance(v, str) and v}, True)


def _emit_orphan_code_collections_deferral(
    folder: Path,
    weaviate_url: str,
    detection: dict,
) -> bool:
    """Emit `orphan_code_collections_detected` (CONSENTED — never auto-drop).

    Returns True when an entry was written (there were orphans), else False.

    SEV-3 #1: also persists the detect-time live-prefix snapshot (Weaviate is UP
    here) into `.claude/state/` so the later fs-level reclaim (Weaviate DOWN) can
    refuse any dir whose prefix was live at detect time.
    """
    live_orphans = detection.get("live_orphans", []) or []
    ondisk_orphans = detection.get("ondisk_orphans", []) or []
    if not live_orphans and not ondisk_orphans:
        return False

    # SEV-3 #1: persist the detect-time live-prefix snapshot for the fs reclaim.
    # Best-effort — a failed write means the reclaim will see "no snapshot" and
    # conservatively refuse, which is the safe direction.
    if ondisk_orphans:
        try:
            _write_orphan_live_prefix_snapshot(
                folder, detection.get("live_prefixes_normalised", []) or [],
            )
        except Exception:
            pass
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    total_mb = detection.get("total_reclaim_bytes", 0) / (1024 * 1024)
    detected_bits = []
    if live_orphans:
        detected_bits.append(
            f"{len(live_orphans)} live-schema code class(es) with no current "
            f"`project_codegraph_bindings` prefix"
        )
    if ondisk_orphans:
        detected_bits.append(
            f"{len(ondisk_orphans)} on-disk stranded segment dir(s) whose class "
            f"is already gone from the live schema (~{total_mb:.0f} MB reclaimable)"
        )
    detected = (
        "The orphan code-collection detector found "
        + " and ".join(detected_bits)
        + ". Orphan code collections accumulate across sanitizer generations "
        "and are never garbage-collected on their own; dropping them is the "
        "ONLY way to reclaim their on-disk space (idle collections do not "
        "self-compact). This is a CONSENTED cleanup — nothing is auto-dropped."
    )
    cmd = _format_orphan_code_command(
        weaviate_url, live_orphans, ondisk_orphans, detection.get("volume_dir"),
        project_folder=str(folder),
    )
    entry = DeferralEntry(
        condition_id="orphan_code_collections_detected",
        title="Orphan code-graph collections detected (consented cleanup)",
        detected=detected,
        why_deferred=(
            "Dropping a Weaviate class / removing a segment dir is "
            "irreversible, and the on-disk reclaim requires stopping the "
            "Weaviate container (deleting a segment dir under a running "
            "Weaviate can corrupt the volume). VCO never auto-destroys user "
            "data (CLAUDE.md rule 1) — the user runs the guarded commands "
            "explicitly. The DROP command (a) re-probes the live binding table "
            "+ live schema before any drop, so a case-only variant of a live "
            "binding is never dropped. The fs-level RECLAIM runs with Weaviate "
            "down and CANNOT re-fetch the schema, so it instead guards against "
            "BOTH the live binding keep-set AND the detect-time live-prefix "
            "snapshot (captured while Weaviate was up), and refuses everything "
            "when either is unavailable."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)
    return True


# ── FIX-C run-time re-validating drop commands (consented) ─────────────────


def _revalidated_orphan_live_classes(weaviate_url: str) -> list[str]:
    """Re-run the live-schema orphan detection AT RUN TIME and return the
    class names that are STILL orphans (binding-excluded, case-insensitive).

    This is the re-probe-before-acting guard: a project could have been
    re-added between detect-time and run-time, recreating a binding — so the
    consented drop command re-derives the orphan set from the CURRENT launcher
    bindings + CURRENT live schema, never the stale snapshot. Returns [] when
    the keep-set is unresolvable (refuse to drop anything).
    """
    keep_set, resolvable = _codegraph_keep_set_normalised()
    if not resolvable:
        return []
    live = _list_classes(weaviate_url)
    out: list[str] = []
    for cls in sorted(live):
        decomp = _strip_known_suffix(cls, _CODEGRAPH_SUFFIXES)
        if decomp is None:
            continue
        prefix, _sfx = decomp
        norm = _normalise_prefix_for_match(prefix)
        if not norm or norm in keep_set:
            continue
        out.append(cls)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# v0.2.73 FIX-C-RECUR — prefix-drift forward-guard.
#
# Root cause (H3 #1): VCO's name→prefix sanitizer changed across releases →
# each generation mints a new code-class set, the old one is left behind. FIX-C
# cleans EXISTING orphans; FIX-C-RECUR stops FUTURE ones by recording the
# "last-seen collection_prefix generation" per project and detecting a change.
#
# Recording channel (Python-only, no cargo): a small JSON state file under the
# project's `.claude/state/codegraph-prefix-generation.json`. This is the
# natural Python-writable home (the Rust bindings-column alternative would need
# a migration + Rust reader/writer — out of scope for this Python track). When
# the current binding prefix differs from the recorded generation, emit a
# `codegraph_prefix_drift_detected` deferral (consent — never auto-migrate).
# ═══════════════════════════════════════════════════════════════════════════

_CODEGRAPH_PREFIX_GEN_FILENAME = "codegraph-prefix-generation.json"
_CODEGRAPH_PREFIX_GEN_SCHEMA = "vco.codegraph_prefix_generation.v1"


def _codegraph_prefix_gen_path(folder: Path) -> Path:
    """Return the per-project code-prefix generation state file path
    (`<folder>/.claude/state/codegraph-prefix-generation.json`)."""
    return Path(folder) / ".claude" / "state" / _CODEGRAPH_PREFIX_GEN_FILENAME


def _read_codegraph_prefix_generation(folder: Path) -> Optional[str]:
    """Read the recorded last-seen code-graph prefix for this project, or None.

    Soft-fail: returns None on any error (missing file / malformed JSON).
    """
    p = _codegraph_prefix_gen_path(folder)
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    val = data.get("collection_prefix")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _write_codegraph_prefix_generation(folder: Path, prefix: str) -> bool:
    """Atomic-write the last-seen code-graph prefix generation. Best-effort:
    returns False on any I/O failure rather than raising (recording is an aid,
    not a hard requirement)."""
    p = _codegraph_prefix_gen_path(folder)
    payload = {
        "schema": _CODEGRAPH_PREFIX_GEN_SCHEMA,
        "collection_prefix": prefix,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_text
        atomic_write_text(p, json.dumps(payload, indent=2) + "\n")
        return True
    except Exception:
        try:
            p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False


def detect_codegraph_prefix_drift(
    folder: Path,
    project_name: str,
    *,
    emit_deferral: bool = True,
    weaviate_url: Optional[str] = None,
) -> Optional[dict]:
    """Detect a code-graph prefix-generation drift for a project (FIX-C-RECUR).

    Compares the CURRENT canonical code prefix (from the sanitizer) against the
    last-seen generation recorded in `.claude/state/`. On a DIFFERENCE:
      * emit a `codegraph_prefix_drift_detected` deferral (consent — names the
        old + new prefix + the consented migrate/cleanup command),
      * DO NOT auto-migrate.
    On NO change (or first-ever run): record the current prefix silently and
    return None.

    Returns a dict ``{"old_prefix", "new_prefix"}`` when drift was detected,
    else None. Best-effort throughout (never raises into the caller).
    """
    try:
        current = derive_project_code_prefix(project_name)
    except Exception:
        return None
    if not current:
        return None

    recorded = _read_codegraph_prefix_generation(folder)

    if recorded is None:
        # First-ever observation — record the baseline, no drift.
        _write_codegraph_prefix_generation(folder, current)
        return None

    if _normalise_prefix_for_match(recorded) == _normalise_prefix_for_match(current):
        # Same generation (case/separator drift only counts as same via the
        # normalised compare — the collections are the SAME logical set).
        # Refresh the exact-cased record and return.
        if recorded != current:
            _write_codegraph_prefix_generation(folder, current)
        return None

    # DRIFT: the sanitizer produced a genuinely different prefix generation.
    drift = {"old_prefix": recorded, "new_prefix": current}
    if emit_deferral:
        try:
            _emit_codegraph_prefix_drift_deferral(
                folder, project_name, recorded, current,
                weaviate_url=weaviate_url,
            )
        except Exception:
            pass
    # Record the NEW generation so the deferral fires ONCE per drift, not every
    # run (the user consents + migrates; the next run sees no further drift).
    _write_codegraph_prefix_generation(folder, current)
    return drift


def _emit_codegraph_prefix_drift_deferral(
    folder: Path,
    project_name: str,
    old_prefix: str,
    new_prefix: str,
    *,
    weaviate_url: Optional[str] = None,
) -> None:
    """Emit `codegraph_prefix_drift_detected` (consent — never auto-migrate)."""
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    wv = weaviate_url or _weaviate_url_default()
    old_classes = ", ".join(f"{old_prefix}{s}" for s in _CODEGRAPH_SUFFIXES)
    detected = (
        f"The code-graph collection prefix for project {project_name!r} "
        f"changed from {old_prefix!r} to {new_prefix!r} (a sanitizer-"
        f"generation change). The previous generation's code classes "
        f"({old_classes}) are now ORPHANED — the analyzer writes only to the "
        f"NEW prefix, so the old set accumulates dead on-disk segments unless "
        f"migrated or dropped."
    )
    cmd = (
        "# The old code-graph classes are orphaned by the prefix change.\n"
        "# Reclaim them (CONSENTED) — the detector re-validates against the\n"
        "# live binding table before any drop:\n"
        "python -m vco_lib.project_init detect-orphan-code-collections "
        f"--weaviate-url {wv!r} --project-folder {str(folder)!r}\n"
        "# then run the drop command it prints in "
        "`orphan_code_collections_detected`.\n"
        "# The new-prefix code graph is (re)built by re-running the analyzer:\n"
        f".claude/scripts/code-graph-analyze . --project {project_name!r}"
    )
    entry = DeferralEntry(
        condition_id="codegraph_prefix_drift_detected",
        title="Code-graph collection prefix drifted across a sanitizer generation",
        detected=detected,
        why_deferred=(
            "Migrating/dropping the previous-generation code classes is "
            "irreversible; VCO never auto-migrates (consent — CLAUDE.md rule "
            "1). The forward-guard records the current prefix so a future "
            "sanitizer change is detected once rather than silently orphaning "
            "a collection generation."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


# ---------------------------------------------------------------------------
# v0.2.63 "Safe add" — per-add opt-in (default OFF) that protects a project's
# sensitive, often-committed project-root `.env` and keeps VCO-created files
# out of the user's VCS, delegating any disruptive reconciliation to the
# project's own Claude via UPDATE_DEFERRED.md ("any deferral should end in the
# .md deferred document"). Default behaviour (safe_add=False) is unchanged.
# The `.env` skip + sidecar are Rust-owned (launcher); Python records the
# structured deferral + writes `.git/info/exclude`, so the deferral-MD format
# stays single-owned by Python (the launcher never hand-parses it).
# ---------------------------------------------------------------------------

# Suffix appended to a live file path to form its safe-add reference sidecar
# (e.g. `.env` -> `.env.vco.reference`). The Rust launcher uses the same string
# when it writes the `.env` reference; kept here so the git-exclude pattern
# below matches it too.
_SAFE_ADD_SIDECAR_SUFFIX = ".vco.reference"

# v0.2.63 (C1 fix — "check if files are VCO's or user's"): the safe-add
# `.git/info/exclude` entries are computed PER-ADD from the files VCO actually
# created (`_safe_add_exclude_entries`), NOT a blanket dir-glob. A blanket
# `/.vscode/` or `/infrastructure/` or `/knowledge/` would silently hide a
# user's OWN same-named dir (those names are common in existing projects —
# safe-add's whole purpose). So we exclude only VCO-created paths.
#
# Top-level entries that are UNAMBIGUOUSLY VCO's (a user can't own them) →
# collapse to a single dir/file glob instead of listing every created file
# under them. Everything else is excluded as its SPECIFIC created path.
_SAFE_ADD_VCO_EXCLUSIVE_TOPLEVEL = {
    ".claude": "/.claude/",
    ".vco-manifest.json": "/.vco-manifest.json",
}


def _safe_add_exclude_entries(result: dict, folder: Path) -> list:
    """Compute the `.git/info/exclude` entries for ONLY the paths VCO actually
    created in THIS add — collision-safe (never a blanket dir-glob that could
    hide a user's own `.vscode/` / `infrastructure/` / `knowledge/` files).

    VCO-exclusive top-level namespaces (`.claude/`, `.vco-manifest.json`)
    collapse to one glob; every other VCO-created path is excluded SPECIFICALLY
    (e.g. `/infrastructure/docker-compose.yml`, not `/infrastructure/`). Plus the
    Rust-written `.env.vco.reference` sidecar (not in the Python create list).
    """
    actions = result.get("actions", {}) or {}
    created: list = []
    for key in ("create", "overwrite", "always-overwrite"):
        created.extend(actions.get(key, []) or [])

    entries: list = []
    seen = set()

    def _add(entry: str) -> None:
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)

    for rel in created:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel:
            continue
        top = rel.split("/", 1)[0]
        glob = _SAFE_ADD_VCO_EXCLUSIVE_TOPLEVEL.get(top)
        if glob is not None:
            _add(glob)
        else:
            # A specific VCO-created path (a root file like CLAUDE.md, or a file
            # inside a possibly-user-owned dir like .vscode/ or infrastructure/).
            # Anchored so it matches ONLY this exact path the user did not author.
            _add("/" + rel)

    # The manifest is VCO's even if `actions` didn't enumerate it.
    if result.get("manifest_written"):
        _add("/.vco-manifest.json")
    # The Rust launcher wrote the `.env` reference sidecar before this step.
    sidecar = ".env" + _SAFE_ADD_SIDECAR_SUFFIX
    if (folder / sidecar).exists():
        _add("/" + sidecar)
    return entries


def _append_git_info_exclude(
    folder: Path, paths: tuple,
) -> dict:
    """Idempotently append ``paths`` to ``<folder>/.git/info/exclude``.

    `.git/info/exclude` is the LOCAL-only ignore file: it is never committed
    (unlike the tracked `.gitignore`), so adding VCO-created paths there keeps
    them out of the user's commits without modifying any tracked file.

    Soft-fail + idempotent:
      - No `.git` directory (not a git repo, or a bare/worktree layout where
        `.git` is a file) -> action="not_a_git_repo", no-op.
      - Entry already present (exact-line match) -> not re-added.
      - Write failure -> action="write_failed:<ErrorClass>", no raise.

    Returns ``{"action": str, "added": [str, ...], "path": str}``. Actions:
      - "appended"        — one or more new lines written.
      - "noop"            — every path already present.
      - "not_a_git_repo"  — no `.git` directory.
      - "write_failed:*"  — append raised OSError.

    v0.2.63 (safe-add): keeps VCO files out of the user's Bitbucket/Git repo.
    """
    git_dir = folder / ".git"
    result: dict = {"action": "not_a_git_repo", "added": [], "path": ""}
    # Only the standard (non-bare, non-submodule-file) layout is handled. When
    # `.git` is a file (worktree/submodule pointer) we conservatively skip —
    # resolving the real gitdir is out of scope and the user can exclude
    # manually.
    if not git_dir.is_dir():
        return result

    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"
    result["path"] = str(exclude_path)

    try:
        existing = (
            exclude_path.read_text(encoding="utf-8")
            if exclude_path.exists()
            else ""
        )
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    # Exact-line membership check (strip trailing whitespace per line).
    present = {line.strip() for line in existing.splitlines()}
    to_add = [p for p in paths if p not in present]
    if not to_add:
        result["action"] = "noop"
        return result

    block_lines = [
        "",
        "# VCO safe-add (v0.2.63): keep orchestrator-created files out of "
        "your commits.",
        "# This is .git/info/exclude (LOCAL-only) — not the tracked "
        ".gitignore.",
    ]
    block_lines.extend(to_add)
    block = "\n".join(block_lines) + "\n"

    # Ensure we don't glue onto a non-newline-terminated last line.
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"

    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        _write_file_atomic(exclude_path, (prefix + block).encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "appended"
    result["added"] = to_add
    return result


def _emit_safe_add_skipped_env_merge_deferral(
    folder: Path, *, sidecar_rel: str,
) -> None:
    """Emit `safe_add_skipped_env_merge`: under Safe add, VCO did NOT
    append/rewrite the project-root `.env` (it may be committed to the user's
    VCS). The VCO-intended keys were written to ``sidecar_rel``
    (`.env.vco.reference`) by the Rust launcher.

    This is the CORE safe-add deferral. The Rust launcher writes the sidecar
    and skips the `.env` append + the b12 KG_COLLECTION rewrite; this function
    records the structured deferral row so the project's Claude knows the
    project-root `.env` was left untouched. Written from Python so the
    structured deferral format has a single owner (the launcher never
    hand-parses UPDATE_DEFERRED.md).

    Accuracy note (v0.2.64): the skip is NOT a "KG routing is broken until you
    hand-merge" condition. VCO has three env channels and only the third is
    skipped under safe-add:
      * ``.claude/settings.json`` env block — read by MCP subprocesses
        (KG_COLLECTION / DEVELOPMENT_COLLECTION / PROJECT_NAME / …). Written
        UNCONDITIONALLY by `apply_project_env_via_python` (it runs BEFORE the
        safe-add branch). So per-project KG routing already uses VCO's
        launcher-resolved values.
      * ``.claude/env`` — VCO-owned, gitignored shell channel. Also written
        UNCONDITIONALLY. CLI shell users get full VCO env with
        ``source .claude/env``.
      * project-root ``.env`` — the ONLY file safe-add skips, because it may
        be committed. Skipping it only costs the ``source ./.env`` convenience
        for users who specifically relied on that committed file.
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    live_rel = ".env"
    vco_env_rel = ".claude/env"
    # NOTE: `detected` / `why_deferred` MUST stay single-line — the
    # UPDATE_DEFERRED.md round-trip (`DeferralReport.read`) parses these as
    # one `**Field**: value` line and truncates any embedded newline. Only
    # `command_to_apply` survives multi-line (it's a fenced code block).
    detected_msg = (
        f"Safe add is ON, so VCO did NOT append its canonical keys to (nor "
        f"rewrite a KG_COLLECTION line in) the existing project-root "
        f"`{live_rel}` — that file is often committed to your VCS. VCO's "
        f"intended `.env` content was written to `{sidecar_rel}` instead. "
        f"This does NOT break per-project KG routing: VCO env is already "
        f"active through its own two channels, both written unconditionally. "
        f"MCP subprocesses (KG_COLLECTION / DEVELOPMENT_COLLECTION / "
        f"PROJECT_NAME / ACTIVE_EMBEDDING) read `.claude/settings.json`, which "
        f"carries VCO's launcher-resolved values; CLI shell users get the full "
        f"VCO env with `source {vco_env_rel}` (the VCO-owned, gitignored shell "
        f"channel) — NOT `source ./{live_rel}`. The skipped project-root "
        f"`{live_rel}` merge ONLY affects the `source ./{live_rel}` "
        f"convenience for users who relied on that specific committed file; "
        f"nothing about KG routing is degraded."
    )
    cmd = (
        f"# (a) RECOMMENDED — load the full VCO env for a CLI shell session\n"
        f"#     (this channel is always written, never skipped by safe-add):\n"
        f"source {str(folder / vco_env_rel)!r}\n"
        f"#     (bash/zsh; `source` is a POSIX builtin. On native-Windows\n"
        f"#     PowerShell/cmd there is nothing to source — the MCP env channel\n"
        f"#     `.claude/settings.json` is already active and carries the same\n"
        f"#     KG routing, so no shell step is needed there.)\n"
        f"#\n"
        f"# (b) OR, if you specifically want the keys in your committed .env,\n"
        f"#     review what VCO would have added, then copy by hand:\n"
        f"diff {str(folder / live_rel)!r} {str(folder / sidecar_rel)!r}\n"
        f"#\n"
        f"# (c) OR re-add the project WITHOUT Safe add to let VCO merge the\n"
        f"#     keys into the project-root .env automatically.\n"
        f"#\n"
        f"# Then dismiss this deferral:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id safe_add_skipped_env_merge"
    )

    entry = DeferralEntry(
        condition_id="safe_add_skipped_env_merge",
        title="VCO left your project-root .env untouched (safe-add) — reference sidecar written",
        detected=detected_msg,
        why_deferred=(
            "Safe add deliberately protects the sensitive, often-committed "
            "project-root `.env`. Appending VCO keys (or rewriting a stale "
            "KG_COLLECTION line) would mutate a file the user tracks and "
            "commits, risking a leak of VCO config into their VCS or a "
            "clobbered customisation. Nothing is broken by the skip: MCP "
            "routing reads `.claude/settings.json` and the CLI shell channel "
            "`.claude/env` are both written unconditionally. This row is "
            "informational so the project's own agent knows the project-root "
            "`.env` was intentionally left alone and can reconcile the sidecar "
            "if the user wants those keys in the committed file."
        ),
        command_to_apply=cmd,
        severity="info",
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _has_user_secret_shaped_line(path: Path) -> bool:
    """v0.2.83 PLAN-v0283 B-F8: does ``path`` carry a secret-shaped
    managed-block line? Extracted (unchanged semantics) from the closure that
    used to live inside ``_emit_user_secret_values_retained_deferral`` so the
    reconciler can RE-DETECT the same state (single home, one concern).

    - ``.claude/env``: a ``export KEY="..."`` line inside the managed block
      whose KEY ``is_secret_shaped_env_key`` flags AND whose quoted value is
      non-empty (v0.2.84 PLAN-v0284 D6 / P4).
    - ``.claude/settings.json``: an ``env`` key that ``is_secret_shaped_env_key``
      flags (the SINGLE secret-shape home — never a substring fork).

    No value is ever read/printed — only a pattern/shape + emptiness match.
    Soft-fails to ``False`` on any read/parse error.

    v0.2.84 PLAN-v0284 D6 (P4): the ``.claude/env`` branch used to match a
    COARSE regex (``export\\s+[A-Z_][A-Z0-9_]*="``) that flagged EVERY uppercase
    export in the managed block — so every safe-add project (23/23 pure-config
    exports, zero secrets) re-emitted the ``user_secret_values_retained_in_tree``
    deferral forever and the reconciler's re-detect could never self-clear it.
    Both surfaces now route through the SINGLE secret-shape home
    (``vco_lib.secrets_audit.is_secret_shaped_env_key``); the ``.claude/env``
    branch additionally requires a non-empty quoted value (an empty
    ``export FOO_TOKEN=""`` carries no VALUE to worry about).
    """
    if not path.is_file():
        return False
    from vco_lib.secrets_audit import is_secret_shaped_env_key
    # A managed-block export line: `export KEY="value"` (the canonical shape the
    # config-projection writer emits). Captures KEY and the double-quoted value
    # so we can shape-check the key and test the value for non-emptiness. We do
    # NOT retain / log the value — only ``bool(value)`` participates.
    _managed_export_re = re.compile(
        r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"',
        re.MULTILINE,
    )
    try:
        text = path.read_text(encoding="utf-8")
        if path.name == "env":
            from vco_lib.config_projection import (
                CLAUDE_ENV_MANAGED_BEGIN,
                CLAUDE_ENV_MANAGED_END,
            )
            begin = text.find(CLAUDE_ENV_MANAGED_BEGIN)
            end = text.find(CLAUDE_ENV_MANAGED_END)
            if begin == -1 or end == -1:
                return False
            managed_block = text[begin:end]
            # D6 (P4): flag ONLY when a secret-SHAPED key carries a non-empty
            # value. Pure-config routing keys (KG_COLLECTION, WEAVIATE_URL, ...)
            # are never secret-shaped, so a config-only managed block reads
            # clean and the deferral can self-clear.
            for m in _managed_export_re.finditer(managed_block):
                key, value = m.group(1), m.group(2)
                if is_secret_shaped_env_key(key) and value != "":
                    return True
            return False
        elif path.name == "settings.json":
            # settings.json branch UNCHANGED (v0.2.84 D6): keys already
            # route through the single secret-shape home. No value check here
            # (the env-key MAP has no ``export KEY="..."`` quoting to inspect).
            try:
                data = json.loads(text)
                env_block = data.get("env", {})
                if not isinstance(env_block, dict):
                    return False
                for key in env_block:
                    if is_secret_shaped_env_key(key):
                        return True
            except Exception:  # noqa: BLE001 — malformed settings.json → soft no-detection
                return False
        return False
    except Exception:
        return False


def _scan_user_secret_values_retained(folder: Path) -> bool:
    """v0.2.83 PLAN-v0283 B-F8: True when a secret-shaped managed-block line
    survives in EITHER VCO env surface (``.claude/env`` or
    ``.claude/settings.json``). Shared by the emitter and the reconciler so the
    self-clear decision uses the SAME detection the emit uses (no drift)."""
    folder = Path(folder)
    return (
        _has_user_secret_shaped_line(folder / ".claude" / "env")
        or _has_user_secret_shaped_line(folder / ".claude" / "settings.json")
    )


def _emit_user_secret_values_retained_deferral(folder: Path) -> None:
    """Emit `user_secret_values_retained_in_tree`: pre-v0.2.73 user-secret
    VALUEs may still reside in committable tree files (.claude/env or
    .claude/settings.json) from the Rust GUI writer before S-4's strip
    invariant shipped.

    ONE-TIME scanner that detects a secret-shaped line in the managed block
    (no value printed — only a pattern match). Self-clearing: once the next
    env-projection refresh scrubs the value, the deferral is never re-emitted.
    FOREIGN from install.py's perspective (v0.2.73 S-8): emitted ONLY on the
    bundle-update path here, NEVER by install.py --update (which doesn't
    re-detect it). It is therefore deliberately NOT in
    ``install.py::_INSTALL_OWNED_CONDITION_IDS`` — install.py preserves it
    verbatim (per ``deferral_report.condition_is_owned``: non-owned == FOREIGN
    == preserved). If it were OWNED, an ``install.py --update`` run would seed
    the report, fail to re-detect this bundle-update-only condition, and
    silently DROP the secret-retention notice while the value may still be in
    the tree (the exact A-2 clobber class this whole track fixes). It clears
    the next time THIS bundle-update path runs and finds the value gone.

    Severity is "warning" (not critical) because:
      * The value IS still a secret until the next refresh (users should rotate
        if the key was leaked to VCS).
      * The next refresh will scrub it automatically.
      * No immediate action required — just awareness + precaution.
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    # Cheap scan (no value parsing — never print a value) across BOTH surfaces.
    # v0.2.83 B-F8: routed through the shared module-level detector so the
    # reconciler's self-clear uses the SAME logic.
    if not _scan_user_secret_values_retained(folder):
        return  # No pre-fix artifacts; don't emit.

    # The rotate advice is CONDITIONAL IN PROSE ("if this project's git has a
    # push remote…") rather than gated on a probe: the user knows their own
    # VCS topology, and a git subprocess per bundle-update would be a wasted
    # call (the advice reads correctly whether or not a remote exists). We also
    # deliberately do NOT try to discover the user's real repo — it may be
    # nested anywhere in the tree (see E-two-git-contexts) — so a single
    # root-level `git config` probe would be unreliable anyway.
    rotate_advice = (
        "If this project's git has a push remote and the pre-v0.2.73 value "
        "was committed or pushed, rotate the affected key now to be safe."
    )

    entry = DeferralEntry(
        condition_id="user_secret_values_retained_in_tree",
        title="Pre-v0.2.73: user-secret VALUES may be in committable tree files",
        detected=(
            "Secret-shaped managed-block lines were found in one or more "
            "VCO env surfaces (.claude/env or .claude/settings.json). These "
            "may contain VALUES from the Rust GUI writer before v0.2.73's "
            "strip-only invariant shipped."
        ),
        why_deferred=(
            "Deferred: the value scrubbing happens automatically at the next "
            "env-projection refresh (which runs on the next project refresh/"
            "CLI command / launcher restart). This deferral is a ONE-TIME "
            "notice only; once the refresh scrubs the value from tree files, "
            "the deferral will NOT re-emit."
        ),
        command_to_apply=(
            f"# The next project env refresh will scrub the values automatically.\n"
            f"# No manual action required UNLESS the key was committed/pushed:\n"
            f"# {rotate_advice}\n"
            f"# Dismiss this deferral once you've reviewed it:\n"
            f"python -m vco_lib.project_init dismiss-deferral "
            f"--folder {str(folder)!r} "
            f"--condition-id user_secret_values_retained_in_tree"
        ),
        severity="warning",
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def _emit_safe_add_git_exclude_deferral(
    folder: Path, git_result: dict,
) -> None:
    """Emit `safe_add_git_exclude_updated`: record that VCO appended its
    created paths to `.git/info/exclude` (local-only) under Safe add.

    Informational — the project's Claude should know VCO files are locally
    git-ignored so it doesn't get confused about why `git status` is clean for
    `.claude/` etc. The tracked `.gitignore` is intentionally untouched.
    """
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    added = git_result.get("added", [])
    added_str = ", ".join(added) if added else "(none)"
    exclude_path = git_result.get("path", ".git/info/exclude")

    detected_msg = (
        f"Safe add is ON. VCO appended {len(added)} path pattern(s) to the "
        f"LOCAL-only `{exclude_path}` so orchestrator-created files stay out "
        f"of your commits: {added_str}. The tracked `.gitignore` was NOT "
        f"modified."
    )

    cmd = (
        f"# Review the local-only excludes VCO added:\n"
        f"cat {str(exclude_path)!r}\n"
        f"# To stop excluding a path, delete its line from that file.\n"
        f"# Then dismiss this deferral:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id safe_add_git_exclude_updated"
    )

    entry = DeferralEntry(
        condition_id="safe_add_git_exclude_updated",
        title="VCO files excluded from your commits via .git/info/exclude (safe-add)",
        detected=detected_msg,
        why_deferred=(
            "Safe add keeps VCO config out of the user's VCS by writing to the "
            "local-only `.git/info/exclude` rather than the tracked "
            "`.gitignore`. Recorded as a deferral so the project's agent knows "
            "these paths are intentionally git-ignored locally and can revisit "
            "the choice if the user wants VCO files tracked."
        ),
        command_to_apply=cmd,
        severity="info",
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)


def install_project_bundle(
    folder: Path,
    orchestrator_root: Optional[Path] = None,
    *,
    update_mode: bool = False,
    force: bool = False,
    dry_run: bool = False,
    write_env: bool = False,
    project_name: Optional[str] = None,
    log_event: Optional[Callable[..., None]] = None,
    safe_add: bool = False,
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
        write_env: A2 (2026-05-28) — when True, write `.claude/env` +
            `.claude/settings.json env` using launcher.db when available,
            falling back to a default bundle derived from `--orchestrator-root`
            + project-name (folder basename or `project_name`).  Makes
            ``install-bundle`` standalone-usable without a running launcher.
        project_name: raw project name for env derivation (overrides folder
            basename when ``write_env`` is True and launcher.db is absent).
        log_event: optional forensic logger.
        safe_add: v0.2.63 — per-add opt-in (default OFF → no behaviour change).
            When True (and NOT dry_run / update_mode): record a
            ``safe_add_skipped_env_merge`` deferral when the Rust launcher left
            a ``.env.vco.reference`` sidecar (it skipped the sensitive,
            often-committed project-root ``.env``), and append VCO-created paths
            to the project's LOCAL-only ``.git/info/exclude`` (never the tracked
            ``.gitignore``) so VCO files stay out of the user's commits, with a
            ``safe_add_git_exclude_updated`` deferral. The ``.claude/settings.json``
            and ``.vscode/settings.json`` merges are UNCHANGED under safe-add
            (those files are rarely committed).

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
            "adopt": [<rel>...],                # v0.2.84 D7: shipped-file adoption
            "skip-existing": [<rel>...],
            "skip-disabled": [<rel>...],        # Wave 2 D, 2026-05-22
            "keep-regenerated": [<rel>...],     # v0.2.57: regen'd data, no warn
            "orphan-deleted": [<rel>...],       # v0.2.24 §A0
            "orphan-preserved": [<rel>...],     # v0.2.24 §A0
        },
        # v0.2.84 D7: relative path of the per-run adoption-backup dir (present
        # only when >=1 file was adopted this run). Additive — install.py
        # call-sites are untouched.
        "adopt_backup_dir": <rel> | absent,
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
                         "noop", "preserve", "adopt", "skip-existing",
                         "skip-disabled", "keep-regenerated",
                         "orphan-deleted", "orphan-preserved",
                         "orphan-retired", "knowledge-retired")},
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

    # v0.2.81: one authority for "is this bundle target the orchestrator
    # root?" — drives both the curated-knowledge gate (via
    # `_enumerate_bundle_files` → `_enumerate_knowledge_ops`) and the
    # knowledge-retirement branch in the orphan loop below.
    is_root_target = _is_root_bundle_target(orchestrator_root, folder)

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
        # v0.2.81: `knowledge-retired` = prior curated knowledge/ entries
        # that went root-only. Non-root: left on disk (never deleted, never
        # deferred), only pruned from the manifest. See the orphan loop.
        "actions": {k: [] for k in
                    ("create", "overwrite", "always-overwrite",
                     "noop", "preserve", "adopt", "skip-existing",
                     "skip-disabled", "keep-regenerated",
                     "orphan-deleted", "orphan-preserved",
                     "orphan-retired", "knowledge-retired")},
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

    # NEW-7 / B1 (v0.2.53) — bundle-update resume sentinel.
    #
    # Mid-update Cmd-C currently leaves the manifest stale + files
    # partially overwritten. Drop a sentinel here BEFORE any FS mutation
    # so a subsequent session-start can detect the interruption and warn
    # the user to re-run `install-bundle --update` (which is idempotent
    # — partial overwrites are corrected on the next pass via the
    # manifest hash-compare). Cleared after the manifest write
    # succeeds.
    #
    # Only fires in update_mode (first-install can be safely re-run
    # without a sentinel — the heal paths cover it) and only when not
    # dry_run (dry-run never mutates the FS so there's nothing to
    # resume). Best-effort: a failed sentinel write logs but does NOT
    # block the install.
    # v0.2.70 (Bug B): every `_write_file_atomic` call reachable from this
    # function that gets REDIRECTED to a `.vco-new` sibling (because the target
    # or an ancestor is a symlink VCO refused to write through) appends its
    # (original_target, vco_new) pair here. After the loop + settings merge +
    # project-level templates + the resume sentinel we emit ONE consolidated
    # `symlink_preserved_under_install_path` deferral (skipped on dry-run).
    # Mirrors the accumulate-then-emit-once pattern used by `user_modified_paths`
    # / `skipped_existing_paths` / `orphan_preserved`. Declared HERE (above the
    # sentinel write) because the sentinel is the FIRST `.claude/`-writing site.
    symlink_redirect_events: list[tuple[Path, Path]] = []

    _sentinel_written = False
    if update_mode and not dry_run:
        _sentinel_written = write_bundle_update_resume_sentinel(
            folder,
            operation="install-bundle-update",
            orchestrator_root=orchestrator_root,
            vco_version=result.get("vco_version", "unknown"),
            redirect_sink=symlink_redirect_events,
        )
        if _sentinel_written:
            _log("4.bundle.sentinel", "start",
                 "wrote bundle-update resume sentinel",
                 data={"path": str(_bundle_sentinel_path(folder))})

    manifest = _read_manifest(folder)
    new_files: dict[str, dict] = {}
    # Schema v2: preserved_files records every file VCO chose not to
    # overwrite during this run (`preserve` in update mode + `skip-existing`
    # in first-install mode). Rebuilt from scratch each run so converged
    # files (no longer diverged) automatically fall off.
    new_preserved: dict[str, dict] = {}
    user_modified_paths: list[str] = []
    skipped_existing_paths: list[str] = []
    # v0.2.84 PLAN-v0284 D7 (P5/R2): shipped-file adoption. `adopted_paths`
    # collects the dest_rels adopted this run (shipped bytes written after a
    # timestamped backup of the current bytes). ONE `<UTC-basic-ts>` dir per run
    # under `.claude/backups/bundle-adoptions/`, computed lazily on the first
    # adoption so a run with zero adoptions creates no backup dir.
    adopted_paths: list[tuple[str, str]] = []  # (dest_rel, backup_rel)
    _adopt_backup_ts: Optional[str] = None

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

        # Honour --force: in update mode, treat preserve/adopt as a plain
        # overwrite. v0.2.84 D7 (P5): `adopt` already writes shipped bytes, but
        # under --force we short-circuit to `overwrite` so force keeps its exact
        # historical meaning — take the shipped version with NO adoption backup
        # (the user explicitly asked to discard local edits; a backup would be
        # noise). This also keeps the `--force` self-clear path unchanged (force
        # runs never populate `user_modified_paths` → reconciler clears the
        # stale `bundle_user_modified_preserved` entry).
        if force and update_mode and action in ("preserve", "adopt"):
            action = "overwrite"

        # Compute the new shipped hash regardless of action — needed for
        # manifest update on every file we recognize.
        shipped_hash = _bytes_sha256(source_bytes)

        # Record into manifest only when we actually deposited the
        # shipped content (or when we previously did and it's still
        # what's on disk — noop / always-overwrite cases).
        record_in_manifest = False

        if action == "adopt":
            # v0.2.84 PLAN-v0284 D7 (P5/R2): shipped-file adoption. Back up the
            # CURRENT on-disk bytes to `.claude/backups/bundle-adoptions/<ts>/`
            # (never destroy bytes without a captured copy), THEN write the
            # shipped bytes and record the manifest entry (as `overwrite` would).
            # Backup-write failure ⇒ NO adoption: fall back to today's
            # `preserve` + `bundle_user_modified_preserved` deferral so the
            # divergent file is still surfaced. Dry-run never mutates the FS.
            if dry_run:
                # Planning preview: report the would-be adoption, no backup, no
                # write, no manifest claim (mirrors dry-run for overwrite/create,
                # which also skip the FS write and manifest record below).
                pass
            else:
                try:
                    current_bytes = target_path.read_bytes()
                    if _adopt_backup_ts is None:
                        _adopt_backup_ts = _adopt_backup_timestamp()
                    backup_rel = _backup_bytes_for_adoption(
                        folder, op.dest_rel, _adopt_backup_ts, current_bytes,
                    )
                except Exception as e:
                    # Backup failed → do NOT adopt. Fall back to preserve.
                    err = f"{type(e).__name__}: {e}"
                    _log("4.bundle.adopt", "warn",
                         f"{op.dest_rel}: adoption backup failed ({err}) — "
                         "falling back to preserve + deferral",
                         data={"path": op.dest_rel, "error": err})
                    result["warnings"].append(
                        f"adoption backup failed for {op.dest_rel} ({err}); "
                        "preserved local file + deferral emitted"
                    )
                    action = "preserve"
                else:
                    # Backup captured — write the shipped bytes.
                    try:
                        mode: Optional[int] = None
                        if op.dest_rel.endswith((".sh",)) or "/scripts/" in op.dest_rel.replace("\\", "/"):
                            mode = 0o700
                        _redirect = _write_file_atomic(target_path, source_bytes, mode=mode)
                        if _redirect is not None:
                            symlink_redirect_events.append((target_path, _redirect))
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        _log("4.bundle.adopt", "error",
                             f"{op.dest_rel}: shipped write failed after backup: {err}",
                             data={"path": op.dest_rel, "error": err})
                        result["errors"].append({"path": op.dest_rel, "error": err})
                        # Bytes were backed up but the shipped write failed —
                        # do NOT claim the manifest entry, do NOT record an
                        # adoption. The on-disk file is unchanged (atomic write
                        # never partially replaced it). Skip to the next op.
                        continue
                    adopted_paths.append((op.dest_rel, backup_rel))
                    record_in_manifest = True

            # Fall-through: when `action` was flipped to "preserve" above (backup
            # failure), do the preserve bookkeeping now (single home for that
            # logic mirrors the `if action == "preserve"` block below).
            if action == "preserve":
                user_modified_paths.append(op.dest_rel)
                existing = manifest.get("files", {}).get(op.dest_rel)
                if existing is not None:
                    new_files[op.dest_rel] = existing
                new_preserved[op.dest_rel] = {
                    "shipped_sha256": shipped_hash,
                    "preserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "shipped_source": op.source_rel,
                    "reason": "preserve",
                }

        elif action == "preserve":
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

        elif action == "keep-regenerated":
            # v0.2.57: regenerated per-project data file (e.g.
            # `.node_formats.json`). Its divergence from the shipped seed
            # is EXPECTED (the project regenerated its own cache), NOT a
            # user customization. So unlike "preserve":
            #   * do NOT append to user_modified_paths (no
            #     bundle_user_modified_preserved warning),
            #   * do NOT record a `reason="preserve"` entry (it isn't a
            #     user edit VCO must remember to offer --force for),
            #   * keep the local file untouched on disk.
            # We DO keep the manifest's prior `files` entry if present so
            # later updates still recognize the shipped baseline, and we
            # record a distinct preserved-entry reason for auditability
            # (kept-regenerated) WITHOUT it surfacing in the user-modified
            # deferral list. Schema-bump regeneration is gated separately
            # by the artifact_schema_versions DB registry. (The kept path is
            # recorded in result["actions"]["keep-regenerated"] below; no
            # separate accumulator is needed.)
            existing = manifest.get("files", {}).get(op.dest_rel)
            if existing is not None:
                new_files[op.dest_rel] = existing
            new_preserved[op.dest_rel] = {
                "shipped_sha256": shipped_hash,
                "preserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "shipped_source": op.source_rel,
                "reason": "keep-regenerated",
            }

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
                    # v0.2.70 (Bug B): capture a `.vco-new` redirect (symlink-
                    # blocking) so the consolidated symlink deferral lists it.
                    _redirect = _write_file_atomic(target_path, source_bytes, mode=mode)
                    if _redirect is not None:
                        symlink_redirect_events.append((target_path, _redirect))
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
    orphan_retired: list[str] = []  # v0.2.83 B-F5: kept-on-disk, manifest-retired
    knowledge_retired: list[str] = []
    prior_files: dict = manifest.get("files", {}) or {}
    new_files_keys = set(new_files.keys())
    # Compute orphans BEFORE we also include user-modified preserves —
    # the preserved files write their prior entry into new_files (line
    # ~4456), so subtracting new_files_keys from prior_files gives the
    # paths the new run truly did NOT see.
    seen_in_ops = {op.dest_rel for op in ops}
    for prior_rel, prior_entry in prior_files.items():
        if prior_rel in seen_in_ops:
            # Re-shipped this run; not an orphan. (The allowlisted per-
            # project knowledge files — TAG_HIERARCHY.md etc. — are in ops
            # for every target, so they're excluded from retirement here.)
            continue
        # v0.2.81 knowledge-retirement branch (data-safety, constraint 3):
        # on a NON-root project the ~115 curated `knowledge/**` entries are
        # no longer shipped, so they'd otherwise fall into the orphan
        # machinery below — case (b) would DELETE them from disk and case
        # (c) would emit a 113-file deferral. Both are forbidden. Instead:
        # leave the file on disk UNTOUCHED (now user-owned state, the
        # logical completion of V52-C), and simply DON'T carry the manifest
        # entry forward (it falls out of `new_files` → pruned on rewrite).
        # No disk op, no deferral. Root targets skip this branch entirely so
        # a curated node genuinely deleted from templates/knowledge/ still
        # orphan-processes normally at the root.
        # Normalize the separator BEFORE the prefix test: manifest keys are
        # raw `dest_rel` = `str(Path("knowledge") / rel)`, host-OS-shaped
        # (`knowledge\concepts\foo.md` on Windows). Without this, a Windows
        # non-root project's first post-v0.2.81 update would MISS this branch
        # → fall into the orphan machinery → MASS-DELETE the curated copies +
        # emit deferrals + spawn a re-embed (all three data-safety violations
        # decision 5 forbids). Mirrors `_classify_bundle_op_kind` (which
        # `.replace("\\", "/")` before its own prefix check) and the Rust
        # `is_kg_or_docs_rel_path` normalizer — cross-OS parity.
        if not is_root_target and prior_rel.replace("\\", "/").startswith("knowledge/"):
            knowledge_retired.append(prior_rel)
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
            # Case (c) — user-modified, upstream-deleted.
            # v0.2.83 PLAN-v0283 B-F5: AUTO-KEEP + RETIRE. The file is NEVER
            # deleted (it's the user's now — the logical completion of "VCO no
            # longer manages this file"). Instead of emitting a deferral +
            # keeping the manifest entry forever, we RETIRE the manifest entry
            # (drop it from `new_files` → pruned on rewrite) and record an
            # auto-resolution. `orphan_preserved` is left empty for this file,
            # so no `bundle_user_modified_deletion_preserved` deferral is
            # emitted AND `still_orphan_preserved` stays False → the reconciler
            # clears any pre-existing stale entry. Mirrors the v0.2.81
            # knowledge-retirement branch (keep on disk, drop manifest entry).
            orphan_retired.append(prior_rel)
            # (intentionally NOT: orphan_preserved.append / new_files[...] = ...)

    result["actions"]["orphan-deleted"] = orphan_deleted
    result["actions"]["orphan-preserved"] = orphan_preserved
    result["actions"]["orphan-retired"] = orphan_retired
    result["actions"]["knowledge-retired"] = knowledge_retired
    # v0.2.83 B-F5: one honest auto-resolution record per retired orphan (file
    # kept on disk, manifest entry dropped). Best-effort — never abort install.
    if orphan_retired and not dry_run:
        try:
            from vco_lib import deferral_emit as _de
            for _rel in orphan_retired:
                _de.record_auto_resolution(
                    folder,
                    "bundle_user_modified_deletion_preserved",
                    "retired_orphan_manifest_entry",
                    f"kept user-modified upstream-deleted file `{_rel}` on disk "
                    "and retired its .vco-manifest.json entry (no longer "
                    "VCO-managed)",
                    log=_log_auto,
                )
        except Exception as _exc:  # noqa: BLE001 — bookkeeping is best-effort
            _log("4.bundle.orphan_retired", "warn",
                 f"orphan-retire auto-resolution record failed: {_exc}")
    if orphan_retired:
        _log("4.bundle.orphan_retired", "info",
             f"retired {len(orphan_retired)} user-modified orphan manifest "
             f"entries (files left on disk, no longer VCO-managed)",
             data={"count": len(orphan_retired)})
    if knowledge_retired:
        _log("4.bundle.knowledge_retired", "info",
             f"retired {len(knowledge_retired)} curated knowledge/ manifest "
             f"entries (files left on disk, read via shared collection)",
             data={"count": len(knowledge_retired)})

    # Smart-merge settings.json template separately. The template carries
    # the orchestrator's hooks block + permissions defaults. The merge
    # logic mirrors install.py:_merge_settings_template + _smart_merge_settings.
    settings_template = _settings_template_path(orchestrator_root)
    if settings_template.exists():
        try:
            settings_target = folder / ".claude" / "settings.json"
            settings_action, settings_redirect = _merge_settings_template_for_bundle(
                settings_template, settings_target,
                dry_run=dry_run,
            )
            result["settings_action"] = settings_action
            # v0.2.70 (Bug B / W-F1): when `.claude` itself is a symlink VCO
            # refused to write through, the settings.json write redirected to a
            # `.vco-new` sibling. Thread that into the SAME accumulator as the
            # main file loop so the consolidated symlink deferral lists
            # settings.json too (otherwise the symlinked-`.claude` case
            # under-reports — it would list agent redirects but not settings).
            if settings_redirect is not None:
                symlink_redirect_events.append((settings_target, settings_redirect))
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
            cleanup_result = _cleanup_legacy_bash_env_in_project(
                folder, redirect_sink=symlink_redirect_events,
            )
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

    # Phase 0.B Part 2 (2026-05-25): project the FULL canonical env via
    # the `vco_lib.config_projection.apply_project_env` contract. This
    # replaces the historical two-step backfill (PR-7's PROJECT_NAME +
    # CODE_GRAPH_PROJECT + v0.2.28's KG_COLLECTION + SHARED + DEV)
    # with a single DB-sourced projection that always reflects the
    # launcher.db's current bindings. User-set values for CANONICAL
    # keys are no longer preserved (they're overwritten with the DB
    # value); non-canonical user-added env keys ARE preserved by the
    # contract's deep-merge.
    #
    # Soft-fail discipline matches the legacy backfills: launcher.db
    # missing (rare here — `register_project` ran already), no row for
    # this folder (race with create flow), or any apply error becomes
    # a warning, not a fatal install-bundle failure.
    if not dry_run:
        try:
            # v0.2.37 (Gap 6a): thread orchestrator_root through to the
            # env-projection step so `.claude/env` carries
            # VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR / VCT_INSTALL_ROOT
            # exports. Without these, KG-sync / code-graph wrappers in a
            # fresh OSS install (no launcher running) fail with
            # "claude_mcp_servers/ not found" because they need an
            # absolute pointer to the orchestrator clone. The launcher
            # was the only previous emitter of VCT_INSTALL_ROOT — direct
            # CLI invocations had no way to reach the analyzer venv.
            backfill = _apply_canonical_env_via_config_projection(
                folder, orchestrator_root=orchestrator_root,
            )
            # A2 (2026-05-28): when the DB-sourced projection was skipped
            # (launcher.db absent / project not registered) and the caller
            # explicitly requested env emission via --write-env, fall back
            # to the standalone bundle derived from orchestrator_root +
            # project-name.  This makes `install-bundle` fully functional
            # for OSS-developer / fork-integrator workflows that don't run
            # through the launcher.
            if write_env and backfill["action"] in ("db_unreachable", "not_registered"):
                standalone = _apply_standalone_env(
                    folder,
                    orchestrator_root=orchestrator_root,
                    project_name=project_name,
                )
                _log("4.bundle.standalone_env", "ok",
                     f"standalone_env_apply: {standalone['action']}",
                     data=standalone)
                result["standalone_env"] = standalone
                if standalone["action"] == "applied":
                    # Surface standalone write in the top-level result so
                    # the CLI human-readable output picks it up.
                    backfill = standalone
            result["backfill_code_graph_project"] = backfill
            result["backfill_kg_collection"] = backfill  # same data
            _log("4.bundle.backfill", "ok",
                 f"canonical_env_apply: {backfill['action']}",
                 data=backfill)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.backfill", "error",
                 f"canonical env apply failed: {err}",
                 data={"error": err})
            result["warnings"].append(
                f"canonical_env_apply failed: {err}"
            )

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
    # the project tree).
    #
    # v0.2.83 PLAN-v0283 B-F4: these 4 keys are provably inert, so we now
    # AUTO-PRUNE them (default ON, no env gate) rather than merely nudging.
    # `_autoprune_legacy_vscode_mcp_env_keys` deletes exactly the 4 keys via a
    # JSON parse/edit/atomic-write and records an auto-resolution. When it
    # can't run safely (unparseable JSONC / unexpected shape / write error) it
    # returns False and we FALL BACK to the historical deferral. After a
    # successful prune the keys are gone, so `legacy_vscode_mcp_detected`
    # stays False → the reconciler clears any pre-existing stale entry.
    legacy_vscode_mcp_detected = False
    if not dry_run:
        try:
            vscode_mcp_detect = _detect_legacy_vscode_mcp_env_keys(folder)
            result["legacy_vscode_mcp_env"] = vscode_mcp_detect
            if vscode_mcp_detect["action"] == "detected":
                if _autoprune_legacy_vscode_mcp_env_keys(folder, vscode_mcp_detect):
                    # Auto-pruned — no deferral, entry (if any) self-clears.
                    result["legacy_vscode_mcp_autopruned"] = True
                    _log("4.bundle.legacy_vscode_mcp", "ok",
                         f"legacy_vscode_mcp_env: auto-pruned "
                         f"{len(vscode_mcp_detect['keys'])} inert key(s)",
                         data=vscode_mcp_detect)
                else:
                    # Fallback: could not safely prune → emit the deferral.
                    legacy_vscode_mcp_detected = True
                    _emit_legacy_vscode_mcp_env_deferral(folder, vscode_mcp_detect)
                    _log("4.bundle.legacy_vscode_mcp", "ok",
                         f"legacy_vscode_mcp_env: detected "
                         f"{len(vscode_mcp_detect['keys'])} key(s) "
                         "(auto-prune unavailable — deferral emitted)",
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
    # Project name derived from folder basename — kept simple per
    # coord ("no fancy templating engine"). Callers that want a
    # different display name can edit CLAUDE.md after install.
    # Assigned OUTSIDE the try: the legacy-KG / legacy-codegraph
    # detection further down also reads it, and a templates-install
    # failure swallowed by the except below must not leave it unbound
    # (NameError in the failure path).
    derived_project_name = folder.name or "Project"
    try:
        templates_result = _install_project_level_templates(
            folder,
            orchestrator_root=orchestrator_root,
            project_name=derived_project_name,
            dry_run=dry_run,
        )
        # v0.2.70 (Bug B / B-1): fold any `.claude/`-redirected template writes
        # (CONTEXT_STATE.md, *.reference.md sidecars) into the SAME consolidated
        # symlink deferral as the main file loop. Pop the key BEFORE assigning
        # to result["templates"] — the tuples carry Path objects which are not
        # JSON-serialisable (the result dict is serialised by the Rust caller).
        symlink_redirect_events.extend(
            templates_result.pop("symlink_redirects", []) or []
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

            # NEW-7 / B1 (v0.2.53) — manifest write succeeded → clear
            # the resume sentinel. From the caller's perspective the
            # bundle update is now atomically committed: the FS reflects
            # the new shipped versions AND the manifest agrees on the
            # hashes. Any pre-existing sentinel (incl. one we just
            # wrote ourselves) is now safe to remove.
            if _sentinel_written:
                if clear_bundle_update_resume_sentinel(folder):
                    _log("4.bundle.sentinel", "end",
                         "cleared bundle-update resume sentinel",
                         data={"path": str(_bundle_sentinel_path(folder))})
                else:
                    # Sentinel survives — best-effort. The next
                    # session-start detection sees a stale sentinel +
                    # a fresh manifest and can treat the install as
                    # complete. This branch shouldn't trip in practice.
                    result["warnings"].append(
                        "bundle-update sentinel not cleared after successful install"
                    )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.manifest", "error",
                 f"manifest write failed: {err}",
                 data={"error": err})
            result["errors"].append({"path": str(_MANIFEST_REL), "error": err})

    # v0.2.47 RL-7.5 chunker-preset deferral (per-project flow).
    # When the prior `.vco-manifest.json` recorded a vco_version strictly
    # less than v0.2.46 AND the current orchestrator is >= v0.2.46, append
    # a `chunker_preset_overhaul_pending` deferral entry. Per-project flow
    # is independent of the launcher-driven flow (which writes the same
    # deferral to the orchestrator-root project only — see
    # `launcher/src-tauri/src/commands/chunker_revision_deferral.rs`).
    # Soft-fail: a write error logs but doesn't abort the install.
    if not dry_run:
        prev_version = (manifest or {}).get("vco_version") or ""
        running_version = result.get("vco_version") or ""
        if prev_version and running_version and _crosses_chunker_boundary(
            prev_version, running_version,
        ):
            try:
                _emit_chunker_resync_deferral(folder, prev_version, running_version)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"chunker-resync deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"chunker-resync deferral write failed: {err}"
                )

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
        # v0.2.70 (Bug B): one consolidated symlink-redirect deferral covering
        # every `_write_file_atomic` redirect (main file loop + settings merge)
        # that landed at a `.vco-new` sibling because the target / an ancestor
        # is a symlink VCO refused to write through. Soft-fail: a deferral write
        # failure must not abort the bundle install.
        if symlink_redirect_events:
            try:
                _emit_symlink_redirect_deferral(
                    folder, symlink_redirect_events, orchestrator_root,
                )
                # v0.2.70 (Bug A2 SHOULD-FIX): the redirect itself only logged
                # to stderr, which never reaches the launcher GUI (the Rust
                # reads `warnings[]` off stdout). Surface a one-line summary so
                # the user learns their bundle content landed at `.vco-new`
                # siblings (because `.claude`/an ancestor is a symlink) instead
                # of silently discovering it via UPDATE_DEFERRED.md. Mirrors the
                # `bundle_user_modified_preserved` summary→warning pattern.
                # Classified Info on both toast paths (no error/failed marker).
                result["warnings"].append(
                    f"{len(symlink_redirect_events)} file(s) redirected to "
                    f".vco-new because the target is a symlink — see "
                    f"UPDATE_DEFERRED.md (symlink_preserved_under_install_path)"
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.deferral", "error",
                     f"symlink-redirect deferral write failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"symlink-redirect deferral write failed: {err}"
                )

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

        # v0.2.84 PLAN-v0284 D7 (P5/R2): shipped-file adoption NOTICE. When one
        # or more divergent bundle files were adopted this run, surface a
        # ONE-TIME visible notice — NOT an eternal deferral. Three surfaces:
        #   (i)   a stdout NOTICE block listing adopted paths + the backup dir,
        #   (ii)  one `record_auto_resolution` JSONL row + loud log PER FILE
        #         (v0.2.83 B-F9 shape), keyed to `bundle_user_modified_preserved`
        #         so the audit trail ties to the retired deferral condition,
        #   (iii) additive `result["adopt_backup_dir"]` (the `actions.adopt`
        #         list is populated by the per-op tail append; both are
        #         additive-only — install.py call-sites are untouched).
        # Backups are kept forever (small, user-prunable). The reconciler's
        # `still_user_modified=bool(user_modified_paths)` naturally clears any
        # STALE `bundle_user_modified_preserved` entry because adopted files
        # never land in `user_modified_paths` (only the backup-failure fallback
        # does, and that path re-emits the deferral for exactly those files).
        if adopted_paths and _adopt_backup_ts is not None:
            from vco_lib.paths import to_posix_rel as _to_posix_rel
            adopt_backup_dir_rel = _to_posix_rel(
                _ADOPT_BACKUPS_REL / _adopt_backup_ts
            )
            result["adopt_backup_dir"] = adopt_backup_dir_rel
            # (i) stdout NOTICE block. Printed to stdout (not stderr) so the
            # CLI human-readable path and the launcher's stdout reader both see
            # it. Kept compact + bounded via `_format_file_list_md`.
            notice_paths = _format_file_list_md(
                sorted(rel for rel, _ in adopted_paths)
            )
            print(
                "\n[vct] NOTICE — shipped-file adoption (v0.2.84):\n"
                f"  {len(adopted_paths)} bundle file(s) at VCO-shipped "
                "destinations diverged from the shipped version and were "
                "ADOPTED (refreshed to the current shipped bytes).\n"
                "  Your previous bytes were backed up (kept forever; prune "
                f"when you no longer need them) under:\n    {adopt_backup_dir_rel}\n"
                f"{notice_paths}\n",
                flush=True,
            )
            result["warnings"].append(
                f"{len(adopted_paths)} VCO-shipped file(s) adopted (refreshed); "
                f"prior bytes backed up under {adopt_backup_dir_rel}"
            )
            # (ii) one auto-resolution record per adopted file.
            try:
                from vco_lib import deferral_emit as _de_adopt
                for rel, backup_rel in adopted_paths:
                    _de_adopt.record_auto_resolution(
                        folder,
                        "bundle_user_modified_preserved",
                        "adopted_shipped_file",
                        f"{rel} (backup: {backup_rel})",
                        log=_log_auto,
                    )
            except Exception as e:  # noqa: BLE001 — the JSONL trail is best-effort
                _log("4.bundle.adopt", "warn",
                     f"adoption auto-resolution record failed: "
                     f"{type(e).__name__}: {e}")

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

        # v0.2.83 PLAN-v0283 B-F6: auto-drop EMPTY legacy code-graph candidates
        # (re-probe-before-drop + post-drop probe; case_only NEVER dropped; KG
        # family UNTOUCHED — only this codegraph list is passed). The deferral
        # is emitted ONLY for the NON-droppable remainder, and the reconciler's
        # self-clear flag uses the remainder too (so an all-empty run clears any
        # stale codegraph deferral).
        codegraph_dropped: list[str] = []
        legacy_codegraph_remaining = legacy_codegraph_candidates
        if legacy_codegraph_candidates:
            try:
                codegraph_dropped, legacy_codegraph_remaining = (
                    _autodrop_empty_codegraph_candidates(
                        folder, legacy_codegraph_candidates, weaviate_url,
                    )
                )
                if codegraph_dropped:
                    result["codegraph_autodropped"] = codegraph_dropped
                    _log("4.bundle.legacy-codegraph", "ok",
                         f"auto-dropped {len(codegraph_dropped)} empty legacy "
                         f"code-graph class(es)",
                         data={"dropped": codegraph_dropped})
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _log("4.bundle.legacy-codegraph", "error",
                     f"legacy-codegraph auto-drop failed: {err}",
                     data={"error": err})
                result["warnings"].append(
                    f"legacy-codegraph auto-drop failed: {err}"
                )
                # Conservative on error: defer the whole set.
                legacy_codegraph_remaining = legacy_codegraph_candidates

        if legacy_codegraph_remaining:
            try:
                _emit_legacy_codegraph_deferral(
                    folder,
                    project_name=derived_project_name,
                    weaviate_url=weaviate_url,
                    candidates=legacy_codegraph_remaining,
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
        # v0.2.83 B-F6: report the NON-dropped remainder (what actually deferred).
        result["legacy_codegraph_candidates"] = legacy_codegraph_remaining

        # v0.2.73 FIX-C-RECUR wiring (F1): run the code-graph prefix-drift
        # forward-guard ONCE per bundle install/update, right beside the legacy
        # code-graph detection it complements. `detect_codegraph_prefix_drift`
        # was inert before this — detector + deferral emitter + generation-record
        # all existed but NOTHING invoked it, so a sanitizer-generation prefix
        # change would silently orphan a whole code-class generation without ever
        # surfacing a deferral. It records the current prefix on first run (no
        # drift), and emits `codegraph_prefix_drift_detected` (consent — never
        # auto-migrate) when the current prefix differs from the recorded
        # generation. Soft-fail (best-effort — never blocks the bundle install).
        try:
            drift = detect_codegraph_prefix_drift(
                folder, derived_project_name,
                emit_deferral=True, weaviate_url=weaviate_url,
            )
            result["codegraph_prefix_drift"] = drift
            _log("4.bundle.codegraph-prefix-drift", "ok",
                 ("drift detected" if drift else "no drift (baseline recorded)"),
                 data={"drift": drift})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.codegraph-prefix-drift", "error",
                 f"codegraph prefix-drift detection failed: {err}",
                 data={"error": err})
            result["warnings"].append(
                f"codegraph prefix-drift detection failed: {err}"
            )

        # Reconcile + trim: drop entries this install resolved.
        try:
            _reconcile_bundle_deferrals(
                folder,
                still_user_modified=bool(user_modified_paths) and not force,
                still_skipped_existing=bool(skipped_existing_paths),
                still_template_review_pending=bool(template_review_diverged),
                still_legacy_kg=bool(legacy_kg_candidates),
                # v0.2.83 B-F6: use the NON-dropped remainder — an all-empty run
                # (everything auto-dropped) leaves no remainder → the stale
                # codegraph deferral self-clears.
                still_legacy_codegraph=bool(legacy_codegraph_remaining),
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

    # v0.2.63 Safe add: protect the sensitive project-root `.env` + keep VCO
    # files out of the user's commits. Runs only on a real first-install add
    # with safe_add ON (not dry-run, not update). The `.env` skip + the
    # `.env.vco.reference` sidecar are Rust-owned (launcher); here we (a) record
    # the env-merge deferral when that sidecar is present, and (b) append the
    # VCO-created paths to the LOCAL-only `.git/info/exclude`. Both soft-fail.
    if safe_add and not dry_run and not update_mode:
        try:
            env_sidecar_rel = ".env" + _SAFE_ADD_SIDECAR_SUFFIX
            if (folder / env_sidecar_rel).exists():
                _emit_safe_add_skipped_env_merge_deferral(
                    folder, sidecar_rel=env_sidecar_rel,
                )
                _log("4.bundle.safe_add_env", "ok",
                     "safe_add_skipped_env_merge deferral emitted",
                     data={"sidecar": env_sidecar_rel})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.safe_add_env", "error",
                 f"safe-add env deferral failed: {err}", data={"error": err})
            result["warnings"].append(f"safe-add env deferral failed: {err}")

        try:
            git_result = _append_git_info_exclude(
                folder, tuple(_safe_add_exclude_entries(result, folder)),
            )
            result["safe_add_git_exclude"] = git_result
            _log("4.bundle.safe_add_git_exclude", "ok",
                 f"safe_add_git_exclude: {git_result['action']}",
                 data=git_result)
            if git_result.get("action") == "appended":
                _emit_safe_add_git_exclude_deferral(folder, git_result)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.safe_add_git_exclude", "error",
                 f"safe-add git-exclude failed: {err}", data={"error": err})
            result["warnings"].append(f"safe-add git-exclude failed: {err}")
    elif safe_add and not dry_run and update_mode:
        # A-12 (v0.2.73): safe_add is an ADD-TIME-only concept (the `.env`
        # protection + sidecar deferral). A caller that passes
        # safe_add=True together with update_mode=True previously got NEITHER
        # the sidecar deferral NOR the git-exclude NOR any signal — a silent
        # no-op. "Conservative defaults" requires the do-nothing to be
        # logged so the caller isn't misled into thinking safe-add ran.
        _log("4.bundle.safe_add", "warn",
             "safe_add ignored in update_mode (safe-add is an add-time-only "
             "concept; .env protection + sidecar deferral apply only on a "
             "fresh add). The unconditional .git/info/exclude coverage still "
             "runs below (G1).",
             data={"safe_add": True, "update_mode": True})

    # G1 (v0.2.73, secrets spec item #9; widened v0.2.75 P2): unconditional
    # `.git/info/exclude` coverage on EVERY add AND update — not just the
    # safe-add first-install branch above and not just updates. Pre-v0.2.75
    # the gap was the FRESH NON-SAFE ADD: `if update_mode:` skipped it, so a
    # project added with "Safe add" OFF got no exclude entries until its
    # first bundle update. Keeps VCO-created paths out of the user's commits
    # for every add flavour and for projects added before safe-add existed.
    # Uses the same collision-safe append helper (LOCAL-only
    # `.git/info/exclude`, never the tracked `.gitignore`; worktree/bare
    # `.git`-file conservatively skipped inside the helper; idempotent
    # exact-line dedup so the update re-run after a fresh add is a noop).
    # Soft-fails. Skipped when the safe-add branch above already ran the
    # append this call (safe fresh add), and on dry runs (must not mutate).
    if not dry_run and not (safe_add and not update_mode):
        try:
            git_result = _append_git_info_exclude(
                folder, tuple(_safe_add_exclude_entries(result, folder)),
            )
            result["g1_git_exclude"] = git_result
            _log("4.bundle.g1_git_exclude", "ok",
                 f"g1_git_exclude: {git_result['action']}",
                 data=git_result)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.g1_git_exclude", "warning",
                 f"G1 git-exclude failed: {err}", data={"error": err})
            result["warnings"].append(f"G1 git-exclude failed: {err}")

    # v0.2.73 S-8 (ONE-TIME): scan for pre-fix user-secret VALUES in tree files
    # and emit a deferral notice if found. Triggered on every bundle-update run
    # (cheap pattern scan, no value parsing); self-clears once the next env
    # refresh removes the value.
    if update_mode:
        try:
            _emit_user_secret_values_retained_deferral(folder)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _log("4.bundle.secrets_retention_audit", "warning",
                 f"pre-fix secret-value scan failed: {err}", data={"error": err})
            result["warnings"].append(f"pre-fix secret-value scan failed: {err}")

    return result


# Wrappers that ship the resilient `$VCT_INSTALL_ROOT` interpreter-discovery
# ladder and therefore SHOULD be stale-checked. MUST match the Rust
# `wrapper_requires_resilience_marker` set (code-graph-analyze, kg-sync +
# `.ps1` siblings). `kg-duplicates` has no ladder and is excluded there too.
_RESILIENT_WRAPPER_BASENAMES: tuple[str, ...] = (
    "code-graph-analyze",
    "code-graph-analyze.ps1",
    "kg-sync",
    "kg-sync.ps1",
)

# The marker string that MUST appear in a healthy (RT-4+) wrapper. Mirrors the
# Rust `analyzer_wrapper_is_resilient` marker. We detect a string the templates
# already ship rather than adding a fresh sentinel, so the check stays true no
# matter how the templates are re-worded, as long as they honour the ladder.
_RESILIENT_WRAPPER_MARKER = "VCT_INSTALL_ROOT"


def _codegraph_wrapper_still_stale(folder: Path) -> bool:
    """v0.2.77 L4-1 probe: does the project STILL carry a stale codegraph
    analyzer / kg-sync wrapper (exists but lacks the resilient
    ``$VCT_INSTALL_ROOT`` ladder)?

    Mirrors the Rust ``analyzer_wrapper_is_resilient`` health-check so the
    ``stale_codegraph_wrapper_pending`` deferral (emitted Rust-side when the
    launcher falls back to the orchestrator copy) self-clears once the user
    refreshes the wrapper (Option A ``cp`` / Option B ``--force``).

    Returns ``True`` if ANY resilient-ladder wrapper under
    ``.claude/scripts`` exists AND does not contain the marker (conservative:
    an unreadable file is treated as stale — same as the Rust default). Returns
    ``False`` when no such wrapper is stale (nothing left to defer).
    """
    scripts_dir = folder / ".claude" / "scripts"
    for basename in _RESILIENT_WRAPPER_BASENAMES:
        wrapper = scripts_dir / basename
        if not wrapper.is_file():
            continue
        try:
            contents = wrapper.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable → conservatively treat as stale (still-applicable),
            # mirroring the Rust conservative default.
            return True
        if _RESILIENT_WRAPPER_MARKER not in contents:
            return True
    return False


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
    still_stale_wrapper: Optional[bool] = None,
    still_user_secret_retained: Optional[bool] = None,
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

    v0.2.83 PLAN-v0283:
      * B-F8: `user_secret_values_retained_in_tree` now self-clears here. When
        the caller passes ``still_user_secret_retained=None`` (default) AND the
        entry is present on disk, we RE-DETECT via
        ``_scan_user_secret_values_retained`` (the SAME scan the emitter uses);
        once the next env-projection refresh scrubbed the value, the entry
        clears. An explicit bool overrides the probe (mirrors the stale-wrapper
        contract). Note: this condition is FOREIGN to install.py's owned set
        (S-8) — it is only ever cleared HERE, on the bundle-update path.
      * B-F9: every clear records an ``auto-resolutions.jsonl`` line + loud log
        via ``record_auto_resolution`` (no silent mutations).
      * Write now routes through the ONE locked emitter home
        (``deferral_emit.locked_report``) so a concurrent detached writer can't
        drop entries mid-reconcile.
    """
    from vco_lib.deferral_report import DeferralReport
    from vco_lib import deferral_emit as _de

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
        # next install clears the stale deferral entry. v0.2.83 B-F5 makes
        # this the self-clear for the "retire the manifest entry" path too.
        "bundle_user_modified_deletion_preserved": still_orphan_preserved,
        # v0.2.24 RL-defect Fix 2 (2026-05-22): legacy .vscode MCP_*
        # detection is recomputed every install — when the user removes
        # the inert keys (via the deferral's command OR the v0.2.83 B-F4
        # auto-prune) the next install sees `action=none` and clears it.
        "legacy_vscode_mcp_env_keys_present": still_legacy_vscode_mcp,
    }

    report = DeferralReport.read(folder)
    initial_ids = {e.condition_id for e in report.entries}

    # v0.2.77 L4-1: the codegraph-analyzer/kg-sync stale-wrapper deferral
    # (emitted Rust-side when the launcher falls back to the orchestrator copy)
    # had no self-clear. Recompute here — probed from disk (only when the entry
    # is actually present, to avoid needless reads) when the caller passes None
    # (the default), so once the user refreshes the wrapper (Option A cp /
    # Option B --force) the next bundle update sees a healthy wrapper and clears
    # the entry.
    if "stale_codegraph_wrapper_pending" in initial_ids:
        bundle_conditions["stale_codegraph_wrapper_pending"] = (
            _codegraph_wrapper_still_stale(folder)
            if still_stale_wrapper is None
            else still_stale_wrapper
        )

    # v0.2.83 B-F8: user_secret_values_retained_in_tree self-clear. Only probe
    # when the entry is actually present (avoid needless FS reads). None ⇒
    # re-detect via the shared scan; explicit bool overrides.
    if "user_secret_values_retained_in_tree" in initial_ids:
        bundle_conditions["user_secret_values_retained_in_tree"] = (
            _scan_user_secret_values_retained(folder)
            if still_user_secret_retained is None
            else still_user_secret_retained
        )

    if not initial_ids & set(bundle_conditions):
        # Nothing on-disk we own → no reconciliation to do.
        return

    # Which owned conditions are on-disk AND no longer applicable this run.
    to_resolve = [
        cid for cid, still_applicable in bundle_conditions.items()
        if not still_applicable and cid in initial_ids
    ]
    if not to_resolve:
        return

    # Route the mutate+write through the ONE locked emitter home so a
    # concurrent detached writer can't clobber the trimmed report. mark_resolved
    # inside the lock re-reads from disk (locked_report reads at enter), then the
    # context-exit write unlinks the file if the entry list is now empty.
    resolved_ids: list[str] = []
    with _de.locked_report(folder) as locked:
        for cid in to_resolve:
            if locked.has_condition(cid):
                locked.mark_resolved(cid)
                resolved_ids.append(cid)

    # B-F9: one honest auto-resolution record per cleared condition.
    for cid in resolved_ids:
        _de.record_auto_resolution(
            folder,
            cid,
            "reconciled_stale_bundle_deferral",
            "condition no longer applies on this bundle update — cleared",
            log=_log_auto,
        )


def _canonical_path_eq(
    a: "str | Path",
    b: "str | Path",
    *,
    is_windows: Optional[bool] = None,
) -> bool:
    """Canonical filesystem-path equality (symlink- + case-safe).

    Resolves BOTH operands via ``Path.resolve()`` (collapses ``..``,
    follows symlinks) and compares. On Windows the comparison is
    case-insensitive (NTFS is case-preserving but case-insensitive), which
    mirrors the lowercase-string compare the four in-function ``_path_eq``
    closures used before this extraction (the single home for that logic,
    per the one-concern-one-home rule).

    Conservative failure mode: any ``OSError`` / ``RuntimeError`` /
    ``ValueError`` (e.g. embedded-NUL path) / ``TypeError`` while resolving
    either operand → ``False``. Callers that use this as a "is this the
    root?" gate therefore fail toward *less* action (a target is treated as
    NON-root, so curated knowledge is NOT shipped) rather than toward
    writing into the wrong place.

    ``is_windows`` is injectable so the Windows case-fold branch can be
    unit-tested on any host (defaults to the real platform when None).
    """
    if is_windows is None:
        import platform as _platform
        is_windows = _platform.system().lower().startswith("win")
    try:
        ap = Path(a).resolve()
        bp = Path(b).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        # ValueError: embedded NUL byte / bad path; TypeError: non-path
        # input. Any malformed operand → conservative False (the four
        # migrated DB-row call-sites never hit these; the root-gate call-
        # site benefits from the wider guard).
        return False
    if is_windows:
        return str(ap).lower() == str(bp).lower()
    return ap == bp


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
) -> tuple[str, Optional[Path]]:
    """Mirror of install.py:_merge_settings_template + _smart_merge_settings.

    Inlined here (rather than importing from install.py) so vco_lib stays
    import-free of install.py — install.py imports vco_lib, not the other
    way around.

    Returns ``(status, redirect_target)``:
      * ``status`` — one of ``would-create`` / ``created`` / ``would-merge`` /
        ``merged`` / ``unchanged`` / ``unchanged (user file unparseable)``.
      * ``redirect_target`` — v0.2.70 (Bug B / W-F1): the ``.vco-new`` Path
        when the settings.json write was redirected because ``.claude`` (or
        the file itself) is a symlink VCO refused to write through, else
        ``None``. The caller (``install_project_bundle``) threads this into
        the SAME ``symlink_redirect_events`` accumulator as the main file
        loop so the consolidated symlink deferral also lists settings.json
        (the symlinked-``.claude`` case would otherwise under-report).
    """
    template_data = json.loads(template_path.read_text(encoding="utf-8"))

    if not target_path.exists():
        if dry_run:
            return "would-create", None
        target_path.parent.mkdir(parents=True, exist_ok=True)
        redirect = _write_file_atomic(
            target_path,
            (json.dumps(template_data, indent=2) + "\n").encode("utf-8"),
        )
        return "created", redirect

    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unchanged (user file unparseable)", None

    merged = _smart_merge_for_bundle(existing, template_data)
    if merged == existing:
        return "unchanged", None

    if dry_run:
        return "would-merge", None

    redirect = _write_file_atomic(
        target_path,
        (json.dumps(merged, indent=2) + "\n").encode("utf-8"),
    )
    return "merged", redirect


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


def _apply_canonical_env_via_config_projection(
    folder: Path,
    *,
    orchestrator_root: Optional[Path] = None,
) -> dict:
    """Phase 0.B Part 2 (2026-05-25): project the FULL canonical env
    into a project's `.claude/settings.json::env` + `.claude/env` via
    the single-writer contract.

    Resolves the project_id by folder match in launcher.db, then
    delegates to `vco_lib.config_projection.apply_project_env`. Soft-
    fails to no-op when:

      - launcher.db is missing (pre-launcher-boot / corrupt install).
      - The folder isn't registered in `projects` (race with create flow,
        or post-unregister).

    Args:
        folder: project root (must already exist).
        orchestrator_root: v0.2.37 (Gap 6a) — when set, the env bundle
            carries VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR
            exports so direct CLI users (no launcher) can locate the
            orchestrator clone from `.claude/env`. Forwarded to
            `project_env_from_db` which is the canonical emit site.

    Returns:
        Same shape as the legacy backfill helpers' return dict for
        backward-compat with the install-bundle event log consumer:
            {"action": str, "added_keys": [str, ...], "path": str,
             "resolved_values": {key: value, ...}}

        Actions:
            - "applied": canonical env successfully projected.
            - "db_unreachable": launcher.db missing.
            - "not_registered": no project row matches folder.
            - "apply_failed:<ExceptionName>:<message>": contract raised.
    """
    settings_file = folder / ".claude" / "settings.json"
    result: dict = {
        "action": "missing",
        "added_keys": [],
        "path": str(settings_file),
        "resolved_values": {},
    }

    # Resolve project_id from folder path. The lookup mirrors
    # `_read_kg_binding_override`'s soft-fail pattern. Path resolution
    # delegated to `vco_lib.paths.launcher_db_path` (v0.2.40 F5).
    from vco_lib.paths import launcher_db_path
    db_path = launcher_db_path()
    if not db_path.is_file():
        result["action"] = "db_unreachable"
        return result
    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        result["action"] = "db_unreachable"
        return result

    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=2.0
        )
    except _sqlite3.Error:
        result["action"] = "db_unreachable"
        return result

    project_id: Optional[str] = None
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, folder_path FROM projects")
            rows = cur.fetchall()
        except _sqlite3.Error:
            rows = []
        for row_id, row_folder in rows:
            if _canonical_path_eq(row_folder or "", folder_canonical):
                project_id = str(row_id)
                break
    finally:
        try:
            conn.close()
        except _sqlite3.Error:
            pass

    if project_id is None:
        result["action"] = "not_registered"
        return result

    # Delegate to the contract. The import lives in its OWN try: if it
    # were inside the call-try below and failed, the `except
    # DbUnreachable` clause would itself NameError on the unbound
    # exception class, masking the real ImportError.
    try:
        from vco_lib.config_projection import (
            apply_project_env,
            project_env_from_db,
            DbUnreachable,
            ProjectNotFound,
        )
    except ImportError as e:  # pragma: no cover — module ships in-tree
        result["action"] = f"apply_failed:{type(e).__name__}:{e}"
        return result
    try:
        bundle = project_env_from_db(
            project_id,
            orchestrator_root=orchestrator_root,
        )
        report = apply_project_env(bundle)
    except DbUnreachable:
        result["action"] = "db_unreachable"
        return result
    except ProjectNotFound:
        result["action"] = "not_registered"
        return result
    except Exception as e:
        result["action"] = f"apply_failed:{type(e).__name__}:{e}"
        return result

    keys_written: set[str] = set()
    for _surface, keys in report.items():
        keys_written.update(keys)

    result["action"] = "applied"
    result["added_keys"] = sorted(keys_written)
    result["resolved_values"] = dict(bundle["canonical_env"])
    return result


def _apply_standalone_env(
    folder: Path,
    orchestrator_root: Optional[Path],
    project_name: Optional[str],
) -> dict:
    """A2 (2026-05-28): write env surfaces from --orchestrator-root alone.

    Used when ``--write-env`` is passed but launcher.db is absent (or the
    project is not registered in it).  Constructs a :class:`ProjectEnvBundle`
    from the folder-basename + orchestrator_root without any DB access, then
    delegates to :func:`~vco_lib.config_projection.apply_project_env`.

    This is the standalone / fork-integrator path that makes ``install-bundle``
    fully functional without a running launcher.

    Args:
        folder: target project root (must exist).
        orchestrator_root: path to the orchestrator clone.  ``VCT_ORCHESTRATOR_ROOT``
            is only emitted when this is non-None.
        project_name: raw project name override.  Defaults to ``folder.name``.

    Returns:
        Same shape as :func:`_apply_canonical_env_via_config_projection`::

            {"action": str, "added_keys": [str, ...], "path": str,
             "resolved_values": {key: value, ...}}

        Actions:
            - ``"applied"``: env written.
            - ``"apply_failed:<ExceptionName>:<message>"``: write error.
    """
    from vco_lib.config_projection import apply_project_env, ProjectEnvBundle

    settings_file = folder / ".claude" / "settings.json"
    result: dict = {
        "action": "missing",
        "added_keys": [],
        "path": str(settings_file),
        "resolved_values": {},
    }

    raw_name = project_name or folder.name or "Project"
    sanitized = sanitize_for_weaviate_class(raw_name)

    # v0.2.76 (seams-lens #1 / CG naming): CODE_GRAPH_PROJECT must NOT use the
    # underscore-DROPPING KG sanitizer — the analyzer/hub/binding name code-graph
    # classes with the underscore-PRESERVING `canonical_class_prefix`
    # (SSOT: vco_lib.codegraph_naming), so `My_Project` binds `My_Project_Code*`
    # but this writer used to emit `CODE_GRAPH_PROJECT=MyProject` → the env-driven
    # analyzer / MCP hub-down fallback targeted `MyProject_Code*` → split-brain.
    #
    # This is the STANDALONE writer (`_apply_standalone_env`, --write-env without
    # launcher.db, per this function's docstring): there is NO reachable
    # `project_codegraph_bindings` row to consult, so we cannot honor an existing
    # binding as SSOT. We fall back to `canonical_class_prefix` (the SAME rule the
    # analyzer/hub/binding-seed use), which is the correct value for a
    # never-yet-analyzed project and matches what the first analysis will bind. No
    # contradiction risk: with no DB, there is no binding to disagree with. The
    # DB-backed writers (config_projection.project_env_from_db, the Rust
    # populate()) resolve binding-first; this path only ever runs when they can't.
    from vco_lib.codegraph_naming import canonical_class_prefix as _canonical_cg_prefix

    try:
        code_graph_project = _canonical_cg_prefix(raw_name)
    except ValueError:
        # `canonical_class_prefix` rejects leading-digit / all-symbol names that
        # `sanitize_for_weaviate_class` coerces (e.g. to "vct"). Keep the
        # coerced value for those pathological names so --write-env never crashes
        # on a weird folder basename — the analyzer applies the identical
        # canonical rule and, for a rejectable name, its own binding-seed path
        # will settle the final prefix on first analysis.
        code_graph_project = sanitized

    # v0.2.84 PLAN-v0284 D1/D3 last-resort clause: this is the no-DB standalone
    # path (--write-env without a reachable launcher.db), so there is NO binding
    # to resolve from — name derivation is the CORRECT last resort here (per D1,
    # name-derivation survives only in genuinely binding-less contexts). The
    # DB-backed / settings-pinned writers (config_projection.project_env_from_db,
    # the Rust populate(), and D3's binding-first bootstrap/migrate) resolve
    # binding-first; this path only ever runs when they can't.
    kg_collection = f"{sanitized}_KnowledgeGraph"
    dev_collection = f"{sanitized}_Development"
    diagrams_collection = f"{sanitized}_Diagrams"
    shared_kg = "VibeCodedOrchestrator_KnowledgeGraph"

    env: dict[str, str] = {
        "PROJECT_NAME": raw_name,
        "CODE_GRAPH_PROJECT": code_graph_project,
        "KG_COLLECTION": kg_collection,
        "DEVELOPMENT_COLLECTION": dev_collection,
        "DIAGRAMS_COLLECTION": diagrams_collection,
        "SHARED_KG_COLLECTION": shared_kg,
        "SHARED_KG_WRITE_DISABLED": "false",
        "SHARED_KG_OPT_OUT": "false",
        "ACTIVE_EMBEDDING": "qwen3",
        "WEAVIATE_URL": "http://localhost:8081",
        "WEAVIATE_PORT": "8081",
        "OLLAMA_URL": "http://localhost:11435",
        "OLLAMA_PORT": "11435",
        "CODE_EMBED_URL": "http://localhost:11440",
        "CODE_EMBED_PORT": "11440",
    }

    if orchestrator_root is not None:
        orch = Path(orchestrator_root).resolve()
        env["VCT_ORCHESTRATOR_ROOT"] = str(orch)
        env["VCT_INFRASTRUCTURE_DIR"] = str(orch / "infrastructure")
        # v0.2.37 Gap 6a legacy alias consumed by code-graph-analyze wrapper
        env["VCT_INSTALL_ROOT"] = str(orch)

    bundle: ProjectEnvBundle = {
        "canonical_env": env,
        "project_id": "",      # no DB row — sentinel empty string
        "project_root": folder.resolve(),
    }

    try:
        report = apply_project_env(bundle)
    except Exception as e:
        result["action"] = f"apply_failed:{type(e).__name__}:{e}"
        return result

    keys_written: set[str] = set()
    for _surface, keys in report.items():
        keys_written.update(keys)

    result["action"] = "applied"
    result["added_keys"] = sorted(keys_written)
    result["resolved_values"] = dict(env)
    return result


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
    import sqlite3 as _sqlite3

    out: dict = {"collection_prefix": None, "has_manual_override": False}

    # Path resolution delegated to `vco_lib.paths.launcher_db_path` (v0.2.40 F5).
    from vco_lib.paths import launcher_db_path
    db_path = launcher_db_path()

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

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
            if _canonical_path_eq(row_folder or "", folder_canonical):
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
    import sqlite3 as _sqlite3

    out: dict = {
        "primary_kg_collection": None,
        "primary_has_manual_override": False,
        "shared_kg_collection": None,
        "shared_has_manual_override": False,
    }

    # Path resolution delegated to `vco_lib.paths.launcher_db_path` (v0.2.40 F5).
    from vco_lib.paths import launcher_db_path
    db_path = launcher_db_path()

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

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
            if _canonical_path_eq(row_folder or "", folder_canonical):
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
    import sqlite3 as _sqlite3

    out: dict = {}

    # DB path resolution — delegated to `vco_lib.paths.launcher_db_path`
    # (v0.2.40 F5). The canonical resolver also honours `$VCT_STATE_DIR`
    # so multi-launcher dev setups continue to work, and mirrors
    # install.py._discover_app_state_db_path.
    from vco_lib.paths import launcher_db_path
    db_path = launcher_db_path()

    if not db_path.is_file():
        return out

    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return out

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
            if _canonical_path_eq(row_folder or "", folder_canonical):
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
    # v0.2.84 PLAN-v0284 D3/D1: route BOTH the suffix-swap and the name-derived
    # fallback through the ONE-rule helper `_dev_diagrams_from_primary` so the
    # dev-name derivation has a single home (== config_projection's future
    # one-rule realization + the hub's Decision C). Same resolved value as
    # before for both the binding-first primary and the name-derived last
    # resort — just no longer an inline duplicate of the rule.
    if "DEVELOPMENT_COLLECTION" not in env:
        primary = env.get("KG_COLLECTION", "")
        primary = primary if isinstance(primary, str) else ""
        dev_name, _diagrams = _dev_diagrams_from_primary(
            primary, _derive_basename(),
        )
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
#   memory pressure. Reproduced across multiple multi-GB project trees
#   (47 GB scientific stack, 32 GB / 76k-file legacy clone, 33 GB cargo
#   target). Local fix in each case
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
    live on multiple large project trees on 2026-05-16). The launcher now
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


# Interpreter tokens after which a `.claude/hooks/<name>` token is the
# INVOKED script (bash/sh/PowerShell, .exe variants). Used by
# `_vco_hook_script_identity` to anchor on the invoked-script POSITION.
_HOOK_INTERPRETER_TOKENS = frozenset({
    "bash", "sh", "dash", "zsh",
    "pwsh", "pwsh.exe", "powershell", "powershell.exe",
})
# PowerShell flags whose VALUE (next token) is the script to run.
_HOOK_SCRIPT_FLAG_TOKENS = frozenset({"-file", "-command"})
# Shell control operators that reset "command start" so the token after them
# can begin a fresh invocation (e.g. the `||` in the VCO disable-guard prefix
# `[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/x.sh`).
_HOOK_CMD_SEPARATOR_TOKENS = frozenset({"||", "&&", ";", "|", "&"})

# A bare `.claude/hooks/<name>.{sh,ps1}` token (with optional `${VAR}/` /
# `%VAR%/` / path prefix ahead of `.claude/`, no embedded whitespace), with the
# capture group on the basename. Anchored to the FULL token (the token has
# already been split on whitespace + de-quoted by the caller).
_HOOK_TOKEN_RE = re.compile(
    r"^(?:[^\s]*/)?\.claude/hooks/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:sh|ps1))$"
)


def _vco_hook_script_identity(command: str) -> Optional[str]:
    """Extract the canonical IDENTITY of a VCO-shipped hook command: the hook
    SCRIPT basename under `.claude/hooks/` (e.g. `ensure-containers.ps1`,
    `pre-tool-use.sh`), normalized across path-separator (`\\` vs `/`),
    `${...}` / `%...%` variable expansion, and quoting. Returns `None` when the
    command does NOT *invoke* a script under `.claude/hooks/` — i.e. it is a
    user's OWN custom hook, which must never be rewritten/dropped.

    v0.2.70 (Stream G): two commands with the SAME identity are the SAME VCO
    hook (one may be a stale form of the other, e.g. a backslash path
    pre-v0.2.70 vs the forward-slash form shipped now). This is the conservative
    matcher behind the supersede-not-stack merge.

    BLOCKER G-1 fix: the identity is resolved ONLY when `.claude/hooks/<name>`
    is the *invoked script*, NOT when it appears anywhere in the command string.
    A user command that merely *references* a VCO hook path as an ARGUMENT or a
    pipe/cat operand (e.g. `bash my-wrapper.sh --target .claude/hooks/x.sh` or
    `cat .claude/hooks/x.sh | grep foo`) is a CUSTOM user hook and returns
    `None` (never superseded/destroyed). A token is the invoked script iff it is
    (a) the first command-start token, (b) immediately follows a shell
    interpreter token (`bash`/`pwsh`/...), or (c) immediately follows a
    PowerShell `-File`/`-Command` flag. Tokens appearing later as arguments or
    after a pipe (without one of those anchors) are NOT invocations.
    """
    if not command or not isinstance(command, str):
        return None
    # Normalize path separators (bash eats `\`; PowerShell accepts both) so a
    # stale backslash command and the forward-slash form collapse to the same
    # identity. Strip quotes so quoted path tokens still split cleanly.
    norm = command.replace("\\", "/").replace('"', " ").replace("'", " ")
    tokens = norm.split()
    if not tokens:
        return None

    # Walk tokens tracking whether the CURRENT position is a command-start
    # (eligible to be the invoked script): true at index 0, after a shell
    # control operator, after an interpreter token, or after a -File/-Command
    # flag. A `.claude/hooks/<name>` token is the invoked script ONLY at such a
    # position. Anywhere else (a later argument / pipe operand) → not an
    # invocation; keep scanning but never resolve identity for it.
    at_command_start = True
    expect_script_value = False  # set after a -File/-Command flag
    for tok in tokens:
        low = tok.lower()
        if expect_script_value:
            # This token is the explicit script value of -File/-Command.
            m = _HOOK_TOKEN_RE.match(tok)
            if m:
                return m.group(1)
            expect_script_value = False
            at_command_start = False
            continue
        if at_command_start:
            m = _HOOK_TOKEN_RE.match(tok)
            if m:
                return m.group(1)
            if low in _HOOK_INTERPRETER_TOKENS:
                # Next token is the script the interpreter runs.
                at_command_start = True
                continue
            # A real executable / token at command-start that isn't an
            # interpreter (e.g. `cat`, `my-wrapper.sh`) — subsequent tokens are
            # its arguments, not invocations.
            at_command_start = False
            continue
        # Not at command start: handle separators + script flags.
        if low in _HOOK_CMD_SEPARATOR_TOKENS:
            at_command_start = True
            continue
        if low in _HOOK_SCRIPT_FLAG_TOKENS:
            expect_script_value = True
            continue
        # Plain argument — ignore (this is where the BLOCKER false-positive
        # was: a hook path here is an arg, not an invocation).
    return None


def _merge_hooks_for_bundle(user_hooks: dict, template_hooks: dict) -> dict:
    """Per-event hook array merge.

    v0.2.70 (Stream G — supersede-not-stack): historically this was APPEND-ONLY
    by exact command-STRING identity, so when a VCO-shipped hook's command form
    changed (e.g. a path-separator fix `...\\hooks\\x.ps1` -> `.../hooks/x.ps1`,
    a flag change, or an interpreter change) bundle-update did NOT heal existing
    projects — it STACKED the new command next to the stale one, leaving the
    BROKEN command actively firing (and failing) at every event alongside the
    working one. That is worse than dead code: the stale invocation keeps
    running.

    Now: a template entry whose hook-script IDENTITY (the `.claude/hooks/<name>`
    basename, normalized across `\\`/`/`, var-expansion, quoting) matches an
    EXISTING user entry's identity but whose command STRING differs SUPERSEDES
    the stale one — the stale command is dropped and the template's current
    command installed, leaving exactly ONE invocation per VCO hook. Identity +
    string both match → left as-is (idempotent). Identity absent from the user's
    set → appended (genuinely new VCO hook; today's behavior).

    CONSERVATIVE GUARD (critical): a command is only treated as a VCO hook when
    `_vco_hook_script_identity` resolves it to a `.claude/hooks/<name>` script
    VCO actually ships (i.e. the same identity appears in the TEMPLATE). A
    user's OWN custom hook (a script not under `.claude/hooks/`, or one VCO
    doesn't ship) returns `None` / has no template match and is PRESERVED
    byte-for-byte — never rewritten or dropped. When in doubt, fall back to the
    pre-v0.2.70 append behavior (a wrong replace that clobbers a user hook is
    worse than a missed supersede).
    """
    out = dict(user_hooks)

    def _entry_cmds(entry: dict) -> list[str]:
        if not isinstance(entry, dict):
            return []
        cmds: list[str] = []
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            # Keep only non-empty string commands (drops None + falsy) — this
            # also narrows the element type to `str` for the return contract.
            if isinstance(cmd, str) and cmd:
                cmds.append(cmd)
        return cmds

    for event, t_entries in template_hooks.items():
        if event not in out:
            out[event] = list(t_entries)
            continue
        u_entries = out[event] if isinstance(out[event], list) else []

        # Exact command strings already present (idempotent skip).
        existing_cmds: set[str] = set()
        for entry in u_entries:
            for c in _entry_cmds(entry):
                existing_cmds.add(c)

        # Build the merged entry list. First pass: SUPERSEDE stale VCO hook
        # commands in the USER entries whose identity matches a template
        # identity but whose string differs from the current template command.
        # Map identity -> current template command (first occurrence wins;
        # the template ships at most one command per identity per event). The
        # KEYS of this map ARE the set of VCO-shipped identities eligible to
        # supersede — a user command whose identity is absent here (e.g. a
        # user's own hook, or a hook this template doesn't ship) is never
        # rewritten. This is the eligibility guard (no separate set needed).
        template_cmd_for_identity: dict[str, str] = {}
        for t_entry in t_entries:
            for c in _entry_cmds(t_entry):
                ident = _vco_hook_script_identity(c)
                if ident and ident not in template_cmd_for_identity:
                    template_cmd_for_identity[ident] = c

        merged_entries: list = []
        superseded_identities: set[str] = set()
        for entry in u_entries:
            if not isinstance(entry, dict):
                merged_entries.append(entry)
                continue
            new_entry = dict(entry)
            new_hooks: list = []
            for h in entry.get("hooks", []):
                if not isinstance(h, dict) or not h.get("command"):
                    new_hooks.append(h)
                    continue
                cmd = h["command"]
                ident = _vco_hook_script_identity(cmd)
                # CONSERVATIVE: only supersede when the identity is a VCO hook
                # the template ships AND the string actually differs (stale
                # form). A user's own hook (ident None, or ident not in the
                # template) is preserved verbatim.
                if (
                    ident
                    and ident in template_cmd_for_identity
                    and cmd != template_cmd_for_identity[ident]
                ):
                    new_h = dict(h)
                    new_h["command"] = template_cmd_for_identity[ident]
                    new_hooks.append(new_h)
                    superseded_identities.add(ident)
                else:
                    new_hooks.append(h)
                    if ident and cmd == template_cmd_for_identity.get(ident):
                        # Already current — record so the append pass skips it.
                        superseded_identities.add(ident)
            new_entry["hooks"] = new_hooks
            merged_entries.append(new_entry)

        # Second pass: APPEND genuinely-new template hooks at PER-COMMAND
        # (inner-hook) granularity. A template command is "handled" (so it
        # must NOT be re-appended) when EITHER its exact string is already
        # present verbatim OR its VCO-hook identity was just superseded/
        # confirmed-current in a user entry above.
        #
        # WHY per-command, not per-entry (pre-existing bug, A3): the template
        # ships several inner-hooks in ONE event group (e.g. the `Stop` group
        # carries cost-tracker + notify-stop + stop-drain-citations together).
        # Appending the WHOLE group whenever ANY one inner-hook is new would
        # re-introduce the already-present cost-tracker/notify-stop commands as
        # a second entry → a duplicate cost row in costs.jsonl and a double
        # desktop notification at every turn-end. Adding the new
        # stop-drain-citations hook makes this fire on every existing project's
        # next bundle update. So append only the inner-hooks that are NOT
        # already handled, preserving their per-hook config (timeout/async).
        def _cmd_handled(c: str) -> bool:
            if c in existing_cmds:
                return True
            ident = _vco_hook_script_identity(c)
            return ident is not None and ident in superseded_identities

        for t_entry in t_entries:
            if not isinstance(t_entry, dict):
                continue
            # Carry forward only the template inner-hooks whose command is not
            # already present (a command-less hook item, if any, is dropped on
            # the append path — it has no identity to dedup and the user's
            # existing group already covers any structural hooks).
            new_inner = [
                h
                for h in t_entry.get("hooks", [])
                if isinstance(h, dict)
                and h.get("command")
                and not _cmd_handled(h["command"])
            ]
            if not new_inner:
                continue
            appended = dict(t_entry)
            appended["hooks"] = new_inner
            merged_entries.append(appended)

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
        # v0.2.84 PLAN-v0284 D3 (P2 / ruling R3): resolve BINDING-FIRST when a
        # `--project-folder` is available (update step 3 walks the CORRECT
        # collection family instead of the display-name-derived one). Pre-.84
        # this name-derived from `--name`, so a diverged binding made the
        # migrate dispatcher walk the wrong `<DisplayName>_*` family. Falls back
        # to the identical name-derived set when no folder / no binding.
        _mig_folder = getattr(args, "project_folder", None)
        _mig_folder_path = Path(_mig_folder).resolve() if _mig_folder else None
        derived = _resolve_bundle_collection_names_binding_first(
            args.name, _mig_folder_path,
        )
        # Inject env so migrate_collections picks them up. We don't mutate
        # the caller's environment beyond this process — argparse callers
        # are typically the Rust subprocess or a CLI invocation, not a long-
        # lived shell.
        os.environ["KG_COLLECTION"] = derived["kg_collection"]
        os.environ["DEVELOPMENT_COLLECTION"] = derived["development_collection"]
        # v0.2.54 Track D (live-test finding): DIAGRAMS_COLLECTION must be
        # scoped to --name too. Pre-fix, only KG + Dev were overridden, so
        # an AMBIENT DIAGRAMS_COLLECTION (e.g. exported by the invoking
        # project's .claude/settings.json env into the shell) leaked into
        # the plan — `migrate-collections --name OtherProject
        # --force-rebuild` would then drop + rebuild the CURRENT project's
        # live Diagrams collection, violating the documented "--name scopes
        # the work to THIS project's collections" contract.
        os.environ["DIAGRAMS_COLLECTION"] = derived["diagrams_collection"]

        # Build a minimal Namespace-like for migrate_collections dispatch.
        # v0.2.73 FIX-D4: thread --name + --index-type so _build_plan can add
        # the (GATED) code-graph collections when hfresh is requested.
        ns = argparse.Namespace(
            force_rebuild=bool(args.force_rebuild),
            name=args.name,
            index_type=getattr(args, "index_type", None),
        )
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
    # v0.2.55 (stale-migration-deferral fix): True when a clean dry-run
    # cleared a stale `schema_migration_required` entry left by an earlier
    # update.
    result.setdefault("stale_migrate_deferral_cleared", False)
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

    # v0.2.54 Track D (P0-2): rebuild recovery for the CLI path. The
    # `rebuild` action drops + recreates the collection with the target
    # schema (see migrate_collections), but the CLI handler historically
    # had NO re-ingest step — install.py's `_seed_weaviate` only runs on
    # the install.py call path. Since the `schema_migration_required`
    # deferral's `command_to_apply` points users at exactly this CLI,
    # following the documented recovery command used to leave the KG
    # empty until the next full install.py run.
    #
    # Recovery contract:
    #   * wet-run only (dry-run has nothing to recover);
    #   * when `--project-folder` is given, run the project's bundled
    #     `.claude/scripts/sync_knowledge_graph.py --all` with THIS
    #     interpreter (the orchestrator venv — it has weaviate +
    #     weaviate_mcp importable) so knowledge/ + docs/ re-ingest
    #     immediately;
    #   * when `--project-folder` is absent, we cannot locate the .md
    #     sources — surface `reingest_required` in the JSON envelope and
    #     print the exact kg-sync command so neither a human nor an LLM
    #     agent mistakes "schema recreated" for "data restored".
    project_folder = getattr(args, "project_folder", None)
    rebuilt_collections = [
        e["collection"] for e in result.get("plan", [])
        if e.get("action") == "rebuild"
    ]
    result.setdefault("reingest_required", False)
    result.setdefault("reingest", None)
    if rebuilt_collections and not bool(args.dry_run):
        if project_folder:
            folder = Path(project_folder).resolve()
            sync_script = (
                folder / ".claude" / "scripts" / "sync_knowledge_graph.py"
            )
            if sync_script.is_file():
                import subprocess  # local import — module convention

                sync_env = dict(os.environ)
                if args.weaviate_url:
                    sync_env["WEAVIATE_URL"] = args.weaviate_url
                print(
                    f"  re-ingesting after rebuild of "
                    f"{', '.join(rebuilt_collections)} via {sync_script} ...",
                    file=sys.stderr,
                )
                try:
                    proc = subprocess.run(
                        [sys.executable, str(sync_script), "--all"],
                        cwd=str(folder),
                        env=sync_env,
                        timeout=900,
                    )
                    result["reingest"] = {
                        "script": str(sync_script),
                        "returncode": proc.returncode,
                    }
                    if proc.returncode != 0:
                        result["reingest_required"] = True
                        result["errors"].append({
                            "collection": None,
                            "action": "reingest",
                            "error": (
                                f"post-rebuild re-ingest exited "
                                f"{proc.returncode}; run "
                                f"`.claude/scripts/kg-sync --all` from "
                                f"{folder} to restore the dropped data"
                            ),
                        })
                except (OSError, subprocess.TimeoutExpired) as e:
                    result["reingest_required"] = True
                    result["errors"].append({
                        "collection": None,
                        "action": "reingest",
                        "error": (
                            f"post-rebuild re-ingest failed to run: "
                            f"{type(e).__name__}: {e}; run "
                            f"`.claude/scripts/kg-sync --all` from "
                            f"{folder} to restore the dropped data"
                        ),
                    })
            else:
                result["reingest_required"] = True
                result["errors"].append({
                    "collection": None,
                    "action": "reingest",
                    "error": (
                        f"rebuild dropped {', '.join(rebuilt_collections)} "
                        f"but {sync_script} is missing — run "
                        f"`.claude/scripts/kg-sync --all` from {folder} "
                        f"to restore the data"
                    ),
                })
        else:
            result["reingest_required"] = True
            print(
                "  NOTE: rebuild recreated "
                f"{', '.join(rebuilt_collections)} with the target schema "
                "but the data was NOT re-ingested (no --project-folder "
                "given). Run `.claude/scripts/kg-sync --all` from the "
                "project folder to restore it.",
                file=sys.stderr,
            )

    # PR 5: drift-detection deferral (pre-update path).
    if project_folder and bool(args.dry_run) and not result["errors"] and not all_projects:
        # v0.2.70: `copy` is ALWAYS lossless — the staging double-copy
        # round-trips every EXISTING UUID + named vector + property byte-for-byte
        # via `_copy_collection_with_vectors` (no re-embedding; the live
        # collection is not dropped until the staging swap's count-match
        # assertion passes). So `copy` must AUTO-APPLY without consent — only
        # genuinely data-losing actions defer, and `action == "rebuild"` is the
        # exact lossy set here. `legacy_single_vector` classifies `rebuild`
        # (never `copy`). A same-name/different-dim slot is INVISIBLE to
        # `_schema_delta` (name-only comparison): on its own it yields `noop`;
        # when it COEXISTS with a genuinely-missing slot, `_classify_action`
        # returns `copy` (driven by the missing slot) and the mismatch slot
        # rides along — but copy still only round-trips the EXISTING vectors
        # verbatim (it neither fixes nor worsens the dim-mismatch, and never
        # re-embeds/drops), so it remains lossless + data-safe. Genuine
        # dim-mismatch remediation is owned by the schema_migration_runner
        # subsystem (it defers). The dry-run plan strips `delta`, leaving
        # `action` as the only signal here — sufficient given that proof.
        #
        # NOTE: this auto-apply is NEW behavior, NOT a mirror of
        # `install.py --update` (whose drift detector EXCLUDES the additive
        # v0.2.18 slots and never reaches the apply for an additive 3->5 drift).
        # It is justified purely by losslessness. The launcher's WET follow-up
        # that actually applies the additive subset lives in
        # `projects_v2.rs::run_migrate_dry_run` (the dry-run probe here only
        # stops deferring — it never mutates).
        destructive = [
            e for e in result.get("plan", [])
            if e.get("action") == "rebuild"
        ]
        resolved_folder = Path(project_folder).resolve()
        if destructive:
            try:
                _emit_migrate_required_deferral(
                    resolved_folder,
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
        else:
            # v0.2.55 (stale-migration-deferral fix): the dry-run is CLEAN (no
            # copy/rebuild needed). PRE-v0.2.55 this branch did nothing, so
            # a `schema_migration_required` entry written by an EARLIER
            # update (when a migration WAS pending) survived forever even
            # after the migration was applied or the schema healed —
            # exactly the stale-deferral carry-forward bug (the entry was re-read by
            # `DeferralReport.read()` on every subsequent bundle update and
            # never cleared because the emitter is gated on `destructive`).
            # Re-probe-clears-stale, matching the Track D `--apply-deferred`
            # discipline: a clean dry-run IS the re-probe; clear the stale
            # entry. Soft-fail — never abort the update over a deferral
            # housekeeping write.
            try:
                # v0.2.83 PLAN-v0283 WP-B2: resolve via the ONE locked emitter
                # home (read-modify-write under the exclusive lock; foreign
                # entries preserved). resolve_conditions returns the count it
                # actually cleared, so the flag is set only when it fired.
                from vco_lib import deferral_emit as _de
                cleared = _de.resolve_conditions(
                    resolved_folder, ["schema_migration_required"],
                )
                if cleared:
                    result["stale_migrate_deferral_cleared"] = True
                    # stderr (not stdout) so `--json` output stays parseable.
                    print(
                        "  [ok] schema_migration_required: dry-run clean — "
                        "cleared stale migration deferral (no copy/rebuild "
                        "needed).",
                        file=sys.stderr,
                    )
            except Exception as e:
                # Housekeeping only — report but don't fail.
                result["errors"].append({
                    "collection": None,
                    "action": "deferral-clear",
                    "error": f"stale migrate-deferral clear failed: "
                             f"{type(e).__name__}: {e}",
                })

    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"dry_run: {result['dry_run']}  "
            f"deferral_emitted: {result['deferral_emitted']}  "
            f"reingest_required: {result.get('reingest_required', False)}"
        )
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
    `<sanitized>_Development`, `<sanitized>_Diagrams`, and — v0.2.73 GAP-1 —
    the 5 code-graph classes `<CodePrefix>_CodeModule/CodeClass/CodeFunction/
    CodeAPI/CodeInteraction`). The shared KG (`VibeCodedOrchestrator_KnowledgeGraph`
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
    # fix/a1-indexing-pipeline (2026-05-25): include the per-project
    # Diagrams collection so unregister cleans up the full set the
    # bootstrap path creates. Idempotent — a 404 from Weaviate on a
    # never-created Diagrams class still counts as a successful drop
    # (consistent with the existing KG / Dev semantics).
    #
    # v0.2.73 GAP-1 (H3): ALSO drop the 5 code-graph collections. Pre-fix,
    # `drop-collections` was CODE-BLIND — a consented project-unregister with
    # `purge_collections:true` left `<Prefix>_CodeFunction` (potentially the
    # 87 GB monster) et al. behind → an instant orphan the wizard can't
    # always reclaim (GAP-4). `code_collections` is [] when the code prefix
    # can't be derived, so this is a no-op on degenerate names. Same
    # idempotent 404-is-success semantics as the KG/DEV/DIAGRAMS drops. This
    # is a CONSENTED drop (the user opted into purge_collections) — it does
    # NOT scan/guess; it drops exactly THIS project's own canonical set.
    targets = [
        derived["kg_collection"],
        derived["development_collection"],
        derived["diagrams_collection"],
    ]
    targets.extend(derived.get("code_collections", []) or [])
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


# ── v0.2.73 FIX-C CLI commands (consented orphan cleanup) ──────────────────


def _cmd_detect_orphan_code_collections(args: argparse.Namespace) -> int:
    """`detect-orphan-code-collections --weaviate-url <url>
    [--volume-dir <dir>] [--project-folder <folder>] [--json]`

    READ-ONLY. Runs the two-source orphan detector (binding-exclusion seed)
    and, when --project-folder is set, emits the consented
    `orphan_code_collections_detected` deferral. Never drops anything.
    Exit 0 always (detection is informational); exit 2 on bad args.
    """
    weaviate_url = args.weaviate_url or _weaviate_url_default()
    detection = _detect_orphan_code_collections(
        weaviate_url, volume_dir=getattr(args, "volume_dir", None),
    )
    detection["volume_dir"] = getattr(args, "volume_dir", None)
    emitted = False
    folder = getattr(args, "project_folder", None)
    if folder:
        emitted = _emit_orphan_code_collections_deferral(
            Path(folder), weaviate_url, detection,
        )
    out = {
        "live_orphans": detection["live_orphans"],
        "ondisk_orphans": detection["ondisk_orphans"],
        "keep_resolvable": detection["keep_resolvable"],
        "total_reclaim_bytes": detection["total_reclaim_bytes"],
        "deferral_emitted": emitted,
    }
    if getattr(args, "json", False):
        print(json.dumps(out))
    else:
        if not detection["keep_resolvable"]:
            print(
                "keep-set UNRESOLVABLE (launcher.db unreachable) — flagged "
                "NOTHING (conservative).",
                file=sys.stderr,
            )
        for o in detection["live_orphans"]:
            print(f"live orphan: {o['class_name']} ({o.get('object_count')})")
        for o in detection["ondisk_orphans"]:
            mb = o["size_bytes"] / (1024 * 1024)
            print(f"on-disk orphan: {o['dir']} ({mb:.1f} MB)")
        print(f"deferral_emitted: {emitted}")
    return 0


def _cmd_drop_orphan_code_collections(args: argparse.Namespace) -> int:
    """`drop-orphan-code-collections --weaviate-url <url> --confirm [--json]`

    CONSENTED drop of LIVE-schema orphan code classes. RE-VALIDATES against the
    CURRENT launcher bindings + CURRENT live schema at RUN TIME (re-probe-
    before-acting) — never trusts a stale detect-time snapshot, and never drops
    a class whose prefix (case-insensitively) matches a live binding. Requires
    --confirm. Exit 0 on clean drop; 1 on any drop error; 2 without --confirm.
    """
    if not getattr(args, "confirm", False):
        print(
            "refusing to drop without --confirm (this is a destructive, "
            "consented operation)",
            file=sys.stderr,
        )
        return 2
    weaviate_url = args.weaviate_url or _weaviate_url_default()
    # RUN-TIME re-validation: re-derive the orphan set from the CURRENT
    # bindings + CURRENT live schema. If the keep-set is unresolvable, this
    # returns [] and we drop nothing.
    to_drop = _revalidated_orphan_live_classes(weaviate_url)
    result: dict = {"dropped": [], "errors": [], "revalidated_count": len(to_drop)}
    for cls in to_drop:
        try:
            _delete_class(cls, weaviate_url=weaviate_url)
            result["dropped"].append(cls)
        except Exception as e:
            result["errors"].append({"collection": cls, "error": f"{type(e).__name__}: {e}"})
    if getattr(args, "json", False):
        print(json.dumps(result))
    else:
        for n in result["dropped"]:
            print(f"dropped orphan: {n}")
        if not to_drop:
            print("no orphans to drop after run-time re-validation "
                  "(keep-set unresolvable, or all reclaimed already).")
        for err in result["errors"]:
            print(f"  ERROR {err['collection']}: {err['error']}")
    return 1 if result["errors"] else 0


def _cmd_reclaim_stranded_code_segments(args: argparse.Namespace) -> int:
    """`reclaim-stranded-code-segments --volume-dir <dir> --weaviate-url <url>
    --confirm --i-understand-filesystem-level [--project-folder <dir>] [--json]`

    FILESYSTEM-LEVEL reclaim of on-disk stranded code segment dirs whose class
    is already gone from the live schema. HARD invariants:
      * requires BOTH --confirm AND --i-understand-filesystem-level.
      * REFUSES to run while Weaviate is reachable (`/v1/meta`) — deleting a
        segment dir under a running Weaviate can corrupt the volume. The user
        must stop the container first.

    ACTUAL run-time guard (SEV-3 #1 — the docstring previously overstated this):
    with Weaviate DOWN this command CANNOT re-fetch the live schema, so it does
    NOT re-verify absence live. Instead it removes ONLY a dir whose normalised
    prefix is BOTH:
      (a) absent from the launcher.db code-graph binding keep-set, AND
      (b) absent from the DETECT-TIME live-prefix snapshot (captured while
          Weaviate was UP and persisted into `.claude/state/` by the detector).
    It REFUSES to remove ANYTHING when the keep-set is unresolvable (launcher.db
    down) OR when the detect-time snapshot is missing (``--project-folder``
    omitted, or the snapshot file absent) — because without the snapshot it
    cannot rule out the "active code-graph, momentarily-absent binding row"
    degenerate. Conservative by construction: never rm a dir it cannot prove is
    dead.
    Exit 0 on clean reclaim; 1 on error; 2 on missing flags / Weaviate-up.
    """
    if not getattr(args, "confirm", False) or not getattr(
        args, "i_understand_filesystem_level", False,
    ):
        print(
            "refusing: this filesystem-level reclaim requires BOTH --confirm "
            "and --i-understand-filesystem-level",
            file=sys.stderr,
        )
        return 2
    volume_dir = getattr(args, "volume_dir", None)
    if not volume_dir or not os.path.isdir(volume_dir):
        print(f"error: --volume-dir {volume_dir!r} is not a directory", file=sys.stderr)
        return 2
    weaviate_url = args.weaviate_url or _weaviate_url_default()

    # HARD GUARD: Weaviate MUST be down. Probe /v1/meta; if it answers, refuse.
    try:
        status, _body = _http_request(
            "GET", f"{weaviate_url.rstrip('/')}/v1/meta", timeout=5.0,
        )
        weaviate_up = status == 200
    except Exception:
        weaviate_up = False
    if weaviate_up:
        print(
            "refusing: Weaviate is RUNNING (/v1/meta answered). Stop the "
            "weaviate container first — removing a segment dir under a live "
            "Weaviate can corrupt the volume.",
            file=sys.stderr,
        )
        return 2

    # Re-detect stranded dirs against the (now-down) volume. Because Weaviate is
    # down we CANNOT re-fetch the live schema for an absence re-verify. Two
    # independent guards make the rm safe (SEV-3 #1):
    #   (1) launcher.db code-graph binding keep-set — a prefix that IS a live
    #       binding is never removed; unresolvable keep-set → remove NOTHING.
    #   (2) DETECT-TIME live-prefix snapshot (captured while Weaviate was UP,
    #       persisted by the detector) — a prefix that HAD a live class at detect
    #       time is never removed, even without a binding row (the "active
    #       code-graph, momentarily-absent binding row" degenerate). A MISSING
    #       snapshot → remove NOTHING (we cannot rule that degenerate out).
    keep_set, resolvable = _codegraph_keep_set_normalised()

    project_folder = getattr(args, "project_folder", None)
    if project_folder:
        live_snapshot, snapshot_present = _read_orphan_live_prefix_snapshot(
            Path(project_folder),
        )
    else:
        live_snapshot, snapshot_present = (set(), False)

    result: dict = {
        "removed": [],
        "errors": [],
        "keep_resolvable": resolvable,
        "snapshot_present": snapshot_present,
    }

    # SEV-3 #1: without the detect-time snapshot we cannot verify a prefix was
    # dead at detect time → refuse everything (conservative).
    if not snapshot_present:
        if getattr(args, "json", False):
            print(json.dumps(result))
        else:
            print(
                "detect-time live-prefix snapshot MISSING (pass --project-folder "
                "pointing at the project whose deferral emitted this command) — "
                "removed NOTHING (conservative).",
                file=sys.stderr,
            )
        return 0

    import shutil
    for dirname, size in _list_ondisk_weaviate_dirs(volume_dir):
        low = dirname.lower()
        # code-suffix shape only.
        matched_sfx = next(
            (s for s in _CODEGRAPH_SUFFIXES if low.endswith(s.lower())), None,
        )
        if matched_sfx is None:
            continue
        pfx = dirname[: -len(matched_sfx)]
        pfx_norm = _normalise_prefix_for_match(pfx)
        # keep-set guard (skip if unresolvable OR the prefix is a live binding).
        if not resolvable:
            continue  # conservative: never rm when we can't confirm keep-set.
        if pfx_norm in keep_set:
            continue
        # SEV-3 #1 snapshot guard: a prefix that had ANY live class at detect
        # time is active — never rm (guards the momentarily-absent-binding-row
        # degenerate the docstring now honestly describes).
        if pfx_norm in live_snapshot:
            continue
        target = os.path.join(volume_dir, dirname)
        try:
            shutil.rmtree(target)
            result["removed"].append({"dir": dirname, "size_bytes": int(size)})
        except Exception as e:
            result["errors"].append({"dir": dirname, "error": f"{type(e).__name__}: {e}"})

    if getattr(args, "json", False):
        print(json.dumps(result))
    else:
        if not resolvable:
            print("keep-set UNRESOLVABLE — removed NOTHING (conservative).",
                  file=sys.stderr)
        for r in result["removed"]:
            mb = r["size_bytes"] / (1024 * 1024)
            print(f"reclaimed: {r['dir']} ({mb:.1f} MB)")
        for err in result["errors"]:
            print(f"  ERROR {err['dir']}: {err['error']}")
    return 1 if result["errors"] else 0


def _bundle_update_pointer_heal() -> None:
    """R8 (v0.2.76): run the MACHINE-WIDE shared-KG pointer-drift heal after a
    per-project `install-bundle --update`.

    The heal itself is machine-wide (``orchestrator_root_kg_collection`` is one
    app_state key), so running it from a per-project update is redundant with
    the root ``install.py --update`` pass — but it is idempotent and cheap
    (a couple of SELECTs on an already-converged DB), so wiring it here closes
    the window where a user only ever runs bundle-updates (never the root
    update) and the pointer stays drifted.

    launcher.db metadata ONLY (the heal writes no Weaviate objects). Soft-fails
    silently on every error: missing launcher.db, Weaviate unreachable, sqlite
    lock. Never raises — a bundle update must always exit on its own result.
    """
    import sqlite3
    import urllib.request

    try:
        from vco_lib.kg_binding_heal import heal_shared_kg_pointer_drift
    except Exception:
        return

    # launcher.db discovery via the canonical resolver (vco_lib.paths).
    try:
        from vco_lib.paths import vct_root_dir
        db_path = vct_root_dir() / "launcher.db"
    except Exception:
        return
    if not db_path.is_file():
        return

    # Weaviate schema (existence-only; the sole Weaviate call in the heal).
    weaviate_url = (
        os.environ.get("WEAVIATE_URL")
        or f"http://localhost:{os.environ.get('WEAVIATE_PORT', '8081')}"
    )
    try:
        resp = urllib.request.urlopen(  # noqa: S310 (localhost only)
            f"{weaviate_url}/v1/schema", timeout=5,
        )
        schema = json.loads(resp.read())
    except Exception:
        return  # Weaviate unreachable → can't verify existence → skip.
    # Walrus + isinstance so the element type narrows to `str` (pyright:
    # repeating `c.get("class")` in the element expression re-widens to
    # `str | None` — CI caught exactly that on the v0.2.76 push).
    existing_classes = {
        name
        for c in schema.get("classes", [])
        if isinstance(c, dict) and isinstance((name := c.get("class")), str) and name
    }

    # Minimal deferral sink + log shim (the CLI has no DeferralReport here).
    class _Sink:
        def add_entry(self, *_a, **_k):
            return None

    class _Entry:
        def __init__(self, **_k):
            pass

    def _log(_step, _level, msg, **_k):
        # Surface convergence loudly on stderr; the heal only logs on change.
        if "[kg-heal]" in msg:
            print(f"[install-bundle] {msg}", file=sys.stderr)

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.cursor()
            changed = heal_shared_kg_pointer_drift(
                cur,
                existing_classes=existing_classes,
                log_event=_log,
                deferral_report=_Sink(),
                deferral_entry_cls=_Entry,
            )
            if changed:
                conn.commit()
        finally:
            conn.close()
    except Exception:
        return  # sqlite lock / any error → soft-fail.


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
        write_env=bool(getattr(args, "write_env", False)),
        project_name=getattr(args, "project_name", None) or None,
        safe_add=bool(getattr(args, "safe_add", False)),  # v0.2.63
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
    # R8 (v0.2.76): after a real bundle UPDATE, run the machine-wide shared-KG
    # pointer-drift heal (idempotent, soft-fail) so users who only ever run
    # bundle-updates still get the pointer converged. Skipped on dry-run.
    if bool(args.update) and not bool(args.dry_run):
        _bundle_update_pointer_heal()
    return 1 if result["errors"] else 0


def _cmd_dismiss_deferral(args: argparse.Namespace) -> int:
    """`dismiss-deferral --folder <path> --condition-id <id> [--json]`

    Remove the deferral entry whose `condition_id` matches `<id>` from
    `<folder>/.claude/context/UPDATE_DEFERRED.md`. Idempotent: re-running
    on an already-dismissed entry (or against a non-existent deferrals
    file) silently succeeds with exit 0.

    Used by the user (or a future launcher GUI button) to silence a
    deferral whose condition cannot be auto-resolved by re-running
    `install-bundle --update --force`. The deferral emission sites in this
    module (`bundle_user_modified_preserved`, `bundle_skipped_existing_files`,
    `template_review_pending`, `safe_add_skipped_env_merge`,
    `safe_add_git_exclude_updated`, plus any future ones) all reference this
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

    # N-1 (v0.2.83): route the resolve→write through the SHARED deferral file
    # lock (deferral_emit.locked_report) so a concurrent writer — a mid-run
    # install.py finalize(), a detached resync child — cannot interleave its
    # read/write pair with ours and drop entries. The earlier direct
    # `report.write(folder)` here was the LAST un-locked read-modify-write on
    # the deferral file. The malformed-file / no-match exit paths above already
    # returned, so reaching here means we WILL mutate. The `remaining` count now
    # comes from the LOCKED re-read (authoritative post-write disk state) rather
    # than the pre-lock in-memory parse — a concurrent writer's committed
    # entries are correctly reflected. Flat call-site (NOT nested inside another
    # locked_report) — the non-reentrancy contract holds.
    from vco_lib import deferral_emit as _de

    with _de.locked_report(folder) as locked:
        locked.mark_resolved(condition_id)
        remaining = len(locked.entries)
    # `locked_report` writes + deletes-when-empty + strips the CLAUDE.md
    # reminder block on exit — same side effects as the prior direct write.

    # v0.2.83 PLAN-v0283 B-F7 + D9: content-keyed dismissal memory. When the
    # user dismisses `template_review_pending`, snapshot the current reference-
    # sidecar hashes so the producer suppresses re-emission until VCO ships a
    # genuinely new reference. Best-effort + SILENT (never touches the JSON
    # payload / stderr contract, never raises).
    if condition_id == "template_review_pending":
        _store_template_review_dismissal(folder)

    payload = {
        "dismissed": True,
        "condition_id": condition_id,
        "remaining": remaining,
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


def _cmd_check_bundle_resume(args: argparse.Namespace) -> int:
    """Check for a stale bundle-update resume sentinel (NEW-7 / B1).

    Always exits 0 (this is a probe, not a mutation). Caller parses
    the JSON to decide whether to surface a resume nudge.
    """
    folder = Path(args.folder).resolve()
    sentinel = read_bundle_update_resume_sentinel(folder)
    payload = {
        "folder": str(folder),
        "resume_needed": sentinel is not None,
        "sentinel": sentinel,
        "sentinel_path": str(_bundle_sentinel_path(folder)),
    }
    print(json.dumps(payload, indent=2))
    return 0


#: Stable artifact_name for the code_formats registry row — same rationale
#: as ``_NODE_FORMATS_ARTIFACT_NAME`` (one sidecar per project, identified by
#: project_id; a fixed name survives project-folder renames).
_CODE_FORMATS_ARTIFACT_NAME = "default"


def _run_formats_schema_check(
    args: argparse.Namespace,
    *,
    artifact_type: str,
    artifact_name: str,
    regenerate_fn,
    deferral_fn,
    downgrade_detail: str,
    success_action: str = "regenerated",
) -> int:
    """v0.2.73 M2: the GENERIC schema-version gate for regenerated sidecar
    caches (one home — v0.2.57's kg_node_formats machinery, generalized with
    per-artifact parameters instead of a second copy).

    Both sidecars are DERIVED (regeneratable), so:
      * NEVER_MATERIALIZED / UP_TO_DATE → register at canonical, no action.
      * RECREATE_NEEDED (stored < canonical) → ``regenerate_fn(folder)``
        brings the cache to the NEW schema (kg: inline force-regeneration;
        code: keep-regenerated — delete, the generator rebuilds). If it
        can't run, ``deferral_fn(folder, canonical, detail)`` writes an INFO
        deferral and the existing cache is NOT touched (no data loss).
      * REFUSE_DOWNGRADE (stored > canonical) → INFO deferral; never
        downgrade-regenerate.

    An artifact_type not yet in the schema registry (the window before the
    release's schema_versions entry lands) → soft ``skipped`` result, exit 0
    (conservative default: a probe never crashes the update flow).

    DB-aware: needs ``--db`` (defaults to the canonical launcher.db) and an
    optional ``--project-id``. Folder-only callers (no launcher) pass no
    project-id; the registry keys on COALESCE(project_id,'') so NULL works.
    Always exits 0 (update-time probe; soft-fail keeps the bundle update
    flowing). Prints a JSON result.
    """
    from . import artifact_version_registry as avr
    from . import schema_versions as sv

    folder = Path(args.folder).resolve()
    db_path = Path(args.db).resolve() if getattr(args, "db", None) else _launcher_db_path()
    project_id = getattr(args, "project_id", None) or None
    now_ms = int(getattr(args, "now_ms", 0)) or _now_ms_safe()

    try:
        canonical = sv.canonical_version(artifact_type)
    except KeyError:
        print(json.dumps({
            "folder": str(folder),
            "artifact_type": artifact_type,
            "artifact_name": artifact_name,
            "action": "skipped",
            "detail": f"{artifact_type} not in the schema registry yet",
        }, indent=2))
        return 0

    status = avr.check_artifact_version(
        db_path,
        project_id=project_id,
        artifact_type=artifact_type,
        artifact_name=artifact_name,
    )

    result = {
        "folder": str(folder),
        "artifact_type": artifact_type,
        "artifact_name": artifact_name,
        "canonical_version": canonical,
        "status": status.name,
        "action": "none",
        "regenerated": False,
        "deferral_written": False,
    }

    if status in (avr.ArtifactVersionStatus.NEVER_MATERIALIZED,
                  avr.ArtifactVersionStatus.UP_TO_DATE):
        # Record the version so a FUTURE canonical bump is detectable.
        # Idempotent upsert. Register UNCONDITIONALLY (review C1): a fresh
        # project is, by definition, at the current sidecar schema — whether
        # or not the cache file has been materialized YET (both generators
        # run async/in the background, so the file may not exist at
        # update-check time). Registering here closes the window where a
        # schema bump shipped between create and first update would be
        # swallowed as NEVER_MATERIALIZED (registering straight to the new
        # canonical, skipping regeneration). Now the first check records the
        # CURRENT canonical, so the NEXT bump is correctly seen as
        # RECREATE_NEEDED. A project that never materializes the cache just
        # carries a harmless registry row.
        avr.register_artifact_version(
            db_path, project_id=project_id, artifact_type=artifact_type,
            artifact_name=artifact_name, schema_version=canonical,
            materialized_at=now_ms,
        )
        result["action"] = "registered"
    elif status == avr.ArtifactVersionStatus.RECREATE_NEEDED:
        # The sidecar schema bumped. Bring the cache to the new schema via
        # the artifact's strategy; if that can't run, defer — NEVER
        # overwrite/clobber the existing cache.
        regenerated, detail = regenerate_fn(folder)
        if regenerated:
            avr.register_artifact_version(
                db_path, project_id=project_id, artifact_type=artifact_type,
                artifact_name=artifact_name, schema_version=canonical,
                materialized_at=now_ms,
            )
            result["action"] = success_action
            result["regenerated"] = True
        else:
            deferral_fn(folder, canonical, detail)
            result["action"] = "deferred"
            result["deferral_written"] = True
            result["detail"] = detail
    elif status == avr.ArtifactVersionStatus.REFUSE_DOWNGRADE:
        deferral_fn(folder, canonical, downgrade_detail)
        result["action"] = "deferred-downgrade"
        result["deferral_written"] = True

    print(json.dumps(result, indent=2))
    return 0


def _cmd_check_node_formats_schema(args: argparse.Namespace) -> int:
    """v0.2.57: schema-version gate for the regenerated KG-summary cache
    (`knowledge/.node_formats.json`, artifact_type ``kg_node_formats``).

    Thin caller of :func:`_run_formats_schema_check` (v0.2.73 M2
    generalization — behaviour identical). RECREATE_NEEDED attempts an
    inline regeneration (re-run generate-kg-summary.py --force over every
    knowledge node); if that can't run here (generator/backend unavailable),
    an INFO ``regenerated_data_schema_migration_pending`` deferral is
    written and the existing cache is untouched.

    artifact_name: a STABLE constant, NOT the folder basename (review N2).
    The project is already identified by project_id; keying on the volatile
    folder name would orphan the registry row on a folder rename +
    re-trigger the NEVER_MATERIALIZED window. There is exactly one
    node-formats cache per project, so a fixed name is correct.
    """
    return _run_formats_schema_check(
        args,
        artifact_type="kg_node_formats",
        artifact_name=_NODE_FORMATS_ARTIFACT_NAME,
        regenerate_fn=_regenerate_node_formats,
        deferral_fn=_write_node_formats_migration_deferral,
        downgrade_detail=(
            "on-disk kg_node_formats schema is NEWER than this orchestrator "
            "expects (you may have downgraded). Not modifying the cache."
        ),
    )


def _cmd_check_code_formats_schema(args: argparse.Namespace) -> int:
    """v0.2.73 M2: schema-version gate for the regenerated code-summary
    sidecar (`.claude/.code_formats.json`, artifact_type ``code_formats``).

    Thin caller of :func:`_run_formats_schema_check`. Unlike the KG cache
    (regenerated inline from knowledge/*.md), the code sidecar's bump action
    is KEEP-REGENERATED: delete the old-schema sidecar and let the generator
    (``generate-code-summary.py`` — the resync background rider, or a manual
    run) rebuild it in the new schema. Never a forward-migration. Before the
    ``code_formats`` registry entry ships in schema_versions, this is a soft
    no-op (``action: skipped``).
    """
    return _run_formats_schema_check(
        args,
        artifact_type="code_formats",
        artifact_name=_CODE_FORMATS_ARTIFACT_NAME,
        regenerate_fn=_regenerate_code_formats,
        deferral_fn=_write_code_formats_migration_deferral,
        downgrade_detail=(
            "on-disk code_formats schema is NEWER than this orchestrator "
            "expects (you may have downgraded). Not modifying the sidecar."
        ),
        success_action="keep-regenerated",
    )


def _regenerate_code_formats(folder: Path) -> tuple[bool, str]:
    """Keep-regenerated strategy for `.claude/.code_formats.json` (M2 D5):
    delete the old-schema sidecar and let ``generate-code-summary.py``
    rebuild it (resync rider or manual run). Returns (ok, detail); a
    deletion failure → (False, reason) so the caller DEFERS (sidecar left
    intact — no half-migrated state)."""
    sidecar = folder / ".claude" / ".code_formats.json"
    if not sidecar.exists():
        return (True, "no code_formats sidecar on disk — nothing to migrate")
    try:
        sidecar.unlink()
    except OSError as exc:
        return (False, f"could not delete old-schema sidecar {sidecar}: {exc}")
    return (
        True,
        "old-schema sidecar deleted; the code-summary generator rebuilds it "
        "on the next codegraph resync (or run: python "
        ".claude/scripts/generate-code-summary.py --project <name>)",
    )


def _cmd_migrate_schema(args: argparse.Namespace) -> int:
    """CLI entrypoint for the version-gated schema-migration runner.

    Mirrors ``check-node-formats-schema`` (a thin DB-aware subprocess surface
    the launcher + manual retries call). Three modes:

      * default              → run the runner against ``migrations/`` (no-op
                               today), print the ``MigrationRunReport`` as JSON.
      * ``--check``          → dry-run; plan only, no mutation, no registry
                               write. Surfaces ``pending_regenerate`` so the
                               launcher's ``probe_stale_derived_collections``
                               Tauri command can render the modal.
      * ``--regenerate <type> --artifact-name <name>`` → the launcher's
                               "Regenerate now" choice for ONE derived
                               collection: drop + recreate + re-sync via the
                               EXISTING guarded body
                               (``vco_lib.schema_regenerate``), then register at
                               canonical. The destructive recreate ONLY happens
                               on this explicit flag (the launcher's
                               ``apply_stale_derived_choice(choice="regenerate")``
                               Tauri command, driven by an explicit modal click).
                               The shared-KG branch still routes through the
                               guarded ``migrate-shared-kg-schema`` script
                               (GUARD 1/2); per-project / codegraph branches
                               reuse ``migrate-collections --force-rebuild`` /
                               ``code-graph-analyze --force-recreate``. NO new
                               drop path.

    Always exits 0 (probe / soft-fail) unless ``--strict``. Prints JSON.
    """
    from . import schema_migration_runner as smr
    from . import schema_versions as sv

    folder = Path(args.folder).resolve()
    db_path = Path(args.db).resolve() if getattr(args, "db", None) else _launcher_db_path()
    project_id = getattr(args, "project_id", None) or None
    now_ms = int(getattr(args, "now_ms", 0)) or _now_ms_safe()
    weaviate_url = (
        os.environ.get("WEAVIATE_URL")
        or f"http://localhost:{os.environ.get('WEAVIATE_PORT', '8081')}"
    )
    # The migration EDGES ship in the orchestrator clone's `migrations/`, NOT
    # in the per-project folder. The launcher runs this subcommand with
    # cwd=<orchestrator-root> (mirrors run_node_formats_schema_check), so the
    # orchestrator root is the parent of this vco_lib package. A test / manual
    # caller can override via --migrations-dir.
    migrations_override = getattr(args, "migrations_dir", None)
    if migrations_override:
        migrations_dir = Path(migrations_override).resolve()
    else:
        orchestrator_root = Path(__file__).resolve().parent.parent
        migrations_dir = orchestrator_root / "migrations"
    # Per-project CLI surface (launcher → apply_post_bundle_steps) migrates ONLY
    # that project's own collections — never the shared KG / launcher-global
    # shapes (those are the ROOT update's job, via install.py). So default
    # include_orchestrator_wide=False here; --include-orchestrator-wide opts in
    # (the root project's own per-project run, or a deliberate manual call).
    include_wide = bool(getattr(args, "include_orchestrator_wide", False))

    regenerate_type = getattr(args, "regenerate", None)
    if regenerate_type:
        # POLICY STEP 3 "Regenerate now" (Piece 4) — the EXPLICIT, user-clicked
        # drop+recreate+re-sync for ONE derived collection. This is the only
        # code path in v0.2.60 that performs a destructive Weaviate recreate,
        # and it ONLY runs when the launcher passes --regenerate (driven by an
        # explicit modal click) — never automatically. The recreate composes
        # EXISTING machinery (the guarded migrate-shared-kg-schema script for
        # the shared KG with GUARD 1/2; migrate-collections --force-rebuild for
        # per-project KG/Dev/Diagrams; code-graph-analyze --force for codegraph)
        # via vco_lib.schema_regenerate — NO new drop path.
        from . import schema_regenerate as sregen

        artifact_name = getattr(args, "artifact_name", None)
        result = {
            "folder": str(folder),
            "mode": "regenerate",
            "artifact_type": regenerate_type,
            "artifact_name": artifact_name,
            "action": "regenerate",
            "ok": False,
        }
        if regenerate_type not in sv.CANONICAL_VERSIONS:
            result["error"] = f"unknown artifact_type {regenerate_type!r}"
            print(json.dumps(result, indent=2))
            return 0 if not getattr(args, "strict", False) else 1
        if not artifact_name:
            result["error"] = "--regenerate requires --artifact-name"
            print(json.dumps(result, indent=2))
            return 0 if not getattr(args, "strict", False) else 1

        # --check is a DRY-RUN: it must NEVER perform the destructive recreate.
        # Report what WOULD be regenerated and return without touching
        # Weaviate. (Without this guard, `--regenerate <type> --check` would
        # fall through to the real drop+rebuild below — a dry-run that mutates
        # is both a contract violation and a data-safety footgun.)
        if getattr(args, "check", False):
            result["mode"] = "regenerate-check"
            result["ok"] = True
            result["would_regenerate"] = True
            result["note"] = (
                "dry-run: would drop+recreate+re-sync this collection from "
                "on-disk source. Re-run WITHOUT --check to perform it."
            )
            print(json.dumps(result, indent=2))
            return 0

        # The per-project recreate needs the raw project name to derive the
        # canonical class names (migrate-collections --name). Resolve from the
        # explicit --project-name, else the PROJECT_NAME env. The shared-KG and
        # codegraph branches don't need it.
        project_name = (
            getattr(args, "project_name", None)
            or os.environ.get("PROJECT_NAME")
            or None
        )
        # Orchestrator-wide artifacts (shared KG) are keyed project_id=NULL in
        # the registry (mirrors run_schema_migrations' ORCHESTRATOR_WIDE_TYPES).
        effective_pid = (
            None
            if regenerate_type in smr.ORCHESTRATOR_WIDE_TYPES
            else project_id
        )
        orchestrator_root = Path(__file__).resolve().parent.parent
        regen = sregen.regenerate_derived_collection(
            artifact_type=regenerate_type,
            artifact_name=artifact_name,
            folder=folder,
            db_path=db_path,
            project_id=effective_pid,
            project_name=project_name,
            env=os.environ,
            weaviate_url=weaviate_url,
            when=now_ms,
            orchestrator_root=orchestrator_root,
        )
        result.update(regen.to_dict())

        # C1: when the drop completed but re-ingest is unconfirmed
        # (reingest_incomplete), the registry row stays absent — so the NEXT
        # update would see NEVER_MATERIALIZED and silently register the EMPTY
        # collection at canonical without re-ingesting. Persist a
        # schema_reingest_incomplete_<slug> deferral so the gap is visible +
        # actionable (the kg-sync / code-graph-analyze remediation). Soft-fail:
        # a deferral-write error never crashes the regenerate result.
        result["reingest_deferral_written"] = False
        if regen.reingest_incomplete:
            try:
                entry = sregen.build_reingest_incomplete_entry(regen, folder)
                if entry is not None:
                    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked home.
                    from vco_lib import deferral_emit as _de
                    _de.emit(folder, entry)
                    result["reingest_deferral_written"] = True
            except Exception as exc:  # never block on a deferral write
                print(
                    f"[migrate-schema] reingest-incomplete deferral write "
                    f"failed (non-fatal): {exc}",
                    file=sys.stderr,
                )

        print(json.dumps(result, indent=2))
        # Soft-fail by default (the launcher reads `ok`/`refused`/`error` from
        # the JSON). --strict returns non-zero on a non-ok, non-refused outcome
        # so a manual CLI retry surfaces the failure.
        if getattr(args, "strict", False):
            return 0 if (regen.ok or regen.refused) else 1
        return 0

    check = bool(getattr(args, "check", False))
    # A1 (v0.2.74 migration delivery): resolve the codegraph prefix from the
    # SSOT (launcher.db project_codegraph_bindings, RO-URI) and pass EXPLICIT
    # artifact_names + an AUGMENTED env (CODE_GRAPH_PROJECT set) so the codegraph
    # loop iterates AND each edge subprocess resolves the same prefix — even
    # when this CLI surface is spawned with an env that lacks CODE_GRAPH_PROJECT
    # (the launcher's run_schema_migration_check now injects it, but a manual
    # retry / older launcher may not). Falls back to the project's PROJECT_NAME
    # env then the launcher.db name lookup.
    _cg_names, _cg_env, _cg_prefix = smr.resolve_codegraph_migration_inputs(
        os.environ,
        db_path=db_path,
        project_id=project_id,
        project_name=(
            getattr(args, "project_name", None) or os.environ.get("PROJECT_NAME")
        ),
    )
    report = smr.run_schema_migrations(
        db_path=db_path,
        project_id=project_id,
        migrations_dir=migrations_dir,
        deferral_report=None,
        weaviate_url=weaviate_url,
        env=_cg_env,
        check=check,
        artifact_names=_cg_names or None,
        now_ms=now_ms,
        project_root=migrations_dir.parent,
        include_orchestrator_wide=include_wide,
    )

    # CONCERN-1 (2026-06-16): actually PERSIST the findings to the project's
    # UPDATE_DEFERRED.md when NOT --check, mirroring _cmd_check_node_formats_
    # schema's _write_node_formats_migration_deferral. Without this, a real
    # per-project run with a genuinely stale collection would emit
    # pending_regenerate in JSON but write NO durable deferral — the Rust
    # toast's "deferral written to UPDATE_DEFERRED.md" claim would lie and a
    # future Claude session reading the file at session-start would see
    # nothing. --check stays no-write (dry-run). Uses the SAME builder the
    # install.py shim uses (schema_migration_runner.build_deferral_entries) so
    # the condition_ids are identical across both surfaces. Soft-fail: a
    # deferral-write error never crashes the probe.
    deferral_written = False
    if not check:
        entries = smr.build_deferral_entries(report)
        if entries:
            try:
                # v0.2.83 PLAN-v0283 WP-B2: batch-emit via the ONE locked home.
                from vco_lib import deferral_emit as _de
                _de.emit_entries(folder, entries)
                deferral_written = True
            except Exception as exc:  # never block the probe
                print(
                    f"[migrate-schema] deferral write failed (non-fatal): {exc}",
                    file=sys.stderr,
                )

    result = {
        "folder": str(folder),
        "mode": "check" if check else "apply",
        "registered": len(report.registered),
        "register_failed": len(report.register_failed),
        "up_to_date": len(report.up_to_date),
        "applied": len(report.applied),
        "refused": len(report.refused),
        "errors": len(report.errors),
        "pending_regenerate": report.pending_regenerate,
        "deferral_written": deferral_written,
        "planned": [
            {"artifact_type": a, "artifact_name": n, "edge": e}
            for (a, n, e) in report.planned
        ],
    }
    print(json.dumps(result, indent=2))
    return 0


def _now_ms_safe() -> int:
    """Milliseconds since epoch. Wrapped so tests can inject via --now-ms
    (Date.now()-style time isn't available in some sandboxes)."""
    return int(time.time() * 1000)


def _regenerate_node_formats(folder: Path) -> tuple[bool, str]:
    """Force-regenerate `knowledge/.node_formats.json` by re-running the
    KG-summary generator over every node. Returns (ok, detail).

    Returns ``ok=True`` ONLY when regeneration ACTUALLY happened — i.e.
    every node's generator run exited 0 AND the cache file's content
    changed. Otherwise ``(False, <reason>)`` so the caller DEFERS and
    leaves the cache intact (no data loss, no false "migrated" registry
    row).

    Why the content-change check (review B1): `generate-kg-summary.py`
    exits 0 WITHOUT writing when no summary backend is available
    (`select_backend() == "skip"` — the normal headless-update state: no
    `claude` CLI, no Ollama, no API key). A per-node exit-code check alone
    would then report success while nothing was regenerated, registering
    the artifact at the new schema version with a stale cache + no
    deferral — silently masking the migration. Hashing the cache
    before/after the `--force` pass detects this: a real regeneration
    rewrites entries (new ``generated_at`` at minimum), so an UNCHANGED
    file after force-regenerating >=1 node means the backend didn't run.

    Why all-or-defer (review C2): a PARTIAL regen (some nodes succeed,
    some genuinely error) would leave a MIXED-schema cache that the
    registry would then mark fully-migrated, so the stale entries never
    self-heal. Any per-node failure → defer; the registry stays at the old
    version and the next update retries until it fully converges.
    """
    gen = folder / ".claude" / "scripts" / "generate-kg-summary.py"
    if not gen.is_file():
        return (False, f"generator not found at {gen}")
    knowledge_dir = folder / "knowledge"
    nodes = sorted(p for p in knowledge_dir.rglob("*.md")) if knowledge_dir.is_dir() else []
    if not nodes:
        return (False, "no knowledge/**/*.md nodes to regenerate from")
    formats_path = knowledge_dir / ".node_formats.json"
    before_hash = _file_sha256(formats_path) if formats_path.exists() else ""
    py = sys.executable or "python3"
    failures = 0
    for node in nodes:
        try:
            proc = _subprocess_run_quiet(
                [py, str(gen), str(node), "--force"], cwd=folder
            )
            if proc != 0:
                failures += 1
        except Exception:
            failures += 1
    # C2: any failure → defer (never register a half-migrated cache).
    if failures:
        return (False, f"{failures}/{len(nodes)} node regenerations failed — deferring")
    # B1: all exited 0, but did anything actually get written? If the cache
    # is byte-identical, the backend was unavailable (exit-0-no-write) →
    # treat as NOT regenerated and defer.
    after_hash = _file_sha256(formats_path) if formats_path.exists() else ""
    if after_hash == before_hash:
        return (False,
                "generator ran but the cache did not change — summary backend "
                "unavailable (no claude CLI / Ollama / API key). Cache left "
                "intact; re-run when a backend is available.")
    return (True, f"regenerated {len(nodes)} node summaries")


def _subprocess_run_quiet(argv: list[str], cwd: Path) -> int:
    """Run a subprocess, returning its exit code; stdout/stderr suppressed.
    Isolated so tests can monkeypatch it without spawning real processes."""
    import subprocess
    return subprocess.run(
        argv, cwd=str(cwd),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode


def _write_formats_migration_deferral(
    folder: Path,
    canonical: int,
    detail: str,
    *,
    condition_id: str,
    title: str,
    sidecar_desc: str,
    derived_from: str,
    regen_cmd: str,
    log_tag: str,
) -> None:
    """INFO deferral: a regenerated-sidecar schema bumped but the migration
    action couldn't run inline. The existing cache is untouched; this tells
    the project's Claude how to regenerate manually. Generic home (v0.2.73
    M2) — per-artifact wrappers below supply the artifact-specific text."""
    try:
        from vco_lib.deferral_report import DeferralEntry
        from vco_lib import deferral_emit as _de
        # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
        _de.emit(folder, DeferralEntry(
            condition_id=condition_id,
            severity="info",
            title=title,
            detected=(
                f"`{sidecar_desc}` is at an older schema than "
                f"this orchestrator's canonical v{canonical}. It is a "
                f"regenerated cache (derived from {derived_from}), so it was "
                f"NOT overwritten. Inline regeneration did not run: {detail}."
            ),
            why_deferred=(
                "Regenerated-data files are re-derived, never blind-overwritten. "
                "When the generator backend is available, re-running it rewrites "
                "the cache in the new schema with no data loss."
            ),
            command_to_apply=(
                f"{regen_cmd}\n"
                f"# Then dismiss:\n"
                f"python -m vco_lib.project_init dismiss-deferral --folder {str(folder)!r} "
                f"--condition-id {condition_id}"
            ),
        ))
    except Exception as exc:  # never block the update flow
        print(f"[{log_tag}] deferral write failed (non-fatal): {exc}", file=sys.stderr)


def _write_node_formats_migration_deferral(folder: Path, canonical: int, detail: str) -> None:
    """INFO deferral `regenerated_data_schema_migration_pending`: the
    .node_formats schema bumped but regeneration couldn't run inline. Thin
    wrapper over the generic writer (v0.2.73 M2) — text unchanged."""
    _write_formats_migration_deferral(
        folder, canonical, detail,
        condition_id="regenerated_data_schema_migration_pending",
        title="KG-summary cache schema bumped — regeneration pending",
        sidecar_desc="knowledge/.node_formats.json",
        derived_from="your KG nodes",
        regen_cmd=(
            "# Regenerate every node summary in the new schema:\n"
            "for f in knowledge/**/*.md; do "
            "python .claude/scripts/generate-kg-summary.py \"$f\" --force; done"
        ),
        log_tag="node-formats",
    )


def _write_code_formats_migration_deferral(folder: Path, canonical: int, detail: str) -> None:
    """INFO deferral `code_formats_schema_migration_pending`: the
    .code_formats schema bumped but the keep-regenerated action (delete +
    let the generator rebuild) couldn't complete."""
    _write_formats_migration_deferral(
        folder, canonical, detail,
        condition_id="code_formats_schema_migration_pending",
        title="Code-summary sidecar schema bumped — regeneration pending",
        sidecar_desc=".claude/.code_formats.json",
        derived_from="your project's code-graph rows",
        regen_cmd=(
            "# Delete the old-schema sidecar and regenerate:\n"
            "rm .claude/.code_formats.json\n"
            "python .claude/scripts/generate-code-summary.py --project <name> --force"
        ),
        log_tag="code-formats",
    )


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
        "--index-type", default=None, choices=["hnsw", "hfresh"],
        help="(v0.2.73 FIX-D4, GATED) Target vectorIndexType for the "
             "project's 5 CODE-GRAPH collections. 'hnsw' (default) leaves "
             "code collections OUT of the migration plan (pre-D4 behaviour). "
             "'hfresh' opts them into a vector-preserving `copy` migrate "
             "(re-imports client vectors, NO re-embed) — but HFresh is "
             "PREVIEW and forces mandatory RQ compression; the integrator "
             "runs a 1.37 scratch-test before this ever becomes the default.",
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

    # v0.2.73 FIX-C: orphan code-collection cleanup (consented) ------------
    p_det = sub.add_parser(
        "detect-orphan-code-collections",
        help=(
            "READ-ONLY two-source orphan CODE-collection detector "
            "(binding-exclusion seed). With --project-folder, emits the "
            "consented `orphan_code_collections_detected` deferral. Never "
            "drops anything."
        ),
    )
    p_det.add_argument("--weaviate-url", default=None,
                       help="Override Weaviate URL (default: WEAVIATE_URL env "
                            "or http://localhost:8081).")
    p_det.add_argument("--volume-dir", default=None,
                       help="Weaviate volume dir — enables the on-disk "
                            "stranded-segment source (the big reclaim). "
                            "Omit for live-schema-only detection.")
    p_det.add_argument("--project-folder", default=None,
                       help="Emit the consented deferral into "
                            "<folder>/.claude/context/UPDATE_DEFERRED.md.")
    p_det.add_argument("--json", action="store_true",
                       help="Emit a single JSON object on stdout.")
    p_det.set_defaults(func=_cmd_detect_orphan_code_collections)

    p_dropo = sub.add_parser(
        "drop-orphan-code-collections",
        help=(
            "CONSENTED drop of LIVE-schema orphan code classes. RE-VALIDATES "
            "against current bindings + live schema at run time. Requires "
            "--confirm."
        ),
    )
    p_dropo.add_argument("--weaviate-url", default=None,
                         help="Override Weaviate URL.")
    p_dropo.add_argument("--confirm", action="store_true",
                         help="Required — this is a destructive operation.")
    p_dropo.add_argument("--json", action="store_true",
                         help="Emit a single JSON object on stdout.")
    p_dropo.set_defaults(func=_cmd_drop_orphan_code_collections)

    p_recl = sub.add_parser(
        "reclaim-stranded-code-segments",
        help=(
            "FILESYSTEM-LEVEL reclaim of on-disk stranded code segment dirs "
            "(class already gone from live schema). REFUSES while Weaviate is "
            "up. Requires --confirm AND --i-understand-filesystem-level."
        ),
    )
    p_recl.add_argument("--volume-dir", required=True,
                        help="Weaviate volume dir to reclaim from.")
    p_recl.add_argument("--weaviate-url", default=None,
                        help="Override Weaviate URL (used only to REFUSE when "
                             "Weaviate is still up).")
    p_recl.add_argument("--confirm", action="store_true",
                        help="Required — destructive filesystem operation.")
    p_recl.add_argument("--i-understand-filesystem-level",
                        dest="i_understand_filesystem_level",
                        action="store_true",
                        help="Second required flag — acknowledges this deletes "
                             "on-disk dirs (Weaviate MUST be stopped first).")
    p_recl.add_argument("--project-folder", dest="project_folder", default=None,
                        help="Project folder holding the detect-time live-prefix "
                             "snapshot (.claude/state/). REQUIRED to remove "
                             "anything — without it the reclaim refuses (SEV-3 "
                             "#1 conservative guard).")
    p_recl.add_argument("--json", action="store_true",
                        help="Emit a single JSON object on stdout.")
    p_recl.set_defaults(func=_cmd_reclaim_stranded_code_segments)

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
        "--write-env", action="store_true", dest="write_env",
        help=(
            "A2 (v0.2.38): write .claude/env + .claude/settings.json env "
            "block after copying bundle files.  Uses launcher.db when "
            "available; falls back to a default bundle derived from "
            "--orchestrator-root and --project-name (or folder basename) "
            "when the launcher is absent.  Makes install-bundle standalone-"
            "usable for OSS-developer / fork-integrator workflows."
        ),
    )
    p_bundle.add_argument(
        "--project-name", default=None, dest="project_name",
        help=(
            "Raw project display name used for KG_COLLECTION / "
            "CODE_GRAPH_PROJECT derivation when --write-env is set and "
            "the launcher DB is absent.  Defaults to the folder basename."
        ),
    )
    p_bundle.add_argument(
        "--safe-add", action="store_true", dest="safe_add",
        help=(
            "v0.2.63: per-add opt-in. Skip recording the project-root .env "
            "merge (the launcher writes a .env.vco.reference sidecar + this "
            "emits a safe_add_skipped_env_merge deferral) and append "
            "VCO-created paths to the LOCAL-only .git/info/exclude (never the "
            "tracked .gitignore). Default OFF = auto-merge as before."
        ),
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
            "deferral-emission sites in this module."
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

    # check-bundle-resume (NEW-7 / B1, v0.2.53) ---------------------------
    # Surfaces the bundle-update resume sentinel for the launcher's
    # session-start check. Reads
    # ``<folder>/.claude/state/bundle-update-resume-needed.json`` and
    # emits a single JSON line describing the state. Exit code 0 always
    # (this is a probe, not a mutating command). The launcher uses the
    # JSON shape to decide whether to render an "Update bundle (resume)"
    # nudge in the per-project settings page.
    p_check_resume = sub.add_parser(
        "check-bundle-resume",
        help=(
            "Probe a project for a stale bundle-update resume sentinel. "
            "Emits {'resume_needed': bool, 'sentinel': {...}|null} on "
            "stdout. Exit 0 even when no sentinel is present."
        ),
    )
    p_check_resume.add_argument(
        "--folder", required=True,
        help="Target user-project folder.",
    )
    p_check_resume.set_defaults(func=_cmd_check_bundle_resume)

    # v0.2.57: schema-version gate for the regenerated KG-summary cache
    # (knowledge/.node_formats.json). The launcher subprocesses this during
    # update — mirrors the migrate-collections schema-drift probe but for the
    # kg_node_formats artifact. Registers on first materialize; regenerates
    # (or defers) on a canonical schema bump. NEVER blind-overwrites.
    p_check_nf = sub.add_parser(
        "check-node-formats-schema",
        help=(
            "Schema-version gate for knowledge/.node_formats.json "
            "(artifact_type kg_node_formats). Registers at canonical on first "
            "materialize; on a schema bump, regenerates the cache (or writes "
            "an info deferral). Emits a JSON result; exit 0 (probe)."
        ),
    )
    p_check_nf.add_argument(
        "--folder", required=True,
        help="Target user-project folder.",
    )
    p_check_nf.add_argument(
        "--db", default=None,
        help="launcher.db path (defaults to the canonical ~/.vct/launcher.db).",
    )
    p_check_nf.add_argument(
        "--project-id", default=None,
        help="Project id/slug for the artifact_schema_versions row "
             "(optional; registry keys on COALESCE(project_id,'')).",
    )
    p_check_nf.add_argument(
        "--now-ms", default=0, type=int,
        help="Override materialized_at epoch-ms (testing; 0 = wall clock).",
    )
    p_check_nf.set_defaults(func=_cmd_check_node_formats_schema)

    # v0.2.73 M2: same gate for the code-summary sidecar
    # (.claude/.code_formats.json, artifact_type code_formats). Bump action
    # is keep-regenerated (delete + let generate-code-summary.py rebuild).
    p_check_cf = sub.add_parser(
        "check-code-formats-schema",
        help=(
            "Schema-version gate for .claude/.code_formats.json "
            "(artifact_type code_formats). Registers at canonical on first "
            "materialize; on a schema bump, deletes the old-schema sidecar "
            "so the generator rebuilds it (or writes an info deferral). "
            "Emits a JSON result; exit 0 (probe)."
        ),
    )
    p_check_cf.add_argument(
        "--folder", required=True,
        help="Target user-project folder.",
    )
    p_check_cf.add_argument(
        "--db", default=None,
        help="launcher.db path (defaults to the canonical ~/.vct/launcher.db).",
    )
    p_check_cf.add_argument(
        "--project-id", default=None,
        help="Project id/slug for the artifact_schema_versions row "
             "(optional; registry keys on COALESCE(project_id,'')).",
    )
    p_check_cf.add_argument(
        "--now-ms", default=0, type=int,
        help="Override materialized_at epoch-ms (testing; 0 = wall clock).",
    )
    p_check_cf.set_defaults(func=_cmd_check_code_formats_schema)

    # v0.2.60: version-gated schema-migration runner (verified no-op today).
    p_migrate = sub.add_parser(
        "migrate-schema",
        help=(
            "Run the version-gated schema-migration runner over migrations/ "
            "(no-op today). --check dry-runs; --regenerate <type> "
            "--artifact-name <name> is the launcher's 'Regenerate now' choice. "
            "Emits a JSON MigrationRunReport summary; exit 0 (probe)."
        ),
    )
    p_migrate.add_argument(
        "--folder", required=True,
        help="Target user-project folder (the project whose per-project "
             "artifacts are migrated). Migration EDGES are read from the "
             "orchestrator clone's migrations/, not this folder.",
    )
    p_migrate.add_argument(
        "--db", default=None,
        help="launcher.db path (defaults to the canonical ~/.vct/launcher.db).",
    )
    p_migrate.add_argument(
        "--project-id", default=None,
        help="Project id/slug for the per-project artifact_schema_versions "
             "rows (the launcher passes the project's real id; "
             "orchestrator-wide rows are keyed NULL).",
    )
    p_migrate.add_argument(
        "--migrations-dir", default=None,
        help="Override the migrations/ directory (default: the orchestrator "
             "clone root beside vco_lib). Testing / manual use.",
    )
    p_migrate.add_argument(
        "--include-orchestrator-wide", action="store_true",
        dest="include_orchestrator_wide",
        help="Also migrate the orchestrator-wide artifacts (shared KG + "
             "Layer-5 launcher/global shapes, keyed project_id=NULL). Set by "
             "the ROOT update; OFF for a non-root project's bundle update.",
    )
    p_migrate.add_argument(
        "--check", action="store_true",
        help="Dry-run: plan only, no mutation, no registry write.",
    )
    p_migrate.add_argument(
        "--regenerate", default=None, metavar="ARTIFACT_TYPE",
        help="POLICY STEP 3 'Regenerate now' for ONE derived collection.",
    )
    p_migrate.add_argument(
        "--artifact-name", default=None,
        help="Live class name for --regenerate (e.g. the shared KG class).",
    )
    p_migrate.add_argument(
        "--project-name", default=None,
        help="Raw project name for the per-project --regenerate path "
             "(derives the canonical KG/Dev/Diagrams class names via "
             "migrate-collections --name). Falls back to $PROJECT_NAME. "
             "Unused by the shared-KG / codegraph regenerate branches.",
    )
    p_migrate.add_argument(
        "--strict", action="store_true",
        help="Return non-zero on a regenerate validation error (default soft).",
    )
    p_migrate.add_argument(
        "--now-ms", default=0, type=int,
        help="Override materialized_at epoch-ms (testing; 0 = wall clock).",
    )
    p_migrate.set_defaults(func=_cmd_migrate_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
