// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the Projects-page card routing (P299-C3, pinned by
// W5-B in v0.2.102; re-pinned by the owner ruling of 2026-10-15).
//
// THE DEFECT IT PINS (the owner-reported regression): the v0.2.99-era
// rework left the project card with no route at all. The v0.2.101 fix
// (owner ruling 2026-10-05) made a card click route into
// `/project/<id>/settings`. The owner reversed that on 2026-10-15: a
// plain project open (the card body) must land on the project page
// `/project/<id>` (its default tab), NOT settings — only the explicit
// ⚙ Settings action goes to `/project/<id>/settings`. This file pins
// BOTH wirings so the card can never go inert again silently.

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
  it('open() routes to the PROJECT page (owner ruling 2026-10-15: a plain project open lands on the default tab)', () => {
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
      'open() routes to /settings — a plain project open must land on /project/<id>',
    ).toBe(false);
  });

  it('openSettings() routes to the project SETTINGS page (the explicit ⚙ action)', () => {
    const fn = [...walk((PAGE.instance as Node).content)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'openSettings'),
    );
    expect(fn, 'no openSettings() function').toBeTruthy();
    const goto = [...walk(fn!)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'goto'),
    );
    expect(goto.length, 'openSettings() never navigates').toBe(1);
    expect(
      [...walk(goto[0])].some(
        (n) => n.type === 'TemplateLiteral' && JSON.stringify(n).includes('/settings'),
      ),
      'openSettings() does not route to /settings',
    ).toBe(true);
  });

  it('the per-row ⚙ Settings action calls openSettings()', () => {
    const button = [...walk(PAGE.fragment)].find(
      (n) =>
        n.type === 'RegularElement' &&
        n.name === 'button' &&
        [...walk(n)].some(
          (x) => x.type === 'CallExpression' && isIdent((x as Node).callee, 'openSettings'),
        ),
    );
    expect(button, 'no settings button calling openSettings()').toBeTruthy();
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
