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

import type { UpdateOutcome, UpdateRunResult } from '$lib/stores/updater';

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
      ? `Saved to branch ${b.branch}${b.uncommitted_branch ? ` and ${b.uncommitted_branch}` : ''} and to ${b.bundle}.`
      : `Nothing local needed saving; the previous HEAD is kept as branch ${b.branch}.`
    : '';
  const msg = (outcome.message ?? '').trim();
  if (msg && (!b || msg.includes(b.branch))) return msg;
  return [msg || 'Reset to upstream finished.', where].filter(Boolean).join(' ');
}

/** Run the reset through the ONE update action (`updater.run('ResetHard')`). */
export function runResetToUpstream(
  run: (kind: 'ResetHard') => Promise<UpdateRunResult>,
): Promise<UpdateRunResult> {
  return run('ResetHard');
}
