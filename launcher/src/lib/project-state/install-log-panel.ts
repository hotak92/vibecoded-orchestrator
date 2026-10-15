// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 (W5-B C8): derivations for the project Settings "Install log"
// panel. The log itself is read by the `read_install_log` Tauri command
// (the orchestrator root's `state/logs/install.jsonl` — see
// commands/installer/install_log.rs; the panel names the path it read).
// Pure so vitest can pin the tail derivation and the copy payload.

/** Mirrors `installer::InstallEvent` (opaque `data` kept out — the panel
 *  renders ts/actor/step/phase/detail only). */
export interface InstallEventView {
  ts: string;
  actor: string;
  step: string;
  phase: string;
  detail: string;
}

/** Mirrors `installer::InstallState`. */
export interface InstallStateView {
  session_started: string | null;
  completed_steps: string[];
  skipped_steps: string[];
  failed_steps: [string, string][];
  last_event_ts: string | null;
  looks_complete: boolean;
}

/** Mirrors `installer::InstallLog`. */
export interface InstallLogView {
  events: InstallEventView[];
  state_summary: InstallStateView;
  log_path: string;
  exists: boolean;
}

/** How many events the panel renders by default (the TAIL). */
export const TAIL_SIZE = 30;

/** The last `TAIL_SIZE` events, oldest first (chronological reading). */
export function tailEvents(log: InstallLogView | null, tail = TAIL_SIZE): InstallEventView[] {
  if (!log) return [];
  return log.events.slice(-tail);
}

/** Unique key for one event line in the panel's keyed `{#each}`.
 *
 * v0.2.103: POSITION is part of identity. Real install logs contain
 * duplicate (ts, step, phase) triples — multiple actors finish the same
 * step within the same second, and the same actor can emit twice for one
 * step (the owner's own log: 3,481 distinct triples repeat, 9,177
 * surplus rows among 20,305 events, and 4 colliding keys inside the
 * rendered 30-event tail) — and Svelte 5 THROWS `each_key_duplicate` on
 * colliding keys. That throw aborted the Settings tab's render
 * mid-flush and left the stale "Loading…" DOM in place (the
 * owner-reported "Settings tab shows Loading… forever"). The index
 * disambiguates; ts/step/phase stay in the key so it still reads as
 * "this event" in devtools. */
export function eventKey(e: InstallEventView, index: number): string {
  return `${index}:${e.ts}:${e.step}:${e.phase}`;
}

/** One line per event, for the Copy button's payload. */
export function eventLine(e: InstallEventView): string {
  return `${e.ts} ${e.actor} ${e.step} ${e.phase} ${e.detail}`;
}

/** The full copy payload: the summary block, then every event line. */
export function copyText(log: InstallLogView | null): string {
  if (!log) return '';
  const s = log.state_summary;
  const head = [
    `install log: ${log.log_path}`,
    `session started: ${s.session_started ?? '(none)'}`,
    `looks complete: ${s.looks_complete}`,
    `failed steps: ${s.failed_steps.map(([step, err]) => `${step} (${err})`).join('; ') || '(none)'}`,
    `events: ${log.events.length}`,
  ].join('\n');
  return [head, ...log.events.map(eventLine)].join('\n');
}

/** The one-line verdict the panel heads with. */
export function summaryLine(log: InstallLogView | null): string {
  if (!log) return 'Install log not loaded.';
  if (!log.exists) return 'No install log yet (fresh install — nothing has run).';
  const s = log.state_summary;
  if (s.failed_steps.length > 0) {
    return `Last install session has ${s.failed_steps.length} failed step(s).`;
  }
  if (s.looks_complete) return 'Last install session completed.';
  return 'Last install session did not reach a complete state.';
}
