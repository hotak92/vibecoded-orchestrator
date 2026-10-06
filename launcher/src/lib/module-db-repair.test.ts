// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import {
  failureMapOf,
  loadModuleDbMigrationFailures,
  reapplyModuleDbMigrationsAction,
  type ModuleDbRepairDeps,
} from './module-db-repair';

function deps(over: Partial<ModuleDbRepairDeps> = {}): ModuleDbRepairDeps {
  return {
    invoke: vi.fn(async () => ({ applied: [], skipped: [], errors: [] })) as unknown as ModuleDbRepairDeps['invoke'],
    toast: { success: vi.fn(), error: vi.fn() },
    ...over,
  };
}

describe('failureMapOf (the tile gate)', () => {
  it('keys by module id with the recorded detail', () => {
    const m = failureMapOf([
      { module_id: 'vct-rl-reranker', detail: 'm2 failed' },
      { module_id: 'other', detail: 'x' },
    ]);
    expect(m.get('vct-rl-reranker')).toBe('m2 failed');
    expect(m.size).toBe(2);
  });

  it('an empty record gates nothing (the button is shown for no module)', () => {
    expect(failureMapOf([]).size).toBe(0);
  });
});

describe('loadModuleDbMigrationFailures', () => {
  it('reads the recorded failures through the list command', async () => {
    const d = deps({
      invoke: vi.fn(async () => [{ module_id: 'm1', detail: 'boom' }]) as unknown as ModuleDbRepairDeps['invoke'],
    });
    const m = await loadModuleDbMigrationFailures(d);
    expect(d.invoke).toHaveBeenCalledWith('list_module_db_migration_failures');
    expect(m.get('m1')).toBe('boom');
  });
});

describe('reapplyModuleDbMigrationsAction (v0.2.101 Q4)', () => {
  it('invokes the apply command with the module id and reports success', async () => {
    const d = deps({
      invoke: vi.fn(async () => ({ applied: ['0002_x.sql'], skipped: [], errors: [] })) as unknown as ModuleDbRepairDeps['invoke'],
    });
    expect(await reapplyModuleDbMigrationsAction(d, 'vct-rl-reranker')).toBe('ok');
    expect(d.invoke).toHaveBeenCalledWith('apply_module_db_migrations', {
      moduleId: 'vct-rl-reranker',
    });
    expect(d.toast.success).toHaveBeenCalledTimes(1);
    expect(d.toast.error).not.toHaveBeenCalled();
  });

  it('a clean no-op apply (all skipped) is still a success', async () => {
    const d = deps();
    expect(await reapplyModuleDbMigrationsAction(d, 'm')).toBe('ok');
    expect(d.toast.success).toHaveBeenCalledTimes(1);
  });

  it('a report with errors is a failure with the errors surfaced', async () => {
    const d = deps({
      invoke: vi.fn(async () => ({
        applied: [],
        skipped: [],
        errors: ['0002_x.sql: syntax error'],
      })) as unknown as ModuleDbRepairDeps['invoke'],
    });
    expect(await reapplyModuleDbMigrationsAction(d, 'm')).toBe('failed');
    expect(d.toast.error).toHaveBeenCalledTimes(1);
    expect(String((d.toast.error as ReturnType<typeof vi.fn>).mock.calls[0][0])).toContain(
      '0002_x.sql: syntax error',
    );
    expect(d.toast.success).not.toHaveBeenCalled();
  });

  it('a thrown command error is a failure, never a crash into the tile', async () => {
    const d = deps({
      invoke: (async () => {
        throw new Error('db locked');
      }) as unknown as ModuleDbRepairDeps['invoke'],
    });
    expect(await reapplyModuleDbMigrationsAction(d, 'm')).toBe('failed');
    expect(d.toast.error).toHaveBeenCalledTimes(1);
  });
});
