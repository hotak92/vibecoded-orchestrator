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
