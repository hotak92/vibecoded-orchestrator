-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 050 (v0.2.105, lane "counts"): cached Weaviate class counts.
--
-- WHAT IT ANSWERS: "how many objects does this collection hold, and when
-- was that last measured?" — WITHOUT querying Weaviate.
--
-- WHY A CACHE AT ALL: Weaviate (1.28 and earlier) loads a collection into
-- RAM the first time anything queries it and never unloads it. The
-- launcher's KG and /codegraph pages used to run one GraphQL
-- `Aggregate { <Class> { meta { count } } }` per class on the machine for
-- every render, which loaded EVERY project's KG and code graph into
-- Weaviate's memory even though a project only ever reads its own
-- collections plus the shared one. Counts for out-of-scope collections
-- are now served from this table; a live Aggregate runs only for the
-- acting project's own + shared + granted collections, or when the user
-- presses "Refresh counts" (which counts everything once and refills
-- this cache).
--
-- `count` is the last measured object count; `counted_at` is when it was
-- measured (epoch ms). "Row absent" means never counted — the UI shows
-- "—". Invalidation (a KG sync, a code-graph build, an orphan-cleanup
-- delete) removes the row, so the next view says "needs recount" instead
-- of showing a number the launcher knows is stale.
--
-- Additive: a new table, no existing row touched. Rows are keyed by the
-- Weaviate class name and are machine-global (a collection is ONE object
-- on the machine), so there is no project FK and no cascade.

CREATE TABLE IF NOT EXISTS collection_count_cache (
    collection  TEXT PRIMARY KEY,
    count       INTEGER NOT NULL CHECK (count >= 0),
    counted_at  INTEGER NOT NULL
);
