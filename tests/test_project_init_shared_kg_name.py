"""PR-26 / Group E (v0.2.12 / 2026-05-16): canonical shared KG name alignment.

`vco_lib.project_init.derive_project_collection_names` returns a dict whose
`shared_kg_collection` value is the canonical class name every project on
this machine uses for the shared cross-project KG. This test pins that
value so any future drift between the Python helper and the Rust /
TypeScript constants is caught at CI time.

Why the name is `VibecodedOrchestrator_KnowledgeGraph` (lowercase-d
"Vibecoded"):
  - Matches the actual class shape on existing user installs (the public
    rename from VibeCodedTools_KnowledgeGraph happened in v0.2.12 but the
    raw class name was already this in production for the orchestrator
    developer's primary install).
  - Matches the launcher IdentityTab fallback constant
    (`launcher/src/lib/project-state/IdentityTab.svelte`).
  - Matches the migration script behaviour
    (`scripts/migrate-shared-kg-schema.{sh,ps1}`).

Out-of-scope drift (tracked in the PR-26 commit body):
  - `launcher/src-tauri/src/commands/project_env_settings.rs::DEFAULT_SHARED_KG_COLLECTION`
    still carries the old name. The launcher's Priority-1 app_state
    override (set by the new picker) takes precedence at runtime, so
    fresh installs that pick a canonical via the GUI override the
    default. Updating the Rust constant is intentionally out-of-scope
    for Group E (Rust default + every test that asserts it is in the
    cross-PR coordination plan).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


CANONICAL_SHARED_KG_NAME = "VibecodedOrchestrator_KnowledgeGraph"


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

    def test_canonical_name_has_lowercase_d_vibecoded(self):
        # Explicit casing pin — the name is "Vibecoded" (lowercase d),
        # NOT "VibeCoded" (capital C+D). This casing distinction is
        # load-bearing because Weaviate class names are case-sensitive
        # at the storage layer; a mismatch routes to a different class.
        self.assertIn("Vibecoded", CANONICAL_SHARED_KG_NAME)
        self.assertNotIn("VibeCoded", CANONICAL_SHARED_KG_NAME)

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


if __name__ == "__main__":
    unittest.main()
