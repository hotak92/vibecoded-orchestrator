# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.envfile — the shared managed-env / dotenv line parser.

v0.2.84 fix-pass (one concern, one home): `install_weaviate._managed_env_value`
and `agent_secrets._parse_dotenv_value` had grown a byte-identical line loop;
they now share `vco_lib.envfile`. These tests pin the shared parser's contract
directly (CRLF-safety, quote stripping, `export` prefix, comment/blank skip,
managed-block scoping, first-match-wins) so a future edit can't silently drift
either caller's behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import envfile  # noqa: E402

BEGIN = "# vco-managed-begin"
END = "# vco-managed-end"


# ── parse_env_lines: line-level rule ──────────────────────────────────────

def test_parse_plain_assignment():
    assert list(envfile.parse_env_lines("KEY=value\n")) == [("KEY", "value")]


def test_parse_export_prefix_stripped():
    assert list(envfile.parse_env_lines('export KEY="value"\n')) == [("KEY", "value")]


def test_double_and_single_quotes_stripped():
    lines = list(envfile.parse_env_lines('A="dq"\nB=\'sq\'\nC=bare\n'))
    assert lines == [("A", "dq"), ("B", "sq"), ("C", "bare")]


def test_only_one_quote_pair_stripped():
    # A value like ""x"" loses exactly one outer pair.
    assert list(envfile.parse_env_lines('K=""x""\n')) == [("K", '"x"')]


def test_blank_and_comment_lines_skipped():
    text = "\n# a comment\n   \nKEY=v\n# trailing\n"
    assert list(envfile.parse_env_lines(text)) == [("KEY", "v")]


def test_lines_without_equals_skipped():
    assert list(envfile.parse_env_lines("NOTANASSIGNMENT\nKEY=v\n")) == [("KEY", "v")]


def test_first_equals_is_the_split():
    assert list(envfile.parse_env_lines("URL=http://x?a=1&b=2\n")) == [
        ("URL", "http://x?a=1&b=2"),
    ]


def test_key_whitespace_stripped():
    assert list(envfile.parse_env_lines("  export   KEY = \"v\"  \n")) == [("KEY", "v")]


def test_crlf_parses_identically_to_lf():
    lf = list(envfile.parse_env_lines('export KEY="v"\n'))
    crlf = list(envfile.parse_env_lines('export KEY="v"\r\n'))
    assert lf == crlf == [("KEY", "v")]


# ── extract_managed_block ─────────────────────────────────────────────────

def test_extract_block_present():
    text = f'prefix\n{BEGIN}\nexport K="v"\n{END}\nsuffix\n'
    block = envfile.extract_managed_block(text, BEGIN, END)
    assert block is not None
    assert BEGIN in block
    assert 'export K="v"' in block
    assert END not in block  # excludes the end marker onward
    assert "suffix" not in block


def test_extract_block_absent_returns_none():
    assert envfile.extract_managed_block("no markers here", BEGIN, END) is None


def test_extract_block_end_before_begin_returns_none():
    text = f'{END}\nstuff\n{BEGIN}\n'
    assert envfile.extract_managed_block(text, BEGIN, END) is None


def test_extract_block_empty_markers_returns_none():
    assert envfile.extract_managed_block(f"{BEGIN}\n{END}", "", END) is None
    assert envfile.extract_managed_block(f"{BEGIN}\n{END}", BEGIN, "") is None


# ── parse_managed_env_lines: block scoping ────────────────────────────────

def test_managed_scoped_ignores_out_of_block():
    text = (
        'export OUTSIDE="leak"\n'
        f'{BEGIN}\nexport INSIDE="v"\n{END}\n'
        'export ALSO_OUT="leak2"\n'
    )
    pairs = list(envfile.parse_managed_env_lines(text, begin_marker=BEGIN, end_marker=END))
    keys = [k for k, _ in pairs]
    assert "INSIDE" in keys
    assert "OUTSIDE" not in keys
    assert "ALSO_OUT" not in keys


def test_managed_absent_block_yields_nothing():
    text = 'export OUTSIDE="leak"\n'  # no markers
    assert list(envfile.parse_managed_env_lines(text, begin_marker=BEGIN, end_marker=END)) == []


def test_managed_no_markers_parses_whole_text():
    text = 'export A="1"\nB=2\n'  # bare .env (no block) → whole file
    assert list(envfile.parse_managed_env_lines(text)) == [("A", "1"), ("B", "2")]


# ── env_value: first-match-wins key lookup ────────────────────────────────

def test_env_value_first_match_wins():
    text = 'K="first"\nK="second"\n'
    assert envfile.env_value(text, "K") == "first"


def test_env_value_missing_key_returns_none():
    assert envfile.env_value("A=1\n", "MISSING") is None


def test_env_value_scoped_to_block():
    text = f'K="outside"\n{BEGIN}\nK="inside"\n{END}\n'
    # Without scoping → whole-text first match = outside.
    assert envfile.env_value(text, "K") == "outside"
    # With scoping → only the managed block is consulted = inside.
    assert envfile.env_value(text, "K", begin_marker=BEGIN, end_marker=END) == "inside"


def test_env_value_scoped_missing_block_returns_none():
    assert envfile.env_value('K="v"\n', "K", begin_marker=BEGIN, end_marker=END) is None


def test_env_value_crlf_managed_block():
    text = f'{BEGIN}\r\nexport KG_COLLECTION="Crlf_KG"\r\n{END}\r\n'
    assert envfile.env_value(
        text, "KG_COLLECTION", begin_marker=BEGIN, end_marker=END,
    ) == "Crlf_KG"
