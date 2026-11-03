// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.105 — Claude accounts: the wire shape of every call, the pure
// verdicts both views render, and the wiring of the two views (parsed with
// the Svelte compiler, so a comment cannot satisfy it).

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn() }));

import { invoke } from '$lib/tauri';
import {
  addAccount,
  formatDuration,
  gatewayAccountLines,
  listAccounts,
  loginVerdict,
  pickerVisible,
  projectAccountStatus,
  projectVerdict,
  reapplyHelps,
  removeAccount,
  saveOutcome,
  setDefaultAccount,
  setProjectAccount,
  suggestedConfigDir,
  validateNewAccount,
  type ProjectAccountStatus,
} from './gateway_accounts';

const mocked = vi.mocked(invoke);

beforeEach(() => {
  mocked.mockReset();
  mocked.mockResolvedValue({ ok: true });
});

describe('wire shape', () => {
  it('names every command and argument the Rust side registers', async () => {
    await listAccounts();
    await addAccount('work', 'Work', '~/.claude-work');
    await removeAccount('work');
    await setDefaultAccount('work');
    await setProjectAccount('p1', 'work');
    await setProjectAccount('p1', null);
    await projectAccountStatus('p1');
    expect(mocked.mock.calls).toEqual([
      ['gateway_accounts_list'],
      ['gateway_accounts_add', { id: 'work', label: 'Work', configDir: '~/.claude-work' }],
      ['gateway_accounts_remove', { id: 'work' }],
      ['gateway_accounts_set_default', { id: 'work' }],
      ['project_gateway_account_set', { projectId: 'p1', account: 'work' }],
      ['project_gateway_account_set', { projectId: 'p1', account: null }],
      ['project_gateway_account_status', { projectId: 'p1' }],
    ]);
  });
});

describe('verdicts', () => {
  it('validates a new account like the Python side', () => {
    expect(validateNewAccount('work', '/home/u/.claude-work', [])).toBeNull();
    expect(validateNewAccount('work', '~/.claude-work', [])).toBeNull();
    expect(validateNewAccount('Work', '/x', [])).toMatch(/lower-case/);
    expect(validateNewAccount('primary', '/x', [])).toMatch(/already exists/);
    expect(validateNewAccount('work', '/x', ['work'])).toMatch(/already exists/);
    expect(validateNewAccount('work', 'relative', [])).toMatch(/absolute/);
    expect(suggestedConfigDir('team')).toBe('~/.claude-team');
    expect(suggestedConfigDir('')).toBe('');
  });

  it('reads login state by name and expiry, preferring the gateway view', () => {
    expect(loginVerdict({ login: null, gateway: null }).tone).toBe('bad');
    expect(
      loginVerdict({ login: { state: 'present', expires_at_ms: 1, expires_in_s: 30000, problem: null }, gateway: null }),
    ).toEqual({ text: 'logged in — expires in 8 h 20 min', tone: 'ok' });
    expect(
      loginVerdict({ login: { state: 'present', expires_at_ms: 1, expires_in_s: 600, problem: null }, gateway: null })
        .tone,
    ).toBe('warn');
    const gw = {
      id: 'work', usable: false, oauth_state: 'expired', oauth_expires_in_s: -5, fallbacks: 2,
      last_fallback_at: 1, last_fallback_reason: 'expired', refresh: null,
    };
    expect(
      loginVerdict({ login: { state: 'present', expires_at_ms: 1, expires_in_s: 99999, problem: null }, gateway: gw }),
    ).toEqual({ text: 'login expired', tone: 'bad' });
    expect(formatDuration(59)).toBe('1 min');
    expect(formatDuration(7200)).toBe('2 h');
  });

  it('says plainly when a project is billed to the fallback account', () => {
    const base: ProjectAccountStatus = {
      ok: true, project_id: 'p1', assigned: 'work', selected: 'work', source: 'project',
      effective: 'work', falling_back: false, file_says: 'work', drift: false, label: 'Work',
      drift_cause: null, selector_line: 'x-vco-account: work',
    };
    expect(projectVerdict(base).tone).toBe('ok');
    const fb = projectVerdict({ ...base, effective: 'primary', falling_back: true });
    expect(fb.tone).toBe('bad');
    expect(fb.text).toContain('billed to, "primary"');
    expect(
      projectVerdict({ ...base, drift: true, file_says: null, drift_cause: 'not_applied' }).text,
    ).toContain('Re-apply');
  });

  it('names a remedy that helps for each drift cause (A3)', () => {
    const base: ProjectAccountStatus = {
      ok: true, project_id: 'p1', assigned: 'work', selected: 'work', source: 'project',
      effective: 'work', falling_back: false, file_says: null, drift: true,
      drift_cause: 'user_value', selector_line: 'x-vco-account: work',
    };
    const own = projectVerdict(base).text;
    expect(own).toContain('your own ANTHROPIC_CUSTOM_HEADERS');
    expect(own).toContain('`x-vco-account: work`');
    expect(own).not.toContain('Re-apply');
    expect(reapplyHelps(base)).toBe(false);
    const tracked = { ...base, drift_cause: 'local_tracked' as const };
    expect(projectVerdict(tracked).text).toContain('git rm --cached .claude/settings.local.json');
    expect(reapplyHelps(tracked)).toBe(true);
    expect(projectVerdict({ ...base, drift_cause: 'local_unknown' }).text).toContain('Git could not say');
    expect(reapplyHelps({ ...base, drift: false, drift_cause: null })).toBe(false);
  });

  it('toasts success only when the saved choice is in force (A3/A10)', () => {
    const status: ProjectAccountStatus = {
      ok: true, project_id: 'p1', assigned: 'work', selected: 'work', source: 'project',
      effective: 'work', falling_back: false, file_says: 'work', drift: false,
      drift_cause: null, selector_line: 'x-vco-account: work',
    };
    expect(saveOutcome({ ok: true, status }).tone).toBe('ok');
    const notInForce = saveOutcome({
      ok: true,
      status: { ...status, drift: true, file_says: null, drift_cause: 'user_value' },
    });
    expect(notInForce.tone).toBe('warn');
    expect(notInForce.text).toContain('saved, but not in force yet');
    const projFail = saveOutcome({ ok: true, projection_error: 'SettingsWriteRefused: x' });
    expect(projFail.tone).toBe('warn');
    expect(projFail.text).toContain('Claude account saved');
    expect(projFail.text).not.toContain('not saved');
    expect(saveOutcome({ ok: false, error: 'nope' })).toEqual({ text: 'nope', tone: 'bad' });
  });

  it('lists one Services line per gateway account, only once there are two', () => {
    const primary = {
      id: 'primary', label: 'Personal', is_default: true, usable: true, oauth_state: 'present',
      oauth_expires_in_s: 30000, fallbacks: 0, last_fallback_at: null, last_fallback_reason: null,
      refresh: null,
    };
    expect(gatewayAccountLines([primary])).toEqual([]);
    expect(gatewayAccountLines(null)).toEqual([]);
    const work = { ...primary, id: 'work', label: 'Work seat', is_default: false, usable: false,
      oauth_state: 'expired', fallbacks: 3, last_fallback_reason: 'expired' };
    const lines = gatewayAccountLines([primary, work]);
    expect(lines.map((l) => l.id)).toEqual(['primary', 'work']);
    expect(lines[0].text).toBe('Personal (primary) · default: logged in — expires in 8 h 20 min');
    expect(lines[1].text).toBe('Work seat (work): login expired — fell back 3× (expired)');
    expect(lines[1].tone).toBe('warn');
  });

  it('shows the picker only once a second account exists', () => {
    expect(pickerVisible(null)).toBe(false);
    expect(pickerVisible({ accounts: [{ id: 'primary' }] as never })).toBe(false);
    expect(pickerVisible({ accounts: [{ id: 'primary' }, { id: 'work' }] as never })).toBe(true);
  });
});

type Node = Record<string, unknown>;
const here = dirname(fileURLToPath(import.meta.url));
const ast = (rel: string) =>
  parse(readFileSync(resolve(here, rel), 'utf8'), { modern: true }) as unknown as Node;

function calls(root: unknown, name: string): Node[] {
  return [...walk(root)].filter((n) => n.type === 'CallExpression' && isIdent(n.callee, name));
}

function onMountReaches(file: Node, fn: string): boolean {
  return calls((file.instance as Node).content, 'onMount').some((c) =>
    [...walk(c)].some((n) => isIdent(n, fn)),
  );
}

describe('wiring', () => {
  it('the Identity tab mounts the account picker', () => {
    const tab = ast('../project-state/IdentityTab.svelte');
    const mounted = [...walk(tab.fragment)].some(
      (n) => n.type === 'Component' && n.name === 'GatewayAccountPicker',
    );
    expect(mounted).toBe(true);
  });

  it('the picker loads on mount and saves through setProjectAccount', () => {
    const picker = ast('../components/GatewayAccountPicker.svelte');
    expect(onMountReaches(picker, 'loadAccountChoice')).toBe(true);
    expect(calls((picker.instance as Node).content, 'setProjectAccount').length).toBe(1);
    expect(calls((picker.instance as Node).content, 'projectAccountStatus').length).toBe(1);
  });

  it('the Preferences page lists on mount and wires add, remove and default', () => {
    const page = ast('../../routes/preferences/accounts/+page.svelte');
    expect(onMountReaches(page, 'loadAccounts')).toBe(true);
    const all = [...walk(page)];
    for (const fn of ['addAccount', 'removeAccount', 'setDefaultAccount', 'listAccounts']) {
      expect(all.some((n) => n.type === 'CallExpression' && isIdent(n.callee, fn)), fn).toBe(true);
    }
  });

  it('Preferences links to the accounts page', () => {
    const prefs = readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf8');
    const linked = calls(parse(prefs, { modern: true }), 'goto').some(
      (c) => ((c.arguments as Node[])[0] as Node)?.value === '/preferences/accounts',
    );
    expect(linked).toBe(true);
  });
});
