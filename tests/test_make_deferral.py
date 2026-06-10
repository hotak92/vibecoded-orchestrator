# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 DEDUP-3: tests for _make_deferral helper.

Verifies the new helper that builds DeferralEntry instances with
shared phrasing patterns (textwrap.dedent + strip on multi-line
fields, default `kg_node_refs=[]`).

Per docs/INSTALL_ARCHITECTURE_v2.md §5.1 DEDUP-3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location("install_under_test_d3", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_d3"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_helper_exists(install_module):
    assert hasattr(install_module, "_make_deferral")


def test_minimal_args_produce_valid_entry(install_module):
    """Minimal invocation: id + title + detected + why + command."""
    entry = install_module._make_deferral(
        "foo_bar",
        title="Foo Bar",
        detected="something happened",
        why_deferred="needs user consent",
        command_to_apply="python install.py --apply-foo",
    )
    assert entry.condition_id == "foo_bar"
    assert entry.title == "Foo Bar"
    assert entry.detected == "something happened"
    assert entry.why_deferred == "needs user consent"
    assert entry.command_to_apply == "python install.py --apply-foo"
    assert entry.severity == "warning"  # default
    assert entry.kg_node_refs == []  # explicit default


def test_default_kg_node_refs_is_empty_list(install_module):
    """Audit bug class: missing kwarg should never leave kg_node_refs=None."""
    entry = install_module._make_deferral(
        "x", title="X", detected="d", why_deferred="w", command_to_apply="c",
    )
    # Specifically NOT None — must be a list.
    assert isinstance(entry.kg_node_refs, list)
    assert entry.kg_node_refs == []


def test_multiline_detected_is_dedented(install_module):
    """textwrap.dedent applied to triple-quoted body."""
    entry = install_module._make_deferral(
        "x", title="X",
        detected=(
            "\n"
            "    Line one of detected.\n"
            "    Line two.\n"
            "    "
        ),
        why_deferred="w", command_to_apply="c",
    )
    # Dedent + strip removes leading whitespace per line + trailing newlines.
    assert entry.detected == "Line one of detected.\nLine two."


def test_multiline_why_deferred_is_dedented(install_module):
    entry = install_module._make_deferral(
        "x", title="X", detected="d",
        why_deferred=(
            "\n"
            "        A multi-line\n"
            "        explanation.\n"
        ),
        command_to_apply="c",
    )
    assert entry.why_deferred == "A multi-line\nexplanation."


def test_command_to_apply_is_dedented(install_module):
    """For multi-step recipes the dedent removes leading whitespace."""
    entry = install_module._make_deferral(
        "x", title="X", detected="d", why_deferred="w",
        command_to_apply=(
            "\n"
            "        python install.py --foo \\\n"
            "          --bar\n"
        ),
    )
    assert entry.command_to_apply == (
        "python install.py --foo \\\n  --bar"
    )


def test_severity_passes_through(install_module):
    entry = install_module._make_deferral(
        "x", title="X", detected="d", why_deferred="w", command_to_apply="c",
        severity="critical",
    )
    assert entry.severity == "critical"


def test_severity_validated_by_dataclass(install_module):
    """Invalid severity raises (validation owned by DeferralEntry)."""
    with pytest.raises(ValueError):
        install_module._make_deferral(
            "x", title="X", detected="d", why_deferred="w",
            command_to_apply="c", severity="bogus",
        )


def test_kg_node_refs_preserved(install_module):
    entry = install_module._make_deferral(
        "x", title="X", detected="d", why_deferred="w", command_to_apply="c",
        kg_node_refs=[
            "knowledge/concepts/a.md",
            "knowledge/concepts/b.md",
        ],
    )
    assert entry.kg_node_refs == [
        "knowledge/concepts/a.md",
        "knowledge/concepts/b.md",
    ]


def test_kg_node_refs_is_copied_not_aliased(install_module):
    """Mutating the input list after construction must not change the entry."""
    src = ["knowledge/concepts/a.md"]
    entry = install_module._make_deferral(
        "x", title="X", detected="d", why_deferred="w", command_to_apply="c",
        kg_node_refs=src,
    )
    src.append("knowledge/concepts/b.md")
    assert entry.kg_node_refs == ["knowledge/concepts/a.md"]


def test_entry_round_trips_through_deferral_report(install_module, tmp_path):
    """Built entry passes through DeferralReport.write/read intact."""
    from vco_lib.deferral_report import DeferralReport
    report = DeferralReport()
    entry = install_module._make_deferral(
        "round_trip_test",
        title="Round-trip test",
        detected=(
            "\n"
            "        Detected line 1.\n"
            "        Detected line 2.\n"
        ),
        why_deferred="Round-trip rationale.",
        command_to_apply="python install.py --apply-round-trip",
        severity="info",
        kg_node_refs=["knowledge/concepts/round-trip.md"],
    )
    report.add_entry(entry)
    # DeferralReport.write takes the install root folder; it places
    # the file at <folder>/.claude/context/UPDATE_DEFERRED.md itself.
    written = report.write(tmp_path)
    assert written
    target = tmp_path / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    # Round-trip verifications:
    assert "round_trip_test" in body
    assert "Detected line 1." in body
    assert "Detected line 2." in body
    assert "python install.py --apply-round-trip" in body
    assert "knowledge/concepts/round-trip.md" in body


def test_helper_used_at_least_once_in_install_py(install_module):
    """v0.2.53 lands the helper and migrates at least one site to it.

    Catches accidental deletion of the helper or a future regression
    that re-introduces 51 inline DeferralEntry constructions.
    """
    src = INSTALL_PY.read_text(encoding="utf-8")
    # The helper definition itself contains "_make_deferral" once.
    occurrences = src.count("_make_deferral")
    # We expect at minimum: 1 def + 1 migration callsite = 2.
    assert occurrences >= 2, (
        f"_make_deferral should be both defined AND used in install.py "
        f"after v0.2.53; found {occurrences} occurrence(s)."
    )
