// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.105 — Claude accounts served by the model gateway. Thin wrappers over
// the Tauri commands in `launcher/src-tauri/src/commands/gateway_accounts.rs`
// (which spawn `python -m vco_lib.gateway_accounts`, the one home of the
// rules), plus the pure verdicts the two views render, so vitest can reach
// them without a component renderer.
//
// Nothing here ever sees a credential: login state arrives as a state word
// and an expiry.

import { invoke } from '$lib/tauri';

export interface LoginState {
  state: 'present' | 'expired' | 'absent' | 'unreadable' | string;
  expires_at_ms: number | null;
  expires_in_s: number | null;
  problem: string | null;
}

export interface LoginCommands {
  posix: string;
  powershell: string;
  check_posix: string;
}

/** The running gateway's view of one account (`/health` `accounts`). */
export interface GatewayAccountView {
  id: string;
  usable: boolean;
  oauth_state: string;
  oauth_expires_in_s: number | null;
  fallbacks: number;
  last_fallback_at: number | null;
  last_fallback_reason: string | null;
  refresh: { outcome: string; detail: string; at: number; next_try_at: number | null } | null;
}

export interface AccountRow {
  id: string;
  label: string;
  config_dir: string;
  is_default: boolean;
  is_primary: boolean;
  projects: string[];
  login: LoginState | null;
  login_commands: LoginCommands | null;
  gateway: GatewayAccountView | null;
}

export interface AccountsListing {
  ok: boolean;
  default: string;
  accounts: AccountRow[];
  projects: Record<string, string>;
  gateway: { ok: boolean; reason?: string; message?: string };
  selector_header: string;
  min_claude_code_version: string;
  error?: string;
}

export interface ProjectAccountStatus {
  ok: boolean;
  project_id: string;
  assigned: string | null;
  selected: string;
  source: 'project' | 'default';
  effective: string;
  falling_back: boolean;
  file_says: string | null;
  drift: boolean;
  /** Why the file does not carry the choice (`null` without drift). Only
   *  `not_applied` is fixed by Re-apply. */
  drift_cause: 'not_applied' | 'local_tracked' | 'local_unknown' | 'user_value' | null;
  /** The header line that selects the assigned account (`x-vco-account: <id>`). */
  selector_line: string | null;
  label?: string;
  error?: string;
}

export interface MutationAnswer {
  ok: boolean;
  error?: string;
  exported?: string;
  /** set-project: the choice was saved, but re-writing the project's env failed. */
  projection_error?: string;
  /** set-project: the project's status re-read after the save. */
  status?: ProjectAccountStatus;
}

export const listAccounts = () => invoke<AccountsListing>('gateway_accounts_list');

export const addAccount = (id: string, label: string, configDir: string) =>
  invoke<MutationAnswer>('gateway_accounts_add', { id, label, configDir });

export const removeAccount = (id: string) => invoke<MutationAnswer>('gateway_accounts_remove', { id });

export const setDefaultAccount = (id: string) =>
  invoke<MutationAnswer>('gateway_accounts_set_default', { id });

/** `account: null` = "use the default account". */
export const setProjectAccount = (projectId: string, account: string | null) =>
  invoke<MutationAnswer>('project_gateway_account_set', { projectId, account });

export const projectAccountStatus = (projectId: string) =>
  invoke<ProjectAccountStatus>('project_gateway_account_status', { projectId });

// ── pure verdicts ────────────────────────────────────────────────────────

/** Same grammar as `vco_lib.gateway_accounts.ID_PATTERN`. */
export const ACCOUNT_ID_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

export function validateNewAccount(id: string, configDir: string, taken: string[]): string | null {
  const trimmed = id.trim();
  if (!ACCOUNT_ID_RE.test(trimmed)) {
    return 'Use 1–32 lower-case letters, digits or dashes, starting with a letter or digit.';
  }
  if (trimmed === 'primary' || taken.includes(trimmed)) return `An account named "${trimmed}" already exists.`;
  const dir = configDir.trim();
  if (!(dir.startsWith('/') || dir.startsWith('~') || /^[A-Za-z]:[\\/]/.test(dir))) {
    return 'The configuration directory must be an absolute path.';
  }
  return null;
}

/** The default config dir offered for a new account: `~/.claude-<id>`. */
export function suggestedConfigDir(id: string): string {
  return id.trim() ? `~/.claude-${id.trim()}` : '';
}

export type Tone = 'ok' | 'warn' | 'bad';

/** One line for an account's login, by state and expiry only. */
export function loginVerdict(row: Pick<AccountRow, 'login' | 'gateway'>): { text: string; tone: Tone } {
  const state = row.gateway?.oauth_state ?? row.login?.state ?? 'absent';
  const expiresIn = row.gateway?.oauth_expires_in_s ?? row.login?.expires_in_s ?? null;
  if (state === 'present') {
    if (expiresIn !== null && expiresIn < 7200) {
      return { text: `logged in — expires in ${formatDuration(expiresIn)}`, tone: 'warn' };
    }
    return {
      text: expiresIn === null ? 'logged in' : `logged in — expires in ${formatDuration(expiresIn)}`,
      tone: 'ok',
    };
  }
  if (state === 'expired') return { text: 'login expired', tone: 'bad' };
  if (state === 'unreadable') return { text: 'login file unreadable', tone: 'bad' };
  return { text: 'not logged in', tone: 'bad' };
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** What the project page says about where this project's work is billed. */
export function projectVerdict(status: ProjectAccountStatus): { text: string; tone: Tone } {
  if (status.falling_back) {
    return {
      text:
        `This project selects "${status.selected}", which cannot be used right now — ` +
        `its requests are served by, and billed to, "${status.effective}" until it is logged in again.`,
      tone: 'bad',
    };
  }
  if (status.drift) {
    return { text: driftText(status), tone: 'warn' };
  }
  return { text: `Requests are served by "${status.effective}".`, tone: 'ok' };
}

/** What to say about a choice that is not in force — a remedy that helps. */
export function driftText(status: Pick<ProjectAccountStatus, 'drift_cause' | 'selector_line'>): string {
  const line = status.selector_line;
  switch (status.drift_cause) {
    case 'local_tracked':
      return (
        "This project's .claude/settings.local.json is committed to git, so VCO does not write the " +
        'account choice into it (it would be shared with everyone who clones the project). Untrack it ' +
        'with `git rm --cached .claude/settings.local.json`, then press Re-apply.'
      );
    case 'local_unknown':
      return (
        "Git could not say whether this project's .claude/settings.local.json is committed, so VCO " +
        'does not write the account choice into it. The project\'s pending actions name git\'s error; ' +
        'fix it, then press Re-apply.'
      );
    case 'user_value':
      return (
        "This project's .claude/settings.local.json carries your own ANTHROPIC_CUSTOM_HEADERS, which " +
        'VCO never overwrites. Add the line ' +
        (line ? `\`${line}\`` : 'that selects the account') +
        ' to it (one header per line) to use this account.'
      );
    default:
      return "The project's .claude/settings.local.json does not carry this choice yet. Press Re-apply.";
  }
}

/** Is the Re-apply button worth showing? Only when re-running the projection
 *  can bring the choice into force (a tracked/unknown layer needs the user
 *  first; their own header is never overwritten). */
export function reapplyHelps(status: Pick<ProjectAccountStatus, 'drift' | 'drift_cause'> | null): boolean {
  return !!status && status.drift && status.drift_cause !== 'user_value';
}

/** The toast after a save: success only when the choice is now in force. */
export function saveOutcome(answer: MutationAnswer): { text: string; tone: 'ok' | 'warn' | 'bad' } {
  if (!answer.ok) return { text: answer.error ?? 'The account choice was not saved.', tone: 'bad' };
  if (answer.projection_error) {
    return {
      text: `Claude account saved, but the project's settings could not be updated: ${answer.projection_error}`,
      tone: 'warn',
    };
  }
  if (answer.status?.drift) {
    return { text: `Claude account saved, but not in force yet: ${driftText(answer.status)}`, tone: 'warn' };
  }
  return { text: 'Claude account saved — the next request of a running chat uses it.', tone: 'ok' };
}

/** The picker is shown once a second account exists. */
export function pickerVisible(listing: Pick<AccountsListing, 'accounts'> | null): boolean {
  return !!listing && listing.accounts.length > 1;
}
