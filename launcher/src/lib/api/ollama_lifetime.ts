// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106 (owner ruling 2026-11-11) — "the hub manages unloading": the GUI
// half of how long Ollama keeps a model loaded.
//
//   * Preferences → Ollama model unloading: the ONE machine-wide idle time
//     (`get_ollama_idle_unload` / `set_ollama_idle_unload`, stored in
//     launcher.db `app_state[ollama.idle_unload]`, read by the hub on every
//     check). Validation lives in Rust (`vct_launcher_core::services::
//     ollama_lifetime`, the parser the hub resolves with) — this module only
//     offers presets and shows the backend's answer.
//   * Services → Ollama models: the hub's list of loaded models and its
//     "Unload now" route (`ollama_models_status` / `ollama_unload_model`).
//
// Every decision the two components make is here, so vitest can drive it
// with an injected `invoke`.

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

/** Mirrors `commands::ollama_lifetime_prefs::OllamaIdleState`. */
export interface OllamaIdleState {
  stored: string | null;
  effective: string;
  effective_secs: number;
  stored_invalid: boolean;
  default_idle: string;
  max_idle: string;
  caller_keep_alive: string;
  env_override: string | null;
}

export interface IdlePreset {
  value: string;
  label: string;
}

/** The value the Dropdown uses for "type your own duration". */
export const CUSTOM_PRESET = 'custom';

/** The choices the Preferences field offers. Values are what the backend
 *  stores (it normalises, so `presetFor` compares against these). The
 *  longest is the hub's maximum; "never" is deliberately absent — keeping a
 *  model loaded for good is `VCO_OLLAMA_KEEP_ALIVE=-1`, which the hub then
 *  leaves alone. */
export const IDLE_PRESETS: IdlePreset[] = [
  { value: '0', label: 'Unload as soon as idle — slow: reloads on almost every lookup (~2 s each)' },
  { value: '2m', label: '2 minutes' },
  { value: '5m', label: '5 minutes' },
  { value: '10m', label: '10 minutes (default)' },
  { value: '30m', label: '30 minutes (longest)' },
  { value: CUSTOM_PRESET, label: 'Other duration…' },
];

export function loadOllamaIdle(deps: InvokeDeps): Promise<OllamaIdleState> {
  return deps.invoke<OllamaIdleState>('get_ollama_idle_unload');
}

/** Store `value` (a preset value or a typed duration). `null` or an empty
 *  string restores the default. Rejects with the backend's reason when the
 *  value is not a duration the hub accepts. */
export function saveOllamaIdle(deps: InvokeDeps, value: string | null): Promise<OllamaIdleState> {
  const trimmed = value?.trim() ?? '';
  return deps.invoke<OllamaIdleState>('set_ollama_idle_unload', {
    value: trimmed === '' ? null : trimmed,
  });
}

/** Which preset the dropdown shows for a state: the stored value's preset,
 *  `custom` for any other stored value, the default's preset when unset. */
export function presetFor(state: OllamaIdleState): string {
  const current = state.stored === null || state.stored_invalid ? state.default_idle : state.effective;
  return IDLE_PRESETS.some((p) => p.value === current && p.value !== CUSTOM_PRESET)
    ? current
    : CUSTOM_PRESET;
}

/** A duration as people say it: "0" → "as soon as idle", "1m30s" → "1 min 30 s". */
export function humanDuration(d: string): string {
  if (d === '0') return 'as soon as idle';
  const parts: string[] = [];
  for (const m of d.matchAll(/(\d+)([hms])/g)) {
    const unit = m[2] === 'h' ? 'h' : m[2] === 'm' ? 'min' : 's';
    parts.push(`${m[1]} ${unit}`);
  }
  return parts.length ? parts.join(' ') : d;
}

/** The sentence under the field: what is in force and why. */
export function idleSummary(state: OllamaIdleState): string {
  const inForce =
    state.effective === '0'
      ? 'The hub unloads a model as soon as no VCO request is using it.'
      : `The hub unloads a model after ${humanDuration(state.effective)} without a VCO request.`;
  const origin =
    state.stored === null
      ? ' (default)'
      : state.stored_invalid
        ? ` The stored value "${state.stored}" is not a duration the hub accepts, so the default applies.`
        : '';
  return inForce + origin;
}

/** The warning shown when the launcher's environment sets the override. */
export function envOverrideNote(state: OllamaIdleState): string | null {
  if (state.env_override === null) return null;
  const v = state.env_override.trim();
  if (v === '') {
    return 'VCO_OLLAMA_KEEP_ALIVE is set but empty in this launcher\'s environment: processes that inherit it send no keep_alive, so Ollama\'s own server setting decides for the models they load, not this setting.';
  }
  return `VCO_OLLAMA_KEEP_ALIVE=${v} is set in this launcher's environment. VCO processes that inherit it send that keep_alive themselves, and the hub leaves the models they load to Ollama's own timer — this setting applies only to models loaded without it. Remove the variable to let the hub manage every model.`;
}

// ── Services → Ollama models ────────────────────────────────────────────

export type Holder = 'hub' | 'caller' | 'busy';

/** Mirrors `vct_hub::ollama_lifetime::ModelView`. */
export interface OllamaModelView {
  name: string;
  size: number;
  size_vram: number;
  holder: Holder;
  ollama_expires_at: string | null;
  last_use: string | null;
  unload_at: string | null;
  unload_requested: boolean;
}

export interface OllamaUnloadEvent {
  model: string;
  at: string | null;
  reason: 'idle' | 'manual' | string;
  ok: boolean;
  error: string | null;
}

/** Mirrors `vct_hub::ollama_lifetime::ModelsStatus`. */
export interface OllamaModelsStatus {
  reachable: boolean;
  error: string | null;
  idle_secs: number;
  idle: string;
  idle_stored_invalid: boolean;
  caller_keep_alive: string;
  poll_secs: number;
  models: OllamaModelView[];
  recent_unloads: OllamaUnloadEvent[];
}

export function loadOllamaModels(deps: InvokeDeps): Promise<OllamaModelsStatus> {
  return deps.invoke<OllamaModelsStatus>('ollama_models_status');
}

export function unloadOllamaModel(deps: InvokeDeps, model: string): Promise<unknown> {
  return deps.invoke('ollama_unload_model', { model });
}

/** Bytes as GB/MB with one decimal. */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 MB';
  const gb = n / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(n / 1024 ** 2).toFixed(0)} MB`;
}

/** Where the model's memory is: all in VRAM, all in RAM, or split. */
export function memoryLine(m: OllamaModelView): string {
  const vram = Math.min(m.size_vram, m.size);
  const ram = Math.max(m.size - vram, 0);
  if (vram === 0) return `${formatBytes(m.size)} RAM`;
  if (ram === 0) return `${formatBytes(m.size)} VRAM`;
  return `${formatBytes(m.size)} (${formatBytes(vram)} VRAM + ${formatBytes(ram)} RAM)`;
}

function clock(iso: string | null, now: Date): string {
  if (!iso) return '—';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  const mins = Math.round((t.getTime() - now.getTime()) / 60000);
  const hhmm = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (mins <= 0) return `${hhmm} (due now)`;
  return `${hhmm} (in ${mins} min)`;
}

/** What the card says about when a model goes, and who decides. */
export function holderLine(m: OllamaModelView, now: Date = new Date()): string {
  if (m.holder === 'busy') return 'In use — a request is running; the hub will not unload it now.';
  if (m.holder === 'caller') {
    const exp = m.ollama_expires_at ? new Date(m.ollama_expires_at).getTime() : NaN;
    const when =
      Number.isFinite(exp) && exp - now.getTime() > 365 * 24 * 3600 * 1000
        ? 'never on its own (use Unload now)'
        : `at ${clock(m.ollama_expires_at, now)}`;
    return `Kept by its caller: the last request asked Ollama for its own keep-alive (VCO_OLLAMA_KEEP_ALIVE or a non-VCO client), so Ollama unloads it ${when} — the hub leaves it alone.`;
  }
  if (m.unload_requested) return 'Unloading…';
  return `The hub unloads it at ${clock(m.unload_at, now)} unless a VCO request uses it first.`;
}
