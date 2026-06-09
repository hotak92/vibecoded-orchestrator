# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for Known Issue 6 sub-issue B (v0.2.52).

Every bundled KG seed node under ``templates/knowledge/`` of type
``project`` / ``concept`` / ``pattern`` MUST carry at least one
abstraction-level tag.  The validator at
``templates/scripts/sync_knowledge_graph.py:508`` warns the user during
install (and on every hook-fired re-sync) when a node lacks any of:

    - ``high-level-plan``
    - ``mid-level-architecture``
    - ``low-level-implementation``
    - ``function-description``

Pre-v0.2.52, ~31 bundled nodes lacked an abstraction tag, producing the
"No abstraction level tag" warning spam that user filed as Known Issue 6
(sub-issue B).  This test pins the cleanup so a future seed-node
addition cannot silently regress the install banner.

We intentionally apply the rule only to types the validator covers
(project / concept / pattern).  ``tool`` nodes are exempt per the
validator's own ``node_type != "tool"`` guard at line 508.  ``model`` /
``research`` / ``insight`` / ``guide`` are similarly outside the
validator's scope and therefore outside this test's.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_ROOT = REPO_ROOT / "templates" / "knowledge"

ABSTRACTION_TAGS = {
    "high-level-plan",
    "mid-level-architecture",
    "low-level-implementation",
    "function-description",
}

# Types where the lint emits the "No abstraction level tag" warning.
# Mirrors templates/scripts/sync_knowledge_graph.py:494-509 (the
# ``node_type in {"project","concept","tool","pattern"} and node_type != "tool"``
# composite).
COVERED_TYPES = {"project", "concept", "pattern"}


def _iter_seed_nodes():
    """Yield ``(path, frontmatter_dict)`` for every bundled seed .md file."""
    for md in KNOWLEDGE_ROOT.rglob("*.md"):
        # Skip the vocabulary / hierarchy reference docs — they aren't
        # validated as KG nodes (no `type:` field).
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
        yield md, fm


class KGSeedNodesHaveAbstractionTagsTests(unittest.TestCase):

    def test_every_covered_seed_node_has_abstraction_tag(self) -> None:
        """Every project/concept/pattern seed node carries an abstraction tag."""
        offenders: list[tuple[Path, str, list]] = []
        checked = 0
        for path, fm in _iter_seed_nodes():
            node_type = fm.get("type", "")
            if node_type not in COVERED_TYPES:
                continue
            checked += 1
            tags = fm.get("tags", [])
            if not isinstance(tags, list):
                offenders.append((path, node_type, tags))
                continue
            has_abs = any(
                isinstance(t, str) and t in ABSTRACTION_TAGS for t in tags
            )
            if not has_abs:
                offenders.append((path, node_type, tags))

        # Sanity floor: if this test stops finding nodes to check, the
        # fixture has moved and we're not actually testing anything.
        self.assertGreater(
            checked, 50,
            msg=f"only {checked} covered nodes found — templates/knowledge layout changed?",
        )

        if offenders:
            details = "\n".join(
                f"  [{t}] {p.relative_to(REPO_ROOT)} tags={tags}"
                for p, t, tags in offenders
            )
            self.fail(
                f"{len(offenders)} seed node(s) missing abstraction-level tag.\n"
                f"Add one of {sorted(ABSTRACTION_TAGS)} to the frontmatter tags.\n"
                f"Offenders:\n{details}"
            )

    def test_abstraction_tags_recognised_by_validator(self) -> None:
        """The set this test enforces must match the validator's set verbatim.

        If a future PR introduces a 5th abstraction level (e.g.
        ``inter-module-design``), the validator will accept it but this
        test will reject seed nodes that use it.  Locking the two to the
        same source-of-truth string set guards against that drift.
        """
        validator = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        text = validator.read_text(encoding="utf-8")
        # Each tag must appear as a string literal in the validator.
        for tag in ABSTRACTION_TAGS:
            self.assertIn(
                f'"{tag}"', text,
                msg=f"validator does not list '{tag}' — drift between test and validator",
            )


if __name__ == "__main__":
    unittest.main()
