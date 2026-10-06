# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 §9 — the query cache must NEVER cache an empty or timed-out result.

Lineage: the kickoff probe (reviews/V02101-INJECTION-KICKOFF-PROBES-2026-10-05.md,
Cause 1b) found the SHELL cache (`_lib/query-cache.sh` + `codegraph_query_block`)
storing 0-byte blobs for killed legs, poisoning each key for the whole 900 s
TTL — a re-Read of the same file was a fast EMPTY hit. Wave 1 fixed and
red-proved that on the shell side. Wave 3 (review SF-2/nit-6) then RETIRED the
shell cache entirely with its last callers (`codegraph_query_block`,
`vco_dual_search_cached`, `vco_kg_search_cached` via pre-tool-use §5): the
surviving home of the contract is the ROUTER's Python cache
(`hook_context_router.cache_get/cache_put`, same state dir, same
sha1-of-0x1f-joined key algorithm, disjoint `kgi`/`cgi` namespaces).

These pins hold the contract at that home:
  * ACT   — an empty put writes NOTHING; a leg that produced nothing is
            indistinguishable from a killed leg, so neither may poison the key;
  * ACT   — a pre-existing EMPTY entry (any older writer) reads as a MISS and
            is removed (self-heal);
  * LEAVE-ALONE — non-empty round-trip, TTL staleness, key determinism and
            namespace disjointness behave as designed.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

import hook_context_router as router  # noqa: E402


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    return tmp_path


def _cache_file(proj: Path, key: str) -> Path:
    return proj / ".claude" / "state" / "query_cache" / key


class TestNeverCacheEmpty:
    def test_put_empty_writes_nothing(self, proj: Path) -> None:
        key = router.cache_key("cgi", "emptysym", "edit", "f.py", "p1")
        router.cache_put(str(proj), key, "")
        assert not _cache_file(proj, key).exists()

    def test_empty_entry_is_a_miss_and_self_heals(self, proj: Path) -> None:
        key = router.cache_key("kgi", "poison", "bash_read", "q", "3", "p1")
        f = _cache_file(proj, key)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")  # simulate an older writer's poison entry
        assert router.cache_get(str(proj), key) is None
        assert not f.exists(), "the poisoned entry must be removed on touch"

    def test_timed_out_leg_is_not_cached(self, proj: Path) -> None:
        """The router only calls cache_put with a leg's captured text; an
        abandoned (timed-out) leg yields "" → the empty-put guard is the
        timeout guard. Pin the composition: put("") after a '' leg result
        leaves the key clean for the next live attempt."""
        key = router.cache_key("cgi", "slowq", "grep", "", "p1")
        leg_result = ""  # what run_legs returns for an abandoned leg
        router.cache_put(str(proj), key, leg_result)
        assert not _cache_file(proj, key).exists()
        assert router.cache_get(str(proj), key) is None


class TestLeaveAlone:
    def test_nonempty_roundtrip(self, proj: Path) -> None:
        key = router.cache_key("kgi", "bash_read", "widget loader", "3", "p1")
        blob = "KG: Widget Node | concept | score=0.90 | TITLES\n"
        router.cache_put(str(proj), key, blob)
        assert router.cache_get(str(proj), key) == blob

    def test_stale_entry_is_a_miss(self, proj: Path) -> None:
        key = router.cache_key("kgi", "edit", "stale q", "3", "p1")
        router.cache_put(str(proj), key, "KG: Stale | concept | score=0.9 | TITLES\n")
        f = _cache_file(proj, key)
        old = time.time() - 5000
        os.utime(f, (old, old))
        assert router.cache_get(str(proj), key) is None

    def test_key_is_deterministic_and_namespaced(self) -> None:
        a = router.cache_key("kgi", "q", "3", "p1")
        b = router.cache_key("kgi", "q", "3", "p1")
        c = router.cache_key("cgi", "q", "3", "p1")
        d = router.cache_key("kgi", "q", "3", "p2")
        assert a == b, "keys must be deterministic"
        assert len({a, c, d}) == 3, "surface namespace and prompt_id must separate keys"

    def test_key_algorithm_is_the_legacy_sha1_of_joined_parts(self) -> None:
        """The router's key derivation is the same algorithm the retired
        shell cache used (sha1 over each part FOLLOWED by 0x1f) — pinned so
        the state dir stays one coherent keyspace for any tooling that
        computes keys the documented way."""
        parts = ("kgi", "q", "3", "p1")
        want = hashlib.sha1(
            "".join(f"{p}\x1f" for p in parts).encode("utf-8")
        ).hexdigest()
        assert router.cache_key(*parts) == want
