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
    [*REPO.glob("templates/agents/**/*.md"), *REPO.glob("templates/skills/**/SKILL.md")]
)


def _frontmatter(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    return text.split("---", 2)[1]


def test_the_scan_finds_the_shipped_catalogue() -> None:
    assert len(FILES) > 50, f"found only {len(FILES)} agent/skill files"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_frontmatter_is_a_yaml_mapping_with_a_name(path: Path) -> None:
    fm = _frontmatter(path)
    if fm is None:
        pytest.skip("no frontmatter block (a co-located doc, not a definition)")
    data = yaml.safe_load(fm)
    assert isinstance(data, dict), f"{path}: frontmatter is not a mapping"
    assert data.get("name"), f"{path}: frontmatter has no name"
    assert data.get("description"), f"{path}: frontmatter has no description"
