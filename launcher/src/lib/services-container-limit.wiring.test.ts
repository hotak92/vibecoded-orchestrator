// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.107 (lane CONTAINERS) — the Services page shows "managed / foreign"
// and Weaviate's memory limit, and a banner for an uncapped container. The
// decisions live in `$lib/api/service_endpoints` (unit-tested there); this
// file pins that the PAGE calls them and renders their result. It walks the
// AST the Svelte compiler produces, so a mention in a comment does not
// satisfy it (same shape as `services-routing-guidance.wiring.test.ts`). It
// does not prove a real DOM paints the line — `vitest.config.ts` has no
// component runner.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const PAGE = parse(readFileSync(resolve(here, '../routes/services/+page.svelte'), 'utf8'), {
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

function calls(root: unknown, name: string): Node[] {
  return [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));
}

function mentions(root: unknown, name: string): boolean {
  return [...walk(root)].some((n) => isIdent(n, name));
}

const fragment = PAGE.fragment;
const script = (PAGE.instance as Node).content;

describe('Services page — managed / foreign and the memory limit', () => {
  it('each row renders ownershipLine() of the container the detector inspected', () => {
    const line = calls(fragment, 'ownershipLine');
    expect(line.length).toBe(1);
    const args = line[0].arguments as Node[];
    expect(isIdent(args[0], 'svc')).toBe(true);
    // the second argument is containerOf(containerFacts, svc) — the row's own container
    const inner = args[1] as Node;
    expect(inner.type === 'CallExpression' && isIdent(inner.callee, 'containerOf')).toBe(true);
    expect(isIdent((inner.arguments as Node[])[0], 'containerFacts')).toBe(true);
  });

  it('the uncapped banner is derived from the snapshot and the detector reply, and rendered', () => {
    const banner = calls(script, 'uncappedBanner');
    expect(banner.length).toBe(1);
    const args = banner[0].arguments as Node[];
    expect(isIdent(args[0], 'snapshot')).toBe(true);
    expect(isIdent(args[1], 'containerFacts')).toBe(true);
    expect(mentions(fragment, 'weaviateUncapped')).toBe(true);
  });

  it('the detector is asked when the page opens, not on the 5 s poll', () => {
    const refreshFacts = calls(script, 'refreshContainerFacts');
    expect(refreshFacts.length).toBeGreaterThanOrEqual(1);
    const poll = calls(script, 'pollWhileActive');
    expect(poll.length).toBe(1);
    expect(calls(poll[0], 'refreshContainerFacts').length).toBe(0);
    expect(calls(poll[0], 'getEndpointCandidates').length).toBe(0);
  });
});
