// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the two v0.2.101 Q4 GUI affordances (owner ruling
// 2026-10-05, PLAN-V02101-PULL-IN-FROM-V02102-2026-10-05 §7):
//
//   (a) ModuleCatalog's per-module "Re-apply DB migrations" repair button —
//       rendered ONLY for modules whose last DB-migration apply reported
//       errors (the durable app_state record), and running through the ONE
//       TS engine (`$lib/module-db-repair`), never a direct invoke of the
//       Tauri command (the toast/summary logic lives in the engine).
//   (b) Preferences' "Re-render env for all projects" action — same shape:
//       the ONE engine (`$lib/project-state/all-projects-env`), confirm
//       BEFORE the destructive run, and a REAL project count in the dialog
//       (the projects store refreshed at click time), not a hardcoded 0.
//
// Final-batch review nit 7: the Q2 rebuild got an AST wiring guard but
// these two new surfaces did not — this file closes that gap with the same
// established pattern (see projects-page-affordances.wiring.test.ts and
// packs.wiring.test.ts): a grep is satisfied by the comments that explain
// the wiring, so every assertion here walks the parsed Svelte AST instead.
//
// WHAT IT STILL DOES NOT PROVE: that a click reaches the handler in a real
// DOM, or that the Rust commands themselves work (pinned by the Rust
// suites, module-db-repair.test.ts and all-projects-env.test.ts, which own
// the engine logic + confirm-gating).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from './test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

type Node = Record<string, unknown>;

function parseSvelte(rel: string): { instance: Node; fragment: Node } {
  const src = readFileSync(resolve(here, rel), 'utf8');
  const page = parse(src, { modern: true }) as unknown as Node;
  return {
    instance: (page.instance as Node).content as Node,
    fragment: page.fragment as Node,
  };
}

const CATALOG = parseSvelte('./components/ModuleCatalog.svelte');
const PREFS = parseSvelte('../routes/preferences/+page.svelte');

// ─── AST helpers (same idioms as projects-page-affordances.wiring.test.ts) ──

function findFunction(root: Node, name: string): Node | null {
  for (const n of walk(root)) {
    if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
  }
  return null;
}

function identifiers(root: unknown): Set<string> {
  return new Set(
    [...walk(root)]
      .filter((n) => n.type === 'Identifier')
      .map((n) => n.name as string),
  );
}

/** Names imported from `sourceModule` (`import { a, b } from '...'`). */
function importedNames(instance: Node, sourceModule: string): Set<string> {
  const names = new Set<string>();
  for (const n of walk(instance)) {
    if (n.type !== 'ImportDeclaration') continue;
    const src = n.source as Node | undefined;
    if (src?.value !== sourceModule) continue;
    for (const spec of (n.specifiers as Node[]) ?? []) {
      if (spec.type === 'ImportSpecifier' && spec.imported) {
        names.add((spec.imported as Node).name as string);
      }
    }
  }
  return names;
}

/** `name(...)` calls anywhere under `root` (identifier callee). */
function callsNamed(root: unknown, name: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, name),
  );
}

/** Literal first arguments of direct `invoke(...)` calls in a script. */
function invokeLiterals(instance: Node): string[] {
  return [...walk(instance)]
    .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'))
    .map((n) => {
      const a = (n.arguments as Node[])[0];
      return a?.type === 'Literal' && typeof a.value === 'string'
        ? a.value
        : `NON-LITERAL@${JSON.stringify(a ?? {})}`;
    });
}

function elements(fragment: Node, tag: string): Node[] {
  return [...walk(fragment)].filter(
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

// ─── (a) ModuleCatalog — the DB-migration repair affordance ────────────────

describe('Q4 (a) — ModuleCatalog DB-migration repair button', () => {
  it('imports BOTH engine helpers from the one home ($lib/module-db-repair)', () => {
    const names = importedNames(CATALOG.instance, '$lib/module-db-repair');
    expect(names).toContain('loadModuleDbMigrationFailures');
    expect(names).toContain('reapplyModuleDbMigrationsAction');
  });

  it('renders the repair button ONLY inside the failure-record gate', () => {
    // The {#if dbMigrationFailures.has(m.id)} block must wrap a button with
    // the testid — a button rendered unconditionally (or in a different
    // gate) fails this even if the testid survives.
    const ifBlocks = [...walk(CATALOG.fragment)].filter(
      (n) => n.type === 'IfBlock',
    );
    const gated = ifBlocks.some((blk) => {
      const testRefs = identifiers(blk.test);
      // `dbMigrationFailures.has(m.id)` — a MEMBER call (callee is a
      // MemberExpression, property `has`), not a bare identifier call.
      const has = [...walk(blk.test)].some(
        (n) =>
          n.type === 'CallExpression' &&
          (n.callee as Node)?.type === 'MemberExpression' &&
          ((n.callee as Node).property as Node)?.name === 'has',
      );
      if (!testRefs.has('dbMigrationFailures') || !has) return false;
      return [...walk(blk)].some(
        (n) =>
          n.type === 'RegularElement' &&
          n.name === 'button' &&
          attrText(n, 'data-testid') === 'reapply-db-migrations',
      );
    });
    expect(
      gated,
      'the reapply-db-migrations button must render inside ' +
        '{#if dbMigrationFailures.has(m.id)} — the durable failure record ' +
        'is the gate, not the network',
    ).toBe(true);
  });

  it('the button is wired to handleReapplyDbMigrations and disables while running', () => {
    const btn = elements(CATALOG.fragment, 'button').find(
      (b) => attrText(b, 'data-testid') === 'reapply-db-migrations',
    );
    expect(btn, 'no button with data-testid="reapply-db-migrations"').toBeTruthy();
    const onclick = attr(btn!, 'onclick');
    expect(
      onclick && identifiers(onclick).has('handleReapplyDbMigrations'),
      'the repair button must call handleReapplyDbMigrations on click',
    ).toBe(true);
    const disabled = attr(btn!, 'disabled');
    expect(
      disabled && identifiers(disabled).has('reapplyingMigrationsId'),
      'the repair button must disable on reapplyingMigrationsId (no double click)',
    ).toBe(true);
  });

  it('handleReapplyDbMigrations goes through the TS engine and re-reads on success', () => {
    const fn = findFunction(CATALOG.instance, 'handleReapplyDbMigrations');
    expect(fn, 'no handleReapplyDbMigrations() handler at all').toBeTruthy();
    expect(
      callsNamed(fn, 'reapplyModuleDbMigrationsAction').length,
      'the handler must call reapplyModuleDbMigrationsAction (the one engine)',
    ).toBeGreaterThan(0);
    const reload = [...walk(fn)].some((n) =>
      n.type === 'CallExpression' && isIdent(n.callee, 'loadDbMigrationFailures'),
    );
    expect(
      reload,
      'a clean apply must re-read the failure map so the button disappears',
    ).toBe(true);
  });

  it('the failure map loads through the engine with { invoke } injected (never a direct command call)', () => {
    const calls = callsNamed(
      CATALOG.instance,
      'loadModuleDbMigrationFailures',
    );
    expect(calls.length, 'loadModuleDbMigrationFailures is never called').toBeGreaterThan(0);
    const injected = calls.some((c) => {
      const arg = (c.arguments as Node[])[0];
      return !!arg && identifiers(arg).has('invoke');
    });
    expect(injected, 'the load must inject { invoke } (dep-injected engine)').toBe(true);
    const literals = invokeLiterals(CATALOG.instance);
    expect(
      literals,
      'the component must not bypass the engine with a direct invoke of the migration commands',
    ).not.toContain('apply_module_db_migrations');
    expect(literals).not.toContain('list_module_db_migration_failures');
  });
});

// ─── (b) Preferences — the all-projects env re-render affordance ───────────

describe('Q4 (b) — Preferences re-render env action', () => {
  it('reRenderAllProjectsEnv goes through the TS engine (never a direct command call)', () => {
    const fn = findFunction(PREFS.instance, 'reRenderAllProjectsEnv');
    expect(fn, 'no reRenderAllProjectsEnv() handler at all').toBeTruthy();
    expect(
      callsNamed(fn, 'refreshAllProjectsEnvAction').length,
      'the handler must call refreshAllProjectsEnvAction (the one engine — ' +
        'it owns the confirm gate and the summary copy)',
    ).toBeGreaterThan(0);
    expect(
      invokeLiterals(PREFS.instance),
      'the page must not bypass the engine with a direct invoke of refresh_all_projects_env',
    ).not.toContain('refresh_all_projects_env');
  });

  it('passes a REAL project count — the store refreshed at click time, not a literal 0', () => {
    const fn = findFunction(PREFS.instance, 'reRenderAllProjectsEnv');
    expect(fn).toBeTruthy();
    const engineCalls = callsNamed(fn, 'refreshAllProjectsEnvAction');
    const countArg = (engineCalls[0].arguments as Node[])[1];
    expect(countArg, 'refreshAllProjectsEnvAction is called without a count').toBeTruthy();
    const isLiteralZero =
      countArg.type === 'Literal' && (countArg.value as unknown) === 0;
    expect(
      isLiteralZero,
      'the confirm count is a hardcoded 0 — the dialog always says ' +
        '"every project" (final-batch review nit 6)',
    ).toBe(false);
    const refs = identifiers(countArg);
    expect(
      refs.has('projects'),
      'the count must come from the projects store',
    ).toBe(true);
    // Freshness: the store does not self-load; the handler must refresh it
    // before reading the count (one list invoke, ms-scale).
    const refreshed = [...walk(fn)].some(
      (n) =>
        n.type === 'CallExpression' &&
        n.callee &&
        (n.callee as Node).type === 'MemberExpression' &&
        identifiers(n.callee).has('load') &&
        identifiers(n.callee).has('projects'),
    );
    expect(
      refreshed,
      'the handler must call projects.load() before reading the count — ' +
        'Preferences never triggers the Projects page load, so a bare read ' +
        'is a stale 0',
    ).toBe(true);
  });
});
