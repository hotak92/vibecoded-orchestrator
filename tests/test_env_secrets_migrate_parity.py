# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity test for the `.env` secret auditor + sentinel rewriter.

v0.2.75 Part 7 created ``launcher/src-tauri/src/commands/env_secrets_migrate.rs``
as a MUST-MATCH Rust mirror of ``vco_lib/secrets_audit.py`` — ~250 lines of
security-sensitive `.env` parsing / sentinel-rewriting that the launcher GUI
"Migrate from .env" button runs in-process (the install.py CLI arm uses the
Python module). A `.env` migrated from the GUI and one migrated from the CLI
must produce byte-identical results.

Comment-only "MUST MATCH" parity is a known fork risk (the B-3 lesson: a
comment doesn't fail CI when one side drifts). This test pins the two
implementations against a SHARED fixture — the same one-fixture-two-runners
shape as ``tests/test_kg_sanitizer_parity.py`` /
``tests/test_project_naming_parity.py``. The matching Rust ``#[test]``
``env_secrets_parity_matches_shared_fixture`` in ``env_secrets_migrate.rs``
consumes the SAME ``tests/fixtures/env_secrets_parity.json``; a divergence in
either the auditor or the rewriter fails one of the two runners.

The fixture's tricky cases (each named): quoted vs unquoted values; the
quoted-value-drops-trailing-comment asymmetry Part 7 discovered; placeholder /
already-migrated / empty values; first-occurrence-only duplicate replacement;
CRLF line endings (normalised to LF on both sides — Python via file-read
universal-newlines, Rust via ``str::lines()``); no-trailing-newline
preservation; ``export`` prefix + odd spacing; and the ``KEY`` / ``_KEY`` suffix
rule (with ``MONKEY`` / ``PYTHONPATH`` NOT flagged).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib.secrets_audit import audit_env_secrets, rewrite_env_with_sentinels


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "env_secrets_parity.json"


def _load_fixture() -> dict:
    assert FIXTURE_PATH.exists(), (
        f"Parity fixture missing: {FIXTURE_PATH} — shared with the Rust "
        "#[test] env_secrets_parity_matches_shared_fixture in "
        "env_secrets_migrate.rs"
    )
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Fixture root must be an object, got {type(data)}"
    assert "cases" in data and data["cases"], "Fixture missing non-empty 'cases'"
    return data


_FIXTURE = _load_fixture()
_CASES = _FIXTURE["cases"]


def test_fixture_has_format_version() -> None:
    assert _FIXTURE.get("_format_version") == 1, (
        "Fixture _format_version != 1 — coordinate the bump with the Rust side."
    )


def test_fixture_covers_the_named_tricky_cases() -> None:
    """Guard against a fixture-trim silently dropping a tricky case."""
    names = {c["name"] for c in _CASES}
    required = {
        "quoted_and_unquoted",
        "trailing_comments_quoted_vs_unquoted",
        "placeholders_and_migrated",
        "duplicate_first_occurrence",
        "crlf_line_endings",
        "no_trailing_newline",
        "export_prefix_and_spacing",
        "key_suffix_key_rule",
    }
    missing = required - names
    assert not missing, f"Fixture dropped required tricky case(s): {sorted(missing)}"


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_python_audit_matches_fixture(case: dict, tmp_path: Path) -> None:
    """The Python auditor must return exactly the fixture's audit_expected
    (key + value, in order). Production reads a FILE (universal-newlines), so we
    write the input to a temp `.env` and read it via the real API."""
    env_path = tmp_path / ".env"
    env_path.write_text(case["input"], encoding="utf-8")
    got = [{"key": s.key, "value": s.value} for s in audit_env_secrets(env_path)]
    assert got == case["audit_expected"], (
        f"[{case['name']}] Python audit diverges from fixture:\n"
        f"  got:      {got}\n  expected: {case['audit_expected']}\n"
        "If intentional, regenerate the fixture AND update the Rust mirror."
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_python_rewrite_matches_fixture(case: dict, tmp_path: Path) -> None:
    """The Python rewriter must produce the fixture's expected text + counts."""
    rw = case["rewrite"]
    env_path = tmp_path / ".env"
    env_path.write_text(case["input"], encoding="utf-8")
    replaced, missed = rewrite_env_with_sentinels(env_path, rw["migrated_keys"])
    out_text = env_path.read_text(encoding="utf-8")
    assert out_text == rw["expected_text"], (
        f"[{case['name']}] Python rewrite text diverges from fixture:\n"
        f"  got:      {out_text!r}\n  expected: {rw['expected_text']!r}"
    )
    assert replaced == rw["expected_replaced"], (
        f"[{case['name']}] replaced count: got {replaced}, "
        f"expected {rw['expected_replaced']}"
    )
    assert sorted(missed) == rw["expected_missed"], (
        f"[{case['name']}] missed keys: got {sorted(missed)}, "
        f"expected {rw['expected_missed']}"
    )
