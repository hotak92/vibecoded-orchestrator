-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
--
-- v0.2.106 — the serving gate judges CONFIDENCE BOUNDS (owner ruling
-- 2026-11-12): a project's model is served only when a paired bootstrap over
-- its held-out window says, at `gate_confidence` (one-sided), that the
-- NDCG@10 gain over plain cosine is >= +2% AND the cited node's MRR@10 minus
-- cosine's is >= 0. The point estimates stay in the 0007 columns; these are
-- the bounds the decision used (NULL = undefined, a failing bound).
-- Written by rl_server.RLServer._push_rl_online_state; read by the
-- launcher's RL panel (launcher/src/lib/rl-online-state.ts).

ALTER TABLE rl_online_state ADD COLUMN ndcg_gain_lb REAL;     -- lower bound of the relative NDCG@10 gain
ALTER TABLE rl_online_state ADD COLUMN mrr_diff_lb REAL;      -- lower bound of MRR@10(model) - MRR@10(cosine), citing queries
ALTER TABLE rl_online_state ADD COLUMN gate_confidence REAL;  -- one-sided confidence of both bounds (0.90)
