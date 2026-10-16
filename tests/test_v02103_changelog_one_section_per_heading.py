# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review L-4 — CHANGELOG ``[Unreleased]`` keeps ONE section
per ``###`` heading (Keep a Changelog shape).

Parallel lanes each appended their own ``### Changed`` / ``### Fixed`` block,
so the section carried two of each and a reader scanning "Fixed" missed half
of it. The release step moves ``[Unreleased]`` into the version block
verbatim, so it is the block that must stay well-formed.
"""
from __future__ import annotations

from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _unreleased_headings(text: str) -> list:
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## [Unreleased]"))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## [")), len(lines))
    return [ln.strip() for ln in lines[start + 1:end] if ln.startswith("### ")]


def test_unreleased_has_one_section_per_heading():
    headings = _unreleased_headings(CHANGELOG.read_text(encoding="utf-8"))
    dupes = sorted({h for h in headings if headings.count(h) > 1})
    assert not dupes, f"[Unreleased] repeats {dupes}: merge them into one section each"


def test_the_check_sees_a_repeated_heading():
    """The other arm: a repeated heading IS reported (so the check above is
    not vacuously green on a parse that finds no headings)."""
    sample = ("# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- a\n\n"
              "### Changed\n\n- b\n\n### Fixed\n\n- c\n\n## [0.1.0] - x\n\n### Fixed\n")
    headings = _unreleased_headings(sample)
    assert headings == ["### Fixed", "### Changed", "### Fixed"]
