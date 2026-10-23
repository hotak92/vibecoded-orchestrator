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
    it must cover: the 11 default free agents + 12 machine-gated gateway agents
    (8 until v0.2.102 added the kimi pair and the two high-effort implementer
    siblings) + 6 default skills, plus every member of the opt-in packs. A moved
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
    assert default == 30, (
        f"the default catalogue is {default} definitions, expected 30 "
        "(11 free agents + 12 gateway agents + 7 skills)"
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


#: Agents whose ``tools`` include Write/Edit but which the WORKTREE_ISOLATION_GUIDE
#: deliberately exempts, each with the guide-derived reason. Every other agent
#: under ``templates/agents/`` and ``templates/packs/*/agents/`` that can Write
#: or Edit MUST carry ``isolation: worktree`` — the guide's rule ("Add
#: ``isolation: worktree`` to any agent that writes or edits source code
#: files"). Pack members ship to the same destinations as default agents, so
#: the guide's rule covers them equally.
ISOLATION_EXEMPT: dict[str, str] = {
    # guide: explicit "NOT enabled" list — defaults
    "agents/free/planner": "guide: planning only",
    "agents/free/code-explorer": "guide: read-only research (Write/Edit only saves its report file)",
    # v0.2.102 W6-K: the reviewer's Write is scoped BY ITS INSTRUCTIONS to its
    # own report file at the path the brief names (never source/tests/docs);
    # a worktree isolation would make it review a COPY and write the report
    # into the throwaway worktree — the opposite of the rule's intent.
    "agents/free/code-reviewer": "report-only review: Write saves its report file at the brief-named path, never source",
    "agents/free/tester": "guide: writes tests but low risk (tests go in tests/)",
    "agents/free/doc-maintainer": "guide: documentation",
    "agents/free/kg-maintainer": "guide: knowledge graph read/write to isolated state",
    # guide rule: report/artifact-only agents write documents, never source
    "agents/free/web-explorer": "report-only research: one written report, no source edits",
    "agents/free/gui-tester": "report-only: screenshots and structured reports, no source edits",
    "agents/module-gateway/deepseek-researcher": "report-only research lane",
    "agents/module-gateway/glm-flash-researcher": "report-only research lane",
    "agents/module-gateway/glm-planner": "planning/report-only lane",
    "agents/module-gateway/glm-reviewer": "review/report-only lane",
    "agents/module-gateway/kimi-reviewer": "review/report-only lane",
    # pack members judged by the same rule (review M1, v0.2.102)
    "packs/ai-engineering/agents/ai-agentic-architect": "multi-agent design documents, never execution",
    "packs/consulting/agents/consulting-cto-portfolio-coordinator": "portfolio summaries, no source edits",
    "packs/consulting/agents/consulting-employee-impersonator": "role-voice drafts, no source edits",
    "packs/consulting/agents/consulting-sow-drafter": "SOW/proposal documents",
    "packs/design-ux/agents/enterprise-ux-architect": "design deliverables, no source edits",
    "packs/gtm-marketing/agents/brand-identity-architect": "identity spec documents/artifacts",
    "packs/gtm-marketing/agents/inbox-triage-operator": "triage report only",
    "packs/gtm-marketing/agents/landing-page-critic": "critique report only",
    "packs/gtm-marketing/agents/launch-orchestrator": "launch copy artifacts",
    "packs/gtm-marketing/agents/outbound-sequence-writer": "outreach sequence drafts",
    "packs/ops-sre/agents/automation-engineer": "one implementation-ready spec document",
    "packs/science/agents/discipline-expert": "consultation/report documents",
    "packs/science/agents/experiment-designer": "experiment design documents",
    "packs/science/agents/paper-triage": "triage table report",
}

AGENT_DEFS = sorted(
    [
        *(
            p for p in (REPO / "templates" / "agents").rglob("*.md")
            if "_archive" not in p.parts
        ),
        *(REPO / "templates" / "packs").glob("*/agents/*.md"),
    ]
)


def _tools_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    assert isinstance(raw, list), f"tools frontmatter is neither string nor list: {raw!r}"
    return [str(t) for t in raw]


def _is_write_capable(tools: list[str]) -> bool:
    """Write/Edit in the list, or a list that inherits them.

    A missing or wildcard (``*``) ``tools`` field inherits ALL tools,
    including Write/Edit — such a definition is write-capable, never skipped.
    """
    return bool({"Write", "Edit", "*"} & set(tools)) or not tools


def test_write_capable_agents_carry_worktree_isolation() -> None:
    """Every shipped agent that can Write or Edit carries ``isolation: worktree``.

    ``templates/agents/WORKTREE_ISOLATION_GUIDE.md`` promises that agents
    which write or edit source code run in a harness-created worktree. The
    bundled free agents follow it; the gateway write lanes (implementers and
    the mechanical-edit sweeper) and the pack code writers (``gui-expert``,
    ``ai-llm-expert``) shipped without it, so their edits landed directly in
    the caller's tree. This pins the guide's rule against every shipped
    definition — defaults and pack members alike, which install to the same
    destinations — with the guide's own exemptions declared above.
    """
    templates_dir = REPO / "templates"
    violators: list[str] = []
    checked = 0
    for path in AGENT_DEFS:
        fm = _frontmatter(path)
        if fm is None:
            continue  # co-located doc (e.g. the guide itself), not a definition
        data = yaml.safe_load(fm)
        if not isinstance(data, dict) or not data.get("name"):
            continue
        rel = str(path.relative_to(templates_dir))[: -len(".md")]
        if "tools" not in data:
            violators.append(
                f"{rel}: frontmatter has no tools key (inherits ALL tools, "
                f"including Write/Edit — declare the tool list explicitly)"
            )
            continue
        tools = _tools_list(data.get("tools"))
        if not _is_write_capable(tools):
            continue
        checked += 1
        if rel in ISOLATION_EXEMPT:
            continue
        if data.get("isolation") != "worktree":
            violators.append(
                f"{rel}: tools include Write/Edit but frontmatter has no "
                f"'isolation: worktree' (tools: {tools})"
            )
    assert not violators, "\n".join(violators)
    # The exemption set must not drift: a stale entry (file gone, or the
    # agent since given isolation) would mask a future violation silently.
    def _agent_path(rel: str) -> Path:
        return templates_dir / f"{rel}.md"

    live = {str(p.relative_to(templates_dir))[: -len(".md")] for p in AGENT_DEFS}
    stale = sorted(set(ISOLATION_EXEMPT) - live)
    assert not stale, f"ISOLATION_EXEMPT names non-existent agents: {stale}"

    def _is_isolated(rel: str) -> bool:
        fm = _frontmatter(_agent_path(rel))
        data = yaml.safe_load(fm) if fm else None
        return isinstance(data, dict) and data.get("isolation") == "worktree"

    already_isolated = sorted(
        rel for rel in ISOLATION_EXEMPT
        if _agent_path(rel).exists() and _is_isolated(rel)
    )
    assert not already_isolated, (
        f"these exempt agents already carry isolation: worktree — remove them "
        f"from ISOLATION_EXEMPT: {already_isolated}"
    )
    assert checked >= len(ISOLATION_EXEMPT), (
        f"only {checked} write-capable agents found; the scan is broken"
    )
