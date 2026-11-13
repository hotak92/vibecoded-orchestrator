// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106 (owner ruling 2026-11-11) — "the hub manages unloading": the
// Preferences field and the Services → Ollama models card.
//
// Three layers:
//   1. the helpers DRIVE the real commands (injected `invoke`) and phrase the
//      answers;
//   2. the TS mirrors carry exactly the Rust structs' fields;
//   3. AST wiring: the components call the helpers, and the pages mount them.
// WHAT IT DOES NOT PROVE: real-DOM click delivery (no DOM harness here), the
// Rust validation (`ollama_lifetime_prefs.rs` tests) or the hub's unloading
// (`vct-hub/src/ollama_lifetime.rs` tests, against a fake Ollama).

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { isIdent, rustStructFields, tsInterfaceFields, walk } from '../test-support/wiring-ast';
import { loadRust, sourceFile } from '../test-support/source-census';
import {
  CUSTOM_PRESET,
  IDLE_PRESETS,
  envOverrideNote,
  holderLine,
  humanDuration,
  idleSummary,
  loadOllamaIdle,
  loadOllamaModels,
  memoryLine,
  presetFor,
  saveOllamaIdle,
  unloadOllamaModel,
  type OllamaIdleState,
  type OllamaModelView,
} from './ollama_lifetime';

const here = dirname(fileURLToPath(import.meta.url));
type Node = Record<string, unknown>;

function recordingInvoke(result: unknown = {}) {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  const invoke = async <T>(cmd: string, args?: Record<string, unknown>): Promise<T> => {
    calls.push([cmd, args]);
    return result as T;
  };
  return { calls, invoke };
}

const unset: OllamaIdleState = {
  stored: null,
  effective: '10m',
  effective_secs: 600,
  stored_invalid: false,
  default_idle: '10m',
  max_idle: '30m',
  caller_keep_alive: '45m',
  env_override: null,
};

describe('Preferences field: helpers drive the real commands', () => {
  it('load invokes get_ollama_idle_unload', async () => {
    const d = recordingInvoke(unset);
    expect(await loadOllamaIdle(d)).toEqual(unset);
    expect(d.calls).toEqual([['get_ollama_idle_unload', undefined]]);
  });

  it('save sends the trimmed value; empty or null restores the default', async () => {
    const d = recordingInvoke(unset);
    await saveOllamaIdle(d, ' 2m ');
    await saveOllamaIdle(d, '');
    await saveOllamaIdle(d, null);
    expect(d.calls).toEqual([
      ['set_ollama_idle_unload', { value: '2m' }],
      ['set_ollama_idle_unload', { value: null }],
      ['set_ollama_idle_unload', { value: null }],
    ]);
  });

  it('a backend refusal reaches the caller (the field shows it)', async () => {
    const invoke = async <T>(): Promise<T> => {
      throw new Error("'31m' is longer than the 30m the hub accepts");
    };
    await expect(saveOllamaIdle({ invoke }, '31m')).rejects.toThrow('30m');
  });
});

describe('Preferences field: presets and sentences', () => {
  it('offers the default, as-soon-as-idle and the hub maximum, and no "never"', () => {
    const values = IDLE_PRESETS.map((p) => p.value);
    expect(values).toEqual(['0', '2m', '5m', '10m', '30m', CUSTOM_PRESET]);
    expect(IDLE_PRESETS.find((p) => p.value === '10m')!.label).toContain('default');
    expect(IDLE_PRESETS.find((p) => p.value === '0')!.label).toContain('slow');
    expect(values).not.toContain('-1');
  });

  it('presetFor: unset → default preset; a stored preset → itself; anything else → custom', () => {
    expect(presetFor(unset)).toBe('10m');
    expect(presetFor({ ...unset, stored: '2m', effective: '2m', effective_secs: 120 })).toBe('2m');
    expect(presetFor({ ...unset, stored: '15m', effective: '15m', effective_secs: 900 })).toBe(CUSTOM_PRESET);
    expect(presetFor({ ...unset, stored: 'never', stored_invalid: true })).toBe('10m');
  });

  it('the summary says what is in force and where it came from', () => {
    expect(idleSummary(unset)).toContain('after 10 min');
    expect(idleSummary(unset)).toContain('(default)');
    expect(idleSummary({ ...unset, stored: '0', effective: '0', effective_secs: 0 })).toContain('as soon as');
    expect(idleSummary({ ...unset, stored: 'never', stored_invalid: true })).toContain('not a duration');
    expect(humanDuration('1m30s')).toBe('1 min 30 s');
  });

  it("the launcher's own VCO_OLLAMA_KEEP_ALIVE is named, with what it does", () => {
    expect(envOverrideNote(unset)).toBeNull();
    const note = envOverrideNote({ ...unset, env_override: '-1' })!;
    expect(note).toContain('VCO_OLLAMA_KEEP_ALIVE=-1');
    expect(note).toContain('leaves the models they load');
    expect(envOverrideNote({ ...unset, env_override: '' })).toContain('no keep_alive');
  });
});

describe('Services card: helpers and sentences', () => {
  const model: OllamaModelView = {
    name: 'snowflake-arctic-embed2:latest',
    size: 1_209_536_512,
    size_vram: 0,
    holder: 'hub',
    ollama_expires_at: '2026-11-11T15:33:32Z',
    last_use: '2026-11-11T15:00:00Z',
    unload_at: '2026-11-11T15:10:00Z',
    unload_requested: false,
  };

  it('load and unload go to the hub commands', async () => {
    const d = recordingInvoke({ models: [] });
    await loadOllamaModels(d);
    await unloadOllamaModel(d, 'arctic');
    expect(d.calls).toEqual([
      ['ollama_models_status', undefined],
      ['ollama_unload_model', { model: 'arctic' }],
    ]);
  });

  it('memory: RAM, VRAM or split', () => {
    expect(memoryLine(model)).toBe('1.1 GB RAM');
    expect(memoryLine({ ...model, size_vram: model.size })).toBe('1.1 GB VRAM');
    expect(memoryLine({ ...model, size: 3 * 1024 ** 3, size_vram: 1024 ** 3 })).toBe(
      '3.0 GB (1.0 GB VRAM + 2.0 GB RAM)',
    );
  });

  it('who decides when it goes', () => {
    const now = new Date('2026-11-11T15:05:00Z');
    expect(holderLine(model, now)).toContain('The hub unloads it');
    expect(holderLine(model, now)).toContain('in 5 min');
    expect(holderLine({ ...model, holder: 'busy' }, now)).toContain('request is running');
    const caller = holderLine({ ...model, holder: 'caller' }, now);
    expect(caller).toContain('VCO_OLLAMA_KEEP_ALIVE');
    expect(caller).toContain('leaves it alone');
    expect(holderLine({ ...model, holder: 'caller', ollama_expires_at: '2318-02-23T00:00:00Z' }, now)).toContain(
      'never on its own',
    );
  });
});

describe('TS mirrors carry the Rust fields', () => {
  const RUST = loadRust();
  const prefs = RUST.find((f) => f.rel === 'src/commands/ollama_lifetime_prefs.rs')!;
  const hubPath = resolve(here, '../../../src-tauri/vct-hub/src/ollama_lifetime.rs');
  const hub = sourceFile('vct-hub/src/ollama_lifetime.rs', readFileSync(hubPath, 'utf8'), 'rust');
  const ts = sourceFile('ollama_lifetime.ts', readFileSync(resolve(here, 'ollama_lifetime.ts'), 'utf8'), 'ts');

  it.each([
    [prefs, 'OllamaIdleState', 'OllamaIdleState'],
    [hub, 'ModelView', 'OllamaModelView'],
    [hub, 'ModelsStatus', 'OllamaModelsStatus'],
    [hub, 'UnloadEvent', 'OllamaUnloadEvent'],
  ])('%#: %s', (src, rust, iface) => {
    expect(tsInterfaceFields(ts.text, iface as string).sort()).toEqual(
      rustStructFields((src as { text: string }).text, rust as string).sort(),
    );
  });
});

describe('component + page wiring (AST)', () => {
  const load = (rel: string) =>
    parse(readFileSync(resolve(here, rel), 'utf8'), { modern: true }) as unknown as Node;
  const calls = (root: unknown, name: string) =>
    [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));
  const mounted = (page: Node, name: string) =>
    [...walk(page.fragment)].filter(
      (n) => (n.type === 'Component' || n.type === 'InlineComponent') && n.name === name,
    );

  it('the field loads on mount and saves through the helper', () => {
    const ast = load('../preferences/OllamaIdleUnload.svelte');
    const instance = (ast.instance as Node).content as Node;
    const mounts = calls(instance, 'onMount');
    expect(mounts.length).toBe(1);
    expect(calls(mounts[0], 'loadOllamaIdle').length).toBe(1);
    expect(calls(instance, 'saveOllamaIdle').length).toBe(1);
    // The dropdown offers the presets; "Use the default" saves null.
    expect(mounted(ast, 'Dropdown').length).toBe(1);
    const fragmentCalls = calls(ast.fragment, 'save');
    expect(fragmentCalls.some((c) => (c.arguments as Node[])[0]?.type === 'Literal')).toBe(true);
  });

  it('the card loads, polls and unloads through the helpers', () => {
    const ast = load('../components/OllamaModelsCard.svelte');
    const instance = (ast.instance as Node).content as Node;
    expect(calls(instance, 'loadOllamaModels').length).toBe(1);
    expect(calls(instance, 'unloadOllamaModel').length).toBe(1);
    expect(calls(instance, 'pollWhileActive').length).toBe(1);
    expect(calls(ast.fragment, 'unload').length).toBe(1);
    expect(calls(ast.fragment, 'holderLine').length).toBe(1);
    expect(calls(ast.fragment, 'memoryLine').length).toBe(1);
  });

  it('Preferences mounts the field and Services mounts the card', () => {
    expect(mounted(load('../../routes/preferences/+page.svelte'), 'OllamaIdleUnload').length).toBe(1);
    expect(mounted(load('../../routes/services/+page.svelte'), 'OllamaModelsCard').length).toBe(1);
  });
});
