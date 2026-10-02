// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 F-W2-04: the overlay's failed state follows `upd.failed`, not
// `!!upd.error`.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { progressTick } from './update-progress-phase';

const base = { updating: false, failed: false, handover: false, prevUpdating: false };

describe('progressTick', () => {
  it('failed wins over everything', () => {
    expect(progressTick({ ...base, failed: true, updating: true, prevUpdating: true })).toBe('failed');
  });
  it('rising/steady edge runs; falling edge completes or hands over', () => {
    expect(progressTick({ ...base, updating: true })).toBe('running');
    expect(progressTick({ ...base, prevUpdating: true })).toBe('completed');
    expect(progressTick({ ...base, prevUpdating: true, handover: true })).toBe('handover');
    expect(progressTick(base)).toBe('idle');
  });
  it('the modal feeds `failed` from upd.failed, never from upd.error', () => {
    const src = readFileSync(
      fileURLToPath(new URL('./OrchestratorUpdateProgressModal.svelte', import.meta.url)),
      'utf8',
    );
    expect(src).toContain('failed: upd.failed');
    expect(src).not.toMatch(/!!\s*upd\.error/);
  });
});
