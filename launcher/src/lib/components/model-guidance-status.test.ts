// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.103 (plan item 12) — the model-guidance status surface.
//
// Three layers:
//   1. the helper DRIVES the real invoke (injected `invoke`);
//   2. the phrasing helpers (vendor rows, the last-refresh line);
//   3. AST wiring: the component loads the status on mount, and the
//      Secrets preferences page mounts it.
// WHAT IT DOES NOT PROVE: real-DOM rendering (no DOM harness in this suite)
// and the Python answers (tests/test_v02103_model_guidance.py).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from '../test-support/wiring-ast';
import {
  formatWhen,
  lastRefreshText,
  loadModelGuidanceStatus,
  vendorRows,
  vendorStateText,
  type ModelGuidanceStatusPayload,
} from './model-guidance-status';

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

const payload: ModelGuidanceStatusPayload = {
  vendors: {
    zai: { state: 'present', last_known: true, display: 'z.ai (GLM)' },
    qwen: { state: 'unavailable', last_known: true, display: 'QwenCloud (Qwen/DeepSeek/GLM)' },
    kimi: { state: 'absent', last_known: false, display: 'Kimi' },
  },
  last_refresh: {
    at: '2026-10-14T10:05:00+00:00',
    trigger: 'secrets',
    added: ['kimi'],
    removed: [],
    projects_changed: 2,
    root_changed: false,
  },
  recorded_at: '2026-10-14T10:05:00+00:00',
  registered_projects: 2,
};

describe('helpers drive the real command', () => {
  it('loadModelGuidanceStatus invokes get_model_guidance_status', async () => {
    const d = recordingInvoke(payload);
    const out = await loadModelGuidanceStatus(d);
    expect(d.calls).toEqual([['get_model_guidance_status', undefined]]);
    expect(out.vendors.zai.state).toBe('present');
  });
});

describe('vendor rows', () => {
  it('sorted by display name for a stable render', () => {
    expect(vendorRows(payload).map((v) => v.id)).toEqual(['kimi', 'qwen', 'zai']);
  });
  it('present → detected chip', () => {
    const s = vendorStateText(payload.vendors.zai);
    expect(s.tone).toBe('ok');
    expect(s.label).toBe('detected');
  });
  it('absent → no-key chip', () => {
    const s = vendorStateText(payload.vendors.kimi);
    expect(s.tone).toBe('off');
    expect(s.label).toBe('no key');
  });
  it('unavailable with a last-known key → the rows are kept, said out loud', () => {
    const s = vendorStateText(payload.vendors.qwen);
    expect(s.tone).toBe('warn');
    expect(s.text).toContain('keeps the last-known rows');
  });
  it('unavailable with no record → nothing changed, no false promise', () => {
    const s = vendorStateText({ state: 'unavailable', last_known: null, display: 'x' });
    expect(s.text).toContain('nothing was changed');
    expect(s.text).not.toContain('last-known rows');
  });
  it('unknown (no gateway) reads as not installed', () => {
    const s = vendorStateText({ state: 'unknown', last_known: null, display: 'x' });
    expect(s.tone).toBe('off');
    expect(s.text).toContain('not installed');
  });
});

describe('last refresh line', () => {
  it('never refreshed → names the three automatic triggers', () => {
    const t = lastRefreshText(null);
    expect(t).toContain('not been refreshed yet');
    expect(t).toContain('launcher startup');
  });
  it('stamped with a diff → when, trigger, what changed, how many projects', () => {
    const t = lastRefreshText(payload.last_refresh);
    expect(t).toContain('2026-10-14 10:05');
    expect(t).toContain('(secrets)');
    expect(t).toContain('kimi rows added');
    expect(t).toContain('2 project(s) re-rendered');
  });
  it('a no-change refresh says nothing was rewritten', () => {
    const t = lastRefreshText({
      at: '2026-10-14T09:00:00+00:00',
      trigger: 'startup',
      added: [],
      removed: [],
      projects_changed: 0,
      root_changed: false,
    });
    expect(t).toContain('already current');
  });
  it('formatWhen shortens an ISO stamp and passes other text through', () => {
    expect(formatWhen('2026-10-14T10:05:00+00:00')).toBe('2026-10-14 10:05');
    expect(formatWhen('weird')).toBe('weird');
  });
});

describe('component + page wiring (AST)', () => {
  const src = readFileSync(resolve(here, 'ModelGuidanceStatus.svelte'), 'utf8');
  const ast = parse(src, { modern: true }) as unknown as Node;
  const instance = (ast.instance as Node).content as Node;

  function fn(name: string): Node {
    for (const n of walk(instance)) if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
    throw new Error(`no ${name}()`);
  }
  const calls = (root: unknown, name: string) =>
    [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));

  it('refresh() loads the status and onMount runs refresh()', () => {
    expect(calls(fn('refresh'), 'loadModelGuidanceStatus').length).toBe(1);
    const mounts = calls(instance, 'onMount');
    expect(mounts.length).toBe(1);
    expect(calls(mounts[0], 'refresh').length).toBe(1);
  });

  it('the Secrets preferences page mounts <ModelGuidanceStatus />', () => {
    const page = parse(
      readFileSync(resolve(here, '../../routes/preferences/secrets/+page.svelte'), 'utf8'),
      { modern: true },
    ) as unknown as Node;
    const mounted = [...walk(page.fragment)].filter(
      (n) => (n.type === 'Component' || n.type === 'InlineComponent') && n.name === 'ModelGuidanceStatus',
    );
    expect(mounted.length).toBe(1);
  });
});
