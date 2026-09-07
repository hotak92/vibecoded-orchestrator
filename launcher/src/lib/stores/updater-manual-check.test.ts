// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.83 (WP-A2 / D6): tests for the updater store's `manualCheck()` — the
// ONE real update-check entry point behind RightSidebar's "Check Update"
// button and the UpdateBadge amber "Retry now" button. Replaces the old
// setTimeout fake (A-RC5).
//
// Contract under test (D6):
//   - browser mode (no Tauri) ⇒ 'check_failed';
//   - runs orchestrator.checkStatus(); then reads the fresh store:
//       null updateStatus                    ⇒ 'check_failed'
//       remote_check.state === 'unknown'     ⇒ 'check_failed'
//       a real update kind present           ⇒ 'available' (+ un-dismiss)
//       otherwise                            ⇒ 'up_to_date'
//
// v0.2.92 (WP-13): migrated from the `remote_check_ok` / `remote_check_error`
// bool pair to the tri-state `remote_check: CheckState`. The pair's
// `not_applicable` case did not exist, and its ABSENCE was read as healthy;
// both are now explicit states. See `orchestrator.ts::renderCheck`.
//   - the `remoteCheckFailed` amber-derivation is populated for the
//     check_failed case so the badge can paint amber.

import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { get, writable } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// localStorage polyfill — updater.ts pulls in install-state-store which reads
// localStorage synchronously at store construction (loadSeen()).
beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() { return s.size; },
      clear() { s.clear(); },
      getItem(k: string) { return s.has(k) ? (s.get(k) as string) : null; },
      key(i: number) { return Array.from(s.keys())[i] ?? null; },
      removeItem(k: string) { s.delete(k); },
      setItem(k: string, v: string) { s.set(k, String(v)); },
    } as Storage;
  }
});

// ---------------------------------------------------------------------------
// Mock ./orchestrator: a real writable store (so `get(orchestrator)` works)
// plus a controllable `checkStatus` and a `cancelScheduledRetry` spy. The
// test drives what `checkStatus()` "resolves to" by setting `nextStoreValue`
// — checkStatus applies it to the store and resolves.
// ---------------------------------------------------------------------------

type OrchValue = {
  status: string;
  version: string;
  installPath: string;
  updateStatus: Record<string, unknown> | null;
  // v0.2.83 (N-4): mirror the real orchestrator store field the updater now
  // derives its amber `remoteCheckFailed` from.
  lastCheckFailed: boolean | null;
};

const orchStore = writable<OrchValue>({
  status: 'installed',
  version: '0.2.82',
  installPath: '/install/root',
  updateStatus: null,
  lastCheckFailed: null,
});

let nextStoreValue: OrchValue | null = null;
const checkStatusMock = vi.fn(async () => {
  if (nextStoreValue) orchStore.set(nextStoreValue);
});
const cancelScheduledRetryMock = vi.fn();

let tauriIsAvailable = true;

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
  tauriAvailable: () => tauriIsAvailable,
}));

vi.mock('./orchestrator', () => ({
  orchestrator: {
    subscribe: orchStore.subscribe,
    checkStatus: checkStatusMock,
  },
  cancelScheduledRetry: () => cancelScheduledRetryMock(),
  // v0.2.92 (WP-13): the tri-state helpers are PURE, so the mock ships the
  // real behaviour rather than a stub. A stub here would let the updater's
  // reading of `remote_check` pass while the shipped narrowing did something
  // else — the mock would be testing itself.
  renderCheck: (st: { state?: string } | null | undefined) => {
    if (!st || typeof st !== 'object') return 'unknown';
    if (st.state === 'ok') return 'ok';
    if (st.state === 'not_applicable') return 'not_applicable';
    return 'unknown';
  },
  checkError: (st: { state?: string; error?: string } | null | undefined) =>
    st && typeof st === 'object' && st.state === 'unknown' ? st.error || null : null,
}));

/** Local mirror of `orchestrator.ts::renderCheck`, used by the `value()`
 *  helper below so the fixture computes `lastCheckFailed` the same way the
 *  real store does. Kept in sync by
 *  `renders_could_not_check_distinct_from_up_to_date` above, which drives the
 *  real narrowing through the updater. */
function narrow(st: unknown): 'ok' | 'not_applicable' | 'unknown' {
  const o = st as { state?: string } | null | undefined;
  if (!o || typeof o !== 'object') return 'unknown';
  if (o.state === 'ok') return 'ok';
  if (o.state === 'not_applicable') return 'not_applicable';
  return 'unknown';
}

// The tri-state, in the wire shape Rust emits.
const CHECK_OK = { state: 'ok' } as const;
const CHECK_NA = { state: 'not_applicable' } as const;
const checkUnknown = (error: string) => ({ state: 'unknown', error }) as const;

// Full UpdateStatus fixture with parametrised remote-check + update flags.
// `remote_check` defaults to `ok` so a test that does not care about the
// probe reads as "the check ran and succeeded".
function status(
  over: Partial<{
    remote_ahead: boolean;
    install_stale: boolean;
    binary_stale: boolean;
    remote_check: Record<string, unknown>;
    head_detached: boolean;
  }> = {},
): Record<string, unknown> {
  return {
    remote_ahead: false,
    install_stale: false,
    binary_stale: false,
    source_version: '0.2.83',
    installed_version: '0.2.82',
    running_version: '0.2.82',
    on_disk_binary_version: '0.2.82',
    remote_check: CHECK_OK,
    head_detached: false,
    ...over,
  };
}

function value(updateStatus: Record<string, unknown> | null): OrchValue {
  // v0.2.83 (N-4) / v0.2.92 (WP-13): compute lastCheckFailed EXACTLY as the
  // real orchestrator store does on a completed installed-check, so the
  // updater's derivation is exercised against realistic store shapes: a null
  // status OR an `unknown` remote_check is a failed check; `ok` and
  // `not_applicable` are not.
  //
  // NOTE the deliberate difference from the pre-v0.2.92 helper: a MISSING
  // `remote_check` is now a FAILED check, not a healthy one. That compat
  // branch existed for a launcher GUI running against an older Rust binary —
  // which cannot happen (they ship in one binary) — and it encoded exactly
  // the reflex this release removes: inferring health from silence.
  const lastCheckFailed =
    updateStatus === null || narrow(updateStatus.remote_check) === 'unknown';
  return {
    status: 'installed',
    version: '0.2.82',
    installPath: '/install/root',
    updateStatus,
    lastCheckFailed,
  };
}

let updater: typeof import('./updater').updater;

beforeEach(async () => {
  vi.resetModules();
  checkStatusMock.mockClear();
  cancelScheduledRetryMock.mockClear();
  tauriIsAvailable = true;
  nextStoreValue = null;
  orchStore.set(value(null));
  const mod = await import('./updater');
  updater = mod.updater;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('manualCheck (D6)', () => {
  it("returns 'up_to_date' when the check succeeds with no pending update", async () => {
    nextStoreValue = value(status({ remote_check: CHECK_OK }));

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('up_to_date');
    expect(checkStatusMock).toHaveBeenCalledTimes(1);
    // Cancelled the scheduled retry as part of "cancel + replace".
    expect(cancelScheduledRetryMock).toHaveBeenCalledTimes(1);
    const s = get(updater);
    expect(s.checking).toBe(false);
    expect(s.remoteCheckFailed).toBe(false);
    expect(s.kind).toBeNull();
  });

  it("returns 'available' and resets dismissed=false when a real update is pending", async () => {
    // Pre-dismiss the badge so we can prove manualCheck un-dismisses it.
    updater.dismiss();
    expect(get(updater).dismissed).toBe(true);

    nextStoreValue = value(status({ remote_ahead: true, remote_check: CHECK_OK }));

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('available');
    const s = get(updater);
    expect(s.kind).toBe('remote_ahead');
    expect(s.dismissed).toBe(false); // re-shown
    expect(s.available).toBe(true);
    expect(s.checking).toBe(false);
  });

  it("returns 'check_failed' when remote_check is unknown", async () => {
    nextStoreValue = value(
      status({ remote_check: checkUnknown('fetch failed') }),
    );

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('check_failed');
    const s = get(updater);
    // Amber derivation populated so the badge can render the failed state.
    expect(s.remoteCheckFailed).toBe(true);
    expect(s.remoteCheckError).toBe('fetch failed');
    expect(s.kind).toBeNull();
    expect(s.checking).toBe(false);
  });

  it("returns 'check_failed' when updateStatus is null (command soft-failed)", async () => {
    nextStoreValue = value(null);

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('check_failed');
    const s = get(updater);
    expect(s.checking).toBe(false);
    // v0.2.83 (N-4): a null updateStatus completed check now DOES paint amber —
    // the derivation reads the orchestrator store's explicit lastCheckFailed
    // (true here), not the (null) updateStatus. Pre-N-4 this rendered NOTHING,
    // silently implying "up to date" for a check that actually failed.
    expect(s.remoteCheckFailed).toBe(true);
    // No status object ⇒ no error string to surface (the badge falls back to
    // the generic "Couldn't check for updates" copy).
    expect(s.remoteCheckError).toBeNull();
    expect(s.kind).toBeNull();
  });

  it("returns 'check_failed' in browser mode WITHOUT calling the backend", async () => {
    tauriIsAvailable = false;

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('check_failed');
    expect(checkStatusMock).not.toHaveBeenCalled();
    expect(get(updater).checking).toBe(false);
  });

  // v0.2.92 (WP-13). This test USED to assert the opposite — that a MISSING
  // `remote_check_ok` meant 'up_to_date'. That compat branch is deleted, and
  // with it the test that pinned it: a trusted test asserting "absent means
  // healthy" is the most expensive form of the defect this release fixes,
  // because it makes the wrong behaviour look deliberate.
  it("treats a MISSING remote_check as 'check_failed', NOT 'up_to_date'", async () => {
    const bare = status();
    delete bare.remote_check;
    nextStoreValue = value(bare);

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('check_failed');
    expect(get(updater).remoteCheckFailed).toBe(true);
  });

  // v0.2.92 (WP-13): the third state. An install with no git remote is not a
  // failed check and must not paint amber or schedule retries — but it is
  // also not a *successful* remote check, and nothing may claim it was.
  it("treats a NOT_APPLICABLE remote_check as 'up_to_date' without amber", async () => {
    nextStoreValue = value(status({ remote_check: CHECK_NA }));

    const outcome = await updater.manualCheck();

    expect(outcome).toBe('up_to_date');
    const s = get(updater);
    expect(s.remoteCheckFailed).toBe(false);
    expect(s.remoteCheckError).toBeNull();
  });

  // The three renderings must be mutually distinguishable at the store
  // boundary — this is the "couldn't check renders distinctly from up to
  // date" acceptance criterion, asserted as one comparison.
  it('renders could-not-check distinctly from up-to-date and not-applicable', async () => {
    const outcomes: Record<string, string> = {};
    for (const [label, check] of [
      ['ok', CHECK_OK],
      ['not_applicable', CHECK_NA],
      ['unknown', checkUnknown('rev-list: fatal: ambiguous argument')],
    ] as const) {
      nextStoreValue = value(status({ remote_check: check }));
      outcomes[label] = await updater.manualCheck();
    }
    expect(outcomes.ok).toBe('up_to_date');
    expect(outcomes.not_applicable).toBe('up_to_date');
    expect(outcomes.unknown).toBe('check_failed');
    expect(outcomes.unknown).not.toBe(outcomes.ok);
  });
});

describe('remoteCheckFailed derivation (D3) via syncFromOrchestrator', () => {
  it('is true only when remote_check is unknown AND no real kind', async () => {
    orchStore.set(value(status({ remote_check: checkUnknown('x') })));
    updater.syncFromOrchestrator();
    expect(get(updater).remoteCheckFailed).toBe(true);
  });

  it('is suppressed when a real update kind is present (never amber over a real badge)', async () => {
    orchStore.set(
      value(status({ remote_ahead: true, remote_check: checkUnknown('x') })),
    );
    updater.syncFromOrchestrator();
    const s = get(updater);
    expect(s.kind).toBe('remote_ahead');
    expect(s.remoteCheckFailed).toBe(false);
  });

  it('is false when the remote check succeeded', async () => {
    orchStore.set(value(status({ remote_check: CHECK_OK })));
    updater.syncFromOrchestrator();
    expect(get(updater).remoteCheckFailed).toBe(false);
  });

  // v0.2.83 (N-4): the two cases the OLD (updateStatus-derived) logic got wrong.
  it('N-4: is TRUE for a null-status completed check (amber derivable, not blank)', async () => {
    // updateStatus === null but the check COMPLETED and FAILED → lastCheckFailed
    // true. The old `!!us && ...` derivation returned false here (blank badge).
    orchStore.set(value(null));
    updater.syncFromOrchestrator();
    const s = get(updater);
    expect(s.remoteCheckFailed).toBe(true);
    expect(s.kind).toBeNull();
    expect(s.remoteCheckError).toBeNull();
  });

  it('N-4: is FALSE before the first completed check (lastCheckFailed=null → no amber flash)', async () => {
    // Simulate the pre-check store state directly (lastCheckFailed null).
    orchStore.set({
      status: 'installed',
      version: '0.2.82',
      installPath: '/install/root',
      updateStatus: null,
      lastCheckFailed: null,
    });
    updater.syncFromOrchestrator();
    // null !== true → no amber during startup, even though updateStatus is null.
    expect(get(updater).remoteCheckFailed).toBe(false);
  });
});

// Structural assertion (D6, A-RC5): RightSidebar's "Check Update" outcome
// must come from the REAL flow (updater.manualCheck()), never a setTimeout-
// faked "Up to date". This is the *-logic.test.ts source-text idiom used
// elsewhere in the repo (color-rgb.test.ts, extras-orphan-listener-gate).
describe('RightSidebar "Check Update" is wired to the real flow, not a fake', () => {
  const HERE = dirname(fileURLToPath(import.meta.url)); // src/lib/stores
  const RIGHT_SIDEBAR = resolve(
    HERE,
    '..',
    'components',
    'RightSidebar.svelte',
  );

  it('handleCheckUpdate calls updater.manualCheck()', () => {
    const src = readFileSync(RIGHT_SIDEBAR, 'utf-8');
    expect(src).toContain('updater.manualCheck()');
  });

  it('does NOT set "Up to date" from a setTimeout (no faked outcome)', () => {
    const src = readFileSync(RIGHT_SIDEBAR, 'utf-8');
    // The old fake was: setTimeout(() => { updateStatus = 'Up to date' ... }).
    // Assert no setTimeout body assigns any of the outcome literals — the
    // ONLY setTimeout that survives is the 4s auto-CLEAR (sets null), which
    // must never carry an outcome string.
    //
    // Extract each setTimeout(...) callback body and forbid outcome literals
    // inside it.
    const outcomeLiterals = [
      "'Up to date'",
      "'Update available — see badge'",
      "'Check failed'",
      "'Checking…'",
    ];
    // Find every `setTimeout(` occurrence and scan a generous window after it.
    let idx = src.indexOf('setTimeout(');
    while (idx !== -1) {
      const window = src.slice(idx, idx + 300);
      for (const lit of outcomeLiterals) {
        expect(
          window.includes(lit),
          `setTimeout body must not assign the outcome literal ${lit} ` +
            `(would resurrect the A-RC5 fake). Offending window:\n${window}`,
        ).toBe(false);
      }
      idx = src.indexOf('setTimeout(', idx + 1);
    }
  });
});
