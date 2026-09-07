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
 * v0.2.92 (BLOCKER-2): identity comes from `--from-resolver`, not from the
 * DISPLAY name. `--force-recreate` DROPS the five `<prefix>_Code*` classes,
 * and the analyzer sanitizes whatever `--project` receives into that prefix —
 * so passing the display name targeted `VibeCodedOrchestrator_Code*` on a
 * project whose binding says `VCODev_Code*`, rebuilding a family the project
 * does not read (and, on a collision, dropping one another project does).
 * `--from-resolver` asks vct-hub for `collection_prefix`, which is exactly
 * what the per-edit hooks and the launcher's own analyzer spawns use. It also
 * removes the unquoted-`${name}` bug: a display name with spaces produced a
 * command that parsed as a different project.
 */
export function buildDropRecreateCommand(): string {
  return 'code-graph-analyze . --from-resolver --force-recreate';
}
