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
      return `${settingsPath} is not valid JSON or JSONC, so the launcher will not edit it — a rewrite could destroy what is there. Fix the file by hand and reload. Nothing was written.`;
    case 'jsonc_edit_refused':
      return `${settingsPath} has comments or trailing commas, and this change could not be made in place without risking them — a rewrite could destroy what is there. Edit it by hand, or remove the comments, and reload. Nothing was written; the project's UPDATE_DEFERRED.md names the file too.`;
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

/**
 * Which OS the new-hook Command hint must fit.
 *
 * `'windows'` | `'other'` — the hint only has two shapes, so the granular
 * linux/macos split other surfaces need would be detail this one cannot use.
 */
export type HintOs = 'windows' | 'other';

/**
 * Classify a `navigator.userAgent` for the new-hook hint.
 *
 * The launcher is a Tauri webview, so `navigator.userAgent` is the one OS
 * fact available on every platform without a backend round trip; the
 * WebView2/WebKitGTK/WKWebView strings all carry the OS name.
 */
export function detectHintOs(userAgent: string): HintOs {
  return /Windows/i.test(userAgent) ? 'windows' : 'other';
}

/**
 * The placeholder for the new-hook Command field, in the form the HOST OS
 * can actually run (R9 H8: the tab used to show the Linux bash form on
 * Windows too, and a user copying it for a `.ps1` hook got a command the
 * compatibility doc says breaks under the PowerShell fallback — a user's
 * own hook is never rewritten, so the mistake stays).
 */
export function newHookCommandPlaceholder(os: HintOs): string {
  return os === 'windows'
    ? 'powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/my-hook.ps1"'
    : 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-hook.sh"';
}

// ─── async PostToolUse sub-hook toggles (v0.2.101 review SF-2) ───────────
//
// The merged async dispatcher (post-tool-use-async) replaced eight
// individually-toggleable PostToolUse registrations with ONE row — parking
// that row would stop all six sub-hooks at once. The per-sub-hook switch is
// kept: a stem listed in VCO_ASYNC_DISABLED_HOOKS (<project>/.claude/env —
// the SAME per-project knob file, channel and write command
// (set_claude_env_value) the lean-ctx toggle below uses; never a second
// store) is skipped by both dispatcher siblings. A disable that predates
// the merge is carried into the key by the bundle update
// (vco_lib.hook_retirements.carry_parked_async_disables), so turning a
// sub-hook off before v0.2.101 keeps it off after.

/** The `.claude/env` key this section owns. */
export const ASYNC_DISABLED_KEY = 'VCO_ASYNC_DISABLED_HOOKS';

/** The routed sub-hooks. MUST MATCH the dispatcher ROUTE_TABLEs
 * (templates/hooks/post-tool-use-async.{sh,ps1}) — the vitest suite
 * DERIVES the set from the shipped table, so a routing row without a
 * toggle here (or a toggle without a row) is red. */
export const ASYNC_SUBHOOK_STEMS: readonly string[] = [
  'post-edit-outcome',
  'kg-summary-generator',
  'post-bash-context-record',
  'post-git-commit-kg-sync',
  'post-file-delete',
  'kg-update-nudge',
];

/** One line per row: what turning the sub-hook off stops. */
export const ASYNC_SUBHOOK_DESCRIPTIONS: Record<string, string> = {
  'post-edit-outcome': 'Edit/Write outcome telemetry for the RL retrieval pipeline',
  'kg-summary-generator': 'KG node summary refresh after knowledge edits and node writes',
  'post-bash-context-record': 'Bash outcome telemetry paired with the pre-bash injection',
  'post-git-commit-kg-sync': 'Background KG review agent after a git commit',
  'post-file-delete': 'Diagram delete cascade (SQLite + sidecar + Weaviate)',
  'kg-update-nudge': 'Per-tool-call work-unit bookkeeping behind the KG-write nudge',
};

/** Parse the env value: split on ',', trim, drop empties, de-dupe, keep
 * order. Mirrors what both dispatcher siblings accept. */
export function parseAsyncDisabled(raw: string | null | undefined): string[] {
  if (!raw) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of raw.split(',')) {
    const s = part.trim();
    if (s && !seen.has(s)) {
      seen.add(s);
      out.push(s);
    }
  }
  return out;
}

/** Whether one stem is currently switched off (exact stem match — a
 * `post-file` entry must never disable `post-file-delete`). */
export function isAsyncSubhookDisabled(
  raw: string | null | undefined,
  stem: string,
): boolean {
  return parseAsyncDisabled(raw).includes(stem);
}

/** The value to persist after a toggle (`disabled` = switch the stem OFF).
 * `null` removes the key entirely — the last re-enable must not leave an
 * empty `VCO_ASYNC_DISABLED_HOOKS=` behind; an absent key is the file's
 * "nothing disabled" state. Unknown stems (a hand edit) are preserved. */
export function asyncDisabledValueAfterToggle(
  raw: string | null | undefined,
  stem: string,
  disabled: boolean,
): string | null {
  const current = parseAsyncDisabled(raw);
  const next = disabled
    ? current.includes(stem)
      ? current
      : [...current, stem]
    : current.filter((s) => s !== stem);
  return next.length > 0 ? next.join(',') : null;
}

/** Whether the tab should offer the sub-hook toggles at all: only when the
 * dispatcher registration is actually among the project's listed hooks (a
 * not-yet-updated project still has the eight direct registrations and
 * their own rows). */
export function dispatcherRowPresent(hooks: EffectiveHook[]): boolean {
  return hooks.some(
    (h) => h.event === 'PostToolUse' && h.command.includes('post-tool-use-async'),
  );
}

/** The copy under the section. */
export const ASYNC_SUBHOOK_HINT =
  'These background PostToolUse hooks are routed by the single async ' +
  'post-tool-use-async dispatcher (one registration — a tool call no longer ' +
  'grows your session transcript per hook). Turning one off adds its stem to ' +
  'VCO_ASYNC_DISABLED_HOOKS in <project>/.claude/env and the dispatcher skips ' +
  'it on every tool call. A disable you set before v0.2.101 was carried into ' +
  'this key by the bundle update.';

/** Confirmation copy after a successful toggle write. */
export function asyncSubhookToastText(stem: string, disabled: boolean): string {
  return disabled
    ? `${stem} is now skipped by the async dispatcher`
    : `${stem} runs again on matching tool calls`;
}

// ─── lean-ctx per-project toggle (PR-6 v0.2.11; copy fixed + control wired
// in v0.2.101 alongside the allow-list inversion) ─────────────────────────
//
// Three logical states map to two on-disk states for
// `<project>/.claude/env::VCO_LEAN_CTX_DEFAULT`:
//   * 'default' → key absent (the hook treats absence as "on")
//   * 'on'      → key present, value 'on'
//   * 'off'     → key present, value 'off'
// The logic lives here (not in the .svelte) so the mapping and the copy the
// user reads are unit-testable — the v0.2.101 GUI audit found the toggle's
// state + handlers had shipped in HooksTab.svelte with NO markup ever
// rendering them (a delivered-nowhere control); the description below must
// match the allow-list rule the hooks actually enforce.

/** The `VCO_LEAN_CTX_DEFAULT` key this toggle owns. */
export const LEAN_CTX_KEY = 'VCO_LEAN_CTX_DEFAULT';

/** The toggle's three logical states. */
export type LeanCtxChoice = 'default' | 'on' | 'off';

export const LEAN_CTX_OPTIONS: Array<{ value: LeanCtxChoice; label: string }> = [
  { value: 'default', label: 'Default (on)' },
  { value: 'on', label: 'Per-project: on' },
  { value: 'off', label: 'Per-project: off' },
];

/**
 * What the user reads under the toggle. MUST describe the v0.2.101
 * allow-list rule (compress only known-noisy commands; everything else
 * raw; every compression lossless via the tee pointer) — not the retired
 * "compress everything except exemptions" rule.
 */
export const LEAN_CTX_HINT =
  'When on, the PreToolUse hook compresses ONLY allow-listed noisy commands ' +
  '(package installs, image pulls, downloads, test/build runs); loops, pipes, git, ' +
  'unknown and credential-bearing commands run raw. Every compressed run saves its ' +
  'full raw output under .claude/state/lean-ctx-tee/ and prints a pointer to it, so ' +
  'nothing is lost. Needs the lean-ctx binary — without one the hook does nothing.';

/**
 * Map the on-disk env value to a toggle state. Both hook siblings read the
 * key case-insensitively (the .sh via a POSIX `[oO][fF][fF]` case-glob, the
 * .ps1 via ToLowerInvariant — SF-3, v0.2.101 review), so this mapping is
 * case-insensitive too. Any value other than the two the hooks read
 * (including a manual edit or a missing key) renders as 'default': the user
 * keeps the on-disk override until they actively move the toggle, which
 * then writes cleanly.
 */
export function leanCtxChoiceFromEnvValue(v: string | null | undefined): LeanCtxChoice {
  if (v === null || v === undefined) return 'default';
  const lower = v.toLowerCase();
  if (lower === 'off') return 'off';
  if (lower === 'on') return 'on';
  return 'default';
}

/** The env value to persist for a chosen state ('default' removes the key). */
export function leanCtxEnvValueForChoice(c: LeanCtxChoice): string | null {
  return c === 'default' ? null : c;
}

/** Confirmation copy after a successful toggle write. */
export function leanCtxToastText(c: LeanCtxChoice): string {
  return c === 'default'
    ? 'Reverted to default (allow-listed compression on)'
    : `Per-project compression set to ${c}`;
}
