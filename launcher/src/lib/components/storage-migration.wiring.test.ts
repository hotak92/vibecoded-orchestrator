// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the Storage page's volume-data migration panel
// (v0.2.102, W5-B). `migrate_to_named_volume` / `migrate_to_bind_path`
// were registered commands with NO caller (owner-deferred to v0.2.102).
// Both are destructive-ADJACENT (a full copy that overwrites same-named
// destination files), so the arms are pinned: the invoke is reachable
// ONLY through the confirm modal's Continue, the modal opens ONLY via the
// Migrate button (never auto-run), and Cancel never reaches the invoke.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const CARD = parse(
  readFileSync(resolve(here, 'StorageSettingsCard.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;
// The route that hosts the card — the reachable-component leg.
const ROUTE = readFileSync(
  resolve(here, '../../routes/preferences/storage/+page.svelte'),
  'utf8',
);

function fnBody(name: string): Node {
  const fn = [...walk((CARD.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
  expect(fn, `no function declaration named ${name}`).toBeTruthy();
  return fn!;
}

/** Identifier-referenced invokes of a literal command inside a subtree. */
function invokesOf(root: unknown, cmd: string): Node[] {
  return [...walk(root)].filter(
    (n) =>
      n.type === 'CallExpression' &&
      isIdent(n.callee, 'invoke') &&
      (n.arguments as Node[])[0]?.type === 'Literal' &&
      ((n.arguments as Node[])[0] as Node).value === cmd,
  );
}

describe('StorageSettingsCard — volume-data migration (W5-B)', () => {
  it('parses', () => {
    expect([...walk(CARD)].length).toBeGreaterThan(100);
  });

  it('runMigration invokes BOTH commands (one per direction) with the shared arg builder', () => {
    const fn = fnBody('runMigration');
    expect(invokesOf(fn, 'migrate_to_bind_path').length).toBe(1);
    expect(invokesOf(fn, 'migrate_to_named_volume').length).toBe(1);
    // The payload comes from migrationArgs — never an ad-hoc object (the
    // validation the button enforces would be bypassed).
    expect(
      [...walk(fn)].some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'migrationArgs')),
      'runMigration does not use the shared migrationArgs builder',
    ).toBe(true);
    // The act arm REFUSES to run on invalid input even if reached directly
    // (`if (args === null) … return`).
    const hasNullArgsGuard = [...walk(fn)].some((n) => {
      if (n.type !== 'IfStatement') return false;
      const t = (n as Node).test as Node | undefined;
      const right = t?.right as Node | undefined;
      return (
        t?.type === 'BinaryExpression' &&
        t.operator === '===' &&
        isIdent(t.left, 'args') &&
        right?.type === 'Literal' &&
        right?.value === null
      );
    });
    expect(hasNullArgsGuard, 'runMigration has no null-args guard').toBe(true);
  });

  it('the invoke is gated behind the modal: requestMigration only OPENS, never runs', () => {
    const fn = fnBody('requestMigration');
    // Opens the modal…
    expect(
      [...walk(fn)].some(
        (n) => n.type === 'AssignmentExpression' && isIdent(n.left, 'migModalOpen'),
      ),
    ).toBe(true);
    // …and contains NO invoke of either migration command (never auto-run).
    expect(invokesOf(fn, 'migrate_to_bind_path').length).toBe(0);
    expect(invokesOf(fn, 'migrate_to_named_volume').length).toBe(0);
  });

  it('the CANCEL arm closes the modal and never invokes', () => {
    const fn = fnBody('cancelMigration');
    expect(
      [...walk(fn)].some(
        (n) => n.type === 'AssignmentExpression' && isIdent(n.left, 'migModalOpen'),
      ),
      'cancelMigration does not close the modal',
    ).toBe(true);
    expect(invokesOf(fn, 'migrate_to_bind_path').length).toBe(0);
    expect(invokesOf(fn, 'migrate_to_named_volume').length).toBe(0);
    expect(
      [...walk(fn)].some((n) => isIdent(n, 'runMigration')),
      'the cancel arm reaches runMigration',
    ).toBe(false);
  });

  it('the ACT arm (modal Continue) calls runMigration; the modal copy comes from modalCopy', () => {
    const modal = [...walk(CARD.fragment)].filter(
      (n) => n.type === 'Component' && (n as Node).name === 'DialogRoot',
    );
    expect(modal.length, 'no confirm modal rendered').toBe(1);
    expect(
      [...walk(modal[0])].some(
        (n) => n.type === 'CallExpression' && isIdent(n.callee, 'runMigration'),
      ),
      "the modal's Continue does not run the migration",
    ).toBe(true);
    expect(
      [...walk(modal[0])].some((n) => isIdent(n, 'migCopy')),
      'the modal does not render the shared modalCopy text',
    ).toBe(true);
  });

  it('the Migrate button is disabled while inputs are invalid (the gate before the modal)', () => {
    const button = [...walk(CARD.fragment)].find(
      (n) => {
        if (n.type !== 'RegularElement' || n.name !== 'button') return false;
        const attrs = (n.attributes as Node[]).filter(
          (a) => a.type === 'Attribute' && a.name === 'onclick',
        );
        return attrs.some((a) => {
          const val = a.value;
          const nodes = Array.isArray(val) ? val : [val];
          return nodes.some((v) => {
            const e = (v as Node)?.expression ?? v;
            return e && (e as Node).type === 'Identifier' && isIdent(e, 'requestMigration');
          });
        });
      },
    );
    expect(button, 'no button whose onclick is requestMigration').toBeTruthy();
    expect(
      [...walk(button!)].some((n) => isIdent(n, 'migError')),
      'the Migrate button is not gated on validation',
    ).toBe(true);
  });

  it('is reachable: the Preferences → Storage route renders StorageSettingsCard', () => {
    expect(ROUTE).toContain('<StorageSettingsCard');
  });
});
