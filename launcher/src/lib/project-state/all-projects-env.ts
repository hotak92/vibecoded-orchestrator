// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.101 (owner ruling 2026-10-05, Q4): the "Re-render env for all
// projects" Preferences action. The v0.2.100 retirement of the all-projects
// button was reversed; the guard is the confirmation dialog, not absence.
//
// Pure of the Tauri/DOM singletons (injected, same shape as
// `project-maintenance.ts`) so the confirm-gating and the act / leave-alone
// behaviour are unit-testable without a component runner.

export interface RefreshAllProjectsEnvResult {
  refreshed: string[];
  refreshed_with_warnings: [string, string[]][];
  failed: [string, string][];
  skipped: string[];
  global_warnings: string[];
}

export interface AllProjectsEnvDeps {
  confirm: (message: string) => boolean;
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  toast: {
    success: (m: unknown) => unknown;
    error: (m: unknown) => unknown;
  };
}

export function refreshAllProjectsEnvConfirmText(projectCount: number): string {
  const n = projectCount > 0 ? `${projectCount} project(s)` : 'every project';
  return (
    `Re-render the environment files of ${n} at once? This rewrites each ` +
    `project's .claude/env and the env block of .claude/settings.json from ` +
    `the launcher's current settings, and can take a while (one subprocess ` +
    `per project). The per-project repair on each project's Settings tab ` +
    `covers a single project if you only need one.`
  );
}

/** One sentence summarising the backend report, whatever its shape. */
export function refreshAllProjectsEnvSummary(r: RefreshAllProjectsEnvResult): string {
  const parts: string[] = [];
  if (r.refreshed.length > 0) parts.push(`${r.refreshed.length} refreshed`);
  if (r.refreshed_with_warnings.length > 0) {
    parts.push(`${r.refreshed_with_warnings.length} refreshed with warnings`);
  }
  if (r.failed.length > 0) parts.push(`${r.failed.length} failed`);
  if (r.skipped.length > 0) parts.push(`${r.skipped.length} skipped (folder missing)`);
  if (parts.length === 0) return 'No projects to re-render.';
  const head = `Env re-render finished: ${parts.join(', ')}.`;
  const firstFailure = r.failed[0]?.[1] ?? r.refreshed_with_warnings[0]?.[1]?.[0];
  return firstFailure ? `${head} First issue: ${firstFailure}` : head;
}

/** Returns true when the refresh ran (even with per-project failures),
 *  false when the user declined or the command itself failed. */
export async function refreshAllProjectsEnvAction(
  deps: AllProjectsEnvDeps,
  projectCount: number,
): Promise<boolean> {
  if (!deps.confirm(refreshAllProjectsEnvConfirmText(projectCount))) return false;
  try {
    const r = await deps.invoke<RefreshAllProjectsEnvResult>('refresh_all_projects_env');
    const summary = refreshAllProjectsEnvSummary(r);
    if (r.failed.length > 0 || r.global_warnings.length > 0) {
      deps.toast.error(summary);
    } else {
      deps.toast.success(summary);
    }
    return true;
  } catch (e) {
    deps.toast.error(`Re-rendering every project's environment failed: ${e}`);
    return false;
  }
}
