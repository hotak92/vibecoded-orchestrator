#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# NEW-11 (2026-05-28): one-shot repair for malformed typed_links in legacy KG collections.
"""
Repair malformed typed_links in Weaviate KG collections.

Pre-canonicalization writers emitted typed_links as a JSON list of strings
("relation::target" form) rather than the list-of-objects shape that Weaviate's
gRPC serialiser requires.  The malformed rows cause the whole collection to be
unreadable via gRPC:

    weaviate.exceptions.WeaviateQueryError: Query call with protocol GRPC search
    failed with message creating primitive value for typed_links:
    proto: invalid type: []interface {}

This script walks every KG collection via REST GraphQL (which tolerates the
malformed field), identifies rows with bad typed_links, converts them to the
canonical shape, and writes them back via the Weaviate Python client's
data.update().

Usage:
    python repair_kg_typed_links.py
    python repair_kg_typed_links.py --collections MyProject_KnowledgeGraph LegacyKG
    python repair_kg_typed_links.py --dry-run

Environment:
    WEAVIATE_URL    http://localhost:8081  (default)
    GRPC_PORT       50052                 (default)

Exit codes:
    0   all rows OK or successfully repaired
    1   one or more rows could not be repaired
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import requests
import weaviate

# v0.2.91 (Decision #21): honors the global VCO_LOG_LEVEL pref via the
# shared vco_lib helper instead of a hardcoded INFO level. Bare import —
# vco_lib is a SHIPPED, editable-installed part of every healthy install,
# so a failed import here already fails loudly (ImportError), matching the
# "no silent-fallback on vco_lib imports" discipline used elsewhere.
from vco_lib.log_setup import configure_logging

configure_logging(format="%(levelname)s %(message)s")
logger = logging.getLogger("repair_typed_links")

WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

# Properties fetched per object via REST GraphQL.
_GRAPHQL_FIELDS = "title file_path typed_links"

# Maximum objects to fetch per REST GraphQL page.
_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Shape normalisation (mirrors _normalize_typed_links in sync_knowledge_graph)
# ---------------------------------------------------------------------------

def _is_canonical(typed_links: Any) -> bool:
    """Return True if typed_links already has the canonical list-of-objects shape."""
    if not isinstance(typed_links, list):
        return False
    for item in typed_links:
        if not isinstance(item, dict):
            return False
        if "relation_type" not in item or "target_title" not in item:
            return False
    return True


def _normalize(typed_links: Any) -> tuple[list, bool]:
    """Convert typed_links to the canonical shape.

    Returns:
        (normalized_list, was_changed) — if was_changed is False the caller
        can skip the write-back.
    """
    if typed_links is None or typed_links == []:
        return [], False

    if not isinstance(typed_links, list):
        logger.warning("typed_links is not a list (type=%s) — dropping", type(typed_links).__name__)
        return [], True

    normalized: list = []
    changed = False
    for item in typed_links:
        if isinstance(item, dict):
            if "relation_type" in item and "target_title" in item:
                normalized.append(item)
            else:
                # Dict with wrong keys — coerce if possible
                logger.warning("typed_links dict has unexpected keys %s — dropping item", list(item.keys()))
                changed = True
        elif isinstance(item, str):
            changed = True
            if "::" in item:
                relation, _, target = item.partition("::")
                normalized.append({"relation_type": relation.strip(), "target_title": target.strip()})
            else:
                # Plain string with no separator — treat as relatedTo
                logger.warning("typed_links string %r has no '::' separator — storing as relatedTo", item)
                normalized.append({"relation_type": "relatedTo", "target_title": item.strip()})
        else:
            logger.warning("typed_links item type %s unexpected — skipping", type(item).__name__)
            changed = True

    return normalized, changed


# ---------------------------------------------------------------------------
# REST GraphQL helpers (bypass gRPC which fails on malformed rows)
# ---------------------------------------------------------------------------

def _graphql_post(query: str) -> dict:
    """POST a GraphQL query to Weaviate's REST endpoint."""
    resp = requests.post(
        f"{WEAVIATE_URL}/v1/graphql",
        json={"query": query},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_all_objects(collection_name: str) -> list[dict]:
    """Fetch all objects from a collection using REST GraphQL pagination.

    Returns list of dicts with keys: _id (uuid str), title, file_path, typed_links.
    """
    objects: list[dict] = []
    after_cursor: str | None = None

    while True:
        after_clause = f', after: "{after_cursor}"' if after_cursor else ""
        query = f"""
        {{
          Get {{
            {collection_name}(
              limit: {_PAGE_SIZE}
              {after_clause}
            ) {{
              _additional {{ id }}
              {_graphql_fields}
            }}
          }}
        }}
        """
        try:
            result = _graphql_post(query)
        except requests.HTTPError as exc:
            logger.error("GraphQL request failed for %s: %s", collection_name, exc)
            raise

        data = result.get("data", {}).get("Get", {}).get(collection_name, [])
        if not data:
            break

        objects.extend(data)
        if len(data) < _PAGE_SIZE:
            break
        # Advance cursor to last item
        after_cursor = data[-1]["_additional"]["id"]

    return objects


# ---------------------------------------------------------------------------
# Repair logic
# ---------------------------------------------------------------------------

def _repair_collection(
    client: weaviate.WeaviateClient,
    collection_name: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Repair typed_links in one collection.

    Returns:
        (rows_checked, rows_fixed, rows_skipped_error)
    """
    logger.info("Inspecting collection: %s", collection_name)
    try:
        objects = _fetch_all_objects(collection_name)
    except Exception as exc:
        logger.error("Failed to fetch objects from %s: %s", collection_name, exc)
        return 0, 0, 0

    checked = len(objects)
    fixed = 0
    errors = 0

    coll = client.collections.get(collection_name)

    for obj in objects:
        obj_id: str = obj["_additional"]["id"]
        raw_typed_links = obj.get("typed_links")
        title = obj.get("title", "<no title>")
        file_path = obj.get("file_path", "")

        # REST GraphQL may return typed_links as a JSON string in some schema
        # configurations — parse if needed.
        if isinstance(raw_typed_links, str):
            try:
                raw_typed_links = json.loads(raw_typed_links)
            except json.JSONDecodeError:
                logger.warning("typed_links for %r is a non-JSON string — dropping", title)
                raw_typed_links = None

        if _is_canonical(raw_typed_links):
            # Already correct — no write needed
            continue

        normalized, changed = _normalize(raw_typed_links)
        if not changed:
            continue

        label = f"{title!r} ({file_path})"
        if dry_run:
            logger.info("DRY-RUN would fix: %s — %r → %r", label, raw_typed_links, normalized)
            fixed += 1
            continue

        try:
            coll.data.update(
                uuid=obj_id,
                properties={"typed_links": normalized},
            )
            logger.info("Fixed: %s", label)
            fixed += 1
        except Exception as exc:
            logger.error("Failed to update %s (%s): %s", label, obj_id, exc)
            errors += 1

    return checked, fixed, errors


def _discover_kg_collections(client: weaviate.WeaviateClient) -> list[str]:
    """Return names of collections that look like KG collections (heuristic)."""
    try:
        all_names = list(client.collections.list_all().keys())
    except Exception as exc:
        logger.error("Could not list Weaviate collections: %s", exc)
        return []

    # KG collections typically end with _KnowledgeGraph or ARE KnowledgeGraph
    return [
        name for name in all_names
        if name == "KnowledgeGraph" or name.endswith("_KnowledgeGraph")
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair malformed typed_links in Weaviate KG collections."
    )
    parser.add_argument(
        "--collections",
        nargs="+",
        metavar="NAME",
        help="Specific collection names to repair (default: all *_KnowledgeGraph)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fixed without writing anything",
    )
    args = parser.parse_args()

    logger.info("Connecting to Weaviate at %s (gRPC port %d)", WEAVIATE_URL, GRPC_PORT)
    try:
        client = weaviate.connect_to_local(
            port=int(WEAVIATE_URL.rsplit(":", 1)[-1]) if ":" in WEAVIATE_URL.split("//", 1)[-1] else 8081,
            grpc_port=GRPC_PORT,
        )
    except Exception as exc:
        logger.error("Could not connect to Weaviate: %s", exc)
        return 1

    try:
        if args.collections:
            collections = args.collections
        else:
            collections = _discover_kg_collections(client)
            if not collections:
                logger.warning("No KG collections found. Use --collections to specify.")
                return 0
            logger.info("Discovered KG collections: %s", collections)

        total_checked = total_fixed = total_errors = 0
        for coll_name in collections:
            checked, fixed, errors = _repair_collection(client, coll_name, dry_run=args.dry_run)
            total_checked += checked
            total_fixed += fixed
            total_errors += errors
            logger.info(
                "  %s: checked=%d fixed=%d errors=%d",
                coll_name,
                checked,
                fixed,
                errors,
            )

        action = "Would fix" if args.dry_run else "Fixed"
        logger.info(
            "Summary: checked=%d  %s=%d  errors=%d",
            total_checked,
            action,
            total_fixed,
            total_errors,
        )
        return 1 if total_errors else 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
