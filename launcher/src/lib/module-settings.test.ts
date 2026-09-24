// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.97 — the bundled modules' manifest `settings`, editable from
// Preferences → Modules (`CoreModuleSettingsPanel.svelte`). The panel's
// decisions live in `./module-settings`; these tests pin them, with the
// Tauri bridge mocked (no command reaches a real launcher/hub).

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn(), tauriAvailable: vi.fn(() => true) }));

import { invoke } from '$lib/tauri';
import {
  checkSettingValue,
  isMachineWide,
  editorHref,
  hasEditableMachineWide,
  isEditable,
  listModuleSettings,
  liveSettingValues,
  liveValueFor,
  moduleOffersProject,
  loadModuleSetting,
  machineWideNote,
  numberSettingControl,
  saveModuleSetting,
  settingProjectArg,
  settingWidget,
  splitByScope,
  textSettingControl,
  type ListedModuleSettings,
  type ListedSetting,
  type ManifestSettingDecl,
  type SettingBinding,
} from './module-settings';

const here = dirname(fileURLToPath(import.meta.url));
const LAUNCHER = resolve(here, '../..');
const invokeMock = vi.mocked(invoke);

type Declared = { module_id: string; name: string; settings: ManifestSettingDecl[] };

/** The bundled manifests as the Rust `bundled_module_settings` lists them. */
function bundledModules(): Declared[] {
  const dir = resolve(LAUNCHER, 'bundled_manifests');
  return readdirSync(dir)
    .filter((n) => n.endsWith('.json'))
    .sort()
    .map((n) => JSON.parse(readFileSync(resolve(dir, n), 'utf8')))
    .filter((m) => Array.isArray(m.settings) && m.settings.length > 0)
    .map((m) => ({ module_id: m.id, name: m.name, settings: m.settings }));
}

function decl(moduleId: string, key: string): ManifestSettingDecl {
  const m = bundledModules().find((x) => x.module_id === moduleId)!;
  return m.settings.find((s) => s.key === key)!;
}

beforeEach(() => {
  invokeMock.mockReset();
});

describe('the shared validation case table (parity with the Rust write gate)', () => {
  const cases = JSON.parse(
    readFileSync(
      resolve(
        LAUNCHER,
        'src-tauri/vct-launcher-core/tests/fixtures/setting_validation_cases.json',
      ),
      'utf8',
    ),
  ) as Array<{ name: string; decl: ManifestSettingDecl; value: unknown; ok: boolean }>;

  it('covers accept and reject', () => {
    expect(cases.length).toBeGreaterThanOrEqual(20);
    expect(cases.some((c) => c.ok)).toBe(true);
    expect(cases.some((c) => !c.ok)).toBe(true);
  });

  for (const c of cases) {
    it(`${c.ok ? 'accepts' : 'refuses'}: ${c.name}`, () => {
      const problem = checkSettingValue(c.decl, c.value);
      expect(problem === null, String(problem)).toBe(c.ok);
    });
  }
});

describe('bundled settings are listed by scope', () => {
  it('the hub port is machine-wide and the session-state thresholds are per project', () => {
    const mods = bundledModules();
    const hub = mods.find((m) => m.module_id === 'vct-hub-api')!;
    const session = mods.find((m) => m.module_id === 'vct-session-state')!;
    expect(splitByScope(hub).machineWide.map((d) => d.key)).toEqual(['VCT_HUB_PORT']);
    expect(splitByScope(hub).perProject).toEqual([]);
    expect(splitByScope(session).machineWide).toEqual([]);
    expect(splitByScope(session).perProject.map((d) => d.key)).toEqual([
      'CONTEXT_STATE_MAX_LINES',
      'MEMORY_MAX_LINES',
    ]);
    const allGlobal = mods.flatMap((m) => m.settings.filter(isMachineWide).map((d) => d.key));
    // Machine-wide: the hub port and the machine's service settings.
    expect(allGlobal).toEqual([
      'CODE_EMBED_BACKEND',
      'CODE_EMBED_DEVICE',
      'CODE_EMBED_PORT',
      'CODE_EMBED_BACKEND',
      'VCT_HUB_PORT',
      'SHARED_KG_COLLECTION',
      'WEAVIATE_URL',
    ]);
  });

  it('the list comes from the list_module_settings command', async () => {
    invokeMock.mockResolvedValueOnce([listed(bundledModules()[0], null)]);
    const got = await listModuleSettings();
    expect(invokeMock).toHaveBeenCalledWith('list_module_settings');
    expect(got).toHaveLength(1);
  });

  it('every bundled setting has a widget this launcher can edit', () => {
    for (const m of bundledModules()) {
      for (const d of m.settings) {
        expect(settingWidget(d), `${m.module_id}/${d.key}`).not.toBe('unsupported');
      }
    }
    expect(settingWidget(decl('vct-hub-api', 'VCT_HUB_PORT'))).toBe('number');
    expect(settingWidget(decl('vct-code-embedding', 'CODE_EMBED_BACKEND'))).toBe('select');
    expect(settingWidget(decl('vct-kg', 'WEAVIATE_URL'))).toBe('text');
  });

  it('a machine-wide setting says its module must be restarted', () => {
    const hub = bundledModules().find((m) => m.module_id === 'vct-hub-api')!;
    expect(machineWideNote(hub)).toMatch(/Restart Hub API Server after a change/);
  });
});

describe('saving goes through set_module_setting with the right scope', () => {
  it('a machine-wide value is saved WITHOUT a project', async () => {
    invokeMock.mockResolvedValueOnce(undefined);
    await saveModuleSetting('vct-hub-api', decl('vct-hub-api', 'VCT_HUB_PORT'), 'proj-1', 8802);
    expect(invokeMock).toHaveBeenCalledWith('set_module_setting', {
      moduleId: 'vct-hub-api',
      controlId: 'VCT_HUB_PORT',
      value: 8802,
      projectId: null,
    });
  });

  it('a per-project value is saved for the picked project', async () => {
    invokeMock.mockResolvedValueOnce(undefined);
    const d = decl('vct-session-state', 'CONTEXT_STATE_MAX_LINES');
    await saveModuleSetting('vct-session-state', d, 'proj-1', 800);
    expect(invokeMock).toHaveBeenCalledWith('set_module_setting', {
      moduleId: 'vct-session-state',
      controlId: 'CONTEXT_STATE_MAX_LINES',
      value: 800,
      projectId: 'proj-1',
    });
    expect(settingProjectArg(d, '')).toBe('');
  });

  it('an invalid value is refused in the UI and nothing is sent', async () => {
    const port = decl('vct-hub-api', 'VCT_HUB_PORT');
    await expect(saveModuleSetting('vct-hub-api', port, '', 80)).rejects.toThrow(/at least 1024/);
    await expect(saveModuleSetting('vct-hub-api', port, '', 7700.5)).rejects.toThrow(/whole number/);
    const backend = decl('vct-code-embedding', 'CODE_EMBED_BACKEND');
    await expect(saveModuleSetting('vct-code-embedding', backend, 'p', 'cpu')).rejects.toThrow(
      /one of gpu, ollama/,
    );
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("a backend refusal surfaces as the save's error", async () => {
    invokeMock.mockRejectedValueOnce(new Error('set_module_setting: refused'));
    await expect(
      saveModuleSetting('vct-hub-api', decl('vct-hub-api', 'VCT_HUB_PORT'), '', 9000),
    ).rejects.toThrow(/refused/);
  });
});

describe('loading', () => {
  it('reads a machine-wide value without a project and falls back to the declared default', async () => {
    invokeMock.mockResolvedValueOnce(null);
    const v = await loadModuleSetting('vct-hub-api', decl('vct-hub-api', 'VCT_HUB_PORT'), 'proj-1');
    expect(invokeMock).toHaveBeenCalledWith('get_module_setting', {
      moduleId: 'vct-hub-api',
      controlId: 'VCT_HUB_PORT',
      projectId: null,
    });
    expect(v).toBe(7700);
  });

  it('returns the stored value when there is one', async () => {
    invokeMock.mockResolvedValueOnce('ollama');
    const d = decl('vct-code-embedding', 'CODE_EMBED_BACKEND');
    expect(await loadModuleSetting('vct-code-embedding', d, 'proj-1')).toBe('ollama');
  });
});

describe('the reused config-tab controls', () => {
  it('the number control carries no min/max, so an out-of-range value is refused rather than clamped', () => {
    const port = decl('vct-hub-api', 'VCT_HUB_PORT');
    const c = numberSettingControl(port);
    expect(c).toMatchObject({ kind: 'number_input', id: 'VCT_HUB_PORT', default: 7700, step: 1 });
    expect(c.min ?? null).toBeNull();
    expect(c.max ?? null).toBeNull();
    // What the control's `validate` prop returns for 80 — the inline reason.
    expect(checkSettingValue(port, 80)).toMatch(/at least 1024/);
  });

  it('the text control is keyed by the setting key', () => {
    const c = textSettingControl(decl('vct-kg', 'WEAVIATE_URL'));
    expect(c).toMatchObject({ kind: 'text_input', id: 'WEAVIATE_URL', default: 'http://localhost:8081' });
  });
});

// ─── Round 2: bindings, installed modules, live values ─────────────────

const STORED: SettingBinding = { kind: 'stored', reader: 'r' };
const KG_HOME: SettingBinding = {
  kind: 'elsewhere',
  live: 'project_kg_collection',
  home: 'The project primary KG binding.',
  editor_route: '/project/{project_id}',
  editor_label: 'Open the project (Identity tab)',
};

/** A Rust-shaped listing entry; every setting stored unless `bindings` says otherwise. */
function listed(
  m: Declared,
  projects: string[] | null,
  bindings: Record<string, SettingBinding> = {},
): ListedModuleSettings {
  return {
    module_id: m.module_id,
    name: m.name,
    origin: projects === null ? 'bundled' : 'installed',
    projects,
    settings: m.settings.map((d) => ({ ...d, binding: bindings[d.key] ?? STORED })),
  };
}

describe('a setting whose live value lives elsewhere', () => {
  const kg = bundledModules().find((m) => m.module_id === 'vct-kg')!;
  const kgListed = listed(kg, null, {
    KG_COLLECTION: KG_HOME,
    SHARED_KG_COLLECTION: { ...KG_HOME, live: 'shared_kg_collection' },
    WEAVIATE_URL: {
      kind: 'elsewhere',
      live: 'weaviate_url',
      home: 'vct-config.toml',
      editor_route: null,
      editor_label: null,
    },
  });

  it('is not editable, and the module offers no editable machine-wide value', () => {
    expect(kgListed.settings.some(isEditable)).toBe(false);
    expect(hasEditableMachineWide(kgListed)).toBe(false);
    const hub = listed(bundledModules().find((m) => m.module_id === 'vct-hub-api')!, null);
    expect(hasEditableMachineWide(hub)).toBe(true);
  });

  it('links to its editor with the picked project, and not without one', () => {
    expect(editorHref(KG_HOME, 'p 1')).toBe('/project/p%201');
    expect(editorHref(KG_HOME, '')).toBeNull();
    const noEditor = kgListed.settings.find((s) => s.key === 'WEAVIATE_URL')!;
    expect(editorHref(noEditor.binding, 'p1')).toBeNull();
    expect(editorHref(STORED, 'p1')).toBeNull();
  });

  it('shows the live value the backend resolved', async () => {
    invokeMock.mockResolvedValueOnce([
      { module_id: 'vct-kg', key: 'KG_COLLECTION', value: 'Foo_KnowledgeGraph', note: null },
    ]);
    const live = await liveSettingValues('p1');
    expect(invokeMock).toHaveBeenCalledWith('module_setting_live_values', { projectId: 'p1' });
    expect(liveValueFor(live, 'vct-kg', 'KG_COLLECTION')?.value).toBe('Foo_KnowledgeGraph');
    expect(liveValueFor(live, 'vct-kg', 'WEAVIATE_URL')).toBeNull();
    invokeMock.mockResolvedValueOnce([]);
    await liveSettingValues('');
    expect(invokeMock).toHaveBeenLastCalledWith('module_setting_live_values', { projectId: null });
  });
});

describe('an installed (catalog) module', () => {
  const session = bundledModules().find((m) => m.module_id === 'vct-session-state')!;
  const catalog = listed({ ...session, module_id: 'vct-test-catalog', name: 'Test' }, ['p1']);

  it('offers its per-project settings only for the projects it is installed in', () => {
    expect(moduleOffersProject(catalog, 'p1')).toBe(true);
    expect(moduleOffersProject(catalog, 'p2')).toBe(false);
    expect(moduleOffersProject(catalog, '')).toBe(false);
    const bundled = listed(session, null);
    expect(moduleOffersProject(bundled, 'any-project')).toBe(true);
  });

  it('saves through the same validated path', async () => {
    const d = catalog.settings.find((s) => s.key === 'MEMORY_MAX_LINES') as ListedSetting;
    await expect(saveModuleSetting('vct-test-catalog', d, 'p1', 9999)).rejects.toThrow(/at most 2000/);
    expect(invokeMock).not.toHaveBeenCalled();
    invokeMock.mockResolvedValueOnce(undefined);
    await saveModuleSetting('vct-test-catalog', d, 'p1', 300);
    expect(invokeMock).toHaveBeenCalledWith('set_module_setting', {
      moduleId: 'vct-test-catalog',
      controlId: 'MEMORY_MAX_LINES',
      value: 300,
      projectId: 'p1',
    });
  });
});
