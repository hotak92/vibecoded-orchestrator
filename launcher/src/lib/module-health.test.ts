// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (lane V): the module-tile health pill (`module-health.ts`).

import { describe, it, expect } from 'vitest';
import {
  pickModuleHealth,
  resolveModuleHealthPill,
  type HealthSnapshot,
} from './module-health';

const snapshot: HealthSnapshot = {
  'vct-hub-api': {
    health: { state: 'up', last_checked: '2026-09-24T10:00:00Z', last_error: null },
    project_health: {},
  },
  'vct-code-embedding': {
    health: {
      state: 'down',
      last_checked: '2026-09-24T10:00:00Z',
      last_error: 'connection refused or unreachable',
    },
    project_health: {},
  },
  'vct-kg': {
    health: {
      state: 'unknown',
      last_checked: null,
      last_error: "health_check type 'stdio_ping' is not probed by the hub",
    },
    project_health: {},
  },
  'vct-rl-reranker': {
    health: null,
    project_health: {
      p1: { state: 'up', last_checked: '2026-09-24T10:00:00Z', last_error: null },
      p2: { state: 'down', last_checked: '2026-09-24T10:00:00Z', last_error: 'HTTP 503' },
    },
  },
};

describe('resolveModuleHealthPill', () => {
  it('shows up and down from a probe that ran', () => {
    const up = resolveModuleHealthPill(snapshot, 'vct-hub-api', null);
    expect(up?.state).toBe('up');
    expect(up?.label).toBe('Running');
    const down = resolveModuleHealthPill(snapshot, 'vct-code-embedding', 'p1');
    expect(down?.state).toBe('down');
    expect(down?.tooltip).toContain('connection refused');
  });

  it('shows unknown as unknown with the reason, never as down', () => {
    const pill = resolveModuleHealthPill(snapshot, 'vct-kg', null);
    expect(pill?.state).toBe('unknown');
    expect(pill?.label).toBe('Status unknown');
    expect(pill?.tooltip).toContain('stdio_ping');
  });

  it('never carries a stale up/down past a failed refresh', () => {
    for (const id of ['vct-hub-api', 'vct-code-embedding']) {
      const pill = resolveModuleHealthPill(snapshot, id, null, 'read hub.port: missing');
      expect(pill?.state).toBe('unknown');
      expect(pill?.tooltip).toContain('not reachable');
    }
  });

  it("uses the current project's instance of a per-project module", () => {
    expect(resolveModuleHealthPill(snapshot, 'vct-rl-reranker', 'p1')?.state).toBe('up');
    expect(resolveModuleHealthPill(snapshot, 'vct-rl-reranker', 'p2')?.state).toBe('down');
    // Not installed in this project and no machine-wide instance: no pill.
    expect(resolveModuleHealthPill(snapshot, 'vct-rl-reranker', 'p3')).toBeNull();
  });

  it('shows no pill for a module the hub does not report, or with no snapshot', () => {
    expect(resolveModuleHealthPill(snapshot, 'vct-search', null)).toBeNull();
    expect(resolveModuleHealthPill(null, 'vct-hub-api', null)).toBeNull();
    expect(resolveModuleHealthPill(null, 'vct-hub-api', null, 'down')).toBeNull();
  });

  it('treats an unrecognised state as unknown', () => {
    const odd = {
      m: { health: { state: 'flapping', last_checked: null, last_error: null }, project_health: {} },
    } as unknown as HealthSnapshot;
    expect(resolveModuleHealthPill(odd, 'm', null)?.state).toBe('unknown');
  });
});

describe('pickModuleHealth', () => {
  it('prefers the project instance, falls back to the machine-wide one', () => {
    const view = {
      health: { state: 'up' as const, last_checked: null, last_error: null },
      project_health: { p1: { state: 'down' as const, last_checked: null, last_error: 'x' } },
    };
    expect(pickModuleHealth(view, 'p1')?.state).toBe('down');
    expect(pickModuleHealth(view, 'p2')?.state).toBe('up');
    expect(pickModuleHealth(view, null)?.state).toBe('up');
    expect(pickModuleHealth(undefined, 'p1')).toBeNull();
  });
});
