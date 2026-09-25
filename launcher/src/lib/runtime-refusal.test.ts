// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 review R11 L6 — the install preflight modal shows WHY the stale
// runtime record was not switched.
//
// The pure half: `notSwitchedReason` against every shape. The wiring half:
// `InstallPreflightRuntimeModal.svelte` derives it from the availability it
// shows and renders it on the refused-pin branch — parsed with the Svelte
// compiler, so a mention in a comment does not satisfy it (the reasoning of
// `module-health.wiring.test.ts`). It does not prove a real DOM paints the
// line — `vitest.config.ts` has no component runner.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

import { notSwitchedReason } from './runtime-refusal';

type Node = Record<string, unknown>;

describe('notSwitchedReason', () => {
  it('shows the backend reason on the refused-pin path', () => {
    expect(
      notSwitchedReason({ pinned_unusable: true, not_switched: ' podman holds none of VCO\'s data ' }),
    ).toBe("podman holds none of VCO's data");
  });

  it('shows nothing without a refused pin or without a reason', () => {
    expect(notSwitchedReason({ pinned_unusable: false, not_switched: 'x' })).toBeNull();
    expect(notSwitchedReason({ pinned_unusable: true, not_switched: null })).toBeNull();
    expect(notSwitchedReason({ pinned_unusable: true, not_switched: '   ' })).toBeNull();
    expect(notSwitchedReason({ pinned_unusable: true })).toBeNull();
    expect(notSwitchedReason(null)).toBeNull();
  });
});

const here = dirname(fileURLToPath(import.meta.url));
const MODAL = parse(
  readFileSync(resolve(here, 'components/InstallPreflightRuntimeModal.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

function* walk(node: unknown): Generator<Node> {
  if (node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const obj = node as Node;
  if (typeof obj.type === 'string') yield obj;
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'parent' || k === 'loc') continue;
    yield* walk(v);
  }
}

function isIdent(n: unknown, name: string): boolean {
  return (n as Node | undefined)?.type === 'Identifier' && (n as Node).name === name;
}

describe('InstallPreflightRuntimeModal — not-switched wiring', () => {
  it('derives the reason from the availability it shows', () => {
    const derived = [...walk((MODAL.instance as Node).content)].filter(
      (n) =>
        n.type === 'VariableDeclarator' &&
        isIdent(n.id, 'notSwitched') &&
        (n.init as Node | undefined)?.type === 'CallExpression' &&
        isIdent((n.init as Node).callee, '$derived'),
    );
    expect(derived.length).toBe(1);
    const inner = ((derived[0].init as Node).arguments as Node[])[0];
    expect(inner.type).toBe('CallExpression');
    expect(isIdent(inner.callee, 'notSwitchedReason')).toBe(true);
    expect(isIdent((inner.arguments as Node[])[0], 'runtimeInfo')).toBe(true);
  });

  it('renders it inside the refused-pin branch', () => {
    const pinnedIf = [...walk(MODAL.fragment)].find(
      (n) => n.type === 'IfBlock' && isIdent(n.test, 'pinnedUnusable'),
    );
    expect(pinnedIf).toBeDefined();
    const shown = [...walk((pinnedIf as Node).consequent)].find(
      (n) => n.type === 'IfBlock' && isIdent(n.test, 'notSwitched'),
    );
    expect(shown).toBeDefined();
    const tags = [...walk((shown as Node).consequent)].filter(
      (n) => n.type === 'ExpressionTag' && isIdent(n.expression, 'notSwitched'),
    );
    expect(tags.length).toBe(1);
  });
});
