// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-12 — unit tests for the model gateway card's API + logic.
//
// Two things are pinned:
//   1. The WIRE SHAPE of every call — command name and argument names. A
//      renamed command or a camelCase/snake_case slip is a runtime "command
//      not found" no type checker can see.
//   2. The DECISIONS the card makes, above all the two that would be
//      silently wrong if they regressed: the tri-state never collapsing into
//      up/down, and `pointPanelAtGateway` never sending a model the user did
//      not pick.
//
// Pure unit tests: `$lib/tauri` is mocked, no Tauri runtime required.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
}));

import { invoke } from '$lib/tauri';
import {
  DEFAULT_GATEWAY_MODEL,
  ROUTING_GUIDANCE_MODULE,
  SLOT_OVERRIDE_KEYS,
  canStop,
  checkModelGateway,
  describeStatus,
  describeDogfood,
  describeOAuthExpiry,
  describeSupervision,
  describeWriteResult,
  gatewayIsConfigured,
  getModelGatewayStatus,
  inspectVSCodeTarget,
  listVSCodeTargets,
  pointPanelAtGateway,
  pointPanelPort,
  pointPanelWarnings,
  projectHasRoutingGuidance,
  resetPanelToNative,
  setModelGatewayBoot,
  setProjectRoutingGuidance,
  startModelGateway,
  stopDisabledReason,
  stopModelGateway,
} from './model_gateway';
import type {
  ModelGatewayStatus,
  VSCodeInspection,
  VSCodeWriteResult,
} from '$lib/types/model-gateway';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function makeStatus(over: Partial<ModelGatewayStatus> = {}): ModelGatewayStatus {
  return {
    process: 'running',
    pid: 4242,
    supervised: true,
    supervision: 'launcher',
    port: 11436,
    base_url: 'http://127.0.0.1:11436',
    reachable: true,
    health: {
      ok: true,
      service: 'vct-model-gateway',
      version: '0.2.92',
      port: 11436,
      host: '127.0.0.1',
      catalog_source: { claude: 'live', zai: 'live' },
      context_table_source: 'export',
      context_table_path: '/home/u/.vct/model-gateway/chat_model_context.json',
      oauth_present: true,
      oauth_state: 'present',
      oauth_expires_in_s: 8 * 3600,
      vendors: ['zai'],
      vendor_keys_cached: ['zai'],
      token_file_permissions: 'owner_only',
    },
    health_error: null,
    boot: 'disabled',
    token_present: true,
    python: '/opt/vco/.venv/bin/python',
    ...over,
  };
}

function makeInspection(over: Partial<VSCodeInspection> = {}): VSCodeInspection {
  return {
    path: '/home/u/.config/Code/User/settings.json',
    exists: true,
    parseable: true,
    refusal_reason: null,
    message: null,
    points_at_vco_gateway: false,
    base_url: null,
    model: null,
    discovery_enabled: null,
    disable_login_prompt: null,
    slot_overrides: [],
    managed_keys_present: [],
    permissions: 'owner_only',
    ...over,
  };
}

beforeEach(() => {
  mockInvoke.mockReset();
});

describe('wire shape', () => {
  it('status takes no arguments', async () => {
    mockInvoke.mockResolvedValue(makeStatus());
    await getModelGatewayStatus();
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_status');
  });

  it('start passes an explicit null when no port is chosen', async () => {
    mockInvoke.mockResolvedValue(makeStatus());
    await startModelGateway();
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_start', { port: null });
  });

  it('start forwards a chosen port', async () => {
    mockInvoke.mockResolvedValue(makeStatus());
    await startModelGateway(11999);
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_start', { port: 11999 });
  });

  it('stop and check take no arguments', async () => {
    mockInvoke.mockResolvedValue({ stopped: true, message: 'ok' });
    await stopModelGateway();
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_stop');

    mockInvoke.mockResolvedValue('report');
    await checkModelGateway();
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_check');
  });

  it('boot toggle sends a bool', async () => {
    mockInvoke.mockResolvedValue('enabled');
    await setModelGatewayBoot(true);
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_set_boot', {
      enabled: true,
    });
  });

  it('targets unwraps the envelope and tolerates an absent list', async () => {
    mockInvoke.mockResolvedValue({ targets: [{ app_id: 'code' }] });
    expect(await listVSCodeTargets()).toHaveLength(1);

    mockInvoke.mockResolvedValue({});
    expect(await listVSCodeTargets()).toEqual([]);
  });

  it('inspect and reset send the path', async () => {
    mockInvoke.mockResolvedValue(makeInspection());
    await inspectVSCodeTarget('/p/settings.json');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_vscode_inspect', {
      path: '/p/settings.json',
    });

    mockInvoke.mockResolvedValue({ ok: true });
    await resetPanelToNative('/p/settings.json');
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_reset_native', {
      path: '/p/settings.json',
    });
  });

  it('routing-guidance toggle reuses the generic module command', async () => {
    mockInvoke.mockResolvedValue(undefined);
    await setProjectRoutingGuidance('proj-1', true);
    expect(mockInvoke).toHaveBeenCalledWith('set_project_module_enabled', {
      projectId: 'proj-1',
      moduleName: 'model_gateway',
      enabled: true,
    });

    mockInvoke.mockResolvedValue(false);
    await projectHasRoutingGuidance('proj-1');
    expect(mockInvoke).toHaveBeenCalledWith('is_project_module_active', {
      projectId: 'proj-1',
      moduleName: ROUTING_GUIDANCE_MODULE,
    });
  });
});

describe('R15 — the model is the user’s choice', () => {
  it('sends model: null when the user picked nothing', async () => {
    mockInvoke.mockResolvedValue({ ok: true });
    await pointPanelAtGateway({ path: '/p/settings.json' });
    expect(mockInvoke).toHaveBeenCalledWith('model_gateway_point_panel', {
      path: '/p/settings.json',
      model: null,
      removeSlotOverrides: false,
      port: null,
    });
  });

  it('sends model: null for a whitespace-only choice', async () => {
    mockInvoke.mockResolvedValue({ ok: true });
    await pointPanelAtGateway({ path: '/p/settings.json', model: '   ' });
    expect(mockInvoke.mock.calls[0][1].model).toBeNull();
  });

  it('pointPanelPort hands over the live gateway port, and only a live one', () => {
    // The Services card's half of R1-1: without this the panel write
    // re-resolves (launcher env -> port file -> 11436) and can name a
    // different process than the card is describing.
    const base = makeStatus();
    const live = makeStatus({
      port: 11437,
      reachable: true,
      health: { ...base.health!, port: 11437 },
    });
    expect(pointPanelPort(live)).toBe(11437);
    // LEAVE-ALONE half: no proof, no port — Rust resolves as it always did.
    expect(pointPanelPort(makeStatus({ port: 11437, reachable: false }))).toBeNull();
    expect(pointPanelPort(makeStatus({ port: 11437, reachable: null }))).toBeNull();
    expect(
      pointPanelPort(
        makeStatus({
          port: 11437,
          reachable: true,
          health: { ...base.health!, service: 'vco-model-router' },
        }),
      ),
    ).toBeNull();
    expect(pointPanelPort(null)).toBeNull();
  });

  it('does not repeat the writer’s own kept-Default sentence (R3-4)', () => {
    // The Python writer already names a preserved vendor Default in
    // `message`; appending a second sentence printed it twice in the toast.
    const line = describeWriteResult({
      action: 'point_at_gateway',
      path: '/p/settings.json',
      ok: true,
      status: 'written',
      reason: null,
      message:
        'Panel pointed at the model gateway. Kept your Default claude-gw/glm-5.3[1m] — it is a vendor model.',
      backup_path: null,
      keys_written: ['ANTHROPIC_BASE_URL'],
      permissions: 'owner_only',
      paste_block: null,
      restart_required: true,
      vendor_default_preserved: 'claude-gw/glm-5.3[1m]',
    });
    expect(line.match(/Kept your Default/g)?.length).toBe(1);
    expect(line).toContain('claude-gw/glm-5.3[1m]');
  });

  it('the done message names the endpoint that was WRITTEN', () => {
    const line = describeWriteResult({
      action: 'point_at_gateway',
      path: '/p/settings.json',
      ok: true,
      status: 'written',
      reason: null,
      message: 'Panel pointed at the model gateway.',
      backup_path: null,
      keys_written: ['ANTHROPIC_BASE_URL'],
      permissions: 'owner_only',
      paste_block: null,
      restart_required: true,
      base_url: 'http://127.0.0.1:11437',
    });
    expect(line).toContain('Pointed at http://127.0.0.1:11437.');
  });

  it('sends port: null by default and a known port when the caller has one', async () => {
    // R1-1b: a caller that just started a gateway knows which port it bound;
    // one that does not lets the Rust side resolve as before.
    mockInvoke.mockResolvedValue({ ok: true });
    await pointPanelAtGateway({ path: '/p/settings.json' });
    expect(mockInvoke.mock.calls[0][1].port).toBeNull();
    await pointPanelAtGateway({ path: '/p/settings.json', port: 11437 });
    expect(mockInvoke.mock.calls[1][1].port).toBe(11437);
  });

  it('forwards an explicit choice, trimmed', async () => {
    mockInvoke.mockResolvedValue({ ok: true });
    await pointPanelAtGateway({
      path: '/p/settings.json',
      model: ' claude-gw/glm-5.3 ',
    });
    expect(mockInvoke.mock.calls[0][1].model).toBe('claude-gw/glm-5.3');
  });

  it('the offered default is first-party — never a vendor model (v0.2.94)', () => {
    // It was 'claude-gw/glm-5.3' until 2026-09-08. ANTHROPIC_MODEL is what a
    // RESTARTED panel resumes on, so this pre-selection is how a machine ran
    // a release cycle on GLM while the picker still said Fable.
    expect(DEFAULT_GATEWAY_MODEL).toBe('claude-opus-5');
    expect(DEFAULT_GATEWAY_MODEL).not.toContain('flash');
    expect(DEFAULT_GATEWAY_MODEL).not.toContain('claude-gw/');
  });

  it('names all six slot keys it must never write', () => {
    expect([...SLOT_OVERRIDE_KEYS].sort()).toEqual(
      [
        'ANTHROPIC_DEFAULT_FABLE_MODEL',
        'ANTHROPIC_DEFAULT_HAIKU_MODEL',
        'ANTHROPIC_DEFAULT_OPUS_MODEL',
        'ANTHROPIC_DEFAULT_SONNET_MODEL',
        'ANTHROPIC_SMALL_FAST_MODEL',
        'CLAUDE_CODE_SUBAGENT_MODEL',
      ].sort(),
    );
  });

  it('warns by name about slot overrides already in the file', () => {
    const warnings = pointPanelWarnings(
      makeInspection({ slot_overrides: ['CLAUDE_CODE_SUBAGENT_MODEL'] }),
    );
    expect(warnings.join(' ')).toContain('CLAUDE_CODE_SUBAGENT_MODEL');
    expect(warnings.join(' ')).toContain('carried forward');
  });

  it('says nothing about slots when there are none — the RC note is the only warning', () => {
    const warnings = pointPanelWarnings(makeInspection());
    expect(warnings).toHaveLength(1);
    expect(warnings.join(' ')).toContain('Remote Control');
    expect(warnings.join(' ')).not.toContain('slot');
  });
});

describe('Remote Control disclosure (endpoint gate)', () => {
  // Live-verified 2026-09-04 (Claude Code 2.1.258): Remote Control is
  // endpoint-gated — it initializes only in sessions talking directly to
  // api.anthropic.com. Pointing the panel at the gateway sets
  // ANTHROPIC_BASE_URL, so /remote-control there ALWAYS fails. The user
  // must hear this BEFORE pointing, not from a failing toast afterwards.
  it('always warns, even on a clean inspection', () => {
    const warnings = pointPanelWarnings(makeInspection());
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('Remote Control');
    expect(warnings[0]).toContain('api.anthropic.com');
    expect(warnings[0]).toContain('--remote-control');
  });

  it('warns on the JSONC paste path too', () => {
    // The paste block lands in the same env block — same limitation.
    const warnings = pointPanelWarnings(
      makeInspection({ parseable: false, refusal_reason: 'not_strict_json' }),
    );
    expect(warnings.join(' ')).toContain('Remote Control');
    expect(warnings.join(' ')).toContain('not strict JSON');
  });

  it('never promises compatibility or suggests a downgrade', () => {
    const text = pointPanelWarnings(makeInspection()).join(' ').toLowerCase();
    expect(text).not.toContain('downgrade');
    expect(text).not.toContain('will work');
    expect(text).not.toContain('compatible');
  });
});

describe('the tri-state survives the UI', () => {
  it('unreachable-unknown is its own label, not "stopped"', () => {
    const line = describeStatus(
      makeStatus({
        reachable: null,
        health: null,
        health_error: 'no answer within 1500 ms',
      }),
    );
    expect(line.tone).toBe('unknown');
    expect(line.label).toBe('unknown');
    expect(line.label).not.toContain('stopped');
    expect(line.detail).toContain('no answer within 1500 ms');
  });

  it('a live gateway reads as running', () => {
    const line = describeStatus(makeStatus());
    expect(line.tone).toBe('up');
    expect(line.detail).toContain('0.2.92');
  });

  it('counts families from catalog_source, not from vendors + 1', () => {
    const s = makeStatus();
    s.health!.catalog_source = { claude: 'live', zai: 'live', acme: 'static' };
    s.health!.vendors = ['zai'];
    expect(describeStatus(s).detail).toContain('3 model families');
  });

  it('pluralises honestly', () => {
    const s = makeStatus();
    s.health!.catalog_source = { claude: 'live' };
    expect(describeStatus(s).detail).toContain('1 model family');
  });

  it('a stale pid file is a warning, not a clean stop', () => {
    const line = describeStatus(
      makeStatus({
        process: 'stale_pid_file',
        reachable: false,
        health: null,
        pid: 999,
      }),
    );
    expect(line.tone).toBe('warn');
    expect(line.detail).toContain('999');
  });

  it('alive process + refused port is a warning, not "running"', () => {
    const line = describeStatus(
      makeStatus({
        process: 'running',
        reachable: false,
        health: null,
        health_error: 'connection refused',
      }),
    );
    expect(line.tone).toBe('warn');
    expect(line.label).not.toBe('running');
  });

  it('a genuine stop reads as stopped, and says whether it ever ran', () => {
    const never = describeStatus(
      makeStatus({
        process: 'not_running',
        reachable: false,
        health: null,
        pid: null,
        supervised: false,
        token_present: false,
      }),
    );
    expect(never.tone).toBe('down');
    expect(never.detail).toContain('never been started');
  });

  it('no status at all is unknown, not down', () => {
    expect(describeStatus(null).tone).toBe('unknown');
  });
});

describe('stop refuses what it cannot identify', () => {
  it('is enabled only for a supervised gateway', () => {
    expect(canStop(makeStatus({ supervised: true }))).toBe(true);
    expect(canStop(makeStatus({ supervised: false }))).toBe(false);
    expect(canStop(null)).toBe(false);
  });

  it('explains the refusal instead of leaving a dead button', () => {
    const why = stopDisabledReason(
      makeStatus({ supervised: false, process: 'running', pid: 777 }),
    );
    expect(why).toContain('777');
    expect(why).toContain('Start at login');
    expect(why).toContain('Windows');
  });

  it('has no reason to give when it is enabled', () => {
    expect(stopDisabledReason(makeStatus({ supervised: true }))).toBe('');
  });
});

describe('gatewayIsConfigured gates the panel + guidance affordances', () => {
  it('a running gateway counts', () => {
    expect(gatewayIsConfigured(makeStatus({ reachable: true }))).toBe(true);
  });

  it('a stopped gateway that has run before still counts', () => {
    expect(
      gatewayIsConfigured(
        makeStatus({ reachable: false, health: null, token_present: true }),
      ),
    ).toBe(true);
  });

  it('boot-registered counts even before the first run', () => {
    expect(
      gatewayIsConfigured(
        makeStatus({
          reachable: false,
          health: null,
          token_present: false,
          boot: 'enabled',
        }),
      ),
    ).toBe(true);
  });

  it('a machine that has never run it does NOT count', () => {
    expect(
      gatewayIsConfigured(
        makeStatus({
          reachable: false,
          health: null,
          token_present: false,
          boot: 'disabled',
        }),
      ),
    ).toBe(false);
  });

  it('no status does not count', () => {
    expect(gatewayIsConfigured(null)).toBe(false);
  });
});

describe('permission and parse warnings', () => {
  it('a JSONC file warns and suppresses the rest', () => {
    const warnings = pointPanelWarnings(
      makeInspection({
        parseable: false,
        refusal_reason: 'not_strict_json',
        slot_overrides: ['CLAUDE_CODE_SUBAGENT_MODEL'],
      }),
    );
    // The unconditional Remote Control note stays (the paste path points
    // at the gateway too); the file-specific warnings are suppressed to
    // just the JSONC one.
    expect(warnings).toHaveLength(2);
    expect(warnings[0]).toContain('Remote Control');
    expect(warnings[1]).toContain('not strict JSON');
    expect(warnings.join(' ')).not.toContain('CLAUDE_CODE_SUBAGENT_MODEL');
  });

  it('a world-readable settings file is called out', () => {
    expect(
      pointPanelWarnings(makeInspection({ permissions: 'broader' })).join(' '),
    ).toContain('readable by other accounts');
  });

  it('unknown permissions are not treated as fine', () => {
    expect(
      pointPanelWarnings(makeInspection({ permissions: 'unknown' })).join(' '),
    ).toContain('could not determine');
  });
});

describe('describeWriteResult', () => {
  const base: VSCodeWriteResult = {
    action: 'point_at_gateway',
    path: '/p/settings.json',
    ok: true,
    status: 'written',
    reason: null,
    message: 'Panel pointed at the model gateway.',
    backup_path: '/p/settings.json.bak-20260903-101010',
    keys_written: ['ANTHROPIC_BASE_URL'],
    keys_preserved: ['HTTPS_PROXY'],
    keys_removed: [],
    slot_overrides_preserved: [],
    permissions: 'owner_only',
    paste_block: null,
    restart_required: true,
  };

  it('reports what it wrote, kept and backed up', () => {
    const text = describeWriteResult(base);
    expect(text).toContain('wrote 1 key');
    expect(text).toContain('kept 1 existing key');
    expect(text).toContain('backup made');
  });

  it('a refusal returns the refusal message verbatim', () => {
    expect(
      describeWriteResult({ ...base, ok: false, message: 'nope' }),
    ).toBe('nope');
  });

  it('an unchanged run says so without inventing counts', () => {
    expect(
      describeWriteResult({ ...base, status: 'unchanged', message: 'already set' }),
    ).toBe('already set');
  });
});

// ─── v0.2.94: supervision and login expiry ────────────────────────────────
//
// Both exist because "running" was not the whole truth. A gateway nobody
// supervises dies silently, and a login the gateway cannot refresh expires
// silently; the card has to say so BEFORE the user's editor starts failing.

describe('describeSupervision', () => {
  it('names a hand-started gateway as unsupervised', () => {
    const line = describeSupervision(
      makeStatus({ supervised: false, supervision: 'unsupervised' }),
    );
    expect(line?.tone).toBe('warn');
    expect(line?.label).toBe('unsupervised (hand-started)');
    expect(line?.detail).toContain('Start at login');
  });

  it('does not upgrade "could not ask" to supervised', () => {
    const line = describeSupervision(makeStatus({ supervision: 'unknown' }));
    expect(line?.tone).toBe('unknown');
    expect(line?.label).toContain('unknown');
  });

  it('says who owns it when that is known', () => {
    expect(describeSupervision(makeStatus({ supervision: 'launcher' }))?.tone).toBe('up');
    expect(
      describeSupervision(makeStatus({ supervision: 'boot_service' }))?.label,
    ).toBe('supervised at login');
  });

  it('says nothing about a gateway that is not running', () => {
    expect(describeSupervision(makeStatus({ supervision: 'not_running' }))).toBeNull();
    expect(describeSupervision(null)).toBeNull();
  });
});

describe('describeOAuthExpiry', () => {
  function withExpiry(seconds: number | null) {
    const s = makeStatus();
    return { ...s, health: { ...s.health!, oauth_expires_in_s: seconds } };
  }

  it('stays quiet while there is plenty of time', () => {
    expect(describeOAuthExpiry(withExpiry(8 * 3600))).toBeNull();
  });

  it('warns inside the last half hour, in minutes', () => {
    const line = describeOAuthExpiry(withExpiry(25 * 60));
    expect(line?.tone).toBe('warn');
    expect(line?.label).toContain('25 min');
  });

  it('reports an expired login as down, with the remedy', () => {
    const line = describeOAuthExpiry(withExpiry(-60));
    expect(line?.tone).toBe('down');
    expect(line?.detail).toContain('claude');
  });

  it('says nothing when no expiry is stated', () => {
    expect(describeOAuthExpiry(withExpiry(null))).toBeNull();
  });
});

describe('describeDogfood', () => {
  const refused = {
    ok: false,
    status: 'refused' as const,
    reason: 'dogfood:ascii_6mib',
    message: 'the same 6291456-byte request answered HTTP 413 through the gateway',
    cases: [{ case: 'ascii_6mib', ok: false, detail: 'gateway 413/None vs native 200/1024' }],
    elapsed_s: 2.4,
  };

  it('shouts when the gateway answered differently from Anthropic', () => {
    const line = describeDogfood(makeStatus({ dogfood: refused }));
    expect(line?.tone).toBe('down');
    expect(line?.label).toContain('dogfood:ascii_6mib');
    expect(line?.detail).toContain('413');
  });

  it('stays silent when the proof merely could not run', () => {
    expect(
      describeDogfood(
        makeStatus({
          dogfood: { ...refused, status: 'skipped', reason: null, ok: false },
        }),
      ),
    ).toBeNull();
  });

  it('survives a refusal envelope that carries no cases', () => {
    // The CLI's own refusal (a missing host token) has no `cases`; this runs
    // inside a `$derived`, so a throw here takes the whole card down.
    const { cases: _dropped, ...caseless } = refused;
    const line = describeDogfood(makeStatus({ dogfood: caseless as never }));
    expect(line?.tone).toBe('down');
    expect(line?.detail).toContain('413');
  });

  it('stays silent on a pass, and on a status that never ran one', () => {
    expect(
      describeDogfood(makeStatus({ dogfood: { ...refused, status: 'ok', ok: true } })),
    ).toBeNull();
    expect(describeDogfood(makeStatus())).toBeNull();
  });
});
