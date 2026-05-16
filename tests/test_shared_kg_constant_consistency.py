# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PR-34 (v0.2.12, Group M) — cross-language shared-KG constant invariant.

Pins the canonical shared-KG class name + its legacy alias in lockstep
across the four surfaces that carry the literal:

  * Python  — ``vco_lib/project_init.py::_SHARED_KG_NAME``
              ``vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME``
  * Rust    — ``launcher/src-tauri/src/commands/project_env_settings.rs``
              ``DEFAULT_SHARED_KG_COLLECTION`` / ``LEGACY_SHARED_KG_COLLECTION``

Background: PR-26 (Group E) renamed the canonical from
``VibeCodedTools_KnowledgeGraph`` to ``VibecodedOrchestrator_KnowledgeGraph``
but stayed inside its allowlist, leaving five other surfaces still
hardcoding the OLD name. PR-34 sweeps them. This test pins the resulting
constants in lockstep so any future drift fails CI loudly — the picker's
per-project app_state override mechanism papers over default mismatches
at runtime, but a divergent set of DEFAULTS across surfaces is a footgun
for fresh installs and fresh-project flows.

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


CANONICAL_SHARED_KG_NAME = "VibecodedOrchestrator_KnowledgeGraph"
LEGACY_SHARED_KG_NAME = "VibeCodedTools_KnowledgeGraph"

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
    """Python source of truth for the canonical + legacy names."""

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

    def test_canonical_and_legacy_are_distinct(self):
        # Sanity: if these collide, every migration-detection path that
        # branches on (legacy ⇒ deferral, canonical ⇒ no-op) collapses.
        self.assertNotEqual(
            project_init._SHARED_KG_NAME,
            project_init._LEGACY_SHARED_KG_NAME,
            "canonical and legacy shared-KG names must differ; "
            "otherwise legacy-detection short-circuits to no-op",
        )


class RustConstantsTests(unittest.TestCase):
    """Rust source of truth — parsed from the .rs file directly because
    the Cargo workspace is not part of the pytest build."""

    def setUp(self) -> None:
        self.constants = _parse_rust_constants()

    def test_default_shared_kg_collection_present(self):
        self.assertIn(
            "DEFAULT_SHARED_KG_COLLECTION",
            self.constants,
            f"Expected `pub const DEFAULT_SHARED_KG_COLLECTION: &str = ...` "
            f"in {RUST_CONSTS_FILE.relative_to(REPO_ROOT)} — the constant "
            "was removed or renamed.",
        )

    def test_legacy_shared_kg_collection_present(self):
        self.assertIn(
            "LEGACY_SHARED_KG_COLLECTION",
            self.constants,
            f"Expected `pub const LEGACY_SHARED_KG_COLLECTION: &str = ...` "
            f"in {RUST_CONSTS_FILE.relative_to(REPO_ROOT)} — the alias was "
            "removed; migration-detection code in commands::kg relies on it.",
        )

    def test_rust_default_matches_canonical(self):
        self.assertEqual(
            self.constants.get("DEFAULT_SHARED_KG_COLLECTION"),
            CANONICAL_SHARED_KG_NAME,
            "Rust DEFAULT_SHARED_KG_COLLECTION drifted from "
            f"{CANONICAL_SHARED_KG_NAME!r}",
        )

    def test_rust_legacy_matches_expected(self):
        self.assertEqual(
            self.constants.get("LEGACY_SHARED_KG_COLLECTION"),
            LEGACY_SHARED_KG_NAME,
            "Rust LEGACY_SHARED_KG_COLLECTION drifted from "
            f"{LEGACY_SHARED_KG_NAME!r}",
        )


class CrossLanguageInvariantTests(unittest.TestCase):
    """The headline invariant: Python + Rust must agree byte-for-byte."""

    def test_python_and_rust_canonical_match(self):
        rust = _parse_rust_constants()
        self.assertEqual(
            project_init._SHARED_KG_NAME,
            rust.get("DEFAULT_SHARED_KG_COLLECTION"),
            "Python `_SHARED_KG_NAME` and Rust `DEFAULT_SHARED_KG_COLLECTION` "
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


if __name__ == "__main__":
    unittest.main()
