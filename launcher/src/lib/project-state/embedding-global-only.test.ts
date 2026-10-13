// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-J — ONE global embedding profile (owner ruling 2026-10-11:
// "I'd just remove the per-project selection and go with one single global
// setting"). Preferences → Embedding profile is the only selector; the project
// Settings tab no longer mounts a per-project picker, and the model-switch
// panel that picker opened in RegenerateOrDeferModal is gone (the KG sync's
// additive fill handles a model change by itself).
//
// The backend side (every resolver global-only, retired rows unread) is pinned
// in Rust (project_env_settings / config_api / project_backfill tests) and
// Python (tests/test_config_projection.py, tests/test_v02102_kg_embed_model.py);
// the invoke census (invoke-names.test.ts) fails if any frontend file still
// invokes a removed command.

import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk } from '../test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));
type Node = Record<string, unknown>;

function componentNames(file: string): string[] {
  const ast = parse(readFileSync(file, 'utf8'), { modern: true }) as unknown as Node;
  return [...walk(ast.fragment)]
    .filter((n) => n.type === 'Component')
    .map((n) => String(n.name));
}

describe('one global embedding profile', () => {
  it('the project Settings tab mounts no per-project embedding picker', () => {
    const names = componentNames(resolve(here, 'SettingsTab.svelte'));
    expect(names).not.toContain('ActiveEmbeddingPicker');
    expect(existsSync(resolve(here, 'ActiveEmbeddingPicker.svelte'))).toBe(false);
  });

  it('the regenerate-or-defer modal takes no model-switch context', () => {
    const src = readFileSync(resolve(here, '../components/RegenerateOrDeferModal.svelte'), 'utf8');
    const ast = parse(src, { modern: true }) as unknown as Node;
    const props = [...walk((ast.instance as Node).content)].find(
      (n) =>
        n.type === 'VariableDeclarator' &&
        (n.init as Node)?.type === 'CallExpression' &&
        ((n.init as Node).callee as Node)?.type === 'Identifier' &&
        (((n.init as Node).callee as Node).name as string) === '$props',
    ) as Node | undefined;
    expect(props, 'no $props() destructure').toBeTruthy();
    const keys = (((props!.id as Node).properties as Node[]) ?? []).map(
      (p) => ((p.key as Node)?.name as string) ?? '',
    );
    expect(keys).toEqual(expect.arrayContaining(['projectId', 'artifacts', 'onClose']));
    expect(keys).not.toContain('modelSwitch');
  });
});
