# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106: the QwenCloud and z.ai picker rows, fed their LIVE lists.

Two owner decisions of 2026-11-10 and one plan finding meet here.

* **DeepSeek flash moves to the dated build** ``deepseek-v4.1-flash-1017``
  (DeepSeek changelog https://api-docs.deepseek.com/updates/, 2026-10-17:
  same architecture and prompt format, new weights). QwenCloud serves BOTH
  that id and the undated ``deepseek-v4.1-flash``, separately, and does not
  document which weights the undated id serves — so the dated id is THE
  published flash and the undated one is curated-hidden (still routable by
  name, so an agent file pinned to it before the update keeps answering).
  Live probe 2026-11-10 (fixture ``medium-qwen-deepseek-1017.body``): HTTP
  200, ``model`` echoed exactly, ``thinking`` + ``text`` blocks, effort
  ``medium`` accepted.
* **Picker filters are RULES, not literal ids.** The live list of
  2026-11-10 (``qwen-models-live-2026-11-10.json``) carries a non-chat image
  model (``qwen-image-3.0-pro``) and a video model (``wan3.0-video``) that the
  old prefix list did not name, and two dated builds
  (``deepseek-v4-pro-0813``, ``deepseek-v4-flash-0731``) besides the pinned
  one. A dated build (``-MMDD`` / ``-YYYYMMDD`` tail) never reaches the
  picker unless the row explicitly offers it (owner ruling 2026-09-22, with
  the ONE owner-approved exception of 2026-11-10); a non-chat modality
  (image / video / audio / asr / tts) never does.
* **z.ai's FlashX ids are not on the Coding Plan** (z.ai docs
  https://docs.z.ai/guides/vlm/glm-5.3-flash: "GLM-5.3-FlashX is not yet
  available on the plan"): never published, never forwarded.

The expected lists are WRITTEN OUT, never derived from the shipped rows —
deriving them would compare the row against itself.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
from pathlib import Path

from model_router import catalog as cat
from model_router import effort as E
from model_router import routing
from model_router.context_table import load_seed
from model_router.routing import ONE_M_SUFFIX, Route, RouteError
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS, Vendor

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "model_router"
QWEN_LIVE = json.loads(
    (FIXTURES / "qwen-models-live-2026-11-10.json").read_text(encoding="utf-8"),
)
ZAI_LIVE = json.loads(
    (FIXTURES / "zai-models-live-2026-11-10.json").read_text(encoding="utf-8"),
)

Q = "claude-gw/qwen/"
Z = "claude-gw/"

#: Never published AND never reported hidden: non-chat modalities, the router
#: alias, dated builds the row does not offer, and the retired glm-5.2.
QWEN_NEVER_LISTED = (
    "auto", "wan2.7-image", "wan2.7-image-pro", "wan3.0-video",
    "qwen-image-3.0-pro", "qwen-audio-3.0-tts-plus",
    "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-asr-flash",
    "deepseek-v4-flash-0731", "deepseek-v4-pro-0813",
    "glm-5.2",
)

#: Curated-hidden: reported in ``hidden``, routable by name.
QWEN_CURATED_HIDDEN = (
    f"{Q}qwen3.7-plus{ONE_M_SUFFIX}",
    f"{Q}deepseek-v4-pro{ONE_M_SUFFIX}",
    f"{Q}deepseek-v4.1-flash{ONE_M_SUFFIX}",
)

QWEN_PUBLISHED_ALL = (
    f"{Q}qwen3.7-max{ONE_M_SUFFIX}",
    f"{Q}qwen3.6-flash{ONE_M_SUFFIX}",
    f"{Q}qwen3.8-max{ONE_M_SUFFIX}",
    f"{Q}qwen3.8-flash{ONE_M_SUFFIX}",
    f"{Q}glm-5.3{ONE_M_SUFFIX}",
    f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}",
)

QWEN_PUBLISHED_LATEST = (
    f"{Q}qwen3.8-max{ONE_M_SUFFIX}",
    f"{Q}qwen3.8-flash{ONE_M_SUFFIX}",
    f"{Q}glm-5.3{ONE_M_SUFFIX}",
    f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}",
)

ZAI_PUBLISHED = (f"{Z}glm-5.3{ONE_M_SUFFIX}", f"{Z}glm-5.3-flash{ONE_M_SUFFIX}")
#: Withheld by the z.ai row's truth filter (listed ids that reroute
#: server-side): reported hidden, bare spelling.
ZAI_WITHHELD = tuple(
    f"{Z}{m}" for m in (
        "glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7",
        "glm-5", "glm-5-turbo", "glm-5.1",
    )
)
ZAI_NOT_PROVIDED = ("glm-5.2", "glm-5.3-flashx", "glm-5.3-flashx-legal")


def _service(vendor: Vendor, payload: dict) -> cat.CatalogService:
    async def fetch(url, headers):
        if "anthropic.com" in url:
            return {"data": []}
        return payload

    async def vendor_key(_vendor):
        return "key"

    return cat.CatalogService(
        vendors={vendor.vendor_id: vendor},
        anthropic=ANTHROPIC_FAMILY,
        fetch_json=fetch,
        oauth_token=lambda: "tok",
        vendor_key=vendor_key,
        live_ttl_s=3600,
        static_ttl_s=60,
        clock=lambda: 100.0,
    )


def _union(vendor: Vendor, payload: dict, catalog_filter: str) -> cat.CatalogUnion:
    return asyncio.run(
        _service(vendor, payload).union(
            table=load_seed(), catalog_filter=catalog_filter,
        ),
    )


def _spellings(model_id: str, namespace: str) -> tuple[str, ...]:
    return (
        model_id, f"{namespace}{model_id}", f"{namespace}{model_id}{ONE_M_SUFFIX}",
    )


class QwenLiveListTests(unittest.TestCase):
    """The 2026-11-10 live list through the real catalog path."""

    def _check(self, catalog_filter: str, published: tuple[str, ...],
               extra_hidden: tuple[str, ...]) -> None:
        union = _union(VENDORS["qwen"], QWEN_LIVE, catalog_filter)
        self.assertEqual(union.sources["qwen"], cat.SOURCE_LIVE)
        self.assertEqual(
            sorted(e.id for e in union.entries if e.id.startswith(Q)),
            sorted(published),
        )
        self.assertEqual(
            sorted(h for h in union.hidden if h.startswith(Q)),
            sorted(QWEN_CURATED_HIDDEN + extra_hidden),
        )
        listed = {e.id for e in union.entries} | set(union.hidden)
        for model_id in QWEN_NEVER_LISTED:
            for spelling in _spellings(model_id, Q):
                self.assertNotIn(spelling, listed)

    def test_all_filter_publishes_exactly_the_chat_rows(self) -> None:
        self._check(cat.CATALOG_FILTER_ALL, QWEN_PUBLISHED_ALL, ())

    def test_latest_filter_publishes_the_newest_of_each_family(self) -> None:
        self._check(
            cat.CATALOG_FILTER_LATEST, QWEN_PUBLISHED_LATEST,
            (f"{Q}qwen3.7-max{ONE_M_SUFFIX}", f"{Q}qwen3.6-flash{ONE_M_SUFFIX}"),
        )

    def test_the_keyless_fallback_publishes_the_same_flash(self) -> None:
        """No key: the row's declared ids serve. The pinned dated build is
        among them and the undated id is hidden, exactly as live."""
        async def no_key(_vendor):
            return None

        service = _service(VENDORS["qwen"], QWEN_LIVE)
        service._vendor_key = no_key  # noqa: SLF001 - the injected seam
        union = asyncio.run(
            service.union(table=load_seed(), catalog_filter=cat.CATALOG_FILTER_ALL),
        )
        ids = {e.id for e in union.entries}
        self.assertIn(f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}", ids)
        self.assertNotIn(f"{Q}deepseek-v4.1-flash{ONE_M_SUFFIX}", ids)
        self.assertIn(f"{Q}deepseek-v4.1-flash{ONE_M_SUFFIX}", union.hidden)


class GenericRuleTests(unittest.TestCase):
    """The two rules on a vendor row the shipped data never names."""

    ACME = Vendor(
        vendor_id="acme", display_suffix=" · Acme", namespace="claude-acme/",
        upstream="https://api.acme.example", secret_keys=("acme_key",),
        bare_id_prefixes=("acme",),
        catalog_url="https://lists.acme.example/v1/models",
        static_ids=("acme-chat-2", "acme-chat-2-0915"),
    )

    def test_dated_and_non_chat_ids_are_dropped_by_rule(self) -> None:
        payload = {"data": [{"id": m} for m in (
            "acme-chat-2",
            "acme-chat-2-0915",        # dated, but offered by the row: kept
            "acme-chat-2-1101",        # dated MMDD, not offered: dropped
            "acme-chat-1-20251231",    # dated YYYYMMDD, not offered: dropped
            "acme-chat-3-1399",        # not a valid MMDD: a version, kept
            "acme-image-2",            # image modality: dropped
            "acme-video-1.5",          # video modality: dropped
            "acme-voice-tts-1",        # tts modality: dropped
            "acme-asr-flash",          # asr modality: dropped
            "acme-audio-2",            # audio modality: dropped
        )]}
        union = _union(self.ACME, payload, cat.CATALOG_FILTER_ALL)
        listed = {e.id for e in union.entries} | set(union.hidden)
        self.assertEqual(
            sorted(i for i in listed if i.startswith("claude-acme/")),
            sorted([
                "claude-acme/acme-chat-2", "claude-acme/acme-chat-2-0915",
                "claude-acme/acme-chat-3-1399",
            ]),
        )


class ZaiLiveListTests(unittest.TestCase):
    """z.ai's live list (2026-11-10): what the picker shows, and FlashX."""

    def test_publishes_exactly_the_two_verified_ids(self) -> None:
        for catalog_filter in (cat.CATALOG_FILTER_ALL, cat.CATALOG_FILTER_LATEST):
            with self.subTest(catalog_filter=catalog_filter):
                union = _union(VENDORS["zai"], ZAI_LIVE, catalog_filter)
                self.assertEqual(union.sources["zai"], cat.SOURCE_LIVE)
                self.assertEqual(
                    sorted(e.id for e in union.entries if e.id.startswith(Z)),
                    sorted(ZAI_PUBLISHED),
                )
                self.assertEqual(
                    sorted(h for h in union.hidden if h.startswith(Z)),
                    sorted(ZAI_WITHHELD),
                )
                listed = {e.id for e in union.entries} | set(union.hidden)
                for model_id in ZAI_NOT_PROVIDED:
                    for spelling in _spellings(model_id, Z):
                        self.assertNotIn(spelling, listed)

    def test_flashx_ids_are_refused_by_name_on_every_spelling(self) -> None:
        for model_id in (
            "claude-gw/glm-5.3-flashx", "claude-gw/glm-5.3-flashx[1m]",
            "glm-5.3-flashx", "claude-gw/glm-5.3-flashx-legal",
            "glm-5.3-flashx-legal", "claude-gw/qwen/glm-5.3-flashx",
        ):
            with self.subTest(model_id=model_id):
                decision = routing.route(model_id)
                self.assertIsInstance(decision, RouteError)
                assert isinstance(decision, RouteError)
                self.assertEqual(decision.reason, "unknown_model")
                if not model_id.startswith(Q):
                    self.assertIn(
                        "claude-gw/glm-5.3-flash",
                        [routing.strip_1m(s) for s in decision.suggestions],
                        "the refusal points at the plan's flash",
                    )
        # The plan's flash still routes.
        self.assertNotIsInstance(routing.route("claude-gw/glm-5.3-flash"), RouteError)


class DeepSeekFlashRoutingTests(unittest.TestCase):
    def test_the_dated_build_routes_as_itself(self) -> None:
        decision = routing.route(f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}")
        assert isinstance(decision, Route), decision
        self.assertEqual(decision.forward_model, "deepseek-v4.1-flash-1017")
        valid, reason, _ = routing.validate_model_id(
            f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}",
        )
        self.assertTrue(valid, reason)

    def test_the_undated_id_stays_answerable_by_name(self) -> None:
        decision = routing.route(f"{Q}deepseek-v4.1-flash{ONE_M_SUFFIX}")
        assert isinstance(decision, Route), decision
        self.assertEqual(decision.forward_model, "deepseek-v4.1-flash")

    def test_effort_policy_covers_the_dated_build(self) -> None:
        dated = E.EFFORT_POLICIES[("qwen", "deepseek-v4.1-flash-1017")]
        self.assertEqual(dated.translate, {})
        self.assertEqual(
            dated.collapses, {"low": "high", "medium": "high", "xhigh": "max"},
        )
        self.assertIn("medium", dated.accepted)

    def test_live_probe_fixture_echoes_the_dated_id(self) -> None:
        body = json.loads(
            (FIXTURES / "medium-qwen-deepseek-1017.body").read_text(encoding="utf-8"),
        )
        self.assertEqual(body["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(
            [b["type"] for b in body["content"]], ["thinking", "text"],
        )

    def test_seed_cites_a_1m_text_and_image_row(self) -> None:
        row = load_seed().lookup("deepseek-v4.1-flash-1017", "qwen")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.context_window, 1_000_000)
        self.assertTrue(row.window_1m)


class ShippedAgentPinTests(unittest.TestCase):
    PIN = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)

    def test_every_shipped_deepseek_agent_pins_the_dated_build(self) -> None:
        agents = sorted(
            (REPO / "templates" / "agents" / "module-gateway").glob("deepseek-*.md"),
        )
        self.assertTrue(agents)
        for path in agents:
            with self.subTest(agent=path.name):
                match = self.PIN.search(path.read_text(encoding="utf-8"))
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(
                    match.group(1), f"{Q}deepseek-v4.1-flash-1017{ONE_M_SUFFIX}",
                )


if __name__ == "__main__":
    unittest.main()
