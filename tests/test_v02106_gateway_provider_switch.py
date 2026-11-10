# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the provider switch takes effect AT ONCE: the gateway leg.

The owner's ask (2026-11-10): a provider switched off in the launcher (Home →
Model providers) must stop being served by the RUNNING gateway, with no
restart and no bundle update:

* it disappears from ``/v1/models`` (and its upstream model list is not even
  fetched);
* a request naming one of its ids is refused LOCALLY — never forwarded — with
  a message naming the cause and the remedy;
* the quota fallback ladder never escalates INTO it (a disabled rung is
  skipped to the next one; a fully disabled ladder relays the original
  refusal);
* ``/vendor-keys`` answers ``usable: false`` with the same reason, so the
  subagent pre-flight refuses the launch with cause and remedy;
* a flip is picked up without a restart (a short TTL cache — never a DB read
  per request on the hot path);
* a setting that cannot be READ disables nothing (the module's soft-fail
  rule: a failed read must never take a provider away);
* first-party Claude routes are never affected.

The setting is read through ``vco_lib.model_providers`` (rung A — no second
implementation of the row's encoding), TTL-cached by
``model_router.provider_switch``. Everything runs against the stub-upstream
harness of ``tests.test_model_router_server``; the ladder legs reuse the
two-stub fixture of ``tests.test_v02102_gateway_routing``.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db, set_app_state  # noqa: E402
from tests.test_model_router_server import GatewayTestBase  # noqa: E402
from tests.test_v02102_gateway_routing import (  # noqa: E402
    HAIKU,
    LadderTestBase,
    _success,
)
from model_router import provider_switch  # noqa: E402

APP_STATE_KEY = "model_providers.disabled"


class ProviderSwitchReaderTests(unittest.TestCase):
    """The TTL-cached reader itself (unit level, injected reader/clock)."""

    def setUp(self) -> None:
        provider_switch.reset()
        self.addCleanup(provider_switch.reset)

    def test_the_off_list_is_read_through_the_injected_reader(self) -> None:
        with mock.patch.object(provider_switch, "_read_live",
                               lambda: frozenset({"kimi"})) as _:
            self.assertEqual(provider_switch.disabled_vendors(), {"kimi"})

    def test_the_read_is_cached_for_the_ttl(self) -> None:
        calls: list[int] = []

        def reader() -> frozenset[str]:
            calls.append(1)
            return frozenset({"kimi"})

        clock = mock.Mock(return_value=1000.0)
        with mock.patch.object(provider_switch, "_read_live", reader):
            provider_switch.disabled_vendors(now=clock, ttl_s=30.0)
            provider_switch.disabled_vendors(now=clock, ttl_s=30.0)
        self.assertEqual(len(calls), 1)

    def test_a_change_is_seen_once_the_ttl_expires(self) -> None:
        state = {"off": frozenset({"kimi"})}
        moments = iter([1000.0, 1000.5, 1003.0])
        clock = mock.Mock(side_effect=lambda: next(moments))
        with mock.patch.object(provider_switch, "_read_live",
                               lambda: state["off"]):
            self.assertEqual(
                provider_switch.disabled_vendors(now=clock, ttl_s=2.0),
                {"kimi"})
            state["off"] = frozenset()
            # Within the TTL the stale answer is served (the hot-path rule).
            self.assertEqual(
                provider_switch.disabled_vendors(now=clock, ttl_s=2.0),
                {"kimi"})
            self.assertEqual(
                provider_switch.disabled_vendors(now=clock, ttl_s=2.0),
                set())

    def test_a_failed_read_disables_nothing_and_is_cached(self) -> None:
        calls: list[int] = []

        def reader() -> frozenset[str]:
            calls.append(1)
            raise OSError("database is locked")

        with mock.patch.object(provider_switch, "_read_live", reader):
            self.assertEqual(provider_switch.disabled_vendors(), set())
            self.assertEqual(provider_switch.disabled_vendors(), set())
        self.assertEqual(len(calls), 1)


class _SwitchBase(GatewayTestBase):
    """The stock stub-gateway fixture plus a real launcher.db the switch
    reads, with the cache TTL pinned tiny so a flip lands within a test."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db_dir = self.root / "db"
        self.db_dir.mkdir()
        self.switch_db = make_launcher_db(self.db_dir / "launcher.db")
        env = mock.patch.dict(os.environ, {
            "VCT_LAUNCHER_DB_PATH": str(self.switch_db),
            provider_switch.ENV_TTL_S: "0.05",
        })
        env.start()
        self.addCleanup(env.stop)
        provider_switch.reset()
        self.addCleanup(provider_switch.reset)

    def switch(self, *vendor_ids: str) -> None:
        set_app_state(self.switch_db, APP_STATE_KEY, json.dumps(sorted(vendor_ids)))

    async def settle_cache(self) -> None:
        provider_switch.reset()  # a test never waits out a clock


class DisabledVendorSurfaceTests(_SwitchBase):
    async def test_a_disabled_vendor_disappears_from_the_picker(self) -> None:
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        self.switch("zai")
        resp = await self.client.get("/v1/models", headers=self.auth())
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        ids = [row["id"] for row in body["data"]]
        self.assertTrue(any(i.startswith("claude-opus-5") for i in ids),
                        f"first-party rows stay: {ids}")
        self.assertFalse([i for i in ids if "glm" in i],
                         f"a switched-off vendor must not be picked: {ids}")
        # Its model list is not even fetched — the switch is a full stop.
        self.assertEqual(
            [r for r in self.vendor_up.requests if r["path"] == "/v1/models"],
            [])
        self.assertEqual(body["_vct_catalog_source"].get("zai"), "provider-off")

    async def test_a_request_naming_a_disabled_vendor_is_refused_locally(self) -> None:
        self.switch("zai")
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 403)
        body = await resp.json()
        message = body["error"]["message"]
        self.assertIn("switched off", message)
        self.assertIn("Model providers", message)  # the remedy: where to undo it
        self.assertIn("zai", message)
        # NEVER forwarded.
        self.assertEqual(self.vendor_up.message_requests, [])

    async def test_a_bare_vendor_id_is_refused_the_same_way(self) -> None:
        self.switch("zai")
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.vendor_up.message_requests, [])

    async def test_first_party_requests_are_never_affected(self) -> None:
        self.switch("zai")
        self.anthropic_up.messages_body = {"id": "msg_1", "model": "claude-opus-5",
                                           "content": []}
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.anthropic_up.message_requests), 1)

    async def test_vendor_keys_reports_disabled_without_probing_the_key(self) -> None:
        def exploding_getter(key: str, project: Any = None) -> str:
            raise AssertionError("a disabled vendor's key must not be probed")

        client = await self.make_client(key_getter=exploding_getter)
        self.switch("zai")
        resp = await client.get("/vendor-keys?model=claude-gw/glm-5.3",
                                headers=self.auth())
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["route"], "vendor")
        self.assertEqual(body["vendor"], "zai")
        self.assertIs(body["usable"], False)
        self.assertEqual(body["state"], "disabled")
        self.assertIn("switched off", body["problem"])
        self.assertIn("Model providers", body["problem"])

    async def test_a_switch_flip_is_picked_up_without_a_restart(self) -> None:
        """The app was built BEFORE the flip: the TTL cache, not a restart,
        carries the change."""
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)

        self.switch("zai")
        await self.settle_cache()
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.vendor_up.message_requests), 1)

        self.switch()  # back on
        await self.settle_cache()
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 2)

    async def test_an_unreadable_setting_disables_nothing(self) -> None:
        """A missing launcher.db is 'nothing disabled' — a read failure must
        never take a provider away."""
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.root / "absent.db")
        await self.settle_cache()
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)


class DisabledVendorLadderTests(LadderTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db_dir = self.root / "db"
        self.db_dir.mkdir()
        self.switch_db = make_launcher_db(self.db_dir / "launcher.db")
        env = mock.patch.dict(os.environ, {
            "VCT_LAUNCHER_DB_PATH": str(self.switch_db),
            provider_switch.ENV_TTL_S: "0.05",
        })
        env.start()
        self.addCleanup(env.stop)
        provider_switch.reset()
        self.addCleanup(provider_switch.reset)

    async def test_the_ladder_skips_a_disabled_vendor_rung(self) -> None:
        """zai off: a qualifying first-party 429 escalates PAST the zai rung
        to the qwen rung — a disabled vendor's quota is never spent."""
        set_app_state(self.switch_db, APP_STATE_KEY, json.dumps(["zai"]))
        provider_switch.reset()
        self.refuse(self.anthropic_up)
        self.qwen_up.messages_body = _success("qwen3.8-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["model"], "qwen3.8-flash")
        self.assertEqual(self.vendor_up.message_requests, [],
                         "the disabled rung must never be called")
        self.assertEqual(len(self.qwen_up.message_requests), 1)
        sent = json.loads(self.qwen_up.message_requests[0]["body"])
        self.assertEqual(sent["model"], "qwen3.8-flash")
        rows = await self.rows()
        self.assertEqual(rows[0]["forward"], "qwen3.8-flash")

    async def test_a_fully_disabled_ladder_relays_the_original_refusal(self) -> None:
        """Every vendor rung off: the client's answer is the ORIGINAL 429 —
        the ladder never fabricates a success."""
        set_app_state(self.switch_db, APP_STATE_KEY, json.dumps(["qwen", "zai"]))
        provider_switch.reset()
        self.refuse(self.anthropic_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(self.vendor_up.message_requests, [])
        self.assertEqual(self.qwen_up.message_requests, [])


class RealOrderTests(GatewayTestBase):
    """The brief's sandbox real-order check, end to end and in order.

    ONE sandbox (a throwaway launcher.db with an ``app_state`` table, two
    throwaway registered projects installed from a fake orchestrator, the
    gateway test app built BEFORE any flip — nothing here touches the real
    ``~/.vct`` state): the REAL ``set`` CLI switches zai off, and the agents,
    the guidance provider set and the RUNNING gateway all follow at once;
    then back on, and everything is restored. The gateway legs wait out the
    real (pinned-tiny) TTL instead of resetting the cache, so the pickup
    mechanism itself is what is proven.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import shutil as _sh

        import tests.test_install_bundle as _tib
        from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES

        self.orch = self.root / "orchestrator"
        self.orch.mkdir()
        _tib._make_fake_orchestrator(self.orch)
        dst = self.orch / "templates" / "agents" / "module-gateway"
        dst.mkdir(parents=True)
        src = REPO_ROOT / "templates" / "agents" / "module-gateway"
        for name in MODULE_GATEWAY_AGENT_FILES:
            _sh.copyfile(src / name, dst / name)
        self.proj_a = self.root / "project-a"
        self.proj_b = self.root / "project-b"
        self.proj_a.mkdir()
        self.proj_b.mkdir()
        (self.root / "db").mkdir()
        self.switch_db = make_launcher_db(
            self.root / "db" / "launcher.db",
            projects=[
                {"project_id": "aa1e2d3c-4b5a-6978-8976-a5b4c3d2e1f0",
                 "name": "Alpha", "folder_path": self.proj_a},
                {"project_id": "bb2e3d4c-5b6a-7988-8976-a5b4c3d2e1f0",
                 "name": "Beta", "folder_path": self.proj_b},
            ],
        )
        env = mock.patch.dict(os.environ, {
            "VCT_LAUNCHER_DB_PATH": str(self.switch_db),
            provider_switch.ENV_TTL_S: "0.05",
        })
        env.start()
        self.addCleanup(env.stop)
        provider_switch.reset()
        self.addCleanup(provider_switch.reset)
        from vco_lib import module_gated_delivery as mgd
        from vco_lib import project_init

        class _Sig:
            configured = True
            reason = "test signal"

        with mock.patch.object(mgd, "_default_machine_signal", lambda: _Sig()):
            for folder in (self.proj_a, self.proj_b):
                project_init.install_project_bundle(
                    folder, orchestrator_root=self.orch, update_mode=False)

    def set_cli(self, vendor: str, enabled: bool) -> dict:
        """The REAL CLI entry point, in-process (argparse and all)."""
        from vco_lib import model_providers as mp
        from vco_lib import module_gated_delivery as mgd

        class _Sig:
            configured = True
            reason = "test signal"

        out = io.StringIO()
        # The restore leg consults the per-project module gate, whose default
        # reads the host's VS Code settings — inject "configured" so the test
        # is hermetic (the same seam the delivery tests use).
        with mock.patch.object(mgd, "_default_machine_signal", lambda: _Sig()):
            with redirect_stdout(out):
                code = mp.main([
                    "set", "--vendor", vendor,
                    "--enabled" if enabled else "--disabled", "--json",
                    "--orchestrator-root", str(self.orch),
                ])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def parked(self, folder: Path) -> set[str]:
        dis = folder / ".claude" / "agents.disabled"
        return {p.name for p in dis.glob("glm-*.md")} if dis.is_dir() else set()

    def enabled(self, folder: Path) -> set[str]:
        return {p.name for p in (folder / ".claude" / "agents").glob("glm-*.md")}

    async def test_off_then_on_takes_effect_everywhere_at_once(self) -> None:
        from vco_lib import model_selection

        glm_defs = {p.name for p in (self.orch / "templates" / "agents"
                                     / "module-gateway").glob("glm-*.md")}
        # Baseline: served, delivered, rendered.
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        self.assertEqual(self.enabled(self.proj_a), glm_defs)
        self.assertIn("zai", model_selection.reachable_providers(
            key_probe=lambda _v: True, state_path=self.root / "mg.json"))

        # ── OFF, through the real CLI ─────────────────────────────────────
        payload = self.set_cli("zai", False)
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["agents"]["parked_projects"], 2)
        for folder in (self.proj_a, self.proj_b):
            self.assertEqual(self.enabled(folder), set())
            self.assertEqual(self.parked(folder), glm_defs)
        # The guidance leg reads the same row at once.
        self.assertNotIn("zai", model_selection.reachable_providers(
            key_probe=lambda _v: True, state_path=self.root / "mg.json"))
        # The RUNNING gateway (built before the flip) follows within the TTL.
        await asyncio.sleep(0.1)
        resp = await self.client.get("/v1/models", headers=self.auth())
        ids = [row["id"] for row in (await resp.json())["data"]]
        self.assertFalse([i for i in ids if "glm" in i], ids)
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8})
        self.assertEqual(resp.status, 403)
        self.assertIn("switched off", (await resp.json())["error"]["message"])
        self.assertEqual(len(self.vendor_up.message_requests), 1,
                         "a switched-off vendor is never forwarded to")

        # ── ON again ──────────────────────────────────────────────────────
        payload = self.set_cli("zai", True)
        self.assertTrue(payload["stored"])
        self.assertFalse(payload["agents"]["held"])
        self.assertEqual(payload["agents"]["restored_projects"], 2)
        for folder in (self.proj_a, self.proj_b):
            self.assertEqual(self.enabled(folder), glm_defs)
            self.assertEqual(self.parked(folder), set())
        await asyncio.sleep(0.1)
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
