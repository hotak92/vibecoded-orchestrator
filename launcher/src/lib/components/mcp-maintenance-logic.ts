// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 WP-D + WP-E surfacing — pure decisions for McpMaintenanceSection.
//
// Extracted for the same reason as `codegraph-build-banner-logic.ts`: the repo
// has no jsdom / testing-library, so anything that DECIDES something lives in a
// plain .ts and is unit-tested, and the .svelte file is markup over it.
//
// The three facts this file owns:
//   1. npx presence is TRI-state — `true` / `false` / `null` ("could not ask").
//      Rendering `null` the same as `false` accuses a machine of a problem it
//      may not have; only `false` is actionable.
//   2. `command_resolvable === false` is the "cannot spawn" tag. `null` renders
//      nothing (positive evidence only — the same rule the badge follows).
//   3. A retirement badge is DURABLE metadata written by the convergence
//      engine; the sentence comes from the badge itself, never recomposed.

/** Mirrors `maintenance.rs::McpRegistrationEntry`. */
export interface McpRegistrationEntryView {
  name: string;
  present: boolean;
  path_matches_install: boolean;
  command: string;
  command_resolvable: boolean | null;
}

/** Mirrors `maintenance.rs::McpRegistrationStatusReport` (npx fields only). */
export interface NpxProbeView {
  npx_present: boolean | null;
  npx_path: string;
  remediation: string;
}

/** Mirrors `project_mcp_servers.rs::ProjectMcpServer` (the fields used here). */
export interface ProjectMcpServerView {
  mcp_name: string;
  enabled: boolean;
  config: Record<string, unknown> | null;
}

/**
 * The durable retirement badge key inside `config_json`.
 * MUST MATCH `project_mcp_servers.rs::MCP_RETIRED_CONFIG_KEY`.
 */
export const MCP_RETIRED_CONFIG_KEY = '_vct_retired';

/** How the npx line should read. `unknown` is NOT a synonym for `missing`. */
export type NpxState = 'present' | 'missing' | 'unknown';

export function npxState(report: NpxProbeView | null | undefined): NpxState {
  if (!report) return 'unknown';
  if (report.npx_present === true) return 'present';
  if (report.npx_present === false) return 'missing';
  return 'unknown';
}

/**
 * True when an entry earns the red "cannot spawn" tag.
 *
 * ONLY `false` qualifies. `null` means "not applicable (path-shaped command)"
 * or "the probe could not run" — neither is evidence, and neither may accuse a
 * working MCP of being broken.
 */
export function showsCannotSpawnTag(
  entry: Pick<McpRegistrationEntryView, 'command_resolvable'>,
): boolean {
  return entry.command_resolvable === false;
}

/** Rows carrying the durable retirement badge. */
export function retiredRows<T extends ProjectMcpServerView>(rows: T[]): T[] {
  return rows.filter(
    (r) => !!r.config && Object.hasOwn(r.config, MCP_RETIRED_CONFIG_KEY),
  );
}

/**
 * The retirement sentence, taken from the badge the engine WROTE.
 *
 * `convergence.rs::retired_badge` composes it from the shared
 * `mcp_scan_rules.toml` `[deprecated.*]` registry, so reading it back verbatim
 * is what keeps the GUI's wording from drifting off the table that drove the
 * retirement. The fallbacks exist only for a badge written by a build that
 * predates the composed `badge` field.
 */
export function retiredBadgeText(row: ProjectMcpServerView): string {
  const badge = row.config?.[MCP_RETIRED_CONFIG_KEY] as
    | { removed_in?: unknown; reason?: unknown; badge?: unknown }
    | undefined;
  if (typeof badge?.badge === 'string' && badge.badge) return badge.badge;
  if (typeof badge?.removed_in === 'string' && badge.removed_in) {
    return `retired in ${badge.removed_in}`;
  }
  return 'retired';
}

/**
 * True when a retired row is currently ENABLED — i.e. the user deliberately
 * turned it back on after the retirement. The convergence engine's idempotence
 * leaves such a row alone, and the UI says so rather than looking like a bug.
 */
export function isUserReEnabledAfterRetirement(row: ProjectMcpServerView): boolean {
  return retiredRows([row]).length === 1 && row.enabled;
}
