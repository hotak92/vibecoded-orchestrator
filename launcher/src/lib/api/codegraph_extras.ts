// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.47: thin API wrapper over the six Tauri commands shipped by
// Agent A in `launcher/src-tauri/src/commands/project_codegraph_extras.rs`.
//
// Centralising the `invoke()` calls here makes the component testable
// without mocking `@tauri-apps/api/core` directly — tests can mock this
// module instead. It is also where future telemetry / retry policy
// would live if the commands grow them.

import { invoke } from '$lib/tauri';
import type {
  AddExtraPathResult,
  ExtraPath,
  SyncOutcome,
} from '$lib/types/codegraph-extras';

/** GET — newest-first. Returns [] for projects with no rows. */
export async function listExtraPaths(projectId: string): Promise<ExtraPath[]> {
  return invoke<ExtraPath[]>('list_project_codegraph_extra_paths', {
    projectId,
  });
}

/**
 * INSERT one row. Backend returns either the new row OR a
 * `disambiguation_required` payload when `path` is the root of an
 * existing launcher project AND `force` is false / absent.
 *
 * Caller MUST handle the disambiguation variant (see
 * ExtrasDisambiguationModal.svelte) — otherwise the row was NOT
 * persisted and re-calling with `force: true` is the only way to
 * commit it.
 */
export async function addExtraPath(
  projectId: string,
  path: string,
  opts: { label?: string | null; force?: boolean } = {},
): Promise<AddExtraPathResult> {
  return invoke<AddExtraPathResult>('add_project_codegraph_extra_path', {
    projectId,
    path,
    label: opts.label ?? null,
    force: opts.force ?? false,
  });
}

/** DELETE one row. CASCADE on project deletion already covers projects. */
export async function removeExtraPath(
  projectId: string,
  path: string,
): Promise<void> {
  await invoke<void>('remove_project_codegraph_extra_path', {
    projectId,
    path,
  });
}

/** Soft enable/disable. Toggling enabled=false drops the path from
 *  subsequent analyzes; toggling back on re-includes it. */
export async function setExtraPathEnabled(
  projectId: string,
  path: string,
  enabled: boolean,
): Promise<void> {
  await invoke<void>('set_project_codegraph_extra_path_enabled', {
    projectId,
    path,
    enabled,
  });
}

/** Single-path analyze. `incremental=false` forces a full scan; `true`
 *  asks the analyzer to limit enumeration to `git log
 *  <last_indexed_commit>..HEAD` (falls back to full scan on non-git
 *  paths). Resolves when the analyzer subprocess exits. */
export async function syncExtraPath(
  projectId: string,
  path: string,
  incremental: boolean,
): Promise<SyncOutcome> {
  return invoke<SyncOutcome>('sync_project_codegraph_extra_path', {
    projectId,
    path,
    incremental,
  });
}

/**
 * Whole-project re-analyze that visits the project's own repo + every
 * currently-enabled extra path in ONE analyzer invocation. Used after
 * add (`prune_stale=false`), remove / disable (`prune_stale=true`),
 * and the user-initiated "Reindex everything" button.
 *
 * CRITICAL: `prune_stale=true` analyzer runs MUST visit every file
 * meant to be in the project's codegraph — otherwise the prune-stale
 * pass deletes UUIDs for files that simply weren't visited this round.
 * The backend takes care of building the right `--extra-path` list
 * from the post-mutation DB snapshot.
 */
export async function reindexAfterExtrasChange(
  projectId: string,
  pruneStale: boolean,
): Promise<SyncOutcome> {
  return invoke<SyncOutcome>('reindex_project_codegraph_after_extras_change', {
    projectId,
    pruneStale,
  });
}

/**
 * Same invoke shape as the access-matrix panel — granting the current
 * project READ on the existing project's codegraph. Used by the
 * disambiguation modal's "Add as project" branch.
 *
 * Mirrors `CrossProjectAccessTab.svelte`'s call. Errors propagate to the
 * caller.
 */
export async function grantCodegraphReadAccess(
  grantorProjectId: string,
  granteeProjectId: string,
): Promise<void> {
  await invoke<void>('codegraph_grant_access', {
    req: {
      grantor_project_id: grantorProjectId,
      grantee_project_id: granteeProjectId,
      access_level: 'read',
    },
  });
}
