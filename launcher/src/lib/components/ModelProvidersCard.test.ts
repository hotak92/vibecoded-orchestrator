// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.105 — BEHAVIOURAL test of the Home page's "Model providers" card.
//
// Mounts the REAL `ModelProvidersCard.svelte` under jsdom and drives the
// actual DOM: flipping a provider's switch calls the save command with
// `(vendor, enabled)`, the row shows the key STATE as words (a value can never
// appear — the payload has no value field at all), an off provider shows the
// standing "what this does" line, and a `stored: false` answer is rendered as
// "nothing was changed" rather than as applied.
//
// `load` / `save` are the component's injectable seams, so no Tauri runtime is
// involved; the wire shape itself is pinned in
// `$lib/api/model_providers.test.ts`.

// @vitest-environment jsdom

import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import ModelProvidersCard from './ModelProvidersCard.svelte';
import type {
  ModelProviderRow,
  ModelProvidersPayload,
  ModelProviderSetResult,
} from '$lib/api/model_providers';

let app: ReturnType<typeof mount> | null = null;

function row(over: Partial<ModelProviderRow> = {}): ModelProviderRow {
  return {
    vendor_id: 'kimi',
    display: 'Kimi Code',
    enabled: true,
    key_state: 'present',
    secret_key_names: ['kimi_api_key'],
    docs_url: 'https://example.invalid/kimi',
    upstream_host: 'api.kimi.ai',
    definitions: ['kimi-implementer', 'kimi-reviewer'],
    ...over,
  };
}

function payload(over: Partial<ModelProvidersPayload> = {}): ModelProvidersPayload {
  return {
    disabled: [],
    gateway_available: true,
    providers: [
      row(),
      row({
        vendor_id: 'zai',
        display: 'Z.ai',
        key_state: 'absent',
        secret_key_names: ['glm_api_key'],
        definitions: ['glm-reviewer'],
      }),
    ],
    ...over,
  };
}

async function mountCard(props: {
  load: () => Promise<ModelProvidersPayload>;
  save: (vendor: string, enabled: boolean) => Promise<ModelProviderSetResult>;
}) {
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(ModelProvidersCard, { target, props });
  await flush();
}

/** Let the toggle's promise chain (save → note → reload) and the following
 *  re-render settle: two microtask flushes plus one macrotask. */
async function flush() {
  await tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await tick();
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
});

function switchFor(vendor: string): HTMLInputElement {
  return document.querySelector(
    `[data-testid="provider-switch-${vendor}"]`,
  ) as HTMLInputElement;
}

describe('ModelProvidersCard.svelte', () => {
  it('lists every provider with its key state in words', async () => {
    await mountCard({
      load: async () => payload(),
      save: async (v, e) => ({ vendor: v, enabled: e, stored: true }),
    });
    const text = document.body.textContent ?? '';
    expect(text).toContain('Model providers');
    expect(text).toContain('Kimi Code');
    expect(text).toContain('Z.ai');
    expect(text).toContain('key detected');
    expect(text).toContain('no key');
    // The declared key NAME is shown; a value never exists in the payload.
    expect(text).toContain('kimi_api_key');
    expect(document.querySelectorAll('[data-provider]').length).toBe(2);
  });

  it('switching a provider off calls the command with (vendor, enabled)', async () => {
    const save = vi.fn(async (vendor: string, enabled: boolean) => ({
      vendor,
      enabled,
      stored: true,
    }));
    await mountCard({ load: async () => payload(), save });

    const input = switchFor('kimi');
    expect(input.checked).toBe(true);
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith('kimi', false);
    const note = document.querySelector('[data-testid="providers-note"]');
    expect(note?.textContent).toContain('Kimi Code is off');
    expect(note?.textContent).toContain('nothing is deleted');
  });

  it('a provider that is already off shows the standing explanation', async () => {
    await mountCard({
      load: async () =>
        payload({ providers: [row({ enabled: false })], disabled: ['kimi'] }),
      save: async (v, e) => ({ vendor: v, enabled: e, stored: true }),
    });
    expect(switchFor('kimi').checked).toBe(false);
    const notice = document.querySelector('[data-testid="providers-off-notice"]');
    expect(notice?.textContent).toContain('.claude/agents.disabled/');
  });

  it('an unstored change is reported as not saved, and the row stays on', async () => {
    await mountCard({
      load: async () => payload(),
      save: async (vendor, enabled) => ({ vendor, enabled, stored: false }),
    });
    const input = switchFor('kimi');
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    const note = document.querySelector('[data-testid="providers-note"]');
    expect(note?.textContent).toContain('Could not save');
    expect(note?.textContent).toContain('Nothing was changed');
    // The reload reported the provider still on.
    expect(switchFor('kimi').checked).toBe(true);
  });

  it('renders nothing when no gateway provider can be named', async () => {
    await mountCard({
      load: async () => payload({ gateway_available: false, providers: [] }),
      save: async (v, e) => ({ vendor: v, enabled: e, stored: true }),
    });
    expect(document.querySelector('.providers-card')).toBeNull();
  });

  it('surfaces a failed read instead of an empty card', async () => {
    await mountCard({
      load: async () => {
        throw new Error('vco_lib.model_providers exited 1');
      },
      save: async (v, e) => ({ vendor: v, enabled: e, stored: true }),
    });
    const alert = document.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('vco_lib.model_providers exited 1');
  });
});