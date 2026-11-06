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

/** The REMOVE flow's options (v0.2.106): the owner's definition of removing
 *  a project — everything the settings page's default unregister removes
 *  PLUS the project's own Weaviate collections. Used by the ONE shared
 *  Remove flow (`ProjectRemoveFlow.svelte`) that both the Projects-page card
 *  and the top-bar selector open; `runUnregister` supplies
 *  `leaveUnremovable: false` on the first call. */
export const REMOVE_UNREGISTER_OPTIONS: Readonly<{
  purgeLauncherFiles: boolean;
  purgeCollections: boolean;
}> = Object.freeze({ purgeLauncherFiles: true, purgeCollections: true });

/** Whether a project may be removed from the GUI: every project except the
 *  orchestrator root (its `.claude/` is the live install, and the backend
 *  refuses to purge the orchestrator clone). The project card reads it; the
 *  top-bar selector hides its whole row-action strip (rename + remove) for
 *  the same `orchestrator_root` host. */
export function isRemovableProject(project: { host?: string | null }): boolean {
  return project.host !== 'orchestrator_root';
}

/** How many names a detail line lists before "and N more". */
export const REPORT_NAMES_SHOWN = 8;

function names(list: readonly string[]): string {
  const shown = list.slice(0, REPORT_NAMES_SHOWN).join(', ');
  const more = list.length - REPORT_NAMES_SHOWN;
  return more > 0 ? `${shown} and ${more} more` : shown;
}

/**
 * The ONE wording of an unregister's result, for every surface that shows it
 * (the shared Remove flow and the Settings → Danger zone): a one-line
 * `summary` with counts, and one `details` line per non-empty list — what was
 * kept because the user changed it, what was left as theirs, and VCO's
 * entries taken out of files that stay. Names only, never a value.
 * `verb` is the surface's own word ("Removed" / "Unregistered").
 */
export function describeUnregisterReport(
  report: UnregisterReport,
  verb: string,
): { summary: string; details: string[] } {
  const parts: string[] = [];
  const count = (n: number, what: string) => {
    if (n > 0) parts.push(`${n} ${what}`);
  };
  count(report.filesPurged.length, 'files removed');
  count(report.settingsEntriesRemoved.length, 'settings entries removed');
  count(report.keysPurgedFromEnv.length, 'env keys cleaned');
  count(report.collectionsDropped.length, 'collections dropped');
  count(report.filesKeptModified.length, 'files you changed kept');
  const summary = parts.length > 0
    ? `${verb} "${report.projectName}" — ${parts.join(', ')}`
    : `${verb} "${report.projectName}"`;
  const details: string[] = [];
  if (report.filesKeptModified.length > 0) {
    details.push(`Kept because you changed them: ${names(report.filesKeptModified)}`);
  }
  if (report.keptAsYours.length > 0) {
    details.push(`Left in place: ${names(report.keptAsYours)}`);
  }
  if (report.settingsEntriesRemoved.length > 0) {
    details.push(`VCO's entries removed from files that stay: ${names(report.settingsEntriesRemoved)}`);
  }
  return { summary, details };
}

/** The shared Remove flow's unregister: {@link REMOVE_UNREGISTER_OPTIONS} with
 *  the stop (and its "Unregister anyway" escape) intact — the stop can fire,
 *  and its escape re-runs with the same options (review R6 F46). */
export function removeUnregister(
  del: (options: UnregisterOptions) => Promise<UnregisterReport>,
  confirmLeave: (message: string) => Promise<boolean>,
): Promise<UnregisterOutcome> {
  return runUnregister(del, { ...REMOVE_UNREGISTER_OPTIONS }, confirmLeave);
}
