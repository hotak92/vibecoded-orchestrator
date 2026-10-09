// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the chat-model-context pane's `text_only` column
// (v0.2.101, GUI gap G4).
//
// THE DEFECT IT PINS: the shipped seed gained `text_only` (the
// image-capability flag the model gateway reads), but the launcher's mirror
// of that seed — row type, parser, converge, export and this pane — dropped
// it on the floor: the Preferences page could not show which models take
// text-only input, and an edit or reseed silently lost the flag.
//
// WHAT IS TESTED WHERE: the Rust half (parse/converge/export carrying the
// flag, migration 048) is unit-tested in
// `vct-launcher-core/src/db/chat_model_context.rs` and `migrations.rs`; the
// draft/validation half in `api/chat_model_context.test.ts`. This file pins
// the half those cannot see: that the PANE renders `row.text_only` as its
// own column and edits it through `draft.text_only`. Parsed with the Svelte
// compiler so a mention in a comment does not satisfy it (same shape as
// `bundle-staleness.wiring.test.ts`).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
// SF-1 (v0.2.101 review): the TS↔Rust field-mirror extractors and the Rust
// source loader, from their one homes in test-support.
import { loadRust } from './test-support/source-census';
import { rustStructFields, tsInterfaceFields } from './test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const PANE = parse(
  readFileSync(
    resolve(here, '../routes/preferences/chat-model-context/+page.svelte'),
    'utf8',
  ),
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

/** An identifier `name` appears as real syntax somewhere in `node`. */
function subtreeHasIdentifier(node: unknown, name: string): boolean {
  return [...walk(node)].some((n) => isIdent(n, name));
}

describe('chat-model-context pane — text_only is wired (v0.2.101 G4)', () => {
  it('renders row.text_only in the table (its own column, both branches)', () => {
    const members = [...walk(PANE)].filter((n) => isMember(n, 'row', 'text_only'));
    expect(members.length).toBeGreaterThan(0);
    // Both outcomes of the branch must be spelled: the flagged badge and
    // the image-capable default. Raw source check because the text lives
    // in Text nodes, not the identifier.
    const source = readFileSync(
      resolve(here, '../routes/preferences/chat-model-context/+page.svelte'),
      'utf8',
    );
    expect(source).toContain('text only');
    expect(source).toContain('>image<');
  });

  it('edits the flag through draft.text_only (a user edit cannot drop it)', () => {
    const source = readFileSync(
      resolve(here, '../routes/preferences/chat-model-context/+page.svelte'),
      'utf8',
    );
    expect(source).toContain('bind:checked={draft.text_only}');
  });

  it('the table header declares the Input column', () => {
    const source = readFileSync(
      resolve(here, '../routes/preferences/chat-model-context/+page.svelte'),
      'utf8',
    );
    expect(source).toMatch(/<th[^>]*>Input<\/th>/);
  });
});

// ─── the ReseedOutcome mirror (v0.2.101, review SF-1) ──────────────────────
// THE DEFECT IT PINS: the Rust `ReseedOutcome` gained `retired` (the NB-02
// converge/reseed retire counter) but the TS mirror kept four counters — a
// drifted mirror type-checks and silently drops the field at runtime, so the
// reseed toast under-reported deletions its own click performed. Same shape
// as the `PackInfo` mirror in `packs.wiring.test.ts`.

describe('TS ReseedOutcome mirrors the Rust ReseedOutcome struct', () => {
  it('field sets are identical (and the retire counter is among them)', () => {
    const db = loadRust().find(
      (f) => f.rel === 'vct-launcher-core/src/db/chat_model_context.rs',
    );
    expect(db, 'the chat_model_context.rs crate file moved?').toBeTruthy();
    const rust = rustStructFields(db!.text, 'ReseedOutcome');
    const ts = tsInterfaceFields(
      readFileSync(resolve(here, 'types/chat-model-context.ts'), 'utf8'),
      'ReseedOutcome',
    );
    expect(rust.length).toBeGreaterThan(0);
    // (Red-proof mutation: delete `retired: number;` from the TS interface
    // and the set comparison below fails.)
    expect(rust).toContain('retired');
    expect([...ts].sort()).toEqual([...rust].sort());
  });
});

// ─── the edited-vs-shipped badge (v0.2.102, OD-3) ──────────────────────────
// THE DEFECT IT PINS: a shipped row can drift from the seed without the
// `user_edited` flag (machine-seeded, an import, an older seed), and a row
// edited BACK to the shipped values keeps the flag while matching the seed
// again — so the flag alone cannot answer "does this row still say what
// VCO ships?". OD-3 ordered the minimal indicator: a backend-computed
// per-row diff against the embedded seed, a badge per differing row, and a
// read-only (field, shipped, yours) popover. A wiring test rather than a
// unit test because the badge's data comes from a Tauri command and this
// repo's vitest runs headless (no component runner): what CAN be pinned is
// that the pane asks the command, branches per row on its answer, and
// renders the popover's three columns.

describe('chat-model-context pane — the edited-vs-shipped badge is wired (OD-3)', () => {
  const PANE_SOURCE = readFileSync(
    resolve(here, '../routes/preferences/chat-model-context/+page.svelte'),
    'utf8',
  );

  /** `editedMap.get(...)` — a CALL on the derived map of the command's rows. */
  function subtreeCallsEditedMapGet(node: unknown): boolean {
    return [...walk(node)].some((n) => {
      if (n.type !== 'CallExpression') return false;
      const callee = n.callee as Node | undefined;
      return (
        callee?.type === 'MemberExpression' &&
        isIdent(callee.object, 'editedMap') &&
        isIdent(callee.property, 'get')
      );
    });
  }

  it('asks the backend command and stores its answer (never re-derives the diff)', () => {
    // The api wrapper invokes the Rust command by name — the pane does not
    // compute the comparison client-side (the seed comparison is the
    // backend's, one home).
    const api = readFileSync(resolve(here, 'api/chat_model_context.ts'), 'utf8');
    expect(api).toContain('chat_model_context_edited_diff');
    // The pane loads it alongside rows/status and keeps it in state.
    expect(PANE_SOURCE).toContain('getChatModelContextEditedDiff');
    expect(PANE_SOURCE).toMatch(/editedDiffs\s*=\s*\$state/);
    // No client-side re-derivation: the pane never parses the seed itself.
    expect(PANE_SOURCE).not.toContain('seed.json');
  });

  it('branches per row on the edited-diff map and RENDERS the badge', () => {
    // Locate the badge by its TEST (a call on the command's map), then
    // assert on what its CONSEQUENT displays — a branch that exists but
    // renders nothing is indistinguishable from no branch (same two-claim
    // shape as the SecretsPanel wiring guard).
    const arm = [...walk(PANE)]
      .filter((n) => n.type === 'IfBlock')
      .find((n) => n.test && subtreeCallsEditedMapGet(n.test));
    expect(
      arm,
      'no {#if} tests `editedMap.get(...)`; a row differing from the ' +
        'shipped seed would render no badge',
    ).toBeTruthy();
    const shown = [...walk(arm!.consequent)]
      .filter((n) => n.type === 'Text' && typeof n.data === 'string')
      .map((n) => n.data as string)
      .join(' ')
      .replace(/\s+/g, ' ');
    expect(shown).toContain('vs shipped');
  });

  it('renders the read-only popover with the three columns, gated on the same map', () => {
    // The popover row must be conditioned on BOTH the open row (`diffOpen`)
    // and the map, and show field / shipped / yours — the evidence half of
    // OD-3. Locate by the two-part test; assert on the consequent.
    const arm = [...walk(PANE)]
      .filter((n) => n.type === 'IfBlock')
      .find(
        (n) =>
          n.test &&
          subtreeHasIdentifier(n.test, 'diffOpen') &&
          subtreeCallsEditedMapGet(n.test),
      );
    expect(arm, 'the diff popover has no {#if} gating it on diffOpen and the map').toBeTruthy();
    const shown = [...walk(arm!.consequent)]
      .filter((n) => n.type === 'Text' && typeof n.data === 'string')
      .map((n) => n.data as string)
      .join(' ')
      .replace(/\s+/g, ' ')
      .toLowerCase();
    for (const column of ['field', 'shipped', 'yours']) {
      expect(shown, `the popover renders the '${column}' column`).toContain(column);
    }
    // Read-only: no <input> inside the popover — the Edit button above is
    // the change surface, the popover is the evidence.
    expect(
      [...walk(arm!.consequent)].some((n) => n.type === 'InputElement'),
      'the diff popover must not contain an input',
    ).toBe(false);
  });

  it('the Rust command exists and is registered in the invoke handler', () => {
    const cmd = loadRust().find(
      (f) => f.rel === 'src/commands/chat_model_context.rs',
    );
    expect(cmd, 'the commands/chat_model_context.rs file moved?').toBeTruthy();
    expect(cmd!.text).toContain('pub async fn chat_model_context_edited_diff');
    const lib = loadRust().find((f) => f.rel === 'src/lib.rs');
    expect(lib, 'src/lib.rs moved?').toBeTruthy();
    expect(lib!.text).toContain('commands::chat_model_context::chat_model_context_edited_diff');
  });
});
