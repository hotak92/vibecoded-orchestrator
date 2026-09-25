// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (service endpoints SSOT) — wiring pins for the Services page and
// the adoption dialog. `$lib/api/service_endpoints.test.ts` pins every
// decision against the pure functions; this file pins the half a pure test
// cannot see: that the markup RENDERS those functions' output (the mode
// badge, the row's actions, the dialog's verbs) — parsed with the Svelte
// compiler, so a name in a comment does not satisfy it (same technique as
// `lib/module-health.wiring.test.ts`). It does not prove a DOM paints them:
// `vitest.config.ts` has no component runner.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const PAGE = parse(readFileSync(resolve(here, '+page.svelte'), 'utf8'), { modern: true }) as unknown as Node;
const DIALOG = parse(
  readFileSync(resolve(here, '../../lib/components/ExternalServicesDialog.svelte'), 'utf8'),
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

const isIdent = (n: unknown, name: string) =>
  (n as Node | undefined)?.type === 'Identifier' && (n as Node).name === name;

function calls(root: unknown, name: string): Node[] {
  return [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));
}

/** Text the markup renders (Text nodes — comments are not Text). */
function renderedText(root: Node): string {
  return [...walk(root.fragment)]
    .filter((n) => n.type === 'Text')
    .map((n) => String(n.data))
    .join(' ');
}

describe('Services page wiring', () => {
  it('renders the rows from the snapshot and each row’s badge from modeBadge(svc)', () => {
    const each = [...walk(PAGE.fragment)].find(
      (n) =>
        n.type === 'EachBlock' &&
        (n.expression as Node)?.type === 'MemberExpression' &&
        isIdent((n.expression as Node).object, 'snapshot') &&
        isIdent((n.expression as Node).property, 'services'),
    );
    expect(each, 'an {#each snapshot.services as svc}').toBeDefined();
    const badge = calls(each, 'modeBadge');
    expect(badge.length).toBe(1);
    expect(isIdent((badge[0].arguments as Node[])[0], 'svc')).toBe(true);
  });

  it('renders the row’s buttons from serviceActions(svc) — the only source of row actions', () => {
    const acts = calls(PAGE.fragment, 'serviceActions');
    expect(acts.length).toBe(1);
    expect(isIdent((acts[0].arguments as Node[])[0], 'svc')).toBe(true);
    const inner = [...walk(PAGE.fragment)].find(
      (n) => n.type === 'EachBlock' && calls(n.expression, 'serviceActions').length === 1,
    );
    expect(inner, '{#each serviceActions(svc) as action}').toBeDefined();
    // The button dispatches through onRowAction(svc, action).
    const dispatch = calls(inner, 'onRowAction');
    expect(dispatch.length).toBe(1);
  });

  it('never renders "Refuse" or "Reset adoption"', () => {
    const text = renderedText(PAGE).toLowerCase();
    expect(text).not.toContain('refuse');
    expect(text).not.toContain('reset adoption');
    expect(calls((PAGE.instance as Node).content, 'invoke').map((c) => (c.arguments as Node[])[0]?.value)).not.toContain(
      'services_reset_adoption',
    );
  });

  it('"Let VCO manage it" runs the hand_to_vco verb from the confirmation', () => {
    const verbs = calls((PAGE.instance as Node).content, 'runEndpointAction');
    const hand = verbs.find((c) =>
      [...walk(c.arguments)].some((n) => n.type === 'Literal' && n.value === 'hand_to_vco'),
    );
    expect(hand, 'runEndpointAction({ action: "hand_to_vco", … })').toBeDefined();
  });
});

describe('Move to another port wiring (R7b F22)', () => {
  it('each row renders moveOffer(svc), and the button only for kind "move"', () => {
    const each = [...walk(PAGE.fragment)].find(
      (n) =>
        n.type === 'EachBlock' &&
        (n.expression as Node)?.type === 'MemberExpression' &&
        isIdent((n.expression as Node).object, 'snapshot'),
    );
    const offers = calls(each, 'moveOffer');
    expect(offers.length).toBe(1);
    expect(isIdent((offers[0].arguments as Node[])[0], 'svc')).toBe(true);
    // The button that opens the dialog sits under an {#if move.kind === 'move'}.
    const guarded = [...walk(each)].find(
      (n) =>
        n.type === 'IfBlock' &&
        [...walk(n.test)].some((t) => t.type === 'Literal' && t.value === 'move') &&
        calls((n as Node).consequent, 'openMove').length === 1,
    );
    expect(guarded, "{#if move.kind === 'move'} … openMove(…)").toBeDefined();
  });

  it('the dialog sends moveRequest(…) through moveService, then refreshes', () => {
    const script = (PAGE.instance as Node).content;
    const sends = calls(script, 'moveService');
    expect(sends.length).toBe(1);
    expect(calls(sends[0], 'moveRequest').length).toBe(1);
    const confirm = [...walk(script)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'confirmMove'),
    );
    expect(confirm && calls(confirm, 'refresh').length).toBe(1);
  });
});

describe('Adoption dialog wiring', () => {
  it('"Use this one" sends adoptActionFor(service, c); "Run VCO’s own copy" sends vcoCopyAction', () => {
    const runs = calls(DIALOG.fragment, 'run');
    const argCallees = runs.map((r) => ((r.arguments as Node[])[1] as Node)?.callee as Node);
    expect(argCallees.some((c) => isIdent(c, 'adoptActionFor'))).toBe(true);
    expect(argCallees.some((c) => isIdent(c, 'vcoCopyAction'))).toBe(true);
    // The verb goes to the rows' one writer.
    expect(calls((DIALOG.instance as Node).content, 'runEndpointAction').length).toBe(1);
  });

  it('listens for the boot event and opens only for pending choices', () => {
    const listens = calls((DIALOG.instance as Node).content, 'listen').filter(
      (c) => ((c.arguments as Node[])[0] as Node)?.value === 'vct-external-services-detected',
    );
    expect(listens.length).toBe(1);
    expect(calls(listens[0], 'pendingServices').length).toBe(1);
  });

  it('offers no refuse/reset choice', () => {
    const text = renderedText(DIALOG).toLowerCase();
    expect(text).not.toContain('refuse');
    expect(text).not.toContain('reset');
  });
});
