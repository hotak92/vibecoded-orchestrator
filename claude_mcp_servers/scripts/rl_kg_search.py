#!/usr/bin/env python3
"""
Quick KG search with RL reranking — uses the same pipeline as the weaviate MCP.

Called by the pre-edit hook for context injection with RL-reranked results.
Uses the MCP server's functions directly (same Weaviate client, same RL server,
same score-driven verbosity tiers).

Usage:
    python rl_kg_search.py "query text" [--limit 1]

Output: human-readable text with title | type | score=X.XX | content,
formatted per the score-driven tier system in
claude_mcp_servers/weaviate_mcp/server.py (_get_result_verbosity_by_score and
_format_result_by_tier).
"""
import asyncio
import argparse
import os
import sys

# Add parent dir so we can import weaviate_mcp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main():
    parser = argparse.ArgumentParser(description="KG search with RL reranking")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", type=int, default=1, help="Max results (default: 1)")
    args = parser.parse_args()

    # Import the MCP server's internals
    from weaviate_mcp.server import (
        get_weaviate_client,
        _get_search_vector,
        _format_obj,
        _enrich_with_adjacent_chunks,
        _rl_cache_and_rerank,
        _get_result_verbosity_by_score,
        _format_result_by_tier,
        _kg_collections_to_search,
        KG_COLLECTION,
        EMBEDDING_SOURCE,
        _RL_OVERFETCH,
    )
    import uuid

    client = get_weaviate_client()
    try:
        # P1-D (2026-05-08): fan out across self + shared + peer KGs from
        # the launcher's access matrix (VCT_KG_ACCESS_LIST). Pre-fix this
        # script queried only KG_COLLECTION, so the pre-edit hook missed
        # peer-project context that the launcher GUI had granted —
        # turning the access matrix into a UI-only feature with no
        # runtime effect.
        collections_to_search = _kg_collections_to_search(include_dev=False)

        fetch_limit = args.limit * _RL_OVERFETCH

        # Run the search across each collection and merge candidates by
        # title-keyed best score. The MCP server's hybrid_search /
        # semantic_graph_search already do this — we replicate the
        # minimum: per-collection over-fetch, then sort + take top-k for
        # the RL rerank.
        if EMBEDDING_SOURCE == "weaviate":
            vector = None
            target_name = None
        else:
            vector, target_name = await _get_search_vector(args.query)

        all_formatted: list[dict] = []
        for coll_name in collections_to_search:
            try:
                coll = client.collections.get(coll_name)
            except Exception as exc:
                # Collection may not exist (peer never indexed yet) —
                # skip silently.
                continue
            try:
                if EMBEDDING_SOURCE == "weaviate":
                    primary = coll.query.near_text(
                        query=args.query,
                        limit=fetch_limit,
                        return_metadata=["distance"],
                    )
                else:
                    nv_kwargs = dict(
                        near_vector=vector,
                        limit=fetch_limit,
                        return_metadata=["distance"],
                    )
                    if target_name:
                        nv_kwargs["target_vector"] = target_name
                    primary = coll.query.near_vector(**nv_kwargs)
            except Exception:
                continue
            if not primary.objects:
                continue
            coll_formatted = [
                _format_obj(obj, coll_name, obj.metadata.distance)
                for obj in primary.objects
            ]
            coll_formatted = _enrich_with_adjacent_chunks(coll, coll_formatted, coll_name)
            all_formatted.extend(coll_formatted)

        if not all_formatted:
            return

        # Use the primary KG collection handle for the tier helper's
        # sidecar lookups. This matches the hybrid_search behaviour —
        # the formatter only needs A handle for chunk fetches; results
        # carry their own source-collection info.
        coll = client.collections.get(KG_COLLECTION)

        # Preserve a normalised score (1 - distance) for the tier helper.
        for r in all_formatted:
            if "score" not in r:
                d = r.get("distance")
                r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

        # Multi-collection merge: sort by score so the RL server sees a
        # clean top-k regardless of which collection each result came
        # from. Without this, peer-collection results would be appended
        # below self-collection results even when their score is higher.
        all_formatted.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        # RL rerank (calls RL server, falls back to Weaviate order)
        task_id = f"pre_edit_{uuid.uuid4().hex[:8]}"
        results = await _rl_cache_and_rerank(task_id, args.query, all_formatted, args.limit)
        for r in results:
            if "score" not in r:
                d = r.get("distance")
                r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

        # Render per-result tier through the shared helper. Output format mirrors
        # the legacy "title | type | score=X.XX | <body>" contract that the
        # pre-edit hook expects so the hook stays compatible after the refactor.
        for r in results:
            score = float(r.get("score") or 0.0)
            tier = _get_result_verbosity_by_score(score)
            if tier == "discard":
                continue
            entry = _format_result_by_tier(r, tier, sidecar_db=None, coll=coll)
            if entry is None:
                continue

            title = entry.get("title", "")
            node_type = entry.get("node_type", "")
            label_map = {
                "summary":      "SUMMARY",
                "single_chunk": "FULL",         # legacy label preserved for hook regex
                "three_chunks": "3 CHUNKS",
                "full":         "FULL NODE",
            }
            label = label_map.get(tier, tier.upper())
            chunks_shown = entry.get("chunks_shown")
            chunks_total = entry.get("chunks_total")

            # For multi-chunk renderings, append a (shown/total) marker like the
            # original output did. For summary, body is description/summary/content.
            if tier == "summary":
                body = (
                    entry.get("description")
                    or entry.get("summary")
                    or entry.get("content", "")
                )
                print(f"{title} | {node_type} | score={score:.2f} | {body}")
            elif chunks_shown and chunks_total and chunks_total > 1:
                body = entry.get("content", "")
                print(
                    f"{title} | {node_type} | score={score:.2f} | "
                    f"{label} ({chunks_shown}/{chunks_total} chunks):"
                )
                print(body)
            else:
                body = entry.get("content", "")
                print(f"{title} | {node_type} | score={score:.2f} | {label}:")
                print(body)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
