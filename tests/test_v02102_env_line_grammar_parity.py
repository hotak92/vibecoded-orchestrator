# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Python half of the env-line grammar parity table (v0.2.102 W6-Z1).

``tests/fixtures/env_line_grammar_parity.json`` is the ONE case table for the
whole-file env-line grammar. Python owns the rule (``vco_lib/envfile.py``);
the Rust reader ``vct-launcher-core/src/services/env_file.rs`` mirrors it and
its own test (``env_line_grammar_parity_table``) walks the SAME table. These
tests pin the Python side to it, so neither side can drift without one of the
two suites going red.

They also pin the table's managed-block text to the CURRENT writer
(``config_projection._build_managed_block``), so the Rust test reads real
managed-block bytes, not a hand-written imitation that the writer has moved
away from.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import envfile  # noqa: E402
from vco_lib.config_projection import _build_managed_block  # noqa: E402

TABLE_PATH = REPO_ROOT / "tests" / "fixtures" / "env_line_grammar_parity.json"
TABLE = json.loads(TABLE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", TABLE["lines"], ids=lambda c: repr(c["line"]))
def test_parse_env_line_matches_the_table(case):
    got = envfile.parse_env_line(case["line"])
    assert (list(got) if got is not None else None) == case["expect"]


@pytest.mark.parametrize("case", TABLE["files"], ids=lambda c: c["name"])
def test_last_wins_lookup_matches_the_table(case):
    assert envfile.env_value(case["text"], case["key"], last_wins=True) == case["expect_last"]


def test_the_table_carries_the_writers_current_managed_block():
    block = _build_managed_block(TABLE["managed_block_env"])
    carrying = [f["name"] for f in TABLE["files"] if block in f["text"]]
    assert carrying, (
        "no files case contains the current _build_managed_block output — "
        "regenerate the managed-block cases in " + str(TABLE_PATH))
    assert "override_after_managed_block" in carrying


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize(
    "case",
    [c for c in TABLE["files"] if c["name"] != "crlf"],  # bash keeps a CRLF line's \r
    ids=lambda c: c["name"],
)
def test_last_wins_lookup_is_what_bash_source_gives(tmp_path, case):
    env_file = tmp_path / "env"
    env_file.write_text(case["text"], encoding="utf-8")
    out = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         'unset "$2"; source "$1" >/dev/null 2>&1; '
         'if [ -n "${!2+x}" ]; then printf "set:%s" "${!2}"; else printf unset; fi',
         "_", str(env_file), case["key"]],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    want = case["expect_last"]
    assert out == ("unset" if want is None else f"set:{want}")
