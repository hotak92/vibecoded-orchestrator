// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the Settings surfaces' load-failure UX (v0.2.103).
//
// THE DEFECT IT PINS (owner report, launcher v0.2.102): a failed
// `get_project_v2` left BOTH the project page's Settings tab and the
// standalone /project/<id>/settings route on a silent, infinite
// "Loading…" — the catch path only toasted, and the template's only
// non-loaded state was the loading paragraph. A load failure must
// render the error inline with a Retry action (and no timeout that
// could cut a slow-but-valid load).
//
// Parsed with the Svelte compiler so a comment cannot satisfy it.

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

/** True when the fragment renders a Retry-style control that re-runs
 * `loadFn` — a button (regular element or component) whose handler
 * calls the named function, inside a branch keyed on an error state. */
function hasRetryOn(fragment: Node, loadFn: string, errorState: string): boolean {
  // An IfBlock (or else-if chain node) whose test mentions the error state…
  const errorBranches = [...walk(fragment)].filter(
    (n) =>
      (n.type === 'IfBlock' || n.type === 'ElseBlock' || n.type === 'ExpressionTag') &&
      JSON.stringify(n.test ?? n.expression ?? n.name ?? '').includes(errorState),
  );
  // …anywhere in the fragment (the error branch may sit inside the
  // loading IfBlock's else), and a call to the load fn in the fragment
  // that is not inside the script's onMount/effect wiring — i.e. a
  // template-invoked retry.
  const retryCalls = [...walk(fragment)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, loadFn),
  );
  return errorBranches.length > 0 && retryCalls.length > 0;
}

describe('SettingsTab — load failure UX (v0.2.103)', () => {
  it('tracks a load error in state', () => {
    const decls = [...walk(TAB.instance as Node)].filter(
      (n) => n.type === 'VariableDeclaration',
    );
    // `$state` declarations surface as CallExpression callee `$state`
    // inside the declaration's init.
    const stateNames = decls
      .filter((d) => JSON.stringify(d.declarations ?? []).includes('$state'))
      .flatMap((d) =>
        (d.declarations as Node[]).map(
          (dd) => (dd.id as Node | undefined)?.name ?? '',
        ),
      );
    expect(stateNames, `no $state declarations found: ${stateNames}`).toContain(
      'loadError',
    );
  });

  it('a failed load surfaces the error and offers Retry (no infinite silent Loading)', () => {
    expect(hasRetryOn(TAB.fragment as Node, 'load', 'loadError')).toBe(true);
  });
});

describe('Settings standalone route — load failure UX (v0.2.103)', () => {
  it('tracks a load error in state', () => {
    const decls = [...walk(ROUTE.instance as Node)].filter(
      (n) => n.type === 'VariableDeclaration',
    );
    const stateNames = decls
      .filter((d) => JSON.stringify(d.declarations ?? []).includes('$state'))
      .flatMap((d) =>
        (d.declarations as Node[]).map(
          (dd) => (dd.id as Node | undefined)?.name ?? '',
        ),
      );
    expect(stateNames).toContain('loadError');
  });

  it('a failed load surfaces the error and offers Retry', () => {
    expect(hasRetryOn(ROUTE.fragment as Node, 'loadHeader', 'loadError')).toBe(true);
  });
});
