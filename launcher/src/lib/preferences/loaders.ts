// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (WP-16, L3-F12): the Preferences page's ONE loader registry.
//
// Before this, `routes/preferences/+page.svelte` fired ~35 IPC calls from
// `onMount` — every section's reads, including two slow probes
// (`detect_existing_services`, which walks containers, and the Ollama tags
// fetch) — whether or not the user ever scrolled to that section.
//
// Now every read the page makes lives in `PREF_LOADERS` below, ONE entry per
// section loader:
//
//   * `eager: true`  — runs at mount (`createPreferenceLoaders().mountEager()`).
//                      The eager set is budgeted: ≤ PREF_EAGER_IPC_BUDGET IPC
//                      calls in total (`loaders.test.ts` measures it by
//                      running every eager loader against a counting mock).
//   * `eager: false` — runs the first time its section scrolls into view
//                      (`lazySection` action), then again only on an explicit
//                      reload (a button, a save, `refresh()`).
//   * `slow: true`   — a probe that may take seconds; never eager (pinned).
//
// The page keeps the write side (every `set_*` / `app_state_set*` / action
// command) and the state it renders; the loaders here only READ and return
// data. How to add a section: see ./README.md — one entry here, one handler
// in the page's `createPreferenceLoaders({...})` map (the `Record` type makes
// a missing handler a type error), one `use:lazySection` on the section.

import { invoke, safeInvoke, listen } from '$lib/tauri';
import { getVersion } from '@tauri-apps/api/app';
import { getModuleUpdateAutoCheckEnabled } from '$lib/api/module_updates';

/** The most IPC calls the page may make at mount through eager loaders. */
export const PREF_EAGER_IPC_BUDGET = 8;

// ── app_state keys the page reads here AND writes in the page ────────────
// One home: the page imports these, never re-declares them.
export const RL_LOCAL_OFF_KEY = 'RL_LOCAL_LOGGING_DISABLED';
export const APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL = 'rl.local_logging_disabled_global';
export const APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL = 'rl.online_training_disabled_global';
export const APP_STATE_KEY_KG_SUMMARY_CONSENT = 'kg_summary_openai_consent';
export const APP_STATE_KEY_KG_SUMMARY_MODEL = 'kg_summary_openai_model';
export const APP_STATE_KEY_KG_SUMMARY_OVERRIDE = 'kg_summary_backend_override';
export const APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL = 'kg_summary_ollama_model';
export const APP_STATE_KEY_CODE_EMBED_OVERRIDE = 'code_embed_backend_override';
export const APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL = 'code_embed_openai_model';
export const APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL = 'code_embed_ollama_model';
export const APP_STATE_KEY_ACTIVE_EMBEDDING = 'embedding.active_profile';
export const APP_STATE_KEY_HARDWARE_SNAPSHOT = 'launcher.hardware_snapshot';

/** Default Ollama URL. The launcher pins this to 11435 (not 11434) to avoid
 *  collisions with users' pre-existing Ollama installs — see CLAUDE.md
 *  "Default ports". */
export const OLLAMA_URL = 'http://localhost:11435';

/** `app_state_get`'s row shape. */
export interface AppStateRow {
  key: string;
  is_set: boolean;
  value: string | null;
}

/** Context a loader may need. */
export interface PrefLoadContext {
  /** The selected project (per-project reads return null without one). */
  projectId: string | null;
}

/** The page's two handlers; payload types are the page's own mirrors. */
export interface OpenAiKeyEventHandlers<I, R> {
  onInvalidated: (payload: I) => void;
  onRestored: (payload: R) => void;
}

export interface PrefLoaderSpec {
  /** Runs at mount when true; on first section visibility otherwise. */
  eager: boolean;
  /** A probe that can take seconds. Never eager. */
  slow?: boolean;
  /** The section heading it feeds (for the README table and reviewers). */
  section: string;
}

const appStateGet = (key: string) => invoke<AppStateRow>('app_state_get', { key });
const appStateGetBool = (key: string) => invoke<boolean | null>('app_state_get_bool', { key });

/**
 * THE registry. Each `load` performs exactly the reads its section needs and
 * returns the data; the page applies it to its state. Errors propagate
 * (except where a read is documented soft) so each section keeps its own
 * error rendering.
 */
export const PREF_LOADERS = {
  // ── top of the page (in view at mount) ───────────────────────────────
  windowPrefs: {
    eager: true,
    section: 'Window behaviour',
    load: <T>() => invoke<T>('get_tray_window_prefs'),
  },
  embeddingCatalog: {
    eager: true,
    section: 'Default embedding models',
    /** Two reads with independent failure: a catalog error is rendered,
     *  a missing defaults row is the common first-boot case. */
    load: async <C, D>() => {
      let catalog: C | null = null;
      let catalogError: string | null = null;
      try {
        catalog = await invoke<C>('get_embedding_catalog', { projectId: null });
      } catch (e) {
        catalogError = String(e);
      }
      let defaults: D | null = null;
      try {
        defaults = await invoke<D>('get_default_embedding_models');
      } catch (e) {
        console.warn('[vct] get_default_embedding_models:', e);
      }
      return { catalog, catalogError, defaults };
    },
  },
  openAiKeyEvents: {
    eager: true,
    section: 'OpenAI API key (page-wide toast)',
    /** Two event subscriptions (each one IPC). Page-wide on purpose: the
     *  "key failing validation" toast must fire wherever the user is on the
     *  page. Returns the unlisten functions. */
    load: async <I, R>(h: OpenAiKeyEventHandlers<I, R>) => {
      const invalidated = await listen<I>('vct-openai-key-invalidated', (e) => h.onInvalidated(e.payload));
      const restored = await listen<R>('vct-openai-key-restored', (e) => h.onRestored(e.payload));
      return [invalidated, restored] as const;
    },
  },

  // ── lazy: loaded when the section scrolls into view ──────────────────
  artifactTool: {
    eager: false,
    section: 'Artifact tool (ArtifactToolPanel mounts on visibility)',
    load: async () => undefined,
  },
  kgSummary: {
    eager: false,
    section: 'KG Summaries',
    load: () =>
      Promise.all([
        appStateGetBool(APP_STATE_KEY_KG_SUMMARY_CONSENT),
        appStateGet(APP_STATE_KEY_KG_SUMMARY_MODEL),
        appStateGet(APP_STATE_KEY_KG_SUMMARY_OVERRIDE),
        appStateGet(APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL),
      ]),
  },
  codeEmbed: {
    eager: false,
    section: 'Code Graph Embeddings',
    load: () =>
      Promise.all([
        appStateGet(APP_STATE_KEY_CODE_EMBED_OVERRIDE),
        appStateGet(APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL),
        appStateGet(APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL),
      ]),
  },
  ollamaModels: {
    eager: false,
    slow: true,
    section: 'KG Summaries + Code Graph Embeddings (shared Ollama tags probe)',
    /** Plain HTTP to the local Ollama, not IPC — but it can hang for the
     *  connect timeout when Ollama is down, so it is lazy. */
    load: async (): Promise<string[]> => {
      const resp = await fetch(`${OLLAMA_URL}/api/tags`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: { models?: Array<{ name: string }> } = await resp.json();
      return (data.models ?? []).map((m) => m.name).sort();
    },
  },
  stateDir: {
    eager: false,
    section: 'Storage',
    load: () => invoke<string>('get_resolved_vct_root_dir'),
  },
  pat: {
    eager: false,
    section: 'GitHub access token',
    load: async () => {
      const present = await invoke<boolean>('has_github_pat');
      const preview = present ? await invoke<string | null>('get_github_pat_preview') : null;
      return { present, preview };
    },
  },
  openAi: {
    eager: false,
    section: 'OpenAI API key (optional)',
    /** `present` failing is an error the section renders; the preview is
     *  soft (null on failure). */
    load: async () => {
      const present = await invoke<boolean>('has_openai_api_key');
      let preview: string | null = null;
      if (present) {
        try {
          preview = await invoke<string | null>('get_openai_api_key_preview');
        } catch {
          preview = null;
        }
      }
      return { present, preview };
    },
  },
  rlLocal: {
    eager: false,
    section: 'Local data collection (per project)',
    /** Null without a selected project (nothing to read). */
    load: (ctx: PrefLoadContext) =>
      ctx.projectId
        ? invoke<string | null>('get_claude_env_value', {
            projectId: ctx.projectId,
            key: RL_LOCAL_OFF_KEY,
          })
        : Promise.resolve(null),
  },
  rlUploadConsent: {
    eager: false,
    section: 'Local data collection (upload consent)',
    load: <T>() => safeInvoke<T>('telemetry_status'),
  },
  rlGlobalTelemetry: {
    eager: false,
    section: 'Local data collection (host-wide masters)',
    load: () =>
      Promise.all([
        appStateGetBool(APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL),
        appStateGetBool(APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL),
      ]),
  },
  dualFlags: {
    eager: false,
    section: 'Dual-write flags (DualWriteFlagsPanel mounts on visibility)',
    load: async () => undefined,
  },
  logLevel: {
    eager: false,
    section: 'Diagnostic log level',
    load: <T>() => invoke<T>('get_logging_level'),
  },
  hardwareSnapshot: {
    eager: false,
    section: 'Hardware',
    /** Soft: an absent row just leaves "no snapshot yet". */
    load: () => safeInvoke<AppStateRow>('app_state_get', { key: APP_STATE_KEY_HARDWARE_SNAPSHOT }),
  },
  bootAutostart: {
    eager: false,
    section: 'Startup (hub boot autostart)',
    load: () => invoke<string>('get_hub_boot_autostart'),
  },
  sessionAutostart: {
    eager: false,
    section: 'Startup (launcher session autostart)',
    load: () => invoke<boolean>('get_launcher_session_autostart'),
  },
  services: {
    eager: false,
    slow: true,
    section: 'Shared services',
    load: <T>() => invoke<T>('detect_existing_services'),
  },
  activeEmbedding: {
    eager: false,
    section: 'Embedding profile (global)',
    load: () => appStateGet(APP_STATE_KEY_ACTIVE_EMBEDDING),
  },
  volumes: {
    eager: false,
    section: 'Volume location',
    load: <T>() => invoke<T>('get_volumes_config'),
  },
  moduleUpdateAutoCheck: {
    eager: false,
    section: 'Module updates (24 h automatic check)',
    /** One read; the API wrapper is the ONE home of the command name. */
    load: () => getModuleUpdateAutoCheckEnabled(),
  },
  appVersion: {
    eager: false,
    section: 'About',
    load: () => getVersion(),
  },
} as const satisfies Record<string, PrefLoaderSpec & { load: (...args: never[]) => Promise<unknown> }>;

export type PrefLoaderKey = keyof typeof PREF_LOADERS;

export const PREF_LOADER_KEYS = Object.keys(PREF_LOADERS) as PrefLoaderKey[];

/** Handler per registry key: applies that loader's data to the page. */
export type PrefLoaderHandlers = Record<PrefLoaderKey, () => Promise<void> | void>;

export interface PreferenceLoaders {
  /** Run every eager loader (call from `onMount`, and only this). */
  mountEager(): void;
  /** First activation of a lazy section; later calls are no-ops. */
  activate(key: PrefLoaderKey): void;
  /** Re-run a loader only if its section was already activated (e.g. the
   *  selected project changed). */
  refresh(key: PrefLoaderKey): void;
  isActive(key: PrefLoaderKey): boolean;
}

/** The scheduler. Eager/lazy is decided by the registry, not the page. */
export function createPreferenceLoaders(handlers: PrefLoaderHandlers): PreferenceLoaders {
  const activated = new Set<PrefLoaderKey>();
  function run(key: PrefLoaderKey): void {
    activated.add(key);
    Promise.resolve()
      .then(() => handlers[key]())
      .catch((e) => console.debug(`[vct] preferences loader ${key} failed`, e));
  }
  return {
    mountEager() {
      for (const key of PREF_LOADER_KEYS) {
        if (PREF_LOADERS[key].eager && !activated.has(key)) run(key);
      }
    },
    activate(key) {
      if (!activated.has(key)) run(key);
    },
    refresh(key) {
      if (activated.has(key)) run(key);
    },
    isActive(key) {
      return activated.has(key);
    },
  };
}

/**
 * Svelte action: activate the given lazy loaders the first time the section
 * scrolls into view (200px early). Without `IntersectionObserver` (tests,
 * very old WebViews) the loaders run immediately — degrade to the old
 * eager behaviour, never to a section that never loads.
 */
export function lazySection(
  node: Element,
  params: { loaders: PreferenceLoaders; keys: PrefLoaderKey[] },
): { destroy(): void } {
  const fire = () => {
    for (const k of params.keys) params.loaders.activate(k);
  };
  if (typeof IntersectionObserver === 'undefined') {
    fire();
    return { destroy() {} };
  }
  const io = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        fire();
        io.disconnect();
      }
    },
    { rootMargin: '200px 0px' },
  );
  io.observe(node);
  return { destroy: () => io.disconnect() };
}
