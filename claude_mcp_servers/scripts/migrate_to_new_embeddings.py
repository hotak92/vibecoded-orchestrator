#!/usr/bin/env python3
# Manual migration tool — see docs/CONFIGURATION.md
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Migrate Weaviate collections to support new embedding models.

This script adds new named vectors to existing collections alongside old ones:
  KG collections:    qwen3_embed (1024d)    alongside any legacy ollama_embed
  Code collections:  codesage_embed (2048d) alongside any legacy ollama_code_embed

The migration preserves all existing data and vectors — it only adds the new
named vector slots and backfills them with new embeddings.

Usage:
    # Migrate all collections (KG + code):
    python migrate_to_new_embeddings.py --all

    # Migrate specific collection:
    python migrate_to_new_embeddings.py --collection KnowledgeGraph

    # Dry run (show what would happen):
    python migrate_to_new_embeddings.py --all --dry-run

    # Backfill only (collections already have the named vector slots):
    python migrate_to_new_embeddings.py --all --backfill-only

Environment:
    WEAVIATE_URL            http://localhost:8081
    GRPC_PORT               50052
    OLLAMA_URL              http://localhost:11435
    EMBEDDING_MODEL         qwen3-embedding:0.6b
    CODE_EMBED_SERVICE_URL  http://localhost:11440
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import requests
import weaviate
from weaviate.classes.config import Configure, Property

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_embeddings")

WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# v0.2.18: prefer EmbeddingService for catalog discovery + per-active-
# backend dispatch. Import is graceful so this script still works on a
# half-installed venv (the inline fallbacks below preserve the
# pre-v0.2.18 hardcoded behaviour).
try:
    _vco_lib_parent = Path(__file__).resolve().parent.parent.parent
    if str(_vco_lib_parent) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent))
    from vco_lib.embedding_service import (
        EmbeddingService,
        NoEmbeddingBackendError,
    )
    HAS_EMBEDDING_SERVICE = True
except Exception as _exc:  # pragma: no cover (half-install case)
    logger.warning(
        "vco_lib.embedding_service not importable (%s); falling back to "
        "hardcoded slot/model names. Re-run install.py --update.", _exc
    )
    HAS_EMBEDDING_SERVICE = False
    EmbeddingService = None  # type: ignore[assignment]
    NoEmbeddingBackendError = Exception  # type: ignore[assignment]


# Collections that use the "code" vector scheme
CODE_COLLECTIONS = {"CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"}

# v0.2.18: legacy hardcoded slot names — only used when EmbeddingService
# isn't importable. With EmbeddingService present we read the active
# slot from the catalog at run-time (via `_active_kg_slot()` /
# `_active_code_slot()`), so a user with ACTIVE_EMBEDDING=openai migrates
# their collections to `openai_text_embed` / `openai_code_embed`.
NEW_KG_VECTOR = "qwen3_embed"
NEW_CODE_VECTOR = "codesage_embed"


def _active_kg_slot() -> str:
    """Return the slot name to migrate KG collections TO.

    With EmbeddingService: the active text slot (resolved from env). So
    `ACTIVE_EMBEDDING=openai` migrates to `openai_text_embed`,
    `ACTIVE_EMBEDDING=arctic` to `arctic2_embed`, default `qwen3` to
    `qwen3_embed`.
    Without EmbeddingService: hardcoded `qwen3_embed` (pre-v0.2.18).
    """
    if not HAS_EMBEDDING_SERVICE:
        return NEW_KG_VECTOR
    try:
        svc = EmbeddingService.for_project()
        return svc.text_vector_slot
    except NoEmbeddingBackendError:
        return NEW_KG_VECTOR


def _active_code_slot() -> str:
    """Return the slot name to migrate code collections TO.

    With EmbeddingService: the active code slot. Without: hardcoded
    `codesage_embed`.
    """
    if not HAS_EMBEDDING_SERVICE:
        return NEW_CODE_VECTOR
    try:
        svc = EmbeddingService.for_project()
        return svc.code_vector_slot
    except NoEmbeddingBackendError:
        return NEW_CODE_VECTOR


def _discover_text_catalog() -> list:
    """Return reachable text-embedding models, or empty list on no service.

    Used by `--list` to surface what models are actually available on
    this machine — replaces the pre-v0.2.18 assumption that only
    qwen3-embedding existed.
    """
    if not HAS_EMBEDDING_SERVICE:
        return []
    try:
        return EmbeddingService.discover_text_models()
    except Exception as e:
        logger.warning("Text model discovery failed: %s", e)
        return []


def _discover_code_catalog() -> list:
    """Return reachable code-embedding models, or empty list on no service."""
    if not HAS_EMBEDDING_SERVICE:
        return []
    try:
        return EmbeddingService.discover_code_models()
    except Exception as e:
        logger.warning("Code model discovery failed: %s", e)
        return []


def get_client() -> weaviate.WeaviateClient:
    client = weaviate.connect_to_local(
        host=WEAVIATE_URL.replace("http://", "").split(":")[0],
        port=int(WEAVIATE_URL.split(":")[-1]),
        grpc_port=GRPC_PORT,
    )
    return client


def is_code_collection(name: str) -> bool:
    """Check if collection uses code scheme (handles project prefixes)."""
    for code_coll in CODE_COLLECTIONS:
        if name.endswith(code_coll):
            return True
    return name in CODE_COLLECTIONS


def get_text_embedding(text: str) -> list[float] | None:
    """Get embedding from the active text backend.

    v0.2.18: routes through EmbeddingService.embed_text() — picks
    Ollama / OpenAI / etc. based on env. Falls back to direct Ollama
    call (pre-v0.2.18 hardcode) when service unavailable.
    """
    if HAS_EMBEDDING_SERVICE:
        try:
            svc = EmbeddingService.for_project()
            return svc.embed_text(text)
        except NoEmbeddingBackendError as e:
            logger.warning("EmbeddingService unavailable for text embed: %s", e)
        except Exception as e:
            logger.warning("EmbeddingService text embed failed (%s); falling back to inline Ollama", e)
    # Legacy fallback: direct Ollama call.
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={
                "model": os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
                "prompt": text,
                "options": {"num_ctx": 8192},
            },
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()["embedding"]
        logger.warning("Text embedding failed: HTTP %s", resp.status_code)
        return None
    except Exception as e:
        logger.warning("Text embedding error: %s", e)
        return None


def get_code_embedding(text: str) -> list[float] | None:
    """Get embedding from the active code backend.

    v0.2.18: routes through EmbeddingService.embed_code(). Falls back
    to direct CodeEmbed HTTP call when service unavailable.
    """
    if HAS_EMBEDDING_SERVICE:
        try:
            svc = EmbeddingService.for_project()
            return svc.embed_code(text)
        except NoEmbeddingBackendError as e:
            logger.warning("EmbeddingService unavailable for code embed: %s", e)
        except Exception as e:
            logger.warning("EmbeddingService code embed failed (%s); falling back to inline CodeEmbed", e)
    # Legacy fallback: direct CodeEmbed call.
    try:
        resp = requests.post(
            f"{CODE_EMBED_SERVICE_URL}/api/embeddings",
            json={"model": "", "prompt": text},
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()["embedding"]
        logger.warning("Code embedding failed: HTTP %s", resp.status_code)
        return None
    except Exception as e:
        logger.warning("Code embedding error: %s", e)
        return None


def collection_has_named_vector(client: weaviate.WeaviateClient, coll_name: str, vec_name: str) -> bool:
    """Check if a collection already has a specific named vector configured."""
    try:
        coll = client.collections.get(coll_name)
        config = coll.config.get()
        if not config.vector_config:
            return False
        for vc in config.vector_config:
            if vc == vec_name:
                return True
        return False
    except Exception:
        return False


def add_named_vector_to_collection(
    client: weaviate.WeaviateClient,
    coll_name: str,
    vec_name: str,
    vec_dim: int,
    dry_run: bool = False,
) -> bool:
    """Add a new named vector slot to a collection by recreating it.

    WARNING: This deletes and recreates the collection. All data must be
    backed up first (the caller handles backup/restore).
    """
    if dry_run:
        logger.info("[DRY RUN] Would add named vector '%s' (%dd) to '%s'", vec_name, vec_dim, coll_name)
        return True

    coll = client.collections.get(coll_name)
    config = coll.config.get()

    # Determine the legacy vector name for flat-vector collections
    _is_code = is_code_collection(coll_name)
    _legacy_vec_name = "ollama_code_embed" if _is_code else "ollama_embed"

    # Read all objects with vectors
    logger.info("Reading all objects from '%s'...", coll_name)
    all_objects = []
    for obj in coll.iterator(include_vector=True):
        if isinstance(obj.vector, dict):
            vec_dict = obj.vector
        elif obj.vector:
            # Flat vector → map to legacy named vector
            vec_dict = {_legacy_vec_name: obj.vector}
        else:
            vec_dict = {}
        all_objects.append({
            "properties": dict(obj.properties),
            "vector": vec_dict,
        })
    logger.info("Read %d objects from '%s'", len(all_objects), coll_name)

    # Clone properties
    def _clone_prop(prop_obj) -> Property:
        nested = getattr(prop_obj, "nested_properties", None)
        kwargs = {
            "name": prop_obj.name,
            "data_type": prop_obj.data_type,
            "description": getattr(prop_obj, "description", None),
        }
        if nested:
            kwargs["nested_properties"] = [_clone_prop(np) for np in nested]
        return Property(**kwargs)

    existing_props = [_clone_prop(prop) for prop in config.properties]

    # Build vector config — keep existing + add new
    existing_vec_names = set()
    if config.vector_config:
        for vc in config.vector_config:
            existing_vec_names.add(vc)

    # Always include the legacy vector name so old embeddings are preserved
    all_vec_names = existing_vec_names | {vec_name, _legacy_vec_name}
    vectorizer_config = [
        Configure.NamedVectors.none(name=vn)
        for vn in sorted(all_vec_names)
    ]

    # Delete and recreate. Preserve `index_null_state=True` so the
    # MCP `_stale_filter()` (`valid_until is_none(True) | > now`) keeps
    # working on KG collections post-migration. CANNOT be retro-added
    # via Reconfigure on Weaviate ≤1.30, so we must include it here.
    # Audit finding 2026-04-30 (Code-M2 / KG schema gotchas).
    logger.info("Recreating '%s' with named vectors: %s", coll_name, sorted(all_vec_names))
    client.collections.delete(coll_name)
    client.collections.create(
        name=coll_name,
        properties=existing_props,
        vectorizer_config=vectorizer_config,
        inverted_index_config=Configure.inverted_index(index_null_state=True),
    )

    # Re-insert objects with existing vectors
    new_coll = client.collections.get(coll_name)
    inserted = 0
    for obj_data in all_objects:
        vectors = {}
        for vname, vec in obj_data["vector"].items():
            if vec and vname in all_vec_names:
                vectors[vname] = vec
        new_coll.data.insert(
            properties=obj_data["properties"],
            vector=vectors if vectors else None,
        )
        inserted += 1
        if inserted % 100 == 0:
            logger.info("Re-inserted %d/%d objects", inserted, len(all_objects))

    logger.info("Recreated '%s': %d objects with named vectors %s", coll_name, inserted, sorted(all_vec_names))
    return True


def backfill_new_embeddings(
    client: weaviate.WeaviateClient,
    coll_name: str,
    dry_run: bool = False,
    batch_size: int = 50,
) -> dict:
    """Generate new embeddings for all objects in a collection.

    v0.2.18: target slot is resolved dynamically from EmbeddingService —
    so a project running ACTIVE_EMBEDDING=openai migrates to
    `openai_text_embed`/`openai_code_embed` instead of the hardcoded
    qwen3/codesage names.
    """
    is_code = is_code_collection(coll_name)
    target_vec = _active_code_slot() if is_code else _active_kg_slot()
    embed_fn = get_code_embedding if is_code else get_text_embedding

    if dry_run:
        coll = client.collections.get(coll_name)
        count = sum(1 for _ in coll.iterator())
        logger.info("[DRY RUN] Would backfill %d objects in '%s' with '%s'", count, coll_name, target_vec)
        return {"collection": coll_name, "target": target_vec, "total": count, "dry_run": True}

    coll = client.collections.get(coll_name)
    total = 0
    updated = 0
    skipped = 0
    errors = 0

    for obj in coll.iterator(include_vector=True):
        total += 1

        # Skip if already has the new vector
        existing = obj.vector if isinstance(obj.vector, dict) else {}
        if target_vec in existing and existing[target_vec]:
            skipped += 1
            continue

        content = obj.properties.get("content", "")
        if not content:
            skipped += 1
            continue

        # Truncate for embedding
        text = content[:16000]  # conservative limit
        vec = embed_fn(text)
        if vec is None:
            errors += 1
            continue

        try:
            coll.data.update(
                uuid=obj.uuid,
                vector={target_vec: vec},
            )
            updated += 1
        except Exception as e:
            logger.warning("Failed to update %s: %s", obj.uuid, e)
            errors += 1

        if (updated + errors) % batch_size == 0:
            logger.info(
                "Backfill '%s': %d/%d (updated=%d, skipped=%d, errors=%d)",
                coll_name, total, total, updated, skipped, errors,
            )

    result = {
        "collection": coll_name,
        "target_vector": target_vec,
        "total": total,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info("Backfill complete: %s", json.dumps(result))
    return result


def migrate_collection(
    client: weaviate.WeaviateClient,
    coll_name: str,
    dry_run: bool = False,
    backfill_only: bool = False,
) -> dict:
    """Full migration for a single collection: add named vector + backfill.

    v0.2.18: slot name + dim are resolved from EmbeddingService for the
    project's currently-active model. Pre-v0.2.18 hardcoded
    qwen3_embed(1024) / codesage_embed(2048).
    """
    is_code = is_code_collection(coll_name)
    new_vec = _active_code_slot() if is_code else _active_kg_slot()
    # Dim is resolved from EmbeddingService when possible — otherwise
    # fall back to the legacy defaults that match the legacy slot names.
    new_dim = 2048 if is_code else 1024
    if HAS_EMBEDDING_SERVICE:
        try:
            svc = EmbeddingService.for_project()
            new_dim = svc.code_dim if is_code else svc.text_dim
        except Exception:
            pass  # fall back to legacy default

    # Step 1: Add named vector if not present
    if not backfill_only:
        has_vec = collection_has_named_vector(client, coll_name, new_vec)
        if has_vec:
            logger.info("'%s' already has named vector '%s', skipping schema change", coll_name, new_vec)
        else:
            add_named_vector_to_collection(client, coll_name, new_vec, new_dim, dry_run=dry_run)

    # Step 2: Backfill new embeddings
    return backfill_new_embeddings(client, coll_name, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Migrate Weaviate collections to new embedding models")
    parser.add_argument("--all", action="store_true", help="Migrate all collections")
    parser.add_argument("--collection", type=str, help="Migrate specific collection")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changes")
    parser.add_argument("--backfill-only", action="store_true", help="Only backfill embeddings (skip schema changes)")
    parser.add_argument("--list", action="store_true", help="List all collections and their vector config")
    args = parser.parse_args()

    client = get_client()

    if args.list:
        # v0.2.18: surface the dynamic catalog so operators can see what
        # they could migrate TO, in addition to current collection state.
        text_models = _discover_text_catalog()
        code_models = _discover_code_catalog()
        if text_models or code_models:
            print("=== Reachable Embedding Models (v0.2.18 catalog) ===")
            for m in text_models:
                marker = "✓" if m.available_now else "✗"
                print(f"  TEXT  {marker} {m.id:40s} slot={m.slot:20s} dim={m.dim} backend={m.backend}")
            for m in code_models:
                marker = "✓" if m.available_now else "✗"
                print(f"  CODE  {marker} {m.id:40s} slot={m.slot:20s} dim={m.dim} backend={m.backend}")
            print(f"\n  Active text slot (migration target): {_active_kg_slot()}")
            print(f"  Active code slot (migration target): {_active_code_slot()}")
            print()
        print("=== Existing Collections ===")
        for name in sorted(client.collections.list_all()):
            coll = client.collections.get(name)
            config = coll.config.get()
            vc = config.vector_config
            vecs = list(vc.keys()) if vc and hasattr(vc, 'keys') else (list(vc) if vc else ["default"])
            count = sum(1 for _ in coll.iterator())
            scheme = "code" if is_code_collection(name) else "kg"
            print(f"  {name} ({scheme}): {count} objects, vectors: {vecs}")
        client.close()
        return

    if not args.all and not args.collection:
        parser.error("Specify --all or --collection NAME")

    # Check services are reachable. v0.2.18: prefer EmbeddingService's
    # construction probe (which checks every configured backend in one
    # call) over the legacy per-backend probes below.
    if HAS_EMBEDDING_SERVICE:
        try:
            svc = EmbeddingService.for_project()
            logger.info(
                "EmbeddingService ready (text=%s slot=%s, code=%s slot=%s)",
                svc.text_model_id, svc.text_vector_slot,
                svc.code_model_id, svc.code_vector_slot,
            )
        except NoEmbeddingBackendError as e:
            logger.error("No embedding backend reachable: %s", e)
            sys.exit(1)
    else:
        # Legacy fallback (pre-v0.2.18 path).
        embedding_model_for_probe = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": embedding_model_for_probe, "prompt": "test", "options": {"num_ctx": 8192}},
                timeout=10,
            )
            assert resp.status_code == 200, f"Ollama embedding failed: {resp.status_code}"
            logger.info("Ollama (%s) reachable", embedding_model_for_probe)
        except Exception as e:
            logger.error("Ollama not reachable: %s", e)
            sys.exit(1)

        try:
            resp = requests.get(f"{CODE_EMBED_SERVICE_URL}/health", timeout=10)
            if resp.status_code == 200:
                logger.info("Code embedding service reachable: %s", resp.json())
            else:
                logger.warning("Code embedding service returned %s — code collections will be skipped", resp.status_code)
        except Exception:
            logger.warning("Code embedding service not reachable at %s — code collections will be skipped", CODE_EMBED_SERVICE_URL)

    results = []

    if args.all:
        for name in sorted(client.collections.list_all()):
            try:
                result = migrate_collection(client, name, dry_run=args.dry_run, backfill_only=args.backfill_only)
                results.append(result)
            except Exception as e:
                logger.error("Failed to migrate '%s': %s", name, e)
                results.append({"collection": name, "error": str(e)})
    else:
        result = migrate_collection(client, args.collection, dry_run=args.dry_run, backfill_only=args.backfill_only)
        results.append(result)

    print("\n=== Migration Summary ===")
    for r in results:
        print(json.dumps(r, indent=2))

    client.close()


if __name__ == "__main__":
    main()
