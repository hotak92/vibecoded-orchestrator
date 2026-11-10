# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the provider switch takes effect AT ONCE: the agents leg.

The owner's ask (2026-11-10): "I should be able to manually enable/disable
providers with the projects/gateway auto-updating on what I set." v0.2.105
moved the definitions only on the NEXT bundle update. This module pins the
immediate leg: a stored ``set`` parks (off) or restores (on) the vendor's
gateway agent definitions in the orchestrator root AND every registered
project, through the bundle engine's own park/restore functions — one home,
no second implementation.

Pinned here, against the REAL CLI (``python -m vco_lib.model_providers set``)
on a fake orchestrator with two registered throwaway projects:

* off ⇒ the vendor's unmodified definitions move to
  ``.claude/agents.disabled/`` everywhere, the manifest entries drop, and the
  additive ``provider_parked_agents`` map records exactly those moves;
* on ⇒ exactly the parked copies come back (a copy the USER parked with the
  Agents tab is never touched), the manifest re-gains a correct ``files``
  entry, and the registry empties;
* on with NO resolving key ⇒ the restore is HELD (as the delivery gate would
  decide), and the summary says so;
* one unreadable project never stops the rest (soft-fail per project);
* the JSON payload carries the per-project summary, so the GUI can say what
  happened — and so does the text output.

The machine signal and the vendor key probe are injected exactly as in
``test_v02105_provider_switch`` (which this file's fixtures extend).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_v02105_provider_switch as t105  # noqa: E402 — fixture base
from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES  # noqa: E402
from vco_lib import model_providers as mp  # noqa: E402
from vco_lib import module_gated_delivery as mgd  # noqa: E402

KIMI_DEFS = sorted(n for n in MODULE_GATEWAY_AGENT_FILES if n.startswith("kimi-"))


class ApplyBase(t105._Bundle):
    """Two registered projects plus the orchestrator root, all installed."""

    def setUp(self) -> None:
        super().setUp()
        self.proj_b = self.tmp / "project-b"
        self.proj_b.mkdir()
        # Both projects + the root in ONE launcher.db (the apply enumerates
        # the registered rows through it).
        self.db(
            folder=self.proj,
            register=False,
        )
        from tests.common.launcher_db_fixture import add_project

        add_project(self.db_path, project_id=t105.UUID, name="Alpha",
                    folder_path=self.proj)
        add_project(self.db_path,
                    project_id="1f2e3d4c-5b6a-7988-8976-a5b4c3d2e1f0",
                    name="Beta", folder_path=self.proj_b)
        with t105._patch_signal(True):
            self.install(self.proj)
            self.install(self.proj_b)
            self.install(self.orch)

    def db(self, **kw):  # keep the fixture's db path around
        path = super().db(**kw)
        self.db_path = path
        return path

    def run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mp.main(argv)
        line = out.getvalue().strip().splitlines()[-1]
        return code, json.loads(line), err.getvalue()

    def set_cli(self, vendor: str, enabled: bool) -> tuple[int, dict, str]:
        # The restore leg consults the per-project module gate, whose default
        # reads THIS machine's VS Code settings — inject "configured" exactly
        # as the delivery tests do, so the run is hermetic.
        with t105._patch_signal(True):
            return self.run_cli([
                "set", "--vendor", vendor,
                "--enabled" if enabled else "--disabled", "--json",
                "--orchestrator-root", str(self.orch),
            ])

    def enabled_names(self, folder: Path) -> set[str]:
        return {p.name for p in (folder / ".claude" / "agents").glob("kimi-*.md")}

    def parked_names(self, folder: Path) -> set[str]:
        disabled = folder / ".claude" / "agents.disabled"
        if not disabled.is_dir():
            return set()
        return {p.name for p in disabled.glob("kimi-*.md")}


class ParkOnSwitchOffTests(ApplyBase):
    def test_off_parks_the_vendor_everywhere_at_once(self) -> None:
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        self.assertTrue(payload["stored"])
        for folder in (self.proj, self.proj_b, self.orch):
            self.assertEqual(self.enabled_names(folder), set(),
                             f"{folder}: nothing kimi may stay enabled")
            self.assertEqual(self.parked_names(folder), set(KIMI_DEFS),
                             f"{folder}: both kimi definitions parked")
            manifest = self.manifest(folder)
            for name in KIMI_DEFS:
                rel = f".claude/agents/{name}"
                self.assertNotIn(rel, {k.replace("\\", "/")
                                       for k in manifest["files"]})
            self.assertEqual(
                manifest["provider_parked_agents"],
                {f".claude/agents/{name}": "kimi" for name in KIMI_DEFS})
        # The other vendors' definitions never moved.
        self.assertIn("glm-reviewer.md",
                      {p.name for p in (self.proj / ".claude" / "agents").glob("*.md")})

    def test_the_payload_reports_what_happened_per_project(self) -> None:
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        agents = payload["agents"]
        self.assertEqual(agents["vendor"], "kimi")
        self.assertFalse(agents["enabled"])
        self.assertFalse(agents["held"])
        self.assertEqual(agents["parked_projects"], 3)
        self.assertEqual(agents["failed_projects"], 0)
        folders = {p["folder"] for p in agents["projects"]}
        self.assertEqual(folders, {str(self.proj), str(self.proj_b),
                                   str(self.orch)})
        for row in agents["projects"]:
            self.assertEqual(row["status"], "ok")
            self.assertEqual(sorted(row["parked"]), KIMI_DEFS)

    def test_off_twice_is_quiet_and_keeps_the_marker(self) -> None:
        self.set_cli("kimi", False)
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        self.assertEqual(payload["agents"]["parked_projects"], 0)
        self.assertEqual(self.parked_names(self.proj), set(KIMI_DEFS))
        self.assertEqual(
            self.manifest(self.proj)["provider_parked_agents"],
            {f".claude/agents/{name}": "kimi" for name in KIMI_DEFS})

    def test_an_edited_copy_is_left_in_place_and_reported(self) -> None:
        edited = self.proj / ".claude" / "agents" / "kimi-reviewer.md"
        edited.write_text("my own reviewer\n", encoding="utf-8")
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        self.assertTrue(edited.is_file())
        self.assertEqual(edited.read_text(encoding="utf-8"), "my own reviewer\n")
        self.assertEqual(self.parked_names(self.proj), {"kimi-implementer.md"})
        row = [p for p in payload["agents"]["projects"]
               if p["folder"] == str(self.proj)][0]
        self.assertEqual(row["parked"], ["kimi-implementer.md"])
        self.assertEqual(row["kept"], ["kimi-reviewer.md"])
        # Still recognised: the manifest entry stays so a later state still
        # tracks the file.
        self.assertIn(".claude/agents/kimi-reviewer.md",
                      {k.replace("\\", "/")
                       for k in self.manifest(self.proj)["files"]})


class RestoreOnSwitchOnTests(ApplyBase):
    def setUp(self) -> None:
        super().setUp()
        code, payload, _ = self.set_cli("kimi", False)
        assert code == 0 and payload["stored"]

    def test_on_restores_exactly_what_was_parked(self) -> None:
        original = (self.orch / "templates" / "agents" / "module-gateway"
                    / "kimi-reviewer.md").read_bytes()
        code, payload, _err = self.set_cli("kimi", True)
        self.assertEqual(code, 0)
        self.assertEqual(payload["agents"]["restored_projects"], 3)
        for folder in (self.proj, self.proj_b, self.orch):
            self.assertEqual(self.enabled_names(folder), set(KIMI_DEFS))
            self.assertEqual(self.parked_names(folder), set())
            self.assertNotIn("provider_parked_agents", self.manifest(folder))
            # The manifest re-gains a correct files entry: the on-disk hash,
            # and the shipped source the next update reconciles against.
            manifest = self.manifest(folder)
            entry = manifest["files"].get(".claude/agents/kimi-reviewer.md") or \
                manifest["files"][".claude\\agents\\kimi-reviewer.md"]
            from vco_lib.hashing import sha256_file

            self.assertEqual(
                entry["sha256"],
                sha256_file(folder / ".claude" / "agents" / "kimi-reviewer.md"))
            self.assertEqual(
                entry["source"].replace("\\", "/"),
                "templates/agents/module-gateway/kimi-reviewer.md")
        self.assertEqual((self.proj / ".claude" / "agents"
                          / "kimi-reviewer.md").read_bytes(), original)

    def test_on_with_no_resolving_key_stays_held(self) -> None:
        """The key gate outranks the switch on the way back: delivery would
        hold the definition, so the immediate restore holds it too."""
        with mock.patch.object(mgd, "_default_vendor_probe",
                               lambda: (lambda _v: False)):
            code, payload, _err = self.set_cli("kimi", True)
        self.assertEqual(code, 0)
        self.assertTrue(payload["stored"])
        agents = payload["agents"]
        self.assertTrue(agents["held"])
        self.assertIn("key", agents["held_reason"])
        self.assertEqual(agents["restored_projects"], 0)
        for folder in (self.proj, self.proj_b, self.orch):
            self.assertEqual(self.enabled_names(folder), set())
            self.assertEqual(self.parked_names(folder), set(KIMI_DEFS))
            self.assertEqual(
                self.manifest(folder)["provider_parked_agents"],
                {f".claude/agents/{name}": "kimi" for name in KIMI_DEFS})

    def test_a_user_parked_copy_is_never_restored_by_the_switch(self) -> None:
        # The user parked kimi-reviewer in project-b BEFORE the switch moved
        # anything there: rebuild that state by hand.
        dis = self.proj_b / ".claude" / "agents.disabled"
        # (the switch parked both; move the reviewer back to enabled and
        #  re-park it WITHOUT the marker, as the Agents tab would)
        shutil.move(str(dis / "kimi-reviewer.md"),
                    str(self.proj_b / ".claude" / "agents" / "kimi-reviewer.md"))
        manifest_path = self.proj_b / ".claude" / ".vco-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider_parked_agents"] = {
            ".claude/agents/kimi-implementer.md": "kimi"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        shutil.move(str(self.proj_b / ".claude" / "agents" / "kimi-reviewer.md"),
                    str(dis / "kimi-reviewer.md"))

        code, _payload, _err = self.set_cli("kimi", True)
        self.assertEqual(code, 0)
        # The user's own parked copy stays put; the switch's comes back.
        self.assertTrue((dis / "kimi-reviewer.md").is_file())
        self.assertFalse((self.proj_b / ".claude" / "agents"
                          / "kimi-reviewer.md").exists())
        self.assertTrue((self.proj_b / ".claude" / "agents"
                         / "kimi-implementer.md").is_file())


class SoftFailTests(ApplyBase):
    def test_one_unreadable_project_never_stops_the_rest(self) -> None:
        # A manifest that is not JSON fails THAT project, not the run.
        (self.proj_b / ".claude" / ".vco-manifest.json").write_text(
            "not json", encoding="utf-8")
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        agents = payload["agents"]
        self.assertEqual(agents["failed_projects"], 1)
        row = [p for p in agents["projects"]
               if p["folder"] == str(self.proj_b)][0]
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])
        self.assertEqual(self.parked_names(self.proj), set(KIMI_DEFS))
        self.assertEqual(self.parked_names(self.orch), set(KIMI_DEFS))

    def test_a_project_without_a_manifest_is_skipped_not_failed(self) -> None:
        lone = self.tmp / "lone"
        lone.mkdir()
        from tests.common.launcher_db_fixture import add_project

        add_project(self.db_path,
                    project_id="2f3e4d5c-6b7a-8988-8976-a5b4c3d2e1f0",
                    name="Lone", folder_path=lone)
        code, payload, _err = self.set_cli("kimi", False)
        self.assertEqual(code, 0)
        row = [p for p in payload["agents"]["projects"]
               if p["folder"] == str(lone)][0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(payload["agents"]["failed_projects"], 0)
        self.assertEqual(self.parked_names(self.proj), set(KIMI_DEFS))

    def test_an_unstored_set_applies_nothing(self) -> None:
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "absent.db")
        code, payload, _err = self.run_cli([
            "set", "--vendor", "kimi", "--disabled", "--json",
            "--orchestrator-root", str(self.orch),
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["stored"])
        self.assertNotIn("agents", payload)
        self.assertEqual(self.parked_names(self.proj), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
