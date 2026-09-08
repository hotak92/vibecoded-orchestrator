-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 045 (v0.2.94): tombstones for deleted chat-model context rows.
--
-- WHY THIS TABLE EXISTS: the boot seed changed shape. Until v0.2.94 it was
-- `seed_chat_model_context_if_empty` — it wrote only into an EMPTY table, and
-- that emptiness gate doubled as the "do not reinstate what the user
-- deleted" guarantee. It also meant an UPGRADED install never saw a newly
-- shipped model: v0.2.93 added four Claude 5 rows to the shipped seed and
-- every table that already held the ten GLM rows stayed at ten, so the
-- gateway kept advertising Claude ids with the client's conservative default
-- window. A first-boot-only seed is a seed that only ever works on the
-- machines that did not need it.
--
-- So the seed became per-row (`seed_chat_model_context_upsert_missing`:
-- insert what is absent, never touch what is there). That fixes upgrades and
-- breaks the other half — an absent row is absent whether the user deleted it
-- or the vendor is new, so a deleted shipped row would silently come back on
-- the next boot. This table is the missing distinction, and it is a TABLE
-- rather than a flag column because the row it remembers no longer exists:
-- there is nothing left to hang a column on.
--
--   model_id    the deleted row's key, verbatim. Not a foreign key — its
--               whole purpose is to outlive the row, so an FK would delete
--               the memory along with the thing being remembered.
--   deleted_at  ISO-8601 UTC, same format and same reason as
--               chat_model_context.updated_at (rendered verbatim, compared by
--               eye against the export's generated_at).
--
-- WHO READS IT: exactly two paths, and the asymmetry is the design.
--   * The BOOT SEED skips any id with a tombstone. An automatic path must
--     never undo a human's explicit delete.
--   * "Reseed from shipped defaults" CLEARS the tombstones it is about to
--     re-insert. That button already documents itself as restoring a deleted
--     shipped row, and it is a deliberate click — the one place where
--     reversing the delete is what the user asked for.
--
-- A user's own (non-shipped) row leaves a tombstone too. It costs one row and
-- keeps the rule single: "deleted means deleted until you explicitly reseed",
-- with no second class of delete to reason about.
--
-- Forward-only and idempotent: `CREATE TABLE IF NOT EXISTS`, gated again by
-- the runner's `_schema_migrations` version check. Plain DDL — not
-- self-transactional; it rides the runner's outer transaction.
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 44->45 atomically with this migration
-- (B-2 discipline).

CREATE TABLE IF NOT EXISTS chat_model_context_tombstone (
    model_id   TEXT PRIMARY KEY
                    CHECK (length(trim(model_id, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0),
    deleted_at TEXT NOT NULL
                    CHECK (length(trim(deleted_at, ' ' || char(9) || char(10) || char(13) || char(11) || char(12))) > 0)
);

-- On `trim(x, ...)`: SQLite's one-argument `trim()` strips SPACES ONLY, so a
-- tab-only id would sail through `length(trim(x)) > 0`. The explicit
-- character set (space, TAB, LF, CR, VT, FF) is what makes this agree with
-- Rust's `str::trim()` on the writer side — the same reasoning, and the same
-- character set, as migration 043's CHECKs.
--
-- No index beyond the PRIMARY KEY: the seed's only query is a per-id
-- membership test, which the key already answers, and this table holds at
-- most one row per model the user has ever deleted.
