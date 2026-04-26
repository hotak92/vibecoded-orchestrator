-- Bug 33: extend the orchestrator_tier CHECK constraint to allow
-- "admin" — a server-classified tier for maintainer / admin licenses.
-- Admin variant IDs live in the Supabase env (LS_ADMIN_VARIANT_IDS),
-- never in client-side source.
--
-- SQLite cannot drop a CHECK constraint in place, so we use the
-- standard "recreate table" pattern: rename → CREATE new with new
-- constraint → INSERT old data → DROP old. id=1 invariant preserved.

PRAGMA foreign_keys = OFF;

ALTER TABLE tier_cache RENAME TO tier_cache_old;

CREATE TABLE tier_cache (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    orchestrator_tier TEXT NOT NULL
                      CHECK (orchestrator_tier IN ('free','pro','mao','enterprise','admin')),
    module_licenses   TEXT NOT NULL,
    last_validated    INTEGER NOT NULL,
    last_error        TEXT
);

INSERT INTO tier_cache (id, orchestrator_tier, module_licenses, last_validated, last_error)
SELECT id, orchestrator_tier, module_licenses, last_validated, last_error
FROM tier_cache_old;

DROP TABLE tier_cache_old;

PRAGMA foreign_keys = ON;
