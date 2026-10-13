# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Deferred citation pending-queue — stage / read / accumulate / delete / collect.

F-QUEUE (v0.2.70, maintainer ruled NO DEFER): hook-path citations are recovered
by persisting the staged retrieval ctx to disk at retrieval time, then a Stop
hook drains it at turn-end. This module owns the pending-file lifecycle — ONE
home, imported by both the staging side (``rl_kg_search.py`` + the MCP
``_populate_citation_cache``) and the drain side (``rl_drain_citations.py``).

Reuses the proven pre-bash/post-bash pairing-file pattern (a JSON file under
``.claude/state/``), so it is launcher.db-free, hub-free, and survives process
death. NO new hub endpoint, NO new launcher.db table (per the queue design).

Pending file path::

    <project_root>/.claude/state/rl_pending/<session>__<task_id>.json

⚠️ ACCUMULATE-DON'T-DROP (maintainer ruling): the pending file is a true
ACCUMULATOR. At turn-end, if the cumulative answer window is still < the token
threshold the drain LEAVES the file for the next Stop (``delete_pending`` is NOT
called) so it keeps accumulating into subsequent turns. Compute+write+delete
happen only at a genuine threshold or when the OWNING chat has really ended.

v0.2.102 W6-RL2 (owner rulings R7-3 / R7-4): there is NO scoring deadline. A
pending citation waits for its own chat's end events — the main chat's Stop
drains and its SessionEnd drain, a subagent's SubagentStop drain — however
long that chat runs; the final-window floors apply only once that chat has
ended (SubagentStop fired, SessionEnd fired and was recorded with
:func:`mark_session_ended`). It is scored only against the transcript of the
chat that produced it. The one remaining time bound is a garbage collector
for PROVABLY abandoned files (:func:`collect_abandoned`: its transcript idle
for :func:`abandoned_after_seconds`, default 7 days, and no live owner),
which scores a file before removing it, or records it as unscored.
"""

from __future__ import annotations

import glob
import json
import os
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "pending_dir",
    "stage_pending",
    "read_pending",
    "list_pending_for_session",
    "delete_pending",
    "collect_abandoned",
    "abandoned_after_seconds",
    "claim_pending",
    "claim_task_pending",
    "release_claim",
    "finish_scored",
    "readopt_stale_claims",
    "adopt_brief_citations",
    "agent_transcript_for",
    "session_transcript_for",
    "mark_session_ended",
    "clear_session_ended",
    "ended_sessions",
    "session_ended_marker",
    "session_has_pending",
    "NO_SESSION_PREFIX",
    "record_unscored",
    "ABANDONED_AFTER_ENV",
    "ABANDONED_AFTER_DAYS_DEFAULT",
    "UNSCORED_LOG_REL",
    "CLAIM_MARK",
    "CTX_PENDING_PATH_KEY",
    "AGENT_WINDOW_FINAL_KEY",
]

# v0.2.102 W6-RL2 (owner ruling, R7-4): the ONLY time bound left on a pending
# file — a garbage collector, never a scoring deadline. A file is collected
# only when its chat's transcript has not been written for this long AND it
# has no live owner (:func:`collect_abandoned`). Agents have run for hours, so
# the bound is long; it is read through the one env reader
# (``vco_lib.env_values``). Replaces the 60-minute ``PENDING_TTL_SECONDS``.
ABANDONED_AFTER_ENV = "RL_CITATION_ABANDONED_DAYS"
ABANDONED_AFTER_DAYS_DEFAULT: float = 7.0

# Where a citation removed WITHOUT a score is recorded (relative to the
# project), one JSON row per file, so the loss is counted, not silent.
UNSCORED_LOG_REL = Path(".claude") / "logs" / "rl-citations-unscored.jsonl"

# v0.2.102 W6-RL2: ``<session>`` + this suffix, beside the pending files,
# records that the session's SessionEnd drain ran: the chat has really ended,
# so any drain may finalize its leftover files (final floors). Not a
# ``*.json`` name, so no pending-file glob ever sees it.
SESSION_ENDED_SUFFIX = ".session_ended"

# v0.2.102 W6-C2 fix round (review R4c-1): a drain CLAIMS a pending file
# before it computes it, by renaming ``<name>.json`` to
# ``<name>.json.draining.<pid>``. The rename is atomic, so of two drains that
# list the same file (the agent's SubagentStop drain and the parent's Stop
# drain, or two overlapping async Stop drains) exactly one owns it; the other
# no longer sees a ``*.json`` and moves on. Every leave path renames the claim
# back. A claim whose owner died is re-adopted (:func:`readopt_stale_claims`),
# so a crash mid-compute never loses the citation.
CLAIM_MARK = ".draining."

# v0.2.102 W6-FIN: the key under which the MCP populate path records, in the
# IN-MEMORY citation ctx only, the pending file it staged for the same task.
# The answer monitor hands it to :func:`claim_task_pending`, which can then
# tell "a drain consumed it" (staged, now gone: do not compute again) from
# "never staged" (compute, or the citation is lost). Never written to disk.
CTX_PENDING_PATH_KEY = "pending_path"

# A claim older than this is re-adopted when liveness cannot be decided, or
# when its pid is alive but provably REUSED (the process holding it started
# after the claim; review R5-4, :func:`vco_lib.atomic.claim_owner_gone`). A
# live holder that is merely slow keeps its claim: taking it over would let
# two drains score one citation. (Its own value: until W6-RL2 it was an alias
# of the retired 60-minute pending TTL, and it keeps that hour.)
CLAIM_STALE_SECONDS: float = 3600.0

# R7-1: how many passes :func:`finish_scored` makes at most while re-stages
# or standing-down claims keep appearing. Each pass needs a NEW file to
# appear, so the bound is a guard against a pathological loop, not a timer.
_FINISH_MAX_PASSES = 64

# v0.2.102 W6-R5FIX (review R5-9): a brief citation stamped with a subagent
# transcript while that agent may still be running carries this key set to
# ``False``; the parent drain then applies the main chat's accumulating gate
# rather than the final-window floors. Absent (or true) = the stamped
# transcript is a finished agent's final answer.
AGENT_WINDOW_FINAL_KEY = "agent_window_final"

# The file-name prefix of a retrieval staged without a session id (the MCP
# path: the server is not told which chat calls it). Such a file belongs to
# the chat whose transcript holds its query (the drain's attribution rule).
NO_SESSION_PREFIX = "nosession"

# Filesystem-safe key sanitiser. session_id / task_id come from Claude Code +
# our own uuid hex, but a defensive scrub keeps a stray path separator from
# escaping the pending dir.
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def _resolve_project_root() -> Optional[Path]:
    """``CLAUDE_PROJECT_DIR``, else the nearest VCO project above the cwd.

    The ONE resolver every ``.claude/state`` writer shares
    (:func:`vco_lib.project_root.resolve_project_root`). Before v0.2.102 this
    fell back to the BARE cwd, so ``kg-search`` run from a subfolder (no
    ``CLAUDE_PROJECT_DIR`` in a plain shell) staged
    ``<subfolder>/.claude/state/rl_pending/*.json`` that no drain ever
    visits. ``None`` when no project encloses the cwd.
    """
    from vco_lib.project_root import resolve_project_root  # noqa: PLC0415

    return resolve_project_root()


def pending_dir(project_root: "str | Path | None" = None) -> Optional[Path]:
    """Return ``<root>/.claude/state/rl_pending`` (created on demand).

    ``None`` when no ``project_root`` is given and none can be resolved: there
    is no directory the drain would ever read, so nothing is written (the
    conservative default — an orphan under the cwd is worse than no file).
    """
    root = Path(project_root) if project_root else _resolve_project_root()
    if root is None:
        logger.info(
            "citation_pending: no VCO project encloses %s and CLAUDE_PROJECT_DIR "
            "is unset; skipping the pending-queue write",
            _cwd_for_log(),
        )
        return None
    d = root / ".claude" / "state" / "rl_pending"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _cwd_for_log() -> str:
    try:
        return str(Path.cwd())
    except OSError:
        return "<unreadable cwd>"


def _safe(key: str) -> str:
    return _SAFE_KEY.sub("-", key or "")


def _pending_path(
    session_id: str, task_id: str, root: "str | Path | None"
) -> Optional[Path]:
    d = pending_dir(root)
    if d is None:
        return None
    fname = f"{_safe(session_id) or NO_SESSION_PREFIX}__{_safe(task_id)}.json"
    return d / fname


def stage_pending(
    *,
    session_id: str,
    task_id: str,
    seq: Optional[int],
    query: str,
    ctx: dict,
    source: str = "hook",
    project_root: "str | Path | None" = None,
    agent_id: Optional[str] = None,
) -> Optional[Path]:
    """Persist the staged citation ctx as a pending file. Returns the path
    (or None on soft-fail). Idempotent per (session_id, task_id): a re-stage
    overwrites. The ctx is the same dict shape as ``_rl_node_content_cache``.

    Adds the queue-routing fields the drain needs: ``session_id``, ``task_id``,
    ``seq`` (1-based KG-call counter, or None for the hook path which has no
    process-global seq), ``query`` snippet (for transcript matching),
    ``ts_ms`` (stage time: the timestamp anchor, and the collector's age
    when no transcript is found), ``source`` ("hook" | "mcp") and ``agent_id``.

    ``agent_id`` (v0.2.102 W6-C2, census F12): the subagent whose tool call
    staged this retrieval — ``""`` for the main chat. A subagent's hook
    events carry the PARENT's session_id and transcript_path, so without it
    the parent's Stop drain scored a lane's injection against the PARENT's
    answer. ``None`` reads ``VCT_AGENT_ID``, which the context router sets
    for a subagent's call (the hook-path KG leg runs in the router's
    process); the drain routes agent-tagged files to the agent's own
    transcript (``rl_drain_citations.drain_session``).
    """
    if agent_id is None:
        agent_id = os.environ.get("VCT_AGENT_ID", "")
    try:
        # One agent-id rule (vco_lib is a hard dependency here, exactly like
        # the atomic writer below — a failed import fails the stage).
        from vco_lib.hook_agent_key import sanitize_agent_id  # noqa: PLC0415

        agent_id = sanitize_agent_id(agent_id or "")
        payload = {
            "session_id": session_id or "",
            "task_id": task_id,
            "seq": seq,
            "query": (query or "")[:120],
            "ts_ms": int(time.time() * 1000),
            "source": source,
            "agent_id": agent_id,
            "ctx": ctx,
        }
        # W6-C2 fix round (review R4c-8): a retrieval staged for a subagent's
        # BRIEF (PreToolUse:Agent, the router's `agent` surface) carries the
        # Agent call's tool_use_id, so PostToolUse:Agent — the first event
        # that names the subagent — can re-tag it (adopt_brief_citations).
        brief_id = _safe_tool_use_id(os.environ.get("VCT_BRIEF_TOOL_USE_ID", ""))
        if brief_id:
            payload["brief_tool_use_id"] = brief_id
        path = _pending_path(session_id, task_id, project_root)
        if path is None:
            return None
        # v0.2.92: one home for tmp+os.replace. `vco_lib` is a hard
        # dependency of every MCP process (server.py imports it at
        # startup), so this import is loud, not a soft degrade.
        from vco_lib.atomic import atomic_write_json  # noqa: PLC0415

        atomic_write_json(path, payload, indent=None, fsync=False)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.debug("stage_pending: write failed for %s (%s)", task_id[:8], exc)
        return None


def read_pending(path: "str | Path") -> Optional[dict]:
    """Read one pending file. Returns the payload dict or None on soft-fail."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def list_pending_for_session(
    session_id: str, project_root: "str | Path | None" = None
) -> list[Path]:
    """All pending files for a session, oldest-first (by mtime).

    Matches the ``<session>__*`` prefix. When ``session_id`` is empty (the hook
    path could not resolve one) the drain still needs to see every orphan, so an
    empty session_id lists ALL pending files (the drain then matches each by its
    own ``session_id`` field + the transcript)."""
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return []
    safe = _safe(session_id)
    pattern = f"{safe}__*.json" if safe else "*.json"
    try:
        files = [p for p in d.glob(pattern) if p.suffix == ".json"]
    except OSError:
        return []
    return sorted(files, key=lambda p: _safe_mtime(p))


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def delete_pending(path: "str | Path") -> None:
    """One-shot delete after a successful (or definitively-terminal) compute.
    Soft-fail (already-gone / permission). Do NOT call this for a sub-threshold
    accumulation — the accumulate-don't-drop ruling keeps the file for the next
    Stop."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("delete_pending: unlink failed (%s)", exc)


def abandoned_after_seconds() -> float:
    """The garbage-collection bound, in seconds: ``RL_CITATION_ABANDONED_DAYS``
    (default 7 days; a non-positive or unparseable value keeps the default),
    read through the one env reader."""
    from vco_lib.env_values import env_float  # noqa: PLC0415

    return env_float(ABANDONED_AFTER_ENV, ABANDONED_AFTER_DAYS_DEFAULT, above=0.0) * 86400.0


def collect_abandoned(
    project_root: "str | Path | None" = None,
    *,
    idle_after_s: Optional[float] = None,
    now: Optional[float] = None,
    live_session: str = "",
    source_fn: "Optional[Callable[[dict], str]]" = None,
    score_fn: "Optional[Callable[[Path, dict, str], Optional[str]]]" = None,
    counts: Optional[dict] = None,
) -> int:
    """Garbage-collect PROVABLY abandoned pending files; returns the count
    removed. Not a scoring deadline (v0.2.102 W6-RL2, owner ruling R7-4).

    A file is abandoned only when BOTH hold:

    * its chat's transcript (``source_fn(payload)``: the recorded agent
      transcript, or its session's transcript in the same folder) has not
      been written for ``idle_after_s`` (default
      :func:`abandoned_after_seconds`, 7 days). With no locatable transcript
      the file's own stage time stands in;
    * it has no live owner: it is not claimed (a claim is invisible to the
      ``*.json`` listing), and it does not belong to ``live_session`` (the
      session whose drain is running, alive by definition).

    Each abandoned file is CLAIMED (:func:`claim_pending`, the one claim all
    scorers take), re-checked on the claimed payload (a re-stage or a fresh
    transcript write hands it back untouched), then scored with
    ``score_fn(claimed, payload, transcript)`` — the drain's one scoring path
    under the final floors, since nothing will be added to a transcript idle
    that long — which returns ``None`` when a citation was written, else a
    reason. Scored files finish through :func:`finish_scored`; an unscored
    one (no transcript, below every floor, compute failure, no scorer) is
    removed and recorded in :data:`UNSCORED_LOG_REL`. ``counts`` receives
    ``scored`` / ``unscored``. ``now`` is injectable for tests."""
    d = pending_dir(project_root)
    tally = counts if counts is not None else {}
    tally.setdefault("scored", 0)
    tally.setdefault("unscored", 0)
    if d is None or not d.exists():
        return 0
    idle = abandoned_after_seconds() if idle_after_s is None else float(idle_after_s)
    now_s = time.time() if now is None else float(now)
    live = _safe(live_session)
    removed = 0
    try:
        candidates = sorted(d.glob("*.json"))
    except OSError:
        return 0
    for p in candidates:
        if live and p.name.startswith(f"{live}__"):
            continue  # the running session's own file: it has a live owner
        before = read_pending(p)
        stage_mtime = _safe_mtime(p)
        transcript = _source_for(source_fn, before)
        if not _abandoned(p, before, transcript, now_s, idle):
            continue
        claimed = claim_pending(p)
        if claimed is None:
            continue  # a drain / the monitor holds it now: it has an owner
        payload = read_pending(claimed)
        transcript = _source_for(source_fn, payload)
        restaged = (isinstance(before, dict) and isinstance(payload, dict)
                    and before.get("ts_ms") != payload.get("ts_ms"))
        if restaged or not _abandoned(claimed, payload, transcript, now_s, idle,
                                      stage_mtime=stage_mtime):
            release_claim(claimed, p)  # re-staged, or its chat wrote meanwhile
            continue
        if not isinstance(payload, dict):
            reason: Optional[str] = "unreadable_payload"
        elif score_fn is None:
            reason = "no_scorer"
        elif not transcript:
            reason = "no_transcript"
        else:
            try:
                reason = score_fn(claimed, payload, transcript)
            except Exception as exc:  # noqa: BLE001 — one bad file never stops the pass
                reason = f"score_error:{type(exc).__name__}"
        removed += 1
        if reason is None:
            finish_scored(claimed, p)
            tally["scored"] += 1
        else:
            delete_pending(claimed)
            tally["unscored"] += 1
            record_unscored(d, p.name, payload, reason, when="abandoned")
    _collect_stale_markers(d, now_s, idle)
    return removed


def _source_for(source_fn: "Optional[Callable[[dict], str]]", payload: object) -> str:
    if source_fn is None or not isinstance(payload, dict):
        return ""
    try:
        return str(source_fn(payload) or "")
    except Exception:  # noqa: BLE001 — an unresolvable source is "no transcript"
        return ""


def _abandoned(
    p: Path, payload: object, transcript: str, now_s: float, idle: float,
    *, stage_mtime: Optional[float] = None,
) -> bool:
    """True when the file's chat has been silent for ``idle`` seconds: the
    transcript's mtime when it exists, else the stage time (``ts_ms``, else
    the file's mtime — ``stage_mtime`` for a claimed file, whose own mtime
    the claim refreshed)."""
    ref: Optional[float] = None
    if transcript:
        try:
            ref = Path(transcript).stat().st_mtime
        except OSError:
            ref = None
    if ref is None:
        ts = payload.get("ts_ms") if isinstance(payload, dict) else None
        if isinstance(ts, (int, float)) and ts > 0:
            ref = float(ts) / 1000.0
        else:
            ref = stage_mtime if stage_mtime is not None else _safe_mtime(p)
    return (now_s - ref) >= idle


def _collect_stale_markers(d: Path, now_s: float, idle: float) -> None:
    """Remove ended-session markers older than the bound whose session has no
    pending file or claim left (the marker has nothing more to say)."""
    try:
        markers = list(d.glob(f"*{SESSION_ENDED_SUFFIX}"))
    except OSError:
        return
    for m in markers:
        sid = m.name[: -len(SESSION_ENDED_SUFFIX)]
        if (now_s - _safe_mtime(m)) >= idle and not _session_has_files(d, sid):
            try:
                m.unlink()
            except OSError:
                pass


def record_unscored(
    pdir: Path, name: str, payload: object, reason: str, *, when: str,
) -> None:
    """One row per citation removed without a score in ``<project>/``
    :data:`UNSCORED_LOG_REL` plus a WARNING log line. ``when`` names the
    removal: ``"session_ended"`` (finalized after its chat ended) or
    ``"abandoned"`` (garbage-collected). Never raises: a failed row only
    leaves the log line."""
    info = payload if isinstance(payload, dict) else {}
    row = {
        "removed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "when": when,
        "file": name,
        "reason": reason,
        "session_id": str(info.get("session_id") or ""),
        "task_id": str(info.get("task_id") or ""),
        "agent_id": str(info.get("agent_id") or ""),
        "source": str(info.get("source") or ""),
        "ts_ms": info.get("ts_ms"),
    }
    logger.warning(
        "citation_pending: unscored citation %s (%s, %s) -- the citation for task "
        "%s was removed without a score", name, when, reason, row["task_id"][:16])
    try:
        log = pdir.parent.parent.parent / UNSCORED_LOG_REL
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("citation_pending: could not record the unscored citation (%s)", exc)


# ── claims (v0.2.102 W6-C2 fix round, review R4c-1) ───────────────────────


def claim_pending(path: "str | Path") -> Optional[Path]:
    """Take ``path`` for this process (``<name>.json.draining.<pid>``, the
    one rename-claim primitive :func:`vco_lib.atomic.claim_by_rename`).
    Returns the claimed path, or ``None`` when another process took (or
    deleted) it first. Never raises.

    v0.2.102 W6-RL2 (review R7-1): a claim taken while ANOTHER claim of the
    same name exists stands down (handed back, ``None``). That other claim
    holds an earlier stage of the same task — a re-stage landed while it was
    being scored — and :func:`finish_scored` consumes the re-stage once that
    score lands, so one retrieval never yields two citation events. Of two
    claims of one name, at most one passes this check: each is created
    before its own check, so the later one always sees the earlier."""
    from vco_lib.atomic import claim_by_rename  # noqa: PLC0415

    src = Path(path)
    claimed = claim_by_rename(src, CLAIM_MARK)
    if claimed is None:
        return None
    if _other_claims(src, claimed):
        release_claim(claimed, src)
        return None
    return claimed


def _other_claims(path: Path, own: Path) -> "list[Path]":
    try:
        return [c for c in path.parent.glob(glob.escape(path.name) + CLAIM_MARK + "*")
                if c != own]
    except OSError:
        return []


def finish_scored(claimed: "str | Path", path: "str | Path") -> int:
    """Consume a claim whose citation was WRITTEN — the one finish rule of
    every scorer (Stop / SubagentStop / SessionEnd drains, the collector,
    the MCP answer monitor; v0.2.102 W6-RL2, review R7-1).

    A re-stage of the same task that landed while the score was computing
    is the same retrieval (re-stages overwrite by contract), and its event
    was just written. So before the claim is deleted, every newer stage is
    consumed too, while the claim still stands (any scorer that tries to
    take the re-stage meanwhile stands down, :func:`claim_pending`): a
    re-staged ``<name>.json`` is taken with the same rename-claim primitive
    (:func:`vco_lib.atomic.claim_by_rename` — from this process it folds
    into this claim, atomically), and a standing-down claim of the same
    name is removed (its hand-back then finds nothing, harmlessly). Passes
    repeat until one finds nothing new. A stage that lands after the last
    pass, in the instant before the claim is deleted, is a genuinely later
    stage and is scored on its own. Returns the count consumed. Never
    raises."""
    from vco_lib.atomic import claim_by_rename  # noqa: PLC0415

    src, own = Path(path), Path(claimed)
    consumed = 0
    for _ in range(_FINISH_MAX_PASSES):
        found = False
        for other in _other_claims(src, own):
            found = True
            try:
                other.unlink()
                consumed += 1
            except OSError:
                pass  # it was just handed back: the next pass sees <name>.json
        if src.exists():
            found = True
            if claim_by_rename(src, CLAIM_MARK) is not None:
                consumed += 1
        if not found:
            break
    delete_pending(own)
    if consumed:
        logger.info(
            "citation_pending: %s was re-staged while it was scored; the "
            "re-stage was consumed (one citation event)", src.name)
    return consumed


def release_claim(claimed: "str | Path", path: "str | Path") -> None:
    """Hand a claimed file back under its pending name. When a file of that
    name appeared meanwhile (a re-stage of the same task, which by contract
    overwrites), the newer stage wins and the claim is dropped. On a failed
    rename the claim stays on disk, where :func:`readopt_stale_claims` finds
    it once this process is gone. Never raises."""
    from vco_lib.atomic import release_rename_claim  # noqa: PLC0415

    if not release_rename_claim(Path(claimed), Path(path)):
        logger.debug("release_claim: could not hand back %s", Path(claimed).name)


def claim_task_pending(
    task_id: str,
    *,
    staged_path: "str | Path | None" = None,
    project_root: "str | Path | None" = None,
) -> "tuple[str, Optional[Path], Optional[Path]]":
    """Claim THIS task's pending file before an in-process compute.

    The MCP answer monitor's entry to the SAME claim the Stop-hook drains
    take (:func:`claim_pending`), so a drain running while the monitor
    computes cannot score the citation a second time, and vice versa
    (v0.2.102 W6-FIN). Returns ``(state, claimed, pending_path)``:

    * ``"claimed"`` — this process now owns ``claimed``; compute it, then
      :func:`delete_pending` it on a landed write or :func:`release_claim`
      it back to ``pending_path`` on EVERY other leave path.
    * ``"taken"`` — the file exists only as another process's claim, or a
      file that was staged is gone: a drain owns it or already scored it.
      Do NOT compute (that would be the second write). Nothing is lost: a
      drain that does not score it renames it back for the next drain.
    * ``"unstaged"`` — no pending file was ever staged for this task (the
      stage soft-failed, or no project root resolved). No drain can see the
      retrieval, so the caller computes it directly; skipping would lose it.

    ``staged_path`` is the path :func:`stage_pending` returned (the populate
    path records it in the cached ctx); it makes "staged, now gone" — a
    drain consumed it — distinguishable from "never staged". Without it the
    task's file is looked up by its ``__<task_id>.json`` suffix. Never raises.
    """
    if staged_path:
        path = Path(staged_path)
        claimed = claim_pending(path)
        return ("claimed", claimed, path) if claimed is not None else ("taken", None, path)
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return ("unstaged", None, None)
    suffix = f"__{_safe(task_id)}.json"
    try:
        names = [p for p in d.iterdir() if suffix in p.name]
    except OSError:
        return ("unstaged", None, None)
    for p in names:
        if p.name.endswith(suffix):
            claimed = claim_pending(p)
            if claimed is not None:
                return ("claimed", claimed, p)
            return ("taken", None, p)
    for p in names:
        if CLAIM_MARK in p.name and p.name.rpartition(CLAIM_MARK)[0].endswith(suffix):
            return ("taken", None, p.with_name(p.name.rpartition(CLAIM_MARK)[0]))
    return ("unstaged", None, None)


def readopt_stale_claims(
    project_root: "str | Path | None" = None,
    *,
    stale_after_s: float = CLAIM_STALE_SECONDS,
) -> int:
    """Rename back every claim whose owner is gone: its pid is dead, or the
    claim is older than ``stale_after_s`` and its pid is proven reused or
    its liveness unknown (:func:`vco_lib.atomic.claim_owner_gone`). A live
    holder's claim is left alone however old (review R5-4). Returns the
    count re-adopted."""
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return 0
    try:
        claims = list(d.glob(f"*.json{CLAIM_MARK}*"))
    except OSError:
        return 0
    from vco_lib.atomic import claim_owner_gone  # noqa: PLC0415 — the one staleness rule

    now = time.time()
    adopted = 0
    for c in claims:
        base_name, _, pid_txt = c.name.rpartition(CLAIM_MARK)
        try:
            pid = int(pid_txt)
        except ValueError:
            pid = -1
        mtime = _safe_mtime(c)
        if claim_owner_gone(pid, now - mtime if mtime else 0.0, stale_after_s=stale_after_s):
            release_claim(c, c.with_name(base_name))
            adopted += 1
    return adopted


# ── brief citations (v0.2.102 W6-C2 fix round, review R4c-8) ──────────────

_TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _safe_tool_use_id(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if _TOOL_USE_ID.fullmatch(raw) else ""


def agent_transcript_for(parent_transcript: str, agent_id: str) -> str:
    """Where Claude Code writes a subagent's transcript, beside the parent's:
    ``<parent without .jsonl>/subagents/agent-<id>.jsonl`` (the layout
    SubagentStop's ``agent_transcript_path`` shows — probe 2026-10-13).
    ``""`` when either input is missing or the file does not exist, so a
    caller never points a drain at a guessed path."""
    from vco_lib.hook_agent_key import sanitize_agent_id  # noqa: PLC0415

    agent = sanitize_agent_id(agent_id or "")
    if not parent_transcript or not agent or not parent_transcript.endswith(".jsonl"):
        return ""
    cand = Path(parent_transcript[: -len(".jsonl")]) / "subagents" / f"agent-{agent}.jsonl"
    try:
        return str(cand) if cand.is_file() else ""
    except OSError:
        return ""


def session_transcript_for(known_transcript: str, session_id: str) -> str:
    """The main-chat transcript of ``session_id``, found beside a transcript
    this process already knows: ``<same folder>/<session_id>.jsonl``.

    Claude Code keeps every session of a project as
    ``<project transcripts dir>/<session_id>.jsonl`` and a subagent's beside
    its parent's (:func:`agent_transcript_for`), so from either kind of
    ``known_transcript`` the session file is a sibling of the parent. ONLY
    that folder is looked at: a VCO chat is always started in the project's
    root, so its sessions share one transcripts folder, and a citation is
    scored against its OWN chat or not at all (owner ruling R7-3 — the
    cross-folder search the R6-2 coordinator commit added is gone). An empty
    ``session_id`` means the known transcript's own session. ``""`` when the
    inputs are missing, the id is not filesystem-safe, or the file does not
    exist — a caller never points a compute at a guessed path; it leaves the
    citation unscored and logs it."""
    if not known_transcript or not known_transcript.endswith(".jsonl"):
        return ""
    known = Path(known_transcript)
    if known.parent.name == "subagents":
        known = Path(str(known.parent.parent) + ".jsonl")
    if session_id:
        if _safe(session_id) != session_id:
            return ""
        known = known.with_name(f"{session_id}.jsonl")
    try:
        return str(known) if known.is_file() else ""
    except OSError:
        return ""


# ── chat end records (v0.2.102 W6-RL2, owner ruling R7-4) ──────────────────


def session_ended_marker(
    session_id: str, project_root: "str | Path | None" = None,
) -> Optional[Path]:
    """``<pending dir>/<session>.session_ended``, or ``None`` without a
    session id or a pending dir."""
    safe = _safe(session_id)
    d = pending_dir(project_root) if safe else None
    return d / f"{safe}{SESSION_ENDED_SUFFIX}" if d is not None else None


def mark_session_ended(session_id: str, project_root: "str | Path | None" = None) -> bool:
    """Record that ``session_id`` ended (its SessionEnd drain runs). Written
    FIRST by that drain, so a drain that could not finish still leaves the
    record: the next drain of any session then finalizes the leftovers.
    Returns whether the record exists. Never raises."""
    m = session_ended_marker(session_id, project_root)
    if m is None:
        return False
    try:
        m.touch()
        return True
    except OSError as exc:
        logger.info("citation_pending: could not record the end of %s (%s)",
                    session_id[:16], exc)
        return False


def clear_session_ended(
    session_id: str,
    project_root: "str | Path | None" = None,
    *,
    older_than: Optional[float] = None,
) -> None:
    """Drop the end record of a session that is live again (a resumed chat's
    Stop drain). ``older_than`` (epoch seconds): only a record written before
    then — a record newer than the calling drain's own start belongs to a
    SessionEnd that followed this turn, and stands. Never raises."""
    m = session_ended_marker(session_id, project_root)
    if m is None:
        return
    try:
        if older_than is not None and m.stat().st_mtime >= older_than:
            return
        m.unlink()
    except OSError:
        pass


def ended_sessions(project_root: "str | Path | None" = None) -> "list[str]":
    """The (filesystem-safe) ids of every session with an end record."""
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return []
    try:
        return sorted(m.name[: -len(SESSION_ENDED_SUFFIX)]
                      for m in d.glob(f"*{SESSION_ENDED_SUFFIX}"))
    except OSError:
        return []


def session_has_pending(session_id: str, project_root: "str | Path | None" = None) -> bool:
    """Whether ``session_id`` still has a pending file or claim. ``True`` when
    that cannot be told (keep its end record rather than lose it)."""
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return False
    return _session_has_files(d, _safe(session_id))


def _session_has_files(d: Path, safe_sid: str) -> bool:
    try:
        return any(d.glob(f"{glob.escape(safe_sid)}__*.json*"))
    except OSError:
        return True


def adopt_brief_citations(
    session_id: str,
    tool_use_id: str,
    agent_id: str,
    *,
    agent_finished: bool,
    parent_transcript: str = "",
    project_root: "str | Path | None" = None,
) -> int:
    """Re-tag the citations staged for one Agent call's BRIEF with the
    subagent that received it.

    The KG block injected into a subagent's prompt is retrieved at
    PreToolUse:Agent, by the CALLER (the main chat: ``agent_id`` ""), before
    the subagent exists — so it was staged as a main-chat retrieval and
    scored against the parent's answer. PostToolUse:Agent names the subagent
    (``tool_response.agentId``); this hands its brief citations over:

    * ``agent_finished`` (a FOREGROUND agent: PostToolUse:Agent fires after
      its SubagentStop, so its own drain has already run) — tagged AND
      stamped with its transcript (:func:`agent_transcript_for`, beside
      ``parent_transcript``), so the parent's next Stop drain scores them
      against the subagent's final answer;
    * not finished (a BACKGROUND launch: PostToolUse:Agent fires at launch)
      — tagged; the agent's own SubagentStop drain scores them. When the
      agent's transcript already exists and is non-empty (a very fast agent
      can finish — and run its one SubagentStop drain — before this
      detached adopt job lands; review R5-9) it is ALSO stamped, with
      ``agent_window_final: false``: a parent Stop drain then scores it
      against the subagent's answer under the main chat's
      accumulate-don't-drop rules (the 25k gate) instead of the
      final-window floors, because the agent may still be running; the
      final floors apply once the session has ended. Without the stamp a
      tagged file whose agent drain already ran had no transcript and could
      never be scored. The agent's own drain, when it comes, clears the
      flag (its window is final).

    A finished agent whose transcript cannot be located re-tags nothing:
    a tagged file without a transcript could never be scored, and the
    parent's answer is a better label than none. Files are claimed
    while rewritten, so a concurrent drain never sees a half state.
    Returns the count re-tagged. Never raises."""
    from vco_lib.hook_agent_key import sanitize_agent_id  # noqa: PLC0415

    tuid = _safe_tool_use_id(tool_use_id)
    agent = sanitize_agent_id(agent_id or "")
    if not tuid or not agent:
        return 0
    agent_transcript_path = ""
    window_final = True
    if agent_finished:
        agent_transcript_path = agent_transcript_for(parent_transcript, agent)
        if not agent_transcript_path:
            return 0
    else:
        cand = agent_transcript_for(parent_transcript, agent)
        try:
            if cand and Path(cand).stat().st_size > 0:
                agent_transcript_path, window_final = cand, False
        except OSError:
            pass
    adopted = 0
    for path in list_pending_for_session(session_id, project_root):
        payload = read_pending(path)
        if not isinstance(payload, dict) or payload.get("brief_tool_use_id") != tuid:
            continue
        if payload.get("agent_id"):
            continue  # a subagent's own retrieval, or already adopted
        claimed = claim_pending(path)
        if claimed is None:
            continue
        try:
            payload = read_pending(claimed)
            if isinstance(payload, dict) and not payload.get("agent_id"):
                payload["agent_id"] = agent
                if agent_transcript_path:
                    payload["agent_transcript_path"] = agent_transcript_path
                    if not window_final:
                        payload[AGENT_WINDOW_FINAL_KEY] = False
                from vco_lib.atomic import atomic_write_json  # noqa: PLC0415

                atomic_write_json(claimed, payload, indent=None, fsync=False)
                adopted += 1
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the hook
            logger.debug("adopt_brief_citations: %s (%s)", path.name, exc)
        finally:
            release_claim(claimed, path)
    return adopted
