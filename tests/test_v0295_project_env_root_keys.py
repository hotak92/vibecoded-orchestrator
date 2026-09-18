# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""`.claude/env` always carries the orchestrator-root portability keys (v0.2.95).

THE DEFECT (field report 2026-09-14). A correctly installed orchestrator
plus a correctly installed project still produced a NON-DISCOVERABLE
environment: the project's `.claude/env` carried the header advertising
``VCT_ORCHESTRATOR_ROOT`` / ``VCT_INFRASTRUCTURE_DIR`` as "portability keys
(when present)" and none of them were present. The venv ladder's DURABLE tier
— the one that works in a plain terminal, in CI and from cron — reads exactly
that file-backed key, so its absence is what made `kg-sync` refuse while a
perfectly good venv sat two directories away.

WHY IT WAS ABSENT. The three keys were emitted only when a CALLER handed down a
resolved root, and the launcher's `ProjectEnvSettings::populate` hands down
`None` whenever its own `resolve_orchestrator_root` fails (a PATH-installed
launcher binary far from the clone — the case `install.py::
_seed_launcher_install_path` documents). An apply REBUILDS the managed block
from scratch, so such a run does not merely skip the keys: it REMOVES the ones
an earlier bundle update wrote.

THE FIX, and what these tests pin:

1. With no caller-supplied root, the projection resolves the clone from its own
   module location and emits all three keys.
2. It is CONFIRMED, never guessed — an unconfirmable location omits them.
3. A caller-supplied root still wins (the launcher's answer is not overridden).
4. The bundle engine backfills an already-registered project on `--update`,
   which is how an affected project is healed.
5. OWNERSHIP: these are canonical, VCO-owned keys. The managed block carries
   VCO's value; everything outside the markers is preserved verbatim.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import config_projection as cp  # noqa: E402
from vco_lib import project_init  # noqa: E402

ROOT_KEYS = ("VCT_ORCHESTRATOR_ROOT", "VCT_INFRASTRUCTURE_DIR", "VCT_INSTALL_ROOT")


class ModuleRootResolutionTests(unittest.TestCase):
    def test_resolves_the_clone_this_module_ships_inside(self):
        resolved = cp._orchestrator_root_from_module()
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(str(resolved)), REPO_ROOT)

    def test_confirmation_is_positive_not_a_walk_up_guess(self):
        """A copy of `vco_lib` with no `vct-module.json` above it (the
        non-editable site-packages shape) must yield None, not the parent
        directory it happens to sit in. A wrong absolute pointer written into
        every project is worse than an absent one."""
        with tempfile.TemporaryDirectory() as td:
            fake_pkg = Path(td) / "site-packages" / "vco_lib"
            fake_pkg.mkdir(parents=True)
            (fake_pkg / "config_projection.py").write_text("", encoding="utf-8")
            with patch.object(cp, "__file__", str(fake_pkg / "config_projection.py")):
                self.assertIsNone(cp._orchestrator_root_from_module())

    def test_a_foreign_vct_module_manifest_is_not_mistaken_for_the_clone(self):
        """`vct-module.json` also marks THIRD-PARTY VCT modules. The walk
        requires a sibling `vco_lib/` too, so a module manifest above an
        unrelated copy does not qualify."""
        with tempfile.TemporaryDirectory() as td:
            mod = Path(td) / "some-3rd-party-module"
            (mod / "pkg").mkdir(parents=True)
            (mod / "vct-module.json").write_text("{}", encoding="utf-8")
            with patch.object(cp, "__file__", str(mod / "pkg" / "config_projection.py")):
                self.assertIsNone(cp._orchestrator_root_from_module())


class BundleBackfillTests(unittest.TestCase):
    """The delivery: an already-installed project gains the keys on its next
    ordinary bundle update (ruling R17, and the field-reported heal)."""

    def setUp(self):
        from tests.test_install_bundle import _make_fake_orchestrator

        self.tmp = Path(tempfile.mkdtemp(prefix="vct-envroot-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        (self.orch / "infrastructure").mkdir(exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _env_text(self) -> str:
        return (self.proj / ".claude" / "env").read_text(encoding="utf-8")

    def test_update_writes_the_three_keys_for_an_unregistered_project(self):
        """No launcher.db row (the OSS / fork-integrator shape), so the
        standalone writer runs — it must emit the keys the ladder needs."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=False, write_env=True, project_name="EnvRootProj",
        )
        # Simulate the reported state: the file exists, the keys do not.
        env_file = self.proj / ".claude" / "env"
        self.assertTrue(env_file.is_file())
        stripped = "\n".join(
            ln for ln in self._env_text().splitlines()
            if not any(k in ln for k in ROOT_KEYS)
        )
        env_file.write_text(stripped + "\n", encoding="utf-8")
        for key in ROOT_KEYS:
            self.assertNotIn(key, self._env_text())

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=True, write_env=True, project_name="EnvRootProj",
        )
        text = self._env_text()
        self.assertIn(f'export VCT_ORCHESTRATOR_ROOT="{self.orch}"', text)
        self.assertIn(
            f'export VCT_INFRASTRUCTURE_DIR="{self.orch / "infrastructure"}"', text,
        )
        self.assertIn(f'export VCT_INSTALL_ROOT="{self.orch}"', text)

    def test_content_outside_the_managed_block_is_preserved(self):
        """OWNERSHIP DECISION, pinned. The three keys are canonical
        (``_CANONICAL_KEYS``), so the MANAGED block is VCO's and carries VCO's
        value — a stale value inside it is reconciled, not preserved. What the
        user owns is everything OUTSIDE the markers, and that is untouched.
        """
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=False, write_env=True, project_name="EnvRootProj",
        )
        env_file = self.proj / ".claude" / "env"
        env_file.write_text(
            "# my own notes\nexport MY_OWN_KEY=\"kept\"\n" + self._env_text(),
            encoding="utf-8",
        )
        # Corrupt the managed value the way a moved install would.
        text = self._env_text().replace(
            f'export VCT_ORCHESTRATOR_ROOT="{self.orch}"',
            'export VCT_ORCHESTRATOR_ROOT="/gone/stale/clone"',
        )
        env_file.write_text(text, encoding="utf-8")

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=True, write_env=True, project_name="EnvRootProj",
        )
        after = self._env_text()
        self.assertIn('export MY_OWN_KEY="kept"', after)
        self.assertIn("# my own notes", after)
        self.assertIn(f'export VCT_ORCHESTRATOR_ROOT="{self.orch}"', after)
        self.assertNotIn("/gone/stale/clone", after)

    def test_settings_env_block_carries_them_too(self):
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=False, write_env=True, project_name="EnvRootProj",
        )
        settings = json.loads(
            (self.proj / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        env = settings.get("env", {})
        for key in ROOT_KEYS:
            self.assertIn(key, env, settings)
        self.assertEqual(env["VCT_ORCHESTRATOR_ROOT"], str(self.orch))


class CallerRootStillWinsTests(unittest.TestCase):
    def test_explicit_root_is_not_overridden_by_the_module_walk(self):
        """The fallback fires only when the caller could not answer. A
        launcher that DID resolve its root keeps its answer — otherwise a
        second clone on the same machine could silently repoint projects."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            other = tdp / "other-clone"
            (other / "infrastructure").mkdir(parents=True)
            project_folder = tdp / "proj"
            project_folder.mkdir()
            state_dir = tdp / "state"
            state_dir.mkdir()

            from tests.test_config_projection import _make_launcher_db
            _make_launcher_db(
                state_dir / "launcher.db",
                project_id="p-explicit-root",
                project_name="ExplicitRoot",
                project_folder=str(project_folder.resolve()),
                project_slug="explicit-root",
            )
            import os
            with patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                bundle = cp.project_env_from_db(
                    "p-explicit-root",
                    db_path=state_dir / "launcher.db",
                    orchestrator_root=other,
                )
        self.assertEqual(bundle["canonical_env"]["VCT_ORCHESTRATOR_ROOT"], str(other))


if __name__ == "__main__":
    unittest.main()
