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
  per-agent state key (:func:`vco_lib.hook_agent_key.agent_state_key`),
  through :func:`append_alert` — the ONE writer. The shell hooks
  (``subagent-stop-reconcile``, ``post-tool-security``) reach it through
  ``python -m vco_lib.agent_alerts append`` (the row's inputs as JSON on
  stdin; ``_lib/agent-alerts.{sh,ps1}``), so the path, the row format and
  the locking live here only. A row carries ``kind``, ``message`` (the
  human text, path + line + shape, NEVER a secret value), ``session_id``,
  ``agent_id``, ``agent_type`` and a timestamp.
* **the reader** (:func:`deliver_for_payload`, hook
  ``agent-alerts-deliver.{sh,ps1}``) surfaces them to the PARENT as
  ``additionalContext``:

  - on ``PostToolUse`` for ``Agent``/``Task`` naming a subagent — the
    foreground case: ``tool_response.agentId`` names the finished
    subagent, so its rows are delivered the moment its report lands;
  - on any other MAIN-CHAT ``PostToolUse`` (no ``agent_id`` in the
    payload; W6-C2 fix round, review R4c-10) and on ``UserPromptSubmit`` —
    every pending row of the session. This covers a BACKGROUND agent (its
    PostToolUse:Agent fires at LAUNCH) and a row a detached SubagentStop
    job wrote after the foreground delivery ran, without waiting for the
    next prompt in a long autonomous turn. The hook starts no interpreter
    unless an alert file exists.

  The reader selects rows by their ``session_id`` / ``agent_id`` FIELDS, not
  by the file name, so the path convention is a writer-side detail.

Concurrency (W6-C2 fix round, review R4c-2): every append and every claim
holds ONE per-project lock (``.claude/state/agent_alerts.lock``;
``flock`` on POSIX, the ``kg-update-nudge`` O_EXCL sidecar on Windows —
:func:`vco_lib.atomic.portable_exclusive_lock`). A writer therefore never
holds an open handle on a file a delivery is renaming: the row lands either
before the claim (and is delivered) or after it (in a fresh file). The claim
renames the file to ``<name>.delivering.<pid>``; the claimed file is removed
only AFTER the envelope has been written, and a claim whose delivery died is
appended back to its file by the next delivery (review R4c-6) — an alert can
be delivered twice after a crash, never lost.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from vco_lib.atomic import claim_by_rename, claim_owner_gone, portable_exclusive_lock
from vco_lib.hook_agent_key import agent_state_key, sanitize_agent_id

#: File prefix/suffix of a pending alert file under ``.claude/state``.
#: MUST MATCH ``_lib/session-id.sh`` ``vco_agent_alerts_file`` and
#: ``session-id.ps1`` ``Get-VcoAgentAlertsFile``.
ALERT_FILE_PREFIX = "agent_alerts_"
ALERT_FILE_SUFFIX = ".jsonl"

#: The per-project lock every append and claim holds. Its name does not
#: match the ``agent_alerts_*.jsonl`` glob.
LOCK_FILE_NAME = "agent_alerts.lock"
#: Marker of a claimed (being-delivered) file: ``<name>.delivering.<pid>``.
DELIVERING_MARK = ".delivering."
#: A claim older than this is re-adopted even if its pid looks alive (a
#: delivery takes milliseconds; a minute means it is stuck or the pid was
#: reused).
DELIVERING_STALE_SECONDS = 60.0

#: ``None`` = pick the lock protocol by platform; tests set ``False`` to
#: drive the Windows (O_EXCL sidecar) protocol on POSIX.
_USE_FLOCK: Optional[bool] = None

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


def _lock(project_root: str):
    return portable_exclusive_lock(_state_dir(project_root) / LOCK_FILE_NAME,
                                   use_flock=_USE_FLOCK)


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
        with _lock(project_root):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return None


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
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
    return rows


def _claim(project_root: str, path: Path) -> Optional[Path]:
    """Take ``path`` under the lock (rename to ``<name>.delivering.<pid>``,
    mtime = now). ``None`` when another delivery took it first."""
    with _lock(project_root):
        return claim_by_rename(path, DELIVERING_MARK)


def _append_back(project_root: str, path: Path, rows: List[Dict[str, Any]]) -> bool:
    if not rows:
        return True
    try:
        with _lock(project_root):
            with open(path, "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def readopt_stale_deliveries(project_root: str,
                             stale_after_s: float = DELIVERING_STALE_SECONDS) -> int:
    """Append every abandoned ``*.delivering.<pid>`` claim (dead pid, or older
    than ``stale_after_s``) back to its alert file, so a delivery killed
    between its claim and its envelope loses nothing. Returns the count
    re-adopted. Never raises."""
    if not project_root:
        return 0
    state = _state_dir(project_root)
    try:
        claims = sorted(state.glob(f"{ALERT_FILE_PREFIX}*{ALERT_FILE_SUFFIX}{DELIVERING_MARK}*"))
    except OSError:
        return 0
    import time  # noqa: PLC0415

    adopted = 0
    for c in claims:
        base_name, _, pid_txt = c.name.rpartition(DELIVERING_MARK)
        try:
            pid = int(pid_txt)
        except ValueError:
            pid = -1
        try:
            age = time.time() - c.stat().st_mtime
        except OSError:
            continue
        if not claim_owner_gone(pid, age, stale_after_s=stale_after_s):
            continue
        if _append_back(project_root, c.with_name(base_name), _read_rows(c)):
            try:
                c.unlink()
            except OSError:
                pass
            adopted += 1
    return adopted


def _row_matches(row: Dict[str, Any], session_id: str, agent_id: str) -> bool:
    if agent_id:
        return sanitize_agent_id(str(row.get("agent_id") or "")) == agent_id
    return bool(session_id) and str(row.get("session_id") or "") == session_id


def _collect(project_root: str, session_id: str,
             agent_id: str = "") -> Tuple[List[Dict[str, Any]], List[Path]]:
    """Claim and read every pending row for ``agent_id`` (any session) or,
    with no agent id, for ``session_id``. Returns ``(rows, claims)``; the
    caller removes the claims (:func:`_finish`) only once the rows have
    been handed over. Rows of a claimed file that belong to someone else
    are written back (the claim is per file, the match per row). Never
    raises."""
    agent = sanitize_agent_id(agent_id or "")
    if not project_root or (not agent and not session_id):
        return [], []
    readopt_stale_deliveries(project_root)
    state = _state_dir(project_root)
    try:
        if agent:
            candidates = sorted(state.glob(f"{ALERT_FILE_PREFIX}*__{agent}{ALERT_FILE_SUFFIX}"))
        else:
            candidates = sorted(state.glob(f"{ALERT_FILE_PREFIX}*{ALERT_FILE_SUFFIX}"))
    except OSError:
        return [], []
    taken: List[Dict[str, Any]] = []
    claims: List[Path] = []
    for path in candidates:
        if not agent:
            # Session sweep: read-only peek first, so another session's
            # file is never claimed (and rewritten) by this one.
            if not _file_has_session(path, session_id):
                continue
        claimed = _claim(project_root, path)
        if claimed is None:
            continue
        rows = _read_rows(claimed)
        mine = [r for r in rows if _row_matches(r, session_id, agent)]
        others = [r for r in rows if not _row_matches(r, session_id, agent)]
        if not _append_back(project_root, path, others):
            # Could not hand the others back: keep the WHOLE claim on disk
            # (re-adopted later) rather than lose them; deliver nothing from
            # it now, so nothing is delivered twice either.
            continue
        taken.extend(mine)
        claims.append(claimed)
    return taken, claims


def _finish(claims: List[Path]) -> None:
    for c in claims:
        try:
            c.unlink()
        except OSError:
            pass


def collect(project_root: str, session_id: str, agent_id: str = "") -> List[Dict[str, Any]]:
    """Take (and remove) every pending row for ``agent_id`` (any session) or,
    with no agent id, for ``session_id``. Never raises."""
    rows, claims = _collect(project_root, session_id, agent_id)
    _finish(claims)
    return rows


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


def _target(payload: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """``(event, session_id, agent_id)`` to deliver for, or ``None``.

    * a SUBAGENT's own event (``agent_id`` in the payload) — never: its
      context reaches only the subagent, the very gap this channel closes;
    * PostToolUse for Agent/Task naming the agent (``tool_response.agentId``)
      — that agent's rows;
    * any other main-chat PostToolUse, and UserPromptSubmit — the session's
      rows (review R4c-10: no waiting for the next prompt)."""
    event = str(payload.get("hook_event_name") or "")
    if event not in ("PostToolUse", "UserPromptSubmit"):
        return None
    if str(payload.get("agent_id") or ""):
        return None
    session_id = str(payload.get("session_id") or "")
    agent_id = ""
    if event == "PostToolUse" and str(payload.get("tool_name") or "") in ("Agent", "Task"):
        resp = payload.get("tool_response")
        if isinstance(resp, dict):
            agent_id = str(resp.get("agentId") or resp.get("agent_id") or "")
    return event, session_id, agent_id


def _envelope(event: str, text: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    })


def deliver_for_payload(project_root: str, payload: Dict[str, Any],
                        emit: Optional[Callable[[str], None]] = None) -> str:
    """The hook entry: the JSON envelope for ``payload`` ("" when there is
    nothing to deliver). ``emit`` (the CLI passes a stdout writer) is called
    with the envelope BEFORE the claimed files are removed, so a delivery
    that dies before its envelope is out loses nothing (review R4c-6)."""
    target = _target(payload)
    if target is None:
        return ""
    event, session_id, agent_id = target
    rows, claims = _collect(project_root, session_id, agent_id)
    text = format_alerts(rows)
    out = _envelope(event, text) if text else ""
    if out and emit is not None:
        emit(out)
    _finish(claims)
    return out


def _emit_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vco_lib.agent_alerts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dv = sub.add_parser("deliver", help="hook: read the payload on stdin, print the envelope")
    dv.add_argument("--project-root", required=True)
    ap_ = sub.add_parser(
        "append",
        help="file one alert: the row's inputs as JSON on stdin (session_id, "
             "agent_id, agent_type, kind, message, optional extra keys). Exit 0 "
             "filed (or nothing to file: main chat), 1 the write failed, 2 bad input")
    ap_.add_argument("--project-root", required=True)
    args = ap.parse_args(argv)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if args.cmd == "deliver":
            return 0
        sys.stderr.write("agent_alerts append: stdin is not a JSON object - alert NOT filed\n")
        return 2
    if args.cmd == "deliver":
        deliver_for_payload(args.project_root, data, emit=_emit_stdout)
        return 0
    agent = sanitize_agent_id(str(data.get("agent_id") or ""))
    message = str(data.get("message") or "")
    if not agent or not message:
        return 0  # main chat / nothing to say: nothing to file, by contract
    extra = {k: v for k, v in data.items()
             if k not in ("session_id", "agent_id", "agent_type", "kind", "message")}
    path = append_alert(args.project_root, str(data.get("session_id") or ""),
                        agent, str(data.get("agent_type") or ""),
                        str(data.get("kind") or ""), message, extra)
    if path is None:
        sys.stderr.write(
            f"agent_alerts append: could not write the alert under "
            f"{_state_dir(args.project_root)} - it was NOT filed for the parent\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
