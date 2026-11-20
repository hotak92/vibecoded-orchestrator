# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 -- every shipped hook registration states its timeout.

Six registrations (pre-compact-save, agent-alerts-deliver on
UserPromptSubmit and PostToolUse, subagent-start-kg-inject,
subagent-stop-reconcile, worktree-guard) had none and ran on the harness's
default. They were left without one on purpose -- each must never be killed
mid-run -- so they now state 600 s, the harness's command-hook default:
the same bound, written down. Shipped timeouts are deliberately long; this
test also keeps every registration's timeout positive.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = {"linux": ROOT / "templates" / "settings.json.linux.template",
             "windows": ROOT / "templates" / "settings.json.windows.template"}
LONG = {"pre-compact-save", "agent-alerts-deliver", "subagent-start-kg-inject",
        "subagent-stop-reconcile", "worktree-guard"}


def _handlers(doc):
    for event, groups in doc["hooks"].items():
        for g in groups:
            for h in g.get("hooks", []):
                yield event, h


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_every_registration_has_a_timeout(os_name):
    doc = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    missing = [(e, h["command"]) for e, h in _handlers(doc)
               if not isinstance(h.get("timeout"), int) or h["timeout"] <= 0]
    assert missing == []


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_the_never_kill_hooks_state_the_harness_default(os_name):
    doc = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    seen = 0
    for _e, h in _handlers(doc):
        stem = h["command"].rstrip('"\\').rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem in LONG:
            seen += 1
            assert h["timeout"] == 600, (stem, h)
    assert seen == 6
