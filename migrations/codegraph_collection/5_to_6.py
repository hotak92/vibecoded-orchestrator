# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# @idempotent: yes
# @destructive: no
# @classification: derived
"""codegraph_collection schema edge v5 → v6 (v0.2.73 M1/M4).

ADDITIVE, data-preserving: adds the retrieval-metadata props to the project's
live code-graph classes WITHOUT dropping or re-embedding anything (NO drop,
NO re-embed — these are render/rank metadata, not embedding inputs).
Backfill of existing rows is DATA-side, not framework-side:
``vco_lib.codegraph_resync.backfill_codegraph_metadata`` populates ``is_test``
(pure function of the stored path); ``create_cross_references`` self-heals
``n_callers`` on every analyze.

New props (all ``skip_vectorization`` — metadata, no re-index):
  * ``is_test``   (BOOL) — CodeFunction + CodeClass + CodeModule (test/spec/
                   fixture flag from the path heuristic; retrieval downweight)
  * ``n_callers`` (INT)  — CodeFunction only (inbound call count,
                   Python-resolved, project-internal; render-time context)

Adding a property is a non-destructive metadata operation. The edge is
idempotent (an already-present prop is skipped) and trivially safe to repeat.
A class that does not exist yet is skipped (it will be born v6-shaped by
``analyze_code_graph.create_collections``).

Exit codes (the runner keys the version advance on exit 0):
  * 0 — every present class carries the new props (or the classes are absent).
  * non-zero — Weaviate unreachable / a genuine add_property error. The runner
    then does NOT advance the recorded version and re-attempts next update.

This edge deliberately mirrors ``analyze_code_graph._ensure_is_test_property``
/ ``_ensure_n_callers_property`` (which stay as belt-and-suspenders for
Weaviate-down / standalone-analyzer windows the edge can't cover) so a project
reaches the v6 shape whichever path runs first.
"""

from __future__ import annotations

import os
import re
import sys


# Props to add, keyed by which classes get them. Kept in lock-step with
# analyze_code_graph.py's create_collections + _ensure_* helpers.
_IS_TEST_CLASSES = ("CodeModule", "CodeClass", "CodeFunction")
_N_CALLERS_CLASSES = ("CodeFunction",)

_IS_TEST_DESC = (
    "True when the source file is a test/spec/fixture "
    "(path heuristic; retrieval downweight)"
)
_N_CALLERS_DESC = (
    "Inbound call count (Python-resolved, project-internal; "
    "render-time context)"
)


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


def _add_prop_if_absent(coll, prop_name: str, data_type_name: str,
                        description: str) -> None:
    """Idempotent ``add_property`` of a skip-vectorized prop. No-op if the
    property already exists. Generalizes 4_to_5's ``_add_int_prop_if_absent``
    so BOOL + INT share one helper. Mirrors analyze_code_graph._ensure_*."""
    from weaviate.classes.config import DataType, Property

    config = coll.config.get()
    if prop_name in {p.name for p in config.properties}:
        return
    coll.config.add_property(
        Property(
            name=prop_name,
            data_type=getattr(DataType, data_type_name),
            description=description,
            skip_vectorization=True,
        )
    )


def main() -> int:
    prefix = _resolve_codegraph_prefix()
    if not prefix:
        # No codegraph project resolvable from env → nothing to patch for this
        # project. v0.2.74 HIGH-2: print the EDGE_NOOP_NO_PREFIX sentinel so the
        # runner does NOT falsely advance the recorded version on a rc=0 that
        # touched NOTHING (the A1 second-order trap). The runner surfaces a
        # deferral + retries once the prefix is threaded into the edge env by A1.
        print("EDGE_NOOP_NO_PREFIX=1")
        print("5_to_6: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to patch")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"5_to_6: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    try:
        for base in _IS_TEST_CLASSES:
            class_name = f"{prefix}_{base}"
            try:
                if not client.collections.exists(class_name):
                    # Absent → will be born v6-shaped by create_collections.
                    continue
                coll = client.collections.get(class_name)
                _add_prop_if_absent(coll, "is_test", "BOOL", _IS_TEST_DESC)
                if base in _N_CALLERS_CLASSES:
                    _add_prop_if_absent(
                        coll, "n_callers", "INT", _N_CALLERS_DESC
                    )
                print(f"5_to_6: {class_name} at v6 shape (props present/added)")
            except Exception as exc:
                # A genuine per-class add_property error is a real failure — the
                # runner must NOT advance the version on a half-applied edge.
                print(f"5_to_6: add_property failed on {class_name}: {exc}", file=sys.stderr)
                return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    # v0.2.74 HIGH-2: the real body ran (props present/added on every present
    # class, or the classes are absent-but-scope-was-resolvable) → safe to
    # advance the recorded version.
    print("EDGE_APPLIED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
