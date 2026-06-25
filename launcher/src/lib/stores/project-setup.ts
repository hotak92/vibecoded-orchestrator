// Defect B (v0.2.68): async project-setup progress + serialized add queue.
//
// `create_project_v2` returns FAST (synchronous phase only: DB row +
// `.claude/env`); the heavy phase (bootstrap-collections + install-bundle +
// post-bundle) runs detached on the Rust side and streams a
// `project://setup-progress` event to the global `OperationProgressBanner`.
//
// This store is a MODULE-SINGLETON. The `listen('project://setup-progress')`
// is registered ONCE at module load (mirroring modules.ts:119-153) — NOT in
// a component's `onMount` — so the active-setup state survives the post-add
// route change (ProjectSelector closes its modal and the user navigates to
// the new project view). A component-local `$state` would lose the setup on
// that navigation; the global banner reads this singleton instead.
//
// It also owns a SERIALIZED add QUEUE: rapid "Add" clicks enqueue, and the
// processor pops ONE at a time so two concurrent adds don't both hit the
// synchronous DB/env phase of `create_project_v2` at once. The heavy phases
// themselves are already serialized per-project by the backend re-entrancy
// guard (F7); the queue serializes the fast create-invoke and gives the
// banner a clean "Adding X — N queued" count.
//
// The merge is a PURE reducer (`mergeSetupProgress`) so it's unit-testable
// without a Tauri host — same pattern as `mergeInstallProgress` in modules.ts.
//
// F5 (warnings channel): the terminal `project://setup-progress` event
// carries the classified `warnings` list. The pre-Defect-B inline behaviour
// was that `create_project_v2`'s returned warnings got toasted; after
// backgrounding, the heavy-phase warnings (Weaviate bootstrap deferred,
// bundle preserved-files, schema migration) arrive here instead. We re-toast
// each at its severity (info/amber vs error/red) AND surface them in the
// banner terminal state.

import { writable } from 'svelte/store';
import { invoke, listen, tauriAvailable } from '$lib/tauri';
import { toast } from '$lib/stores/toast';
import type {
  CreateProjectResult,
  ProjectHost,
  ProjectSetupStatus,
  SetupProgressEvent,
  SetupWarning,
} from '$lib/types/launcher';

/** A queued add request. The queue serializes the `create_project_v2`
 *  invoke; the heavy phase then runs detached on the backend. */
export interface AddRequest {
  name: string;
  folder_path: string;
  host: ProjectHost;
  safe_add: boolean;
}

/** Internal queue entry: an `AddRequest` plus the resolver of the per-request
 *  promise `enqueueAdd` hands back (resolved when THIS request's fast
 *  create-invoke returns, so the caller can close its modal). */
interface QueueEntry {
  req: AddRequest;
  resolve: (result: CreateProjectResult | null) => void;
  reject: (err: unknown) => void;
}

/** The active (or most-recent) setup, mirrored from the latest event. */
export interface ActiveSetup {
  project_id: string;
  project_name: string;
  status: ProjectSetupStatus;
  /** Coarse phase on non-terminal events; null on terminal. */
  phase: SetupProgressEvent['phase'];
  /** Populated on the terminal event. */
  warnings: SetupWarning[];
  error: string | null;
  /** ms-epoch when this store first observed the setup — drives the banner's
   *  alive/elapsed indicator. */
  observed_at: number;
}

interface ProjectSetupState {
  /** The setup currently driving the banner (latest event wins). null when
   *  no setup has been observed this session. */
  active: ActiveSetup | null;
  /** Names of adds queued behind the active create-invoke. The banner shows
   *  the count ("Adding X — N queued"). */
  queue: string[];
}

/**
 * Pure reducer: fold an incoming `project://setup-progress` event into the
 * next active-setup view. Extracted so the merge is unit-testable without a
 * Tauri host (the `listen` wiring is skipped entirely in browser mode).
 *
 * The latest event for ANY project wins the banner — setups for different
 * projects can overlap (the backend serializes only per-project), and the
 * user cares about the most recent one. On terminal events we keep the row
 * (the banner self-hides after a delay) but stamp the warnings/error.
 */
export function mergeSetupProgress(
  current: ActiveSetup | null,
  e: SetupProgressEvent,
  now: number,
): ActiveSetup {
  // Preserve `observed_at` if this event is for the same project we're
  // already tracking (so the elapsed timer doesn't reset on each phase).
  const sameProject = current?.project_id === e.project_id;
  return {
    project_id: e.project_id,
    project_name: e.project_name,
    status: e.status,
    phase: e.phase,
    warnings: e.warnings,
    error: e.error,
    observed_at: sameProject ? current!.observed_at : now,
  };
}

function isTerminal(status: ProjectSetupStatus): boolean {
  return status === 'done' || status === 'deferred' || status === 'failed';
}

// F5 (queue persistence): the add queue is in-memory, so a launcher restart
// (or window reload) mid-queue would silently drop adds that hadn't started
// yet. We mirror the visible queue-name list to localStorage; on module load
// we surface a one-time toast naming the dropped intents (simplest correct
// option per the directive — "surface a toast on restart if pending-add
// intents existed"). We do NOT auto-replay them: an add carries a folder path
// + host the user chose in the modal, and silently re-running a folder
// operation on restart is riskier than asking the user to re-add. The key is
// cleared once surfaced so the toast fires at most once per dropped batch.
const QUEUE_PERSIST_KEY = 'vct.project_setup_queue';

function loadPersistedQueueNames(): string[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const raw = localStorage.getItem(QUEUE_PERSIST_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function persistQueueNames(names: string[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    if (names.length === 0) localStorage.removeItem(QUEUE_PERSIST_KEY);
    else localStorage.setItem(QUEUE_PERSIST_KEY, JSON.stringify(names));
  } catch {
    // Non-fatal: persistence is best-effort. Worst case we lose the
    // restart-surfacing toast for this batch.
  }
}

function createProjectSetupStore() {
  const { subscribe, update, set } = writable<ProjectSetupState>({
    active: null,
    queue: [],
  });

  // Serialized-queue processor state. `processing` guards the single-flight
  // invariant; the actual create-invoke is injected (so tests / callers can
  // supply it) — defaults to the real `create_project_v2` wrapper below.
  let processing = false;

  /**
   * The create-invoke used by the queue processor. Pulled out as a setter so
   * the consumer (the projects store) injects its own `create` wrapper,
   * avoiding a circular import between projects.ts and project-setup.ts.
   * Returns the new project id on success (or null).
   */
  let createFn:
    | ((req: AddRequest) => Promise<CreateProjectResult | null>)
    | null = null;

  function setCreateFn(
    fn: (req: AddRequest) => Promise<CreateProjectResult | null>,
  ) {
    createFn = fn;
  }

  async function drainQueue(): Promise<void> {
    if (processing) return;
    processing = true;
    try {
      // Loop until the queue is empty. Each create-invoke returns FAST (the
      // heavy phase is detached); we pop the next only after the previous
      // invoke RETURNS so the synchronous DB/env phase never overlaps.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const next = popNext();
        if (!next) break;
        if (!createFn) {
          // No injected create-fn (browser mode / not wired). Resolve the
          // promise with null so the caller doesn't hang, then continue.
          next.resolve(null);
          continue;
        }
        try {
          const result = await createFn(next.req);
          next.resolve(result);
        } catch (err) {
          // The synchronous phase failed hard (folder gone, DB error). Reject
          // THIS request's promise so the caller can render the error, then
          // continue the queue so one bad add doesn't wedge the rest.
          next.reject(err);
        }
      }
    } finally {
      processing = false;
    }
  }

  function popNext(): QueueEntry | null {
    const entry = pending.shift() ?? null;
    if (entry) {
      update((s) => {
        const queue = s.queue.slice(1);
        persistQueueNames(queue);
        return { ...s, queue };
      });
    }
    return entry;
  }

  // The real (object) queue; `state.queue` mirrors just the names for the UI.
  const pending: QueueEntry[] = [];

  /**
   * Enqueue an add. Pushes onto the serialized queue and kicks the processor.
   * The first add starts immediately; subsequent rapid adds wait their turn.
   * Returns a promise that resolves when THIS request's FAST create-invoke
   * returns (the caller closes its modal then) — the heavy phase continues
   * detached, observed via the global banner.
   */
  function enqueueAdd(req: AddRequest): Promise<CreateProjectResult | null> {
    return new Promise<CreateProjectResult | null>((resolve, reject) => {
      pending.push({ req, resolve, reject });
      update((s) => {
        const queue = [...s.queue, req.name];
        persistQueueNames(queue);
        return { ...s, queue };
      });
      void drainQueue();
    });
  }

  // ── Module-load listener (singleton) ──────────────────────────────────
  // Registered ONCE here so the active-setup state survives route changes.
  if (tauriAvailable()) {
    // F5 (queue persistence): if the previous session left queued add-names
    // that never started (in-memory queue lost to a restart/reload), surface
    // them once so the user knows to re-add — no silent loss. We do NOT
    // auto-replay (re-running a folder operation unattended is riskier than
    // asking). The persisted-names key is cleared after surfacing.
    const dropped = loadPersistedQueueNames();
    if (dropped.length > 0) {
      persistQueueNames([]);
      const list = dropped.join(', ');
      toast.info(
        `${dropped.length} queued project add${dropped.length === 1 ? '' : 's'} ` +
          `(${list}) did not finish before the launcher restarted — ` +
          `please re-add ${dropped.length === 1 ? 'it' : 'them'}.`,
      );
    }

    void listen<SetupProgressEvent>('project://setup-progress', (evt) => {
      const e = evt.payload;
      update((s) => ({
        ...s,
        active: mergeSetupProgress(s.active, e, Date.now()),
      }));

      // F5: on the TERMINAL event, re-toast every classified warning at its
      // severity (info/amber for deferral + preserved-files, error/red for a
      // genuine subprocess failure). Preserves the pre-Defect-B inline toast
      // behaviour now that the heavy-phase warnings arrive asynchronously.
      if (isTerminal(e.status)) {
        for (const w of e.warnings) {
          if (w.severity === 'error') toast.error(w.message);
          else toast.info(w.message);
        }
      }
    });
  }

  return {
    subscribe,
    setCreateFn,
    enqueueAdd,
    /** Test/edge hook: reset to empty (e.g. after a hard error). */
    reset() {
      pending.length = 0;
      set({ active: null, queue: [] });
    },
    /** Dismiss the active banner (terminal states). */
    dismiss() {
      update((s) => ({ ...s, active: null }));
    },
  };
}

export const projectSetup = createProjectSetupStore();
