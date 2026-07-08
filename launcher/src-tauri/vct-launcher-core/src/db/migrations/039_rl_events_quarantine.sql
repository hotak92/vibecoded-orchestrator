-- Migration 039 (v0.2.75 RL-14): quarantine marker for poisoned rl_events rows.
--
-- WHY: pre-v0.2.70 hybrid-fusion/BM25 paths wrote UNBOUNDED node scores
-- (observed max 10.37) into retrieval events; compute_unified_targets then
-- clamped >1 → 1.0, silently mis-marking those nodes as max-cited. The F-E
-- writer clamp (v0.2.70) stopped NEW poison at the source, but historical
-- rows remain in the corpus and keep contaminating every offline training
-- pass that reads them. Deleting them would destroy query-distribution
-- signal — so they are MARKED, not removed:
--
--   * quarantined_at    INTEGER NULL — unix-millis when the row was marked.
--                        NULL = clean (the overwhelmingly common case).
--   * quarantine_reason TEXT NULL    — stable machine tag ('score_out_of_range'
--                        for the historical class; future poison classes add
--                        their own tags).
--
-- Training-data reads (hub GET /api/v1/rl/events + the module DB API's
-- per-module events route) exclude quarantined rows by default; rl-doctor
-- reports the quarantined count.
--
-- The one-time MARKING pass for the historical score>1.0 class runs in Rust
-- (Db::backfill_quarantine_out_of_range, called once from Db::open guarded
-- by the app_state key 'rl_events.quarantine_backfill_v1') rather than in
-- this migration: payload_json is writer-supplied TEXT the hub never
-- JSON-validates, so a SQL json_each() pass would hard-error the whole
-- migration on one malformed row, whereas the Rust pass skips unparseable
-- payloads softly. The columns land here; the marking is data hygiene, not
-- schema.
--
-- Plain additive ALTER TABLEs — idempotent via the runner's version check,
-- not self-transactional. LAUNCHER_DB_TABLE_SET_VERSION bumps 38->39
-- atomically with this migration (B-2 discipline).

ALTER TABLE rl_events ADD COLUMN quarantined_at INTEGER;
ALTER TABLE rl_events ADD COLUMN quarantine_reason TEXT;

-- Partial index: the training-read hot path filters `quarantined_at IS NULL`
-- on every list; quarantined rows are expected to stay a tiny minority, so a
-- partial index on the marked rows keeps the doctor's count cheap without
-- bloating the main read path.
CREATE INDEX IF NOT EXISTS idx_rl_events_quarantined
    ON rl_events (quarantined_at)
    WHERE quarantined_at IS NOT NULL;
