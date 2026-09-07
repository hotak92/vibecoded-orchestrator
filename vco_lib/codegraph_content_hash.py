# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The code-graph per-object CONTENT-HASH rule — one home (v0.2.92).

Extracted VERBATIM from ``templates/scripts/analyze_code_graph.py`` (where it
had lived since v0.2.61 Track E). Nothing about the rule changed in the move:
the field tables, the exclusion set, the scalar rendering and the digest
construction are byte-identical, so every stored ``content_hash`` on every
existing install still matches. A behaviour change here would silently
re-``replace()`` (and re-embed) EVERY row in EVERY project's graph — this
repo has already been bitten once by a sidecar hash-scheme mismatch, so treat
the digest as a wire format.

WHY IT MOVED
------------
This is the DECISION half of the analyzer's tombstone-skip: the digest it
produces is what ``vco_lib.codegraph_guards.classify_row`` compares to decide
SKIP / STAMP / EMBED. Producer and consumer now live next to each other in
``vco_lib``, importable and unit-testable without loading the 7k-line template
script, and there is exactly ONE place a future field change has to be made.
``analyze_code_graph`` re-exports the names it used to define
(``_CONTENT_HASH_FIELDS`` / ``_CONTENT_HASH_EXCLUDE`` /
``_content_hash_for_object`` / ``_stable_scalar``) so existing call sites and
tests are unaffected.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping


# v0.2.61 (Track E): per-object content-hash fields for the tombstone-skip.
# Maps the bare collection base-name to the ORDERED list of property keys
# whose values define an object's semantically-meaningful content. The hash
# is computed over ONLY these fields (in this fixed order) so that:
#   * the SAME content yields the SAME hash across runs (stable skip key), and
#   * volatile / run-derived fields (last_modified, project_source, language,
#     file_path) are EXCLUDED — backfilling those on an otherwise-unchanged
#     object must NOT change the hash, or we'd re-`replace()` (re-tombstone)
#     every row on the migration run that stamps them.
#
# Field choice rationale (only fields that drive the embedding vector or the
# searchable body, plus the identity key, so a genuine change is always
# reflected; references/UUIDs are derived from these same fields so they need
# not be hashed separately):
#   CodeModule      → path + module_summary + imports
#   CodeClass       → full_name + signature + class_body + methods + composes
#   CodeFunction    → full_name + signature + function_body + type_uses
#                     (+ cfg_summary + data_flow_vars — v0.2.73 CG-3 INERT
#                      tombstone padding; always "" now, kept only so existing
#                      rows don't re-hash. See _CONTENT_HASH_FIELDS below.)
#   CodeAPI         → endpoint + method + api_description + parameters + returns
#   CodeInteraction → interaction_type + protocol + endpoint + raw_target
#                     + direction + description
# A collection whose name isn't recognised falls back to hashing ALL scalar/
# list properties (excluding the volatile set) — fail-safe toward "include
# more", which can only cause an extra (correct) write, never a wrong skip.
_CONTENT_HASH_FIELDS = {
    "CodeModule": ["path", "module_summary", "import_names"],
    # v0.2.72 (P3): chunk_num is part of the content hash for chunkable
    # entities so two chunks of the same entity — which share full_name but
    # carry different chunk bodies AND a different chunk_num — always hash
    # distinctly (defense-in-depth; their bodies already differ). The
    # tombstone-skip then compares like-for-like per chunk.
    "CodeClass": ["full_name", "signature", "class_body", "methods", "composes", "chunk_num"],
    "CodeFunction": [
        "full_name", "signature", "function_body",
        "type_uses",
        # v0.2.73 (CG-3) TOMBSTONE: cfg_summary + data_flow_vars are RETAINED here
        # as INERT PADDING even though the Joern CFG/PDG extractor that populated
        # them was removed. WHY keep them: the content hash mixes each listed
        # field's value; a row indexed BEFORE CG-3 stored these as "" / [] and its
        # stored content_hash includes them. If we DROPPED them from this list, the
        # next walk on every existing install would recompute a DIFFERENT hash for
        # EVERY function -> a one-time WHOLE-COLLECTION re-replace() (a large write
        # burst — the exact I/O the v0.2.73 read/write-reduction work exists to
        # avoid). Because the analyzer no longer EMITS these props, `properties.get`
        # returns None and `_stable_scalar(None)` == `_stable_scalar("")` == "", so
        # keeping the names makes the post-CG-3 hash BYTE-IDENTICAL to the stored
        # one -> ZERO rewrite. The names hash as a constant "" forever; remove them
        # only alongside a deliberate, batched re-hash migration.
        "cfg_summary", "data_flow_vars",
        "chunk_num",
    ],
    "CodeAPI": ["endpoint", "method", "api_description", "parameters", "returns"],
    "CodeInteraction": [
        "interaction_type", "protocol", "endpoint",
        "raw_target", "direction", "description",
    ],
}

# Fields that are deterministic-but-derived or volatile — NEVER part of the
# content hash even on the all-fields fallback path. `content_hash` itself is
# excluded so the hash is a fixed point (hashing-in the prior hash would make
# it unstable). `last_modified` is a filesystem mtime (changes on touch with
# no content change). `project_source` / `language` / `file_path` are stamped
# by `_dedup_insert` and are pure functions of (file, source-root) — including
# them would force a one-time re-write whenever a backfill migration first
# stamps them, defeating the skip.
# KNOWN LIMITATION (Stage-1 correctness SEV-3 #2, pre-existing v0.2.61 tradeoff):
# `start_line`/`end_line` are EXCLUDED from the content hash, so a function whose
# body is byte-identical but whose line range SHIFTED (an edit above it in the
# file) is content-hash-unchanged → the per-object skip (and, since FIX-B2, the
# embed) is skipped, leaving the STORED start_line/end_line stale until a full
# reanalyze. The VECTOR stays correct (body unchanged); only the display line
# range drifts. Accepted tradeoff: including line ranges would force a re-write
# of every function below any edit on every keystroke — the exact write
# amplification the skip exists to avoid. A full `code-graph-analyze` (no
# --only-file) re-stamps the ranges.
_CONTENT_HASH_EXCLUDE = frozenset({
    "content_hash", "last_modified", "project_source", "language",
    "file_path", "start_line", "end_line",
    # v0.2.72 (P7): the embedding-revision marker is generation metadata, not
    # semantic content — excluding it keeps the content hash a fixed point so
    # stamping/bumping the revision never triggers a spurious content-hash
    # rewrite on the unknown-collection fallback path.
    "embed_revision",
    # v0.2.73 (M1/M4): generation metadata, same rationale — `is_test` is a
    # pure function of the excluded `file_path`; `n_callers` is recomputed by
    # every cross-reference pass. Neither belongs in _CONTENT_HASH_FIELDS.
    "is_test", "n_callers",
})


def _stable_scalar(value: Any) -> str:
    """Render a property value into a stable, order-independent string.

    Lists are rendered element-wise (each element coerced to str) WITHOUT
    sorting — the analyzer emits these lists deterministically per parse, so
    preserving order keeps the hash byte-stable while a genuine reorder (which
    is a real content change in source) correctly changes the hash. None and
    missing values render as the empty string so an absent field and an
    explicitly-empty field hash identically (avoids spurious re-writes when a
    property is omitted vs. set to "").
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\x1e".join(_stable_scalar(v) for v in value)
    if isinstance(value, bool):
        # Render bools before the int branch (bool is a subclass of int) so
        # True/False hash distinctly from 1/0 textual collisions are avoided.
        return "true" if value else "false"
    return str(value)


def _content_hash_for_object(collection_name: str, properties: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 over an object's semantically-meaningful content.

    v0.2.61 (Track E) — mirrors the KG-sync `content_hash` discipline
    (templates/scripts/sync_knowledge_graph.py) for the code graph. Used by
    `_dedup_insert` to SKIP a `replace()` when the object is byte-identical to
    what's already indexed, eliminating needless HNSW vector tombstones.

    Args:
        collection_name: full per-project collection name (e.g.
            ``MyProject_CodeFunction``) OR a bare base name. We match on the
            base suffix so the per-project prefix is irrelevant.
        properties: the ``insert_params["properties"]`` dict for this object.

    Returns:
        Hex SHA-256 digest. Deterministic for identical content across runs,
        OSes, and machines (uses POSIX-normalized inputs the callers already
        produce). Never raises — a malformed value degrades into its ``str()``.

    Field selection: per `_CONTENT_HASH_FIELDS` for the recognised base names;
    otherwise every scalar/list property except `_CONTENT_HASH_EXCLUDE`. The
    fallback errs toward hashing MORE fields, which can only cause an extra
    (correct) write — never an incorrect skip.
    """
    base = ""
    for known in _CONTENT_HASH_FIELDS:
        if collection_name == known or collection_name.endswith(known):
            base = known
            break

    if base:
        fields = _CONTENT_HASH_FIELDS[base]
    else:
        # Unknown collection → hash all non-excluded keys in sorted order so
        # the digest is stable regardless of dict insertion order.
        fields = sorted(k for k in properties.keys() if k not in _CONTENT_HASH_EXCLUDE)

    parts = [base]
    for key in fields:
        parts.append(key)
        parts.append(_stable_scalar(properties.get(key)))
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
