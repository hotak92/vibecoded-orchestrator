// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.42 W6 (UX-1): paid-modules-agnostic UX gating helper.
//
// The launcher is intentionally "agnostic" about paid module configs:
// Store and Modules tabs remain fully browsable regardless of install state.
// However, module-specific controls (RL checkboxes, Reset weights button, etc.)
// should only appear when the module is both:
//
//   (a) installed — an install row exists with a terminal-success status
//       (status === 'installed' | 'running' | 'stopped'); and
//   (b) running   — the container is actively running
//       (status === 'running').
//
// This module provides `moduleIsActive(moduleId, installed)` as the
// single, consistent gate used by every Svelte component that renders
// paid-module-specific UI. Use the derived `rlRerankerActive` store for
// the RL Reranker specifically (avoids repeating the module ID string).
//
// Design notes:
//   - Pure functions only — no Tauri calls, no side effects.
//   - The `installed` array comes from the `modules` store's
//     `$modules.installed` field (already loaded by the parent route).
//   - "active" deliberately requires running, not merely installed/stopped.
//     A stopped container cannot serve RL inference, so showing tuning
//     controls while it's stopped would be misleading.
//   - Separate `moduleIsInstalled` predicate for cases that only need
//     the install check (e.g. License Manager row visibility).

import type { ModuleInstallRow } from '$lib/types/launcher';

/**
 * Returns true when the module is installed AND its container is running.
 *
 * @param moduleId  - The stable wire module ID (e.g. 'vct-rl-reranker').
 * @param installed - The current project's install rows from the modules store.
 */
export function moduleIsActive(
  moduleId: string,
  installed: ModuleInstallRow[],
): boolean {
  const row = installed.find((r) => r.module_id === moduleId);
  return row?.status === 'running';
}

/**
 * Returns true when the module has any install row in a state where the
 * user has already paid + installed and license-management surfaces should
 * remain visible.
 *
 * Statuses treated as "installed": 'installed' | 'running' | 'stopped'
 *   | 'broken' | 'error'.
 *
 * v0.2.42 MF-4 (2026-05-31): 'broken' and 'error' ARE included. Rationale:
 * a user with a paid license whose container failed to start still needs
 * access to the License Manager modal to manage, re-validate, or remove
 * their key. Hiding the row orphans them from key management at the
 * worst possible time (when something already went wrong). The license
 * itself is server-side state, not container state — they're orthogonal.
 *
 * Statuses NOT treated as installed: 'installing' (in-flight; no row to
 * manage yet; the post-install completion will flip the status).
 */
export function moduleIsInstalled(
  moduleId: string,
  installed: ModuleInstallRow[],
): boolean {
  const row = installed.find((r) => r.module_id === moduleId);
  if (!row) return false;
  return (
    row.status === 'installed' ||
    row.status === 'running' ||
    row.status === 'stopped' ||
    row.status === 'broken' ||
    row.status === 'error'
  );
}

/** The stable module ID for the RL Reranker paid module. */
export const RL_RERANKER_MODULE_ID = 'vct-rl-reranker';
