# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W4-B — gateway catalogue + attribution: G1, G2, G3, X1, X2.

* **G1 (merged with P299-A6b) — the "duplicate picker row" is CLIENT-NATIVE.**
  Evidence (shipped client 2.1.285, read from the binary): the two rows the
  owner saw at every new Claude release are the client's OWN built-ins — the
  slot row (``jP()``/``$6()``: ``{value:"sonnet", label:"Sonnet",
  description:"Sonnet 5.5 · Efficient for routine tasks"}``) and its own 1M
  variant (``yb()``: ``{value:"sonnet[1m]", label:"Sonnet 5.5 (1M context)",
  description:"Sonnet 5.5 for long sessions"}``). The gateway cannot produce
  the second row's text (its description shape is
  ``"Anthropic · 1M context · …"``), and the client merges gateway rows
  through a dedupe predicate (``Fo`` in the binary: exact id, fable family,
  or same base id + same kind). What the GATEWAY owes, and what is pinned
  here: exactly ONE published row per first-party model (the ``[1m]``
  companion under the default ``one_m_only`` mode), and genuinely different
  models staying distinct rows.
* **G2 — cache fields for vendor routes.** Claude Code's "Prompt cache warm,
  about N min left" reads ``usage.cache_read_input_tokens`` /
  ``cache_creation_input_tokens`` (webview 2.1.292: ``kv1``/``dg``/``Kf0`` —
  the ``message_delta`` merge is the last writer for cache fields). Vendor
  streams carry the figures only in the final ``message_delta`` and not
  always under the Anthropic names (qwen's ``prompt_tokens_details``).
  The gateway completes the two cache fields from what the vendor returned —
  mapping the vendor's own cache dialect when present, ZEROS when absent,
  never a positive the vendor did not state — on ``message_start``,
  ``message_delta`` and the buffered non-stream body. No
  ``cache_creation.ephemeral_*`` TTL block is EVER planted: no seed citation
  documents a vendor cache TTL, so no TTL is claimed.
* **G3 — per-agent model attribution.** The ledger already keys rows per
  (session, agent) and W3-B proved a fallback-answered row names the
  answering model (``tests/test_v02102_gateway_routing.py``). This half
  surfaces it: ``UsageLedger.agent_rows`` + the ``agents`` block of
  ``GET /usage/windows``, so the launcher's usage card can show which model
  answered for each agent even while the harness's agents GUI row shows the
  session model for every lane (anthropics/claude-code#93324).
* **X1 — ``window_client`` for first-party 1M-native models.** Claude Code
  sends neither the ``[1m]`` suffix nor the ``context-1m`` beta for
  first-party 1M-native models (``claude-opus-5-5``), yet budgets 1M — a
  session reached 791K while every ledger row recorded
  ``window_client: 200000``. With no explicit signal, the client window for
  a FIRST-PARTY route is derived from the context table: a row saying 1M
  (flag or window) widens it; a row saying 200K, or no row, keeps the 200K
  default.
* **X2 — the Kimi usage-window refresh reuses the last relayed client
  user-agent.** Its quota endpoint sits behind the same Cloudflare
  user-agent gate as its messages endpoint (403 "error code: 1010" to a
  library UA). The relay itself shipped earlier (``_client_user_agent``,
  a short recency list since v0.2.104);
  what this lane adds is the missing arm: with NO client user-agent relayed
  yet, the fetch is SKIPPED and the vendor reads unknown — a UA is relayed,
  never invented (vendor ToS), so a 403-ing UA-less fetch is never sent.

No network: upstreams are the loopback stubs from
:mod:`tests.test_model_router_server`.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional

from model_router.catalog import (
    WINDOW_ROWS_ONE_M_ONLY,
    CatalogService,
)
from model_router.context_table import ModelContext, load_seed
from model_router.tool_ids import SseIdRewriter
from model_router.usage import (
    CLIENT_WINDOW_1M,
    CLIENT_WINDOW_DEFAULT,
    UsageLedger,
    UsageRecord,
    build_record,
)
from model_router.usage_windows import UsageWindows
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS
from tests.test_model_router_server import GatewayTestBase
from tests.test_v02102_gateway_routing import _success


def _seed_row(model_id: str) -> Optional[ModelContext]:
    return load_seed().lookup(model_id)


# ══════════════════════════════════════════════════════════════════════════
# G1 — one published row per first-party model (the gateway's half)
# ══════════════════════════════════════════════════════════════════════════
class OneRowPerModelTests(unittest.TestCase):
    """The picker union never lists one first-party model twice.

    The client-side duplicate the owner observed is client-native (see the
    module docstring); the gateway's half of the contract is that its own
    list carries each model exactly once, so a client whose dedupe is
    exact-id still never sees two GATEWAY rows for one model.
    """

    def _union(self, anthropic_ids: list[str]) -> Any:
        async def run() -> Any:
            async def fetch_json(url: str, headers: Mapping[str, str]) -> Optional[dict]:
                return {
                    "data": [
                        {"id": model_id, "display_name": model_id.split("-", 1)[-1]}
                        for model_id in anthropic_ids
                    ],
                }

            async def no_key(_vendor: Any) -> Optional[str]:
                return None

            service = CatalogService(
                vendors={},
                anthropic=ANTHROPIC_FAMILY,
                fetch_json=fetch_json,
                oauth_token=lambda: "token",
                vendor_key=no_key,
                live_ttl_s=3600,
                static_ttl_s=60,
            )
            return await service.union(
                table=load_seed(), window_rows=WINDOW_ROWS_ONE_M_ONLY,
            )

        return asyncio.run(run())

    def test_a_1m_first_party_model_is_exactly_one_published_row(self) -> None:
        """The fixture that COULD yield two rows (a model that exists with
        and without ``[1m]``) yields one: the suffixed companion, the plain
        id reported hidden."""
        catalog = self._union(["claude-sonnet-5-5"])
        ids = [entry.id for entry in catalog.entries]
        self.assertEqual(ids, ["claude-sonnet-5-5[1m]"])
        self.assertIn("claude-sonnet-5-5", catalog.hidden)
        # And no id appears twice, however the modes combine.
        self.assertEqual(len(ids), len(set(ids)))

    def test_two_genuinely_different_models_stay_two_rows(self) -> None:
        """The leave-alone: dedupe is per MODEL, never a merge of two models."""
        catalog = self._union(["claude-sonnet-5-5", "claude-opus-5-5"])
        ids = sorted(entry.id for entry in catalog.entries)
        self.assertEqual(ids, ["claude-opus-5-5[1m]", "claude-sonnet-5-5[1m]"])

    def test_a_200k_first_party_model_keeps_its_plain_row(self) -> None:
        """A model the table does NOT flag 1M publishes its plain id — the
        suffix is earned, never sprayed."""
        catalog = self._union(["claude-haiku-4-5-20251001"])
        ids = [entry.id for entry in catalog.entries]
        self.assertEqual(ids, ["claude-haiku-4-5-20251001"])

    def test_the_current_haiku_is_the_one_haiku_row(self) -> None:
        """Haiku 5.5 (1M per the models overview) publishes only its
        ``[1m]`` row, and as the family's latest it is the picker's ONE
        haiku: the legacy Haiku 4.5 beside it is withheld as an older
        sibling (hidden, still routable by name) — never re-spelled with a
        ``[1m]`` it does not have."""
        catalog = self._union(["claude-haiku-5-5", "claude-haiku-4-5-20251001"])
        ids = sorted(entry.id for entry in catalog.entries)
        self.assertEqual(ids, ["claude-haiku-5-5[1m]"])
        self.assertIn("claude-haiku-5-5", catalog.hidden)
        self.assertIn("claude-haiku-4-5-20251001", catalog.hidden)
        self.assertNotIn("claude-haiku-4-5-20251001[1m]", catalog.hidden)


# ══════════════════════════════════════════════════════════════════════════
# X1 — window_client for first-party ids derives from the context table
# ══════════════════════════════════════════════════════════════════════════
class ClientWindowDerivationTests(GatewayTestBase):
    """The ledger's ``window_client`` is the window the CLIENT budgeted.

    For first-party 1M-native models the client sends no signal (no suffix,
    no beta) yet budgets 1M, so the gateway derives the budget from the
    context table — and only for the first-party route, where the client's
    native knowledge applies.
    """

    async def _post_and_row(self, model: str, **payload: Any) -> dict:
        self.anthropic_up.messages_body = _success(model)
        resp = await self.client.post(
            "/v1/messages",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                **payload,
            },
            headers={
                **self.auth(),
                "x-claude-code-session-id": "chat-x1",
            },
        )
        self.assertEqual(resp.status, 200)
        from model_router import server as srv

        await self.client.app[srv.APP_KEY].quiesce()  # handlers finished + appends drained
        lines = [
            json.loads(line)
            for line in self.usage_ledger_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(lines), 1)
        return lines[0]

    async def test_first_party_1m_native_without_any_signal_records_1m(self) -> None:
        """The X1 case: no ``[1m]``, no ``context-1m`` beta — the seed's
        ``claude-opus-5-5`` row (1M, window_1m) answers for the client's
        budget."""
        row = await self._post_and_row("claude-opus-5-5")
        self.assertEqual(row["window_client"], CLIENT_WINDOW_1M)
        self.assertEqual(row["requested"], "claude-opus-5-5")

    async def test_haiku_5_5_without_any_signal_records_1m(self) -> None:
        """Haiku 5.5 is 1M-native like the other 5.5 models, so the client
        budgets 1M with no suffix and no beta; the seed's
        ``claude-haiku-5-5`` row answers for that budget."""
        row = await self._post_and_row("claude-haiku-5-5")
        self.assertEqual(row["window_client"], CLIENT_WINDOW_1M)

    async def test_the_legacy_haiku_keeps_the_200k_client_window(self) -> None:
        """Haiku 4.5 (200K, legacy) has no row, so nothing widens it."""
        row = await self._post_and_row("claude-haiku-4-5-20251001")
        self.assertEqual(row["window_client"], CLIENT_WINDOW_DEFAULT)

    async def test_a_first_party_id_the_table_does_not_name_keeps_200k(self) -> None:
        """No table row → the client's documented default for an id it does
        not recognise: 200K. Nothing is widened on a guess."""
        row = await self._post_and_row("claude-opus-4-8")
        self.assertEqual(row["window_client"], CLIENT_WINDOW_DEFAULT)

    async def test_a_table_row_saying_200k_keeps_the_200k_client_window(self) -> None:
        """The other arm: an EXPORT row for a first-party id that says 200K
        must not be widened — the table is believed whole."""
        export = self.root / "chat_model_context.json"
        export.write_text(
            json.dumps({
                "schema_version": 1,
                "generated_at": "2026-10-08T00:00:00Z",
                "source": "launcher.db",
                "models": {
                    "claude-opus-9-9": {
                        "vendor": "anthropic",
                        "context_window": 200000,
                        "max_output": 64000,
                        "window_1m": False,
                        "source": "https://docs.example/claude-opus-9-9",
                    },
                },
            }),
            encoding="utf-8",
        )
        row = await self._post_and_row("claude-opus-9-9")
        self.assertEqual(row["window_client"], CLIENT_WINDOW_DEFAULT)

    async def test_a_vendor_route_is_never_widened_by_the_table(self) -> None:
        """Only FIRST-PARTY routes derive: behind the gateway a vendor id is
        budgeted 200K by the client unless it carries the suffix, whatever
        the table says the model's real window is."""
        self.vendor_up.messages_body = _success("glm-5.1")
        resp = await self.client.post(
            "/v1/messages",
            json={
                "model": "claude-gw/glm-5.1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            headers={
                **self.auth(),
                "x-claude-code-session-id": "chat-x1",
            },
        )
        self.assertEqual(resp.status, 200)
        from model_router import server as srv

        await self.client.app[srv.APP_KEY].quiesce()  # handlers finished + appends drained
        lines = [
            json.loads(line)
            for line in self.usage_ledger_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["window_client"], CLIENT_WINDOW_DEFAULT)

    def test_the_suffix_and_beta_signals_are_untouched(self) -> None:
        """Explicit signals still answer first: the suffix half of
        ``client_window`` and the threaded ``one_m_client`` OR over the
        table derivation, exactly as before."""
        record = build_record(
            session=None, agent=None, parent_agent=None,
            requested="claude-opus-5-5[1m]", route="anthropic",
            forward="claude-opus-5-5", stream=False, status=200,
            totals={"input_tokens": 5, "output_tokens": 1},
            usage_complete=True, window_actual=CLIENT_WINDOW_1M,
            window_source="table",
        )
        self.assertEqual(record.window_client, CLIENT_WINDOW_1M)
        record = build_record(
            session=None, agent=None, parent_agent=None,
            requested="claude-opus-4-8", route="anthropic",
            forward="claude-opus-4-8", stream=False, status=200,
            totals={"input_tokens": 5, "output_tokens": 1},
            usage_complete=True, window_actual=None,
            window_source="unknown", one_m_client=True,
        )
        self.assertEqual(record.window_client, CLIENT_WINDOW_1M)


# ══════════════════════════════════════════════════════════════════════════
# X2 — the Kimi usage refresh never goes out without a relayed client UA
# ══════════════════════════════════════════════════════════════════════════
class _RecordingFetch:
    def __init__(self, answer: "tuple[Optional[int], Any]" = (200, {})) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer

    async def __call__(self, url: str, headers: Mapping[str, str]):
        self.calls.append((url, dict(headers)))
        return self.answer


def _kimi_vendor() -> Any:
    kimi = VENDORS["kimi"]
    data = {**kimi.__dict__, "quota_url": "https://kimi.example/v1/usages"}
    return type(kimi)(**data)


def _make_windows(fetch: _RecordingFetch) -> UsageWindows:
    async def vendor_key(_vendor: Any) -> Optional[str]:
        return "synthetic-key"

    return UsageWindows(
        anthropic_upstream="https://anthropic.example/",
        vendors={"kimi": _kimi_vendor()},
        oauth_token=lambda: None,  # the first-party leg stays out of this test
        vendor_key=vendor_key,
        fetch=fetch,
        ledger_path=lambda: None,
        rng=lambda: 0.5,
    )


class KimiUserAgentGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_relayed_client_user_agent_skips_the_fetch(self) -> None:
        """X2: with NO client user-agent relayed yet, the refresh does not
        send the UA-less fetch the edge 403s — the vendor reads unknown with
        the reason beside it."""
        fetch = _RecordingFetch()
        service = _make_windows(fetch)
        await service.refresh()
        quota_calls = [url for url, _h in fetch.calls if "usages" in url]
        self.assertEqual(quota_calls, [], "the UA-less fetch must never be sent")
        vendor = {v["id"]: v for v in service.snapshot()["vendors"]}["kimi"]
        self.assertEqual(vendor["state"], "unknown")
        self.assertIn("user-agent", (vendor["problem"] or "").lower())

    async def test_the_last_relayed_client_user_agent_is_reused(self) -> None:
        """The arm that shipped earlier stays: once a real client's UA has
        been relayed, the refresh asks as that client — never an invented one."""
        fetch = _RecordingFetch()
        service = _make_windows(fetch)
        service.note_activity("claude-cli/2.1.292 (external, cli)")
        # note_activity SCHEDULED a refresh; cancel it so the explicit one
        # below is the only fetch this test counts.
        await service.stop()
        await service.refresh()
        quota = [headers for url, headers in fetch.calls if "usages" in url]
        self.assertEqual(len(quota), 1)
        self.assertEqual(quota[0].get("User-Agent"), "claude-cli/2.1.292 (external, cli)")


# ══════════════════════════════════════════════════════════════════════════
# G2 — cache fields completed on vendor streams and bodies
# ══════════════════════════════════════════════════════════════════════════
def _sse(events: list[dict]) -> bytes:
    return b"".join(
        b"event: " + str(payload.get("type", "message")).encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
        for payload in events
    )


def _emitted(raw: bytes, *, floor: Optional[int] = None) -> list[dict]:
    rewriter = SseIdRewriter(message_start_usage=floor)
    out = rewriter.feed(raw) + rewriter.flush()
    payloads = []
    for block in out.split(b"\n\n"):
        for line in block.splitlines():
            if line.startswith(b"data:"):
                payloads.append(json.loads(line[len(b"data:"):].strip()))
    return payloads


def _message_start(usage: dict) -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "glm-5.3", "content": [], "usage": usage,
        },
    }


def _message_delta(usage: dict) -> dict:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": usage,
    }


class CacheFieldCompletionTests(unittest.TestCase):
    """G2: the two cache fields reach the client on every vendor stream —
    the vendor's own figures when it states them (either dialect), zeros
    when it says nothing, never a positive it did not report."""

    def test_message_delta_gains_the_missing_cache_fields(self) -> None:
        """The z.ai shape: ``message_delta`` reports ``cache_read`` and
        nothing about creation — the client merge (``Kf0``) makes the delta
        the last writer, so the missing field is completed HERE."""
        payloads = _emitted(_sse([
            _message_start({"input_tokens": 0, "output_tokens": 0}),
            _message_delta({
                "input_tokens": 18, "output_tokens": 21,
                "cache_read_input_tokens": 32256,
            }),
        ]))
        delta = [p for p in payloads if p.get("type") == "message_delta"][0]
        self.assertEqual(delta["usage"]["cache_read_input_tokens"], 32256)
        self.assertEqual(delta["usage"]["cache_creation_input_tokens"], 0)

    def test_message_start_gains_zero_cache_fields(self) -> None:
        """The same completion at turn start: present-and-zero, so a
        transcript stamp reads "no cache activity known yet" rather than
        having no cache keys at all."""
        payloads = _emitted(_sse([
            _message_start({"input_tokens": 0, "output_tokens": 0}),
            _message_delta({"input_tokens": 18, "output_tokens": 21}),
        ]))
        start = [p for p in payloads if p.get("type") == "message_start"][0]
        usage = start["message"]["usage"]
        self.assertEqual(usage["cache_read_input_tokens"], 0)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)

    def test_cached_tokens_maps_to_cache_read(self) -> None:
        """The qwen dialect: cache hits reported as
        ``prompt_tokens_details.cached_tokens`` — the vendor's own figure,
        carried under the name the client reads."""
        payloads = _emitted(_sse([
            _message_start({"input_tokens": 6, "output_tokens": 0}),
            _message_delta({
                "input_tokens": 36, "output_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 128},
            }),
        ]))
        delta = [p for p in payloads if p.get("type") == "message_delta"][0]
        self.assertEqual(delta["usage"]["cache_read_input_tokens"], 128)
        self.assertEqual(delta["usage"]["cache_creation_input_tokens"], 0)

    def test_no_cache_info_emits_zeros_never_a_warm_cache(self) -> None:
        """The leave-alone: no vendor cache info → zeros, and NO
        ``cache_creation.ephemeral_*`` TTL block is ever planted — no seed
        citation documents a vendor cache TTL, so no TTL is claimed."""
        payloads = _emitted(_sse([
            _message_start({"input_tokens": 6, "output_tokens": 0}),
            _message_delta({"input_tokens": 36, "output_tokens": 20}),
        ]))
        delta = [p for p in payloads if p.get("type") == "message_delta"][0]
        self.assertEqual(delta["usage"]["cache_read_input_tokens"], 0)
        self.assertEqual(delta["usage"]["cache_creation_input_tokens"], 0)
        self.assertNotIn("cache_creation", delta["usage"])

    def test_a_fully_reported_stream_is_byte_identical(self) -> None:
        """A vendor that states every field is relayed VERBATIM — the
        rewriter's first property is unchanged by the completion."""
        raw = _sse([
            _message_start({
                "input_tokens": 10, "output_tokens": 0,
                "cache_creation_input_tokens": 4, "cache_read_input_tokens": 6,
            }),
            _message_delta({
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 4, "cache_read_input_tokens": 6,
            }),
        ])
        rewriter = SseIdRewriter()
        out = rewriter.feed(raw) + rewriter.flush()
        self.assertEqual(out, raw)

    def test_the_input_tokens_floor_still_fires_alongside(self) -> None:
        """The v0.2.101 floor and the G2 completion compose: a flagged
        vendor's deficient ``input_tokens`` is lifted AND the cache fields
        are completed, in the one event."""
        payloads = _emitted(_sse([
            _message_start({"input_tokens": 0, "output_tokens": 0}),
        ]), floor=4096)
        usage = payloads[0]["message"]["usage"]
        self.assertEqual(usage["input_tokens"], 4096)
        self.assertEqual(usage["cache_read_input_tokens"], 0)


class CacheFieldNonStreamTests(GatewayTestBase):
    """The buffered vendor body gets the same completion."""

    async def test_a_non_stream_vendor_body_carries_the_cache_fields(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "glm-5.3",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 36, "output_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 64},
            },
        }
        resp = await self.client.post(
            "/v1/messages",
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            headers={**self.auth(), "x-claude-code-session-id": "chat-g2"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["usage"]["cache_read_input_tokens"], 64)
        self.assertEqual(data["usage"]["cache_creation_input_tokens"], 0)
        self.assertNotIn("cache_creation", data["usage"])


# ══════════════════════════════════════════════════════════════════════════
# G3 — per-agent model attribution
# ══════════════════════════════════════════════════════════════════════════
def _record(
    session: Optional[str], agent: Optional[str], forward: str,
    *, route: str = "vendor:zai", fallback_from: str = "",
) -> UsageRecord:
    return build_record(
        session=session, agent=agent, parent_agent=None,
        requested="claude-gw/glm-5.3", route=route, forward=forward,
        stream=True, status=200,
        totals={"input_tokens": 10, "output_tokens": 2},
        usage_complete=True, window_actual=200000, window_source="table",
        fallback_from=fallback_from,
    )


class AgentRowTests(unittest.TestCase):
    """``UsageLedger.agent_rows``: one row per (session, agent) identity,
    naming the model that ANSWERED — the attribution the harness's agents
    GUI cannot show (its row displays the session model for every lane)."""

    def test_rows_are_keyed_per_identity_and_name_the_answering_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / "ledger.jsonl")
            ledger.submit(_record("s1", None, "claude-opus-5-5", route="anthropic"))
            ledger.submit(_record("s1", "agent-aaa", "glm-5.3-flash"))
            rows = ledger.agent_rows()
        self.assertEqual(len(rows), 2)
        by_agent = {row["agent"]: row for row in rows}
        main = by_agent[None]
        self.assertEqual(main["model"], "claude-opus-5-5")
        self.assertEqual(main["session"], "s1")
        sub = by_agent["agent-aaa"]
        self.assertEqual(sub["model"], "glm-5.3-flash")
        self.assertEqual(sub["fallback_from"], "")

    def test_a_fallback_answered_row_names_the_fallback_model(self) -> None:
        """The W3-B guarantee, on the attribution surface: a request the
        ladder served names the rung that ANSWERED, with the escape in
        ``fallback_from`` — never the requested id."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / "ledger.jsonl")
            ledger.submit(_record(
                "s1", "agent-bbb", "glm-5.3-flash",
                fallback_from="anthropic:claude-haiku-5-5",
            ))
            rows = ledger.agent_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "glm-5.3-flash")
        self.assertEqual(rows[0]["fallback_from"], "anthropic:claude-haiku-5-5")


class AgentAttributionRouteTests(GatewayTestBase):
    """``GET /usage/windows`` carries the ``agents`` block the launcher's
    usage card renders."""

    async def test_the_snapshot_names_each_agents_answering_model(self) -> None:
        self.vendor_up.messages_body = _success("glm-5.3")
        resp = await self.client.post(
            "/v1/messages",
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            headers={
                **self.auth(),
                "x-claude-code-session-id": "chat-g3",
                "x-claude-code-agent-id": "agent-ccc",
            },
        )
        self.assertEqual(resp.status, 200)
        resp = await self.client.get("/usage/windows", headers=self.auth())
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        agents = data.get("agents")
        self.assertIsInstance(agents, list)
        mine = [row for row in agents if row["agent"] == "agent-ccc"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["model"], "glm-5.3")
        self.assertEqual(mine[0]["session"], "chat-g3")
        self.assertEqual(mine[0]["route"], "vendor:zai")

    async def test_a_failing_attribution_read_never_fails_the_route(self) -> None:
        # The agents block is a monitor projection: if building it raises, the
        # route still answers 200 with the windows snapshot and an empty list.
        from model_router.server import APP_KEY

        gateway = self.client.server.app[APP_KEY]

        def boom():
            raise RuntimeError("attribution store unreadable")

        gateway.usage.agent_rows = boom
        resp = await self.client.get("/usage/windows", headers=self.auth())
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("agents"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
