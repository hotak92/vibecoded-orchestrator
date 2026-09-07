-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 044 (v0.2.92, WP-17 / W3): the project-move ledger.
--
-- WHAT IT ANSWERS: "is a move of this project in flight, and if the process
-- died, how far did it get?".
--
-- ── WHY A TABLE AND NOT JUST A SENTINEL FILE ───────────────────────────────
-- The move already writes a sentinel under BOTH folders. A file cannot do two
-- things this row does:
--
--   1. SINGLE-FLIGHT. Two concurrent moves of the same project — a CLI run and
--      a GUI click, or two terminals — must not both copy into two different
--      destinations and then race to flip `folder_path`. `UNIQUE(project_id)
--      WHERE status IN ('running','flipped')` makes the second claim fail in
--      SQLite rather than in application logic, so there is no check-then-act
--      window. It is a PARTIAL unique index precisely so history rows
--      (completed / failed / aborted) accumulate freely.
--   2. SURVIVE THE FOLDER. The sentinel lives inside a folder; a move whose
--      destination was never created, or whose source has been deleted since,
--      leaves no readable sentinel anywhere. The row does, and the launcher's
--      boot sweep reads it.
--
-- ── THE STATUS MACHINE (the whole failure-semantics contract) ──────────────
--   running   — claimed. Files may be being copied INTO the destination.
--               NOTHING in `projects` has changed; the project still lives at
--               `src` and still works. A crash here is safe: the destination
--               holds only ADDED files (the engine never overwrites), and the
--               row is what tells the next boot to say so.
--   flipped   — the commit transaction succeeded: `projects.folder_path` is
--               `dst`, the dependent path columns were re-pointed and a
--               code-graph rebuild was queued, ALL in one transaction. The
--               project lives at `dst` and works. What may still be owed is
--               the post-commit reconciliation (env re-projection, kg-sync
--               parity, the ledger entries) — idempotent and re-runnable with
--               `vco project move --verify`.
--   completed — reconciliation finished too. Nothing is owed.
--   failed    — the move aborted before the flip. `error` says why. The
--               project never moved.
--
-- There is deliberately NO 'rolling_back'. Nothing is rolled back: the engine
-- performs zero deletions and zero overwrites, so "undoing" a refused move
-- means leaving the added files where they are and telling the user, which is
-- a report rather than a state.
--
-- ── WHY THE ROW IS NEVER DELETED ───────────────────────────────────────────
-- A completed move's row is the only durable record that this project used to
-- live somewhere else. The ledger entry naming the old folder is dismissible;
-- the audit row is one line among thousands. Keeping the history costs a few
-- rows per project per lifetime and answers "where did this come from?" long
-- after the sentinel is gone. Rows die only with their project, via the FK
-- cascade — the same discipline every other per-project table uses.
--
-- Plain CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS: idempotent by
-- construction AND by the runner's version check, and NOT self-transactional
-- (no table rebuild, no foreign_keys pragma), so the runner's outer
-- transaction wraps it. LAUNCHER_DB_TABLE_SET_VERSION bumps 43->44 atomically
-- with this migration (B-2).

CREATE TABLE IF NOT EXISTS project_moves (
    id           TEXT PRIMARY KEY,              -- caller-supplied move id (uuid)
    project_id   TEXT NOT NULL,                 -- references projects.id
    src          TEXT NOT NULL,                 -- folder_path as it was at claim time
    dst          TEXT NOT NULL,                 -- requested destination
    status       TEXT NOT NULL
                 CHECK (status IN ('running','flipped','completed','failed')),
    error        TEXT,                          -- populated on 'failed' only
    started_at   INTEGER NOT NULL,              -- ms since epoch
    flipped_at   INTEGER,                       -- ms; set by the commit txn
    finished_at  INTEGER,                       -- ms; terminal states
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- THE single-flight gate. Partial index: at most one live move per project,
-- enforced by SQLite. History rows are exempt because they carry a terminal
-- status.
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_moves_live
    ON project_moves(project_id)
    WHERE status IN ('running', 'flipped');

-- Boot sweep + history reads: "what happened to this project, newest first".
CREATE INDEX IF NOT EXISTS idx_project_moves_project_started
    ON project_moves(project_id, started_at DESC);
