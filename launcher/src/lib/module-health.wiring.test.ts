// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the module-tile health pill (v0.2.97, lane V).
//
// `module-health.test.ts` pins every pill state against the pure resolver.
// This file pins the half a pure test cannot see: that `ModuleCatalog.svelte`
// READS the hub's snapshot (`invoke('module_health_snapshot')`) and RENDERS
// the resolver's pill on each tile, fed with the snapshot, the tile's module
// id, the current project and the read error. Parsed with the Svelte
// compiler, so a mention in a comment does not satisfy it (same reasoning as
// `bundle-staleness.wiring.test.ts`). It does not prove a real DOM paints
// the pill — `vitest.config.ts` has no component runner.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const CATALOG = parse(readFileSync(resolve(here, 'components/ModuleCatalog.svelte'), 'utf8'), {
  modern: true,
}) as unknown as Node;

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

/** `obj.prop` or `obj?.prop` (an optional member is wrapped in a ChainExpression). */
function isMember(n: unknown, obj: string, prop: string): boolean {
  let node = n as Node | undefined;
  if (node?.type === 'ChainExpression') node = node.expression as Node;
  return (
    node?.type === 'MemberExpression' && isIdent(node.object, obj) && isIdent(node.property, prop)
  );
}

function calls(root: unknown, name: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, name),
  );
}

describe('ModuleCatalog — health pill wiring', () => {
  it('reads the snapshot from the hub command', () => {
    const reads = calls((CATALOG.instance as Node).content, 'invoke').filter((c) => {
      const a = (c.arguments as Node[])[0];
      return a?.type === 'Literal' && a.value === 'module_health_snapshot';
    });
    expect(reads.length).toBe(1);
  });

  it('renders the resolver pill per tile from the snapshot, module, project and error', () => {
    const resolved = calls(CATALOG.fragment, 'resolveModuleHealthPill');
    expect(resolved.length).toBe(1);
    const args = resolved[0].arguments as Node[];
    expect(isIdent(args[0], 'healthSnapshot')).toBe(true);
    const moduleArg = args[1];
    expect(moduleArg.type).toBe('MemberExpression');
    expect(isIdent(moduleArg.object, 'm') && isIdent(moduleArg.property, 'id')).toBe(true);
    // R7b F26 / R8 G7: the CURRENT project's id picks the per-project health
    // row — `project?.id`, falling back to null (machine-wide health) only
    // when no project is open.
    const projectArg = args[2];
    const projectId =
      projectArg?.type === 'LogicalExpression' && projectArg.operator === '??'
        ? projectArg.left
        : projectArg;
    expect(isMember(projectId, 'project', 'id')).toBe(true);
    if (projectArg?.type === 'LogicalExpression') {
      const fallback = projectArg.right as Node;
      expect(fallback.type === 'Literal' && fallback.value === null).toBe(true);
    }
    expect(isIdent(args[3], 'healthError')).toBe(true);

    // The {@const} result is what an {#if} renders.
    const ifs = [...walk(CATALOG.fragment)].filter(
      (n) => n.type === 'IfBlock' && isIdent(n.test, 'healthPill'),
    );
    expect(ifs.length).toBe(1);
    const pill = [...walk(ifs[0].consequent)].find(
      (n) =>
        n.type === 'RegularElement' &&
        (n.attributes as Node[]).some(
          (a) =>
            a.type === 'Attribute' &&
            a.name === 'data-testid' &&
            Array.isArray(a.value) &&
            (a.value as Node[])[0]?.data === 'module-health-pill',
        ),
    );
    expect(pill).toBeDefined();
  });
});
