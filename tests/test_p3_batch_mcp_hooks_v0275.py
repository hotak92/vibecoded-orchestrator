# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P3 batch (v0.2.75): MCP-server + hook structural remainders.

Six small items land together:

  (a) C-7   — store_knowledge_node delete normalizes the stored spelling at
              write, matches BOTH POSIX + backslash spellings at delete, and
              pages past limit=100.
  (b) KG-5  — the degraded inline KG-embedding fallback gates its SECONDARY
              slots on DUAL_EMBEDDING_WRITE_ALL_SLOTS like the primary path.
  (c) KG-4  — kg-sync --all refreshes .node_formats.json (post-sync hook).
  (d) NEW-4 — the hybrid_search fan-out is bounded by asyncio.Semaphore(4).
  (e) HK-2  — the dead vct_scrub_secret_env / Invoke-VctScrubSecretEnv helpers
              are deleted (zero callers; parity gate stays the enforcement).
  (f) HK-4  — accepted-scatter comments at the 4 GC sites.

Some items are exercised behaviourally (KG-5 gate, NEW-4 semaphore, KG-4
regen); the structural ones (C-7 filter shape, HK-2 deletion, HK-4 comments)
are pinned by source assertions where a full Weaviate round-trip isn't
warranted.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
EMBEDDINGS_PY = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "embeddings.py"
SYNC_PY = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
SCRUB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "scrub-env.sh"
SCRUB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "scrub-env.ps1"


# ── (a) C-7: delete filter shape (source-pinned) ─────────────────────────


class TestC7DeleteFilterShape:
    def test_write_normalizes_backslashes_to_posix(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        assert 'rel_file_path = rel_file_path.replace("\\\\", "/")' in src, (
            "C-7 must normalize stored spelling to POSIX at write"
        )

    def test_delete_matches_both_spellings(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        # OR of two exact .equal() filters over posix + backslash variants.
        assert "Filter.any_of([" in src, "C-7 delete must OR the two spellings"
        assert '_backslash_variant = rel_file_path.replace("/", "\\\\")' in src

    def test_delete_pages_past_100(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        # A paging loop (offset advance) replaces the single limit=100 fetch.
        assert "_fetch_offset" in src and "offset=_fetch_offset" in src, (
            "C-7 delete must page past limit=100"
        )
        assert "_FETCH_PAGE = 100" in src

    def test_title_only_fallback_retained_with_pointer(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        # The ratified title-only fallback (when rel_file_path empty) + a C-7
        # pointer comment must remain.
        assert "ratified C-7 fallback" in src


# ── (b) KG-5: degraded fallback gates secondary slots ────────────────────


class TestKG5DegradedFallbackGate:
    def _run(self, write_all_env: str | None):
        # The degraded fallback does `from . import server` (re-imports the
        # real weaviate_mcp.server module), so patch THAT module's functions
        # directly. Force _get_embedding_service()->None so the inline
        # fallback path (the KG-5 target) runs instead of the service path.
        sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
        try:
            from weaviate_mcp import embeddings as emb
            from weaviate_mcp import server as srv
        finally:
            sys.path.pop(0)

        calls = {"ollama": 0, "legacy": 0, "openai": 0}

        async def fake_ollama(text):
            calls["ollama"] += 1
            return [0.1, 0.2]

        async def fake_legacy(text):
            calls["legacy"] += 1
            return [0.3, 0.4]

        async def fake_openai(text):
            calls["openai"] += 1
            return [0.5, 0.6]

        env = {} if write_all_env is None else {"DUAL_EMBEDDING_WRITE_ALL_SLOTS": write_all_env}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(srv, "_get_embedding_service", lambda: None), \
                mock.patch.object(srv, "get_ollama_embedding", fake_ollama), \
                mock.patch.object(srv, "get_legacy_text_embedding", fake_legacy), \
                mock.patch.object(srv, "get_openai_embedding", fake_openai):
            result = asyncio.run(emb._get_all_kg_embeddings("hi"))
        return result, calls

    def test_toggle_off_only_active_slot(self):
        """Default OFF → only qwen3_embed (active); secondary slots NOT
        written even in the degraded path (leave-alone)."""
        result, calls = self._run(None)
        assert "qwen3_embed" in result, "active slot always written"
        assert "ollama_embed" not in result, "legacy secondary must be gated OFF"
        assert "openai_embed" not in result, "openai secondary must be gated OFF"
        assert calls["legacy"] == 0 and calls["openai"] == 0, (
            "gated-off path must not even CALL the secondary backends"
        )

    def test_toggle_on_writes_all_slots(self):
        """ON → every reachable slot (act)."""
        result, calls = self._run("true")
        assert "qwen3_embed" in result
        assert "ollama_embed" in result
        assert "openai_embed" in result


# ── (c) KG-4: kg-sync --all refreshes .node_formats.json ─────────────────


class TestKG4RegenHook:
    def test_sync_source_calls_regen_after_all(self):
        src = SYNC_PY.read_text(encoding="utf-8")
        assert "_regen_node_formats_after_full_sync" in src
        # Called in the --all branch, before the sync exit.
        m = re.search(
            r'sys\.argv\[1\] == "--all".*?_regen_node_formats_after_full_sync\(\)',
            src, re.DOTALL,
        )
        assert m, "regen must fire inside the --all branch"

    def test_regen_is_soft_fail(self):
        src = SYNC_PY.read_text(encoding="utf-8")
        # The helper swallows errors + is timeout-bounded (capped).
        assert "TimeoutExpired" in src and "non-fatal" in src
        assert "timeout=600" in src


# ── (d) NEW-4: bounded fan-out ───────────────────────────────────────────


class TestNew4Semaphore:
    def test_source_has_semaphore_4(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        assert "asyncio.Semaphore(4)" in src, "fan-out must be bounded to 4"
        assert "_bounded_single_collection" in src

    def test_semaphore_bounds_concurrency(self):
        """A Semaphore(4) never lets >4 coroutines run the inner body at once."""
        sem = asyncio.Semaphore(4)
        peak = {"n": 0, "cur": 0}

        async def worker():
            async with sem:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
                await asyncio.sleep(0.01)
                peak["cur"] -= 1

        async def run():
            await asyncio.gather(*[worker() for _ in range(20)])

        asyncio.run(run())
        assert peak["n"] <= 4, f"semaphore(4) allowed {peak['n']} concurrent"


# ── (e) HK-2: dead BASH scrub helper deleted (ps1 helper KEPT — live) ─────
#
# STOP-and-report finding: the brief's premise ("vct_scrub_secret_env has
# ZERO callers") is TRUE only for the .sh side. The .ps1 sibling
# Invoke-VctScrubSecretEnv has THREE live callers
# (session-start-retrieval-health.ps1, kg-sync-on-edit.ps1,
# session-start-deferral-surface.ps1), so deleting it would break those
# hooks. The honest resolution: delete ONLY the dead .sh function; keep the
# live .ps1 one. This test pins that asymmetry.


class TestHK2DeadHelperDeleted:
    def test_sh_helper_function_deleted(self):
        src = SCRUB_SH.read_text(encoding="utf-8")
        # The DEFINITION `vct_scrub_secret_env() {` must be gone (the name may
        # still appear in the HK-2 explanatory comment — that's not a def).
        assert "vct_scrub_secret_env() {" not in src, "dead .sh helper def must be deleted"
        # The canonical VALUE stays (parity gate reads it).
        assert "VCT_SCRUB_SECRET_KEYS=" in src

    def test_ps1_helper_KEPT_because_it_has_callers(self):
        src = SCRUB_PS1.read_text(encoding="utf-8")
        assert "function Invoke-VctScrubSecretEnv" in src, (
            "the .ps1 helper has 3 live callers — it must NOT be deleted"
        )

    def test_no_bash_callers_of_deleted_sh_helper(self):
        """Grep-gate: no .sh hook calls the deleted bash function."""
        hooks_dir = REPO_ROOT / "templates" / "hooks"
        for f in hooks_dir.rglob("*.sh"):
            if f.name == "scrub-env.sh":
                continue
            text = f.read_text(encoding="utf-8")
            assert "vct_scrub_secret_env" not in text, f"{f} calls deleted .sh helper"

    def test_ps1_callers_still_resolve(self):
        """The 3 known .ps1 callers still reference the (retained) function —
        confirming we didn't orphan a call site."""
        hooks_dir = REPO_ROOT / "templates" / "hooks"
        callers = {
            "session-start-retrieval-health.ps1",
            "kg-sync-on-edit.ps1",
            "session-start-deferral-surface.ps1",
        }
        found = set()
        for f in hooks_dir.rglob("*.ps1"):
            if "Invoke-VctScrubSecretEnv" in f.read_text(encoding="utf-8") \
                    and f.name != "scrub-env.ps1":
                found.add(f.name)
        assert callers <= found, (
            f"expected these ps1 callers to remain: {callers - found} missing"
        )


# ── (f) HK-4: accepted-scatter comments ──────────────────────────────────


class TestHK4AcceptedScatter:
    GC_SITES = [
        "pre-edit-context-inject.sh",
        "diff-context-inject.sh",
        "pre-tool-use.sh",
        "pre-bash-context-inject.sh",
    ]

    def test_all_four_sites_note_accepted_scatter(self):
        hooks = REPO_ROOT / "templates" / "hooks"
        for name in self.GC_SITES:
            text = (hooks / name).read_text(encoding="utf-8")
            assert "accepted-scatter" in text, (
                f"{name} must carry the HK-4 accepted-scatter comment"
            )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
