-- v0.2.40 L1: per-paid-module license keys.
--
-- Background
--   Through v0.2.39 the launcher held exactly ONE license key in the
--   keychain under (service='vct.global.licensing', username=
--   'VIBECODED_LICENSE_KEY') and that single key drove the orchestrator
--   tier + every per-module entitlement (the existing
--   `tier_cache.module_licenses` JSON projection). That worked while
--   the only paid module was bundled into the orchestrator tier itself,
--   but breaks down once each paid module — RL Reranker, MAO, future
--   agent packs — can be purchased independently.
--
--   L1's mission is to let each paid module own its OWN license key,
--   keyed by `module_id`. Adding a key for module A must not invalidate
--   module B's entitlement; refreshing module A's key must not touch
--   module B's cached tier; deactivating module A must leave module B
--   untouched. The existing `tier_cache.module_licenses` JSON shape
--   already projects per-module entitlements, so this migration adds
--   the SOURCE-OF-TRUTH side: the raw user-provided keys, keyed by
--   `module_id`.
--
-- What this migration does
--   Adds two new tables. Neither replaces `tier_cache` — that table
--   continues to hold the EFFECTIVE projected state (orchestrator tier
--   + the per-module overlay map). The new tables hold raw user input
--   (the keys themselves, audit timestamps, last validation outcome).
--
--   1. `license_keys` — one row per (module_id) the user has activated
--      a key for. The reserved `module_id = '__orchestrator__'` slot
--      holds the legacy orchestrator-tier root key (the single-key
--      model from v0.2.39 and earlier). For per-module add-on keys
--      (RL Reranker, MAO, etc.), `module_id` is the manifest id (e.g.
--      `'vct-rl-reranker'`). The raw key VALUE never lives in this
--      table — only a short prefix (first 12 chars, for the GUI's
--      "ends in ..." display) plus the keychain coordinates needed to
--      re-fetch it. The actual key bytes live in the OS keychain at
--      (service='vct.global.licensing', username='license_key__<module_id>').
--      That mirrors the existing single-key layout where
--      username='VIBECODED_LICENSE_KEY' — same scope, same module,
--      just multiple usernames.
--
--   2. `license_key_validations` — append-only audit of every
--      validation attempt for a given module_id. Lets the GUI render
--      a "last 3 validation outcomes" timeline without a lossy
--      single-column scalar. Capped at the application layer
--      (`launcher/src-tauri/src/db/license_keys.rs` keeps the most
--      recent N rows per module via a window query).
--
-- Why NOT replace tier_cache
--   `tier_cache` has the `id INTEGER PRIMARY KEY CHECK (id = 1)`
--   single-row invariant baked into migrations 001 and 005. Loosening
--   that constraint would touch every reader (tier.rs, modules.rs,
--   licensing.rs, the GUI store, the Python validator's mirror in
--   `~/.vibecoded/license_cache.json`) — out of scope for v0.2.40.
--   The cleaner split is: tier_cache stays the EFFECTIVE projection,
--   `license_keys` is the per-module SOURCE of input keys, and the
--   refresh flow (`license_refresh` in commands/licensing.rs) writes
--   per-module entries into `tier_cache.module_licenses` as it
--   validates each key independently.
--
-- Legacy single-key compatibility
--   The legacy keychain entry at
--   (service='vct.global.licensing', username='VIBECODED_LICENSE_KEY')
--   is NOT deleted by this migration. On first reach to
--   `list_license_keys` after the migration, if no `license_keys` row
--   exists AND the legacy keychain entry is present, the Rust side
--   synthesizes a virtual `module_id='__orchestrator__'` row
--   pointing at the legacy username. The user can then explicitly
--   "promote" it to a real row (which writes the value under the new
--   username scheme) without losing the cached tier in between.
--
-- See also
--   - launcher/src-tauri/src/commands/licensing.rs   (Tauri command surface)
--   - launcher/src-tauri/vct-launcher-core/src/db/license_keys.rs  (CRUD)
--   - VCThelpers/license/validator.py::feature_enabled(module_id=...)

CREATE TABLE IF NOT EXISTS license_keys (
    -- Manifest module id (e.g. 'vct-rl-reranker'). Reserved value
    -- '__orchestrator__' identifies the root orchestrator-tier key.
    -- PRIMARY KEY enforces "one key per module" per the L1 design
    -- (per-paid-module keys, not multiple keys per module — the user
    -- replaces the key in place when rotating).
    module_id            TEXT PRIMARY KEY,

    -- Short display prefix of the key (first 12 chars). The full key
    -- VALUE is NEVER stored in this column — only what the GUI shows
    -- in a "ends in ..." label. Allows the GUI to render the row
    -- without re-fetching from the keychain on every refresh.
    key_prefix           TEXT NOT NULL,

    -- Keychain coordinates for the actual key bytes. `keychain_username`
    -- is the `username` field in (service='vct.global.licensing',
    -- username=<this>). For migrated legacy rows this is the constant
    -- 'VIBECODED_LICENSE_KEY'; for new per-module rows it's
    -- 'license_key__<module_id>'. Storing this explicitly (rather than
    -- recomputing from module_id) preserves the legacy mapping after
    -- migration without a one-time rewrite.
    keychain_username    TEXT NOT NULL,

    -- Effective tier the last successful validation returned (e.g.
    -- 'pro', 'mao', 'enterprise'). NULL when the key has never been
    -- validated (just-added rows). Distinct from the `tier_cache`
    -- projection's `module_licenses[module_id].tier`: this column
    -- records what the SERVER said the LAST TIME we validated THIS
    -- key, whereas the tier_cache projection is the effective merged
    -- state the rest of the launcher consumes.
    tier                 TEXT,

    -- Unix milliseconds of the last validation attempt (success OR
    -- failure — `last_validation_error` distinguishes). NULL = never
    -- validated. Drives the "Last validated" label in the GUI.
    validated_at         INTEGER,

    -- Human-readable error from the last validation attempt. NULL on
    -- success. Drives the "Validation failed: ..." status badge in
    -- the GUI when the row's `tier` is still set but `validated_at`
    -- is recent and an error is recorded (transient network failure
    -- with cached tier still in use).
    last_validation_error TEXT,

    -- Audit timestamps. created_at = first time the user activated
    -- this module's key; updated_at = last `set_module_license_key`
    -- call (key rotation refreshes this without changing created_at).
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);

-- Append-only audit of validation attempts. Capped at the application
-- layer (license_keys.rs trims to the most recent N rows per module).
-- Distinct from `audit_log` (which captures Tauri-command-level events
-- like `license_activate`); this table captures the server-validation
-- round-trip outcomes (success / 401 / 5xx / network failure / etc.)
-- for the per-module license-management GUI's timeline view.
CREATE TABLE IF NOT EXISTS license_key_validations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id           TEXT NOT NULL,
    validated_at        INTEGER NOT NULL,
    -- Server-returned tier on success, NULL on failure.
    tier                TEXT,
    -- HTTP status code (200 / 401 / 503 / 0=network failure).
    http_status         INTEGER NOT NULL,
    -- Human-readable error message on failure, NULL on success.
    error_message       TEXT
);

CREATE INDEX idx_license_key_validations_module
    ON license_key_validations(module_id, validated_at DESC);
