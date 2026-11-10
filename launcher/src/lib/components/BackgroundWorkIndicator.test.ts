// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106: the MenuBar background-work pill, driven end to end by the job
// events the backend already emits (only the Tauri bridge is faked). Field
// report: after closing "Update all" nothing showed that jobs still ran.

// @vitest-environment jsdom

import { describe, it, expect, vi, afterEach } from 'vitest';
import { mount, unmount, tick } from 'svelte';

const handlers: Record<string, (e: { payload: unknown }) => void> = {};
const statusRows: Record<string, unknown> = {};

vi.mock('$lib/tauri', () => ({
  tauriAvailable: () => true,
  isTauriRuntime: () => true,
  listen: (event: string, h: (e: { payload: unknown }) => void) => {
    handlers[event] = h;
    return Promise.resolve(() => {});
  },
  invoke: () => Promise.resolve(null),
  safeInvoke: (cmd: string, args?: { projectId?: string }) =>
    Promise.resolve(statusRows[`${cmd}:${args?.projectId ?? ''}`] ?? null),
}));

const { default: BackgroundWorkIndicator } = await import('./BackgroundWorkIndicator.svelte');
const { backgroundWork } = await import('$lib/stores/background-work');

let app: ReturnType<typeof mount> | null = null;

async function render() {
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(BackgroundWorkIndicator, { target });
  await tick();
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  backgroundWork.reset();
});

describe('BackgroundWorkIndicator', () => {
  it('is hidden while idle, shows while a job runs, and hides when it ends', async () => {
    await render();
    expect(document.querySelector('.bgw-pill')).toBeNull();

    handlers['code-graph-build-progress']({
      payload: { project_id: 'p1', status: 'running', files_analyzed: 5, current_phase: 'python' },
    });
    await tick();
    const pill = document.querySelector('.bgw-pill');
    expect(pill?.textContent).toContain('1 job running');

    (pill as HTMLButtonElement).click();
    await tick();
    expect(document.querySelector('.bgw-pop')?.textContent).toContain(
      'building code graph (5 files)',
    );

    handlers['code-graph-build-progress']({
      payload: { project_id: 'p1', status: 'success', files_analyzed: 9, current_phase: null },
    });
    await tick();
    expect(document.querySelector('.bgw-pill')).toBeNull();
  });

  it('a job that started before the window loaded is seeded from its status row', async () => {
    statusRows['get_kg_summary_status:p9'] = {
      project_id: 'p9',
      status: 'running',
      nodes_total: 4,
      nodes_succeeded: 1,
      nodes_unchanged: 0,
      nodes_failed: 0,
      nodes_skipped: 0,
    };
    await backgroundWork.seed(['p9']);
    await render();
    expect(document.querySelector('.bgw-pill')?.textContent).toContain('1 job running');
  });
});
