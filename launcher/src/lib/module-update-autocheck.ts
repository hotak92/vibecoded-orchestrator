// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.100 - Preferences: "Check for module updates automatically".
//
// The pure half of the toggle (vitest runs under `environment: node`, so the
// logic worth pinning lives here, not inside the `.svelte` file).
//
// The backend (`get_module_update_auto_check_enabled`) resolves a never-written
// setting to ON, and the 24 h poll then runs. So while the answer is in flight,
// or when it cannot be obtained, the switch must render ON: rendering OFF would
// tell the user nothing is being checked while the poll is in fact running.

/** Shipped default. MUST MATCH `commands/module_updates.rs` (absent key = ON). */
export const DEFAULT_MODULE_UPDATE_AUTO_CHECK = true;

export function resolveModuleUpdateAutoCheck(loaded: boolean | null | undefined): boolean {
  return typeof loaded === 'boolean' ? loaded : DEFAULT_MODULE_UPDATE_AUTO_CHECK;
}

/** Says what the switch does AND when it takes effect. */
export function moduleUpdateAutoCheckHint(enabled: boolean): string {
  return enabled
    ? 'Once a day the launcher checks whether the modules you installed have a newer version and shows a count on the Modules entry in the sidebar. Nothing is updated without you.'
    : 'The launcher will not check for module updates by itself. Use the refresh button on the Modules page to check on demand. A change takes effect at the next daily check (within an hour).';
}
