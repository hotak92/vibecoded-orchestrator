# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 catalogue plan §9.1 — the shipped catalogue + pack table, pinned.

The decision-of-record catalogue (plan §1) and the pack table (§3.1) are the
deliverable; this suite is its ratchet. Every name list here is the authority
in the plan, pinned exactly — a template added, removed, renamed, or a pack
member moved without the plan being amended fails here.

Covers:
* ``templates/agents/free/`` == the 11 kept default agents (names exact);
* ``templates/agents/module-gateway/`` == the 8 kept gateway agents, and
  ``tests/common/module_gateway.MODULE_GATEWAY_AGENT_FILES`` equals the live
  directory listing (the delivery contract cannot drift from the tree);
* ``templates/skills/`` == the 6 kept default skills (names exact);
* ``templates/packs/packs.toml`` parses through the ONE reader
  (``vco_lib.packs.load_packs`` — which itself validates member existence,
  cross-pack uniqueness and no-double-delivery loudly), and the 11 packs carry
  exactly the §3.1 members (19 agent moves + 30 skill moves);
* ``templates/specializations/`` contains exactly the §5 doc list;
* zero ``skills:`` frontmatter keys under ``templates/agents/`` (owner R4);
* the default catalogue's frontmatter ``description`` chars stay inside the
  §8 budget — SUM ≤ 12 000 (packs excluded) and ≤ 240 per item — pinned with
  a synthetic positive control proving the measurement counts.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import packs as packs_mod  # noqa: E402

# ── Plan §1.1: the 11 kept default free agents (names exact). ──────────────
FREE_AGENTS = {
    "agent-author", "code-explorer", "code-reviewer", "doc-maintainer",
    "expert-coder", "frontend-specialist", "gui-tester", "kg-maintainer",
    "planner", "tester", "web-explorer",
}

# ── Plan §1.2: the 8 kept gateway agents (2 retired by merge). ─────────────
GATEWAY_AGENTS = {
    "deepseek-implementer", "deepseek-researcher", "glm-flash-researcher",
    "glm-implementer", "glm-planner", "glm-reviewer", "qwen-flash-sweeper",
    "qwen-implementer",
}

# ── Plan §1.3: the 6 kept default skills. ──────────────────────────────────
DEFAULT_SKILLS = {
    "context-compress", "fix-issue", "orchestrator-installer",
    "project-bootstrapper", "rc-native", "task-breakdown",
}

# ── Plan §3.1: the 11 packs and their exact members (the table IS the
# authority; 19 agent moves + 30 skill moves). ─────────────────────────────
PACK_MEMBERS: dict[str, set[str]] = {
    "dev-advisors": {
        "accessibility-checker", "ai-rag-advisor", "architect",
        "debug-expert", "security-reviewer"},
    "devops-reliability": {
        "idempotency-keys", "k8s-manifest-reviewer", "slo-designer",
        "terraform-plan-reviewer", "webhook-receiver"},
    "ai-engineering": {
        "ai-llm-expert", "ai-agentic-architect",
        "structured-output-extraction", "workflow-cost-estimator"},
    "science": {
        "discipline-expert", "experiment-designer", "paper-triage",
        "equation-check", "hpc-submit", "repro-audit", "stats-consult"},
    "marketing-sales-product": {
        "build-vs-buy-decision", "content-calendar-planner",
        "saas-metrics-health-check", "saas-pricing-strategist",
        "sales-call-prep", "seo-content-brief"},
    "consulting": {
        "consulting-cto-portfolio-coordinator",
        "consulting-employee-impersonator", "consulting-sow-drafter",
        "consulting-due-diligence", "consulting-incident-coordinator",
        "consulting-portfolio-status"},
    "design-media": {
        "ai-image-prompting", "batch-image-pipeline", "design-system-auditor",
        "gui-ux-expert", "photoshop-scripting"},
    "design-ux": {"gui-expert", "enterprise-ux-architect"},
    "gtm-marketing": {
        "brand-identity-architect", "landing-page-critic",
        "outbound-sequence-writer", "inbox-triage-operator",
        "launch-orchestrator"},
    "ops-sre": {
        "sre-incident-responder", "postmortem-author", "automation-engineer"},
    "migration": {"code-migrator"},
}
PACK_AGENT_MOVES = 19   # plan §3.1 (the draft's 18 was off by one)
PACK_SKILL_MOVES = 30

# ── Plan §5: the specializations doc set (languages/ ships only when a doc
# exists — today it does not, so the three subfolders are the whole tree). ──
SPECIALIZATION_DOCS = {
    "fields/backend.md", "fields/api-design.md", "fields/database.md",
    "fields/deployment.md", "fields/frontend.md", "fields/prompt-engineering.md",
    "review-kinds/code.md", "review-kinds/security.md", "review-kinds/test.md",
    "review-kinds/architecture-design.md", "review-kinds/docs-vs-code.md",
    "review-topics/performance.md", "review-topics/frontend-ui-a11y.md",
    "review-topics/database-migrations.md", "review-topics/api-design.md",
    "review-topics/infra-ci.md", "review-topics/data-ml.md",
}


# ── Plan §8: the description budget. Every default agent/skill description
# loads into every session's startup context, so the SUM across the default
# catalogue is capped at 12 000 chars (≈3k tokens — far under Claude Code's
# 15 000-token all-descriptions startup warning) and no single item may
# exceed 240 chars. Packs are excluded: they load only where opted in. ────
DESCRIPTION_BUDGET_TOTAL = 12_000
DESCRIPTION_MAX_PER_ITEM = 240

# The one frontmatter block extractor for this suite (used by the `skills:`
# ban below AND the budget tests — one concern, one home).
_FRONTMATTER_BLOCK = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)


def _frontmatter_mapping(path: Path) -> dict:
    """Parsed frontmatter of a shipped definition.

    Reuses the two parsers the catalogue suites already rely on: this
    file's ``---`` block regex and the ``yaml.safe_load`` of that block
    from ``tests/test_v02100_shipped_frontmatter_parses.py`` (which also
    pins that every shipped block is a YAML mapping with a description).
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_BLOCK.match(text)
    if m is None:
        raise AssertionError(f"{path}: no frontmatter block")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise AssertionError(f"{path}: frontmatter is not a mapping")
    return data


def _default_catalogue_files(base: Path) -> list[Path]:
    """The default-catalogue definition files under ``base`` — free agents +
    gateway agents + default skills; packs excluded (plan §8 measures what
    EVERY project loads at startup)."""
    return sorted([
        *base.glob("templates/agents/free/*.md"),
        *base.glob("templates/agents/module-gateway/*.md"),
        *base.glob("templates/skills/*/SKILL.md"),
    ])


def _budget_problems(base: Path) -> list[str]:
    """Every description-budget violation under ``base`` (empty = inside
    budget). Takes the root to measure so the positive-control arm can run
    the SAME code path against a synthetic tree."""
    lengths = {
        p: len(str(_frontmatter_mapping(p).get("description", "")))
        for p in _default_catalogue_files(base)
    }
    problems: list[str] = []
    total = sum(lengths.values())
    if total > DESCRIPTION_BUDGET_TOTAL:
        problems.append(
            f"total description chars {total} > budget "
            f"{DESCRIPTION_BUDGET_TOTAL}")
    for p, n in sorted(lengths.items()):
        if n > DESCRIPTION_MAX_PER_ITEM:
            problems.append(
                f"{p.relative_to(base).as_posix()}: description {n} chars "
                f"> per-item cap {DESCRIPTION_MAX_PER_ITEM}")
    return problems


def _write_synthetic_definition(
        root: Path, rel: str, name: str, description: str) -> None:
    """Plant one synthetic definition (agent ``.md`` or skill ``SKILL.md``)
    at ``rel`` under ``root`` for the budget control arms."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8")


def _stems(directory: Path) -> set[str]:
    return {p.stem for p in directory.glob("*.md")}


class DefaultCatalogueTests(unittest.TestCase):
    def test_free_agents_are_the_eleven_kept(self):
        self.assertEqual(_stems(REPO_ROOT / "templates/agents/free"), FREE_AGENTS)

    def test_gateway_agents_are_the_eight_kept(self):
        self.assertEqual(
            _stems(REPO_ROOT / "templates/agents/module-gateway"), GATEWAY_AGENTS)

    def test_gateway_delivery_contract_equals_live_directory(self):
        # The named-once delivery contract must equal the directory listing —
        # mutate one name in either and this fails (the §9.1 red-proof arm).
        from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES
        self.assertEqual(
            sorted(MODULE_GATEWAY_AGENT_FILES),
            sorted(p.name for p in
                   (REPO_ROOT / "templates/agents/module-gateway").glob("*.md")))
        # Positive control: the pin above is not vacuous — dropping a name
        # from one side makes the two sets unequal.
        self.assertNotEqual(
            sorted(MODULE_GATEWAY_AGENT_FILES),
            sorted(MODULE_GATEWAY_AGENT_FILES[:-1] + ("renamed-agent.md",)))

    def test_default_skills_are_the_six_kept(self):
        skills_dir = REPO_ROOT / "templates/skills"
        self.assertEqual(
            {p.name for p in skills_dir.iterdir() if p.is_dir()}, DEFAULT_SKILLS)

    def test_no_skills_frontmatter_under_templates_agents(self):
        # Owner R4 / plan §1.1: NO `skills:` frontmatter anywhere in the
        # shipped agents (the 14 pre-v0.2.101 blocks were stripped).
        offenders = []
        for md in (REPO_ROOT / "templates/agents").rglob("*.md"):
            m = _FRONTMATTER_BLOCK.match(md.read_text(encoding="utf-8"))
            if m and re.search(r"^skills\s*:", m.group(1), re.MULTILINE):
                offenders.append(str(md.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


class PackTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # load_packs IS the validator: member existence, malformed member
        # paths, cross-pack uniqueness (path AND installed name) and
        # no-double-delivery with the default catalogue all raise here.
        cls.table = packs_mod.load_packs(REPO_ROOT)

    def test_table_parses_with_exactly_the_eleven_packs(self):
        self.assertEqual(set(self.table), set(PACK_MEMBERS))

    def test_every_pack_carries_exactly_its_planned_members(self):
        for name, expected in PACK_MEMBERS.items():
            self.assertEqual(
                set(packs_mod.member_names(self.table[name])), expected,
                f"pack {name!r} members drifted from the §3.1 table")

    def test_member_move_counts(self):
        agents = skills = 0
        for pack in self.table.values():
            for member in pack.members:
                parts = Path(member).parts
                if parts[1] == "agents":
                    agents += 1
                else:
                    skills += 1
        self.assertEqual((agents, skills), (PACK_AGENT_MOVES, PACK_SKILL_MOVES))

    def test_every_member_file_exists_on_disk(self):
        # load_packs already proved this; pin the walk independently so a
        # validator regression cannot hide a missing member.
        base = REPO_ROOT / "templates/packs"
        for pack in self.table.values():
            for member in pack.members:
                self.assertTrue((base / member).is_file(), member)

    def test_no_member_double_delivered_with_defaults(self):
        default_names = (
            _stems(REPO_ROOT / "templates/agents/free")
            | _stems(REPO_ROOT / "templates/agents/module-gateway")
            | {p.name for p in (REPO_ROOT / "templates/skills").iterdir()
               if p.is_dir()})
        for pack in self.table.values():
            overlap = default_names & set(packs_mod.member_names(pack))
            self.assertEqual(overlap, set(), pack.name)

    def test_broken_table_raises_loudly(self):
        # The validator's posture: a broken table is a broken install — a
        # raised PacksTableError, never an empty catalogue.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(packs_mod.PacksTableError):
                packs_mod.load_packs(root)  # table missing entirely
            (root / "templates/packs").mkdir(parents=True)
            (root / "templates/packs/packs.toml").write_text(
                "[pack.p]\nmembers = ['p/agents/ghost.md']\n", encoding="utf-8")
            with self.assertRaises(packs_mod.PacksTableError):  # member absent
                packs_mod.load_packs(root)


class SpecializationsTests(unittest.TestCase):
    def test_specializations_tree_is_exactly_the_planned_docs(self):
        base = REPO_ROOT / "templates/specializations"
        on_disk = {
            p.relative_to(base).as_posix()
            for p in base.rglob("*") if p.is_file()}
        self.assertEqual(on_disk, SPECIALIZATION_DOCS)


class DescriptionBudgetTests(unittest.TestCase):
    """Plan §8 — the description-budget ratchet (round-A review SF-1/SF-2).

    Every default agent/skill description loads into every session's startup
    context, so the catalogue's TOTAL description chars are capped at 12 000
    and each item at 240 (detail belongs in the body, which loads only when
    the definition runs). The synthetic control arms prove the measurement
    counts — a parser stuck at zero, or a scan pinned to the repo root,
    passes the real-tree arm vacuously and is caught here instead.
    """

    def test_scan_covers_exactly_the_default_catalogue(self):
        # Non-vacuity pin: the budget measures exactly the 25 default
        # definitions — the 11 free agents + 8 gateway agents + 6 skills
        # pinned above — and nothing else (packs excluded by construction).
        rel = {
            p.relative_to(REPO_ROOT / "templates").as_posix()
            for p in _default_catalogue_files(REPO_ROOT)
        }
        expected = (
            {f"agents/free/{n}.md" for n in FREE_AGENTS}
            | {f"agents/module-gateway/{n}.md" for n in GATEWAY_AGENTS}
            | {f"skills/{n}/SKILL.md" for n in DEFAULT_SKILLS}
        )
        self.assertEqual(rel, expected)

    def test_default_catalogue_is_within_the_description_budget(self):
        # The plan §8 pin itself: SUM ≤ 12 000 AND every item ≤ 240.
        self.assertEqual(_budget_problems(REPO_ROOT), [])

    def test_over_budget_synthetic_catalogue_is_flagged(self):
        # Positive control (plan §8 requires one): a synthetic catalogue
        # with an over-cap item AND an over-budget total is flagged on
        # BOTH arms by the same code path the real pin uses.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_synthetic_definition(
                root, "templates/agents/free/ok-agent.md", "ok-agent",
                "A short, well-behaved description.")
            _write_synthetic_definition(
                root, "templates/agents/module-gateway/over-agent.md",
                "over-agent", "x" * (DESCRIPTION_MAX_PER_ITEM + 1))
            _write_synthetic_definition(
                root, "templates/skills/over-skill/SKILL.md", "over-skill",
                "y" * (DESCRIPTION_BUDGET_TOTAL + 1))

            self.assertEqual(
                len(_default_catalogue_files(root)), 3,
                "the synthetic scan must find every planted definition")
            problems = _budget_problems(root)
            self.assertTrue(
                any("total description chars" in p for p in problems),
                f"over-budget total not flagged: {problems}")
            self.assertTrue(
                any("over-agent" in p for p in problems),
                f"over-cap item not flagged: {problems}")
            self.assertTrue(
                any("over-skill" in p for p in problems),
                f"over-cap skill not flagged: {problems}")

    def test_within_budget_synthetic_catalogue_is_clean(self):
        # Negative control: the arm above is not simply always-red — a
        # synthetic catalogue inside both limits measures clean through
        # the same code path.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_synthetic_definition(
                root, "templates/agents/free/small-agent.md", "small-agent",
                "Does one bounded thing; one report.")
            _write_synthetic_definition(
                root, "templates/skills/small-skill/SKILL.md", "small-skill",
                "Does one bounded thing.")
            self.assertEqual(_budget_problems(root), [])


if __name__ == "__main__":
    unittest.main()
