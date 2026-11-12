// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.106 — Preferences → KG Summaries → "Local summary model": the panel's
// decisions (local_summary_model.ts) and its wiring (the component calls
// them, the page mounts it inside the lazy KG Summaries section).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import {
  APP_STATE_KEY_LOCAL_SUMMARY_MODEL,
  LOCAL_SUMMARY_ENV,
  canDownload,
  canRemove,
  downloadOutcomeText,
  envOverrideNote,
  presenceLine,
  removeLabel,
  removeLocalSummaryModel,
  removeUnavailableNote,
  saveLocalSummaryEnabled,
  toggleDescription,
  type LocalSummaryStatus,
} from './local_summary_model';

const here = dirname(fileURLToPath(import.meta.url));
const GB = 1024 ** 3;

function st(over: Partial<LocalSummaryStatus> = {}): LocalSummaryStatus {
  return {
    ok: true,
    enabled: true,
    source: 'default',
    invalid_env: null,
    env_var: 'VCT_LOCAL_SUMMARY_MODEL',
    model: 'qwen3.5:9b',
    model_source: 'install-record',
    present: true,
    size_bytes: Math.round(6.1 * GB),
    approx_size_bytes: Math.round(6.1 * GB),
    ollama_url: 'http://localhost:11435',
    vco_owns_ollama: true,
    owner_detail: "VCO's own container vco_ollama",
    ...over,
  };
}

function recorder(confirmAnswer = true) {
  const calls: Array<{ cmd: string; args?: Record<string, unknown> }> = [];
  const confirms: string[] = [];
  return {
    calls,
    confirms,
    deps: {
      invoke: async <T>(cmd: string, args?: Record<string, unknown>): Promise<T> => {
        calls.push({ cmd, args });
        return { ok: true } as T;
      },
      confirm: (m: string) => {
        confirms.push(m);
        return confirmAnswer;
      },
    },
  };
}

describe('the setting', () => {
  it('names the same app_state key and env var as the Python home', () => {
    const py = readFileSync(resolve(here, '../../../../vco_lib/local_summary_model.py'), 'utf-8');
    expect(py).toContain(`APP_STATE_KEY = "${APP_STATE_KEY_LOCAL_SUMMARY_MODEL}"`);
    expect(py).toContain(`ENV_SWITCH = "${LOCAL_SUMMARY_ENV}"`);
  });

  it('the toggle writes the bool through app_state_set_bool, both ways', async () => {
    for (const value of [false, true]) {
      const r = recorder();
      await saveLocalSummaryEnabled(r.deps, value);
      expect(r.calls).toEqual([
        { cmd: 'app_state_set_bool', args: { key: APP_STATE_KEY_LOCAL_SUMMARY_MODEL, value } },
      ]);
    }
  });

  it("describes the download in the owner's words with the model and its size", () => {
    expect(toggleDescription(st())).toBe(
      "Downloads qwen3.5:9b (~6.1 GB) as a fallback when Claude can't summarise. " +
        'Turn off to save disk; summaries then use Claude/cloud only.',
    );
    // Not installed: the tier table's nominal size is shown.
    expect(toggleDescription(st({ present: false, size_bytes: null, approx_size_bytes: 1.2 * GB }))).toContain(
      '(~1.2 GB)',
    );
  });

  it('says when the env override wins over the toggle, and when it is ignored', () => {
    expect(envOverrideNote(st())).toBeNull();
    expect(envOverrideNote(st({ source: 'env', enabled: false }))).toContain('VCT_LOCAL_SUMMARY_MODEL=off');
    expect(envOverrideNote(st({ invalid_env: 'maybe' }))).toContain('ignored');
  });
});

describe('remove (guarded by switch OFF + ownership + confirm)', () => {
  const off = st({ enabled: false });

  it('is offered only when OFF, downloaded, and VCO owns the Ollama', () => {
    expect(canRemove(off)).toBe(true);
    expect(canRemove(st())).toBe(false); // ON: the next update would re-download
    expect(canRemove(st({ enabled: false, present: false }))).toBe(false);
    expect(canRemove(st({ enabled: false, present: null }))).toBe(false);
    expect(canRemove(st({ enabled: false, vco_owns_ollama: false }))).toBe(false);
    expect(canRemove(st({ enabled: false, vco_owns_ollama: null }))).toBe(false);
    expect(removeLabel(off)).toBe('Remove the downloaded model (frees ~6.1 GB)');
  });

  it('a foreign Ollama gets an explanation instead of a button', () => {
    const foreign = st({ enabled: false, vco_owns_ollama: false, owner_detail: 'ollama is adopted' });
    expect(removeUnavailableNote(foreign)).toContain('ollama rm qwen3.5:9b');
    expect(removeUnavailableNote(off)).toBeNull();
  });

  it('invokes the removal only after the user confirms', async () => {
    const yes = recorder(true);
    await removeLocalSummaryModel(yes.deps, off);
    expect(yes.confirms).toHaveLength(1);
    expect(yes.confirms[0]).toContain('qwen3.5:9b');
    expect(yes.calls.map((c) => c.cmd)).toEqual(['remove_local_summary_model']);

    const no = recorder(false);
    expect(await removeLocalSummaryModel(no.deps, off)).toBeNull();
    expect(no.calls).toEqual([]);
  });

  it('never asks or invokes when removal is not allowed', async () => {
    for (const bad of [st(), st({ enabled: false, vco_owns_ollama: false })]) {
      const r = recorder(true);
      expect(await removeLocalSummaryModel(r.deps, bad)).toBeNull();
      expect(r.confirms).toEqual([]);
      expect(r.calls).toEqual([]);
    }
  });
});

describe('presence + download', () => {
  it('offers Download now only when ON and Ollama lacks the model', () => {
    expect(canDownload(st({ present: false }))).toBe(true);
    expect(canDownload(st())).toBe(false);
    expect(canDownload(st({ present: null }))).toBe(false);
    expect(canDownload(st({ enabled: false, present: false }))).toBe(false);
  });

  it('presence lines', () => {
    expect(presenceLine(st())).toBe('qwen3.5:9b is downloaded (~6.1 GB).');
    expect(presenceLine(st({ present: false }))).toContain('next install/update downloads it');
    expect(presenceLine(st({ enabled: false, present: false }))).toBe('qwen3.5:9b is not downloaded.');
    expect(presenceLine(st({ present: null }))).toContain('did not answer');
  });

  it('reports the finished download for THIS model, never ok for a failure', () => {
    const base = { ok: true, pulled: [], present: [], failed: {}, error: null };
    expect(downloadOutcomeText({ ...base, pulled: ['qwen3.5:9b'] }, 'qwen3.5:9b').ok).toBe(true);
    expect(downloadOutcomeText({ ...base, ok: false, failed: { 'qwen3.5:9b': 'disk full' } }, 'qwen3.5:9b')).toEqual({
      ok: false,
      text: 'Could not download qwen3.5:9b: disk full.',
    });
    expect(downloadOutcomeText({ ...base, ok: false, error: 'boom' }, 'qwen3.5:9b').ok).toBe(false);
    expect(downloadOutcomeText(base, 'qwen3.5:9b').ok).toBe(false);
  });
});

describe('wiring', () => {
  const component = readFileSync(resolve(here, '../preferences/LocalSummaryModel.svelte'), 'utf-8');
  const page = readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf-8');
  const lib = readFileSync(resolve(here, '../../../src-tauri/src/lib.rs'), 'utf-8');

  it('the component drives every action through the tested functions', () => {
    for (const fn of [
      'loadLocalSummaryStatus({ invoke })',
      'saveLocalSummaryEnabled({ invoke }, next)',
      'removeLocalSummaryModel({ invoke, confirm',
      'downloadLocalSummaryModel({ invoke })',
      'listen<ModelEnsureEvent>(MODEL_ENSURE_EVENT',
    ]) {
      expect(component).toContain(fn);
    }
  });

  it('the page mounts it inside the lazy KG Summaries section, after it loaded', () => {
    const section = page.slice(page.indexOf('id="pr-kgsum-title"'));
    const mount = section.indexOf('<LocalSummaryModel />');
    expect(mount).toBeGreaterThan(0);
    expect(section.slice(0, mount)).toContain('{#if kgSummaryLoadedOnce}');
    expect(page).toContain('kgSummaryLoadedOnce = true;');
  });

  it('every command the panel invokes is registered', () => {
    for (const cmd of [
      'get_local_summary_model_status',
      'remove_local_summary_model',
      'download_local_summary_model',
    ]) {
      expect(lib).toContain(`commands::local_summary_model::${cmd}`);
    }
  });
});
