# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — ONE reader for the ``.claude/env`` managed block.

There were two: ``vco_lib.envfile.env_value`` (used by the orphan-collection
check) and ``config_projection._read_managed_env_canonical_value`` (the
projection's own read-back, used by the repoint audit and the env
verifiers). They disagreed on the one escape the block writer applies — a
``"`` in a value is written as ``\\"`` — and on which END marker closes the
block. The projection's reader is now a thin binding of the file and the
markers to ``envfile.env_value``, and every value the writer can emit reads
back exactly, through every entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import config_projection as cp  # noqa: E402
from vco_lib import install_weaviate as iw  # noqa: E402
from vco_lib.envfile import env_value  # noqa: E402

BEGIN, END = cp.CLAUDE_ENV_MANAGED_BEGIN, cp.CLAUDE_ENV_MANAGED_END

VALUES = [
    "plain",
    'with "quotes" inside',
    'ends with a quote"',
    "C:\\Users\\me\\proj",
    'a\\"b',
    "trailing backslash\\",
    "${VCO_LOG_LEVEL:-info}",
    "spaces  inside",
    "",
]


def _readers(tmp_path: Path, text: str, key: str) -> dict:
    env_file = tmp_path / "env"
    env_file.write_text(text, encoding="utf-8")
    return {
        "envfile": env_value(text, key, begin_marker=BEGIN, end_marker=END),
        "config_projection": cp._read_managed_env_canonical_value(env_file, key),
        "install_weaviate": iw._managed_env_value(text, key),
    }


@pytest.mark.parametrize("value", VALUES)
def test_every_value_the_writer_emits_reads_back_exactly_everywhere(tmp_path, value):
    """RED before for the quoted values: envfile / install_weaviate returned
    the writer's escaped form (``with \\"quotes\\" inside``)."""
    text = 'export MY_OWN="kept"\n' + cp._build_managed_block({"K": value})
    got = _readers(tmp_path, text, "K")
    assert got == {name: value for name in got}, got


def test_the_block_is_closed_by_the_first_end_after_begin(tmp_path):
    """A stray END marker BEFORE the block (e.g. from a hand edit) must not
    hide the block. RED before for envfile: it searched END from the start."""
    text = f"{END}\n# notes\n" + cp._build_managed_block({"K": "v"})
    assert set(_readers(tmp_path, text, "K").values()) == {"v"}


def test_values_outside_the_block_are_never_read(tmp_path):
    text = 'export K="user"\n' + cp._build_managed_block({"OTHER": "x"})
    assert set(_readers(tmp_path, text, "K").values()) == {None}


def test_a_whole_file_dotenv_read_does_not_unescape():
    """LEAVE-ALONE: without markers (a ``.env``, the tier-3 secret read) the
    value is taken as the shell/PowerShell dotenv resolvers take it — one
    quote pair stripped, no escape processing."""
    assert env_value('K="a\\"b"\n', "K") == 'a\\"b'
