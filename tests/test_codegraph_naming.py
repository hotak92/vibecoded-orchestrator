# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""X-1 / v0.2.76: the ONE naming home ``vco_lib.codegraph_naming``.

``vco_lib/codegraph_naming.py`` is the single source of truth for BOTH
project-name → Weaviate-class-name rules:

  * ``sanitize_for_weaviate_class`` — underscore-DROPPING (KG / Development /
    Diagrams basenames);
  * ``canonical_class_prefix`` — underscore-PRESERVING (code-graph prefix).

This test locks:
  1. the KG sanitizer over the FULL ``kg_sanitizer_parity.json`` fixture,
     including the previously-``divergent`` pathological inputs, now
     asserting the UNIFIED Python answer (``"vct"``) — the v0.2.76 X-1
     divergence-elimination;
  2. that ``vco_lib.project_init`` / ``vco_lib.project_naming`` re-export the
     SAME function objects (no second copy);
  3. the CLI round-trips through a real subprocess (the surface Rust calls).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib.codegraph_naming import (
    FALLBACK_PREFIX,
    canonical_class_prefix,
    sanitize_for_weaviate_class,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "kg_sanitizer_parity.json"


def _load_fixture() -> dict:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "agree" in data
    return data


_FIXTURE = _load_fixture()


# ─────────────────────── re-export identity ───────────────────────


def test_project_init_reexports_same_object():
    """``vco_lib.project_init.sanitize_for_weaviate_class`` IS the canonical
    function object — no divergent second copy."""
    from vco_lib import project_init

    assert project_init.sanitize_for_weaviate_class is sanitize_for_weaviate_class


def test_project_naming_reexports_same_object():
    from vco_lib import project_naming

    assert project_naming.canonical_class_prefix is canonical_class_prefix


# ─────────────────────── KG sanitizer (underscore-dropping) ──────────────────


@pytest.mark.parametrize(
    "input_name,expected", _FIXTURE["agree"],
    ids=lambda v: repr(v) if isinstance(v, str) else None,
)
def test_kg_sanitizer_agree_domain(input_name: str, expected: str) -> None:
    assert sanitize_for_weaviate_class(input_name) == expected


# The previously-divergent inputs (empty / all-non-alnum / leading-digit)
# now ALL resolve to the unified fallback prefix on the Python side. The Rust
# port matches this after v0.2.76 (the ``divergent`` fixture array is retired).
_UNIFIED_FALLBACK_INPUTS = [
    "123abc", "9", "1st-project", "007bond", "", "   ", "!!!", "...!!!", "___",
]


@pytest.mark.parametrize("bad_input", _UNIFIED_FALLBACK_INPUTS)
def test_kg_sanitizer_out_of_domain_unifies_to_fallback(bad_input: str) -> None:
    assert sanitize_for_weaviate_class(bad_input) == FALLBACK_PREFIX
    assert FALLBACK_PREFIX == "vct"


def test_kg_sanitizer_leading_digit_falls_back_not_p_prepend() -> None:
    """The v0.2.76 unification: a leading-digit name falls back to the
    sentinel prefix, NOT the old Rust ``P``-prepend."""
    assert sanitize_for_weaviate_class("123abc") == "vct"
    assert sanitize_for_weaviate_class("123abc") != "P123abc"


# ─────────────────────── code-graph prefix (underscore-preserving) ───────────


def test_canonical_prefix_preserves_underscore():
    assert canonical_class_prefix("Camel_Case") == "Camel_Case"


def test_canonical_prefix_drops_spaces_pascalcase():
    assert canonical_class_prefix("VibeCoded Orchestrator") == "VibeCodedOrchestrator"
    assert canonical_class_prefix("foo bar") == "FooBar"


def test_canonical_prefix_dash_to_underscore():
    assert canonical_class_prefix("Foo-Bar") == "Foo_Bar"


@pytest.mark.parametrize("bad", ["", "   ", "123abc", "!!!", "_only_"])
def test_canonical_prefix_rejects_invalid(bad: str):
    with pytest.raises(ValueError):
        canonical_class_prefix(bad)


# ─────────────────────── CLI round-trip (the Rust-facing surface) ────────────


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Invoke ``python -m vco_lib.codegraph_naming`` in a subprocess, with the
    repo root on PYTHONPATH so ``vco_lib`` imports regardless of the venv's
    editable-install state."""
    merged = dict(os.environ)
    # Prepend the repo root so the in-tree vco_lib wins over any editable copy.
    existing = merged.get("PYTHONPATH", "")
    merged["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.codegraph_naming", *args],
        capture_output=True,
        text=True,
        env=merged,
    )


def test_cli_kg_matches_function():
    for name in ("VCO_dev", "VibeCoded Orchestrator", "Foo-Bar", "123abc", ""):
        res = _run_cli("--kg", name)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == sanitize_for_weaviate_class(name)


def test_cli_prefix_matches_function():
    for name in ("Camel_Case", "VibeCoded Orchestrator", "Foo-Bar", "MyProject"):
        res = _run_cli("--prefix", name)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == canonical_class_prefix(name)


def test_cli_prefix_rejects_bad_name_nonzero():
    res = _run_cli("--prefix", "123abc")
    assert res.returncode != 0
    assert "non-letter" in res.stderr


def test_cli_requires_a_mode_flag():
    res = _run_cli("foo")
    assert res.returncode != 0  # argparse: required mutually-exclusive group


def test_cli_rejects_both_flags():
    res = _run_cli("--kg", "--prefix", "foo")
    assert res.returncode != 0  # argparse: mutually-exclusive


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
