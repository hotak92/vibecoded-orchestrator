// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.105 (L-SMALL) — the pending-actions panel's Copy affordance, driven for
// real: mount the component for a PROJECT ledger and for the ORCHESTRATOR ROOT
// ledger (the Updates page's mount), click a row's Copy button, and assert the
// clipboard receives exactly `entryCopyText(entry, view)`.
//
// The ledger store is the only mock: it reaches Tauri, which does not exist
// under vitest. Everything else is the shipped component and shipped helpers.

// @vitest-environment jsdom

import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import type { ComponentProps } from 'svelte';

import DeferralLedgerPanel from './DeferralLedgerPanel.svelte';
import {
  entryCopyText,
  ROOT_SCOPE_LABEL,
  type DeferralLedgerView,
  type LedgerEntry,
} from '$lib/deferral-ledger';

const { loadProjectLedger } = vi.hoisted(() => ({ loadProjectLedger: vi.fn() }));

vi.mock('$lib/stores/deferrals', async () => {
  const { writable } = await import('svelte/store');
  const state = writable<{
    view: DeferralLedgerView | null;
    loading: boolean;
    error: string | null;
    loaded: boolean;
  }>({ view: null, loading: false, error: null, loaded: false });
  return {
    __setRootView: (view: DeferralLedgerView | null) =>
      state.set({ view, loading: false, error: null, loaded: true }),
    deferrals: {
      subscribe: state.subscribe,
      refreshRoot: vi.fn(),
      rootView: () => null,
      reset: vi.fn(),
    },
    loadProjectLedger,
    dismissDeferral: vi.fn(),
  };
});

// The mock adds this setter for the root-scope mount; it is typed here rather
// than imported so the real module's surface stays untouched.
type RootLedgerMock = {
  __setRootView: (view: DeferralLedgerView | null) => void;
};

async function setRootView(view: DeferralLedgerView | null): Promise<void> {
  const store = (await import('$lib/stores/deferrals')) as unknown as RootLedgerMock;
  store.__setRootView(view);
}

function entry(
  partial: Partial<LedgerEntry> & { condition_id: string },
): LedgerEntry {
  return {
    title: `T ${partial.condition_id}`,
    detected: 'D',
    why_deferred: 'W',
    command_to_apply: 'cmd',
    severity: 'warning',
    detected_at: '2026-08-27T00:00:00Z',
    kg_node_refs: [],
    disposition: 'action_required',
    disposition_source: 'registry',
    actionable: true,
    auto_retryable: false,
    retries: {
      attempts: 0,
      cap: 3,
      cap_reached: false,
      succeeded: 0,
      failed: 0,
      inconclusive: 0,
      skipped: 0,
      outcomes: [],
    },
    ...partial,
  };
}

function view(partial: Partial<DeferralLedgerView> = {}): DeferralLedgerView {
  const entries = partial.entries ?? [];
  const actionable = entries.filter((e) => e.actionable).length;
  return {
    scope: 'project',
    scope_label: 'My Project',
    folder: '/home/u/proj',
    source: 'sidecar',
    actionable_count: actionable,
    action_required_count: entries.filter(
      (e) => e.disposition === 'action_required',
    ).length,
    record_count: entries.length - actionable,
    warnings: [],
    ...partial,
    entries,
  };
}

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

let app: ReturnType<typeof mount> | null = null;

async function mountPanel(
  props: ComponentProps<typeof DeferralLedgerPanel>,
): Promise<void> {
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(DeferralLedgerPanel, { target, props });
  // onMount's async load resolves on a microtask; two ticks are enough for
  // the view to render.
  await tick();
  await Promise.resolve();
  await tick();
}

afterEach(async () => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  loadProjectLedger.mockReset();
  await setRootView(null);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: undefined,
  });
});

describe('DeferralLedgerPanel — Copy per pending action', () => {
  it("renders one Copy button per entry and copies that entry's full text", async () => {
    const calls = stubClipboard();
    const one = entry({ condition_id: 'weaviate_unreachable_at_update' });
    const two = entry({
      condition_id: 'kg_sync_failures_pending',
      command_to_apply: '# retry\npython -m vco_lib.doctor',
    });
    const v = view({ entries: [one, two] });
    loadProjectLedger.mockResolvedValue(v);

    await mountPanel({ scope: 'project', projectId: 'p1' });

    const buttons = document.querySelectorAll<HTMLButtonElement>(
      '[data-testid="copy-btn"]',
    );
    expect(buttons.length).toBe(2);

    buttons[0].click();
    buttons[1].click();
    await tick();

    expect(calls).toEqual([entryCopyText(one, v), entryCopyText(two, v)]);
    expect(calls[1]).toContain('Remedy:\n# retry\npython -m vco_lib.doctor');
  });

  it("names the ORCHESTRATOR ROOT on the Updates page's panel", async () => {
    const calls = stubClipboard();
    const e = entry({ condition_id: 'update_resume_required' });
    const rootView = view({
      scope: 'orchestrator_root',
      scope_label: 'orchestrator-root',
      entries: [e],
    });
    await setRootView(rootView);

    await mountPanel({ scope: 'orchestrator_root' });

    const button = document.querySelector<HTMLButtonElement>(
      '[data-testid="copy-btn"]',
    );
    expect(button, 'root row rendered a Copy button').toBeTruthy();
    button!.click();
    await tick();

    expect(calls).toEqual([entryCopyText(e, rootView)]);
    expect(calls[0]).toContain(`Scope: ${ROOT_SCOPE_LABEL}`);
  });
});