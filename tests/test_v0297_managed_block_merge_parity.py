# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``.claude/env`` managed-block splice, pinned case by case (v0.2.97).

``config_projection._merge_managed_block`` (what ``apply`` writes with) is
pinned to ``tests/fixtures/managed_block_merge_parity.json``. It had a Rust
mirror (``projects_v2::merge_claude_env_managed_block``) whose only caller was
the unregister strip; review R6 retired both — the unregister's ``.claude/env``
strip is ``vco_lib.unregister_env`` — so the fixture now pins the one splice.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import config_projection as cp

FIXTURE = Path(__file__).parent / "fixtures" / "managed_block_merge_parity.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_uses_the_real_markers():
    assert DATA["_format_version"] == 1
    assert DATA["begin_marker"] == cp.CLAUDE_ENV_MANAGED_BEGIN
    assert DATA["end_marker"] == cp.CLAUDE_ENV_MANAGED_END
    assert len(DATA["cases"]) >= 10


@pytest.mark.parametrize("case", DATA["cases"], ids=[c["name"] for c in DATA["cases"]])
def test_python_merge_matches_the_shared_fixture(case):
    assert cp._merge_managed_block(case["prior"], case["managed"]) == case["expected"]
