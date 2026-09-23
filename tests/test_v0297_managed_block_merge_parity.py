# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Python side of the managed-block splice parity (v0.2.97).

``config_projection._merge_managed_block`` (what ``apply`` writes with) and
the Rust ``projects_v2::merge_claude_env_managed_block`` (the one deliberate
mirror, used by the launcher's unregister strip) must splice ``.claude/env``
identically. Both are pinned to ``tests/fixtures/managed_block_merge_parity.json``;
the Rust ``#[test] managed_block_merge_matches_the_shared_parity_fixture``
reads the same file, so a change on either side fails one of the two.
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
