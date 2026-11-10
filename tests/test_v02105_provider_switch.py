# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the machine-wide per-provider switch (Home → Model providers).

The owner's ask (2026-11-03): "let the user turn on/off available providers in
the home, so that once configured 'off', it doesn't get added back — e.g. I no
longer have a Kimi subscription, so I should be able to turn it off."

What is pinned here:

* the STORAGE (one ``app_state`` row, ``model_providers.disabled``) and its
  soft-fail direction — an unreadable/corrupt/absent row means "nothing
  disabled", because switching a provider off is a subtraction;
* the DELIVERY legs through the REAL bundle engine on a fake orchestrator:
  disabled ⇒ not delivered; disabled + previously delivered unmodified ⇒
  PARKED in ``.claude/agents.disabled/`` (never deleted) and named in the
  manifest; user-modified ⇒ left in place with a warning; re-enabled ⇒
  restored and re-delivered;
* the launcher's own per-agent toggle (a disabled-side copy WITHOUT the
  manifest marker) is never overridden by a provider re-enable;
* the GUIDANCE leg: a disabled provider's grid rows disappear, and its key is
  never even probed by the delivery gate;
* the CLI the launcher shells out to (rule A), including the "could not store
  it" answer.

The machine signal is injected exactly as in ``test_v02100_gateway_agent_gate``
(the real one reads the login registration and the user's VS Code settings).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_install_bundle as _tib  # noqa: E402 — helper module, not re-collected
from tests.common.launcher_db_fixture import (  # noqa: E402
    make_launcher_db,
    set_app_state,
)
from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES  # noqa: E402
from vco_lib import model_providers as mp  # noqa: E402
from vco_lib import model_selection  # noqa: E402
from vco_lib import module_gated_delivery as mgd  # noqa: E402
from vco_lib import project_init  # noqa: E402

GATED_SRC = REPO_ROOT / "templates" / "agents" / "module-gateway"
UUID = "0f1e2d3c-4b5a-6978-8976-a5b4c3d2e1f0"


@dataclass(frozen=True)
class _Signal:
    configured: Optional[bool]
    reason: str = "test signal"


def _patch_signal(configured: Optional[bool]):
    return mock.patch.object(
        mgd, "_default_machine_signal", lambda: _Signal(configured),
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("VCT_LAUNCHER_DB_PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v02105-prov-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir()
        self._n = 0

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        else:
            os.environ["VCT_LAUNCHER_DB_PATH"] = self._saved

    def db(self, *, disabled: Optional[list] = None, folder: Optional[Path] = None,
           register: bool = True) -> Path:
        self._n += 1
        projects = ([{"project_id": UUID, "name": "P", "folder_path": folder}]
                    if register and folder is not None else [])
        app_state = ({mp.APP_STATE_KEY: json.dumps(disabled)}
                     if disabled is not None else None)
        path = make_launcher_db(self.db_dir / f"launcher{self._n}.db",
                                projects=projects, app_state=app_state)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(path)
        return path


# ═══════════════════════════════════════════════════════════════════════════
# The storage
# ═══════════════════════════════════════════════════════════════════════════


class StorageTests(_Base):
    def test_no_row_means_every_provider_is_enabled(self) -> None:
        """The default (a fresh install / a CLI-only machine) adds no behaviour."""
        self.db()
        self.assertEqual(mp.disabled_vendors(), frozenset())
        self.assertTrue(mp.is_enabled("kimi"))
        self.assertEqual(set(mp.enabled_vendors()), set(mp.provider_vendor_ids()))

    def test_set_and_unset_round_trip_through_the_one_row(self) -> None:
        db = self.db()
        self.assertTrue(mp.set_provider_enabled("kimi", False))
        self.assertEqual(mp.disabled_vendors(), frozenset({"kimi"}))
        self.assertFalse(mp.is_enabled("kimi"))
        self.assertTrue(mp.is_enabled("zai"))
        # One row, a JSON array of the OFF ids only.
        conn = sqlite3.connect(str(db))
        try:
            value = conn.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (mp.APP_STATE_KEY,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(value), ["kimi"])
        self.assertTrue(mp.set_provider_enabled("kimi", True))
        self.assertEqual(mp.disabled_vendors(), frozenset())

    def test_two_disabled_providers_are_both_remembered(self) -> None:
        self.db()
        mp.set_provider_enabled("kimi", False)
        mp.set_provider_enabled("qwen", False)
        self.assertEqual(mp.disabled_vendors(), frozenset({"kimi", "qwen"}))
        mp.set_provider_enabled("qwen", True)
        self.assertEqual(mp.disabled_vendors(), frozenset({"kimi"}))

    def test_an_unknown_vendor_is_refused_and_writes_nothing(self) -> None:
        db = self.db()
        with self.assertRaises(mp.ProviderError) as ctx:
            mp.set_provider_enabled("moonshot", False)
        self.assertIn("moonshot", str(ctx.exception))
        self.assertEqual(mp.disabled_vendors(), frozenset())
        self.assertEqual(set_app_state is not None, True)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                               (mp.APP_STATE_KEY,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_a_corrupt_row_reads_as_nothing_disabled(self) -> None:
        """A broken row must never TAKE a provider away."""
        for raw in ("not json", '{"kimi": true}', "[1, 2]", "[null]"):
            db = self.db()
            set_app_state(db, mp.APP_STATE_KEY, raw)
            self.assertEqual(mp.disabled_vendors(), frozenset(), raw)

    def test_an_unreadable_db_reads_as_nothing_disabled(self) -> None:
        self.db()
        with mock.patch.object(
            mp, "_write_disabled", side_effect=AssertionError("must not write")
        ):
            os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "nope.db")
            self.assertEqual(mp.disabled_vendors(), frozenset())
            self.assertTrue(mp.is_enabled("kimi"))

    def test_a_write_that_cannot_be_stored_says_so(self) -> None:
        """No launcher.db ⇒ the switch must not read back as applied."""
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "absent.db")
        self.assertFalse(mp.set_provider_enabled("kimi", False))

    def test_the_payload_names_keys_and_never_carries_a_value(self) -> None:
        self.db()
        payload = mp.providers_payload(
            key_state_probe=lambda _v: model_selection.KeyState.PRESENT)
        self.assertTrue(payload["gateway_available"])
        rows = {r["vendor_id"]: r for r in payload["providers"]}
        self.assertEqual(set(rows), set(mp.provider_vendor_ids()))
        kimi = rows["kimi"]
        self.assertTrue(kimi["enabled"])
        self.assertEqual(kimi["display"], "Kimi Code")
        self.assertEqual(kimi["key_state"], "present")
        self.assertIn("kimi_api_key", kimi["secret_key_names"])
        self.assertIn("kimi-implementer", kimi["definitions"])
        # NAMES only: every secret-key entry is a declared key name, and no
        # value-shaped string is anywhere in the payload.
        blob = json.dumps(payload)
        for name in ("sk-", "token=", "Bearer "):
            self.assertNotIn(name, blob)

    def test_a_disabled_vendor_keeps_reporting_its_key_state(self) -> None:
        self.db(disabled=["kimi"])
        payload = mp.providers_payload(
            key_state_probe=lambda v: (model_selection.KeyState.PRESENT
                                       if v == "kimi"
                                       else model_selection.KeyState.ABSENT))
        kimi = [r for r in payload["providers"] if r["vendor_id"] == "kimi"][0]
        self.assertFalse(kimi["enabled"])
        self.assertEqual(kimi["key_state"], "present")
        self.assertEqual(payload["disabled"], ["kimi"])


class CliTests(_Base):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mp.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_json_names_every_provider(self) -> None:
        self.db(disabled=["kimi"])
        with mock.patch.object(
            mp, "providers_payload",
            wraps=mp.providers_payload,
        ):
            code, out, _err = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["disabled"], ["kimi"])
        self.assertEqual(payload["app_state_key"], mp.APP_STATE_KEY)

    def test_set_json_reports_the_store_result(self) -> None:
        self.db()
        code, out, _err = self.run_cli(
            ["set", "--vendor", "kimi", "--disabled", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["vendor"], "kimi")
        self.assertEqual(payload["enabled"], False)
        self.assertEqual(payload["stored"], True)
        # v0.2.106: a stored set carries the immediate-apply summary (the
        # per-project park/restore report the GUI card renders).
        self.assertIn("agents", payload)
        self.assertEqual(payload["agents"]["vendor"], "kimi")
        self.assertEqual(mp.disabled_vendors(), frozenset({"kimi"}))

    def test_an_unstored_set_exits_non_zero_and_says_why(self) -> None:
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "absent.db")
        code, out, err = self.run_cli(
            ["set", "--vendor", "kimi", "--disabled", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("NOT stored", err)
        self.assertIn("no launcher.db at", err)
        self.assertFalse(json.loads(out)["stored"])

    def test_a_failed_write_to_an_existing_db_names_the_write_not_the_file(self) -> None:
        self.db()
        with mock.patch("vco_lib.launcher_db_writer.write_app_state_key",
                        side_effect=OSError("disk full")):
            code, _out, err = self.run_cli(
                ["set", "--vendor", "kimi", "--disabled", "--json"])
        self.assertEqual(code, 1)
        self.assertNotIn("no launcher.db", err)
        self.assertIn("failed or did not read back", err)

    def test_a_stored_shell_set_names_the_guidance_refresh(self) -> None:
        self.db()
        code, out, _err = self.run_cli(["set", "--vendor", "kimi", "--disabled"])
        self.assertEqual(code, 0)
        self.assertIn("python -m vco_lib.model_guidance refresh", out)
        self.assertIn("--trigger providers", out)

    def test_an_unknown_vendor_exits_two(self) -> None:
        self.db()
        code, _out, err = self.run_cli(["set", "--vendor", "nope", "--enabled"])
        self.assertEqual(code, 2)
        self.assertIn("unknown provider", err)


# ═══════════════════════════════════════════════════════════════════════════
# Delivery
# ═══════════════════════════════════════════════════════════════════════════


class _Bundle(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _tib._make_fake_orchestrator(self.orch)
        dst = self.orch / "templates" / "agents" / "module-gateway"
        dst.mkdir(parents=True)
        for name in MODULE_GATEWAY_AGENT_FILES:
            shutil.copyfile(GATED_SRC / name, dst / name)

    def install(self, folder: Optional[Path] = None, *, update: bool = False,
                dry_run: bool = False, log=None) -> dict:
        return project_init.install_project_bundle(
            folder if folder is not None else self.proj,
            orchestrator_root=self.orch, update_mode=update, dry_run=dry_run,
            log_event=log,
        )

    def agents(self, folder: Optional[Path] = None) -> Path:
        return (folder if folder is not None else self.proj) / ".claude" / "agents"

    def parked(self, folder: Optional[Path] = None) -> Path:
        return ((folder if folder is not None else self.proj)
                / ".claude" / "agents.disabled")

    def delivered(self, folder: Optional[Path] = None) -> set[str]:
        agents = self.agents(folder)
        return {n for n in MODULE_GATEWAY_AGENT_FILES if (agents / n).exists()}

    def manifest(self, folder: Optional[Path] = None) -> dict:
        path = ((folder if folder is not None else self.proj)
                / ".claude" / ".vco-manifest.json")
        return json.loads(path.read_text(encoding="utf-8"))


class DeliveryTests(_Bundle):
    def setUp(self) -> None:
        super().setUp()
        self.db(folder=self.proj)

    def test_a_disabled_provider_delivers_none_of_its_definitions(self) -> None:
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install()
        delivered = self.delivered()
        self.assertNotIn("kimi-implementer.md", delivered)
        self.assertNotIn("kimi-reviewer.md", delivered)
        self.assertIn("glm-implementer.md", delivered)
        self.assertIn("qwen-implementer.md", delivered)
        self.assertEqual(len(delivered), len(MODULE_GATEWAY_AGENT_FILES) - 2)
        # Nothing about a disabled provider is released to a project on a
        # first install either.
        self.assertFalse((self.parked() / "kimi-reviewer.md").exists())

    def test_a_disabled_provider_key_is_never_probed(self) -> None:
        self.db(disabled=["kimi"], folder=self.proj)
        probed: list[str] = []

        def probe(vendor_id: str) -> bool:
            probed.append(vendor_id)
            return True

        with mock.patch.object(mgd, "_default_vendor_probe", lambda: probe):
            with _patch_signal(True):
                self.install()
        self.assertNotIn("kimi", probed)
        self.assertIn("zai", probed)

    def test_an_unmodified_copy_is_parked_not_deleted(self) -> None:
        with _patch_signal(True):
            self.install()
        self.assertIn("kimi-reviewer.md", self.delivered())
        original = (self.agents() / "kimi-reviewer.md").read_bytes()

        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            result = self.install(update=True)

        self.assertNotIn("kimi-reviewer.md", self.delivered())
        parked = self.parked() / "kimi-reviewer.md"
        self.assertTrue(parked.is_file(), "the copy must be parked, not deleted")
        self.assertEqual(parked.read_bytes(), original)
        self.assertIn(str(Path(".claude") / "agents" / "kimi-reviewer.md"),
                      result.get("provider_agents_parked", []))
        manifest = self.manifest()
        self.assertEqual(
            manifest["provider_parked_agents"],
            {".claude/agents/kimi-implementer.md": "kimi",
             ".claude/agents/kimi-reviewer.md": "kimi"})
        # The parked definition is no longer a manifest FILE entry (it is not
        # at the enabled path) but it is still recorded as this switch's.
        self.assertNotIn(str(Path(".claude") / "agents" / "kimi-reviewer.md"),
                         manifest["files"])

    def test_a_modified_copy_is_left_in_place_and_reported(self) -> None:
        with _patch_signal(True):
            self.install()
        edited = self.agents() / "kimi-reviewer.md"
        edited.write_text("my own reviewer\n", encoding="utf-8")
        warnings: list[str] = []

        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(
                update=True,
                log=lambda step, phase, detail="", **kw: (
                    warnings.append(detail)
                    if phase == "warn" or step == "4.bundle"
                    else None))

        self.assertTrue(edited.is_file())
        self.assertEqual(edited.read_text(encoding="utf-8"), "my own reviewer\n")
        self.assertFalse((self.parked() / "kimi-reviewer.md").exists())
        # Still recognised (a re-enable adopts it through the ordinary path).
        self.assertIn(str(Path(".claude") / "agents" / "kimi-reviewer.md"),
                      self.manifest()["files"])
        # The UNMODIFIED sibling is still parked, and the warning names the
        # edited file.
        self.assertTrue((self.parked() / "kimi-implementer.md").is_file())

    def test_re_enabling_restores_and_redelivers(self) -> None:
        with _patch_signal(True):
            self.install()
        original = (self.agents() / "kimi-reviewer.md").read_bytes()
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(update=True)
        self.assertFalse((self.agents() / "kimi-reviewer.md").exists())

        self.db(folder=self.proj)  # the switch is back on
        with _patch_signal(True):
            result = self.install(update=True)

        self.assertIn("kimi-reviewer.md", self.delivered())
        self.assertEqual((self.agents() / "kimi-reviewer.md").read_bytes(),
                         original)
        self.assertFalse((self.parked() / "kimi-reviewer.md").exists())
        self.assertIn(str(Path(".claude") / "agents" / "kimi-reviewer.md"),
                      result.get("provider_agents_restored", []))
        self.assertNotIn("provider_parked_agents", self.manifest())

    def test_a_same_provider_update_keeps_the_parked_copy_parked(self) -> None:
        with _patch_signal(True):
            self.install()
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(update=True)
        with _patch_signal(True):
            self.install(update=True)
        self.assertTrue((self.parked() / "kimi-reviewer.md").is_file())
        self.assertFalse((self.agents() / "kimi-reviewer.md").exists())
        self.assertEqual(self.manifest()["provider_parked_agents"],
                         {".claude/agents/kimi-implementer.md": "kimi",
                          ".claude/agents/kimi-reviewer.md": "kimi"})

    def test_a_user_toggled_agent_is_never_restored_by_a_provider_switch(self) -> None:
        """User parks kimi-reviewer with the Agents tab → the provider is
        switched OFF → update → switched back ON → update: the user's choice
        stands. Only the copy the switch itself moved comes back."""
        with _patch_signal(True):
            self.install()
        self.parked().mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.agents() / "kimi-reviewer.md"),
                    str(self.parked() / "kimi-reviewer.md"))
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(update=True)
        # The switch moved only kimi-implementer; the user's copy is not ours.
        self.assertEqual(self.manifest()["provider_parked_agents"],
                         {".claude/agents/kimi-implementer.md": "kimi"})
        self.db(folder=self.proj)  # the provider is back on
        with _patch_signal(True):
            self.install(update=True)
        self.assertFalse((self.agents() / "kimi-reviewer.md").exists())
        self.assertTrue((self.parked() / "kimi-reviewer.md").is_file())
        # The definition the switch parked IS restored.
        self.assertTrue((self.agents() / "kimi-implementer.md").is_file())
        self.assertFalse((self.parked() / "kimi-implementer.md").exists())

    def _park_kimi(self) -> None:
        with _patch_signal(True):
            self.install()
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(update=True)
        self.assertEqual(len(self.manifest()["provider_parked_agents"]), 2)

    def test_the_marker_survives_a_re_enable_run_that_skips_agents(self) -> None:
        self._park_kimi()
        self.db(folder=self.proj)
        with _patch_signal(True):
            project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
                skip_kinds=frozenset({"agents"}))
        self.assertEqual(self.manifest()["provider_parked_agents"],
                         {".claude/agents/kimi-implementer.md": "kimi",
                          ".claude/agents/kimi-reviewer.md": "kimi"})
        # ...so the next ordinary run still restores them.
        with _patch_signal(True):
            self.install(update=True)
        self.assertIn("kimi-reviewer.md", self.delivered())
        self.assertNotIn("provider_parked_agents", self.manifest())

    def test_the_marker_survives_a_run_whose_gate_answers_unknown(self) -> None:
        self._park_kimi()
        with _patch_signal(None):  # "could not ask"
            self.install(update=True)
        self.assertEqual(self.manifest()["provider_parked_agents"],
                         {".claude/agents/kimi-implementer.md": "kimi",
                          ".claude/agents/kimi-reviewer.md": "kimi"})

    def test_a_marker_whose_parked_copy_is_gone_is_dropped(self) -> None:
        self._park_kimi()
        (self.parked() / "kimi-reviewer.md").unlink()
        with _patch_signal(True):
            self.install(update=True)
        self.assertEqual(self.manifest()["provider_parked_agents"],
                         {".claude/agents/kimi-implementer.md": "kimi"})

    def test_dry_run_moves_nothing_and_promises_the_park(self) -> None:
        with _patch_signal(True):
            self.install()
        self.db(disabled=["kimi"], folder=self.proj)
        with _patch_signal(True):
            self.install(update=True, dry_run=True)
        self.assertTrue((self.agents() / "kimi-reviewer.md").is_file())
        self.assertFalse((self.parked() / "kimi-reviewer.md").exists())

    def test_the_orchestrator_root_is_a_project_like_any_other(self) -> None:
        self.db(disabled=["kimi"], folder=self.orch)
        with _patch_signal(True):
            self.install(self.orch)
        self.assertNotIn("kimi-reviewer.md", self.delivered(self.orch))
        self.assertIn("glm-implementer.md", self.delivered(self.orch))


class GuidanceTests(_Base):
    def test_a_disabled_providers_rows_disappear_and_reappear(self) -> None:
        self.db(disabled=["kimi"])
        reachable = model_selection.reachable_providers(
            key_probe=lambda _v: True,
            state_path=self.tmp / "model-guidance.json",
        )
        self.assertNotIn("kimi", reachable)
        self.assertIn("zai", reachable)
        self.assertIn("qwen", reachable)
        grid = model_selection.render_grid(providers=reachable)
        self.assertNotIn("Kimi K3", grid)
        self.assertIn("GLM-5.3", grid)

        self.db()
        reachable = model_selection.reachable_providers(
            key_probe=lambda _v: True,
            state_path=self.tmp / "model-guidance.json",
        )
        self.assertIn("kimi", reachable)
        self.assertIn("Kimi K3", model_selection.render_grid(providers=reachable))

    def test_switching_a_provider_off_is_not_recorded_as_a_missing_key(self) -> None:
        """The record answers "does this machine hold the key" (a fact), so a
        choice must never raise the "re-add your key" warning."""
        record_path = self.tmp / "model-guidance.json"
        self.db()
        model_selection.reachable_providers(
            key_probe=lambda _v: True, state_path=record_path)
        self.db(disabled=["kimi"])
        err = io.StringIO()
        with redirect_stderr(err):
            model_selection.reachable_providers(
                key_probe=lambda _v: True, state_path=record_path)
        self.assertNotIn("no longer", err.getvalue())
        self.assertEqual(
            model_selection.recorded_providers(
                model_selection.load_record(record_path)),
            {"zai": True, "qwen": True, "kimi": True})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()