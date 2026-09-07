// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Form copy for a secret INPUT whose current value must not be shown.
//
// A password field can only say three honest things about what is already
// stored, and they map onto the three `StorePresence` states:
//
//   present  → there is a value; leaving the box blank keeps it
//   absent   → there is none; the box is how you supply one
//   unknown  → we could not read the store
//
// The third one is the reason this module exists. `coordination_get_config`
// used to return a BOOLEAN, collapsing a locked or erroring keychain into
// `false`, and the page rendered that as "Required." — a confident absence
// produced by a check that could not run. The user's response to "Required."
// is to paste the secret again, overwriting an entry that was fine, so the
// failure is not merely cosmetic. See
// `knowledge/concepts/a-check-that-could-not-run-reads-as-absence-2026-09-03.md`.
//
// This is presentation for the SAME `StorePresence` the secrets panel's
// `badgeOf` renders — a second WORDING of one model, not a second model.
// The distinction that matters is preserved in both: "we could not look"
// never renders as "it is not there".

import type { StorePresence } from '$lib/stores/secrets';

/** Placeholder for the input box.
 *
 * `emptyPlaceholder` is the caller's own wording for the absent case
 * ("paste service key" for a required field, "optional" for an optional
 * one) — the presence vocabulary is shared, the field's tone is not. */
export function keyPlaceholder(
  presence: StorePresence | undefined,
  emptyPlaceholder: string,
): string {
  if (presence === 'present') return '•••••• (already set)';
  if (presence === 'unknown') return 'leave blank unless you mean to replace it';
  // `undefined` (config not loaded yet) is treated as unknown, NOT as
  // absent: before the round-trip lands we know nothing either way.
  if (presence === undefined) return 'leave blank unless you mean to replace it';
  return emptyPlaceholder;
}

/** Hint line under the input. */
export function keyHint(
  presence: StorePresence | undefined,
  emptyHint: string,
): string {
  if (presence === 'present') return 'Stored in keychain. Leave blank to keep.';
  if (presence === 'unknown' || presence === undefined) {
    return (
      'Could not read the keychain (locked, or the daemon did not answer), ' +
      'so we cannot tell whether a value is stored. This is NOT the same as ' +
      '"not set" — unlock your login keychain and reload before re-entering ' +
      'a value, or you may overwrite a working one.'
    );
  }
  return emptyHint;
}
