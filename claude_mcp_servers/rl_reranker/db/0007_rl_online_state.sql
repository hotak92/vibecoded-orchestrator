-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
--
-- v0.2.106 — per-project online learning + serving gate of the RL reranker
-- (owner ruling 2026-11-12: per-project online adaptation, opt-in; a
-- project's model is served only while it beats plain cosine on that
-- project's own held-out queries: NDCG@10 >= +2% AND the cited node's MRR@10
-- not below cosine's).
--
-- Written by the container after every online round
-- (rl_server.RLServer._push_rl_online_state); read by the launcher's RL panel
-- (RlRerankerOnlineState.svelte). One row per project (single-column key:
-- the hub resolves ONE primary-key column), for the embedding profile the
-- container is pinned to (`embedding_source`).

CREATE TABLE IF NOT EXISTS rl_online_state (
    project_id          TEXT    NOT NULL PRIMARY KEY,
    embedding_source    TEXT    NOT NULL DEFAULT '',  -- "qwen3" / "arctic" / ...
    refreshed_at        INTEGER NOT NULL DEFAULT 0,   -- unix s of the last round
    serving             INTEGER NOT NULL DEFAULT 0,   -- 1 = the project's model reranks
    reason              TEXT    NOT NULL DEFAULT '',  -- why it serves / does not
    training_enabled    INTEGER NOT NULL DEFAULT 1,   -- 0 = online training off for the project
    events_seen         INTEGER NOT NULL DEFAULT 0,   -- citation events received
    queries_trained     INTEGER NOT NULL DEFAULT 0,
    train_steps         INTEGER NOT NULL DEFAULT 0,
    rounds              INTEGER NOT NULL DEFAULT 0,
    holdout_queries     INTEGER NOT NULL DEFAULT 0,   -- queries in the held-out window
    holdout_target      INTEGER NOT NULL DEFAULT 0,   -- window size the gate needs
    serving_turned_off  INTEGER NOT NULL DEFAULT 0,   -- times the gate stopped serving
    last_eval_at        INTEGER NOT NULL DEFAULT 0,   -- unix s
    ndcg_gain           REAL,                         -- relative NDCG@10 gain over cosine
    ndcg                REAL,
    cos_ndcg            REAL,
    mrr                 REAL,                         -- MRR@10 of the cited node
    cos_mrr             REAL,
    cited_queries       INTEGER,                      -- held-out queries that cite a node
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
