# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 PLAN-v0284 D3 (P2 / ruling R3): binding-first bootstrap/migrate.

`bootstrap_collections` and `_cmd_migrate_collections` used to name-derive the
KG/Dev/Diagrams collection names from the launcher DISPLAY name on EVERY create
AND update. A project whose primary binding (e.g. `VCODev_KnowledgeGraph`)
differed from its display name ("VibeCoded Orchestrator") got empty
`VibeCodedOrchestrator_*` shells created — and RE-created after the operator
dropped them (the R3 re-creator). D3 makes both flows resolve names
binding-first via the config_projection one-rule SEAM
(`resolve_collection_names_for_folder`, launcher.db binding-first) FIRST, then
an on-disk settings.json env pin (when the launcher DB is unreachable / the
folder is unregistered), then name-derived last resort (fresh create).

D3 INTEGRATION (WP-2 landed, this commit): the AUTHORITATIVE launcher.db read is
now the config_projection seam (`ProjectNotFound`/`DbUnreachable` = "no binding
resolvable" → fall through). The old local `_read_kg_collection_from_launcher_db`
tier is DELETED. The launcher.db binding WINS over the on-disk settings.json pin
(corrected authority — the binding is the source of truth for R3).

REGRESSION PIN (R3): with a launcher.db binding `VCODev_KnowledgeGraph` +
display name "VibeCoded Orchestrator", bootstrap creates `VCODev_Development`
and does NOT create `VibeCodedOrchestrator_*`. Leave-alone: no db + no env ⇒
name-derived (unchanged fresh-create).
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

from tests.test_config_projection import _make_launcher_db  # noqa: E402
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

    def _db_with_binding(self, folder: Path, *, name: str, slug: str,
                         primary: str | None) -> Path:
        """Build a launcher.db registering `folder` with an optional primary
        KG binding. Returns the db path (pass as `db_path=` to the resolver)."""
        db = self.tmp / f"launcher-{slug}.db"
        _make_launcher_db(
            db,
            project_id=f"id-{slug}",
            project_name=name,
            project_folder=str(folder.resolve()),
            project_slug=slug,
            kg_bindings={"primary": primary} if primary else {},
        )
        return db

    def test_seam_binding_first_pin(self):
        """PIN (R3): a launcher.db binding `VCODev_KnowledgeGraph` + display name
        "VibeCoded Orchestrator" ⇒ the SEAM resolves dev VCODev_Development, NOT
        VibeCodedOrchestrator_Development. D3 integration: authoritative via
        config_projection.resolve_collection_names_for_folder."""
        db = self._db_with_binding(
            self.tmp, name="VibeCoded Orchestrator", slug="vco",
            primary="VCODev_KnowledgeGraph",
        )
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", self.tmp, db_path=db,
        )
        self.assertEqual(out["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "VCODev_Development")
        self.assertEqual(out["diagrams_collection"], "VCODev_Diagrams")
        self.assertNotEqual(
            out["development_collection"], "VibeCodedOrchestrator_Development"
        )

    def test_settings_env_pin_used_when_folder_unregistered(self):
        """When the folder is UNREGISTERED (seam raises ProjectNotFound), the
        on-disk settings.json env `KG_COLLECTION` pin is used (standalone CLI
        bootstrap on a folder the launcher never saw)."""
        # A db that does NOT register self.tmp (registers a different folder).
        other = self.tmp / "other"
        other.mkdir()
        db = self._db_with_binding(
            other, name="Other", slug="other", primary="Other_KnowledgeGraph",
        )
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "VCODev_KnowledgeGraph"})
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", self.tmp, db_path=db,
        )
        self.assertEqual(out["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "VCODev_Development")

    def test_settings_env_pin_used_when_db_unreachable(self):
        """When the launcher DB is unreachable (seam raises DbUnreachable), the
        settings.json env pin is used."""
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "VCODev_KnowledgeGraph"})
        missing_db = self.tmp / "does-not-exist" / "launcher.db"
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", self.tmp, db_path=missing_db,
        )
        self.assertEqual(out["kg_collection"], "VCODev_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "VCODev_Development")

    def test_seam_binding_wins_over_settings_env_pin(self):
        """D3-integration authority (CHANGED from pre-seam `tier1_wins_over_tier2`):
        the launcher.db binding is the SOURCE OF TRUTH for R3, so a registered
        folder's binding WINS over a (possibly stale) on-disk settings.json pin.
        Pre-.84-integration the settings.json pin won; the seam corrects this."""
        db = self._db_with_binding(
            self.tmp, name="Display Name", slug="disp",
            primary="FromDb_KnowledgeGraph",
        )
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "FromEnv_KnowledgeGraph"})
        out = project_init._resolve_bundle_collection_names_binding_first(
            "Display Name", self.tmp, db_path=db,
        )
        self.assertEqual(out["kg_collection"], "FromDb_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "FromDb_Development")

    def test_seam_no_binding_name_derives(self):
        """A registered folder with NO primary binding ⇒ the seam name-derives
        internally (fresh create) — dev is name-derived from the display name."""
        db = self._db_with_binding(
            self.tmp, name="Fresh Create", slug="fresh", primary=None,
        )
        out = project_init._resolve_bundle_collection_names_binding_first(
            "Fresh Create", self.tmp, db_path=db,
        )
        self.assertEqual(out["kg_collection"], "FreshCreate_KnowledgeGraph")
        self.assertEqual(out["development_collection"], "FreshCreate_Development")

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

    def test_tier3_name_derived_when_unregistered_and_no_env(self):
        """LEAVE-ALONE: folder present but unregistered (seam ProjectNotFound) +
        no settings pin ⇒ name-derived (identical to
        `derive_project_collection_names`)."""
        other = self.tmp / "other2"
        other.mkdir()
        db = self._db_with_binding(
            other, name="Other", slug="other2", primary="Other_KnowledgeGraph",
        )
        out = project_init._resolve_bundle_collection_names_binding_first(
            "VibeCoded Orchestrator", self.tmp, db_path=db,
        )
        expected = project_init.derive_project_collection_names("VibeCoded Orchestrator")
        self.assertEqual(out["kg_collection"], expected["kg_collection"])
        self.assertEqual(
            out["development_collection"], expected["development_collection"]
        )

    def test_custom_primary_uses_slug_fallback_for_dev(self):
        """A non-`_KnowledgeGraph` custom primary from the settings.json pin ⇒
        dev/diagrams fall back to the sanitized basename (mirrors the hub's
        Decision C via the shared `_dev_diagrams_from_primary`). Uses the
        settings-pin fallback tier (unregistered folder)."""
        other = self.tmp / "other3"
        other.mkdir()
        db = self._db_with_binding(
            other, name="Other", slug="other3", primary="Other_KnowledgeGraph",
        )
        self._write_settings_env(self.tmp, {"KG_COLLECTION": "MyCustomKG"})
        out = project_init._resolve_bundle_collection_names_binding_first(
            "Weird Name", self.tmp, db_path=db,
        )
        self.assertEqual(out["kg_collection"], "MyCustomKG")
        # basename of "Weird Name" via sanitize_for_weaviate_class.
        base = project_init.sanitize_for_weaviate_class("Weird Name")
        self.assertEqual(out["development_collection"], f"{base}_Development")


class BootstrapBindingFirstTests(unittest.TestCase):
    """PIN (R3) at the `bootstrap_collections` seam: a launcher.db binding routes
    the CREATE to the binding-paired names, not the display-name-derived set.

    D3 integration: the R3 fixture is now a launcher.db binding (the AUTHORITATIVE
    source, resolved via the config_projection seam) — the `_resolve_launcher_db_path`
    default is patched so `bootstrap_collections`' `db_path=None` seam call reaches
    the test fixture hermetically.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-bootstrap-"))
        (self.tmp / ".claude").mkdir(parents=True)
        # Register the folder in a fixture launcher.db with the divergent binding.
        self.db = self.tmp / "launcher.db"
        _make_launcher_db(
            self.db,
            project_id="id-vco",
            project_name="VibeCoded Orchestrator",
            project_folder=str(self.tmp.resolve()),
            project_slug="vco",
            kg_bindings={"primary": "VCODev_KnowledgeGraph"},
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_bootstrap_creates_binding_paired_dev_not_display_derived(self):
        """PIN (R3): bootstrap with a launcher.db binding `VCODev_KnowledgeGraph`
        + display name "VibeCoded Orchestrator" plans `VCODev_Development`, NEVER
        `VibeCodedOrchestrator_Development` — resolved through the config_projection
        seam.

        Weaviate is stubbed unreachable so the flow returns after building the
        derived-name set (we assert on the deferral's recorded target names,
        which carry the resolved `derived` dict).
        """
        import vco_lib.config_projection as _cp
        captured = {}

        def _fake_write_bootstrap_deferral(folder, *, project_name, weaviate_url, derived, kg_only):
            captured["derived"] = derived

        with mock.patch.object(
            project_init, "_is_weaviate_reachable", return_value=False,
        ), mock.patch.object(
            project_init, "_attempt_container_restart", return_value=False,
        ), mock.patch.object(
            project_init, "_write_bootstrap_deferral", _fake_write_bootstrap_deferral,
        ), mock.patch.object(
            _cp, "_resolve_launcher_db_path", return_value=self.db,
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
