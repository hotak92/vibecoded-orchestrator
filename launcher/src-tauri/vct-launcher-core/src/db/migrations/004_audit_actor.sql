-- Migration 003: add `actor` column to audit_log.
--
-- Records the OS user who triggered each audit event. Populated by the
-- launcher process at startup from $USER / $USERNAME (with a literal
-- "system" fallback). NDA-bound multi-user consultant work needs the
-- "who" column to make audit logs meaningful — solo solo-tenant
-- launches are unaffected (column defaults to "unknown" if env is
-- empty, and existing rows backfill to "system").

ALTER TABLE audit_log ADD COLUMN actor TEXT NOT NULL DEFAULT 'system';

CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
