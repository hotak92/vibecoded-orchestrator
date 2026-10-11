// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-I — the KG embedding-model status + duplicate-scan skip list.
//
// Three layers:
//   1. the helpers DRIVE the real invokes (injected `invoke`) and phrase the
//      payloads (fill progress, the "objects missing the vector" why);
//   2. the TS payload types mirror the Rust structs field-for-field, so a
//      field the scanner emits cannot silently vanish between Rust and the
//      GUI (W6-H's `objects_missing_vector` was dropped by the Rust payload
//      before this lane);
//   3. AST wiring: the component loads the status on mount, its button runs
//      the scan, the skip list renders from `objects_missing_vector`, and
//      the Preferences page mounts the component.
// WHAT IT DOES NOT PROVE: real-DOM click delivery (no DOM harness in this
// suite) and the Python answers (tests/test_v02102_kg_embed_model.py).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { sourceFile } from '../test-support/source-census';
import { isIdent, rustStructFields, tsInterfaceFields, walk } from '../test-support/wiring-ast';
import {
  hasSlotsText,
  kgEmbedFillText,
  loadKgEmbedStatus,
  missingVectorExplanation,
  runDuplicateScan,
  type KgEmbedCollection,
  type MissingVectorObject,
} from './kg-embed-status';

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

  it('runDuplicateScan invokes kg_check_duplicates with the project id', async () => {
    const d = recordingInvoke({ threshold: 0.95, pairs: [], nodes_skipped: 0, objects_missing_vector: [] });
    await runDuplicateScan('p-1', d);
    expect(d.calls).toEqual([['kg_check_duplicates', { projectId: 'p-1' }]]);
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

describe('the duplicate scan skip list', () => {
  const objs: MissingVectorObject[] = [
    { uuid: 'u1', title: 'Node A', path: 'knowledge/a.md', target_vector: 'qwen3_embed', has_slots: ['arctic2_embed'] },
    { uuid: 'u2', title: 'Node B', path: 'knowledge/b.md', target_vector: 'qwen3_embed', has_slots: [] },
  ];
  it('explains why the objects were not compared and names the slot', () => {
    const why = missingVectorExplanation(objs);
    expect(why).toContain('2 object(s)');
    expect(why).toContain('qwen3_embed');
    expect(why).toContain('could not compare');
  });
  it('no objects → no explanation', () => {
    expect(missingVectorExplanation([])).toBe('');
  });
  it('names the slots each object DOES carry', () => {
    expect(hasSlotsText(objs[0])).toBe('has: arctic2_embed');
    expect(hasSlotsText(objs[1])).toBe('has no vector at all');
  });
});

describe('TS payload types mirror the Rust structs', () => {
  const rustSrc = sourceFile(
    'orchestrator_core.rs',
    readFileSync(resolve(here, '../../../src-tauri/src/commands/orchestrator_core.rs'), 'utf8'),
    'rust',
  ).text;
  const tsSrc = readFileSync(resolve(here, 'kg-embed-status.ts'), 'utf8');
  it.each(['MissingVectorObject', 'DuplicateScanResult'])('%s field sets are identical', (name) => {
    const rust = rustStructFields(rustSrc, name);
    const ts = tsInterfaceFields(tsSrc, name);
    expect(rust.length).toBeGreaterThan(0);
    expect([...ts].sort()).toEqual([...rust].sort());
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

  it('the button runs scan(), and scan() runs the duplicate scan', () => {
    expect(calls(fn('scan'), 'runDuplicateScan').length).toBe(1);
    const buttons = [...walk(fragment)].filter((n) => n.type === 'RegularElement' && n.name === 'button');
    expect(buttons.length).toBe(1);
    const onclick = (buttons[0].attributes as Node[]).find((a) => a.name === 'onclick');
    expect(onclick, 'button has no onclick').toBeTruthy();
    expect(calls(onclick, 'scan').length).toBe(1);
  });

  it('the skip list renders from objects_missing_vector, with the explanation', () => {
    const eachOverMissing = [...walk(fragment)].filter((n) => {
      if (n.type !== 'EachBlock') return false;
      const e = n.expression as Node;
      return e?.type === 'MemberExpression' && isIdent(e.property, 'objects_missing_vector');
    });
    expect(eachOverMissing.length).toBe(1);
    expect(calls(fragment, 'missingVectorExplanation').length).toBe(1);
    expect(calls(fragment, 'hasSlotsText').length).toBe(1);
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
