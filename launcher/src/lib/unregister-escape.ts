// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The unregister STOP and its explicit escape (owner ruling, v0.2.97 review
// R5 F39).
//
// `delete_project_v2` stops when a secret value VCO wrote could not be removed
// (a read-only folder, a full disk, a file another program holds): once the
// project is gone VCO can no longer check that value, so the stop keeps it
// removable. The stop stays the DEFAULT. Its second action — "Unregister
// anyway — leave these values" — re-runs the unregister with
// `leaveUnremovable: true`: it finishes and writes a note listing each key and
// file to clean by hand (names only, never a value).
//
// Pure (no Svelte, no Tauri) so the flow is unit-tested
// (`unregister-escape.test.ts`); the components supply `del` (the store's
// `projects.delete`) and `confirmLeave` (their dialog).

import type { UnregisterOptions, UnregisterReport } from '$lib/types/launcher';

/** How the Rust stop error opens. MUST MATCH `UNREGISTER_STOPPED_PREFIX` in
 *  `launcher/src-tauri/src/commands/projects_v2/unregister_strip.rs`. */
export const UNREGISTER_STOPPED_PREFIX = 'Unregister stopped';

/** The second action's label — the Rust stop message names it verbatim. */
export const UNREGISTER_ANYWAY_LABEL = 'Unregister anyway — leave these values';

/** The unregister every GUI surface starts from — the settings page's
 *  untouched checkboxes, the project list's quick unregister, the wizard's
 *  re-create. Sent EXPLICITLY (review R6 F46: `null` used to reach a
 *  different Rust default than `{}`). MUST MATCH `impl Default for
 *  UnregisterOptions` in `launcher/src-tauri/src/commands/projects_v2.rs`. */
export const DEFAULT_UNREGISTER_OPTIONS: Readonly<{
  purgeLauncherFiles: boolean;
  purgeCollections: boolean;
}> = Object.freeze({ purgeLauncherFiles: true, purgeCollections: false });

/** The error text of a failed invoke (Tauri rejects with a string). */
export function errorText(e: unknown): string {
  if (typeof e === 'string') return e;
  if (e instanceof Error) return e.message;
  return String(e);
}

/** True for the unregister STOP (not for any other failure). */
export function isUnregisterStopped(e: unknown): boolean {
  return errorText(e).startsWith(UNREGISTER_STOPPED_PREFIX);
}

export type UnregisterOutcome =
  | { kind: 'done'; report: UnregisterReport; leftAnyway: boolean }
  | { kind: 'kept'; message: string };

/**
 * Run an unregister; on the STOP, ask `confirmLeave(message)` whether to
 * "Unregister anyway". `kept` = the user chose to keep the project (the stop
 * stands). Any other failure is re-thrown unchanged. The first call NEVER
 * carries `leaveUnremovable` — the escape is only ever the second action, and
 * it re-sends the SAME options with only `leaveUnremovable` changed.
 */
export async function runUnregister(
  del: (options: UnregisterOptions) => Promise<UnregisterReport>,
  options: UnregisterOptions,
  confirmLeave: (message: string) => Promise<boolean>,
): Promise<UnregisterOutcome> {
  try {
    return { kind: 'done', report: await del({ ...options, leaveUnremovable: false }), leftAnyway: false };
  } catch (e) {
    if (!isUnregisterStopped(e)) throw e;
    const message = errorText(e);
    if (!(await confirmLeave(message))) return { kind: 'kept', message };
    const report = await del({ ...options, leaveUnremovable: true });
    return { kind: 'done', report, leftAnyway: true };
  }
}

/** The project list's quick unregister: the same unregister as the settings
 *  page's defaults ({@link DEFAULT_UNREGISTER_OPTIONS}) — the stop can fire,
 *  and its escape re-runs with the same options (review R6 F46). */
export function quickUnregister(
  del: (options: UnregisterOptions) => Promise<UnregisterReport>,
  confirmLeave: (message: string) => Promise<boolean>,
): Promise<UnregisterOutcome> {
  return runUnregister(del, { ...DEFAULT_UNREGISTER_OPTIONS }, confirmLeave);
}
