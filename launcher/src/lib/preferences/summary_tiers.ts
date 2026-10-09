// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.102 W5-D: the local summary-model tiers as the backend reports them.
//
// `get_summary_model_info` runs `python -m vco_lib.summary_model --json`,
// whose `tiers` come from the ONE tier table
// (`vco_lib.embedding_selection.summary_model_tiers` — the same thresholds
// install's pull list and the runtime resolver use). The Preferences page's
// "No Ollama models installed" hint renders them, so it can never name a
// different set of models than the machine would pull — no literal list here.

/** One local generation tier: the model and when a machine uses it. */
export interface SummaryTier {
  model: string;
  when: string;
}

/** The `tiers` of a `get_summary_model_info` payload, validated. Anything
 *  malformed (null on a failed soft read, a wrong shape) yields `[]` — the
 *  page then shows the hint without a model list rather than a guess. */
export function parseSummaryTiers(info: unknown): SummaryTier[] {
  if (!info || typeof info !== 'object') return [];
  const tiers = (info as { tiers?: unknown }).tiers;
  if (!Array.isArray(tiers)) return [];
  return tiers.filter(
    (t): t is SummaryTier =>
      !!t &&
      typeof t === 'object' &&
      typeof (t as SummaryTier).model === 'string' &&
      (t as SummaryTier).model.length > 0 &&
      typeof (t as SummaryTier).when === 'string',
  );
}
