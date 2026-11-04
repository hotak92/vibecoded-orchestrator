// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.105 (L-SMALL) — behavioural test of the shared Copy button: the real
// component, mounted under jsdom, clicked for real, asserted on what reaches
// the clipboard. This is the "copied text" half of the two panels' Copy
// affordance; `deferral-ledger.test.ts` pins the text a ledger row hands it.

// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { mount, unmount, tick } from 'svelte';

import CopyButton from './CopyButton.svelte';

let app: ReturnType<typeof mount> | null = null;

function stubClipboard() {
  const calls: string[] = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: (text: string) => {
        calls.push(text);
        return Promise.resolve();
      },
    },
  });
  return calls;
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  // Remove the stub so one test's clipboard never leaks into the next.
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: undefined,
  });
});

describe('CopyButton — one button, one clipboard routine', () => {
  it('copies the exact text it was given', async () => {
    const calls = stubClipboard();
    const target = document.createElement('div');
    document.body.appendChild(target);
    app = mount(CopyButton, { target, props: { text: 'line one\nline two' } });
    await tick();

    const button = document.querySelector<HTMLButtonElement>('[data-testid="copy-btn"]');
    expect(button, 'copy button rendered').toBeTruthy();
    button!.click();
    await tick();

    expect(calls).toEqual(['line one\nline two']);
  });

  it('gives the button an accessible name and a title of its own', async () => {
    stubClipboard();
    const target = document.createElement('div');
    document.body.appendChild(target);
    app = mount(CopyButton, {
      target,
      props: { text: 'x', label: 'Copy this pending action', title: 'Copy it' },
    });
    await tick();

    const button = document.querySelector('[data-testid="copy-btn"]')!;
    expect(button.getAttribute('aria-label')).toBe('Copy this pending action');
    expect(button.getAttribute('title')).toBe('Copy it');
  });
});