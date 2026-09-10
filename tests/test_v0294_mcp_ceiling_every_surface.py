# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every surface that declares the `mcp` dependency carries the `<2` ceiling.

mcp 2.0 removed `mcp.server.fastmcp`, which `claude_mcp_servers/weaviate_mcp`
and `search_mcp` import. The ceiling was added to `requirements.txt` and the
root pyproject extra on v0.2.89 tag day (a fresh CI resolver installed 2.0 the
day it shipped). `claude_mcp_servers/pyproject.toml` was missed: it declared
`mcp>=1.27.0` with no ceiling, so a Dependabot PR against THAT file (#364,
2026-09) would have lifted the pin the root files enforce. A pin that exists on
two of three resolution surfaces is not a pin.

Red-proof: drop `,<2` from any of the three files and the test names it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: (file, regex that must match the mcp requirement line)
SURFACES = [
    ("requirements.txt", re.compile(r"^mcp\s*>=\s*[\d.]+\s*,\s*<\s*2\s*$", re.M)),
    ("pyproject.toml", re.compile(r'^\s*"mcp\s*>=\s*[\d.]+\s*,\s*<\s*2\s*",\s*$', re.M)),
    ("claude_mcp_servers/pyproject.toml", re.compile(r'^\s*"mcp\s*>=\s*[\d.]+\s*,\s*<\s*2\s*",\s*$', re.M)),
]

_ANY_MCP_REQ = re.compile(r'^\s*"?mcp\s*(>=|==|~=|>)', re.M)


@pytest.mark.parametrize(("rel", "pattern"), SURFACES, ids=[s[0] for s in SURFACES])
def test_surface_declares_mcp_with_the_ceiling(rel: str, pattern: re.Pattern) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    declared = [m.group(0) for m in _ANY_MCP_REQ.finditer(text)]
    assert declared, f"{rel}: no mcp requirement found — the surface list is stale"
    assert pattern.search(text), (
        f"{rel}: the mcp requirement lacks the `<2` ceiling (found {declared!r}). "
        f"mcp 2.x removed mcp.server.fastmcp; pin every surface or none is pinned."
    )


def test_no_other_python_surface_declares_mcp_unceilinged() -> None:
    """A fourth requirements/pyproject file declaring mcp must join SURFACES."""
    offenders = []
    for path in list(REPO.glob("**/requirements*.txt")) + list(REPO.glob("**/pyproject.toml")):
        if any(part in {".venv", "node_modules", "target", ".git"} for part in path.parts):
            continue
        rel = str(path.relative_to(REPO))
        if rel in {s[0] for s in SURFACES}:
            continue
        for m in _ANY_MCP_REQ.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[
                path.read_text(encoding="utf-8", errors="ignore")[: m.start()].count("\n")
            ]
            if "<2" not in line.replace(" ", ""):
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, "mcp declared without the `<2` ceiling outside SURFACES:\n  " + "\n  ".join(offenders)
