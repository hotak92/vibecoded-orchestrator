// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (WP-03b, owner ruling F-W2-03): the divergence modal's third
// choice, "Reset to upstream (discard local commits)". The backend
// (`update_run.rs::create_reset_backup`) saves the clone's local commits AND
// its uncommitted / untracked changes BEFORE `git reset --hard`, and refuses
// the reset when it cannot:
//
//   * branch `vco-backup/<stamp>`      — the pre-reset HEAD;
//   * branch `vco-backup/<stamp>-wip`  — the working tree, when it was dirty;
//   * `<vct_root>/backups/orchestrator-reset-<stamp>.bundle` — both, verified.
//
// The confirm dialog names both locations BEFORE anything happens (the stamp
// is only known afterwards, so by pattern); the result names them exactly.
// Pure helpers, so the wording is tested without mounting the modal.

import type { ResetBackup, UpdateOutcome, UpdateRunResult } from '$lib/stores/updater';

/** Where the bundle lands, for the confirm dialog. `vctRoot` null → the
 *  launcher's state dir, named generically. */
export function resetBundleDir(vctRoot: string | null): string {
  if (!vctRoot) return '<launcher state dir>/backups/';
  const sep = vctRoot.includes('\\') && !vctRoot.includes('/') ? '\\' : '/';
  const base = vctRoot.endsWith(sep) ? vctRoot : vctRoot + sep;
  return `${base}backups${sep}`;
}

/** The confirm dialog's lines: what is discarded, and BOTH backup locations. */
export function resetConfirmLines(vctRoot: string | null): string[] {
  return [
    'This discards your clone’s local commits and uncommitted changes, then resets it to ' +
      'the upstream release and runs the full update (install.py included).',
    'Before anything is reset, everything local is saved:',
    '• git branch vco-backup/<timestamp> (your commits) and vco-backup/<timestamp>-wip ' +
      '(uncommitted and untracked files, when there are any);',
    `• a verified git bundle in ${resetBundleDir(vctRoot)} (orchestrator-reset-<timestamp>.bundle).`,
    'If the backup cannot be written, nothing is reset.',
  ];
}

/** The result line: the backend's own message (which already names the
 *  backup), or — when only the structured field arrived — built from it. */
export function resetResultText(outcome: UpdateOutcome | null): string {
  if (!outcome) {
    return 'Reset to upstream finished. The launcher restarted before it could report where the backup was written; look for the vco-backup/<timestamp> branch in the clone.';
  }
  const b = outcome.reset_backup;
  const where = b
    ? b.bundle
      ? `Saved to ${backupBranchList(b)} and to ${b.bundle}.`
      : `Nothing local needed saving; the previous HEAD is kept as branch ${b.branch}.`
    : '';
  const msg = (outcome.message ?? '').trim();
  if (msg && (!b || msg.includes(b.branch))) return msg;
  return [msg || 'Reset to upstream finished.', where].filter(Boolean).join(' ');
}

/** Every saved branch, in the backend's order (`ResetBackup::describe`):
 *  HEAD, then the update branch's own tip (F-W4-08), then the wip commit. */
export function backupBranchList(b: ResetBackup): string {
  const all = [b.branch, b.branch_tip, b.uncommitted_branch].filter(
    (x): x is string => typeof x === 'string' && x.length > 0,
  );
  const label = all.length === 1 ? 'branch' : 'branches';
  const joined = all.length <= 1 ? all.join('') : `${all.slice(0, -1).join(', ')} and ${all[all.length - 1]}`;
  return `${label} ${joined}`;
}

/** Titles for the reset refusals the backend names by code. The DETAIL is
 *  always the backend's own reason text (it names the backup and the manual
 *  step); the title only says which refusal it was. */
const RESET_REFUSAL_TITLES: Readonly<Record<string, string>> = {
  reset_backup_failed: 'Reset refused — the backup could not be written',
  reset_abort_failed: 'Reset refused — the merge/rebase in progress could not be aborted',
  reset_state_not_clear: 'Reset refused — a merge/rebase is still in progress',
  reset_target_not_orchestrator: 'Reset refused — the folder is not the orchestrator clone',
};

/** What the divergence modal shows for a failed reset. */
export function resetFailureView(routed: {
  message: string;
  code?: string;
}): { title: string; detail: string } {
  return {
    title: (routed.code && RESET_REFUSAL_TITLES[routed.code]) || 'Reset refused or failed',
    detail: routed.message,
  };
}

/** Run the reset through the ONE update action (`updater.run('ResetHard')`). */
export function runResetToUpstream(
  run: (kind: 'ResetHard') => Promise<UpdateRunResult>,
): Promise<UpdateRunResult> {
  return run('ResetHard');
}
