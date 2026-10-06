// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the Projects-page affordance rebuild (v0.2.101,
// PLAN-V02101-PULL-IN-FROM-V02102-2026-10-05 §7; owner ruling 2026-10-05).
//
// THE DEFECT CLASS IT PINS: an owner-visible affordance that silently
// disappeared in a rework (P299-C3/C4 — the project card stopped routing
// into the project's settings, and the per-project Update row action was
// gone). Both were rebuilt; this file locks them so a future refactor
// cannot drop either without a red test:
//
//   (a) card click → `/project/<id>/settings` (the project's settings
//       page — the same surface the standalone route renders);
//   (b) a per-row "Update" action that runs the EXISTING per-project
//       bundle-update engine — `projects.update()` in the store, which
//       invokes `update_project_v2` (projects_v2.rs → the one
//       `run_install_bundle_core` spawn behind a per-folder single-flight
//       turn). NEVER a second engine: the page must not grow its own
//       direct invoke of an update command, or the toast/summary/deferral
//       logic in the store gets bypassed and diverges.
//
// The established AST pattern (see packs.wiring.test.ts): a grep is
// satisfied by the comments that explain the wiring, so every assertion
// here walks the parsed Svelte AST instead.
//
// WHAT IT STILL DOES NOT PROVE: that a click reaches the handler in a
// real DOM, or that the Rust engine itself works (pinned by the Rust
// suites and update-invoke-census.test.ts).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { loadRust } from './test-support/source-census';
import { isIdent, registeredCommands, walk } from './test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

type Node = Record<string, unknown>;

const PAGE_SRC = readFileSync(
  resolve(here, '../routes/projects/+page.svelte'),
  'utf8',
);
const PAGE = parse(PAGE_SRC, { modern: true }) as unknown as Node;
const INSTANCE = (PAGE.instance as Node).content as Node;
const FRAGMENT = PAGE.fragment as Node;

const RUST = loadRust();
const LIB = RUST.find((f) => f.rel === 'src/lib.rs')!;

// ─── AST helpers ───────────────────────────────────────────────────────────

function findFunction(name: string): Node | null {
  for (const n of walk(INSTANCE)) {
    if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
  }
  return null;
}

/** First-argument template pieces of every `callee(\`…\`)` call in `root`.
 *  A TemplateLiteral's `quasis` are its static parts, so
 *  `` goto(`/project/${id}/settings`) `` → ['/project/', '/settings']. */
function templateArgsOfCalls(root: unknown, callee: string): string[][] {
  return [...walk(root)]
    .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, callee))
    .map((n) => (n.arguments as Node[])[0])
    .filter((a) => a?.type === 'TemplateLiteral')
    .map((a) => (a.quasis as Node[]).map((q) => (q.value as Node).raw as string));
}

/** Calls of the shape `obj.method(…)` anywhere under `root`. */
function memberCalls(root: unknown): string[] {
  return [...walk(root)]
    .filter((n) => n.type === 'CallExpression')
    .map((n) => n.callee as Node)
    .filter((c) => c?.type === 'MemberExpression')
    .map((c) => {
      const obj = (c.object as Node)?.name ?? '?';
      const prop = (c.property as Node)?.name ?? '?';
      return `${obj}.${prop}`;
    });
}

/** Every identifier name referenced under `root`. */
function identifiers(root: unknown): Set<string> {
  return new Set(
    [...walk(root)]
      .filter((n) => n.type === 'Identifier')
      .map((n) => n.name as string),
  );
}

function elements(tag: string): Node[] {
  return [...walk(FRAGMENT)].filter(
    (n) => n.type === 'RegularElement' && n.name === tag,
  );
}

function attr(el: Node, name: string): Node | undefined {
  return (el.attributes as Node[]).find((a) => a.name === name);
}

function attrText(el: Node, name: string): string | null {
  const a = attr(el, name);
  if (!a) return null;
  const v = a.value;
  if (Array.isArray(v)) {
    return v
      .filter((x: Node) => x.type === 'Text')
      .map((x: Node) => x.raw ?? x.data ?? '')
      .join('');
  }
  return null;
}

function buttonByTestId(testId: string): Node | undefined {
  return elements('button').find(
    (b) => attrText(b, 'data-testid') === testId,
  );
}

/** Literal first arguments of direct `invoke(...)` calls in the page script. */
function pageInvokeLiterals(): string[] {
  return [...walk(INSTANCE)]
    .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'))
    .map((n) => {
      const a = (n.arguments as Node[])[0];
      return a?.type === 'Literal' && typeof a.value === 'string'
        ? a.value
        : `NON-LITERAL@${JSON.stringify(a ?? {})}`;
    });
}

// ─── (a) the project card routes into the project's SETTINGS page ─────────

describe('affordance (a) — card click routes into /project/<id>/settings', () => {
  it("the page's open() navigates to the per-project settings route", () => {
    const open = findFunction('open');
    expect(open, 'the page has no open() handler at all').toBeTruthy();
    const targets = templateArgsOfCalls(open, 'goto');
    expect(
      targets,
      'open() must goto(`/project/${id}/settings`) — the settings page, ' +
        'not the project overview (P299-C3 regression)',
    ).toContainEqual(['/project/', '/settings']);
  });

  it('open() still selects the project so the store/selector follow', () => {
    const open = findFunction('open');
    expect(memberCalls(open)).toContain('projects.select');
  });

  it('the ProjectCard in the grid is wired to open() (the route actually reaches the card)', () => {
    const cards = [...walk(FRAGMENT)].filter(
      (n) => n.type === 'Component' && n.name === 'ProjectCard',
    );
    expect(cards.length, 'the page renders no ProjectCard at all').toBeGreaterThan(0);
    const wired = cards.some((c) => {
      const a = attr(c, 'onOpen');
      return !!a && identifiers(a).has('open');
    });
    expect(wired, 'ProjectCard is rendered without onOpen={open}').toBe(true);
  });
});

// ─── (b) the per-project Update row action, on the ONE engine ─────────────

describe('affordance (b) — per-row Update via the existing bundle-update engine', () => {
  it('the page has a row-update handler that calls projects.update (the store engine SettingsTab uses)', () => {
    const fn = findFunction('runRowUpdate');
    expect(
      fn,
      'no runRowUpdate() handler — the per-row Update action is gone (P299-C4 regression)',
    ).toBeTruthy();
    expect(
      memberCalls(fn),
      'runRowUpdate must run through projects.update() — the SAME engine as ' +
        "the Settings page's Update bundle button (store → update_project_v2)",
    ).toContain('projects.update');
  });

  it('the store engine behind it is the registered update_project_v2 command', () => {
    const storeSrc = readFileSync(resolve(here, 'stores/projects.ts'), 'utf8');
    expect(storeSrc).toContain("invoke<UpdateProjectResult>('update_project_v2'");
    expect(
      registeredCommands(LIB),
      'update_project_v2 is invoked by the store but not registered in generate_handler!',
    ).toContain('update_project_v2');
  });

  it('the page never grows a second engine: no direct invoke of an update command', () => {
    const literals = new Set(pageInvokeLiterals());
    for (const forbidden of [
      'update_project_v2',
      'update_all_projects',
      'run_orchestrator_update',
      'run_install_bundle',
    ]) {
      expect(
        literals.has(forbidden),
        `the page must not invoke ${forbidden} directly — go through the projects store`,
      ).toBe(false);
    }
  });

  it('a row Update button exists in the grid markup, wired to runRowUpdate', () => {
    const btn = buttonByTestId('project-row-update');
    expect(btn, 'no button[data-testid=project-row-update] in the page markup').toBeTruthy();
    const onclick = attr(btn!, 'onclick');
    expect(onclick, 'the row Update button has no onclick').toBeTruthy();
    expect(
      identifiers(onclick),
      'the row Update button onclick must call runRowUpdate',
    ).toContain('runRowUpdate');
  });

  it('the row Update button shows a busy/disabled state while the engine runs', () => {
    const btn = buttonByTestId('project-row-update');
    expect(btn).toBeTruthy();
    expect(
      attr(btn!, 'disabled'),
      'the row Update button must be disabled while an update runs',
    ).toBeTruthy();
    expect(
      identifiers(attr(btn!, 'disabled')),
      'the disabled gate must reference the updatingId busy state',
    ).toContain('updatingId');
    // The running engine must be SHOWN, not just gated: the button's own
    // content branches on updatingId (busy label).
    expect(
      identifiers(btn!.children ?? btn!.fragment),
      'the button label must branch on updatingId (busy indicator)',
    ).toContain('updatingId');
  });

  it('the updatingId busy state exists in the page script', () => {
    const declared = [...walk(INSTANCE)].some(
      (n) => n.type === 'VariableDeclarator' && isIdent(n.id, 'updatingId'),
    );
    expect(declared, 'no updatingId state variable').toBe(true);
  });

  it('"Update all" is gated on the row-update busy state too (consistent with the existing flows)', () => {
    const all = elements('button').find(
      (b) => {
        const cls = attrText(b, 'class');
        return cls !== null && cls.includes('pl-update-all');
      },
    );
    expect(all, 'no .pl-update-all button').toBeTruthy();
    expect(
      identifiers(attr(all!, 'disabled')),
      'Update all must be disabled while a row update runs',
    ).toContain('updatingId');
  });

  it('a per-row Settings button exists beside it and routes through open()', () => {
    const btn = buttonByTestId('project-row-settings');
    expect(btn, 'no button[data-testid=project-row-settings]').toBeTruthy();
    expect(
      identifiers(attr(btn!, 'onclick')),
      'the row Settings button must call open (the settings route)',
    ).toContain('open');
  });

  it('a finished row update re-takes the bundle census (the page comment promises every bundle-changing action does)', () => {
    const fn = findFunction('runRowUpdate');
    expect(fn).toBeTruthy();
    expect(
      memberCalls(fn),
      'runRowUpdate must call censusCtl.refresh() when done — a row Update ' +
        'changes bundle state exactly like Update all does',
    ).toContain('censusCtl.refresh');
  });
});
