// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (WP-16, L3-F12): the Preferences page's mount cost and loader
// wiring.
//
//   * the eager set is MEASURED (every eager loader run against a counting
//     IPC mock), not declared: ≤ PREF_EAGER_IPC_BUDGET;
//   * the two slow probes are lazy;
//   * every loader the page had before the registry is present exactly once,
//     and the page's handler map names each registry key once;
//   * zero direct IPC in the page's `onMount`, and no READ command the
//     registry owns is still invoked from the page (one home);
//   * every toggle's write command is still invoked by the page, and its
//     read counterpart is in the registry (census of set_*/get_* pairs).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const calls: string[] = [];
vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(async (cmd: string) => {
    calls.push(cmd);
    return cmd === 'get_embedding_catalog' ? { text_models: [], code_models: [], errors: [] } : null;
  }),
  safeInvoke: vi.fn(async (cmd: string) => {
    calls.push(cmd);
    return null;
  }),
  listen: vi.fn(async (event: string) => {
    calls.push(`listen:${event}`);
    return () => {};
  }),
}));
vi.mock('@tauri-apps/api/app', () => ({
  getVersion: vi.fn(async () => {
    calls.push('plugin:app|version');
    return '0.0.0';
  }),
}));

import {
  PREF_EAGER_IPC_BUDGET,
  PREF_LOADERS,
  PREF_LOADER_KEYS,
  createPreferenceLoaders,
  lazySection,
  type PrefLoaderKey,
} from './loaders';

const PAGE = readFileSync(
  fileURLToPath(new URL('../../routes/preferences/+page.svelte', import.meta.url)),
  'utf8',
);
const SCRIPT = PAGE.split('</script>')[0];
const LOADERS_SRC = readFileSync(fileURLToPath(new URL('./loaders.ts', import.meta.url)), 'utf8');

/** Run one registry loader with the arguments the page would pass. */
async function runLoader(key: PrefLoaderKey): Promise<void> {
  const spec = PREF_LOADERS[key] as { load: (...a: unknown[]) => Promise<unknown> };
  const arg =
    key === 'openAiKeyEvents'
      ? { onInvalidated: () => {}, onRestored: () => {} }
      : key === 'rlLocal'
        ? { projectId: 'p1' }
        : undefined;
  try {
    await spec.load(arg);
  } catch {
    // Reads may reject on the null mock payload; only the calls count.
  }
}

beforeEach(() => {
  calls.length = 0;
});

describe('mount budget', () => {
  it(`the eager loaders make at most ${PREF_EAGER_IPC_BUDGET} IPC calls`, async () => {
    const eager = PREF_LOADER_KEYS.filter((k) => PREF_LOADERS[k].eager);
    for (const k of eager) await runLoader(k);
    expect(calls.length, `eager IPC at mount: ${calls.join(', ')}`).toBeLessThanOrEqual(
      PREF_EAGER_IPC_BUDGET,
    );
    // Pinned so a change to the eager set is a visible diff here.
    expect(eager).toEqual(['windowPrefs', 'embeddingCatalog', 'openAiKeyEvents']);
    expect(calls).toEqual([
      'get_tray_window_prefs',
      'get_embedding_catalog',
      'get_default_embedding_models',
      'listen:vct-openai-key-invalidated',
      'listen:vct-openai-key-restored',
    ]);
  });

  it('the two slow probes are lazy', () => {
    expect(PREF_LOADERS.services.eager).toBe(false);
    expect(PREF_LOADERS.services.slow).toBe(true);
    expect(PREF_LOADERS.ollamaModels.eager).toBe(false);
    expect(PREF_LOADERS.ollamaModels.slow).toBe(true);
    for (const k of PREF_LOADER_KEYS) {
      const spec = PREF_LOADERS[k] as { eager: boolean; slow?: boolean };
      if (spec.slow) expect(spec.eager, `${k} is slow and must not be eager`).toBe(false);
    }
  });

  it('what the page used to fire at mount (~35 IPC) is what the lazy set now defers', async () => {
    for (const k of PREF_LOADER_KEYS) await runLoader(k);
    // Every read the page ever made at mount, now all in the registry.
    expect(calls.length).toBeGreaterThanOrEqual(25);
  });
});

describe('the registry holds every loader exactly once', () => {
  // The page's `onMount` before v0.2.100 called these, one each.
  const BEFORE: Record<string, PrefLoaderKey> = {
    loadPat: 'pat',
    loadInitialHardwareSnapshot: 'hardwareSnapshot',
    loadEmbeddingCatalog: 'embeddingCatalog',
    loadOpenAi: 'openAi',
    subscribeOpenAiEvents: 'openAiKeyEvents',
    loadRlLocalState: 'rlLocal',
    loadRlUploadConsent: 'rlUploadConsent',
    loadRlGlobalTelemetryState: 'rlGlobalTelemetry',
    loadKgSummarySettings: 'kgSummary',
    loadCodeEmbedSettings: 'codeEmbed',
    fetchOllamaModels: 'ollamaModels',
    refreshServices: 'services',
    loadActiveEmbedding: 'activeEmbedding',
    refreshVolumes: 'volumes',
    loadAppVersion: 'appVersion',
    loadStateDir: 'stateDir',
    loadBootAutostart: 'bootAutostart',
    loadSessionAutostart: 'sessionAutostart',
    loadWindowPrefs: 'windowPrefs',
    loadLogLevel: 'logLevel',
  };

  it('each former onMount loader maps to one registry key, wired once in the page', () => {
    for (const [fn, key] of Object.entries(BEFORE)) {
      expect(PREF_LOADER_KEYS, fn).toContain(key);
      const wired = SCRIPT.match(new RegExp(`\\b${key}:\\s*${fn}\\b`, 'g')) ?? [];
      expect(wired.length, `${key}: ${fn} in the page's handler map`).toBe(1);
    }
    // The two child panels that mount their own reads are gated by the
    // registry too (they were eager by virtue of being rendered).
    expect(PREF_LOADER_KEYS).toContain('artifactTool');
    expect(PREF_LOADER_KEYS).toContain('dualFlags');
    expect(new Set(PREF_LOADER_KEYS).size).toBe(PREF_LOADER_KEYS.length);
    expect(PREF_LOADER_KEYS.length).toBe(Object.keys(BEFORE).length + 2);
  });

  it('every lazy key is attached to a section of the page', () => {
    for (const k of PREF_LOADER_KEYS) {
      if (PREF_LOADERS[k].eager) continue;
      expect(PAGE, `use:lazySection names '${k}'`).toMatch(
        new RegExp(`use:lazySection=\\{\\{ loaders, keys: \\[[^\\]]*'${k}'`),
      );
    }
  });
});

describe('one home for reads (grep proofs)', () => {
  it('onMount makes no direct IPC — it only starts the registry', () => {
    const m = /onMount\(\(\) => \{([\s\S]*?)\n {2}\}\);/.exec(SCRIPT);
    expect(m, 'onMount block').not.toBeNull();
    const body = m![1];
    expect(body).not.toMatch(/\b(invoke|safeInvoke|tauriListen|listen|getVersion|fetch)\s*[<(]/);
    expect(body.trim()).toBe('loaders.mountEager();');
  });

  it('no READ command the registry owns is invoked from the page', () => {
    const owned = new Set<string>();
    for (const m of LOADERS_SRC.matchAll(/\b(?:invoke|safeInvoke)<[^>]*>\(\s*'([a-z_]+)'/g)) owned.add(m[1]);
    for (const m of LOADERS_SRC.matchAll(/\b(?:invoke|safeInvoke)\(\s*'([a-z_]+)'/g)) owned.add(m[1]);
    owned.add('app_state_get');
    owned.add('app_state_get_bool');
    // The Re-check flow reads `openai_was_valid` as part of an ACTION, not a
    // section load — the one app_state_get_bool the page keeps.
    const pageReads = [...SCRIPT.matchAll(/\b(?:invoke|safeInvoke)<[^>]*>\(\s*'([a-z_]+)'/g)].map(
      (m) => m[1],
    );
    const leaks = pageReads.filter((c) => owned.has(c));
    expect(leaks).toEqual(['app_state_get_bool']);
    expect(SCRIPT).toContain("{ key: 'openai_was_valid' }");
    expect(owned.size).toBeGreaterThanOrEqual(17);
  });
});

describe('every toggle still reaches its command (set_*/get_* census)', () => {
  // [write command the page invokes, read command the registry invokes]
  const PAIRS: Array<[string, string]> = [
    ['set_tray_window_pref', 'get_tray_window_prefs'],
    ['set_default_embedding_models', 'get_default_embedding_models'],
    ['register_github_pat', 'has_github_pat'],
    ['clear_github_pat', 'has_github_pat'],
    ['register_openai_api_key', 'has_openai_api_key'],
    ['clear_openai_api_key', 'has_openai_api_key'],
    ['set_claude_env_value', 'get_claude_env_value'],
    ['app_state_set_bool', 'app_state_get_bool'],
    ['app_state_set', 'app_state_get'],
    ['telemetry_set_consent', 'telemetry_status'],
    ['set_hub_boot_autostart', 'get_hub_boot_autostart'],
    ['set_launcher_session_autostart', 'get_launcher_session_autostart'],
    ['set_logging_level', 'get_logging_level'],
    ['set_volumes_config_dry_run', 'get_volumes_config'],
    ['migrate_volumes', 'get_volumes_config'],
  ];

  it('each write is invoked from the page and its read lives in the registry', () => {
    for (const [set, get] of PAIRS) {
      expect(SCRIPT, `${set} invoked by the page`).toMatch(new RegExp(`\\(\\s*'${set}'`));
      expect(LOADERS_SRC, `${get} read by the registry`).toMatch(
        get.startsWith('app_state_get')
          ? new RegExp(`'${get}'`)
          : new RegExp(`\\(\\s*'${get}'`),
      );
    }
  });

  it('every set_* the page invokes is in the census', () => {
    const sets = new Set(
      [...SCRIPT.matchAll(/\(\s*'(set_[a-z_]+|app_state_set[a-z_]*)'/g)].map((m) => m[1]),
    );
    const censused = new Set(PAIRS.map(([s]) => s));
    for (const s of sets) expect(censused.has(s), `${s} has no read counterpart in the census`).toBe(true);
  });
});

describe('scheduler', () => {
  function handlers() {
    const ran: string[] = [];
    const h = Object.fromEntries(PREF_LOADER_KEYS.map((k) => [k, () => void ran.push(k)])) as Record<
      PrefLoaderKey,
      () => void
    >;
    return { ran, h };
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));

  it('mountEager runs exactly the eager keys; activate runs a lazy key once', async () => {
    const { ran, h } = handlers();
    const l = createPreferenceLoaders(h);
    l.mountEager();
    await flush();
    expect(ran).toEqual(['windowPrefs', 'embeddingCatalog', 'openAiKeyEvents']);
    l.activate('services');
    l.activate('services');
    await flush();
    expect(ran.filter((k) => k === 'services')).toHaveLength(1);
  });

  it('refresh re-runs only an activated key', async () => {
    const { ran, h } = handlers();
    const l = createPreferenceLoaders(h);
    l.refresh('rlLocal');
    await flush();
    expect(ran).not.toContain('rlLocal');
    l.activate('rlLocal');
    l.refresh('rlLocal');
    await flush();
    expect(ran.filter((k) => k === 'rlLocal')).toHaveLength(2);
  });

  it('a failing handler does not reject into the caller', async () => {
    const l = createPreferenceLoaders({
      ...handlers().h,
      services: () => {
        throw new Error('boom');
      },
    });
    const dbg = vi.spyOn(console, 'debug').mockImplementation(() => {});
    expect(() => l.activate('services')).not.toThrow();
    await flush();
    expect(dbg).toHaveBeenCalled();
    dbg.mockRestore();
  });

  it('lazySection without IntersectionObserver loads immediately (never a dead section)', async () => {
    const { ran, h } = handlers();
    const l = createPreferenceLoaders(h);
    const act = lazySection({} as Element, { loaders: l, keys: ['volumes', 'services'] });
    await flush();
    expect(ran).toEqual(['volumes', 'services']);
    act.destroy();
  });

  it('lazySection with IntersectionObserver waits for visibility', async () => {
    let cb: ((e: Array<{ isIntersecting: boolean }>) => void) | null = null;
    const disconnect = vi.fn();
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(c: typeof cb) {
          cb = c;
        }
        observe() {}
        disconnect = disconnect;
      },
    );
    const { ran, h } = handlers();
    const l = createPreferenceLoaders(h);
    lazySection({} as Element, { loaders: l, keys: ['volumes'] });
    await flush();
    expect(ran).toEqual([]);
    cb!([{ isIntersecting: true }]);
    await flush();
    expect(ran).toEqual(['volumes']);
    expect(disconnect).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
