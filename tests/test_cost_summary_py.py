# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the portable templates/scripts/cost-summary.py (v0.2.54 Track G,
G-3). The old bash-only wrapper both excluded native Windows AND had a broken
argument path (`--days N` was silently ignored — the heredoc read a DAYS env
var that was never exported). These tests pin the argparse contract live, via
subprocess, exactly as a user would run it."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "templates" / "scripts" / "cost-summary.py"


def _write_costs(tmp_path: Path, records: list[dict]) -> Path:
    f = tmp_path / "costs.jsonl"
    f.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return f


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
    )


def _record(ts: datetime, model: str = "claude-sonnet-4-6", cost: float | None = 0.5,
            session: str = "s1", auth: str | None = "api") -> dict:
    r = {
        "timestamp": ts.isoformat(),
        "session_id": session,
        "model": model,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cost_usd": cost,
    }
    if auth is not None:
        r["auth_mode"] = auth
    return r


def test_summary_totals_and_model_breakdown(tmp_path):
    now = datetime.now(timezone.utc)
    costs = _write_costs(tmp_path, [
        _record(now, cost=0.25),
        _record(now, model="claude-opus-4-7", cost=1.0),
        _record(now, cost=None, auth="subscription"),
    ])
    proc = _run("--costs-file", str(costs))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "Records: 3" in out
    assert "1 subscription" in out
    assert "$1.2500" in out  # 0.25 + 1.0 billable; subscription excluded
    assert "claude-opus-4-7" in out


def test_days_filter_actually_filters(tmp_path):
    """The headline G-3 bug: --days must have an effect."""
    now = datetime.now(timezone.utc)
    costs = _write_costs(tmp_path, [
        _record(now),
        _record(now - timedelta(days=30)),
    ])
    proc = _run("--costs-file", str(costs), "--days", "7")
    assert proc.returncode == 0, proc.stderr
    assert "Records: 1" in proc.stdout


def test_session_filter(tmp_path):
    now = datetime.now(timezone.utc)
    costs = _write_costs(tmp_path, [
        _record(now, session="alpha"),
        _record(now, session="beta"),
        _record(now, session="beta"),
    ])
    proc = _run("--costs-file", str(costs), "--session", "beta")
    assert proc.returncode == 0, proc.stderr
    assert "Records: 2" in proc.stdout


def test_missing_file_is_friendly_exit_zero(tmp_path):
    proc = _run("--costs-file", str(tmp_path / "nope.jsonl"))
    assert proc.returncode == 0
    assert "No cost data yet" in proc.stdout


def test_torn_lines_tolerated(tmp_path):
    now = datetime.now(timezone.utc)
    f = tmp_path / "costs.jsonl"
    f.write_text(
        json.dumps(_record(now)) + "\n{torn-line\n", encoding="utf-8"
    )
    proc = _run("--costs-file", str(f))
    assert proc.returncode == 0, proc.stderr
    assert "Records: 1" in proc.stdout


def test_bash_shim_delegates_to_py():
    """The POSIX shim must exec cost-summary.py (no more inline heredoc)."""
    shim = REPO_ROOT / "templates" / "scripts" / "cost-summary"
    body = shim.read_text(encoding="utf-8")
    assert "cost-summary.py" in body
    assert "PYEOF" not in body, "inline heredoc resurrected — keep logic in the .py"
