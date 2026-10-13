# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-C2 (census F8) — alerts a SUBAGENT hit, delivered to its PARENT.

Harness fact (probe 2026-10-13, Claude Code 2.1.285): a PreToolUse /
PostToolUse ``additionalContext`` reaches only the agent that made the tool
call. So a credential alert raised on a subagent's edit (post-tool-security),
or a SubagentStop finding (the reconciler's kept-worktree notice, a worktree
violation, a credential in a file no tool event saw), never reached the main
chat: the subagent saw it — or nobody did — and the only human channel was a
desktop toast. SubagentStop context is no answer either: it keeps the
subagent running (vendor hooks doc, SubagentStop).

The channel:

* **writers** append one JSON row per alert to
  ``<project>/.claude/state/agent_alerts_<key>.jsonl``, ``<key>`` = the
  per-agent state key (:func:`vco_lib.hook_agent_key.agent_state_key`).
  Shell writers compose the same path with ``_lib/session-id.sh``
  ``vco_agent_alerts_file`` / ``session-id.ps1`` ``Get-VcoAgentAlertsFile``;
  Python writers call :func:`append_alert`. A row carries ``kind``,
  ``message`` (the human text, path + line + shape, NEVER a secret value),
  ``session_id``, ``agent_id``, ``agent_type`` and a timestamp.
* **the reader** (:func:`deliver`, hook ``agent-alerts-deliver.{sh,ps1}``)
  surfaces them to the PARENT as ``additionalContext``:

  - on ``PostToolUse`` for ``Agent``/``Task`` — the foreground case: the
    payload's ``tool_response.agentId`` names the finished subagent, so its
    rows are delivered the moment its report lands;
  - on ``UserPromptSubmit`` — every pending row of the session. This covers
    a BACKGROUND agent (its PostToolUse:Agent fires at LAUNCH; completion
    arrives as a synthetic ``<task-notification>`` prompt) and any row a
    detached SubagentStop job wrote after the foreground delivery ran.

  The reader selects rows by their ``session_id`` / ``agent_id`` FIELDS, not
  by the file name, so the path convention is a writer-side detail. Each
  file is claimed with an atomic rename before it is read, so two
  concurrent deliveries can never surface one alert twice; a delivered file
  is deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vco_lib.hook_agent_key import agent_state_key, sanitize_agent_id

#: File prefix/suffix of a pending alert file under ``.claude/state``.
#: MUST MATCH ``_lib/session-id.sh`` ``vco_agent_alerts_file`` and
#: ``session-id.ps1`` ``Get-VcoAgentAlertsFile``.
ALERT_FILE_PREFIX = "agent_alerts_"
ALERT_FILE_SUFFIX = ".jsonl"

#: Bound on what one delivery puts into the parent's context.
MAX_DELIVERED_ROWS = 20
MAX_MESSAGE_CHARS = 600

_HEADER = (
    "[Subagent alerts] Your subagents hit the following. Hook context "
    "reaches only the agent that made the call, so they (not you) saw "
    "these when they happened:"
)


def _state_dir(project_root: str) -> Path:
    return Path(project_root) / ".claude" / "state"


def _sanitize_sid(session_id: str) -> str:
    # The ONE session-id rule (lazy import: inject_intent is heavier than
    # this module needs on its import path).
    from vco_lib.inject_intent import sanitize_session_id

    return sanitize_session_id(session_id or "")


def alert_file(project_root: str, session_id: str, agent_id: str) -> Optional[Path]:
    """Pending-alert file for one (session, subagent). ``None`` for the main
    chat (no agent id): the main chat already receives its own hook context.

    An untrustworthy session id cannot form a composite key
    (:func:`agent_state_key` returns it unchanged), so such an alert files
    under ``nosession__<agent>`` — still found by an agent-scoped delivery."""
    agent = sanitize_agent_id(agent_id or "")
    if not project_root or not agent:
        return None
    key = agent_state_key(_sanitize_sid(session_id), agent)
    if not key.endswith("__" + agent):
        key = f"nosession__{agent}"
    return _state_dir(project_root) / f"{ALERT_FILE_PREFIX}{key}{ALERT_FILE_SUFFIX}"


def append_alert(project_root: str, session_id: str, agent_id: str,
                 agent_type: str, kind: str, message: str,
                 extra: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Append one alert row; returns the file, or ``None`` when there is
    nothing to file (main chat) or the write failed. Never raises."""
    path = alert_file(project_root, session_id, agent_id)
    if path is None or not message:
        return None
    row: Dict[str, Any] = dict(extra or {})
    row.update({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind or "alert",
        "message": message,
        "session_id": session_id or "",
        "agent_id": sanitize_agent_id(agent_id or ""),
        "agent_type": agent_type or "",
    })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return None


def _claim_and_read(path: Path) -> List[Dict[str, Any]]:
    """Atomically take ``path`` (rename to a per-process name), read its
    rows, delete it. A file another delivery already took yields []."""
    claimed = path.with_name(f"{path.name}.delivering.{os.getpid()}")
    try:
        os.replace(path, claimed)
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(claimed, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    try:
        claimed.unlink()
    except OSError:
        pass
    return rows


def _row_matches(row: Dict[str, Any], session_id: str, agent_id: str) -> bool:
    if agent_id:
        return sanitize_agent_id(str(row.get("agent_id") or "")) == agent_id
    return bool(session_id) and str(row.get("session_id") or "") == session_id


def collect(project_root: str, session_id: str, agent_id: str = "") -> List[Dict[str, Any]]:
    """Take every pending row for ``agent_id`` (any session) or, with no
    agent id, for ``session_id``. Rows of a claimed file that belong to
    someone else are written back (the claim is per file, the match per
    row). Never raises."""
    agent = sanitize_agent_id(agent_id or "")
    if not project_root or (not agent and not session_id):
        return []
    state = _state_dir(project_root)
    try:
        if agent:
            candidates = sorted(state.glob(f"{ALERT_FILE_PREFIX}*__{agent}{ALERT_FILE_SUFFIX}"))
        else:
            candidates = sorted(state.glob(f"{ALERT_FILE_PREFIX}*{ALERT_FILE_SUFFIX}"))
    except OSError:
        return []
    taken: List[Dict[str, Any]] = []
    for path in candidates:
        if not agent:
            # Session sweep: read-only peek first, so another session's
            # file is never claimed (and rewritten) by this one.
            if not _file_has_session(path, session_id):
                continue
        rows = _claim_and_read(path)
        mine = [r for r in rows if _row_matches(r, session_id, agent)]
        others = [r for r in rows if not _row_matches(r, session_id, agent)]
        if others:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    for r in others:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            except OSError:
                pass
        taken.extend(mine)
    return taken


def _file_has_session(path: Path, session_id: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and str(row.get("session_id") or "") == session_id:
                    return True
    except OSError:
        return False
    return False


def format_alerts(rows: List[Dict[str, Any]]) -> str:
    """The parent-facing text ("" when there is nothing)."""
    if not rows:
        return ""
    lines = [_HEADER]
    for row in rows[:MAX_DELIVERED_ROWS]:
        msg = str(row.get("message") or "").strip().replace("\n", " ")
        if len(msg) > MAX_MESSAGE_CHARS:
            msg = msg[:MAX_MESSAGE_CHARS] + "..."
        who = str(row.get("agent_type") or "subagent")
        aid = str(row.get("agent_id") or "")
        lines.append(f"- [{who} {aid}] {msg}" if aid else f"- [{who}] {msg}")
    if len(rows) > MAX_DELIVERED_ROWS:
        lines.append(f"- ... and {len(rows) - MAX_DELIVERED_ROWS} more "
                     "(see .claude/logs/credential_alerts.jsonl and "
                     ".claude/logs/worktree-guard.jsonl)")
    return "\n".join(lines)


def deliver_for_payload(project_root: str, payload: Dict[str, Any]) -> str:
    """The hook entry: the JSON envelope to print for ``payload`` ("" when
    there is nothing to deliver). PostToolUse(Agent|Task) delivers the
    finished agent's rows; UserPromptSubmit delivers the session's."""
    event = str(payload.get("hook_event_name") or "")
    session_id = str(payload.get("session_id") or "")
    agent_id = ""
    if event == "PostToolUse":
        if str(payload.get("tool_name") or "") not in ("Agent", "Task"):
            return ""
        resp = payload.get("tool_response")
        if isinstance(resp, dict):
            agent_id = str(resp.get("agentId") or resp.get("agent_id") or "")
        if not agent_id:
            return ""  # nothing names the agent; the prompt sweep covers it
    elif event != "UserPromptSubmit":
        return ""
    rows = collect(project_root, session_id, agent_id)
    text = format_alerts(rows)
    if not text:
        return ""
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    })


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vco_lib.agent_alerts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dv = sub.add_parser("deliver", help="hook: read the payload on stdin, print the envelope")
    dv.add_argument("--project-root", required=True)
    ap_ = sub.add_parser("append", help="append one alert; the row JSON on stdin")
    ap_.add_argument("--project-root", required=True)
    args = ap.parse_args(argv)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    if args.cmd == "deliver":
        out = deliver_for_payload(args.project_root, data)
        if out:
            sys.stdout.write(out + "\n")
        return 0
    extra = {k: v for k, v in data.items()
             if k not in ("session_id", "agent_id", "agent_type", "kind", "message")}
    append_alert(args.project_root, str(data.get("session_id") or ""),
                 str(data.get("agent_id") or ""), str(data.get("agent_type") or ""),
                 str(data.get("kind") or ""), str(data.get("message") or ""), extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
