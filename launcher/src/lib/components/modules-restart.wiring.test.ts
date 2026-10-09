// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the generic per-module Restart (v0.2.102, W5-B OD-11).
//
// THE DEFECT IT PINS: `restart_rl_container` was a registered command
// with NO GUI caller (owner-deferred; the census tracked it). OD-11 built
// the generic `restart_module_container` on the module tile and retired
// the RL-specific command. This pins that the tile's Restart button is
// reachable, guarded by the same container/runtime condition as Start,
// confirm-gated (restart drops in-flight module work), and that the
// cancel arm returns before the invoke.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const CATALOG = parse(
  readFileSync(resolve(here, 'ModuleCatalog.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

function fnBody(name: string): Node {
  const fn = [...walk((CATALOG.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
  expect(fn, `no function declaration named ${name}`).toBeTruthy();
  return fn!;
}

/** Every node in a subtree, paired with its chain of ancestors. */
function* walkWithAncestors(node: unknown, ancestors: Node[] = []): Generator<[Node, Node[]]> {
  if (node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walkWithAncestors(child, ancestors);
    return;
  }
  const obj = node as Node;
  const isNode = typeof obj.type === 'string';
  if (isNode) yield [obj, ancestors];
  const next = isNode ? [...ancestors, obj] : ancestors;
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'parent' || k === 'loc') continue;
    yield* walkWithAncestors(v, next);
  }
}

function attribute(node: Node, name: string): Node | undefined {
  return ((node.attributes as Node[]) ?? []).find(
    (a) => a.type === 'Attribute' && a.name === name,
  );
}

/** The value of a static attribute like data-testid. */
function staticAttr(node: Node, name: string): string | undefined {
  const attr = attribute(node, name);
  if (!attr) return undefined;
  const val = attr.value;
  const nodes = Array.isArray(val) ? val : [val];
  for (const v of nodes) {
    if ((v as Node)?.type === 'Text') return (v as Node).data as string;
  }
  return undefined;
}

describe('ModuleCatalog — generic module Restart (W5-B OD-11)', () => {
  it('parses', () => {
    expect([...walk(CATALOG)].length).toBeGreaterThan(100);
  });

  it('the tile renders a Restart button guarded by container_name AND runtime type', () => {
    const found = [...walkWithAncestors(CATALOG.fragment)].find(
      ([n]) =>
        n.type === 'RegularElement' && n.name === 'button' && staticAttr(n, 'data-testid') === 'btn-restart-container',
    );
    expect(found, 'no [data-testid=btn-restart-container] button').toBeTruthy();
    const [button, ancestors] = found!;
    // onclick → handleRestartContainer (an arrow handler wrapping the call).
    const onclick = attribute(button, 'onclick');
    expect(onclick).toBeTruthy();
    expect(
      [...walk(onclick)].some(
        (n) => n.type === 'CallExpression' && isIdent(n.callee, 'handleRestartContainer'),
      ),
      'the Restart button does not call handleRestartContainer',
    ).toBe(true);
    // Nearest {#if} guard: container_name truthy AND isLongRunningRuntime(...).
    let guard: Node | undefined;
    for (let i = ancestors.length - 1; i >= 0; i--) {
      if (ancestors[i].type === 'IfBlock') {
        guard = ancestors[i];
        break;
      }
    }
    expect(guard, 'the Restart button has no {#if} guard').toBeTruthy();
    const guardNodes = [...walk(guard!.test as Node)];
    expect(
      guardNodes.some((n) => n.type === 'MemberExpression' && isIdent((n as Node).object, 'display.install_row') === false && JSON.stringify(n).includes('container_name')),
      'the guard does not read container_name',
    ).toBe(true);
    expect(
      guardNodes.some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'isLongRunningRuntime')),
      'the guard does not check the runtime type',
    ).toBe(true);
  });

  it('reaches restart_module_container through confirm, with a cancel arm', () => {
    const fn = fnBody('handleRestartContainer');
    const nodes = [...walk(fn)];
    const confirmAt = nodes.findIndex(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'confirm'),
    );
    const invokeAt = nodes.findIndex(
      (n) =>
        n.type === 'CallExpression' &&
        isIdent(n.callee, 'invoke') &&
        (n.arguments as Node[])[0]?.type === 'Literal' &&
        ((n.arguments as Node[])[0] as Node).value === 'restart_module_container',
    );
    expect(confirmAt, 'no confirm() before the restart').toBeGreaterThanOrEqual(0);
    expect(invokeAt, 'no invoke of restart_module_container').toBeGreaterThanOrEqual(0);
    expect(invokeAt, 'the restart runs without consent').toBeGreaterThan(confirmAt);
    expect(
      nodes.some((n) => n.type === 'ReturnStatement'),
      'no cancel-arm early return',
    ).toBe(true);
    // The confirm text names the consequence (in-flight work is dropped).
    const confirmCall = nodes[confirmAt] as Node;
    expect(JSON.stringify((confirmCall.arguments as Node[])[0])).toContain('dropped');
  });

  it('passes BOTH ids (project + module) — the generic command is not RL-pinned', () => {
    const fn = fnBody('handleRestartContainer');
    const invoke = [...walk(fn)].find(
      (n) =>
        n.type === 'CallExpression' &&
        isIdent(n.callee, 'invoke') &&
        ((n.arguments as Node[])[0] as Node)?.value === 'restart_module_container',
    )!;
    const args = [...walk(invoke)];
    expect(args.some((n) => isIdent(n, 'projectId') || isIdent(n, 'project'))).toBe(true);
    expect(args.some((n) => isIdent(n, 'moduleId') || isIdent(n, 'm'))).toBe(true);
    // No RL-specific literal anywhere in the handler.
    expect(JSON.stringify(fn)).not.toContain('vct-rl-reranker');
  });
});
