// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Multimodel <-> Remote Control switch — API wrapper + copy.
//
// Pinned here:
//   1. The WIRE SHAPE of `getPanelMode` / `setPanelMode` — command names and
//      argument names. A camelCase/snake_case slip is a runtime "command not
//      found" no type checker sees.
//   2. `describeMode` lights exactly one pill for the two settable states
//      and none for the two reported-only states, with the one-sentence
//      trade-off the spec asks for.
//   3. `modeSwitchDisabledReason` refuses the Multimodel leg when the gateway
//      is not configured, refuses both when there is no settings file, and
//      is EMPTY (clickable) in the normal case — the leave-alone half.
//
// Pure unit tests: `$lib/tauri` is mocked, no Tauri runtime required.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
}));

import { invoke } from '$lib/tauri';
import {
  MODE_PILL_TOOLTIP,
  PANEL_MODES,
  RESTART_NOTICE,
  describeMode,
  describeModeResult,
  getPanelMode,
  modeSwitchDisabledReason,
  setPanelMode,
} from './model_gateway';
import type { PanelModeReport, PanelModeResult } from './model_gateway';
import type { ModelGatewayStatus, VSCodeTarget } from '$lib/types/model-gateway';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function makeStatus(over: Partial<ModelGatewayStatus> = {}): ModelGatewayStatus {
  return {
    process: 'not_running',
    pid: null,
    supervised: false,
    port: 11436,
    base_url: 'http://127.0.0.1:11436',
    reachable: false,
    health: null,
    health_error: 'connection refused',
    boot: 'disabled',
    token_present: false,
    python: '/usr/bin/python3',
    ...over,
  };
}

function makeReport(over: Partial<PanelModeReport> = {}): PanelModeReport {
  return {
    mode: 'remote-control',
    path: '/home/u/.config/Code/User/settings.json',
    detail: 'Stock Claude Code; Remote Control works.',
    base_url: null,
    model: null,
    slot_overrides: [],
    stash_present: false,
    stash_path: '/home/u/.vct/model-gateway/vscode-mode-stash.json',
    ...over,
  };
}

function makeResult(over: Partial<PanelModeResult> = {}): PanelModeResult {
  return {
    action: 'set_mode',
    mode: 'remote-control',
    path: '/p/settings.json',
    ok: true,
    status: 'written',
    reason: null,
    message: 'Panel set to stock Claude Code.',
    backup_path: '/p/settings.json.bak-1',
    permissions: 'owner_only',
    paste_block: null,
    restart_required: true,
    ...over,
  };
}

const TARGET: VSCodeTarget = {
  app_id: 'code',
  display_name: 'VS Code',
  path: '/home/u/.config/Code/User/settings.json',
  flavour: 'native',
};

beforeEach(() => {
  mockInvoke.mockReset();
});

describe('wire shape', () => {
  it('getPanelMode passes the path under its snake_case-free name', async () => {
    mockInvoke.mockResolvedValue(makeReport());
    const r = await getPanelMode('/p/settings.json');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_mode_get', {
      path: '/p/settings.json',
    });
    expect(r.mode).toBe('remote-control');
  });

  it('setPanelMode sends path + mode and nothing else', async () => {
    mockInvoke.mockResolvedValue(makeResult({ mode: 'multimodel' }));
    await setPanelMode('/p/settings.json', 'multimodel');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_mode_set', {
      path: '/p/settings.json',
      mode: 'multimodel',
    });
    const args = mockInvoke.mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(args).sort()).toEqual(['mode', 'path']);
    // The host token never comes from the GUI.
    expect(JSON.stringify(args).toLowerCase()).not.toContain('token');
  });

  it('the settable modes are exactly the two words the writer accepts', () => {
    expect([...PANEL_MODES]).toEqual(['multimodel', 'remote-control']);
  });
});

describe('describeMode', () => {
  it('lights the Multimodel pill with its trade-off', () => {
    const d = describeMode('multimodel');
    expect(d.active).toBe('multimodel');
    expect(d.label).toBe('Multimodel');
    expect(d.tooltip).toBe('GLM + Claude in one picker; Remote Control unavailable.');
  });

  it('lights the Remote Control pill with its trade-off', () => {
    const d = describeMode('remote-control');
    expect(d.active).toBe('remote-control');
    expect(d.label).toBe('Remote Control');
    expect(d.tooltip).toBe(
      'Stock Claude Code; phone Remote Control works; GLM models unavailable in the panel.',
    );
  });

  it('lights NO pill for a custom endpoint and says VCO leaves it alone', () => {
    const d = describeMode('unmanaged');
    expect(d.active).toBeNull();
    expect(d.tooltip).toBe('Panel points at a custom endpoint; VCO leaves it alone.');
  });

  it('lights NO pill for an unparseable file and guesses nothing', () => {
    const d = describeMode('unparseable');
    expect(d.active).toBeNull();
    expect(d.tooltip).toContain('not strict JSON');
  });

  it('is neutral before the first load', () => {
    expect(describeMode(null).active).toBeNull();
  });

  it('pill tooltips are the same copy as the state descriptions', () => {
    expect(MODE_PILL_TOOLTIP.multimodel).toBe(describeMode('multimodel').tooltip);
    expect(MODE_PILL_TOOLTIP['remote-control']).toBe(describeMode('remote-control').tooltip);
  });
});

describe('modeSwitchDisabledReason', () => {
  it('is empty for both pills when the gateway is configured and a file exists', () => {
    const s = makeStatus({ token_present: true });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], makeReport())).toBe('');
    expect(modeSwitchDisabledReason('remote-control', s, [TARGET], makeReport())).toBe('');
  });

  it('disables ONLY the Multimodel pill when the gateway is not configured', () => {
    const s = makeStatus({ token_present: false, boot: 'disabled', reachable: false });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], makeReport())).toContain(
      'Start the model gateway',
    );
    expect(modeSwitchDisabledReason('remote-control', s, [TARGET], makeReport())).toBe('');
  });

  it('treats a running gateway as configured even before a token is seen', () => {
    const s = makeStatus({ reachable: true, token_present: false });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], makeReport())).toBe('');
  });

  it('disables both pills when no settings file was found', () => {
    const s = makeStatus({ token_present: true });
    expect(modeSwitchDisabledReason('multimodel', s, [], null)).toContain('settings.json');
    expect(modeSwitchDisabledReason('remote-control', s, [], null)).toContain('settings.json');
  });

  it('disables both pills for an unparseable file rather than letting the writer refuse', () => {
    const s = makeStatus({ token_present: true });
    const r = makeReport({ mode: 'unparseable' });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], r)).toContain('not strict JSON');
    expect(modeSwitchDisabledReason('remote-control', s, [TARGET], r)).toContain(
      'not strict JSON',
    );
  });

  it('a null status (not loaded) disables Multimodel only', () => {
    expect(modeSwitchDisabledReason('multimodel', null, [TARGET], makeReport())).not.toBe('');
    expect(modeSwitchDisabledReason('remote-control', null, [TARGET], makeReport())).toBe('');
  });
});

describe('describeModeResult', () => {
  it('a write shows the restart notice, never an automated restart', () => {
    expect(describeModeResult(makeResult())).toBe(RESTART_NOTICE);
    expect(RESTART_NOTICE.toLowerCase()).toContain('restart vs code');
  });

  it('names what was stashed / restored / healed alongside the notice', () => {
    const r = makeResult({
      values_stashed: ['ANTHROPIC_MODEL'],
      values_healed: ['CLAUDE_CODE_SUBAGENT_MODEL'],
    });
    const line = describeModeResult(r);
    expect(line.startsWith(RESTART_NOTICE)).toBe(true);
    expect(line).toContain('stashed ANTHROPIC_MODEL');
    expect(line).toContain('[1m] added to CLAUDE_CODE_SUBAGENT_MODEL');

    const back = describeModeResult(
      makeResult({ mode: 'multimodel', keys_restored: ['ANTHROPIC_DEFAULT_HAIKU_MODEL'] }),
    );
    expect(back).toContain('restored ANTHROPIC_DEFAULT_HAIKU_MODEL');
  });

  it('a no-op or a refusal shows the writer’s own message, not the restart notice', () => {
    expect(
      describeModeResult(makeResult({ status: 'unchanged', message: 'Already there.' })),
    ).toBe('Already there.');
    expect(
      describeModeResult(
        makeResult({ ok: false, status: 'refused', reason: 'not_strict_json', message: 'JSONC.' }),
      ),
    ).toBe('JSONC.');
  });
});
