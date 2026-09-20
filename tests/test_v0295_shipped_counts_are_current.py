# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The advertised agent / skill / hook counts must equal the shipped tree.

v0.2.95 review MINOR-1: `post-bash-file-sync` was hook number 46 and seven
user-facing sites still said 45 (the review named six; the seventh,
docs/INSTALL_RECOVERY.md, turned up in the sweep that fixed them) — the same drift lane C had just repaired
from 44. Nothing pinned the numbers, so each new hook re-opened the finding
and the next reader had to re-count by hand.

A LITERAL ratchet is the right shape here and the only possible one: the
subject IS a number written in prose. There is no behaviour to drive. So
each site is asserted with the MEASURED value interpolated — adding a hook
turns these red and the fix is to edit the sentence, which is exactly the
work that kept being skipped.

Positive control (`test_the_assertions_bind_to_the_number`): the same
sentences with a deliberately wrong count must NOT be found. Without it a
fuzzy needle could match regardless of the digits and the ratchet would be
decorative.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"

#: `bash .claude/hooks/<name>.sh …` inside a settings template command.
_REGISTERED = re.compile(r"\.claude/hooks/([A-Za-z0-9_.-]+)\.sh")


def _agents() -> int:
    return len(list((REPO_ROOT / "templates" / "agents" / "free").glob("*.md")))


def _skills() -> int:
    return len(list((REPO_ROOT / "templates" / "skills").rglob("SKILL.md")))


def _hooks() -> int:
    """Top-level hook scripts. `_lib/` helpers are libraries, not hooks."""
    return len(list(HOOKS_DIR.glob("*.sh")))


def _registered(template: str = "linux") -> set[str]:
    doc = json.loads(
        (REPO_ROOT / "templates" / f"settings.json.{template}.template").read_text(
            encoding="utf-8"
        )
    )
    names: set[str] = set()
    for groups in doc["hooks"].values():
        for group in groups:
            for hook in group.get("hooks", []):
                names.update(_REGISTERED.findall(hook.get("command", "")))
    return names


def _sites() -> list[tuple[str, str]]:
    """``(path, sentence)`` with the measured counts interpolated."""
    a, s, h, r = _agents(), _skills(), _hooks(), len(_registered())
    return [
        ("README.md", f"**Yes ({h} hooks, {s} skills, {a} agents)**"),
        ("README.md", f"KG, code graph, {h} hooks, {a} agents, {s} skills"),
        ("docs/GETTING_STARTED.md", f"renders {h} hooks (both `.sh` and `.ps1`"),
        ("docs/features/INDEX.md", f"{a} free agents, {s} skills, and {h} hooks."),
        ("docs/features/INDEX.md", f"| {a} free agents, {s} skills, {h} hooks,"),
        (
            "docs/features/03-agents-skills-hooks.md",
            f"{s} skills, and {h} hooks ({r} event-registered",
        ),
        ("docs/INSTALL_RECOVERY.md", f"enable/disable for the {h} shipped"),
        ("vct-module.json", f"Hooks: {h} hook scripts"),
        ("vct-module.json", f"Of the {h}, {r} are event-registered"),
        ("vct-module.json", f"Skills: {s} — `find templates/skills"),
        ("vct-module.json", f"The core: {h} hooks, {a} bundled agents, {s} skills"),
    ]


@pytest.mark.parametrize(("relpath", "needle"), _sites())
def test_the_advertised_counts_match_the_shipped_tree(relpath: str, needle: str) -> None:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert needle in text, (
        f"{relpath} no longer states the measured counts.\n"
        f"  expected to find: {needle!r}\n"
        f"  measured: {_agents()} agents, {_skills()} skills, {_hooks()} hooks "
        f"({len(_registered())} event-registered)\n"
        "A shipped count that drifts is a promise the tree stopped keeping — "
        "update the sentence, do not relax this test."
    )


@pytest.mark.parametrize(("relpath", "needle"), _sites())
def test_the_assertions_bind_to_the_number(relpath: str, needle: str) -> None:
    """Positive control: the same sentence with a wrong count must be absent."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    wrong = re.sub(r"\d+", lambda m: str(int(m.group()) + 1), needle, count=1)
    assert wrong != needle, f"no digit in the needle to perturb: {needle!r}"
    assert wrong not in text, (
        f"{relpath} contains BOTH {needle!r} and {wrong!r} — the needle does "
        "not discriminate, so the ratchet above proves nothing. Make it more "
        "specific."
    )


def test_both_settings_templates_register_the_same_hooks() -> None:
    """A hook registered on one OS only is half-delivered.

    The windows template names `.ps1`; compare basenames.
    """
    windows = json.loads(
        (REPO_ROOT / "templates" / "settings.json.windows.template").read_text(
            encoding="utf-8"
        )
    )
    win_names: set[str] = set()
    for groups in windows["hooks"].values():
        for group in groups:
            for hook in group.get("hooks", []):
                win_names.update(
                    re.findall(
                        r"\.claude[/\\]hooks[/\\]([A-Za-z0-9_.-]+)\.ps1",
                        hook.get("command", ""),
                    )
                )
    assert _registered() == win_names, (
        "linux and windows settings templates register different hooks:\n"
        f"  linux-only : {sorted(_registered() - win_names)}\n"
        f"  windows-only: {sorted(win_names - _registered())}"
    )
