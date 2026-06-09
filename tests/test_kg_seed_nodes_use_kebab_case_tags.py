# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for Known Issue 6 sub-issue C (v0.2.52).

Every tag on every bundled KG seed node under ``templates/knowledge/``
must match the validator's allowed format — lowercase / digits /
hyphens, OR an all-caps acronym.

The validator at ``templates/scripts/sync_knowledge_graph.py:480-491``
emits the "Tag '<X>' uses camelCase" warning when a tag mixes upper and
lower case AND has more than one uppercase letter AND no hyphens.  This
permits acronyms like ``AI`` / ``RAG`` / ``MCP`` (all upper-case) but
rejects ``LoRA`` / ``IaC`` / ``ComfyUI`` (mixed case).

Pre-v0.2.52, 10 bundled seed nodes used camelCase tags (LoRA, QLoRA,
MoE, NeSy, DeepProbLog, OAuth, RoPE, IaC, ComfyUI, OpenAPI), spamming
the install banner with the "uses camelCase" warning for each occurrence
on every fresh seed.  This test pins the cleanup.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_ROOT = REPO_ROOT / "templates" / "knowledge"

# Mirror the validator's camelCase check from
# ``templates/scripts/sync_knowledge_graph.py:488-491`` EXACTLY.  A tag
# triggers the "uses camelCase" warning iff:
#
#     any(c.isupper() for c in tag)
#     AND not tag.isupper()
#     AND "-" not in tag
#     AND len([c for c in tag if c.isupper()]) > 1
#
# So the validator ALLOWS:
#   - all-lowercase tags                 (``fine-tuning``, ``embedding``)
#   - all-uppercase acronyms             (``AI``, ``RAG``, ``MCP``)
#   - tags with at least one hyphen      (``REST-API``, ``Q-learning``)
#   - tags with exactly one uppercase    (``Microsoft``, ``Anthropic``)
#
# And it REJECTS:
#   - camelCase / PascalCase without hyphens: ``LoRA``, ``ComfyUI``,
#     ``DeepProbLog``, ``QLoRA``, ``RoPE``, ``OAuth``, ``IaC``, ``MoE``,
#     ``NeSy``, ``OpenAPI``.
#
# Locking this test to the validator's behaviour (instead of imposing a
# stricter house style) means a future PR can't pass the validator and
# fail this test, or vice versa — they share one source of truth.
def _is_camel_case(tag: str) -> bool:
    """Replicates the validator's rejection rule character-for-character."""
    if not any(c.isupper() for c in tag):
        return False
    if tag.isupper():
        return False
    if "-" in tag:
        return False
    upper_count = sum(1 for c in tag if c.isupper())
    return upper_count > 1


def _iter_seed_node_tags():
    """Yield ``(path, tag)`` for every tag on every bundled seed node."""
    for md in KNOWLEDGE_ROOT.rglob("*.md"):
        if md.name in ("VOCABULARY.md", "TAG_HIERARCHY.md"):
            continue
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                # YAML might parse a tag like ``2025`` as int — coerce.
                yield md, str(tag)
                continue
            yield md, tag


class KGSeedTagsAreKebabCaseTests(unittest.TestCase):

    def test_every_tag_is_kebab_case_or_uppercase_acronym(self) -> None:
        """No tag may mix upper and lower case in a way that triggers
        the ``sync_knowledge_graph.py`` camelCase warning.
        """
        offenders: list[tuple[Path, str]] = []
        checked_tags = 0
        for path, tag in _iter_seed_node_tags():
            checked_tags += 1
            if _is_camel_case(tag):
                offenders.append((path, tag))

        self.assertGreater(
            checked_tags, 200,
            msg=f"only {checked_tags} tags checked — fixture moved or empty?",
        )

        if offenders:
            # Group by file for a readable diff.
            grouped: dict[str, list[str]] = {}
            for p, t in offenders:
                grouped.setdefault(str(p.relative_to(REPO_ROOT)), []).append(t)
            details = "\n".join(
                f"  {p}: {tags}" for p, tags in sorted(grouped.items())
            )
            self.fail(
                f"{len(offenders)} non-kebab-case tag(s) found across "
                f"{len(grouped)} seed node(s). Rename to lowercase with "
                f"hyphens (or use an all-uppercase acronym for short forms "
                f"like AI/RAG/MCP).\nOffenders:\n{details}"
            )

    def test_camel_case_lint_in_validator_matches_test_rule(self) -> None:
        """Lock the test rule against the validator source so they
        cannot drift independently.

        The rejection rule (above ``_is_camel_case``) is a literal port
        of the validator's check.  This test asserts the validator's
        warning string is still present in the source — if a refactor
        changes the message text (e.g. softens it), the rejection rule
        may also have moved and this test needs revisiting.
        """
        validator = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        text = validator.read_text(encoding="utf-8")
        self.assertIn("uses camelCase - use lowercase with hyphens", text)


if __name__ == "__main__":
    unittest.main()
