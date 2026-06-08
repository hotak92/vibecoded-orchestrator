-- v0.2.49 access-matrix overhaul, Step D / Phase 7 — one-shot
-- force-update migration.
--
-- TWO PASSES (per Step F user verdicts Q3 + Q4, 2026-06-08):
--
-- Pass 1: Upgrade default shared rows from `read` to `write`.
-- Pass 2: DELETE all cross-project peer rows that don't belong in
--         the "default" v0.2.49 shape.
--
-- Both passes stamp `updated_at = created_at + 1` (a deliberate
-- sentinel value) so the
-- `is_user_configured(row) := row.updated_at != row.created_at`
-- predicate reads FALSE for migrated rows (system-stamped, not
-- user-touched). FUTURE-cycle migrations (v0.2.50+) can then use
-- the predicate correctly: user-configured rows post-v0.2.49 will
-- have `updated_at` set to wall-clock millis (much greater than
-- `created_at + 1`), so the predicate flips TRUE only for genuine
-- user edits.
--
-- The sentinel-vs-wall-clock distinction matters because of F-2c
-- (the "preserve user-configured peers" mode-setter loop, see
-- `commands/kg.rs::set_collection_access_mode`). Pre-Step-F the
-- migration stamped wall-clock millis → ALL migrated rows read as
-- user-configured = TRUE → F-2c silently skipped them on future
-- mode changes. Sentinel value fixes the predicate semantic.
--
-- Why this lands as a force-update with no per-row predicate gate
-- ---------------------------------------------------------------
-- User directive 2026-06-08 (verbatim): "force update to default to
-- read+write permissions on own + shared collection, no cross-project
-- permissions (excluding root/shared), then is_user_configured = FALSE
-- for them, since those were set to their default programmatically".
--
-- Translation: the v0.2.49 "default shape" for `kg_collection_access`
-- is exactly three rows per project:
--   (a) own primary (the project's `role='primary'` binding's
--       collection_name) at `write`
--   (b) own dev (the `_Development` sibling of own primary) at `write`
--   (c) shared (the project's `role='shared'` binding's collection_name,
--       which post-Step-A persistence equals the orchestrator-root's
--       primary KG name) at `write`
--
-- Any OTHER row in `kg_collection_access` is either a user-explicit
-- peer grant from a pre-v0.2.49 install OR drift from old install
-- paths. Per the user directive, the migration drops ALL such rows.
-- User recovery: re-grant via the launcher GUI Identity → Manage
-- access tab (which is now the canonical UX for cross-project grants).
--
-- Idempotency
-- -----------
-- Re-running this migration is a no-op:
--   * The schema-migration tracker (`_schema_migrations`) only runs
--     each version once, so the runner never re-applies.
--   * Pass 1's `WHERE access_level = 'read'` filter excludes already-
--     upgraded rows on manual re-run.
--   * Pass 2's `NOT IN (default keep-list)` filter excludes the
--     already-kept-row set on manual re-run (idempotent set
--     difference).
--
-- Corrupted-projects carve-out (per RL chat msg 237)
-- ----------------------------------------------------
-- A "corrupted" project is one that lacks a `role='primary'` binding
-- row. For such a project the keep-list's "own primary" SELECT yields
-- nothing → the project's rows would be DELETEd entirely → user loses
-- the access matrix with no recovery path. To prevent this destruction:
-- we ADDITIONALLY preserve rows for project_ids that have ZERO
-- `role='primary'` bindings (treating the corrupted state as
-- "preserve what's there + the user re-registers via the GUI to heal").
-- Aligns with the "no auto-destroy user data" discipline elsewhere
-- in install.py + the deferral pattern.

-- ───── Pass 1: Upgrade shared `read` → `write` with sentinel ─────
UPDATE kg_collection_access
SET access_level = 'write',
    updated_at   = created_at + 1
WHERE access_level = 'read'
  AND collection_name IN (
      SELECT collection_name
      FROM project_kg_bindings
      WHERE role = 'shared'
  );

-- ───── Pass 2: Drop cross-project peer rows ─────
--
-- Keep ONLY rows that match the v0.2.49 default shape:
--   (a) own primary
--   (b) own dev (derived: REPLACE(_KnowledgeGraph → _Development))
--   (c) shared
--   (d) [corruption carve-out] rows for projects with NO
--       `role='primary'` binding (preserved as-is to avoid destroying
--       the only access trail for misregistered projects)
--
-- Also stamps `updated_at = created_at + 1` on the SURVIVING rows
-- (the sentinel value, so `is_user_configured` reads FALSE for them
-- post-migration per user directive).
DELETE FROM kg_collection_access
WHERE (project_id, collection_name) NOT IN (
    -- (a) own primary: explicit role='primary' binding
    SELECT pkb.project_id, pkb.collection_name
    FROM project_kg_bindings pkb
    WHERE pkb.role = 'primary'

    UNION

    -- (b) own dev: derived from own primary by suffix swap
    SELECT pkb.project_id,
           REPLACE(pkb.collection_name, '_KnowledgeGraph', '_Development')
    FROM project_kg_bindings pkb
    WHERE pkb.role = 'primary'
      AND pkb.collection_name LIKE '%\_KnowledgeGraph' ESCAPE '\'

    UNION

    -- (c) shared: explicit role='shared' binding
    SELECT pkb.project_id, pkb.collection_name
    FROM project_kg_bindings pkb
    WHERE pkb.role = 'shared'

    UNION

    -- (d) corruption carve-out: rows for projects with NO
    -- role='primary' binding stay untouched (preserve-and-let-the-
    -- user-re-register vs aggressive destroy).
    SELECT kca.project_id, kca.collection_name
    FROM kg_collection_access kca
    WHERE NOT EXISTS (
        SELECT 1 FROM project_kg_bindings pkb_inner
        WHERE pkb_inner.project_id = kca.project_id
          AND pkb_inner.role = 'primary'
    )
);

-- Sentinel-stamp every surviving row to `created_at + 1`. Per user
-- directive 2026-06-08 (Q3): post-migration the default keep-list
-- rows are considered "system-defaulted" → is_user_configured must
-- read FALSE for them downstream.
--
-- Why we stamp ALL surviving rows (not just `WHERE updated_at = 0`):
-- pre-v0.2.49 rows have `updated_at = 0` (per migration 029 backfill
-- of legacy rows), but rows written by `kg_seed_access` or
-- `kg_set_access` between v0.2.49 install and migration 031 run have
-- `updated_at = wall_clock_millis` (NOT 0). Both classes survived
-- Pass 2's keep-list filter and BOTH must read as is_user_configured
-- = FALSE post-migration per the directive. The sentinel covers both.
--
-- Idempotent on re-run: every row that's already at the sentinel
-- stays at the sentinel (`x = x + 1 - 1` would be wrong, but
-- `updated_at = created_at + 1` is FIXED relative to `created_at`,
-- so re-stamping produces the same value).
--
-- The exception class — rows that should KEEP a wall-clock-millis
-- updated_at post-migration — doesn't exist: by Pass 2's filter,
-- only the v0.2.49 default keep-list rows survive, all of which are
-- system-defaulted per the directive. Any user-explicit cross-project
-- grants were already deleted by Pass 2.
--
-- FUTURE-cycle code that needs to distinguish "migrated by 031" from
-- "user-touched post-v0.2.49" can check `updated_at == created_at + 1`
-- for the migration sentinel, OR `updated_at > created_at + 1` for
-- post-migration user mutations. Pre-migration rows where the user
-- HAD set updated_at to wall-clock millis lose that signal (the
-- directive's accepted tradeoff).
UPDATE kg_collection_access
SET updated_at = created_at + 1;
