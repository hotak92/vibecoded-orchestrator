-- 2026-07-14 — canonicalize the shared-KG gate module_id (split-brain fix).
--
-- BACKGROUND
-- ----------
-- The per-project shared-KG gate flags
--   * shared_kg_read_disabled
--   * shared_kg_write_disabled
--   * shared_kg_opt_out          (legacy alias of write_disabled)
-- were WRITTEN by the launcher GUI setters/getters
-- (commands/projects_v2.rs) under module_settings.module_id = '__project__',
-- but READ by the hub /config resolver (vct-hub/src/config_api.rs) and the
-- Python env projection (vco_lib/config_projection.py) under
-- module_id = 'orchestrator-core'. Same key, two namespaces, two reader
-- populations → the GUI toggle read FALSE on the hub + Python surfaces (a
-- permanently-disabled toggle from those consumers' perspective).
--
-- v0.2.8x repoints ALL writers/getters to the canonical 'orchestrator-core'
-- id (db/module_settings_keys.rs::ORCHESTRATOR_CORE_MODULE_ID). This
-- migration relocates any EXISTING '__project__' rows for these three keys
-- onto the canonical id so a project that toggled the gate before the fix
-- keeps its choice.
--
-- CONFLICT SEMANTICS (per the 2026-07-14 user directive)
-- ------------------------------------------------------
-- If BOTH a '__project__' row and an 'orchestrator-core' row already exist
-- for the same (project_id, key), keep the NEWER value. module_settings has
-- no updated_at column, so "newer" is decided by rowid (SQLite rowids are
-- monotonically increasing on insert, so a higher rowid == a later write).
-- Whichever row wins, the legacy '__project__' row is DELETED afterwards so
-- no stale duplicate remains.
--
-- IDEMPOTENCY
-- -----------
-- The schema-migration runner applies each version exactly once. Even on a
-- manual re-run the operations converge: after the DELETE there are no
-- '__project__' rows left for these keys, so the UPDATE/INSERT match nothing
-- and the final DELETE is a no-op.
--
-- NOT self-transactional: no PRAGMA foreign_keys toggle, no table rebuild —
-- rides the runner's outer transaction.
--
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 39 -> 40 atomically with this
-- migration's Rust registration.

-- Step 1: for (project_id, key) pairs that exist ONLY under '__project__'
-- (no canonical row yet), relocate them by flipping the module_id in place.
-- The partial UNIQUE index idx_ms_unique_per_project is on
-- (project_id, module_id, setting_key) WHERE project_id IS NOT NULL, so this
-- UPDATE cannot violate uniqueness for the no-canonical-row case.
UPDATE module_settings
SET module_id = 'orchestrator-core'
WHERE module_id = '__project__'
  AND setting_key IN (
      'shared_kg_read_disabled',
      'shared_kg_write_disabled',
      'shared_kg_opt_out'
  )
  AND NOT EXISTS (
      SELECT 1 FROM module_settings AS canon
      WHERE canon.project_id  IS module_settings.project_id
        AND canon.module_id   = 'orchestrator-core'
        AND canon.setting_key = module_settings.setting_key
  );

-- Step 2: for (project_id, key) pairs where BOTH rows exist, keep the NEWER
-- (higher-rowid) value. Overwrite the canonical row's value with the
-- '__project__' row's value ONLY when the legacy row is newer.
UPDATE module_settings AS canon
SET setting_value = (
    SELECT legacy.setting_value
    FROM module_settings AS legacy
    WHERE legacy.project_id  IS canon.project_id
      AND legacy.module_id   = '__project__'
      AND legacy.setting_key = canon.setting_key
)
WHERE canon.module_id = 'orchestrator-core'
  AND canon.setting_key IN (
      'shared_kg_read_disabled',
      'shared_kg_write_disabled',
      'shared_kg_opt_out'
  )
  AND EXISTS (
      SELECT 1 FROM module_settings AS legacy
      WHERE legacy.project_id  IS canon.project_id
        AND legacy.module_id   = '__project__'
        AND legacy.setting_key = canon.setting_key
        AND legacy.rowid       > canon.rowid
  );

-- Step 3: delete every remaining legacy '__project__' row for these keys
-- (both the "kept canonical, drop legacy" case and the "already relocated in
-- step 1 leaves nothing" case). After this the gate keys live ONLY under the
-- canonical id.
DELETE FROM module_settings
WHERE module_id = '__project__'
  AND setting_key IN (
      'shared_kg_read_disabled',
      'shared_kg_write_disabled',
      'shared_kg_opt_out'
  );
