// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// BEHAVIOURAL test of the ONE shared "Remove project" flow (v0.2.106):
// the real component is mounted under jsdom and driven through actual DOM
// events. `$lib/tauri` is the only mock (there is no Tauri host under
// vitest), so every assertion runs through the REAL projects store —
// clicking "Yes, remove" must reach `delete_project_v2` through the same
// invoke the settings page uses.

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

import ProjectRemoveFlow from './ProjectRemoveFlow.svelte';
import { toast } from '$lib/stores/toast';
import type { UnregisterReport } from '$lib/types/launcher';

const STOP =
  'Unregister stopped — the project is still registered (…). These values VCO wrote could not ' +
  'be removed: OPENAI_API_KEY in .claude/env. … choose "Unregister anyway — leave these values" …';

const GAMMA = { id: 'p1', name: 'Gamma' };

function report(extra: Partial<UnregisterReport> = {}): UnregisterReport {
  return {
    projectId: 'p1',
    projectName: 'Gamma',
    filesPurged: [],
    keysPurgedFromEnv: [],
    collectionsDropped: [],
    warnings: [],
    leftInPlace: [],
    leftoversNote: null,
    ...extra,
  };
}

function deleteCalls() {
  return invokeMock.mock.calls.filter((c) => c[0] === 'delete_project_v2');
}

let app: ReturnType<typeof mount> | null = null;
let project = { ...GAMMA };

async function mountFlow() {
  project = { ...GAMMA };
  const target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(ProjectRemoveFlow, { target, props: { project } });
  await tick();
}

function yes(): HTMLButtonElement {
  return document.querySelector('[data-testid="project-remove-yes"]') as HTMLButtonElement;
}
function no(): HTMLButtonElement {
  return document.querySelector('[data-testid="project-remove-no"]') as HTMLButtonElement;
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
  invokeMock.mockReset();
  vi.useRealTimers();
});

describe('ProjectRemoveFlow — the shared confirm dialog', () => {
  it('says plainly what goes and what stays, with Yes/No buttons', async () => {
    await mountFlow();
    // Collapse template line-wrapping so phrase assertions are about copy,
    // not about where the markup breaks a line.
    const text = (document.body.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).toContain('Remove project');
    expect(text).toContain('Gamma');
    // What goes: VCO's own files, env lines, and the project's collections.
    expect(text).toContain('hooks, scripts and bundled files');
    expect(text).toContain('knowledge graph');
    expect(text).toContain('code graph');
    // What stays: the folder and everything that is the user's.
    expect(text).toContain('source code');
    expect(text).toContain('.env');
    expect(text).toContain('secrets in the keychain');
    expect(yes(), 'Yes, remove button rendered').toBeTruthy();
    expect(no(), 'No button rendered').toBeTruthy();
  });

  it('No invokes nothing and closes the dialog', async () => {
    await mountFlow();
    no().click();
    await tick();
    expect(deleteCalls(), 'No must not invoke delete_project_v2').toHaveLength(0);
    expect(yes(), 'dialog closed after No').toBeNull();
  });

  it('Esc invokes nothing and closes the dialog (Esc = No)', async () => {
    await mountFlow();
    // A real Esc on a native <dialog> fires 'cancel' then 'close'.
    const dlg = document.querySelector('dialog')!;
    dlg.dispatchEvent(new Event('cancel', { bubbles: false }));
    dlg.dispatchEvent(new Event('close', { bubbles: false }));
    await tick();
    expect(deleteCalls(), 'Esc must not invoke delete_project_v2').toHaveLength(0);
    expect(yes(), 'dialog closed after Esc').toBeNull();
  });

  it('"Yes, remove" invokes delete_project_v2 ONCE with exactly the removal options', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'delete_project_v2') {
        return report({ filesPurged: ['a', 'b'], collectionsDropped: ['KG'] });
      }
      return undefined;
    });
    const toasts: { kind: string; message: string }[] = [];
    const unsub = toast.subscribe((list) => toasts.splice(0, toasts.length, ...list));
    await mountFlow();
    yes().click();
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
    // The report counts are toasted as a success summary.
    expect(
      toasts.some(
        (t) =>
          t.kind === 'success' &&
          t.message.includes('Removed "Gamma"') &&
          t.message.includes('2 files removed') &&
          t.message.includes('1 collections dropped'),
      ),
      `summary toast with report counts, got: ${JSON.stringify(toasts)}`,
    ).toBe(true);
    unsub();
    await vi.waitFor(() =>
      expect(yes(), 'dialog closed after a successful remove').toBeNull(),
    );
  });

  it('both buttons are disabled while the remove runs', async () => {
    let release!: (r: UnregisterReport) => void;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'delete_project_v2') {
        return new Promise<UnregisterReport>((res) => (release = res));
      }
      return undefined;
    });
    await mountFlow();
    yes().click();
    await tick();
    expect(yes().disabled, 'Yes disabled while running').toBe(true);
    expect(no().disabled, 'No disabled while running').toBe(true);
    release(report());
    // Completing the remove closes the dialog (the {#if project} guard
    // unmounts it), so the buttons re-enable by being gone — assert that.
    await vi.waitFor(() => expect(yes()).toBeNull());
  });

  it('a stop opens UnregisterStoppedDialog; keeping the project invokes nothing more', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'delete_project_v2') throw STOP;
      return undefined;
    });
    await mountFlow();
    yes().click();
    await tick();
    await tick();
    // The stop dialog appeared — and not merely its always-mounted shell:
    // a closed native <dialog> keeps its children in the DOM, so assert on
    // the stop MESSAGE (only rendered into the open dialog by
    // askLeaveAnyway) rather than the static title/buttons.
    const text = document.body.textContent ?? '';
    expect(text).toContain('These values VCO wrote could not');
    expect(text).toContain('Keep the project');
    // …and choosing to keep ends the flow with exactly one invoke.
    const keep = [...document.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Keep the project',
    )!;
    keep.click();
    await tick();
    await tick();
    expect(deleteCalls()).toHaveLength(1);
  });

  it('"Unregister anyway" re-runs with leaveUnremovable: true', async () => {
    invokeMock
      .mockImplementationOnce(async () => {
        throw STOP;
      })
      .mockImplementation(async () => report({ leftInPlace: ['OPENAI_API_KEY in .claude/env'] }));
    await mountFlow();
    yes().click();
    await tick();
    await tick();
    const anyway = [...document.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Unregister anyway — leave these values',
    )!;
    anyway.click();
    await tick();
    await tick();
    const calls = deleteCalls();
    expect(calls).toHaveLength(2);
    expect(calls[1][1]).toEqual({
      id: 'p1',
      options: {
        purgeLauncherFiles: true,
        purgeCollections: true,
        leaveUnremovable: true,
      },
    });
  });
});
