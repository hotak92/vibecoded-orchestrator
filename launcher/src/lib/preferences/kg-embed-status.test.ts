// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-I — the KG embedding-model status. (W6-J moved the duplicate
// scan to the project's KG tab — project-state/kg-duplicates.test.ts covers
// it — and kept this panel to the per-collection fill status.)
//
// Two layers:
//   1. the helper DRIVES the real invoke (injected `invoke`) and phrases the
//      fill progress;
//   2. AST wiring: the component loads the status on mount, and the
//      Preferences page mounts it.
// WHAT IT DOES NOT PROVE: real-DOM click delivery (no DOM harness in this
// suite) and the Python answers (tests/test_v02102_kg_embed_model.py).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from '../test-support/wiring-ast';
import { kgEmbedFillText, loadKgEmbedStatus, type KgEmbedCollection } from './kg-embed-status';

const here = dirname(fileURLToPath(import.meta.url));
type Node = Record<string, unknown>;

function recordingInvoke(result: unknown) {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  const invoke = async <T>(cmd: string, args?: Record<string, unknown>): Promise<T> => {
    calls.push([cmd, args]);
    return result as T;
  };
  return { calls, invoke };
}

const base: KgEmbedCollection = {
  collection: 'Acme_KnowledgeGraph',
  recorded_model: 'qwen3-embedding:0.6b',
  objects: null,
  target_model: 'snowflake-arctic-embed2:latest',
  filling: true,
  filled: null,
  total: null,
  complete: false,
  cost_bearing: false,
  target_refused: false,
};

describe('helpers drive the real commands', () => {
  it('loadKgEmbedStatus invokes get_kg_embed_model_status', async () => {
    const d = recordingInvoke({ target: 'x', projects: [] });
    const out = await loadKgEmbedStatus(d);
    expect(d.calls).toEqual([['get_kg_embed_model_status', undefined]]);
    expect(out.projects).toEqual([]);
  });
});

describe('fill progress text', () => {
  it('progress stamped → "n of N"', () => {
    expect(kgEmbedFillText({ ...base, filled: 3, total: 10 })).toContain('3 of 10');
  });
  it('fill owed, no progress yet → pending with the count, started by VCO', () => {
    const t = kgEmbedFillText({ ...base, total: 12 });
    expect(t).toContain('up to 12');
    expect(t).toContain('next whole-tree sync');
  });
  it('complete', () => {
    expect(kgEmbedFillText({ ...base, filling: false, complete: true, recorded_model: base.target_model }))
      .toBe('complete');
  });
  it('no record yet', () => {
    expect(kgEmbedFillText({ ...base, recorded_model: null, filling: false })).toContain('not recorded yet');
  });
  it('refused OpenAI target names the toggle and states no API call', () => {
    const t = kgEmbedFillText({ ...base, target_refused: true });
    expect(t).toContain('Allow OpenAI for embeddings');
    expect(t).toContain('no API call');
  });
});

describe('component + page wiring (AST)', () => {
  const src = readFileSync(resolve(here, 'KgEmbedStatus.svelte'), 'utf8');
  const ast = parse(src, { modern: true }) as unknown as Node;
  const instance = (ast.instance as Node).content as Node;
  const fragment = ast.fragment as Node;

  function fn(name: string): Node {
    for (const n of walk(instance)) if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
    throw new Error(`no ${name}()`);
  }
  const calls = (root: unknown, name: string) =>
    [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));

  it('refresh() loads the status and onMount runs refresh()', () => {
    expect(calls(fn('refresh'), 'loadKgEmbedStatus').length).toBe(1);
    const mounts = calls(instance, 'onMount');
    expect(mounts.length).toBe(1);
    expect(calls(mounts[0], 'refresh').length).toBe(1);
  });

  it('W6-J: the duplicate scan is gone from Preferences (it lives on the KG tab now)', () => {
    expect(calls(instance, 'runDuplicateScan').length).toBe(0);
    expect([...walk(fragment)].filter((n) => n.type === 'RegularElement' && n.name === 'button').length).toBe(0);
  });

  it('the Preferences page mounts <KgEmbedStatus />', () => {
    const page = parse(readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf8'), {
      modern: true,
    }) as unknown as Node;
    const mounted = [...walk(page.fragment)].filter(
      (n) => (n.type === 'Component' || n.type === 'InlineComponent') && n.name === 'KgEmbedStatus',
    );
    expect(mounted.length).toBe(1);
  });
});
