// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.103, review R19 F4 — Preferences → Startup: "Start the container
// engine automatically".
//
// The pure half of the toggle, extracted so it can be tested (same shape as
// session-autostart.ts): vitest runs under `environment: node` in this repo,
// so a component test is not available and the logic worth pinning would
// otherwise live only inside a `.svelte` file.
//
// What is worth pinning is the DEFAULT. The backend's
// `get_container_engine_autostart` resolves an absent `app_state` row to the
// shipped default (ON), so the checkbox must render ON while the answer is
// in flight and whenever the answer cannot be obtained. Rendering OFF in
// either case would tell the user engines will not be started when they
// will — the toggle would be lying about shipped behaviour.

/** Shipped default, per the owner's 2026-10-15 ruling.
 *
 *  MUST MATCH `commands/engine_autostart_pref.rs::DEFAULT_ENGINE_AUTOSTART`,
 *  `vct-launcher-core/src/services/engine_autostart.rs::
 *  DEFAULT_ENGINE_AUTOSTART` and
 *  `vco_lib/container_engine_start.py::DEFAULT_ENGINE_AUTOSTART`.
 */
export const DEFAULT_ENGINE_AUTOSTART = true;

/**
 * The checkbox state to render.
 *
 * `null` means "not answered yet, or the command was unreachable" — both
 * resolve to the shipped default rather than to `false`.
 */
export function resolveEngineAutostart(loaded: boolean | null | undefined): boolean {
  return typeof loaded === 'boolean' ? loaded : DEFAULT_ENGINE_AUTOSTART;
}

/**
 * The one-line hint under the toggle. Says what the switch does AND when it
 * takes effect — and names the env override, so a user who set the env knob
 * finds why the checkbox alone does not re-enable it.
 */
export function engineAutostartHint(enabled: boolean): string {
  return enabled
    ? 'Docker Desktop / the podman machine is started automatically when it is down and a VCO session, the launcher or the hub starts — only for the runtime this install recorded, never more than one start at a time. The env variable VCT_CONTAINER_ENGINE_AUTOSTART=0 overrides this to off.'
    : 'The container engine will not be started for you; start Docker Desktop or the podman machine yourself when a session needs it.';
}
