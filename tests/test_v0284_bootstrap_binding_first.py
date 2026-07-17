# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 PLAN-v0284 D3 (P2 / ruling R3): binding-first bootstrap/migrate.

`bootstrap_collections` and `_cmd_migrate_collections` used to name-derive the
KG/Dev/Diagrams collection names from the launcher DISPLAY name on EVERY create
AND update. A project whose primary binding (e.g. `VCODev_KnowledgeGraph`)
differed from its display name ("VibeCoded Orchestrator") got empty
`VibeCodedOrchestrator_*` shells created — and RE-created after the operator
dropped them (the R3 re-creator). D3 makes both flows resolve names
binding-first: (1) explicit `KG_COLLECTION` in the project's settings.json env,
(2) launcher.db primary binding, (3) name-derived last resort (fresh create).

REGRESSION PIN (R3): with a fixture binding `VCODev_KnowledgeGraph`, bootstrap
creates `VCODev_Development` and does NOT create `VibeCodedOrchestrator_*`.
Leave-alone: no db + no env ⇒ name-derived (unchanged fresh-create).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


class ResolverBindingFirstTests(unittest.TestCase):
    """Direct unit tests on the resolver the D3 call-sites delegate to."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-bindfirst-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _write_settings_env(self, folder: Path, env: dict) -> None:
        claude = folder / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text(
            json.dumps({"env": env}, indent=2), encoding="utf-8"
        )

    def test_tier1_settings_env_pins_primary(self):
        """PIN (R3): settings.json env `KG_COLLECTION=VCODev_KnowledgeGraph` +
        display name "VibeCoded Orchestrator" ⇒ dev is VCODev_Development, NOT
        VibeCodedOrchestrator_Development."""
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "VCODev_KnowledgeGraph"})
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", self.tmp,
        )
        self.assertEqual(out["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "VCODev_Development")
        self.assertEqual(out["diagrams_collection"], "VCODev_Diagrams")
        self.assertNotEqual(
            out["development_collection"], "VibeCodedOrchestrator_Development"
        )

    def test_tier2_launcher_db_binding(self):
        """launcher.db primary binding wins when settings.json has no pin."""
        with mock.patch.object(
            project_init, "_read_kg_collection_from_launcher_db",
            return_value={"primary_kg_collection": "VCODev_KnowledgeGraph"},
        ):
            out = project_init._resolve_bundle_collection_names_binding_first(
                "VibeCoded Orchestrator", self.tmp,
            )
        self.assertEqual(out["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "VCODev_Development")

    def test_tier1_wins_over_tier2(self):
        """An explicit settings.json pin beats the launcher.db binding."""
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "FromEnv_KnowledgeGraph"})
        with mock.patch.object(
            project_init, "_read_kg_collection_from_launcher_db",
            return_value={"primary_kg_collection": "FromDb_KnowledgeGraph"},
        ):
            out = project_init._resolve_bundle_collection_names_binding_first(
                "Display Name", self.tmp,
            )
        self.assertEqual(out["kg_collection"], "FromEnv_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "FromEnv_Development")

    def test_tier3_name_derived_when_no_folder(self):
        """LEAVE-ALONE: no folder ⇒ name-derived (unchanged fresh-create)."""
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", None,
        )
        self.assertEqual(
            out["kg_collection"], "VibeCodedOrchestrator_KnowledgeGraph"
        )
        self.assertEqual(
            out["development_collection"], "VibeCodedOrchestrator_Development"
        )

    def test_tier3_name_derived_when_no_db_no_env(self):
        """LEAVE-ALONE: folder present but no settings pin + no db binding ⇒
        name-derived (identical to `derive_project_collection_names`)."""
        with mock.patch.object(
            project_init, "_read_kg_collection_from_launcher_db",
            return_value={},
        ):
            out = project_init._resolve_bundle_collection_names_binding_first(
                "VibeCoded Orchestrator", self.tmp,
            )
        expected = project_init.derive_project_collection_names("VibeCoded Orchestrator")
        self.assertEqual(out["kg_collection"], expected["kg_collection"])
        self.assertEqual(
            out["development_collection"], expected["development_collection"]
        )

    def test_custom_primary_uses_slug_fallback_for_dev(self):
        """A non-`_KnowledgeGraph` custom primary ⇒ dev/diagrams fall back to the
        sanitized basename (mirrors the hub's Decision C)."""
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "MyCustomKG"})
        out = project_init._resolve_bundle_collection_names_binding_first(
            "Weird Name", self.tmp,
        )
        self.assertEqual(out["kg_collection"], "MyCustomKG")
        # basename of "Weird Name" via sanitize_for_weaviate_class.
        base = project_init.sanitize_for_weaviate_class("Weird Name")
        self.assertEqual(out["development_collection"], f"{base}_Development")


class BootstrapBindingFirstTests(unittest.TestCase):
    """PIN (R3) at the `bootstrap_collections` seam: a fixture binding routes
    the CREATE to the binding-paired names, not the display-name-derived set."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-bootstrap-"))
        (self.tmp / ".claude").mkdir(parents=True)
        (self.tmp / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"KG_COLLECTION": "VCODev_KnowledgeGraph"}}, indent=2),
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_bootstrap_creates_binding_paired_dev_not_display_derived(self):
        """PIN (R3): bootstrap with a fixture binding `VCODev_KnowledgeGraph`
        (via settings.json env) + display name "VibeCoded Orchestrator" plans
        `VCODev_Development`, NEVER `VibeCodedOrchestrator_Development`.

        Weaviate is stubbed unreachable so the flow returns after building the
        derived-name set (we assert on the deferral's recorded target names,
        which carry the resolved `derived` dict).
        """
        captured = {}

        def _fake_write_bootstrap_deferral(folder, *, project_name, weaviate_url, derived, kg_only):
            captured["derived"] = derived

        with mock.patch.object(
            project_init, "_is_weaviate_reachable", return_value=False,
        ), mock.patch.object(
            project_init, "_attempt_container_restart", return_value=False,
        ), mock.patch.object(
            project_init, "_write_bootstrap_deferral", _fake_write_bootstrap_deferral,
        ):
            result = project_init.bootstrap_collections(
                "VibeCoded Orchestrator",
                weaviate_url="http://localhost:65535",
                project_folder=self.tmp,
            )

        self.assertTrue(result["deferred"])
        derived = captured["derived"]
        self.assertEqual(derived["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(derived["development_collection"], "VCODev_Development")
        self.assertEqual(derived["diagrams_collection"], "VCODev_Diagrams")
        self.assertNotEqual(
            derived["development_collection"], "VibeCodedOrchestrator_Development"
        )

    def test_bootstrap_no_folder_name_derives(self):
        """LEAVE-ALONE: bootstrap with no project_folder name-derives (fresh
        create path unchanged)."""
        captured = {}

        with mock.patch.object(
            project_init, "_is_weaviate_reachable", return_value=False,
        ), mock.patch.object(
            project_init, "_attempt_container_restart", return_value=False,
        ):
            result = project_init.bootstrap_collections(
                "VibeCoded Orchestrator",
                weaviate_url="http://localhost:65535",
                project_folder=None,
            )
        # No folder ⇒ no deferral write path exercised, but the resolved names
        # are name-derived — probe the resolver directly for the no-folder case.
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", None,
        )
        self.assertEqual(
            out["development_collection"], "VibeCodedOrchestrator_Development"
        )
        self.assertFalse(result["weaviate_reachable"])


if __name__ == "__main__":
    unittest.main()
