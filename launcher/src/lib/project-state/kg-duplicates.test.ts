// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-J — the duplicate scan on the project's KG tab, and the
// launcher bell's "may need a re-scan" alert (owner 2026-10-11).
//
// Layers:
//   1. the helpers DRIVE the real invokes (injected `invoke`) and phrase the
//      "objects missing the vector" why;
//   2. the TS payload types mirror the Rust structs field-for-field, so a
//      field the scanner emits cannot silently vanish between Rust and the
//      GUI (W6-H's `objects_missing_vector` was dropped by the Rust payload
//      before W6-I);
//   3. the alert: candidacy from the status payload (fill running/owed, or a
//      never-recorded collection — never a refused-OpenAI consent state),
//      the entry's message + per-project scan actions, and the store sync
//      (upsert while candidates exist, clear when none do, never invent one
//      on a failed status read);
//   4. AST wiring: the KG tab's button runs the scan and renders the skip
//      list; the bell renders the alert's actions and runs them; the layout
//      schedules the startup check.
// WHAT IT DOES NOT PROVE: real-DOM click delivery (no DOM harness in this
// suite) and the scanner itself (tests/test_v0294_kg_duplicates_target_vector.py).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { sourceFile } from '../test-support/source-census';
import { isIdent, rustStructFields, tsInterfaceFields, walk } from '../test-support/wiring-ast';
import {
  duplicateScanSummary,
  hasSlotsText,
  missingVectorExplanation,
  runDuplicateScan,
  type MissingVectorObject,
} from './kg-duplicates';
import {
  findRescanCandidates,
  rescanAlertEntry,
  syncKgRescanAlert,
  KG_RESCAN_ALERT_KEY,
} from '../kg-rescan-alerts';
import { get } from 'svelte/store';
import { errorCount, notifications } from '../stores/notifications';
import type { KgEmbedCollection, KgEmbedStatusPayload } from '../preferences/kg-embed-status';

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

const coll = (over: Partial<KgEmbedCollection>): KgEmbedCollection => ({
  collection: 'Acme_KnowledgeGraph',
  recorded_model: 'qwen3-embedding:0.6b',
  objects: null,
  target_model: 'qwen3-embedding:0.6b',
  filling: false,
  filled: null,
  total: null,
  complete: true,
  cost_bearing: false,
  target_refused: false,
  ...over,
});

const payloadWith = (collections: KgEmbedCollection[]): KgEmbedStatusPayload => ({
  target: 'qwen3-embedding:0.6b',
  projects: [{ project_id: 'p1', name: 'Acme', folder: '/acme', target_model: 'qwen3-embedding:0.6b', collections }],
});

describe('the duplicate scan helper', () => {
  it('runDuplicateScan invokes kg_check_duplicates with the project id', async () => {
    const d = recordingInvoke({ threshold: 0.95, pairs: [], nodes_skipped: 0, objects_missing_vector: [] });
    await runDuplicateScan('p-1', d);
    expect(d.calls).toEqual([['kg_check_duplicates', { projectId: 'p-1' }]]);
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
  it('the bell summary carries the verdict (pairs + objects not compared)', () => {
    const s = duplicateScanSummary('Scan Acme', {
      threshold: 0.95, pairs: [{}, {}], nodes_skipped: 0, objects_missing_vector: objs,
    });
    expect(s).toContain('Scan Acme: 2 likely duplicate pair(s) at threshold 0.95');
    expect(s).toContain('2 object(s) lack the searched vector');
    const clean = duplicateScanSummary('Scan Acme', { threshold: 0.95, pairs: [], nodes_skipped: 0, objects_missing_vector: [] });
    expect(clean).not.toContain('lack the searched vector');
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
  const tsSrc = readFileSync(resolve(here, 'kg-duplicates.ts'), 'utf8');
  it.each(['MissingVectorObject', 'DuplicateScanResult'])('%s field sets are identical', (name) => {
    const rust = rustStructFields(rustSrc, name);
    const ts = tsInterfaceFields(tsSrc, name);
    expect(rust.length).toBeGreaterThan(0);
    expect([...ts].sort()).toEqual([...rust].sort());
  });
});

describe('the re-scan candidacy (signals that already exist)', () => {
  it('a collection whose fill is owed/running qualifies', () => {
    const c = findRescanCandidates(payloadWith([coll({ filling: true, complete: false })]));
    expect(c).toEqual([{ projectId: 'p1', name: 'Acme' }]);
  });
  it('a never-recorded collection that HOLDS objects qualifies', () => {
    const c = findRescanCandidates(payloadWith([coll({ recorded_model: null, objects: 12, complete: false })]));
    expect(c).toHaveLength(1);
  });
  it('an EMPTY/absent unrecorded collection never qualifies (it would alert forever)', () => {
    expect(findRescanCandidates(payloadWith([coll({ recorded_model: null, objects: 0, complete: false })]))).toEqual([]);
    // not counted (Weaviate unreachable) → no verdict, no invented alert
    expect(findRescanCandidates(payloadWith([coll({ recorded_model: null, objects: null, complete: false })]))).toEqual([]);
  });
  it('a steady collection does not qualify', () => {
    expect(findRescanCandidates(payloadWith([coll({})]))).toEqual([]);
  });
  it('an OpenAI-REFUSED target is a consent state, not a scan deficiency', () => {
    const c = findRescanCandidates(payloadWith([coll({ recorded_model: null, objects: 12, complete: false, target_refused: true })]));
    expect(c).toEqual([]);
  });
  it('a project with no id is skipped (the scan button needs it)', () => {
    const p = payloadWith([coll({ filling: true })]);
    p.projects[0].project_id = null;
    expect(findRescanCandidates(p)).toEqual([]);
  });
});

describe('the alert entry and the store sync', () => {
  it('one action per candidate, running kg_check_duplicates for that project', () => {
    const entry = rescanAlertEntry([
      { projectId: 'p1', name: 'Acme' },
      { projectId: 'p2', name: 'Beta' },
    ]);
    expect(entry.key).toBe(KG_RESCAN_ALERT_KEY);
    expect(entry.message).toContain('2 projects');
    expect(entry.actions).toEqual([
      { label: 'Scan Acme', command: 'kg_check_duplicates', args: { projectId: 'p1' } },
      { label: 'Scan Beta', command: 'kg_check_duplicates', args: { projectId: 'p2' } },
    ]);
  });

  it('sync upserts the alert while a project qualifies and clears it when none does', async () => {
    const upserts: unknown[] = [];
    const resolves: string[] = [];
    const store = {
      upsertAlert: (e: unknown, _now: number) => upserts.push(e),
      resolve: (k: string) => resolves.push(k),
    };
    const filling = recordingInvoke(payloadWith([coll({ filling: true })]));
    await syncKgRescanAlert({ invoke: filling.invoke, store, now: () => 42 });
    expect(upserts).toHaveLength(1);
    expect(resolves).toEqual([]);
    expect((upserts[0] as { actions: unknown[] }).actions).toHaveLength(1);

    const steady = recordingInvoke(payloadWith([coll({})]));
    await syncKgRescanAlert({ invoke: steady.invoke, store });
    expect(upserts).toHaveLength(1);
    expect(resolves).toEqual([KG_RESCAN_ALERT_KEY]);
  });

  it('a failed status read never invents nor clears an alert', async () => {
    const upserts: unknown[] = [];
    const resolves: string[] = [];
    const store = {
      upsertAlert: (e: unknown, _now: number) => upserts.push(e),
      resolve: (k: string) => resolves.push(k),
    };
    const invoke = async () => {
      throw new Error('status unavailable');
    };
    await syncKgRescanAlert({ invoke, store });
    expect(upserts).toEqual([]);
    expect(resolves).toEqual([]);
  });
});

describe('the notifications store carries info alerts', () => {
  it('upsertAlert replaces the entry under its key (no ×N count), resolve clears it', () => {
    const now = 1000;
    notifications.upsertAlert(
      { key: 'test-alert', message: 'one project', actions: [{ label: 'Scan Acme', command: 'kg_check_duplicates', args: { projectId: 'p1' } }] },
      now,
    );
    notifications.upsertAlert(
      { key: 'test-alert', message: 'two projects', actions: [
        { label: 'Scan Acme', command: 'kg_check_duplicates', args: { projectId: 'p1' } },
        { label: 'Scan Beta', command: 'kg_check_duplicates', args: { projectId: 'p2' } },
      ] },
      now + 10,
    );
    // an info alert is not an error: the error badge count ignores it
    expect(get(errorCount)).toBe(0);
    // read the store through a subscription (no peek API)
    let seen: import('../stores/notifications').ErrorNotification[] = [];
    const unsub = notifications.subscribe((list) => (seen = list));
    let entry = seen.find((n) => n.key === 'test-alert');
    expect(entry?.message).toBe('two projects');
    expect(entry?.count).toBe(1);
    expect(entry?.tone).toBe('info');
    expect(entry?.actions).toHaveLength(2);

    // one action done → only that action leaves
    notifications.dropAction(entry!.id, 'Scan Acme');
    entry = seen.find((n) => n.key === 'test-alert');
    expect(entry?.actions?.map((a) => a.label)).toEqual(['Scan Beta']);

    // the last action done → the alert is gone
    notifications.dropAction(entry!.id, 'Scan Beta');
    expect(seen.find((n) => n.key === 'test-alert')).toBeUndefined();

    // resolve() clears an existing alert
    notifications.upsertAlert({ key: 'test-alert', message: 'again' }, now + 20);
    notifications.resolve('test-alert');
    expect(seen.find((n) => n.key === 'test-alert')).toBeUndefined();
    unsub();
  });
});

describe('component + layout wiring (AST)', () => {
  const calls = (root: unknown, name: string) =>
    [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));

  it('the KG tab: the button runs the scan, the skip list renders with its explanation', () => {
    const src = readFileSync(resolve(here, 'KgCodegraphTab.svelte'), 'utf8');
    const ast = parse(src, { modern: true }) as unknown as Node;
    const instance = (ast.instance as Node).content as Node;
    const fragment = ast.fragment as Node;

    let scanFn: Node | null = null;
    for (const n of walk(instance)) {
      if (n.type === 'FunctionDeclaration' && isIdent(n.id, 'scanKgDuplicates')) scanFn = n;
    }
    expect(scanFn, 'no scanKgDuplicates()').toBeTruthy();
    expect(calls(scanFn, 'runDuplicateScan').length).toBe(1);

    const buttons = [...walk(fragment)].filter(
      (n) => n.type === 'RegularElement' && n.name === 'button'
        && JSON.stringify(n.attributes).includes('kg-check-duplicates'),
    );
    expect(buttons.length).toBe(1);
    expect(calls(buttons[0], 'scanKgDuplicates').length).toBe(1);

    const eachOverMissing = [...walk(fragment)].filter((n) => {
      if (n.type !== 'EachBlock') return false;
      const e = n.expression as Node;
      return e?.type === 'MemberExpression' && isIdent(e.property, 'objects_missing_vector');
    });
    expect(eachOverMissing.length).toBe(1);
    expect(calls(fragment, 'missingVectorExplanation').length).toBe(1);
    expect(calls(fragment, 'hasSlotsText').length).toBe(1);
  });

  it('W6-J: the KG tab no longer offers a per-project KG model pick', () => {
    const src = readFileSync(resolve(here, 'KgCodegraphTab.svelte'), 'utf8');
    expect(src).not.toContain('embedding_model:');
    expect(src).not.toContain("ariaLabel=\"KG embedding model\"");
    // and the save no longer sends a model
    const saveKg = src.slice(src.indexOf('async function saveKg()'), src.indexOf('async function saveCg()'));
    expect(saveKg).toContain("'set_project_kg_binding'");
    expect(saveKg).not.toContain('embedding_model');
    expect(saveKg).not.toContain('enrichmentTarget');
  });

  it('the bell renders the alert actions and runs them', () => {
    const src = readFileSync(resolve(here, '../components/NotificationBell.svelte'), 'utf8');
    const ast = parse(src, { modern: true }) as unknown as Node;
    const instance = (ast.instance as Node).content as Node;
    const fragment = ast.fragment as Node;

    let runFn: Node | null = null;
    for (const n of walk(instance)) {
      if (n.type === 'FunctionDeclaration' && isIdent(n.id, 'runAction')) runFn = n;
    }
    expect(runFn, 'no runAction()').toBeTruthy();
    expect(calls(runFn, 'invoke').length).toBe(1);
    // the toast carries the scan verdict (the result is kept nowhere else)
    expect(calls(runFn, 'duplicateScanSummary').length).toBe(1);
    // `notifications.dropAction(...)` — a member call, matched by property name.
    const dropCalls = [...walk(runFn)].filter(
      (n) =>
        n.type === 'CallExpression' &&
        (n.callee as Node)?.type === 'MemberExpression' &&
        isIdent((n.callee as Node).property, 'dropAction'),
    );
    expect(dropCalls.length).toBe(1);

    const actionButtons = [...walk(fragment)].filter(
      (n) => n.type === 'RegularElement' && n.name === 'button'
        && JSON.stringify(n.attributes).includes('bell-action'),
    );
    expect(actionButtons.length).toBe(1);
    expect(calls(actionButtons[0], 'runAction').length).toBe(1);
  });

  it('the enrichment progress event keeps a listener on its remaining caller', () => {
    // The retired EnrichmentProgressModal was the only listener for
    // `vct-enrichment-progress`; the model-switch "Regenerate now" is the
    // remaining `enrich_collection_vectors` caller, so it listens and renders.
    const src = readFileSync(resolve(here, '../components/RegenerateOrDeferModal.svelte'), 'utf8');
    const ast = parse(src, { modern: true }) as unknown as Node;
    const instance = (ast.instance as Node).content as Node;
    let regen: Node | null = null;
    for (const n of walk(instance)) {
      if (n.type === 'FunctionDeclaration' && isIdent(n.id, 'regenerateModelSwitch')) regen = n;
    }
    expect(regen, 'no regenerateModelSwitch()').toBeTruthy();
    const listens = calls(regen, 'listen').filter(
      (c) => ((c.arguments as Node[])[0] as Node)?.value === 'vct-enrichment-progress',
    );
    expect(listens.length).toBe(1);
    expect(JSON.stringify(ast.fragment)).toContain('rgd-regen-progress');
  });

  it('the layout schedules the re-scan check at startup and cancels it on teardown', () => {
    const src = readFileSync(resolve(here, '../../routes/+layout.svelte'), 'utf8');
    expect(src).toContain('scheduleStartupCheck(() =>');
    expect(src).toContain('syncKgRescanAlert({ invoke, store: notifications })');
    expect(src).toContain('cancelKgRescanCheck();');
  });
});
