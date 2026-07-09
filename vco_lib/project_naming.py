# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canonical project-name → Weaviate-class-prefix sanitizer
(underscore-PRESERVING rule).

**SSOT classification (NEW-10 / DEDUP-6, v0.2.53)**: This is the canonical
SSOT for the underscore-PRESERVING rule used by code-graph collection
names (``CodeFunction``, ``CodeClass``, etc. — prefixed with the
project's name). The companion SSOT for the underscore-DROPPING rule
(KG / Development / Diagrams collections) lives at
``vco_lib.project_init.sanitize_for_weaviate_class``. The two rules are
intentionally distinct because production Weaviate schemas contain
classes named by BOTH rules — see the module docstring of
``project_init.py``'s sanitizer for the migration rationale.

DEDUP-6 (v0.2.53) downstream callers that now route through this
function:
  * ``vco_lib.codegraph_to_mermaid._sanitize_collection_prefix``
    (thin wrapper that catches ``ValueError`` for malformed inputs and
    falls back to the legacy regex behaviour).
  * ``templates/scripts/analyze_code_graph.py`` (already used this).

Single source of truth, callable from both Python and Rust (Rust calls the
matching `project_naming.rs` port and the parity test pins them together
against a shared JSON fixture).

History (2026-05-17 / v0.2.15 / bug 0.7):

Before this module there were three competing sanitizers — each shipped
in production, each producing a different prefix for the same project
name:

  1. `vco_lib.project_init.sanitize_for_weaviate_class` ── splits on
     `[^A-Za-z0-9]+` (treats underscore as a separator), PascalCases
     each segment, concatenates.
       "Camel_Case"             -> "CamelCase"    (loses the underscore)
       "VibeCoded Orchestrator" -> "VibeCodedOrchestrator"
       "Foo-Bar"                -> "FooBar"

  2. `analyze_code_graph.py:_sanitize_collection_prefix` ── replaces any
     non-`[A-Za-z0-9_]` with `_`, then title-cases the first character.
     Crucially this PRESERVES underscores, replaces dashes with
     underscores, and replaces spaces with underscores too:
       "Camel_Case"             -> "Camel_Case"   (preserved)
       "VibeCoded Orchestrator" -> "VibeCoded_Orchestrator"  (space->_)
       "Foo-Bar"                -> "Foo_Bar"       (dash->_)

  3. Launcher `sanitize_kg_collection` (Rust) ── same algorithm as (1)
     above, ported to Rust. Used by the wizard to display the "current
     prefix:" line in the per-project settings.

The fallout was a real wedge (2026-05-17): the wizard told the user
"current prefix: VibeCodedOrchestrator", the analyze script wrote
under `VibeCoded_Orchestrator_*`, and a prior generation had already
created `VibecodedOrchestrator_*` (lowercase-c, from the fallback path
that fed the repo folder name `vibecoded-orchestrator` through (2)).
Weaviate's class-name uniqueness is CASE-INSENSITIVE, so the third
collision (`VibeCoded_Orchestrator` ≈ `Vibecoded_orchestrator`) caused
`Collection.create()` to keep failing forever.

The canonical sanitizer here matches schema-on-disk observed across
existing `base`-host installs (where Python-side `_sanitize_collection_prefix`
output is the de-facto truth — Weaviate already has `Camel_Case_*`
classes). Drop-spaces (no underscore insertion) was chosen over the
Rust-style "PascalCase + drop-separators" because the latter loses the
underscore from underscored project names and produces case-collision risk.

This module is the SINGLE source of truth. `analyze_code_graph.py` imports
from here; the legacy `_sanitize_collection_prefix` is kept only as a
thin wrapper for back-compat with external callers. Launcher's
`project_naming.rs` is the Rust port and is pinned against this
implementation by `tests/test_project_naming_parity.py` +
`launcher/src-tauri/tests/project_naming_parity.rs` (both consume
`tests/fixtures/project_naming.json`).
"""

# X-1 / v0.2.76: the underscore-PRESERVING rule now lives in the ONE naming
# home ``vco_lib.codegraph_naming`` (alongside the underscore-DROPPING
# ``sanitize_for_weaviate_class``). This module re-exports it so the
# historical import path ``from vco_lib.project_naming import
# canonical_class_prefix`` keeps working for existing callers (the migration
# edges, the analyzer template's bootstrap, codegraph_to_mermaid, the schema
# migration runner, the weaviate MCP, and the parity tests). New code should
# import from ``vco_lib.codegraph_naming`` directly. See that module's
# docstring for the full rules-and-rationale write-up (unchanged behaviour).
from vco_lib.codegraph_naming import canonical_class_prefix  # noqa: E402

__all__ = ["canonical_class_prefix"]
