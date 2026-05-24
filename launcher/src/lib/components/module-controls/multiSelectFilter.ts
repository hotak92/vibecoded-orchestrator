// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.32 L6: pure-function filter evaluator for `multi_select`'s
// optional `filter` predicate. Extracted from `ModuleConfigTab.svelte`
// so it can be exercised by a future TS test runner (vitest, jest)
// without booting Svelte / DOM — and so the Svelte component stays
// thin and declarative.
//
// The runtime-value resolver lives in Rust (`module_get_runtime_value`
// Tauri command). The renderer calls that command on mount, stuffs
// the results into a `runtimeValues` map keyed by the `equals_runtime`
// identifier, then passes the map to `filterOptions` for synchronous
// evaluation during render.

import type { MultiSelectFilter, SelectOption } from '$lib/types/manifest';

/**
 * Apply a [[MultiSelectFilter]] to a fetched options list.
 *
 * `kind: "match"` keeps only options whose `meta.<meta_field>` equals
 * the resolved runtime value referenced by `equals_runtime`. Unknown
 * filter kinds fall through to "no filtering" (defensive — future
 * variants land additively without breaking older renderers).
 *
 * **Fail-open posture** (renderer-side L6 spec choice):
 * - `filter` is null/undefined ⇒ return all options.
 * - `runtimeValues[filter.equals_runtime]` is missing OR empty string
 *   ⇒ return all options. (The Rust resolver returns "" for unknown
 *    `equals_runtime` identifiers precisely so the renderer falls
 *    back to "show everything" rather than "hide everything".)
 * - An individual option has no `meta` OR `meta[meta_field]` is
 *   missing / wrong type ⇒ HIDE that option (we don't know it
 *   matches the runtime).
 *
 * The fail-open at the filter level (rather than at the option level)
 * mirrors the UX bias: users would rather see too many options than
 * see zero options because of a transient runtime-lookup failure.
 */
export function filterOptions(
  opts: SelectOption[],
  filter: MultiSelectFilter | null | undefined,
  runtimeValues: Record<string, string>,
): SelectOption[] {
  if (!filter) return opts;
  if (filter.kind === 'match') {
    const runtimeVal = runtimeValues[filter.equals_runtime];
    if (runtimeVal === undefined || runtimeVal === '') return opts;
    return opts.filter((opt) => {
      const meta = opt.meta as Record<string, unknown> | null | undefined;
      if (!meta) return false;
      const matchVal = meta[filter.meta_field];
      return typeof matchVal === 'string' && matchVal === runtimeVal;
    });
  }
  // Unknown filter kind — defensive fallback (return all options
  // rather than crashing on a future tag the manifest knows but
  // this renderer doesn't yet).
  return opts;
}

// ─── Test fixtures (consumed when a TS test runner lands) ───────────────
//
// vitest / jest will pick these up if installed; they're harmless
// re-exports otherwise. Each `*_fixture` returns a fresh object so
// tests don't accidentally share mutable state.
//
// Why ship test data with no runner: the brief calls for a "hidden-
// options assertion when filter mismatches runtime" — pinning the
// expected behaviour HERE (in code) makes it trivial to wire up
// vitest later. The runtime resolver (Rust `module_get_runtime_value`)
// already has its own coverage in `module_default_weights.rs`.

/** Three options: two with `meta.embedding_source` matching `"qwen3"`, one with `"openai"`. */
export function multiSelectFilter_test_fixture_options(): SelectOption[] {
  return [
    { value: 'qwen3-v1', label: 'Qwen3 v1', meta: { embedding_source: 'qwen3' } },
    { value: 'qwen3-v2', label: 'Qwen3 v2', meta: { embedding_source: 'qwen3' } },
    { value: 'openai-v1', label: 'OpenAI v1', meta: { embedding_source: 'openai' } },
    // One option without `meta` — should be HIDDEN under a strict-match filter.
    { value: 'no-meta', label: 'Mystery option' },
  ];
}

/** Sanity-check expectations for `filterOptions`. Used by future tests AND
 * by any developer reading this module to understand the contract. */
export const multiSelectFilter_test_fixture_expectations = {
  /** No filter → all options visible (4). */
  no_filter_count: 4,
  /** filter matches "qwen3", runtimeValues sets "container.active_embedding" → 2. */
  match_qwen3_count: 2,
  /** filter matches "openai" → 1. */
  match_openai_count: 1,
  /** Unknown runtime identifier (empty string) → fail-open, all 4. */
  empty_runtime_count: 4,
  /** Unknown filter kind → defensive fallback, all 4. */
  unknown_kind_count: 4,
};
