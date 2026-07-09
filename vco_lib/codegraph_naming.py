# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for project-name → Weaviate-class-name derivation (X-1 /
v0.2.76).

Two derivation rules live here, and NOWHERE else:

  * :func:`sanitize_for_weaviate_class` — the underscore-DROPPING rule used
    to build ``<prefix>_KnowledgeGraph`` / ``<prefix>_Development`` /
    ``<prefix>_Diagrams`` collection names. (Previously the SSOT lived in
    ``vco_lib.project_init``; it now re-exports from here.)
  * :func:`canonical_class_prefix` — the underscore-PRESERVING rule used to
    build the per-project code-graph class prefix (``<prefix>_CodeFunction``
    etc.). (Previously the SSOT lived in ``vco_lib.project_naming``; it now
    re-exports from here.)

Why one home (the v0.2.76 X-1 ruling)
-------------------------------------
Before this module the two rules lived in two files and each had a Rust
port kept in lock-step by a parity fixture. The Rust ports historically
DIVERGED from Python on pathological out-of-domain inputs (empty /
all-non-alnum / leading-digit), pinned per-side in
``tests/fixtures/kg_sanitizer_parity.json``'s ``divergent`` array. Per the
user ruling (2026-07-09, ``DESIGN-part9-gated-themes-2026-07-08.md`` §2):
Python semantics are the single source of truth; divergence is eliminated
AT THE SOURCE, not policed after the fact. This module is that source.

The two rules stay intentionally DISTINCT (production Weaviate schemas
contain classes named by BOTH — see ``project_naming.py``'s history
docstring for the v0.2.15 wedge). "One home" means one FILE and one CLI
surface, not one algorithm — each rule is still its own function.

Loud-fail discipline
--------------------
This module has NO fallback arms. It IS the source of truth. Callers that
cannot import it (a broken VCO install) must FAIL LOUDLY (ruling #1), never
silently degrade to an inline copy.

CLI (the surface Rust / any cross-language caller shells out to)
----------------------------------------------------------------
    python -m vco_lib.codegraph_naming --kg     <name>   # KG sanitizer
    python -m vco_lib.codegraph_naming --prefix <name>   # codegraph prefix

Prints the derived name as a single machine-readable line on stdout; a bad
usage (missing flag / bad name for ``--prefix``) exits non-zero with a
message on stderr. Latency-tolerant callers only — user-action-triggered,
ms-scale paths (create / rename / Identity tab), never per-keystroke or
per-env-key loops.
"""

from __future__ import annotations

import argparse
import re
import sys

__all__ = [
    "sanitize_for_weaviate_class",
    "canonical_class_prefix",
    "FALLBACK_PREFIX",
]

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Sanitizer regex (underscore-DROPPING rule): split on any non-alphanumeric
# run.
_SAFE_CLASS_RE = re.compile(r"[^A-Za-z0-9]+")

# Non-alphanumeric-or-underscore matcher (underscore-PRESERVING rule): any
# single character that is NOT alphanumeric or underscore.
_NON_ALNUM_OR_UNDERSCORE = re.compile(r"[^A-Za-z0-9_]")

# Fallback prefix when a project name has no usable alphanumeric characters
# or starts with a digit. Lowercase ``vct`` is intentional — Weaviate
# capitalizes the first letter on POST regardless, and the prefix flags the
# class as installer-managed. This is the UNIFIED fallback (v0.2.76 X-1):
# both the Python side AND the Rust port now return this for the
# out-of-domain inputs (empty / all-non-alnum / leading-digit) that used to
# diverge — the ``divergent`` fixture class is retired.
FALLBACK_PREFIX = "vct"


# ---------------------------------------------------------------------------
# Rule 1 — underscore-DROPPING (KG / Development / Diagrams collections)
# ---------------------------------------------------------------------------


def sanitize_for_weaviate_class(project_name: str) -> str:
    """PascalCase a project name into a Weaviate class basename
    (underscore-DROPPING rule).

    This function is the CANONICAL underscore-dropping sanitizer used to
    derive ``<prefix>_KnowledgeGraph`` / ``<prefix>_Development`` /
    ``<prefix>_Diagrams`` collection names. It is the single source of
    truth across languages — the Rust ``sanitize_kg_collection`` port must
    behave identically (verified by ``tests/fixtures/kg_sanitizer_parity.json``).

    Rules:
      1. Split on any non-alphanumeric run (``-``, ``_``, space, etc.).
      2. PascalCase each surviving part (uppercase first letter, keep rest).
      3. Concatenate.
      4. Fallback rule (v0.2.76 X-1 unification): if NOTHING survives (empty
         / all-non-alnum input) OR the result starts with a digit (invalid
         Weaviate class name — Weaviate class names must begin with a
         letter), fall back to :data:`FALLBACK_PREFIX` (``"vct"``).
         Rationale: an installer-managed sentinel prefix is preferable to a
         ``P``-prepend guess — it is visibly "not a user name" and avoids
         minting a plausible-but-wrong collection for garbage input.

    Note on non-ASCII: the regex ``[^A-Za-z0-9]+`` treats any non-ASCII
    character as a separator, so ``étude`` → ``["tude"]`` → ``"Tude"``
    (the ``é`` is stripped, not preserved).
    """
    base = project_name or ""
    parts = [p for p in _SAFE_CLASS_RE.split(base) if p]
    if not parts:
        return FALLBACK_PREFIX
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or not pascal[0].isalpha():
        return FALLBACK_PREFIX
    return pascal


# ---------------------------------------------------------------------------
# Rule 2 — underscore-PRESERVING (code-graph class prefix)
# ---------------------------------------------------------------------------


def canonical_class_prefix(project_name: str) -> str:
    """Convert a human project name into a Weaviate class prefix
    (underscore-PRESERVING rule).

    The result is the exact prefix used to build per-project code-graph
    class names: ``<prefix>_CodeFunction``, ``<prefix>_CodeClass``, etc.

    Rules:
        1. Strip leading/trailing whitespace.
        2. Split on whitespace runs into word parts, then PascalCase each
           part (uppercase the first character, preserve the rest) and
           concatenate — the "drop-spaces with implicit word-boundary
           capitalization" rule:
             ``"foo bar"``                → ``"FooBar"``
             ``"VibeCoded Orchestrator"`` → ``"VibeCodedOrchestrator"``
             ``"  spaced   out  "``       → ``"SpacedOut"``
        3. Replace remaining non-``[A-Za-z0-9_]`` characters with a single
           underscore. ``"Foo-Bar"`` → ``"Foo_Bar"`` (dash → ``_``);
           underscores already present pass through — ``"Camel_Case"`` stays
           ``"Camel_Case"``. Runs are NOT collapsed (``"Foo--Bar"`` →
           ``"Foo__Bar"``).
        4. Verify the first character is a letter (Weaviate requirement).
           Names that sanitize to a leading-digit / leading-symbol form
           raise ``ValueError`` rather than silently prepending a fallback
           prefix — the caller should re-prompt for a valid name.

    Raises:
        ValueError: If ``project_name`` is not a str, is empty /
            whitespace-only, sanitizes to empty, or would start with a
            non-letter character.
    """
    if not isinstance(project_name, str):
        raise ValueError(
            f"project_name must be str, got {type(project_name).__name__}"
        )

    stripped = project_name.strip()
    if not stripped:
        raise ValueError("project_name is empty (or whitespace-only)")

    parts = stripped.split()
    if not parts:
        # Defensive: stripped is non-empty (checked) but split() could in
        # theory return [] for some pathological inputs.
        raise ValueError(f"project_name {project_name!r} has no word parts")

    pascal_parts = [p[:1].upper() + p[1:] for p in parts]
    pascal = "".join(pascal_parts)

    cleaned = _NON_ALNUM_OR_UNDERSCORE.sub("_", pascal)

    if not cleaned:
        raise ValueError(
            f"project_name {project_name!r} sanitizes to empty string"
        )

    first = cleaned[0]
    if not first.isalpha():
        raise ValueError(
            f"project_name {project_name!r} sanitizes to {cleaned!r}, "
            "which starts with a non-letter character — Weaviate class "
            "names must begin with a letter [A-Z]"
        )

    return cleaned


# ---------------------------------------------------------------------------
# CLI — the surface cross-language callers (Rust) shell out to
# ---------------------------------------------------------------------------


def _main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.codegraph_naming",
        description=(
            "Derive a Weaviate class name from a project name. The single "
            "source of truth for both the KG (underscore-dropping) and "
            "code-graph (underscore-preserving) naming rules; cross-language "
            "callers (Rust) shell out here so no divergence class can exist."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--kg",
        action="store_true",
        help="derive the KG/Development/Diagrams basename "
        "(underscore-dropping rule; sanitize_for_weaviate_class)",
    )
    mode.add_argument(
        "--prefix",
        action="store_true",
        help="derive the code-graph class prefix "
        "(underscore-preserving rule; canonical_class_prefix)",
    )
    parser.add_argument("name", help="the raw project name")

    args = parser.parse_args(argv)

    if args.kg:
        # The KG sanitizer never rejects — it falls back to FALLBACK_PREFIX
        # for out-of-domain input, so this always prints a valid basename.
        sys.stdout.write(sanitize_for_weaviate_class(args.name) + "\n")
        return 0

    # --prefix: canonical_class_prefix raises ValueError for names that
    # cannot become a valid class prefix. Surface that as a non-zero exit +
    # stderr so the caller (Rust) can present a precise validation error.
    try:
        sys.stdout.write(canonical_class_prefix(args.name) + "\n")
    except ValueError as exc:
        sys.stderr.write(f"codegraph_naming --prefix: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
