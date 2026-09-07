// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the Secret-refs "Set?" column.
//
// `secret-ref-status.test.ts` proves the derivation is CORRECT. It says
// nothing about whether `SecretsTab.svelte` calls it — and this repo has
// already shipped a fix that was correct, fully unit-tested, and had zero
// callers (knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md).
// The launcher's vitest runs headless with no svelte component runner, so
// rendering the component and asserting on output is not available here.
//
// WHAT THIS DOES INSTEAD: parses the real component with the real Svelte
// compiler and asserts on the AST.
//
// WHY THE AST AND NOT A TEXT SEARCH: the natural guard — grep the source
// for "badgeForRef" — reproduces the very defect it guards against, because
// a COMMENT containing the name satisfies it. That is instance #8 in the
// node above, and it is not hypothetical here: Svelte's compiler carries
// markup comments through into the emitted JS, so even a compiled-output
// substring check would be comment-satisfiable. A `CallExpression` node
// cannot be a comment.
//
// WHAT IT STILL DOES NOT PROVE: that the call's result reaches the user's
// eye. That last hop needs a component runner; if one is ever added to
// `vitest.config.ts`, replace this file with a render assertion rather than
// adding to it.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(resolve(here, 'SecretsTab.svelte'), 'utf8');
const AST = parse(SOURCE, { modern: true }) as unknown as Record<string, unknown>;

/** Every node in the AST, depth-first. */
function* walk(node: unknown): Generator<Record<string, unknown>> {
  if (node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const obj = node as Record<string, unknown>;
  if (typeof obj.type === 'string') yield obj;
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'parent' || k === 'loc') continue;
    yield* walk(v);
  }
}

const NODES = [...walk(AST)];

/** The AST subtree of the named function declaration. */
function bodyOf(fnName: string): Record<string, unknown>[] {
  const fn = NODES.find(
    (n) =>
      n.type === 'FunctionDeclaration' &&
      (n.id as Record<string, unknown> | undefined)?.name === fnName,
  );
  expect(fn, `no function declaration named ${fnName}`).toBeTruthy();
  return [...walk(fn)];
}

function callsTo(name: string): Record<string, unknown>[] {
  return NODES.filter((n) => {
    if (n.type !== 'CallExpression') return false;
    const callee = n.callee as Record<string, unknown> | undefined;
    return callee?.type === 'Identifier' && callee.name === name;
  });
}

function identifierReads(name: string): Record<string, unknown>[] {
  return NODES.filter((n) => n.type === 'Identifier' && n.name === name);
}

function memberReadsOf(prop: string): Record<string, unknown>[] {
  return NODES.filter((n) => {
    if (n.type !== 'MemberExpression') return false;
    const p = n.property as Record<string, unknown> | undefined;
    return p?.type === 'Identifier' && p.name === prop;
  });
}

describe('SecretsTab is wired to the live status derivation', () => {
  it('parses (guards against the assertions below silently matching nothing)', () => {
    // If the parse ever produced an empty/foreign shape, every `expect
    // .length === 0` below would pass vacuously. Pin a floor.
    expect(NODES.length).toBeGreaterThan(100);
  });

  it('CALLS badgeForRef — not merely mentions it', () => {
    expect(callsTo('badgeForRef').length).toBeGreaterThan(0);
  });

  it('CALLS entryOf, so the rows it probes are the rows it renders', () => {
    // Registering/refreshing a different (project, scope, module, key)
    // tuple than the badge reads would leave every badge at 'unknown'
    // forever while the probes ran perfectly.
    expect(callsTo('entryOf').length).toBeGreaterThan(0);
  });

  // ── The derivation's RESULT must reach the markup ──────────────────
  //
  // These exist because the first version of this file was WEAK, and the
  // mutation that proved it is worth naming: replacing the status cell
  // with a hardcoded `<span>set</span>` while LEAVING the
  // `{@const badge = badgeForRef(...)}` line in place kept every
  // assertion green. "The helper is called" and "the helper decides what
  // the user sees" are different claims, and only the first was pinned —
  // the same one-level-up blind spot that makes a source-scan guard
  // useless. A discarded call is indistinguishable from no call at all.

  it('CONSUMES the badge it computes — a discarded result is a dead call', () => {
    // `badge` must appear beyond the ConstTag that declares it. One
    // occurrence = declared and thrown away.
    expect(identifierReads('badge').length).toBeGreaterThan(1);
  });

  it('renders the badge through the shared copy tables, not literals', () => {
    // A hardcoded cell would reference neither. These are also what stop
    // the tab from inventing its own wording for a state the sibling
    // panel already names.
    expect(identifierReads('BADGE_LABEL').length).toBeGreaterThan(1);
    expect(identifierReads('BADGE_TITLE').length).toBeGreaterThan(1);
  });

  it('PROBES from load(), where the ref list actually arrives', () => {
    // Second weak-test lesson, also worth naming: deleting the
    // `await refreshStatuses(refs)` line from `load()` left every earlier
    // assertion green, because `entryOf` was still called inside the
    // now-unreachable `refreshStatuses` body. Every badge would have sat
    // at 'unknown' forever and the suite would not have noticed.
    //
    // So scope the assertion to `load`'s own subtree — a call from a dead
    // sibling function cannot satisfy it.
    const calls = bodyOf('load').filter((n) => {
      if (n.type !== 'CallExpression') return false;
      const callee = n.callee as Record<string, unknown> | undefined;
      return callee?.type === 'Identifier' && callee.name === 'refreshStatuses';
    });
    expect(calls.length).toBeGreaterThan(0);
  });

  it('RE-PROBES after the shared-secrets toggle, whose marker decides the badge', () => {
    // The toggle beside this column writes/removes
    // `~/.vct-secrets/projects/<NAME>/.no-shared-fallback`, and that marker
    // is what makes a key satisfied only by `~/.vct-secrets/shared/` count
    // as resolving here. A toggle that does not re-probe leaves every such
    // badge asserting the pre-toggle world until the tab is reloaded — a
    // rendered status that is, again, not a measured one.
    //
    // Scoped to `toggleSharedSecrets`'s own subtree: the identical call in
    // `load()` must not be able to satisfy it.
    const calls = bodyOf('toggleSharedSecrets').filter((n) => {
      if (n.type !== 'CallExpression') return false;
      const callee = n.callee as Record<string, unknown> | undefined;
      return callee?.type === 'Identifier' && callee.name === 'refreshStatuses';
    });
    expect(calls.length).toBeGreaterThan(0);
  });

  it('DECLARES the reader before probing, inside refreshStatuses itself', () => {
    // A `keychain-shared` ref is owned by the `_user_shared_` sentinel,
    // which names no project — so without an explicit reader the backend
    // answers about the all-readers `*` row, and neither this project's
    // pause of a shared key nor the "Disable shared secrets" checkbox
    // rendered ten lines above this table could ever reach the column.
    //
    // Scoped to `refreshStatuses`'s own subtree so the call cannot be
    // satisfied from a sibling that the probe loop does not go through,
    // and asserting on the ARGUMENT too: `setRequesterProject(null)` would
    // type-check, run, and restore the exact defect.
    const calls = bodyOf('refreshStatuses').filter((n) => {
      if (n.type !== 'CallExpression') return false;
      const callee = n.callee as Record<string, unknown> | undefined;
      if (callee?.type !== 'MemberExpression') return false;
      const prop = callee.property as Record<string, unknown> | undefined;
      return prop?.type === 'Identifier' && prop.name === 'setRequesterProject';
    });
    expect(
      calls.length,
      'refreshStatuses never declares whose view it is probing',
    ).toBeGreaterThan(0);
    const args = calls[0].arguments as Record<string, unknown>[];
    expect(args).toHaveLength(1);
    expect(args[0].type).toBe('Identifier');
    expect(args[0].name).toBe('projectId');
  });

  it('no longer reads the stale stored `is_set` column anywhere', () => {
    // This is the defect itself: `project_secret_refs.is_set` is written
    // once by the ref's writer and never revised. Any read of it in this
    // component is a rendered status that is not a measured one.
    expect(memberReadsOf('is_set')).toHaveLength(0);
  });
});
