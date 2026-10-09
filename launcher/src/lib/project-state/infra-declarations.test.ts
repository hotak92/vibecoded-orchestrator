// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W5-A (M3): the Identity tab's "Infrastructure this project owns"
// panel — its pure helpers (correctness) and its wiring (the component is
// mounted, calls the registered commands, and the TS wire shape mirrors the
// Rust struct field-for-field).
//
// The launcher's vitest has no component runner, so the wiring half asserts
// on the real Svelte AST (svelte/compiler) — a comment naming a function can
// never satisfy an AST `CallExpression`, which is why this is not a text grep.

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import {
  KIND_SPECS,
  addValue,
  normaliseValue,
  removeValue,
  setPayload,
} from './infra-declarations';
import { walk, isIdent, rustStructFields, tsInterfaceFields } from '../test-support/wiring-ast';
import { sourceFile } from '../test-support/source-census';

const here = dirname(fileURLToPath(import.meta.url));

describe('normaliseValue mirrors the backend rule', () => {
  // The same cases as `normalise_matches_the_python_rule` (Rust) and the
  // Python parity table in tests/test_v02102_infra_boundary.py.
  const cases: [Parameters<typeof normaliseValue>[0], string, string | null][] = [
    ['external_collection', ' peerRuntime_Memory ', 'PeerRuntime_Memory'],
    ['external_collection', '1Bad', null],
    ['external_collection', 'has-dash', null],
    ['unmanaged_container', 'peer-weaviate.1', 'peer-weaviate.1'],
    ['unmanaged_container', '-lead', null],
    ['unmanaged_container', 'a b', null],
    ['local_port', '011436', '11436'],
    ['local_port', '0', null],
    ['local_port', '65536', null],
    ['local_port', '+80', null],
    ['unmanaged_container', '', null],
  ];
  for (const [kind, raw, want] of cases) {
    it(`${kind} ${JSON.stringify(raw)} -> ${JSON.stringify(want)}`, () => {
      expect(normaliseValue(kind, raw)).toBe(want);
    });
  }
});

describe('list editing', () => {
  it('adds a normalised value once and names a refused one', () => {
    expect(addValue('local_port', ['8082'], ' 11436 ')).toEqual({ values: ['8082', '11436'] });
    expect(addValue('local_port', ['8082'], '8082')).toEqual({ values: ['8082'] });
    const bad = addValue('unmanaged_container', [], 'bad name');
    expect('error' in bad && bad.error).toContain("'bad name'");
  });

  it('removes exactly the value', () => {
    expect(removeValue(['a', 'b'], 'a')).toEqual(['b']);
  });

  it('builds the camelCase args the Tauri command takes', () => {
    expect(setPayload('p1', 'local_port', ['11436'])).toEqual({
      projectId: 'p1',
      kind: 'local_port',
      values: ['11436'],
    });
  });

  it('renders the three kinds the backend stores', () => {
    expect(KIND_SPECS.map((s) => s.kind)).toEqual(['external_collection', 'unmanaged_container', 'local_port']);
  });
});

// ─── wiring ──────────────────────────────────────────────────────────────

function ast(file: string) {
  const src = readFileSync(resolve(here, file), 'utf8');
  return [...walk(parse(src, { modern: true }))];
}

function invokedNames(nodes: Record<string, unknown>[]): string[] {
  return nodes
    .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'))
    .map((n) => ((n.arguments as Record<string, unknown>[])[0] as Record<string, unknown>)?.value as string);
}

describe('InfraDeclarationsPanel wiring', () => {
  it('IdentityTab renders the panel', () => {
    const nodes = ast('IdentityTab.svelte');
    const rendered = nodes.some(
      (n) => n.type === 'Component' && (n as { name?: string }).name === 'InfraDeclarationsPanel',
    );
    expect(rendered).toBe(true);
  });

  it('the panel loads and saves through the registered commands', () => {
    const nodes = ast('InfraDeclarationsPanel.svelte');
    const names = invokedNames(nodes);
    expect(names).toContain('get_project_infra_declarations');
    expect(names).toContain('set_project_infra_declarations');
    // The save passes the helper-built payload (not a hand-rolled object).
    expect(nodes.some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'setPayload'))).toBe(true);
    const lib = readFileSync(resolve(here, '../../../src-tauri/src/lib.rs'), 'utf8');
    expect(lib).toMatch(/commands::infra_declarations::get_project_infra_declarations/);
    expect(lib).toMatch(/commands::infra_declarations::set_project_infra_declarations/);
  });

  it('the TS wire shapes mirror the Rust structs field-for-field', () => {
    const ts = sourceFile('infra-declarations.ts', readFileSync(resolve(here, 'infra-declarations.ts'), 'utf8'), 'ts');
    const core = sourceFile(
      'infra_declarations.rs',
      readFileSync(resolve(here, '../../../src-tauri/vct-launcher-core/src/db/infra_declarations.rs'), 'utf8'),
      'rust',
    );
    const cmd = sourceFile(
      'commands/infra_declarations.rs',
      readFileSync(resolve(here, '../../../src-tauri/src/commands/infra_declarations.rs'), 'utf8'),
      'rust',
    );
    expect(tsInterfaceFields(ts.text, 'InfraDeclarations').sort()).toEqual(
      rustStructFields(core.text, 'InfraDeclarations').sort(),
    );
    expect(tsInterfaceFields(ts.text, 'InfraDeclarationsWriteResult').sort()).toEqual(
      rustStructFields(cmd.text, 'InfraDeclarationsWriteResult').sort(),
    );
  });
});
