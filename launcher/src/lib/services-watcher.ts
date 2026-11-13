// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.106 — Preferences → Services: "Restart stopped services
// automatically" (the `launcher.services_watcher_enabled` app_state row).
//
// The pure half of the toggle, extracted so it can be tested (same shape
// as engine-autostart.ts): vitest runs under `environment: node` in this
// repo, so the logic worth pinning would otherwise live only inside a
// `.svelte` file.
//
// What is worth pinning is the DEFAULT. The backend's
// `get_services_watcher_enabled` resolves an absent `app_state` row to
// the shipped default (ON), so the checkbox must render ON while the
// answer is in flight and whenever the answer cannot be obtained.

/** Shipped default, ON.
 *
 * MUST MATCH `commands/services_watcher_pref.rs::
 * DEFAULT_SERVICES_WATCHER` and the Rust readers' absent-row default
 * (the launcher watcher's `read_watcher_enabled` and the hub's
 * `infra_watchdog::watcher_enabled`).
 */
export const DEFAULT_SERVICES_WATCHER = true;

/**
 * The checkbox state to render.
 *
 * `null` means "not answered yet, or the command was unreachable" — both
 * resolve to the shipped default rather than to `false` (a toggle that
 * flashed OFF would tell the user self-healing is off when it is not).
 */
export function resolveServicesWatcher(loaded: boolean | null | undefined): boolean {
  return typeof loaded === 'boolean' ? loaded : DEFAULT_SERVICES_WATCHER;
}

/**
 * The one-line hint under the toggle. Says what the switch does, that ONE
 * switch governs both automatic restarters, and that a service stopped
 * from the Services page is left down (the pause marker) until Start.
 */
export function servicesWatcherHint(enabled: boolean): string {
  return enabled
    ? 'When a VCO-managed service stops on its own, the launcher restarts it automatically (first try after about 30 s, then with longer waits, up to 3 attempts). A service you stop from the Services page is left down — both this restarter and the hub watchdog honor that pause until you press Start. This switch stops nothing; it only disables automatic restarts.'
    : 'Automatic restarts are off: neither the launcher watcher nor the hub watchdog (a separate mechanism that reads the same switch) will bring a stopped service back. Services you start yourself keep running.';
}
