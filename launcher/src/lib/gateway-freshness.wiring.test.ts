// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the stale-gateway restart affordance (v0.2.101, P299-A2).
//
// THE DEFECT IT PINS: a stale gateway was restartable from the GUI only
// through a modal a single Dismiss suppressed forever; the usage card showed
// `outdated_gateway` with no way to act on it, and the Services page had no
// freshness surface at all (a failed check was console-only).
//
// WHAT IS TESTED WHERE: offer()'s BEHAVIOUR (dismissal bypass, never
// restarting without the modal's Continue, visible failure) is pinned in
// `gateway-freshness.test.ts`; the pure card condition in
// `subscription-usage.test.ts`; the store shell in
// `stores/gateway-freshness.test.ts`. This file pins the half those cannot
// see: that the two GUI surfaces actually CALL offer(), that the card's
// button sits behind the `restartOffered` guard (and only it), and that the
// Services page renders the store's failure state. Parsed with the Svelte
// compiler so a mention in a comment does not satisfy it (same shape as
// `bundle-staleness.wiring.test.ts`).
//
// WHAT IT STILL DOES NOT PROVE: that a click reaches the handler in a real
// DOM — `vitest.config.ts` stands up no component runner.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from './test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));

function load(rel: string): Node {
  const src = readFileSync(resolve(here, rel), 'utf8');
  return parse(src, { modern: true }) as unknown as Node;
}

const CARD = load('components/SubscriptionUsageCard.svelte');
const SERVICES = load('../routes/services/+page.svelte');

/** `obj.prop` (an optional member is wrapped in a ChainExpression). */
function isMember(n: unknown, obj: string, prop: string): boolean {
  let node = n as Node | undefined;
  if (node?.type === 'ChainExpression') node = node.expression as Node;
  return (
    node?.type === 'MemberExpression' && isIdent(node.object, obj) && isIdent(node.property, prop)
  );
}

/** Calls of `obj.prop(...)` anywhere in a subtree. */
function memberCalls(root: unknown, obj: string, prop: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isMember(n.callee, obj, prop),
  );
}

/** Every node in a subtree, paired with its chain of ancestors. */
function* walkWithAncestors(
  node: unknown,
  ancestors: Node[] = [],
): Generator<[Node, Node[]]> {
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

/** The one "Restart gateway…" button of a parsed component, with ancestors. */
function restartButton(ast: Node): [Node, Node[]] | undefined {
  return [...walkWithAncestors(ast.fragment)].find(
    ([n]) =>
      n.type === 'RegularElement' && n.name === 'button' && textOf(n).includes('Restart gateway'),
  );
}

/** The onclick's calls of a named local handler. */
function onclickHandlerCalls(button: Node, handler: string): Node[] {
  const onclick = (button.attributes as Node[]).find(
    (a) => a.type === 'Attribute' && a.name === 'onclick',
  );
  if (!onclick) return [];
  return [...walk(onclick)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, handler),
  );
}

function textOf(node: unknown): string {
  return [...walk(node)]
    .filter((n) => n.type === 'Text')
    .map((n) => n.data as string)
    .join('');
}

/** The nearest `{#if}` enclosing a node, or undefined. */
function nearestIf(ancestors: Node[]): Node | undefined {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    if (ancestors[i].type === 'IfBlock') return ancestors[i];
  }
  return undefined;
}

describe('SubscriptionUsageCard — "Restart gateway…" affordance (v0.2.101, P299-A2)', () => {
  it('routes its button through offerGatewayRestart() to gatewayFreshness.offer()', () => {
    const found = restartButton(CARD);
    expect(found, 'no "Restart gateway…" button in the card').toBeTruthy();
    const [button] = found!;
    const handlerCalls = onclickHandlerCalls(button, 'offerGatewayRestart');
    expect(handlerCalls.length, 'onclick does not call offerGatewayRestart()').toBe(1);
    // …and the handler itself reaches the store's offer (instance script).
    const fn = [...walk((CARD.instance as Node).content)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'offerGatewayRestart'),
    );
    expect(fn, 'no offerGatewayRestart() in the card instance script').toBeTruthy();
    expect(memberCalls(fn, 'gatewayFreshness', 'offer').length).toBe(1);
  });

  it("the button's nearest guard is restartOffered — the outdated_gateway-only condition", () => {
    const found = restartButton(CARD);
    expect(found, 'the offer button disappeared').toBeTruthy();
    // (Red-proof mutation: replace the `{#if restartOffered(result)}` guard
    // with a weaker one — e.g. `{#if result != null}` — and this fails: the
    // nearest enclosing {#if} no longer calls restartOffered.)
    const guard = nearestIf(found![1]);
    expect(guard, 'the button has no {#if} guard at all').toBeTruthy();
    const test = guard!.test as Node;
    expect(test.type).toBe('CallExpression');
    expect(isIdent(test.callee, 'restartOffered')).toBe(true);
  });

  it('renders freshnessNote — a non-stale verdict answers the click, not silence (review nit 2)', () => {
    const noteGuard = [...walk(CARD.fragment)].some(
      (n) => n.type === 'IfBlock' && isIdent(n.test, 'freshnessNote'),
    );
    expect(noteGuard, 'the card never renders {#if freshnessNote}').toBe(true);
  });
});

describe('Services page — "Restart gateway…" affordance (v0.2.101, P299-A2)', () => {
  it('wires a button through offerGatewayRestart() to gatewayFreshness.offer()', () => {
    const found = restartButton(SERVICES);
    expect(found, 'no "Restart gateway…" button in the markup').toBeTruthy();
    const handlerCalls = onclickHandlerCalls(found![0], 'offerGatewayRestart');
    expect(handlerCalls.length, 'onclick does not call offerGatewayRestart()').toBe(1);
    // …and the handler itself reaches the store's offer (instance script).
    const fn = [...walk((SERVICES.instance as Node).content)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'offerGatewayRestart'),
    );
    expect(fn, 'no offerGatewayRestart() in the instance script').toBeTruthy();
    expect(memberCalls(fn, 'gatewayFreshness', 'offer').length).toBe(1);
  });

  it('renders freshnessNote — the button is never a silent no-op', () => {
    const noteGuard = [...walk(SERVICES.fragment)].some(
      (n) => n.type === 'IfBlock' && isIdent(n.test, 'freshnessNote'),
    );
    expect(noteGuard, 'the Services page never renders {#if freshnessNote}').toBe(true);
  });

  it('subscribes to the freshness store and renders its error — a failed check is visible, not console-only', () => {
    // const gwFreshness = $derived($gatewayFreshness);
    const subscribed = [...walk((SERVICES.instance as Node).content)].some(
      (n) =>
        n.type === 'CallExpression' &&
        isIdent(n.callee, '$derived') &&
        (n.arguments as Node[]).some((a) => isIdent(a, '$gatewayFreshness')),
    );
    expect(subscribed, 'the page never derives from $gatewayFreshness').toBe(true);
    // {#if gwFreshness.error} … {gwFreshness.error} …
    // (Red-proof mutation: remove the banner and this fails — the failure
    // state would exist in the store but reach no UI.)
    const guards = [...walk(SERVICES.fragment)].filter(
      (n) => n.type === 'IfBlock' && isMember(n.test, 'gwFreshness', 'error'),
    );
    expect(guards.length, 'no {#if gwFreshness.error} banner').toBe(1);
    const rendered = [...walk(guards[0])].some(
      (n) => n.type === 'ExpressionTag' && isMember(n.expression, 'gwFreshness', 'error'),
    );
    expect(rendered, 'the banner never renders the error text').toBe(true);
  });
});
