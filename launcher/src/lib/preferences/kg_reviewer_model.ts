// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.103 (owner ruling 2026-10-15): Preferences -> "KG commit reviewer".
//
// ONE machine-wide model for the background post-commit KG reviewer
// (`post-git-commit-kg-sync` -> `.claude/scripts/kg_reviewer_backend.py`).
// The choices and the default are NOT declared here: they come from the one
// rule table `vco_lib/kg_reviewer_config.toml`, served by the Rust command
// `get_kg_reviewer_model` (which also refuses any other value on
// `set_kg_reviewer_model`). This module only maps them to what the user reads.

/** `get_kg_reviewer_model` / `set_kg_reviewer_model` payload. */
export interface KgReviewerModelInfo {
  model: string;
  is_set: boolean;
  default: string;
  choices: string[];
  env_override_key: string;
}

const LABELS: Record<string, string> = {
  haiku: 'Haiku (fastest, cheapest)',
  sonnet: 'Sonnet (balanced)',
  opus: 'Opus (most careful, highest cost)',
};

/** The selector's options, in table order; the default is marked. */
export function kgReviewerModelOptions(
  info: Pick<KgReviewerModelInfo, 'choices' | 'default'>,
): Array<{ value: string; label: string }> {
  return info.choices.map((c) => {
    const base = LABELS[c] ?? c;
    return { value: c, label: c === info.default ? `${base} - default` : base };
  });
}

/** What the user reads under the selector. */
export function kgReviewerModelHint(info: Pick<KgReviewerModelInfo, 'env_override_key'>): string {
  return (
    'After each commit a background reviewer updates the KG nodes under knowledge/ ' +
    '(never docs or code), reverts any edit that breaks its rules, and commits only ' +
    'its own nodes as "kg-sync: <hash> <subject>". This model is used on every ' +
    'project. If it is unavailable, only a comparable gateway model (GLM-5.3, Kimi K3) ' +
    'may stand in - never a flash model - otherwise the review is skipped. ' +
    `${info.env_override_key} in the environment overrides this choice.`
  );
}

/** Confirmation copy after a successful write. */
export function kgReviewerModelToastText(model: string): string {
  return `KG commit reviewer now runs ${model}`;
}
