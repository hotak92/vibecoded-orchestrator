// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — "the model gateway needs restarting" after an update.
//
// An orchestrator update rewrites the gateway's source underneath a running
// daemon and restarts nothing, so the gateway can keep serving the previous
// release. It is NEVER restarted automatically: the VS Code panel routes every
// chat through it and a restart ends live agent sessions. So after an update
// the launcher asks (`model_gateway_freshness`) and, only when the backend has
// PROVEN the gateway stale, shows a modal: Continue restarts it
// (`model_gateway_restart_stale`), Dismiss leaves it.
//
// The verdict is decided once, in `python -m vco_lib.gateway_freshness`; this
// file decides only what the GUI does with it, and is pure so vitest can pin
// every branch — including the leave-alone ones — without a DOM.

/** Mirrors `commands::gateway_freshness::RestartPlan`. */
export interface GatewayRestartPlan {
  mechanism: string;
  possible: boolean;
  reason: string;
}

/** Mirrors `commands::gateway_freshness::FreshnessReport`. */
export interface GatewayFreshnessReport {
  verdict: string;
  summary: string;
  running_version: string | null;
  checkout_version: string | null;
  served_sha: string | null;
  expected_sha: string | null;
  pid: number | null;
  port: number | null;
  prompt: boolean;
  restart: GatewayRestartPlan | null;
}

/** Mirrors `commands::gateway_freshness::RestartResult`. */
export interface GatewayRestartResult {
  outcome: string;
  restarted: boolean;
  message: string;
}

/** localStorage key holding the identity of the last dismissed prompt. */
export const DISMISS_STORAGE_KEY = 'vct.gateway_freshness.dismissed';

/** The words the owner specified; one home so the modal and tests agree. */
export const MODAL_TITLE = 'Restart the model gateway?';
export const MODAL_MESSAGE =
  'The model gateway needs restarting to use the updated version. ' +
  'Make sure no agent is running before continuing.';

/**
 * Identity of ONE stale situation: this checkout's source, the running
 * process's code, and that process. A dismissal holds for exactly this —
 * the next update (new checkout digest) or a restart (new pid) asks again.
 */
export function promptIdentity(report: GatewayFreshnessReport): string {
  return [
    report.expected_sha ?? report.checkout_version ?? '',
    report.served_sha ?? report.running_version ?? '',
    report.pid ?? '',
  ].join('|');
}

/**
 * Show the modal? Only for a PROVEN-stale gateway (`prompt`), and not when the
 * user already dismissed this exact situation. Anything else — current,
 * unknown, not running, a failed check (`null`) — shows nothing.
 */
export function shouldOfferRestart(
  report: GatewayFreshnessReport | null | undefined,
  dismissedIdentity: string | null,
): boolean {
  if (!report || report.prompt !== true) return false;
  return promptIdentity(report) !== dismissedIdentity;
}

/**
 * May the modal offer Continue? Only when something that owns the process
 * can restart it. Otherwise the modal still tells the user, and says how to
 * do it by hand, but offers no button that would do nothing.
 */
export function canContinue(report: GatewayFreshnessReport | null | undefined): boolean {
  return !!report && report.prompt === true && report.restart?.possible === true;
}

/** Minimal storage surface (a real `localStorage`, or a test fake). */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function readDismissed(storage: KeyValueStore | null): string | null {
  try {
    return storage?.getItem(DISMISS_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

export function writeDismissed(storage: KeyValueStore | null, identity: string): void {
  try {
    storage?.setItem(DISMISS_STORAGE_KEY, identity);
  } catch {
    // Best-effort: an unwritable store only means the prompt may reappear.
  }
}

export type Invoker = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

export interface FreshnessState {
  report: GatewayFreshnessReport | null;
  open: boolean;
  restarting: boolean;
  result: GatewayRestartResult | null;
  error: string | null;
}

export const INITIAL_FRESHNESS_STATE: FreshnessState = {
  report: null,
  open: false,
  restarting: false,
  result: null,
  error: null,
};

/**
 * The three actions, with I/O injected. The Svelte store is a thin shell
 * around this; the tests drive it with a fake invoker and assert which
 * backend commands each action does — and does NOT — reach.
 */
export function createFreshnessController(deps: {
  invoke: Invoker;
  storage: KeyValueStore | null;
  set: (state: FreshnessState) => void;
  get: () => FreshnessState;
}) {
  const { invoke, storage, set, get } = deps;

  /** Ask the backend. Never restarts anything. A failed check shows nothing. */
  async function check(): Promise<void> {
    if (get().open) return; // a modal already up is not replaced mid-decision
    let report: GatewayFreshnessReport | null = null;
    try {
      report = await invoke<GatewayFreshnessReport>('model_gateway_freshness');
    } catch (e) {
      console.warn('[gateway-freshness] check failed:', e);
      report = null;
    }
    const open = shouldOfferRestart(report, readDismissed(storage));
    set({ ...INITIAL_FRESHNESS_STATE, report, open });
  }

  /** Continue: the ONLY path that restarts the gateway. */
  async function continueRestart(): Promise<void> {
    const state = get();
    if (!state.open || state.restarting || !canContinue(state.report)) return;
    set({ ...state, restarting: true, error: null });
    try {
      const result = await invoke<GatewayRestartResult>('model_gateway_restart_stale');
      set({ ...get(), restarting: false, result });
    } catch (e) {
      set({ ...get(), restarting: false, error: String(e) });
    }
  }

  /** Dismiss: leave the gateway alone and remember this exact situation. */
  function dismiss(): void {
    const state = get();
    // Idempotent: DialogRoot also reports a close it performed itself, and a
    // second call must not turn "Close after a restart" into a dismissal.
    if (!state.open || state.restarting) return;
    if (state.report && !state.result) {
      writeDismissed(storage, promptIdentity(state.report));
    }
    set({ ...INITIAL_FRESHNESS_STATE, report: state.report });
  }

  return { check, continueRestart, dismiss };
}
