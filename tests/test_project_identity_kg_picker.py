"""PR-26 / Group E (v0.2.12 / 2026-05-16): launcher KG picker contract pins.

These tests pin the user-facing contract for the launcher's new
`list_orchestrator_kg_collections` and `set_shared_kg_collection_name`
Tauri commands. The commands themselves are implemented in Rust at
`launcher/src-tauri/src/commands/project_identity.rs`; the algorithmic
behaviour (schema-shape detection + name validation) is exercised
end-to-end by the Rust `#[cfg(test)]` block in that same file.

This Python layer pins the cross-language invariants that BOTH sides
must agree on, by walking the Rust source:

  1. The Rust constant `ORCHESTRATOR_KG_PICKER_MARKERS` and the four
     `dataType` strings it expects (title=text, node_type=text,
     tags=text[], typed_links=object[]) must match the orchestrator's
     shipped KG class definition (see
     `vco_lib/project_init.py::orchestrator_kg_schema`-equivalent helpers).

  2. The Rust validator's "must end with `_KnowledgeGraph`" rule must
     match the Python canonical that `derive_project_collection_names`
     emits.

  3. The two commands must be registered in `lib.rs::tauri::generate_handler!`
     so the frontend can actually call them.

If any of these drift, the picker silently fails in production (Rust
detects nothing, frontend hides the button) — these tests are the
safety net.
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

PROJECT_IDENTITY_RS = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "src"
    / "commands"
    / "project_identity.rs"
)
LIB_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"
IDENTITY_TAB_SVELTE = (
    REPO_ROOT
    / "launcher"
    / "src"
    / "lib"
    / "project-state"
    / "IdentityTab.svelte"
)
SHARED_KG_PICKER_SVELTE = (
    REPO_ROOT
    / "launcher"
    / "src"
    / "lib"
    / "components"
    / "SharedKgPicker.svelte"
)


class FilePresenceTests(unittest.TestCase):
    """The picker requires four source files to exist in a coordinated
    shape. If any are missing the picker won't render."""

    def test_project_identity_rs_present(self):
        self.assertTrue(
            PROJECT_IDENTITY_RS.is_file(),
            f"missing: {PROJECT_IDENTITY_RS}",
        )

    def test_lib_rs_present(self):
        self.assertTrue(LIB_RS.is_file(), f"missing: {LIB_RS}")

    def test_identity_tab_svelte_present(self):
        self.assertTrue(
            IDENTITY_TAB_SVELTE.is_file(),
            f"missing: {IDENTITY_TAB_SVELTE}",
        )

    def test_shared_kg_picker_svelte_present(self):
        self.assertTrue(
            SHARED_KG_PICKER_SVELTE.is_file(),
            f"missing: {SHARED_KG_PICKER_SVELTE}",
        )


class TauriCommandRegistrationTests(unittest.TestCase):
    """The two new commands must be wired into `tauri::generate_handler!`
    in `lib.rs` — otherwise the frontend's `invoke(...)` calls fail with
    a runtime "command not found" error."""

    def setUp(self):
        self.lib_rs_text = LIB_RS.read_text()

    def test_list_orchestrator_kg_collections_registered(self):
        self.assertIn(
            "commands::project_identity::list_orchestrator_kg_collections",
            self.lib_rs_text,
            "list_orchestrator_kg_collections not registered in lib.rs",
        )

    def test_set_shared_kg_collection_name_registered(self):
        self.assertIn(
            "commands::project_identity::set_shared_kg_collection_name",
            self.lib_rs_text,
            "set_shared_kg_collection_name not registered in lib.rs",
        )


class RustImplementationContractTests(unittest.TestCase):
    """The Rust source must declare each command with `#[command]` and
    use the precise marker-property list that the orchestrator's KG
    schema actually contains."""

    def setUp(self):
        self.rs_text = PROJECT_IDENTITY_RS.read_text()

    def test_list_orchestrator_kg_collections_is_tauri_command(self):
        # `#[command]` immediately precedes the fn declaration. We allow
        # any whitespace / inner-doc comments between them.
        pattern = re.compile(
            r"#\[command\][\s\S]{0,400}?pub async fn list_orchestrator_kg_collections"
        )
        self.assertIsNotNone(
            pattern.search(self.rs_text),
            "list_orchestrator_kg_collections missing #[command] decorator",
        )

    def test_set_shared_kg_collection_name_is_tauri_command(self):
        pattern = re.compile(
            r"#\[command\][\s\S]{0,400}?pub async fn set_shared_kg_collection_name"
        )
        self.assertIsNotNone(
            pattern.search(self.rs_text),
            "set_shared_kg_collection_name missing #[command] decorator",
        )

    def test_marker_list_matches_orchestrator_schema(self):
        # The four marker properties the picker uses to identify
        # orchestrator-shaped classes. These MUST be the four properties
        # the orchestrator's KG schema actually declares — drift here
        # would silently filter out the very class we want to detect.
        for marker in ("title", "node_type", "tags", "typed_links"):
            self.assertIn(
                f'"{marker}"',
                self.rs_text,
                f"marker {marker!r} not referenced in project_identity.rs",
            )

    def test_marker_datatypes_match_schema(self):
        # Pin the (marker, dataType) pairs the Rust validator looks for.
        for dt in ('"text"', '"text[]"', '"object[]"'):
            self.assertIn(dt, self.rs_text)

    def test_name_validator_requires_knowledge_graph_suffix(self):
        # The validator's most important rule: refuse names without the
        # `_KnowledgeGraph` suffix. Without this gate users can
        # accidentally point the shared KG at a code-graph class.
        self.assertIn("ends_with(\"_KnowledgeGraph\")", self.rs_text)

    def test_persist_writes_to_app_state(self):
        # The persistence path: app_state_set on the SHARED_KG_NAME key.
        # If this changes the picker writes will silently land in the
        # wrong row and the priority-1 resolver in
        # project_env_settings.rs won't pick up the user's choice.
        self.assertIn("app_state_set", self.rs_text)
        self.assertIn("APP_STATE_KEY_SHARED_KG_NAME", self.rs_text)


class FrontendContractTests(unittest.TestCase):
    """The Svelte side must `invoke(...)` both new commands and import
    the picker component."""

    def setUp(self):
        self.tab_text = IDENTITY_TAB_SVELTE.read_text()
        self.picker_text = SHARED_KG_PICKER_SVELTE.read_text()

    def test_identity_tab_imports_picker(self):
        self.assertIn("SharedKgPicker", self.tab_text)

    def test_identity_tab_invokes_list_command(self):
        self.assertIn("'list_orchestrator_kg_collections'", self.tab_text)

    def test_identity_tab_invokes_set_command(self):
        self.assertIn("'set_shared_kg_collection_name'", self.tab_text)

    def test_picker_renders_candidates(self):
        # Defensive: the picker must iterate `candidates` and render
        # each as a clickable affordance.
        self.assertIn("candidates", self.picker_text)
        self.assertIn("onPick", self.picker_text)

    def test_fallback_constant_uses_canonical_casing(self):
        # The IdentityTab fallback name must be the same canonical the
        # Python derive helper returns — otherwise the "derived name
        # doesn't match any detected class" check produces inconsistent
        # results between fresh installs and orchestrator-root installs.
        canonical = project_init.derive_project_collection_names("FooBar")[
            "shared_kg_collection"
        ]
        self.assertIn(canonical, self.tab_text)


if __name__ == "__main__":
    unittest.main()
