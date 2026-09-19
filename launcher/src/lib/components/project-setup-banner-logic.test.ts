// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — behaviour of the post-add / post-adopt setup banner.
//
// These are the asserts the field report cares about: the banner says which
// stage is running, it goes away when setup completes, and a failure keeps
// its error text plus a Retry. They run against the pure view-model
// (`project-setup-banner-logic.ts`) because the launcher's vitest run is a
// `node` environment — no DOM, so a mounted component cannot be asserted.

import { describe, it, expect } from 'vitest';
import {
  SETUP_BANNER_HIDE_TERMINAL_AFTER_MS,
  buildSetupBannerView,
  setupBannerNeedsTick,
  setupElapsedLabel,
  setupPhaseLabel,
} from './project-setup-banner-logic';
import type { ActiveSetup } from '$lib/stores/project-setup';

const T0 = 1_700_000_000_000;

function active(partial: Partial<ActiveSetup> = {}): ActiveSetup {
  return {
    project_id: 'proj-1',
    project_name: 'Demo',
    status: 'running',
    phase: 'bundle',
    warnings: [],
    error: null,
    observed_at: T0,
    ...partial,
  };
}

describe('stage text', () => {
  it('names the bundle stage the field report saw', () => {
    const vm = buildSetupBannerView(active({ phase: 'bundle' }), 0, T0 + 3_000);
    expect(vm?.phaseLabel).toBe('Installing project bundle (hooks, scripts, agents)…');
    expect(vm?.title).toBe('Setting up Demo');
    expect(vm?.status).toBe('running');
    expect(vm?.detail).toContain('3s elapsed');
    expect(vm?.detail).toContain('finishes in the background');
  });

  it('names the other phases, and falls back for an unknown one', () => {
    expect(setupPhaseLabel('running', 'bootstrap')).toBe('Creating knowledge collections…');
    expect(setupPhaseLabel('running', 'post_bundle')).toBe(
      'Indexing — continues in the background…',
    );
    expect(setupPhaseLabel('running', null)).toBe('Setting up…');
  });

  it('presents a queued add as running, and shows the queue depth', () => {
    const vm = buildSetupBannerView(active({ status: 'pending', phase: null }), 2, T0);
    expect(vm?.status).toBe('running');
    expect(vm?.phaseLabel).toBe('Queued…');
    expect(vm?.title).toBe('Adding Demo — 2 queued');
  });

  it('formats elapsed under and over a minute', () => {
    expect(setupElapsedLabel(0)).toBe('0s elapsed');
    expect(setupElapsedLabel(59_000)).toBe('59s elapsed');
    expect(setupElapsedLabel(61_000)).toBe('1m 1s elapsed');
  });
});

describe('completion hides the banner', () => {
  it('shows the done state inside its window', () => {
    const vm = buildSetupBannerView(active({ status: 'done', phase: null }), 0, T0 + 1_000);
    expect(vm?.status).toBe('done');
    expect(vm?.phaseLabel).toBe('Setup complete.');
    expect(vm?.canDismiss).toBe(true);
    expect(vm?.canRetry).toBe(false);
  });

  it('renders nothing once the window expires', () => {
    const at = T0 + SETUP_BANNER_HIDE_TERMINAL_AFTER_MS;
    expect(buildSetupBannerView(active({ status: 'done', phase: null }), 0, at)).toBeNull();
    expect(buildSetupBannerView(active({ status: 'deferred', phase: null }), 0, at)).toBeNull();
  });

  it('renders nothing when no setup has been observed', () => {
    expect(buildSetupBannerView(null, 0, T0)).toBeNull();
  });

  it('stops ticking once the terminal window has passed', () => {
    expect(setupBannerNeedsTick(active(), T0 + 600_000)).toBe(true); // still running
    expect(setupBannerNeedsTick(active({ status: 'done' }), T0 + 1_000)).toBe(true);
    expect(
      setupBannerNeedsTick(
        active({ status: 'done' }),
        T0 + SETUP_BANNER_HIDE_TERMINAL_AFTER_MS + 2_000,
      ),
    ).toBe(false);
    expect(setupBannerNeedsTick(null, T0)).toBe(false);
  });
});

describe('failure state', () => {
  const failed = active({
    status: 'failed',
    phase: null,
    error: 'install-bundle exited 1: permission denied',
    warnings: [{ message: 'bundle: 2 files preserved', severity: 'info' }],
  });

  it('keeps the error text verbatim and offers Retry', () => {
    const vm = buildSetupBannerView(failed, 0, T0 + 500);
    expect(vm?.status).toBe('failed');
    expect(vm?.phaseLabel).toBe('Setup failed.');
    expect(vm?.error).toBe('install-bundle exited 1: permission denied');
    expect(vm?.canRetry).toBe(true);
    expect(vm?.canDismiss).toBe(false);
    expect(vm?.warnings).toHaveLength(1);
  });

  it('never auto-hides — a failure waits for the user', () => {
    const vm = buildSetupBannerView(failed, 0, T0 + SETUP_BANNER_HIDE_TERMINAL_AFTER_MS * 20);
    expect(vm).not.toBeNull();
    expect(vm?.status).toBe('failed');
  });
});

describe('deferred is informational, not a failure', () => {
  it('offers dismiss but no Retry, and says the project is usable', () => {
    const vm = buildSetupBannerView(active({ status: 'deferred', phase: null }), 0, T0 + 10);
    expect(vm?.status).toBe('deferred');
    expect(vm?.canRetry).toBe(false);
    expect(vm?.canDismiss).toBe(true);
    expect(vm?.phaseLabel).toContain('when Weaviate is ready');
    expect(vm?.detail).toContain('ready to use now');
  });
});
