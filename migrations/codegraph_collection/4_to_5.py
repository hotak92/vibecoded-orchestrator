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

P2d (v0.2.75): the property SPECS + the ensure-if-missing loop live in ONE
shared home — ``vco_lib/codegraph_schema.py``. NEW PROPS: add them to
``vco_lib/codegraph_schema.py`` FIRST (``tests/test_codegraph_schema_parity.py``
locks the table against the analyzer's ``_ensure_*`` helpers AND this edge's
fallback), then extend the edge subset + fallback. The inline
``_FALLBACK_SPECS`` / ``_fallback_ensure`` below cover only the
vco_lib-unimportable window (torn checkout) — keep them MINIMAL and in
MUST-MATCH lock-step with the shared home.
"""

from __future__ import annotations

import os
import re
import sys


# Props THIS edge owns (the v5 additions). Authoritative specs:
# vco_lib.codegraph_schema.CODEGRAPH_PROPERTY_SPECS filtered to this subset.
_V5_PROPS = ("embed_revision", "chunk_num", "total_chunks")

_EMBED_REVISION_DESC = (
    "Embedding-generation revision this row's vector(s) were produced under "
    "(P7 revision-gated forced resync; see CODEGRAPH_EMBED_REVISION)"
)
_CHUNK_NUM_DESC = "0-indexed chunk number within this entity (0 for single-chunk)"
_TOTAL_CHUNKS_DESC = "Total chunk count for this entity (1 for single-chunk)"

# MUST-MATCH vco_lib/codegraph_schema.py: ``specs_subset(_V5_PROPS)`` — the
# class-name-suffix → ((prop, DataType name, description), ...) projection.
# Consumed ONLY by the inline fallback; parity is asserted by
# tests/test_codegraph_schema_parity.py.
_FALLBACK_SPECS = {
    "CodeModule": (("embed_revision", "INT", _EMBED_REVISION_DESC),),
    "CodeClass": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        ("chunk_num", "INT", _CHUNK_NUM_DESC),
        ("total_chunks", "INT", _TOTAL_CHUNKS_DESC),
    ),
    "CodeFunction": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        ("chunk_num", "INT", _CHUNK_NUM_DESC),
        ("total_chunks", "INT", _TOTAL_CHUNKS_DESC),
    ),
    "CodeAPI": (("embed_revision", "INT", _EMBED_REVISION_DESC),),
    "CodeInteraction": (("embed_revision", "INT", _EMBED_REVISION_DESC),),
}


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


def _fallback_ensure(client, prefix: str, specs) -> dict:
    """MINIMAL inline copy of
    ``vco_lib.codegraph_schema.ensure_codegraph_properties`` — MUST-MATCH its
    semantics (absent class skipped → ``"absent"``; already-present prop
    skipped silently; skip-vectorized add; present class → ``"ensured"``).
    Runs ONLY when vco_lib is unimportable (torn checkout)."""
    from weaviate.classes.config import DataType, Property

    results = {}
    for suffix, prop_specs in specs.items():
        class_name = f"{prefix}_{suffix}"
        if not client.collections.exists(class_name):
            results[class_name] = "absent"
            continue
        coll = client.collections.get(class_name)
        existing = {p.name for p in coll.config.get().properties}
        for prop_name, dtype_name, desc in prop_specs:
            if prop_name in existing:
                continue
            coll.config.add_property(
                Property(
                    name=prop_name,
                    data_type=getattr(DataType, dtype_name),
                    description=desc,
                    skip_vectorization=True,
                )
            )
        results[class_name] = "ensured"
    return results


def _ensure_props(client, prefix: str) -> dict:
    """Run the shared property-ensure loop, falling back to the inline copy.

    Import-with-fallback deliberately mirrors ``_resolve_codegraph_prefix``'s
    ``canonical_class_prefix`` shape: the runner executes this edge with
    ``cwd=<project_root>``, so ``vco_lib`` is importable in the common case.
    Only the IMPORT is guarded — a genuine ensure failure (either path)
    propagates to ``main()`` → non-zero exit → the runner defers + retries.
    """
    ensure_fn = None
    try:
        sys.path.insert(0, os.getcwd())
        from vco_lib.codegraph_schema import (
            ensure_codegraph_properties as ensure_fn,
        )
    except Exception:
        ensure_fn = None
    if ensure_fn is not None:
        return ensure_fn(client, prefix, props_subset=_V5_PROPS)
    return _fallback_ensure(client, prefix, _FALLBACK_SPECS)


def main() -> int:
    prefix = _resolve_codegraph_prefix()
    if not prefix:
        # No codegraph project resolvable from env → nothing to patch for this
        # project. v0.2.74 HIGH-2: print the EDGE_NOOP_NO_PREFIX sentinel so the
        # runner does NOT falsely advance the recorded version on a rc=0 that
        # touched NOTHING (the A1 second-order trap). The runner surfaces a
        # deferral + retries once the prefix is threaded into the edge env by A1.
        print("EDGE_NOOP_NO_PREFIX=1")
        print("4_to_5: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to patch")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"4_to_5: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    try:
        try:
            # P2d: the ensure-if-missing loop lives in vco_lib/codegraph_schema
            # (inline fallback when unimportable). Absent classes are skipped —
            # they will be born v5-shaped by create_collections.
            results = _ensure_props(client, prefix)
        except Exception as exc:
            # A genuine add_property error is a real failure — the runner
            # must NOT advance the version on a half-applied edge. The shared
            # loop attaches the failing class via ``.class_name``.
            failed_on = getattr(exc, "class_name", "a code-graph class")
            print(f"4_to_5: add_property failed on {failed_on}: {exc}", file=sys.stderr)
            return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    for class_name, status in results.items():
        if status == "ensured":
            print(f"4_to_5: {class_name} at v5 shape (props present/added)")
    # v0.2.74 HIGH-2: the real body ran (props present/added on every present
    # class, or the classes are absent-but-scope-was-resolvable) → safe to
    # advance the recorded version.
    print("EDGE_APPLIED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
