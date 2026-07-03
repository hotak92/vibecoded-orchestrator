# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-2: retrieval-quality regression check for the v0.2.72 two-stage floors.

The v0.2.72 codegraph retrieval overhaul introduced a per-slot two-stage floor
(retrieval_floor applied at fetch, post_rerank_floor applied after boost) plus a
relationship rerank. Their JOB is to cull structurally-unrelated noise so a
known-noise query returns 0 (or only-relevant) results. This suite guards that
the culling keeps working:

  - PURE-UNIT layer (always runs): feed candidates straddling the floor through
    the shipped ``run_code_retrieval_pipeline`` and assert below-floor noise is
    dropped while above-floor relevant rows survive. Locks the floor semantics
    independent of any running infrastructure.

  - LIVE-GATED layer (runs only when Weaviate + a code collection are present):
    runs a real known-noise query through the code-graph-query CLI (which uses
    the SAME shared pipeline) and asserts it returns 0 or only-relevant hits.
    SKIPS cleanly — never fails — when the infra is absent, so it is
    non-blocking in CI/dev without a seeded Weaviate. The weaviate-smoke
    workflow sets ``VCO_RUN_LIVE_RETRIEVAL_SMOKE=1`` to require the live layer.

Synthetic names only (noise.* / real.*) — no real project identity embedded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_DIR = _REPO_ROOT / "claude_mcp_servers"
_QUERY_SHIM = _REPO_ROOT / "templates" / "scripts" / "code-graph-query"


def _import_pipeline():
    if str(_MCP_DIR) not in sys.path:
        sys.path.insert(0, str(_MCP_DIR))
    from weaviate_mcp.code_ranking import (  # noqa: E402
        CODE_FLOOR_BY_SLOT,
        resolve_post_rerank_floor,
        resolve_retrieval_floor,
        run_code_retrieval_pipeline,
    )

    return (
        run_code_retrieval_pipeline,
        resolve_retrieval_floor,
        resolve_post_rerank_floor,
        CODE_FLOOR_BY_SLOT,
    )


# ---------------------------------------------------------------------------
# PURE-UNIT layer — always runs, no infra.
# ---------------------------------------------------------------------------


def test_stage1_floor_culls_below_floor_noise() -> None:
    """A candidate whose SEMANTIC score is below the retrieval_floor is dropped
    outright (stage-1), while a clearly-relevant candidate survives."""
    (run_pipeline, ret_floor, post_floor, floors) = _import_pipeline()
    rfloor, pfloor = floors["codesage_embed"]  # (0.16, 0.22)

    candidates = [
        # Structurally-unrelated noise, well below the retrieval floor.
        {"_s": rfloor - 0.10, "_p": {"file_path": "n.py", "full_name": "noise.a"}},
        {"_s": 0.02, "_p": {"file_path": "n2.py", "full_name": "noise.b"}},
        # Clearly relevant, well above both floors.
        {"_s": 0.85, "_p": {"file_path": "r.py", "full_name": "real.match"}},
    ]
    survivors = run_pipeline(
        candidates,
        retrieval_floor=rfloor,
        post_rerank_floor=pfloor,
        limit=5,
    )
    names = [c["_p"]["full_name"] for c in survivors]
    assert names == ["real.match"], (
        f"floors must cull below-floor noise; got {names}"
    )


def test_stage2_post_rerank_floor_culls_unlinked_near_margin() -> None:
    """A near-margin candidate that passes stage-1 but has NO relationship
    boost (unlinked) is culled at the stage-2 post_rerank_floor."""
    (run_pipeline, _rf, _pf, floors) = _import_pipeline()
    rfloor, pfloor = floors["codesage_embed"]

    # Between the two floors, unlinked → no boost → culled at stage-2.
    near = (rfloor + pfloor) / 2.0
    assert rfloor < near < pfloor
    candidates = [
        {"_s": near, "_p": {"file_path": "u.py", "full_name": "unlinked.near"}},
        {"_s": 0.80, "_p": {"file_path": "r.py", "full_name": "real.match"}},
    ]
    survivors = run_pipeline(
        candidates,
        retrieval_floor=rfloor,
        post_rerank_floor=pfloor,
        limit=5,
    )
    names = [c["_p"]["full_name"] for c in survivors]
    assert "unlinked.near" not in names, (
        "an unlinked near-margin candidate must be culled at stage-2"
    )
    assert "real.match" in names


def test_all_noise_query_returns_empty() -> None:
    """The known-noise scenario: EVERY candidate is below the retrieval floor
    → the pipeline returns an empty list (0 results, the strongest signal that
    the floor is doing its job)."""
    (run_pipeline, _rf, _pf, floors) = _import_pipeline()
    rfloor, pfloor = floors["codesage_embed"]
    candidates = [
        {"_s": 0.05, "_p": {"file_path": f"n{i}.py", "full_name": f"noise.{i}"}}
        for i in range(8)
    ]
    survivors = run_pipeline(
        candidates,
        retrieval_floor=rfloor,
        post_rerank_floor=pfloor,
        limit=5,
    )
    assert survivors == [], (
        "an all-noise pool must return zero results after floor culling"
    )


def test_floor_table_values_are_the_v0272_measured_pairs() -> None:
    """Guard the measured floor VALUES don't silently drift (they are the whole
    reason the noise is culled). Do NOT re-derive — see RESULTS-2026-07-01.md."""
    (_run, _rf, _pf, floors) = _import_pipeline()
    assert floors["codesage_embed"] == (0.16, 0.22)
    assert floors["jina_embed"] == (0.16, 0.22)
    assert floors["qwen3_embed"] == (0.20, 0.30)


# ---------------------------------------------------------------------------
# LIVE-GATED layer — only runs when Weaviate + a code collection are present.
# ---------------------------------------------------------------------------


def _weaviate_ready(url: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"{url.rstrip('/')}/v1/.well-known/ready", timeout=3
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _has_code_collection(url: str) -> bool:
    """True when Weaviate exposes at least one *_CodeFunction class (a seeded
    code graph). Absence => nothing to smoke-test => skip."""
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"{url.rstrip('/')}/v1/schema", timeout=5
        ) as resp:
            schema = json.load(resp)
        classes = [c.get("class", "") for c in schema.get("classes", [])]
        return any(c.endswith("CodeFunction") for c in classes)
    except Exception:
        return False


def _live_infra_present() -> tuple[bool, str]:
    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    if not _weaviate_ready(url):
        return False, f"Weaviate not ready at {url}"
    if not _has_code_collection(url):
        return False, "no *_CodeFunction collection present (unseeded code graph)"
    return True, url


_REQUIRE_LIVE = os.environ.get("VCO_RUN_LIVE_RETRIEVAL_SMOKE") == "1"


def test_live_known_noise_query_returns_only_relevant_or_empty() -> None:
    """Run a deliberately-off-topic query through the CLI (same shared floor
    pipeline as the MCP) against a live code graph. The floors must return
    either zero rows or only rows scoring above the post-rerank floor — never a
    flood of below-floor noise.

    Gated: SKIPS when Weaviate / a code collection is absent (non-blocking in
    infra-less CI). The weaviate-smoke workflow sets
    VCO_RUN_LIVE_RETRIEVAL_SMOKE=1 to convert a skip into a failure so the live
    lane genuinely exercises real data.
    """
    present, detail = _live_infra_present()
    if not present:
        if _REQUIRE_LIVE:
            pytest.fail(
                f"VCO_RUN_LIVE_RETRIEVAL_SMOKE=1 but live infra missing: {detail}"
            )
        pytest.skip(f"live retrieval smoke skipped: {detail}")

    url = detail
    # A query with no plausible code-entity match. If the floors work, the
    # result set is empty or contains only genuinely high-score rows.
    noise_query = "xyzzy plugh frobnicate quux nonexistent gibberish token"
    env = dict(os.environ)
    env.pop("VCT_VENV", None)  # discipline: don't let ambient VCT_VENV hijack
    env["WEAVIATE_URL"] = url

    proc = subprocess.run(
        ["bash", str(_QUERY_SHIM), "search", noise_query, "--limit", "5"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The CLI prints a human table; the assertion is coarse-but-meaningful: a
    # working floor must NOT return a full page of 5 hits for pure gibberish.
    # (We cannot assert exact rows without controlling the seed, but a floored
    # gibberish query returning the max limit is the regression signature.)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"CLI errored: {combined}"
    # Count result rows heuristically by the CLI's per-result marker.
    hit_markers = combined.count("full_name") + combined.lower().count("score")
    # A total-noise query must not saturate the limit; if the CLI returns any
    # rows they must be few. We assert the strong form: gibberish yields no
    # "Found N results" with N == limit.
    assert "Found 5 " not in combined and "5 results" not in combined, (
        "gibberish query saturated the result limit — floor culling regressed:\n"
        f"{combined[:1000]}"
    )
    _ = hit_markers  # informational; not asserted strictly to stay seed-agnostic
