// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (P2-M6) — the commit decision for `NumberInputControl`.
//
// Pre-fix, `commit()` collapsed two very different inputs onto one path:
//
//   * the user CLEARED the field  → fall back to the declared default,
//   * the user typed something unparseable → ALSO fall back to the
//     declared default,
//
// and both then ran through `commitValue`, which persists and toasts
// "Saved <label>". The second case tells the user their value was saved
// while a different value was — the exact shape of a dishonest control.
//
// Split here as a pure decision so the branch is unit-testable under the
// node/vitest setup (which doesn't mount Svelte components), and so the
// component has no second copy of the rule.

/** The min/max/default triple the manifest declares for the control. */
export interface NumberBounds {
  min?: number | null;
  max?: number | null;
  default?: number | null;
}

export type NumberCommitDecision =
  /** Persist `value` and reflect `display` back into the input. */
  | { action: 'persist'; value: number; display: string }
  /** Refuse: show `message` inline, persist nothing, toast nothing. */
  | { action: 'reject'; message: string };

/** Clamp into `[min, max]` when either bound is declared. */
export function clampToBounds(n: number, bounds: NumberBounds): number {
  let result = n;
  if (bounds.min !== null && bounds.min !== undefined && result < bounds.min) {
    result = bounds.min;
  }
  if (bounds.max !== null && bounds.max !== undefined && result > bounds.max) {
    result = bounds.max;
  }
  return result;
}

/**
 * Decide what a commit (blur / Enter) should do.
 *
 * @param raw       The input's current value. Typed `unknown` because a
 *                  native `<input type="number">` binding can hand back a
 *                  number, an empty string, or `undefined`.
 * @param bounds    The control's declared min / max / default.
 * @param badInput  `input.validity.badInput` at commit time. A native
 *                  number input reports garbage ("abc", "1..2") as an
 *                  EMPTY value with this flag set — without it, garbage
 *                  is indistinguishable from a deliberate clear, and the
 *                  silent default-substitution comes straight back.
 *
 * Rules, in order:
 *   1. `badInput` → reject. The field holds text the browser could not
 *      parse; nothing is saved.
 *   2. Empty (after trim) → persist the declared default (or 0). This is
 *      the legitimate "clear the field" gesture and keeps the
 *      pre-v0.2.91 behaviour for it.
 *   3. Non-empty but unparseable (including a lone "-" or ".") → reject.
 *   4. Otherwise persist the clamped number.
 */
export function decideNumberCommit(
  raw: unknown,
  bounds: NumberBounds,
  badInput = false,
): NumberCommitDecision {
  const text = raw === null || raw === undefined ? '' : String(raw);
  const trimmed = text.trim();

  if (badInput) {
    return {
      action: 'reject',
      message: 'Not a number — nothing was saved. Enter a numeric value.',
    };
  }

  if (trimmed === '') {
    const fallback = bounds.default ?? 0;
    return { action: 'persist', value: fallback, display: String(fallback) };
  }

  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) {
    return {
      action: 'reject',
      message: `“${trimmed}” is not a number — nothing was saved.`,
    };
  }

  const clamped = clampToBounds(parsed, bounds);
  return { action: 'persist', value: clamped, display: String(clamped) };
}
