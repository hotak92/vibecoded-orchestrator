# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Memory bounds for the RL caches of the long-lived weaviate-kg MCP — one home.

The weaviate-kg MCP subprocess lives as long as the Claude Code session that
spawned it (hours, often a whole working day), so every per-process cache it
keeps needs a bound in BYTES, not just in entries: an entry-count cap says
nothing when one entry can be a multi-GB parsed transcript (2026-10-13: one MCP
process reached 6.5 GB of anonymous heap in about two hours).

Three bounds live here, each read from the environment AT CALL TIME (so a test
or a user can change it without a restart of the importing module) and each
falling back to its default on an unset, malformed, zero or negative value — a
typo must never turn a bound into "unbounded" or into "cache nothing":

* ``transcript_cache_max_bytes`` — the parsed-transcript cache of
  ``rl_client.answer_window`` (estimated retained bytes).
* ``node_cache_max_bytes`` — the per-search citation ctx cache
  ``rl_state._rl_node_content_cache`` (estimated retained bytes).
* ``monitor_max_concurrent`` — how many in-process answer monitors may run at
  once; a search over the cap defers its citation to the durable pending-file
  path instead (``search_pipeline._spawn_answer_monitor``).
"""

from __future__ import annotations

import os
from array import array

__all__ = [
    "approx_heap_bytes",
    "TRANSCRIPT_CACHE_MAX_BYTES_ENV",
    "NODE_CACHE_MAX_BYTES_ENV",
    "MONITOR_MAX_CONCURRENT_ENV",
    "DEFAULT_TRANSCRIPT_CACHE_MAX_BYTES",
    "DEFAULT_NODE_CACHE_MAX_BYTES",
    "DEFAULT_MONITOR_MAX_CONCURRENT",
    "transcript_cache_max_bytes",
    "node_cache_max_bytes",
    "monitor_max_concurrent",
]

TRANSCRIPT_CACHE_MAX_BYTES_ENV = "VCO_RL_TRANSCRIPT_CACHE_MAX_BYTES"
NODE_CACHE_MAX_BYTES_ENV = "VCO_RL_NODE_CACHE_MAX_BYTES"
MONITOR_MAX_CONCURRENT_ENV = "VCO_RL_MONITOR_MAX_CONCURRENT"

# 256 MiB of SLIMMED transcript state. The cache keeps only the fields the
# answer-window consumers read (tool RESULTS — the bulk of a transcript — are
# reduced to a type stub), so this holds several long sessions at once. The
# most-recently-used transcript is always kept even when it alone exceeds the
# cap: dropping it would make every 2 s monitor poll re-parse the whole file,
# which costs the same memory transiently AND the CPU on every poll.
DEFAULT_TRANSCRIPT_CACHE_MAX_BYTES = 256 * 1024 * 1024

# 64 MiB of compacted citation ctx. With vectors stored as packed floats a
# typical entry is 0.5-1.5 MB, so this holds roughly 40-120 in-flight searches.
# Eviction loses nothing: a running monitor holds its own reference to its ctx
# from its first poll, and every search also stages the durable pending file
# the Stop-hook drain computes from.
DEFAULT_NODE_CACHE_MAX_BYTES = 64 * 1024 * 1024

# 64 concurrent monitors. Each one polls every 2 s for up to an hour and pins
# its compacted ctx (about 1 MB); 64 covers a busy main chat plus a dozen
# subagents searching at once. Searches over the cap are NOT dropped: their
# pending file (staged for every search) is computed by the Stop-hook drain.
DEFAULT_MONITOR_MAX_CONCURRENT = 64


def _positive_int_env(name: str, default: int) -> int:
    """Read ``name`` as a positive int; anything else yields ``default``."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def transcript_cache_max_bytes() -> int:
    return _positive_int_env(TRANSCRIPT_CACHE_MAX_BYTES_ENV, DEFAULT_TRANSCRIPT_CACHE_MAX_BYTES)


def node_cache_max_bytes() -> int:
    return _positive_int_env(NODE_CACHE_MAX_BYTES_ENV, DEFAULT_NODE_CACHE_MAX_BYTES)


def monitor_max_concurrent() -> int:
    return _positive_int_env(MONITOR_MAX_CONCURRENT_ENV, DEFAULT_MONITOR_MAX_CONCURRENT)


def approx_heap_bytes(obj: object) -> int:
    """Estimated retained heap of a JSON-shaped object, in bytes.

    The one estimator both byte bounds use. CPython's typical costs: str ≈ 49 +
    len, dict ≈ 232 + 24/entry, list ≈ 56 + 8/slot, scalar ≈ 32, and an
    ``array.array`` its real payload (itemsize × len) plus its header. An
    ESTIMATE — a bound only needs it proportional and cheap. Iterative, so a
    deeply nested tool input cannot hit the recursion limit.
    """
    total = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            total += 49 + len(o)
        elif isinstance(o, dict):
            total += 232 + 24 * len(o)
            stack.extend(o.values())
        elif isinstance(o, list):
            total += 56 + 8 * len(o)
            stack.extend(o)
        elif isinstance(o, array):
            total += 80 + o.itemsize * len(o)
        else:
            total += 32
    return total
