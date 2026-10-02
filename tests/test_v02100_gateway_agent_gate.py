# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-10 (AD-7, U20) — the gateway agent gate, end to end.

The ten ``templates/agents/module-gateway/`` definitions reached no project on
any machine before this release: the only opener was a per-project launcher.db
row nothing in the ordinary flow wrote, and "could not read the DB" collapsed
into "no" on a path that DELETES (L5-F01/F02). These tests pin the rebuilt
gate against the REAL launcher.db schema (``tests/common/launcher_db_fixture``)
and the REAL bundle engine:

* the verdict table (explicit row > machine signal; unreadable/locked DB →
  UNKNOWN; no DB / unregistered → machine signal);
* delivery on a PROJECT target and on the ORCHESTRATOR ROOT (same enumerator);
* leave-alone (explicit opt-out; machine not configured);
* UNKNOWN carries previously delivered definitions forward — never deletes —
  and records ``gated_delivery_unknown``; a later decisive run clears it;
* a SKIP on a configured machine records ``gated_delivery_skipped``;
* a retired definition is orphan-removed on a decisive run (F-W1-18) and
  carried forward on an undecidable one;
* the machine signal's own legs (registration x panel, tri-state);
* the shipped set is EXACTLY the owner's ten, and every ``model:`` id is one
  the gateway registry routes and knows (F-W1-11a / F-W1-17);
* the hand-written-definition check names the closest valid ids.

The machine signal is injected (``_default_machine_signal`` patched): the real
one reads the login registration and the user's VS Code settings, which a
test must never depend on.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_install_bundle as _tib  # noqa: E402 — helper module, not re-collected
from tests.common.launcher_db_fixture import (  # noqa: E402
    create_corrupt_launcher_db,
    create_empty_launcher_db,
    insert_rows,
    make_launcher_db,
)
from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES  # noqa: E402
from vco_lib import module_gated_delivery as mgd  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

GATED_SRC = REPO_ROOT / "templates" / "agents" / "module-gateway"
UUID = "0f1e2d3c-4b5a-6978-8976-a5b4c3d2e1f0"

#: The owner's shipped set (F-W1-17, owner correction 2026-09-29): z.ai
#: implementer/reviewer/planner/flash-researcher and the six qwen-provider
#: lanes. NO flash reviewer on either provider.
OWNER_SHIPPED_SET = frozenset({
    "glm-implementer.md", "glm-reviewer.md", "glm-planner.md",
    "glm-flash-researcher.md",
    "qwen-implementer.md", "qwen-flash-implementer.md",
    "qwen-flash-sweeper.md", "qwen-flash-researcher.md",
    "deepseek-implementer.md", "deepseek-researcher.md",
})


@dataclass(frozen=True)
class _Signal:
    configured: Optional[bool]
    reason: str = "test signal"


def _patch_signal(configured: Optional[bool]):
    return mock.patch.object(
        mgd, "_default_machine_signal", lambda: _Signal(configured),
    )


class _Env(unittest.TestCase):
    """Tmp fake orchestrator + project, a launcher.db override, restored."""

    def setUp(self) -> None:
        self._saved = os.environ.get("VCT_LAUNCHER_DB_PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v02100-gate-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _tib._make_fake_orchestrator(self.orch)
        dst = self.orch / "templates" / "agents" / "module-gateway"
        dst.mkdir(parents=True)
        for name in MODULE_GATEWAY_AGENT_FILES:
            shutil.copyfile(GATED_SRC / name, dst / name)
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir()

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        else:
            os.environ["VCT_LAUNCHER_DB_PATH"] = self._saved

    # -- DB shapes ---------------------------------------------------------

    def db(self, *, folder: Optional[Path] = None, enabled: Optional[int] = None,
           register: bool = True) -> Path:
        folder = folder if folder is not None else self.proj
        projects = [{"project_id": UUID, "name": "P", "folder_path": folder}] \
            if register else []
        self._n = getattr(self, "_n", 0) + 1  # a fresh file per shape
        db = make_launcher_db(self.db_dir / f"launcher{self._n}.db", projects=projects)
        if enabled is not None and register:
            insert_rows(db, "project_modules", [{
                "project_id": UUID, "module_name": "model_gateway",
                "enabled": enabled,
            }])
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db)
        return db

    def corrupt_db(self) -> Path:
        self._n = getattr(self, "_n", 0) + 1
        path = self.db_dir / f"corrupt{self._n}.db"
        create_corrupt_launcher_db(path)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(path)
        return path

    # -- bundle helpers ----------------------------------------------------

    def install(self, folder: Optional[Path] = None, *, update: bool = False) -> dict:
        return project_init.install_project_bundle(
            folder if folder is not None else self.proj,
            orchestrator_root=self.orch, update_mode=update,
        )

    def delivered(self, folder: Optional[Path] = None) -> set[str]:
        agents = (folder if folder is not None else self.proj) / ".claude" / "agents"
        return {n for n in MODULE_GATEWAY_AGENT_FILES if (agents / n).exists()}

    def manifest_files(self, folder: Optional[Path] = None) -> dict:
        path = (folder if folder is not None else self.proj) / ".claude" / ".vco-manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))["files"]


# ═══════════════════════════════════════════════════════════════════════════
# The verdict table
# ═══════════════════════════════════════════════════════════════════════════


class GateVerdictTableTests(_Env):
    def gate(self, configured: Optional[bool]) -> mgd.GateVerdict:
        return mgd.gateway_agents_gate(
            self.proj, machine_signal=lambda: _Signal(configured))

    def test_no_row_follows_the_machine_signal(self) -> None:
        self.db()
        self.assertIs(self.gate(True).state, mgd.GateState.DELIVER)
        self.assertEqual(self.gate(True).signal, mgd.SIGNAL_MACHINE)
        self.assertIs(self.gate(False).state, mgd.GateState.SKIP)
        self.assertIs(self.gate(None).state, mgd.GateState.UNKNOWN)

    def test_explicit_on_delivers_even_without_a_gateway(self) -> None:
        self.db(enabled=1)
        v = self.gate(False)
        self.assertIs(v.state, mgd.GateState.DELIVER)
        self.assertEqual(v.signal, mgd.SIGNAL_PROJECT_ROW)

    def test_explicit_off_skips_and_remembers_the_machine_was_configured(self) -> None:
        self.db(enabled=0)
        v = self.gate(True)
        self.assertIs(v.state, mgd.GateState.SKIP)
        self.assertEqual(v.signal, mgd.SIGNAL_PROJECT_ROW)
        self.assertIs(v.machine_configured, True)

    def test_no_launcher_db_file_is_an_answer_not_unknown(self) -> None:
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "absent" / "launcher.db")
        self.assertIs(self.gate(True).state, mgd.GateState.DELIVER)
        self.assertIs(self.gate(False).state, mgd.GateState.SKIP)

    def test_unregistered_folder_follows_the_machine_signal(self) -> None:
        self.db(register=False)
        self.assertIs(self.gate(True).state, mgd.GateState.DELIVER)
        self.assertIs(self.gate(False).state, mgd.GateState.SKIP)

    def test_corrupt_db_is_unknown_whatever_the_machine_says(self) -> None:
        self.corrupt_db()
        for configured in (True, False, None):
            v = self.gate(configured)
            self.assertIs(v.state, mgd.GateState.UNKNOWN, configured)
            self.assertEqual(v.signal, mgd.SIGNAL_LAUNCHER_DB)

    def test_db_without_project_modules_table_is_unknown(self) -> None:
        # Real migrations up to 021: `projects` exists, `project_modules`
        # (migration 022) does not — a DB the gate cannot fully ask.
        path = create_empty_launcher_db(self.db_dir / "old.db", up_to=21)
        from tests.common.launcher_db_fixture import add_project
        add_project(path, project_id=UUID, name="P", folder_path=self.proj)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(path)
        self.assertIs(self.gate(True).state, mgd.GateState.UNKNOWN)

    def test_a_locked_db_is_unknown(self) -> None:
        """The transient that used to delete agents during "Update all"."""
        db = self.db()
        holder = sqlite3.connect(str(db), timeout=0, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA journal_mode=DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            v = self.gate(True)
        finally:
            holder.execute("ROLLBACK")
        self.assertIs(v.state, mgd.GateState.UNKNOWN)

    def test_resolve_active_modules_opens_read_only(self) -> None:
        """L5-F02: the resolver used to open launcher.db READ-WRITE."""
        db = self.db(enabled=1)
        seen: list[str] = []
        real = sqlite3.connect

        def spy(target, *a, **kw):
            seen.append(str(target))
            return real(target, *a, **kw)

        with mock.patch.object(mgd.sqlite3, "connect", spy):
            active = project_init.resolve_active_modules(UUID, db_path=db)
        self.assertIn("model_gateway", active)
        self.assertTrue(seen and all("mode=ro" in s for s in seen), seen)

    def test_modules_verdict_is_tri_state(self) -> None:
        db = self.db(enabled=1)
        self.assertEqual(mgd.active_modules_verdict(UUID, db_path=db).source,
                         mgd.MODULES_FROM_DB)
        self.assertEqual(
            mgd.active_modules_verdict(UUID, db_path=self.tmp / "nope.db").source,
            mgd.MODULES_DEFAULT)
        bad = create_corrupt_launcher_db(self.tmp / "bad.db")
        self.assertEqual(mgd.active_modules_verdict(UUID, db_path=bad).source,
                         mgd.MODULES_UNKNOWN)


# ═══════════════════════════════════════════════════════════════════════════
# Delivery through the real bundle engine — project AND root
# ═══════════════════════════════════════════════════════════════════════════


class BundleDeliveryTests(_Env):
    def test_configured_machine_no_row_delivers_all_ten_to_a_project(self) -> None:
        self.db()
        with _patch_signal(True):
            self.install()
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))
        for name in MODULE_GATEWAY_AGENT_FILES:
            self.assertEqual(
                (self.proj / ".claude" / "agents" / name).read_bytes(),
                (GATED_SRC / name).read_bytes(), name)

    def test_configured_machine_delivers_to_the_orchestrator_root_too(self) -> None:
        """The root update goes through the same enumerator (AD-7)."""
        self.db(folder=self.orch)
        with _patch_signal(True):
            self.install(self.orch)
        self.assertEqual(self.delivered(self.orch), set(MODULE_GATEWAY_AGENT_FILES))

    def test_root_without_a_gateway_gets_none(self) -> None:
        self.db(folder=self.orch)
        with _patch_signal(False):
            self.install(self.orch)
        self.assertEqual(self.delivered(self.orch), set())

    def test_machine_without_gateway_delivers_nothing(self) -> None:
        self.db()
        with _patch_signal(False):
            result = self.install()
        self.assertEqual(self.delivered(), set())
        self.assertIn(str(Path(".claude") / "agents" / "coder.md"),
                      result["actions"]["create"])

    def test_explicit_off_delivers_nothing_and_records_the_skip(self) -> None:
        self.db(enabled=0)
        lines: list[str] = []

        def log_event(step, phase, detail="", **_kw):
            if step == "4.bundle.gated":
                lines.append(detail)

        with _patch_signal(True):
            project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=False,
                log_event=log_event)
        self.assertEqual(self.delivered(), set())
        self.assertTrue(DeferralReport.read(self.proj).has_condition(mgd.CID_SKIPPED))
        self.assertTrue(any("SKIP" in ln for ln in lines), lines)

    def test_off_on_an_unconfigured_machine_is_not_a_record(self) -> None:
        self.db(enabled=0)
        with _patch_signal(False):
            self.install()
        self.assertFalse(DeferralReport.read(self.proj).has_condition(mgd.CID_SKIPPED))

    def test_explicit_on_after_off_redelivers_and_clears_the_record(self) -> None:
        self.db(enabled=0)
        with _patch_signal(True):
            self.install()
        self.db(enabled=1)
        with _patch_signal(True):
            self.install(update=True)
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))
        self.assertFalse(DeferralReport.read(self.proj).has_condition(mgd.CID_SKIPPED))


class UnknownCarriesForwardTests(_Env):
    """L5-F02: "could not ask" never deletes what an earlier run delivered."""

    def _deliver_first(self) -> dict:
        self.db()
        with _patch_signal(True):
            self.install()
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))
        return {k: v for k, v in self.manifest_files().items()
                if "agents" in k and Path(k).name in MODULE_GATEWAY_AGENT_FILES}

    def test_unreadable_db_on_update_keeps_every_definition_and_manifest_entry(self) -> None:
        before = self._deliver_first()
        self.corrupt_db()
        with _patch_signal(True):
            result = self.install(update=True)
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))
        after = self.manifest_files()
        for rel, entry in before.items():
            self.assertEqual(after.get(rel), entry, rel)
        self.assertEqual(
            [p for p in result["actions"]["orphan-deleted"] if "agents" in p], [])
        self.assertTrue(DeferralReport.read(self.proj).has_condition(mgd.CID_UNKNOWN))

    def test_machine_signal_unknown_on_update_keeps_them_too(self) -> None:
        self._deliver_first()
        with _patch_signal(None):
            self.install(update=True)
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))
        self.assertTrue(DeferralReport.read(self.proj).has_condition(mgd.CID_UNKNOWN))

    def test_the_next_decisive_run_clears_the_unknown_record(self) -> None:
        self._deliver_first()
        self.corrupt_db()
        with _patch_signal(True):
            self.install(update=True)
        self.db()
        with _patch_signal(True):
            self.install(update=True)
        self.assertFalse(DeferralReport.read(self.proj).has_condition(mgd.CID_UNKNOWN))
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))

    def test_dry_run_writes_no_ledger(self) -> None:
        self.corrupt_db()
        with _patch_signal(True):
            project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=False,
                dry_run=True)
        self.assertFalse(DeferralReport.read(self.proj).has_condition(mgd.CID_UNKNOWN))


class RetiredDefinitionTests(_Env):
    """F-W1-18: a definition the release stops shipping leaves installed
    projects on a decisive update (unmodified → deleted, edited → kept and
    no longer managed), and is carried forward on an undecidable one."""

    RETIRED = "glm-flash-reviewer.md"

    def setUp(self) -> None:
        super().setUp()
        self.retired_src = (self.orch / "templates" / "agents" / "module-gateway"
                            / self.RETIRED)
        self.retired_src.write_text(
            "---\nname: glm-flash-reviewer\nmodel: claude-gw/glm-5.3-flash[1m]\n---\nx\n",
            encoding="utf-8")
        self.db()
        with _patch_signal(True):
            self.install()
        self.dst = self.proj / ".claude" / "agents" / self.RETIRED
        self.assertTrue(self.dst.exists())
        self.retired_src.unlink()

    def test_unmodified_retired_copy_is_removed_on_a_deliver_run(self) -> None:
        with _patch_signal(True):
            self.install(update=True)
        self.assertFalse(self.dst.exists())
        self.assertEqual(self.delivered(), set(MODULE_GATEWAY_AGENT_FILES))

    def test_edited_retired_copy_is_kept_and_unmanaged(self) -> None:
        self.dst.write_text("user edit\n", encoding="utf-8")
        with _patch_signal(True):
            self.install(update=True)
        self.assertTrue(self.dst.exists())
        rel = str(Path(".claude") / "agents" / self.RETIRED)
        self.assertNotIn(rel, self.manifest_files())

    def test_unknown_run_carries_the_retired_copy_forward(self) -> None:
        self.corrupt_db()
        with _patch_signal(True):
            self.install(update=True)
        self.assertTrue(self.dst.exists())


class RecordGateOutcomesTests(unittest.TestCase):
    def test_log_lines_name_every_verdict(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vct-v02100-rec-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        lines: list[str] = []
        pfx = mgd.GATEWAY_AGENTS_SOURCE_PREFIX
        emitted = mgd.record_gate_outcomes(tmp, [
            (pfx, mgd.GateVerdict(mgd.GateState.SKIP, "machine", "no gateway",
                                  machine_configured=False)),
        ], log=lines.append)
        self.assertEqual(emitted, [])
        self.assertTrue(lines and "SKIP" in lines[0])

    def test_carries_forward_matches_windows_separators(self) -> None:
        unknown = mgd.GateVerdict(mgd.GateState.UNKNOWN, "launcher_db", "x")
        outcomes = [(mgd.GATEWAY_AGENTS_SOURCE_PREFIX, unknown)]
        self.assertTrue(mgd.carries_forward(
            {"source": "templates\\agents\\module-gateway\\glm-implementer.md"},
            outcomes))
        self.assertFalse(mgd.carries_forward(
            {"source": "templates/agents/free/coder.md"}, outcomes))
        deliver = mgd.GateVerdict(mgd.GateState.DELIVER, "machine", "x")
        self.assertFalse(mgd.carries_forward(
            {"source": "templates/agents/module-gateway/glm-implementer.md"},
            [(mgd.GATEWAY_AGENTS_SOURCE_PREFIX, deliver)]))


# ═══════════════════════════════════════════════════════════════════════════
# The machine signal (gateway_ensure.machine_gateway_signal)
# ═══════════════════════════════════════════════════════════════════════════


class MachineSignalTests(unittest.TestCase):
    PORT = 11977

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v02100-sig-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        env = mock.patch.dict(os.environ, {"VCT_MODEL_GATEWAY_PORT": str(self.PORT)})
        env.start()
        self.addCleanup(env.stop)

    def settings(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def pointed(self) -> Path:
        return self.settings("pointed.json", json.dumps({
            "claudeCode.environmentVariables": {
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.PORT}"}}))

    def signal(self, state, paths, *, running=False, token=False):
        from vco_lib import gateway_ensure as ge
        res = ge.GatewayEnsureResult(state=state, reason="r")
        with mock.patch.object(ge, "gateway_status", return_value=res), \
             mock.patch.object(ge, "is_running", return_value=running), \
             mock.patch.object(ge, "_gateway_has_run_here", return_value=token):
            return ge.machine_gateway_signal(settings_paths=paths)

    def test_registered_and_pointed_is_configured(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        s = self.signal(GatewayState.REGISTERED_NOT_RUNNING, [self.pointed()])
        self.assertIs(s.configured, True)
        self.assertEqual(s.panel, "pointed")

    def test_hand_started_or_previously_run_counts_as_set_up(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        self.assertIs(self.signal(GatewayState.NOT_REGISTERED, [self.pointed()],
                                  running=True).configured, True)
        self.assertIs(self.signal(GatewayState.NOT_REGISTERED, [self.pointed()],
                                  token=True).configured, True)

    def test_never_set_up_is_not_configured_even_if_pointed(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        self.assertIs(
            self.signal(GatewayState.NOT_REGISTERED, [self.pointed()]).configured,
            False)

    def test_set_up_but_panel_elsewhere_is_not_configured(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        other = self.settings("other.json", json.dumps({
            "claudeCode.environmentVariables": {
                "ANTHROPIC_BASE_URL": "https://example.invalid"}}))
        s = self.signal(GatewayState.RUNNING, [other, self.tmp / "missing.json"])
        self.assertIs(s.configured, False)

    def test_unparseable_settings_is_could_not_ask(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        bad = self.settings("bad.json", "{ not json at all")
        self.assertIsNone(self.signal(GatewayState.RUNNING, [bad]).configured)

    def test_unrunnable_registration_is_could_not_ask(self) -> None:
        from vco_lib.gateway_ensure import GatewayState
        s = self.signal(GatewayState.REGISTERED_BUT_UNRUNNABLE, [self.pointed()])
        self.assertIsNone(s.configured)

    def test_status_json_carries_the_machine_signal(self) -> None:
        from vco_lib import gateway_ensure as ge
        sig = ge.MachineGatewaySignal(True, "running", "pointed", "r")
        res = ge.GatewayEnsureResult(state=ge.GatewayState.RUNNING, reason="r")
        out: list[str] = []
        with mock.patch.object(ge, "gateway_status", return_value=res), \
             mock.patch.object(ge, "machine_gateway_signal", return_value=sig), \
             mock.patch("builtins.print", lambda *a, **k: out.append(a[0])):
            ge.main(["status", "--json"])
        self.assertEqual(json.loads(out[0])["machine_signal"]["configured"], True)


# ═══════════════════════════════════════════════════════════════════════════
# The shipped set, and every id it names (contract)
# ═══════════════════════════════════════════════════════════════════════════


class ShippedDefinitionContractTests(unittest.TestCase):
    def test_the_shipped_set_is_exactly_the_owners_ten(self) -> None:
        shipped = {p.name for p in GATED_SRC.iterdir() if p.is_file()}
        self.assertEqual(shipped, OWNER_SHIPPED_SET,
                         "templates/agents/module-gateway/ must hold exactly the "
                         "owner's ten definitions (no flash reviewer, no strays)")
        self.assertEqual(set(MODULE_GATEWAY_AGENT_FILES), OWNER_SHIPPED_SET)

    def test_every_model_id_routes_and_is_known_to_the_registry(self) -> None:
        from model_router.routing import route, validate_model_id
        from model_router.context_table import load_seed
        seed = load_seed()
        for name in sorted(OWNER_SHIPPED_SET):
            model = mgd._frontmatter_model(GATED_SRC / name)
            with self.subTest(definition=name, model=model):
                assert model is not None, name
                self.assertTrue(model.startswith("claude-gw/"))
                ok, reason, _ = validate_model_id(model)
                self.assertTrue(ok, reason)
                decision = route(model)
                self.assertFalse(decision.is_anthropic)  # type: ignore[union-attr]
                # The `[1m]` spelling is the one the router's vendor-keyed
                # table earns — never a window the endpoint does not document.
                row = seed.lookup_id(model)
                self.assertIsNotNone(row)
                self.assertEqual(model.endswith("[1m]"), bool(row.window_1m))  # type: ignore[union-attr]

    def test_the_hand_written_definition_check_names_the_closest_ids(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vct-v02100-ids-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        (tmp / "qwen-coder.md").write_text(
            "---\nname: qwen-coder\nmodel: claude-gw/qwen3.8-max\n---\n", encoding="utf-8")
        (tmp / "deepseek-r.md").write_text(
            "---\nname: d\nmodel: claude-gw/deepseek-4.1-flash[1m]\n---\n", encoding="utf-8")
        (tmp / "ok.md").write_text(
            "---\nname: ok\nmodel: claude-gw/qwen/qwen3.8-max[1m]\n---\n", encoding="utf-8")
        (tmp / "first-party.md").write_text(
            "---\nname: fp\nmodel: opus\n---\n", encoding="utf-8")
        problems = {Path(p["path"]).name: p for p in mgd.check_agent_model_ids([tmp])}
        self.assertEqual(set(problems), {"qwen-coder.md", "deepseek-r.md"})
        self.assertIn("claude-gw/qwen/qwen3.8-max",
                      problems["qwen-coder.md"]["suggestions"])
        self.assertIn("claude-gw/qwen/deepseek-v4.1-flash[1m]",
                      problems["deepseek-r.md"]["suggestions"])

    def test_the_shipped_set_passes_the_hand_written_check(self) -> None:
        self.assertEqual(mgd.check_agent_model_ids([GATED_SRC]), [])


class StatusCliTests(_Env):
    def test_status_json_payload_shape(self) -> None:
        self.db()
        from vco_lib import gateway_ensure as ge
        sig = ge.MachineGatewaySignal(True, "running", "pointed", "set up")
        out: list[str] = []
        with mock.patch.object(ge, "machine_gateway_signal", return_value=sig), \
             mock.patch("builtins.print", lambda *a, **k: out.append(a[0])):
            mgd.main(["status", "--json", "--folder", str(self.proj)])
        payload = json.loads(out[0])
        self.assertEqual(payload["gate"]["state"], "deliver")
        self.assertEqual(payload["machine_signal"]["configured"], True)
        self.assertIsInstance(payload["agent_id_problems"], list)



if __name__ == "__main__":
    unittest.main()
