// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.103 (plan item 17) — the add flow and the bundle update toast the
// backend's informational `notices` (today: "Created a local git repository
// (no remote)") as successes/info, never as red errors, and keep warnings on
// their own severity path. Drives the REAL store with a mocked invoke.

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// `projects.ts` reads localStorage synchronously at module load.
beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() { return s.size; },
      clear() { s.clear(); },
      getItem(k: string) { return s.has(k) ? (s.get(k) as string) : null; },
      key(i: number) { return Array.from(s.keys())[i] ?? null; },
      removeItem(k: string) { s.delete(k); },
      setItem(k: string, v: string) { s.set(k, String(v)); },
    } as Storage;
  }
});

const invokeMock = vi.fn();
vi.mock('$lib/tauri', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
  tauriAvailable: () => true,
}));

const toastCalls: Array<{ kind: string; text: string }> = [];
vi.mock('$lib/stores/toast', () => ({
  toast: {
    info: (m: unknown) => toastCalls.push({ kind: 'info', text: String(m) }),
    success: (m: unknown) => toastCalls.push({ kind: 'success', text: String(m) }),
    error: (m: unknown) => toastCalls.push({ kind: 'error', text: String(m) }),
  },
}));

vi.mock('$lib/stores/project-setup', () => ({
  projectSetup: { setCreateFn: () => {} },
}));

import { projects } from './projects';
import type { ProjectView } from '$lib/types/launcher';

const NOTICE =
  'Created a local git repository (no remote, nothing committed). Isolated subagent worktrees need a first commit.';

function view(id: string): ProjectView {
  return { id, name: id, folder_path: `/tmp/${id}`, host: 'base', slug: id } as unknown as ProjectView;
}

beforeEach(() => {
  invokeMock.mockReset();
  toastCalls.length = 0;
});

describe('create: notices are success toasts, warnings stay red', () => {
  it('toasts the local-repo notice as success', async () => {
    invokeMock.mockResolvedValueOnce({
      project: view('p1'),
      warnings: ['env file write failed'],
      notices: [NOTICE],
    });
    await projects.create('p1', '/tmp/p1', 'base' as never, false);
    expect(toastCalls).toContainEqual({ kind: 'success', text: NOTICE });
    expect(toastCalls).toContainEqual({ kind: 'error', text: 'env file write failed' });
    expect(toastCalls.filter((t) => t.text === NOTICE)).toHaveLength(1);
  });

  it('an older backend without `notices` still works', async () => {
    invokeMock.mockResolvedValueOnce({ project: view('p2'), warnings: [] });
    await projects.create('p2', '/tmp/p2', 'base' as never, false);
    expect(toastCalls).toEqual([]);
  });
});

describe('update: the one-time catch-up notice is an info toast', () => {
  it('toasts notices as info, never error', async () => {
    invokeMock.mockResolvedValueOnce({
      project: view('p3'),
      warnings: [],
      summary: {
        created: 0, overwritten: 0, preserved: 0, noop: 3, always_overwritten: 0,
        skipped_existing: 0, errors_count: 0, adopted: 0, preserved_by_reason: {},
      },
      notices: [NOTICE],
    });
    await projects.update('p3');
    expect(toastCalls).toContainEqual({ kind: 'info', text: NOTICE });
    expect(toastCalls.some((t) => t.kind === 'error' && t.text === NOTICE)).toBe(false);
  });
});
