// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.92 duplication-merge (PLAN-EXTENSION §3.12) — the Svelte `ContainerFullness`
// discriminated union in `+page.svelte` is a HAND-WRITTEN MIRROR of the Rust
// enum in `vct-launcher-core/src/services/picker.rs` (`#[serde(tag = "kind",
// rename_all = "snake_case")]`). Generating the TS type from the Rust source is
// out of reach in this build (no ts-rs / typeshare in the toolchain), so this
// test PINS the mirror instead: every Rust variant appears as a `kind: '<snake>'`
// arm in the Svelte union and vice versa, and the serde attributes that make
// `kind` the discriminator are still there.
//
// Why it matters: the picker table renders `fullnessSummary()` by switching on
// `kind`; a Rust variant added without a Svelte arm renders as "probe failed"
// for a container that probed fine, and a Svelte arm without a Rust variant is
// dead UI. Neither is a type error today — the payload crosses a JSON boundary.
//
// Red-proof: adding `Foo { .. }` to the Rust enum (or a `kind: 'foo'` arm to the
// union) fails this test naming the missing side.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const SVELTE = resolve(here, '+page.svelte');
const PICKER_RS = resolve(
  here,
  '../../../src-tauri/vct-launcher-core/src/services/picker.rs'
);

function rustVariantsSnakeCase(src: string): string[] {
  const start = src.indexOf('pub enum ContainerFullness {');
  expect(start, 'picker.rs: `pub enum ContainerFullness` not found').toBeGreaterThan(-1);
  // Body ends at the first `}` at column 0 after the enum header.
  const bodyEnd = src.indexOf('\n}', start);
  const body = src.slice(start, bodyEnd);
  const attrs = src.slice(Math.max(0, start - 200), start);
  expect(attrs).toContain('tag = "kind"');
  expect(attrs).toContain('rename_all = "snake_case"');
  const variants: string[] = [];
  for (const m of body.matchAll(/^\s{4}([A-Z][A-Za-z0-9]*)\s*\{/gm)) {
    variants.push(m[1].replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase());
  }
  return variants.sort();
}

function svelteKinds(src: string): string[] {
  const start = src.indexOf('type ContainerFullness =');
  expect(start, '+page.svelte: `type ContainerFullness =` not found').toBeGreaterThan(-1);
  // Object members inside the union carry their own `;`, so the alias ends
  // at the NEXT top-level declaration in the <script> block, not at a `;`.
  const rest = src.slice(start);
  const nextDecl = rest.search(/\n  (interface|type|let|const|function|\$:|export) /);
  expect(nextDecl, 'could not find the declaration after the union').toBeGreaterThan(0);
  const body = rest.slice(0, nextDecl);
  const kinds: string[] = [];
  for (const m of body.matchAll(/kind:\s*'([a-z0-9_]+)'/g)) kinds.push(m[1]);
  return kinds.sort();
}

describe('services page ContainerFullness mirrors picker.rs (§3.12 pin)', () => {
  const rs = readFileSync(PICKER_RS, 'utf8');
  const svelte = readFileSync(SVELTE, 'utf8');

  it('has the same discriminator values on both sides', () => {
    const rustKinds = rustVariantsSnakeCase(rs);
    const tsKinds = svelteKinds(svelte);
    expect(rustKinds.length).toBeGreaterThan(0);
    expect(tsKinds).toEqual(rustKinds);
  });

  it('the Svelte switch renders every kind (no arm falls through to "probe failed")', () => {
    for (const kind of svelteKinds(svelte)) {
      expect(svelte, `fullnessSummary() lacks a case for '${kind}'`).toContain(`case '${kind}':`);
    }
  });

  it('the snake_case converter is what serde does', () => {
    // Guards the converter itself so a false green cannot hide behind it.
    expect(rustVariantsSnakeCase('#[serde(tag = "kind", rename_all = "snake_case")]\npub enum ContainerFullness {\n    CodeEmbed {\n    },\n    Weaviate {\n    },\n}\n')).toEqual(['code_embed', 'weaviate']);
  });
});
