# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PR-34 (v0.2.12, Group M) — cross-language shared-KG constant invariant.

Pins the canonical shared-KG class name + its TWO legacy aliases in
lockstep across the four surfaces that carry the literal:

  * Python  — ``vco_lib/project_init.py::_SHARED_KG_NAME``
              ``vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME`` (pre-v0.2.12)
              ``vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME_LOWERCASE_C``
                  (v0.2.12–v0.2.22)
  * Rust    — ``launcher/src-tauri/src/commands/project_env_settings.rs``
              ``LAST_RESORT_SHARED_KG_COLLECTION`` (renamed from
                  ``DEFAULT_SHARED_KG_COLLECTION`` in v0.2.40 W40-C; value
                  unchanged — rename is purely an audit-discipline signal
                  that this value is the END of the resolution chain)
              ``LEGACY_SHARED_KG_COLLECTION`` (pre-v0.2.12)
              ``LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C`` (v0.2.12–v0.2.22)

Background:

  * PR-26 (v0.2.12, Group E) renamed the canonical from
    ``VibeCodedTools_KnowledgeGraph`` to
    ``VibecodedOrchestrator_KnowledgeGraph`` (lowercase c, matching the
    actual on-disk casing in production).
  * PR-34 (v0.2.12, Group M) swept the remaining surfaces still
    hardcoding the OLD name and pinned them via this lockstep test.
  * v0.2.23 B1 (2026-05-21) flipped the canonical casing again from
    lowercase-c ``Vibecoded`` back to capital-C ``VibeCoded`` to match
    the brand spelling. The case-flip is supported by case-insensitive
    adoption in ``install.py::_ensure_collections`` plus the binding-row
    self-heal in ``install.py::_self_heal_kg_bindings_on_update`` —
    existing installs with the lowercase-c class on disk are adopted in
    place rather than recreated. The lowercase-c name lives on as
    ``_LEGACY_SHARED_KG_NAME_LOWERCASE_C`` so case-mismatch detection
    code can recognise it without scattering the literal.

This test pins both the canonical and BOTH legacy aliases so any future
drift fails CI loudly — the picker's per-project app_state override
mechanism papers over default mismatches at runtime, but a divergent set
of DEFAULTS across surfaces is a footgun for fresh installs and
fresh-project flows.

Why a regex parse of the Rust file instead of a generated binding: the
Rust crate (``launcher/``) is a separate Cargo workspace that's not part
of the Python test runner's build, so we can't import the constant
directly. A string-level read of the ``.rs`` file with anchored regex
matches is sufficient for a constant-string invariant.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# v0.2.23 B1 (2026-05-21): canonical casing flipped from lowercase-c to
# capital-C to match the brand spelling.
CANONICAL_SHARED_KG_NAME = "VibeCodedOrchestrator_KnowledgeGraph"
# Pre-v0.2.12 PR-26 name (still a recognised legacy alias).
LEGACY_SHARED_KG_NAME = "VibeCodedTools_KnowledgeGraph"
# v0.2.12–v0.2.22 lowercase-c canonical (now a legacy alias).
LEGACY_SHARED_KG_NAME_LOWERCASE_C = "VibecodedOrchestrator_KnowledgeGraph"

RUST_CONSTS_FILE = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "src"
    / "commands"
    / "project_env_settings.rs"
)

# Anchored regex for each Rust constant. The full pattern matches:
#   pub const NAME: &str = "STRING";
# where STRING is captured. Anchored on `pub const NAME:` so a doc
# comment mentioning the name doesn't get picked up.
_RUST_CONST_RE = re.compile(
    r'^pub const (?P<name>[A-Z_]+)\s*:\s*&str\s*=\s*"(?P<value>[^"]+)"\s*;',
    re.MULTILINE,
)


def _parse_rust_constants() -> dict[str, str]:
    """Return {const_name: string_value} for every ``pub const NAME: &str``
    declaration in ``project_env_settings.rs``. Test fails loudly if the
    file is missing — that's a structural drift we want to catch."""
    if not RUST_CONSTS_FILE.exists():
        raise AssertionError(
            f"Expected Rust constants file at {RUST_CONSTS_FILE}; "
            "the cross-language invariant test cannot run without it."
        )
    body = RUST_CONSTS_FILE.read_text(encoding="utf-8")
    return {m.group("name"): m.group("value") for m in _RUST_CONST_RE.finditer(body)}


class PythonConstantsTests(unittest.TestCase):
    """Python source of truth for the canonical + BOTH legacy names."""

    def test_canonical_matches_expected(self):
        self.assertEqual(
            project_init._SHARED_KG_NAME,
            CANONICAL_SHARED_KG_NAME,
            "vco_lib/project_init.py::_SHARED_KG_NAME drifted from "
            f"{CANONICAL_SHARED_KG_NAME!r} — either revert the rename or "
            "update tests/test_shared_kg_constant_consistency.py and "
            "every surface listed in its module docstring.",
        )

    def test_legacy_alias_matches_expected(self):
        self.assertEqual(
            project_init._LEGACY_SHARED_KG_NAME,
            LEGACY_SHARED_KG_NAME,
            "vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME drifted from "
            f"{LEGACY_SHARED_KG_NAME!r} — the legacy alias must keep "
            "pointing at the pre-v0.2.12 PR-26 class name for migration-"
            "detection code to recognize pre-rename installs.",
        )

    def test_legacy_alias_lowercase_c_matches_expected(self):
        # v0.2.23 B1: pin the second legacy alias (v0.2.12–v0.2.22
        # canonical, now used by case-mismatch detection paths).
        self.assertEqual(
            project_init._LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            "vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME_LOWERCASE_C "
            f"drifted from {LEGACY_SHARED_KG_NAME_LOWERCASE_C!r} — the "
            "lowercase-c alias must keep pointing at the v0.2.12–v0.2.22 "
            "canonical class name for case-mismatch detection code (in "
            "install.py::_self_heal_kg_bindings_on_update + "
            "install.py::_ensure_collections case-insensitive adoption) "
            "to recognise pre-flip installs.",
        )

    def test_canonical_and_legacy_are_distinct(self):
        # Sanity: if any pair collides, every migration-detection path
        # that branches on (legacy ⇒ deferral, canonical ⇒ no-op)
        # collapses.
        self.assertNotEqual(
            project_init._SHARED_KG_NAME,
            project_init._LEGACY_SHARED_KG_NAME,
            "canonical and pre-v0.2.12 legacy shared-KG names must "
            "differ; otherwise legacy-detection short-circuits to no-op",
        )
        self.assertNotEqual(
            project_init._SHARED_KG_NAME,
            project_init._LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            "canonical and v0.2.12–v0.2.22 lowercase-c legacy shared-KG "
            "names must differ; the v0.2.23 B1 case-flip is the whole "
            "point of the lowercase-c alias",
        )
        self.assertNotEqual(
            project_init._LEGACY_SHARED_KG_NAME,
            project_init._LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            "the two legacy aliases must differ; one tracks pre-v0.2.12 "
            "PR-26 class names, the other tracks v0.2.12–v0.2.22 names",
        )

    def test_canonical_has_capital_c_vibecoded(self):
        # v0.2.23 B1: explicit casing pin — the canonical is "VibeCoded"
        # (capital C+D) for brand consistency. The lowercase-c
        # "Vibecoded" is the v0.2.12–v0.2.22 legacy alias.
        self.assertIn("VibeCoded", CANONICAL_SHARED_KG_NAME)
        self.assertNotIn("Vibecoded", CANONICAL_SHARED_KG_NAME.replace("VibeCoded", "X"))


class RustConstantsTests(unittest.TestCase):
    """Rust source of truth — parsed from the .rs file directly because
    the Cargo workspace is not part of the pytest build."""

    def setUp(self) -> None:
        self.constants = _parse_rust_constants()

    def test_last_resort_shared_kg_collection_present(self):
        # v0.2.40 W40-C (2026-05-30): const renamed from
        # `DEFAULT_SHARED_KG_COLLECTION` to `LAST_RESORT_SHARED_KG_COLLECTION`
        # to signal that this value is the END of the resolution chain
        # (DB-read → app_state override → const), not the first choice.
        # The value is unchanged; only the name moved to make fallback-only
        # call sites greppable.
        self.assertIn(
            "LAST_RESORT_SHARED_KG_COLLECTION",
            self.constants,
            f"Expected `pub const LAST_RESORT_SHARED_KG_COLLECTION: &str = ...` "
            f"in {RUST_CONSTS_FILE.relative_to(REPO_ROOT)} — the constant "
            "was removed or renamed. (v0.2.40 W40-C renamed the prior "
            "`DEFAULT_SHARED_KG_COLLECTION` to `LAST_RESORT_*`.)",
        )

    def test_legacy_shared_kg_collection_present(self):
        self.assertIn(
            "LEGACY_SHARED_KG_COLLECTION",
            self.constants,
            f"Expected `pub const LEGACY_SHARED_KG_COLLECTION: &str = ...` "
            f"in {RUST_CONSTS_FILE.relative_to(REPO_ROOT)} — the alias was "
            "removed; migration-detection code in commands::kg relies on it.",
        )

    def test_legacy_shared_kg_collection_lowercase_c_present(self):
        # v0.2.23 B1: second legacy alias for the v0.2.12–v0.2.22 default.
        self.assertIn(
            "LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C",
            self.constants,
            f"Expected `pub const LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C: "
            f"&str = ...` in {RUST_CONSTS_FILE.relative_to(REPO_ROOT)} — "
            "the alias was removed; case-mismatch detection in "
            "commands::kg + the binding-row self-heal rely on it.",
        )

    def test_rust_last_resort_matches_canonical(self):
        # v0.2.40 W40-C: name change from DEFAULT_* → LAST_RESORT_*; value
        # unchanged.
        self.assertEqual(
            self.constants.get("LAST_RESORT_SHARED_KG_COLLECTION"),
            CANONICAL_SHARED_KG_NAME,
            "Rust LAST_RESORT_SHARED_KG_COLLECTION drifted from "
            f"{CANONICAL_SHARED_KG_NAME!r}",
        )

    def test_rust_legacy_matches_expected(self):
        self.assertEqual(
            self.constants.get("LEGACY_SHARED_KG_COLLECTION"),
            LEGACY_SHARED_KG_NAME,
            "Rust LEGACY_SHARED_KG_COLLECTION drifted from "
            f"{LEGACY_SHARED_KG_NAME!r}",
        )

    def test_rust_legacy_lowercase_c_matches_expected(self):
        # v0.2.23 B1: pin the v0.2.12–v0.2.22 legacy alias on the Rust
        # side too.
        self.assertEqual(
            self.constants.get("LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C"),
            LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            "Rust LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C drifted from "
            f"{LEGACY_SHARED_KG_NAME_LOWERCASE_C!r}",
        )


class CrossLanguageInvariantTests(unittest.TestCase):
    """The headline invariant: Python + Rust must agree byte-for-byte."""

    def test_python_and_rust_canonical_match(self):
        # v0.2.40 W40-C: Rust const renamed `DEFAULT_*` → `LAST_RESORT_*`.
        # The value invariant still holds: Python's _SHARED_KG_NAME and
        # Rust's LAST_RESORT_SHARED_KG_COLLECTION must agree byte-for-byte
        # so fresh installs converge on the same fallback name across
        # surfaces.
        rust = _parse_rust_constants()
        self.assertEqual(
            project_init._SHARED_KG_NAME,
            rust.get("LAST_RESORT_SHARED_KG_COLLECTION"),
            "Python `_SHARED_KG_NAME` and Rust `LAST_RESORT_SHARED_KG_COLLECTION` "
            "diverged — fresh installs / fresh projects without picker "
            "interaction will pick up different defaults on different "
            "surfaces. Sync them and re-run the test.",
        )

    def test_python_and_rust_legacy_match(self):
        rust = _parse_rust_constants()
        self.assertEqual(
            project_init._LEGACY_SHARED_KG_NAME,
            rust.get("LEGACY_SHARED_KG_COLLECTION"),
            "Python `_LEGACY_SHARED_KG_NAME` and Rust "
            "`LEGACY_SHARED_KG_COLLECTION` diverged — migration-detection "
            "across surfaces will recognize different sets of legacy "
            "installs. Sync them and re-run the test.",
        )

    def test_python_and_rust_legacy_lowercase_c_match(self):
        # v0.2.23 B1: cross-language pin for the v0.2.12–v0.2.22 legacy.
        rust = _parse_rust_constants()
        self.assertEqual(
            project_init._LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            rust.get("LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C"),
            "Python `_LEGACY_SHARED_KG_NAME_LOWERCASE_C` and Rust "
            "`LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C` diverged — case-"
            "mismatch detection (install.py case-insensitive adoption + "
            "the binding-row self-heal + the launcher's kg.rs is_shared "
            "probe) will recognise different sets of v0.2.12–v0.2.22 "
            "installs. Sync them and re-run the test.",
        )


class MaintainKgScriptGuardTests(unittest.TestCase):
    """v0.2.54 Track D: templates/scripts/maintain_knowledge_graph.py
    carries a shared-collection refusal whose hazard-name set is a
    LITERAL mirror of the project_init constants (the shipped script
    can't depend on a project_init import succeeding at runtime — the
    guard must hold even when vco_lib resolution is broken). This pins
    the mirror so a rename in project_init fails loudly here."""

    SCRIPT = REPO_ROOT / "templates" / "scripts" / "maintain_knowledge_graph.py"

    def test_guard_set_mirrors_all_known_shared_names(self):
        body = self.SCRIPT.read_text(encoding="utf-8")
        for name in (
            CANONICAL_SHARED_KG_NAME,
            LEGACY_SHARED_KG_NAME,
            # The lowercase-c v0.2.12–v0.2.22 alias needs no separate
            # literal: the guard's case-insensitive comparison of the
            # canonical literal covers it — asserted below.
        ):
            self.assertIn(
                name.lower(),
                body.lower(),
                f"maintain_knowledge_graph.py shared-KG guard lost the "
                f"{name!r} literal — destructive --fix/--rebuild would "
                "no longer refuse on that shared collection.",
            )
        self.assertIn(
            ".strip().lower()",
            body,
            "the guard's case-insensitive comparison was removed — the "
            "lowercase-c legacy alias (v0.2.12–v0.2.22) is only covered "
            "via case-folding of the canonical literal.",
        )


if __name__ == "__main__":
    unittest.main()
