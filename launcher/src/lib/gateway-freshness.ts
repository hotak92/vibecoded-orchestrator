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

/**
 * The one format of the visible check-failure state (v0.2.101, P299-A2).
 * Both ask paths use it, so a failure reads the same wherever it surfaces.
 */
export function checkFailureMessage(e: unknown): string {
  return `gateway freshness check failed: ${String(e)}`;
}

/**
 * What a "Restart gateway…" click that did NOT open the modal should still
 * say (review, 1A nit 2): the backend's own summary for a non-stale verdict,
 * so the click is never a silent no-op — e.g. the gateway restarted between
 * the usage bridge's polls and is already current while the card still says
 * `outdated_gateway`. `null` when there is nothing to add: the modal opened
 * (a proven-stale verdict IS the answer), or the check failed (its own
 * visible `error` state is the answer). One home: the usage card and the
 * Services page render the same rule.
 */
export function offerNote(report: GatewayFreshnessReport | null): string | null {
  return report && report.prompt !== true ? report.summary : null;
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
 * The four actions, with I/O injected. The Svelte store is a thin shell
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

  // Single-flight (review R1 F3): the launcher-start timer and an update that
  // finishes meanwhile can both ask. A second caller shares the pending check
  // instead of starting its own, and an answer that lands while the modal is
  // up, restarting, or showing a result is DROPPED — before this, a late check
  // reset `restarting`/`result`, re-enabled Continue mid-restart, and a second
  // click restarted the gateway twice.
  let pendingCheck: Promise<void> | null = null;

  function decisionInProgress(state: FreshnessState): boolean {
    return state.open || state.restarting || state.result !== null;
  }

  /**
   * Ask the backend. Never restarts anything; never opens the modal for a
   * dismissed situation. A failed check shows no MODAL, but since v0.2.101
   * (P299-A2) it is recorded in `error` — a visible state the Services page
   * renders — instead of living in the console only.
   */
  function check(): Promise<void> {
    if (pendingCheck) return pendingCheck;
    pendingCheck = (async () => {
      // A modal already up is never replaced mid-decision.
      if (decisionInProgress(get())) return;
      let report: GatewayFreshnessReport | null = null;
      let failure: string | null = null;
      try {
        report = await invoke<GatewayFreshnessReport>('model_gateway_freshness');
      } catch (e) {
        console.warn('[gateway-freshness] check failed:', e);
        report = null;
        failure = checkFailureMessage(e);
      }
      // Re-read AFTER the await: the state may have moved on while we waited.
      if (decisionInProgress(get())) return;
      const open = shouldOfferRestart(report, readDismissed(storage));
      set({ ...INITIAL_FRESHNESS_STATE, report, open, error: failure });
    })().finally(() => {
      pendingCheck = null;
    });
    return pendingCheck;
  }

  /**
   * The user-initiated ask (v0.2.101, P299-A2): a FRESH check that IGNORES
   * the stored dismissal — pressing "Restart gateway…" IS the user asking
   * again, and before this a single Dismiss suppressed the prompt forever —
   * opening the existing modal when, and only when, the backend proves the
   * gateway stale. It never restarts anything itself: the modal's Continue
   * stays the ONLY restart path. A failed check is recorded in `error` (a
   * visible state), because this ran on an explicit click and silence would
   * look like a dead button. Returns the fresh report; `null` only when the
   * check itself failed.
   *
   * Deliberately NOT folded into `check`'s single-flight: that one coalesces
   * automatic asks (launch timer, post-update) where a second caller wants
   * the same answer. Here the user asked NOW and must get a fresh verdict;
   * the post-await `decisionInProgress` guard keeps overlapping landings
   * from clobbering a modal, a restart or a result already under way.
   */
  async function offer(): Promise<GatewayFreshnessReport | null> {
    // Never disturb a decision already under way — the modal being up means
    // the user is already looking at the only restart path.
    if (decisionInProgress(get())) return get().report;
    let report: GatewayFreshnessReport | null = null;
    try {
      report = await invoke<GatewayFreshnessReport>('model_gateway_freshness');
    } catch (e) {
      // Same post-await guard as the success path (review, 1A nit 1): a
      // background check may have opened the modal while this ask was in
      // flight, and a FAILED ask must never reset the store and close the
      // modal the user is reading. The failure stays console-only in that
      // case — the open modal outranks it.
      if (!decisionInProgress(get())) {
        set({ ...INITIAL_FRESHNESS_STATE, error: checkFailureMessage(e) });
      }
      return null;
    }
    // Re-read AFTER the await: a background check may have opened the modal
    // (or a restart begun) while this ask was in flight.
    if (decisionInProgress(get())) return report;
    if (report.prompt === true) {
      // The stored dismissal is deliberately NOT consulted — see above. The
      // next Dismiss re-stores it, so nothing is lost by leaving it alone.
      set({ ...INITIAL_FRESHNESS_STATE, report, open: true });
    } else {
      set({ ...INITIAL_FRESHNESS_STATE, report });
    }
    return report;
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

  return { check, continueRestart, dismiss, offer };
}

/** How long after launcher start the first check runs (boot probes first). */
export const STARTUP_CHECK_DELAY_MS = 4000;

/** Timer surface, so the schedule is testable with fake timers. */
export interface TimerApi {
  setTimeout: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeout: (handle: ReturnType<typeof setTimeout>) => void;
}

/**
 * Arm the launcher-start check and return its cancel (review R1 F15). The
 * layout calls the cancel on teardown, so a remount (HMR, a dev reload) never
 * piles up checks from windows that no longer exist.
 *
 * v0.2.99: the default is now `undefined` + a direct call to the REAL
 * globals. The previous default `{ setTimeout, clearTimeout }` captured
 * DETACHED natives and invoked them as plain-object methods — a
 * `TypeError: Illegal invocation` in every real browser engine (Chromium,
 * WebKitGTK) the moment the layout mounted. Vitest's fake timers do not
 * enforce the receiver, which is why the unit suite stayed green while the
 * launcher's `+layout.svelte` onMount died BEFORE registering the
 * orchestrator update check — v0.2.97 shipped with no update badge and no
 * hourly re-check on any real install. The injected `timers` parameter is
 * unchanged for tests that need fake timers.
 */
export function scheduleStartupCheck(
  check: () => unknown,
  timers?: TimerApi,
  delayMs: number = STARTUP_CHECK_DELAY_MS,
): () => void {
  const handle = timers
    ? timers.setTimeout(() => void check(), delayMs)
    : setTimeout(() => void check(), delayMs);
  return () => {
    if (timers) timers.clearTimeout(handle);
    else clearTimeout(handle);
  };
}
