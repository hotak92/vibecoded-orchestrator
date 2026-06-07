-- v0.2.49 access-matrix overhaul, Step A.5 / Phase 1 follow-up.
--
-- Adds `created_at` + `updated_at` audit columns to
-- kg_collection_access so the access matrix can carry a per-row
-- audit trail.
--
-- Why this lands NOW
-- ------------------
-- The v0.2.49 plan's core invariant
--   `is_user_configured(row) := row.updated_at != row.created_at`
-- is the canonical signal for "this row was explicitly user-changed
-- versus seeded by code." Without audit columns, the predicate is
-- undefinable; with them, the predicate is a single field-equality
-- check.
--
-- Per user directive 2026-06-08: "add the new fields in the table
-- from this version." The audit columns get added in v0.2.49 + all
-- new INSERTs from this release forward populate them correctly.
-- Future cycles (v0.2.50+) can then use `is_user_configured` for
-- per-row decisions (default-flip migrations, peer-revoke skip
-- behavior, etc.) with full correctness.
--
-- The companion v0.2.49 force-upgrade migration (Step D / Phase 7
-- of the plan) does NOT depend on this audit data — it rewrites
-- every legacy `read` shared-row to `write` unconditionally per
-- user directive 2026-06-08: "force-update everything to new
-- default permissions." Legacy rows therefore have
-- `created_at == updated_at == 0` (the DEFAULT), reading as
-- "not user-configured" by the predicate, which matches user
-- intent.
--
-- Schema change
-- -------------
-- SQLite supports `ALTER TABLE ... ADD COLUMN` natively; no
-- recreate-and-copy needed.
--
-- Backfill: DEFAULT 0 for existing rows. We do NOT backfill with
-- the current time (which would mark every existing row as "just
-- created"), since legacy rows pre-date the audit-trail concept
-- and their true creation time is unrecoverable. `0` is a sentinel
-- that distinguishes legacy rows from any v0.2.49+ row that will
-- always have `created_at > 0`.
--
-- Idempotency
-- -----------
-- The migration runner only applies each version once. Re-running
-- install.py --update on a host that's already past v29 is a no-op
-- at the migration layer.

ALTER TABLE kg_collection_access
    ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;

ALTER TABLE kg_collection_access
    ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
