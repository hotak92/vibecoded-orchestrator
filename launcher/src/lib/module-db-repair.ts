// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.101 (owner ruling 2026-10-05, Q4): the module tile's "Re-apply DB
// migrations" repair action. The install/update engine soft-fails a
// module's DB-migration apply and records the failure
// (`module_db_migration_failures` in app_state, written by
// installer_engine, cleared by a clean apply); the tile shows the repair
// button ONLY for modules present in that record.
//
// Pure of the Tauri/DOM singletons (injected, same shape as
// `project-maintenance.ts`) so the gating and the act / leave-alone
// behaviour are unit-testable without a component runner.

export interface ModuleDbMigrationFailure {
  module_id: string;
  detail: string;
}

export interface ModuleDbRepairDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  toast: {
    success: (m: unknown) => unknown;
    error: (m: unknown) => unknown;
  };
}

/** The modules whose last DB-migration apply reported errors, by id. */
export function failureMapOf(rows: ModuleDbMigrationFailure[]): Map<string, string> {
  return new Map(rows.map((r) => [r.module_id, r.detail]));
}

export async function loadModuleDbMigrationFailures(
  deps: Pick<ModuleDbRepairDeps, 'invoke'>,
): Promise<Map<string, string>> {
  const rows = await deps.invoke<ModuleDbMigrationFailure[]>(
    'list_module_db_migration_failures',
  );
  return failureMapOf(Array.isArray(rows) ? rows : []);
}

/** Outcome of one repair click: 'ok' when the re-apply ran clean (the
 *  backend has cleared the recorded failure — the caller should re-read
 *  it so the button disappears), 'failed' otherwise. */
export type ReapplyOutcome = 'ok' | 'failed';

export async function reapplyModuleDbMigrationsAction(
  deps: ModuleDbRepairDeps,
  moduleId: string,
): Promise<ReapplyOutcome> {
  try {
    const report = await deps.invoke<{
      applied: string[];
      skipped: string[];
      errors: unknown[];
    }>('apply_module_db_migrations', { moduleId });
    if (report.errors.length > 0) {
      deps.toast.error(
        `Re-applying DB migrations failed: ${report.errors.map((e) => String(e)).join('; ')}`,
      );
      return 'failed';
    }
    deps.toast.success(
      report.applied.length > 0
        ? `Re-applied ${report.applied.length} DB migration(s).`
        : 'DB migrations verified — nothing left to apply.',
    );
    return 'ok';
  } catch (e) {
    deps.toast.error(`Re-applying DB migrations failed: ${e}`);
    return 'failed';
  }
}
