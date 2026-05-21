"""PR-26 / Group E (v0.2.12 / 2026-05-16): canonical shared KG name alignment.

`vco_lib.project_init.derive_project_collection_names` returns a dict whose
`shared_kg_collection` value is the canonical class name every project on
this machine uses for the shared cross-project KG. This test pins that
value so any future drift between the Python helper and the Rust /
TypeScript constants is caught at CI time.

The canonical name is `VibeCodedOrchestrator_KnowledgeGraph` (capital-C
"VibeCoded") since v0.2.23 B1 (2026-05-21):
  - Matches the brand spelling.
  - Existing installs with the v0.2.12–v0.2.22 lowercase-c class
    `VibecodedOrchestrator_KnowledgeGraph` are adopted in place via
    case-insensitive lookup in `install.py::_ensure_collections` and the
    launcher.db binding-row self-heal in
    `install.py::_self_heal_kg_bindings_on_update`. No rename, no
    re-embedding.
  - Matches the launcher IdentityTab fallback constant
    (`launcher/src/lib/project-state/IdentityTab.svelte`).
  - Matches the migration script behaviour
    (`scripts/migrate-shared-kg-schema.{sh,ps1}`).

Drift history:
  - PR-26 / Group E (v0.2.12 / 2026-05-16): renamed
    `VibeCodedTools_KnowledgeGraph` → `VibecodedOrchestrator_KnowledgeGraph`
    (lowercase c) to match the actual on-disk class shape in production.
  - PR-34 / Group M (v0.2.12 / 2026-05-16): swept remaining surfaces.
  - v0.2.23 B1 (2026-05-21): flipped from lowercase-c back to capital-C
    to match the brand spelling. Case-insensitive adoption keeps the
    on-disk class casing unchanged for existing installs.

The cross-language invariant `tests/test_shared_kg_constant_consistency.py`
pins the Python + Rust constants in lockstep so any future drift fails CI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# v0.2.23 B1: canonical casing flipped to capital-C "VibeCoded" to match
# the brand spelling. Existing lowercase-c installs are adopted in place.
CANONICAL_SHARED_KG_NAME = "VibeCodedOrchestrator_KnowledgeGraph"
# v0.2.12–v0.2.22 legacy alias kept for case-mismatch detection.
LEGACY_LOWERCASE_C_NAME = "VibecodedOrchestrator_KnowledgeGraph"


class CanonicalSharedKgNameTests(unittest.TestCase):
    """The Python source of truth for the canonical shared KG class name."""

    def test_constant_matches_canonical(self):
        # Stable across every project_name input — the shared KG is
        # cross-project so its name must not vary with the caller.
        for project_name in ("FooBar", "ExampleProj", "Acme", "any-name", ""):
            result = project_init.derive_project_collection_names(project_name)
            self.assertEqual(
                result["shared_kg_collection"],
                CANONICAL_SHARED_KG_NAME,
                f"shared_kg_collection drifted for project_name={project_name!r}",
            )

    def test_canonical_name_has_capital_c_vibecoded(self):
        # v0.2.23 B1: explicit casing pin — the canonical name is
        # "VibeCoded" (capital C+D), matching the brand spelling. The
        # lowercase-c "Vibecoded" was the v0.2.12–v0.2.22 canonical
        # (now a legacy alias for case-mismatch detection). This casing
        # distinction is load-bearing because Weaviate class names are
        # case-sensitive at the storage layer; a mismatch routes to a
        # different class — that's exactly why install.py now does
        # case-insensitive adoption.
        self.assertIn("VibeCoded", CANONICAL_SHARED_KG_NAME)
        # The literal "Vibecoded" (lowercase c) must NOT appear as a
        # substring after stripping the canonical "VibeCoded" prefix —
        # this catches a regression where someone accidentally drops
        # the capital C back to lowercase.
        residual = CANONICAL_SHARED_KG_NAME.replace("VibeCoded", "X", 1)
        self.assertNotIn(
            "Vibecoded", residual,
            f"unexpected lowercase 'Vibecoded' in canonical name: "
            f"{CANONICAL_SHARED_KG_NAME!r}",
        )

    def test_canonical_name_ends_with_knowledge_graph(self):
        # The launcher's `set_shared_kg_collection_name` Tauri command
        # rejects any name that doesn't end in `_KnowledgeGraph` — this
        # test pins the suffix so that contract holds.
        self.assertTrue(
            CANONICAL_SHARED_KG_NAME.endswith("_KnowledgeGraph"),
            f"{CANONICAL_SHARED_KG_NAME!r} missing _KnowledgeGraph suffix",
        )

    def test_canonical_name_shape_is_weaviate_class_safe(self):
        # Weaviate class names: start with letter, [A-Za-z0-9_] thereafter.
        self.assertTrue(CANONICAL_SHARED_KG_NAME[0].isalpha())
        for ch in CANONICAL_SHARED_KG_NAME[1:]:
            self.assertTrue(
                ch.isalnum() or ch == "_",
                f"invalid char {ch!r} in {CANONICAL_SHARED_KG_NAME!r}",
            )

    def test_lowercase_c_alias_is_recognised_legacy(self):
        # v0.2.23 B1: pin that the v0.2.12–v0.2.22 lowercase-c canonical
        # is now the recognised legacy alias (for case-mismatch detection).
        self.assertEqual(
            project_init._LEGACY_SHARED_KG_NAME_LOWERCASE_C,
            LEGACY_LOWERCASE_C_NAME,
        )
        # And critically: it must differ from the canonical (otherwise
        # case-mismatch detection collapses to a no-op).
        self.assertNotEqual(
            CANONICAL_SHARED_KG_NAME,
            LEGACY_LOWERCASE_C_NAME,
        )
        # And they must differ ONLY in casing (the canonical and legacy
        # represent the same class, just with different brand casing).
        self.assertEqual(
            CANONICAL_SHARED_KG_NAME.lower(),
            LEGACY_LOWERCASE_C_NAME.lower(),
        )


class NoLegacyVibeCodedToolsNameInHotPath(unittest.TestCase):
    """The legacy `VibeCodedTools_KnowledgeGraph` name must NOT be the
    value returned by `derive_project_collection_names` anymore. It MAY
    still appear in historical comments / migration scripts / KG nodes
    that document the rename — those are out-of-scope for this test.
    """

    def test_derive_does_not_return_legacy_name(self):
        for project_name in ("FooBar", "ExampleProj", "Acme"):
            result = project_init.derive_project_collection_names(project_name)
            self.assertNotEqual(
                result["shared_kg_collection"],
                "VibeCodedTools_KnowledgeGraph",
                "Legacy name leaked into derive_project_collection_names; "
                "the constant was reverted or a regression landed",
            )

    def test_derive_does_not_return_lowercase_c_alias(self):
        # v0.2.23 B1: the v0.2.12–v0.2.22 lowercase-c canonical is now a
        # legacy alias too — derive_project_collection_names must return
        # the capital-C canonical, not the lowercase-c legacy.
        for project_name in ("FooBar", "ExampleProj", "Acme"):
            result = project_init.derive_project_collection_names(project_name)
            self.assertNotEqual(
                result["shared_kg_collection"],
                LEGACY_LOWERCASE_C_NAME,
                "Lowercase-c legacy alias leaked into "
                "derive_project_collection_names; the v0.2.23 B1 case-flip "
                "regressed",
            )


if __name__ == "__main__":
    unittest.main()
