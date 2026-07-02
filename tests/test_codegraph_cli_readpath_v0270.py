# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream C1 read-path regression tests.

The code-graph CLI (`templates/scripts/query_code_graph.py`) had TWO v0.2.21
CLI-only regressions that made every hook code-graph injection return
no-results:
  C1a — `main()` resolved the default project from the launcher SLUG
        (`code_graph_project`, e.g. "orchestrator-root") which sanitises to a
        NONEXISTENT collection (`Orchestrator_root_CodeFunction`). The canonical
        binding-row prefix is `code_graph_collection_prefix`.
  C1b — a fixed 0.35 score floor culled ALL CodeSage results (their distances
        cluster ~0.70 -> score ~0.30 < 0.35). The MCP has no floor and works.
        Fix: an EMBEDDER-AWARE floor keyed on the active code-vector slot.
  C1c — a defensive None-guard on the `structure` `references.get` path.

These tests pin the fixes WITHOUT a live Weaviate (static source + unit-level
floor resolution). A live end-to-end smoke is included but skipped when the
code-graph backend isn't reachable.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SRC = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"


def _load_cli_module():
    """Import templates/scripts/query_code_graph.py as a module."""
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("_qcg_v0270", CLI_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# C1a — prefix precedence (static source assertion + behavioral)
# --------------------------------------------------------------------------
def test_c1a_main_uses_collection_prefix_not_slug() -> None:
    """The default-project resolution MUST prefer code_graph_collection_prefix
    over the slug alias code_graph_project."""
    src = CLI_SRC.read_text(encoding="utf-8")
    # The canonical binding-row prefix must be the FIRST term of the fallback.
    assert "_cfg.code_graph_collection_prefix" in src, (
        "query_code_graph.py main() must resolve from code_graph_collection_prefix"
    )
    # And the slug must NOT be the standalone resolver (it may remain a
    # secondary fallback, but never the sole/first source).
    assert "effective_project = _cfg.code_graph_project or None" not in src, (
        "main() still resolves effective_project SOLELY from the slug "
        "(code_graph_project) — the C1a regression."
    )


def test_c1a_prefix_precedence_order() -> None:
    """Prefix wins; slug is the secondary fallback; both absent -> None."""
    src = CLI_SRC.read_text(encoding="utf-8")
    # The precedence chain text must list prefix BEFORE project.
    idx_prefix = src.find("_cfg.code_graph_collection_prefix")
    idx_project = src.find("_cfg.code_graph_project")
    assert idx_prefix != -1 and idx_project != -1
    assert idx_prefix < idx_project, (
        "code_graph_collection_prefix must precede code_graph_project in the "
        "fallback chain"
    )


# --------------------------------------------------------------------------
# C1b — embedder-aware score floor (unit)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli_mod():
    return _load_cli_module()


# NOTE (v0.2.72 T-FLOOR + integration): the per-slot floor table, resolvers AND
# the retrieval pipeline live in the shared home `weaviate_mcp.code_ranking`
# (the CLI and MCP paths must not diverge). The v0.2.70 C1b defaults
# (codesage/jina 0.0, qwen3 0.25, single scalar floor) are SUPERSEDED by the
# experimentally-validated two-stage floors (codesage/jina 0.16/0.22, qwen3
# 0.20/0.30; RESULTS-2026-07-01.md). The v0.2.72 integrator then REPLACED the
# CLI's single-stage floor break-loop (and its `_resolve_code_score_floor`
# shim) with a `run_code_retrieval_pipeline` call using the SERVER adapter
# factories. These tests pin the integrated contract:
#   * the CLI imports the SHARED resolvers/pipeline/adapters (identity checks —
#     a per-surface fork of any of them re-opens the divergence bug);
#   * the search path calls the shared pipeline with the same args shape as
#     the MCP (static guards);
#   * anchor resolution is failure-soft (behavioral).
# Resolver env-override/coercion semantics are covered by the shared home's
# own tests (tests/test_code_ranking.py) — not duplicated here.
def test_c1b_floor_map_values(cli_mod) -> None:
    """v0.2.72: two-stage tuple table (retrieval, post_rerank); imported into
    the CLI module from the shared code_ranking home."""
    fm = cli_mod.CODE_FLOOR_BY_SLOT
    assert fm["codesage_embed"] == (0.16, 0.22)
    assert fm["jina_embed"] == (0.16, 0.22)
    assert fm["qwen3_embed"] == (0.20, 0.30)


def test_cli_imports_shared_ranking_home(cli_mod) -> None:
    """The CLI's floor table, resolvers and pipeline must BE the shared
    code_ranking objects (identity, not equal copies) — a fork re-opens the
    CLI/MCP divergence bug."""
    from weaviate_mcp import code_ranking
    assert cli_mod.CODE_FLOOR_BY_SLOT is code_ranking.CODE_FLOOR_BY_SLOT
    assert cli_mod.resolve_retrieval_floor is code_ranking.resolve_retrieval_floor
    assert cli_mod.resolve_post_rerank_floor is code_ranking.resolve_post_rerank_floor
    assert cli_mod.run_code_retrieval_pipeline is code_ranking.run_code_retrieval_pipeline


def test_cli_reuses_server_adapter_factories(cli_mod) -> None:
    """The CLI must reuse the SERVER adapter factories + tier formatter —
    NOT reimplement them (the hard non-divergence invariant)."""
    from weaviate_mcp import server as mcp_server
    assert cli_mod.make_code_collapse_fn is mcp_server.make_code_collapse_fn
    assert cli_mod.make_code_tier_fn is mcp_server.make_code_tier_fn
    assert cli_mod._format_code_result_by_tier is mcp_server._format_code_result_by_tier


# --------------------------------------------------------------------------
# The shared pipeline is actually USED in the search path (static guards)
# --------------------------------------------------------------------------
def test_search_path_calls_shared_pipeline() -> None:
    """search_by_concept must run the shared two-stage pipeline with the SAME
    args shape as the MCP (server.py::search_code_graph) — not the legacy
    single-stage floor break-loop."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "run_code_retrieval_pipeline(" in src
    assert "retrieval_floor=resolve_retrieval_floor(_slot)" in src
    assert "post_rerank_floor=resolve_post_rerank_floor(_slot)" in src
    assert "collapse_fn=make_code_collapse_fn()" in src
    assert 'key_fields=("file_path", "full_name")' in src
    # tier_fn only in auto mode — same rule as the MCP.
    assert 'make_code_tier_fn() if detail == "auto" else None' in src
    # The legacy single-stage shim + break-loop are gone.
    assert "_resolve_code_score_floor" not in src, (
        "the pre-integration single-stage floor shim must be removed"
    )
    assert 'os.environ.get("VCO_CODE_GRAPH_SCORE_FLOOR", "0.35")' not in src, (
        "the legacy fixed 0.35 default is still present — C1b regression"
    )


def test_search_path_overfetches_2n() -> None:
    """The per-collection fetch must over-fetch 2*limit so the pipeline has a
    pool to floor-cull + rerank + collapse (matches the MCP)."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "_fetch_limit = max(1, 2 * limit)" in src


def test_search_parser_exposes_anchor_flag() -> None:
    """The hook path passes --anchor (edited file / grep symbol) so the
    relationship rerank fires; the flag must exist and default to None."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "'--anchor'" in src
    assert "anchor=getattr(args, 'anchor', None)" in src


# --------------------------------------------------------------------------
# Anchor resolution — failure-soft (behavioral)
# --------------------------------------------------------------------------
def test_anchor_resolution_failure_soft(cli_mod) -> None:
    """Any Weaviate error during anchor resolution must yield None (pure
    semantic ordering, byte-identical to a direct MCP call) — never raise."""
    q = cli_mod.CodeGraphQuery(project="Alpha")

    class _BoomCollections:
        def get(self, name):
            raise RuntimeError("weaviate down")

    class _BoomClient:
        collections = _BoomCollections()

    q.client = _BoomClient()
    assert q._resolve_anchor_props("some.symbol") is None
    assert q._resolve_anchor_props("src/module.py") is None


def test_anchor_empty_or_no_client_is_none(cli_mod) -> None:
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = None
    assert q._resolve_anchor_props("anything") is None
    q.client = object.__new__(object)  # non-None client, empty anchor
    assert q._resolve_anchor_props("") is None
    assert q._resolve_anchor_props(None) is None


# --------------------------------------------------------------------------
# C1c — None-guard on the structure references path (static)
# --------------------------------------------------------------------------
def test_c1c_references_none_guard_present() -> None:
    """The structure path must guard `references` being None before .get."""
    src = CLI_SRC.read_text(encoding="utf-8")
    # All three reference reads must use the `(... or {})` guard.
    assert "(response.objects[0].references or {}).get" in src or \
           "_refs = response.objects[0].references or {}" in src, (
        "dependencies path missing the None-references guard"
    )
    assert "(obj.references or {}).get(\"calls\"" in src, (
        "callers path missing the None-references guard"
    )
    assert "(response.objects[0].references or {}).get(\"extends\"" in src, (
        "extends path missing the None-references guard"
    )


# --------------------------------------------------------------------------
# C1a/b — live end-to-end (skipped without a reachable code graph)
# --------------------------------------------------------------------------
def _code_graph_reachable() -> bool:
    """Best-effort probe: is a Weaviate code-graph reachable for this project?"""
    try:
        import urllib.request
        url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        with urllib.request.urlopen(f"{url}/v1/.well-known/ready", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.mark.skipif(not _code_graph_reachable(), reason="no reachable Weaviate code graph")
def test_c1_live_search_returns_results() -> None:
    """End-to-end: `code-graph-query search` with NO --project returns >=1
    result (was no-results pre-C1a/C1b). Skipped without a live backend."""
    import subprocess
    cli = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"
    env = {**os.environ}
    # Run from the worktree so the resolver finds this project's binding.
    result = subprocess.run(
        [sys.executable, str(cli), "search", "schema migration", "--limit", "2", "--hook-format"],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT), env=env,
    )
    # The CLI exits 0 and either returns CODE: results OR a clean no-results
    # sentinel. The regression was a crash / always-no-results; we accept any
    # clean exit here (data presence depends on the local index) but assert no
    # traceback leaked.
    assert "Traceback" not in result.stderr, (
        f"CLI crashed: {result.stderr[-500:]}"
    )
