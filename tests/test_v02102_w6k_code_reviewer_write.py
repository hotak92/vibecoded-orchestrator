# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-K item 7 — the code-reviewer agent gets the Write tool,
scoped by its own instructions to its report file only.

Owner ruling 2026-10-11: reviewers must be able to WRITE their report at
the path the brief names (a reply can be lost to context compaction), while
staying read-only for everything else. The definition file IS the shipped
config, so the contract is pinned here at frontmatter/body level.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT = REPO_ROOT / "templates" / "agents" / "free" / "code-reviewer.md"


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "frontmatter block missing"
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


def test_write_is_in_the_tools_list():
    # The tools list is read from the FRONTMATTER (the harness reads it
    # there), never from a `tools:` line anywhere in the body.
    fm = _frontmatter(AGENT.read_text(encoding="utf-8"))
    assert fm.get("name") == "code-reviewer", fm
    assert fm.get("tools"), "tools: line missing from the frontmatter"
    tools = [t.strip() for t in fm["tools"].split(",")]
    assert "Write" in tools, tools
    # read-only review capability preserved
    for needed in ("Read", "Grep", "Glob", "Bash"):
        assert needed in tools, needed
    # still NO Edit: writing is limited to creating the report file
    assert "Edit" not in tools, tools


def test_body_scopes_write_to_the_report_file_only():
    import re as _re

    body = _re.sub(r"\s+", " ", AGENT.read_text(encoding="utf-8"))
    assert "The Write tool has exactly one permitted target" in body
    assert "report file your" in body and "brief names" in body
    # the negative instruction is explicit: never any other file
    assert "Never Write, Edit or create any other file" in body
    # and the no-brief-named fallback: do not use Write at all
    assert "names no report path, do not use Write" in body
