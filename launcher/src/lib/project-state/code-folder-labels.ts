// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-AA (owner naming, 2026-10-13): the user-visible wording of the
// extra code-graph paths panel (ExtraCodegraphPathsPanel.svelte). The option
// is "Add code folder" because a folder added there now governs TWO things:
// it is indexed into the project's code graph, AND isolated subagents may get
// worktrees there through lane reservations (vco_lib/lane_reservation.py
// allow-lists the project plus its ENABLED code folders).
//
// Internal names are unchanged — the `project_codegraph_extra_paths` table,
// the hub's `code_graph_extra_paths` field and the component file names — so
// no migration is involved. One home for the strings, so the panel, its
// modals' titles and the tests cannot drift apart.

export const CODE_FOLDER = {
  heading: 'Code folders',
  addButton: 'Add code folder',
  addButtonBusy: 'Picking…',
  addAria: 'Add a code folder',
  help: "Indexed in this project's code graph, and subagents may get isolated worktrees there.",
  details:
    'Use it for folders that are not launcher projects themselves, e.g. a sibling git repo ' +
    'you want to query alongside your own code. Edits to files in these folders are ' +
    'auto-indexed. Symlinks are resolved on add — folders show their real on-disk location.',
  empty: 'No code folders added. Click "Add code folder" to index a folder.',
  listAria: 'Code folders',
  pickerTitle: 'Select a code folder',
  disabledAria: 'This code folder is currently disabled',
} as const;

/** Sync-modal title after a folder is enabled again. */
export function reenabledTitle(): string {
  return 'Re-syncing after re-enabling code folder';
}

/** Sync-modal title after a folder is disabled. */
export function disabledTitle(projectName: string): string {
  return `Re-syncing ${projectName} codegraph after disabling code folder`;
}

/** Sync-modal title after a folder is removed. */
export function removedTitle(projectName: string): string {
  return `Re-syncing ${projectName} codegraph after code folder removal`;
}

/** The confirm() text before removing a folder. */
export function removeConfirm(path: string): string {
  return (
    `Remove code folder '${path}' from this project?\n\n` +
    'Codegraph entries originally sourced from this folder will be ' +
    'pruned in the follow-up re-sync. Isolated subagents can no longer ' +
    'get worktrees there.'
  );
}
