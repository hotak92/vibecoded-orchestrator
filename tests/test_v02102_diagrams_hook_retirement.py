# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 — the diagram HOOK registrations are retired, not orphaned.

Both diagram hook scripts stopped shipping in v0.2.102
(``pre-diagram-path-validation`` and the diagram-delete cascade
``post-file-delete``). Without a ``vco_lib/hook_retirements.py``
declaration, each existing install's registration would be preserved
byte-for-byte as "the user's own hook" FOREVER — the exact field failure
mode the retirement table was built for (see its module docstring).

These tests pin:
  1. one retirement row per script per OS extension, ``retired_in``
     ``v0.2.102``;
  2. the scrub actually DROPS a settings.json registration shaped like the
     ones VCO shipped (anchored command, matching event);
  3. ``post-file-delete``'s row is async-only (VCO shipped it exclusively
     as an async PostToolUse registration — SF-3 conservative shape) and
     the pre-diagram row is not;
  4. neither script ships in ``templates/hooks/`` anymore, and the async
     dispatcher no longer routes to ``post-file-delete``.

Each assertion was RED on the pre-change tree.
"""
from __future__ import annotations

from pathlib import Path

import vco_lib.hook_retirements as hr

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"


def _targets(event: str, retired_in: str) -> set[str]:
    return {
        e.target
        for e in hr.RETIRED_REGISTRATIONS
        if e.event == event and e.retired_in == retired_in
    }


def test_pre_diagram_path_validation_retired_both_os():
    targets = _targets("PreToolUse", "v0.2.102")
    assert "pre-diagram-path-validation.sh" in targets, targets
    assert "pre-diagram-path-validation.ps1" in targets, targets
    for e in hr.RETIRED_REGISTRATIONS:
        if e.target.startswith("pre-diagram-path-validation"):
            assert not e.async_only, e  # VCO shipped it as a SYNC hook


def test_post_file_delete_retired_both_os_async_only():
    targets = _targets("PostToolUse", "v0.2.102")
    assert "post-file-delete.sh" in targets, targets
    assert "post-file-delete.ps1" in targets, targets
    for e in hr.RETIRED_REGISTRATIONS:
        if e.target.startswith("post-file-delete") and e.retired_in == "v0.2.102":
            assert e.async_only, e  # VCO shipped it ONLY as an async registration


def test_scrub_drops_a_shipped_pre_diagram_registration():
    user_hooks = {
        "PreToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/'
                            'pre-diagram-path-validation.sh"'
                        ),
                        "timeout": 3,
                    }
                ],
            }
        ]
    }
    out, removals = hr.scrub_retired_registrations(user_hooks)
    assert removals, "the shipped pre-diagram registration must be scrubbed"
    assert "PreToolUse" not in out or out["PreToolUse"] == [], out


def test_scrub_drops_a_shipped_async_post_file_delete_registration():
    user_hooks = {
        "PostToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/'
                            'post-file-delete.sh"'
                        ),
                        "async": True,
                    }
                ],
            }
        ]
    }
    out, removals = hr.scrub_retired_registrations(user_hooks)
    assert removals, "the shipped async post-file-delete registration must go"
    # The dispatcher's own registration (same event) must SURVIVE.
    keep = {
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/'
                            'post-tool-use-async.sh"'
                        ),
                        "async": True,
                        "timeout": 15,
                    }
                ],
            }
        ]
    }
    combined = {
        "PostToolUse": user_hooks["PostToolUse"] + keep["PostToolUse"]
    }
    out2, removals2 = hr.scrub_retired_registrations(combined)
    assert len(removals2) == 1, removals2
    cmds = [h["command"] for g in out2["PostToolUse"] for h in g["hooks"]]
    assert any("post-tool-use-async" in c for c in cmds), out2


def test_diagram_hook_scripts_no_longer_ship():
    for name in (
        "pre-diagram-path-validation.sh",
        "pre-diagram-path-validation.ps1",
        "post-file-delete.sh",
        "post-file-delete.ps1",
    ):
        assert not (HOOKS / name).is_file(), f"templates/hooks/{name} ships"


def test_async_dispatcher_no_longer_routes_post_file_delete():
    for sibling in ("post-tool-use-async.sh", "post-tool-use-async.ps1"):
        text = (HOOKS / sibling).read_text(encoding="utf-8")
        assert "post-file-delete" not in text, sibling


def test_v02101_merged_stems_no_longer_include_post_file_delete():
    """The v0.2.101 merged-dispatcher set and the ROUTE_TABLE are pinned
    to each other by tests/test_v02101_async_posttooluse_dispatcher.py;
    post-file-delete now retires through its own v0.2.102 rows."""
    assert "post-file-delete" not in hr._ASYNC_MERGED_STEMS_V02101
