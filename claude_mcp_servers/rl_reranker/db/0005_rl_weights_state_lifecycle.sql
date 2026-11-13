-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- vct-rl-reranker module — migration 0005 (v0.2.8)
-- Per-project lifecycle columns matching the 4 GUI configurations:
--   1. local model, online-training ON  (default)
--   2. local model, online-training OFF (online_training_disabled=1)
--   3. using global, read-only          (is_using_global=1)
--   4. mixed (per-project local model + contributes events to global retrain pool)
--
-- Plus last_finetuned_path so the "rotate to most recent finetune" GUI button
-- (deferred to launcher v0.2.44+) has a concrete artifact to point at.
--
-- Owner: vct-rl-reranker (namespace "rl"). Forward-only.
--
-- Idempotency: SQLite's ADD COLUMN does NOT support IF NOT EXISTS. The launcher's
-- module_db_migrations apply path wraps each migration in a savepoint and skips
-- migrations whose sha256 already matches an applied row, so re-applying this
-- file on an already-migrated DB is impossible at the apply layer. Container
-- startup similarly checks module_db_migrations before running migrations.
-- If you need to re-apply manually for debugging, DROP COLUMN equivalent is
-- "CREATE TABLE ... AS SELECT ... + RENAME"; not in scope here.

ALTER TABLE rl_weights_state ADD COLUMN is_using_global          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rl_weights_state ADD COLUMN online_training_disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rl_weights_state ADD COLUMN last_finetuned_path      TEXT    NOT NULL DEFAULT '';
