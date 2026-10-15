// SPDX-License-Identifier: AGPL-3.0-or-later
//
// BEHAVIOURAL test of the v0.2.103 toast dwell policy's renderer half.
//
// Replaces the former Svelte-AST hover-wiring scan (review MEDIUM-2: an
// AST walk is still a source scan — a handler that called
// `toast.setHovered(t.id, false)` on both events passed it). This file
// mounts the REAL `Toast.svelte` under jsdom and drives the actual
// DOM events: hovering a toast pauses its dismissal, leaving re-arms
// it, keyboard focus/blur pause it the same way (WCAG 2.2.1, review
// LOW-5), and the × button dismisses through the store. Timing and
// behaviour only — the component is unrestyled.

// @vitest-environment jsdom

import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import Toast from './Toast.svelte';
import { toast } from '$lib/stores/toast';

let app: ReturnType<typeof mount> | null = null;

async function mountToasts() {
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(Toast, { target });
  await tick();
}

/** jsdom has no PointerEvent; a plain Event with the right type reaches
 *  the element's listener (pointerenter/leave do not bubble, so Svelte
 *  attaches them directly to the element — no delegation involved). */
function fire(el: Element, type: string) {
  const bubbles = type !== 'pointerenter' && type !== 'pointerleave';
  el.dispatchEvent(new Event(type, { bubbles }));
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  // Drain the store so one test's toasts never leak into the next.
  toast.subscribe((list) => {
    for (const t of list) toast.dismiss(t.id);
  })();
  vi.useRealTimers();
});

describe('Toast.svelte — the renderer half of the dwell policy', () => {
  it('hovering pauses dismissal; pointerleave re-arms it', async () => {
    vi.useFakeTimers();
    await mountToasts();

    toast.success('Saved'); // short message → minimum 8 s dwell
    await tick();
    const el = document.querySelector('.toast');
    expect(el, 'toast rendered').toBeTruthy();

    fire(el!, 'pointerenter');
    vi.advanceTimersByTime(60_000); // far past any dwell
    await tick(); // let a (wrongful) removal flush into the DOM
    expect(
      document.querySelector('.toast'),
      'a hovered toast must NOT be dismissed',
    ).toBeTruthy();

    fire(el!, 'pointerleave');
    vi.advanceTimersByTime(8_000); // re-armed full dwell
    await tick();
    expect(
      document.querySelector('.toast'),
      'leaving re-arms the dismissal',
    ).toBeNull();
  });

  it('keyboard focus pauses dismissal; blur re-arms it', async () => {
    vi.useFakeTimers();
    await mountToasts();

    toast.error('boom'); // 15 s minimum dwell
    await tick();
    const button = document.querySelector('.toast .toast-x');
    expect(button, 'dismiss button rendered').toBeTruthy();

    // Tabbing to the × button: focusin bubbles from the button to the
    // toast element's onfocusin.
    fire(button!, 'focusin');
    vi.advanceTimersByTime(60_000);
    await tick(); // let a (wrongful) removal flush into the DOM
    expect(
      document.querySelector('.toast'),
      'a focused toast must NOT be dismissed',
    ).toBeTruthy();

    fire(button!, 'focusout');
    vi.advanceTimersByTime(15_000);
    await tick();
    expect(
      document.querySelector('.toast'),
      'blurring re-arms the dismissal',
    ).toBeNull();
  });

  it('the × button dismisses through the store', async () => {
    vi.useFakeTimers();
    await mountToasts();

    toast.info('stays until clicked');
    await tick();
    expect(document.querySelector('.toast')).toBeTruthy();

    fire(document.querySelector('.toast .toast-x')!, 'click');
    await tick();
    expect(
      document.querySelector('.toast'),
      'clicking × removes the toast immediately',
    ).toBeNull();
  });
});
