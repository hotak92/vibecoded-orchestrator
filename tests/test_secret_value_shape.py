# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Parity + unit tests for the single-line secret-value shape SSOT (v0.2.80 Part A).

``vco_lib/secret_value_shape.py`` is the Python source of truth for the
value-shape predicate (A>B>C rule). Two mirrors — the bash
``vct::_is_single_line_secret`` and the Rust predicate in
``secrets_import.rs`` — reproduce it, all three locked to the SAME canonical
fixture ``tests/fixtures/secret_value_shape_parity.json``.

This module is the Python parity leg (the Rust ``#[test]`` and the bash
``test_vct.sh`` case are the other two legs — a divergence in any mirror fails
its own leg). It also carries the Python-only units: the ``allow_multiline``
escape hatch, the classifier taxonomy edge cases, and the act/leave-alone
decisions.

Mirrors the one-fixture-three-runners shape of
``tests/test_env_secrets_migrate_parity.py`` + its
``tests/fixtures/env_secrets_parity.json`` (the parity precedent the plan and
the Opus review both point at).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib.secret_value_shape import (
    classify_secret_value,
    is_single_line_secret,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "secret_value_shape_parity.json"


def _load_fixture() -> dict:
    assert FIXTURE_PATH.exists(), (
        f"Parity fixture missing: {FIXTURE_PATH} — shared with the Rust "
        "#[test] in secrets_import.rs and the bash case in "
        "tools/vct-secrets/tests/test_vct.sh"
    )
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Fixture root must be an object, got {type(data)}"
    assert "cases" in data and data["cases"], "Fixture missing non-empty 'cases'"
    return data


_FIXTURE = _load_fixture()
_CASES = _FIXTURE["cases"]

#: Every case name the fixture MUST carry (Opus review #7). Guards against a
#: fixture-trim silently dropping a discriminating case.
_REQUIRED_CASE_NAMES = {
    "single_line_valid",
    "embedded_lf",
    "embedded_crlf",
    "post_line0_key_eq_blob",
    "export_key_eq_blob",
    "github_pat_over_200",
    "pem_private_key_legit",
    "indented_json_with_eq_not_a_blob",
    "base64_padding_not_a_blob",
    "line0_is_key_eq",
}


def test_fixture_has_format_version() -> None:
    assert _FIXTURE.get("_format_version") == 1, (
        "Fixture _format_version != 1 — coordinate the bump with the Rust + "
        "bash mirrors."
    )


def test_fixture_covers_named_cases() -> None:
    """The fixture must carry every required discriminating case."""
    names = {c["name"] for c in _CASES}
    missing = _REQUIRED_CASE_NAMES - names
    assert not missing, f"Fixture dropped required case(s): {sorted(missing)}"


def test_fixture_case_schema() -> None:
    """Every case carries the full field schema the three runners consume."""
    required_fields = {
        "name",
        "value",
        "key_name",
        "expect_ok",
        "expect_reason",
        "expect_taxonomy",
    }
    for case in _CASES:
        missing = required_fields - set(case)
        assert not missing, f"[{case.get('name')}] case missing fields: {sorted(missing)}"
        assert isinstance(case["expect_ok"], bool), (
            f"[{case['name']}] expect_ok must be a bool"
        )
        # An accepted value carries an empty reason; a rejected one carries a
        # non-empty slug. This invariant is what the mirrors rely on.
        if case["expect_ok"]:
            assert case["expect_reason"] == "", (
                f"[{case['name']}] accepted case must have empty expect_reason"
            )
        else:
            assert case["expect_reason"], (
                f"[{case['name']}] rejected case must have a non-empty expect_reason"
            )


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_is_single_line_secret_matches_fixture(case: dict) -> None:
    """``is_single_line_secret`` must return the fixture's (ok, reason)."""
    ok, reason = is_single_line_secret(case["value"], key_name=case["key_name"])
    assert ok == case["expect_ok"], (
        f"[{case['name']}] ok mismatch: got {ok}, expected {case['expect_ok']}. "
        f"reason={reason!r}. If intentional, regenerate the fixture AND update "
        "the Rust + bash mirrors."
    )
    assert reason == case["expect_reason"], (
        f"[{case['name']}] reason mismatch: got {reason!r}, "
        f"expected {case['expect_reason']!r}"
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_classify_secret_value_matches_fixture(case: dict) -> None:
    """``classify_secret_value`` must return the fixture's taxonomy tag."""
    got = classify_secret_value(case["value"], key_name=case["key_name"])
    assert got == case["expect_taxonomy"], (
        f"[{case['name']}] taxonomy mismatch: got {got!r}, "
        f"expected {case['expect_taxonomy']!r}"
    )


# ---------------------------------------------------------------------------
# Python-only units — behaviours not (or not fully) expressible in the fixture.
# ---------------------------------------------------------------------------


def test_blob_signature_not_confused_with_indented_continuation() -> None:
    """A multi-line value with only INDENTED KEY= lines is not a col-0 blob.

    Its rejection reason must be the generic ``embedded_newline`` (rule a), NOT
    the ``blob_key_eq_continuation`` blob signature (rule b) — the col-0 anchor
    must not fire on an indented continuation. This is the review-#4 negative.
    """
    value = "token-value\n    INDENTED_KEY=deep\n    OTHER=x"
    ok, reason = is_single_line_secret(value)
    assert ok is False
    assert reason == "embedded_newline", (
        f"col-0 anchor false-fired on an indented KEY= line: reason={reason!r}"
    )


def test_allow_multiline_bypasses_blob_reject() -> None:
    """``allow_multiline=True`` accepts an arbitrary multi-line value.

    The escape hatch lets a caller (``vct set --allow-multiline``) vouch for a
    multi-line format the allowlist does not yet recognise. It bypasses the
    embedded-newline + blob-signature rejects.
    """
    blobby = "line-0\nSOME_KEY=value\nANOTHER=thing"
    # Without the flag: rejected as a blob.
    ok_default, reason_default = is_single_line_secret(blobby)
    assert ok_default is False
    assert reason_default == "blob_key_eq_continuation"
    # With the flag: accepted (caller vouches).
    ok_allow, reason_allow = is_single_line_secret(blobby, allow_multiline=True)
    assert ok_allow is True
    assert reason_allow == ""


def test_allow_multiline_still_rejects_control_char() -> None:
    """``allow_multiline`` does NOT waive the control-char reject.

    An embedded NUL / control char signals corruption regardless of the
    caller's intent, so the escape hatch must not accept it.
    """
    corrupt = "line-0\x00still-corrupt"
    ok, reason = is_single_line_secret(corrupt, allow_multiline=True)
    assert ok is False
    assert reason == "control_char"


def test_allow_multiline_still_rejects_over_long_github_pat() -> None:
    """``allow_multiline`` does NOT waive the github_pat length heuristic."""
    over = "ghp_" + ("A" * 300)
    ok, reason = is_single_line_secret(
        over, allow_multiline=True, key_name="github_pat"
    )
    assert ok is False
    assert reason == "github_pat_over_200"


def test_length_heuristic_only_for_github_pat_named_keys() -> None:
    """A legitimately-long value under a non-github_pat key is NOT rejected.

    Leave-alone case: a 300-char JWT / base64 API key under some other key name
    is single-line and valid — the length heuristic must not fire on it.
    """
    long_jwt = "eyJhbGci" + ("Q" * 300)  # single line, not github_pat-named
    ok, reason = is_single_line_secret(long_jwt, key_name="app_jwt")
    assert ok is True, f"long non-github_pat value wrongly rejected: {reason!r}"
    assert reason == ""
    assert classify_secret_value(long_jwt, key_name="app_jwt") == "ok"


def test_trailing_newline_does_not_make_single_line_look_multi() -> None:
    """A single trailing newline (normal on a file-store secret) is trimmed.

    Leave-alone case: ``ghp_...\\n`` is still a valid single-line secret, not a
    two-line blob.
    """
    value = "ghp_" + ("A" * 36) + "\n"
    ok, reason = is_single_line_secret(value, key_name="github_pat")
    assert ok is True
    assert reason == ""
    assert classify_secret_value(value, key_name="github_pat") == "ok"


def test_length_corruption_distinct_from_blob() -> None:
    """A malformed single-line ghp_ token is length_corruption, not blob.

    ``github_pat.broken`` = 52-char ``ghp_`` (12 too long) with no embedded
    newline: nothing to split → distinct taxonomy so the doctor tells the user
    "re-issue", not "split it" (Part C).
    """
    too_long_pat = "ghp_" + ("A" * 48)  # 52 chars, single line, no newline
    # Single-line: is_single_line_secret accepts it (it is one line, under 200).
    ok, reason = is_single_line_secret(too_long_pat, key_name="github_pat")
    assert ok is True
    assert reason == ""
    # But the taxonomy flags the malformed shape distinctly from a blob.
    assert classify_secret_value(too_long_pat, key_name="github_pat") == "length_corruption"


def test_pem_public_key_and_certificate_also_accepted() -> None:
    """The legit-multiline allowlist covers the CERTIFICATE / PUBLIC KEY frames."""
    cert = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBfakeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "-----END CERTIFICATE-----"
    )
    ok, reason = is_single_line_secret(cert, key_name="tls_cert")
    assert ok is True
    assert reason == ""
    assert classify_secret_value(cert, key_name="tls_cert") == "legit_multiline"


def test_well_formed_classic_pat_is_ok_not_length_corruption() -> None:
    """Leave-alone: an exactly-40-char classic PAT is ``ok``, never flagged."""
    good = "ghp_" + ("B" * 36)
    assert is_single_line_secret(good, key_name="github_pat") == (True, "")
    assert classify_secret_value(good, key_name="github_pat") == "ok"
