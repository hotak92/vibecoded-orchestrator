// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.92 (review MAJOR-10) — struct ↔ interface parity for `UpdateSummary`.
//
// The failure this closes is a CLASS, not an incident. Twice now a field the
// Rust struct serialises has been absent from the TS interface, and both times
// the consequence was a dishonest toast rather than a compile error:
//
//   * `adopted` — an update that backed up and replaced every file the user
//     had edited reported "Project bundle already up to date"
//     (`stores/bundle-summary-logic.ts` exists because of it);
//   * `kg_or_docs_content_changed` — the neighbouring field, still missing
//     when the review looked, because fixing one instance of a class by hand
//     does not fix the class.
//
// Neither the compiler nor the existing vitest could catch it: nothing
// CONSTRUCTS an `UpdateSummary` in the frontend (it arrives from `invoke`, and
// the fixture casts `as UpdateSummary`), so a missing field is simply a field
// nobody reads. A source-level diff of the two declarations is the cheap check
// that does catch it — the same "pin the two homes to each other" pattern
// `kg-sync-banner-logic.test.ts` uses for the Svelte components.
//
// Scope note: this pins the FIELD SET, not the types. A `u32` that becomes
// `i64` is a different (and much rarer) failure; the one that has actually
// shipped twice is a field going missing.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** Repo root: launcher/src/lib/types → launcher/src/lib → src → launcher → root. */
const REPO_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));

const RUST_SRC = `${REPO_ROOT}launcher/src-tauri/src/commands/projects_v2.rs`;
const TS_SRC = `${REPO_ROOT}launcher/src/lib/types/launcher.ts`;

/**
 * Body of a brace-delimited block starting at `header`, matched by counting
 * braces rather than by a lazy `[\s\S]*?}` — a nested `{}` (an attribute, a
 * generic default, a mapped type) would truncate the lazy form silently, and a
 * parity test that silently reads half a struct is worse than none.
 */
function blockBody(source: string, header: string): string {
  const start = source.indexOf(header);
  if (start < 0) throw new Error(`declaration not found: ${header}`);
  let i = source.indexOf('{', start);
  if (i < 0) throw new Error(`no opening brace for: ${header}`);
  let depth = 0;
  const bodyStart = i + 1;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(bodyStart, i);
    }
  }
  throw new Error(`unterminated block: ${header}`);
}

/** Strip `///` + `//` comments and `#[…]` attribute lines from a Rust body. */
function stripRustNoise(body: string): string {
  return body
    .split('\n')
    .filter((l) => !/^\s*(\/\/|#\[)/.test(l))
    .join('\n');
}

/** Strip JSDoc/block comments and `//` lines from a TS body. */
function stripTsNoise(body: string): string {
  return body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((l) => !/^\s*\/\//.test(l))
    .join('\n');
}

/** `pub <name>: …` field names of a Rust struct body. */
export function rustFieldNames(body: string): string[] {
  return [...stripRustNoise(body).matchAll(/^\s*pub\s+([A-Za-z_]\w*)\s*:/gm)].map(
    (m) => m[1],
  );
}

/** `<name>[?]: …` property names of a TS interface body. */
export function tsFieldNames(body: string): string[] {
  return [...stripTsNoise(body).matchAll(/^\s*([A-Za-z_]\w*)\??\s*:/gm)].map(
    (m) => m[1],
  );
}

describe('UpdateSummary — Rust struct ↔ TS interface parity (review MAJOR-10)', () => {
  const rustBody = blockBody(
    readFileSync(RUST_SRC, 'utf-8'),
    'pub struct UpdateSummary',
  );
  const tsBody = blockBody(
    readFileSync(TS_SRC, 'utf-8'),
    'export interface UpdateSummary',
  );

  const rust = rustFieldNames(rustBody);
  const ts = tsFieldNames(tsBody);

  it('reads a plausible struct and interface (guards the regexes themselves)', () => {
    // A parity test whose extractors quietly returned [] would pass forever.
    expect(rust.length).toBeGreaterThanOrEqual(8);
    expect(ts.length).toBeGreaterThanOrEqual(8);
    expect(rust).toContain('adopted');
    expect(ts).toContain('adopted');
  });

  it('the wire names are the field names (no serde rename in play)', () => {
    // The comparison below equates Rust FIELD names with TS PROPERTY names,
    // which is only sound while the struct carries no `rename` / `rename_all`.
    // If one is ever added, this fails and forces the mapping to be made
    // explicit rather than assumed.
    expect(rustBody).not.toMatch(/rename/);
  });

  it('declares exactly the same field set on both sides', () => {
    const missingInTs = rust.filter((f) => !ts.includes(f));
    const extraInTs = ts.filter((f) => !rust.includes(f));
    expect({ missingInTs, extraInTs }).toEqual({
      missingInTs: [],
      extraInTs: [],
    });
  });

  it('still carries the two fields this class of bug has already dropped', () => {
    for (const field of ['adopted', 'kg_or_docs_content_changed']) {
      expect(rust).toContain(field);
      expect(ts).toContain(field);
    }
  });
});

// The extractors are the load-bearing part — if they mis-parse, the parity
// assertion above is decorative. Pinned against inline fixtures so a change to
// them shows up here rather than as a parity test that stopped testing.
describe('field extractors', () => {
  it('ignores Rust doc comments, attributes and nested braces', () => {
    const body = `
    /// A doc comment mentioning pub fake: u32
    #[serde(default)]
    pub real: u32,
    // pub commented_out: u32,
    pub other: Option<HashMap<String, u32>>,
`;
    expect(rustFieldNames(body)).toEqual(['real', 'other']);
  });

  it('ignores TS block comments', () => {
    const body = `
  /** Doc mentioning fake: number and a stray * / shape. */
  real: number;
  optional?: boolean;
`;
    expect(tsFieldNames(body)).toEqual(['real', 'optional']);
  });

  it('brace-counts rather than lazy-matching to the first "}"', () => {
    const src = 'struct X { a: Foo<{ nested: 1 }>, b: u32 }';
    expect(blockBody(src, 'struct X')).toContain('b: u32');
  });
});
