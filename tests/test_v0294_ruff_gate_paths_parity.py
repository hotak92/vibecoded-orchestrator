# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The two ruff gates must name the SAME paths (v0.2.94, lane G).

ruff runs in two places and they are meant to be one gate wearing two hats:

  * CI — ``.github/workflows/ci.yml``, job ``python-lint`` ("Python (ruff)").
  * pre-ship — ``scripts/pre-ship-check.sh``, Gate 3c.

If they drift, the cheap local gate stops predicting the expensive remote one,
which is precisely the failure mode ``pre-ship-check.sh`` exists to prevent
(its own Gate-3c comment says "the gate MUST match CI"). The drift is silent:
a path added to one list keeps both gates GREEN, so nothing surfaces until a
push goes red on a file the local gate never looked at.

WHAT THIS PINS
--------------
1. Both files still contain their ruff invocation at all (a rename/removal of
   either gate fails here rather than quietly halving the coverage).
2. The two path lists are byte-equal.
3. The list still contains ``tests`` — the v0.2.94 sweep cleared tests/'s 174
   pre-existing findings specifically so it could join the gate, and a silent
   removal would strand that work.

The assertions read the SHIPPED bytes of both files and extract the list from
the real command line — editing either file edits what this test measures, so
there is no third copy of the path list to keep in sync.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_SHIP = REPO_ROOT / "scripts" / "pre-ship-check.sh"

#: The CI job's step: ``        run: ruff check <paths>``
_CI_RUFF = re.compile(r"^\s*run:\s*ruff check (?P<paths>.+?)\s*$", re.MULTILINE)

#: Gate 3c's invocation: ``elif "$_RUFF_BIN" check <paths> > /tmp/...``
_PRE_SHIP_RUFF = re.compile(
    r'^\s*elif\s+"\$_RUFF_BIN"\s+check\s+(?P<paths>.+?)\s*>\s*/tmp/', re.MULTILINE
)


def _sole_match(pattern: "re.Pattern[str]", path: Path) -> str:
    """Return the single ``paths`` capture, failing loudly on 0 or 2+."""
    text = path.read_text(encoding="utf-8")
    found = pattern.findall(text)
    assert found, (
        f"no ruff invocation found in {path}; the gate was renamed or removed "
        f"— update this test together with it, do not delete the coverage"
    )
    assert len(found) == 1, (
        f"expected exactly one ruff invocation in {path}, found {len(found)}: "
        f"{found}"
    )
    return found[0]


def _paths(raw: str) -> list[str]:
    return raw.split()


@pytest.fixture(scope="module")
def ci_paths() -> list[str]:
    return _paths(_sole_match(_CI_RUFF, CI_WORKFLOW))


@pytest.fixture(scope="module")
def pre_ship_paths() -> list[str]:
    return _paths(_sole_match(_PRE_SHIP_RUFF, PRE_SHIP))


def test_both_gates_declare_a_path_list(ci_paths, pre_ship_paths) -> None:
    assert ci_paths, "CI's ruff step passed no paths"
    assert pre_ship_paths, "Gate 3c's ruff call passed no paths"


def test_ci_and_pre_ship_ruff_paths_are_identical(ci_paths, pre_ship_paths) -> None:
    assert ci_paths == pre_ship_paths, (
        "the ruff gates have drifted apart — pre-ship-check.sh Gate 3c no "
        f"longer predicts CI's Python (ruff) job.\n"
        f"  ci.yml         : {ci_paths}\n"
        f"  pre-ship-check : {pre_ship_paths}\n"
        "Edit both or neither."
    )


def test_tests_directory_is_gated(ci_paths, pre_ship_paths) -> None:
    """tests/ joined the gate in v0.2.94 — keep it there."""
    for name, paths in (("ci.yml", ci_paths), ("pre-ship-check.sh", pre_ship_paths)):
        assert "tests" in paths, (
            f"{name} stopped gating tests/. The v0.2.94 sweep cleared all 174 "
            f"findings so this could be gated; dropping it re-opens the hole. "
            f"Got: {paths}"
        )


def test_every_gated_path_exists() -> None:
    """A typo'd path makes ruff scan nothing and still exit 0 — a green gate
    that checks less than it claims. Pin that each entry resolves."""
    for entry in _paths(_sole_match(_CI_RUFF, CI_WORKFLOW)):
        assert (REPO_ROOT / entry).exists(), (
            f"ruff gate names {entry!r}, which does not exist in the repo"
        )
