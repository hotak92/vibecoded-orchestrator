// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.105 — the Home page's "Model providers" card: the machine-wide
// per-provider switch.
//
// The SETTING is `launcher.db` `app_state` `model_providers.disabled`, whose
// only reader and writer is `vco_lib.model_providers` (rung A of the
// cross-language ladder). The two commands here are the transport: Rust shells
// that helper and passes its JSON through, so this file never re-derives a
// vendor list, a key state or a row encoding.
//
// Everything a test can decide without a DOM lives here — the key-state
// wording, the one-line explanation of what switching a provider off does, and
// the two failure sentences — so the Svelte card is markup plus a switch. Key
// STATES are shown as words; the row also carries the vendor's declared
// secret-key NAMES (public registry strings), and no key VALUE ever reaches
// this payload.

import { invoke } from '$lib/tauri';

/** The live answer of a vendor's key probe, exactly as the helper reports it. */
export type ProviderKeyState = 'present' | 'absent' | 'unavailable' | 'unknown';

/** One gateway provider as the Home card draws it. */
export interface ModelProviderRow {
  vendor_id: string;
  /** Human name from the gateway's vendor row ("Kimi Code", "Z.ai"). */
  display: string;
  /** The user's machine-wide switch. */
  enabled: boolean;
  key_state: ProviderKeyState;
  /** Declared key NAMES (never values) — the registry's own spellings. */
  secret_key_names: string[];
  docs_url: string;
  /** Host of the vendor's upstream, so the card can say where requests go. */
  upstream_host: string;
  /** Gateway agent definitions this vendor owns (file stems). */
  definitions: string[];
}

/** The `status` payload. */
export interface ModelProvidersPayload {
  disabled: string[];
  /** `false` when the vendor registry itself could not be read. */
  gateway_available: boolean;
  reason?: string;
  providers: ModelProviderRow[];
  app_state_key?: string;
}

/** One project's outcome inside the apply summary. */
export interface ProviderApplyProjectRow {
  project: string;
  folder: string;
  status: 'ok' | 'skipped' | 'held' | 'failed';
  parked: string[];
  already_parked: string[];
  kept: string[];
  restored: string[];
  /** Restored definitions parked under an older release and brought to this
   *  release's version the way a bundle update does (a copy that matched no
   *  shipped version is backed up first). */
  refreshed?: string[];
  notes: string[];
  error: string;
}

/**
 * What the immediate apply did (v0.2.106): a stored `set` parks or restores
 * the vendor's agent definitions in the orchestrator root and every
 * registered project AT ONCE, and this is the per-project record of it.
 * `held` means the vendor was switched ON but its key does not resolve, so
 * the definitions stay parked — the same decision the delivery gate makes,
 * and the bundle update's restore makes the same one (one rule,
 * `module_gated_delivery.restore_gate`), so they return with the first
 * update or switch after the key resolves.
 */
export interface ProviderApplySummary {
  vendor: string;
  enabled: boolean;
  held: boolean;
  held_reason: string;
  parked_projects: number;
  restored_projects: number;
  failed_projects: number;
  /** Non-empty when the launcher's project list could not be read: only the
   *  orchestrator root was reached (counted in `failed_projects`). */
  registry_error?: string;
  projects: ProviderApplyProjectRow[];
}

/** The `set` payload: `stored: false` means the choice was NOT saved. */
export interface ModelProviderSetResult {
  vendor: string;
  enabled: boolean;
  stored: boolean;
  /** Present on a stored change: what the agents leg did, per project. */
  agents?: ProviderApplySummary;
}

/** Every gateway provider with its switch and its key state. */
export async function getModelProviders(): Promise<ModelProvidersPayload> {
  return invoke<ModelProvidersPayload>('model_providers_status');
}

/**
 * Switch ONE provider on or off machine-wide.
 *
 * A stored change fires the guidance refresh on the Rust side (every
 * registered project's CLAUDE.md follows the switch); a `stored: false`
 * answer must be shown as "not saved", never as applied.
 */
export async function setModelProviderEnabled(
  vendor: string,
  enabled: boolean,
): Promise<ModelProviderSetResult> {
  return invoke<ModelProviderSetResult>('model_providers_set', { vendor, enabled });
}

/**
 * The one line the card shows when a provider is off: what the choice means,
 * in the user's terms. Deliberately concrete about the four effects and about
 * the one thing that does NOT happen (nothing is deleted). All four apply AT
 * ONCE (v0.2.106) — no restart, no bundle update.
 */
export const PROVIDER_OFF_NOTICE =
  'Off is remembered on this machine and applies at once: this provider\'s ' +
  'agent definitions move to .claude/agents.disabled/ in every project ' +
  '(nothing is deleted — switching it back on restores them), its rows leave ' +
  'the model guidance in every CLAUDE.md, and the running gateway stops ' +
  'serving its models within seconds.';

/**
 * The one sentence reporting what the AGENTS leg of a stored toggle did —
 * parked/restored counts, the key-held case, and any per-project failure.
 * A stored change with no summary means the helper answered before the apply
 * existed; say the setting landed and nothing more.
 */
export function applyOutcomeLabel(result: ModelProviderSetResult): string {
  const a = result.agents;
  if (!a) {
    return 'The agent follow-up did not report back; the setting itself is saved.';
  }
  if (a.held) {
    return (
      `Its agent definitions stay parked (${a.held_reason || 'the key does not resolve'}) ` +
      '— they come back with the first bundle update (or provider switch) after the key resolves.'
    );
  }
  const moved = result.enabled ? a.restored_projects : a.parked_projects;
  const verb = result.enabled ? 'restored in' : 'parked in';
  let out =
    moved > 0
      ? `Its agent definitions were ${verb} ${moved} ${moved === 1 ? 'project' : 'projects'} right away`
      : 'No delivered agent definitions needed moving';
  const refreshed = result.enabled
    ? a.projects.reduce((n, p) => n + (p.refreshed?.length ?? 0), 0)
    : 0;
  if (refreshed > 0) {
    out +=
      ` — ${refreshed} of them came from an older release and ` +
      `${refreshed === 1 ? 'was' : 'were'} brought to this release's version, as a bundle ` +
      'update would (a copy that matched no version VCO shipped is backed up under ' +
      '.claude/backups/bundle-adoptions/ first)';
  }
  if (a.failed_projects > 0) {
    out +=
      `, but the follow-up failed in ${a.failed_projects} ` +
      `${a.failed_projects === 1 ? 'project' : 'projects'} — flip the switch again or let the next update finish it`;
  }
  if (a.registry_error) {
    out += ` (${a.registry_error})`;
  }
  return out + '.';
}

/** Short wording + tone for one key state. Words only — never a value. */
export function keyStateLabel(
  state: ProviderKeyState,
): { text: string; tone: 'ok' | 'muted' | 'warn' } {
  switch (state) {
    case 'present':
      return { text: 'key detected', tone: 'ok' };
    case 'absent':
      return { text: 'no key', tone: 'muted' };
    case 'unavailable':
      return { text: 'key store unavailable', tone: 'warn' };
    default:
      return { text: 'key state unknown', tone: 'warn' };
  }
}

/** "3 agents" / "1 agent" / "no agent definitions". */
export function definitionsLabel(row: ModelProviderRow): string {
  const n = row.definitions.length;
  if (n === 0) return 'no agent definitions';
  return n === 1 ? '1 agent' : `${n} agents`;
}

/** The declared key NAMES, for the row's detail line ("kimi_api_key or …"). */
export function secretNamesLabel(row: ModelProviderRow): string {
  if (row.secret_key_names.length === 0) return 'no key name declared';
  return row.secret_key_names.join(' or ');
}

/**
 * What to tell the user after a toggle, in one sentence.
 *
 * Three outcomes are kept apart: the helper answered `stored: false` (the
 * database refused the write — nothing changed); the call itself FAILED
 * (`result === null`: no Python, a timeout, unparseable output — the row may
 * or may not have been written, so the caller re-reads the status and passes
 * what it now says as `nowEnabled`); and a stored change.
 */
export function toggleNote(
  row: ModelProviderRow,
  enabled: boolean,
  result: ModelProviderSetResult | null,
  nowEnabled: boolean | null = null,
): string {
  if (result === null) {
    if (nowEnabled === enabled) {
      return `The launcher could not confirm the change for ${row.display}, but the saved setting now reads ${enabled ? 'on' : 'off'}.`;
    }
    if (nowEnabled === null) {
      return `Could not ask the launcher to change ${row.display}, and its saved state could not be read back. Check the card again in a moment.`;
    }
    return `Could not ask the launcher to change ${row.display}: the saved setting is still ${nowEnabled ? 'on' : 'off'}.`;
  }
  if (result.stored !== true) {
    return `Could not save the change for ${row.display}: the launcher database could not be written. Nothing was changed.`;
  }
  const outcome = applyOutcomeLabel(result);
  return enabled
    ? `${row.display} is on again: its rows return to the model guidance and the gateway serves its models again. ${outcome}`
    : `${row.display} is off. ${outcome} ${PROVIDER_OFF_NOTICE}`;
}

/** Should the card render at all? (No gateway ⇒ the card is meaningless.) */
export function cardVisible(payload: ModelProvidersPayload | null): boolean {
  return Boolean(payload && payload.gateway_available && payload.providers.length > 0);
}

/** Is any provider currently switched off? (Drives the standing notice.) */
export function anyDisabled(payload: ModelProvidersPayload | null): boolean {
  return Boolean(payload && payload.providers.some((p) => !p.enabled));
}