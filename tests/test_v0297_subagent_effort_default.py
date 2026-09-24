# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bundled subagents ship at ``medium`` reasoning effort (owner ruling
2026-09-24, v0.2.97).

``medium`` is the default; ``high`` is reserved for genuinely hard-reasoning
roles and is the ceiling; ``xhigh`` and ``max`` are never used for any
subagent.  Before v0.2.97 every bundled agent pinned ``effort: high``
(v0.2.96) or ``xhigh`` (earlier), which made the most expensive reasoning
budget the silent default for every dispatch.

This test ratchets the shipped surface so the ruling cannot silently drift:

- no agent or skill frontmatter under ``templates/`` pins ``xhigh``/``max``;
- every shipped agent declares ``medium`` except the ALLOW_HIGH allowlist
  (each entry carries the one-line reason it was deliberately kept at the
  ceiling — an entry without a real reason is exactly the drift this test
  exists to catch);
- ``templates/ORCHESTRATOR-CLAUDE.md.template`` documents the ``medium``
  default and does not regress to telling readers to brief ad-hoc agents at
  ``high`` by default.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "templates" / "agents"
SKILLS_DIR = REPO_ROOT / "templates" / "skills"
ORCH_TEMPLATE = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"

VALID_EFFORTS = {"low", "medium", "high"}

# Agents deliberately kept at the `high` ceiling (genuinely hard-reasoning
# roles only).  Keyed by frontmatter `name`, value = reason it stays at high.
AGENTS_ALLOWED_HIGH: dict[str, str] = {
    "deep-researcher": "sustained multi-source research synthesis",
    "sre-incident-responder": "live-incident debugging under time pressure",
    "glm-reviewer": "adversarial review lane on real diffs",
}

# Skills deliberately kept at the `high` ceiling.  Keyed by skill dir name.
SKILLS_ALLOWED_HIGH: dict[str, str] = {
    "equation-check": "symbolic math verification (was xhigh before v0.2.97)",
    "terraform-plan-reviewer": "cross-resource implication analysis at scale",
}


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the flat ``key: value`` frontmatter of an agent/skill ``.md``."""
    fields: dict[str, str] = {}
    in_fm = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if in_fm and line.startswith(("effort:", "name:")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _agent_definitions() -> list[Path]:
    # `_archive/` never materializes for users (see test_v0297_shipped_script_refs).
    return sorted(
        p
        for p in AGENTS_DIR.rglob("*.md")
        if "_archive" not in p.parts
    )


def _skill_definitions() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def test_no_shipped_subagent_pins_xhigh_or_max() -> None:
    for path in _agent_definitions() + _skill_definitions():
        effort = _frontmatter(path).get("effort")
        assert effort in VALID_EFFORTS | {None}, (
            f"{path.relative_to(REPO_ROOT)} pins effort `{effort}` — xhigh/max "
            "are never used for subagents (owner ruling 2026-09-24, v0.2.97)"
        )


def test_bundled_agents_default_to_medium() -> None:
    for path in _agent_definitions():
        fields = _frontmatter(path)
        name = fields.get("name", path.stem)
        effort = fields.get("effort")
        if name in AGENTS_ALLOWED_HIGH:
            assert effort == "high", (
                f"{name} is on the ALLOW_HIGH allowlist but pins `{effort}` — "
                "drop the allowlist entry instead of keeping a stale exception"
            )
            continue
        if effort is None:
            continue  # no pin — inherits the session level, which is fine
        assert effort == "medium", (
            f"{name} ({path.relative_to(REPO_ROOT)}) pins effort `{effort}` — "
            "bundled agents ship at `medium`; `high` needs an ALLOW_HIGH entry "
            "with a reason"
        )


def test_bundled_skills_default_to_medium() -> None:
    for path in _skill_definitions():
        skill = path.parent.name
        effort = _frontmatter(path).get("effort")
        if skill in SKILLS_ALLOWED_HIGH:
            assert effort == "high", (
                f"{skill} is on the ALLOW_HIGH allowlist but pins `{effort}` — "
                "drop the allowlist entry instead of keeping a stale exception"
            )
            continue
        if effort is None:
            continue  # no pin — inherits the session level, which is fine
        assert effort == "medium", (
            f"{skill} ({path.relative_to(REPO_ROOT)}) pins effort `{effort}` — "
            "bundled skills ship at `medium`; `high` needs an ALLOW_HIGH entry "
            "with a reason"
        )


def test_orchestrator_template_documents_medium_default() -> None:
    text = ORCH_TEMPLATE.read_text(encoding="utf-8")
    assert "Every bundled agent ships at `medium`" in text, (
        "templates/ORCHESTRATOR-CLAUDE.md.template no longer states that "
        "bundled agents ship at `medium` (v0.2.97 default)"
    )
    assert "at `high` effort by default" not in text, (
        "templates/ORCHESTRATOR-CLAUDE.md.template regressed to telling "
        "readers to brief ad-hoc agents at `high` by default — the default "
        "is `medium` (owner ruling 2026-09-24)"
    )
