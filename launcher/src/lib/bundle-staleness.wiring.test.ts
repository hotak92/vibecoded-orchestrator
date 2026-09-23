// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the bundle-staleness census triggers (v0.2.97).
//
// THE DEFECT IT PINS: after a successful "Update all" the Projects page kept
// showing "8 stale of 9 project bundles" and a "Bundle stale" chip on every
// card, while the census itself (`python -m vco_lib.bundle_staleness`)
// reported every project current. The census was only re-taken on mount and
// on the orchestrator-update edge; the modal reaching `done` told the page
// nothing, and Refresh reloaded only the project list.
//
// WHAT IS TESTED WHERE: every trigger's BEHAVIOUR (fires / does not fire,
// newest response wins) is tested against `createCensusController` in
// `bundle-staleness.test.ts`. This file pins that the two Svelte files
// actually CALL those triggers — the half a pure-function test cannot see,
// and the half that was missing.
//
// WHY THE AST AND NOT A TEXT SEARCH: a grep for `onFinished` is satisfied by
// the comments that explain it. Only a syntax node distinguishes a call from
// prose about a call (same reasoning as `SecretsPanel.wiring.test.ts`).
//
// WHAT IT STILL DOES NOT PROVE: that a click reaches the handler in a real
// DOM. `vitest.config.ts` stands up no component runner; if one is ever
// added, replace these structural assertions with render + click tests.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));

function load(rel: string): Node {
  const src = readFileSync(resolve(here, rel), 'utf8');
  return parse(src, { modern: true }) as unknown as Node;
}

const MODAL = load('components/UpdateAllProjectsModal.svelte');
const PAGE = load('../routes/projects/+page.svelte');

/** Every node in a subtree, depth-first. */
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

/** Every node in a subtree, each paired with its chain of ancestors. */
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

function isIdent(n: unknown, name: string): boolean {
  return (n as Node | undefined)?.type === 'Identifier' && (n as Node).name === name;
}

/** `obj.prop` */
function isMember(n: unknown, obj: string, prop: string): boolean {
  const m = n as Node | undefined;
  return m?.type === 'MemberExpression' && isIdent(m.object, obj) && isIdent(m.property, prop);
}

/** Calls (optional or not) whose callee is the identifier `name`. */
function callsOf(root: unknown, name: string): Node[] {
  return [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));
}

/** Calls whose callee is `obj.prop`. */
function memberCallsOf(root: unknown, obj: string, prop: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isMember(n.callee, obj, prop),
  );
}

/** The instance-script function declaration named `name`. */
function fnDecl(ast: Node, name: string): Node | undefined {
  return [...walk((ast.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
}

function enclosingFnName(ancestors: Node[]): string | null {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const a = ancestors[i];
    if (a.type === 'FunctionDeclaration') return ((a.id as Node).name as string) ?? null;
  }
  return null;
}

/** A markup attribute's single `{expression}`. */
function attrExpr(el: Node, name: string): Node | undefined {
  const attr = (el.attributes as Node[]).find((a) => a.type === 'Attribute' && a.name === name);
  if (!attr) return undefined;
  const v = attr.value as Node | Node[] | true;
  if (v === true) return undefined;
  const tag = Array.isArray(v) ? v.find((x) => x.type === 'ExpressionTag') : v;
  return tag?.type === 'ExpressionTag' ? (tag.expression as Node) : undefined;
}

function classOf(el: Node): string {
  const attr = (el.attributes as Node[]).find((a) => a.type === 'Attribute' && a.name === 'class');
  const v = attr?.value;
  if (!Array.isArray(v)) return '';
  return v.map((x: Node) => (x.type === 'Text' ? (x.data as string) : '')).join('');
}

function literalArg(call: Node): unknown {
  const a = (call.arguments as Node[])[0];
  return a?.type === 'Literal' ? a.value : undefined;
}

// ─── UpdateAllProjectsModal: reaching `done` always tells the host ──────

describe('UpdateAllProjectsModal — the done phase notifies the host', () => {
  const script = (MODAL.instance as Node).content;

  it('declares an onFinished prop', () => {
    const props = [...walk(script)].find(
      (n) =>
        n.type === 'VariableDeclarator' &&
        (n.init as Node | undefined)?.type === 'CallExpression' &&
        isIdent((n.init as Node).callee, '$props'),
    );
    expect(props, 'no `$props()` destructuring found').toBeTruthy();
    const keys = ((props!.id as Node).properties as Node[]).map(
      (p) => ((p.key as Node).name as string) ?? null,
    );
    expect(keys).toContain('onFinished');
  });

  it("every `phase = 'done'` is inside finishRun (so no path reaches done silently)", () => {
    const doneAssigns = [...walkWithAncestors(script)].filter(
      ([n]) =>
        n.type === 'AssignmentExpression' &&
        isIdent(n.left, 'phase') &&
        (n.right as Node).type === 'Literal' &&
        (n.right as Node).value === 'done',
    );
    expect(doneAssigns.length).toBeGreaterThan(0);
    for (const [, anc] of doneAssigns) {
      expect(enclosingFnName(anc)).toBe('finishRun');
    }
  });

  it('finishRun calls onFinished, and nothing else does (close / cancel never fire it)', () => {
    const finish = fnDecl(MODAL, 'finishRun');
    expect(finish, 'no finishRun function').toBeTruthy();
    expect(callsOf(finish!.body, 'onFinished').length).toBe(1);
    // The whole component — script AND markup — calls it exactly once, so
    // the Cancel / Close / backdrop paths cannot reach it.
    const everywhere = [...callsOf(script, 'onFinished'), ...callsOf(MODAL.fragment, 'onFinished')];
    expect(everywhere.length).toBe(1);
    // And finishRun is called only from runUpdateAll.
    const finishCalls = [...walkWithAncestors(MODAL)].filter(
      ([n]) => n.type === 'CallExpression' && isIdent(n.callee, 'finishRun'),
    );
    expect(finishCalls.length).toBe(2);
    for (const [, anc] of finishCalls) expect(enclosingFnName(anc)).toBe('runUpdateAll');
  });

  it('runUpdateAll finishes on BOTH the success and the failure path', () => {
    const run = fnDecl(MODAL, 'runUpdateAll');
    expect(run).toBeTruthy();
    // The try whose block awaits `projects.updateAll(...)`.
    const tryStmt = [...walk(run!.body)].find(
      (n) =>
        n.type === 'TryStatement' &&
        memberCallsOf(n.block, 'projects', 'updateAll').length > 0,
    );
    expect(tryStmt, 'no try around projects.updateAll').toBeTruthy();
    const ok = callsOf(tryStmt!.block, 'finishRun');
    const failed = callsOf(tryStmt!.handler, 'finishRun');
    expect(ok.map(literalArg)).toEqual(['completed']);
    expect(failed.map(literalArg)).toEqual(['errored']);
  });

  it('the confirm-phase Cancel button only closes', () => {
    const buttons = [...walk(MODAL.fragment)].filter(
      (n) => n.type === 'RegularElement' && n.name === 'button',
    );
    const closers = buttons.filter((b) => isIdent(attrExpr(b, 'onclick'), 'close'));
    expect(closers.length).toBeGreaterThanOrEqual(2); // Cancel + Close
    const close = fnDecl(MODAL, 'close');
    expect(callsOf(close!.body, 'finishRun').length).toBe(0);
    expect(callsOf(close!.body, 'onFinished').length).toBe(0);
  });
});

// ─── Projects page: every trigger reaches the census controller ─────────

describe('Projects page — census triggers are wired to the controller', () => {
  const script = (PAGE.instance as Node).content;

  it('builds the controller over the real census command', () => {
    const decl = [...walk(script)].find(
      (n) =>
        n.type === 'VariableDeclarator' &&
        isIdent(n.id, 'censusCtl') &&
        (n.init as Node | undefined)?.type === 'CallExpression' &&
        isIdent((n.init as Node).callee, 'createCensusController'),
    );
    expect(decl, 'censusCtl = createCensusController(...) not found').toBeTruthy();
    const invokes = callsOf(decl, 'invoke');
    expect(invokes.map(literalArg)).toEqual(['bundle_staleness_census']);
  });

  it("passes the controller's updateAllFinished to the modal as onFinished", () => {
    const modal = [...walk(PAGE.fragment)].find(
      (n) => n.type === 'Component' && n.name === 'UpdateAllProjectsModal',
    );
    expect(modal).toBeTruthy();
    expect(isMember(attrExpr(modal!, 'onFinished'), 'censusCtl', 'updateAllFinished')).toBe(true);
  });

  it('Refresh reloads the list AND re-takes the census', () => {
    const refresh = [...walk(PAGE.fragment)].find(
      (n) => n.type === 'RegularElement' && n.name === 'button' && classOf(n) === 'pl-refresh',
    );
    expect(refresh, 'no .pl-refresh button').toBeTruthy();
    const handler = attrExpr(refresh!, 'onclick');
    expect(isIdent(handler, 'refreshAll')).toBe(true);
    const fn = fnDecl(PAGE, 'refreshAll');
    expect(memberCallsOf(fn!.body, 'censusCtl', 'refresh').length).toBe(1);
    expect(memberCallsOf(fn!.body, 'projects', 'load').length).toBe(1);
  });

  it('mount takes the census', () => {
    const mount = callsOf(script, 'onMount');
    expect(mount.length).toBe(1);
    expect(memberCallsOf(mount[0], 'censusCtl', 'load').length).toBe(1);
  });

  it('feeds the updater, the projects list and the setup store to the controller from effects', () => {
    const effects = [...walk(script)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, '$effect'),
    );
    for (const trigger of ['updaterTick', 'projectsChanged', 'setupObserved']) {
      const fed = effects.some((e) => memberCallsOf(e, 'censusCtl', trigger).length === 1);
      expect(fed, `no $effect calls censusCtl.${trigger}`).toBe(true);
    }
  });

  it('Dismiss goes through the controller (so a later census cannot resurrect the call-out)', () => {
    const dismiss = [...walk(PAGE.fragment)].find(
      (n) =>
        n.type === 'RegularElement' &&
        n.name === 'button' &&
        classOf(n) === 'pl-census-dismiss',
    );
    expect(dismiss).toBeTruthy();
    expect(isMember(attrExpr(dismiss!, 'onclick'), 'censusCtl', 'dismissNotice')).toBe(true);
  });
});
