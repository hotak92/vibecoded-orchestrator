# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity test for the KG (underscore-DROPPING) sanitizer.

Audit F1.2 / A3.1 / D3 (v0.2.75): the per-project KG / Development / Diagrams
collection basename is derived from the project name by TWO independent
implementations —

  * Python: ``vco_lib.project_init.sanitize_for_weaviate_class`` (used by
    ``apply_project_env`` when the Rust launcher shells out to project it).
  * Rust:   ``projects_v2.rs::sanitize_kg_collection`` (used by the launcher's
    own env-settings projection + the Identity-tab display).

Both feed ``KG_COLLECTION`` into ``.claude/env`` / ``.claude/settings.json``.
If they drift on a VALID project name, the launcher and the Python re-projection
would compute different collection names for the same project and the env
overwrites would point at the wrong collection. The pre-existing byte-identity
test (``tests/test_config_projection_byte_identical.py``) only catches drift
POST-projection on a single hand-picked name; this LOWER-level test pins the
whole valid-name domain against a shared fixture so a refactor of either
sanitizer is caught immediately.

Two-runner design (same shape as ``tests/test_project_naming_parity.py`` for
the code-graph sanitizer): this Python test and the Rust ``#[test]``
``kg_sanitizer_matches_shared_fixture`` in ``projects_v2.rs`` consume the SAME
``tests/fixtures/kg_sanitizer_parity.json``. A divergence fails one side.

The fixture:
  * ``agree`` — every input paired with the single output BOTH implementations
                MUST produce. X-1 / v0.2.76 (ruling #2): the previously-
                divergent pathological inputs (empty / all-non-alnum /
                leading-digit) now live here too — both sides converge on the
                sentinel prefix ``"vct"`` (the Rust ``"Project"`` / ``"P"``-
                prepend divergence was eliminated at the source; the SSOT is
                ``vco_lib.codegraph_naming.sanitize_for_weaviate_class``). The
                old ``divergent`` array is RETIRED.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib.project_init import sanitize_for_weaviate_class


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kg_sanitizer_parity.json"


def _load_fixture() -> dict:
    assert FIXTURE_PATH.exists(), (
        f"Parity fixture missing: {FIXTURE_PATH} — shared with the Rust "
        "#[test] kg_sanitizer_matches_shared_fixture in projects_v2.rs"
    )
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Fixture root must be an object, got {type(data)}"
    assert "agree" in data, "Fixture missing required 'agree' array"
    assert "divergent" not in data, (
        "The 'divergent' array is RETIRED (X-1 / v0.2.76): both sanitizers "
        "now converge on 'vct' for out-of-domain input. Those rows belong in "
        "'agree'."
    )
    return data


_FIXTURE = _load_fixture()


def test_fixture_has_format_version() -> None:
    assert _FIXTURE.get("_format_version") == 2, (
        "Fixture _format_version != 2 — v2 retired the 'divergent' array "
        "(X-1 / v0.2.76). The Rust parity #[test] must parse v2; coordinate "
        "the bump across both sides."
    )


def test_fixture_has_substantial_valid_domain_coverage() -> None:
    """Guard against a fixture-trim that quietly un-pins the valid domain."""
    assert len(_FIXTURE["agree"]) >= 100, (
        "The 'agree' array should pin 100+ valid-name edge cases "
        f"(non-ASCII in-word, punctuation runs, mixed case, digits-in-middle, "
        f"long names, underscores, spaces); found {len(_FIXTURE['agree'])}."
    )


@pytest.mark.parametrize(
    "input_name,expected", _FIXTURE["agree"],
    ids=lambda v: repr(v) if isinstance(v, str) else None,
)
def test_python_matches_agree_domain(input_name: str, expected: str) -> None:
    """Every VALID-name fixture row must produce the expected output under the
    Python sanitizer. The Rust #[test] asserts the same for its implementation
    — a divergence between languages fails at least one side."""
    actual = sanitize_for_weaviate_class(input_name)
    assert actual == expected, (
        f"Python KG sanitizer diverges from fixture: "
        f"sanitize_for_weaviate_class({input_name!r}) = {actual!r}, "
        f"fixture says {expected!r}. If intentional, update the fixture AND the "
        f"Rust sanitize_kg_collection in the same commit."
    )


def test_out_of_domain_inputs_now_in_agree_and_unify_to_vct() -> None:
    """X-1 / v0.2.76: the previously-divergent pathological inputs live in
    'agree' now, and the Python side produces the unified sentinel prefix
    'vct' for each. (The Rust #[test] asserts the same output for its side —
    both consume this same 'agree' domain, so convergence is enforced.)"""
    unified = {
        "123abc", "9", "1st-project", "007bond", "", "   ", "!!!", "...!!!",
        "___",
    }
    agree_inputs = {row[0] for row in _FIXTURE["agree"]}
    missing = unified - agree_inputs
    assert not missing, (
        f"expected the unified out-of-domain inputs in 'agree', missing: {missing}"
    )
    for raw in unified:
        assert sanitize_for_weaviate_class(raw) == "vct", raw
