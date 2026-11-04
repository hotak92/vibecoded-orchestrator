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
  /**
   * The `if` filters of the rules this row covers (v0.2.101). EMPTY for an
   * ordinary hook. An `if` group — several settings.json entries sharing one
   * (event, matcher, command) and differing only by their `if` filter —
   * renders as ONE row carrying every rule here: the toggle acts on the
   * group as a unit (the writer parks and restores all of its entries
   * together), so per-rule checkboxes would each toggle everything anyway.
   */
  if_rules: string[];
  /**
   * The OTHER matchers this row also covers (v0.2.104, review R29). EMPTY
   * for an ordinary hook. A hook VCO ships under declared sibling matchers
   * of one event (the hub ensure: `startup` and `resume|clear|compact`) is
   * ONE hook: one row under its primary matcher, and the toggle disables /
   * restores every registration together. Optional so an older backend that
   * omits it still renders.
   */
  unit_matchers?: string[];
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
      // v0.2.102 (review R9 F3 + lane W6-HTAB): the bundle merge carries this
      // disable to an event VCO newly ships the same script on, while the
      // script runs on no event (vco_lib.parked_hooks.find_parked_match,
      // live_identities); the tab load then parks that registration as its
      // own row (park_carried_registrations), so it has its own toggle and
      // re-enabling it restores it at once.
      return `Removed from ${settingsPath} by the launcher. The entry is stored here, so re-enabling restores it exactly. The hook script file was not touched. While this script runs on no event, a VCO update that ships it on a new event keeps that one off too, and it gets its own Disabled row here.`;
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

// ─── `if` groups (v0.2.101) ────────────────────────────────────────────────
//
// Several settings.json entries can share one (event, matcher, command) and
// differ only by their `if` filter — the shipped Bash PreToolUse injection
// group carries ten. The backend collapses them into ONE row carrying every
// rule; these helpers shape what the row shows. The toggle is the GROUP's:
// disabling parks all rules together, enabling restores every one
// byte-identically.

/** The badge next to the command: `3 if-rules`, or '' for an ordinary hook. */
export function ifRulesLabel(hook: EffectiveHook): string {
  const n = hook.if_rules?.length ?? 0;
  if (n === 0) return '';
  return `${n} if-rule${n === 1 ? '' : 's'}`;
}

/**
 * The badge's hover text: every rule, one per line. Says plainly that the
 * toggle acts on the whole group — the one fact a user needs before
 * clicking a checkbox that looks like it belongs to a single command.
 */
export function ifRulesTooltip(hook: EffectiveHook): string {
  const rules = hook.if_rules ?? [];
  return [
    `This command is registered ${rules.length === 1 ? 'once' : `${rules.length} times`}, once per \`if\` filter:`,
    ...rules.map((r) => `• ${r}`),
    'The toggle applies to every rule at once; enabling restores them all exactly.',
  ].join('\n');
}

// ── declared sibling matchers (v0.2.104, review R29) ─────────────────────

/** The Matcher cell: the row's matcher, then every matcher it also covers. */
export function matcherLabel(hook: EffectiveHook): string {
  const own = hook.matcher || 'every';
  const others = hook.unit_matchers ?? [];
  return others.length === 0 ? own : [own, ...others].join(' + ');
}

/**
 * The Matcher cell's hover text when the row covers several registrations
 * ('' otherwise): says the toggle acts on all of them, the one fact a user
 * needs before unticking what looks like a single registration.
 */
export function matcherTooltip(hook: EffectiveHook): string {
  const others = hook.unit_matchers ?? [];
  if (others.length === 0) return '';
  return [
    `This hook is registered under ${others.length + 1} matchers of ${hook.event}:`,
    ...[hook.matcher || 'every', ...others].map((m) => `• ${m}`),
    'It is one hook: the toggle disables or restores every registration at once.',
  ].join('\n');
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
 * toggle here (or a toggle without a row) is red.
 * (v0.2.102: `post-file-delete` left the table — it was 100% the
 * diagram-delete cascade and was retired with the dropped diagrams
 * integrations.) */
export const ASYNC_SUBHOOK_STEMS: readonly string[] = [
  'post-edit-outcome',
  'kg-summary-generator',
  'post-bash-context-record',
  'post-git-commit-kg-sync',
  'stop-drain-citations',
  'kg-update-nudge',
];

/** One line per row: what turning the sub-hook off stops. */
export const ASYNC_SUBHOOK_DESCRIPTIONS: Record<string, string> = {
  'post-edit-outcome': 'Edit/Write outcome telemetry for the RL retrieval pipeline',
  'kg-summary-generator': 'KG node summary refresh after knowledge edits and node writes',
  'post-bash-context-record': 'Bash outcome telemetry paired with the pre-bash injection',
  'post-git-commit-kg-sync': 'Background KG review agent after a git commit',
  'stop-drain-citations':
    'RL: hand the KG citations of a subagent brief to that subagent, so they are scored against its answer',
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
 * `post-git` entry must never disable `post-git-commit-kg-sync`). */
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

/**
 * The routed sub-hook a hook row's command invokes, or null.
 *
 * Display only: it decides whether a row's tooltip mentions the
 * dispatcher route, never what runs — the same kind of literal check
 * {@link dispatcherRowPresent} makes. A stem matches only as the script
 * file name under `.claude/hooks/` (`/` or `\`), so `post-git` never
 * matches `post-git-commit-kg-sync`.
 */
export function routedSubhookStem(command: string): string | null {
  for (const stem of ASYNC_SUBHOOK_STEMS) {
    for (const ext of ['.sh', '.ps1']) {
      if (
        command.includes(`.claude/hooks/${stem}${ext}`) ||
        command.includes(`.claude\\hooks\\${stem}${ext}`)
      ) {
        return stem;
      }
    }
  }
  return null;
}

/**
 * The extra tooltip sentence for a DISABLED row whose script the async
 * dispatcher also routes (v0.2.102, lane W6-HTAB). Disabling the row parks
 * that event's registration only; the dispatcher's Agent/Task route is a
 * different event with its own switch in the section below, which this
 * points at instead of flipping it behind the user's back. '' otherwise.
 */
export function routedSubhookNote(hook: EffectiveHook, hooks: EffectiveHook[]): string {
  if (hook.state !== 'disabled' || !dispatcherRowPresent(hooks)) return '';
  const stem = routedSubhookStem(hook.command);
  if (stem === null) return '';
  return ` The async dispatcher also runs ${stem} on matching tool calls; turn that off with its own switch under "Async PostToolUse sub-hooks".`;
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
  'nothing is lost. Needs the lean-ctx binary — without one the hook does nothing. ' +
  'This setting does not affect the kill guard that runs in the same hook (see the ' +
  'lean-ctx-rewrite row).';

// ─── kill guard note (v0.2.105) ──────────────────────────────────────────
//
// The kill guard has no toggle of its own here — its switch,
// VCO_KILL_GUARD=off, is the user's decision and lives in the hook
// environment, like the wait guard's (docs/CONFIGURATION.md). What the tab
// MUST say is which rows carry it, because parking one of those rows turns
// a layer of the guard off: lean-ctx-rewrite hosts the runtime check (it is
// the one hook allowed to rewrite a shell command), pre-tool-use the static
// one. Display only — the same literal script-name check routedSubhookStem
// makes.

/** The kill-guard layer a hook row's script carries, or null. */
export function killGuardLayer(command: string): 'runtime' | 'static' | null {
  const has = (stem: string) =>
    ['.sh', '.ps1'].some(
      (ext) =>
        command.includes(`.claude/hooks/${stem}${ext}`) ||
        command.includes(`.claude\\hooks\\${stem}${ext}`),
    );
  if (has('lean-ctx-rewrite')) return 'runtime';
  if (has('pre-tool-use')) return 'static';
  return null;
}

/** The extra tooltip sentence for a row that carries a kill-guard layer
 * ('' otherwise). Shown in every state: turning the row OFF is exactly when
 * the user needs to know what else stops. */
export function killGuardNote(hook: EffectiveHook): string {
  const layer = killGuardLayer(hook.command);
  if (layer === 'runtime') {
    return ' This hook also runs the kill guard\'s runtime check: every kill/pkill/killall ' +
      '(and Stop-Process/taskkill) first checks its target pids and refuses pid 1, the ' +
      'shell\'s own parents and desktop processes. Turning this row off turns that check off ' +
      '(the static check in pre-tool-use stays). The lean-ctx setting does not affect it.';
  }
  if (layer === 'static') {
    return ' This hook also runs the kill guard\'s static check (refuses kills of pid 1, ' +
      '$PPID, parent lookups and broad pkill/killall patterns). Turning this row off turns ' +
      'that check off.';
  }
  return '';
}

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
