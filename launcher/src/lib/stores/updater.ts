// Orchestrator update detection + dismissal state.
//
// `check_for_updates` is already called by the orchestrator store on every
// `checkStatus()`. This store layers on top: tracks the version detected
// and the timestamp the user last saw a notification, so we don't re-toast
// on every render.
//
// v0.2.16 (W4 / 0.5): the underlying Rust command now returns a full
// `UpdateStatus` struct with three independent flags
// (remote_ahead / install_stale / binary_stale). We render priority-
// based UX in `UpdateBadge.svelte`. The `dismiss-until-version-bump`
// behaviour preserved here applies to the highest-priority pending
// state; once resolved (e.g. install_stale → user clicks Install
// Update → manifest.version catches up to source.version → flag goes
// false) the banner auto-dismisses on the next `checkStatus()` poll.

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import { orchestrator, cancelScheduledRetry } from './orchestrator';
// M-P1-5: scope the seen-version flag by install_root so two clones
// on the same machine maintain independent dismissal state. The
// helper transparently migrates the legacy unscoped key on first
// scoped read.
import {
  getInstallScopedFlag,
  setInstallScopedFlag,
  clearInstallScopedFlag,
} from './install-state-store';
import type { InstallHealth } from '$lib/types/launcher';

const SEEN_KEY = 'vct.update.seen_version';

// Lazy-resolved install_root cache. The updater store is created at
// module-load time (before the first `check_install_health` round-
// trip), so we cannot synchronously know the install_root. Instead we
// resolve it lazily on first read/write and reuse the cached value.
// `null` is a sentinel for "resolved, but unknown" (dev mode); the
// store helpers map that to the "unknown" bucket which still beats
// the cross-clone leak of the pre-v0.2.53 unscoped key.
let cachedInstallRoot: string | null | undefined = undefined;

async function resolveInstallRoot(): Promise<string | null> {
  if (cachedInstallRoot !== undefined) return cachedInstallRoot;
  if (!tauriAvailable()) {
    cachedInstallRoot = null;
    return null;
  }
  try {
    const h = await invoke<InstallHealth>('check_install_health');
    cachedInstallRoot = h.install_root ?? null;
  } catch {
    cachedInstallRoot = null;
  }
  return cachedInstallRoot;
}

/** v0.2.16 (W4 / 0.5): which of the four update signals to render.
 *  Priority order (v0.2.51):
 *    'merge_resolved_incomplete' > 'binary_stale' > 'install_stale' > 'remote_ahead'.
 *  `merge_resolved_incomplete` is HIGHEST priority because every other
 *  flag is meaningless until install.py finishes against the freshly-
 *  merged source: a binary refresh against a non-installed source would
 *  ship a launcher that doesn't match its own manifest.
 *  `null` when no signal is true. */
export type UpdateKind =
  | 'merge_resolved_incomplete'
  | 'binary_stale'
  | 'install_stale'
  | 'remote_ahead'
  | null;

/**
 * v0.2.23 (B4 / D19): structured payload returned by `update_orchestrator`
 * when `git pull --ff-only` fails because the local clone has diverged
 * from upstream. Mirrors Rust `serialize_orchestrator_non_ff_error`.
 *
 * When set, `UpdateBadge.svelte` renders `OrchestratorUpdateDivergenceModal`
 * instead of a raw error toast — the user picks Merge / Rebase / Cancel.
 *
 * v0.2.27: the Rust side splits the file list into two categories so the
 * UI can render them as separate sections. `local_only_files` are paths
 * that exist on the local clone but NOT on upstream (e.g. user-added
 * `other_projects_knowledge/*` — these are not merge blockers). The
 * `diverged_files` list is reserved for paths where BOTH sides have
 * changes that need to be reconciled. The Rust split lands in a separate
 * commit; the modal degrades gracefully if `local_only_files` is absent.
 */
export type OrchestratorNonFfPayload = {
  event: 'orchestrator_update_non_ff';
  branch: string;
  local_sha: string | null;
  remote_sha: string | null;
  diverged_files: string[];
  git_stderr: string;
  /** v0.2.27: paths only present on the local clone (additive, no merge
   *  required). Optional — pre-v0.2.27 Rust returns undefined; the modal
   *  treats the whole `diverged_files` list as diverging in that case. */
  local_only_files?: string[];
};

interface UpdaterState {
  available: boolean;
  /** v0.2.16: which signal is currently being rendered. Drives copy +
   *  action button choice in `UpdateBadge.svelte`. */
  kind: UpdateKind;
  /** The version we detected as available — kept opaque (Rust doesn't
   * expose the new version yet, only a boolean). When the boolean
   * transitions false→true we re-show. */
  lastSeenVersion: string | null;
  updating: boolean;
  error: string | null;
  dismissed: boolean;
  /** v0.2.23 (B4 / D19): when non-null, render the divergence modal
   *  instead of the popover error. Cleared by the modal's onClose. */
  nonFf: OrchestratorNonFfPayload | null;
  /** v0.2.83 (WP-A2 / D6): a `manualCheck()` (RightSidebar "Check Update"
   *  button, or the badge's "Retry now") is in flight. Drives the button's
   *  "Checking…" label so the user gets honest feedback instead of the old
   *  setTimeout fake. Distinct from `updating`, which means an actual
   *  install/update/restart is running. */
  checking: boolean;
  /** v0.2.83 (WP-A2 / D3): the last `check_for_updates` could NOT determine
   *  remote state (`remote_check_ok === false`) AND there is no real pending
   *  update to show (kind === null). When true, `UpdateBadge` renders the
   *  amber "couldn't check, retrying" state instead of nothing — the badge
   *  must NEVER silently imply "up to date" when the check actually failed.
   *  Derived in `syncFromOrchestrator()` from the orchestrator store. */
  remoteCheckFailed: boolean;
  /** v0.2.83 (WP-A2 / D3): the concise error/stage label from the failed
   *  remote check (`updateStatus.remote_check_error`), surfaced in the
   *  amber popover copy. Null when the check succeeded or is not applicable. */
  remoteCheckError: string | null;
}

// Synchronous loadSeen for the initial store value. When the lazy
// install_root resolution has not yet completed, we read whatever the
// store helper sees for the "unknown" bucket — which transparently
// migrates the legacy key. The first async store action that observes
// install_root (refresh / dismiss) then rewrites under the scoped key
// AND clears the unknown bucket.
function loadSeen(): string | null {
  return getInstallScopedFlag(SEEN_KEY, cachedInstallRoot ?? null);
}

async function saveSeen(v: string | null) {
  const root = await resolveInstallRoot();
  if (v) {
    setInstallScopedFlag(SEEN_KEY, root, v);
  } else {
    clearInstallScopedFlag(SEEN_KEY, root);
  }
}

function pickKind(status: {
  remote_ahead: boolean;
  install_stale: boolean;
  binary_stale: boolean;
  merge_resolved_incomplete?: boolean;
} | null): UpdateKind {
  if (!status) return null;
  // v0.2.51 Bug A: merge_resolved_incomplete is HIGHEST priority. When
  // a prior conflict-resolution path was abandoned, every downstream
  // signal is misleading until install.py finishes against the freshly-
  // merged source. Re-entering via resume_orchestrator_update is the
  // ONLY correct next step.
  //
  // Then: binary > install > remote (unchanged from v0.2.16).
  // - binary_stale wins because restart is fastest + a newer binary can
  //   change every other code path.
  // - install_stale next: without an install.py pass, `.claude/` drifts.
  // - remote_ahead last: "fully behind" but lowest urgency.
  if (status.merge_resolved_incomplete) return 'merge_resolved_incomplete';
  if (status.binary_stale) return 'binary_stale';
  if (status.install_stale) return 'install_stale';
  if (status.remote_ahead) return 'remote_ahead';
  return null;
}

/**
 * v0.2.23 (B4 / D19): try to parse a Tauri error as a non-FF divergence
 * payload from `update_orchestrator`. Returns null on any other shape.
 */
function parseNonFfError(raw: unknown): OrchestratorNonFfPayload | null {
  if (typeof raw !== 'string') return null;
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.event === 'orchestrator_update_non_ff') {
      return parsed as OrchestratorNonFfPayload;
    }
  } catch {
    // not JSON
  }
  return null;
}

function createUpdaterStore() {
  const { subscribe, update } = writable<UpdaterState>({
    available: false,
    kind: null,
    lastSeenVersion: loadSeen(),
    updating: false,
    error: null,
    dismissed: false,
    nonFf: null,
    checking: false,
    remoteCheckFailed: false,
    remoteCheckError: null,
  });

  // Local implementation shared by the public `syncFromOrchestrator` method
  // and `manualCheck`. Kept as a plain function (not a `this.`-method call)
  // so it's robust against `this`-binding — an internal caller never has to
  // worry about how the method was invoked.
  function doSync() {
    const o = get(orchestrator);
    const installed = o.status === 'installed' || o.status === 'updating';
    if (!installed) {
      update((s) => ({
        ...s,
        available: false,
        kind: null,
        // Not installed ⇒ no remote to check; clear the amber state too.
        remoteCheckFailed: false,
        remoteCheckError: null,
      }));
      return;
    }
    const kind = pickKind(o.updateStatus);
    // v0.2.83 (WP-A2 / D3 + N-4): the amber "couldn't check for updates" state
    // is ONLY meaningful when there is no real pending update to surface. If a
    // real `kind` is active (remote_ahead / install_stale / …), that takes
    // precedence and the amber state is suppressed — a stale failed check from
    // a prior poll must not paint amber over a genuine update badge.
    //
    // N-4: derive from the orchestrator store's EXPLICIT `lastCheckFailed`, NOT
    // from the live `updateStatus`. The old `!!us && us.remote_check_ok===false`
    // derivation rendered NOTHING when the check itself soft-failed to a null
    // `updateStatus` (the command errored) — the exact silent gap N-4 closes.
    // `lastCheckFailed` is `true` for BOTH a null-status completed check AND an
    // explicit `remote_check_ok===false`; `null` before the first completed
    // check (so no amber flash during startup); `false` on success.
    const us = o.updateStatus;
    const remoteCheckFailed = o.lastCheckFailed === true && kind === null;
    const remoteCheckError = remoteCheckFailed
      ? (us?.remote_check_error ?? null)
      : null;
    if (kind !== null) {
      // v0.2.16 (W4): the dismissal marker now keys on
      // `<kind>:<version-snapshot>` so dismissing one kind (e.g.
      // install_stale@0.2.15) doesn't suppress a later kind
      // (binary_stale@0.2.16). Version snapshot is the current
      // installed version; flipping kinds OR upgrading versions
      // re-shows the badge.
      const versionSnapshot = us
        ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
        : (o.version || '');
      const marker = `${kind}:${versionSnapshot}`;
      update((s) => ({
        ...s,
        available: true,
        kind,
        dismissed: s.lastSeenVersion === marker ? s.dismissed : false,
        // A real update takes precedence — never paint amber over it.
        remoteCheckFailed,
        remoteCheckError,
      }));
    } else {
      update((s) => ({
        ...s,
        available: false,
        kind: null,
        dismissed: false,
        remoteCheckFailed,
        remoteCheckError,
      }));
    }
  }

  return {
    subscribe,

    /** Pull update status from the orchestrator store. Re-shows the toast
     * if the underlying version changed since the last dismissal. */
    syncFromOrchestrator() {
      doSync();
    },

    /**
     * v0.2.83 (WP-A2 / D6): the ONE real update-check entry point behind
     * every manual "check for updates" surface — RightSidebar's "Check
     * Update" button (which used to be a setTimeout fake, A-RC5) and the
     * UpdateBadge amber-state "Retry now" button. Runs the actual backend
     * check and reports the outcome so the caller can render honest copy.
     *
     * Contract:
     *   - browser mode (no Tauri) ⇒ 'check_failed' (nothing to check);
     *   - sets `checking: true` for the duration (drives the button label);
     *   - awaits `orchestrator.checkStatus()` — after A-F3 this never throws,
     *     so we don't need a try/catch here; a failed backend probe surfaces
     *     as a null updateStatus or remote_check_ok===false, both handled;
     *   - reads the freshly-updated orchestrator store: a null updateStatus
     *     OR remote_check_ok===false ⇒ 'check_failed' (we couldn't determine
     *     remote state — never report 'up_to_date' in that case);
     *   - syncs our derived state; a real pending update (kind !== null) ⇒
     *     un-dismiss the badge so it re-shows even if previously dismissed,
     *     and report 'available';
     *   - otherwise ⇒ 'up_to_date'.
     *
     * A manual check also cancels any pending remote-check retry (D3
     * single-flight "cancel + replace") — the checkStatus() it runs will
     * re-arm the episode if the remote is still unreachable, or reset it on
     * success.
     */
    async manualCheck(): Promise<'available' | 'up_to_date' | 'check_failed'> {
      if (!tauriAvailable()) {
        // No backend to ask. Surface honestly; do NOT touch store state
        // beyond clearing any leftover `checking` flag (there won't be one,
        // but keep the invariant that checking is false when idle).
        update((s) => ({ ...s, checking: false }));
        return 'check_failed';
      }
      // Cancel + replace the scheduled retry: the checkStatus() below is the
      // fresh attempt, and it will re-schedule (fail) or reset (success).
      cancelScheduledRetry();
      update((s) => ({ ...s, checking: true }));
      try {
        await orchestrator.checkStatus();
      } finally {
        update((s) => ({ ...s, checking: false }));
      }
      const o = get(orchestrator);
      const us = o.updateStatus;
      // Couldn't determine remote state ⇒ honest 'check_failed'. A missing
      // remote_check_ok (older Rust) is treated as healthy — only an explicit
      // false, or a null status (command soft-failed), is a failure.
      if (us === null || us.remote_check_ok === false) {
        // Refresh derived state (paints the amber remote-check-failed badge
        // when appropriate) before reporting.
        doSync();
        return 'check_failed';
      }
      doSync();
      const kind = pickKind(us);
      if (kind !== null) {
        // Un-dismiss so a previously-dismissed badge re-shows on an explicit
        // user-initiated check (they asked; show them the answer).
        update((s) => ({ ...s, dismissed: false }));
        return 'available';
      }
      return 'up_to_date';
    },

    dismiss() {
      const o = get(orchestrator);
      const kind = pickKind(o.updateStatus);
      const us = o.updateStatus;
      const versionSnapshot = us
        ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
        : (o.version || '');
      const marker = `${kind ?? 'none'}:${versionSnapshot}`;
      // Fire-and-forget: the store mutation MUST stay synchronous
      // (UpdateBadge depends on it for derived state), and the
      // localStorage write is best-effort anyway.
      void saveSeen(marker);
      update((s) => ({
        ...s,
        dismissed: true,
        lastSeenVersion: marker,
      }));
    },

    /** Resolve `remote_ahead` — git pull + install.py --update. */
    async runUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null, nonFf: null }));
      try {
        await orchestrator.update_orchestrator();
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          kind: null,
          dismissed: false,
          nonFf: null,
        }));
        // Re-check to refresh the new install/binary state.
        await orchestrator.checkStatus();
      } catch (e) {
        // v0.2.23 (B4 / D19): detect divergence. The error string is
        // the raw Tauri Err payload; the orchestrator store wraps it as
        // an Error so we unwrap before parsing.
        const raw = e instanceof Error ? e.message : String(e);
        const nff = parseNonFfError(raw);
        if (nff) {
          // Surface the modal instead of a toast — the user has a real
          // choice to make (merge vs rebase vs cancel) and the raw
          // git stderr is unactionable.
          update((s) => ({
            ...s,
            updating: false,
            error: null,
            nonFf: nff,
          }));
        } else {
          update((s) => ({
            ...s,
            updating: false,
            error: raw,
            nonFf: null,
          }));
        }
      }
    },

    /**
     * v0.2.23 (B4 / D19): dismiss the divergence modal. Called by the
     * modal component's onClose after the user picks an action (or
     * cancels). Clearing `nonFf` removes the modal from the DOM.
     */
    dismissNonFf() {
      update((s) => ({ ...s, nonFf: null }));
    },

    /**
     * v0.2.16 (W4 / 0.5): resolve `install_stale` — install.py --update
     * only, no git pull. Source is already current; this just refreshes
     * `.claude/` and bumps state/install-manifest.json::version.
     */
    async applyPendingInstall(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null }));
      try {
        await orchestrator.apply_pending_install();
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          kind: null,
          dismissed: false,
        }));
        // Re-check so install_stale clears + any new flags surface.
        await orchestrator.checkStatus();
      } catch (e) {
        update((s) => ({
          ...s,
          updating: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /**
     * v0.2.51 (Bug A): resolve `merge_resolved_incomplete` — call the new
     * `resume_orchestrator_update` Tauri command, which verifies the
     * working tree is clean (no leftover conflict markers, no in-flight
     * merge state) and then re-enters the post-merge tail of
     * `update_orchestrator` (install.py --update + binary refresh +
     * auto-restart). The Rust side audit-logs `update_orchestrator_resumed`
     * for forensic clarity.
     *
     * On success the launcher auto-restarts mid-call; in practice we
     * rarely reach the success branch here. Errors surface as toast +
     * popover error string (the user can see e.g. "found unresolved
     * conflict markers in N files").
     */
    async resumeUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null, nonFf: null }));
      try {
        const o = get(orchestrator);
        await invoke('resume_orchestrator_update', { path: o.installPath });
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          kind: null,
          dismissed: false,
          nonFf: null,
        }));
        // Re-check so merge_resolved_incomplete clears + any newer flags
        // (binary_stale typically — the swap just landed) surface.
        await orchestrator.checkStatus();
      } catch (e) {
        const raw = e instanceof Error ? e.message : String(e);
        update((s) => ({
          ...s,
          updating: false,
          error: raw,
          nonFf: null,
        }));
      }
    },

    /**
     * v0.2.16 (W4 / 0.5): resolve `binary_stale` — re-exec the on-disk
     * launcher binary. The Rust `restart_launcher` command spawns the
     * new binary detached and exits the current process; the user sees
     * the launcher window blank for ~1s then come back at the new
     * version.
     */
    async runRestart(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null }));
      try {
        const o = get(orchestrator);
        await invoke('restart_launcher', { installRoot: o.installPath });
        // Note: in practice we never reach here — restart_launcher
        // exits the process. Kept defensively in case of failures
        // (e.g. binary missing) so the spinner clears.
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          kind: null,
          dismissed: false,
        }));
      } catch (e) {
        update((s) => ({
          ...s,
          updating: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const updater = createUpdaterStore();
