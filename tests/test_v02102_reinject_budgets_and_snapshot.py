# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W6-D — the pre-compact snapshot covers MANUAL compacts too,
and the compact re-inject keeps every section inside the injection cap.

Item 1 (measured defect: three manual compacts re-injected a pre-compact
snapshot dated 2026-09-29 — ``pre-compact-save`` was registered under
PreCompact matcher ``auto``, which never matches a manual ``/compact``):

* both settings templates register ``pre-compact-save`` under matcher ``*``
  (the vendor hooks doc gives PreCompact exactly two matcher values,
  ``manual`` and ``auto``; an omitted or ``*`` matcher fires on both);
* the hook itself saves a fresh snapshot when the payload says
  ``trigger: "manual"``;
* defence in depth: the re-inject does NOT replay a snapshot older than the
  current session's compaction (one line saying so instead);
* the settings merge migrates an installed ``auto`` registration to ``*``
  exactly once (idempotent), leaving the user's own hooks alone.

Item 2 (measured: a 20.7 KB CONTEXT_STATE.md made the whole-stream cap cut
the re-inject inside section 1, so the plan / commits / snapshot sections
never reached the model):

* every section gets a CHARACTER budget from the one committed table
  ``templates/hooks/_lib/reinject-budgets.txt`` (A>B>C tier B — one table
  both OS siblings parse), truncating WITHIN a section with a pointer
  naming its file; CONTEXT_STATE is the flex section;
* oversize CONTEXT_STATE => all sections present, total <= cap, each
  truncated section names its file;
* small inputs => no cut markers, full content (unchanged behaviour).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib.bundle_globs import hook_lib_data_globs
from vco_lib.hook_retirements import MATCHER_MIGRATIONS
from vco_lib import settings_merge

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
LINUX = REPO / "templates" / "settings.json.linux.template"
WINDOWS = REPO / "templates" / "settings.json.windows.template"
TPLS = [LINUX, WINDOWS]
BUDGETS = HOOKS / "_lib" / "reinject-budgets.txt"
PWSH = shutil.which("pwsh") or "pwsh"
HAVE_PWSH = shutil.which("pwsh") is not None
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    not HAVE_PWSH, reason="pwsh not installed"))]
MARKER = "[VCO: output cut at "
DEFAULT_CAP = 9500


def _env(project: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for key in ("VCT_DISABLE_HOOKS", "VCO_HOOK_CONTEXT_CAP",
                "VCO_SNAPSHOT_MAX_AGE"):
        env.pop(key, None)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.update(extra)
    return env


def _hook(impl: str, name: str, project: Path, payload: str,
          timeout: int = 120, **extra: str) -> str:
    argv = (["bash", str(HOOKS / f"{name}.sh")] if impl == "sh" else
            [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
             str(HOOKS / f"{name}.ps1")])
    res = subprocess.run(argv, input=payload, capture_output=True, text=True,
                         timeout=timeout, env=_env(project, **extra),
                         cwd=str(project))
    assert res.returncode == 0, res.stderr
    return res.stdout


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "context" / "plans").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(
        "# State\n\n## Current Status\nshort\n", encoding="utf-8")
    return proj


def _big(chars: int) -> str:
    line = "- a progress note long enough to fill the section budget nicely\n"
    return "# State\n" + line * (chars // len(line) + 1)


# ═══════════════════════════════════════════════════════════════════════
# Item 1 — registration, hook behaviour, staleness guard, migration
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_precompact_registered_for_every_trigger(tpl: Path):
    doc = json.loads(tpl.read_text(encoding="utf-8"))
    groups = doc["hooks"]["PreCompact"]
    stems = {h["command"].split("/")[-1].rstrip('"')
             .removesuffix(".sh").removesuffix(".ps1")
             for g in groups for h in g["hooks"]}
    assert "pre-compact-save" in stems
    matchers = {g.get("matcher") for g in groups}
    # "*" fires on BOTH PreCompact triggers (manual and auto); "auto" alone
    # never fires on a manual /compact — the measured defect.
    assert matchers == {"*"}, matchers


def test_migration_row_covers_precompact():
    row = next(r for r in MATCHER_MIGRATIONS if r.event == "PreCompact")
    assert (row.old, row.new) == ("auto", "*")
    assert row.targets == frozenset({"pre-compact-save.sh", "pre-compact-save.ps1"})


@pytest.mark.parametrize("impl", _SHELLS)
def test_manual_compact_saves_a_fresh_snapshot(impl, tmp_path):
    proj = _project(tmp_path)
    payload = json.dumps({
        "hook_event_name": "PreCompact", "trigger": "manual",
        "session_id": "s1", "custom_instructions": None,
    })
    _hook(impl, "pre-compact-save", proj, payload)
    snap = proj / ".claude" / "context" / "pre-compact-snapshot.md"
    assert snap.is_file(), "a manual /compact must save the snapshot too"
    saved = next(ln for ln in snap.read_text(encoding="utf-8-sig").splitlines()
                 if ln.startswith("Saved: "))
    stamp = datetime.fromisoformat(saved[len("Saved: "):])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=2)


@pytest.mark.parametrize("impl", _SHELLS)
def test_stale_snapshot_is_not_replayed(impl, tmp_path):
    proj = _project(tmp_path)
    snap = proj / ".claude" / "context" / "pre-compact-snapshot.md"
    stale_body = ("# Pre-Compaction Snapshot\n"
                  "Saved: 2026-09-29T03:27:47+02:00\n\n"
                  "## Modified Files (git status, top 30)\n"
                  "M src/ancient-file.cpython.py\n")
    snap.write_text(stale_body, encoding="utf-8")
    # Age the file past the default 30-minute freshness window.
    if impl == "sh":
        old = datetime.now() - timedelta(days=11)
        subprocess.run(["touch", "-d", "@%d" % int(old.timestamp()), str(snap)],
                       check=True)
    else:
        subprocess.run(
            [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command",
             f"(Get-Item -LiteralPath '{snap}').LastWriteTime = "
             f"(Get-Date).AddDays(-11)"],
            check=True, capture_output=True)
    out = _hook(impl, "compact-context-reinject", proj,
                json.dumps({"session_id": "s1"}),
                VCO_SNAPSHOT_MAX_AGE="1800")
    assert "stale -- NOT replayed" in out
    assert "2026-09-29T03:27:47" in out, "the notice names when it was saved"
    assert "ancient-file" not in out, "stale git status must not be replayed"


@pytest.mark.parametrize("impl", _SHELLS)
def test_fresh_snapshot_is_replayed(impl, tmp_path):
    proj = _project(tmp_path)
    snap = proj / ".claude" / "context" / "pre-compact-snapshot.md"
    snap.write_text("# Pre-Compaction Snapshot\nSaved: now\n\n"
                    "## Modified Files (git status, top 30)\nM src/fresh.py\n",
                    encoding="utf-8")
    out = _hook(impl, "compact-context-reinject", proj,
                json.dumps({"session_id": "s1"}))
    assert "## Pre-Compaction Snapshot" in out
    assert "src/fresh.py" in out
    assert "stale" not in out


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_precompact_auto_registration_migrates_once(tpl: Path):
    template = json.loads(tpl.read_text(encoding="utf-8"))
    ext = "sh" if tpl is LINUX else "ps1"
    # An installed v0.2.101-shaped file: pre-compact-save under matcher "auto".
    user = {"hooks": {"PreCompact": [{
        "matcher": "auto",
        "hooks": [{
            "type": "command",
            "command": (f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/'
                        f'pre-compact-save.{ext}"' if ext == "sh" else
                        f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                        f'"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/'
                        f'pre-compact-save.{ext}"'),
            "timeout": 5,
        }],
    }]}}
    once = settings_merge.smart_merge_settings(user, template)
    groups = [g for g in once["hooks"]["PreCompact"]
              if "pre-compact-save" in json.dumps(g)]
    assert [g.get("matcher") for g in groups] == ["*"], groups
    assert len(groups) == 1, "migration must not stack a second group"
    # Idempotent: a second pass over its own output changes nothing.
    twice = settings_merge.smart_merge_settings(once, template)
    assert twice["hooks"]["PreCompact"] == once["hooks"]["PreCompact"]


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_precompact_user_hook_is_untouched(tpl: Path):
    template = json.loads(tpl.read_text(encoding="utf-8"))
    own = {"type": "command", "command": "bash ~/my-own-precompact.sh",
           "timeout": 5}
    user = {"hooks": {"PreCompact": [{"matcher": "auto", "hooks": [dict(own)]}]}}
    merged = settings_merge.smart_merge_settings(user, template)
    assert {"matcher": "auto", "hooks": [own]} in merged["hooks"]["PreCompact"]
    # the shipped registration was still added under "*"
    shipped = [g for g in merged["hooks"]["PreCompact"]
               if "pre-compact-save" in json.dumps(g)]
    assert [g.get("matcher") for g in shipped] == ["*"]


# ═══════════════════════════════════════════════════════════════════════
# Item 2 — per-section budgets inside the cap
# ═══════════════════════════════════════════════════════════════════════


def test_budget_table_is_wellformed_and_ships():
    rows = {}
    for line in BUDGETS.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        key, val, *_ = t.split()
        rows[key] = int(val)
    assert {"session_state", "plan", "commits", "snapshot", "pruned",
            "overhead"} <= set(rows)
    assert sum(v for k, v in rows.items() if k != "overhead") + \
        rows["overhead"] < 10000
    # hook_lib_data_globs must cover it, or it never reaches installs.
    import fnmatch
    assert any(fnmatch.fnmatch("reinject-budgets.txt", g)
               for g in hook_lib_data_globs())


@pytest.mark.parametrize("impl", _SHELLS)
def test_oversize_context_state_keeps_every_section(impl, tmp_path):
    proj = _project(tmp_path)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(_big(30000),
                                                       encoding="utf-8")
    (proj / ".claude" / "context" / "CONTEXT_STATE_s1.md").write_text(
        _big(5000), encoding="utf-8")
    plan = proj / ".claude" / "context" / "plans" / "PLAN-big.md"
    # head -30 caps the plan at 30 lines, so the LINES must be long enough
    # that the section still exceeds its 1200-char budget (a cut must occur).
    plan.write_text(
        "# Plan\n" + ("plan step line padded out past sixty characters wide\n" * 200),
        encoding="utf-8")
    (proj / ".claude" / "context" / "pre-compact-snapshot.md").write_text(
        "# Pre-Compaction Snapshot\nSaved: now\n\n"
        "## Modified Files\nM src/snapshot-file.py\n", encoding="utf-8")
    (proj / ".claude" / "context" / "pruned-context-summary.md").write_text(
        "edited: knowledge/a.md\n" * 60, encoding="utf-8")
    out = _hook(impl, "compact-context-reinject", proj,
                json.dumps({"session_id": "s1"}))
    # every section is represented
    for header in ("## Current Task State", "## This Session's Task State",
                   "## Active Plan", "## Recent Commits",
                   "## Pre-Compaction Snapshot"):
        assert header in out, header
    assert "snapshot-file.py" in out, "the snapshot section reached the model"
    # total stays inside the cap
    assert len(out.rstrip("\n")) <= DEFAULT_CAP
    # each truncated section names its file
    assert out.count(MARKER) >= 2
    assert "Read .claude/CONTEXT_STATE.md" in out
    assert "Read .claude/context/plans/PLAN-big.md" in out
    # the flex section actually yielded room: the plan section is present
    # AFTER the context-state section was cut
    assert out.index(MARKER) < out.index("## Active Plan")


@pytest.mark.parametrize("impl", _SHELLS)
def test_small_inputs_are_unchanged(impl, tmp_path):
    proj = _project(tmp_path)
    (proj / ".claude" / "context" / "CONTEXT_STATE_s1.md").write_text(
        "## RL Task\nthis session only\n", encoding="utf-8")
    plan = proj / ".claude" / "context" / "plans" / "PLAN-small.md"
    plan.write_text("# Plan\nship it\n", encoding="utf-8")
    (proj / ".claude" / "context" / "pre-compact-snapshot.md").write_text(
        "# Pre-Compaction Snapshot\nSaved: now\n\nM src/fresh.py\n",
        encoding="utf-8")
    out = _hook(impl, "compact-context-reinject", proj,
                json.dumps({"session_id": "s1"}))
    assert MARKER not in out, "nothing was oversize; nothing may be cut"
    for needle in ("short", "this session only", "ship it", "src/fresh.py"):
        assert needle in out, needle
