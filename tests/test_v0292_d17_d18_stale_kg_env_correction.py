"""The LIVE writer heals a stale KG-collection env value on bundle update.

v0.2.92: this file began as the D18 test suite for a *correction* helper that
rewrote `.claude/settings.json::env` when its `KG_COLLECTION` disagreed with
the registered binding. That helper had zero production callers and its
premise was false — the live writer already overwrites canonical keys from the
binding on every update — so the helper was removed with the user's approval
and the tests that exercised it went with it.

What remains is the part that was always worth having: proof, through the
PRODUCTION entry point (`install_project_bundle(update_mode=True)`), that a
stale pre-rename value on disk is healed. Testing through the production entry
point rather than a private helper is the difference between "fixed" and
"fixed and delivered" — the distinction that made the original D18 fix
invisible.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_kg_binding,
    make_launcher_db,
)
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402

from vco_lib import project_init  # noqa: E402


def _make_project(tmp: Path, settings_env: dict | None = None) -> Path:
    """Fake project folder with `.claude/settings.json` (env block seeded
    when a dict — possibly empty — is given)."""
    folder = tmp / "field-project"
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    body: dict = {"$schema": "ignored", "permissions": {"allow": []}}
    if settings_env is not None:
        body["env"] = settings_env
    (folder / ".claude" / "settings.json").write_text(
        json.dumps(body, indent=2) + "\n", encoding="utf-8",
    )
    return folder


def _make_bound_db(state_dir: Path, project_folder: Path,
                   primary: str | None, shared: str | None) -> Path:
    """launcher.db (REAL shipped schema) with this folder registered and
    the requested live KG bindings — NO `manual_override` sentinel: the
    plain auto-seeded binding IS the registered live binding the field
    defect left unhealed."""
    pid = "00000000-0000-0000-0000-0000000000d18"
    db_path = make_launcher_db(
        state_dir / "launcher.db",
        projects=[{
            "project_id": pid,
            "name": "Field",
            "folder_path": str(project_folder.resolve()),
            "slug": "field",
            "created_at": 0,
            "updated_at": 0,
        }],
    )
    for role, coll in (("primary", primary), ("shared", shared)):
        if coll is None:
            continue
        add_kg_binding(db_path, pid, role, coll, config_json="{}", updated_at=0)
    return db_path


class _Base(unittest.TestCase):
    def on_disk(self, folder: Path) -> dict:
        return json.loads(
            (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
        )["env"]

    def backups(self, folder: Path):
        ctx = folder / ".claude" / "context"
        return list(ctx.glob("settings.json.bak-*")) if ctx.is_dir() else []

    def trail_rows(self, folder: Path):
        path = folder / ".claude" / "logs" / "auto-resolutions.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]












class ProductionEntryPointTests(_Base):
    """The D18 premise check, through the PRODUCTION entry point.

    D18's stated premise: "no number of bundle updates could heal" a stale
    `KG_COLLECTION` because the writer "only ADDED missing keys, preserving
    present ones verbatim". That describes the LEGACY backfill
    (`_backfill_kg_collection_env_in_project`, REMOVED in v0.2.92) — which had ZERO production
    callers (references outside tests are comments). The LIVE writer inside
    `install_project_bundle` (what `install-bundle --update` and the
    launcher's per-project "Update bundle" button run) is
    `_apply_canonical_env_via_config_projection` →
    `vco_lib.config_projection.apply_project_env`, whose contract
    OVERWRITES present canonical keys with the launcher.db-resolved value
    (`_write_json_env_block`: "set env[key] = value").

    These tests pin which path actually heals the field state, so the
    premise cannot silently regress: if the projection ever went back to
    preserve-present semantics, the ghost-collection defect returns.
    """

    def _run_bundle_update(self, folder: Path, state_dir: Path) -> dict:
        with tempfile.TemporaryDirectory() as td:
            orch = Path(td) / "orchestrator"
            orch.mkdir()
            _make_fake_orchestrator(orch)
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                return project_init.install_project_bundle(
                    folder,
                    orchestrator_root=orch,
                    update_mode=True,
                    log_event=lambda *a, **k: None,
                )

    def test_production_bundle_update_heals_stale_kg_collection(self):
        """install_project_bundle(update_mode=True) — the entry point behind
        'install-bundle --update' AND the launcher's Update-bundle button —
        OVERWRITES a present-but-stale KG_COLLECTION with the registered
        live binding, on both canonical surfaces (.claude/settings.json and
        .claude/env). The D18-modeled state heals here already; the private
        the removed backfill added no reachable healing in any state."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "PreRename_KnowledgeGraph",
                "SHARED_KG_COLLECTION": "PreRename_Shared",
                "DEVELOPMENT_COLLECTION": "PreRename_Development",
            })
            (folder / ".claude" / "env").write_text(
                "export KG_COLLECTION=\"PreRename_KnowledgeGraph\"\n",
                encoding="utf-8",
            )
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_bound_db(state_dir, folder,
                           primary="PostRename_KnowledgeGraph",
                           shared="PostRename_Shared")

            result = self._run_bundle_update(folder, state_dir)

            self.assertEqual(result["errors"], [])
            self.assertEqual(
                result["backfill_kg_collection"]["action"], "applied",
                "the canonical env projection must run on every non-dry "
                "bundle pass (create AND update)",
            )
            settings_env = self.on_disk(folder)
            self.assertEqual(settings_env["KG_COLLECTION"],
                             "PostRename_KnowledgeGraph",
                             "live writer must heal the ghost pre-rename value")
            self.assertEqual(settings_env["SHARED_KG_COLLECTION"],
                             "PostRename_Shared")
            self.assertEqual(settings_env["DEVELOPMENT_COLLECTION"],
                             "PostRename_Development")
            claude_env = (folder / ".claude" / "env").read_text(encoding="utf-8")
            self.assertIn("PostRename_KnowledgeGraph", claude_env,
                          "the .claude/env surface (where the field report "
                          "saw the ghost value) heals in the same pass")

    def test_unregistered_project_is_left_alone_by_the_live_writer(self):
        """The ONE state where the live projection does not heal: the project
        is not registered for this folder, so the projection soft-fails
        'not_registered' and leaves the file alone.

        That is correct, and the leave-alone case is worth pinning: with no
        positively resolved binding there is no truth to write, and guessing
        one (from the folder name, say) is how a project ends up pointed at a
        collection nobody chose. The remedy here is RE-REGISTERING the project
        so its folder matches in launcher.db — not an env rewrite."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "PreRename_KnowledgeGraph",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            other_folder = tmp / "some-other-project"
            other_folder.mkdir()
            _make_bound_db(state_dir, other_folder,  # registered, but NOT ours
                           primary="Other_KnowledgeGraph", shared=None)

            result = self._run_bundle_update(folder, state_dir)

            self.assertEqual(
                result["backfill_kg_collection"]["action"], "not_registered",
                "projection must soft-fail, never guess",
            )
            self.assertEqual(
                self.on_disk(folder).get("KG_COLLECTION"),
                "PreRename_KnowledgeGraph",
                "unregistered → the canonical env projection writes nothing "
                "(other settings.json sections like hooks may still update; "
                "the ENV block is what routes KG traffic)",
            )


if __name__ == "__main__":
    unittest.main()
