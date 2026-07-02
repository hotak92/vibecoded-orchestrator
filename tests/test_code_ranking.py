# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the shared code-retrieval ranking pipeline (v0.2.72 T-FLOOR).

Covers P1 (two-stage per-slot floor resolution) + P2 (relationship rerank
boost) + the shared ``run_code_retrieval_pipeline`` both entry points call.

Pure module → no Weaviate, no I/O; every case is a deterministic transform over
plain dicts. Env is injected as a Mapping so overrides are pinned per-test.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Resolve weaviate_mcp from THIS repo (ahead of any pip-editable install that
# may point at a different clone) — same shim as the sibling codegraph tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp.code_ranking import (  # noqa: E402
    BOOST_CALL_LINKED,
    BOOST_SAME_FILE,
    BOOST_SHARED_TYPE,
    CODE_FLOOR_BY_SLOT,
    RELATIONSHIP_BOOST_CAP,
    rerank_score,
    resolve_post_rerank_floor,
    resolve_retrieval_floor,
    run_code_retrieval_pipeline,
)


# ─── helpers ─────────────────────────────────────────────────────────────────
def _cand(score, *, full_name="", file_path="", call_names=None, type_uses=None, name=""):
    """Build a normalised candidate in the pipeline's {"_s", "_p", ...} shape."""
    props = {}
    if full_name:
        props["full_name"] = full_name
    if name:
        props["name"] = name
    if file_path:
        props["file_path"] = file_path
    if call_names is not None:
        props["call_names"] = call_names
    if type_uses is not None:
        props["type_uses"] = type_uses
    return {"_s": score, "_p": props}


# ─── P1: resolve_retrieval_floor ─────────────────────────────────────────────
def test_retrieval_floor_per_slot_defaults():
    # env={} isolates from the process env (env=None reads os.environ — see
    # test_env_none_reads_process_environ below).
    assert resolve_retrieval_floor("codesage_embed", {}) == 0.16
    assert resolve_retrieval_floor("jina_embed", {}) == 0.16
    assert resolve_retrieval_floor("qwen3_embed", {}) == 0.20


def test_retrieval_floor_unknown_slot_falls_back():
    # Unknown slot → default pair (mirrors the shipped codesage default).
    assert resolve_retrieval_floor("mystery_embed", {}) == 0.16


def test_env_none_reads_process_environ(monkeypatch):
    """env=None (the default at BOTH the MCP and CLI call sites) must read the
    PROCESS environment — this is how the launcher-projected
    VCO_CODE_GRAPH_* overrides (GUI → app_state → config_projection →
    settings.json env → subprocess env) actually reach the search path.
    Regression guard: pre-fix, env=None silently ignored the override and the
    GUI floor settings were inert."""
    monkeypatch.setenv("VCO_CODE_GRAPH_RETRIEVAL_FLOOR", "0.31")
    monkeypatch.setenv("VCO_CODE_GRAPH_POST_RERANK_FLOOR", "0.44")
    assert resolve_retrieval_floor("codesage_embed") == 0.31
    assert resolve_post_rerank_floor("codesage_embed") == 0.44
    # And explicit {} still isolates (defaults, ignoring the process env).
    assert resolve_retrieval_floor("codesage_embed", {}) == 0.16
    assert resolve_post_rerank_floor("codesage_embed", {}) == 0.22


def test_retrieval_floor_env_override_wins():
    env = {"VCO_CODE_GRAPH_RETRIEVAL_FLOOR": "0.42"}
    assert resolve_retrieval_floor("codesage_embed", env) == 0.42


def test_retrieval_floor_empty_string_coerces_to_default():
    # v0.2.27 discipline: "" is NOT parsed as literal 0.0; fall through.
    env = {"VCO_CODE_GRAPH_RETRIEVAL_FLOOR": ""}
    assert resolve_retrieval_floor("qwen3_embed", env) == 0.20
    env_ws = {"VCO_CODE_GRAPH_RETRIEVAL_FLOOR": "   "}
    assert resolve_retrieval_floor("qwen3_embed", env_ws) == 0.20


def test_retrieval_floor_unparseable_falls_through():
    env = {"VCO_CODE_GRAPH_RETRIEVAL_FLOOR": "not-a-number"}
    assert resolve_retrieval_floor("codesage_embed", env) == 0.16


# ─── P1: resolve_post_rerank_floor ───────────────────────────────────────────
def test_post_rerank_floor_per_slot_defaults():
    # env={} isolates from the process env (env=None reads os.environ).
    assert resolve_post_rerank_floor("codesage_embed", {}) == 0.22
    assert resolve_post_rerank_floor("jina_embed", {}) == 0.22
    assert resolve_post_rerank_floor("qwen3_embed", {}) == 0.30


def test_post_rerank_floor_env_override_wins():
    env = {"VCO_CODE_GRAPH_POST_RERANK_FLOOR": "0.5"}
    assert resolve_post_rerank_floor("codesage_embed", env) == 0.5


def test_post_rerank_floor_empty_string_coerces_to_default():
    env = {"VCO_CODE_GRAPH_POST_RERANK_FLOOR": ""}
    assert resolve_post_rerank_floor("codesage_embed", env) == 0.22


def test_deprecated_alias_maps_to_post_rerank():
    # Legacy single-floor key maps to the post-rerank gate.
    env = {"VCO_CODE_GRAPH_SCORE_FLOOR": "0.33"}
    assert resolve_post_rerank_floor("codesage_embed", env) == 0.33
    # ...and does NOT affect the retrieval floor.
    assert resolve_retrieval_floor("codesage_embed", env) == 0.16


def test_canonical_post_rerank_key_wins_over_deprecated_alias():
    env = {
        "VCO_CODE_GRAPH_POST_RERANK_FLOOR": "0.40",
        "VCO_CODE_GRAPH_SCORE_FLOOR": "0.33",
    }
    assert resolve_post_rerank_floor("codesage_embed", env) == 0.40


def test_floor_table_shape():
    # C-H1: per-slot table preserved, each a two-stage (retrieval, post) pair.
    for slot, pair in CODE_FLOOR_BY_SLOT.items():
        assert isinstance(pair, tuple) and len(pair) == 2, slot
        assert pair[0] <= pair[1], f"{slot}: retrieval floor must be <= post-rerank"


# ─── P2: rerank_score ────────────────────────────────────────────────────────
def test_no_anchor_returns_zero_empty():
    delta, signals = rerank_score({"full_name": "mod.foo"}, None)
    assert delta == 0.0
    assert signals == {}


def test_call_linked_candidate_leaf_in_anchor_calls():
    # anchor CALLS the candidate (candidate.leaf ∈ anchor.call_names).
    anchor = {"full_name": "mod.caller", "call_names": ["foo", "bar"]}
    cand = {"full_name": "mod.foo"}
    delta, signals = rerank_score(cand, anchor)
    assert delta == BOOST_CALL_LINKED
    assert signals["call_linked"] is True


def test_call_linked_anchor_leaf_in_candidate_calls():
    # candidate CALLS the anchor (anchor.leaf ∈ candidate.call_names).
    anchor = {"full_name": "mod.target"}
    cand = {"full_name": "mod.other", "call_names": ["target", "baz"]}
    delta, signals = rerank_score(cand, anchor)
    assert delta == BOOST_CALL_LINKED
    assert signals["call_linked"] is True


def test_call_linked_rust_double_colon_leaf():
    anchor = {"full_name": "server::start_hub", "call_names": ["init_db"]}
    cand = {"full_name": "db::init_db"}
    delta, signals = rerank_score(cand, anchor)
    assert signals["call_linked"] is True
    assert delta == BOOST_CALL_LINKED


def test_unlinked_gets_zero_boost():
    anchor = {"full_name": "mod.caller", "call_names": ["foo"]}
    cand = {"full_name": "other.unrelated"}
    delta, signals = rerank_score(cand, anchor)
    assert delta == 0.0
    assert signals["call_linked"] is False
    assert signals["same_file"] is False
    assert signals["shared_type"] is False


def test_same_file_boost_independent():
    anchor = {"full_name": "a.x", "file_path": "src/svc.py"}
    cand = {"full_name": "b.y", "file_path": "src/svc.py"}
    delta, signals = rerank_score(cand, anchor)
    assert delta == BOOST_SAME_FILE
    assert signals["same_file"] is True


def test_same_file_empty_paths_do_not_match():
    anchor = {"full_name": "a.x", "file_path": ""}
    cand = {"full_name": "b.y", "file_path": ""}
    delta, signals = rerank_score(cand, anchor)
    assert signals["same_file"] is False
    assert delta == 0.0


def test_shared_type_boost_independent():
    anchor = {"full_name": "a.x", "type_uses": ["Widget", "Config"]}
    cand = {"full_name": "b.y", "type_uses": ["Config", "Other"]}
    delta, signals = rerank_score(cand, anchor)
    assert delta == BOOST_SHARED_TYPE
    assert signals["shared_type"] is True


def test_boost_cap_respected():
    # All three signals fire → 0.05 + 0.03 + 0.02 = 0.10 → capped at 0.08.
    anchor = {
        "full_name": "mod.caller",
        "file_path": "src/svc.py",
        "call_names": ["foo"],
        "type_uses": ["Widget"],
    }
    cand = {
        "full_name": "mod.foo",
        "file_path": "src/svc.py",
        "type_uses": ["Widget"],
    }
    delta, signals = rerank_score(cand, anchor)
    assert delta == RELATIONSHIP_BOOST_CAP
    assert signals["capped"] is True
    assert signals["call_linked"] and signals["same_file"] and signals["shared_type"]


def test_boost_sum_below_cap_not_capped():
    anchor = {"full_name": "a.x", "file_path": "src/svc.py", "type_uses": ["W"]}
    cand = {"full_name": "b.y", "file_path": "src/svc.py", "type_uses": ["W"]}
    delta, signals = rerank_score(cand, anchor)
    assert delta == BOOST_SAME_FILE + BOOST_SHARED_TYPE  # 0.05, below cap
    assert signals["capped"] is False


# ─── run_code_retrieval_pipeline ─────────────────────────────────────────────
def test_retrieval_floor_drops_subfloor():
    cands = [_cand(0.60, full_name="a.hit"), _cand(0.10, full_name="b.noise")]
    out = run_code_retrieval_pipeline(
        cands, retrieval_floor=0.16, post_rerank_floor=0.22, limit=10
    )
    names = [c["_p"]["full_name"] for c in out]
    assert names == ["a.hit"]


def test_linked_near_margin_rescued_over_post_floor():
    # 0.19 semantic is above retrieval_floor 0.16 but below post_rerank 0.22.
    # A +0.05 call-link boost lifts it to 0.24 → survives.
    anchor = {"full_name": "mod.caller", "call_names": ["foo"]}
    cands = [_cand(0.19, full_name="mod.foo")]
    out = run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        anchor_props=anchor,
        limit=10,
    )
    assert len(out) == 1
    assert out[0]["_rerank"] == 0.19 + BOOST_CALL_LINKED
    assert out[0]["_boost"]["signals"]["call_linked"] is True


def test_unlinked_near_margin_culled_by_post_floor():
    # 0.19 semantic, no boost (unlinked) → reranked 0.19 < 0.22 → culled.
    anchor = {"full_name": "mod.caller", "call_names": ["foo"]}
    cands = [_cand(0.19, full_name="other.unrelated")]
    out = run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        anchor_props=anchor,
        limit=10,
    )
    assert out == []


def test_noise_pool_stays_empty_even_with_anchor():
    # All below retrieval_floor → dropped at stage 1, boost never applies.
    anchor = {"full_name": "mod.caller", "call_names": ["foo", "bar"]}
    cands = [
        _cand(0.10, full_name="mod.foo", call_names=["mod.caller"]),
        _cand(0.05, full_name="mod.bar"),
    ]
    out = run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        anchor_props=anchor,
        limit=10,
    )
    assert out == []


def test_overfetch_trimmed_to_limit():
    # 2N candidates all clear both floors → trimmed to N, highest scores kept.
    cands = [_cand(0.90 - i * 0.02, full_name=f"m.f{i}") for i in range(20)]
    out = run_code_retrieval_pipeline(
        cands, retrieval_floor=0.16, post_rerank_floor=0.22, limit=10
    )
    assert len(out) == 10
    # Highest-scored survive; sorted desc.
    scores = [c["_rerank"] for c in out]
    assert scores == sorted(scores, reverse=True)
    assert out[0]["_p"]["full_name"] == "m.f0"


def test_dedup_by_key_fields():
    # Same (file_path, full_name) twice → collapsed, highest reranked kept.
    cands = [
        _cand(0.50, full_name="m.dup", file_path="a.py"),
        _cand(0.70, full_name="m.dup", file_path="a.py"),
        _cand(0.60, full_name="m.other", file_path="a.py"),
    ]
    out = run_code_retrieval_pipeline(
        cands, retrieval_floor=0.16, post_rerank_floor=0.22, limit=10
    )
    names = [c["_p"]["full_name"] for c in out]
    assert names.count("m.dup") == 1
    dup = next(c for c in out if c["_p"]["full_name"] == "m.dup")
    assert dup["_rerank"] == 0.70  # the higher-scored duplicate won


def test_sort_stable_ties_keep_input_order():
    cands = [
        _cand(0.50, full_name="m.first"),
        _cand(0.50, full_name="m.second"),
    ]
    out = run_code_retrieval_pipeline(
        cands, retrieval_floor=0.16, post_rerank_floor=0.22, limit=10
    )
    assert [c["_p"]["full_name"] for c in out] == ["m.first", "m.second"]


def test_collapse_fn_injection_honored():
    called = {}

    def collapse(results):
        called["collapse"] = list(results)
        # Drop the last row to prove the pipeline uses the returned list.
        return results[:-1]

    cands = [_cand(0.9, full_name="m.a"), _cand(0.8, full_name="m.b"), _cand(0.7, full_name="m.c")]
    out = run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        limit=10,
        collapse_fn=collapse,
    )
    assert "collapse" in called
    assert len(called["collapse"]) == 3  # sees all survivors, pre-trim
    assert [c["_p"]["full_name"] for c in out] == ["m.a", "m.b"]


def test_tier_fn_injection_honored():
    def tier(results):
        for i, r in enumerate(results):
            r["_tier"] = "full" if i == 0 else "ref"
        return results

    cands = [_cand(0.9, full_name="m.a"), _cand(0.8, full_name="m.b")]
    out = run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        limit=10,
        tier_fn=tier,
    )
    assert out[0]["_tier"] == "full"
    assert out[1]["_tier"] == "ref"


def test_collapse_runs_before_tier():
    order = []

    def collapse(results):
        order.append("collapse")
        return results

    def tier(results):
        order.append("tier")
        return results

    cands = [_cand(0.9, full_name="m.a")]
    run_code_retrieval_pipeline(
        cands,
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        limit=10,
        collapse_fn=collapse,
        tier_fn=tier,
    )
    assert order == ["collapse", "tier"]


def test_pipeline_preserves_extra_candidate_keys():
    cands = [{"_s": 0.9, "_p": {"full_name": "m.a"}, "_c": "CodeFunction", "_d": 0.1}]
    out = run_code_retrieval_pipeline(
        cands, retrieval_floor=0.16, post_rerank_floor=0.22, limit=10
    )
    assert out[0]["_c"] == "CodeFunction"
    assert out[0]["_d"] == 0.1
    assert "_rerank" in out[0] and "_boost" in out[0]
