-- Per-secret active flag for the v2 secrets API (Bug 3 follow-up to PR #60).
--
-- Background: the original v0.1.3 lifecycle made `Unset` clear the
-- keychain value. Token rotation forced users to re-type the value when
-- re-Setting, which they explicitly do not want. The new lifecycle keeps
-- the keychain value across an Unset and instead flips a metadata flag
-- in this DB. `Reactivate` flips the flag back without touching the
-- keychain, so a one-click "pause then resume" works without re-entry.
--
-- Storage choice: keep the keychain SINGLE-VALUE (no shadow keys, no
-- ".active" / ".archive" suffix gymnastics). The keychain holds the
-- value; this DB row holds whether readers should be allowed to see it.
-- This keeps the OS keychain UI (Seahorse on Linux, Keychain Access on
-- macOS, Credential Manager on Windows) clean — one entry per logical
-- secret — and makes the gate explicit and easy to audit.
--
-- Read-time gate: `is_secret_set` and `get_secret_preview` cross-check
-- this table. If `active=0` they behave as if the secret were not set.
-- This is enforced in `commands/secrets_cmd.rs`.
--
-- Natural key matches the keychain entry shape:
-- (scope, project_id, module_id, key). `project_id` carries the same
-- sentinel values used by the runtime (`_global_`, `_user_shared_`) so
-- that global / shared secrets get one row each instead of being
-- duplicated per project. NO foreign key on `project_id` because of
-- those sentinels — the application enforces validity via
-- `enforce_scope_invariants`.

CREATE TABLE IF NOT EXISTS secret_active_state (
    scope        TEXT NOT NULL                       -- 'per_project'|'shared'|'global'
                 CHECK (scope IN ('per_project','shared','global')),
    project_id   TEXT NOT NULL,                       -- real project id OR sentinel
    module_id    TEXT NOT NULL,
    key          TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,          -- 1=readers may see; 0=paused/inactive
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (scope, project_id, module_id, key)
);

CREATE INDEX IF NOT EXISTS idx_secret_active_state_active
    ON secret_active_state(active);
