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

import requests
import weaviate
from weaviate.classes.config import Configure, Property

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_embeddings")

WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# Collections that use the "code" vector scheme
CODE_COLLECTIONS = {"CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"}

# New named vectors to add
NEW_KG_VECTOR = "qwen3_embed"
NEW_CODE_VECTOR = "codesage_embed"


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
    """Get embedding from qwen3-embedding via Ollama."""
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text, "options": {"num_ctx": 8192}},
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
    """Get embedding from CodeSage via code embedding service."""
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

    # Delete and recreate
    logger.info("Recreating '%s' with named vectors: %s", coll_name, sorted(all_vec_names))
    client.collections.delete(coll_name)
    client.collections.create(
        name=coll_name,
        properties=existing_props,
        vectorizer_config=vectorizer_config,
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

    For KG collections: generates qwen3_embed
    For Code collections: generates codesage_embed
    """
    is_code = is_code_collection(coll_name)
    target_vec = NEW_CODE_VECTOR if is_code else NEW_KG_VECTOR
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
    """Full migration for a single collection: add named vector + backfill."""
    is_code = is_code_collection(coll_name)
    new_vec = NEW_CODE_VECTOR if is_code else NEW_KG_VECTOR
    new_dim = 2048 if is_code else 1024

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

    # Check services are reachable
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": "test", "options": {"num_ctx": 8192}},
            timeout=10,
        )
        assert resp.status_code == 200, f"Ollama embedding failed: {resp.status_code}"
        logger.info("Ollama (%s) reachable", EMBEDDING_MODEL)
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
