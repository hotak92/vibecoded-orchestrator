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
  source_version: string;
  installed_version: string;
  running_version: string;
  on_disk_binary_version: string;
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
          ? (updateStatus.remote_ahead || updateStatus.install_stale || updateStatus.binary_stale)
          : false;
        update((s) => ({
          ...s,
          status: 'installed',
          version: version ?? s.version,
          updateAvailable,
          updateStatus: updateStatus ?? null,
          installPath: currentPath,
        }));
      } else {
        update((s) => ({ ...s, status: 'not_installed', installPath: currentPath, updateStatus: null }));
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
