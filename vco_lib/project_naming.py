# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canonical project-name → Weaviate-class-prefix sanitizer.

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
       "SimRacing_AI"           -> "SimRacingAI"   (loses the underscore)
       "VibeCoded Orchestrator" -> "VibeCodedOrchestrator"
       "Foo-Bar"                -> "FooBar"

  2. `analyze_code_graph.py:_sanitize_collection_prefix` ── replaces any
     non-`[A-Za-z0-9_]` with `_`, then title-cases the first character.
     Crucially this PRESERVES underscores, replaces dashes with
     underscores, and replaces spaces with underscores too:
       "SimRacing_AI"           -> "SimRacing_AI"  (preserved)
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
output is the de-facto truth — Weaviate already has `SimRacing_AI_*`
classes). Drop-spaces (no underscore insertion) was chosen over the
Rust-style "PascalCase + drop-separators" because the latter loses the
underscore from `SimRacing_AI` and produces case-collision risk.

This module is the SINGLE source of truth. `analyze_code_graph.py` imports
from here; the legacy `_sanitize_collection_prefix` is kept only as a
thin wrapper for back-compat with external callers. Launcher's
`project_naming.rs` is the Rust port and is pinned against this
implementation by `tests/test_project_naming_parity.py` +
`launcher/src-tauri/tests/project_naming_parity.rs` (both consume
`tests/fixtures/project_naming.json`).
"""

import re

__all__ = ["canonical_class_prefix"]


# Matches any single character that is NOT alphanumeric or underscore.
# Spaces are handled separately (whitespace-split before this regex runs);
# everything else this pattern matches gets replaced with an underscore.
_NON_ALNUM_OR_UNDERSCORE = re.compile(r"[^A-Za-z0-9_]")


def canonical_class_prefix(project_name: str) -> str:
    """Convert a human project name into a Weaviate class prefix.

    The result is the exact prefix used to build per-project Weaviate
    class names: ``<prefix>_KnowledgeGraph``, ``<prefix>_CodeFunction``,
    etc.

    Rules:
        1. Strip leading/trailing whitespace.
        2. Split on whitespace runs into word parts, then PascalCase each
           part (uppercase the first character, preserve the rest) and
           concatenate. This is the "drop-spaces with implicit word-
           boundary capitalization" rule:
             ``"foo bar"``                → ``"FooBar"``
             ``"VibeCoded Orchestrator"`` → ``"VibeCodedOrchestrator"``
             ``"  spaced   out  "``       → ``"SpacedOut"``
        3. Replace remaining non-``[A-Za-z0-9_]`` characters with a single
           underscore. So ``"Foo-Bar"`` becomes ``"Foo_Bar"`` (dash → ``_``).
           Underscores already present in the input are preserved verbatim
           — ``"SimRacing_AI"`` stays ``"SimRacing_AI"``.
        4. Verify the first character is a letter (Weaviate requirement
           for class names). Names that sanitize to a leading-digit /
           leading-symbol form raise ``ValueError`` rather than silently
           prepending a fallback prefix — the caller should re-prompt
           the user for a valid name.

    Edge cases:
        - Empty input → ``ValueError``.
        - Whitespace-only input → ``ValueError`` (post-strip is empty).
        - Leading digit → ``ValueError`` (Weaviate rejects class names
          that don't start with a letter; we surface this early rather
          than letting the Weaviate client return a confusing schema
          error mid-analyze).
        - All-special-chars input → ``ValueError`` (post-sanitize is
          empty or all underscores with no letter to uppercase).
        - Unicode: each non-ASCII codepoint counts as a non-``[A-Za-z0-9_]``
          char and gets replaced by an underscore (so ``"étude"`` →
          ``"_tude"`` — likely not useful, but never a crash). The
          ``é`` is whitespace-equivalent only if it falls inside Python's
          ``str.split()`` notion of whitespace; otherwise it's an
          in-word special character that becomes an underscore.

    Examples:
        >>> canonical_class_prefix("SD15")
        'SD15'
        >>> canonical_class_prefix("SimRacing_AI")
        'SimRacing_AI'
        >>> canonical_class_prefix("VibeCoded Orchestrator")
        'VibeCodedOrchestrator'
        >>> canonical_class_prefix("foo bar")
        'FooBar'
        >>> canonical_class_prefix("Foo-Bar")
        'Foo_Bar'
        >>> canonical_class_prefix("  spaced  out  ")
        'SpacedOut'

    Raises:
        ValueError: If ``project_name`` is empty / whitespace-only /
            sanitizes to empty / would start with a digit.
    """
    if not isinstance(project_name, str):
        raise ValueError(
            f"project_name must be str, got {type(project_name).__name__}"
        )

    stripped = project_name.strip()
    if not stripped:
        raise ValueError("project_name is empty (or whitespace-only)")

    # Step 1: split on whitespace runs, then drop empty parts. This is the
    # "drop-spaces" rule with implicit PascalCasing at word boundaries:
    # "foo bar" → ["foo", "bar"]; "  spaced   out  " → ["spaced", "out"].
    # We uppercase the FIRST char of each part below so word boundaries
    # remain visible after concatenation — otherwise "foo bar" would
    # collapse to "Foobar", which is ambiguous and doesn't match the
    # observed schema convention (e.g. "VibeCoded Orchestrator" →
    # "VibeCodedOrchestrator", where the capital O preserves the
    # word-boundary).
    parts = stripped.split()
    if not parts:
        # Defensive: stripped is non-empty (we checked) but split() with
        # no args could in theory return [] for some pathological inputs.
        raise ValueError(f"project_name {project_name!r} has no word parts")

    # Step 2: PascalCase each part — uppercase first char, preserve the
    # rest verbatim. "SimRacing" stays "SimRacing"; "ai" becomes "Ai";
    # "Foo-Bar" stays "Foo-Bar" (separator-handling happens in step 3).
    pascal_parts = [p[:1].upper() + p[1:] for p in parts]
    pascal = "".join(pascal_parts)

    # Step 3: replace any remaining non-[A-Za-z0-9_] with a single
    # underscore. Underscores already in the input pass through unchanged
    # (the negated class includes `_`), so "SimRacing_AI" stays
    # "SimRacing_AI". Dashes and other punctuation become underscores:
    # "Foo-Bar" → "Foo_Bar". Multiple consecutive special chars become
    # multiple underscores — we intentionally do NOT collapse runs to one
    # underscore, since collapsing would mask intent ("Foo--Bar" vs
    # "Foo-Bar") and there's no observed need to.
    cleaned = _NON_ALNUM_OR_UNDERSCORE.sub("_", pascal)

    if not cleaned:
        # Defensive: shouldn't be reachable given the strip+split steps
        # above, but guard anyway.
        raise ValueError(
            f"project_name {project_name!r} sanitizes to empty string"
        )

    # Step 4: ensure the first character is a letter. Weaviate class names
    # must start with [A-Z] (uppercase letter). Step 2 already uppercased
    # the first char of the first part, so the only way this trips is if
    # the first part itself starts with a non-letter (digit or symbol).
    first = cleaned[0]
    if not first.isalpha():
        raise ValueError(
            f"project_name {project_name!r} sanitizes to {cleaned!r}, "
            "which starts with a non-letter character — Weaviate class "
            "names must begin with a letter [A-Z]"
        )

    return cleaned
