# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL-5 (v0.2.73) — bounded retention / GC for the ``rl_events`` store.

Background
----------
Pre-v0.2.47 the RL corpus lived in a flat JSONL file
(``~/.claude/retrieval_rl_data/rl_events.jsonl``) that hit **700 MB** in
practice — every retrieval event carries per-node 1024-dim embeddings and the
v0.2.71 dual-log fan-out *doubles* the row rate (a second slot-suffixed event
per retrieval). v0.2.47 moved the corpus into launcher.db's ``rl_events`` table
(queryable, indexed) but the table is **append-only**: ``insert_rl_event`` only
ever appends, and there is no prune path. So the same unbounded-growth hazard
migrated from a JSONL file to a SQLite table (worse in one respect — SQLite
never returns freed pages to the OS without a VACUUM).

This module is the **Python-side retention DRIVER**. The actual row-deletion is
performed hub-side (single-writer rule: Python never opens launcher.db
directly), so the driver:

  1. Decides *whether* a prune is due (cadence throttle — we do not prune on
     every event, that would hammer the hub) and *what* the cutoff should be
     (age-based OR row-count-based, whichever bound is configured, both applied
     when both are set).
  2. Computes the **in-flight-citation protection floor** so a retrieval event
     whose citation has not yet been drained is NEVER pruned out from under an
     in-flight citation-drain (RL-4's terminal-session floor keeps pending
     files alive for up to an hour; a retention pass that deleted the paired
     retrieval event would orphan the citation and corrupt the (retrieval,
     citation) training pair).
  3. Calls the hub prune route (``hub_writer.post_rl_prune``) with the resolved
     cutoff. Soft-fail throughout: a down hub, a missing prune route (older
     hub binary), or any transport error leaves the corpus untouched and the
     search path unaffected.

Boundary note (v0.2.73 W2-F / W2-B split)
-----------------------------------------
The hub-side DELETE route + the launcher-core ``prune_rl_events`` DB method are
Rust and owned by the launcher/hub track (W2-B). They are delivered as a patch
spec (see the W2-F report). This module talks to the route through
``hub_writer.post_rl_prune`` and degrades gracefully (returns a skipped result)
when the route is absent — so it is safe to merge BEFORE the hub side lands.

Config (env, all optional — sane defaults when unset)
----------------------------------------------------
  * ``RL_EVENTS_RETENTION_MAX_AGE_DAYS``  — delete events older than N days.
    Default ``90``. ``0`` / negative disables the age bound.
  * ``RL_EVENTS_RETENTION_MAX_ROWS``      — keep at most N most-recent rows.
    Default ``0`` (row-count bound disabled; age is the primary bound). When
    set, the hub keeps the newest N and deletes the rest.
  * ``RL_EVENTS_RETENTION_DISABLED``      — truthy → never prune (opt-out for
    users who want the full corpus, e.g. offline-training operators).
  * ``RL_EVENTS_RETENTION_MIN_INTERVAL_S`` — minimum seconds between prune
    passes for one process. Default ``3600`` (hourly). Throttle only; a fresh
    process always allows the first pass.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── defaults ────────────────────────────────────────────────────────────
_DEFAULT_MAX_AGE_DAYS = 90
_DEFAULT_MAX_ROWS = 0  # 0 = disabled (age is the primary bound)
_DEFAULT_MIN_INTERVAL_S = 3600.0  # hourly cadence throttle

# The in-flight-citation protection window. A retrieval event younger than this
# may still have an un-drained citation (RL-4 keeps pending files alive up to
# PENDING_TTL_SECONDS = 3600s; we add generous headroom so a retention cutoff
# never races the drain). The age bound is clamped so the cutoff is NEVER more
# recent than (now - this floor), regardless of how small MAX_AGE_DAYS is set.
_INFLIGHT_PROTECT_SECONDS = 6 * 3600.0  # 6h — 6× the pending TTL, generous.

_ENV_MAX_AGE_DAYS = "RL_EVENTS_RETENTION_MAX_AGE_DAYS"
_ENV_MAX_ROWS = "RL_EVENTS_RETENTION_MAX_ROWS"
_ENV_DISABLED = "RL_EVENTS_RETENTION_DISABLED"
_ENV_MIN_INTERVAL_S = "RL_EVENTS_RETENTION_MIN_INTERVAL_S"

# Per-process throttle state. Module-level so a long-lived MCP subprocess only
# prunes on the configured cadence, not on every event write.
_last_prune_ts: float = 0.0


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def retention_disabled() -> bool:
    """True iff the user opted out of retention entirely."""
    return _truthy(os.environ.get(_ENV_DISABLED))


class RetentionPlan:
    """Resolved prune parameters for one pass. Pure data — no I/O."""

    __slots__ = ("cutoff_ms", "max_rows", "reason")

    def __init__(self, cutoff_ms: Optional[int], max_rows: Optional[int], reason: str) -> None:
        # Delete events with ts_ms < cutoff_ms (age bound). None → no age bound.
        self.cutoff_ms = cutoff_ms
        # Keep at most this many most-recent rows (row-count bound). None/0 →
        # no row-count bound.
        self.max_rows = max_rows
        # Human-readable why (for logs / rl-doctor). Never carries user data.
        self.reason = reason

    def is_noop(self) -> bool:
        return self.cutoff_ms is None and not self.max_rows


def compute_retention_plan(now_ms: Optional[int] = None) -> RetentionPlan:
    """Resolve the age + row-count cutoffs from env, applying the in-flight floor.

    The age cutoff is CLAMPED so it is never more recent than
    ``now - _INFLIGHT_PROTECT_SECONDS`` — even if the user sets
    ``RL_EVENTS_RETENTION_MAX_AGE_DAYS=0.001`` the retention pass will not delete
    an event that could still have an un-drained citation. ``now_ms`` is
    injectable for tests.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)

    max_age_days = _env_int(_ENV_MAX_AGE_DAYS, _DEFAULT_MAX_AGE_DAYS)
    max_rows = _env_int(_ENV_MAX_ROWS, _DEFAULT_MAX_ROWS)

    cutoff_ms: Optional[int] = None
    reasons = []
    if max_age_days > 0:
        cutoff_ms = now - max_age_days * 86_400_000
        reasons.append(f"age>{max_age_days}d")

    # In-flight protection floor: never let the cutoff cross into the window
    # where a citation might still be pending. This bounds the age cutoff from
    # ABOVE (more recent) — a too-aggressive age setting is silently made safe.
    inflight_floor_ms = now - int(_INFLIGHT_PROTECT_SECONDS * 1000)
    if cutoff_ms is not None and cutoff_ms > inflight_floor_ms:
        cutoff_ms = inflight_floor_ms
        reasons.append("clamped-to-inflight-floor")

    row_bound: Optional[int] = max_rows if max_rows and max_rows > 0 else None
    if row_bound is not None:
        reasons.append(f"rows>{row_bound}")

    return RetentionPlan(
        cutoff_ms=cutoff_ms,
        max_rows=row_bound,
        reason=",".join(reasons) or "noop",
    )


def _throttle_ready(min_interval_s: float, now: float) -> bool:
    """True iff enough time has passed since the last prune in this process."""
    global _last_prune_ts
    if _last_prune_ts <= 0.0:
        return True  # first pass in this process always allowed
    return (now - _last_prune_ts) >= min_interval_s


def maybe_run_retention(
    *,
    project_id: Optional[str] = None,
    force: bool = False,
    now_ms: Optional[int] = None,
    prune_fn=None,
) -> dict:
    """Run one retention pass if due. Soft-fail. Returns a small status dict.

    The status dict is JSON-safe and carries NO user data (only counts +
    tags): ``{"ran": bool, "skipped": str|None, "deleted": int|None,
    "reason": str}`` — consumed by rl-doctor (RL-12) so a Pro user can see the
    corpus is being bounded.

    Args:
        project_id: Optional scope; when set the hub prunes only that project's
            rows. None → global prune (all projects on this install).
        force: Bypass the cadence throttle (rl-doctor / manual invocation).
        now_ms: Injectable clock for tests.
        prune_fn: Override the hub prune callable (tests). Defaults to
            ``hub_writer.post_rl_prune``.
    """
    global _last_prune_ts

    if retention_disabled():
        return {"ran": False, "skipped": "disabled", "deleted": None, "reason": "opt-out"}

    now_s = (now_ms / 1000.0) if now_ms is not None else time.time()
    min_interval_s = _env_float(_ENV_MIN_INTERVAL_S, _DEFAULT_MIN_INTERVAL_S)
    if not force and not _throttle_ready(min_interval_s, now_s):
        return {"ran": False, "skipped": "throttled", "deleted": None, "reason": "cadence"}

    plan = compute_retention_plan(now_ms=now_ms)
    if plan.is_noop():
        # Even a no-op consumes the throttle so we don't recompute every event.
        _last_prune_ts = now_s
        return {"ran": False, "skipped": "noop", "deleted": None, "reason": plan.reason}

    if prune_fn is None:
        try:
            from .hub_writer import post_rl_prune as prune_fn  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.debug("maybe_run_retention: hub prune import failed (%s)", exc)
            return {"ran": False, "skipped": "no_prune_route", "deleted": None, "reason": plan.reason}

    # Mark the throttle BEFORE the call so a slow/failing hub can't cause a
    # tight retry loop across successive events.
    _last_prune_ts = now_s
    try:
        result = prune_fn(
            cutoff_ms=plan.cutoff_ms,
            max_rows=plan.max_rows,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 — retention never breaks the caller
        logger.debug("maybe_run_retention: prune call raised (%s)", exc)
        return {"ran": False, "skipped": "prune_error", "deleted": None, "reason": plan.reason}

    if result is None:
        # Route absent on this hub binary (older hub) → graceful skip.
        return {"ran": False, "skipped": "route_unsupported", "deleted": None, "reason": plan.reason}

    deleted = None
    if isinstance(result, dict):
        deleted = result.get("deleted")
    logger.debug("maybe_run_retention: pruned %s rows (%s)", deleted, plan.reason)
    return {"ran": True, "skipped": None, "deleted": deleted, "reason": plan.reason}


def _reset_throttle_for_test() -> None:
    """Test hook — reset the per-process throttle state."""
    global _last_prune_ts
    _last_prune_ts = 0.0


__all__ = [
    "RetentionPlan",
    "compute_retention_plan",
    "maybe_run_retention",
    "retention_disabled",
]
