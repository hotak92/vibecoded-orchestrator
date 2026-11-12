// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106 (owner ruling 2026-11-12) — Preferences → KG Summaries → "Local
// summary model": the opt-out for VCO's one local text-generation model.
//
// Every decision the panel makes lives here (vitest-pinned, injected
// `invoke`); `LocalSummaryModel.svelte` is markup over it. The setting is an
// `app_state` bool written through the generic `app_state_set_bool`; the
// READ side (env override → this key → ON), the presence/size probe and the
// guarded removal are one Python home, `vco_lib.local_summary_model`, reached
// through `get_local_summary_model_status` / `remove_local_summary_model`.

/** MUST MATCH vco_lib/local_summary_model.py APP_STATE_KEY. */
export const APP_STATE_KEY_LOCAL_SUMMARY_MODEL = 'kg_summary_local_model_enabled';
/** MUST MATCH vco_lib/local_summary_model.py ENV_SWITCH. */
export const LOCAL_SUMMARY_ENV = 'VCT_LOCAL_SUMMARY_MODEL';

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

/** Mirror of `vco_lib.local_summary_model.status` JSON. */
export interface LocalSummaryStatus {
  ok: boolean;
  enabled: boolean;
  /** `env` | `preferences` | `default` */
  source: string;
  invalid_env: string | null;
  env_var: string;
  model: string;
  model_source: string;
  /** `null` = Ollama did not answer (unknown is not "absent"). */
  present: boolean | null;
  size_bytes: number | null;
  approx_size_bytes: number | null;
  ollama_url: string;
  /** `null` = not checked. */
  vco_owns_ollama: boolean | null;
  owner_detail: string;
}

export function loadLocalSummaryStatus(deps: InvokeDeps): Promise<LocalSummaryStatus> {
  return deps.invoke<LocalSummaryStatus>('get_local_summary_model_status');
}

/** Persist the toggle. The env override (if any) still wins — the panel
 *  says so via `envOverrideNote`. */
export function saveLocalSummaryEnabled(deps: InvokeDeps, enabled: boolean): Promise<void> {
  return deps.invoke<void>('app_state_set_bool', {
    key: APP_STATE_KEY_LOCAL_SUMMARY_MODEL,
    value: enabled,
  });
}

/** "Download now": `true` when the background ensure started. */
export function downloadLocalSummaryModel(deps: InvokeDeps): Promise<boolean> {
  return deps.invoke<boolean>('download_local_summary_model');
}

/** Ask, then remove. Returns `null` when the user declined (nothing was
 *  invoked), else the backend's result. */
export async function removeLocalSummaryModel(
  deps: InvokeDeps & { confirm: (message: string) => boolean },
  st: LocalSummaryStatus,
): Promise<unknown | null> {
  if (!canRemove(st)) return null;
  if (!deps.confirm(removeConfirmText(st))) return null;
  return deps.invoke('remove_local_summary_model');
}

export function formatGb(n: number | null | undefined): string {
  if (!n || n <= 0) return 'size unknown';
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** The size to show: what Ollama reports for the copy it holds, else the
 *  tier table's nominal size. */
export function sizeLabel(st: LocalSummaryStatus): string {
  const n = st.size_bytes ?? st.approx_size_bytes;
  return n ? `~${formatGb(n)}` : 'size unknown';
}

/** The toggle's help line (owner's wording). */
export function toggleDescription(st: LocalSummaryStatus): string {
  return (
    `Downloads ${st.model} (${sizeLabel(st)}) as a fallback when Claude can't summarise. ` +
    `Turn off to save disk; summaries then use Claude/cloud only.`
  );
}

/** The env override beats the toggle: say so instead of a toggle that lies. */
export function envOverrideNote(st: LocalSummaryStatus): string | null {
  if (st.invalid_env) {
    return `${st.env_var}=${st.invalid_env} is not on/off and is ignored.`;
  }
  if (st.source !== 'env') return null;
  return (
    `${st.env_var}=${st.enabled ? 'on' : 'off'} is set in the environment and wins over ` +
    `this toggle.`
  );
}

/** The one-line state under the toggle. */
export function presenceLine(st: LocalSummaryStatus): string {
  if (st.present === null) return `Ollama did not answer — cannot tell whether ${st.model} is downloaded.`;
  if (st.present) return `${st.model} is downloaded (${sizeLabel(st)}).`;
  return st.enabled
    ? `${st.model} is not downloaded yet — the next install/update downloads it.`
    : `${st.model} is not downloaded.`;
}

/** Removal is offered only when it can succeed and makes sense: the switch
 *  is OFF (else the next update re-downloads it), the model is on disk, and
 *  VCO owns that Ollama (a foreign Ollama is the user's to manage). */
export function canRemove(st: LocalSummaryStatus): boolean {
  return !st.enabled && st.present === true && st.vco_owns_ollama === true;
}

/** Why removal is not offered while the model is on disk and the switch is
 *  off — so the user is never left guessing. */
export function removeUnavailableNote(st: LocalSummaryStatus): string | null {
  if (st.enabled || st.present !== true || st.vco_owns_ollama === true) return null;
  return (
    `This Ollama is not run by VCO (${st.owner_detail}), so VCO does not delete models from it — ` +
    `use \`ollama rm ${st.model}\` there if you want the space back.`
  );
}

export function removeLabel(st: LocalSummaryStatus): string {
  return `Remove the downloaded model (frees ${sizeLabel(st)})`;
}

export function removeConfirmText(st: LocalSummaryStatus): string {
  return (
    `Delete ${st.model} (${sizeLabel(st)}) from VCO's Ollama?\n\n` +
    `KG summaries will use Claude/cloud only. Turning the local summary model back on ` +
    `downloads it again.`
  );
}

/** "Download now" is offered when the switch is ON and Ollama answered
 *  without holding the model. */
export function canDownload(st: LocalSummaryStatus): boolean {
  return st.enabled && st.present === false;
}

/** The finished "Download now" run (`dual_flags::MODEL_ENSURE_EVENT`,
 *  host-wide scope), as one line naming THIS model. */
export function downloadOutcomeText(
  outcome: { ok: boolean; pulled: string[]; present: string[]; failed: Record<string, string>; error: string | null },
  model: string,
): { ok: boolean; text: string } {
  const why = outcome.failed?.[model];
  if (why) return { ok: false, text: `Could not download ${model}: ${why}.` };
  if (!outcome.ok) return { ok: false, text: `The model download failed: ${outcome.error ?? 'unknown error'}.` };
  if (outcome.pulled.includes(model)) return { ok: true, text: `${model} downloaded.` };
  if (outcome.present.includes(model)) return { ok: true, text: `${model} is already downloaded.` };
  return { ok: false, text: `${model} was not downloaded — the local summary model may be turned off.` };
}
