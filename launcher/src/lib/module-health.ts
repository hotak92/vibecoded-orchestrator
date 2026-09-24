// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (lane V) — the module-tile health pill.
//
// A module manifest's `runtime.health_check` is polled by the hub
// (`vct-hub/src/module_health.rs`); the launcher reads the result through
// the `module_health_snapshot` Tauri command (a projection of the hub's
// `GET /api/v1/modules/catalog`). This file turns that snapshot into the
// pill a tile shows. Pure — no Tauri, no DOM — so every state is tested in
// `module-health.test.ts`.
//
// The one rule that matters: UNKNOWN IS NEVER SHOWN AS DOWN. "Down" means
// a probe ran and failed. Everything else — not probed yet, a check the hub
// cannot run (stdio_ping), a refused URL, or the hub itself unreachable —
// is "unknown", with the reason in the tooltip.

export type HealthState = 'up' | 'down' | 'unknown';

/** One module instance's health, as the hub serves it. */
export interface ModuleHealth {
  state: HealthState;
  last_checked: string | null;
  last_error: string | null;
}

/** `module_health_snapshot`'s value for one module. */
export interface ModuleHealthView {
  health: ModuleHealth | null;
  project_health: Record<string, ModuleHealth>;
}

export type HealthSnapshot = Record<string, ModuleHealthView>;

export interface HealthPill {
  state: HealthState;
  label: string;
  tooltip: string;
}

export const HEALTH_LABELS: Record<HealthState, string> = {
  up: 'Running',
  down: 'Down',
  unknown: 'Status unknown',
};

function when(iso: string | null): string {
  if (!iso) return '';
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? iso : t.toLocaleTimeString();
}

/**
 * The instance a tile shows: the current project's own instance when the
 * module is installed per project, else the machine-wide one (a bundled or
 * global module serves every project).
 */
export function pickModuleHealth(
  view: ModuleHealthView | undefined,
  projectId: string | null,
): ModuleHealth | null {
  if (!view) return null;
  if (projectId && view.project_health[projectId]) return view.project_health[projectId];
  return view.health ?? null;
}

/**
 * The pill for `moduleId`, or `null` when the hub reports nothing for it
 * (the module declares no health check, or it is not active here) — the
 * tile then shows no pill at all.
 *
 * `hubError` is the reason the LAST read failed; the previous snapshot is
 * then shown as unknown ("could not be refreshed"), never as its stale
 * state — a module that was up an hour ago is not known to be up now.
 */
export function resolveModuleHealthPill(
  snapshot: HealthSnapshot | null,
  moduleId: string,
  projectId: string | null,
  hubError: string | null = null,
): HealthPill | null {
  const health = pickModuleHealth(snapshot?.[moduleId], projectId);
  if (!health) return null;
  if (hubError) {
    return {
      state: 'unknown',
      label: HEALTH_LABELS.unknown,
      tooltip: `Health could not be refreshed — the hub is not reachable (${hubError}).`,
    };
  }
  const state: HealthState =
    health.state === 'up' || health.state === 'down' ? health.state : 'unknown';
  const at = when(health.last_checked);
  let tooltip: string;
  if (state === 'up') {
    tooltip = `Health check passed${at ? ` at ${at}` : ''}.`;
  } else if (state === 'down') {
    tooltip = `Health check failed${at ? ` at ${at}` : ''}: ${health.last_error ?? 'no details'}.`;
  } else {
    tooltip = health.last_error
      ? `Not checked: ${health.last_error}.`
      : 'Not checked yet — the hub probes it shortly.';
  }
  return { state, label: HEALTH_LABELS[state], tooltip };
}

/** How often an open Modules page re-reads the hub's snapshot. */
export const HEALTH_REFRESH_MS = 30_000;
