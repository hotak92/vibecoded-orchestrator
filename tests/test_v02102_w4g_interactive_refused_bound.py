# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-G — "busy" vs "down" on INTERACTIVE embed paths (coordinator 2026-10-09).

The owner rule "never drop a queued request" stays: an HTTP 503 means the
code-embed service queued the request (busy / loading) and an interactive
caller waits up to the interactive tolerance, honouring Retry-After. A
CONNECTION REFUSED / no listener means the service is DOWN: an interactive
caller retries only for a short bound (~10 s — a container restart) and then
fails loudly, instead of stalling a hook or a user's query for 90 s. Batch
callers keep their long tolerance for both kinds. One policy home:
``vco_lib.retry_after.resolve_refused_tolerance``.

A fake clock drives every loop — no real sleeping, no network.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import retry_after as ra  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += max(0.0, float(secs))

    async def asleep(self, secs):
        self.now += max(0.0, float(secs))


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "sleep", c.sleep)
    for k in (ra.EMBED_503_TOLERANCE_ENV, ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV,
              ra.EMBED_INTERACTIVE_REFUSED_TOLERANCE_ENV, ra.EMBED_503_RETRY_DELAY_ENV):
        monkeypatch.delenv(k, raising=False)
    return c


def _refused():
    raise ConnectionRefusedError("[Errno 111] Connection refused")


def _busy():
    raise RuntimeError("CodeEmbed /api/embeddings returned HTTP 503: busy")


def _run_interactive(fn):
    token = ra.set_503_tolerance_override(ra.resolve_503_interactive_tolerance())
    try:
        return ra.retry_transient_embed(fn, interactive=True)
    finally:
        ra.reset_503_tolerance_override(token)


def test_policy_values():
    assert ra.resolve_refused_tolerance(interactive=False) == ra.DEFAULT_503_TOLERANCE_SECS
    assert ra.resolve_refused_tolerance(interactive=True) == ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS


def test_interactive_refused_gives_up_after_the_short_bound(clock):
    start = clock.now
    with pytest.raises(ConnectionRefusedError):
        _run_interactive(_refused)
    elapsed = clock.now - start
    assert elapsed <= ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS + 0.001, elapsed
    assert elapsed >= ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS - 0.001, elapsed


def test_interactive_refused_then_back_within_the_bound_succeeds(clock):
    calls = []

    def fn():
        calls.append(clock.now)
        if len(calls) < 3:
            _refused()
        return [1.0]

    assert _run_interactive(fn) == [1.0]
    assert clock.now - calls[0] < ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS


def test_interactive_503_still_waits_the_interactive_tolerance(clock):
    start = clock.now
    with pytest.raises(RuntimeError):
        _run_interactive(_busy)
    elapsed = clock.now - start
    assert elapsed > ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS
    assert elapsed >= ra.DEFAULT_503_INTERACTIVE_TOLERANCE_SECS - 10.0


def test_batch_refused_keeps_the_long_tolerance(clock):
    start = clock.now
    with pytest.raises(ConnectionRefusedError):
        ra.retry_transient_embed(_refused)
    assert clock.now - start >= ra.DEFAULT_503_TOLERANCE_SECS - 10.0


def test_cli_post_code_embed_interactive_fails_fast_when_down(clock, monkeypatch):
    import requests

    def _post(*a, **k):
        raise requests.ConnectionError("Connection refused")

    monkeypatch.setattr(requests, "post", _post)
    start = clock.now
    with pytest.raises(requests.ConnectionError):
        ra.post_code_embed("http://code-embed.invalid", "x", interactive=True)
    assert clock.now - start <= ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS + 0.001


# ─── the MCP query path's async loop ─────────────────────────────────────


def _mcp_inline(monkeypatch, clock, tolerance_secs):
    emb = importlib.import_module("weaviate_mcp.embeddings")

    def _session(*a, **k):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    monkeypatch.setattr(emb.aiohttp, "ClientSession", _session)
    monkeypatch.setattr(emb.asyncio, "sleep", clock.asleep)
    start = clock.now
    out = asyncio.run(emb._inline_code_embed_http("q", tolerance_secs=tolerance_secs))
    return out, clock.now - start


def test_mcp_interactive_refused_gives_up_after_the_short_bound(monkeypatch, clock):
    out, elapsed = _mcp_inline(monkeypatch, clock, ra.resolve_503_interactive_tolerance())
    assert out is None
    assert elapsed <= ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS + 0.001, elapsed


def test_mcp_batch_refused_keeps_the_long_tolerance(monkeypatch, clock):
    out, elapsed = _mcp_inline(monkeypatch, clock, None)
    assert out is None
    assert elapsed >= ra.DEFAULT_503_TOLERANCE_SECS - 10.0, elapsed
