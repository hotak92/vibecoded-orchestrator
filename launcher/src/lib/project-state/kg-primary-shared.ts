// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.105: the Identity tab's "this project's primary KG is the SHARED
// collection" banner and its one-click repair.
//
// Detection is the Python SSOT (`vco_lib.kg_primary_shared`), reached through
// the `get_kg_primary_shared_status` command; the repair is the existing
// `update_project_identity` command with `kg_collection = expected` (it
// re-points the primary binding and re-projects the project env; no data is
// moved or deleted). Nothing here re-derives the rule.

import type { UpdateProjectIdentityRequest } from '$lib/types/identity';

/** The ledger condition this banner repairs (registered in
 *  `vco_lib/deferral_conditions.toml`; pinned there by
 *  `kg-primary-shared.test.ts`). */
export const CID_KG_PRIMARY_BOUND_TO_SHARED = 'kg_primary_bound_to_shared';

/** Mirror of the Rust `KgPrimarySharedStatus` (project_identity.rs). */
export interface KgPrimarySharedStatus {
  bound_to_shared: boolean;
  bound: string;
  expected: string;
  expected_is_shared: boolean;
}

/** The `update_project_identity` request that repairs the binding, or null
 *  when there is nothing to repair or no one-click target (a project named
 *  like the orchestrator derives the shared name itself). */
export function repairRequest(status: KgPrimarySharedStatus | null): UpdateProjectIdentityRequest | null {
  if (!status || !status.bound_to_shared || status.expected_is_shared || !status.expected.trim()) {
    return null;
  }
  return { kg_collection: status.expected, code_graph_project: null };
}

/** The banner sentence (null = no banner). */
export function bannerMessage(status: KgPrimarySharedStatus | null): string | null {
  if (!status || !status.bound_to_shared) return null;
  const head =
    `This project's primary KG collection is ${status.bound}, the SHARED KG every ` +
    'project reads. Its own notes land in the shared corpus and its searches treat ' +
    'the shared corpus as its own.';
  if (status.expected_is_shared) {
    return `${head} Type a KG collection name of its own above and save.`;
  }
  return (
    `${head} Repair re-points the binding to ${status.expected}; nothing is moved or ` +
    'deleted, and notes already written stay in the shared collection. Re-sync the ' +
    "project's knowledge afterwards to fill its own collection."
  );
}
