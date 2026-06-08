# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for vco_lib.project_naming.canonical_class_prefix.

Pin the canonical sanitizer's behaviour:
  - Documented success cases.
  - Edge cases (empty / whitespace / leading-digit / all-symbol input).
  - Preservation of underscores already in the name.
  - Drop-spaces with implicit word-boundary capitalization.

The parity test in test_project_naming_parity.py separately pins the
Rust port to the same shared fixture; that test is what catches drift
between the two implementations.
"""

import pytest

from vco_lib.project_naming import canonical_class_prefix


# ─────────────────────────────────────────────────────────────────────
# Docstring examples (golden cases)
# ─────────────────────────────────────────────────────────────────────


class TestDocstringExamples:
    """Examples from the canonical_class_prefix docstring. If these
    break we MUST update the docstring; users read the docstring as
    the spec."""

    def test_sd15(self):
        assert canonical_class_prefix("MyProject") == "MyProject"

    def test_camel_case_preserves_underscore(self):
        # Crucial: existing schema-on-disk for underscored-CamelCase
        # project names uses the underscore form. The Rust-style
        # sanitize_kg_collection would strip it to CamelCase — that's
        # the bug 0.7 we're fixing.
        assert canonical_class_prefix("Camel_Case") == "Camel_Case"

    def test_vibecoded_orchestrator_drops_space(self):
        # The space goes; the O remains uppercase. Word boundary is
        # implicitly capitalized by the PascalCase rule (rule 2).
        assert (
            canonical_class_prefix("VibeCoded Orchestrator")
            == "VibeCodedOrchestrator"
        )

    def test_lowercase_word_uppercases_at_boundary(self):
        # "foo bar" → PascalCase each word → "Foo" + "Bar" → "FooBar"
        assert canonical_class_prefix("foo bar") == "FooBar"

    def test_dash_becomes_underscore(self):
        assert canonical_class_prefix("Foo-Bar") == "Foo_Bar"

    def test_extra_whitespace_collapsed(self):
        assert canonical_class_prefix("  spaced  out  ") == "SpacedOut"


# ─────────────────────────────────────────────────────────────────────
# Edge cases — error paths
# ─────────────────────────────────────────────────────────────────────


class TestErrorCases:
    """canonical_class_prefix raises ValueError rather than returning
    a fallback like "vct" or "Project" — fail-fast principle. The
    caller should re-prompt or surface a config-validation error.
    """

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            canonical_class_prefix("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            canonical_class_prefix("   ")

    def test_tabs_and_newlines_raise(self):
        with pytest.raises(ValueError, match="empty"):
            canonical_class_prefix("\t\n")

    def test_all_symbols_raise(self):
        # "!!!" → split() returns ["!!!"], PascalCase → "!!!" (first
        # char is "!" — .upper() is "!"), cleaned → "___", first char
        # "_" is not isalpha → ValueError.
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("!!!")

    def test_leading_digit_raises(self):
        # "123abc" stays "123abc" (no spaces, no special chars to
        # underscore). First char "1" is not isalpha → ValueError.
        # Weaviate would reject this server-side; we surface it early.
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("123abc")

    def test_leading_digit_after_sanitize_raises(self):
        # "9-foo" → "9_foo", first char "9" not alpha → ValueError.
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("9-foo")

    def test_only_underscores_raises(self):
        # First non-space char in "_only_" is "_", not isalpha →
        # ValueError. The rest doesn't matter; we never reach the
        # rest-of-string preservation step.
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("_only_underscores_")

    def test_non_string_raises_value_error(self):
        # We coerce TypeError into ValueError so callers can catch
        # one exception type at the validation boundary.
        with pytest.raises(ValueError, match="must be str"):
            canonical_class_prefix(123)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must be str"):
            canonical_class_prefix(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must be str"):
            canonical_class_prefix(["a", "b"])  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# Preservation properties
# ─────────────────────────────────────────────────────────────────────


class TestPreservation:
    """Properties that distinguish canonical_class_prefix from the
    legacy Rust sanitize_kg_collection and the legacy Python
    _sanitize_collection_prefix."""

    def test_intra_word_uppercase_preserved(self):
        # "VibeCoded" has a capital C in the middle. We must NOT
        # lowercase it; we only touch the FIRST char of each
        # whitespace-separated word.
        assert canonical_class_prefix("VibeCoded") == "VibeCoded"

    def test_underscore_preserved_in_single_word(self):
        assert canonical_class_prefix("foo_bar") == "Foo_bar"

    def test_underscore_preserved_in_multi_segment(self):
        # The trick case: underscore-separated parts pass through
        # whitespace-split as one token, so we ONLY uppercase the
        # first char of the whole token. "Camel_Case" → "Camel_Case"
        # (NOT "CamelCase" and NOT "Camel_case").
        assert canonical_class_prefix("Camel_Case") == "Camel_Case"

    def test_dash_to_underscore_preserves_case(self):
        # Dashes get underscore-substituted, but they do NOT cause
        # the following char to capitalize. "foo-bar" → "Foo_bar"
        # (NOT "Foo_Bar" — that would require treating dash as a
        # word boundary, which we don't).
        assert canonical_class_prefix("foo-bar") == "Foo_bar"

    def test_consecutive_specials_not_collapsed(self):
        # Each special char becomes its own underscore. "Foo--Bar" →
        # "Foo__Bar" (two underscores). Rationale: collapsing would
        # mask user intent; the user might genuinely mean two segments
        # they want kept distinct.
        assert canonical_class_prefix("Foo--Bar") == "Foo__Bar"

    def test_leading_uppercase_unchanged(self):
        # If the first char of the first word is already uppercase,
        # we don't gratuitously rewrite it.
        assert canonical_class_prefix("AlreadyCapital") == "AlreadyCapital"


# ─────────────────────────────────────────────────────────────────────
# Differential vs legacy implementations
# ─────────────────────────────────────────────────────────────────────


class TestVsLegacy:
    """Pin the deliberate divergences from the two legacy sanitizers
    so that an accidental "revert to old behaviour" PR fails loudly."""

    def test_camel_case_diverges_from_rust(self):
        # Rust sanitize_kg_collection strips the underscore and
        # title-cases each surviving segment: Camel_Case → CamelCase.
        # We deliberately diverge: Camel_Case → Camel_Case.
        result = canonical_class_prefix("Camel_Case")
        assert result != "CamelCase", (
            "Regressed to legacy Rust sanitize_kg_collection — "
            "would lose underscores in project names like "
            "'Camel_Case', breaking existing code-graph schemas "
            "on `base`-host installs"
        )

    def test_vibecoded_orchestrator_diverges_from_python(self):
        # Python _sanitize_collection_prefix replaces space with
        # underscore: VibeCoded Orchestrator → VibeCoded_Orchestrator.
        # We deliberately diverge: VibeCoded Orchestrator → VibeCodedOrchestrator
        # (no underscore — matches the wizard's display + the existing
        # VibeCodedOrchestrator_KnowledgeGraph schema).
        result = canonical_class_prefix("VibeCoded Orchestrator")
        assert result != "VibeCoded_Orchestrator", (
            "Regressed to legacy Python _sanitize_collection_prefix — "
            "would insert underscore for spaces, diverging from "
            "the launcher wizard's 'current prefix:' display"
        )


# ─────────────────────────────────────────────────────────────────────
# Unicode handling
# ─────────────────────────────────────────────────────────────────────


class TestUnicode:
    """Non-ASCII inputs aren't first-class — we don't bother trying to
    PascalCase Cyrillic etc. — but they MUST NOT crash. Each non-ASCII
    codepoint that survives whitespace-split becomes an underscore."""

    def test_accented_first_letter_is_substituted(self):
        # "étude" doesn't whitespace-split, so it's one token. First
        # char "é" is not in [A-Za-z0-9_] → substituted to "_" by the
        # regex. Result starts with "_" → ValueError (non-letter first).
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("étude")

    def test_ascii_after_accented_separator(self):
        # "café table" → split on space → ["café", "table"]. First
        # token's first char "c" → "C". Second token's first char "t"
        # → "T". Concat → "CaféTable". Then the regex sees "é" as a
        # non-[A-Za-z0-9_] char and substitutes "_". Result: "Caf_éTable"
        # is wrong — let me think… actually "CaféTable" goes through
        # the substitution: "é" → "_", so "Caf_Table". First char "C"
        # is alpha → returns "Caf_Table".
        assert canonical_class_prefix("café table") == "Caf_Table"

    def test_emoji_substituted(self):
        # "🎯 target" → "🎯" gets stripped by split? No — split() with
        # no arg only splits on whitespace, and emoji is not whitespace.
        # So tokens are ["🎯", "target"]. First token PascalCases to
        # "🎯" (emoji uppercase = emoji), second to "Target". Concat:
        # "🎯Target". Then the regex sees "🎯" as non-[A-Za-z0-9_] → "_".
        # Result: "_Target". First char "_" → ValueError.
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("🎯 target")


# ─────────────────────────────────────────────────────────────────────
# Determinism (sanity guarantee for caching / DB lookups)
# ─────────────────────────────────────────────────────────────────────


class TestDeterminism:
    """canonical_class_prefix is a pure function with no global state.
    Trivially deterministic, but pinning it makes the contract
    explicit for callers that key caches off the output."""

    def test_idempotent(self):
        # Running the result through canonical_class_prefix again must
        # produce the same value (the result is itself a valid class
        # name, with no further normalization possible).
        for inp in [
            "VibeCoded Orchestrator",
            "Camel_Case",
            "Foo-Bar",
            "MyProject",
        ]:
            once = canonical_class_prefix(inp)
            twice = canonical_class_prefix(once)
            assert once == twice, (
                f"Not idempotent: {inp!r} → {once!r} → {twice!r}"
            )

    def test_pure(self):
        # Two consecutive calls with identical input give identical
        # output (no hidden state, no time-dependent behaviour).
        for inp in ["X", "FooBar", "a b"]:
            assert canonical_class_prefix(inp) == canonical_class_prefix(inp)


# ─────────────────────────────────────────────────────────────────────
# v0.2.34 B2 follow-up — underscore boundary cases
# ─────────────────────────────────────────────────────────────────────


class TestUnderscoreBoundaryCases:
    """Phase chat's v0.2.33 review documented a (since-resolved)
    suspicion that the Rust port diverged from Python on ``_``
    handling. These five tests pin the Python-side behaviour
    explicitly so a regression to either legacy sanitizer fails
    loudly here — independently of the cross-language parity test in
    ``test_project_naming_parity.py``.

    The shared ``tests/fixtures/project_naming.json`` covers the same
    cases for the Rust port; these Python-side tests give a focused
    first-line signal without the JSON loader."""

    def test_camel_case_preserves_single_underscore(self):
        # Documented Phase-chat suspicion: Rust → "Camel_Case"
        # (correct), Python → "CamelCase" (wrong claim). Both
        # implementations actually produce "Camel_Case"; this test
        # pins it so a regression to either of the two legacy
        # sanitizers (which DID drop the underscore) fails here.
        assert canonical_class_prefix("Camel_Case") == "Camel_Case"

    def test_double_underscore_preserved_verbatim(self):
        # Each underscore passes through the regex unchanged (the
        # negated char class includes ``_``), so "foo__bar" stays as
        # "Foo__bar". We do NOT collapse runs of underscores — that
        # would mask user intent.
        assert canonical_class_prefix("foo__bar") == "Foo__bar"

    def test_leading_underscore_rejected_as_non_letter(self):
        # Leading ``_`` fails the step-4 "first char must be a letter"
        # check. Weaviate would reject server-side too; we surface
        # the error early via ValueError("non-letter").
        with pytest.raises(ValueError, match="non-letter"):
            canonical_class_prefix("_leading")

    def test_trailing_underscore_preserved(self):
        # Trailing underscore is a valid class-name suffix (Weaviate
        # only constrains the FIRST char). We preserve it verbatim
        # because stripping it would be a silent surprise.
        assert canonical_class_prefix("trailing_") == "Trailing_"

    def test_three_consecutive_underscores_preserved(self):
        # Three consecutive underscores stay as three underscores —
        # same rationale as ``test_double_underscore_preserved``.
        # This is the strongest "no run-collapsing" pin.
        assert canonical_class_prefix("foo___bar") == "Foo___bar"
