# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# @idempotent: yes
# @destructive: no
# @classification: derived
"""codegraph_collection schema edge v4 → v5 (v0.2.72 P3/P7).

ADDITIVE, data-preserving: adds the chunking + revision metadata props to the
project's five live code-graph classes WITHOUT dropping or re-embedding
anything. This is the framework-owned half of the P3/P7 change (the schema
shape); the actual per-row re-embed for the ~7-9% of functions/classes that
now CHUNK is handled INCREMENTALLY by the analyzer's per-object
``CODEGRAPH_EMBED_REVISION`` gate (``analyze_code_graph._write_one_object`` +
``vco_lib/codegraph_resync.py``), NOT here — the framework has no
selective-per-row re-embed verb and a full CodeSage re-embed would be
needlessly expensive for a minority tail.

New props (all ``skip_vectorization`` INT — metadata, no re-index):
  * ``embed_revision``  — all 5 classes (generation marker for the resync gate)
  * ``chunk_num``       — CodeFunction + CodeClass (0-indexed chunk within entity)
  * ``total_chunks``    — CodeFunction + CodeClass (chunk count for the entity)

Adding a property is a non-destructive metadata operation. The edge is
idempotent (an already-present prop is skipped) and runs once per artifact_name
(the runner invokes it once per resolved ``<prefix>_Code*`` class name — five
times per project — so this script patches ALL five classes on each run and is
trivially safe to repeat). A class that does not exist yet is skipped (it will
be born v5-shaped by ``analyze_code_graph.create_collections``).

Exit codes (the runner keys the version advance on exit 0):
  * 0 — every present class carries the new props (or the classes are absent).
  * non-zero — Weaviate unreachable / a genuine add_property error. The runner
    then does NOT advance the recorded version and re-attempts next update.

This edge deliberately mirrors ``analyze_code_graph._ensure_embed_revision_property``
/ ``_ensure_chunk_props_property`` (which stay as belt-and-suspenders for
Weaviate-down / standalone-analyzer windows the edge can't cover) so a project
reaches the v5 shape whichever path runs first.
"""

from __future__ import annotations

import os
import re
import sys


# Props to add, keyed by which classes get them. Kept in lock-step with
# analyze_code_graph.py's create_collections + _ensure_* helpers.
_ALL_CLASSES = ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction")
_CHUNKED_CLASSES = ("CodeFunction", "CodeClass")

_EMBED_REVISION_DESC = (
    "Embedding-generation revision this row's vector(s) were produced under "
    "(P7 revision-gated forced resync; see CODEGRAPH_EMBED_REVISION)"
)
_CHUNK_NUM_DESC = "0-indexed chunk number within this entity (0 for single-chunk)"
_TOTAL_CHUNKS_DESC = "Total chunk count for this entity (1 for single-chunk)"


def _resolve_codegraph_prefix() -> str | None:
    """Resolve the project's codegraph class prefix from env, mirroring
    ``schema_migration_runner._resolve_codegraph_prefix`` /
    ``analyze_code_graph._sanitize_collection_prefix``.

    ``CODE_GRAPH_PROJECT`` (already the sanitized prefix the analyzer uses)
    wins; else derive it from ``PROJECT_NAME`` via the SAME
    ``canonical_class_prefix`` helper + legacy regex fallback so we probe the
    class name the analyzer actually wrote. Returns ``None`` when neither is
    set (the runner then has nothing to patch for this project).
    """
    cg = (os.environ.get("CODE_GRAPH_PROJECT") or "").strip()
    if cg:
        return cg
    pn = (os.environ.get("PROJECT_NAME") or "").strip()
    if not pn:
        return None
    try:
        # Prefer the shared canonical helper (must match the analyzer).
        sys.path.insert(0, os.getcwd())
        from vco_lib.project_naming import canonical_class_prefix

        return canonical_class_prefix(pn)
    except Exception:
        # Legacy fallback identical to _sanitize_collection_prefix's:
        # non-[A-Za-z0-9_] -> "_", uppercase a leading lowercase letter.
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", pn)
        if sanitized and sanitized[0].isalpha() and not sanitized[0].isupper():
            sanitized = sanitized[0].upper() + sanitized[1:]
        return sanitized or None


def _connect():
    """Connect to Weaviate the SAME way the analyzer does (custom host/port).

    Reads ``WEAVIATE_URL`` (default localhost:8081) + ``GRPC_PORT`` (default
    50052). Returns the client, or raises on failure (→ non-zero exit → the
    runner defers + retries).
    """
    import weaviate  # local import: only needed when the edge actually runs

    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    m = re.match(r"^(https?)://([^:/]+)(?::(\d+))?", url)
    scheme, host, port = ("http", "localhost", 8081)
    if m:
        scheme = m.group(1)
        host = m.group(2)
        port = int(m.group(3)) if m.group(3) else (443 if scheme == "https" else 8081)
    grpc_port = int(os.environ.get("GRPC_PORT") or os.environ.get("WEAVIATE_GRPC_PORT") or 50052)
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=(scheme == "https"),
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=(scheme == "https"),
    )


def _add_int_prop_if_absent(coll, prop_name: str, description: str) -> None:
    """Idempotent ``add_property`` of a skip-vectorized INT prop. No-op if the
    property already exists. Mirrors analyze_code_graph._ensure_* helpers."""
    from weaviate.classes.config import DataType, Property

    config = coll.config.get()
    if prop_name in {p.name for p in config.properties}:
        return
    coll.config.add_property(
        Property(
            name=prop_name,
            data_type=DataType.INT,
            description=description,
            skip_vectorization=True,
        )
    )


def main() -> int:
    prefix = _resolve_codegraph_prefix()
    if not prefix:
        # No codegraph project resolvable from env → nothing to patch for this
        # project. Treat as success (the runner still advances the version; a
        # fresh install without codegraph env simply has no classes to touch).
        print("4_to_5: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to patch")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"4_to_5: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    try:
        for base in _ALL_CLASSES:
            class_name = f"{prefix}_{base}"
            try:
                if not client.collections.exists(class_name):
                    # Absent → will be born v5-shaped by create_collections.
                    continue
                coll = client.collections.get(class_name)
                _add_int_prop_if_absent(coll, "embed_revision", _EMBED_REVISION_DESC)
                if base in _CHUNKED_CLASSES:
                    _add_int_prop_if_absent(coll, "chunk_num", _CHUNK_NUM_DESC)
                    _add_int_prop_if_absent(coll, "total_chunks", _TOTAL_CHUNKS_DESC)
                print(f"4_to_5: {class_name} at v5 shape (props present/added)")
            except Exception as exc:
                # A genuine per-class add_property error is a real failure — the
                # runner must NOT advance the version on a half-applied edge.
                print(f"4_to_5: add_property failed on {class_name}: {exc}", file=sys.stderr)
                return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
