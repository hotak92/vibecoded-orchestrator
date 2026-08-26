// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.91 WP-F3 / WP-F4 — project rename: one home, live header, honest
// toast severity.
//
// The field report (BUG 4, UI half): renaming a project updated the DB and
// the top-left selector, but the open project page's `<h1>` kept the OLD
// name until a full reload. Two causes, both pinned here:
//
//   1. `SettingsTab.svelte` carried a SECOND `invoke('rename_project_v2')`
//      that patched only its own local state — neither the store nor the
//      page saw it (covered by the source-scan below + the store test).
//   2. The project page loaded its `ProjectView` once and never subscribed
//      to the store, so even a store-patching rename could not repaint it.
//      `liveProjectFor` is the derivation the page now uses; the reactivity
//      test drives the REAL store through a rename and asserts a subscriber
//      composing it sees the new name.
//
// Deliberately NOT tested through the selector's dropdown/outside-click
// path — the v0.2.89 lesson is that synthetic events cannot reproduce
// microtask-checkpoint bugs. This is plain store reactivity, which is
// exactly what a store-level test CAN pin.

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

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

// The add-queue callback registration at the bottom of projects.ts — inert
// here, but it must not drag the real module (and its own imports) in.
vi.mock('$lib/stores/project-setup', () => ({
  projectSetup: { setCreateFn: () => {} },
}));

import { projects, liveProjectFor, isRenameInfoWarning } from './projects';
import type { ProjectView } from '$lib/types/launcher';

function view(id: string, name: string): ProjectView {
  return {
    id,
    name,
    folder_path: `/tmp/${id}`,
    host: 'base',
    slug: name.toLowerCase(),
  } as unknown as ProjectView;
}

async function seed(rows: ProjectView[]) {
  invokeMock.mockReset();
  invokeMock.mockResolvedValueOnce(rows);
  await projects.load();
}

beforeEach(() => {
  invokeMock.mockReset();
  toastCalls.length = 0;
});

// ---------------------------------------------------------------------------
// liveProjectFor — the page's derivation
// ---------------------------------------------------------------------------

describe('liveProjectFor', () => {
  const fallback = view('p1', 'Stale Local Copy');

  it('prefers the store row over the page-local snapshot', () => {
    const store = [view('p1', 'Renamed In Store'), view('p2', 'Other')];
    expect(liveProjectFor(store, 'p1', fallback)?.name).toBe('Renamed In Store');
  });

  it('falls back to the local copy while the store is still empty', () => {
    expect(liveProjectFor([], 'p1', fallback)?.name).toBe('Stale Local Copy');
  });

  it('never matches a different project id', () => {
    const store = [view('p2', 'Other')];
    expect(liveProjectFor(store, 'p1', fallback)?.name).toBe('Stale Local Copy');
  });

  it('renders the fallback while the route id is unresolved', () => {
    const store = [view('p1', 'Renamed In Store')];
    expect(liveProjectFor(store, undefined, fallback)?.name).toBe('Stale Local Copy');
    expect(liveProjectFor(store, '', null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// projects.rename — store patch + the live derivation on top of it
// ---------------------------------------------------------------------------

describe('projects.rename', () => {
  it('patches only the targeted row', async () => {
    await seed([view('p1', 'Alpha'), view('p2', 'Beta')]);
    invokeMock.mockResolvedValueOnce({ project: view('p1', 'Alpha Renamed'), warnings: [] });

    await projects.rename('p1', 'Alpha Renamed');

    const rows = get(projects).projects;
    expect(rows.find((p) => p.id === 'p1')?.name).toBe('Alpha Renamed');
    expect(rows.find((p) => p.id === 'p2')?.name).toBe('Beta');
  });

  /**
   * RED-PROOF for the header staleness: this is exactly what the project
   * page now does — subscribe to the store, recompute the displayed row via
   * `liveProjectFor`, render its `name`. On `f97659a4` the page held a
   * one-shot `$state` instead, so the observed name stayed 'Alpha' (and
   * `liveProjectFor` did not exist at all).
   */
  it('makes a store subscriber observe the new name without reloading', async () => {
    await seed([view('p1', 'Alpha'), view('p2', 'Beta')]);

    let headerName: string | undefined;
    const unsub = projects.subscribe((s) => {
      headerName = liveProjectFor(s.projects, 'p1', view('p1', 'Alpha'))?.name;
    });
    expect(headerName).toBe('Alpha');

    invokeMock.mockResolvedValueOnce({ project: view('p1', 'Alpha Renamed'), warnings: [] });
    await projects.rename('p1', 'Alpha Renamed');

    expect(headerName).toBe('Alpha Renamed');
    unsub();
  });

  // -------------------------------------------------------------------
  // WP-F4 — severity typing
  // -------------------------------------------------------------------

  it('renders the by-design .env drift notice as info, not error', async () => {
    await seed([view('p1', 'Alpha')]);
    const drift =
      "The project-root .env at /tmp/p1/.env carries a KG_COLLECTION line that " +
      "does not match the project's bound collection (Alpha_KnowledgeGraph). VCO " +
      'never rewrites .env automatically — it may be committed to your VCS.';
    invokeMock.mockResolvedValueOnce({
      project: view('p1', 'Alpha Renamed'),
      warnings: [drift],
    });

    await projects.rename('p1', 'Alpha Renamed');

    expect(toastCalls).toEqual([{ kind: 'info', text: drift }]);
  });

  it('keeps a genuine failure red', async () => {
    await seed([view('p1', 'Alpha')]);
    const failure =
      'rename env refresh (apply_project_env_via_python) failed: python not found. ' +
      'KG routing for the renamed project may be stale until manual repair.';
    invokeMock.mockResolvedValueOnce({
      project: view('p1', 'Alpha Renamed'),
      warnings: [failure],
    });

    await projects.rename('p1', 'Alpha Renamed');

    expect(toastCalls).toEqual([{ kind: 'error', text: failure }]);
  });
});

describe('isRenameInfoWarning', () => {
  it('recognises the drift notice by either stable marker', () => {
    expect(isRenameInfoWarning("… does not match the project's bound collection (X).")).toBe(true);
    expect(isRenameInfoWarning('VCO never rewrites .env automatically')).toBe(true);
  });

  it('defaults to error for anything unrecognised', () => {
    expect(isRenameInfoWarning('something went wrong')).toBe(false);
    expect(isRenameInfoWarning('')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// One concern, one home
// ---------------------------------------------------------------------------

describe('rename call-site invariant', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const srcRoot = resolve(here, '../..');

  it("only the store invokes 'rename_project_v2'", () => {
    // A second call-site is what produced the bug: SettingsTab's duplicate
    // invoke bypassed the store, so neither the selector nor the page header
    // learned about the rename.
    const files = [
      'lib/project-state/SettingsTab.svelte',
      'lib/components/ProjectSelector.svelte',
      'routes/project/[id]/+page.svelte',
    ];
    for (const rel of files) {
      const body = readFileSync(resolve(srcRoot, rel), 'utf8');
      expect(body, `${rel} must route renames through projects.rename`).not.toContain(
        "'rename_project_v2'",
      );
    }
  });

  it('the project page header derives from the store, not a one-shot load', () => {
    const body = readFileSync(resolve(srcRoot, 'routes/project/[id]/+page.svelte'), 'utf8');
    expect(body).toContain('liveProjectFor(');
    expect(body).toContain('{liveProject?.name');
    // The stale binding that produced the field report.
    expect(body).not.toContain('<h1>{project?.name');
  });
});
