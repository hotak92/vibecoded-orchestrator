-- v0.2.49 access-matrix overhaul, Step D / Phase 7 — one-shot
-- force-upgrade migration.
--
-- Rewrites every `kg_collection_access` row with `access_level='read'`
-- whose `collection_name` matches a `project_kg_bindings` row with
-- `role='shared'` to `access_level='write'`. Stamps `updated_at` to
-- wall-clock millis so the `is_user_configured(row) := row.updated_at
-- != row.created_at` predicate flips to TRUE for the rewritten rows
-- (FUTURE-cycle policy decisions in v0.2.50+ will respect this
-- post-migration as "user-configured" — which is honest given the
-- migration is itself a deliberate one-shot data flip).
--
-- Why this lands as a force-update with no per-row predicate gate
-- ---------------------------------------------------------------
-- The v0.2.49 access-matrix overhaul plan originally proposed
-- gating the UPGRADE on `!is_user_configured(row)` (item #20 in the
-- plan's first draft): only rewrite rows the user hadn't explicitly
-- configured. The pre-flight audit (agent #1, 2026-06-08) found
-- that `kg_collection_access` didn't even have the audit columns
-- needed to compute the predicate for legacy rows — at the time of
-- this migration, every legacy row's `created_at` and `updated_at`
-- default to 0 (added in migration 029, backfilled to 0 for existing
-- rows), so the predicate would return FALSE for every legacy row
-- regardless.
--
-- User directive 2026-06-08 (verbatim): "let's just force-update
-- everything to new default permissions." Translation: this
-- migration accepts a deliberate one-shot data loss for any legacy
-- user-configured downgrade on a shared collection. Going forward,
-- v0.2.49+ INSERTs preserve the seed-path invariant; any FUTURE
-- migration can use `is_user_configured` correctly.
--
-- New semantic (post-migration, going forward)
-- --------------------------------------------
-- A project's access on a `role='shared'` binding's collection is
-- `write` by default. The user can still demote explicitly via the
-- launcher's CrossProjectAccessTab UI (kg_set_access bumps
-- updated_at — `is_user_configured` flips to TRUE — that downgrade
-- is preserved across future migrations).
--
-- Idempotency
-- -----------
-- Re-running this migration is a no-op:
--   * The schema-migration tracker (`_schema_migrations`) only runs
--     each version once, so the migration RUNNER never re-applies.
--   * Even if applied manually a second time via raw SQL, the
--     `WHERE access_level = 'read'` filter excludes the already-
--     upgraded rows. No data divergence.
--
-- Soft-fail behavior
-- ------------------
-- Standard SQL UPDATE — no failure modes worth flagging. A
-- non-existent `project_kg_bindings` table (legacy DBs pre-migration
-- 002) would cause this to fail, but migration 002 has shipped in
-- every release since v0.2.0, so the table is guaranteed present by
-- the time migration 031 runs.

UPDATE kg_collection_access
SET access_level = 'write',
    updated_at   = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE access_level = 'read'
  AND collection_name IN (
      SELECT collection_name
      FROM project_kg_bindings
      WHERE role = 'shared'
  );
