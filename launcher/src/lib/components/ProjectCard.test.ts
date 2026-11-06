// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// BEHAVIOURAL test of the v0.2.106 Remove button on the project card: the
// whole card is a click/keyboard target that OPENS the project, so the
// button inside it must stop both click and keydown propagation — driving
// the real component under jsdom proves it, and proves the accessible label
// names the project. No mocks: the card is pure props in / callback out.

// @vitest-environment jsdom

import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';

import ProjectCard from './ProjectCard.svelte';

const PROJECT = {
  id: 'p1',
  name: 'Gamma',
  folder_path: '/tmp/gamma',
  host: 'base' as const,
};

let app: ReturnType<typeof mount> | null = null;

async function mountCard(opts: { folderMissing?: boolean } = {}) {
  const target = document.createElement('div');
  document.body.appendChild(target);
  const onOpen = vi.fn();
  const onRemove = vi.fn();
  app = mount(ProjectCard, {
    target,
    props: {
      project: PROJECT,
      onOpen,
      onRemove,
      folderMissing: opts.folderMissing ?? false,
    },
  });
  await tick();
  return { onOpen, onRemove };
}

function removeButton(): HTMLButtonElement {
  return document.querySelector('[data-testid="project-card-remove"]') as HTMLButtonElement;
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  document.body.innerHTML = '';
});

describe('ProjectCard — the Remove button (v0.2.106)', () => {
  it('is labelled with the project name', async () => {
    await mountCard();
    expect(removeButton().getAttribute('aria-label')).toBe('Remove project Gamma');
  });

  it('clicking Remove opens the REMOVE flow, never the project', async () => {
    const { onOpen, onRemove } = await mountCard();
    removeButton().click();
    await tick();
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onRemove.mock.calls[0][0]).toMatchObject({ id: 'p1', name: 'Gamma' });
    expect(onOpen, 'the card must NOT open the project').not.toHaveBeenCalled();
  });

  it('Enter/Space on the Remove button does not open the project either', async () => {
    const { onOpen } = await mountCard();
    const btn = removeButton();
    btn.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await tick();
    expect(onOpen, 'keydown must not reach the card open handler').not.toHaveBeenCalled();
    // And the card's own open button still opens the project (control).
    (document.querySelector('[data-testid="project-card-open"]') as HTMLButtonElement).click();
    await tick();
    expect(onOpen).toHaveBeenCalledWith('p1');
  });

  it('never nests an interactive control inside another (review R2-6)', async () => {
    await mountCard();
    const open = document.querySelector('[data-testid="project-card-open"]') as HTMLButtonElement;
    expect(open.tagName).toBe('BUTTON');
    expect(open.getAttribute('aria-label')).toBe('Open project Gamma');
    expect(document.querySelector('[role="button"]'), 'no role=button wrapper').toBeNull();
    expect(open.contains(removeButton()), 'Remove is a sibling, not a child').toBe(false);
    expect(removeButton().closest('button, [role="button"]')).toBe(removeButton());
  });

  it('the orchestrator-root card offers no Remove; any other card does', async () => {
    const target = document.createElement('div');
    document.body.appendChild(target);
    app = mount(ProjectCard, {
      target,
      props: {
        project: { ...PROJECT, host: 'orchestrator_root' },
        onOpen: vi.fn(),
        onRemove: vi.fn(),
      },
    });
    await tick();
    expect(removeButton(), 'no Remove on the orchestrator root').toBeNull();
    unmount(app);
    app = null;
    document.body.innerHTML = '';
    await mountCard();
    expect(removeButton(), 'Remove on an ordinary project').toBeTruthy();
  });

  it('works when the folder is missing too', async () => {
    const { onOpen, onRemove } = await mountCard({ folderMissing: true });
    removeButton().click();
    await tick();
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });
});
