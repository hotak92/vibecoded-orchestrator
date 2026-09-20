// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.95 (R5c) — the gateway card's THIRD state, the hub supervisor's
// verdict, and the two `/health` blocks that explain a gateway which is up
// and still cannot serve.
//
// A separate file from `model_gateway.test.ts` for the reason that file's
// siblings already exist (`model_gateway.mode.test.ts`): one subject per
// file, and a fixture shaped for that subject. Everything under test is
// pure — vitest here runs in node with no component renderer, so a decision
// left inside the `.svelte` file is a decision no test can reach.

import { describe, expect, it } from 'vitest';

import {
  describeHubCondition,
  describeRegistration,
  describeSecretScope,
  describeUsageLedger,
} from './model_gateway';
import type {
  GatewayRegistration,
  HubGatewayCondition,
  ModelGatewayStatus,
} from '$lib/types/model-gateway';

/** A status carrying only what these functions read. */
function statusWith(over: Partial<ModelGatewayStatus>): ModelGatewayStatus {
  return {
    process: 'not_running',
    pid: null,
    supervised: false,
    supervision: 'not_running',
    port: 11436,
    base_url: 'http://127.0.0.1:11436',
    reachable: false,
    health: null,
    health_error: null,
    boot: 'enabled',
    token_present: true,
    python: '/opt/vco/.venv/bin/python',
    ...over,
  };
}

function registration(over: Partial<GatewayRegistration> = {}): GatewayRegistration {
  return {
    state: 'registered_not_running',
    reason: 'registered and runnable, but nothing is serving',
    runnable: true,
    unit_path: '/home/u/.config/systemd/user/vct-model-gateway.service',
    ...over,
  };
}

describe('describeRegistration — three states, never two', () => {
  it('says "cannot run" for a registration that is present and broken', () => {
    const line = describeRegistration(
      statusWith({
        registration: registration({
          state: 'registered_but_unrunnable',
          runnable: false,
          reason:
            'the registered entry point cannot run: `/usr/bin/python3 -m model_router --version` failed.',
        }),
      }),
    );
    expect(line).not.toBeNull();
    expect(line!.tone).toBe('down');
    // The whole point: it is NOT reported as "off".
    expect(line!.label).toContain('cannot run');
    expect(line!.label).toContain('registered');
    expect(line!.detail).toContain('entry point cannot run');
    // …and it names WHERE, because repairing it means knowing which file.
    expect(line!.detail).toContain('vct-model-gateway.service');
  });

  it('keeps "not registered" separate from "unrunnable"', () => {
    const line = describeRegistration(
      statusWith({ registration: registration({ state: 'not_registered', runnable: null }) }),
    );
    expect(line).not.toBeNull();
    expect(line!.tone).toBe('unknown');
    expect(line!.label).toBe('not registered at login');
    // An offer, not a fault — and it states that VCO will not do it for you.
    expect(line!.detail).toContain('never registers it for you');
    expect(line!.detail).not.toContain('cannot run');
  });

  it('is silent when the gateway is running', () => {
    expect(describeRegistration(statusWith({ registration: registration({ state: 'running' }) })))
      .toBeNull();
  });

  it('treats an ABSENT registration as "not asked", never as "not registered"', () => {
    // This is the payload of a serving gateway: the launcher deliberately
    // does not spend a subprocess asking what the answer obviously is.
    const line = describeRegistration(statusWith({ reachable: true, registration: null }));
    expect(line).toBeNull();
    expect(describeRegistration(statusWith({}))).toBeNull();
    expect(describeRegistration(null)).toBeNull();
  });

  it('reports a runnable-but-idle registration as sound', () => {
    const line = describeRegistration(statusWith({ registration: registration() }));
    expect(line!.tone).toBe('warn');
    expect(line!.detail).toContain('verified');
  });

  it('says nothing it cannot support about a state it does not know', () => {
    const line = describeRegistration(
      statusWith({
        registration: registration({
          state: 'some_future_state' as GatewayRegistration['state'],
        }),
      }),
    );
    expect(line).toBeNull();
  });
});

describe('describeHubCondition — the detached supervisor speaks', () => {
  function condition(over: Partial<HubGatewayCondition> = {}): HubGatewayCondition {
    return {
      state: 'registered_but_unrunnable',
      reason: 'the registered entry point cannot run.',
      attempts: 3,
      port: 11460,
      observed_at_ms: Date.UTC(2026, 8, 16, 12, 0, 0),
      ...over,
    };
  }

  it('names the attempt count, the port and the reason', () => {
    const line = describeHubCondition(statusWith({ hub_condition: condition() }));
    expect(line).not.toBeNull();
    expect(line!.tone).toBe('down');
    expect(line!.label).toContain('3 attempts');
    expect(line!.detail).toContain('entry point cannot run');
    expect(line!.detail).toContain('11460');
    // It must also say the give-up is not permanent.
    expect(line!.detail).toContain('supervise it again');
  });

  it('pluralises one attempt honestly', () => {
    const line = describeHubCondition(statusWith({ hub_condition: condition({ attempts: 1 }) }));
    expect(line!.label).toContain('1 attempt)');
  });

  it('says nothing when nothing is recorded — the normal state', () => {
    expect(describeHubCondition(statusWith({}))).toBeNull();
    expect(describeHubCondition(statusWith({ hub_condition: null }))).toBeNull();
    expect(describeHubCondition(null)).toBeNull();
  });
});

describe('describeSecretScope — "no key configured" vs "no key reachable"', () => {
  function withScope(scope: NonNullable<ModelGatewayStatus['health']>['secret_scope']) {
    return statusWith({
      reachable: true,
      health: {
        ok: true,
        service: 'vct-model-gateway',
        version: '0.2.95',
        port: 11436,
        host: '127.0.0.1',
        catalog_source: { claude: 'live' },
        context_table_source: 'export',
        context_table_path: null,
        oauth_present: true,
        oauth_state: 'present',
        oauth_expires_in_s: 3600,
        vendors: ['acme'],
        vendor_keys_cached: [],
        secret_scope: scope,
        token_file_permissions: 'owner_only',
      },
    });
  }

  it('is loud when the scope resolves to nothing', () => {
    const line = describeSecretScope(
      withScope({
        project: '/home/u/.vct',
        resolvable: false,
        reason: 'the scope is not a registered project.',
      }),
    );
    expect(line!.tone).toBe('down');
    expect(line!.detail).toContain('/home/u/.vct');
    // The sentence that stops the next person debugging the wrong thing.
    expect(line!.detail).toContain('however correct they are');
  });

  it('reports "not probed" as unknown, not as failure', () => {
    const line = describeSecretScope(
      withScope({ project: '/home/u/projects/x', resolvable: null, reason: 'not probed' }),
    );
    expect(line!.tone).toBe('unknown');
    expect(line!.label).toContain('not probed');
  });

  it('adds nothing when the scope resolves', () => {
    const line = describeSecretScope(
      withScope({ project: '/home/u/projects/x', resolvable: true, reason: 'resolves' }),
    );
    expect(line).toBeNull();
  });

  it('adds nothing on a gateway too old to report the block', () => {
    expect(describeSecretScope(withScope(null))).toBeNull();
    expect(describeSecretScope(statusWith({}))).toBeNull();
  });
});

describe('describeUsageLedger', () => {
  function withLedger(ledger: NonNullable<ModelGatewayStatus['health']>['usage_ledger']) {
    return statusWith({
      health: {
        ok: true,
        service: 'vct-model-gateway',
        version: '0.2.95',
        port: 11436,
        host: '127.0.0.1',
        catalog_source: {},
        context_table_source: 'seed',
        context_table_path: null,
        oauth_present: false,
        oauth_state: 'absent',
        oauth_expires_in_s: null,
        vendors: [],
        vendor_keys_cached: [],
        usage_ledger: ledger,
        token_file_permissions: 'owner_only',
      },
    });
  }

  it('names the file and the row count', () => {
    expect(
      describeUsageLedger(
        withLedger({ path: '/home/u/.claude/metrics/gateway-usage.jsonl', rows_written: 12 }),
      ),
    ).toBe('12 rows written to /home/u/.claude/metrics/gateway-usage.jsonl');
  });

  it('singularises one row', () => {
    expect(describeUsageLedger(withLedger({ path: '/x.jsonl', rows_written: 1 }))).toContain(
      '1 row written',
    );
  });

  it('says so when no metrics home resolved — "0 rows" would read as idle', () => {
    expect(describeUsageLedger(withLedger({ path: null, rows_written: 0 }))).toContain(
      'not being written',
    );
  });

  it('is empty when the gateway does not report the block', () => {
    expect(describeUsageLedger(withLedger(null))).toBe('');
    expect(describeUsageLedger(statusWith({}))).toBe('');
  });
});
