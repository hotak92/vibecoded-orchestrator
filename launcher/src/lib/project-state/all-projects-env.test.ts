// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import {
  refreshAllProjectsEnvAction,
  refreshAllProjectsEnvConfirmText,
  refreshAllProjectsEnvSummary,
  type AllProjectsEnvDeps,
  type RefreshAllProjectsEnvResult,
} from './all-projects-env';

function okReport(): RefreshAllProjectsEnvResult {
  return {
    refreshed: ['Alpha', 'Beta'],
    refreshed_with_warnings: [],
    failed: [],
    skipped: ['Gone'],
    global_warnings: [],
  };
}

function deps(over: Partial<AllProjectsEnvDeps> = {}): AllProjectsEnvDeps {
  return {
    confirm: vi.fn(() => true),
    invoke: vi.fn(async () => okReport()) as unknown as AllProjectsEnvDeps['invoke'],
    toast: { success: vi.fn(), error: vi.fn() },
    ...over,
  };
}

describe('refreshAllProjectsEnvAction (v0.2.101 Q4)', () => {
  it('confirms, then calls the all-projects command with no args', async () => {
    const d = deps();
    expect(await refreshAllProjectsEnvAction(d, 2)).toBe(true);
    expect(d.confirm).toHaveBeenCalledWith(refreshAllProjectsEnvConfirmText(2));
    expect(d.confirm).toHaveBeenCalledTimes(1);
    expect(d.invoke).toHaveBeenCalledTimes(1);
    expect(d.invoke).toHaveBeenCalledWith('refresh_all_projects_env');
    expect(d.toast.success).toHaveBeenCalledTimes(1);
    expect(d.toast.error).not.toHaveBeenCalled();
  });

  it('declining the confirmation invokes nothing (leave-alone)', async () => {
    const d = deps({ confirm: vi.fn(() => false) });
    expect(await refreshAllProjectsEnvAction(d, 2)).toBe(false);
    expect(d.invoke).not.toHaveBeenCalled();
    expect(d.toast.success).not.toHaveBeenCalled();
  });

  it('surfaces per-project failures as an error toast naming the first issue', async () => {
    const d = deps({
      invoke: (async () => ({
        ...okReport(),
        refreshed: [],
        failed: [['Alpha', 'boom']],
      })) as unknown as AllProjectsEnvDeps['invoke'],
    });
    expect(await refreshAllProjectsEnvAction(d, 2)).toBe(true);
    expect(d.toast.error).toHaveBeenCalledTimes(1);
    expect(String((d.toast.error as ReturnType<typeof vi.fn>).mock.calls[0][0])).toContain('boom');
    expect(d.toast.success).not.toHaveBeenCalled();
  });

  it('surfaces a command failure as an error toast and returns false', async () => {
    const d = deps({
      invoke: (async () => {
        throw new Error('db locked');
      }) as unknown as AllProjectsEnvDeps['invoke'],
    });
    expect(await refreshAllProjectsEnvAction(d, 2)).toBe(false);
    expect(d.toast.error).toHaveBeenCalledTimes(1);
  });
});

describe('refreshAllProjectsEnvSummary', () => {
  it('names every non-empty bucket', () => {
    const s = refreshAllProjectsEnvSummary({
      refreshed: ['a'],
      refreshed_with_warnings: [['b', ['w']]],
      failed: [['c', 'x']],
      skipped: ['d'],
      global_warnings: [],
    });
    expect(s).toContain('1 refreshed');
    expect(s).toContain('1 refreshed with warnings');
    expect(s).toContain('1 failed');
    expect(s).toContain('1 skipped');
    expect(s).toContain('First issue: x');
  });

  it('an empty report is stated, not rendered as success', () => {
    expect(
      refreshAllProjectsEnvSummary({
        refreshed: [],
        refreshed_with_warnings: [],
        failed: [],
        skipped: [],
        global_warnings: [],
      }),
    ).toBe('No projects to re-render.');
  });
});

describe('refreshAllProjectsEnvConfirmText', () => {
  it('names the count when known, "every project" when not', () => {
    expect(refreshAllProjectsEnvConfirmText(3)).toContain('3 project(s)');
    expect(refreshAllProjectsEnvConfirmText(0)).toContain('every project');
  });
});
