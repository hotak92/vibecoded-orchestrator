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

Two arrays in the fixture:
  * ``agree``     — VALID names (start with an ASCII letter, >=1 alnum). Both
                    implementations MUST produce the identical output. This is
                    the domain that actually reaches env projection (a project
                    that survived name entry).
  * ``divergent`` — pathological OUT-OF-DOMAIN inputs (empty / all-non-alnum /
                    leading-digit) that each side handles with its OWN
                    documented fallback (Python -> ``"vct"``; Rust -> ``"Project"``
                    or a ``"P"``-prepend). These are pinned PER-SIDE, asserting
                    each implementation's own behaviour — NOT cross-language
                    equality — so a future "unify the two sanitizers" change is
                    a deliberate edit of BOTH implementations + this fixture,
                    never a silent drift. See the module docstrings of
                    ``vco_lib/project_init.py`` and ``vco_lib/project_naming.py``
                    for why the two rules are intentionally distinct.
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
    assert "divergent" in data, "Fixture missing required 'divergent' array"
    return data


_FIXTURE = _load_fixture()


def test_fixture_has_format_version() -> None:
    assert _FIXTURE.get("_format_version") == 1, (
        "Fixture _format_version != 1 — the Rust parity #[test] may not know "
        "how to parse this version; coordinate the bump across both sides."
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


@pytest.mark.parametrize(
    "input_name,py_expected,_rust_expected", _FIXTURE["divergent"],
    ids=lambda v: repr(v) if isinstance(v, str) else None,
)
def test_python_divergent_fallback_pinned(
    input_name: str, py_expected: str, _rust_expected: str
) -> None:
    """OUT-OF-DOMAIN inputs: assert the PYTHON side's own documented fallback.
    The Rust #[test] asserts the ``_rust_expected`` column for its side. These
    are pinned separately (not cross-checked for equality) precisely because
    the two implementations DIVERGE here today — pinning both makes any future
    convergence a deliberate, visible edit."""
    actual = sanitize_for_weaviate_class(input_name)
    assert actual == py_expected, (
        f"Python KG sanitizer fallback drifted: "
        f"sanitize_for_weaviate_class({input_name!r}) = {actual!r}, "
        f"fixture pins Python side to {py_expected!r}."
    )


def test_divergence_is_real_not_stale() -> None:
    """Sanity: at least one divergent row must ACTUALLY differ between the two
    columns, else the 'divergent' array is stale (the sides converged) and the
    fixture should be re-classified as 'agree'."""
    real_divergences = [
        row for row in _FIXTURE["divergent"] if row[1] != row[2]
    ]
    assert real_divergences, (
        "No row in 'divergent' actually differs (py col == rust col for all) — "
        "if the two KG sanitizers have converged, move these rows into 'agree'."
    )
