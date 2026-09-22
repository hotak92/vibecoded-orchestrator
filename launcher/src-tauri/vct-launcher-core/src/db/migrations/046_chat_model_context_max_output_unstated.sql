-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — relax the chat_model_context.max_output CHECK from > 0 to
-- >= 0 (migration 046, v0.2.96)
--
-- WHY 0 BECOMES VALID: 0 is the UNSTATED marker — "the vendor's docs publish
-- no per-model max-output figure". The v0.2.96 qwen-vendor work added seven
-- Token-Plan rows (qwen3.8-max, qwen3.8-flash, qwen3.7-max, qwen3.7-plus,
-- qwen3.6-flash, deepseek-v4-pro, deepseek-v4-flash-0731) whose integration
-- page states the 200K context default but NO max-output number. Inventing a
-- plausible figure would violate the same principle the citation CHECK on
-- `source` enforces (R10): this table exists so a client never acts on a
-- guessed number, and a guessed max_output is a guess exactly like a guessed
-- window. So the shipped seed
-- (claude_mcp_servers/model_router/chat_model_context.seed.json) carries
-- max_output: 0 for those rows, and the schema must accept what the shipped
-- data honestly says.
--
-- CROSS-LANGUAGE CONTRACT (one rule, every layer):
--   * 0      = UNSTATED. Valid to store, valid to export, and every consumer
--              treats it as "no information": the Python gateway folds it to
--              None (model_router/catalog.py::_positive) so it is never
--              published as a token count, and the launcher pane renders "—"
--              and offers a blank edit field rather than displaying or
--              applying a number.
--   * > 0    = the cited vendor figure, as before.
--   * < 0    = impossible. Refused here, by ChatModelContextInput::validated,
--              and by the pane's client-side validation.
--
-- MUST MATCH — the rule has FOUR homes and no shared runtime between them,
-- so each names the other three (v0.2.96 D-3; this file previously named the
-- TS home only as "the pane's client-side validation", without a path):
--   1. SQL    — this CHECK.
--   2. Rust   — ChatModelContextInput::validated,
--               launcher/src-tauri/vct-launcher-core/src/db/chat_model_context.rs
--   3. TS     — parseMaxOutputTokens,
--               launcher/src/lib/api/chat_model_context.ts
--   4. Python — catalog.py::_positive,
--               claude_mcp_servers/model_router/catalog.py
-- Widen or narrow the rule in one and it must move in all four.
--
-- SQLite limitation: CHECK constraints are immutable post-CREATE. The only
-- way to widen one is the table-rebuild pattern (mirror of migrations 021
-- and 038): create the replacement table with the new CHECK, copy every row,
-- drop the original, rename.
--
-- CRITICAL: the replacement table carries EVERY column and EVERY other
-- constraint migration 043 declared, VERBATIM — the trim-character-set
-- CHECKs on model_id/vendor/source/updated_at, context_window > 0, the
-- window_1m and user_edited domain CHECKs, the NOT NULLs and the defaults.
-- The ONLY change is max_output's CHECK. Dropping anything else here would
-- be silent data loss or a silently weakened gate on every existing install.
--
-- NO ROW CAN BE LOST BY THE REBUILD: the old CHECK enforced max_output > 0,
-- so every row that exists on any user's DB already satisfies >= 0. The copy
-- is total by construction.
--
-- No index recreation: migration 043 deliberately created none (the PRIMARY
-- KEY on model_id provides the export's ordering for a tens-of-rows table).
--
-- No FK toggles / no self-BEGIN: nothing references chat_model_context via
-- an inbound FOREIGN KEY — migration 045's tombstone table deliberately has
-- NO FK to it (the memory of a delete must outlive the row) — so this
-- rebuild rides the migrations runner's outer transaction exactly like 038
-- (NOT in SELF_TRANSACTIONAL_MIGRATIONS). foreign_keys stays ON throughout.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is executed exactly once per DB, and the
-- create-copy-drop-rename shape converges even on a crash-window replay).
--
-- ATOMIC PAIRING (B-2 lesson): vco_lib/schema_versions.py's
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 45 -> 46 in the SAME merge as this
-- file, then `python scripts/regen_schema_versions_json.py` refreshes the
-- committed snapshot — a Python-side bump landing ahead of this registration
-- would stamp a phantom schema version, and either half landing alone reds
-- the two-sided parity gates (tests/test_v52_ag_schema_versions.py and
-- launcher/src-tauri/tests/schema_versions_rust_parity.rs).

-- 1. Create the replacement table: migration 043's column set and every
--    other constraint verbatim; only max_output's CHECK widens to >= 0.
CREATE TABLE chat_model_context_new (
    model_id       TEXT    PRIMARY KEY
                           CHECK (length(trim(model_id, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    vendor         TEXT    NOT NULL
                           CHECK (length(trim(vendor, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    context_window INTEGER NOT NULL
                           CHECK (context_window > 0),
    -- 0 = UNSTATED (the vendor publishes no figure); negative is impossible.
    max_output     INTEGER NOT NULL
                           CHECK (max_output >= 0),
    window_1m      INTEGER NOT NULL DEFAULT 0
                           CHECK (window_1m IN (0, 1)),
    source         TEXT    NOT NULL
                           CHECK (length(trim(source, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    source_note    TEXT    NOT NULL DEFAULT '',
    user_edited    INTEGER NOT NULL DEFAULT 0
                           CHECK (user_edited IN (0, 1)),
    updated_at     TEXT    NOT NULL
                           CHECK (length(trim(updated_at, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0)
);

-- 2. Copy every row across (column order is identical). The old CHECK
--    guaranteed max_output > 0, so no row can violate the new >= 0.
INSERT INTO chat_model_context_new
    (model_id, vendor, context_window, max_output, window_1m,
     source, source_note, user_edited, updated_at)
SELECT
    model_id, vendor, context_window, max_output, window_1m,
    source, source_note, user_edited, updated_at
FROM chat_model_context;

-- 3. Drop the original.
DROP TABLE chat_model_context;

-- 4. Rename replacement into place.
ALTER TABLE chat_model_context_new RENAME TO chat_model_context;
