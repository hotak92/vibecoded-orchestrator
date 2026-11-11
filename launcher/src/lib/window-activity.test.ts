// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106: an idle launcher must not keep the WebView busy.
//
// Contract under test:
//   - `pollWhileActive` ticks while the window is active, skips every tick
//     while it is minimised or hidden, and runs ONCE when it is shown again;
//   - the change poller (5 s, always on) and the restart banner's poll go
//     through it — while minimised no `poll_changes` IPC is made;
//   - while inactive `<html data-window-inactive>` is set and app.css pauses
//     every animation under it;
//   - the Home page's aurora carries no infinite animation (it re-blurred the
//     glass cards above it every frame).
//
// RED-PROOF: on the pre-fix tree `window-activity.ts` does not exist (every
// test here fails to import), `changes.ts` uses a bare `setInterval`
// (the minimised-poller test counts 12 polls instead of 0), and the Home page
// CSS compiles with `aurora-drift … infinite`.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { compile } from 'svelte/compiler';

const here = dirname(fileURLToPath(import.meta.url));

describe('createWindowActivity / pollWhileActive', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('ticks while active, skips while minimised, catches up once on restore', async () => {
    const { createWindowActivity } = await import('./window-activity');
    const a = createWindowActivity();
    const fn = vi.fn();
    const stop = a.pollWhileActive(fn, 1000);

    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(3);

    a.update({ minimized: true });
    expect(a.isActive()).toBe(false);
    vi.advanceTimersByTime(60_000);
    expect(fn).toHaveBeenCalledTimes(3);

    a.update({ minimized: false });
    expect(fn).toHaveBeenCalledTimes(4); // the one catch-up call
    vi.advanceTimersByTime(1000);
    expect(fn).toHaveBeenCalledTimes(5);

    stop();
    vi.advanceTimersByTime(10_000);
    expect(fn).toHaveBeenCalledTimes(5);
  });

  it('a hidden document is inactive too, and needs both inputs clear', async () => {
    const { createWindowActivity } = await import('./window-activity');
    const a = createWindowActivity();
    const seen: boolean[] = [];
    a.subscribe((v) => seen.push(v));
    a.update({ hidden: true });
    a.update({ minimized: true });
    a.update({ hidden: false }); // still minimised
    expect(a.isActive()).toBe(false);
    a.update({ minimized: false });
    expect(seen).toEqual([false, true]);
  });

  it('no catch-up call when nothing was skipped', async () => {
    const { createWindowActivity } = await import('./window-activity');
    const a = createWindowActivity();
    const fn = vi.fn();
    a.pollWhileActive(fn, 1000);
    a.update({ minimized: true });
    a.update({ minimized: false });
    expect(fn).not.toHaveBeenCalled();
  });

  it('a throwing or rejecting poll does not stop the timer', async () => {
    const { createWindowActivity } = await import('./window-activity');
    const a = createWindowActivity();
    let n = 0;
    a.pollWhileActive(() => {
      n += 1;
      if (n === 1) throw new Error('boom');
      return Promise.reject(new Error('later'));
    }, 1000);
    vi.advanceTimersByTime(3000);
    expect(n).toBe(3);
  });

  it('applyInactiveAttr sets and clears the attribute app.css keys on', async () => {
    const { applyInactiveAttr, INACTIVE_ATTR } = await import('./window-activity');
    const attrs = new Map<string, string>();
    const el = {
      setAttribute: (k: string, v: string) => attrs.set(k, v),
      removeAttribute: (k: string) => attrs.delete(k),
    } as unknown as Element;
    applyInactiveAttr(el, false);
    expect(attrs.has(INACTIVE_ATTR)).toBe(true);
    applyInactiveAttr(el, true);
    expect(attrs.has(INACTIVE_ATTR)).toBe(false);
  });
});

describe('the change poller honours the window state', () => {
  let polls = 0;

  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    polls = 0;
    vi.doMock('$lib/tauri', () => ({
      safeInvoke: async (cmd: string) => {
        if (cmd === 'poll_changes') polls += 1;
        if (cmd === 'current_change_seq') return 0;
        return [];
      },
      tauriAvailable: () => true,
    }));
  });

  afterEach(() => {
    vi.doUnmock('$lib/tauri');
    vi.useRealTimers();
  });

  it('makes no poll_changes call while the window is minimised', async () => {
    const wa = await import('./window-activity');
    const changes = await import('./stores/changes');
    await changes.startChangePoller();

    await vi.advanceTimersByTimeAsync(10_000);
    const whileActive = polls;
    expect(whileActive).toBe(2);

    wa.setWindowState({ minimized: true });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(polls).toBe(whileActive);

    wa.setWindowState({ minimized: false });
    await vi.advanceTimersByTimeAsync(0);
    expect(polls).toBe(whileActive + 1);

    changes.stopChangePoller();
  });
});

describe('CSS: nothing animates for an idle window', () => {
  it('app.css pauses every animation under data-window-inactive', async () => {
    const { INACTIVE_ATTR } = await import('./window-activity');
    const css = readFileSync(resolve(here, '../app.css'), 'utf8');
    const rule = new RegExp(
      `:root\\[${INACTIVE_ATTR}\\] \\*[^{]*\\{[^}]*animation-play-state:\\s*paused\\s*!important`,
    );
    expect(css).toMatch(rule);
  });

  it('the Home page aurora has no infinite animation', () => {
    const src = readFileSync(resolve(here, '../routes/+page.svelte'), 'utf8');
    const out = compile(src, { filename: '+page.svelte', css: 'external' });
    const css = out.css?.code ?? '';
    expect(css).toContain('aurora-subtle');
    const auroraRules = css
      .split('}')
      .filter((r) => r.includes('aurora'));
    for (const r of auroraRules) {
      expect(r).not.toMatch(/animation[^;]*infinite/);
    }
  });
});
