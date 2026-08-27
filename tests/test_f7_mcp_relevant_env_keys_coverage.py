# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F-7 (v0.2.75): MCP_RELEVANT_ENV_KEYS covers every weaviate_mcp env read.

``launcher/src-tauri/src/services/settings_json_watcher.rs`` hashes only
the ``MCP_RELEVANT_ENV_KEYS`` subset of ``.claude/settings.json`` ``env``
to decide whether a settings.json write needs a SIGHUP reload. If a key
the MCP CONSUMES is missing from that list, a hand-edit of it hashes to an
unchanged subset → the watcher SKIPS the reload → the live MCP keeps stale
config. CLAUDE.md advertises hand-editing ``KG_TIER_FULL`` and friends, so
the miss is user-facing (F-7).

This is the DURABLE half of the fix (the additions to the Rust list are
the point-in-time half): it source-greps every ``os.getenv(...)`` /
``os.environ.get(...)`` / ``os.environ[...]`` key in the
``claude_mcp_servers/weaviate_mcp`` tree and asserts each is EITHER in the
Rust list OR in an explicit, documented exclusion set below. A new MCP env
read that is genuinely reload-relevant but forgotten in the Rust list
fails here — mirror-don't-fork, same shape as the migrations-count
source-parse tests.

The Rust crate is a separate Cargo workspace not built by the Python test
runner, so we string-parse the ``.rs`` list rather than importing it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "services"
    / "settings_json_watcher.rs"
)
WEAVIATE_MCP_DIR = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp"

# Env keys the weaviate_mcp reads that are DELIBERATELY excluded from
# MCP_RELEVANT_ENV_KEYS. Each MUST have a reason: a change to it either
# (a) does NOT alter search behaviour a running MCP would need reloaded
# for, or (b) is never written into `.claude/settings.json env` (it comes
# from a different channel — hub token, per-invocation session id, an API
# key routed via ~/.claude.json). Excluding these keeps an idempotent
# settings.json write from needlessly SIGHUP-ing the MCP.
DOCUMENTED_EXCLUSIONS: frozenset[str] = frozenset(
    {
        # Never in settings.json env — resolved from hub files / per call.
        "VCT_HUB_PORT",
        "VCT_HUB_TOKEN",
        # v0.2.91 (WP-D item 4): the hermeticity guard for the stale-env
        # token fallback. Same channel as the VCT_HUB_TOKEN it guards — a
        # test/harness pin from the runner's shell, never projected into
        # settings.json env by any writer (verified: no config_projection
        # / launcher / install.py writer emits it). It also cannot change
        # search behaviour: it only governs whether a PROVABLY-refused hub
        # call may retry once with the on-disk token.
        "VCT_HUB_TOKEN_STRICT",
        "VCT_STATE_DIR",
        "VCT_QUERY_LOG_DIR",
        "VCT_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
        "VCT_DISABLE_HUB_RESOLVER",
        # Secrets — routed via ~/.claude.json / keychain, not settings.json
        # env; and not consumed for search-ROUTING behaviour the watcher
        # gates (GITHUB_TOKEN is the existing documented exclusion in the
        # Rust header for the same reason).
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        # RL-server plumbing: read by the optional RL enrichment path, not
        # the base search fan-out; a change reconnects lazily, no reload
        # needed (and these aren't projected into settings.json env).
        "RL_SERVER_PORT",
        "RL_SERVER_URL",
        "RL_MIN_ANSWER_TOKENS_FOR_CITATION",
        # Model-name aliases consumed only when the matching primary key is
        # set; the primaries (EMBEDDING_MODEL / ACTIVE_EMBEDDING /
        # CODE_EMBED_MODEL) ARE listed and gate the reload. These legacy /
        # provider-specific spellings ride along and don't independently
        # need to trip a reload.
        "LEGACY_TEXT_EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_MODEL",
        "TOKENIZER_MODEL",
        # Sidecar-format toggle: not search-routing config;
        # SHARED_KG_NODE_FORMATS controls sidecar summary loading (re-read
        # per query, no reload needed). (VCO_CODE_GRAPH_TEST_PENALTY and the
        # KG_TIER_*/CODE_TIER_* floors are read via named CONSTANTS in
        # code_ranking.py, not literal os.getenv("…") args, so the strict
        # grep here doesn't surface them — no exclusion entry needed. The
        # floors ARE in the Rust list regardless, for the settings.json
        # hand-edit path.)
        "SHARED_KG_NODE_FORMATS",
    }
)


def _parse_rust_relevant_keys() -> set[str]:
    text = WATCHER_RS.read_text(encoding="utf-8")
    m = re.search(
        r"const\s+MCP_RELEVANT_ENV_KEYS:\s*&\[&str\]\s*=\s*&\[(.*?)\];",
        text,
        re.DOTALL,
    )
    assert m, "MCP_RELEVANT_ENV_KEYS array not found in settings_json_watcher.rs"
    body = m.group(1)
    # String-literal entries "FOO_BAR" (skip // comments — findall on the
    # quoted-string pattern naturally ignores comment prose).
    return set(re.findall(r'"([A-Z_][A-Z0-9_]*)"', body))


def _grep_mcp_env_reads() -> set[str]:
    keys: set[str] = set()
    pat = re.compile(
        r"""os\.(?:getenv|environ\.get)\(\s*["']([A-Z_][A-Z0-9_]*)["']"""
        r"""|os\.environ\[\s*["']([A-Z_][A-Z0-9_]*)["']\s*\]"""
    )
    for py in WEAVIATE_MCP_DIR.rglob("*.py"):
        for a, b in pat.findall(py.read_text(encoding="utf-8")):
            keys.add(a or b)
    return keys


class McpRelevantEnvKeysCoverageTest(unittest.TestCase):
    def test_every_mcp_env_read_is_listed_or_documented_excluded(self):
        rust_keys = _parse_rust_relevant_keys()
        mcp_reads = _grep_mcp_env_reads()
        self.assertTrue(mcp_reads, "sanity: found at least some MCP env reads")

        uncovered = mcp_reads - rust_keys - DOCUMENTED_EXCLUSIONS
        self.assertEqual(
            uncovered,
            set(),
            "weaviate_mcp reads these env keys but they are neither in "
            "MCP_RELEVANT_ENV_KEYS nor in DOCUMENTED_EXCLUSIONS. Add each to "
            "the Rust list (if a change should reload the MCP) OR to the "
            f"exclusion set with a reason: {sorted(uncovered)}",
        )

    def test_f7_added_keys_are_present(self):
        """Pin the specific F-7 additions so a bad revert is caught."""
        rust_keys = _parse_rust_relevant_keys()
        for key in (
            "KG_BASE_DIR",
            "KG_TIER_MIN",
            "KG_TIER_SINGLE_CHUNK",
            "KG_TIER_THREE_CHUNKS",
            "KG_TIER_FULL",
            "KG_HYBRID_ALPHA",
            "KG_HYBRID_CHUNK_BUDGET",
            "CODE_TIER_MIN",
            "CODE_TIER_SINGLE_CHUNK",
            "CODE_TIER_THREE_CHUNKS",
            "CODE_TIER_FULL",
            "CODE_EXPANSION_LIMIT",
            "CODE_SIBLINGS_RANK_1",
            "CODE_SIBLINGS_RANK_2",
            "CODE_TRUNC_CHARS",
            "OLLAMA_BASE_URL",
            "EMBEDDING_SOURCE",
            "CODE_EMBED_MODEL",
        ):
            self.assertIn(key, rust_keys, f"F-7 key {key} missing from Rust list")

    def test_exclusions_are_actually_read(self):
        """An exclusion that the MCP no longer reads is stale bookkeeping —
        flag it so the list stays honest."""
        mcp_reads = _grep_mcp_env_reads()
        stale = {k for k in DOCUMENTED_EXCLUSIONS if k not in mcp_reads}
        self.assertEqual(
            stale,
            set(),
            f"DOCUMENTED_EXCLUSIONS lists keys the MCP no longer reads: {sorted(stale)}",
        )


if __name__ == "__main__":
    unittest.main()
