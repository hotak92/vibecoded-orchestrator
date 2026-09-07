// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the SecretsPanel's presence badge and its Remove
// confirmation.
//
// WHY IT IS NEEDED AT ALL: `badgeOf` gained a `'shared-file-store'` state
// for a key that resolves only from `~/.vct-secrets/shared/`. The panel
// renders the badge with an if/else-if chain whose FINAL `{:else}` prints
// "not set". So a new state the derivation can return, without a matching
// branch here, falls through to that `{:else}` and prints exactly the lie
// the state was added to remove — a mechanism credited and never firing
// (`knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md`).
// The correct derivation would have been fully unit-tested the whole time.
//
// WHY THE AST AND NOT A TEXT SEARCH: grepping the source for
// "shared-file-store" is satisfied by the explanatory COMMENT sitting two
// lines above the branch — and Svelte carries markup comments through into
// the compiled JS, so even a compiled-output substring check is
// comment-satisfiable. Only a syntax node can distinguish a branch from
// prose about a branch.
//
// WHAT IT STILL DOES NOT PROVE: that the rendered span reaches the user's
// eye. That needs a component runner, which `vitest.config.ts` does not
// stand up. If one is ever added, replace this file with a render
// assertion rather than adding to it.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(resolve(here, 'SecretsPanel.svelte'), 'utf8');
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

/** String literals appearing as real syntax (never inside a comment). */
function stringLiterals(value: string): Record<string, unknown>[] {
  return NODES.filter((n) => n.type === 'Literal' && n.value === value);
}

/** `x.<prop>` member reads. */
function memberReadsOf(prop: string): Record<string, unknown>[] {
  return NODES.filter((n) => {
    if (n.type !== 'MemberExpression') return false;
    const p = n.property as Record<string, unknown> | undefined;
    return p?.type === 'Identifier' && p.name === prop;
  });
}

/** Every `{#if}` / `{:else if}` block node. */
function ifBlocks(): Record<string, unknown>[] {
  return NODES.filter((n) => n.type === 'IfBlock');
}

function subtreeHasLiteral(node: unknown, value: string): boolean {
  return [...walk(node)].some((n) => n.type === 'Literal' && n.value === value);
}

function subtreeHasMemberRead(node: unknown, prop: string): boolean {
  return [...walk(node)].some((n) => {
    if (n.type !== 'MemberExpression') return false;
    const p = n.property as Record<string, unknown> | undefined;
    return p?.type === 'Identifier' && p.name === prop;
  });
}

/** Rendered TEXT inside a subtree, lowercased and whitespace-collapsed.
 *
 * `Text` nodes are markup, not comments: Svelte parses `<!-- … -->` into
 * `Comment` nodes, which never appear here. So a prose mention of a state
 * cannot satisfy an assertion about what the branch DISPLAYS. */
function renderedText(node: unknown): string {
  return [...walk(node)]
    .filter((n) => n.type === 'Text' && typeof n.data === 'string')
    .map((n) => n.data as string)
    .join(' ')
    .replace(/\s+/g, ' ')
    .toLowerCase();
}

function testSubtreeContainsLiteral(value: string): boolean {
  return ifBlocks().some((n) => n.test && subtreeHasLiteral(n.test, value));
}

describe('SecretsPanel renders the states its derivation can produce', () => {
  it('parses (guards against every assertion below matching vacuously)', () => {
    expect(NODES.length).toBeGreaterThan(500);
    // Sanity that literal extraction works at all on this file: the
    // pre-existing badge states must be findable the same way.
    expect(stringLiterals('file-store').length).toBeGreaterThan(0);
  });

  it('BRANCHES on the shared-file-store badge AND shows it — the else-arm says "not set"', () => {
    // Two claims, because only pinning the first is the one-level-up blind
    // spot: a branch that exists but renders a hardcoded "not set" is
    // indistinguishable from no branch at all. So find the branch by its
    // TEST, then assert on what its CONSEQUENT displays.
    const arm = ifBlocks().find(
      (n) => n.test && subtreeHasLiteral(n.test, 'shared-file-store'),
    );
    expect(
      arm,
      "no {#if}/{:else if} tests for 'shared-file-store'; a key that resolves " +
        'only from ~/.vct-secrets/shared/ would fall through to the final ' +
        '{:else} and render "not set"',
    ).toBeTruthy();
    const shown = renderedText(arm!.consequent);
    expect(shown, `the branch renders: ${shown}`).toContain('shared file store');
    expect(shown).not.toContain('not set');
    // The tooltip must name the actual file, or the user cannot act on the
    // badge — and that path is the only thing distinguishing this state
    // from the project's own copy.
    expect(subtreeHasMemberRead(arm!.consequent, 'shared_file_store_path')).toBe(true);
  });

  it('branches on every OTHER badge state too, so the else-arm stays "nowhere"', () => {
    // The final {:else} is the 'not-set' arm. Every state that is NOT
    // 'not-set' therefore needs a branch of its own; a missing one is a
    // silent downgrade to the lie.
    for (const badge of [
      'set',
      'unset',
      'file-store',
      'shared-file-store',
      'shared-opted-out',
      'unknown',
    ]) {
      expect(testSubtreeContainsLiteral(badge), `no branch for badge '${badge}'`).toBe(
        true,
      );
    }
  });

  it('the branch list above is EXHAUSTIVE over what badgeOf can return', () => {
    // The loop is a hand-written list, so it can go stale the moment a
    // seventh state is added — and a state absent from BOTH the loop and
    // the component falls through to {:else} and prints "not set" with
    // nothing turning red. Derive the truth from the derivation's own
    // source instead: every member of the `SecretBadge` union must have a
    // branch here, except 'not-set', which IS the else-arm.
    const unionSrc = readFileSync(
      resolve(here, '../stores/secrets.ts'),
      'utf8',
    );
    const decl = unionSrc.slice(
      unionSrc.indexOf('export type SecretBadge'),
      unionSrc.indexOf(';', unionSrc.indexOf('export type SecretBadge')),
    );
    const members = [...decl.matchAll(/'([a-z-]+)'/g)].map((m) => m[1]);
    expect(members.length).toBeGreaterThan(5);
    expect(members).toContain('not-set');
    for (const badge of members) {
      if (badge === 'not-set') continue;
      expect(
        testSubtreeContainsLiteral(badge),
        `badgeOf can return '${badge}' and this component has no branch for ` +
          'it, so it renders the final {:else} — "not set"',
      ).toBe(true);
    }
  });

  it('the shared-opt-out branch SHOWS its own state, and names the toggle', () => {
    // Same two claims as the shared-file-store arm: locate by TEST, then
    // assert on the CONSEQUENT. A branch that exists but prints "not set"
    // is indistinguishable from no branch.
    const arm = ifBlocks().find(
      (n) => n.test && subtreeHasLiteral(n.test, 'shared-opted-out'),
    );
    expect(arm).toBeTruthy();
    const shown = renderedText(arm!.consequent);
    expect(shown, `the branch renders: ${shown}`).toMatch(/not read here/);
    expect(shown).not.toContain('not set');
    // The remedy is a checkbox, not re-entering the value — the tooltip
    // has to say which, or the badge is unactionable.
    // The tooltip is a template literal, so its prose lives in
    // `TemplateElement` nodes, not `Literal` ones. Both are syntax; a
    // `Comment` is neither, so this stays comment-proof.
    const tip = [...walk(arm!.consequent)]
      .map((n) => {
        if (n.type === 'Literal' && typeof n.value === 'string') return n.value;
        if (n.type === 'TemplateElement') {
          const v = n.value as Record<string, unknown> | undefined;
          return typeof v?.cooked === 'string' ? v.cooked : '';
        }
        return '';
      })
      .join(' ')
      .toLowerCase();
    expect(tip).toContain('disable shared secrets');
  });

  it('the Remove confirmation WARNS about a surviving shared copy', () => {
    // Remove deletes a keychain entry and a launcher row. It cannot delete
    // either tier-2 file, so a per-project key satisfied by `shared/<key>`
    // keeps resolving afterwards — a dialog that mentions only
    // `file_store` lets the user believe the key is gone.
    //
    // FIRST VERSION OF THIS TEST WAS WEAK, and the mutation that proved it
    // is worth naming: replacing the survivor branch's test with `{:else if
    // false}` left it GREEN, because a LATER branch (`… === 'unknown'`)
    // still read `shared_file_store` somewhere in the file. "The property
    // is mentioned" and "a branch fires on it" are different claims. So
    // locate the arm by test AND consequent, exactly as above.
    const arm = ifBlocks().find(
      (n) =>
        n.test &&
        subtreeHasMemberRead(n.test, 'shared_file_store') &&
        subtreeHasLiteral(n.test, 'present'),
    );
    expect(
      arm,
      'no branch tests `removeConfirm.shared_file_store === "present"`; the ' +
        'Remove dialog would stay silent about the copy that keeps resolving',
    ).toBeTruthy();
    const shown = renderedText(arm!.consequent);
    expect(shown).toContain('shared');
    expect(shown).toMatch(/resolv|surviv/);
    expect(subtreeHasMemberRead(arm!.consequent, 'shared_file_store_path')).toBe(true);

    // The pre-existing own-namespace warning must not have been dropped
    // while adding the new one.
    const own = ifBlocks().find(
      (n) =>
        n.test &&
        subtreeHasMemberRead(n.test, 'file_store') &&
        !subtreeHasMemberRead(n.test, 'shared_file_store') &&
        subtreeHasLiteral(n.test, 'present'),
    );
    expect(own, 'the own-namespace survivor warning is gone').toBeTruthy();
    expect(subtreeHasMemberRead(own!.consequent, 'file_store_path')).toBe(true);
  });

  it('warns on Set that a SHARED file copy is about to be shadowed', () => {
    // Saving here writes the keychain and leaves the file alone, so the
    // click forks the value — and when the file is the SHARED one, every
    // other project keeps reading the copy this project just stopped
    // using. The pre-existing warning covered only `file-store`; a
    // `shared-file-store` row would have got the bland "Set a value".
    //
    // The title is a ternary, not an {#if}, so look for a
    // ConditionalExpression whose TEST names the badge and whose
    // consequent reads the shared path.
    const arm = NODES.find((n) => {
      if (n.type !== 'ConditionalExpression') return false;
      return (
        subtreeHasLiteral(n.test, 'shared-file-store') &&
        subtreeHasMemberRead(n.consequent, 'shared_file_store_path')
      );
    });
    expect(
      arm,
      'the Set button does not special-case a shared-file-store row; the ' +
        'user gets no warning that saving forks a value other projects read',
    ).toBeTruthy();
  });

  it('derives the badge through badgeOf rather than re-deriving presence', () => {
    // A second derivation here would drift off the invariants badgeOf
    // encodes (unreadable ≠ absent; a readable copy outranks an unreadable
    // store) — the whole reason the two surfaces share one model.
    const calls = NODES.filter((n) => {
      if (n.type !== 'CallExpression') return false;
      const callee = n.callee as Record<string, unknown> | undefined;
      return callee?.type === 'Identifier' && callee.name === 'badgeOf';
    });
    expect(calls.length).toBeGreaterThan(0);
  });
});
