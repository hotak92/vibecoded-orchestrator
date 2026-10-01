// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: delete one diagram snapshot from the Diagrams timeline.
//
// `delete_diagram_snapshot` had no caller: snapshots could be saved and
// restored but never removed, so the timeline only ever grew. Deleting is
// destructive (the snapshot cannot be recovered), so it is confirmed first and
// a declined confirm leaves the snapshot alone. The decision is pure (the
// confirm and the command are injected) so both branches are real asserts
// under node; the tab passes `window.confirm` and the Tauri call.

import type { DiagramSnapshotRow } from '$lib/types/project-state';

export type SnapshotDeleteOutcome = 'deleted' | 'cancelled';

export interface SnapshotDeleteDeps {
  confirm: (message: string) => boolean;
  /** Performs the delete for one snapshot id. The tab supplies the literal
   *  `invoke('delete_diagram_snapshot', ...)` call, so the command name stays
   *  visible to the invoke-name census (no dynamic `invoke(cmd)` here). */
  remove: (snapshotId: number) => Promise<unknown>;
}

/** The confirm text: names the snapshot and what is (and is not) lost. */
export function snapshotDeleteMessage(snap: DiagramSnapshotRow, when: string): string {
  const what = snap.label ? `"${snap.label}" (${snap.trigger})` : `the ${snap.trigger} snapshot`;
  return (
    `Delete ${what} from ${when}? ` +
    'The diagram file itself is not touched, but this snapshot cannot be recovered.'
  );
}

/**
 * Confirm, then delete. Resolves `'cancelled'` without calling the backend
 * when the user declines; rejects with the backend's error when the delete
 * itself fails (the caller toasts it).
 */
export async function deleteSnapshotWithConfirm(
  snap: DiagramSnapshotRow,
  when: string,
  deps: SnapshotDeleteDeps,
): Promise<SnapshotDeleteOutcome> {
  if (!deps.confirm(snapshotDeleteMessage(snap, when))) return 'cancelled';
  await deps.remove(snap.id);
  return 'deleted';
}
