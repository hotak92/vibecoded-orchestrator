// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.97 — a module manifest's `settings` block, editable from the launcher.
//
// `docs/VCT_MODULE_MANIFEST_SPEC.md` §8 promised manifest settings were
// "user-editable via the launcher GUI"; no surface read the block, so a
// module's settings (e.g. `vct-hub-api`'s machine-wide `VCT_HUB_PORT`) were
// writable only from Rust. `CoreModuleSettingsPanel.svelte` (Preferences →
// Modules) now lists the bundled modules' AND the installed catalog modules'
// settings; this file holds its logic so it is testable without mounting
// Svelte.
//
// A listed setting carries its BINDING (Rust `module_setting_bindings`):
// `stored` settings are edited here; a setting whose live value has another
// home (a project's KG binding, the machine's service config) is shown
// read-only with that live value — never stored a second time.
//
// Storage is the existing generic path: `get_module_setting` /
// `set_module_setting` (`commands/module_gui.rs`), which route a DECLARED
// setting by its scope and re-validate every write — the check below is for
// immediate feedback only, never the gate.
//
// `checkSettingValue` must match `validate_setting_value` in
// `launcher/src-tauri/vct-launcher-core/src/module_settings_schema.rs`. Both
// run the shared case table
// `launcher/src-tauri/vct-launcher-core/tests/fixtures/setting_validation_cases.json`.

import { invoke } from '$lib/tauri';
import type { NumberInputControl, TextInputControl } from '$lib/types/manifest';

/** Mirrors `vct_launcher_core::module_settings_schema::SCOPE_*`. */
export const SCOPE_PER_PROJECT = 'per-project';
export const SCOPE_GLOBAL = 'global';

/** One entry of a manifest's `settings` block (Rust `manifest::SettingDecl`). */
export interface ManifestSettingDecl {
  key: string;
  prompt?: string;
  description?: string;
  /** "string" (default) | "integer" | "boolean" | "multiselect" | "path" */
  type?: string;
  default?: unknown;
  default_by_platform?: Record<string, unknown>;
  options?: string[];
  validation?: string | null;
  validation_cmd?: string | null;
  required?: boolean;
  min?: number | null;
  max?: number | null;
  /** "per-project" (default) | "global" (one machine-wide value). */
  scope?: string;
}

/** True for a machine-wide (`scope: "global"`) setting. */
export function isMachineWide(decl: ManifestSettingDecl): boolean {
  return decl.scope === SCOPE_GLOBAL;
}

/** The label a setting is shown (and named in errors) under. */
export function settingLabel(decl: ManifestSettingDecl): string {
  const p = (decl.prompt ?? '').trim();
  return p === '' ? decl.key : p;
}

function settingType(decl: ManifestSettingDecl): string {
  return decl.type ?? 'string';
}

function hasOptions(decl: ManifestSettingDecl): boolean {
  return Array.isArray(decl.options) && decl.options.length > 0;
}

/**
 * Why a `validation` pattern is outside the PORTABLE subset JavaScript
 * `RegExp` (flags `us`) and the Rust `regex` crate read the same way, or
 * `null` when it is inside: literals, `.`, `^` / `$`, `|`, `(...)` /
 * `(?:...)`, the quantifiers `* + ? {n} {n,} {n,m}` (and lazy forms), bracket
 * classes with ranges, and escaping one of `\ ^ $ . | ? * + ( ) [ ] { } /`
 * (plus `-` inside a class). Must match `portable_pattern_problem` in
 * `module_settings_schema.rs` (the shared case table runs both).
 */
export function portablePatternProblem(pattern: string): string | null {
  const ESCAPABLE = '\\^$.|?*+()[]{}/';
  const chars = Array.from(pattern);
  const braceProblem = "a '{' that is not a {n}, {n,} or {n,m} quantifier (escape it)";
  const digitsAt = (from: number): number => {
    let n = 0;
    while (from + n < chars.length && /^[0-9]$/.test(chars[from + n])) n += 1;
    return n;
  };
  let i = 0;
  let inClass = false;
  while (i < chars.length) {
    const c = chars[i];
    if (c === '\\') {
      const next = chars[i + 1];
      if (next === undefined) return 'a trailing backslash';
      if (!(ESCAPABLE.includes(next) || (inClass && next === '-'))) {
        return 'an escape other than a punctuation character (write a class such as [0-9] instead of \\d)';
      }
      i += 2;
      continue;
    }
    if (inClass) {
      if (c === ']') inClass = false;
      else if (c === '[') return "a '[' inside a character class";
      else if ((c === '&' || c === '-' || c === '~') && chars[i + 1] === c) {
        return 'a class operator (&&, --, ~~)';
      }
      i += 1;
      continue;
    }
    if (c === '[') {
      inClass = true;
      let j = i + 1;
      if (chars[j] === '^') j += 1;
      if (chars[j] === ']') return "an empty or ']'-first character class";
      i = j;
      continue;
    }
    if (c === ']' || c === '}') return "a stray ']' or '}' (escape it)";
    if (c === '(' && chars[i + 1] === '?' && chars[i + 2] !== ':') {
      return 'a (? group other than (?: (lookaround, flags, named groups)';
    }
    if (c === '{') {
      let j = i + 1;
      const n = digitsAt(j);
      if (n === 0) return braceProblem;
      j += n;
      if (chars[j] === ',') {
        j += 1;
        j += digitsAt(j);
      }
      if (chars[j] !== '}') return braceProblem;
      i = j + 1;
      continue;
    }
    i += 1;
  }
  return inClass ? 'an unclosed character class' : null;
}

/**
 * Check `value` against its declaration: `null` = acceptable, otherwise a
 * one-line reason. Same rules as the Rust write gate (see the file header).
 */
export function checkSettingValue(
  decl: ManifestSettingDecl,
  value: unknown,
): string | null {
  const name = settingLabel(decl);
  switch (settingType(decl)) {
    case 'integer': {
      // The Rust gate also refuses an integer SPELLED with a fraction or
      // exponent (`7700.0`); a JS number cannot carry that spelling, and the
      // page serializes an integer without one, so it never sends it. The
      // case table marks those rows `json_spelling_only`.
      if (typeof value !== 'number' || !Number.isInteger(value)) {
        return `${name}: must be a whole number`;
      }
      if (decl.min !== null && decl.min !== undefined && value < decl.min) {
        return `${name}: must be at least ${decl.min}`;
      }
      if (decl.max !== null && decl.max !== undefined && value > decl.max) {
        return `${name}: must be at most ${decl.max}`;
      }
      return null;
    }
    case 'boolean':
      return typeof value === 'boolean' ? null : `${name}: must be true or false`;
    case 'string':
    case 'path': {
      if (typeof value !== 'string') return `${name}: must be text`;
      if (value.trim() === '') {
        return decl.required ? `${name}: is required` : null;
      }
      if (hasOptions(decl) && !decl.options!.includes(value)) {
        return `${name}: must be one of ${decl.options!.join(', ')}`;
      }
      if (decl.validation) {
        const problem = portablePatternProblem(decl.validation);
        if (problem !== null) {
          return `${name}: the module's validation pattern uses ${problem}, which the launcher does not support`;
        }
        let re: RegExp;
        try {
          // `u`: match by code point, like the Rust side; `s`: `.` matches
          // every character, like the Rust side's `dot_matches_new_line`.
          re = new RegExp(decl.validation, 'us');
        } catch (e) {
          return `${name}: the module's validation pattern is invalid (${String(e)})`;
        }
        if (!re.test(value)) {
          return `${name}: does not match the required format ${decl.validation}`;
        }
      }
      return null;
    }
    case 'multiselect': {
      if (!Array.isArray(value)) return `${name}: must be a list`;
      if (value.length === 0 && decl.required) return `${name}: pick at least one`;
      for (const item of value) {
        if (typeof item !== 'string') return `${name}: every choice must be text`;
        if (hasOptions(decl) && !decl.options!.includes(item)) {
          return `${name}: '${item}' is not one of ${decl.options!.join(', ')}`;
        }
      }
      return null;
    }
    default:
      return `${name}: unsupported setting type '${settingType(decl)}'`;
  }
}

/** Which widget renders a setting. */
export type SettingWidget = 'number' | 'text' | 'select' | 'checkbox' | 'multiselect' | 'unsupported';

export function settingWidget(decl: ManifestSettingDecl): SettingWidget {
  switch (settingType(decl)) {
    case 'integer':
      return 'number';
    case 'boolean':
      return 'checkbox';
    case 'multiselect':
      return 'multiselect';
    case 'string':
    case 'path':
      return hasOptions(decl) ? 'select' : 'text';
    default:
      return 'unsupported';
  }
}

/**
 * The config-tab controls the existing `NumberInputControl` /
 * `TextInputControl` components render a `number` / `text` setting with.
 * Their `id` is the setting KEY — the key `set_module_setting` validates and
 * routes by.
 *
 * `min` / `max` are deliberately NOT copied onto the number control: that
 * control CLAMPS into its bounds, and a setting out of range must be
 * refused with a reason (the panel passes `checkSettingValue` as the
 * control's `validate`), not silently replaced — a port of 80 saved as 1024
 * is a value the user never typed.
 */
export function numberSettingControl(decl: ManifestSettingDecl): NumberInputControl {
  return {
    kind: 'number_input',
    id: decl.key,
    label: settingLabel(decl),
    tooltip: tooltipOf(decl),
    default: typeof decl.default === 'number' ? decl.default : null,
    step: 1,
  };
}

/** The `text_input` control a free-text (`string` / `path`) setting renders with. */
export function textSettingControl(decl: ManifestSettingDecl): TextInputControl {
  return {
    kind: 'text_input',
    id: decl.key,
    label: settingLabel(decl),
    tooltip: tooltipOf(decl),
    default: typeof decl.default === 'string' ? decl.default : '',
    placeholder: decl.key,
  };
}

function tooltipOf(decl: ManifestSettingDecl): string | null {
  return decl.description?.trim() ? decl.description : null;
}

/** A module's settings split by where their value lives. */
export function splitByScope<D extends ManifestSettingDecl>(module: { settings: D[] }): {
  machineWide: D[];
  perProject: D[];
} {
  return {
    machineWide: module.settings.filter(isMachineWide),
    perProject: module.settings.filter((d) => !isMachineWide(d)),
  };
}

// ─── Round 2: bindings, installed modules, live values ─────────────────
//
// Mirrors `vct_launcher_core::module_setting_bindings::SettingBinding` and
// `module_settings_schema::ListedModuleSettings` (serde shapes).

/** Where a listed setting's value lives. */
export type SettingBinding =
  /** Stored in module settings and read there by `reader` — editable. */
  | { kind: 'stored'; reader: string }
  /** Its live value has another home — shown read-only, never stored here. */
  | {
      kind: 'elsewhere';
      live: string;
      home: string;
      editor_route: string | null;
      editor_label: string | null;
    };

export type ListedSetting = ManifestSettingDecl & { binding: SettingBinding };

export interface ListedModuleSettings {
  module_id: string;
  name: string;
  /**
   * 'bundled' (every project) | 'installed' (a catalog module) |
   * 'dev_passthrough' (a module under development, shown because
   * VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH is set — every project).
   */
  origin: 'bundled' | 'installed' | 'dev_passthrough';
  /** Projects its per-project settings may be edited for; `null` = all. */
  projects: string[] | null;
  settings: ListedSetting[];
  /**
   * The module's `provides` http_api entries, `base_url` RESOLVED by Rust
   * (`PlaceholderCtx::resolve`: `{hub_port}` → the running hub's port).
   */
  http_apis: ProvidedHttpApi[];
}

/** Rust `manifest::ProvidedHttpApi`. */
export interface ProvidedHttpApi {
  base_url: string;
  description: string;
}

/** One "Provides" line the panel shows for a module's HTTP API. */
export interface HttpApiLine {
  url: string;
  description: string | null;
}

/**
 * The HTTP APIs a module provides, as the panel shows them. The URL is the
 * resolved one; an entry whose URL still holds a `{placeholder}` (a token
 * the launcher does not know) is left out rather than shown wrong.
 */
export function httpApiLines(module: Pick<ListedModuleSettings, 'http_apis'>): HttpApiLine[] {
  return (module.http_apis ?? [])
    .filter((a) => a.base_url !== '' && !/\{[^}]*\}/.test(a.base_url))
    .map((a) => ({ url: a.base_url, description: a.description.trim() === '' ? null : a.description }));
}

/** Rust `module_gui::LiveSettingValue`. */
export interface LiveSettingValue {
  module_id: string;
  key: string;
  value: string | null;
  note: string | null;
}

/** True when the editor may write the setting (it is stored here). */
export function isEditable(setting: ListedSetting): boolean {
  return setting.binding.kind === 'stored';
}

/**
 * The launcher route that edits a setting bound elsewhere, with the picked
 * project filled in; `null` when there is no editor, or the route needs a
 * project and none is picked.
 */
export function editorHref(binding: SettingBinding, projectId: string): string | null {
  if (binding.kind !== 'elsewhere' || !binding.editor_route) return null;
  if (binding.editor_route.includes('{project_id}')) {
    if (projectId === '') return null;
    return binding.editor_route.split('{project_id}').join(encodeURIComponent(projectId));
  }
  return binding.editor_route;
}

/**
 * Whether a module's PER-PROJECT settings apply to `projectId`: a bundled
 * module is in every project; an installed module only where it is installed
 * and enabled — elsewhere nothing is offered (no orphan rows).
 */
export function moduleOffersProject(module: ListedModuleSettings, projectId: string): boolean {
  if (projectId === '') return false;
  return module.projects === null || module.projects.includes(projectId);
}

/** The live value shown for a setting bound elsewhere, if resolved. */
export function liveValueFor(
  values: LiveSettingValue[],
  moduleId: string,
  key: string,
): LiveSettingValue | null {
  return values.find((v) => v.module_id === moduleId && v.key === key) ?? null;
}

/** True when the module has a machine-wide setting the editor can change. */
export function hasEditableMachineWide(module: ListedModuleSettings): boolean {
  return module.settings.some((s) => isMachineWide(s) && isEditable(s));
}

/** Every module whose settings the editor lists (bundled + installed). */
export async function listModuleSettings(): Promise<ListedModuleSettings[]> {
  return invoke<ListedModuleSettings[]>('list_module_settings');
}

/** The live values of the settings bound elsewhere. */
export async function liveSettingValues(projectId: string): Promise<LiveSettingValue[]> {
  return invoke<LiveSettingValue[]>('module_setting_live_values', {
    projectId: projectId === '' ? null : projectId,
  });
}

/**
 * The `projectId` argument for `get_module_setting` / `set_module_setting`:
 * `null` for a machine-wide setting (the backend refuses a project for it),
 * the picked project otherwise (`''` when none is picked — the backend then
 * refuses, and the panel disables the field).
 */
export function settingProjectArg(
  decl: ManifestSettingDecl,
  projectId: string,
): string | null {
  return isMachineWide(decl) ? null : projectId;
}

/** The note an editable machine-wide setting carries: read at start. */
export function machineWideNote(module: { name: string }): string {
  return (
    `Machine-wide: one value for this computer, read by ${module.name} when it ` +
    `starts. Restart ${module.name} after a change for the new value to take effect.`
  );
}

/** The stored value, or the declared default when none is stored. */
export async function loadModuleSetting(
  moduleId: string,
  decl: ManifestSettingDecl,
  projectId: string,
): Promise<unknown> {
  const v = await invoke<unknown>('get_module_setting', {
    moduleId,
    controlId: decl.key,
    projectId: settingProjectArg(decl, projectId),
  });
  return v === null || v === undefined ? (decl.default ?? null) : v;
}

/**
 * Save one setting through the generic `set_module_setting` path. An invalid
 * value is refused HERE (the promise rejects with the reason and nothing is
 * sent); a valid one is still re-checked by the backend.
 */
export async function saveModuleSetting(
  moduleId: string,
  decl: ManifestSettingDecl,
  projectId: string,
  value: unknown,
): Promise<void> {
  const problem = checkSettingValue(decl, value);
  if (problem !== null) throw new Error(problem);
  await invoke('set_module_setting', {
    moduleId,
    controlId: decl.key,
    value,
    projectId: settingProjectArg(decl, projectId),
  });
}
