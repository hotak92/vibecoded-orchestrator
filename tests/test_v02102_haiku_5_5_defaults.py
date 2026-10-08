# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Haiku 5.5 is the current Haiku wherever VCO names one (v0.2.102, W4-H).

Anthropic's ``claude-haiku-5-5`` (API id and alias are the same string; 1M
context, 128K output, adaptive thinking with default effort medium) replaced
``claude-haiku-4-5-20251001``, which is now a legacy model — still served,
so the gateway keeps it in its static catalogue and keeps treating it as a
200K model, but no VCO default sends it any more.

Pinned here by BEHAVIOUR, not by scanning source text: each default is driven
through the code path that sends it and the outgoing request is inspected.

* the KG-summary API tier (``templates/scripts/summary_backends.py``);
* the node-format generator's ``--provider haiku`` path
  (``claude_mcp_servers/scripts/generate_node_formats.py``);
* the gateway data: the current Haiku has a cited 1M seed row, the legacy one
  has none, and the first-party route carries no effort-policy row for either
  — first-party adaptive-thinking models pass ``output_config.effort``
  through byte-identical, Haiku 5.5 exactly like Opus/Sonnet 5.5.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARY_BACKENDS = REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
GENERATOR = REPO_ROOT / "claude_mcp_servers" / "scripts" / "generate_node_formats.py"

CURRENT_HAIKU = "claude-haiku-5-5"
LEGACY_HAIKU = "claude-haiku-4-5-20251001"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # an optional dep (requests) absent
        raise unittest.SkipTest(f"{path.name} deps missing ({exc})")
    return mod


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SummaryBackendSendsTheCurrentHaiku(unittest.TestCase):
    def test_the_api_tier_posts_haiku_5_5(self) -> None:
        sb = _load("_w4h_summary_backends", SUMMARY_BACKENDS)
        sent: list[dict[str, Any]] = []

        def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
            sent.append(json.loads(req.data.decode("utf-8")))
            body = {"content": [{"type": "text", "text": "a summary"}]}
            return _Resp(json.dumps(body).encode("utf-8"))

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-not-a-key"}), \
                mock.patch.object(sb.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(sb.call_api("summarise this"), "a summary")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["model"], CURRENT_HAIKU)


class NodeFormatGeneratorSendsTheCurrentHaiku(unittest.TestCase):
    def test_provider_haiku_posts_haiku_5_5(self) -> None:
        gnf = _load("_w4h_generate_node_formats", GENERATOR)
        self.assertEqual(gnf.HAIKU_MODEL, CURRENT_HAIKU)
        captured: dict[str, Any] = {}

        class _Ok:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"content": [{"text": " formats "}]}

        def fake_post(url: str, **kwargs: Any) -> _Ok:
            captured.update(kwargs["json"])
            return _Ok()

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-not-a-key"}), \
                mock.patch.object(gnf.requests, "post", fake_post):
            self.assertEqual(gnf.call_haiku("p", 64), "formats")
        self.assertEqual(captured["model"], CURRENT_HAIKU)


class GatewayDataTreatsHaiku55LikeThe55Generation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        servers = str(REPO_ROOT / "claude_mcp_servers")
        if servers not in sys.path:
            sys.path.insert(0, servers)

    def test_current_haiku_is_a_cited_1m_row_and_legacy_has_none(self) -> None:
        from model_router.context_table import load_seed

        seed = load_seed()
        row = seed.lookup_id(CURRENT_HAIKU)
        self.assertIsNotNone(row)
        assert row is not None  # noqa: S101 — narrowed for the type checker
        self.assertEqual(row.vendor, "anthropic")
        self.assertEqual(row.context_window, 1_000_000)
        self.assertEqual(row.max_output, 128_000)
        self.assertTrue(row.window_1m)
        self.assertIsNone(seed.lookup_id(LEGACY_HAIKU))

    def test_both_haiku_ids_are_in_the_static_catalogue(self) -> None:
        from model_router.catalog import STATIC_CATALOG_PATH

        payload = json.loads(STATIC_CATALOG_PATH.read_text(encoding="utf-8"))
        ids = [m["id"] for m in payload["families"]["anthropic"]["models"]]
        self.assertIn(CURRENT_HAIKU, ids)
        self.assertIn(LEGACY_HAIKU, ids, "legacy, still served: kept listed")

    def test_no_effort_rewrite_for_any_first_party_5_5_model(self) -> None:
        """The effort table holds vendor routes only; a first-party adaptive
        model's ``output_config.effort`` is never rewritten — Haiku 5.5's
        default ``medium`` reaches Anthropic exactly as the client sent it."""
        from model_router.effort import EFFORT_POLICIES, translate_effort

        self.assertFalse(
            [key for key in EFFORT_POLICIES if "claude" in key[1].lower()],
            "an effort row for a Claude model would rewrite a first-party value",
        )
        payload = {"model": CURRENT_HAIKU, "output_config": {"effort": "medium"},
                   "thinking": {"type": "adaptive"}}
        for vendor_id in ("anthropic", ""):
            out, changed = translate_effort(payload, vendor_id, CURRENT_HAIKU)
            self.assertFalse(changed)
            self.assertIs(out, payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
