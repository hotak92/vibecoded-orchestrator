-- launcher.db — heartbeat_at liveness columns on kg_syncs + code_graph_builds
-- (BUG 2, v0.2.89 — field-reported kg_sync rows stuck RUNNING for ~70 h).
--
-- Terminal-state writes for both tables happen only inside the spawned
-- launcher task; the only reconciliation was the boot-time orphan sweep.
-- Any task death while the launcher stays up (tokio task panic/abort, a
-- task parked on the embed-admission queue, launcher killed and not
-- restarted for days) left the row RUNNING forever with zero signal.
--
-- heartbeat_at (ms since epoch, nullable) records "the launcher task
-- driving this row is alive" — the running task stamps it every 60 s,
-- including while parked on the admission semaphore. It is a LIVENESS
-- marker, NOT a per-file / per-node deadline (those are ruled out by
-- design; the existing stall watchdog already bounds subprocess silence).
-- Staleness = status='running' AND COALESCE(heartbeat_at, started_at, 0)
-- older than the stale window (see Db::mark_stale_running_* — the
-- started_at fallback covers pre-migration legacy rows that never got a
-- tick). Consumers: the 5-minute sweeper spawned from
-- resume_pending_syncs + the read-time guards in the two status commands.
--
-- Both columns land in ONE migration because the tables are deliberate
-- twins (kg_syncs.rs module docs) and the sweeper reconciles both in the
-- same pass. Plain additive ALTER TABLE — idempotent via the runner's
-- version check, not self-transactional.

ALTER TABLE kg_syncs ADD COLUMN heartbeat_at INTEGER;
ALTER TABLE code_graph_builds ADD COLUMN heartbeat_at INTEGER;
