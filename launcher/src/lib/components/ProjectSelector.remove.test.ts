// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// BEHAVIOURAL test that the top-bar project selector's delete row action
// runs the ONE shared Remove flow (v0.2.106): the REAL ProjectSelector is
// mounted under jsdom, its project list is served by a mocked invoke (no
// Tauri host under vitest), and clicking the row's delete action must open
// the SHARED confirm dialog — the same "Yes, remove"/"No" testids the
// Projects-page card flow uses, not a selector-local dialog — and "Yes"
// must reach `delete_project_v2` with the owner's removal options. No
// source scans: everything is asserted from the driven DOM.

// @vitest-environment jsdom

import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));

vi.mock('$lib/tauri', () => ({
  invoke: invokeMock,
  safeInvoke: vi.fn(async () => null),
  listen: vi.fn(async () => () => {}),
  tauriAvailable: () => true,
  isTauriRuntime: () => true,
}));

import ProjectSelector from './ProjectSelector.svelte';
import type { ProjectView } from '$lib/types/launcher';
import type { UnregisterReport } from '$lib/types/launcher';

const PROJ: ProjectView = {
  id: 'p1',
  name: 'Gamma',
  folder_path: '/tmp/gamma',
  host: 'base',
  slug: 'gamma',
  created_at: 1,
  updated_at: 1,
  module_count: 2,
};

function report(): UnregisterReport {
  return {
    projectId: 'p1',
    projectName: 'Gamma',
    filesPurged: [],
    keysPurgedFromEnv: [],
    collectionsDropped: [],
    warnings: [],
    leftInPlace: [],
    leftoversNote: null,
  };
}

function deleteCalls() {
  return invokeMock.mock.calls.filter((c) => c[0] === 'delete_project_v2');
}

let app: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  invokeMock.mockReset();
});

async function mountSelector() {
  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === 'list_projects_v2') return [PROJ];
    if (cmd === 'delete_project_v2') return report();
    return undefined;
  });
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(ProjectSelector, { target });
  // onMount → projects.load() → invoke(list_projects_v2) → store update.
  // The dropdown panel renders only when OPEN, so wait on the trigger
  // showing the (auto-selected) project name instead.
  await vi.waitFor(() => {
    const name = document.querySelector('.project-trigger .project-name');
    expect(name?.textContent ?? '').toContain('Gamma');
  });
}

describe('ProjectSelector — the delete row action uses the shared Remove flow', () => {
  it('opens the SHARED Yes/No dialog (no typed confirmation, no selector-local dialog)', async () => {
    await mountSelector();
    // Open the dropdown.
    (document.querySelector('.project-trigger') as HTMLElement).click();
    await tick();
    // Click the row's delete action.
    const del = document.querySelector('button.row-action-danger') as HTMLButtonElement;
    expect(del, 'delete row action rendered').toBeTruthy();
    del.click();
    await tick();
    // The SHARED flow's dialog is in the document…
    expect(
      document.querySelector('[data-testid="project-remove-yes"]'),
      'the shared Remove dialog opened',
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="project-remove-no"]'),
      'the shared Remove dialog has a No button',
    ).toBeTruthy();
    // …and the old typed-confirmation step is gone.
    expect(document.querySelector('input[placeholder="Gamma"]')).toBeNull();
    expect((document.body.textContent ?? '')).toContain('are you sure');
  });

  it('"Yes, remove" reaches delete_project_v2 with the removal options', async () => {
    await mountSelector();
    (document.querySelector('.project-trigger') as HTMLElement).click();
    await tick();
    (document.querySelector('button.row-action-danger') as HTMLButtonElement).click();
    await tick();
    (document.querySelector('[data-testid="project-remove-yes"]') as HTMLButtonElement).click();
    await tick();
    await tick();
    const calls = deleteCalls();
    expect(calls).toHaveLength(1);
    expect(calls[0][1]).toEqual({
      id: 'p1',
      options: {
        purgeLauncherFiles: true,
        purgeCollections: true,
        leaveUnremovable: false,
      },
    });
  });

  it('"No" invokes nothing', async () => {
    await mountSelector();
    (document.querySelector('.project-trigger') as HTMLElement).click();
    await tick();
    (document.querySelector('button.row-action-danger') as HTMLButtonElement).click();
    await tick();
    (document.querySelector('[data-testid="project-remove-no"]') as HTMLButtonElement).click();
    await tick();
    await tick();
    expect(deleteCalls()).toHaveLength(0);
  });
});
