-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
--
-- v0.2.106 — the serving gate judges the project's CUMULATIVE PREQUENTIAL
-- record (owner ruling 2026-11-12, after the rlgate2 study): every query is
-- scored by the model in place when it arrived, before that model trained on
-- it; once the record holds `min_queries` queries and `min_cited_queries`
-- citing ones, every round is a check, the k-th at confidence
-- 1 - error_budget / (k (k+1)) (total false-pass probability <= error_budget
-- over all checks), and the model is served while both lower bounds clear
-- the bar (NDCG@10 gain over plain cosine >= +2%, cited-node MRR@10
-- difference >= 0). The held-out window is gone, so are its two columns.
--
-- Meaning of the earlier columns from this migration on:
--   ndcg / cos_ndcg / mrr / cos_mrr / ndcg_gain / cited_queries (0007) —
--     point estimates over the WHOLE prequential record;
--   last_eval_at (0007) — unix s of the last check;
--   ndcg_gain_lb / mrr_diff_lb / gate_confidence (0008) — the last check's
--     lower bounds and its confidence (1 - error_budget / (k (k+1))).
-- Written by rl_server.RLServer._push_rl_online_state; read by the
-- launcher's RL panel (launcher/src/lib/rl-online-state.ts).

ALTER TABLE rl_online_state DROP COLUMN holdout_queries;
ALTER TABLE rl_online_state DROP COLUMN holdout_target;
ALTER TABLE rl_online_state ADD COLUMN record_queries INTEGER;        -- queries in the prequential record
ALTER TABLE rl_online_state ADD COLUMN min_queries INTEGER;           -- record size before any verdict
ALTER TABLE rl_online_state ADD COLUMN min_cited_queries INTEGER;     -- citing queries before any verdict
ALTER TABLE rl_online_state ADD COLUMN checks INTEGER;                -- k: checks made on the current model
ALTER TABLE rl_online_state ADD COLUMN error_budget REAL;             -- total false-pass probability (0.10)
ALTER TABLE rl_online_state ADD COLUMN served_queries INTEGER;        -- record queries answered by the model
ALTER TABLE rl_online_state ADD COLUMN served_cited_queries INTEGER;  -- ... of them citing a node
ALTER TABLE rl_online_state ADD COLUMN served_ndcg_gain REAL;         -- NDCG@10 gain over cosine on the served searches
ALTER TABLE rl_online_state ADD COLUMN served_mrr REAL;               -- cited-node MRR@10 on the served searches
ALTER TABLE rl_online_state ADD COLUMN served_cos_mrr REAL;           -- plain cosine's on the same searches
