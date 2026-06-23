-- v0.2.65 audit N1-3: index module_access_tokens.token_secret.
--
-- The hub authenticates EVERY module-DB / RL request by looking the bearer
-- token up with `WHERE token_secret = ?1`
-- (vct-hub/src/module_db_api.rs::lookup_token). Migration 019 created the
-- table with a PRIMARY KEY (module_id, project_id) and an index on module_id,
-- but NOT on token_secret — so every auth lookup was a full table scan.
--
-- token_secret is the column the hot-path query filters on, so it gets its
-- own index. The lookup is O(log n) instead of O(n) per request.
--
-- IF NOT EXISTS keeps the migration idempotent (mirrors the
-- idx_module_access_tokens_module index from migration 019). Plain additive
-- index — no table rebuild, so this is NOT a self-transactional migration
-- (it runs inside the migration runner's outer BEGIN IMMEDIATE / COMMIT).
CREATE INDEX IF NOT EXISTS idx_module_access_tokens_secret
    ON module_access_tokens (token_secret);
