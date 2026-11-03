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
  label: string;
  error?: string;
}

export interface MutationAnswer {
  ok: boolean;
  error?: string;
  exported?: string;
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
    return {
      text:
        "The project's .claude/settings.local.json does not carry this choice yet " +
        '(or carries your own ANTHROPIC_CUSTOM_HEADERS). Press Re-apply.',
      tone: 'warn',
    };
  }
  return { text: `Requests are served by "${status.effective}".`, tone: 'ok' };
}

/** The picker is shown once a second account exists. */
export function pickerVisible(listing: Pick<AccountsListing, 'accounts'> | null): boolean {
  return !!listing && listing.accounts.length > 1;
}
