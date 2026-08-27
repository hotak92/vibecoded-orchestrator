#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P4 (v0.2.91) — record what an EXPLICIT MCP retrieval already put in context.

Invoked by the ``post-mcp-retrieval-record`` hook (both flavours) with the hook's
stdin JSON on stdin::

    python mcp_retrieval_record.py <inject_store> [<reads_store>]

ONE HOME (CLAUDE.md "share, don't mirror, cross-language logic", option A): the
parsing + key derivation live here, in Python, and BOTH ``post-mcp-retrieval-
record.sh`` and ``.ps1`` shell out to this same file. A PowerShell re-implementation
would be a mirror of hash-and-parse logic — precisely the class that silently
drifts and makes the two OSes suppress different things.

The gap this closes (2026-08-27 perf investigation, §2)
-------------------------------------------------------
The session already had two suppression channels — the inject-dedup store
(things the HOOKS injected) and the explicit-Read ledger (files the model Read).
Nodes and entities returned by a DELIBERATE ``hybrid_search`` /
``semantic_graph_search`` / ``search_code_graph`` call were recorded NOWHERE, so
a node an agent had just fetched on purpose could be re-injected minutes later by
the pre-edit hook. That is the most annoying redundancy class: the model is shown
something it explicitly went and got.

THE SAFETY RULE: suppress ONLY what is PROVABLY already in context
------------------------------------------------------------------
This never records "the model saw this node" — it records "the model has these
exact bytes":

  * KG results → the injector's OWN per-chunk key ``<title>#<sha1(body)[:12]>``,
    computed from the SAME body text ``rl_kg_search.py --hook-format`` would have
    printed for that entry at that tier. If the hook later retrieves the same
    node at a DIFFERENT tier (different body), the hash differs and the block
    still injects — correct, because that is new content.
  * KG results carrying ``coverage == "complete"`` (the formatter's explicit
    "all chunks returned" marker) ALSO write the node's source path into the
    reads-ledger, so ANY chunk of that node is suppressed. Sound because the
    whole node is demonstrably in context. A partial view never does this.
  * Code results → the entity's ``full_name``, and ONLY when the result carried
    ``function_body`` / ``class_body`` (the untruncated top tier). A metadata-only
    "ref" entry records nothing: the model saw a name, not the code.

Everything else is skipped: a ``titles``-detail search, a truncated middle tier,
a connected-node stub with no content. Over-suppression would silently cost the
model context, which is strictly worse than a duplicate injection.

Never raises, never prints: a malformed payload records nothing.
"""
from __future__ import annotations

import hashlib
import json
import sys

# The RETRIEVAL tools only. store_knowledge_node / query_code_structure /
# describe_excalidraw return no retrieved content to suppress.
KG_TOOLS = ("hybrid_search", "semantic_graph_search")
CODE_TOOLS = ("search_code_graph",)

# Tiers whose rendered block carries actual node text. MUST MATCH the tier names
# stamped by weaviate_mcp.server._format_result_by_tier.
_SUMMARY_TIERS = ("summary", "descriptions")
_CHUNK_TIERS = ("single_chunk", "three_chunks", "full")

# Cap for a key's identity field (KG title / CODE full_name), in UTF-8 BYTES.
# MUST MATCH seen-store.sh's vco_cap_key_field and seen-store.ps1's
# Get-VcoCapKeyField — see :func:`cap_key_field`.
_KEY_FIELD_MAX_BYTES = 200


def cap_key_field(text):
    """Truncate a key's identity field to at most 200 UTF-8 BYTES.

    MUST MATCH ``seen-store.sh::vco_cap_key_field`` and
    ``seen-store.ps1::Get-VcoCapKeyField``. The three implementations key the
    SAME dedup store, so the truncation axis has to be one thing everywhere.

    Why BYTES and not characters (wave-4 NIT-4): bash's ``${field:0:200}``
    counts CHARACTERS under a UTF-8 locale but BYTES under ``LC_ALL=C``, and a
    hook shell's ambient locale is not something VCO controls; PowerShell's
    ``Substring(0, 200)`` counts UTF-16 code units (2 per astral char).
    Python's ``[:200]`` counts code points. Three different answers for a long
    non-ASCII title → three different keys → the suppression silently misses.
    Bytes is the ONE definition all three can pin without depending on the
    ambient locale, so all three now pin it (bash with a function-local
    ``LC_ALL=C``).

    The cut is backed off to a character boundary — a truncated trailing
    multi-byte sequence is DROPPED, never emitted as a partial sequence — so
    every side produces valid text and the three agree byte-for-byte.
    """
    raw = text.encode("utf-8")
    if len(raw) <= _KEY_FIELD_MAX_BYTES:
        return text
    # ``errors="ignore"`` drops exactly the incomplete trailing sequence, which
    # is the same result as the explicit continuation-byte back-off the shell
    # siblings perform.
    return raw[:_KEY_FIELD_MAX_BYTES].decode("utf-8", "ignore")


def normalize_block_body(text):
    """Canonicalize a block body before hashing: trailing newlines → exactly one.

    MUST MATCH ``seen-store.sh::vco_seen_normalize_body`` and
    ``seen-store.ps1::Get-VcoSeenNormalizedBody``.

    WHY (wave-4 MINOR-5): the number of trailing newlines a rendered block
    carries is a function of WHERE IN THE BLOB it sits, not of the content.
    ``rl_kg_search.py --hook-format`` prints ``header`` then ``print(body)``, so
    a body that itself ends in ``\\n`` emits an extra EMPTY line; the injector
    then captures the whole blob with ``KG_RESULT="$(…)"``, which strips
    trailing newlines — removing that empty line for the LAST block only.
    Measured on the real pair: content ``"x\\n"`` reassembles to ``"x\\n\\n"``
    in a non-final block and ``"x\\n"`` in a final one. The recorder cannot know
    a result's eventual position, so the fix is to make BOTH sides hash a body
    whose trailing-newline run is normalized; then the key depends only on the
    content and the two channels agree in every position.

    An empty body stays empty: a ``summary``-tier block renders its text ON the
    header line and has no body at all, and that must keep hashing ``""``.
    """
    stripped = text.rstrip("\n")
    return stripped + "\n" if stripped else ""


def unwrap_payload(resp):
    """Unwrap a tool response to the result-carrying dict, or None.

    TWO payload shapes carry retrieved content, and both must be accepted
    (wave-4 MAJOR-2 — accepting only the first made the recorder structurally
    inert for ``semantic_graph_search``, one of the three tools the hook is
    registered on):

    * ``{"results": [...]}`` — ``hybrid_search`` (server.py:5741) and
      ``search_code_graph`` (7196 / 7865).
    * ``{"success": …, "primary_results": [...], "connected_nodes": [...]}`` —
      ``semantic_graph_search`` (server.py:4874-4882). There is NO ``results``
      key on that response at all.

    MCP responses reach a hook as the raw JSON string, as an already-parsed
    dict, or wrapped in the content-block envelope. Try each shape; give up
    quietly on anything else.
    """
    cands = []
    if isinstance(resp, str):
        cands.append(resp)
    elif isinstance(resp, dict):
        if _carries_results(resp):
            return resp
        content = resp.get("content")
        if isinstance(content, list):
            for it in content:
                if isinstance(it, dict) and isinstance(it.get("text"), str):
                    cands.append(it["text"])
                elif isinstance(it, str):
                    cands.append(it)
    elif isinstance(resp, list):
        for it in resp:
            if isinstance(it, dict) and isinstance(it.get("text"), str):
                cands.append(it["text"])
            elif isinstance(it, str):
                cands.append(it)
    for t in cands:
        try:
            p = json.loads(t)
        except Exception:  # noqa: BLE001 — a non-JSON block is simply not ours
            continue
        if _carries_results(p):
            return p
    return None


def _carries_results(payload):
    """True when ``payload`` is a dict carrying either result-list shape."""
    return isinstance(payload, dict) and (
        isinstance(payload.get("results"), list)
        or isinstance(payload.get("primary_results"), list)
    )


def result_lists(payload):
    """``(primary results, top-level nested results)`` for an unwrapped payload.

    ``semantic_graph_search`` returns its traversed neighbours as a TOP-LEVEL
    ``connected_nodes`` list — a sibling of ``primary_results``, not a field on
    each result. They are ordinary formatted entries (rendered at the
    ``summary`` tier unless the caller asked for ``titles``/``full``), so the
    same content gate applies to them.
    """
    primary = payload.get("results")
    if not isinstance(primary, list):
        primary = payload.get("primary_results")
    if not isinstance(primary, list):
        primary = []
    nested = payload.get("connected_nodes")
    if not isinstance(nested, list):
        nested = []
    return primary, nested


def kg_keys(result):
    """(inject_key, reads_path) for one KG result. Either may be None.

    Body reconstruction MUST mirror ``rl_kg_search.py --hook-format``:
      * summary/descriptions tier → the body sits on the HEADER line, so the
        block body the seen-store hashes is EMPTY.
      * chunked tiers → header line, then ``print(body)`` → the hashed block body
        is ``content`` plus the newline ``print`` appends.
    A tier with no content at all (titles / ref) yields nothing: the model saw a
    name, not the text, so suppressing a later block would LOSE context.
    """
    if not isinstance(result, dict):
        return None, None
    title = cap_key_field(str(result.get("title") or ""))
    if not title:
        return None, None
    tier = str(result.get("tier") or "")
    if tier in _SUMMARY_TIERS:
        if not (result.get("description") or result.get("summary") or result.get("content")):
            return None, None
        body = ""
    elif tier in _CHUNK_TIERS:
        content = result.get("content")
        if not isinstance(content, str) or not content:
            return None, None
        # MUST MATCH the seen-store's view of the block body — both sides now
        # normalize the trailing-newline run (wave-4 MINOR-5; see
        # :func:`normalize_block_body` for why the raw count is not a property
        # of the content). Fails open either way: a mismatched key means a
        # duplicate injection, never a false suppression.
        body = normalize_block_body(content)
        if not body:
            # Whitespace-only content proves nothing is in context, and an empty
            # body would collide with this node's summary-tier key.
            return None, None
    else:
        return None, None
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    reads_path = None
    # Whole node demonstrably in context → suppress ANY chunk of it. The
    # formatter emits this marker ONLY at 100% chunk coverage.
    if result.get("coverage") == "complete":
        fp = str(result.get("file_path") or "")
        if fp:
            reads_path = fp
    return "%s#%s" % (title, digest), reads_path


def code_key(result):
    """The injector's per-entity key (``full_name``) for one code result, or None.

    ONLY when the entry carried the body (the untruncated top tier). A
    metadata-only ref, or a truncated middle tier, records nothing.
    """
    if not isinstance(result, dict):
        return None
    if not (result.get("function_body") or result.get("class_body")):
        return None
    fn = cap_key_field(str(result.get("full_name") or ""))
    return fn or None


def collect(hook_input):
    """(inject_keys, reads_paths) for one hook stdin payload."""
    inject_keys: list = []
    reads_paths: list = []
    if not isinstance(hook_input, dict):
        return inject_keys, reads_paths
    tool = str(hook_input.get("tool_name") or "")
    short = tool.rsplit("__", 1)[-1]
    if short not in KG_TOOLS + CODE_TOOLS:
        return inject_keys, reads_paths
    payload = unwrap_payload(hook_input.get("tool_response"))
    if not payload:
        return inject_keys, reads_paths

    results, connected = result_lists(payload)
    if short in CODE_TOOLS:
        # search_code_graph has no graph-neighbour list; only `results`.
        for r in results:
            k = code_key(r)
            if k:
                inject_keys.append(k)
        return inject_keys, reads_paths

    # `connected` is semantic_graph_search's TOP-LEVEL `connected_nodes` list.
    for r in list(results) + list(connected):
        k, rp = kg_keys(r)
        if k:
            inject_keys.append(k)
        if rp:
            reads_paths.append(rp)
        # Defensive: should a surface ever nest neighbours UNDER the primary
        # they were traversed from, the same content gate applies to them.
        if isinstance(r, dict):
            for nested_key in ("connected", "connections", "connected_nodes"):
                nested = r.get(nested_key)
                if not isinstance(nested, list):
                    continue
                for c in nested:
                    k, rp = kg_keys(c)
                    if k:
                        inject_keys.append(k)
                    if rp:
                        reads_paths.append(rp)
    return inject_keys, reads_paths


def _append(path, values):
    if not path or not values:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for v in values:
                fh.write(v + "\n")
    except Exception:  # noqa: BLE001 — a broken store must never break the tool
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    inject_file = argv[0] if argv else ""
    reads_file = argv[1] if len(argv) > 1 else ""
    try:
        data = json.loads(sys.stdin.read())
    except Exception:  # noqa: BLE001
        return 0
    inject_keys, reads_paths = collect(data)
    _append(inject_file, inject_keys)
    _append(reads_file, reads_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
