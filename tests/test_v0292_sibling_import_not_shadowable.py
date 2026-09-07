# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Sibling imports must not be shadowable by a second checkout on `sys.path`.

Found live (v0.2.92): `code_truncation` and `kg_chunk_plan` both imported
`claude_mcp_servers.weaviate_mcp.chunking` by its ABSOLUTE path first, with the
sibling `.chunking` only as an `ImportError` fallback. The absolute path
resolves against whatever `sys.path` offers — and this repo's dev box has a
second orchestrator checkout on it, whose `MODEL_TOKEN_LIMITS` still carries
`codesage-large-v2: 2048` against this tree's measured 1024.

The consequence is not a crash but a silent swap of a constant: the code-entity
budget reverts to 2.1x the served window and entities are halved again at
HTTP 200. That is the W1 defect restored by an import line, one screen below
its fix. The same order also reintroduced the W8 chunk-plan divergence
(qwen3 max 13 500 vs 8 192).

A sibling import can only ever resolve inside THIS package, so it cannot be
shadowed. This test proves the resolution ORDER behaviourally — by planting a
hostile module at the absolute name and checking the real value still wins —
rather than by scanning the source for an import line, which a comment would
satisfy.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ABSOLUTE = "claude_mcp_servers.weaviate_mcp.chunking"
_HOSTILE_LIMIT = 999_999
_HOSTILE_RATIO = 99.0


@pytest.fixture
def hostile_absolute_chunking():
    """Plant a module at the ABSOLUTE name carrying obviously-wrong values.

    Mimics a second checkout earlier on `sys.path`. Restored afterwards.
    """
    real = sys.modules.get(_ABSOLUTE)
    fake = types.ModuleType(_ABSOLUTE)
    fake.CHARS_PER_TOKEN_CODE = _HOSTILE_RATIO
    fake.MODEL_TOKEN_LIMITS = {"codesage-large-v2": _HOSTILE_LIMIT}
    fake._num_ctx_for_model = lambda _m: _HOSTILE_LIMIT
    sys.modules[_ABSOLUTE] = fake
    try:
        yield
    finally:
        if real is not None:
            sys.modules[_ABSOLUTE] = real
        else:
            sys.modules.pop(_ABSOLUTE, None)
        importlib.reload(
            importlib.import_module("weaviate_mcp.code_truncation")
        )


def test_code_truncation_budget_ignores_a_hostile_absolute_chunking(
    hostile_absolute_chunking,
):
    """`_max_chars_for_model` must read THIS package's token table."""
    mod = importlib.reload(importlib.import_module("weaviate_mcp.code_truncation"))
    budget = mod._max_chars_for_model("codesage-large-v2")

    assert budget < 100_000, (
        f"budget {budget} came from the planted module — the absolute import "
        "won, so another checkout on sys.path can silently restore the "
        "over-budgeting W1 removed"
    )
    from weaviate_mcp import chunking as real_chunking

    expected = int(
        real_chunking._num_ctx_for_model("codesage-large-v2")
        * real_chunking.CHARS_PER_TOKEN_CODE
    )
    assert budget == expected


def test_the_hostile_fixture_would_actually_change_the_answer():
    """Anti-vacuity: if the planted values could not move the budget, the test
    above would pass no matter which import won."""
    from weaviate_mcp import chunking as real_chunking

    real = int(
        real_chunking._num_ctx_for_model("codesage-large-v2")
        * real_chunking.CHARS_PER_TOKEN_CODE
    )
    hostile = int(_HOSTILE_LIMIT * _HOSTILE_RATIO)
    assert hostile != real and hostile > 100_000
