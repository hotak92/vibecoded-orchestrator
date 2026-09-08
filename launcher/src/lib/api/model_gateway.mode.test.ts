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
  DEFAULT_GATEWAY_MODEL,
  FIRST_PARTY_ID_PREFIX,
  GATEWAY_SERVICE_NAME,
  gatewayIsLive,
  MODE_PILL_TOOLTIP,
  PANEL_MODES,
  RESTART_NOTICE,
  TOKEN_RACE_RETRY_DELAY_MS,
  clearPanelDefaultModel,
  defaultModelError,
  describeMode,
  describeModeReport,
  describeModeResult,
  describeSwitchOutcome,
  endpointWarning,
  gatewayStartNeeded,
  getPanelMode,
  isFirstPartyModelId,
  modeSwitchDisabledReason,
  multimodelPillLabel,
  setPanelMode,
  switchToMultimodel,
  vendorDefaultWarning,
} from './model_gateway';
import type {
  MultimodelSwitchDeps,
  PanelModeReport,
  PanelModeResult,
} from './model_gateway';
import type { ModelGatewayStatus, VSCodeTarget } from '$lib/types/model-gateway';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function liveHealth(port = 11436) {
  return {
    ok: true,
    service: GATEWAY_SERVICE_NAME,
    version: '0.2.94',
    port,
    host: '127.0.0.1',
    catalog_source: { claude: 'live' },
    context_table_source: 'launcher.db',
    context_table_path: null,
    oauth_present: false,
    oauth_state: 'absent',
    vendors: [],
    vendor_keys_cached: [],
    token_file_permissions: 'owner_only',
  };
}

function makeLiveStatus(port: number): ModelGatewayStatus {
  return makeStatus({
    port,
    process: 'running',
    reachable: true,
    health: liveHealth(port),
    health_error: null,
    token_present: true,
  });
}

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
    gateway: 'running',
    endpoint: null,
    prototype_endpoint: false,
    default_model_is_vendor: false,
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

  it('setPanelMode sends path + mode + port and nothing else', async () => {
    mockInvoke.mockResolvedValue(makeResult({ mode: 'multimodel' }));
    await setPanelMode('/p/settings.json', 'multimodel');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_mode_set', {
      path: '/p/settings.json',
      mode: 'multimodel',
      port: null,
    });
    const args = mockInvoke.mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(args).sort()).toEqual(['mode', 'path', 'port']);
    // The host token never comes from the GUI.
    expect(JSON.stringify(args).toLowerCase()).not.toContain('token');
  });

  it('setPanelMode forwards a known port so the base URL cannot be re-resolved', async () => {
    // Review R1-1b: without it the Rust side derives the base URL from
    // `resolve_port()`, which on the reporter's machine still answered 11436
    // — a legacy container — while the gateway had started on 11437.
    mockInvoke.mockResolvedValue(makeResult({ mode: 'multimodel' }));
    await setPanelMode('/p/settings.json', 'multimodel', 11437);
    expect(mockInvoke.mock.calls[0][1]).toMatchObject({ port: 11437 });
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

  it('no longer refuses the Multimodel pill on a gateway that has never run', () => {
    // v0.2.94: this used to read "Start the model gateway once (Services
    // page)" — a refusal on exactly the machines that needed the switch
    // most, with the remedy somewhere else. The click now starts it.
    const s = makeStatus({ token_present: false, boot: 'disabled', reachable: false });
    const r = makeReport({ gateway: 'stopped' });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], r)).toBe('');
    expect(modeSwitchDisabledReason('remote-control', s, [TARGET], r)).toBe('');
  });

  it('refuses only what cannot be fixed by clicking: no interpreter at all', () => {
    const s = makeStatus({ python: null });
    expect(modeSwitchDisabledReason('multimodel', s, [TARGET], makeReport())).toContain(
      'No Python interpreter',
    );
    expect(modeSwitchDisabledReason('remote-control', s, [TARGET], makeReport())).toBe('');
  });

  it('leaves the Multimodel pill clickable on a prototype endpoint — that IS the migration', () => {
    const r = makeReport({
      mode: 'unmanaged',
      base_url: 'http://127.0.0.1:8787',
      prototype_endpoint: true,
      gateway: 'stopped',
    });
    expect(modeSwitchDisabledReason('multimodel', makeStatus(), [TARGET], r)).toBe('');
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

  it('a status that has not loaded yet blocks neither pill', () => {
    // The switch does not need the status card's answer: the report carries
    // the gateway state, and the click starts one if it has to.
    expect(modeSwitchDisabledReason('multimodel', null, [TARGET], makeReport())).toBe('');
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

// ─── v0.2.94: a vendor model is never the Default ─────────────────────────

describe('the first-party rule', () => {
  it('accepts Claude ids, with or without the [1m] hint', () => {
    for (const id of ['claude-opus-5', 'claude-opus-5[1m]', 'Claude-Fable-5-1']) {
      expect(isFirstPartyModelId(id)).toBe(true);
    }
  });

  it('is a prefix test, case-folded before the namespace check (R1-5)', () => {
    // A substring test accepted both of these: one is a vendor id that
    // contains the word, the other is the gateway namespace in another case.
    expect(isFirstPartyModelId('glm-5.3-claude')).toBe(false);
    expect(isFirstPartyModelId('Claude-GW/glm-5.3')).toBe(false);
    expect(isFirstPartyModelId('CLAUDE-GW/glm-5.3[1m]')).toBe(false);
    expect(isFirstPartyModelId('my-claude-proxy')).toBe(false);
    expect(FIRST_PARTY_ID_PREFIX).toBe('claude-');
  });

  it('rejects vendor ids AND gateway-namespaced Claude ids', () => {
    // The namespace means only the gateway resolves it, so a panel that came
    // back stock would fall back to a name nothing answers.
    for (const id of [
      'claude-gw/glm-5.3',
      'claude-gw/glm-5.3[1m]',
      'claude-gw/claude-opus-5',
      'glm-5.3',
      'gpt-x',
      '',
      '   ',
      null,
      undefined,
    ]) {
      expect(isFirstPartyModelId(id as string)).toBe(false);
    }
  });

  it('the pre-selected Default is first-party', () => {
    // It was 'claude-gw/glm-5.3' until 2026-09-08, and that pre-selection is
    // how a vendor id reached a real settings.json.
    expect(isFirstPartyModelId(DEFAULT_GATEWAY_MODEL)).toBe(true);
    expect(DEFAULT_GATEWAY_MODEL.toLowerCase()).not.toContain('glm');
  });

  it('defaultModelError names the id and the rule, and is empty when fine', () => {
    expect(defaultModelError('claude-opus-5')).toBe('');
    const err = defaultModelError('claude-gw/glm-5.3');
    expect(err).toContain('claude-gw/glm-5.3');
    expect(err).toContain('restarted');
    expect(defaultModelError('  ')).toContain('untick');
  });

  it('warns about a vendor Default already in the file, naming it', () => {
    expect(vendorDefaultWarning(makeReport())).toBe('');
    const w = vendorDefaultWarning(
      makeReport({ default_model_is_vendor: true, model: 'claude-gw/glm-5.3[1m]' }),
    );
    expect(w).toContain('claude-gw/glm-5.3[1m]');
    expect(w).toContain('restart');
  });

  it('a declined Default is never the one thing the notice leaves out', () => {
    const line = describeModeResult(
      makeResult({ mode: 'multimodel', refusal_reason: "'claude-gw/glm-5.3' was NOT written" }),
    );
    expect(line).toContain(RESTART_NOTICE);
    expect(line).toContain('claude-gw/glm-5.3');
  });

  it('names a vendor Default the writer KEPT, not only one it declined', () => {
    // Review R1-3: the migration click preserves the user's Default on
    // purpose; a notice that says only "Applied" hands back a panel that
    // still resumes on it.
    const line = describeModeResult(
      makeResult({ mode: 'multimodel', vendor_default_preserved: 'claude-gw/glm-5.3[1m]' }),
    );
    expect(line).toContain('claude-gw/glm-5.3[1m]');
    expect(line).toContain('Clear default');
  });

  it('clearPanelDefaultModel sends the path and nothing else', async () => {
    mockInvoke.mockResolvedValue({ ok: true, status: 'written' });
    await clearPanelDefaultModel('/p/settings.json');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_clear_default_model', {
      path: '/p/settings.json',
    });
  });
});

// ─── v0.2.94: the switch starts the gateway instead of refusing ───────────

describe('gateway state in the frame', () => {
  it('names a stopped gateway in the pill label', () => {
    expect(multimodelPillLabel(makeReport({ gateway: 'stopped' }))).toBe(
      'Multimodel (gateway stopped)',
    );
    expect(multimodelPillLabel(makeReport({ gateway: 'running' }))).toBe('Multimodel');
    expect(multimodelPillLabel(null)).toBe('Multimodel');
  });

  it('needs a start for anything but a confirmed running gateway', () => {
    expect(gatewayStartNeeded(makeReport({ gateway: 'running' }))).toBe(false);
    expect(gatewayStartNeeded(makeReport({ gateway: 'stopped' }))).toBe(true);
    expect(gatewayStartNeeded(makeReport({ gateway: 'unreachable' }))).toBe(true);
    expect(gatewayStartNeeded(null)).toBe(true);
  });

  it('reads the GATEWAY, never the panel endpoint, for label and Start (R2-3)', () => {
    // The defect: a dead prototype endpoint on 8787 with the VCO gateway
    // running produced "Multimodel (gateway stopped)" and a Start button that
    // errored "already running".
    const protoDown = makeReport({
      mode: 'unmanaged',
      base_url: 'http://127.0.0.1:8787',
      prototype_endpoint: true,
      gateway: 'running',
      endpoint: 'stopped',
    });
    expect(multimodelPillLabel(protoDown)).toBe('Multimodel');
    expect(gatewayStartNeeded(protoDown)).toBe(false);
    expect(endpointWarning(protoDown)).toContain('port 8787');
    expect(endpointWarning(protoDown)).toContain('Multimodel points it');
  });

  it('offers the Start action when the GATEWAY is the thing that is down', () => {
    const gwDown = makeReport({ gateway: 'stopped', endpoint: 'running' });
    expect(multimodelPillLabel(gwDown)).toBe('Multimodel (gateway stopped)');
    expect(gatewayStartNeeded(gwDown)).toBe(true);
    expect(endpointWarning(gwDown)).toBe('');
  });

  it('says nothing in multimodel mode, where the advice would be inert (R3-5)', () => {
    // The panel already points at the VCO gateway, so "Multimodel points it
    // at the VCO gateway" aims the user at a pill that early-returns. The
    // stopped-gateway label and the Start button beside it are the remedy.
    const onGateway = makeReport({
      mode: 'multimodel',
      base_url: 'http://127.0.0.1:11437',
      gateway: 'stopped',
      endpoint: 'stopped',
    });
    expect(endpointWarning(onGateway)).toBe('');
    expect(multimodelPillLabel(onGateway)).toBe('Multimodel (gateway stopped)');
    expect(gatewayStartNeeded(onGateway)).toBe(true);
  });

  it('says nothing about an endpoint it cannot probe', () => {
    // No local endpoint, or a remote one: `null`, and inventing an alarm
    // about it would be the same class of guess as the old collapsed field.
    expect(endpointWarning(makeReport({ endpoint: null }))).toBe('');
    expect(endpointWarning(null)).toBe('');
    expect(
      endpointWarning(makeReport({ endpoint: 'unreachable', base_url: 'http://127.0.0.1:11436' })),
    ).toContain('did not answer as a VCO gateway');
  });

  it('describes a prototype endpoint as the migration it is', () => {
    const d = describeModeReport(
      makeReport({ mode: 'unmanaged', base_url: 'http://127.0.0.1:8787', prototype_endpoint: true }),
    );
    expect(d.active).toBeNull();
    expect(d.tooltip).toBe(
      'Panel points at a custom local endpoint on port 8787 (a prototype gateway?). Multimodel moves it to the VCO gateway.',
    );
  });

  it('leaves a remote custom endpoint with the leave-it-alone copy', () => {
    const d = describeModeReport(
      makeReport({ mode: 'unmanaged', base_url: 'https://api.z.ai', prototype_endpoint: false }),
    );
    expect(d.tooltip).toBe(describeMode('unmanaged').tooltip);
  });
});

describe('switchToMultimodel', () => {
  const OK = makeResult({ mode: 'multimodel' });
  const NO_TOKEN = makeResult({
    mode: 'multimodel',
    ok: false,
    status: 'refused',
    reason: 'no_host_token',
    message: "the gateway's host token file could not be read",
  });

  function deps(over: Record<string, unknown> = {}) {
    return {
      startGateway: vi.fn().mockResolvedValue(makeLiveStatus(11437)),
      setMode: vi.fn().mockResolvedValue(OK),
      sleep: vi.fn().mockResolvedValue(undefined),
      ...over,
    };
  }

  it('starts the gateway before pointing when it is not running', async () => {
    const d = deps();
    const out = await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'stopped' }), d);
    expect(d.startGateway).toHaveBeenCalledTimes(1);
    expect(d.setMode).toHaveBeenCalledWith('/p/settings.json', 'multimodel', 11437);
    expect(out.started).toBe(true);
    expect(out.startedPort).toBe(11437);
    expect(out.startedLive).toBe(true);
    expect(out.result).toBe(OK);
    // R1-1b: the port the start CHOSE is what the panel write uses.
    expect(d.setMode).toHaveBeenCalledWith('/p/settings.json', 'multimodel', 11437);
  });

  it('the deps contract types the port the switch actually passes (R2-1)', async () => {
    // A `(path, mode)` type here made both call sites a TS2554 waiting for a
    // stricter checker, and would have made any external caller's fake wrong.
    const seen: Array<[string, string, number | null | undefined]> = [];
    const setMode: MultimodelSwitchDeps['setMode'] = async (path, mode, port) => {
      seen.push([path, mode, port]);
      return OK;
    };
    await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'stopped' }), {
      ...deps(),
      setMode,
    });
    expect(seen).toEqual([['/p/settings.json', 'multimodel', 11437]]);
  });

  it('hands the chosen port to the retry as well', async () => {
    const setMode = vi.fn().mockResolvedValueOnce(NO_TOKEN).mockResolvedValueOnce(OK);
    const d = deps({ setMode });
    await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'stopped' }), d);
    for (const call of setMode.mock.calls) {
      expect(call[2]).toBe(11437);
    }
  });

  it('does not claim a start the gateway never confirmed (R1-7)', async () => {
    // `status.port` is the port we ASKED for. Only a /health answer carrying
    // our service name proves anything is listening on it.
    const d = deps({
      startGateway: vi
        .fn()
        .mockResolvedValue(makeStatus({ port: 11437, reachable: false, health: null })),
    });
    const out = await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'stopped' }), d);
    expect(out.startedLive).toBe(false);
    const line = describeSwitchOutcome(out);
    expect(line.text).toContain('not answering yet');
    expect(line.text).not.toContain('Started the model gateway on port');
    expect(line.tone).toBe('err');
  });

  it('gatewayIsLive demands the service name, not just reachability', () => {
    expect(gatewayIsLive(makeLiveStatus(11437))).toBe(true);
    expect(gatewayIsLive(makeStatus({ reachable: true, health: null }))).toBe(false);
    expect(
      gatewayIsLive(
        makeStatus({ reachable: true, health: { ...liveHealth(), service: 'vco-model-router' } }),
      ),
    ).toBe(false);
    expect(gatewayIsLive(null)).toBe(false);
  });

  it('does NOT start one that is already running', async () => {
    const d = deps();
    const out = await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'running' }), d);
    expect(d.startGateway).not.toHaveBeenCalled();
    expect(out.started).toBe(false);
  });

  it('retries the point ONCE when a freshly started gateway has not written its token yet', async () => {
    const setMode = vi.fn().mockResolvedValueOnce(NO_TOKEN).mockResolvedValueOnce(OK);
    const d = deps({ setMode });
    const out = await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'stopped' }), d);
    expect(setMode).toHaveBeenCalledTimes(2);
    expect(d.sleep).toHaveBeenCalledWith(TOKEN_RACE_RETRY_DELAY_MS);
    expect(out.retried).toBe(true);
    expect(out.result).toBe(OK);
  });

  it('shows the real reason after the one retry rather than looping', async () => {
    const setMode = vi.fn().mockResolvedValue(NO_TOKEN);
    const out = await switchToMultimodel(
      '/p/settings.json',
      makeReport({ gateway: 'stopped' }),
      deps({ setMode }),
    );
    expect(setMode).toHaveBeenCalledTimes(2);
    expect(out.result?.ok).toBe(false);
    expect(describeSwitchOutcome(out).tone).toBe('err');
    expect(describeSwitchOutcome(out).text).toContain('host token');
  });

  it('does not retry a refusal that has nothing to do with the token', async () => {
    const jsonc = makeResult({
      mode: 'multimodel',
      ok: false,
      status: 'refused',
      reason: 'not_strict_json',
      message: 'JSONC.',
    });
    const setMode = vi.fn().mockResolvedValue(jsonc);
    const out = await switchToMultimodel(
      '/p/settings.json',
      makeReport({ gateway: 'stopped' }),
      deps({ setMode }),
    );
    expect(setMode).toHaveBeenCalledTimes(1);
    expect(out.retried).toBe(false);
  });

  it('still points when the start fails, and carries the start error', async () => {
    // A gateway started outside this launcher refuses a second start; the
    // point may well succeed against it, so a failed start is not fatal.
    const d = deps({
      startGateway: vi.fn().mockRejectedValue('a model gateway is already running (pid 42)'),
    });
    const out = await switchToMultimodel('/p/settings.json', makeReport({ gateway: 'unreachable' }), d);
    expect(d.setMode).toHaveBeenCalledTimes(1);
    expect(out.started).toBe(false);
    expect(out.startError).toContain('already running');
    expect(out.result).toBe(OK);
  });

  it('reports the port a started gateway actually bound', async () => {
    const out = await switchToMultimodel(
      '/p/settings.json',
      makeReport({ gateway: 'stopped' }),
      deps(),
    );
    const line = describeSwitchOutcome(out);
    expect(line.tone).toBe('ok');
    expect(line.text).toContain('Started the model gateway on port 11437.');
    expect(line.text).toContain(RESTART_NOTICE);
  });
});
