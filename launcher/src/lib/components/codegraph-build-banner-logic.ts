// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.75 P2d (C-11b) — pure decision logic for the CodeGraphBuildBanner's
// prune-failure escalation. Extracted so it is unit-testable without mounting
// the Svelte component (mirrors regenerate-modal-logic.ts).

import type { CodeGraphBuildView } from '$lib/types/launcher';

// The prune-failure signature the Rust reader writes onto a `partial` build's
// error_message when the analyzer emitted PRUNE_FAILURES=N>0 (stale rows a
// plain re-run can't delete because of persistent shard state).
//
// MUST MATCH launcher/src-tauri/src/commands/codegraph.rs
// ("{} stale row(s) could not be pruned; inserts succeeded").
export const PRUNE_FAILURE_SIGNATURE = 'could not be pruned';

/**
 * True when the build is a PARTIAL whose warning text carries the prune-failure
 * signature — the one case where a plain "Rebuild" retries the same failing
 * deletes and the user should be offered the drop-and-recreate escalation.
 *
 * A `partial` WITHOUT the signature (any other stale-row warning) stays a plain
 * rebuild — the escalation must NOT be offered (leave-alone).
 */
export function isPruneFailurePartial(
  view: CodeGraphBuildView | null | undefined,
): boolean {
  return (
    !!view &&
    view.status === 'partial' &&
    !!view.error_message &&
    view.error_message.includes(PRUNE_FAILURE_SIGNATURE)
  );
}

/**
 * Build the drop-and-recreate command the modal DISPLAYS for the user to run
 * manually (never auto-executed). Uses the analyzer's real `--force-recreate`
 * flag — validated by tests/test_deferral_command_argparse_sweep.py (which now
 * scans .svelte + this .ts via the launcher/src root).
 *
 * A missing/blank project name degrades to a `<project>` placeholder so the
 * command shape is still correct (the user substitutes their project name).
 */
export function buildDropRecreateCommand(projectName: string | null | undefined): string {
  const name = (projectName ?? '').trim() || '<project>';
  return `code-graph-analyze . --project ${name} --force-recreate`;
}
