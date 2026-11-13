// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106: a toast raised while a modal is open renders ABOVE the modal.
// Every launcher modal is a native <dialog> in the top layer, so the toast
// host must live inside the topmost open dialog while one is open, and go
// back to <body> when it closes or is removed. Mounts the REAL Toast.svelte.

// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import Toast from './components/Toast.svelte';
import { toast } from '$lib/stores/toast';

let app: ReturnType<typeof mount> | null = null;

/** MutationObserver callbacks run as microtasks. */
const settle = async () => {
  await Promise.resolve();
  await tick();
};

async function mountToasts() {
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(Toast, { target });
  await tick();
}

function host(): HTMLElement {
  const h = document.querySelector('.toast-host');
  if (!(h instanceof HTMLElement)) throw new Error('toast host not rendered');
  return h;
}

function openDialog(): HTMLDialogElement {
  const d = document.createElement('dialog');
  d.setAttribute('open', '');
  document.body.appendChild(d);
  return d;
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  toast.subscribe((list) => {
    for (const t of list) toast.dismiss(t.id);
  })();
});

describe('toast host follows the top layer', () => {
  it('moves into an open modal dialog and back to body when it closes', async () => {
    await mountToasts();
    const dlg = openDialog();
    await settle();
    expect(host().parentElement).toBe(dlg);

    toast.error('hand-to-vco failed');
    await tick();
    expect(dlg.querySelector('.toast-error')?.textContent).toContain('hand-to-vco failed');

    dlg.removeAttribute('open');
    await settle();
    expect(host().parentElement).toBe(document.body);
    expect(host().querySelector('.toast-error')).not.toBeNull();
  });

  it('follows the most recently opened of two stacked dialogs', async () => {
    await mountToasts();
    const first = openDialog();
    await settle();
    const second = openDialog();
    await settle();
    expect(host().parentElement).toBe(second);
    second.removeAttribute('open');
    await settle();
    expect(host().parentElement).toBe(first);
  });

  it('survives its dialog being removed from the DOM while it is inside', async () => {
    await mountToasts();
    const dlg = openDialog();
    await settle();
    toast.error('still visible');
    await tick();
    dlg.remove();
    await settle();
    expect(host().isConnected).toBe(true);
    expect(host().parentElement).toBe(document.body);
    expect(host().textContent).toContain('still visible');
  });
});
