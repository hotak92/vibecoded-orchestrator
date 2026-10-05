# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 review SF-2/SF-3 — per-sub-hook disable + async-only retirements.

SF-2 (capability kept): pre-merge, each of the eight async PostToolUse
registrations was an individually toggleable Hooks-tab row. The merged
dispatcher must not narrow that: a per-project disabled-stems key
(``VCO_ASYNC_DISABLED_HOOKS`` in ``<project>/.claude/env`` — the SAME
per-project hook-knob channel the lean-ctx toggle uses, written by the
launcher's existing ``set_claude_env_value`` command and read by both
dispatcher siblings) skips any routed stem the user turned off, and a
bundle update CARRIES the disable forward: a retired async registration
that was parked (disabled) in launcher.db lands its stem in that key, so
the merge never silently re-enables what the user had switched off.

SF-3 (narrowing): the twelve v0.2.101 retirement rows match ONLY
registrations that carry ``"async": true`` — a user's OWN synchronous
PostToolUse registration of one of the six scripts is left alone by the
scrub, by the parked re-enable refusal, and by the launcher's prune
classifier.

The prune race: the launcher's eager F7 prune releases retired parked
bytes at Hooks-tab load. If that ran BEFORE the bundle update, the carry
would find nothing. So the classifier's answer carries ``carry_pending``
for these rows while the project's settings.json does not yet hold the
dispatcher registration (update not run yet) — and the Rust prune skips
carry-pending rows (pinned in project_hooks_settings.rs's own tests).
Once the update has run (dispatcher present, carry done at update time),
the rows prune as before.

Red-proofs: every Python-side behaviour here was red before the round-2
implementation (missing ``upsert_env_key`` / ``carry_parked_async_disables``
/ ``async_only`` narrowing — import/attribute errors and the sync
leave-alone failing); the mutation re-proofs are named per group below.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    insert_rows,
    make_launcher_db,
    now_ms,
)
from vco_lib import envfile, hook_retirements as hr, parked_hooks  # noqa: E402

DISABLED_KEY = "VCO_ASYNC_DISABLED_HOOKS"

REPO = REPO_ROOT


# ---------------------------------------------------------------------------
# envfile.upsert_env_key — the Python writer, mirroring the Rust write_key
# (launcher/src-tauri/src/commands/claude_env.rs) semantics line for line.
# ---------------------------------------------------------------------------


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_upsert_appends_when_key_absent_and_preserves_the_file(tmp_path):
    p = tmp_path / "env"
    p.write_text("# comment\nFOO=bar\n\nBAZ=qux\n", encoding="utf-8")
    changed = envfile.upsert_env_key(p, DISABLED_KEY, "kg-update-nudge")
    assert changed is True
    assert _read(p) == "# comment\nFOO=bar\n\nBAZ=qux\n" + f"{DISABLED_KEY}=kg-update-nudge\n"


def test_upsert_replaces_in_place_and_collapses_duplicates(tmp_path):
    p = tmp_path / "env"
    p.write_text(
        f"A=1\n{DISABLED_KEY}=old-one\nB=2\n{DISABLED_KEY}=old-two\n",
        encoding="utf-8",
    )
    changed = envfile.upsert_env_key(p, DISABLED_KEY, "new")
    assert changed is True
    assert _read(p) == f"A=1\n{DISABLED_KEY}=new\nB=2\n"


def test_upsert_none_removes_every_occurrence(tmp_path):
    p = tmp_path / "env"
    p.write_text(f"A=1\n{DISABLED_KEY}=x\n{DISABLED_KEY}=y\nB=2\n", encoding="utf-8")
    changed = envfile.upsert_env_key(p, DISABLED_KEY, None)
    assert changed is True
    assert _read(p) == "A=1\nB=2\n"


def test_upsert_is_idempotent_no_mtime_churn(tmp_path):
    p = tmp_path / "env"
    p.write_text(f"{DISABLED_KEY}=a,b\n", encoding="utf-8")
    before = _read(p)
    assert envfile.upsert_env_key(p, DISABLED_KEY, "a,b") is False
    assert _read(p) == before


def test_upsert_creates_missing_file_and_parent(tmp_path):
    p = tmp_path / ".claude" / "env"
    assert envfile.upsert_env_key(p, DISABLED_KEY, "post-file-delete") is True
    assert _read(p) == f"{DISABLED_KEY}=post-file-delete\n"


def test_upsert_none_on_absent_file_is_a_noop(tmp_path):
    p = tmp_path / "env"
    assert envfile.upsert_env_key(p, DISABLED_KEY, None) is False
    assert not p.exists()


def test_upsert_keeps_a_trailing_newline_and_tolerates_its_absence(tmp_path):
    p = tmp_path / "env"
    p.write_text("A=1", encoding="utf-8")  # no trailing newline
    envfile.upsert_env_key(p, DISABLED_KEY, "x")
    assert _read(p) == f"A=1\n{DISABLED_KEY}=x\n"


# ---------------------------------------------------------------------------
# ParkedHook.is_async — the blob's async flag, parsed where the blob's shape
# is already owned (Python), never in Rust.
# ---------------------------------------------------------------------------


def _park_row(db, project_id, event, matcher, command, blob):
    insert_rows(db, "project_hooks", [{
        "project_id": project_id, "event": event, "matcher": matcher,
        "command": command, "enabled": 0, "installed_at": now_ms(),
        "updated_at": now_ms(), "disabled_entry_json": blob,
    }])


def _blob(command: str, *, async_: bool | None) -> str:
    item = {"type": "command", "command": command, "timeout": 5}
    if async_ is not None:
        item["async"] = async_
    return json.dumps({"schema": 1, "event": "PostToolUse", "matcher": "*",
                       "item": item})


def test_read_parked_hooks_exposes_the_blob_async_flag(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "P", "folder_path": proj},
    ])
    async_cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    sync_cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-update-nudge.sh"'
    _park_row(db, "p1", "PostToolUse", "Bash", async_cmd, _blob(async_cmd, async_=True))
    _park_row(db, "p1", "PostToolUse", "*", sync_cmd, _blob(sync_cmd, async_=None))
    _park_row(db, "p1", "Stop", "", "bash x.sh", "{ broken")
    state = parked_hooks.read_parked_hooks(proj, db_path=db)
    assert state.readable
    by_cmd = {h.command: h for h in state.hooks}
    assert by_cmd[async_cmd].is_async is True
    assert by_cmd[sync_cmd].is_async is None
    assert by_cmd["bash x.sh"].is_async is None  # unparseable blob → None


# ---------------------------------------------------------------------------
# SF-3 — async-only narrowing of the twelve rows (act + leave-alone)
# ---------------------------------------------------------------------------


def _retired_stem_rows():
    return [
        e for e in hr.RETIRED_REGISTRATIONS
        if e.retired_in == "v0.2.101" and e.event == "PostToolUse"
    ]


def test_the_v02101_rows_are_declared_async_only():
    rows = _retired_stem_rows()
    assert len(rows) == 12
    assert all(e.async_only for e in rows), (
        "the v0.2.101 rows must be async-only: a user's own SYNC "
        "registration of these scripts is theirs, not VCO's to retire"
    )


@pytest.mark.parametrize("is_async", [True])
def test_async_registration_of_every_stem_matches(is_async):
    for stem in ("post-edit-outcome", "post-bash-context-record",
                 "kg-summary-generator", "post-git-commit-kg-sync",
                 "post-file-delete", "kg-update-nudge"):
        for ext in ("sh", "ps1"):
            target = f"{stem}.{ext}"
            cmd = f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/{target}"'
            m = hr.match_retired_registration(
                "PostToolUse", cmd,
                hook_identity=hr.vco_hook_script_identity(cmd),
                is_async=is_async,
            )
            assert m is not None, (target, is_async)


@pytest.mark.parametrize("is_async", [False, None])
def test_sync_or_unknown_async_registration_does_not_match(is_async):
    """SF-3 leave-alone: without POSITIVE async evidence nothing matches."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-update-nudge.sh"'
    assert hr.match_retired_registration(
        "PostToolUse", cmd,
        hook_identity=hr.vco_hook_script_identity(cmd),
        is_async=is_async,
    ) is None


def test_scrub_removes_the_async_and_keeps_the_sync_twin():
    """ACT + LEAVE-ALONE in one block: the retired async entry goes, a
    user's own synchronous registration of the SAME script stays."""
    async_cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    sync_cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-summary-generator.sh"'
    user_hooks = {
        "PostToolUse": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": async_cmd, "async": True},
            ]},
            {"matcher": "Edit|Write", "hooks": [
                {"type": "command", "command": sync_cmd},  # no async key
            ]},
        ],
    }
    out, removals = hr.scrub_retired_registrations(user_hooks)
    kept = [h["command"] for g in out["PostToolUse"] for h in g["hooks"]]
    assert kept == [sync_cmd]
    assert len(removals) == 1
    assert removals[0]["command"] == async_cmd


# ---------------------------------------------------------------------------
# carry_parked_async_disables — the bundle-update migration (SF-2 b)
# ---------------------------------------------------------------------------


def _world(tmp_path, parked_rows):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "P", "folder_path": proj},
    ])
    for event, matcher, cmd, blob in parked_rows:
        _park_row(db, "p1", event, matcher, cmd, blob)
    return proj, db


def test_carry_writes_the_disabled_stem_of_a_parked_retired_async_row(tmp_path):
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-summary-generator.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Edit", cmd, _blob(cmd, async_=True)),
    ])
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    carried = hr.carry_parked_async_disables(proj, parked)
    assert carried == ["kg-summary-generator"]
    env_text = (proj / ".claude" / "env").read_text(encoding="utf-8")
    assert f"{DISABLED_KEY}=kg-summary-generator" in env_text


def test_carry_leaves_a_parked_sync_registration_alone(tmp_path):
    """A user's own SYNC registration of a retired script, disabled, is
    not a dispatcher sub-hook disable — nothing is carried, no file is
    written (SF-3 leave-alone on the migration path)."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-summary-generator.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Edit", cmd, _blob(cmd, async_=None)),
    ])
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    assert hr.carry_parked_async_disables(proj, parked) == []
    assert not (proj / ".claude" / "env").exists()


def test_carry_leaves_unrelated_parked_rows_alone(tmp_path):
    proj, db = _world(tmp_path, [
        ("Stop", "", "bash .claude/hooks/notify-stop.sh",
         '{"schema": 1, "item": {"command": "bash .claude/hooks/notify-stop.sh"}}'),
    ])
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    assert hr.carry_parked_async_disables(proj, parked) == []
    assert not (proj / ".claude" / "env").exists()


def test_carry_unions_with_an_existing_key_and_is_idempotent(tmp_path):
    cmd_a = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    cmd_b = "bash .claude/hooks/post-edit-outcome.sh"  # pre-v0.2.97 spelling
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd_a, _blob(cmd_a, async_=True)),
        ("PostToolUse", "Edit|Write", cmd_b, _blob(cmd_b, async_=True)),
    ])
    (proj / ".claude" / "env").write_text(
        f"{DISABLED_KEY}=user-own-stem\n", encoding="utf-8")
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    carried = hr.carry_parked_async_disables(proj, parked)
    assert sorted(carried) == ["post-edit-outcome", "post-file-delete"]
    value = envfile.env_value(
        (proj / ".claude" / "env").read_text(encoding="utf-8"), DISABLED_KEY)
    parts = [s.strip() for s in (value or "").split(",")]
    assert parts[0] == "user-own-stem", "the user's own entries keep their place"
    assert set(parts[1:]) == {"post-edit-outcome", "post-file-delete"}
    # idempotent: a second run adds nothing and rewrites nothing
    assert hr.carry_parked_async_disables(proj, parked) == []


def test_carry_dry_run_reports_without_writing(tmp_path):
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd, _blob(cmd, async_=True)),
    ])
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    assert hr.carry_parked_async_disables(proj, parked, dry_run=True) == [
        "post-file-delete"]
    assert not (proj / ".claude" / "env").exists()


def test_carry_on_unreadable_parked_state_is_a_silent_noop(tmp_path):
    from tests.common.launcher_db_fixture import create_corrupt_launcher_db
    proj = tmp_path / "proj"
    proj.mkdir()
    corrupt = create_corrupt_launcher_db(tmp_path / "corrupt.db")
    parked = parked_hooks.read_parked_hooks(proj, db_path=corrupt)
    assert not parked.readable
    assert hr.carry_parked_async_disables(proj, parked) == []


# ---------------------------------------------------------------------------
# the classifier CLI — item_json async evidence + the carry_pending gate
# ---------------------------------------------------------------------------


def _cli(payload: dict, *, db: Path | None = None) -> dict:
    import subprocess

    from tests.common.child_env import child_env
    overrides = {"VCT_LAUNCHER_DB_PATH": str(db)} if db is not None else {}
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.hook_retirements", "match", "--json"],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(REPO), timeout=60, env=child_env(**overrides),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _project_with(tmp_path: Path, name: str, *, dispatcher: bool,
                  settings_text: str | None = None):
    proj = tmp_path / name
    (proj / ".claude").mkdir(parents=True)
    if settings_text is None:
        post = []
        if dispatcher:
            post.append({"matcher": "*", "hooks": [{
                "type": "command",
                "command": 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-tool-use-async.sh"',
                "async": True, "timeout": 15}]})
        settings_text = json.dumps({"hooks": {"PostToolUse": post}})
    (proj / ".claude" / "settings.json").write_text(settings_text, encoding="utf-8")
    return proj


DELETE_CMD = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'


def test_cli_resolves_async_evidence_from_the_parked_rows(tmp_path):
    """SF-3 on the classifier path: the pair stays identity-level (the
    parked BYTES never travel — the Rust pin
    `the_request_carries_every_parked_pair_and_nothing_else` keeps that
    contract); with a project_folder the classifier reads the async
    evidence itself. Positive async → retired; sync/absent evidence →
    conservative not-retired."""
    proj = _project_with(tmp_path, "proj", dispatcher=True)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "P", "folder_path": proj},
    ])
    _park_row(db, "p1", "PostToolUse", "Bash", DELETE_CMD,
              _blob(DELETE_CMD, async_=True))
    _park_row(db, "p1", "PostToolUse", "Edit", DELETE_CMD,
              _blob(DELETE_CMD, async_=None))  # the user's own sync twin
    nudge = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-update-nudge.sh"'
    out = _cli({"pairs": [
        {"event": "PostToolUse", "command": DELETE_CMD, "matcher": "Bash"},
        {"event": "PostToolUse", "command": DELETE_CMD, "matcher": "Edit"},
        {"event": "PostToolUse", "command": nudge, "matcher": "*"},
    ], "project_folder": str(proj)}, db=db)
    assert out["ok"] is True
    rows = out["matches"]
    assert rows[0]["retired"] is True
    assert rows[1]["retired"] is False, "a sync parked twin is the user's"
    assert rows[2]["retired"] is False, "no parked row → no evidence → keep"


def test_cli_without_folder_never_matches_the_async_only_rows():
    """Back-compat shape (no project_folder): the async-only rows have no
    evidence source, so they answer not-retired and the launcher keeps
    every row — the click-time refusal (which holds the blob) still fires."""
    out = _cli({"pairs": [
        {"event": "PostToolUse", "command": DELETE_CMD, "matcher": "Bash"},
    ]})
    assert out["matches"][0]["retired"] is False


def test_cli_carry_pending_gates_the_prune_until_the_update_has_run(tmp_path):
    """Before the bundle update (no dispatcher registration in the project's
    settings.json) the parked bytes are the ONLY evidence of the user's
    disable — the classifier must tell the launcher NOT to release them.
    After the update (dispatcher present; the carry ran at update time) the
    rows prune as before."""
    pair = {"event": "PostToolUse", "command": DELETE_CMD, "matcher": "Bash"}

    stale = _project_with(tmp_path, "stale", dispatcher=False)
    db_stale = make_launcher_db(tmp_path / "state-stale", projects=[
        {"project_id": "p1", "name": "P", "folder_path": stale},
    ])
    _park_row(db_stale, "p1", "PostToolUse", "Bash", DELETE_CMD,
              _blob(DELETE_CMD, async_=True))
    out = _cli({"pairs": [pair], "project_folder": str(stale)}, db=db_stale)
    assert out["matches"][0]["retired"] is True
    assert out["matches"][0]["carry_pending"] is True

    updated = _project_with(tmp_path, "updated", dispatcher=True)
    db_upd = make_launcher_db(tmp_path / "state-upd", projects=[
        {"project_id": "p1", "name": "P", "folder_path": updated},
    ])
    _park_row(db_upd, "p1", "PostToolUse", "Bash", DELETE_CMD,
              _blob(DELETE_CMD, async_=True))
    # The updated state includes the carry's OUTPUT: the gate keys on
    # positive evidence of carry success (re-review-2 nit 3), so the
    # post-update fixture must show the key the update wrote.
    (updated / ".claude" / "env").write_text(
        f"{DISABLED_KEY}=post-file-delete\n", encoding="utf-8")
    out = _cli({"pairs": [pair], "project_folder": str(updated)}, db=db_upd)
    assert out["matches"][0]["retired"] is True
    assert out["matches"][0]["carry_pending"] is False


def test_cli_unreadable_settings_keep_the_row_parked(tmp_path):
    pair = {"event": "PostToolUse", "command": DELETE_CMD, "matcher": "Bash"}
    broken = _project_with(tmp_path, "broken", dispatcher=False,
                           settings_text="{ broken")
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "P", "folder_path": broken},
    ])
    _park_row(db, "p1", "PostToolUse", "Bash", DELETE_CMD,
              _blob(DELETE_CMD, async_=True))
    out = _cli({"pairs": [pair], "project_folder": str(broken)}, db=db)
    assert out["matches"][0]["retired"] is True
    assert out["matches"][0]["carry_pending"] is True, (
        "an unreadable settings.json must never license releasing the user's "
        "only copy of their disable decision"
    )


def test_cli_a_non_async_row_keeps_the_old_answer_shape():
    """Back-compat: a cost-tracker row (not async_only) needs no folder and
    answers carry_pending False — the gate is additive."""
    cmd = "bash .claude/hooks/cost-tracker.sh"
    out = _cli({"pairs": [{"event": "Stop", "command": cmd}]})
    row = out["matches"][0]
    assert row["retired"] is True
    assert row["carry_pending"] is False


# ---------------------------------------------------------------------------
# SF-A — a FAILED carry write must not let the prune re-enable the sub-hook:
# the failure lands an UPDATE_DEFERRED row, the prune gate keys on that row
# (not on dispatcher presence), and a later successful carry closes it.
# ---------------------------------------------------------------------------


def _write_dispatcher_settings(proj: Path) -> None:
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PostToolUse": [
            {"matcher": "*", "hooks": [{
                "type": "command",
                "command": 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-tool-use-async.sh"',
                "async": True, "timeout": 15}]},
        ]},
    }), encoding="utf-8")


def _open_cids(proj: Path) -> set:
    from vco_lib.deferral_report import DeferralReport
    return {e.condition_id for e in DeferralReport.read(proj).entries}


def test_carry_write_failure_emits_a_deferral_and_keeps_the_gate_closed(tmp_path):
    """ACT: `.claude/env` is a DIRECTORY, so the atomic write cannot land.
    The carry must soft-fail (never fail the update), record an owed-work
    deferral row, and the classifier must answer carry_pending=True EVEN
    THOUGH the dispatcher registration is present — gating on dispatcher
    presence alone is exactly the window SF-A names (prune releases the
    parked bytes → the disable is gone → the sub-hook comes back on)."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd, _blob(cmd, async_=True)),
    ])
    (proj / ".claude" / "env").mkdir()  # the write target is a directory
    _write_dispatcher_settings(proj)
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)

    warnings = []
    carried = hr.carry_parked_async_disables(
        proj, parked, log=lambda slot, level, msg, **kw: warnings.append((level, msg)))
    assert carried == []
    assert any(level == "warn" for level, _ in warnings), "the failure must be logged"
    assert hr.CARRY_FAILED_CID in _open_cids(proj), (
        "a failed carry must leave an UPDATE_DEFERRED row so the next update "
        "retries and the launcher keeps the parked bytes"
    )
    out = _cli({"pairs": [{"event": "PostToolUse", "command": cmd, "matcher": "Bash"}],
                "project_folder": str(proj)}, db=db)
    assert out["matches"][0]["retired"] is True
    assert out["matches"][0]["carry_pending"] is True, (
        "dispatcher presence alone must NOT open the prune gate while the "
        "carry deferral is open"
    )


def test_carry_env_write_failure_alone_emits_and_keeps_the_gate_closed(
        tmp_path, monkeypatch):
    """Re-review-2 nit 1 — the literal SF-A shape, tested directly: the env
    file is READABLE and the WRITE raises OSError (read-only fs / immutable
    flag). Red-proof for the write branch: delete the `_emit_carry_failed`
    call in the `except OSError` around `upsert_env_key` → the ledger
    assertion goes red."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd, _blob(cmd, async_=True)),
    ])
    (proj / ".claude" / "env").write_text("# user knobs\n", encoding="utf-8")
    _write_dispatcher_settings(proj)
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)

    from vco_lib import envfile

    def _readonly(*_a, **_k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(envfile, "upsert_env_key", _readonly)
    warnings = []
    carried = hr.carry_parked_async_disables(
        proj, parked,
        log=lambda slot, level, msg, **kw: warnings.append((level, msg)))
    assert carried == []
    assert any(level == "warn" for level, _ in warnings), "the failure must be logged"
    assert hr.CARRY_FAILED_CID in _open_cids(proj), (
        "the WRITE-failure branch must record the owed work"
    )
    out = _cli({"pairs": [{"event": "PostToolUse", "command": cmd, "matcher": "Bash"}],
                "project_folder": str(proj)}, db=db)
    assert out["matches"][0]["carry_pending"] is True


def test_double_failure_env_write_and_ledger_still_keeps_the_rows(
        tmp_path, monkeypatch):
    """Re-review-2 nit 3 — the double-failure window: BOTH the env write and
    the ledger emit fail, so NO failure row exists to key a gate on. The
    prune gate must therefore require POSITIVE evidence of carry success
    (the owed computation — the deferral probe's own one home: False =
    landed; True/None = keep), never the mere absence of a row.

    Red-proof: under the previous ledger-row-only gate this answered
    carry_pending=False (dispatcher present, no open row) → the launcher
    released the bytes → the user's disable was lost with no record."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd, _blob(cmd, async_=True)),
    ])
    (proj / ".claude" / "env").write_text("", encoding="utf-8")  # readable
    _write_dispatcher_settings(proj)
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)

    from vco_lib import deferral_emit, envfile

    def _readonly(*_a, **_k):
        raise OSError(30, "Read-only file system")

    def _ledger_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(envfile, "upsert_env_key", _readonly)
    monkeypatch.setattr(deferral_emit, "emit", _ledger_full)

    warnings = []
    carried = hr.carry_parked_async_disables(
        proj, parked,
        log=lambda slot, level, msg, **kw: warnings.append((level, msg)))
    assert carried == []
    assert hr.CARRY_FAILED_CID not in _open_cids(proj), (
        "the scenario IS the unrecorded failure"
    )
    out = _cli({"pairs": [{"event": "PostToolUse", "command": cmd, "matcher": "Bash"}],
                "project_folder": str(proj)}, db=db)
    assert out["matches"][0]["retired"] is True
    assert out["matches"][0]["carry_pending"] is True, (
        "a failed carry must keep the parked bytes EVEN WHEN its own failure "
        "row could not be recorded — absence of a row is not evidence of success"
    )


def test_the_carry_deferral_clears_once_the_carry_lands(tmp_path, monkeypatch):
    """LEAVE-ALONE + retry: fix the environment, the next carry lands, the
    owed-work row closes, the probe reports settled, and the prune gate
    opens — no row is parked forever after a successful retry."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Bash", cmd, _blob(cmd, async_=True)),
    ])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))  # the probe's DB path
    env_dir = proj / ".claude" / "env"
    env_dir.mkdir()
    _write_dispatcher_settings(proj)
    parked = parked_hooks.read_parked_hooks(proj, db_path=db)
    assert hr.carry_parked_async_disables(proj, parked) == []
    assert hr.CARRY_FAILED_CID in _open_cids(proj)

    from vco_lib.deferral_probes import (
        ProbeContext, async_subhook_disable_carry_still_owed,
    )
    # While the env target is unusable the probe has NO verdict (None) —
    # "positive evidence only": None keeps the entry exactly like True, and
    # a silent environment is never read as "fixed".
    assert async_subhook_disable_carry_still_owed(ProbeContext(folder=proj)) is not False

    # the retry: environment fixed, the next update's carry lands
    env_dir.rmdir()
    assert hr.carry_parked_async_disables(proj, parked) == ["post-file-delete"]
    assert hr.CARRY_FAILED_CID not in _open_cids(proj), (
        "a successful carry must close its own owed-work row"
    )
    assert async_subhook_disable_carry_still_owed(ProbeContext(folder=proj)) is False
    out = _cli({"pairs": [{"event": "PostToolUse", "command": cmd, "matcher": "Bash"}],
                "project_folder": str(proj)}, db=db)
    assert out["matches"][0]["carry_pending"] is False


def test_the_carry_probe_cannot_clear_what_the_next_update_would_re_emit(tmp_path, monkeypatch):
    """Probe/emitter agreement (the house rule for probe:py conditions):
    while a parked stem is missing from the key, the probe says owed — the
    same computation the carry emits from, never a second rule."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/kg-summary-generator.sh"'
    proj, db = _world(tmp_path, [
        ("PostToolUse", "Edit", cmd, _blob(cmd, async_=True)),
    ])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))  # the probe's DB path
    from vco_lib.deferral_probes import (
        ProbeContext, async_subhook_disable_carry_still_owed,
    )
    assert async_subhook_disable_carry_still_owed(ProbeContext(folder=proj)) is True
    # land the carry by hand (the command_to_apply path) → settled
    (proj / ".claude" / "env").write_text(
        f"{DISABLED_KEY}=kg-summary-generator\n", encoding="utf-8")
    assert async_subhook_disable_carry_still_owed(ProbeContext(folder=proj)) is False
    # no parked evidence at all → nothing owed
    empty = tmp_path / "empty"
    empty.mkdir()
    assert async_subhook_disable_carry_still_owed(ProbeContext(folder=empty)) is False


# ---------------------------------------------------------------------------
# engine level — the ordinary bundle update performs the carry
# ---------------------------------------------------------------------------


def test_bundle_update_carries_the_parked_disable(tmp_path, monkeypatch):
    from tests.test_install_bundle import _make_fake_orchestrator
    from vco_lib import project_init

    orch = tmp_path / "orch"
    proj = tmp_path / "proj"
    orch.mkdir()
    proj.mkdir()
    _make_fake_orchestrator(orch)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "P", "folder_path": proj},
    ])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    # 1. a normal install
    project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=False)
    # 2. the user had disabled the async post-file-delete registration:
    #    parked bytes in launcher.db, entry absent from settings.json.
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/post-file-delete.sh"'
    _park_row(db, "p1", "PostToolUse", "Bash", cmd, _blob(cmd, async_=True))
    # 3. the ordinary update
    result = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result["errors"] == []
    assert result.get("carried_async_subhook_disables") == ["post-file-delete"]
    env_text = (proj / ".claude" / "env").read_text(encoding="utf-8")
    assert f"{DISABLED_KEY}=post-file-delete" in env_text
    # N-D: the carry is on the record — one auto-resolution row naming the
    # stem (a regression deleting the record_auto_resolution call goes red
    # here, not silent).
    trail = proj / ".claude" / "logs" / "auto-resolutions.jsonl"
    assert trail.is_file(), "no auto-resolution trail was written"
    rows = [
        json.loads(line)
        for line in trail.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    mine = [r for r in rows if r.get("action") == "carried_parked_async_disable"]
    assert len(mine) == 1, rows
    assert "post-file-delete" in mine[0]["detail"]
    assert DISABLED_KEY in mine[0]["detail"]
    # 4. idempotent on the next update (the row is still parked)
    result2 = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result2.get("carried_async_subhook_disables") in (None, [])
    value = envfile.env_value(
        (proj / ".claude" / "env").read_text(encoding="utf-8"), DISABLED_KEY)
    assert value == "post-file-delete"
