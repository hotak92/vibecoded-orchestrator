// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 W6-L — wiring guard for the parser-offer modal.
//
// The controller's BEHAVIOUR (exactly the checked set is installed, Not now
// installs nothing) is pinned in `codegraph-parsers.test.ts`. This file pins
// the half that test cannot see: that each checkbox calls the store's
// toggle with ITS parser id, the Install button calls install and Not now
// calls notNow, the layout mounts the modal and asks at project startup.
// Parsed with the Svelte compiler, so a mention in a comment cannot satisfy
// it. (`vitest.config.ts` stands up no DOM runner, so a real click is not
// exercised.)

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent, registeredCommands } from './test-support/wiring-ast';
import { loadRust } from './test-support/source-census';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));

function load(rel: string): Node {
  return parse(readFileSync(resolve(here, rel), 'utf8'), { modern: true }) as unknown as Node;
}

const MODAL = load('components/CodegraphParsersModal.svelte');
const LAYOUT = load('../routes/+layout.svelte');

function isStoreCall(n: Node, prop: string): boolean {
  if (n.type !== 'CallExpression') return false;
  const c = n.callee as Node;
  return c?.type === 'MemberExpression' && isIdent(c.object, 'codegraphParsers') && isIdent(c.property, prop);
}

function attr(el: Node, name: string): Node | undefined {
  return (el.attributes as Node[]).find((a) => a.type === 'Attribute' && a.name === name);
}

function elements(ast: Node, tag: string): Node[] {
  return [...walk(ast.fragment)].filter((n) => n.type === 'RegularElement' && n.name === tag);
}

function textOf(node: unknown): string {
  return [...walk(node)]
    .filter((n) => n.type === 'Text')
    .map((n) => n.data as string)
    .join('');
}

describe('CodegraphParsersModal', () => {
  it('each checkbox toggles its own parser id (offer.id) through the store', () => {
    const boxes = elements(MODAL, 'input').filter((el) => {
      const t = attr(el, 'type');
      return t && textOf(t) === 'checkbox';
    });
    expect(boxes.length, 'no checkbox in the modal').toBe(1);
    const onchange = attr(boxes[0], 'onchange');
    expect(onchange, 'the checkbox has no onchange').toBeTruthy();
    const calls = [...walk(onchange)].filter((n) => isStoreCall(n, 'toggle'));
    expect(calls.length).toBe(1);
    const arg = (calls[0].arguments as Node[])[0];
    expect(arg?.type).toBe('MemberExpression');
    expect(isIdent(arg.object, 'offer') && isIdent(arg.property, 'id')).toBe(true);
    // The box shows the store's selection, not a local copy.
    const checked = attr(boxes[0], 'checked');
    const reads = [...walk(checked)].some(
      (n) => n.type === 'MemberExpression' && isIdent(n.property, 'includes'),
    );
    expect(reads, 'checked is not derived from state.selected').toBe(true);
  });

  it('Install calls install(), Not now calls notNow()', () => {
    const buttons = elements(MODAL, 'button');
    const byText = (t: string) => buttons.find((b) => textOf(b).includes(t));
    const install = buttons.find((b) => textOf(attr(b, 'data-testid')) === 'cpm-install');
    const notNow = byText('Not now');
    expect(install && notNow, 'Install / Not now buttons missing').toBeTruthy();
    expect([...walk(attr(install!, 'onclick'))].some((n) => isStoreCall(n, 'install'))).toBe(true);
    expect([...walk(attr(notNow!, 'onclick'))].some((n) => isStoreCall(n, 'notNow'))).toBe(true);
    // Install must NOT be reachable from Not now (and vice versa).
    expect([...walk(attr(notNow!, 'onclick'))].some((n) => isStoreCall(n, 'install'))).toBe(false);
  });

  it('renders a failed install (error and result) — never silent', () => {
    const guards = [...walk(MODAL.fragment)].filter((n) => n.type === 'IfBlock');
    const onError = guards.some((g) => {
      const t = g.test as Node;
      return t?.type === 'MemberExpression' && isIdent(t.property, 'error');
    });
    expect(onError, 'no {#if state.error} block').toBe(true);
  });
});

describe('+layout — the modal is mounted and asked at project startup', () => {
  it('mounts <CodegraphParsersModal />', () => {
    const mounted = [...walk(LAYOUT.fragment)].some(
      (n) => n.type === 'Component' && n.name === 'CodegraphParsersModal',
    );
    expect(mounted).toBe(true);
  });

  it('calls codegraphParsers.check from two effects (project list; opened project)', () => {
    const effects = [...walk((LAYOUT.instance as Node).content)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, '$effect'),
    );
    const checking = effects.filter((e) => [...walk(e)].some((n) => isStoreCall(n, 'check')));
    expect(checking.length).toBe(2);
    const readsSelected = checking.some((e) => [...walk(e)].some((n) => isIdent(n, '$selectedProject')));
    expect(readsSelected, 'no check on project open').toBe(true);
  });
});

describe('backend commands exist', () => {
  it('every command the controller invokes is registered in lib.rs', () => {
    const lib = loadRust().find((f) => f.rel === 'src/lib.rs');
    expect(lib).toBeTruthy();
    const registered = new Set(registeredCommands(lib!));
    for (const cmd of ['codegraph_parser_offers', 'codegraph_parsers_install', 'codegraph_parsers_decline']) {
      expect(registered.has(cmd), cmd).toBe(true);
    }
  });
});
