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

/** The `set` payload: `stored: false` means the choice was NOT saved. */
export interface ModelProviderSetResult {
  vendor: string;
  enabled: boolean;
  stored: boolean;
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
 * in the user's terms. Deliberately concrete about the three effects and about
 * the one thing that does NOT happen (nothing is deleted).
 */
export const PROVIDER_OFF_NOTICE =
  'Off is remembered on this machine: on the next update each project moves ' +
  'this provider\'s agent definitions to .claude/agents.disabled/ (nothing is ' +
  'deleted — switching it back on restores them) and its rows leave the ' +
  'model guidance in every CLAUDE.md.';

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
  return enabled
    ? `${row.display} is on again: its agent definitions come back on the next update and its rows return to the model guidance.`
    : `${row.display} is off. ${PROVIDER_OFF_NOTICE}`;
}

/** Should the card render at all? (No gateway ⇒ the card is meaningless.) */
export function cardVisible(payload: ModelProvidersPayload | null): boolean {
  return Boolean(payload && payload.gateway_available && payload.providers.length > 0);
}

/** Is any provider currently switched off? (Drives the standing notice.) */
export function anyDisabled(payload: ModelProvidersPayload | null): boolean {
  return Boolean(payload && payload.providers.some((p) => !p.enabled));
}