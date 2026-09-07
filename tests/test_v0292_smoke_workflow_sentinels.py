# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The tri-OS smoke legs must assert the bundle DELIVERED (m5, 2026-09-05).

Pre-fix, "Verify install artifacts" checked `.venv`/`state`/`.env` —
directory-level facts that only prove the install RAN. A regression in
hook/script/skill DELIVERY (the bundle step skipping files on Windows)
would pass the only Windows CI leg. The workflow now asserts one file per
bundle kind; this test keeps that true in both legs and keeps the named
sentinels pointing at files that actually ship (renaming a template
without updating the workflow reds here, not on a Friday CI run).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "install-smoke-tri-os.yml"
TEMPLATES = REPO / "templates"

#: Per verify leg: bundle kind → the installed path it must assert (and
#: the shipped template that path comes from — hooks land as
#: `.claude/hooks/<name>`, scripts as `.claude/scripts/<name>`, skills as
#: `.claude/skills/<dir>/SKILL.md`; the POSIX leg's script sentinel is the
#: extension-less wrapper the sibling convention ships there).
LEG_SENTINELS = {
    "Verify install artifacts (POSIX)": {
        "hook": ("hooks/pre-tool-use.sh", ".claude/hooks/pre-tool-use.sh"),
        "script": ("scripts/kg-sync", ".claude/scripts/kg-sync"),
        "skill": ("skills/rc-native/SKILL.md", ".claude/skills/rc-native/SKILL.md"),
    },
    "Verify install artifacts (Windows)": {
        "hook": ("hooks/pre-tool-use.ps1", ".claude/hooks/pre-tool-use.ps1"),
        "script": ("scripts/kg-sync.ps1", ".claude/scripts/kg-sync.ps1"),
        "skill": ("skills/rc-native/SKILL.md", ".claude/skills/rc-native/SKILL.md"),
    },
}


def _leg_body(text: str, name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(name)}\n(.*?)(?=\n      - name:|\Z)",
        text,
        re.S,
    )
    assert step, f"step {name!r} not found in the workflow"
    return step.group(1)


@pytest.mark.parametrize("step_name", list(LEG_SENTINELS))
def test_every_verify_leg_asserts_one_file_per_bundle_kind(step_name: str) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = _leg_body(text, step_name)
    for kind, (template_rel, installed) in LEG_SENTINELS[step_name].items():
        assert installed.replace("/", "\\") in body or installed in body, (
            f"{step_name} does not assert the {kind} sentinel {installed!r} — "
            "a delivery regression in that bundle kind would pass this leg"
        )


def test_the_sentinels_name_files_that_actually_ship() -> None:
    """The sentinel paths must exist in templates/ — otherwise the gate
    asserts a file the bundle never delivers (and passes forever)."""
    for leg in LEG_SENTINELS.values():
        for template_rel, installed in leg.values():
            assert (TEMPLATES / template_rel).is_file(), (
                f"smoke sentinel {installed!r} has no template at "
                f"templates/{template_rel} — the workflow is asserting a "
                "path the bundle never delivers"
            )
