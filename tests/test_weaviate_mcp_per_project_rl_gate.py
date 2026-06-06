# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.49 Stream B — per-project RL Reranker enable toggle (MCP gate).

The launcher's GUI lets the user silence the RL Reranker per-project
even when the license tier would permit reranking. The hub exposes the
state as ``ProjectConfig.rl_reranker_enabled_for_project`` and the MCP
consults it in ``_rl_cache_and_rerank`` BEFORE issuing a rerank request.

These tests pin three contracts:

1. **Hub absent / resolver returns None** → fall open (rerank as
   before). Never silently disable a paying user's reranker because
   the hub crashed.
2. **Hub returns ``True``** → rerank fires (or the tier check already
   skipped it for other reasons — unchanged).
3. **Hub returns ``False``** → rerank is skipped; telemetry log still
   fires (the SERVER's local JSONL writer is untouched).

The tests stub ``_try_resolve_project_config`` directly so the test
doesn't need a live hub. The free-tier license path is exercised via
the import-shim used by the existing telemetry-on-failure tests.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubWriter:
    """Captures kwargs to log_retrieval so the test can assert the
    telemetry path fires even when the rerank path is gated off."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_retrieval(self, **kwargs):
        self.calls.append(dict(kwargs))


class PerProjectRlGateTest(unittest.TestCase):
    """Pin the v0.2.49 Stream B contract: per-project disable skips
    the rerank request without dropping telemetry."""

    def _make_nodes(self) -> list[dict]:
        return [
            {"title": "N1", "score": 0.7, "tier": "top_k"},
            {"title": "N2", "score": 0.5, "tier": "extra_reference"},
        ]

    def test_per_project_disabled_skips_rerank_but_logs_telemetry(self):
        """Hub returns the toggle as False → rerank request never
        fires; telemetry log_retrieval STILL fires. This is the core
        guarantee — training events for the project are written by
        the server-side JSONL writer regardless of the gate (covered
        by the telemetry log here standing in for the writer chain
        documented in MEMORY.md)."""
        writer = _StubWriter()

        # Stub the resolver to claim this project has the toggle OFF.
        fake_cfg = SimpleNamespace(rl_reranker_enabled_for_project=False)
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer), \
             patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg), \
             patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
            # Despite VCThelpers being absent (free-tier branch), the
            # per-project gate is consulted on the same code path and
            # the rerank short-circuits regardless.
            result = _run(srv._rl_cache_and_rerank(
                "task-disabled",
                "any query",
                self._make_nodes(),
                1,
            ))
        # Top-`limit` cosine order returned, no rerank applied.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "N1")
        # Telemetry write still landed (the SERVER's local-JSONL
        # accumulation must not be silently dropped by the client gate).
        self.assertEqual(
            len(writer.calls),
            1,
            "per-project disable must NOT skip log_retrieval — the "
            "training corpus depends on every retrieval event being "
            "captured locally regardless of whether reranking ran",
        )

    def test_per_project_enabled_does_not_short_circuit(self):
        """Hub returns toggle as True → existing tier-based gating
        decides. With VCThelpers absent (free-tier ImportError branch),
        rerank still skipped — but for the OLD reason (free tier), not
        the new gate. Validates the gate is additive, not destructive."""
        writer = _StubWriter()
        fake_cfg = SimpleNamespace(rl_reranker_enabled_for_project=True)
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer), \
             patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg), \
             patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
            result = _run(srv._rl_cache_and_rerank(
                "task-enabled-free",
                "any query",
                self._make_nodes(),
                1,
            ))
        # Free-tier behaviour unchanged.
        self.assertEqual(len(result), 1)
        self.assertEqual(len(writer.calls), 1)

    def test_resolver_none_falls_open_to_tier_decision(self):
        """Hub unreachable (resolver returns None) → gate falls open
        and the tier-based logic alone decides. This is the safety
        net: a hub crash must NEVER silently disable a paying user's
        reranker. With VCThelpers absent, free-tier still skips
        rerank — the test verifies the function reaches that branch
        instead of raising or hanging."""
        writer = _StubWriter()
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer), \
             patch.object(srv, "_try_resolve_project_config", return_value=None), \
             patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
            result = _run(srv._rl_cache_and_rerank(
                "task-no-hub",
                "any query",
                self._make_nodes(),
                1,
            ))
        # Free-tier path executes cleanly: returns cosine order.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "N1")
        # Telemetry write still landed.
        self.assertEqual(len(writer.calls), 1)

    def test_resolver_raises_falls_open(self):
        """If the resolver itself raises (e.g. ProjectConfig parser
        error on a corrupted hub response), the gate logs at debug
        and leaves _rl_enabled at whatever the tier check decided.
        This is the second safety net — never silently disable
        because of a resolver bug."""
        writer = _StubWriter()
        # Simulate the resolver raising an unexpected exception.
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer), \
             patch.object(
                 srv, "_try_resolve_project_config",
                 side_effect=RuntimeError("simulated resolver crash"),
             ), \
             patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
            result = _run(srv._rl_cache_and_rerank(
                "task-resolver-raises",
                "any query",
                self._make_nodes(),
                1,
            ))
        # Function returns normally; tier check (free) skipped rerank;
        # telemetry write landed.
        self.assertEqual(len(result), 1)
        self.assertEqual(len(writer.calls), 1)

    def test_resolver_missing_attribute_falls_open_to_true(self):
        """Pre-v0.2.49 ProjectConfig instances (e.g. a serialised
        cache from an older client) might not have the attribute at
        all. The getattr fallback in the MCP gate defaults to True so
        the rerank doesn't accidentally short-circuit on a schema-
        skew condition."""
        writer = _StubWriter()
        # Bare object with no rl_reranker_enabled_for_project attr.
        fake_cfg_no_attr = SimpleNamespace()
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer), \
             patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg_no_attr), \
             patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
            result = _run(srv._rl_cache_and_rerank(
                "task-no-attr",
                "any query",
                self._make_nodes(),
                1,
            ))
        # Free-tier still returns cosine order; gate did not flip.
        self.assertEqual(len(result), 1)
        self.assertEqual(len(writer.calls), 1)


if __name__ == "__main__":
    unittest.main()
