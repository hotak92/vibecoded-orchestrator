// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.103 (plan item 12) — the model-guidance status for Preferences →
// Secrets: which model providers were detected on this machine and when the
// per-project model guidance was last refreshed.
//
// The answers are computed in Python (`python -m vco_lib.model_guidance
// status`, transported verbatim by the Rust `get_model_guidance_status`).
// This module only invokes it (injected `invoke`, so tests drive the real
// call) and turns the payload into sentences — no provider logic is
// re-derived here.

export interface ModelGuidanceVendor {
  /** present | absent | unavailable (store busy) | unknown (no gateway). */
  state: string;
  /** The last-known record's answer, or null when none was ever recorded. */
  last_known: boolean | null;
  display: string;
}

export interface ModelGuidanceRefresh {
  at: string;
  trigger: string;
  added: string[];
  removed: string[];
  projects_changed: number;
  root_changed: boolean;
}

export interface ModelGuidanceStatusPayload {
  vendors: Record<string, ModelGuidanceVendor>;
  last_refresh: ModelGuidanceRefresh | null;
  recorded_at: string | null;
  registered_projects: number | null;
}

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

export function loadModelGuidanceStatus(deps: InvokeDeps): Promise<ModelGuidanceStatusPayload> {
  return deps.invoke<ModelGuidanceStatusPayload>('get_model_guidance_status');
}

export interface VendorRow extends ModelGuidanceVendor {
  id: string;
}

/** Stable display order (alphabetical by display name). */
export function vendorRows(payload: ModelGuidanceStatusPayload): VendorRow[] {
  return Object.entries(payload.vendors)
    .map(([id, v]) => ({ id, ...v }))
    .sort((a, b) => a.display.localeCompare(b.display));
}

export type VendorTone = 'ok' | 'off' | 'warn';

/** What one vendor's row says, and the chip tone it renders with. */
export function vendorStateText(v: ModelGuidanceVendor): { tone: VendorTone; label: string; text: string } {
  switch (v.state) {
    case 'present':
      return { tone: 'ok', label: 'detected', text: 'key resolves — its models are listed in the guidance' };
    case 'absent':
      return { tone: 'off', label: 'no key', text: 'no key on this machine — its models stay out of the guidance' };
    case 'unavailable':
      return {
        tone: 'warn',
        label: 'store busy',
        text:
          v.last_known === true
            ? 'the secret store could not be read just now — the guidance keeps the last-known rows'
            : 'the secret store could not be read just now — nothing was changed',
      };
    default:
      return { tone: 'off', label: 'unknown', text: 'the model gateway is not installed here' };
  }
}

/** "2026-10-14 10:05" from an ISO timestamp (local reading, no library). */
export function formatWhen(iso: string): string {
  const m = iso.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : iso;
}

/** The one line under the vendor rows: when the guidance last refreshed. */
export function lastRefreshText(r: ModelGuidanceRefresh | null): string {
  if (!r || !r.at) {
    return 'The guidance has not been refreshed yet — it refreshes at the next launcher startup, key change, or install/update.';
  }
  const changes: string[] = [];
  if (r.added.length > 0) changes.push(`${r.added.join(', ')} rows added`);
  if (r.removed.length > 0) changes.push(`${r.removed.join(', ')} rows removed`);
  const scope =
    r.projects_changed > 0 || r.root_changed
      ? `${r.projects_changed} project(s) re-rendered`
      : 'already current, nothing rewritten';
  const suffix = changes.length > 0 ? ` — ${changes.join('; ')}, ${scope}` : ` — ${scope}`;
  return `Guidance last refreshed ${formatWhen(r.at)} (${r.trigger})${suffix}.`;
}
