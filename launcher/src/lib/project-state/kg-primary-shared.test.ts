// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.105: the Identity tab's "primary KG is the shared collection" banner —
// its pure helpers (both ways: repairable / not) and its wiring (the panel is
// mounted for non-root projects, reads the registered status command and
// repairs through `update_project_identity` with the helper-built request;
// the TS wire shape mirrors the Rust struct; the ledger condition it repairs
// is a registered action_required row).
//
// The launcher's vitest has no component runner, so the wiring half asserts
// on the real Svelte AST (svelte/compiler), as infra-declarations.test.ts does.

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import {
  CID_KG_PRIMARY_BOUND_TO_SHARED,
  bannerMessage,
  repairRequest,
  type KgPrimarySharedStatus,
} from './kg-primary-shared';
import { walk, isIdent, rustStructFields, tsInterfaceFields } from '../test-support/wiring-ast';
import { sourceFile } from '../test-support/source-census';

const here = dirname(fileURLToPath(import.meta.url));

const SHARED = 'VibeCodedOrchestrator_KnowledgeGraph';
const bound: KgPrimarySharedStatus = {
  bound_to_shared: true,
  bound: SHARED,
  expected: 'MyApp_KnowledgeGraph',
  expected_is_shared: false,
};

describe('repair request + banner', () => {
  it('a bound project gets a one-click repair to its name-derived class', () => {
    expect(repairRequest(bound)).toEqual({ kg_collection: 'MyApp_KnowledgeGraph', code_graph_project: null });
    const msg = bannerMessage(bound) ?? '';
    expect(msg).toContain(SHARED);
    expect(msg).toContain('MyApp_KnowledgeGraph');
    expect(msg).toContain('nothing is moved or deleted');
  });

  it('a healthy project, or no reading, shows nothing and repairs nothing', () => {
    const healthy = { ...bound, bound_to_shared: false };
    expect(repairRequest(healthy)).toBeNull();
    expect(bannerMessage(healthy)).toBeNull();
    expect(repairRequest(null)).toBeNull();
    expect(bannerMessage(null)).toBeNull();
  });

  it('a project whose name derives the shared class gets the banner but no one-click target', () => {
    const own = { ...bound, expected: SHARED, expected_is_shared: true };
    expect(repairRequest(own)).toBeNull();
    expect(bannerMessage(own)).toContain('Type a KG collection name of its own');
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

describe('KgPrimarySharedPanel wiring', () => {
  it('IdentityTab renders the panel', () => {
    const nodes = ast('IdentityTab.svelte');
    expect(
      nodes.some((n) => n.type === 'Component' && (n as { name?: string }).name === 'KgPrimarySharedPanel'),
    ).toBe(true);
  });

  it('the panel reads the registered status command and repairs through update_project_identity', () => {
    const nodes = ast('KgPrimarySharedPanel.svelte');
    const names = invokedNames(nodes);
    expect(names).toContain('get_kg_primary_shared_status');
    expect(names).toContain('update_project_identity');
    expect(nodes.some((n) => n.type === 'CallExpression' && isIdent(n.callee, 'repairRequest'))).toBe(true);
    const lib = readFileSync(resolve(here, '../../../src-tauri/src/lib.rs'), 'utf8');
    expect(lib).toMatch(/commands::project_identity::get_kg_primary_shared_status/);
    expect(lib).toMatch(/commands::project_identity::update_project_identity/);
  });

  it('the TS wire shape mirrors the Rust struct field-for-field', () => {
    const ts = sourceFile('kg-primary-shared.ts', readFileSync(resolve(here, 'kg-primary-shared.ts'), 'utf8'), 'ts');
    const rs = sourceFile(
      'commands/project_identity.rs',
      readFileSync(resolve(here, '../../../src-tauri/src/commands/project_identity.rs'), 'utf8'),
      'rust',
    );
    expect(tsInterfaceFields(ts.text, 'KgPrimarySharedStatus').sort()).toEqual(
      rustStructFields(rs.text, 'KgPrimarySharedStatus').sort(),
    );
  });

  it('the condition it repairs is a registered action_required row (vco_lib/deferral_conditions.toml)', () => {
    const toml = readFileSync(resolve(here, '../../../../vco_lib/deferral_conditions.toml'), 'utf-8');
    const start = toml.indexOf(`[conditions.${CID_KG_PRIMARY_BOUND_TO_SHARED}]`);
    expect(start).toBeGreaterThan(-1);
    const row = toml.slice(start, toml.indexOf('\n[conditions.', start + 1));
    expect(row).toMatch(/^class = "action_required"$/m);
  });
});
