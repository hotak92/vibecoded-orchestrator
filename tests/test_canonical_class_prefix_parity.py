# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""NEW-10 / DEDUP-6 (v0.2.53) — parity assertions for the consolidated
4-way ``canonical_class_prefix`` SSOT collision.

Before this consolidation the codebase had FOUR project-name → Weaviate-
class-prefix sanitizers, each annotated as a mirror/SSOT of the others,
with subtly different fallback strings (``"Vct"`` vs ``"vct"``) and
underscore-handling rules. See
``.claude/context/audits/vco-lib-python-dedup-2026-06-10.md`` Finding 1
for the full archaeology.

After consolidation:

  * Underscore-DROPPING rule (production manifests):
      ``vco_lib.project_init.sanitize_for_weaviate_class`` is the SSOT.
      ``vco_lib.config_projection._sanitize_kg_collection`` is a thin
      wrapper that preserves the historical "Vct" capitalized fallback
      for its env-write consumers.

  * Underscore-PRESERVING rule (code-graph collections):
      ``vco_lib.project_naming.canonical_class_prefix`` is the SSOT.
      ``vco_lib.codegraph_to_mermaid._sanitize_collection_prefix`` is a
      thin wrapper that catches ``ValueError`` and falls back to the
      legacy regex behaviour for malformed inputs.

These tests pin BOTH consolidations against fixture inputs so a future
regression (someone re-introducing a 4th sanitizer, or accidentally
flipping a rule) trips loudly.
"""

from __future__ import annotations

import pytest

from vco_lib.codegraph_to_mermaid import _sanitize_collection_prefix
from vco_lib.config_projection import _sanitize_kg_collection
from vco_lib.project_init import sanitize_for_weaviate_class
from vco_lib.project_naming import canonical_class_prefix


# ──────────────────────────────────────────────────────────────────────
# Underscore-DROPPING rule (SSOT: project_init.sanitize_for_weaviate_class)
# ──────────────────────────────────────────────────────────────────────


class TestUnderscoreDroppingRule:
    """``config_projection._sanitize_kg_collection`` must delegate to
    ``project_init.sanitize_for_weaviate_class`` for the non-fallback
    inputs (same rule), and preserve its "Vct" capitalized fallback for
    inputs that sanitize to empty / would start with a digit."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("MyProject", "MyProject"),
            ("VibeCoded Orchestrator", "VibeCodedOrchestrator"),
            ("Camel_Case", "CamelCase"),  # underscore DROPPED
            ("Foo-Bar", "FooBar"),
            ("foo bar", "FooBar"),
            ("   spaced   out  ", "SpacedOut"),
            ("Project123", "Project123"),
        ],
    )
    def test_sanitize_for_weaviate_class_normal_cases(
        self, raw: str, expected: str
    ) -> None:
        """The SSOT produces the expected underscore-DROPPED prefix."""
        assert sanitize_for_weaviate_class(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["MyProject", "VibeCoded Orchestrator", "Camel_Case", "Foo-Bar"],
    )
    def test_config_projection_delegates_to_ssot(self, raw: str) -> None:
        """For non-fallback inputs, the wrapper produces the same string
        as the SSOT it delegates to."""
        assert _sanitize_kg_collection(raw) == sanitize_for_weaviate_class(raw)

    def test_config_projection_preserves_capitalized_fallback(self) -> None:
        """When the SSOT falls back to ``"vct"`` (lowercase), the wrapper
        upgrades to ``"Vct"`` (capitalized) for its env-write consumers.
        Inputs that sanitize to empty (e.g. all special chars, leading
        digit) trigger this branch."""
        # Inputs that the underscore-dropping SSOT maps to "vct"
        # (fallback).
        for raw in ["", "   ", "@@@@", "123abc"]:
            ssot = sanitize_for_weaviate_class(raw)
            assert ssot == "vct", (
                f"expected SSOT to fall back to 'vct' on input {raw!r}; "
                f"got {ssot!r}"
            )
            wrapper = _sanitize_kg_collection(raw)
            assert wrapper == "Vct", (
                f"expected wrapper to upgrade SSOT fallback 'vct' → 'Vct' "
                f"on input {raw!r}; got {wrapper!r}"
            )


# ──────────────────────────────────────────────────────────────────────
# Underscore-PRESERVING rule (SSOT: project_naming.canonical_class_prefix)
# ──────────────────────────────────────────────────────────────────────


class TestUnderscorePreservingRule:
    """``codegraph_to_mermaid._sanitize_collection_prefix`` must delegate
    to ``project_naming.canonical_class_prefix`` for valid inputs and
    fall back to the legacy regex behaviour for inputs that would
    otherwise raise ``ValueError``."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("MyProject", "MyProject"),
            ("Camel_Case", "Camel_Case"),  # underscore PRESERVED
            ("VibeCoded Orchestrator", "VibeCodedOrchestrator"),
            ("Foo-Bar", "Foo_Bar"),  # dash → underscore
            ("foo bar", "FooBar"),  # space-split + PascalCase
            ("Project_With_Underscores", "Project_With_Underscores"),
        ],
    )
    def test_canonical_class_prefix_normal_cases(
        self, raw: str, expected: str
    ) -> None:
        """The SSOT produces the expected underscore-PRESERVED prefix."""
        assert canonical_class_prefix(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["MyProject", "Camel_Case", "VibeCoded Orchestrator", "Foo-Bar"],
    )
    def test_codegraph_wrapper_delegates_to_ssot(self, raw: str) -> None:
        """For valid (non-fallback) inputs, the wrapper produces the
        same string as the SSOT it delegates to."""
        assert _sanitize_collection_prefix(raw) == canonical_class_prefix(raw)

    def test_codegraph_wrapper_catches_valueerror(self) -> None:
        """When the SSOT raises ``ValueError`` (empty / whitespace-only /
        leading-digit input), the wrapper falls back to the legacy
        regex behaviour and returns a string rather than propagating
        the exception. Existing callers of the analyze flow expect to
        get a string back even for unusual ``--project`` arguments."""
        # Empty input — SSOT raises, legacy wrapper returns "".
        with pytest.raises(ValueError):
            canonical_class_prefix("")
        # Wrapper does NOT raise.
        result = _sanitize_collection_prefix("")
        assert isinstance(result, str)

        # All-special-chars input — SSOT raises, wrapper returns the
        # regex-only output.
        with pytest.raises(ValueError):
            canonical_class_prefix("@@@@")
        wrapper_result = _sanitize_collection_prefix("@@@@")
        # Legacy regex replaces every non-alnum-underscore with "_",
        # leaving "____". The first char isn't a letter, so no
        # uppercase is applied. Result: "____".
        assert wrapper_result == "____"


# ──────────────────────────────────────────────────────────────────────
# Cross-rule contract: the two SSOTs produce DIFFERENT outputs for
# inputs with underscores. This is intentional — production schemas
# contain classes named by BOTH rules. Unifying them would orphan
# existing collections. See project_naming.py's module docstring.
# ──────────────────────────────────────────────────────────────────────


class TestCrossRuleDivergence:
    """Document (and pin) the cases where the two SSOTs intentionally
    produce different outputs. If a future PR accidentally redirects
    one rule to the other, these tests will trip."""

    @pytest.mark.parametrize(
        "raw,dropping_expected,preserving_expected",
        [
            # The canonical divergence: underscores.
            ("Camel_Case", "CamelCase", "Camel_Case"),
            ("Project_With_Underscores", "ProjectWithUnderscores", "Project_With_Underscores"),
            ("a_b_c", "ABC", "A_b_c"),
        ],
    )
    def test_rules_diverge_on_underscored_inputs(
        self, raw: str, dropping_expected: str, preserving_expected: str
    ) -> None:
        assert sanitize_for_weaviate_class(raw) == dropping_expected
        assert canonical_class_prefix(raw) == preserving_expected
        assert sanitize_for_weaviate_class(raw) != canonical_class_prefix(raw)

    @pytest.mark.parametrize(
        "raw,common",
        [
            # No underscores → both rules produce the same output.
            ("MyProject", "MyProject"),
            ("VibeCoded Orchestrator", "VibeCodedOrchestrator"),
            ("foo bar", "FooBar"),
        ],
    )
    def test_rules_agree_when_no_underscores(self, raw: str, common: str) -> None:
        assert sanitize_for_weaviate_class(raw) == common
        assert canonical_class_prefix(raw) == common


# ──────────────────────────────────────────────────────────────────────
# Anti-regression: no NEW (5th) sanitizer should appear in vco_lib/.
# ──────────────────────────────────────────────────────────────────────


def test_no_fifth_sanitizer_in_vco_lib() -> None:
    """Regression guard against re-introducing a 5th competing sanitizer.

    If you add a new helper to vco_lib that needs to map a project name
    → Weaviate class prefix, USE ONE OF THE TWO SSOTs (above) rather
    than inlining the regex. If you genuinely need a third rule (you
    don't), open a design discussion first — production Weaviate
    schemas don't tolerate a 3rd rule.
    """
    import ast
    import pathlib

    vco_lib_root = pathlib.Path(__file__).resolve().parent.parent / "vco_lib"
    assert vco_lib_root.is_dir(), f"vco_lib not found at {vco_lib_root}"

    # Known-canonical functions, plus wrappers we explicitly endorse.
    # X-1 / v0.2.76: BOTH SSOTs now live in the ONE naming home
    # ``vco_lib/codegraph_naming.py``. ``project_init.py`` /
    # ``project_naming.py`` keep DEPRECATION RE-EXPORTS (import statements,
    # not FunctionDefs — so they don't appear as findings here).
    allowed_names = {
        # SSOT (underscore-dropping) — the ONE home.
        ("codegraph_naming.py", "sanitize_for_weaviate_class"),
        # SSOT (underscore-preserving) — the ONE home.
        ("codegraph_naming.py", "canonical_class_prefix"),
        # Endorsed wrappers (delegate to SSOTs).
        ("config_projection.py", "_sanitize_kg_collection"),
        ("codegraph_to_mermaid.py", "_sanitize_collection_prefix"),
        # v0.2.84 D1 (P2): the ENDORSED Python mirror of the hub's
        # ``vct_launcher_core::collection_naming::sanitize_collection_prefix``
        # (the underscore-PRESERVING slug→prefix rule the dev/diagrams
        # NON-canonical fallback uses). This is a DELIBERATE, hub-parity-
        # locked rule distinct from both SSOTs above (it mirrors the Rust
        # ``sanitize_collection_prefix`` byte-for-byte — see the parity test
        # ``tests/test_v0284_dev_collection_one_rule.py::
        # test_python_sanitizer_byte_matches_hub_pinned_cases``). It is NOT
        # a 5th competing rule for the KG basename — it is the ONE python
        # home for the hub's existing dev/diagrams fallback sanitizer.
        ("config_projection.py", "_sanitize_collection_prefix"),
    }

    # Patterns we recognise as sanitizer-shape function names.
    suspect_name_substrings = (
        "sanitize_for_weaviate",
        "sanitize_kg_collection",
        "sanitize_collection_prefix",
        "canonical_class_prefix",
    )

    findings: list[tuple[str, str]] = []
    for path in vco_lib_root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(s in node.name for s in suspect_name_substrings):
                    findings.append((path.name, node.name))

    unexpected = [
        (file, name) for (file, name) in findings if (file, name) not in allowed_names
    ]
    assert not unexpected, (
        f"Found {len(unexpected)} unexpected sanitizer-shape function(s) in vco_lib: "
        f"{unexpected}. Either add to the allowed_names set above (if it's a new "
        f"endorsed wrapper) or refactor to call one of the two SSOTs."
    )
