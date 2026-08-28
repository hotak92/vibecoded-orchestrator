// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Presentation logic for the Hooks tab (v0.2.91, decision #27).
//
// Kept out of the `.svelte` file so the rules that decide what the user is
// told — and which controls are live — are unit-testable. Before v0.2.91 the
// tab rendered a single `enabled` boolean straight off a DB row that nothing
// enforced; the three-state model here exists so "VCO turned this off" and
// "this isn't in settings.json at all" can never collapse back into one
// checkbox that means neither.

/** What a hook's row actually means. Mirrors the Rust `HookState`. */
export type HookState = 'active' | 'disabled' | 'orphan';

export interface EffectiveHook {
  /** `project_hooks.id`, or null for a hook in settings.json the launcher has never scanned. */
  id: number | null;
  event: string;
  matcher: string;
  command: string;
  source: string;
  source_module: string | null;
  timeout_ms: number | null;
  state: HookState;
}

export interface EffectiveHooksView {
  hooks: EffectiveHook[];
  settings_path: string;
  settings_readable: boolean;
  error_code: string | null;
  error: string | null;
  skipped: string[];
}

/** The checkbox reflects enforcement, not a stored flag. */
export function isChecked(hook: EffectiveHook): boolean {
  return hook.state === 'active';
}

/**
 * Whether the toggle is live for this row.
 *
 * An orphan has no settings.json entry to remove and nothing parked to
 * restore — offering a toggle would promise an effect we cannot deliver,
 * which is the exact failure this work package exists to end. Delete is
 * still offered for orphans (clearing the stale row is a real, honest
 * outcome).
 */
export function canToggle(hook: EffectiveHook, settingsReadable: boolean): boolean {
  return settingsReadable && hook.state !== 'orphan';
}

/** Short label rendered next to the row. */
export function stateLabel(state: HookState): string {
  switch (state) {
    case 'active':
      return 'Running';
    case 'disabled':
      return 'Disabled';
    case 'orphan':
      return 'Not in settings.json';
  }
}

/** Hover text — the full explanation, one sentence, no jargon. */
export function stateTooltip(state: HookState, settingsPath: string): string {
  switch (state) {
    case 'active':
      return `Declared in ${settingsPath} — Claude Code runs it on every matching event.`;
    case 'disabled':
      return `Removed from ${settingsPath} by the launcher. The entry is stored here, so re-enabling restores it exactly. The hook script file was not touched.`;
    case 'orphan':
      return `The launcher has a record of this hook, but ${settingsPath} does not declare it — so it does not run. It was removed outside the launcher (a hand edit, a bundle update, another tool). There is nothing stored to restore; Delete clears the stale record.`;
  }
}

/**
 * The banner shown when settings.json cannot be read.
 *
 * Every branch names the file and says plainly that nothing was written —
 * a refusal the user cannot interpret is indistinguishable from a silent
 * failure.
 */
export function settingsErrorBanner(
  code: string | null,
  message: string | null,
  settingsPath: string,
): string {
  switch (code) {
    case 'missing':
      return `${settingsPath} does not exist yet, so there is nothing to wire hooks into. Run the project's bundle install (Settings → Update bundle) first.`;
    case 'unparseable':
      return `${settingsPath} is not valid JSON, so the launcher will not edit it — a rewrite could destroy what is there. Fix the file by hand and reload. Nothing was written.`;
    case 'hooks_block_malformed':
      return `The \`hooks\` block in ${settingsPath} has a shape the launcher cannot edit safely. Fix it by hand and reload. Nothing was written.`;
    case 'no_python':
      return `The hooks editor needs the orchestrator's Python environment and could not find it. Hook changes are unavailable until that is fixed; nothing was written.`;
    default:
      return message ?? `${settingsPath} could not be read. Nothing was written.`;
  }
}

/**
 * The confirm text for Delete.
 *
 * States BOTH facts the user needs before clicking: the wiring goes away, and
 * the script file does not. "Delete" on a row that reads like a file is
 * otherwise a reasonable thing to fear.
 */
export function unregisterConfirmText(hook: EffectiveHook, settingsPath: string): string {
  const where =
    hook.state === 'active'
      ? `This removes its entry from ${settingsPath}, so it stops running.`
      : `This clears the launcher's record. It is already absent from ${settingsPath}.`;
  return [
    `Unregister the ${hook.event} hook \`${hook.command}\`?`,
    where,
    'The hook script file itself is NOT deleted.',
  ].join('\n\n');
}

/**
 * The one-line note under the tab header.
 *
 * settings.json is frequently VCS-tracked, so an edit made from a GUI toggle
 * shows up in the user's next `git diff` / commit. Saying so up front is
 * cheaper than a surprised user reverting the change.
 */
export function gitVisibilityNote(settingsPath: string): string {
  return `Enabling, disabling and registering hooks edits ${settingsPath} — the file Claude Code reads. It is usually tracked by git, so changes here will show up in your next diff.`;
}

/** Seconds for display; the backend stores milliseconds. */
export function timeoutSeconds(hook: EffectiveHook): number | null {
  if (hook.timeout_ms === null || hook.timeout_ms === undefined) return null;
  return Math.round(hook.timeout_ms / 1000);
}

/**
 * Parse the "Timeout (s)" field.
 *
 * Returns `{ ok: true, value }` for blank (no timeout) or a positive whole
 * number, and `{ ok: false, error }` otherwise — the backend refuses a
 * non-positive timeout, and catching it here means the user gets the reason
 * next to the field instead of a toast after a round trip.
 */
export function parseTimeoutSeconds(
  raw: string,
): { ok: true; value: number | null } | { ok: false; error: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: true, value: null };
  if (!/^\d+$/.test(trimmed)) {
    return { ok: false, error: 'Timeout must be a whole number of seconds.' };
  }
  const value = Number(trimmed);
  if (value <= 0) {
    return { ok: false, error: 'Timeout must be greater than zero.' };
  }
  return { ok: true, value };
}

/**
 * Whether "+ Register" can be submitted, and why not when it cannot.
 *
 * `null` = submittable.
 */
export function registerBlockedReason(
  event: string,
  command: string,
  timeoutRaw: string,
): string | null {
  if (!event.trim()) return 'Pick an event.';
  if (!command.trim()) return 'Enter the command to run.';
  const t = parseTimeoutSeconds(timeoutRaw);
  if (!t.ok) return t.error;
  return null;
}
