// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 access-matrix Phase 5 item #14 (F-2b) — pure helper that
// builds the `kg_set_collection_access_mode` request payload from the
// per-row action a user takes in CrossProjectAccessTab. Extracting the
// payload-build keeps the Svelte component thin AND lets vitest pin the
// payload shape without needing @testing-library/svelte (the project's
// vitest config is pure-node, no DOM).
//
// Item #14 introduced a fourth `mode` value, `'none'`, that maps to the
// GUI's "Remove access" button. The pre-v0.2.49 code dispatched
// `mode='private'` from both the Grant-write and Remove-access paths
// (the in-line ternary `mode === 'private' ? 'private' : 'private'`
// always resolved to `'private'`), which made the Remove-access button
// a no-op. This helper guarantees the dispatched payload carries the
// caller's mode unchanged.

/// One of the four modes accepted by the backend's
/// `kg_set_collection_access_mode` command. The two used from this
/// surface ('private' for Grant write, 'none' for Remove access) are
/// the F-2b dispatch values.
export type CollectionAccessMode = 'shared' | 'projects' | 'private' | 'none';

/// Request payload shape mirroring `commands::kg::CollectionAccessModeReq`.
export interface CollectionAccessModeReq {
  owner_project_id: string;
  collection: string;
  mode: CollectionAccessMode;
  project_ids: string[];
}

/**
 * Build the `kg_set_collection_access_mode` payload for a per-row
 * "Grant write" / "Remove access" mutation initiated from
 * CrossProjectAccessTab.
 *
 * v0.2.49 access-matrix Phase 5 item #14 (F-2b): the caller's `mode`
 * is passed through unchanged. The pre-v0.2.49 code overwrote it with
 * a constant `'private'` via a dead ternary — that bug was the reason
 * "Remove access" silently re-granted write instead of revoking.
 *
 * `project_ids` is always empty for the per-row dispatch: the mutation
 * applies to the owner row + every peer (subject to F-2c's
 * user-configured preservation on the backend), not a curated subset.
 */
export function buildOwnAccessPayload(
  projectId: string,
  collectionName: string,
  mode: 'private' | 'none',
): CollectionAccessModeReq {
  return {
    owner_project_id: projectId,
    collection: collectionName,
    mode,
    project_ids: [],
  };
}
