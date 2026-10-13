# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Deferred citation pending-queue — stage / read / accumulate / delete + TTL.

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
happen only at a genuine threshold or a true terminal (session-end / compaction
force-flush) or TTL expiry.
"""

from __future__ import annotations

import json
import os
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "pending_dir",
    "stage_pending",
    "read_pending",
    "list_pending_for_session",
    "delete_pending",
    "sweep_expired",
    "claim_pending",
    "claim_task_pending",
    "release_claim",
    "readopt_stale_claims",
    "adopt_brief_citations",
    "agent_transcript_for",
    "PENDING_TTL_SECONDS",
    "CLAIM_MARK",
    "CTX_PENDING_PATH_KEY",
]

# Abandoned-session TTL: a pending file with no turn-end (session killed) is
# garbage after this. Mirrors _RL_MONITOR_TIMEOUT (60 min).
PENDING_TTL_SECONDS: float = 3600.0

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

# A claim older than this is re-adopted even when its pid looks alive (the
# pid was reused) or liveness cannot be decided. Generous on purpose: one
# compute is a handful of embedding calls, and taking a LIVE claim over
# would let two drains score one citation.
CLAIM_STALE_SECONDS: float = PENDING_TTL_SECONDS

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
    ``<subfolder>/.claude/state/rl_pending/*.json`` that no drain or TTL
    sweep ever visits. ``None`` when no project encloses the cwd.
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
    fname = f"{_safe(session_id) or 'nosession'}__{_safe(task_id)}.json"
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
    ``ts_ms`` (TTL), ``source`` ("hook" | "mcp") and ``agent_id``.

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


def sweep_expired(
    project_root: "str | Path | None" = None,
    ttl_seconds: float = PENDING_TTL_SECONDS,
    *,
    now_ms: Optional[int] = None,
) -> int:
    """Delete pending files whose ``ts_ms`` is older than ``ttl_seconds``.
    Returns the count deleted. Abandoned-session GC — cheap mtime+payload
    sweep. ``now_ms`` is injectable for tests."""
    d = pending_dir(project_root)
    if d is None or not d.exists():
        return 0
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = now - int(ttl_seconds * 1000)
    deleted = 0
    try:
        candidates = list(d.glob("*.json"))
    except OSError:
        return 0
    for p in candidates:
        payload = read_pending(p)
        ts = None
        if isinstance(payload, dict):
            ts = payload.get("ts_ms")
        # Fall back to mtime when the payload is unreadable / ts missing.
        if not isinstance(ts, (int, float)):
            ts = int(_safe_mtime(p) * 1000)
        if ts < cutoff:
            delete_pending(p)
            deleted += 1
    return deleted


# ── claims (v0.2.102 W6-C2 fix round, review R4c-1) ───────────────────────


def claim_pending(path: "str | Path") -> Optional[Path]:
    """Take ``path`` for this process (``<name>.json.draining.<pid>``, the
    one rename-claim primitive :func:`vco_lib.atomic.claim_by_rename`).
    Returns the claimed path, or ``None`` when another process took (or
    deleted) it first. Never raises."""
    from vco_lib.atomic import claim_by_rename  # noqa: PLC0415

    return claim_by_rename(Path(path), CLAIM_MARK)


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
    claim is older than ``stale_after_s`` (pid reused / liveness unknown).
    A live, young claim is left alone. Returns the count re-adopted."""
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
      — tagged only; the agent's own SubagentStop drain scores them.

    A finished agent whose transcript cannot be located re-tags nothing:
    a tagged file without a transcript would be left to the TTL sweep, and
    the parent's answer is a better label than none. Files are claimed
    while rewritten, so a concurrent drain never sees a half state.
    Returns the count re-tagged. Never raises."""
    from vco_lib.hook_agent_key import sanitize_agent_id  # noqa: PLC0415

    tuid = _safe_tool_use_id(tool_use_id)
    agent = sanitize_agent_id(agent_id or "")
    if not tuid or not agent:
        return 0
    agent_transcript_path = ""
    if agent_finished:
        agent_transcript_path = agent_transcript_for(parent_transcript, agent)
        if not agent_transcript_path:
            return 0
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
                from vco_lib.atomic import atomic_write_json  # noqa: PLC0415

                atomic_write_json(claimed, payload, indent=None, fsync=False)
                adopted += 1
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the hook
            logger.debug("adopt_brief_citations: %s (%s)", path.name, exc)
        finally:
            release_claim(claimed, path)
    return adopted
