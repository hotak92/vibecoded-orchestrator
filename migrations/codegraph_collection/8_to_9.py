# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# @idempotent: yes
# @destructive: no
# @classification: derived
"""codegraph_collection schema edge v8 → v9 (v0.2.102 W6-O, review finding F-7).

ADDITIVE, data-preserving: gives the project's ``<prefix>_CodeAPI`` class the
``is_test`` (BOOL) property the Module/Class/Function classes gained in v6.
NO drop, NO re-embed, NO vector touched.

Why this edge exists
--------------------
``CodeAPI`` had no test flag, so an endpoint inventory built from it could not
leave test code out: on this project's own graph 19 of 22 CodeAPI rows were
routes declared in ``tests/fixtures/...`` files. Searches and any API document
generated from the graph need ``is_test`` to exclude them.

Backfill of existing rows is DATA-side, exactly as for the v6 ``is_test``
(``5_to_6.py``): the property is a pure function of the stored ``file_path``
(``weaviate_mcp.code_ranking.is_test_path``), and
``vco_lib.codegraph_resync.backfill_codegraph_metadata`` — the detached child
every background re-sync spawns, which the v0.2.102 extractor-generation rung
triggers — fills it on rows lacking it. A legacy row with no ``file_path``
gets it when the anchor backfill (same child) or the analyzer's skipped-row
heal (``codegraph_guards.heal_skipped_row``) supplies the path. New rows are
stamped at write time by the analyzer's ``_dedup_insert`` choke point.

The ensure covers every class whose spec lists ``is_test``; the three that got
it in v6 already have it and are skipped (idempotent). A class that does not
exist yet is skipped too (the analyzer's ensure gives it the property on its
first analyze).

Exit codes (the runner keys the version advance on exit 0 + ``EDGE_APPLIED``):
  * 0 — every present class carries ``is_test`` (or the classes are absent).
  * non-zero — Weaviate unreachable, a broken install (vco_lib not importable)
    or a genuine ``add_property`` error. The runner then does NOT advance the
    recorded version and re-attempts on the next update.
"""

from __future__ import annotations

import os
import sys

# Props THIS edge owns (the v9 addition, on CodeAPI). Authoritative specs:
# vco_lib.codegraph_schema.CODEGRAPH_PROPERTY_SPECS filtered to this subset.
_V9_PROPS = ("is_test",)

_BROKEN_INSTALL_MSG = (
    "8_to_9: vco_lib not importable — VCO install is broken; run install.py. "
    "(The migration runner executes this edge from the project root where "
    "vco_lib is editable-installed on every healthy install.)"
)


def _resolve_codegraph_prefix() -> str | None:
    """The project's codegraph class prefix, through the ONE home
    ``vco_lib.codegraph_edge_env.resolve_codegraph_prefix``. ``None`` when no
    scope is resolvable. A failing ``import vco_lib`` is a broken install and
    propagates (→ loud non-zero exit)."""
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_edge_env import resolve_codegraph_prefix

    return resolve_codegraph_prefix()


def _connect():
    """Weaviate via the ONE connect factory (``vco_lib.codegraph_edge_env``).
    Raises on failure (→ non-zero exit → the runner defers + retries)."""
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_edge_env import connect_from_env

    return connect_from_env()


def _ensure_props(client, prefix: str) -> dict:
    """The shared property-ensure loop (``vco_lib.codegraph_schema``) for this
    edge's subset. Absent classes report ``"absent"`` and are left alone."""
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_schema import ensure_codegraph_properties

    return ensure_codegraph_properties(client, prefix, props_subset=_V9_PROPS)


def main() -> int:
    try:
        prefix = _resolve_codegraph_prefix()
    except ImportError as exc:
        print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
        return 1
    if not prefix:
        # No codegraph scope from env → touched nothing. The sentinel stops the
        # runner from advancing the recorded version on this rc=0.
        print("EDGE_NOOP_NO_PREFIX=1")
        print("8_to_9: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to patch")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"8_to_9: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    try:
        try:
            results = _ensure_props(client, prefix)
        except ImportError as exc:
            print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
            return 1
        except Exception as exc:
            failed_on = getattr(exc, "class_name", "a code-graph class")
            print(f"8_to_9: add_property failed on {failed_on}: {exc}", file=sys.stderr)
            return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    for class_name, status in results.items():
        if status == "ensured":
            print(f"8_to_9: {class_name} carries `is_test` (present or added)")
        else:
            print(f"8_to_9: {class_name} does not exist — skipped")
    print("EDGE_APPLIED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
