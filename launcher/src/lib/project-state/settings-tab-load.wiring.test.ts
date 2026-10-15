// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the Settings surfaces' failure UX (v0.2.103, hardened
// by review R8 m1/M2).
//
// WHAT IT PINS:
//   - a failed load renders the error INLINE with a Retry that re-runs
//     the load — and the Retry control lives INSIDE the error branch
//     (the first draft accepted any `loadError` mention plus any `load()`
//     call anywhere in the template, which stays green when the button
//     moves out of the branch);
//   - the whole surface is wrapped in a <svelte:boundary> whose `failed`
//     snippet shows the error and calls reset() — a render-time throw can
//     never again leave a silent "Loading…" (that is exactly how the
//     v0.2.102 install-log key crash presented).
//
// Parsed with the Svelte compiler so a comment cannot satisfy it. The
// lifecycle BEHAVIOUR (reject → error → retry → success, no timeout)
// lives in and is pinned by load-controller.test.ts — this file pins
// only that the template is wired to it.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const TAB = parse(
  readFileSync(resolve(here, 'SettingsTab.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;
const ROUTE = parse(
  readFileSync(
    resolve(here, '../../routes/project/[id]/settings/+page.svelte'),
    'utf8',
  ),
  { modern: true },
) as unknown as Node;

/** True when an IfBlock testing `errorState` contains, WITHIN THAT
 *  BRANCH'S OWN CONTENT, a button whose handler calls `loadFn`. */
function errorBranchWithRetry(fragment: Node, loadFn: string, errorState: string): boolean {
  const ifBlocks = [...walk(fragment)].filter((n) => n.type === 'IfBlock');
  return ifBlocks.some((b) => {
    if (!JSON.stringify(b.test ?? {}).includes(errorState)) return false;
    const branch = [...walk(b.consequent ?? {})];
    return branch.some(
      (n) =>
        n.type === 'RegularElement' &&
        n.name === 'button' &&
        [...walk(n)].some(
          (x) => x.type === 'CallExpression' && isIdent(x.callee, loadFn),
        ),
    );
  });
}

/** True when the fragment has a <svelte:boundary> whose `failed` snippet
 *  renders something AND calls reset() (the retry). */
function boundaryWithReset(fragment: Node): boolean {
  return [...walk(fragment)].some(
    (n) =>
      n.type === 'SvelteBoundary' &&
      [...walk(n.fragment ?? {})].some(
        (s) =>
          s.type === 'SnippetBlock' &&
          isIdent(s.expression, 'failed') &&
          [...walk(s)].some(
            (x) => x.type === 'CallExpression' && isIdent(x.callee, 'reset'),
          ),
      ),
  );
}

function stateNames(instance: Node): string[] {
  const decls = [...walk(instance)].filter(
    (n) => n.type === 'VariableDeclaration',
  );
  return decls
    .filter((d) => JSON.stringify(d.declarations ?? []).includes('$state'))
    .flatMap((d) =>
      (d.declarations as Node[]).map(
        (dd) => String((dd.id as Node | undefined)?.name ?? ''),
      ),
    );
}

describe('SettingsTab — load failure UX (v0.2.103)', () => {
  it('tracks a load error in state', () => {
    expect(stateNames(TAB.instance as Node)).toContain('loadError');
  });

  it('a failed load surfaces the error and offers Retry INSIDE the error branch', () => {
    expect(errorBranchWithRetry(TAB.fragment as Node, 'load', 'loadError')).toBe(true);
  });

  it('the tab is wrapped in a svelte:boundary whose failed snippet retries via reset() (R8 M2)', () => {
    expect(boundaryWithReset(TAB.fragment as Node)).toBe(true);
  });
});

describe('Settings standalone route — load failure UX (v0.2.103)', () => {
  it('tracks a load error in state', () => {
    expect(stateNames(ROUTE.instance as Node)).toContain('loadError');
  });

  it('a failed load surfaces the error and offers Retry INSIDE the error branch', () => {
    expect(
      errorBranchWithRetry(ROUTE.fragment as Node, 'loadHeader', 'loadError'),
    ).toBe(true);
  });

  it('the SettingsTab mount is wrapped in a svelte:boundary whose failed snippet retries via reset() (R8 M2)', () => {
    expect(boundaryWithReset(ROUTE.fragment as Node)).toBe(true);
  });
});
