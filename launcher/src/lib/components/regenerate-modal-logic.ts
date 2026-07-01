// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.71 Track T-C-modal — pure decision helpers for the
// RegenerateOrDeferModal's model-switch "keep previous model" option.
//
// Extracted into a dependency-free `.ts` module so the smart-default
// selection is unit-testable under the existing node/vitest setup (which
// deliberately does NOT mount Svelte components — see vitest.config.ts).
// The modal imports these; tests exercise them directly.

/** One slot's populated-vector count (mirrors the Rust/Python shape). */
export type SlotPopulatedCount = {
  /** Weaviate named-vector slot, e.g. `qwen3_embed`. */
  slot: string;
  /** User-selectable embedding profile this slot maps to, e.g. `qwen3`. */
  profile: string;
  /** Objects with a NON-EMPTY vector in this slot. */
  populated: number;
};

/** Model-switch context surfaced to the modal when a switch is detected. */
export type ModelSwitchContext = {
  /** The model/profile the user (or update) just switched TO. */
  newProfile: string;
  /** Per-slot populated counts for the project's KG collection. */
  slotCounts: SlotPopulatedCount[];
  /** Smart-default: the profile with the MOST populated objects. */
  mostPopulatedProfile: string | null;
  /** Aggregate total objects in the collection (the "N" denominator). */
  total: number;
};

/**
 * Pick the slot row to PROPOSE for "keep previous model".
 *
 * Resolution order:
 *   1. The row whose profile == `mostPopulatedProfile` (the count probe's
 *      smart default), when present in `slotCounts`.
 *   2. Fallback: the row with the highest `populated` count (first max wins
 *      — `reduce` keeps the earliest catalog-order entry on ties).
 *   3. `null` when `modelSwitch` is null/has no slots (no previous model to
 *      revert to → the modal hides the "keep previous" button).
 */
export function pickKeepCandidate(
  modelSwitch: ModelSwitchContext | null,
): SlotPopulatedCount | null {
  if (!modelSwitch) return null;
  const slots = modelSwitch.slotCounts ?? [];
  if (slots.length === 0) return null;
  if (modelSwitch.mostPopulatedProfile) {
    const byProfile = slots.find(
      (s) => s.profile === modelSwitch.mostPopulatedProfile,
    );
    if (byProfile) return byProfile;
  }
  return slots.reduce((best, s) => (s.populated > best.populated ? s : best));
}

/**
 * Whether the modal should render the model-switch THREE-option panel.
 * True iff a model-switch context is present (even with zero populated
 * slots — in that case the panel shows Regenerate + Defer only, with an
 * explanatory note that no previous model exists to keep).
 */
export function showsModelSwitchPanel(
  modelSwitch: ModelSwitchContext | null,
): boolean {
  return modelSwitch !== null;
}

/**
 * Whether the "Keep previous model" button (the THIRD option) is offered.
 * Only when there's an actual populated slot to revert to.
 */
export function offersKeepPrevious(
  modelSwitch: ModelSwitchContext | null,
): boolean {
  return pickKeepCandidate(modelSwitch) !== null;
}
