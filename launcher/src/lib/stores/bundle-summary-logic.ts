// Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
/**
 * Pure decision logic for the bundle-update toast.
 *
 * Extracted from `stores/projects.ts` so it can be tested without a Tauri
 * runtime or a toast host — the same `*-logic.ts` + `*-logic.test.ts` shape the
 * rest of this codebase uses.
 *
 * It exists because the inline version had two honesty defects, both caused by
 * ignoring `adopted`:
 *
 *  1. The "already up to date" branch tested `created/overwritten/
 *     always_overwritten/preserved/errors` but NOT `adopted` — so an update
 *     that backed up and REPLACED every file the user had edited reported
 *     "Project bundle already up to date."
 *  2. The summary line never mentioned adoptions at all.
 *
 * The Rust `UpdateSummary` has always carried the field, and its own comment
 * says it exists precisely so an adoption-only update cannot toast
 * "0 preserved / 0 changed — dishonest-by-omission". The backend computed the
 * honest number and the frontend dropped it at the type boundary.
 */
import type { UpdateSummary } from '$lib/types/launcher';

export type BundleToastKind = 'up-to-date' | 'success' | 'error';

export interface BundleToastDecision {
  kind: BundleToastKind;
  /** Summary clause list, empty for `up-to-date`. */
  line: string;
}

/**
 * Buckets that count files the run left EXACTLY as it found them. These are
 * the only reasons a non-zero tally does not mean "something happened", and
 * the list is closed by the meaning of the words, not by what shipped:
 *
 *  - `noop`             — installed content already matched what we'd write;
 *  - `skipped_existing` — first-install only: the file pre-existed and was
 *                         left alone (always 0 on an update).
 *
 * `preserved` is deliberately NOT here: the user's bytes survived, but the
 * run tried to replace them and could not, which is a fact they need told.
 *
 * The Rust struct exposes no "total changed" field to negate, so this is the
 * `m3` alternative the review asked for — the disjunction is derived from the
 * payload's own keys, minus this denylist.
 */
const UNCHANGED_BUCKETS = new Set<keyof UpdateSummary>(['noop', 'skipped_existing']);

/**
 * Did this run touch anything at all?
 *
 * Derived from the payload's KEYS rather than a hand-listed disjunction. The
 * hand-listed form is what dropped `adopted` — an update that backed up and
 * replaced every file the user had edited reported "already up to date",
 * because the list was written before `adopted` existed and nothing made
 * adding a bucket to the Rust struct also add it here. Any NEW tally bucket is
 * now included automatically; only a bucket that genuinely means "nothing
 * changed" has to be named, and that set is small, stable and justified above.
 *
 * Non-numeric fields are skipped: `kg_or_docs_content_changed` is a DERIVED
 * boolean (true only when a change-causing bucket is already non-zero), so
 * counting it would be double-counting, and a future boolean flag is a
 * different question than "how many files did this touch".
 */
export function bundleTouchedSomething(s: UpdateSummary): boolean {
  return Object.entries(s).some(
    ([key, value]) =>
      typeof value === 'number' &&
      value > 0 &&
      !UNCHANGED_BUCKETS.has(key as keyof UpdateSummary),
  );
}

export function decideBundleToast(s: UpdateSummary): BundleToastDecision {
  if (!bundleTouchedSomething(s)) {
    return { kind: 'up-to-date', line: '' };
  }

  const parts: string[] = [];
  if (s.created > 0) parts.push(`${s.created} created`);
  if (s.overwritten > 0) parts.push(`${s.overwritten} updated`);
  if (s.always_overwritten > 0) parts.push(`${s.always_overwritten} always-updated`);
  // The COMMON outcome for a file the user edited: backed up, then replaced.
  // Named in the user's terms — "your edits" — because the consequence is
  // theirs, and silence here reads as "nothing of mine was touched".
  if (s.adopted > 0) parts.push(`${s.adopted} of your edits replaced (backup kept)`);
  // The RARE fallback: the backup could not be written, so their copy stands.
  if (s.preserved > 0) parts.push(`${s.preserved} of your edits kept (backup failed)`);
  if (s.errors_count > 0) parts.push(`${s.errors_count} errors`);

  return {
    kind: s.errors_count > 0 ? 'error' : 'success',
    line: parts.length > 0 ? parts.join(', ') : 'no changes',
  };
}
