-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — module deprecation surface (migration 018, v0.2.31)
--
-- Tracks (1) the deprecation history of every (project × module) pair as
-- an append-only audit trail, and (2) a single first-seen marker per pair
-- that the launcher's one-shot desktop notification consults to suppress
-- re-fires across sessions.
--
-- The deprecation flag itself comes from the Supabase `rl-latest-version`
-- (or any module's `runtime.update_endpoint`) response. When the launcher
-- polls and observes a transition (false → true OR true → false), it
-- appends a row to `deprecation_events` and — for the first
-- false → true transition only — inserts a row into `module_deprecation_seen`.
--
-- WHY two tables:
--   * `deprecation_events` is append-only. Useful for support-ticket
--     triage ("when did the user first see the deprecation?") and for
--     the eventual "deprecated for N days" badge text. Every transition
--     is recorded.
--   * `module_deprecation_seen` is a one-row-per-pair sentinel. Tracks
--     "did we already fire the desktop notification?" so the second + Nth
--     session don't re-notify. Cleared only via SQL — there is no
--     `set_deprecation_seen(..., false)` API. Users who want to re-see
--     the notification can delete the row by hand (rare, untested path).
--
-- FK semantics:
--   * project_id → projects.id ON DELETE CASCADE: rows die with project.
--     Deprecation history of a deleted project is not interesting; the
--     module-side audit lives in the publisher's own ticketing system.
--
-- v0.2.31 deferral: we ship the schema + writer + Tauri command surface,
-- but the actual cron polling that drives `apply_deprecation_state` lands
-- in v0.2.32. The `module_update_poll()` callable in module_deprecation.rs
-- is exposed for manual testing in the meantime.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is executed exactly once per DB).

CREATE TABLE IF NOT EXISTS deprecation_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    module_id       TEXT NOT NULL,
    detected_at     INTEGER NOT NULL,                 -- unix epoch millis
    deprecated      INTEGER NOT NULL,                 -- 0 / 1 (boolean)
    message         TEXT,
    eol_date        TEXT,                             -- ISO date or NULL
    migration_url   TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dep_events_project_module
    ON deprecation_events(project_id, module_id, detected_at);

CREATE TABLE IF NOT EXISTS module_deprecation_seen (
    project_id      TEXT NOT NULL,
    module_id       TEXT NOT NULL,
    first_seen_at   INTEGER NOT NULL,                 -- unix epoch millis
    PRIMARY KEY (project_id, module_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
