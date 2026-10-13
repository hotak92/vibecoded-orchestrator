# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-PCHK — the ``edit_outcome`` RL row and its ``post_check``.

The ``post-edit-outcome.{sh,ps1}`` header promised, since v0.2.52, that a
forked background poller fills ``post_check`` from a status file written by
the PostToolUse checkers. Nothing wrote that file and every row carried
``post_check: None``. This module is the ONE home that makes the promise
true; both hook siblings and the checker hooks call into it.

Pieces (each has exactly one home here):

* **Coverage** — :data:`CHECKERS` says which shipped PostToolUse checker
  covers which ``(tool, file suffix)``. Today that is only
  ``py-compile-check`` (``Write`` of a ``*.py`` file; its settings matcher is
  bare ``Write`` and the ``.py`` gate lives in the hook). There is no ruff,
  cargo or tsc PostToolUse checker in the shipped settings. A file no
  checker covers gets ``post_check = {"status": "no_signal", "reason":
  "no_checker"}`` at once, without any wait. A file outside the project
  root is never covered: its status would land in another project's state.
* **Status records** — a checker writes
  ``<project>/.claude/state/last_compile_status_<pathkey>_<agentkey>.json``
  through :func:`vco_lib.atomic.atomic_write_json`. ``<pathkey>`` is a hash
  of the normalised ABSOLUTE path, so two same-named files in different
  directories never share a record (the old ``<file_basename>`` naming
  would have collided). ``<agentkey>`` is
  :func:`vco_lib.hook_agent_key.agent_state_key`, so a subagent's check is
  never credited to the main chat's edit of the same file, or the other way
  round. A record holds only: file path, checker name, ok, exit code,
  timestamp. Never file contents, never the compiler message.
* **The row** — :func:`edit_outcome_payload` is the one builder of the
  ``edit_outcome`` payload; :func:`emit_edit_outcome` the one call into
  ``emit_outcome_event``.
* **The poller** — when a checker covers the file, the hook writes the
  row as a PENDING record under ``.claude/state/edit_outcome_pending/`` and
  starts a DETACHED child (``python -m vco_lib.edit_post_check poll``). It
  waits up to ``VCT_EDIT_POST_CHECK_SECS`` (default 20) for a status record
  newer than the edit, fills ``post_check`` and emits the row ONCE. It never
  blocks the hook, never kills anything, and soft-fails.
* **Never drop the row** (owner rule: RL logs are always collected). Every
  failure path still emits the row: the child cannot start → the hook emits
  it at once with ``post_check: None``; the wait raises → the child emits
  ``None``; the child dies outright (SIGKILL, logout) → the pending record
  stays on disk and the next ``post-edit-outcome`` run (or the next poller)
  emits it with ``None`` once it is older than the window plus a grace
  period. Exactly one process emits each row: the poller and the sweeper
  both take the pending record by an atomic rename
  (:func:`vco_lib.atomic.claim_by_rename`) before they emit.
* **Housekeeping** — the poller deletes status records older than
  :data:`STATUS_TTL_S` (keys vary per session and agent, so they would
  otherwise pile up); nothing else under ``.claude/state`` is touched.

``post_check`` values:

* ``{"status": "ok" | "fail", "checker", "exit_code", "latency_ms"}``;
* ``{"status": "no_signal", "reason": "no_checker"}`` — nothing checks it;
* ``{"status": "no_signal", "reason": "timeout", "checker", "window_s"}`` —
  a checker covers it but reported nothing inside the window;
* ``None`` — the poller could not run (see above).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

#: The window env key. Read only through :func:`window_secs`.
ENV_WINDOW = "VCT_EDIT_POST_CHECK_SECS"
DEFAULT_WINDOW_S = 20
#: Upper clamp: a mistyped huge value must not leave pollers alive for hours.
MAX_WINDOW_S = 300

#: Status-record file name: ``<STATUS_PREFIX><pathkey>_<agentkey>.json``.
STATUS_PREFIX = "last_compile_status_"
#: Pending rows waiting for their poller, one ``<task_id>.json`` each.
PENDING_DIRNAME = "edit_outcome_pending"
#: Rename mark of a claimed pending record (``<name><mark><pid>``).
CLAIM_MARK = ".claim-"
#: Seconds past the window before an unclaimed pending record counts as
#: abandoned (a poller that started late on a loaded machine keeps its row).
SWEEP_GRACE_S = 60
#: A claimed record whose owner is gone is re-emitted after this age.
CLAIM_STALE_S = 600
#: A status record older than this is deleted by the poller: it can only
#: ever answer an edit made within the (at most 300 s) window, and keys vary
#: per session and agent, so without a sweep they would pile up.
STATUS_TTL_S = 3600
#: Rows a hook run sweeps at most (each emit is a hub POST; the async
#: dispatcher that runs the hook has a 15 s budget).
SWEEP_LIMIT_HOOK = 3
POLL_INTERVAL_S = 0.2

EmitFn = Callable[..., Any]


@dataclass(frozen=True)
class Checker:
    """A shipped PostToolUse checker: its name (the status ``checker``
    field), the tools whose payloads it receives, the suffixes it checks."""

    name: str
    tools: Tuple[str, ...]
    suffixes: Tuple[str, ...]


#: The coverage table. MUST MATCH the checker hooks: ``py-compile-check``
#: is registered on bare ``Write`` in both settings templates and gates on
#: ``.py`` itself (pinned by tests/test_v02102_w6pchk_edit_post_check.py).
CHECKERS: Tuple[Checker, ...] = (
    Checker(name="py_compile", tools=("Write",), suffixes=(".py",)),
)


# ── keys and paths ─────────────────────────────────────────────────────────


def window_secs(env: Optional[Mapping[str, str]] = None) -> int:
    """``VCT_EDIT_POST_CHECK_SECS``: 0..300, default 20; out of range clamps."""
    from vco_lib.env_values import env_int  # noqa: PLC0415

    return env_int(ENV_WINDOW, DEFAULT_WINDOW_S, env=env, minimum=0,
                   maximum=MAX_WINDOW_S, clamp=True)


def _norm_abs(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def path_key(file_path: str) -> str:
    """16 hex chars of sha256 over the normalised absolute path."""
    from vco_lib.hashing import sha256_text  # noqa: PLC0415

    return sha256_text(_norm_abs(file_path))[:16]


def agent_key(session_id: str, agent_id: str) -> str:
    """The per-agent component: the shared agent-key rule over the
    sanitised session id; an empty result is the literal ``default``."""
    from vco_lib.hook_agent_key import agent_state_key  # noqa: PLC0415
    from vco_lib.inject_intent import sanitize_session_id  # noqa: PLC0415

    return agent_state_key(sanitize_session_id(session_id or ""), agent_id or "") or "default"


def state_dir(project_root: str) -> Path:
    return Path(project_root) / ".claude" / "state"


def status_path(project_root: str, file_path: str, session_id: str, agent_id: str) -> Path:
    name = f"{STATUS_PREFIX}{path_key(file_path)}_{agent_key(session_id, agent_id)}.json"
    return state_dir(project_root) / name


def pending_dir(project_root: str) -> Path:
    return state_dir(project_root) / PENDING_DIRNAME


def _inside(file_path: str, project_root: str) -> bool:
    if not file_path or not project_root:
        return False
    try:
        f = os.path.normcase(os.path.realpath(file_path))
        r = os.path.normcase(os.path.realpath(project_root))
        return os.path.commonpath([f, r]) == r
    except (OSError, ValueError):  # different drives, empty, unresolvable
        return False


def covering_checker(tool_name: str, file_path: str, project_root: str) -> Optional[str]:
    """The checker that reports on this edit, or ``None`` (→ no_signal)."""
    if not _inside(file_path, project_root):
        return None
    # Case-sensitive, exactly like the hooks' own gates (``endswith(".py")``
    # / ``EndsWith('.py')``): a covered file must be one the checker checks.
    for checker in CHECKERS:
        if tool_name in checker.tools and file_path.endswith(checker.suffixes):
            return checker.name
    return None


# ── producer side (the checker hooks) ──────────────────────────────────────


def write_status(*, project_root: str, file_path: str, checker: str, tool_name: str,
                 exit_code: int, session_id: str, agent_id: str,
                 now_ms: Optional[int] = None) -> Optional[Path]:
    """Record one check result. ``None`` (nothing written) when ``checker``
    does not cover this ``(tool, file)`` here — the poller would not be
    waiting for it. Raises on a write failure (the hook reports it)."""
    if covering_checker(tool_name, file_path, project_root) != checker:
        return None
    from vco_lib.atomic import atomic_write_json  # noqa: PLC0415

    record = {
        "v": 1,
        "file_path": os.path.abspath(file_path),
        "checker": checker,
        "ok": int(exit_code) == 0,
        "exit_code": int(exit_code),
        "ts_ms": int(time.time() * 1000) if now_ms is None else int(now_ms),
    }
    path = status_path(project_root, file_path, session_id, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record, indent=None, fsync=False, mode=0o600)
    return path


def read_status(project_root: str, file_path: str, session_id: str,
                agent_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(status_path(project_root, file_path, session_id,
                                      agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if _norm_abs(str(data.get("file_path", ""))) != _norm_abs(file_path):
        return None  # a hash collision is not this file's result
    return data


# ── the row ────────────────────────────────────────────────────────────────


def edit_outcome_payload(*, tool_name: str, file_path: str, diff_size: int,
                         file_existed_before: bool, ts_ms: int, agent_id: str,
                         post_check: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The ``edit_outcome`` payload (schema in ``outcome_emit``'s docstring)."""
    return {
        "tool_name": tool_name,
        "file_path": file_path,
        "diff_size": int(diff_size),
        "file_existed_before": bool(file_existed_before),
        "ts_ms": int(ts_ms),
        "post_check": post_check,
        # W6-C2 F13: "" for the main chat, the subagent's id otherwise.
        "agent_id": agent_id,
    }


def emit_edit_outcome(row: Mapping[str, Any], emit: EmitFn) -> bool:
    """Send one row. ``row`` = ``{task_id, session_id, project_id, payload}``."""
    return bool(emit(
        event_type="edit_outcome",
        task_id=row["task_id"],
        task_type="edit_outcome",
        payload=row["payload"],
        session_id=row.get("session_id", ""),
        project_id=row.get("project_id"),
    ))


def no_checker_post_check() -> Dict[str, Any]:
    return {"status": "no_signal", "reason": "no_checker"}


def edit_reference_ms(file_path: str, hook_ts_ms: int) -> int:
    """When the edit landed: the file's mtime (the checker ran after it),
    capped at the hook's own stamp so a future-dated mtime cannot hide a
    real result. Unreadable → the hook stamp."""
    try:
        mtime_ms = os.stat(file_path).st_mtime_ns // 1_000_000
    except OSError:
        return int(hook_ts_ms)
    return int(min(mtime_ms, hook_ts_ms)) if hook_ts_ms else int(mtime_ms)


def await_post_check(*, project_root: str, file_path: str, session_id: str,
                     agent_id: str, checker: str, since_ms: int, window_s: int,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     poll_s: float = POLL_INTERVAL_S) -> Dict[str, Any]:
    """Wait (at most ``window_s``) for a status at or after ``since_ms``."""
    deadline = clock() + max(0, window_s)
    while True:
        st = read_status(project_root, file_path, session_id, agent_id)
        if st is not None and int(st.get("ts_ms", 0) or 0) >= since_ms:
            return {
                "status": "ok" if st.get("ok") else "fail",
                "checker": st.get("checker", checker),
                "exit_code": st.get("exit_code"),
                "latency_ms": max(0, int(st.get("ts_ms", 0)) - int(since_ms)),
            }
        remaining = deadline - clock()
        if remaining <= 0:
            return {"status": "no_signal", "reason": "timeout",
                    "checker": checker, "window_s": int(window_s)}
        sleep(min(poll_s, remaining))


# ── pending records, poller, sweeper ───────────────────────────────────────


def _load_pending(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("row"), dict):
        return None
    return data


def _emit_claimed(claimed: Path, record: Mapping[str, Any],
                  post_check: Optional[Dict[str, Any]], emit: EmitFn) -> bool:
    row = dict(record["row"])
    row["payload"] = dict(row.get("payload") or {}, post_check=post_check)
    try:
        return emit_edit_outcome(row, emit)
    except Exception:  # noqa: BLE001 — telemetry never raises into a hook
        return False
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass


def run_poller(pending: Path, *, emit: Optional[EmitFn] = None,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic,
               awaiter: Callable[..., Dict[str, Any]] = await_post_check) -> str:
    """The detached child's work. Returns what happened (for tests/logs):
    ``emitted`` / ``emitted_none`` / ``lost_claim`` / ``unreadable``."""
    from vco_lib.atomic import claim_by_rename  # noqa: PLC0415

    record = _load_pending(pending)
    if record is None:
        return "unreadable"
    try:
        post_check: Optional[Dict[str, Any]] = awaiter(
            project_root=record["project_root"], file_path=record["file_path"],
            session_id=record["session_id_raw"], agent_id=record["agent_id"],
            checker=record["checker"], since_ms=int(record["since_ms"]),
            window_s=int(record["window_s"]), sleep=sleep, clock=clock)
    except Exception:  # noqa: BLE001 — a failed wait still emits the row
        post_check = None
    claimed = claim_by_rename(pending, CLAIM_MARK)
    if claimed is None:
        return "lost_claim"  # the sweeper already emitted this row
    _emit_claimed(claimed, record, post_check, emit or _default_emit())
    try:
        sweep_stale_pending(str(record["project_root"]), emit or _default_emit())
        prune_old_status(str(record["project_root"]))
    except Exception:  # noqa: BLE001
        pass
    return "emitted" if post_check is not None else "emitted_none"


def sweep_stale_pending(project_root: str, emit: EmitFn, *,
                        now_ms: Optional[int] = None,
                        limit: Optional[int] = None) -> int:
    """Emit (``post_check: None``) every pending row whose poller is gone:
    unclaimed and older than its window + :data:`SWEEP_GRACE_S`, or claimed
    by a dead owner for longer than :data:`CLAIM_STALE_S`. Returns the
    count. Never raises."""
    from vco_lib.atomic import claim_by_rename, claim_owner_gone  # noqa: PLC0415

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        entries = sorted(pending_dir(project_root).iterdir())
    except OSError:
        return 0
    done = 0
    for entry in entries:
        if limit is not None and done >= limit:
            break
        name = entry.name
        try:
            if CLAIM_MARK in name:
                pid_text = name.rsplit(CLAIM_MARK, 1)[1]
                pid = int(pid_text) if pid_text.isdigit() else 0
                age_s = max(0.0, now / 1000.0 - entry.stat().st_mtime)
                if not claim_owner_gone(pid, age_s, stale_after_s=CLAIM_STALE_S):
                    continue
                record = _load_pending(entry)
                claimed = claim_by_rename(entry, CLAIM_MARK)
            elif name.endswith(".json"):
                record = _load_pending(entry)
                if record is not None:
                    due = (int(record.get("created_ms", 0))
                           + (int(record.get("window_s", DEFAULT_WINDOW_S)) + SWEEP_GRACE_S) * 1000)
                    if now < due:
                        continue
                claimed = claim_by_rename(entry, CLAIM_MARK)
            else:
                continue
        except OSError:
            continue
        if claimed is None:
            continue
        if record is None:
            # Unreadable: nothing to emit; keep it out of every later sweep.
            try:
                claimed.unlink()
            except OSError:
                pass
            continue
        _emit_claimed(claimed, record, None, emit)
        done += 1
    return done


def prune_old_status(project_root: str, *, now_s: Optional[float] = None) -> int:
    """Delete status records older than :data:`STATUS_TTL_S`. Only files
    with this module's prefix are touched. Returns the count; never raises."""
    now = time.time() if now_s is None else now_s
    removed = 0
    try:
        candidates = list(state_dir(project_root).glob(f"{STATUS_PREFIX}*.json"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if now - path.stat().st_mtime > STATUS_TTL_S:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _default_emit() -> EmitFn:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event  # noqa: PLC0415

    return emit_outcome_event


def _spawn_poller(pending: Path, project_root: str) -> None:  # noqa: ARG001
    """Start the detached poller. Raises on failure (caller emits inline)."""
    from vco_lib.install_companions import detached_popen_kwargs  # noqa: PLC0415

    env = dict(os.environ)
    # The child imports vco_lib + claude_mcp_servers exactly as this process
    # did; append (never prepend) this checkout so an explicit PYTHONPATH
    # keeps its precedence.
    root = str(Path(__file__).resolve().parent.parent)
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if root not in parts:
        parts.append(root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # cwd = this checkout, never the project: ``-m`` puts the cwd first on
    # sys.path, and a project folder holding its own ``vco_lib`` or
    # ``claude_mcp_servers`` would shadow ours. The project root reaches the
    # child through the pending record and the KG_BASE_DIR pin in ``env``.
    subprocess.Popen(  # noqa: S603 — fixed argv: this interpreter + this module
        [sys.executable, "-m", "vco_lib.edit_post_check", "poll", str(pending)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, env=env, cwd=root,
        **detached_popen_kwargs(),
    )


def record_edit_outcome(*, project_root: str, session_id: str, agent_id: str,
                        tool_name: str, file_path: str, diff_size: int,
                        file_existed_before: bool, ts_ms: int, emit: EmitFn,
                        project_id: Optional[str] = None,
                        spawn: Optional[Callable[[Path, str], None]] = None,
                        env: Optional[Mapping[str, str]] = None) -> str:
    """The hook's entry point. Returns ``inline`` (no checker: emitted now,
    no_signal), ``detached`` (a poller owns the row) or ``inline_fallback``
    (the poller could not start: emitted now with ``post_check: None``)."""
    try:
        sweep_stale_pending(project_root, emit, limit=SWEEP_LIMIT_HOOK)
    except Exception:  # noqa: BLE001
        pass
    task_id = f"edit_outcome_{uuid.uuid4().hex[:8]}"
    row: Dict[str, Any] = {"task_id": task_id, "session_id": session_id,
                           "project_id": project_id}

    def _payload(post_check: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return edit_outcome_payload(tool_name=tool_name, file_path=file_path,
                                    diff_size=diff_size,
                                    file_existed_before=file_existed_before,
                                    ts_ms=ts_ms, agent_id=agent_id,
                                    post_check=post_check)

    checker = covering_checker(tool_name, file_path, project_root)
    if checker is None:
        row["payload"] = _payload(no_checker_post_check())
        emit_edit_outcome(row, emit)
        return "inline"

    row["payload"] = _payload(None)
    record = {
        "v": 1, "row": row, "project_root": project_root,
        "file_path": file_path, "session_id_raw": session_id,
        "agent_id": agent_id, "checker": checker,
        "since_ms": edit_reference_ms(file_path, ts_ms),
        "window_s": window_secs(env), "created_ms": int(time.time() * 1000),
    }
    pending = pending_dir(project_root) / f"{task_id}.json"
    try:
        from vco_lib.atomic import atomic_write_json  # noqa: PLC0415

        pending.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(pending, record, indent=None, fsync=False, mode=0o600)
    except Exception:  # noqa: BLE001 — no pending record: emit now
        emit_edit_outcome(row, emit)
        return "inline_fallback"
    try:
        (spawn or _spawn_poller)(pending, project_root)
        return "detached"
    except Exception:  # noqa: BLE001 — no poller: emit now, exactly once
        from vco_lib.atomic import claim_by_rename  # noqa: PLC0415

        claimed = claim_by_rename(pending, CLAIM_MARK)
        if claimed is not None:
            _emit_claimed(claimed, record, None, emit)
        return "inline_fallback"


def record_edit_outcome_from_argv(argv: Sequence[str], *, project_root: str,
                                  emit: EmitFn, project_id: Optional[str] = None) -> str:
    """The hook siblings pass their fields as ``key=value`` argv items
    (never interpolated into Python source, and never an empty argv item,
    which Windows PowerShell 5.1 drops)."""
    fields: Dict[str, str] = {}
    for item in argv:
        key, sep, value = item.partition("=")
        if sep:
            fields[key] = value

    def _int(key: str) -> int:
        try:
            return int(fields.get(key, "0") or 0)
        except ValueError:
            return 0

    return record_edit_outcome(
        project_root=project_root, session_id=fields.get("session", "") or "default",
        agent_id=fields.get("agent", ""), tool_name=fields.get("tool", ""),
        file_path=fields.get("file", ""), diff_size=_int("diff"),
        file_existed_before=bool(_int("existed")), ts_ms=_int("ts"), emit=emit,
        project_id=project_id)


def build_parser() -> argparse.ArgumentParser:
    """The module's CLI (pinned by tests/test_v0297_vco_lib_argv_contract.py)."""
    parser = argparse.ArgumentParser(prog="python -m vco_lib.edit_post_check")
    sub = parser.add_subparsers(dest="cmd", required=True)
    poll = sub.add_parser("poll", help="detached poller: fill post_check for one pending row")
    poll.add_argument("pending", help="path of the pending row record (.json)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        run_poller(Path(args.pending))
    except Exception:  # noqa: BLE001 — detached child: nothing to tell
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
