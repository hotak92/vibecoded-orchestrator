import { writable, derived } from 'svelte/store';
import { invoke as tauriInvoke, safeInvoke, listen as tauriListenWrapper } from '$lib/tauri';

async function tauriListen<T>(event: string, handler: (e: { payload: T }) => void) {
  await tauriListenWrapper<T>(event, handler);
}

// ---------------------------------------------------------------------------
// Types (mirror Rust types)
// ---------------------------------------------------------------------------

export interface SystemDetection {
  os: string;
  arch: string;
  has_nvidia_gpu: boolean;
  gpu_name: string;
  has_apple_silicon: boolean;
  has_docker: boolean;
  has_podman: boolean;
  has_python: boolean;
  python_version: string;
  python_cmd: string;
  has_claude_cli: boolean;
  has_git: boolean;
  has_node: boolean;
}

export interface InstallConfig {
  install_path: string;
  use_gpu: boolean;
  cpu_only: boolean;
  openai_key: string | null;
  container_runtime: string | null;
  skip_containers: boolean;
}

export interface InstallProgress {
  stage: string;
  message: string;
  percentage: number;
  error: string | null;
}

export interface InstallResult {
  success: boolean;
  install_path: string;
  message: string;
  system: SystemDetection;
}

/**
 * v0.2.16 (W4 / 0.5): three-state update status surfaced by the
 * `UpdateBadge` banner. Mirror of Rust `commands::installer::UpdateStatus`.
 *
 * The banner renders the highest-priority state when more than one
 * flag is true (priority: binary_stale > install_stale > remote_ahead).
 *
 * Each flag has a distinct resolver:
 * - remote_ahead   → invoke('update_orchestrator')  // git pull + install
 * - install_stale  → invoke('apply_pending_install') // install.py only
 * - binary_stale   → invoke('restart_launcher')      // re-exec dist binary
 */
export interface UpdateStatus {
  remote_ahead: boolean;
  install_stale: boolean;
  binary_stale: boolean;
  /** v0.2.51 (Bug A): a prior `update_orchestrator` / `merge_*` / `rebase_*`
   *  surfaced a conflict modal and the user resolved the conflict outside
   *  the launcher (CLI `git add` + `git commit`) without re-entering the
   *  install flow. Detected via a sentinel file at
   *  `.claude/state/orchestrator-update-resume-needed.json` AND absence of
   *  `.git/MERGE_HEAD`. Highest-priority kind in the UpdateBadge — leaving
   *  this unattended ships a stale install manifest, hook bundle, and
   *  possibly a stale launcher binary.
   *  Optional for backwards-compat with pre-v0.2.51 Rust returns. */
  merge_resolved_incomplete?: boolean;
  /** v0.2.51: which operation hit the conflict. One of `"merge"`,
   *  `"rebase"`, or empty string when no resume is pending. */
  resume_operation?: string;
  /** v0.2.51: branch the conflict happened on (typically `main`). */
  resume_branch?: string;
  source_version: string;
  installed_version: string;
  running_version: string;
  on_disk_binary_version: string;
  /** v0.2.83 (WP-A2 / D2): honest remote-check health. Mirror of the two
   *  fields WP-A1 added to Rust `installer::UpdateStatus`.
   *
   *  `remote_check_ok === false` means the `git fetch` / `rev-list` probe
   *  could NOT determine whether the remote is ahead — the signal is
   *  UNKNOWN, NOT "up to date". `remote_check_error` carries a concise
   *  stage-label / last-stderr-line for the popover copy. On success (or in
   *  the non-git / not-applicable case) Rust returns `remote_check_ok=true`
   *  with `remote_check_error=null`.
   *
   *  Both are OPTIONAL for back-compat: a launcher running against a
   *  pre-v0.2.83 Rust binary returns neither field. Readers MUST treat a
   *  MISSING `remote_check_ok` as `true` (healthy) — an absent field is the
   *  old "no health surface" world, where the check either worked or
   *  soft-failed to `remote_ahead=false`; scheduling a retry there would be
   *  a pointless storm. Only an explicit `=== false` is a failed check. */
  remote_check_ok?: boolean;
  remote_check_error?: string | null;
}

type OrchestratorStatus = 'unknown' | 'not_installed' | 'installed' | 'installing' | 'updating' | 'error';

interface OrchestratorState {
  status: OrchestratorStatus;
  installPath: string;
  version: string;
  /** Derived: any of the three update signals is true. Kept for backward
   *  compat with consumers that just want a bool. New code should read
   *  `updateStatus` directly to get the per-signal breakdown. */
  updateAvailable: boolean;
  /** v0.2.16 (W4 / 0.5): full three-state update status. Null until
   *  the first `checkStatus()` resolves. */
  updateStatus: UpdateStatus | null;
  system: SystemDetection | null;
  progress: InstallProgress | null;
  error: string | null;
}

// ---------------------------------------------------------------------------
// Remote-check retry scheduling (v0.2.83, WP-A2 / D3)
// ---------------------------------------------------------------------------
//
// When a `checkStatus()` lands `remote_check_ok === false`, the remote signal
// is UNKNOWN — not "up to date". Rather than wait up to an hour for the next
// poll (A-RC2), the store schedules a short burst of retries.
//
// Policy (D3):
//   - delays 30s → 90s → 300s, capped at 3 retries per failure episode;
//   - single-flight: at most ONE pending timer. Scheduling again while a
//     timer is armed is a no-op (no stacking); a manual check cancels +
//     replaces the pending timer;
//   - an episode RESETS (retry counter → 0, pending timer cleared) the moment
//     a check lands `remote_check_ok !== false` (ok, or MISSING = older Rust
//     back-compat = treated as ok). Only an explicit `=== false` keeps the
//     episode alive.
//
// The timer lives at module scope (not in the store value) so it survives
// store subscription churn and so `cancelScheduledRetry()` — exported for
// tests and for `manualCheck()`'s "cancel + replace" contract — can reach it.

const RETRY_DELAYS_MS = [30_000, 90_000, 300_000] as const;
const MAX_RETRIES = RETRY_DELAYS_MS.length; // cap 3 per episode

let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retryAttempt = 0; // 0-based index into RETRY_DELAYS_MS for the NEXT retry

/** Cancel any pending remote-check retry and reset the episode counter.
 *  Idempotent. Exported for `manualCheck()` (cancel + replace) and tests. */
export function cancelScheduledRetry(): void {
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  retryAttempt = 0;
}

/** Arm the next remote-check retry if the episode has budget left. Honors
 *  single-flight: does nothing when a timer is already pending. Called by
 *  `checkStatus()` after a failed remote check. */
function scheduleRemoteCheckRetry(): void {
  if (retryTimer !== null) return; // single-flight: never stack timers
  if (retryAttempt >= MAX_RETRIES) return; // episode budget exhausted
  const delay = RETRY_DELAYS_MS[retryAttempt];
  retryAttempt += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    // The retry itself re-runs the full status check. If it fails again,
    // checkStatus() re-arms the next tier (up to the cap); if it succeeds,
    // checkStatus() resets the episode.
    void orchestrator.checkStatus();
  }, delay);
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

function createOrchestratorStore() {
  const { subscribe, set, update } = writable<OrchestratorState>({
    status: 'unknown',
    installPath: '',
    version: '',
    updateAvailable: false,
    updateStatus: null,
    system: null,
    progress: null,
    error: null,
  });

  // Listen for install_progress events from Rust backend
  tauriListen<InstallProgress>('install_progress', (event) => {
    update((s) => ({ ...s, progress: event.payload }));
  });

  return {
    subscribe,

    /** Detect system capabilities — soft-no-op in browser mode */
    async detectSystem(): Promise<SystemDetection | null> {
      const system = await safeInvoke<SystemDetection>('detect_system');
      const defaultPath = await safeInvoke<string>('get_default_install_path');

      if (!system) return null;

      update((s) => ({ ...s, system, installPath: s.installPath || defaultPath || '' }));
      return system;
    },

    /** Check if orchestrator is installed and get version — soft-no-op in browser mode.
     *
     * Bug A (v0.2.5): probes `get_known_install_path` FIRST. That command
     * walks app_state + the launcher binary's ancestor tree looking for a
     * real install — so a user who installed VCO at, say,
     * `/home/x/code/orch/` is no longer told "Not installed"
     * because the hard-coded `$HOME/vibecoded-orchestrator` happens to be
     * empty. Falls back to `get_default_install_path` (used as a SUGGESTION
     * for the install wizard pre-fill, NOT as the probed location) when no
     * existing install is discoverable.
     */
    async checkStatus(): Promise<void> {
      // v0.2.83 (WP-A2 / A-F3): belt-and-braces try/catch. safeInvoke already
      // maps a rejected command to null (A-RC4 fix), so each step below is
      // individually null-guarded — but wrapping the whole body means that
      // even an UNEXPECTED throw (a bug in one of these steps, a non-invoke
      // exception) degrades this ONE poll instead of rejecting into the
      // fire-and-forget caller (`void orchestrator.checkStatus()` at mount /
      // hourly poll / retry timer) and silently killing the store update.
      // One failed step degrades one signal only; the store is never left in
      // a half-updated inconsistent state by an escaping exception.
      try {
        // 1. Known-install discovery. Returns a real path (Some) or null when
        //    no install is detected. Tauri-less environments (browser mode)
        //    return null from safeInvoke — treat the same as "no install".
        const knownPath = await safeInvoke<string | null>('get_known_install_path');

        // 2. Default-path probe — used both as the wizard pre-fill and as a
        //    Tauri-availability gate. Browser mode short-circuits here.
        const defaultPath = await safeInvoke<string>('get_default_install_path');
        if (defaultPath === null) return;

        let currentPath = knownPath ?? '';

        if (!currentPath) {
          // No discoverable install — fall back to the stored installPath
          // (if user already picked one in the wizard) or the OS-aware
          // default. This matches the old behavior for fresh-install users.
          let stored = '';
          const unsub = subscribe((s) => { stored = s.installPath; });
          unsub();
          currentPath = stored || defaultPath;
        }

        // Push the resolved path through the store so the wizard pre-fills
        // correctly (Bug A's UX requirement: the install_path must flow from
        // discovery → store → wizard input).
        update((s) => ({ ...s, installPath: currentPath }));

        const installed = await safeInvoke<boolean>('check_install_status', { path: currentPath });
        if (installed === null) return;

        if (installed) {
          const version = await safeInvoke<string>('get_installed_version', { path: currentPath });
          // v0.2.16 (W4 / 0.5): check_for_updates now returns the full
          // UpdateStatus struct. Keep the legacy boolean as a derived
          // any-of-three signal for old consumers that only care about
          // "is there something to do?".
          const updateStatus = await safeInvoke<UpdateStatus>('check_for_updates', { path: currentPath });
          const updateAvailable = updateStatus
            ? (updateStatus.remote_ahead
                || updateStatus.install_stale
                || updateStatus.binary_stale
                || !!updateStatus.merge_resolved_incomplete)
            : false;
          update((s) => ({
            ...s,
            status: 'installed',
            version: version ?? s.version,
            updateAvailable,
            updateStatus: updateStatus ?? null,
            installPath: currentPath,
          }));

          // v0.2.83 (WP-A2 / D3): remote-check retry episode management.
          // Treat a MISSING remote_check_ok as healthy (older Rust
          // back-compat) — only an EXPLICIT `=== false` is a failed check.
          // A null updateStatus (the command itself soft-failed to null via
          // safeInvoke) is ALSO a failed check: we couldn't determine remote
          // state, so retry rather than pretend "up to date".
          const remoteCheckFailed =
            updateStatus === null || updateStatus.remote_check_ok === false;
          if (remoteCheckFailed) {
            scheduleRemoteCheckRetry();
          } else {
            // Successful (or not-applicable) remote check ⇒ end the episode.
            cancelScheduledRetry();
          }
        } else {
          update((s) => ({ ...s, status: 'not_installed', installPath: currentPath, updateStatus: null }));
          // Not installed ⇒ there is no remote to check; end any episode.
          cancelScheduledRetry();
        }
      } catch (err) {
        // An unexpected throw slipped past the per-step null guards. Log a
        // breadcrumb and leave the store as-is; the next poll (or a manual
        // check) retries. We deliberately do NOT schedule a remote-check
        // retry here — this path is an internal error, not a "remote is
        // unknown" health signal, so it must not masquerade as one.
        console.error('checkStatus', err);
      }
    },

    /** Set install path */
    setInstallPath(path: string) {
      update((s) => ({ ...s, installPath: path }));
    },

    /** Install orchestrator */
    async install(config: InstallConfig): Promise<InstallResult> {
      update((s) => ({ ...s, status: 'installing', error: null, progress: null }));

      try {
        const result = await tauriInvoke<InstallResult>('install_orchestrator', { config });
        update((s) => ({
          ...s,
          status: 'installed',
          installPath: result.install_path,
          system: result.system,
          error: null,
        }));
        return result;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        update((s) => ({ ...s, status: 'error', error: message }));
        throw err;
      }
    },

    /** Update orchestrator */
    async update_orchestrator(): Promise<InstallResult> {
      let currentPath = '';
      const unsub = subscribe((s) => { currentPath = s.installPath; });
      unsub();

      update((s) => ({ ...s, status: 'updating', error: null, progress: null }));

      try {
        const result = await tauriInvoke<InstallResult>('update_orchestrator', { path: currentPath });
        update((s) => ({
          ...s,
          status: 'installed',
          updateAvailable: false,
          error: null,
        }));
        return result;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        update((s) => ({ ...s, status: 'installed', error: message }));
        throw err;
      }
    },

    /**
     * v0.2.16 (W4 / 0.5): apply a pending install (install.py --update)
     * WITHOUT a preceding git pull. Resolves the install_stale banner
     * state. The source tree is already current — this just refreshes
     * `.claude/`, MCP registrations, and bumps
     * `state/install-manifest.json::version`.
     *
     * Distinct from `update_orchestrator` to avoid wasting ~30s pulling
     * an already-current tree (and to avoid noising the launcher's
     * git output for no value).
     */
    async apply_pending_install(): Promise<InstallResult> {
      let currentPath = '';
      const unsub = subscribe((s) => { currentPath = s.installPath; });
      unsub();

      update((s) => ({ ...s, status: 'updating', error: null, progress: null }));

      try {
        const result = await tauriInvoke<InstallResult>('apply_pending_install', { path: currentPath });
        update((s) => ({
          ...s,
          status: 'installed',
          error: null,
        }));
        return result;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        update((s) => ({ ...s, status: 'installed', error: message }));
        throw err;
      }
    },

    /** Clear error */
    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const orchestrator = createOrchestratorStore();

/** Derived: is the orchestrator in a working state? */
export const isOrchestratorReady = derived(orchestrator, ($o) => $o.status === 'installed');

/** Derived: is an operation in progress? */
export const isOrchestratorBusy = derived(
  orchestrator,
  ($o) => $o.status === 'installing' || $o.status === 'updating'
);
