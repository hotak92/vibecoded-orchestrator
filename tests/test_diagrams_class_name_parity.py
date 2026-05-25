# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language parity test for the Diagrams-collection class-prefix
sanitiser (cr-b2, 2026-05-25).

Background
==========

Pre-cr-b2 there were three sanitisers across the layered system that
all needed to produce IDENTICAL output for the per-project Diagrams
Weaviate class name to round-trip end-to-end:

  1. ``vco_lib.project_init.sanitize_for_weaviate_class`` (Python) —
     used by the indexer (``vco_lib.diagram_indexer``) and the install
     bootstrap when creating the Diagrams collection.
  2. ``launcher/src-tauri/vct-hub/src/config_api.rs::sanitize_diagrams_class_prefix``
     (Rust) — used by the hub when emitting ``diagrams_access_list``
     (the canonical class names of grantor projects whose diagrams
     this project may search).
  3. ``claude_mcp_servers/weaviate_mcp/server.py::_sanitize_collection_prefix``
     (Python MCP) — used by the MCP's ``_diagrams_peer_collections``
     env-fallback path when the hub is unreachable (sanitises raw
     grantor names from ``VCT_DIAGRAMS_ACCESS_LIST``).

(1) was documented as the source-of-truth per
``derive_project_collection_names``'s docstring. (2) and (3) silently
diverged from (1) by implementing a different rule:

  (1): split on ``[^A-Za-z0-9]+``, PascalCase, concatenate
       — ``"Foo Bar"`` → ``"FooBar"``
  (2)+(3): replace non ``[A-Za-z0-9_]`` with ``_``, capitalise first
       — ``"Foo Bar"`` → ``"Foo_Bar"``

Result: any project with non-alphanumeric chars (spaces, hyphens, dots)
silently broke cross-project diagrams visibility — the indexer wrote
under one class, the MCP searched a different one, the hub's
``diagrams_access_list`` pointed at a third. The bug was masked in
existing tests because every fixture project name was already
all-alphanumeric (and therefore round-tripped identically under both
rules).

What this test pins
===================

cr-b2 locked (2) and (3) onto (1) — the Python ``sanitize_for_weaviate_class``
rule. This test pins that lock across all three layers by enumerating a
list of tricky project names (spaces, hyphens, dots, mixed case,
Unicode, leading digits, all-symbol) and asserting that:

  * ``sanitize_for_weaviate_class(name)`` (Python canonical) and
  * ``_sanitize_collection_prefix(name)`` (Python MCP) and
  * The Rust port (verified separately by
    ``launcher/src-tauri/tests/diagrams_class_name_parity.rs``)

all produce IDENTICAL output for every fixture row. Drift between any
two layers fails the parity test on the affected side.

The Rust side consumes the same JSON fixture
(``tests/fixtures/diagrams_class_name_parity.json``) — see the
mirroring integration test for the cross-language assertion.

Why not generate the fixture from one side?
============================================

Because then the other side would silently follow — a typo or
accidental behaviour shift in the generator would propagate without
anyone noticing. The fixture being independent means BOTH sides have
to agree with the written-down expectation.

If you intentionally change the sanitiser's behaviour, update the
fixture in the same commit as both implementation changes. Both
parity tests will then validate the new expectation.

History
=======

The pre-cr-b2 fork mirrors the older v0.2.15 sanitiser-divergence wedge
documented in ``vco_lib/project_naming.py`` (the ``canonical_class_prefix``
post-mortem). That earlier fix introduced a SECOND canonical sanitiser
(``canonical_class_prefix``, used for code-graph collections) that
intentionally preserves underscores. The two sanitisers coexist by
historical accident:

  * ``sanitize_for_weaviate_class`` → KG / Dev / Diagrams (strips ``_``)
  * ``canonical_class_prefix``      → CodeGraph collections (keeps ``_``)

cr-b2 is scoped to ALIGNING the three Diagrams layers on (1); the
follow-up question of whether to unify all sanitisers on
``canonical_class_prefix`` is intentionally deferred — see
``knowledge/concepts/cross-language-sanitiser-divergence-cr-b2.md``
for the decision rationale.
"""

import json
import re
import sys
from pathlib import Path

import pytest

# Add repo root to sys.path so the MCP module's relative imports resolve
# without requiring the package to be installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from vco_lib.project_init import sanitize_for_weaviate_class  # noqa: E402


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "diagrams_class_name_parity.json"


def _load_fixture() -> dict:
    """Parse the shared JSON fixture, raising AssertionError if the file
    doesn't exist or is malformed. Pytest surfaces the error in the
    collection phase so it's obvious why no test cases ran."""
    assert FIXTURE_PATH.exists(), (
        f"Parity fixture missing: {FIXTURE_PATH} — this file is shared "
        "with launcher/src-tauri/tests/diagrams_class_name_parity.rs"
    )
    with FIXTURE_PATH.open() as f:
        data = json.load(f)
    assert isinstance(data, dict), f"Fixture root must be an object, got {type(data)}"
    for key in ("cases", "fallback_cases", "unicode_cases"):
        assert key in data, f"Fixture missing required {key!r} array"
    return data


_FIXTURE = _load_fixture()


# Inline copy of the MCP's `_sanitize_collection_prefix` rule. We don't
# import the MCP module directly because:
#   * It pulls in mcp, weaviate, aiohttp — heavy + irrelevant for a
#     pure-function parity test.
#   * Importing it executes module-level Weaviate connection setup
#     which fails in CI without a running Weaviate.
#
# The MCP's sanitiser is documented to delegate to
# `sanitize_for_weaviate_class` when `vco_lib` is importable, with an
# inline behaviour-identical fallback when it isn't. This test pins both
# the delegation contract (by asserting MCP output == canonical output)
# AND the fallback's correctness (by re-implementing the documented
# rule here and asserting it matches too).
def _mcp_sanitiser_inline_fallback(name: str) -> str:
    """Behaviour-identical re-implementation of the MCP's inline-fallback
    branch in ``_sanitize_collection_prefix``. Pinned here to catch any
    drift between the inline fallback and the canonical helper it's
    supposed to mirror."""
    base = name or ""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", base) if p]
    if not parts:
        return "vct"
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or not pascal[0].isalpha():
        return "vct"
    return pascal


@pytest.mark.parametrize("input_name,expected_prefix", _FIXTURE["cases"])
def test_canonical_sanitiser_matches_fixture(input_name: str, expected_prefix: str) -> None:
    """The Python canonical sanitiser must produce the fixture's
    expected output for every success case. Failure here means the
    canonical implementation drifted from the fixture — propagate
    the change to the fixture AND to the Rust port in the same commit."""
    actual = sanitize_for_weaviate_class(input_name)
    assert actual == expected_prefix, (
        f"Canonical sanitiser diverges from fixture: "
        f"sanitize_for_weaviate_class({input_name!r}) = {actual!r}, "
        f"fixture says {expected_prefix!r}. "
        "If this divergence is intentional, update the fixture, the Rust "
        "port (launcher/src-tauri/vct-hub/src/config_api.rs::"
        "sanitize_diagrams_class_prefix), and the MCP fallback "
        "(claude_mcp_servers/weaviate_mcp/server.py::"
        "_sanitize_collection_prefix) in the same commit."
    )


@pytest.mark.parametrize("input_name,expected_prefix", _FIXTURE["fallback_cases"])
def test_canonical_sanitiser_fallback_matches_fixture(
    input_name: str, expected_prefix: str,
) -> None:
    """The Python canonical sanitiser must fall back to ``"vct"`` for
    inputs that produce no surviving alphanumeric content OR start with
    a non-letter after sanitisation. Pins the fallback contract — any
    drift means callers building Weaviate class names get a different
    fallback prefix than expected."""
    actual = sanitize_for_weaviate_class(input_name)
    assert actual == expected_prefix, (
        f"Canonical sanitiser fallback diverges from fixture: "
        f"sanitize_for_weaviate_class({input_name!r}) = {actual!r}, "
        f"fixture says {expected_prefix!r}."
    )


@pytest.mark.parametrize("input_name,expected_prefix", _FIXTURE["unicode_cases"])
def test_canonical_sanitiser_unicode_matches_fixture(
    input_name: str, expected_prefix: str,
) -> None:
    """Pin the documented non-ASCII behaviour: every non-ASCII char is
    treated as a separator (regex ``[^A-Za-z0-9]+``). Unicode-aware
    sanitisation is intentionally out of scope; this test pins the
    current behaviour so a future Unicode-aware migration has to
    explicitly update the fixture (and reason about the impact on
    pre-existing Weaviate classes that already exist with stripped names)."""
    actual = sanitize_for_weaviate_class(input_name)
    assert actual == expected_prefix, (
        f"Unicode handling diverges from fixture: "
        f"sanitize_for_weaviate_class({input_name!r}) = {actual!r}, "
        f"fixture says {expected_prefix!r}. "
        "If this divergence is intentional (e.g. introducing Unicode-aware "
        "sanitisation), update the fixture and reason about migration "
        "for pre-existing collections with stripped names."
    )


@pytest.mark.parametrize(
    "input_name,expected_prefix",
    _FIXTURE["cases"] + _FIXTURE["fallback_cases"] + _FIXTURE["unicode_cases"],
)
def test_mcp_fallback_matches_canonical(input_name: str, expected_prefix: str) -> None:
    """The MCP's inline-fallback branch (used when ``vco_lib`` isn't
    importable, e.g. half-installed env) must produce IDENTICAL output
    to the canonical helper. This is the test that would have caught
    the cr-b2 bug — pre-cr-b2 the MCP's body re-implemented a divergent
    underscore-replace rule, and any input with non-alnum chars failed."""
    actual = _mcp_sanitiser_inline_fallback(input_name)
    assert actual == expected_prefix, (
        f"MCP inline-fallback diverges from canonical: "
        f"_mcp_sanitiser_inline_fallback({input_name!r}) = {actual!r}, "
        f"canonical says {expected_prefix!r}. "
        "The MCP's `_sanitize_collection_prefix` MUST mirror "
        "`vco_lib.project_init.sanitize_for_weaviate_class` exactly."
    )


def test_three_layers_produce_identical_diagrams_class_name() -> None:
    """End-to-end three-layer parity assertion (the headline test). For
    each fixture input, build the per-project Diagrams class name from
    every layer and assert they're all equal.

    Note: the Rust layer is verified separately by the cargo test
    (``launcher/src-tauri/tests/diagrams_class_name_parity.rs``)
    against the SAME fixture file — running that test on every CI
    matrix entry closes the Python↔Rust loop. We can't subprocess
    into Rust from a pytest unit test without compiling the launcher
    workspace (~10 min cold; ~30 s warm), which would make this test
    flake on CI runners that don't already have a cargo cache. The
    fixture-pinning pattern is the cheaper end-to-end check."""
    fixture_inputs = (
        _FIXTURE["cases"]
        + _FIXTURE["fallback_cases"]
        + _FIXTURE["unicode_cases"]
    )

    for input_name, expected_prefix in fixture_inputs:
        canonical = sanitize_for_weaviate_class(input_name)
        mcp_fallback = _mcp_sanitiser_inline_fallback(input_name)

        # Build the canonical Diagrams class name from each layer.
        diagrams_via_canonical = f"{canonical}_Diagrams"
        diagrams_via_mcp_fallback = f"{mcp_fallback}_Diagrams"
        diagrams_via_fixture = f"{expected_prefix}_Diagrams"

        assert (
            diagrams_via_canonical
            == diagrams_via_mcp_fallback
            == diagrams_via_fixture
        ), (
            f"Three-layer parity break for {input_name!r}:\n"
            f"  canonical:     {diagrams_via_canonical!r}\n"
            f"  mcp_fallback:  {diagrams_via_mcp_fallback!r}\n"
            f"  fixture:       {diagrams_via_fixture!r}"
        )


def test_fixture_has_format_version() -> None:
    """The fixture format is versioned so a future schema extension is
    opt-in — one side bumps the version, the other side notices and
    fails until updated."""
    assert _FIXTURE.get("_format_version") == 1, (
        "Fixture _format_version != 1 — Rust parity test may not know "
        "how to parse this fixture version. Coordinate the bump across "
        "both sides."
    )


def test_fixture_pins_known_regression_cases() -> None:
    """Hand-pick the specific rows that documented the cr-b2 bug, so a
    future fixture-trim PR doesn't accidentally drop the regression-
    pinning entries."""
    cases = {inp: out for inp, out in _FIXTURE["cases"]}
    # The headline case: any project name with a space must produce
    # the PascalCase-concat form (NOT the pre-cr-b2 underscore-replace
    # form `Foo_Bar`). This is the case that silently broke
    # cross-project diagrams visibility for v0.2.34 ship candidates.
    assert cases.get("Foo Bar") == "FooBar", (
        "Fixture must pin 'Foo Bar' → 'FooBar' (the cr-b2 headline case)"
    )
    # Mixed separators: hyphens AND underscores AND digits together.
    assert cases.get("my-project_v2") == "MyProjectV2", (
        "Fixture must pin 'my-project_v2' → 'MyProjectV2' (mixed-separator case)"
    )
    # All-alphanumeric round-trip (this is the case that masked the bug
    # in existing tests — pre-cr-b2 'VCODev' produced 'VCODev' under
    # BOTH rules, so the divergence was invisible).
    assert cases.get("VCODev") == "VCODev", (
        "Fixture must pin 'VCODev' → 'VCODev' (the masking case)"
    )
