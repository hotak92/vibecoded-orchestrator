// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.95, ruling R2 — Preferences → Startup: "Start the launcher with a
// Claude Code session".
//
// The pure half of the toggle, extracted so it can be tested: vitest runs
// under `environment: node` in this repo, so a component test is not
// available and the logic worth pinning would otherwise live only inside a
// `.svelte` file.
//
// What is worth pinning is the DEFAULT. The backend's
// `get_launcher_session_autostart` resolves an absent `app_state` row to the
// shipped default (ON), so the checkbox must render ON while the answer is in
// flight and whenever the answer cannot be obtained. Rendering OFF in either
// case would tell the user the launcher will not start with their session
// while it actually will — the toggle would be lying about shipped behaviour,
// which is worse than not having it.

/** Shipped default, per the owner's 2026-09-10 ruling.
 *
 *  MUST MATCH `commands/session_autostart.rs::DEFAULT_SESSION_AUTOSTART` and
 *  `vco_lib/launcher_ensure.py::DEFAULT_SESSION_AUTOSTART`.
 */
export const DEFAULT_SESSION_AUTOSTART = true;

/**
 * The checkbox state to render.
 *
 * `null` means "not answered yet, or the command was unreachable" — both
 * resolve to the shipped default rather than to `false`.
 */
export function resolveSessionAutostart(loaded: boolean | null | undefined): boolean {
  return typeof loaded === 'boolean' ? loaded : DEFAULT_SESSION_AUTOSTART;
}

/**
 * The one-line hint under the toggle. Says what the switch does AND when it
 * takes effect — a preference read by a different process at a later moment
 * is exactly the kind whose timing users guess wrong.
 */
export function sessionAutostartHint(enabled: boolean): string {
  return enabled
    ? 'The launcher starts hidden in the tray when you open a project in VS Code or start a Claude Code session, if it is not already running. No window opens and nothing takes focus.'
    : 'The launcher will not be started for you. Open it yourself from the tray, the desktop entry, or the terminal.';
}
