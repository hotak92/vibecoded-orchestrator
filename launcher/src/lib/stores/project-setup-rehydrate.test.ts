// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.100: the project-setup banner restores its state after a reload.
// `get_project_setup_status` had no caller, so a setup that ran (or was
// running) while the launcher was closed left nothing on screen.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock('$lib/tauri', () => ({
  tauriAvailable: () => true,
  listen: vi.fn(async () => () => {}),
  invoke: invokeMock,
}));
vi.mock('$lib/stores/toast', () => ({ toast: { info: vi.fn(), error: vi.fn() } }));

import { get } from 'svelte/store';
import { projectSetup, viewToActiveSetup } from './project-setup';
import type { ProjectSetupView } from '$lib/types/launcher';

const view = (over: Partial<ProjectSetupView> = {}): ProjectSetupView => ({
  project_id: 'p1',
  status: 'running',
  phase: 'bundle',
  started_at_iso: '2026-10-01T10:00:00.000Z',
  finished_at_iso: null,
  duration_ms: null,
  warnings: [],
  error_message: null,
  ...over,
});

beforeEach(() => {
  invokeMock.mockReset();
  projectSetup.reset();
});

describe('viewToActiveSetup', () => {
  it('a running row keeps its phase and is timed from when it started', () => {
    const a = viewToActiveSetup(view(), 'Alpha', 5)!;
    expect(a.status).toBe('running');
    expect(a.phase).toBe('bundle');
    expect(a.observed_at).toBe(Date.parse('2026-10-01T10:00:00.000Z'));
    expect(a.project_name).toBe('Alpha');
  });

  it('a terminal row drops the phase, keeps warnings and error, and is timed from when it finished', () => {
    const a = viewToActiveSetup(
      view({
        status: 'failed',
        phase: 'bundle',
        finished_at_iso: '2026-10-01T10:05:00.000Z',
        error_message: 'bundle install exited 1',
        warnings: [{ message: 'x', severity: 'error' }],
      }),
      'Alpha',
      5,
    )!;
    expect(a.phase).toBeNull();
    expect(a.error).toBe('bundle install exited 1');
    expect(a.warnings).toHaveLength(1);
    expect(a.observed_at).toBe(Date.parse('2026-10-01T10:05:00.000Z'));
  });

  it('no row, or an unparseable time, is handled', () => {
    expect(viewToActiveSetup(null, 'A', 1)).toBeNull();
    expect(viewToActiveSetup(view({ started_at_iso: 'garbage' }), 'A', 77)!.observed_at).toBe(77);
  });
});

describe('projectSetup.rehydrate', () => {
  it('restores the persisted status for the project after a reload', async () => {
    invokeMock.mockResolvedValueOnce(view({ status: 'failed', error_message: 'boom', finished_at_iso: '2026-10-01T10:05:00.000Z' }));
    await projectSetup.rehydrate('p1', 'Alpha');
    expect(invokeMock).toHaveBeenCalledWith('get_project_setup_status', { projectId: 'p1' });
    const a = get(projectSetup).active!;
    expect(a.status).toBe('failed');
    expect(a.error).toBe('boom');
  });

  it('restores nothing when no setup ever ran for the project', async () => {
    invokeMock.mockResolvedValueOnce(null);
    await projectSetup.rehydrate('p1', 'Alpha');
    expect(get(projectSetup).active).toBeNull();
  });

  it('a live setup already in the store is not overwritten', async () => {
    invokeMock.mockResolvedValueOnce(view({ project_id: 'p1' }));
    await projectSetup.rehydrate('p1', 'Alpha');
    invokeMock.mockResolvedValueOnce(view({ project_id: 'p2', status: 'failed' }));
    await projectSetup.rehydrate('p2', 'Beta');
    expect(get(projectSetup).active!.project_id).toBe('p1');
  });

  it('a dismissed banner is not restored again for that project', async () => {
    invokeMock.mockResolvedValueOnce(view({ status: 'done', finished_at_iso: new Date().toISOString() }));
    await projectSetup.rehydrate('p1', 'Alpha');
    projectSetup.dismiss();
    invokeMock.mockClear();
    await projectSetup.rehydrate('p1', 'Alpha');
    expect(invokeMock).not.toHaveBeenCalled();
    expect(get(projectSetup).active).toBeNull();
  });

  it('a failing read restores nothing and does not throw', async () => {
    invokeMock.mockRejectedValueOnce(new Error('db locked'));
    await expect(projectSetup.rehydrate('p1', 'Alpha')).resolves.toBeUndefined();
    expect(get(projectSetup).active).toBeNull();
  });
});

describe('the global banner drives it from the selected project', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../components/ProjectSetupBanner.svelte', import.meta.url)),
    'utf8',
  );
  it('calls rehydrate with the selected project', () => {
    expect(src).toContain('projectSetup.rehydrate(p.id, p.name)');
  });
});
