-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 043 (v0.2.92, WP-11): the version-keyed CHAT-model context table.
--
-- WHAT IT ANSWERS: "how big is this chat model's context window, and should
-- the gateway advertise it to Claude Code as a 1M-window id?". The model
-- gateway (`claude_mcp_servers/model_router/`) reads the exported form of
-- this table when it builds `/v1/models`; rows with window_1m=1 are
-- advertised as `<id>[1m]`, the client's own convention for the 1M variant.
-- Without it, Claude Code assumes a conservative default window for an id it
-- does not know, so its context indicator under-reads and /compact fires
-- early.
--
-- ── THIS IS *NOT* `MODEL_TOKEN_LIMITS` ─────────────────────────────────────
-- `claude_mcp_servers/weaviate_mcp/chunking.py::MODEL_TOKEN_LIMITS` answers a
-- question phrased with the same words and means something else. Do not merge
-- them, and do not teach either one to read the other:
--
--   * DOMAIN — MODEL_TOKEN_LIMITS covers EMBEDDING models and sets Ollama's
--     `num_ctx` for the chunker. It is a WIRE-FORMAT input to stored
--     embeddings (guarded by the chunker-revision sentinel), so changing it
--     invalidates vectors. This table covers CHAT models and only changes
--     what the gateway ADVERTISES; changing it re-embeds nothing.
--   * LOOKUP RULE, deliberately opposite — `_num_ctx_for_model` matches
--     PARTIALLY because an Ollama tag varies by quantisation
--     (`qwen3-embedding:0.6b` vs `...-q8_0`) while the architectural limit
--     does not. This table is EXACT-full-model-id only: `glm-5.2` has a 1M
--     window while `glm-5.1` has 200K, so a `glm-5*` family wildcard would
--     overstate the smaller by 5x and the user would find out only when a
--     long session silently truncated. That 5x is the entire reason this
--     table exists rather than a regex.
--   * LIFECYCLE — one is code (a Python dict pinned by tests) that changes
--     when we change embedding backends; this one is user-editable data that
--     changes when a vendor ships a model.
--
-- See PLAN-v0292-EXTENSION §3.19 for the full ruling.
--
-- ── COLUMNS ────────────────────────────────────────────────────────────────
--   model_id       FULL vendor model id, verbatim, the PRIMARY KEY. Never a
--                  family prefix, never a pattern (see the lookup rule above).
--   vendor         Vendor registry id (`zai`, ...). Carried so the pane can
--                  group rows and the export can be filtered by a future
--                  consumer without a second source of truth.
--   context_window Total context tokens, the vendor's own decimal-K figure.
--   max_output     Max output tokens, likewise.
--   window_1m      1 = advertise as `<id>[1m]`. The only field the gateway
--                  acts on today; the other two are carried so a status card
--                  can show them from one place.
--   source         The citation. NON-EMPTY IS ENFORCED HERE, by CHECK, and
--                  that is deliberate: R10 says no row without a cited
--                  official source, because guessing a window is exactly what
--                  a version-keyed table exists to prevent. The reader
--                  (`model_router/context_table.py::_parse`) independently
--                  IGNORES an uncited row and warns; the CHECK means our own
--                  writer can never produce one for it to ignore.
--   source_note    Optional caveat travelling WITH the citation, so an honest
--                  qualification survives the DB round-trip into the export.
--                  It is load-bearing for two shipped rows: `glm-5.3-flash`
--                  is documented under /guides/vlm/, and `glm-4.5-air` has no
--                  page of its own (404) so its spec is cited from the
--                  `glm-4.5` card — with the note that third-party listings
--                  of a "1M glm-4.5-air" are NOT official. Without this
--                  column that note would be dropped on import and a future
--                  editor could "correct" the row backwards.
--   user_edited    1 = a human changed this row from the launcher. The
--                  reseed guard: "Reseed from shipped defaults" re-applies
--                  seed rows ONLY where this is 0, so a user edit is never
--                  clobbered by an orchestrator update.
--   updated_at     ISO-8601 UTC (e.g. `2026-09-02T18:04:11Z`). TEXT rather
--                  than the epoch-millis convention used elsewhere in
--                  launcher.db because this value is rendered verbatim in the
--                  pane and read by humans comparing it against the export's
--                  `generated_at`, which is ISO-8601 by the gateway's file
--                  contract.
--
-- ── WHAT IS *NOT* HERE ─────────────────────────────────────────────────────
-- No seed rows. The seed lives in exactly ONE file
-- (`claude_mcp_servers/model_router/chat_model_context.seed.json`, which the
-- gateway ALSO reads as its shipped fallback) and is loaded at first boot by
-- `commands::chat_model_context`. Seeding from SQL would fork the shipped
-- data into two copies with two update paths — the same forking an
-- `include_str!` across the crate boundary would cause.
--
-- No project_id. The table is orchestrator-wide: a model's context window is
-- a property of the model, not of a project.
--
-- Forward-only and idempotent: `CREATE TABLE IF NOT EXISTS`, gated again by
-- the runner's `_schema_migrations` version check, so re-running it on a DB
-- that already has the table is a no-op that touches no row. Plain DDL — not
-- self-transactional; it rides the runner's outer transaction.
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 42->43 atomically with this migration
-- (B-2 discipline).

CREATE TABLE IF NOT EXISTS chat_model_context (
    model_id       TEXT    PRIMARY KEY
                           CHECK (length(trim(model_id, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    vendor         TEXT    NOT NULL
                           CHECK (length(trim(vendor, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    context_window INTEGER NOT NULL
                           CHECK (context_window > 0),
    max_output     INTEGER NOT NULL
                           CHECK (max_output > 0),
    window_1m      INTEGER NOT NULL DEFAULT 0
                           CHECK (window_1m IN (0, 1)),
    -- The citation gate. A blank / whitespace-only source is REFUSED by the
    -- database itself, so no code path — GUI, reseed, a future importer, or a
    -- hand-written UPDATE — can produce an uncited row. See the note under
    -- the table about why the character set is spelled out.
    source         TEXT    NOT NULL
                           CHECK (length(trim(source, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    source_note    TEXT    NOT NULL DEFAULT '',
    user_edited    INTEGER NOT NULL DEFAULT 0
                           CHECK (user_edited IN (0, 1)),
    updated_at     TEXT    NOT NULL
                           CHECK (length(trim(updated_at, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0)
);

-- NOTE ON `trim(x, ...)` ABOVE: SQLite's one-argument `trim()` strips SPACES
-- ONLY — `trim(char(9))` is a tab, length 1, and would sail through a
-- `length(trim(x)) > 0` check. The explicit character set (space, TAB, LF,
-- CR, VT, FF) is what makes these constraints agree with Rust's
-- `str::trim()` (Unicode whitespace) on the writer side AND with the
-- gateway reader's `str.strip()` on the consumer side. Without it a
-- tab-only "citation" would pass here, be written to the export, and then be
-- silently DROPPED by the reader as uncited — a row the launcher shows and
-- the gateway ignores, which is the exact divergence these checks exist to
-- prevent.
--
-- The export walks the whole table in model_id order on every mutation and on
-- every boot. The PRIMARY KEY on model_id already provides that ordering, so
-- no additional index is created: this table holds tens of rows and a second
-- index would cost maintenance for nothing.
