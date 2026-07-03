"""v0.2.73 C-5 / C-6 — CLI≡MCP code-query-embedding parity.

The CLI (``templates/scripts/query_code_graph.py``) and the MCP
(``claude_mcp_servers/weaviate_mcp/server.py``) MUST embed a search query
identically on every ladder slot, and resolve the SAME ``target_vector``
slot — otherwise the two surfaces return different code-graph results
for the same query.

Regression guarded here:
  * C-5 — the MCP used the codesage-biased ``get_code_embedding`` for
    queries, which bypassed ``svc.embed_code`` on non-CodeSage slots
    (qwen3 / jina), so the MCP query vector came from raw HTTP :11440
    while the CLI's came from the resolved slot backend.
  * C-6 — the MCP's svc-None ``target_vector`` fallback branched on
    ``ACTIVE_EMBEDDING`` while the CLI unconditionally used
    ``codesage_embed``.

WORKTREE FOOTGUN (wave-1 diagram-test lesson): these tests do NOT spawn
venv-resolving hooks, but we defensively clear ambient ``VCT_VENV`` /
``VCT_DISABLE_HUB_RESOLVER`` at import time so a hook-spawning sibling in
the same process can't leak state.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
import unittest
from pathlib import Path

# Defensive env hygiene (wave-1 lesson): never inherit an ambient venv
# pin that would repoint EmbeddingService construction.
os.environ.pop("VCT_VENV", None)
os.environ.setdefault("VCT_DISABLE_HUB_RESOLVER", "1")

_REPO = Path(__file__).resolve().parents[1]
_MCP = _REPO / "claude_mcp_servers"
for _p in (str(_REPO), str(_MCP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Every ladder tier the two surfaces must agree on.
_LADDER_SLOTS = ["codesage_embed", "qwen3_embed", "jina_embed", "openai_code_embed"]


class _FakeService:
    """Minimal EmbeddingService stub: records embed_code calls per slot."""

    def __init__(self, code_slot: str):
        self.code_vector_slot = code_slot
        self.text_vector_slot = "qwen3_embed"
        self.embed_code_calls: list[str] = []

    def embed_code(self, text: str) -> list:
        # A slot-tagged vector so a raw-HTTP bypass would be detectable.
        self.embed_code_calls.append(text)
        return [len(self.code_vector_slot), 0.1, 0.2]


class CodeQueryEmbeddingParityTests(unittest.TestCase):
    def setUp(self):
        import claude_mcp_servers.weaviate_mcp.server as server  # noqa: E402

        self.server = server
        # Import the CLI module from templates/scripts.
        scripts_dir = str(_REPO / "templates" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        self.cli = importlib.import_module("query_code_graph")

    def _run(self, coro):
        return asyncio.run(coro)

    # ----- C-5: query embedding routes through svc.embed_code for ALL slots -----

    def test_mcp_query_embedding_uses_service_for_all_slots(self):
        for slot in _LADDER_SLOTS:
            fake = _FakeService(slot)
            self.server._cached_embed_service = fake
            try:
                vec = self._run(self.server.get_code_query_embedding("auth middleware"))
            finally:
                self.server._cached_embed_service = None
            self.assertEqual(
                fake.embed_code_calls,
                ["auth middleware"],
                f"MCP must route slot {slot!r} through svc.embed_code (C-5)",
            )
            self.assertEqual(vec, [len(slot), 0.1, 0.2])

    def test_cli_query_embedding_uses_service_for_all_slots(self):
        for slot in _LADDER_SLOTS:
            fake = _FakeService(slot)
            self.cli._cached_embedding_service = fake
            try:
                vec = self.cli.generate_code_embedding("auth middleware")
            finally:
                self.cli._cached_embedding_service = None
            self.assertEqual(fake.embed_code_calls, ["auth middleware"])
            self.assertEqual(vec, [len(slot), 0.1, 0.2])

    def test_mcp_and_cli_produce_identical_query_vector_per_slot(self):
        """The load-bearing invariant: same query, same slot → same vector."""
        for slot in _LADDER_SLOTS:
            mcp_fake = _FakeService(slot)
            cli_fake = _FakeService(slot)
            self.server._cached_embed_service = mcp_fake
            self.cli._cached_embedding_service = cli_fake
            try:
                mcp_vec = self._run(self.server.get_code_query_embedding("q"))
                cli_vec = self.cli.generate_code_embedding("q")
            finally:
                self.server._cached_embed_service = None
                self.cli._cached_embedding_service = None
            self.assertEqual(
                mcp_vec, cli_vec, f"MCP≡CLI query vector must match on slot {slot!r}"
            )

    # ----- C-6: svc-None target_vector fallback parity -----

    def test_target_vector_slot_parity_svc_present(self):
        for slot in _LADDER_SLOTS:
            fake = _FakeService(slot)
            self.server._cached_embed_service = fake
            self.cli._cached_embedding_service = fake
            try:
                self.assertEqual(
                    self.server._active_code_query_slot(),
                    self.cli._active_code_vector_slot(),
                    f"target_vector slot must match on slot {slot!r}",
                )
                self.assertEqual(self.server._active_code_query_slot(), slot)
            finally:
                self.server._cached_embed_service = None
                self.cli._cached_embedding_service = None

    def test_target_vector_slot_parity_svc_none(self):
        self.server._cached_embed_service = None
        self.cli._cached_embedding_service = None
        # Force _get_embedding_service to return None on both sides.
        orig_mcp = self.server._get_embedding_service
        orig_cli = self.cli._get_or_create_embedding_service
        self.server._get_embedding_service = lambda: None
        self.cli._get_or_create_embedding_service = lambda: None
        try:
            self.assertEqual(
                self.server._active_code_query_slot(),
                self.cli._active_code_vector_slot(),
            )
            # CLI's documented svc-None fallback is codesage_embed.
            self.assertEqual(self.server._active_code_query_slot(), "codesage_embed")
        finally:
            self.server._get_embedding_service = orig_mcp
            self.cli._get_or_create_embedding_service = orig_cli

    def test_codesage_backfill_helper_unchanged_for_non_codesage(self):
        """get_code_embedding stays codesage-biased (backfill contract):
        it must NOT route non-codesage slots through svc.embed_code."""
        fake = _FakeService("qwen3_embed")
        self.server._cached_embed_service = fake

        async def _fake_http(text):
            return [999.0]

        orig = self.server._inline_code_embed_http
        self.server._inline_code_embed_http = _fake_http
        try:
            vec = self._run(self.server.get_code_embedding("x"))
        finally:
            self.server._inline_code_embed_http = orig
            self.server._cached_embed_service = None
        # svc present but slot != codesage → must fall to HTTP, NOT embed_code.
        self.assertEqual(fake.embed_code_calls, [])
        self.assertEqual(vec, [999.0])


if __name__ == "__main__":
    unittest.main()
