// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Decision logic + copy for the machine-global Artifact-tool switch.
//
// Everything here is pure: no `invoke`, no Svelte, no DOM. The repo has no
// jsdom, so `.svelte` files are not unit-testable — this module is where the
// panel's real decisions live so they CAN be tested (same split as
// `dual-flags.ts`, `deferral-ledger.ts`, `module-enable.ts`).
// `ArtifactToolPanel.svelte` is markup over it.
//
// ## What the toggle is, stated once
//
// Claude Code ships the `Artifact` tool's description in the system prompt of
// every request. Turning the tool off removes that description, which is the
// whole point — the saving is in the CONTEXT, not in the blocked call. The
// backend writes two keys to `~/.claude/settings.json`:
//
//   * `permissions.deny: ["Artifact"]` — the BARE tool name, which removes
//     the tool from Claude's context entirely. The scoped form `Artifact(*)`
//     does NOT: it blocks the call and keeps paying for the description. VCO
//     never writes the scoped form, and this module reports a user-authored
//     one separately rather than counting it as a saving.
//   * `enableArtifact: false` — the purpose-built off-switch, kept alongside
//     the deny entry because on CLI versions before 2.1.242 a
//     higher-precedence settings file can override it while deny rules apply
//     additively from every loaded file.
//
// ## Honesty rules this module encodes
//
//   * `artifacts_enabled === null` means the state is UNKNOWN (the file is
//     unreadable or not valid JSON). It is rendered as unknown with the
//     control disabled — never as a guessed default, and never as an excuse
//     to rewrite the file.
//   * Either key alone reads as OFF, but only both together are the state a
//     disable writes; a half-applied file says so.
//   * The panel names whose file it edits (Claude Code's, not VCO's) and how
//     far the change reaches (every project on the machine).

/** Mirror of the Rust `ArtifactToolState` wire shape. */
export interface ArtifactToolState {
  /** Absolute path of the settings file the state was read from. */
  settings_path: string;
  file_exists: boolean;
  /** `true` on, `false` off, `null` unknown (unreadable / unparseable file). */
  artifacts_enabled: boolean | null;
  enable_artifact_false: boolean;
  deny_bare_artifact: boolean;
  /** A user-authored `Artifact(...)` rule. VCO neither writes nor removes it. */
  deny_scoped_artifact: boolean;
  /** Both managed keys present — computed backend-side, not re-derived here. */
  fully_disabled: boolean;
  error: string | null;
  backup_path: string | null;
}

/** Label of the one control. Checked = the tool is available to Claude Code. */
export const CHECKBOX_LABEL = 'Artifact tool available to Claude Code';

/**
 * R6: this control edits Claude Code's OWN configuration and reaches every
 * project. Both facts are stated on the panel, not implied by its placement.
 */
export const SCOPE_NOTE =
  "This edits Claude Code's own global configuration, not VCO state, and it " +
  'applies to every project on this machine — not just the selected one.';

/**
 * R6, without overstating: `permissions` is documented to hot-reload;
 * `enableArtifact` is not documented either way, so the honest claim is "may
 * need", not "will take effect immediately" and not "requires a restart".
 */
export const RESTART_NOTE =
  'A Claude Code session that is already open may need a /clear or a restart ' +
  'before the change is fully reflected. Permission rules are documented to ' +
  'reload on their own; the enableArtifact key is not documented either way.';

/**
 * The backend still rewrites the whole document, but since v0.2.92 it keeps
 * the user's key order (`serde_json`'s `preserve_order`), so the cosmetic
 * side effect this note used to admit is gone.
 *
 * What replaces it is the half that is NOT fixed: a file an OLDER VCO already
 * alphabetised stays alphabetised. VCO never recorded the original order, so
 * there is nothing to restore it from, and rewriting the file purely to
 * reorder it would be another unasked-for write to a file the user owns. The
 * one-time backup is the only path back, so the note names it.
 */
export const REWRITE_NOTE =
  'Toggling this rewrites your settings file, keeping every key, every value ' +
  'and the order you put them in. If an older version of VCO already sorted ' +
  'this file alphabetically, it stays that way — the original order was not ' +
  'recorded anywhere, so it cannot be restored. A copy of the file as it was ' +
  'before VCO first changed it is kept next to it.';

export type NoticeKind =
  | 'unknown'
  | 'partial'
  | 'scoped-deny'
  | 'no-file'
  | 'backup';

export interface Notice {
  kind: NoticeKind;
  /** 'warn' renders as a problem, 'info' as a plain footnote. */
  tone: 'warn' | 'info';
  text: string;
}

/** True when the state could not be determined at all. */
export function isUnknown(state: ArtifactToolState | null): boolean {
  return state === null || state.artifacts_enabled === null;
}

/**
 * Checkbox position. An unknown state renders UNCHECKED but the control is
 * also disabled (see `controlDisabled`), so the box is never a claim.
 */
export function checkboxChecked(state: ArtifactToolState | null): boolean {
  return state?.artifacts_enabled === true;
}

/**
 * Conservative default on a best-effort path: when the state cannot be
 * positively determined, the control is inert rather than guessing.
 */
export function controlDisabled(
  state: ArtifactToolState | null,
  busy: boolean,
): boolean {
  return busy || isUnknown(state);
}

/** One line describing what is actually on disk right now. */
export function statusLine(state: ArtifactToolState | null): string {
  if (state === null) return 'Reading…';
  if (state.artifacts_enabled === null) {
    return "Unknown — this machine's Claude Code settings file could not be read.";
  }
  if (state.artifacts_enabled) {
    return state.file_exists
      ? "On — no off-switch recorded, so Claude Code's default applies."
      : "On — no Claude Code settings file exists yet, so its default applies.";
  }
  return state.fully_disabled
    ? 'Off — both enableArtifact and the bare Artifact deny rule are set.'
    : 'Off — but only partly applied (see below).';
}

/**
 * Everything worth saying beyond the status line, in render order. Kept as
 * data rather than markup so the combinations are testable.
 */
export function notices(state: ArtifactToolState | null): Notice[] {
  if (state === null) return [];
  const out: Notice[] = [];

  if (state.artifacts_enabled === null) {
    out.push({
      kind: 'unknown',
      tone: 'warn',
      text:
        `${state.settings_path} could not be read as JSON, so the switch is ` +
        'inert. Nothing was written — fix or move the file by hand and reload ' +
        `this page. Reported error: ${state.error ?? 'unknown'}`,
    });
    // Nothing below can be trusted when the document did not parse.
    return out;
  }

  if (!state.artifacts_enabled && !state.fully_disabled) {
    const present = state.enable_artifact_false
      ? 'enableArtifact: false'
      : 'the bare "Artifact" deny rule';
    const missing = state.enable_artifact_false
      ? 'the bare "Artifact" deny rule'
      : 'enableArtifact: false';
    out.push({
      kind: 'partial',
      tone: 'warn',
      text:
        `Only ${present} is present; ${missing} is not. Artifacts are off, but ` +
        'the pair is what survives a higher-precedence settings file. Toggle ' +
        'off and on again — or just off — to write both.',
    });
  }

  if (state.deny_scoped_artifact) {
    out.push({
      kind: 'scoped-deny',
      tone: 'info',
      text:
        'Your deny list also contains a scoped Artifact(...) rule. VCO neither ' +
        'writes nor removes it. A scoped rule blocks the call but leaves the ' +
        "tool description in Claude's context, so it saves no tokens — remove " +
        'it by hand if that is not what you wanted.',
    });
  }

  if (!state.file_exists) {
    out.push({
      kind: 'no-file',
      tone: 'info',
      text:
        `No file at ${state.settings_path} yet. Turning the tool off creates ` +
        'one containing only these two keys.',
    });
  }

  if (state.backup_path) {
    out.push({
      kind: 'backup',
      tone: 'info',
      text: `A copy of the file as it was before VCO first changed it is kept at ${state.backup_path}.`,
    });
  }

  return out;
}

/** Toast text after a successful write, phrased from the RE-READ state. */
export function savedMessage(state: ArtifactToolState): string {
  if (state.artifacts_enabled === null) {
    return 'Setting written, but the file could not be re-read.';
  }
  return state.artifacts_enabled
    ? 'Artifact tool re-enabled for every project on this machine.'
    : 'Artifact tool disabled for every project on this machine.';
}
