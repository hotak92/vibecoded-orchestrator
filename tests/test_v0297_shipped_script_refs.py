# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shipped text may only reference ``.claude/scripts/<name>`` that VCO ships.

v0.2.97 promise sweep (``V0297-GLM-PROMISE-SWEEP-2026-09-24.md``) found four
distinct shipped references to scripts nothing provides — ``webhook-dlq``
(A1), ``upload_docs.py`` (B1), ``start-rl-server.*`` (C4),
``validate-readonly.sh`` (C10) — each one instructing a user or silently
gating a hook on a file that never lands in ``.claude/scripts/``.  The
shipping surface for scripts is ``templates/scripts/`` (materialized to
``.claude/scripts/`` by ``vco_lib/bundle_globs.py::script_patterns``), so
every ``.claude/scripts/<name>`` reference in shipped text must resolve
against it.

This test scans the shipped text surface — ``templates/**`` (minus
``templates/agents/_archive/``, which never materializes for users) and
``docs/**`` plus ``README.md`` — and fails on any reference that does not
resolve.  Legitimate non-shipped references must carry an ALLOWLIST entry
with a reason; an entry without a real reason is exactly the defect this
test exists to catch.

The same discipline applies to ``knowledge/concepts/<name>.md`` pointers
(v0.2.97 rounds 2-3): the curated set that SHIPS is ``templates/knowledge/``,
so every shipped pointer to a knowledge node must resolve against it.
The lean-ctx-shim-disabled pointers (round 2) and six maintainer-private
node pointers — blackboard-architecture-coordination,
embedding-service-v0218, gpu-mode-decision-policy,
hook-session-id-stdin-pattern, multi-codebase-code-graph-detection,
multi-source-kg-runtime — were the instances fixed in this cycle, each
replaced with the inline fact; the scan below keeps the class from
regrowing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: ``.claude/scripts/foo`` in either separator flavour.  The captured name
#: must start with a name character so bare directory mentions
#: (``.claude/scripts/``) and interpolation placeholders don't match.
_REF = re.compile(r"\.claude[/\\]scripts[/\\]([A-Za-z0-9_][A-Za-z0-9._\-]*)")

#: Scanned surface: everything users read that VCO ships.
_SCAN_ROOTS = ("templates", "docs")

#: Deliberately NOT scanned: ``templates/agents/_archive/`` never
#: materializes for users (agents ship from ``templates/agents/free/`` per
#: ``vco_lib/project_init.py``), and its archived drafts reference retired
#: tooling by design.
_EXCLUDED_PARTS = {"_archive", "__pycache__"}

#: References that are CORRECT to point at something ``templates/scripts/``
#: does not carry.  Every entry MUST state why — an unexplained entry is the
#: finding this test prevents.
_ALLOWLIST: dict[str, str] = {
    # C4: the RL launcher is provided by the PAID RL module when installed.
    # Nothing in the OSS tree creates it; the hook guards on existence so
    # its absence is a silent no-op (kept deliberately — do not "fix" by
    # inventing a script).
    "start-rl-server.sh": "provided by the paid RL module when installed; guarded no-op otherwise",
    "start-rl-server.ps1": "provided by the paid RL module when installed; guarded no-op otherwise",
    # C9 (verified NOT a finding): workflow-maintain's "Doc Check Script"
    # automation CREATES this script on demand in the user's project
    # (templates/skills/workflow-maintain/SKILL.md, "Creates: doc-check
    # script") — it is a product of running the skill, not a shipped file.
    "doc-check": "generated on demand by the workflow-maintain skill in the user's project",
    # Generic path-shape illustrations in shipped-script comments
    # (".claude/scripts/X → .claude → project" in sync_knowledge_graph.py,
    # "<root>/.claude/scripts/foo.py" in analyze_code_graph.py) — they
    # explain path arithmetic, not a script.
    "foo.py": "path-shape illustration in a comment, not a script reference",
    "X": "path-shape illustration in a comment, not a script reference",
}


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for base in _SCAN_ROOTS:
        for p in (REPO / base).rglob("*"):
            if not p.is_file():
                continue
            if _EXCLUDED_PARTS & set(p.parts):
                continue
            files.append(p)
    readme = REPO / "README.md"
    if readme.exists():
        files.append(readme)
    return sorted(files)


def _shipped_script_names() -> set[str]:
    return {p.name for p in (REPO / "templates" / "scripts").iterdir()}


def _normalize(name: str) -> str:
    # "code-graph-" (family mention, e.g. `.claude/scripts/code-graph-*`)
    # and sentence-final dots ("…run .claude/scripts/kg-sync.") are not
    # part of the name.
    return name.rstrip(".-")


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_claude_scripts_references_resolve(path: Path) -> None:
    shipped = _shipped_script_names()
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in _REF.finditer(text):
        raw = match.group(1)
        name = _normalize(raw)
        if name in shipped:
            continue
        # Family mention: `.claude/scripts/code-graph-*` names the prefix
        # of a real shipped family.
        if any(s.startswith(name + "-") for s in shipped):
            continue
        reason = _ALLOWLIST.get(name) or _ALLOWLIST.get(raw)
        assert reason is not None, (
            f"{path.relative_to(REPO)} references .claude/scripts/{raw}, "
            f"but templates/scripts/{raw or name} does not exist and no "
            "allowlist entry covers it. Either point the text at a script "
            "VCO ships, or add an _ALLOWLIST entry WITH a reason."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlist entry whose reference disappeared is stale — remove it.

    Kept honest by re-deriving the referenced set from the same scan the
    resolving test uses, so an entry can never outlive the text that
    justified it.
    """
    shipped = _shipped_script_names()
    referenced: set[str] = set()
    for path in _scan_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        referenced.update(_normalize(m.group(1)) for m in _REF.finditer(text))
        referenced.update(m.group(1) for m in _REF.finditer(text))
    for name in _ALLOWLIST:
        assert name in referenced or name in shipped, (
            f"Allowlist entry {name!r} no longer appears in the scanned "
            "surface — delete it so the list stays meaningful."
        )

#: ``knowledge/concepts/<name>.md`` pointers in shipped text.  These
#: promise a node the reader can open, so the node must ship in
#: ``templates/knowledge/`` (the curated set users receive) or carry an
#: allowlist entry saying why not.
_KNOWLEDGE_REF = re.compile(r"knowledge/concepts/([A-Za-z0-9_][A-Za-z0-9._\-]*\.md)")

_ILLUSTRATIVE = (
    # Names used ILLUSTRATIVELY in agent/skill instruction text: they
    # describe nodes that exist (or would be created) in the USER'S
    # project KG -- report citations ("Following pattern from ..."),
    # example Source lines, or example prompts.  They are not promises
    # that a specific curated node ships.
    "authentication-patterns.md",
    "authentication.md",
    "blackboard-coordination.md",
    "caching-strategies.md",
    "caching-strategy.md",
    "color-management-for-designers.md",
    "design-tokens-architecture.md",
    "email-deliverability-2026.md",
    "gradio-multi-tab-pattern.md",
    "hypothesis-testing-decision-tree.md",
    "icp-and-buyer-persona-framework.md",
    "information-density-heuristics.md",
    "jwt-tokens.md",
    "mcts-llm-planning.md",
    "north-star-metric-selection.md",
    "oauth2-auth.md",
    "password-security.md",
    "python-error-handling.md",
    "rate-limiting.md",
    "redis-caching-pattern.md",
    "rlm-context-loading.md",
    "secret-management.md",
    "unified-pattern.md",
    "vlm-consensus-pattern.md",
    "vlm-consensus.md",
    "vlm-patterns.md",
    "vram-management-strategy.md",
    "vram-management.md",
    "workflow-engine-tradeoffs-2026.md",
)

_PLACEHOLDER = (
    # Pure placeholders in examples ("e.g. knowledge/concepts/foo.md").
    "foo.md",
    "my-pattern.md",
    "node.md",
    "old-approach.md",
    "unused-pattern.md",
)

_KNOWLEDGE_ALLOWLIST: dict[str, str] = {
    **{
        name: (
            "illustrative node name in agent/skill instruction text -- "
            "describes a node in the user's project KG, not a shipped "
            "curated node"
        )
        for name in _ILLUSTRATIVE
    },
    **{
        name: "path-shape placeholder in an example, not a node reference"
        for name in _PLACEHOLDER
    },
}


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_knowledge_concepts_references_resolve(path: Path) -> None:
    """A shipped ``knowledge/concepts/<x>.md`` pointer must be openable.

    The curated set users receive is ``templates/knowledge/``; a pointer
    to anything else promises a file the user's install will never have.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in _KNOWLEDGE_REF.finditer(text):
        node = match.group(1)
        if (REPO / "templates" / "knowledge" / "concepts" / node).exists():
            continue
        reason = _KNOWLEDGE_ALLOWLIST.get(node)
        assert reason is not None, (
            f"{path.relative_to(REPO)} references "
            f"knowledge/concepts/{node}, but templates/knowledge/concepts/"
            f"{node} does not exist and no allowlist entry covers it. "
            "Either point the text at a node VCO ships, replace the "
            "pointer with the inline fact, or add an _KNOWLEDGE_ALLOWLIST "
            "entry WITH a reason."
        )


def test_knowledge_allowlist_has_no_stale_entries() -> None:
    """A knowledge allowlist entry whose reference vanished is stale."""
    referenced: set[str] = set()
    for path in _scan_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        referenced.update(m.group(1) for m in _KNOWLEDGE_REF.finditer(text))
    for name in _KNOWLEDGE_ALLOWLIST:
        assert name in referenced, (
            f"Knowledge allowlist entry {name!r} no longer appears in the "
            "scanned surface -- delete it so the list stays meaningful."
        )
