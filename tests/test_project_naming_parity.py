# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language parity test for canonical_class_prefix.

Consumes tests/fixtures/project_naming.json and asserts that the Python
implementation in vco_lib.project_naming matches every fixture row. The
matching Rust test (launcher/src-tauri/tests/project_naming_parity.rs)
consumes the SAME fixture file — so any divergence between the two
implementations fails one of these two test files.

If you add a row to the fixture, BOTH languages' implementations must
produce the same output. If you intentionally change canonical_class_prefix's
behaviour, update the fixture in the same commit as the implementation
change, and both parity tests will validate the new expectation.

Why not generate the fixture from the Python side? Because then Rust
would be silently following Python — a Python-only typo or accidental
behaviour shift would propagate to Rust without anyone noticing. The
fixture being independent means BOTH sides have to agree with the
written-down expectation.
"""

import json
from pathlib import Path

import pytest

from vco_lib.project_naming import canonical_class_prefix


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "project_naming.json"


def _load_fixture() -> dict:
    """Parse the shared JSON fixture, raising AssertionError if the
    file doesn't exist or isn't well-formed. Pytest will surface the
    error message in the run's setup/collection phase."""
    assert FIXTURE_PATH.exists(), (
        f"Parity fixture missing: {FIXTURE_PATH} — this file is shared "
        "with launcher/src-tauri/tests/project_naming_parity.rs"
    )
    with FIXTURE_PATH.open() as f:
        data = json.load(f)
    assert isinstance(data, dict), f"Fixture root must be an object, got {type(data)}"
    assert "cases" in data, "Fixture missing required 'cases' array"
    assert "errors" in data, "Fixture missing required 'errors' array"
    return data


_FIXTURE = _load_fixture()


@pytest.mark.parametrize("input_name,expected_prefix", _FIXTURE["cases"])
def test_python_matches_fixture(input_name: str, expected_prefix: str) -> None:
    """Every fixture success case must produce the expected output
    under the Python implementation. The Rust test asserts the same
    for its implementation — divergence between languages will fail
    at least one side."""
    actual = canonical_class_prefix(input_name)
    assert actual == expected_prefix, (
        f"Python sanitizer diverges from fixture: "
        f"canonical_class_prefix({input_name!r}) = {actual!r}, "
        f"fixture says {expected_prefix!r}. "
        "If this divergence is intentional, update both the fixture "
        "AND the Rust port in the same commit."
    )


@pytest.mark.parametrize("bad_input", _FIXTURE["errors"])
def test_python_raises_for_fixture_errors(bad_input: str) -> None:
    """Every fixture error case must raise ValueError under the
    Python implementation. The Rust port should return an error
    (Result::Err) for the same inputs."""
    with pytest.raises(ValueError):
        canonical_class_prefix(bad_input)


def test_fixture_has_format_version() -> None:
    """Fixture format is versioned so that if we ever need to extend
    the schema (e.g. add a third array for "warn cases"), the Rust
    side can opt into the new behaviour without breaking older
    fixtures during the rollout."""
    assert _FIXTURE.get("_format_version") == 1, (
        "Fixture _format_version != 1 — Rust parity test may not know "
        "how to parse this fixture version."
    )


def test_fixture_has_coverage_for_known_collision_cases() -> None:
    """Hand-pick a few cases that documented the v0.2.15 wedge so a
    future fixture-trim doesn't accidentally drop the regression-
    pinning rows."""
    cases = {inp: out for inp, out in _FIXTURE["cases"]}
    # The escalation case: base-host Camel_Case must produce
    # Camel_Case (preserved underscore), NOT CamelCase.
    assert cases.get("Camel_Case") == "Camel_Case"
    # The original wedge case: "VibeCoded Orchestrator" must produce
    # VibeCodedOrchestrator (no underscore), matching the schema
    # already on disk.
    assert cases.get("VibeCoded Orchestrator") == "VibeCodedOrchestrator"
    # The folder-name fallback case: "vibecoded-orchestrator" (the
    # Python-side fallback when --project isn't passed) must produce
    # the leading-capital form. (Note this is DIFFERENT from
    # canonical_class_prefix("VibeCoded Orchestrator") — proof that
    # the Python script MUST always be invoked with --project.)
    assert cases.get("vibecoded-orchestrator") == "Vibecoded_orchestrator"
