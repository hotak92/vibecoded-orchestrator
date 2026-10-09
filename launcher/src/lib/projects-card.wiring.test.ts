// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the Projects-page card routing (P299-C3, pinned by
// W5-B in v0.2.102).
//
// THE DEFECT IT PINS (the owner-reported regression): the v0.2.99-era
// rework left the project card with no route at all. The v0.2.101 fix
// (owner ruling 2026-10-05, PLAN-V02101-PULL-IN §7) made a card click
// route into `/project/<id>/settings` — the surface with the bundle
// Update button, rename, repoint and unregister — and added the per-row
// ⚙ Settings button beside it. This file pins that BOTH the card body
// and the settings action are wired and route to the settings page, so
// the card can never go inert again silently.
//
// NOTE for the owner: the W5-B brief words it "the card routes to the
// project page and the settings action to settings". As written in code
// BOTH go to settings, per the 2026-10-05 ruling's own comment ("a card
// click routes into the project's SETTINGS page"). Splitting the card to
// the overview is a one-line change here + open() — an owner decision,
// not taken by this lane.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from './test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const PAGE = parse(
  readFileSync(resolve(here, '../routes/projects/+page.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;
const CARD = parse(
  readFileSync(resolve(here, 'components/ProjectCard.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

describe('Projects page — card routing (P299-C3)', () => {
  it('open() routes to the project SETTINGS page (the 2026-10-05 ruling)', () => {
    const fn = [...walk((PAGE.instance as Node).content)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'open'),
    );
    expect(fn, 'no open() function').toBeTruthy();
    const goto = [...walk(fn!)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'goto'),
    );
    expect(goto.length, 'open() never navigates').toBe(1);
    expect(
      [...walk(goto[0])].some(
        (n) => n.type === 'TemplateLiteral' && JSON.stringify(n).includes('/settings'),
      ),
      'open() does not route to /settings',
    ).toBe(true);
  });

  it('the per-row ⚙ Settings action calls open()', () => {
    const button = [...walk(PAGE.fragment)].find(
      (n) =>
        n.type === 'RegularElement' &&
        n.name === 'button' &&
        [...walk(n)].some(
          (x) => x.type === 'CallExpression' && isIdent((x as Node).callee, 'open'),
        ),
    );
    expect(button, 'no settings button calling open()').toBeTruthy();
  });

  it('the CARD body itself is wired: ProjectCard is rendered with onOpen={open}', () => {
    const card = [...walk(PAGE.fragment)].find(
      (n) => n.type === 'Component' && (n as Node).name === 'ProjectCard',
    );
    expect(card, 'the page never renders ProjectCard').toBeTruthy();
    expect(
      [...walk(card!)].some((n) => isIdent(n, 'open')),
      'ProjectCard does not receive open as onOpen',
    ).toBe(true);
  });

  it('ProjectCard is interactive: role=button, click AND keyboard route through onOpen', () => {
    const div = [...walk(CARD.fragment)].find(
      (n) =>
        n.type === 'RegularElement' &&
        n.name === 'div' &&
        (n.attributes as Node[]).some(
          (a) => a.type === 'Attribute' && a.name === 'role' && JSON.stringify(a.value).includes('button'),
        ),
    );
    expect(div, 'the card body has no role=button').toBeTruthy();
    for (const attr of ['onclick', 'onkeydown'] as const) {
      const handler = (div!.attributes as Node[]).find(
        (a) => a.type === 'Attribute' && a.name === attr,
      );
      expect(handler, `the card body has no ${attr}`).toBeTruthy();
    }
    // Both handlers reach onOpen (the keyboard handler via handleKey).
    const handlers = [...walk((CARD.instance as Node).content)].filter(
      (n) => n.type === 'FunctionDeclaration' && (isIdent(n.id, 'handleKey') || true),
    );
    expect(
      [...walk(handlers[0])].some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'onOpen'))
        || [...walk(CARD.fragment)].some(
          (n) => n.type === 'CallExpression' && isIdent(n.callee, 'onOpen'),
        ),
      'no handler calls onOpen',
    ).toBe(true);
  });
});
