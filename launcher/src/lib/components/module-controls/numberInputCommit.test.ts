// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (P2-M6) — tests for NumberInputControl's commit decision.
//
// The bug: unparseable input was silently replaced by the control's
// declared default, persisted, and confirmed with a "Saved <label>"
// toast. The user was told their value was saved; a different value was.

import { describe, expect, it } from 'vitest';
import { clampToBounds, decideNumberCommit } from './numberInputCommit';

const BOUNDS = { min: 1, max: 100, default: 42 };

describe('decideNumberCommit — rejects garbage instead of substituting', () => {
  it('rejects non-empty unparseable text', () => {
    const d = decideNumberCommit('abc', BOUNDS);
    expect(d.action).toBe('reject');
    // Pre-fix: { action: 'persist', value: 42 } + a "Saved" toast.
    expect(d).not.toHaveProperty('value');
    if (d.action === 'reject') {
      expect(d.message).toContain('abc');
      expect(d.message).toMatch(/nothing was saved/i);
    }
  });

  it('rejects a native number input reporting badInput', () => {
    // `<input type="number">` surfaces garbage as an EMPTY value with
    // validity.badInput set — without that flag it is indistinguishable
    // from a deliberate clear, and the silent substitution returns.
    const d = decideNumberCommit('', BOUNDS, true);
    expect(d.action).toBe('reject');
  });

  it('rejects partial input that is not yet a number', () => {
    for (const raw of ['-', '.', '1..2', '5e', '- 3']) {
      expect(decideNumberCommit(raw, BOUNDS).action).toBe('reject');
    }
  });
});

describe('decideNumberCommit — legitimate paths are unchanged', () => {
  it('treats a cleared field as "restore the default"', () => {
    const d = decideNumberCommit('', BOUNDS);
    expect(d).toEqual({ action: 'persist', value: 42, display: '42' });
  });

  it('falls back to 0 when the control declares no default', () => {
    expect(decideNumberCommit('  ', { default: null })).toEqual({
      action: 'persist',
      value: 0,
      display: '0',
    });
  });

  it('persists a valid number and echoes it back', () => {
    expect(decideNumberCommit('7', BOUNDS)).toEqual({
      action: 'persist',
      value: 7,
      display: '7',
    });
  });

  it('accepts a number-typed binding, not just a string', () => {
    expect(decideNumberCommit(7, BOUNDS)).toEqual({
      action: 'persist',
      value: 7,
      display: '7',
    });
  });

  it('clamps to the declared bounds', () => {
    expect(decideNumberCommit('0', BOUNDS)).toMatchObject({ value: 1 });
    expect(decideNumberCommit('9999', BOUNDS)).toMatchObject({ value: 100 });
    expect(decideNumberCommit('-5', { min: 0 })).toMatchObject({ value: 0 });
  });

  it('accepts negatives and decimals when the bounds allow them', () => {
    expect(decideNumberCommit('-2.5', { min: -10, max: 10 })).toMatchObject({
      value: -2.5,
    });
  });
});

describe('clampToBounds', () => {
  it('is a no-op when neither bound is declared', () => {
    expect(clampToBounds(1234, {})).toBe(1234);
    expect(clampToBounds(-1234, { min: null, max: null })).toBe(-1234);
  });
});
