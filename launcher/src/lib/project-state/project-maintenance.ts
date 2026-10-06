// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Per-project maintenance actions on the project Settings tab. Pure of the
// Tauri/DOM singletons (they are injected) so the confirmation and the
// act / leave-alone behaviour are unit-testable without a component runner.
//
// The all-projects variant lives in `all-projects-env.ts` (Preferences
// surface): the v0.2.100 "no all-projects button" ruling was reversed by
// the owner on 2026-10-05 (v0.2.101 Q4) — it is confirm-gated there.

export interface RefreshProjectEnvResult {
  kg_access_list: string[];
  code_graph_access_list: string[];
  warnings: string[];
}

export interface MaintenanceDeps {
  confirm: (message: string) => boolean;
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  toast: {
    success: (m: unknown) => unknown;
    error: (m: unknown) => unknown;
  };
}

export function refreshEnvConfirmText(projectName: string): string {
  return (
    `Re-render the environment files of "${projectName}"? This rewrites ` +
    `its .claude/env and the env block of .claude/settings.json from the ` +
    `launcher's current settings. Only this project is touched.`
  );
}

/** Returns true when the refresh ran (even with warnings), false otherwise. */
export async function refreshProjectEnvAction(
  deps: MaintenanceDeps,
  projectId: string,
  projectName: string,
): Promise<boolean> {
  if (!deps.confirm(refreshEnvConfirmText(projectName))) return false;
  try {
    const r = await deps.invoke<RefreshProjectEnvResult>('refresh_project_env', {
      projectId,
    });
    if (r.warnings.length > 0) {
      deps.toast.error(
        `Env re-rendered with ${r.warnings.length} warning(s): ${r.warnings[0]}`,
      );
    } else {
      deps.toast.success(`Re-rendered the environment of "${projectName}"`);
    }
    return true;
  } catch (e) {
    deps.toast.error(`Re-rendering the environment failed: ${e}`);
    return false;
  }
}

/** Opens the project's own `.claude/logs` folder in the file manager. */
export async function openProjectLogsAction(
  deps: Pick<MaintenanceDeps, 'invoke' | 'toast'>,
  projectId: string,
): Promise<boolean> {
  try {
    await deps.invoke<void>('orchestrator_open_logs', { projectId });
    return true;
  } catch (e) {
    deps.toast.error(e);
    return false;
  }
}
