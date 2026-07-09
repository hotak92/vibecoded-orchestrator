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

P2d (v0.2.75): the property SPECS + the ensure-if-missing loop live in ONE
shared home — ``vco_lib/codegraph_schema.py``. NEW PROPS: add them to
``vco_lib/codegraph_schema.py`` FIRST (``tests/test_codegraph_schema_parity.py``
locks the table against the analyzer's ``_ensure_*`` helpers), then extend the
edge subset ``_V6_PROPS``.

X-1 / v0.2.76 (ruling #1 — loud-fail, never silent-degrade): the previous inline
``_FALLBACK_SPECS`` / ``_fallback_ensure`` copies + the ``canonical_class_prefix``
legacy-regex fallback are GONE. The runner executes this edge with
``cwd=<project_root>`` where ``vco_lib`` is editable-installed on every healthy
install, so a failing ``import vco_lib`` means a BROKEN install — surfaced as a
loud stderr message + non-zero exit (which the runner treats as defer-and-retry),
NOT a quiet inline-copy degrade that masks the breakage.
"""

from __future__ import annotations

import os
import re
import sys


# Props THIS edge owns (the v6 additions). Authoritative specs:
# vco_lib.codegraph_schema.CODEGRAPH_PROPERTY_SPECS filtered to this subset.
_V6_PROPS = ("is_test", "n_callers")

# X-1 / v0.2.76: the loud broken-install message shared by both vco_lib import
# sites (prefix resolution + property ensure).
_BROKEN_INSTALL_MSG = (
    "5_to_6: vco_lib not importable — VCO install is broken; run install.py. "
    "(The migration runner executes this edge from the project root where "
    "vco_lib is editable-installed on every healthy install.)"
)


def _resolve_codegraph_prefix() -> str | None:
    """Resolve the project's codegraph class prefix from env, mirroring
    ``schema_migration_runner._resolve_codegraph_prefix`` /
    ``analyze_code_graph._sanitize_collection_prefix``.

    ``CODE_GRAPH_PROJECT`` (already the sanitized prefix the analyzer uses)
    wins; else derive it from ``PROJECT_NAME`` via the SAME
    ``canonical_class_prefix`` helper so we probe the class name the analyzer
    actually wrote. Returns ``None`` when neither is set (the runner then has
    nothing to patch for this project).

    X-1 / v0.2.76 (ruling #1): a failing ``import vco_lib`` is a broken install
    — it raises loudly (→ ``main`` prints + exits non-zero), never a legacy
    regex fallback.
    """
    cg = (os.environ.get("CODE_GRAPH_PROJECT") or "").strip()
    if cg:
        return cg
    pn = (os.environ.get("PROJECT_NAME") or "").strip()
    if not pn:
        return None
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_naming import canonical_class_prefix

    return canonical_class_prefix(pn)


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


def _ensure_props(client, prefix: str) -> dict:
    """Run the shared property-ensure loop from ``vco_lib.codegraph_schema``.

    X-1 / v0.2.76 (ruling #1): imports the shared home DIRECTLY. The runner
    executes this edge with ``cwd=<project_root>`` where ``vco_lib`` is
    editable-installed on every healthy install, so a failing import means a
    BROKEN install — raised loudly (→ ``main`` prints the broken-install
    message + exits non-zero → the runner defers + retries), never a silent
    inline-copy degrade. A genuine ensure failure (Weaviate error) likewise
    propagates to ``main()``.
    """
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_schema import ensure_codegraph_properties

    return ensure_codegraph_properties(client, prefix, props_subset=_V6_PROPS)


def main() -> int:
    # X-1 / v0.2.76 (ruling #1): resolving the prefix imports vco_lib. A broken
    # install fails LOUDLY here (before any Weaviate work) → non-zero exit → the
    # runner defers + retries, never a silent legacy-regex degrade.
    try:
        prefix = _resolve_codegraph_prefix()
    except ImportError as exc:
        print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
        return 1
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
        try:
            # P2d: the ensure-if-missing loop lives in vco_lib/codegraph_schema.
            # Absent classes are skipped — they will be born v6-shaped by
            # create_collections.
            results = _ensure_props(client, prefix)
        except ImportError as exc:
            # X-1 / v0.2.76 (ruling #1): a broken install (vco_lib gone) is a
            # loud failure, not a silent inline-copy degrade.
            print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
            return 1
        except Exception as exc:
            # A genuine add_property error is a real failure — the runner
            # must NOT advance the version on a half-applied edge. The shared
            # loop attaches the failing class via ``.class_name``.
            failed_on = getattr(exc, "class_name", "a code-graph class")
            print(f"5_to_6: add_property failed on {failed_on}: {exc}", file=sys.stderr)
            return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    for class_name, status in results.items():
        if status == "ensured":
            print(f"5_to_6: {class_name} at v6 shape (props present/added)")
    # v0.2.74 HIGH-2: the real body ran (props present/added on every present
    # class, or the classes are absent-but-scope-was-resolvable) → safe to
    # advance the recorded version.
    print("EDGE_APPLIED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
