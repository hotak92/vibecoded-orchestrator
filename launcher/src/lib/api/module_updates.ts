// SPDX-License-Identifier: AGPL-3.0-or-later
//
// V52-F (v0.2.52): thin API wrapper over the four Tauri commands shipped
// by `launcher/src-tauri/src/commands/module_updates.rs`.
//
// Centralising the `invoke()` calls here makes consumers (`ModuleCatalog`,
// future menubar badge, future per-project summary widgets) testable
// without mocking `@tauri-apps/api/core` directly — tests can mock this
// module instead.
//
// The Rust side already does the heavy lifting (L0 catalog fetch with
// 15-min TTL, atomic update via `update_module_for_project`, UPDATE_DEFERRED
// emission on partial failure). This file only types the wire and
// re-routes calls.

import { invoke } from '$lib/tauri';

/**
 * One installed module with a newer version available in the L0
 * catalog. Returned as a list by `checkModuleUpdatesAvailable`.
 *
 * `project_id` is the empty string `""` for global-scope modules
 * (their `module_installs.project_id` row is NULL).
 *
 * Mirrors the Rust `ModuleUpdateAvailable` struct field-for-field.
 */
export interface ModuleUpdateAvailable {
  project_id: string;
  module_id: string;
  current_version: string;
  available_version: string;
}

/**
 * Outcome of `updateModuleToLatest`. The `kind` discriminator
 * distinguishes the idempotent no-op from the actual update.
 *
 * Mirrors the Rust `UpdateModuleOutcome` enum, serde-tagged on `kind`.
 */
export type UpdateModuleOutcome =
  | { kind: 'already_latest'; version: string }
  | { kind: 'updated'; previous_version: string; new_version: string };

/**
 * Tauri event name emitted by `spawn_module_update_check_loop` whenever
 * a 24h poll discovers one or more installed modules behind catalog.
 * Payload is `ModuleUpdateAvailable[]` (the full summary, including
 * global modules with `project_id = ""`).
 */
export const EVENT_UPDATES_AVAILABLE = 'vct-module-updates-available';

/**
 * Get the per-project list of installed modules whose `module_version`
 * is behind the latest L0 catalog version. Pure DB+cache read on the
 * Rust side; soft-fails to `[]` on catalog-fetch error (the renderer's
 * existing per-tile `can_update` gate is the authoritative surface
 * for individual rows — this command exists for badge-count consumers
 * that want a single number).
 */
export async function checkModuleUpdatesAvailable(
  projectId: string,
): Promise<ModuleUpdateAvailable[]> {
  return invoke<ModuleUpdateAvailable[]>('check_module_updates_available', {
    projectId,
  });
}

/**
 * Idempotent wrapper around `update_module_for_project`. Behaviour:
 *   - `{kind:'already_latest'}` when the install row matches the catalog
 *     version exactly OR is ahead of it (the manual-install case).
 *   - `{kind:'updated'}` after a successful atomic swap.
 *   - Throws on partial failure; a `module_update_partial_failure`
 *     deferral entry is written to UPDATE_DEFERRED.md (best-effort) so
 *     the failure resurfaces at next session start.
 */
export async function updateModuleToLatest(
  projectId: string,
  moduleId: string,
): Promise<UpdateModuleOutcome> {
  return invoke<UpdateModuleOutcome>('update_module_to_latest', {
    projectId,
    moduleId,
  });
}

/**
 * Read the user's opt-out toggle for the 24h auto-poll. Defaults to
 * `true` (auto-check ON) when the setting has never been written.
 */
export async function getModuleUpdateAutoCheckEnabled(): Promise<boolean> {
  return invoke<boolean>('get_module_update_auto_check_enabled');
}

/**
 * Persist the user's opt-out toggle. Takes effect on the next 24h
 * wake-up of the background poll (≤1h latency).
 */
export async function setModuleUpdateAutoCheckEnabled(enabled: boolean): Promise<void> {
  return invoke<void>('set_module_update_auto_check_enabled', { enabled });
}
