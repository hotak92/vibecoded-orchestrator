// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The ONE home of the source-AST extractors shared by the launcher's
// wiring tests (v0.2.101, L3 review SF-5). Previously `registeredCommands`
// lived in `invoke-names.test.ts` (copied verbatim into
// `packs.wiring.test.ts`) and `walk`/`isIdent` in
// `bundle-staleness.wiring.test.ts` (also copied) — three implementations
// of the same extractors across three test files. Test-only: no runtime
// module imports this file.
//
// Importing a `.test.ts` from another test file would re-run its whole
// suite inside the importer — that is why these live HERE, in test-support
// (which `source-census.ts` already established), not in any test file.

import type { SourceFile } from './source-census';

/** Command names in the `generate_handler![ … ]` list of lib.rs. `#[cfg]`
 *  attributes are skipped; the name is the last path segment. */
export function registeredCommands(lib: SourceFile): string[] {
  const k = lib.code.search(/\bgenerate_handler!\s*\[/);
  if (k < 0) return [];
  const open = lib.code.indexOf('[', k);
  let depth = 0;
  let end = open;
  for (; end < lib.code.length; end++) {
    if (lib.code[end] === '[') depth++;
    else if (lib.code[end] === ']' && --depth === 0) break;
  }
  const body = lib.code.slice(open + 1, end).replace(/#\[[^\]]*\]/g, ' ');
  return body
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '')
    .map((s) => s.split('::').pop()!.trim());
}

/** Every node in a subtree, depth-first. `parent` and `loc` keys are
 *  skipped (circular / bulky). */
export function* walk(node: unknown): Generator<Record<string, unknown>> {
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

/** Identifier node named `name`? (Svelte AST from `svelte/compiler`.) */
export function isIdent(n: unknown, name: string): boolean {
  return (
    (n as Record<string, unknown> | undefined)?.type === 'Identifier' &&
    (n as Record<string, unknown>).name === name
  );
}
