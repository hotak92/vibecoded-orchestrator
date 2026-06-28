# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Content-identity dedup — ONE Python home for the retrieval/injection layer.

v0.2.70 (concern-2 follow-up). The maintainer's bar: chunks with the SAME
content (name + content-hash) must not reach Claude's context twice — across
query chunks, across retrieval paths, or via cross-collection duplicates (e.g.
the SAME node living in both the project KG and the shared KG).

Two distinct dedup AXES exist in the retrieval layer; this module owns BOTH so
every Python retrieval/injection path uses the same definitions instead of
re-implementing them per-site:

  1. IDENTITY dedup — "the same logical node retrieved twice" (e.g. two query
     chunks both surfaced node A, or two chunks of the same multi-chunk node).
     Key = ``(file_path, title)`` for KG; the first non-empty of
     ``full_name / endpoint / path / title`` for code. This is what
     ``_collapse_to_one_per_node`` and the old ``query_chunking._node_key``
     already did — pulled here so there is one definition.

  2. CONTENT-IDENTITY dedup — "two entries with the SAME NAME and the SAME
     CONTENT reaching Claude twice." Key = ``(name, content_hash)``. This
     CATCHES the cross-collection case identity dedup misses: the same node
     title + same body under two different file_paths (project KG + shared KG)
     has the SAME (name, content_hash) but a DIFFERENT (file_path, title), so
     identity dedup keeps both while content-identity collapses them to one.

THE OVER-COLLAPSE GUARD (load-bearing — do not weaken):
  Content-identity keys on ``(NAME, content_hash)``, NOT on ``content_hash``
  alone. Two LEGITIMATELY-DISTINCT items that merely share a body — two real
  files with coincidentally-identical content, or two distinct code entities
  with the same one-line body — have DIFFERENT names (title / full_name), so
  they are NEVER collapsed. We only ever merge entries the user expects to be
  the SAME thing (same name) that ALSO carry the same content. When in doubt,
  the helper keeps both (it never collapses on content alone, and an entry with
  no recoverable content is keyed by identity so it is never silently dropped).

HASH CONVENTION (cross-layer consistency):
  ``content_sha`` mirrors the seen-store's KG convention — ``sha1(body)[:12]``
  (see ``templates/hooks/_lib/seen-store.{sh,ps1}``'s ``vco_seen_hash`` /
  ``Get-VcoSeenHash``). Keeping the same hash function means the content
  fingerprint computed by the Python retrieval helpers and the one computed by
  the shell injection layer AGREE on what "same content" means, so a block
  deduped here and a block deduped at the seen-store can never disagree about
  identity. The shell side is a necessarily-separate per-language home; this is
  the Python home, and the two are pinned to the same algorithm by the
  "MUST MATCH" notes on both sides.

Storage-layer content hashes (``sync_knowledge_graph._content_signature_
excluding_updated``, ``analyze_code_graph._content_hash_for_object``,
``generate_node_formats.content_hash``) are a DIFFERENT layer — they answer
"is the STORED object unchanged so I can skip a re-embed/re-write", carry
deliberate per-site nuances (excluding the ``updated:`` line; field-selective
hashing), and are intentionally NOT routed through here. See the triage in the
investigation report for why those stay distinct.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

__all__ = [
    "content_sha",
    "node_identity_key",
    "code_identity_key",
    "node_content_text",
    "code_content_text",
    "content_identity_key",
    "dedup_by_content_identity",
]


# --- canonical content hash (mirrors seen-store sha1[:12]) -------------------

def content_sha(text: str | None) -> str:
    """Stable short fingerprint of *text* — ``sha1(text)[:12]``.

    MUST MATCH the seen-store's ``vco_seen_hash`` (``sha1(body)[:12]``, bash)
    and ``Get-VcoSeenHash`` (PowerShell) so the Python and shell layers agree on
    "same content". Empty/None text → empty string (the caller then falls back
    to identity-only keying so a content-less entry is never dropped).
    """
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --- identity keys (the "same logical node retrieved twice" axis) ------------

def node_identity_key(node: dict) -> tuple:
    """KG identity — ``(file_path, title)``.

    Mirrors ``weaviate_mcp.server._collapse_to_one_per_node`` and the original
    ``query_chunking._node_key`` so all three agree on node identity.
    """
    return (node.get("file_path") or "", node.get("title") or "")


def code_identity_key(node: dict) -> Any:
    """Code-entity identity — first non-empty of full_name/endpoint/path/title.

    Mirrors the key ``query_chunking.combine_codegraph_results`` used inline.
    Falls back to ``id(node)`` so a property-less dict is never collapsed into
    an unrelated one (it keys on its own object identity → kept).
    """
    return (
        node.get("full_name")
        or node.get("endpoint")
        or node.get("path")
        or node.get("title")
        or id(node)
    )


# --- content extraction (which field carries the body, per node kind) --------

# Order matters: a code entity may also carry an empty "content" field, so the
# code-body fields are checked first for code; for KG, "content" is canonical.
_CODE_BODY_FIELDS = (
    "function_body",
    "class_body",
    "module_summary",
    "api_description",
    "signature",
)


def node_content_text(node: dict) -> str:
    """The KG/doc content body used for the content fingerprint.

    KG and Development-doc result dicts carry the body under ``content`` (see
    ``weaviate_mcp.server._format_obj``). Returns ``""`` when absent → identity
    fallback in the dedup key.
    """
    return (node.get("content") or "").strip()


def code_content_text(node: dict) -> str:
    """The code-entity body used for the content fingerprint.

    Code result dicts carry the body under one of ``_CODE_BODY_FIELDS`` (see
    ``_format_obj``'s CodeFunction/CodeClass/CodeModule/CodeAPI branches). Joins
    the present fields (in a fixed order, so the fingerprint is stable) so a
    signature-only entity and a full-body entity of the same name still compare
    correctly. Falls back to ``content`` then ``""``.
    """
    parts = [str(node.get(f) or "").strip() for f in _CODE_BODY_FIELDS]
    parts = [p for p in parts if p]
    if parts:
        return "\n".join(parts)
    return (node.get("content") or "").strip()


# --- content-identity key (the maintainer's "name + hash" axis) --------------

def content_identity_key(node: dict, *, kind: str = "kg") -> tuple:
    """``(name, content_sha)`` — the content-identity dedup key.

    ``kind`` selects the name + content-field convention:
      * ``"kg"``  → name = title; content = ``content``.
      * ``"code"``→ name = full_name/endpoint/path/title; content = code body.

    OVER-COLLAPSE GUARD: when the content fingerprint is EMPTY (no recoverable
    body), we return the IDENTITY key instead so the entry is keyed by its
    distinct identity and never collapsed with another content-less entry. Two
    distinct items only ever collapse when they share BOTH a name AND a
    non-empty content fingerprint.

    TRUNCATION GUARD (v0.2.70 over-collapse fix): result dicts produced by
    ``weaviate_mcp.server._format_obj`` carry a TRUNCATED display body
    (``content[:300] + "..."``) — hashing that field would key two
    legitimately-distinct same-title nodes that merely share their first 300
    chars on the SAME content fingerprint, silently dropping the one with the
    differing tail. To prevent that, ``_format_obj`` attaches a precomputed
    ``content_sha`` computed from the FULL untruncated body. When present we
    use it verbatim; otherwise we fall back to hashing the (possibly truncated)
    display field. The fallback is acceptable for callers that pass full bodies
    (tests, future producers); the truncation risk only ever existed for the
    ``_format_obj`` display path, which now always supplies ``content_sha``.
    """
    if kind == "code":
        name = code_identity_key(node)
        body = code_content_text(node)
        # Code dicts come from the code formatter, which never truncates a
        # fingerprinted body (ranks 1-2 carry the FULL function/class body;
        # ranks 3-4 omit it → identity fallback). So there is no precomputed
        # full-body sha to honour here — hash the recovered body directly.
        sha = content_sha(body)
    else:
        name = node.get("title") or ""
        body = node_content_text(node)
        # Prefer the full-body fingerprint attached by the KG/doc producer
        # (``_format_obj`` sets ``content_sha`` from the UNTRUNCATED body) over
        # re-hashing ``content``, which on the _format_obj display path is the
        # truncated ``content[:300] + "..."`` form. Falling back to hashing the
        # (possibly truncated) display body is only reached for callers that
        # build dicts without _format_obj (tests / future producers), where the
        # body they pass is the real one.
        precomputed = node.get("content_sha") if isinstance(node, dict) else None
        sha = precomputed if precomputed else content_sha(body)
    if not sha:
        # No comparable content → fall back to identity so we never collapse
        # two content-less-but-distinct entries together. ``code_identity_key``
        # may return a non-tuple (full_name str, or id(node) int), so wrap it in
        # a tuple before tagging — the "__identity__" tag keeps these keys in a
        # separate namespace from real (name, sha) content keys.
        identity = (
            code_identity_key(node) if kind == "code" else node_identity_key(node)
        )
        return ("__identity__", identity)
    return (name, sha)


def dedup_by_content_identity(
    nodes: Iterable[dict],
    *,
    kind: str = "kg",
) -> list[dict]:
    """Return *nodes* with content-identical duplicates collapsed (first wins).

    Order-preserving: the first occurrence of each ``content_identity_key`` is
    kept (so a caller that has already score-sorted keeps its highest-scoring
    representative). Non-dict items are passed through untouched (defensive).

    This is the single shared collapse used by every Python retrieval/injection
    path that means "drop the duplicate-content block before it reaches Claude".
    """
    seen: set = set()
    out: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            out.append(node)
            continue
        key = content_identity_key(node, kind=kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
    return out
