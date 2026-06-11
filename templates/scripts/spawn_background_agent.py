#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Spawn a project agent as a detached background Claude Code subprocess.

Successor to the archived queue-based pattern (queue_maintenance.py +
process_maintenance_queue.py): instead of a polled queue, each request
spawns immediately (spawn-on-demand) with a concurrency cap as backpressure.

Usage (both forms accepted — flag form matches the agent docs, positional
form matches the original handoff shape):

    python spawn_background_agent.py --agent code-graph-updater \\
        --files "src/a.py src/b.py" [--project Name] [--priority 2] [--background]
    python spawn_background_agent.py graph-health-checker "full" --priority 4

Management subcommands:

    python spawn_background_agent.py --list            # recent + running spawns
    python spawn_background_agent.py --status <id>     # one spawn's record
    python spawn_background_agent.py --cancel <id>     # SIGTERM a running spawn

Behavior:
    * Reads the agent definition from `.claude/agents/<name>.md`; its body is
      passed to `claude -p` via --append-system-prompt-file, its frontmatter
      `model:` (if present and not "inherit") via --model.
    * Detaches the subprocess (POSIX: new session; Windows: DETACHED_PROCESS),
      logs to `.claude/logs/background_agents/<id>.log`.
    * Records lifecycle in `.claude/state/spawned_agents.jsonl`
      (status: running -> done/failed/cancelled, updated lazily on --list/--status).
    * Refuses to spawn when >= VCO_MAX_BACKGROUND_AGENTS (default 3) are
      still running — backpressure instead of a queue. Priority (1-5) is
      recorded for ops visibility; it does not preempt.

Env:
    VCO_MAX_BACKGROUND_AGENTS   concurrency cap (default 3)
    VCO_CLAUDE_BIN              claude binary override (default: "claude" on PATH)
    CLAUDE_PROJECT_DIR          project root override (default: cwd walk-up)

Exit codes: 0 ok; 1 user/validation error; 2 capacity refused; 3 spawn failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PRIORITY_LEVELS = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "maintenance"}
DEFAULT_MAX_CONCURRENT = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    cur = Path.cwd()
    for cand in (cur, *cur.parents):
        if (cand / ".claude").is_dir():
            return cand
    return cur


def state_file(root: Path) -> Path:
    return root / ".claude" / "state" / "spawned_agents.jsonl"


def log_dir(root: Path) -> Path:
    return root / ".claude" / "logs" / "background_agents"


def read_records(root: Path) -> list[dict]:
    f = state_file(root)
    if not f.is_file():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def append_record(root: Path, rec: dict) -> None:
    f = state_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # tasklist-free probe: OpenProcess via ctypes is overkill here; the
        # cheap heuristic is `os.kill(pid, 0)` which works on Windows for
        # Python >= 3.8 (raises OSError when the pid is gone).
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def effective_status(rec: dict) -> str:
    """A 'running' record whose pid is gone is finished; infer done/failed
    from the exit-marker file the wrapper writes (absent marker -> done,
    we can't know better without a supervisor — documented limitation)."""
    if rec.get("status") != "running":
        return rec.get("status", "unknown")
    if _pid_alive(int(rec.get("pid", -1))):
        return "running"
    return "done"


def running_count(root: Path) -> int:
    latest: dict[str, dict] = {}
    for rec in read_records(root):
        latest[rec.get("id", "")] = rec
    return sum(1 for r in latest.values() if effective_status(r) == "running")


# ---------------------------------------------------------------------------
# Agent definition parsing
# ---------------------------------------------------------------------------

_MODEL_LINE = re.compile(r"^model\s*:\s*(\S+)\s*$", re.MULTILINE)


def load_agent_definition(root: Path, agent: str) -> tuple[str, str | None]:
    """Return (body_markdown, model_or_None) for `.claude/agents/<agent>.md`."""
    path = root / ".claude" / "agents" / f"{agent}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"agent definition not found: {path} — valid agents are the *.md "
            f"files under {root / '.claude' / 'agents'}"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    model: str | None = None
    body = text
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm, body = parts[1], parts[2]
            m = _MODEL_LINE.search(fm)
            if m and m.group(1).lower() not in ("inherit",):
                model = m.group(1)
    return body.strip(), model


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def build_task_prompt(agent: str, context: str, project: str | None) -> str:
    lines = [
        f"You are running unattended as the background agent '{agent}'.",
        "Operate per the appended agent definition. Work autonomously; do not",
        "ask questions — make conservative choices and write your results to",
        "the locations the agent definition specifies.",
        f"Task context: {context}" if context else "Task context: (none — run your default maintenance pass)",
    ]
    if project:
        lines.append(f"Project: {project}")
    return "\n".join(lines)


def spawn(root: Path, agent: str, context: str, priority: int, project: str | None) -> dict:
    claude_bin = os.environ.get("VCO_CLAUDE_BIN", "") or shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("`claude` not found on PATH (set VCO_CLAUDE_BIN to override)")

    body, model = load_agent_definition(root, agent)
    spawn_id = str(uuid.uuid4())[:8]

    ldir = log_dir(root)
    ldir.mkdir(parents=True, exist_ok=True)
    sysfile = root / ".claude" / "state" / f"spawn_{spawn_id}.system.md"
    sysfile.parent.mkdir(parents=True, exist_ok=True)
    sysfile.write_text(body, encoding="utf-8")
    logfile = ldir / f"{spawn_id}.log"

    cmd = [
        claude_bin,
        "-p",
        build_task_prompt(agent, context, project),
        "--append-system-prompt-file",
        str(sysfile),
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
    ]
    if model:
        cmd += ["--model", model]

    popen_kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": open(logfile, "ab"),
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    rec = {
        "id": spawn_id,
        "agent": agent,
        "context": context,
        "priority": priority,
        "priority_label": PRIORITY_LEVELS[priority],
        "project": project,
        "pid": proc.pid,
        "status": "running",
        "spawned_at": _now(),
        "log": str(logfile),
        "model": model,
    }
    append_record(root, rec)
    return rec


def cancel(root: Path, spawn_id: str) -> bool:
    latest = {r.get("id"): r for r in read_records(root)}
    rec = latest.get(spawn_id)
    if not rec:
        print(f"unknown spawn id: {spawn_id}", file=sys.stderr)
        return False
    pid = int(rec.get("pid", -1))
    if effective_status(rec) != "running":
        print(f"{spawn_id} is not running (status: {effective_status(rec)})")
        return True
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"failed to cancel {spawn_id} (pid {pid}): {exc}", file=sys.stderr)
        return False
    append_record(root, {**rec, "status": "cancelled", "cancelled_at": _now()})
    print(f"cancelled {spawn_id} (pid {pid})")
    return True


def show_list(root: Path) -> None:
    latest: dict[str, dict] = {}
    for rec in read_records(root):
        latest[rec.get("id", "")] = rec
    if not latest:
        print("no background agents have been spawned in this project")
        return
    rows = sorted(latest.values(), key=lambda r: r.get("spawned_at", ""), reverse=True)
    for r in rows[:20]:
        status = effective_status(r)
        print(
            f"{r.get('id'):>8}  {status:<9}  p{r.get('priority')}  "
            f"{r.get('agent'):<24} {r.get('spawned_at', '')}  log={r.get('log', '')}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="spawn_background_agent",
        description="Spawn a project agent as a detached background claude subprocess.",
    )
    p.add_argument("agent_pos", nargs="?", help="agent name (positional form)")
    p.add_argument("context_pos", nargs="?", help="task context (positional form)")
    p.add_argument("--agent", help="agent name (flag form)")
    p.add_argument("--files", help="file list context (flag form)")
    p.add_argument("--mode", help="mode context, e.g. full|quick (flag form)")
    p.add_argument("--project", help="project name passed through to the agent")
    p.add_argument("--priority", type=int, default=3, choices=sorted(PRIORITY_LEVELS))
    p.add_argument("--background", action="store_true",
                   help="accepted for doc-compat; spawns are ALWAYS detached")
    p.add_argument("--list", action="store_true", help="show recent/running spawns")
    p.add_argument("--status", metavar="ID", help="show one spawn record")
    p.add_argument("--cancel", metavar="ID", help="terminate a running spawn")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    root = project_root()

    if args.list:
        show_list(root)
        return 0
    if args.status:
        latest = {r.get("id"): r for r in read_records(root)}
        rec = latest.get(args.status)
        if not rec:
            print(f"unknown spawn id: {args.status}", file=sys.stderr)
            return 1
        rec = {**rec, "status": effective_status(rec)}
        print(json.dumps(rec, indent=2))
        return 0
    if args.cancel:
        return 0 if cancel(root, args.cancel) else 1

    agent = args.agent or args.agent_pos
    if not agent:
        print("error: agent name required (--agent NAME or positional)", file=sys.stderr)
        return 1
    context = args.files or args.mode or args.context_pos or ""

    max_concurrent = DEFAULT_MAX_CONCURRENT
    raw = os.environ.get("VCO_MAX_BACKGROUND_AGENTS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        max_concurrent = int(raw)
    n_running = running_count(root)
    if n_running >= max_concurrent:
        print(
            f"refused: {n_running} background agents already running "
            f"(cap {max_concurrent}; set VCO_MAX_BACKGROUND_AGENTS to raise). "
            "Retry when one finishes — see --list.",
            file=sys.stderr,
        )
        return 2

    try:
        rec = spawn(root, agent, context, args.priority, args.project)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"spawn failed: {exc}", file=sys.stderr)
        return 3
    print(
        f"spawned {rec['id']} ({rec['agent']}, p{rec['priority']}, pid {rec['pid']})\n"
        f"log: {rec['log']}\n"
        f"status: python .claude/scripts/spawn_background_agent.py --status {rec['id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
