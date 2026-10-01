# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-17 — stop-failure-notify: the REAL payload shape (F1) and the
ledger promise on every path.

The shipped CLAUDE.md promises that the StopFailure ledger line "is written for
**every** event regardless" of notification coalescing. Two things made that
false before v0.2.100:

* F1 — Claude Code's StopFailure payload carries ``error`` as a STRING class
  (``"rate_limit"``, ``"authentication_failed"``, ``"unknown"``) and the human
  text in ``error_details`` / ``last_assistant_message``. The hook read only a
  dict-shaped ``error``, so every real event was filed as
  ``unknown: raw payload …`` (14/14 rows on the maintainer's machine).
* The ledger was written ONLY by the Python core. No interpreter, or a core
  that died before its write, recorded nothing — and the core's own write
  failure was swallowed by ``except: pass`` and ``2>/dev/null``.

The hooks run for real (bash, and pwsh when present) from a COPY of the
shipped hooks directory under ``tmp_path`` so a test can remove a ``_lib``
helper without touching the tree; HOME / VCT_STATE_DIR point into tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
PWSH = shutil.which("pwsh")

#: The payload Claude Code actually sends (shape recorded on this machine's
#: ledger; identifiers genericized).
REAL_PAYLOAD = {
    "session_id": "e0483e91-3a60-42bd-8cf0-0d81d1f98893",
    "transcript_path": "/tmp/x.jsonl",
    "cwd": "/tmp",
    "prompt_id": "p-1",
    "agent_id": "a2091ce159d6b22fe",
    "agent_type": "example-researcher",
    "effort": {"level": "high"},
    "hook_event_name": "StopFailure",
    "error": "unknown",
    "last_assistant_message": "API Error: 400 [1211][Unknown Model] the model id is not known",
}


@pytest.fixture()
def world(tmp_path: Path):
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    return hooks, home, project


def _env(home: Path, project: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for var in ("VCT_DISABLE_HOOKS", "VCO_STOP_FAILURE_NOTIFY", "VCO_METRICS_DIR",
                "VCO_METRICS_HOME", "VCO_LEGACY_METRICS_DIR", "VCO_METRICS_MIGRATED"):
        env.pop(var, None)
    env.update(
        HOME=str(home),
        VCT_STATE_DIR=str(home / ".vct"),
        VCT_CLAUDE_DIR=str(home / ".claude"),
        CLAUDE_PROJECT_DIR=str(project),
        # No desktop toast from a test run.
        VCO_STOP_FAILURE_NOTIFY="0",
    )
    env.update(extra)
    return env


def _run(flavour: str, hooks: Path, payload: str, env: dict) -> subprocess.CompletedProcess:
    if flavour == "sh":
        argv = ["bash", str(hooks / "stop-failure-notify.sh")]
    else:
        argv = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(hooks / "stop-failure-notify.ps1")]
    return subprocess.run(argv, input=payload.encode("utf-8"), env=env,
                          capture_output=True, timeout=120)


def _ledger(home: Path) -> list:
    path = home / ".vct" / "metrics" / "failures.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


FLAVOURS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(PWSH is None, reason="pwsh not installed"))]


def _no_python_bin(tmp_path: Path) -> Path:
    """A PATH holding only the tools the hook needs — and no python."""
    bindir = tmp_path / "nopy-bin"
    bindir.mkdir()
    for tool in ("bash", "cat", "head", "tail", "tr", "sed", "date", "dirname",
                 "basename", "mkdir", "ls", "grep", "printf", "env"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    return bindir


def _crashing_python_bin(tmp_path: Path) -> Path:
    """A PATH whose python exits 1 with no output — the 'core crashed' arm."""
    bindir = _no_python_bin(tmp_path)
    for name in ("python3", "python"):
        shim = bindir / name
        shim.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
    return bindir


# ─── F1: the real payload shape ─────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_real_payload_shape_is_parsed(flavour, world):
    hooks, home, project = world
    result = _run(flavour, hooks, json.dumps(REAL_PAYLOAD), _env(home, project))
    assert result.returncode == 0, result.stderr
    rows = _ledger(home)
    assert len(rows) == 1, result.stderr
    row = rows[0]
    assert row["error_type"] == "unknown"
    assert row["error_message"] == REAL_PAYLOAD["last_assistant_message"]
    assert not row["error_message"].startswith("raw payload")
    assert row["agent_type"] == "example-researcher"
    assert row["session_id"] == "e0483e91"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_string_error_class_and_error_details_win(flavour, world):
    hooks, home, project = world
    payload = dict(REAL_PAYLOAD, error="rate_limit", error_details="429 quota exhausted")
    _run(flavour, hooks, json.dumps(payload), _env(home, project))
    row = _ledger(home)[0]
    assert row["error_type"] == "rate_limit"
    assert row["error_message"] == "429 quota exhausted"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_dict_shaped_error_still_parsed(flavour, world):
    """Leave-alone: the older dict shape keeps working."""
    hooks, home, project = world
    payload = {"session_id": "0123456789", "error": {"type": "overloaded", "message": "529"}}
    _run(flavour, hooks, json.dumps(payload), _env(home, project))
    row = _ledger(home)[0]
    assert (row["error_type"], row["error_message"]) == ("overloaded", "529")
    assert "agent_type" not in row


# ─── the ledger promise on every path ───────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_no_python_still_writes_the_ledger(flavour, world, tmp_path):
    hooks, home, project = world
    bindir = _no_python_bin(tmp_path)
    payload = json.dumps(dict(REAL_PAYLOAD, last_assistant_message='quote " and ünïcode\nnewline'))
    result = _run(flavour, hooks, payload, _env(home, project, PATH=str(bindir)))
    assert result.returncode == 0, result.stderr
    rows = _ledger(home)  # json.loads raises on a corrupt line
    assert len(rows) == 1, result.stderr.decode(errors="replace")
    row = rows[0]
    assert row["ledger_writer"] == "shell-fallback"
    assert row["error_message"].startswith("raw payload (no Python interpreter): {")
    assert row["project"] == "proj"
    assert row["error_message"].isascii()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_crashed_core_still_writes_the_ledger(flavour, world, tmp_path):
    hooks, home, project = world
    bindir = _crashing_python_bin(tmp_path)
    result = _run(flavour, hooks, json.dumps(REAL_PAYLOAD), _env(home, project, PATH=str(bindir)))
    assert result.returncode == 0
    rows = _ledger(home)
    assert len(rows) == 1, result.stderr.decode(errors="replace")
    assert rows[0]["error_message"].startswith("raw payload (core unavailable): ")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_healthy_core_writes_exactly_one_line_no_fallback(flavour, world):
    """Leave-alone: with a working core the fallback never adds a second row."""
    hooks, home, project = world
    _run(flavour, hooks, json.dumps(REAL_PAYLOAD), _env(home, project))
    rows = _ledger(home)
    assert len(rows) == 1
    assert "ledger_writer" not in rows[0]


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_unwritable_ledger_is_reported_on_stderr(flavour, world):
    hooks, home, project = world
    metrics = home / ".vct" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "failures.jsonl").mkdir()  # a directory: the append must fail
    result = _run(flavour, hooks, json.dumps(REAL_PAYLOAD), _env(home, project))
    assert result.returncode == 0
    assert b"ledger line NOT written" in result.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_unresolvable_metrics_dir_is_reported_on_stderr(flavour, world):
    hooks, home, project = world
    lib = hooks / "_lib" / ("metrics-dir.sh" if flavour == "sh" else "metrics-dir.ps1")
    lib.unlink()
    result = _run(flavour, hooks, json.dumps(REAL_PAYLOAD), _env(home, project))
    assert result.returncode == 0
    assert b"ledger line NOT written" in result.stderr
    assert b"metrics directory could not be resolved" in result.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_empty_payload_is_still_an_event(flavour, world):
    hooks, home, project = world
    _run(flavour, hooks, "", _env(home, project))
    rows = _ledger(home)
    assert len(rows) == 1
    assert rows[0]["error_type"] == "unknown"
