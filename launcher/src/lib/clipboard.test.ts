// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.105 (L-SMALL) — the ONE clipboard routine.
//
// Both decisions are tested: a write that succeeds reports success and hands
// back `true`; a write that fails (denied, or no clipboard API at all) reports
// failure and hands back `false`. A copy button that silently does nothing is
// the bug this pins out.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

import { copyToClipboard } from './clipboard';
import { toast } from './stores/toast';

function lastToast() {
  const list = get(toast);
  return list[list.length - 1];
}

function drainToasts() {
  for (const t of get(toast)) toast.dismiss(t.id);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  drainToasts();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('copyToClipboard — one home for the clipboard write', () => {
  it('writes the EXACT text and says it copied', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });

    await expect(copyToClipboard('Title\nCondition: x\n\nRemedy:\ncmd'))
      .resolves.toBe(true);

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith('Title\nCondition: x\n\nRemedy:\ncmd');
    expect(lastToast()).toMatchObject({
      kind: 'success',
      message: 'Copied to clipboard',
    });
  });

  it('reports a refused write instead of pretending it copied', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });

    await expect(copyToClipboard('x')).resolves.toBe(false);

    expect(lastToast()).toMatchObject({ kind: 'error', message: 'Copy failed' });
  });

  it('reports a missing clipboard API as a failed copy (leave-alone: no throw)', async () => {
    vi.stubGlobal('navigator', {});

    await expect(copyToClipboard('x')).resolves.toBe(false);

    expect(lastToast()).toMatchObject({ kind: 'error', message: 'Copy failed' });
  });
});