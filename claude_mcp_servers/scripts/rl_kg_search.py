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
    parser.add_argument("--limit", type=int, default=3, help="Max results (default: 3)")
    parser.add_argument(
        "--hook-format",
        action="store_true",
        help="Prefix each result header with 'KG: ' so the pre-edit hook can dedup by title",
    )
    args = parser.parse_args()
    header_prefix = "KG: " if args.hook_format else ""

    # Import the MCP server's internals
    # V52-J (v0.2.52): rerank+emit is now routed through the canonical
    # ``rl_client.search_pipeline.rerank_and_emit`` so every KG-search
    # entry point shares one chokepoint for telemetry + RL rerank.
    # Pre-v0.2.52 this script reached into ``server._rl_cache_and_rerank``
    # directly; the new pipeline owns the license-tier gate, the RL RPC
    # cache, the citation-cache populate, the answer-monitor spawn, and
    # the v3 retrieval-event emit (with the 3-layer session_id resolution
    # + project_id propagation fixes). Caller still owns Weaviate fan-out
    # because the pipeline is intentionally orthogonal to retrieval
    # strategy.
    from weaviate_mcp.server import (
        get_weaviate_client,
        _get_search_vector,
        _format_obj,
        _enrich_with_adjacent_chunks,
        _get_result_verbosity_by_score,
        _format_result_by_tier,
        _kg_collections_to_search,
        _embedding_dim_for,
        KG_COLLECTION,
        EMBEDDING_SOURCE,
        EMBEDDING_MODEL,
        _RL_OVERFETCH,
    )
    from claude_mcp_servers.rl_client.search_pipeline import (
        RerankRequest,
        rerank_and_emit,
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
            # v0.2.21 audit fix: under --hook-format, emit a single short
            # identifying line so the pre-edit hook (which captures our
            # stdout) and the model both see WHAT was searched. Per user
            # direction 2026-05-20: empty stdout is fine when it doesn't
            # reach the model at all (hook's HAS_KG=0 short-circuit), but
            # if it WILL be captured into context it's better to give it a
            # name. Non-hook callers (CLI) get nothing on empty — they're
            # interactive and the silence is informative on its own.
            if args.hook_format:
                print(f"KG: no-results | query='{args.query}' | limit={args.limit}")
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

        # RL rerank + telemetry emit via the V52-J canonical pipeline.
        # NEW-8 (2026-05-28): query embedding is carried into the
        # retrieval-event payload for offline training. Pre-NEW-8, every
        # retrieval event from this script was written without query_emb;
        # the v0.2.52 refactor keeps that contract intact (vector flows
        # through RerankRequest.query_emb) while moving the call-site to
        # a single canonical chokepoint shared with the MCP server +
        # search_knowledge.py CLI.
        #
        # task_type = "pre_edit_kg_search" so offline analysis can
        # distinguish hook-triggered context-injection events from
        # interactive MCP `hybrid_search` calls. The rl_events schema
        # accepts arbitrary task_type strings (varchar column, no
        # enum constraint — see launcher/src-tauri/migrations/*.sql).
        task_id = f"pre_edit_{uuid.uuid4().hex[:8]}"
        req = RerankRequest(
            query=args.query,
            candidates=all_formatted,
            limit=args.limit,
            query_emb=vector,
            embedding_source=EMBEDDING_SOURCE,
            embedding_dim=_embedding_dim_for(EMBEDDING_MODEL),
            embedding_model=EMBEDDING_MODEL,
            task_id=task_id,
            task_type="pre_edit_kg_search",
        )
        rerank_result = await rerank_and_emit(req)
        results = rerank_result.ranked
        for r in results:
            if "score" not in r:
                d = r.get("distance")
                r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

        # Render per-result tier through the shared helper. Output format mirrors
        # the legacy "title | type | score=X.XX | <body>" contract that the
        # pre-edit hook expects so the hook stays compatible after the refactor.
        printed_count = 0
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
            # v0.2.70 Stream D-2: surface file_path in the --hook-format header so
            # the injected block is directly openable AND the shared seen-store
            # (_lib/seen-store.sh) can suppress a block whose source the model
            # already Read explicitly (reads-ledger match). The "| src=<path>"
            # trailer must be LAST (the seen-store extracts the last "| src="
            # occurrence). Only emitted in --hook-format mode + when a path
            # exists, so interactive CLI output is unchanged.
            file_path = entry.get("file_path", "") or ""
            src_trailer = f" | src={file_path}" if (args.hook_format and file_path) else ""
            label_map = {
                "summary":      "SUMMARY",
                "single_chunk": "1 CHUNK",
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
                print(f"{header_prefix}{title} | {node_type} | score={score:.2f} | {body}{src_trailer}")
            elif chunks_shown and chunks_total and chunks_total > 1:
                body = entry.get("content", "")
                print(
                    f"{header_prefix}{title} | {node_type} | score={score:.2f} | "
                    f"{label} ({chunks_shown}/{chunks_total} chunks):{src_trailer}"
                )
                print(body)
            else:
                body = entry.get("content", "")
                print(f"{header_prefix}{title} | {node_type} | score={score:.2f} | {label}:{src_trailer}")
                print(body)
            printed_count += 1

        # v0.2.21 audit fix: if EVERY result was filtered out (tier=discard
        # because score below KG_TIER_MIN, OR _format_result_by_tier returned
        # None for all of them), the loop above produced no stdout. Mirror
        # the all_formatted=[] branch: under --hook-format, emit one short
        # identifying line so the model sees what was searched.
        if printed_count == 0 and args.hook_format:
            print(f"KG: no-results | query='{args.query}' | limit={args.limit}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
