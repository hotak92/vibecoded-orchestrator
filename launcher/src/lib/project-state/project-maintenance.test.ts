// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import {
  openProjectLogsAction,
  refreshEnvConfirmText,
  refreshProjectEnvAction,
  type MaintenanceDeps,
} from './project-maintenance';
import {
  NODE_ACCESS_NOT_ENFORCED_NOTICE,
  accessNoticeFor,
} from '../kg/node-access-notice';

function deps(over: Partial<MaintenanceDeps> = {}): MaintenanceDeps {
  return {
    confirm: vi.fn(() => true),
    invoke: vi.fn(async () => ({
      kg_access_list: [],
      code_graph_access_list: [],
      warnings: [],
    })) as unknown as MaintenanceDeps['invoke'],
    toast: { success: vi.fn(), error: vi.fn() },
    ...over,
  };
}

describe('refreshProjectEnvAction (#15, per-project only)', () => {
  it('confirms, then calls the PER-PROJECT command with this project id', async () => {
    const d = deps();
    expect(await refreshProjectEnvAction(d, 'p1', 'Alpha Proj')).toBe(true);
    expect(d.confirm).toHaveBeenCalledWith(refreshEnvConfirmText('Alpha Proj'));
    expect(d.invoke).toHaveBeenCalledTimes(1);
    expect(d.invoke).toHaveBeenCalledWith('refresh_project_env', { projectId: 'p1' });
    expect(d.toast.success).toHaveBeenCalledTimes(1);
  });

  it('never calls the all-projects command', async () => {
    const d = deps();
    await refreshProjectEnvAction(d, 'p1', 'X');
    const cmds = (d.invoke as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(cmds).not.toContain('refresh_all_projects_env');
  });

  it('declining the confirmation does nothing (leave-alone)', async () => {
    const d = deps({ confirm: vi.fn(() => false) });
    expect(await refreshProjectEnvAction(d, 'p1', 'X')).toBe(false);
    expect(d.invoke).not.toHaveBeenCalled();
    expect(d.toast.success).not.toHaveBeenCalled();
  });

  it('surfaces warnings and failures as error toasts', async () => {
    const w = deps({
      invoke: (async () => ({
        kg_access_list: [],
        code_graph_access_list: [],
        warnings: ['stale'],
      })) as unknown as MaintenanceDeps['invoke'],
    });
    await refreshProjectEnvAction(w, 'p', 'X');
    expect(w.toast.error).toHaveBeenCalledTimes(1);
    expect(w.toast.success).not.toHaveBeenCalled();

    const f = deps({
      invoke: (async () => {
        throw new Error('boom');
      }) as unknown as MaintenanceDeps['invoke'],
    });
    expect(await refreshProjectEnvAction(f, 'p', 'X')).toBe(false);
    expect(f.toast.error).toHaveBeenCalledTimes(1);
  });
});

describe('openProjectLogsAction (#9)', () => {
  it('passes the project id (not a global path) to orchestrator_open_logs', async () => {
    const d = deps();
    await openProjectLogsAction(d, 'p9');
    expect(d.invoke).toHaveBeenCalledWith('orchestrator_open_logs', { projectId: 'p9' });
  });
  it('toasts the refusal when the folder does not exist', async () => {
    const d = deps({
      invoke: (async () => {
        throw 'no logs yet';
      }) as unknown as MaintenanceDeps['invoke'],
    });
    expect(await openProjectLogsAction(d, 'p9')).toBe(false);
    expect(d.toast.error).toHaveBeenCalledWith('no logs yet');
  });
});

describe('node access notice (#12)', () => {
  it('names the owner deferral and v0.2.102; only node scopes carry it', () => {
    expect(NODE_ACCESS_NOT_ENFORCED_NOTICE).toMatch(/not enforced/);
    expect(NODE_ACCESS_NOT_ENFORCED_NOTICE).toMatch(/v0\.2\.102/);
    expect(accessNoticeFor('node')).toBe(NODE_ACCESS_NOT_ENFORCED_NOTICE);
    expect(accessNoticeFor('node-bulk')).toBe(NODE_ACCESS_NOT_ENFORCED_NOTICE);
    expect(accessNoticeFor('collection')).toBeNull();
  });
});

// Wiring: the components must actually call the helpers (a CallExpression
// cannot be satisfied by a comment).
const here = dirname(fileURLToPath(import.meta.url));
function callNames(file: string): Set<string> {
  const src = readFileSync(resolve(here, file), 'utf8');
  const ast = parse(src, { modern: true }) as unknown;
  const out = new Set<string>();
  const visit = (n: unknown): void => {
    if (n === null || typeof n !== 'object') return;
    if (Array.isArray(n)) return n.forEach(visit);
    const o = n as Record<string, unknown>;
    if (o.type === 'CallExpression') {
      const c = o.callee as Record<string, unknown>;
      if (c.type === 'Identifier') out.add(String(c.name));
    }
    for (const [k, v] of Object.entries(o)) if (k !== 'parent' && k !== 'loc') visit(v);
  };
  visit(ast);
  return out;
}

describe('wiring', () => {
  it('SettingsTab calls both maintenance actions', () => {
    const c = callNames('SettingsTab.svelte');
    expect(c.has('refreshProjectEnvAction')).toBe(true);
    expect(c.has('openProjectLogsAction')).toBe(true);
  });
  it('the KG page feeds the notice to the access modal', () => {
    expect(callNames('../../routes/kg/+page.svelte').has('accessNoticeFor')).toBe(true);
  });
});
