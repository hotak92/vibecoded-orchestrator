// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 access-matrix overhaul, Phase 6 S-4 (Stream W3).
//
// Pure-TS helpers consumed by ProjectCard.svelte for rendering the
// folder-missing warning banner. The predicates live here (not inline
// in the Svelte component) so vitest can pin the gating contract in
// the pure-node environment without needing @testing-library/svelte +
// jsdom (the project's vitest config is intentionally minimal — see
// `vitest.config.ts`).
//
// Mirror of Rust types from `commands/project_folder_health.rs::
// ProjectFolderFlag` (snake_case for serde parity).

export interface ProjectFolderFlag {
  /** Project UUID (matches `projects.id`). */
  id: string;
  /** Folder path the project was registered against. */
  folder_path: string;
  /**
   * True when the boot probe could not resolve `folder_path` to a
   * directory on the last launcher boot. The flag is point-in-time —
   * a folder that comes back will clear the flag on the next boot.
   */
  folder_missing_at_last_boot: boolean;
}

/**
 * Predicate: should the warning banner render for this project?
 *
 * Returns true ONLY when the boot-probe flag is set. Folded into its
 * own function so the test suite can pin the exact gating contract —
 * future tweaks (debounce, dismissal state, etc.) plug in here without
 * the test suite needing to mount a Svelte component.
 */
export function shouldShowFolderMissingBanner(
  flag: ProjectFolderFlag | null | undefined,
): boolean {
  if (!flag) return false;
  return flag.folder_missing_at_last_boot === true;
}

/**
 * Build the user-facing banner copy. Centralised so the test can pin
 * the wording (any change here is a deliberate UX call, not a
 * drive-by edit). The copy intentionally names the folder path
 * verbatim — users can copy/paste it back into their file manager.
 */
export function folderMissingBannerCopy(flag: ProjectFolderFlag): string {
  return `Folder not found at ${flag.folder_path}. Did you move or delete it?`;
}

/**
 * Map a list of `ProjectFolderFlag` rows (returned by the
 * `read_project_folder_missing_flags` Tauri command) into a quick-
 * lookup id → boolean map. The ProjectCard parent consumes this to
 * decide whether to pass `folderMissing` per card without re-walking
 * the list on every render.
 */
export function buildFolderMissingMap(
  flags: ProjectFolderFlag[],
): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const f of flags) {
    out[f.id] = f.folder_missing_at_last_boot === true;
  }
  return out;
}
