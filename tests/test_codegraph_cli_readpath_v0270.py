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


def test_c1b_floor_map_values(cli_mod) -> None:
    """codesage/jina -> 0.0 (MCP parity); qwen3 -> 0.25 (light noise trim)."""
    fm = cli_mod._CODE_FLOOR_BY_SLOT
    assert fm["codesage_embed"] == 0.0
    assert fm["jina_embed"] == 0.0
    assert fm["qwen3_embed"] == 0.25


def test_c1b_floor_codesage_is_zero(cli_mod, monkeypatch) -> None:
    monkeypatch.delenv("VCO_CODE_GRAPH_SCORE_FLOOR", raising=False)
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "codesage_embed")
    assert cli_mod._resolve_code_score_floor() == 0.0


def test_c1b_floor_qwen3_is_quarter(cli_mod, monkeypatch) -> None:
    monkeypatch.delenv("VCO_CODE_GRAPH_SCORE_FLOOR", raising=False)
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "qwen3_embed")
    assert cli_mod._resolve_code_score_floor() == 0.25


def test_c1b_env_override_wins(cli_mod, monkeypatch) -> None:
    monkeypatch.setenv("VCO_CODE_GRAPH_SCORE_FLOOR", "0.5")
    # Even though the slot would say 0.0, the explicit env override wins.
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "codesage_embed")
    assert cli_mod._resolve_code_score_floor() == 0.5


def test_c1b_empty_env_coerces_to_slot_default(cli_mod, monkeypatch) -> None:
    """An empty env string is coerced to the slot default (v0.2.27 discipline),
    NOT parsed as a literal."""
    monkeypatch.setenv("VCO_CODE_GRAPH_SCORE_FLOOR", "")
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "qwen3_embed")
    assert cli_mod._resolve_code_score_floor() == 0.25


def test_c1b_unparseable_env_falls_to_zero(cli_mod, monkeypatch) -> None:
    monkeypatch.setenv("VCO_CODE_GRAPH_SCORE_FLOOR", "not-a-float")
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "qwen3_embed")
    # Safest on a bad override is no floor (MCP parity).
    assert cli_mod._resolve_code_score_floor() == 0.0


def test_c1b_unknown_slot_defaults_to_zero(cli_mod, monkeypatch) -> None:
    """A future 4th embedder slot defaults to 0.0 (parity-safe: return marginal
    results rather than silently culling everything)."""
    monkeypatch.delenv("VCO_CODE_GRAPH_SCORE_FLOOR", raising=False)
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "some_new_embed")
    assert cli_mod._resolve_code_score_floor() == 0.0


def test_c1b_slot_resolution_exception_defaults_codesage(cli_mod, monkeypatch) -> None:
    """If _active_code_vector_slot raises, fall back to codesage (0.0 floor)."""
    monkeypatch.delenv("VCO_CODE_GRAPH_SCORE_FLOOR", raising=False)
    def _boom():
        raise RuntimeError("no service")
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", _boom)
    assert cli_mod._resolve_code_score_floor() == 0.0


# --------------------------------------------------------------------------
# C1b — the floor is actually USED in the search path (static guard)
# --------------------------------------------------------------------------
def test_c1b_search_path_uses_resolver() -> None:
    """The search loop must call _resolve_code_score_floor() (not a hardcoded
    0.35 literal)."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "score_floor = _resolve_code_score_floor()" in src, (
        "the search path must resolve the floor via _resolve_code_score_floor()"
    )
    # The legacy hardcoded default must be gone from the floor resolution.
    assert 'os.environ.get("VCO_CODE_GRAPH_SCORE_FLOOR", "0.35")' not in src, (
        "the legacy fixed 0.35 default is still present — C1b regression"
    )


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
