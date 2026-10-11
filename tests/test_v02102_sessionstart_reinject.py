# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-F items 5+6 — SessionStart resume/startup re-inject.

Pre-fix defects (research report 2026-10-10, items 8 and 13 — verified):

* The ORCHESTRATOR template documents "**SessionStart (compact/resume)**:
  compact-context-reinject.sh" but the settings templates register it under
  matcher ``compact`` ONLY — a ``claude --resume`` re-injects nothing.
* ``diff-context-inject.{sh,ps1}`` carry the comment "full injection handled
  by session-start hooks", but NO startup hook injected CONTEXT_STATE.md at
  all.

Post-fix contract:

* Both settings templates register compact-context-reinject under
  ``startup`` and ``compact`` ONLY (kg-update-nudge stays compact-only).
  W6-F had also registered ``resume``; v0.2.102 W6-K withdrew it (owner
  2026-10-11: a resumed chat already has CONTEXT_STATE in context) via a
  matcher-scoped retirement row — the scrub removes the resume group from
  an install that received the W6-F shape, and never touches the
  startup/compact groups of the same script.
* The settings merge delivers the two new registrations to an existing
  install (user file with the v0.2.101 compact-only shape) exactly once,
  idempotently, never touching the user's own hooks.
* The hook honours the payload ``source`` (kept even though the resume
  REGISTRATION is gone — a user may still register it themselves):
  startup/resume inject
  CONTEXT_STATE.md (in full, inside the W6-D per-section budgets and the
  shared cap) but SKIP the compaction artefacts (the pre-compact snapshot
  and pruned-activity summary are meaningless outside compaction);
  source=compact (or absent — the pre-W6-F payload shape) is unchanged.
* diff-context-inject's comment names the real mechanism.

RED on the pre-fix tree: no resume/startup registration, and the hook has
no source awareness.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import settings_merge

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
LINUX = REPO / "templates" / "settings.json.linux.template"
WINDOWS = REPO / "templates" / "settings.json.windows.template"
TPLS = [LINUX, WINDOWS]
HAVE_PWSH = shutil.which("pwsh") is not None
PWSH = shutil.which("pwsh") or "pwsh"
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    not HAVE_PWSH, reason="pwsh not installed"))]
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")


def _sessionstart(doc: dict) -> list:
    return doc["hooks"]["SessionStart"]


def _matchers_for(doc: dict, stem: str) -> set:
    out = set()
    for g in _sessionstart(doc):
        for h in g.get("hooks", []):
            if f"{stem}." in h.get("command", ""):
                out.add(g.get("matcher") or "")
    return out


# --- registration pins ----------------------------------------------------------


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_reinject_registered_for_startup_and_compact_only(tpl: Path):
    """W6-K (owner 2026-10-11): resume must NOT re-inject — a resumed chat
    already has CONTEXT_STATE in context."""
    doc = json.loads(tpl.read_text(encoding="utf-8"))
    matchers = _matchers_for(doc, "compact-context-reinject")
    assert matchers == {"startup", "compact"}, (
        f"{tpl.name}: the re-inject needs exactly the startup and compact "
        f"matchers (resume withdrawn, W6-K); got {sorted(matchers)}"
    )


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_nudge_stays_compact_only(tpl: Path):
    """The nudge's own semantics exclude resume (it reattaches to existing
    state) — the resume widening must not sweep it up."""
    doc = json.loads(tpl.read_text(encoding="utf-8"))
    assert _matchers_for(doc, "kg-update-nudge") == {"compact"}


# --- the settings-merge migration ------------------------------------------------


def _v02101_user(ext: str) -> dict:
    """An installed file in the v0.2.101 shape: the reinject under matcher
    "compact" only, plus the user's own startup hook."""
    cmd = (f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/'
           f'compact-context-reinject.{ext}"' if ext == "sh" else
           f'powershell -NoProfile -ExecutionPolicy Bypass -File '
           f'"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/'
           f'compact-context-reinject.{ext}"')
    own = {"type": "command", "command": "bash ~/my-own-startup.sh",
           "timeout": 3}
    return {"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [dict(own)]},
        {"matcher": "compact", "hooks": [
            {"type": "command", "command": cmd, "timeout": 5}]},
    ]}}


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_startup_registration_migrates_once(tpl: Path):
    template = json.loads(tpl.read_text(encoding="utf-8"))
    ext = "sh" if tpl is LINUX else "ps1"
    user = _v02101_user(ext)
    once = settings_merge.smart_merge_settings(user, template)
    matchers = _matchers_for(once, "compact-context-reinject")
    assert {"startup", "compact"} <= matchers, (
        f"the merge must deliver the startup registration; got {sorted(matchers)}"
    )
    # Idempotent: merging its own output changes nothing.
    twice = settings_merge.smart_merge_settings(once, template)
    assert twice["hooks"]["SessionStart"] == once["hooks"]["SessionStart"]


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_w6f_resume_registration_is_scrubbed(tpl: Path):
    """W6-K: an install that received W6-F's resume group loses it on the
    next ordinary update, and ONLY it — the same script's startup and
    compact groups survive the merge untouched."""
    template = json.loads(tpl.read_text(encoding="utf-8"))
    ext = "sh" if tpl is LINUX else "ps1"
    cmd = (f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/'
           f'compact-context-reinject.{ext}"' if ext == "sh" else
           f'powershell -NoProfile -ExecutionPolicy Bypass -File '
           f'"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/'
           f'compact-context-reinject.{ext}"')
    w6f_user = {"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [
            {"type": "command", "command": cmd, "timeout": 5}]},
        {"matcher": "compact", "hooks": [
            {"type": "command", "command": cmd, "timeout": 5}]},
        {"matcher": "resume", "hooks": [
            {"type": "command", "command": cmd, "timeout": 5}]},
    ]}}
    merged = settings_merge.smart_merge_settings(w6f_user, template)
    matchers = _matchers_for(merged, "compact-context-reinject")
    assert "resume" not in matchers, (
        f"the withdrawn W6-F resume registration must be scrubbed; "
        f"got {sorted(matchers)}"
    )
    assert {"startup", "compact"} <= matchers
    # Idempotent: a second merge changes nothing (no resurrection, no
    # double-removal churn).
    twice = settings_merge.smart_merge_settings(merged, template)
    assert twice["hooks"]["SessionStart"] == merged["hooks"]["SessionStart"]


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_user_own_sessionstart_hook_is_untouched(tpl: Path):
    template = json.loads(tpl.read_text(encoding="utf-8"))
    ext = "sh" if tpl is LINUX else "ps1"
    user = _v02101_user(ext)
    merged = settings_merge.smart_merge_settings(user, template)
    own_groups = [
        g for g in merged["hooks"]["SessionStart"]
        if any("my-own-startup" in h.get("command", "") for h in g.get("hooks", []))
    ]
    assert len(own_groups) == 1
    assert own_groups[0]["hooks"] == [{"type": "command",
                                       "command": "bash ~/my-own-startup.sh",
                                       "timeout": 3}], own_groups


# --- hook behaviour per SessionStart source --------------------------------------


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "context" / "plans").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(
        "# State\n\n## Current Status\nshort\n", encoding="utf-8")
    snap = proj / ".claude" / "context" / "pre-compact-snapshot.md"
    snap.write_text("# Pre-Compaction Snapshot\nSaved: now\n\n"
                    "## Modified Files (git status, top 30)\nM src/fresh.py\n",
                    encoding="utf-8")
    return proj


def _env(project: Path) -> dict:
    env = dict(os.environ)
    for key in ("VCT_DISABLE_HOOKS", "VCO_HOOK_CONTEXT_CAP",
                "VCO_SNAPSHOT_MAX_AGE"):
        env.pop(key, None)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return env


def _reinject(impl: str, project: Path, source: str) -> str:
    payload = json.dumps({"hook_event_name": "SessionStart",
                          "session_id": "s1", "source": source})
    argv = (["bash", str(HOOKS / "compact-context-reinject.sh")]
            if impl == "sh" else
            [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
             str(HOOKS / "compact-context-reinject.ps1")])
    res = subprocess.run(argv, input=payload, capture_output=True, text=True,
                         timeout=120, env=_env(project), cwd=str(project))
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.mark.parametrize("impl", _SHELLS)
def test_startup_injects_context_state_without_compaction_artefacts(impl, tmp_path):
    """Item 6 made true: a plain session start re-injects CONTEXT_STATE.md
    (the diff-context-inject comment's promise), budgeted; the pre-compact
    snapshot is a compaction artefact and does not belong to a startup."""
    out = _reinject(impl, _project(tmp_path), "startup")
    assert "## Current Task State" in out
    assert "Current Status" in out
    assert "## Pre-Compaction Snapshot" not in out
    assert "src/fresh.py" not in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_resume_injects_context_state(impl, tmp_path):
    """The hook still HONOURS source=resume (a user may register it under
    resume themselves); VCO's own registration was withdrawn (W6-K), the
    hook's source awareness was not."""
    out = _reinject(impl, _project(tmp_path), "resume")
    assert "## Current Task State" in out
    assert "Current Status" in out
    assert "## Pre-Compaction Snapshot" not in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_compact_keeps_the_snapshot_section(impl, tmp_path):
    """Unchanged behaviour: after a compaction the snapshot IS replayed."""
    out = _reinject(impl, _project(tmp_path), "compact")
    assert "## Current Task State" in out
    assert "## Pre-Compaction Snapshot" in out
    assert "src/fresh.py" in out


# --- the comment tells the truth -------------------------------------------------


def test_diff_inject_comment_names_the_real_mechanism():
    for ext in ("sh", "ps1"):
        head = (HOOKS / f"diff-context-inject.{ext}").read_text(
            encoding="utf-8-sig")[:4000]
        assert "compact-context-reinject" in head, (
            f"diff-context-inject.{ext}: the 'first prompt / full injection' "
            "comment must name the hook that actually performs it"
        )
