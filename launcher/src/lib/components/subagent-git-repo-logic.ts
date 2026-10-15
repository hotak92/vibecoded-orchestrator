// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (#30) — pure decision logic for the SubagentGitRepoModal's
// "Connect an existing repo" arm, extracted so it is unit-testable without
// mounting the component (mirrors codegraph-build-banner-logic.ts).

/**
 * Shape-validate a git remote URL the user typed.
 *
 * Accepted shapes (and ONLY these — a plain local path belongs in the
 * local-folder arm, not here):
 *   * scheme+host+path — `http|https|ssh|git` `://` non-empty host segment
 *     `/` non-empty path (e.g. `https://github.com/example/example-repo.git`,
 *     `ssh://git@host/org/repo.git`).
 *   * scp-like — `user@host:path`, all three parts non-empty
 *     (e.g. `git@github.com:example/example-repo.git`).
 * Whitespace anywhere (after trimming) rejects. A leading `-` rejects
 * (M8): such a candidate would reach git as an OPTION
 * (`git remote add origin -t@host:path` parses `-t` as a flag), so it
 * must never pass shape validation.
 *
 * MUST MATCH `is_valid_git_remote_url` in
 * launcher/src-tauri/src/commands/worktree_repo_mode.rs — the backend is
 * the authoritative validator; this mirror only drives live UI feedback.
 * The parity contract is executable, not comment-only: BOTH test suites
 * iterate the shared fixture tests/fixtures/git_remote_url_parity.json.
 */
export function isValidGitRemoteUrl(url: string): boolean {
  const u = url.trim();
  if (u === '' || /\s/.test(u) || u.startsWith('-')) return false;
  const schemeIdx = u.indexOf('://');
  if (schemeIdx >= 0) {
    const scheme = u.slice(0, schemeIdx);
    const rest = u.slice(schemeIdx + 3);
    if (!['http', 'https', 'ssh', 'git'].includes(scheme)) return false;
    const slash = rest.indexOf('/');
    if (slash < 0) return false;
    return slash > 0 && rest.length > slash + 1;
  }
  const at = u.indexOf('@');
  if (at <= 0) return false;
  const rest = u.slice(at + 1);
  const colon = rest.indexOf(':');
  if (colon < 0) return false;
  return colon > 0 && rest.length > colon + 1;
}

/** The connect arm's resolved input: exactly one source, already valid. */
export type ConnectResolution =
  | { ok: true; kind: 'remote'; url: string }
  | { ok: true; kind: 'local'; path: string }
  | { ok: false; reason: 'empty' | 'both' | 'invalid_url' };

/**
 * Decide which connect arm the form state selects. The component clears
 * one field when the other is set, so 'both' is normally unreachable —
 * kept as an explicit guard rather than a silent precedence pick.
 */
export function resolveConnectSelection(
  remoteUrl: string,
  localPath: string,
): ConnectResolution {
  const url = remoteUrl.trim();
  const path = localPath.trim();
  if (!url && !path) return { ok: false, reason: 'empty' };
  if (url && path) return { ok: false, reason: 'both' };
  if (url) {
    return isValidGitRemoteUrl(url)
      ? { ok: true, kind: 'remote', url }
      : { ok: false, reason: 'invalid_url' };
  }
  return { ok: true, kind: 'local', path };
}

/**
 * v0.2.103 (plan item 17) — the project page's subagent-git probe result,
 * returned by the `plan_project_git_repo` Tauri command.
 *
 * MUST MATCH the labels `plan_label` returns in
 * launcher/src-tauri/src/commands/worktree_repo_mode.rs.
 *   * `decided`           — a mode is recorded; never prompt again.
 *   * `existing`          — the folder is (inside) a repo; `use_existing`
 *                           was recorded by the probe.
 *   * `needs_choice`      — repo-less but holding nested repos: a REAL
 *                           choice (wrap them in a new root repo, point at
 *                           one, or opt out) — the only state that prompts.
 *   * `auto_init_pending` — repo-less with nothing nested: VCO creates the
 *                           local repo itself at add / the next bundle update
 *                           (owner ruling 2026-10-14: no approval needed).
 *   * `git_unavailable`   — git cannot run; the add/update warning says so.
 */
export type GitRepoPlan =
  | 'decided'
  | 'existing'
  | 'needs_choice'
  | 'auto_init_pending'
  | 'git_unavailable';

/** True only when the subagent-git modal should open for this plan. */
export function shouldOfferSubagentGitModal(plan: GitRepoPlan | string | null): boolean {
  return plan === 'needs_choice';
}
