# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-8 (register issue 14) — stop-failure-notify dedup + hardening.

The 2026-09-20 storm: 304 identical critical desktop toasts
"Claude API Error — VCO_dev / unknown: No details" at a fixed ~4.3 s cadence,
because the hook (a) had no dedup, (b) its payload parse yields nothing for
trust-failure payloads (no ``error`` key), and (c) had no individual kill
switch. These tests pin all three fixes plus the multi-OS lockstep of the
embedded python core (byte-identical between the .sh and the .ps1).

The .sh tests EXECUTE the real hook against a fake HOME / VCT_STATE_DIR /
VCT_CLAUDE_DIR (same pattern as tests/test_v0292_wp8_hook_writers.py) with a
recording notify.py shim in the fake project, so notification behaviour is
asserted from the outside. The .ps1 side cannot be executed on Linux here;
it is pinned by core-equality + structural fingerprints
(test_ps1_core_matches_sh_core, test_ps1_sibling_carries_the_kill_switch).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_SH = REPO / "templates" / "hooks" / "stop-failure-notify.sh"
HOOK_PS1 = REPO / "templates" / "hooks" / "stop-failure-notify.ps1"

# The recording stand-in for the shipped desktop notifier. The real
# notify.py shows a toast; this one appends its argv (unit-separator-joined)
# to $NOTIFY_LOG so tests can assert exactly what the user would have seen.
NOTIFY_SHIM = """\
import os
import sys

log = os.environ.get("NOTIFY_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\\x1f".join(sys.argv[1:]) + "\\n")
"""


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """Fake project with a recording notify.py; returns (project, notify_log)."""
    project = tmp_path / "proj"
    (project / ".claude" / "scripts").mkdir(parents=True)
    shim = project / ".claude" / "scripts" / "notify.py"
    shim.write_text(NOTIFY_SHIM, encoding="utf-8")
    notify_log = tmp_path / "notify.log"
    return project, notify_log


def _env(home: Path, project: Path, notify_log: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        VCT_STATE_DIR=str(home / ".vct"),
        VCT_CLAUDE_DIR=str(home / ".claude"),
        CLAUDE_PROJECT_DIR=str(project),
        NOTIFY_LOG=str(notify_log),
    )
    for var in (
        "VCT_DISABLE_HOOKS",
        "VCO_STOP_FAILURE_NOTIFY",
        "VCO_METRICS_DIR",
        "VCO_METRICS_HOME",
        "VCO_LEGACY_METRICS_DIR",
        "VCO_METRICS_MIGRATED",
        "VCT_INSTALL_ROOT",
        "VCT_VENV",
    ):
        env.pop(var, None)
    return env


def _run(
    payload: str,
    home: Path,
    project: Path,
    notify_log: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = _env(home, project, notify_log)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SH)],
        input=payload.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=60,
    )


def _metrics(home: Path) -> Path:
    return home / ".vct" / "metrics"


def _ledger(home: Path) -> list[dict]:
    path = _metrics(home) / "failures.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))  # raises on corrupted JSON — intended
    return rows


def _state_path(home: Path, project: str, etype: str) -> Path:
    # WP-8 review MINOR-1: the component separator is "@" (never produced
    # by _safe's charset) so (Alpha-Beta, X) cannot collide with (Alpha, Beta-X).
    return _metrics(home) / f"stop_failure_dedup_{project}@{etype}.json"


def _notifications(notify_log: Path) -> list[list[str]]:
    if not notify_log.exists():
        return []
    rows = []
    for line in notify_log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(line.split("\x1f"))
    return rows


def _payload(
    etype: str = "rate_limit",
    message: str = "429 slow down",
    session_id: str = "0123456789abcdef",
) -> str:
    return json.dumps(
        {"session_id": session_id, "error": {"type": etype, "message": message}}
    )


@pytest.fixture()
def world(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(home, project, notify_log) with the recording notifier installed."""
    home = tmp_path / "home"
    home.mkdir()
    project, notify_log = _make_project(tmp_path)
    return home, project, notify_log


# ─── dedup window ────────────────────────────────────────────────────────


def test_first_failure_notifies_and_records_state(world):
    home, project, notify_log = world
    result = _run(_payload(), home, project, notify_log)
    assert result.returncode == 0, result.stderr

    notes = _notifications(notify_log)
    assert len(notes) == 1
    assert notes[0][0] == "Claude API Error — proj"
    assert notes[0][1] == "rate_limit: 429 slow down"

    state = json.loads(_state_path(home, "proj", "rate_limit").read_text())
    assert state["ts"] >= int(time.time()) - 60
    assert state["suppressed"] == 0
    assert state["session_id"] == "01234567"


def test_second_event_within_window_is_suppressed_and_counted(world):
    home, project, notify_log = world
    _run(_payload(), home, project, notify_log)
    _run(_payload(), home, project, notify_log)

    # Suppression is of the TOAST, not the record: two ledger rows.
    rows = _ledger(home)
    assert len(rows) == 2
    assert [r["error_type"] for r in rows] == ["rate_limit", "rate_limit"]

    assert len(_notifications(notify_log)) == 1
    state = json.loads(_state_path(home, "proj", "rate_limit").read_text())
    assert state["suppressed"] == 1


def test_notification_after_window_carries_suppressed_count(world):
    home, project, notify_log = world
    # A notification fired 400 s ago (window = 300 s) with 3 events swallowed
    # since — the next one is DUE and must report what was suppressed.
    metrics = _metrics(home)
    metrics.mkdir(parents=True)
    _state_path(home, "proj", "rate_limit").write_text(
        json.dumps({"ts": int(time.time()) - 400, "suppressed": 3, "session_id": "old"}),
        encoding="utf-8",
    )

    _run(_payload(session_id="ffffffffffffffff"), home, project, notify_log)

    notes = _notifications(notify_log)
    assert len(notes) == 1
    assert notes[0][1] == "rate_limit: 429 slow down (3 suppressed)"
    state = json.loads(_state_path(home, "proj", "rate_limit").read_text())
    assert state["suppressed"] == 0  # counter reset with the notification


def test_distinct_error_classes_dedup_independently(world):
    home, project, notify_log = world
    _run(_payload(etype="rate_limit"), home, project, notify_log)
    _run(_payload(etype="invalid_request_error"), home, project, notify_log)

    # Two classes, two windows, two notifications.
    assert len(_notifications(notify_log)) == 2
    assert _state_path(home, "proj", "rate_limit").exists()
    assert _state_path(home, "proj", "invalid_request_error").exists()


def test_storm_shape_unique_session_ids_still_dedup(world):
    """The adjudication pin: the 2026-09-20 storm used a FRESH session_id on
    every event — a per-session dedup key would have let all 304 through.
    The window authority is (project, error class), and the session_id is
    only recorded for diagnosis (read from the STDIN payload, never env)."""
    home, project, notify_log = world
    sids = [f"{chr(ord('a') + i)}234567890abcdef" for i in range(3)]
    for sid in sids:
        _run(_payload(session_id=sid), home, project, notify_log)

    assert len(_notifications(notify_log)) == 1
    rows = _ledger(home)
    assert len(rows) == 3
    state = json.loads(_state_path(home, "proj", "rate_limit").read_text())
    assert state["suppressed"] == 2
    assert state["session_id"] == "c2345678"  # the LAST event's, for diagnosis


def test_corrupt_state_fails_open(world):
    """A state file nobody can parse must not silently suppress the next
    urgent notification."""
    home, project, notify_log = world
    metrics = _metrics(home)
    metrics.mkdir(parents=True)
    _state_path(home, "proj", "rate_limit").write_text("{not json", encoding="utf-8")

    _run(_payload(), home, project, notify_log)
    assert len(_notifications(notify_log)) == 1


# ─── payload hardening ───────────────────────────────────────────────────


def test_unparseable_payload_logs_truncated_raw_and_stays_json(world):
    home, project, notify_log = world
    raw = "not json { \" at 'all"
    _run(raw, home, project, notify_log)

    rows = _ledger(home)
    assert len(rows) == 1
    # The line parses as JSON (the old hook string-built it and any quote
    # corrupted the ledger).
    assert rows[0]["error_type"] == "unknown"
    assert rows[0]["error_message"].startswith("raw payload: not json")
    # The notification still fires, carrying the evidence.
    notes = _notifications(notify_log)
    assert len(notes) == 1
    assert "raw payload: not json" in notes[0][1]


def test_payload_without_error_key_logs_raw_payload(world):
    """The storm shape: trust-failure payloads have no ``error`` key, which
    the old parser reduced to 'unknown: No details' — 304 times."""
    home, project, notify_log = world
    payload = json.dumps({"session_id": "abcd1234efgh", "message": "trust"})
    _run(payload, home, project, notify_log)

    rows = _ledger(home)
    assert rows[0]["error_type"] == "unknown"
    assert rows[0]["error_message"].startswith('raw payload: {"session_id"')
    assert rows[0]["session_id"] == "abcd1234"


def test_oversized_raw_payload_truncated_at_500_chars(world):
    home, project, notify_log = world
    _run("x" * 2000, home, project, notify_log)

    rows = _ledger(home)
    msg = rows[0]["error_message"]
    assert msg.startswith("raw payload: ")
    assert len(msg) <= len("raw payload: ") + 500
    assert msg.endswith("...")


def test_quotes_and_newlines_in_message_keep_ledger_valid(world):
    home, project, notify_log = world
    payload = _payload(message='he said "hi"\nand\tleft')
    _run(payload, home, project, notify_log)

    rows = _ledger(home)  # json.loads inside raises on corruption
    # _one_line collapses ALL whitespace (newline AND tab) to single spaces.
    assert rows[0]["error_message"] == 'he said "hi" and left'


# ─── individual kill switch ──────────────────────────────────────────────


def test_kill_switch_suppresses_notification_but_not_ledger(world):
    home, project, notify_log = world
    _run(_payload(), home, project, notify_log, extra_env={"VCO_STOP_FAILURE_NOTIFY": "0"})

    assert _notifications(notify_log) == []
    # The ledger is the diagnostic the storm diagnosis depended on — the
    # kill switch mutes the toast, not the record.
    assert len(_ledger(home)) == 1
    assert _state_path(home, "proj", "rate_limit").exists()


def test_kill_switch_is_distinct_from_vct_disable_hooks(world):
    """VCT_DISABLE_HOOKS exits before any work (no ledger, no state);
    VCO_STOP_FAILURE_NOTIFY=0 exits after the ledger, before the toast."""
    home, project, notify_log = world
    _run(_payload(), home, project, notify_log, extra_env={"VCT_DISABLE_HOOKS": "1"})
    assert _notifications(notify_log) == []
    assert _ledger(home) == []


# ─── .ps1 lockstep ───────────────────────────────────────────────────────


def _sh_core() -> str:
    lines = HOOK_SH.read_text(encoding="utf-8").split("\n")
    start = next(
        i for i, line in enumerate(lines) if line.endswith("<<'VCO_STOP_FAILURE_CORE'")
    )
    end = next(
        i for i, line in enumerate(lines) if i > start and line == "VCO_STOP_FAILURE_CORE"
    )
    return "\n".join(lines[start + 1 : end])


def _ps1_core() -> str:
    lines = HOOK_PS1.read_text(encoding="utf-8-sig").split("\n")
    start = next(i for i, line in enumerate(lines) if line.endswith("$StopFailureCore = @'"))
    end = next(i for i, line in enumerate(lines) if i > start and line == "'@")
    return "\n".join(lines[start + 1 : end])


def test_ps1_core_matches_sh_core():
    """The embedded python core is ONE implementation delivered to both OSes.
    The .sh heredoc's ``$(...)`` strips the trailing newline that the .ps1
    here-string keeps, so equality is on the rstrip'd text; every other byte
    must match. (A ``_lib/*.py`` shared file would NOT ship — bundle globs
    are ``*.sh``/``*.ps1`` only — hence the duplication with this pin.)"""
    import ast

    sh_core, ps1_core = _sh_core(), _ps1_core()
    assert sh_core.rstrip("\n") == ps1_core.rstrip("\n")
    ast.parse(sh_core)  # still valid python after any edit to either file


def test_ps1_sibling_carries_the_kill_switch():
    """Structural fingerprints for the .ps1 (not executable on this CI)."""
    raw = HOOK_PS1.read_bytes()
    # Windows PS 5.1 needs the BOM (OS-EXEMPT-PARITY 2026-05-22).
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert 'if ($env:VCO_STOP_FAILURE_NOTIFY -eq "0") { exit 0 }' in text
    # The kill switch sits AFTER the core run (ledger written) and BEFORE the
    # notify block.
    core_run = text.index("& $PY -c $StopFailureCore")
    kill_switch = text.index('VCO_STOP_FAILURE_NOTIFY -eq "0"')
    notify_call = text.index("& $PY $NotifyScript")
    assert core_run < kill_switch < notify_call
    # Same dedup window and truncation cap as the .sh core.
    assert "WINDOW_SECS = 300" in text
    assert "RAW_CAP = 500" in text
    assert 'if ($env:VCT_DISABLE_HOOKS) { exit 0 }' in text


def test_sh_kill_switch_gates_only_the_notification():
    text = HOOK_SH.read_text(encoding="utf-8")
    core_run = text.index('"$PY" -c "$_VCO_SF_CORE"')
    kill_switch = text.index('${VCO_STOP_FAILURE_NOTIFY:-1}" = "0"')
    notify_call = text.index('"$PY" "$PROJECT_DIR/.claude/scripts/notify.py"')
    assert core_run < kill_switch < notify_call


def test_hook_is_executable():
    mode = HOOK_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "the harness invokes this hook directly"
