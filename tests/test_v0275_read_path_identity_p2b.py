# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 P2b — read-path identity pair (C-7 collapse + C-8 chunk interleave).

C-7 (collapse over-merge): ``make_code_collapse_fn``'s fallback identity for
CodeAPI/CodeInteraction was ``("", method+endpoint)`` — same-endpoint rows that
differ only in ``handler`` (APIs) or ``interaction_type``/``direction``/
``raw_target``/``protocol`` (interactions) collapsed to ONE survivor, silently
dropping real distinct edges. The fallback name now folds those discriminators
in, so distinct rows keep separate identities. Function/Class rows (which hit the
``full_name`` branch) are unaffected.

C-8 (cross-file chunk interleave): ``_fetch_code_chunks`` (MCP) and
``_code_chunk_fetcher`` (CLI) filtered ``full_name``+``project`` only, so two
same-``full_name`` entities in different files interleaved chunk bodies at the
three_chunks/full tiers. The winning row's ``file_path`` is now threaded through
the ONE shared formatter call site into both fetchers' filters — CLI≡MCP parity.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_REPO_ROOT / "claude_mcp_servers"))
sys.path.insert(0, str(_REPO_ROOT))

from weaviate_mcp import server as srv  # noqa: E402


def _cand(coll: str, props: dict, score: float) -> dict:
    return {"_c": coll, "_s": score, "_d": 1.0 - score, "_p": props, "_rerank": score}


# ─────────────────────── C-7: collapse identity ───────────────────────


def test_c7_interactions_differing_only_in_direction_both_survive():
    """Two CodeInteraction rows with the SAME endpoint but opposite direction are
    distinct edges — both must survive collapse (pre-fix: only one survived)."""
    rows = [
        _cand("CodeInteraction", {
            "endpoint": "/svc/rpc", "interaction_type": "grpc",
            "direction": "outbound", "raw_target": "svc.Call",
        }, 0.9),
        _cand("CodeInteraction", {
            "endpoint": "/svc/rpc", "interaction_type": "grpc",
            "direction": "inbound", "raw_target": "svc.Call",
        }, 0.8),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 2, "interactions differing only in direction must both survive"
    names = {r["full_name"] for r in out}
    assert len(names) == 2, f"identities must differ; got {names}"


def test_c7_interactions_differing_in_type_or_raw_target_survive():
    """interaction_type / raw_target are also discriminators."""
    rows = [
        _cand("CodeInteraction", {
            "endpoint": "/q", "interaction_type": "http", "direction": "outbound",
            "raw_target": "GET /q",
        }, 0.9),
        _cand("CodeInteraction", {
            "endpoint": "/q", "interaction_type": "queue", "direction": "outbound",
            "raw_target": "publish /q",
        }, 0.8),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 2


def test_c7_apis_differing_only_in_handler_both_survive():
    """Two CodeAPI rows same METHOD+endpoint but different handler → distinct."""
    rows = [
        _cand("CodeAPI", {"method": "GET", "endpoint": "/a", "handler": "h1",
                          "api_description": "d1"}, 0.9),
        _cand("CodeAPI", {"method": "GET", "endpoint": "/a", "handler": "h2",
                          "api_description": "d2"}, 0.8),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 2, "APIs differing only in handler must both survive"


def test_c7_identical_interaction_rows_still_collapse():
    """LEAVE-ALONE: genuinely identical interaction rows (same everything) still
    collapse to one — the fix must not over-split."""
    props = {
        "endpoint": "/svc/rpc", "interaction_type": "grpc",
        "direction": "outbound", "raw_target": "svc.Call",
    }
    rows = [_cand("CodeInteraction", dict(props), 0.9),
            _cand("CodeInteraction", dict(props), 0.5)]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 1, "identical interaction rows must still collapse"


def test_c7_function_class_rows_unaffected():
    """LEAVE-ALONE: Function/Class rows hit the full_name branch — same full_name
    same file still collapses; different files still both survive (F2 behaviour
    preserved)."""
    same = [
        _cand("CodeFunction", {"full_name": "m.f", "file_path": "a.py",
                               "function_body": "x"}, 0.9),
        _cand("CodeFunction", {"full_name": "m.f", "file_path": "a.py",
                               "function_body": "x"}, 0.5),
    ]
    assert len(srv.make_code_collapse_fn()(same)) == 1
    diff = [
        _cand("CodeFunction", {"full_name": "main.run", "file_path": "cli/main.py",
                               "function_body": "1"}, 0.9),
        _cand("CodeFunction", {"full_name": "main.run", "file_path": "worker/main.py",
                               "function_body": "2"}, 0.8),
    ]
    assert len(srv.make_code_collapse_fn()(diff)) == 2


# ─────────────────────── C-8: chunk fetcher file_path threading ───────────────────────


def test_c8_formatter_passes_file_path_to_fetcher():
    """The shared formatter must thread the winning row's file_path as the 5th
    fetcher arg so the fetcher can scope to the same source file."""
    seen = {}

    def _spy_fetcher(full_name, hit_chunk, total, max_chunks, file_path=""):
        seen["full_name"] = full_name
        seen["file_path"] = file_path
        return [
            {"function_body": "[chunk 1/2]\n\nfirst"},
            {"function_body": "[chunk 2/2]\n\nsecond"},
        ]

    props = {
        "full_name": "main.run",
        "file_path": "cli/main.py",
        "function_body": "[chunk 1/2]\n\nfirst",
        "chunk_num": 1,
        "total_chunks": 2,
    }
    out = srv._format_code_result_by_tier(
        props, "CodeFunction", "three_chunks", score=0.7, chunk_fetcher=_spy_fetcher,
    )
    assert seen["file_path"] == "cli/main.py", (
        "the winning row's file_path must reach the fetcher (C-8)"
    )
    assert seen["full_name"] == "main.run"
    assert out["chunks_shown"] == 2


def test_c8_fetcher_scopes_by_file_path_no_interleave():
    """A fetcher fixture that HONORS file_path returns only that file's chunks —
    two same-full_name entities in different files do not interleave."""
    # Two files, same full_name; the fetcher returns each file's own chunks.
    by_file = {
        "cli/main.py": [
            {"function_body": "[chunk 1/2]\n\ncli-first", "file_path": "cli/main.py"},
            {"function_body": "[chunk 2/2]\n\ncli-second", "file_path": "cli/main.py"},
        ],
        "worker/main.py": [
            {"function_body": "[chunk 1/2]\n\nworker-first", "file_path": "worker/main.py"},
            {"function_body": "[chunk 2/2]\n\nworker-second", "file_path": "worker/main.py"},
        ],
    }

    def _scoped_fetcher(full_name, hit_chunk, total, max_chunks, file_path=""):
        return list(by_file.get(file_path, []))

    props_cli = {
        "full_name": "main.run", "file_path": "cli/main.py",
        "function_body": "[chunk 1/2]\n\ncli-first", "chunk_num": 1, "total_chunks": 2,
    }
    out = srv._format_code_result_by_tier(
        props_cli, "CodeFunction", "full", score=0.9, chunk_fetcher=_scoped_fetcher,
    )
    body = out.get("function_body", "")
    assert "cli-first" in body and "cli-second" in body, "cli file's chunks assembled"
    assert "worker-first" not in body and "worker-second" not in body, (
        "the OTHER file's same-full_name chunks must NOT interleave (C-8)"
    )
