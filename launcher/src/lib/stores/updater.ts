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
import { orchestrator } from './orchestrator';

const SEEN_KEY = 'vct.update.seen_version';

/** v0.2.16 (W4 / 0.5): which of the three update signals to render.
 *  Priority order: 'binary_stale' > 'install_stale' > 'remote_ahead'.
 *  `null` when no signal is true. */
export type UpdateKind = 'binary_stale' | 'install_stale' | 'remote_ahead' | null;

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
}

function loadSeen(): string | null {
  if (typeof localStorage === 'undefined') return null;
  return localStorage.getItem(SEEN_KEY);
}

function saveSeen(v: string | null) {
  if (typeof localStorage === 'undefined') return;
  if (v) localStorage.setItem(SEEN_KEY, v);
  else localStorage.removeItem(SEEN_KEY);
}

function pickKind(status: { remote_ahead: boolean; install_stale: boolean; binary_stale: boolean } | null): UpdateKind {
  if (!status) return null;
  // Priority order: binary > install > remote.
  // binary_stale wins because restart is fastest + a newer binary can
  // change every other code path; install_stale next (without an
  // install.py pass, `.claude/` config drifts); remote_ahead last (the
  // most "fully behind" but lowest urgency).
  if (status.binary_stale) return 'binary_stale';
  if (status.install_stale) return 'install_stale';
  if (status.remote_ahead) return 'remote_ahead';
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
  });

  return {
    subscribe,

    /** Pull update status from the orchestrator store. Re-shows the toast
     * if the underlying version changed since the last dismissal. */
    syncFromOrchestrator() {
      const o = get(orchestrator);
      const installed = o.status === 'installed' || o.status === 'updating';
      if (!installed) {
        update((s) => ({ ...s, available: false, kind: null }));
        return;
      }
      const kind = pickKind(o.updateStatus);
      if (kind !== null) {
        // v0.2.16 (W4): the dismissal marker now keys on
        // `<kind>:<version-snapshot>` so dismissing one kind (e.g.
        // install_stale@0.2.15) doesn't suppress a later kind
        // (binary_stale@0.2.16). Version snapshot is the current
        // installed version; flipping kinds OR upgrading versions
        // re-shows the badge.
        const us = o.updateStatus;
        const versionSnapshot = us
          ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
          : (o.version || '');
        const marker = `${kind}:${versionSnapshot}`;
        update((s) => ({
          ...s,
          available: true,
          kind,
          dismissed: s.lastSeenVersion === marker ? s.dismissed : false,
        }));
      } else {
        update((s) => ({ ...s, available: false, kind: null, dismissed: false }));
      }
    },

    dismiss() {
      const o = get(orchestrator);
      const kind = pickKind(o.updateStatus);
      const us = o.updateStatus;
      const versionSnapshot = us
        ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
        : (o.version || '');
      const marker = `${kind ?? 'none'}:${versionSnapshot}`;
      saveSeen(marker);
      update((s) => ({
        ...s,
        dismissed: true,
        lastSeenVersion: marker,
      }));
    },

    /** Resolve `remote_ahead` — git pull + install.py --update. */
    async runUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null }));
      try {
        await orchestrator.update_orchestrator();
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          kind: null,
          dismissed: false,
        }));
        // Re-check to refresh the new install/binary state.
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
