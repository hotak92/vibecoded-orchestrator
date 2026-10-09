// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the project Settings "Install log" panel (v0.2.102,
// W5-B C8). `read_install_log` was a registered command with no caller
// (owner-deferred); SettingsTab is the reachable component. Parsed with
// the Svelte compiler so a comment cannot satisfy it.

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

function fnBody(name: string): Node {
  const fn = [...walk((TAB.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
  expect(fn, `no function declaration named ${name}`).toBeTruthy();
  return fn!;
}

describe('SettingsTab — Install log panel (W5-B C8)', () => {
  it('parses', () => {
    expect([...walk(TAB)].length).toBeGreaterThan(100);
  });

  it('loads the log through read_install_log', () => {
    const invokes = [...walk(fnBody('loadInstallLog'))].filter(
      (n) =>
        n.type === 'CallExpression' &&
        isIdent(n.callee, 'invoke') &&
        (n.arguments as Node[])[0]?.type === 'Literal' &&
        ((n.arguments as Node[])[0] as Node).value === 'read_install_log',
    );
    expect(invokes.length).toBe(1);
  });

  it('loads on mount (the panel never renders a stale nothing)', () => {
    const onMountCalls = [...walk((TAB.instance as Node).content)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'onMount'),
    );
    expect(
      [...walk(onMountCalls)].some((n) => isIdent(n, 'loadInstallLog')),
      'no onMount reaches loadInstallLog',
    ).toBe(true);
  });

  it('renders the tail, the summary and the copy action', () => {
    const fragment = [...walk(TAB.fragment)];
    expect(
      fragment.some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'tailEvents')),
      'the panel never renders the tail',
    ).toBe(true);
    expect(
      fragment.some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'summaryLine')),
      'the panel never renders the summary line',
    ).toBe(true);
    // Copy goes through copyText (the one payload shape, pinned in the
    // pure test) — not an ad-hoc string build.
    expect(
      [...walk(fnBody('copyInstallLog'))].some(
        (n) => n.type === 'CallExpression' && isIdent(n.callee, 'copyText'),
      ),
      'copy does not use the shared copyText builder',
    ).toBe(true);
  });
});
