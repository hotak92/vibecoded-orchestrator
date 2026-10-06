# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every shipped agent and skill has frontmatter that parses as a YAML mapping.

v0.2.100: ``templates/agents/module-gateway/deepseek-researcher.md`` shipped
from wave 1 with an unquoted ``: `` inside its ``description`` — invalid YAML,
so the definition could not be read as an agent. Nothing tested the shipped
files themselves; this pins all of them (the materialization gate only checks
that RENDERING never breaks frontmatter that parsed before).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
FILES = sorted(
    [
        *REPO.glob("templates/agents/**/*.md"),
        *REPO.glob("templates/skills/**/SKILL.md"),
        # Opt-in pack members ship as surely as the defaults — a malformed
        # frontmatter would break the pack exactly as it breaks a default.
        *REPO.glob("templates/packs/*/agents/*.md"),
        *REPO.glob("templates/packs/*/skills/*/SKILL.md"),
    ]
)


def _frontmatter(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    return text.split("---", 2)[1]


def test_the_scan_finds_the_shipped_catalogue() -> None:
    """The parametrized tests skip a file with no frontmatter, so they pass
    vacuously when the globs match nothing. Pin the scan against the catalogue
    it must cover: the 11 default free agents + 8 machine-gated gateway agents
    + 6 default skills, plus every member of the opt-in packs. A moved
    directory or a narrowed glob shrinks FILES and fails HERE rather than
    silently skipping every definition."""
    default = (
        len(list((REPO / "templates" / "agents" / "free").glob("*.md")))
        + len(list((REPO / "templates" / "agents" / "module-gateway").glob("*.md")))
        + len(list((REPO / "templates" / "skills").glob("*/SKILL.md")))
    )
    pack_members = (
        len(list(REPO.glob("templates/packs/*/agents/*.md")))
        + len(list(REPO.glob("templates/packs/*/skills/*/SKILL.md")))
    )
    assert default == 25, (
        f"the default catalogue is {default} definitions, expected 25 "
        "(11 free agents + 8 gateway agents + 6 skills)"
    )
    assert pack_members >= 45, (
        f"the pack tree carries only {pack_members} members; templates/packs/ "
        "is missing or its layout changed"
    )
    assert len(FILES) >= default + pack_members, (
        f"the scan found {len(FILES)} files, fewer than the "
        f"{default + pack_members} the catalogue needs"
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_frontmatter_is_a_yaml_mapping_with_a_name(path: Path) -> None:
    fm = _frontmatter(path)
    if fm is None:
        pytest.skip("no frontmatter block (a co-located doc, not a definition)")
    data = yaml.safe_load(fm)
    assert isinstance(data, dict), f"{path}: frontmatter is not a mapping"
    assert data.get("name"), f"{path}: frontmatter has no name"
    assert data.get("description"), f"{path}: frontmatter has no description"
