# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the model gateway serves more than one Claude account.

A project selects an account with ``x-vco-account: <id>`` (written into its
``.claude/settings.local.json`` ``env.ANTHROPIC_CUSTOM_HEADERS``). An account
that cannot be used never fails the chat (owner ruling 2026-11-03): the
request is served by the fallback account, loudly — a WARNING, the access line
and ledger row, ``/health``, and ONE notice to the model per conversation,
appended at the very end of the prompt so the cached prefix is untouched.

Every decision branch is tested both ways. Fake tokens only; every artefact is
scanned for them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from model_router.server import APP_KEY
from tests.test_v0294_gateway_robustness import ChaosBase
from tests.test_model_router_server import (
    FAKE_OAUTH,
    FAKE_VENDOR_KEY,
    GatewayTestBase,
)

WORK_TOKEN = "acct-work-oauth-token-synthetic"
WORK2_TOKEN = "acct-work-oauth-token-rotated-synthetic"
TEAM_TOKEN = "acct-team-oauth-token-synthetic"
ALL_TOKENS = (FAKE_OAUTH, WORK_TOKEN, WORK2_TOKEN, TEAM_TOKEN, FAKE_VENDOR_KEY)
FIRST_PARTY = "claude-opus-5-5"
TOOLS = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]


def write_login(config_dir: Path, token: str | None, *, expires_in_ms: int = 3_600_000) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    section: dict[str, Any] = {"expiresAt": int(time.time() * 1000) + expires_in_ms}
    if token is not None:
        section["accessToken"] = token
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": section}), encoding="utf-8",
    )


def conversation(first: str, *more: str, tools: bool = True, string_last: bool = False) -> dict:
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": first}]}]
    for i, text in enumerate(more):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"ok {i}"}]})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ]})
    if string_last:
        messages[-1] = {"role": "user", "content": messages[-1]["content"][0]["text"]}
    body: dict[str, Any] = {
        "model": FIRST_PARTY,
        "max_tokens": 16,
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": messages,
    }
    if tools:
        body["tools"] = TOOLS
    return body


class AccountsTestBase(GatewayTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.accounts_file = self.root / "vct" / "model-gateway" / "accounts.json"
        self.work_dir = self.root / "cfg-work"
        self.team_dir = self.root / "cfg-team"
        write_login(self.work_dir, WORK_TOKEN)
        write_login(self.team_dir, TEAM_TOKEN)
        # A usage-less 2xx writes no ledger row by design; these answers
        # carry usage so the row each request owes is observable.
        usage_body = {"id": "msg_1", "type": "message", "model": "stub", "content": [],
                      "usage": {"input_tokens": 10, "output_tokens": 2}}
        self.anthropic_up.messages_body = usage_body
        self.vendor_up.messages_body = {**usage_body, "model": "glm-5.3"}

    def write_registry(self, *, default: str = "primary", accounts: list[dict] | None = None) -> None:
        self.accounts_file.parent.mkdir(parents=True, exist_ok=True)
        if accounts is None:
            accounts = [
                {"id": "work", "label": "Work seat", "config_dir": str(self.work_dir)},
                {"id": "team", "label": "Team seat", "config_dir": str(self.team_dir)},
            ]
        doc = {"schema": 1, "default": default, "accounts": accounts}
        self.accounts_file.write_text(json.dumps(doc), encoding="utf-8")
        # A rewrite within the same mtime tick must still be seen.
        now = time.time() + len(self.accounts_file.read_text())
        import os
        os.utime(self.accounts_file, (now, now))

    def headers(self, account: str | None = None, session: str = "sess-1") -> dict[str, str]:
        h = {**self.auth(), "x-claude-code-session-id": session}
        if account is not None:
            h["x-vco-account"] = account
        return h

    async def send(self, body: dict, account: str | None = None, session: str = "sess-1",
                   path: str = "/v1/messages") -> int:
        resp = await self.client.post(path, json=body, headers=self.headers(account, session))
        await resp.read()
        return resp.status

    def last_upstream(self) -> dict:
        return self.anthropic_up.message_requests[-1]

    def last_bearer(self) -> str:
        return self.last_upstream()["headers"].get("Authorization", "")

    def last_body(self) -> dict:
        return json.loads(self.last_upstream()["body"])

    async def ledger_rows(self) -> list[dict]:
        await self.client.server.app[APP_KEY].quiesce()
        if not self.usage_ledger_path.exists():
            return []
        return [json.loads(line) for line in self.usage_ledger_path.read_text().splitlines() if line]

    @property
    def gateway(self):
        return self.client.server.app[APP_KEY]


class SelectionTests(AccountsTestBase):
    async def test_no_registry_is_todays_behaviour(self) -> None:
        self.assertEqual(await self.send(conversation("hi")), 200)
        self.assertEqual(self.last_bearer(), f"Bearer {FAKE_OAUTH}")
        rows = await self.ledger_rows()
        self.assertEqual(rows[-1]["account"], "primary")
        self.assertEqual(rows[-1]["account_source"], "default")
        self.assertIsNone(rows[-1]["account_fallback"])

    async def test_header_selects_its_own_login(self) -> None:
        self.write_registry()
        self.assertEqual(await self.send(conversation("hi"), account="work"), 200)
        self.assertEqual(self.last_bearer(), f"Bearer {WORK_TOKEN}")
        # Leave-alone arm: another header, another login; no header, primary.
        await self.send(conversation("hi"), account="team")
        self.assertEqual(self.last_bearer(), f"Bearer {TEAM_TOKEN}")
        await self.send(conversation("hi"))
        self.assertEqual(self.last_bearer(), f"Bearer {FAKE_OAUTH}")
        rows = await self.ledger_rows()
        self.assertEqual([r["account"] for r in rows], ["work", "team", "primary"])
        self.assertEqual([r["account_source"] for r in rows], ["header", "header", "default"])

    async def test_the_selector_never_reaches_an_upstream(self) -> None:
        self.write_registry()
        await self.send(conversation("hi"), account="work")
        names = {k.lower() for k in self.last_upstream()["headers"]}
        self.assertNotIn("x-vco-account", names)
        body = {"model": "glm-5.3", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
        await self.send(body, account="work")
        vendor_names = {k.lower() for k in self.vendor_up.message_requests[-1]["headers"]}
        self.assertNotIn("x-vco-account", vendor_names)
        rows = await self.ledger_rows()
        self.assertIsNone(rows[-1]["account"], "a vendor route is served by no Claude account")

    async def test_the_default_account_follows_the_registry_without_restart(self) -> None:
        self.write_registry(default="work")
        await self.send(conversation("hi"))
        self.assertEqual(self.last_bearer(), f"Bearer {WORK_TOKEN}")
        self.write_registry(default="primary")
        await self.send(conversation("hi"))
        self.assertEqual(self.last_bearer(), f"Bearer {FAKE_OAUTH}")


class FallbackTests(AccountsTestBase):
    async def _fallback(self, account: str, reason: str, served_token: str = FAKE_OAUTH) -> None:
        with self.assertLogs("model_router", level="INFO") as logs:
            status = await self.send(conversation("hi"), account=account)
        self.assertEqual(status, 200, "a fallback never fails the chat")
        self.assertEqual(self.last_bearer(), f"Bearer {served_token}")
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertTrue(any("unusable" in r.getMessage() and reason in r.getMessage()
                            for r in warnings), [r.getMessage() for r in warnings])
        access = [r.getMessage() for r in logs.records if "requested=" in r.getMessage()]
        self.assertIn(f"account_fallback={reason}", access[-1])
        rows = await self.ledger_rows()
        self.assertEqual(rows[-1]["account_fallback"], reason)

    async def test_unknown_id_falls_back(self) -> None:
        self.write_registry()
        await self._fallback("nobody", "not_registered")

    async def test_logged_out_account_falls_back(self) -> None:
        self.write_registry()
        (self.work_dir / ".credentials.json").unlink()
        await self._fallback("work", "absent")

    async def test_expired_account_falls_back(self) -> None:
        self.write_registry()
        write_login(self.work_dir, WORK_TOKEN, expires_in_ms=-60_000)
        await self._fallback("work", "expired")

    async def test_malformed_header_falls_back(self) -> None:
        self.write_registry()
        await self._fallback("Not An Id!", "malformed_header")

    async def test_no_registry_file_and_a_header_falls_back(self) -> None:
        await self._fallback("work", "not_registered")

    async def test_fallback_prefers_a_usable_default(self) -> None:
        self.write_registry(default="team")
        (self.work_dir / ".credentials.json").unlink()
        await self._fallback("work", "absent", served_token=TEAM_TOKEN)

    async def test_fallback_skips_an_unusable_default(self) -> None:
        self.write_registry(default="team")
        (self.work_dir / ".credentials.json").unlink()
        (self.team_dir / ".credentials.json").unlink()
        await self._fallback("work", "absent")

    async def test_primary_unusable_is_still_todays_401(self) -> None:
        self.write_credentials(None, expires_in_ms=-1)
        self.credentials.unlink()
        status = await self.send(conversation("hi"))
        self.assertEqual(status, 401)
        self.assertEqual(self.anthropic_up.message_requests, [])

    async def test_a_usable_account_does_not_fall_back(self) -> None:
        self.write_registry()
        with self.assertLogs("model_router", level="INFO") as logs:
            await self.send(conversation("hi"), account="work")
        self.assertFalse([r for r in logs.records if r.levelname == "WARNING"])
        self.assertNotIn("account_fallback", logs.output[-1])

class NoticeTests(AccountsTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.write_registry()
        (self.work_dir / ".credentials.json").unlink()

    def notice_blocks(self, body: dict) -> list[str]:
        last = body["messages"][-1]["content"]
        blocks = last if isinstance(last, list) else [{"type": "text", "text": last}]
        return [b.get("text", "") for b in blocks if "VCO model gateway notice" in b.get("text", "")]

    async def test_the_notice_is_appended_last_and_the_prefix_is_unchanged(self) -> None:
        sent = conversation("first", "second")
        await self.send(sent, account="work")
        got = self.last_body()
        notices = self.notice_blocks(got)
        self.assertEqual(len(notices), 1)
        self.assertIn("billed to", notices[0])
        self.assertIn("claude auth login", notices[0])
        # Prefix: everything the cache keys on, byte for byte.
        for key in ("tools", "system", "model", "max_tokens"):
            self.assertEqual(json.dumps(got[key]), json.dumps(sent[key]))
        self.assertEqual(json.dumps(got["messages"][:-1]), json.dumps(sent["messages"][:-1]))
        sent_last = sent["messages"][-1]["content"]
        got_last = got["messages"][-1]["content"]
        self.assertEqual(json.dumps(got_last[: len(sent_last)]), json.dumps(sent_last))
        self.assertEqual(len(got_last), len(sent_last) + 1, "exactly one block, appended last")
        self.assertIn("VCO model gateway notice", got_last[-1]["text"])

    async def test_told_once_per_conversation(self) -> None:
        await self.send(conversation("first", "second"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)
        # Same conversation (same first message), one turn later: no notice.
        await self.send(conversation("first", "second", "third"), account="work")
        self.assertEqual(self.notice_blocks(self.last_body()), [])
        # A new conversation in the same session (a subagent): told once.
        await self.send(conversation("subagent task"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)
        # Another session with the same first text: its own conversation.
        await self.send(conversation("first", "second"), account="work", session="sess-2")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)

    async def test_a_turn_the_upstream_refused_does_not_use_up_the_notice(self) -> None:
        """A5: the notice is consumed only by a turn the model ANSWERED. A
        refused turn (here a 400) never reached it, so the client's retry of
        the same turn carries the notice again; once a turn succeeds, the
        conversation is told and later turns carry nothing."""
        self.anthropic_up.messages_status = 400
        self.anthropic_up.messages_body = {"type": "error", "error": {
            "type": "invalid_request_error", "message": "nope"}}
        status = await self.send(conversation("first", "second"), account="work")
        self.assertEqual(status, 400)
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)
        self.anthropic_up.messages_status = 200
        self.anthropic_up.messages_body = {"id": "msg_1", "type": "message", "model": "stub",
                                           "content": [], "usage": {"input_tokens": 1,
                                                                    "output_tokens": 1}}
        await self.send(conversation("first", "second"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1, "the retry is told")
        await self.send(conversation("first", "second", "third"), account="work")
        self.assertEqual(self.notice_blocks(self.last_body()), [], "told once it was answered")

    async def test_ineligible_requests_leave_the_notice_pending(self) -> None:
        await self.send(conversation("first", tools=False), account="work")
        self.assertEqual(self.notice_blocks(self.last_body()), [], "no tools: a side call")
        await self.send(conversation("first"), account="work", path="/v1/messages/count_tokens")
        self.assertEqual(self.notice_blocks(self.last_body()), [], "count_tokens is not a turn")
        await self.send(conversation("first"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1, "the next real turn is told")

    async def test_string_content_keeps_its_text_first(self) -> None:
        sent = conversation("first", "second", string_last=True)
        await self.send(sent, account="work")
        last = self.last_body()["messages"][-1]["content"]
        self.assertEqual(last[0], {"type": "text", "text": "second"})
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)

    async def test_no_fallback_no_notice(self) -> None:
        write_login(self.work_dir, WORK_TOKEN)
        await self.send(conversation("first"), account="work")
        self.assertEqual(self.notice_blocks(self.last_body()), [])
        self.assertEqual(self.last_body(), conversation("first"))

    async def test_a_new_fallback_episode_tells_again(self) -> None:
        await self.send(conversation("first"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)
        write_login(self.work_dir, WORK_TOKEN)   # recovered
        await self.send(conversation("first", "x"), account="work")
        self.assertEqual(self.notice_blocks(self.last_body()), [])
        (self.work_dir / ".credentials.json").unlink()   # lapsed again
        await self.send(conversation("first", "x", "y"), account="work")
        self.assertEqual(len(self.notice_blocks(self.last_body())), 1)


class HealthAndSecrecyTests(AccountsTestBase):
    async def test_health_reports_every_account_without_tokens(self) -> None:
        self.write_registry()
        (self.work_dir / ".credentials.json").unlink()
        await self.send(conversation("hi"), account="work")
        resp = await self.client.get("/health", headers=self.auth())
        raw = await resp.text()
        block = json.loads(raw)["accounts"]
        self.assertEqual(block["registry"]["source"], "file")
        rows = {r["id"]: r for r in block["accounts"]}
        self.assertEqual(set(rows), {"primary", "work", "team"})
        self.assertFalse(rows["work"]["usable"])
        self.assertEqual(rows["work"]["oauth_state"], "absent")
        self.assertEqual(rows["work"]["fallbacks"], 1)
        self.assertEqual(rows["work"]["last_fallback_reason"], "absent")
        self.assertTrue(rows["team"]["usable"])
        self.assertTrue(rows["primary"]["is_default"])
        rows_text = json.dumps(await self.ledger_rows())
        for token in ALL_TOKENS:
            self.assertNotIn(token, raw)
            self.assertNotIn(token, rows_text)

    async def test_health_without_registry(self) -> None:
        body = await (await self.client.get("/health", headers=self.auth())).json()
        self.assertEqual(body["accounts"]["registry"]["source"], "absent")
        self.assertEqual([r["id"] for r in body["accounts"]["accounts"]], ["primary"])

    async def test_a_corrupt_rewrite_keeps_the_last_good_registry(self) -> None:
        self.write_registry()
        await self.send(conversation("hi"), account="work")
        self.accounts_file.write_text("{not json", encoding="utf-8")
        import os
        later = time.time() + 50
        os.utime(self.accounts_file, (later, later))
        await self.send(conversation("hi"), account="work")
        self.assertEqual(self.last_bearer(), f"Bearer {WORK_TOKEN}")
        body = await (await self.client.get("/health", headers=self.auth())).json()
        self.assertIsNotNone(body["accounts"]["registry"]["error"])


class RereadTests(ChaosBase):
    """An upstream 401 on one account re-reads THAT account's file only."""

    async def test_upstream_401_rereads_the_same_account_only(self) -> None:
        work_dir = self.root / "cfg-work"
        write_login(work_dir, WORK_TOKEN)
        accounts_file = self.root / "vct" / "model-gateway" / "accounts.json"
        accounts_file.parent.mkdir(parents=True, exist_ok=True)
        accounts_file.write_text(json.dumps({"schema": 1, "default": "primary", "accounts": [
            {"id": "work", "label": "Work", "config_dir": str(work_dir)}]}), encoding="utf-8")
        self.anthropic_up.accept_bearer = WORK2_TOKEN
        self.anthropic_up.on_request = lambda: write_login(work_dir, WORK2_TOKEN)
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(**{"x-vco-account": "work"}),
            json={"model": FIRST_PARTY, "messages": []},
        )
        self.assertEqual(resp.status, 200, await resp.text())
        bearers = [c["headers"]["Authorization"] for c in self.anthropic_up.message_requests]
        self.assertEqual(bearers, [f"Bearer {WORK_TOKEN}", f"Bearer {WORK2_TOKEN}"])
