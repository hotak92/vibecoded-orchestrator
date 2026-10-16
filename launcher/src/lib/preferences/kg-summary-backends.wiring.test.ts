// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the v0.2.103 (8b) cloud vendor tiers in the
// Preferences → KG Summaries backend pin:
//
//   * the radio group offers the two new tiers ('zai', 'qwen') between the
//     CLI and local Ollama, each wired to saveKgSummaryOverride so picking
//     one writes kg_summary_backend_override through app_state_set;
//   * the load-side allowlist accepts them (a pin stored by an older
//     launcher or by hand must not be silently reset to Auto);
//   * the tier names match the Python VALID_BACKENDS — the behavioural
//     half (a 'zai'/'qwen' pin actually forces that tier) is pinned in
//     tests/test_v02103_summary_ladder_cloud_and_vram.py.
//
// Same established AST pattern as embedding-preferences.wiring.test.ts: a
// grep is satisfied by comments, so every assertion walks the parsed
// Svelte AST instead.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from '../test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

type Node = Record<string, unknown>;

const src = readFileSync(
  resolve(here, '../../routes/preferences/+page.svelte'),
  'utf-8',
);
const page = parse(src, { modern: true }) as unknown as Node;
const instance = page.instance as Node;
const fragment = page.fragment as Node;

function findFunction(root: Node, name: string): Node | null {
  for (const n of walk(root)) {
    if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
  }
  return null;
}

/** String literals inside one function body. */
function stringLiterals(root: unknown): string[] {
  return [...walk(root)]
    .filter((n) => n.type === 'Literal' && typeof n.value === 'string')
    .map((n) => n.value as string);
}

/** `<input>` elements of the kg-summary-override radio group: value attr
 *  plus the handler their onchange invokes. */
function overrideRadios(): Map<string, string> {
  const out = new Map<string, string>();
  for (const n of walk(fragment)) {
    if (n.type !== 'RegularElement' || n.name !== 'input') continue;
    const attrs = (n.attributes as Node[] | undefined) ?? [];
    const nameAttr = attrs.find((a) => a.name === 'name');
    const valueAttr = attrs.find((a) => a.name === 'value');
    const changeAttr = attrs.find((a) => a.name === 'onchange');
    if (!nameAttr || !valueAttr || !changeAttr) continue;
    // name="kg-summary-override" is a plain Text attribute value.
    const nameVal = Array.isArray(nameAttr.value)
      ? (nameAttr.value as Node[])
          .filter((x: Node) => x.type === 'Text')
          .map((x: Node) => (x.raw ?? x.data ?? '') as string)
          .join('')
      : '';
    if (nameVal !== 'kg-summary-override') continue;
    const value = Array.isArray(valueAttr.value)
      ? (valueAttr.value as Node[])
          .filter((x: Node) => x.type === 'Text')
          .map((x: Node) => (x.raw ?? x.data ?? '') as string)
          .join('')
      : '';
    // onchange={() => void saveKgSummaryOverride('zai')} — the first string
    // literal inside the handler expression is the tier name it saves.
    const handlerLiterals = stringLiterals(changeAttr).filter(
      (s) => s !== 'kg-summary-override',
    );
    const saved = handlerLiterals[0] ?? '';
    out.set(value, saved);
  }
  return out;
}

describe('v0.2.103 8b — cloud vendor tiers in the KG-summary backend pin', () => {
  it('offers every tier the Python VALID_BACKENDS accepts, in ladder order', () => {
    const radios = overrideRadios();
    expect([...radios.keys()]).toEqual([
      '',
      'cli',
      'zai',
      'qwen',
      'ollama',
      'openai',
      'skip',
    ]);
  });

  it.each([
    ['zai', 'Z.ai GLM'],
    ['qwen', 'QwenCloud'],
  ])('the %s radio writes the pin through saveKgSummaryOverride', (tier) => {
    const radios = overrideRadios();
    expect(radios.get(tier)).toBe(tier);
    const save = findFunction(instance, 'saveKgSummaryOverride');
    expect(save, 'no saveKgSummaryOverride() handler').toBeTruthy();
    // The handler still writes the one app_state key via invoke('app_state_set', ...).
    const invokes = [...walk(save)].filter(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'),
    );
    expect(invokes.length).toBeGreaterThan(0);
    const ids = [...walk(invokes[0])]
      .filter((n) => n.type === 'Identifier')
      .map((n) => n.name as string);
    expect(ids).toContain('APP_STATE_KEY_KG_SUMMARY_OVERRIDE');
    expect(stringLiterals(invokes[0])).toContain('app_state_set');
  });

  it('the load-side allowlist accepts the new tier names (a stored pin survives)', () => {
    const load = findFunction(instance, 'loadKgSummarySettings');
    expect(load, 'no loadKgSummarySettings() handler').toBeTruthy();
    const literals = stringLiterals(load);
    for (const tier of ['', 'cli', 'zai', 'qwen', 'ollama', 'openai', 'skip']) {
      expect(
        literals,
        `loadKgSummarySettings must accept '${tier}'`,
      ).toContain(tier);
    }
  });

  it('the Auto label states the real ladder order (cloud before local)', () => {
    // The Auto radio's <span> names the order the ladder actually tries.
    const autoLabels = [...walk(fragment)].filter(
      (n) =>
        n.type === 'Text' &&
        typeof n.data === 'string' &&
        n.data.includes('Auto (claude CLI'),
    );
    expect(autoLabels.length).toBeGreaterThan(0);
    const text = (autoLabels[0].data as string).replace(/\s+/g, ' ');
    expect(text).toContain('cloud vendor');
    expect(text.indexOf('cloud vendor')).toBeLessThan(text.indexOf('local Ollama'));
  });
});
