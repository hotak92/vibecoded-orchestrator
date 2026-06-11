# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live-process tests for templates/scripts/spawn_background_agent.py.

Per the V52-AH lesson (argv-shape mocks don't catch real failures), these
tests spawn REAL detached subprocesses through the production code path —
only the `claude` binary is a stub script (so no tokens are burned and CI
needs no credentials). The stub records its argv, sleeps briefly, exits.

A gated REAL-claude test (skipped by default) exists at the bottom for
manual verification: VCO_SPAWN_LIVE_TEST=1 pytest -k real_claude.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "templates" / "scripts" / "spawn_background_agent.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX stub-binary tests")


def _load_module():
    spec = importlib.util.spec_from_file_location("spawn_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_project(tmp_path: Path) -> Path:
    """A minimal project: .claude/agents/<name>.md + a stub claude binary."""
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-graph-updater.md").write_text(
        "---\nname: code-graph-updater\nmodel: haiku\n---\nYou update the code graph.\n",
        encoding="utf-8",
    )
    (agents / "no-model-agent.md").write_text(
        "---\nname: no-model-agent\nmodel: inherit\n---\nBody only.\n",
        encoding="utf-8",
    )
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" > "$(dirname "$0")/argv.txt"\n'
        "sleep 1\n"
        'echo "{\\"result\\": \\"stub\\"}"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run(project: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["VCO_CLAUDE_BIN"] = str(project / "bin" / "claude")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _records(project: Path) -> list[dict]:
    f = project / ".claude" / "state" / "spawned_agents.jsonl"
    if not f.is_file():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"stub pid {pid} still alive after {timeout}s")


# ---------------------------------------------------------------------------
# Spawn lifecycle (flag form, doc-compat)
# ---------------------------------------------------------------------------


def test_spawn_flag_form_full_lifecycle(fake_project):
    res = _run(fake_project, "--agent", "code-graph-updater",
               "--files", "a.py b.py", "--priority", "2", "--background")
    assert res.returncode == 0, res.stderr
    recs = _records(fake_project)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["agent"] == "code-graph-updater"
    assert rec["context"] == "a.py b.py"
    assert rec["priority"] == 2
    assert rec["status"] == "running"
    assert rec["model"] == "haiku"

    # The stub really ran: argv recorded, model + system-prompt-file passed.
    _wait_pid_gone(rec["pid"])
    argv = (fake_project / "bin" / "argv.txt").read_text(encoding="utf-8")
    assert "--append-system-prompt-file" in argv
    assert "--model" in argv and "haiku" in argv
    assert "a.py b.py" in argv  # context reached the task prompt

    # Log captured stub output.
    log = Path(rec["log"]).read_text(encoding="utf-8")
    assert "stub" in log

    # --list infers done after the pid exits.
    res2 = _run(fake_project, "--list")
    assert res2.returncode == 0
    assert "done" in res2.stdout


def test_spawn_positional_form(fake_project):
    res = _run(fake_project, "code-graph-updater", "full", "--priority", "4")
    assert res.returncode == 0, res.stderr
    rec = _records(fake_project)[0]
    assert rec["context"] == "full"
    assert rec["priority_label"] == "low"
    _wait_pid_gone(rec["pid"])


def test_inherit_model_not_passed(fake_project):
    res = _run(fake_project, "--agent", "no-model-agent", "--mode", "quick")
    assert res.returncode == 0, res.stderr
    rec = _records(fake_project)[0]
    assert rec["model"] is None
    _wait_pid_gone(rec["pid"])
    argv = (fake_project / "bin" / "argv.txt").read_text(encoding="utf-8")
    assert "--model" not in argv


def test_unknown_agent_fails_cleanly(fake_project):
    res = _run(fake_project, "--agent", "does-not-exist", "--files", "x")
    assert res.returncode == 1
    assert "not found" in res.stderr
    assert _records(fake_project) == []


def test_status_subcommand(fake_project):
    _run(fake_project, "--agent", "code-graph-updater", "--files", "x")
    rec = _records(fake_project)[0]
    res = _run(fake_project, "--status", rec["id"])
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["id"] == rec["id"]
    _wait_pid_gone(rec["pid"])


def test_cancel_running_spawn(fake_project):
    # Slow stub so it's still alive when we cancel.
    stub = fake_project / "bin" / "claude"
    stub.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    _run(fake_project, "--agent", "code-graph-updater", "--files", "x")
    rec = _records(fake_project)[0]
    res = _run(fake_project, "--cancel", rec["id"])
    assert res.returncode == 0, res.stderr
    _wait_pid_gone(rec["pid"])
    latest = {r["id"]: r for r in _records(fake_project)}
    assert latest[rec["id"]]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


def test_concurrency_cap_refuses(fake_project):
    stub = fake_project / "bin" / "claude"
    stub.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    env = {"VCO_MAX_BACKGROUND_AGENTS": "1"}
    res1 = _run(fake_project, "--agent", "code-graph-updater", "--files", "x", env_extra=env)
    assert res1.returncode == 0
    res2 = _run(fake_project, "--agent", "code-graph-updater", "--files", "y", env_extra=env)
    assert res2.returncode == 2
    assert "refused" in res2.stderr
    # cleanup
    rec = _records(fake_project)[0]
    _run(fake_project, "--cancel", rec["id"])


def test_cap_frees_after_completion(fake_project):
    env = {"VCO_MAX_BACKGROUND_AGENTS": "1"}
    res1 = _run(fake_project, "--agent", "code-graph-updater", "--files", "x", env_extra=env)
    assert res1.returncode == 0
    _wait_pid_gone(_records(fake_project)[0]["pid"])
    res2 = _run(fake_project, "--agent", "code-graph-updater", "--files", "y", env_extra=env)
    assert res2.returncode == 0, res2.stderr
    _wait_pid_gone(_records(fake_project)[1]["pid"])


# ---------------------------------------------------------------------------
# Gated real-claude verification (manual; burns tokens)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("VCO_SPAWN_LIVE_TEST") != "1",
                    reason="set VCO_SPAWN_LIVE_TEST=1 for the real-claude spawn test")
def test_real_claude_spawn(tmp_path):
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "echo-agent.md").write_text(
        "---\nname: echo-agent\nmodel: haiku\n---\n"
        "You are a test agent. Reply with exactly: SPAWN-TEST-OK\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env.pop("VCO_CLAUDE_BIN", None)
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--agent", "echo-agent", "--files", "none"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    rec_file = tmp_path / ".claude" / "state" / "spawned_agents.jsonl"
    rec = json.loads(rec_file.read_text(encoding="utf-8").splitlines()[0])
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            os.kill(rec["pid"], 0)
            time.sleep(2)
        except ProcessLookupError:
            break
    log = Path(rec["log"]).read_text(encoding="utf-8")
    assert "SPAWN-TEST-OK" in log
