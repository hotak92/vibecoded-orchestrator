# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# @idempotent: yes
# @destructive: no
# @classification: derived
"""codegraph_collection schema edge v7 → v8 (v0.2.102 S16-4 / S16-5).

ADDITIVE, data-preserving: gives the project's ``<prefix>_CodeInteraction``
class the ``project`` property the other four code-graph classes were born
with, then backfills it on the existing rows. NO drop, NO re-embed, NO vector
touched.

Why this edge exists
--------------------
CodeInteraction only ever carried ``source_project``. Every reader that scopes
code-graph rows by ``project`` therefore failed on it or skipped it without a
word:

  * ``search_code_graph`` filters each of the project's own classes with
    ``project == <project>``; on CodeInteraction that is a "no such prop"
    error, which the search reported as ``schema_missing: <prefix>_CodeInteraction``
    although the class exists;
  * the identity sweep (``codegraph_vector_copy._distinct_stale_identities``)
    logged ``no such prop with name 'project'`` and skipped the class each run;
  * the stale prune and the deleted-file prune scope by ``project`` too, so
    interaction rows of deleted files were never removed.

What it does, per run
---------------------
1. ``add_property(project, TEXT, skip_vectorization)`` on the class when absent
   — through the ONE ensure home, ``vco_lib.codegraph_schema`` (the same spec
   the analyzer's create path and its ``_ensure_interaction_project_property``
   use; ``tests/test_codegraph_schema_parity.py`` locks them together).
2. Backfill: every row whose ``project`` is NULL/empty gets the prefix — the
   identity the analyzer stamps on this prefix family (a per-project class
   holds only that project's rows). Rows that already carry a value are never
   rewritten. Each row is one independent PATCH of that single property, so
   an interrupted run is resumed by the next one.

Class absent (never analyzed, or analyzed by a pre-v0.2.18 analyzer): skipped
with a log line. The edge does NOT create the class — the analyzer's
``create_collections`` is its one creator and gives it ``project`` at birth.
The version still advances: there is nothing left for this edge to do.

Exit codes (the runner keys the version advance on exit 0 + ``EDGE_APPLIED``):
  * 0 — the property is present and every row lacking it was backfilled (or the
    class is absent).
  * non-zero — Weaviate unreachable, a broken install (vco_lib not importable),
    an ``add_property`` error, or one-or-more backfill failures. The runner then
    does NOT advance the recorded version and re-runs the edge next update; the
    re-run only touches the rows still lacking the value.
"""

from __future__ import annotations

import os
import sys

# Props THIS edge owns (the v8 addition). Authoritative spec:
# vco_lib.codegraph_schema.CODEGRAPH_PROPERTY_SPECS filtered to this subset.
_V8_PROPS = ("project",)

_BROKEN_INSTALL_MSG = (
    "7_to_8: vco_lib not importable — VCO install is broken; run install.py. "
    "(The migration runner executes this edge from the project root where "
    "vco_lib is editable-installed on every healthy install.)"
)


def _resolve_codegraph_prefix() -> str | None:
    """The project's codegraph class prefix from env, through the ONE home
    ``vco_lib.codegraph_edge_env.resolve_codegraph_prefix`` (v0.2.102 S2 — the
    four edges and the runner used to carry their own copies). ``None`` when no
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
    """The shared ensure loop (``vco_lib.codegraph_schema``) for this edge's
    subset. Absent classes report ``"absent"`` and are left alone."""
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_schema import ensure_codegraph_properties

    return ensure_codegraph_properties(client, prefix, props_subset=_V8_PROPS)


def _backfill(client, class_name: str, value: str) -> tuple:
    """Backfill ``project`` on the rows lacking it (shared helper; resumable,
    per-row soft-fail). Returns ``(updated, failures)``."""
    sys.path.insert(0, os.getcwd())
    from vco_lib.codegraph_schema import backfill_missing_property

    return backfill_missing_property(
        client.collections.get(class_name), "project", value,
    )


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
        print("7_to_8: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to patch")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"7_to_8: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    try:
        try:
            results = _ensure_props(client, prefix)
        except ImportError as exc:
            print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
            return 1
        except Exception as exc:
            failed_on = getattr(exc, "class_name", "a code-graph class")
            print(f"7_to_8: add_property failed on {failed_on}: {exc}", file=sys.stderr)
            return 1
        for class_name, status in results.items():
            if status == "absent":
                print(
                    f"7_to_8: {class_name} does not exist — skipped (the analyzer "
                    "creates it with `project` on the next analyze)"
                )
                continue
            try:
                updated, failures = _backfill(client, class_name, prefix)
            except ImportError as exc:
                print(f"{_BROKEN_INSTALL_MSG} ({exc})", file=sys.stderr)
                return 1
            print(
                f"7_to_8: {class_name} has `project`; backfilled {updated} row(s) "
                f"with {prefix!r}"
            )
            if failures:
                print(
                    f"7_to_8: {failures} backfill failure(s) on {class_name} — "
                    "deferring version advance (the re-run resumes)",
                    file=sys.stderr,
                )
                return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    print("EDGE_APPLIED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
