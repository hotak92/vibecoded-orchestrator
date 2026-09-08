# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``[1m]`` end to end: advertised in the catalog, stripped on the wire.

Two defects, one root cause — the gateway treated Claude Code's ``[1m]``
spelling as an upstream model name.

1. **Forwarding.** ``claude-fable-5-1[1m]`` went to ``api.anthropic.com``
   verbatim and came back ``404 not_found_error: model:
   claude-fable-5-1[1m]`` (live probe, 2026-09-08; the same id WITHOUT the
   suffix returned 200 in the same minute). The 1M window is bought with the
   ``context-1m-2025-08-07`` beta header, not with a model name.
2. **Advertising.** No first-party id was published with a ``[1m]`` companion,
   so the client budgeted 200K for a 1M model and compacted at a fifth of the
   window the user was paying for.

The catalog half is driven through the real ``ContextTableLoader`` with an
export that has NO Claude rows — the shape every upgraded install actually
has, and the reason the seed has to remain reachable per row.
"""
from __future__ import annotations

import json
import unittest

from model_router import context_table as ct
from model_router.catalog import ONE_M_DISPLAY_SUFFIX
from model_router.routing import ONE_M_SUFFIX, strip_1m, with_1m
from model_router.server import CONTEXT_1M_BETA

from tests.test_model_router_server import GatewayTestBase


class SuffixHelperTests(unittest.TestCase):
    def test_with_1m_is_the_inverse_of_strip_1m_and_idempotent(self) -> None:
        self.assertEqual(with_1m("claude-opus-5"), "claude-opus-5[1m]")
        self.assertEqual(with_1m("claude-opus-5[1m]"), "claude-opus-5[1m]")
        self.assertEqual(strip_1m(with_1m("claude-opus-5")), "claude-opus-5")


class ExportFallbackTests(unittest.TestCase):
    """An export that lacks a row must not mean "no 1M window"."""

    def _loader_with_export(
        self, tmp_path, models: dict, tombstones: object = None,
    ) -> ct.ContextTableLoader:
        path = tmp_path / "chat_model_context.json"
        document: dict = {
            "schema_version": 1,
            "generated_at": "2026-09-08T00:00:00Z",
            "source": "launcher.db",
            "models": models,
        }
        if tombstones is not None:
            document["tombstones"] = tombstones
        path.write_text(json.dumps(document), encoding="utf-8")
        return ct.ContextTableLoader(path)

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-1m-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_vendor_only_export_still_advertises_first_party_1m(self) -> None:
        loader = self._loader_with_export(
            self.root,
            {
                "glm-5.3": {
                    "vendor": "zai", "context_window": 1000000,
                    "max_output": 128000, "window_1m": True,
                    "source": "https://example.invalid/glm-5.3",
                }
            },
        )
        table = loader.current()
        self.assertEqual(table.source, ct.SOURCE_EXPORT)
        self.assertTrue(table.advertise_1m("glm-5.3"), "the export's own row")
        self.assertTrue(
            table.advertise_1m("claude-fable-5-1"),
            "an id the export omits falls through to the shipped seed",
        )

    def test_an_export_row_still_wins_for_the_id_it_names(self) -> None:
        loader = self._loader_with_export(
            self.root,
            {
                "claude-fable-5-1": {
                    "vendor": "anthropic", "context_window": 200000,
                    "max_output": 64000, "window_1m": False,
                    "source": "https://example.invalid/override",
                }
            },
        )
        table = loader.current()
        self.assertFalse(table.advertise_1m("claude-fable-5-1"))

    def test_an_unknown_id_gets_no_1m_advert(self) -> None:
        """Documented default: no row anywhere -> no suffix -> the client's
        own conservative 200K assumption. The gateway never invents a window.
        """
        loader = self._loader_with_export(self.root, {})
        self.assertFalse(loader.current().advertise_1m("claude-newmodel-9"))

    def test_a_tombstoned_id_is_deleted_not_merely_absent(self) -> None:
        """The cross-lane contract with the export writer.

        Per-row fallback makes "the user removed this row in the GUI" and
        "the export never mentioned it" the same state — so the seed would
        hand a deleted row straight back. ``tombstones`` is the writer saying
        DELETED, and it is consulted before the fallback.
        """
        loader = self._loader_with_export(
            self.root, {}, tombstones=["claude-fable-5-1"],
        )
        table = loader.current()
        self.assertIsNone(table.lookup("claude-fable-5-1"))
        self.assertFalse(table.advertise_1m("claude-fable-5-1"))
        self.assertTrue(
            table.advertise_1m("claude-opus-5"),
            "a row that was NOT tombstoned still falls through to the seed",
        )

    def test_an_export_without_the_key_tombstones_nothing(self) -> None:
        """An export written by an older launcher keeps working unchanged."""
        table = self._loader_with_export(self.root, {}).current()
        self.assertEqual(table.tombstones, frozenset())
        self.assertTrue(table.advertise_1m("claude-fable-5-1"))

    def test_a_malformed_tombstones_key_is_ignored_not_fatal(self) -> None:
        table = self._loader_with_export(
            self.root, {}, tombstones="claude-fable-5-1",
        ).current()
        self.assertEqual(table.tombstones, frozenset())
        self.assertEqual(table.source, ct.SOURCE_EXPORT)

    def test_the_seed_alone_needs_no_fallback(self) -> None:
        seed = ct.load_seed()
        self.assertEqual(dict(seed.fallback_rows), {})
        self.assertTrue(seed.advertise_1m("claude-opus-5"))


class OneMGatewayTests(GatewayTestBase):
    async def test_the_suffix_is_stripped_before_the_upstream_sees_it(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-fable-5-1[1m]", "messages": []},
        )
        forwarded = json.loads(self.anthropic_up.message_requests[-1]["body"])
        self.assertEqual(forwarded["model"], "claude-fable-5-1")

    async def test_the_beta_header_is_added_when_the_client_sent_none(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-fable-5-1[1m]", "messages": []},
        )
        headers = self.anthropic_up.message_requests[-1]["headers"]
        self.assertIn(CONTEXT_1M_BETA, headers["anthropic-beta"])

    async def test_every_beta_the_client_sent_is_kept(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "anthropic-beta": "fine-grained-tool-2025"},
            json={"model": "claude-fable-5-1[1m]", "messages": []},
        )
        beta = self.anthropic_up.message_requests[-1]["headers"]["anthropic-beta"]
        self.assertIn("fine-grained-tool-2025", beta)
        self.assertIn(CONTEXT_1M_BETA, beta)

    async def test_a_plain_id_does_not_get_the_1m_beta(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-fable-5-1", "messages": []},
        )
        beta = self.anthropic_up.message_requests[-1]["headers"].get(
            "anthropic-beta", "",
        )
        self.assertNotIn(CONTEXT_1M_BETA, beta)

    async def test_the_client_asking_for_the_window_itself_is_not_duplicated(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "anthropic-beta": CONTEXT_1M_BETA},
            json={"model": "claude-fable-5-1[1m]", "messages": []},
        )
        beta = self.anthropic_up.message_requests[-1]["headers"]["anthropic-beta"]
        self.assertEqual(beta.count(CONTEXT_1M_BETA), 1)

    async def test_the_catalog_publishes_a_1m_companion_for_a_1m_claude_id(self) -> None:
        self.anthropic_up.models_payload = {
            "data": [
                {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1"},
                {"id": "claude-haiku-4-5-20251001", "display_name": "Haiku 4.5"},
            ]
        }
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        resp = await self.client.get("/v1/models", headers=self.auth())
        entries = (await resp.json())["data"]
        ids = [e["id"] for e in entries]

        self.assertIn("claude-fable-5-1", ids)
        self.assertIn("claude-fable-5-1[1m]", ids)
        self.assertNotIn(
            "claude-haiku-4-5-20251001[1m]", ids,
            "haiku has no 1M row — the table decides, the catalog never guesses",
        )
        companion = next(e for e in entries if e["id"] == f"claude-fable-5-1{ONE_M_SUFFIX}")
        self.assertEqual(
            companion["display_name"], f"Claude Fable 5.1{ONE_M_DISPLAY_SUFFIX}",
        )

    async def test_a_tombstoned_id_gets_no_companion_in_the_catalog(self) -> None:
        (self.root / "chat_model_context.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-09-08T00:00:00Z",
                    "source": "launcher.db",
                    "tombstones": ["claude-fable-5-1"],
                    "models": {},
                }
            ),
            encoding="utf-8",
        )
        self.anthropic_up.models_payload = {
            "data": [
                {"id": "claude-fable-5-1", "display_name": "F"},
                {"id": "claude-opus-5", "display_name": "O"},
            ]
        }
        self.vendor_up.models_payload = {"data": []}
        resp = await self.client.get("/v1/models", headers=self.auth())
        ids = [e["id"] for e in (await resp.json())["data"]]
        self.assertIn("claude-fable-5-1", ids)
        self.assertNotIn("claude-fable-5-1[1m]", ids)
        self.assertIn("claude-opus-5[1m]", ids)

    async def test_an_advertised_1m_id_is_one_the_router_honours(self) -> None:
        """A picker row the router would refuse is worse than no row."""
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-fable-5-1", "display_name": "F"}]
        }
        self.vendor_up.models_payload = {"data": []}
        resp = await self.client.get("/v1/models", headers=self.auth())
        for entry in (await resp.json())["data"]:
            with self.subTest(model=entry["id"]):
                answer = await self.client.post(
                    "/v1/messages",
                    headers=self.auth(),
                    json={"model": entry["id"], "messages": []},
                )
                self.assertEqual(answer.status, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
