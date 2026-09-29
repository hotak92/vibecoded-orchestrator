// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, AD-1 + L3-F02/F06): ONE router for every update failure.
//
// The shapes come from the Rust producer's own contract file,
// `tests/fixtures/update_failure_messages.json` (written by WP-03a,
// `update_failure.rs` executes every row). This test reads THAT file, so the
// producer and this consumer cannot drift apart silently:
//   - every `surface_errors[].json` routes to the modal its kind names (the
//     four payload kinds) or to the overlay's failed state (Refused /
//     InstallFailed / Raw) carrying the producer's message ONCE;
//   - every `normalise_cases` row normalises to the same text on this side;
//   - legacy `event`-tagged payloads (still produced by the conflict modal's
//     git commands) keep routing to their modals;
//   - `handleLocally` leaves a payload to the calling modal.

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() {
        return s.size;
      },
      clear: () => s.clear(),
      getItem: (k: string) => (s.has(k) ? (s.get(k) as string) : null),
      key: (i: number) => Array.from(s.keys())[i] ?? null,
      removeItem: (k: string) => void s.delete(k),
      setItem: (k: string, v: string) => void s.set(k, String(v)),
    } as Storage;
  }
});

let runReject: unknown = undefined;

vi.mock('$lib/tauri', () => ({
  invoke: async (cmd: string) => {
    if (cmd === 'run_orchestrator_update' && runReject !== undefined) throw runReject;
    return undefined;
  },
  safeInvoke: async () => null,
  listen: async () => () => {},
  tauriAvailable: () => true,
  isTauriRuntime: () => false,
}));

type SurfaceRow = { name: string; json: Record<string, unknown> };
type NormaliseRow = { raw: string; fallback: string; expect: string };
type Fixture = {
  contract: {
    command: string;
    update_kinds: string[];
    kinds: string[];
    forbidden_message_prefix: string;
    payload_events: Record<string, string>;
  };
  surface_errors: SurfaceRow[];
  normalise_cases: NormaliseRow[];
};

const FIXTURE: Fixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../../../tests/fixtures/update_failure_messages.json', import.meta.url)),
    'utf-8',
  ),
);

type Updater = typeof import('./updater');
let U: Updater;

beforeEach(async () => {
  vi.resetModules();
  runReject = undefined;
  U = await import('./updater');
});

/** Which store route each producer kind must reach. */
const ROUTE_FOR_KIND: Record<string, string> = {
  NonFastForward: 'nonFf',
  UntrackedCollision: 'untrackedCollision',
  AutostashPop: 'autostashPop',
  Conflict: 'conflict',
  Refused: 'failed',
  InstallFailed: 'failed',
  Raw: 'failed',
};

describe('the contract file matches this side', () => {
  it('names the command and the six kinds the store sends', () => {
    expect(FIXTURE.contract.command).toBe('run_orchestrator_update');
    expect(FIXTURE.contract.update_kinds).toEqual([...U.UPDATE_RUN_KINDS]);
  });

  it('every error kind the producer can emit has a route here', () => {
    expect([...FIXTURE.contract.kinds].sort()).toEqual(Object.keys(ROUTE_FOR_KIND).sort());
    // And the fixture exercises every kind at least once.
    const seen = new Set(FIXTURE.surface_errors.map((r) => r.json.kind));
    for (const k of FIXTURE.contract.kinds) expect(seen.has(k), k).toBe(true);
  });
});

describe('routeUpdateError over the producer rows', () => {
  for (const row of FIXTURE.surface_errors) {
    it(`${row.name} → ${ROUTE_FOR_KIND[row.json.kind as string]}`, () => {
      const wire = JSON.stringify(row.json); // the command rejects with a STRING
      const routed = U.routeUpdateError(wire);
      expect(routed.to).toBe(ROUTE_FOR_KIND[row.json.kind as string]);
      if (routed.to === 'failed') {
        // Rendered ONCE, unprefixed, verbatim.
        expect(routed.message).toBe(row.json.message);
        expect(routed.message.startsWith(FIXTURE.contract.forbidden_message_prefix)).toBe(false);
        expect(routed.errorKind).toBe(row.json.kind);
      } else {
        // The modal payload carries the legacy event tag the modal expects.
        expect((routed.payload as { event: string }).event).toBe(
          FIXTURE.contract.payload_events[row.json.kind as string],
        );
      }
    });
  }

  it('also routes the same rows when the webview wraps them in an Error', () => {
    for (const row of FIXTURE.surface_errors) {
      const routed = U.routeUpdateError(new Error(JSON.stringify(row.json)));
      expect(routed.to, row.name).toBe(ROUTE_FOR_KIND[row.json.kind as string]);
    }
  });

  it('NonFastForward keeps the file split the divergence modal renders', () => {
    const row = FIXTURE.surface_errors.find((r) => r.json.kind === 'NonFastForward')!;
    const routed = U.routeUpdateError(JSON.stringify(row.json));
    if (routed.to !== 'nonFf') throw new Error('expected nonFf');
    expect(routed.payload.branch).toBe(row.json.branch);
    expect(routed.payload.diverged_files).toEqual(row.json.diverged_files);
    expect(routed.payload.local_only_files).toEqual(row.json.local_only_files);
    expect(routed.payload.upstream_only_count).toBe(row.json.upstream_only_count);
  });

  it('a payload nested under `payload` routes the same as a flattened one', () => {
    const routed = U.routeUpdateError(
      JSON.stringify({
        kind: 'Conflict',
        message: 'stopped',
        payload: { operation: 'rebase', branch: 'main', conflicted_files: ['x'], git_stderr: '' },
      }),
    );
    expect(routed.to).toBe('conflict');
    if (routed.to === 'conflict') expect(routed.payload.operation).toBe('rebase');
  });

  it('a payload kind missing its payload degrades to the failed state with the message', () => {
    const routed = U.routeUpdateError(JSON.stringify({ kind: 'Conflict', message: 'no files listed' }));
    expect(routed).toEqual({ to: 'failed', message: 'no files listed', errorKind: 'Conflict' });
  });
});

describe('normalizeFailureText over the producer normalise_cases', () => {
  for (const row of FIXTURE.normalise_cases) {
    it(JSON.stringify(row.raw), () => {
      expect(U.normalizeFailureText(row.raw, row.fallback)).toBe(row.expect);
    });
  }

  it('a plain-text rejection is routed to failed with the prefix stripped once', () => {
    expect(U.routeUpdateError('Update failed: Update failed: hub would not stop')).toEqual({
      to: 'failed',
      message: 'hub would not stop',
      errorKind: null,
    });
  });
});

describe('legacy event-tagged payloads still route (conflict-modal git commands)', () => {
  const legacy: Array<[string, Record<string, unknown>]> = [
    ['nonFf', { event: 'orchestrator_update_non_ff', branch: 'main', local_sha: null, remote_sha: null, diverged_files: [], git_stderr: '' }],
    ['untrackedCollision', { event: 'orchestrator_untracked_collision', operation: 'merge', branch: 'main', divergent_files: ['a'] }],
    ['autostashPop', { event: 'orchestrator_autostash_pop_conflict', branch: 'main', conflicted_files: ['a'], git_stderr: '' }],
    ['conflict', { event: 'orchestrator_update_conflict', operation: 'merge', branch: 'main', conflicted_files: ['a'], git_stderr: '' }],
  ];
  it.each(legacy)('%s', (to, payload) => {
    expect(U.routeUpdateError(`\n  ${JSON.stringify(payload)}`).to).toBe(to);
  });
});

describe('the store applies the route (run + failOp)', () => {
  it('each producer payload row opens its modal and is not a failure', async () => {
    for (const row of FIXTURE.surface_errors) {
      const to = ROUTE_FOR_KIND[row.json.kind as string];
      if (to === 'failed') continue;
      vi.resetModules();
      U = await import('./updater');
      runReject = JSON.stringify(row.json);
      const r = await U.updater.run('PullFf');
      expect(r.ok, row.name).toBe(false);
      const s = get(U.updater) as unknown as Record<string, unknown>;
      expect(s[to], row.name).not.toBeNull();
      expect(s.failed, row.name).toBe(false);
      expect(s.updating, row.name).toBe(false);
    }
  });

  it('each producer failure row lands in the overlay once, verbatim', async () => {
    for (const row of FIXTURE.surface_errors) {
      if (ROUTE_FOR_KIND[row.json.kind as string] !== 'failed') continue;
      runReject = JSON.stringify(row.json);
      await U.updater.run('ApplyOnly');
      const s = get(U.updater);
      expect(s.failed, row.name).toBe(true);
      expect(s.error, row.name).toBe(row.json.message);
    }
  });

  it('handleLocally leaves the untracked collision to the calling modal', async () => {
    const row = FIXTURE.surface_errors.find((r) => r.json.kind === 'UntrackedCollision')!;
    runReject = JSON.stringify(row.json);
    const r = await U.updater.run('Merge', { handleLocally: ['untrackedCollision'] });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.routed.to).toBe('untrackedCollision');
    expect(get(U.updater).untrackedCollision).toBeNull();
    expect(get(U.updater).failed).toBe(false);
  });

  it('failOp routes a structured payload from a modal git command to its modal', () => {
    U.updater.beginOp('keep_local');
    const routed = U.updater.failOp(
      JSON.stringify({ event: 'orchestrator_untracked_collision', operation: 'merge', branch: 'main', divergent_files: ['a'] }),
    );
    expect(routed.to).toBe('untrackedCollision');
    expect(get(U.updater).untrackedCollision).not.toBeNull();
    expect(get(U.updater).failed).toBe(false);
  });
});
