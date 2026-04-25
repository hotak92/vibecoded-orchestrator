import { writable, derived } from 'svelte/store';

// Guard Tauri API imports — they fail in browser-only context (vite dev without native window)
const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri) throw new Error(`Tauri not available (browser mode). Cannot invoke '${cmd}'`);
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(cmd, args);
}

async function tauriListen<T>(event: string, handler: (e: { payload: T }) => void) {
  if (!isTauri) return;
  const { listen } = await import('@tauri-apps/api/event');
  listen<T>(event, handler);
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

type OrchestratorStatus = 'unknown' | 'not_installed' | 'installed' | 'installing' | 'updating' | 'error';

interface OrchestratorState {
  status: OrchestratorStatus;
  installPath: string;
  version: string;
  updateAvailable: boolean;
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

    /** Detect system capabilities */
    async detectSystem(): Promise<SystemDetection> {
      const system = await tauriInvoke<SystemDetection>('detect_system');
      const defaultPath = await tauriInvoke<string>('get_default_install_path');

      update((s) => ({ ...s, system, installPath: s.installPath || defaultPath }));
      return system;
    },

    /** Check if orchestrator is installed and get version */
    async checkStatus(): Promise<void> {
      const defaultPath = await tauriInvoke<string>('get_default_install_path');

      update((s) => {
        if (!s.installPath) return { ...s, installPath: defaultPath };
        return s;
      });

      let currentPath = '';
      const unsub = subscribe((s) => { currentPath = s.installPath; });
      unsub();

      if (!currentPath) currentPath = defaultPath;

      const installed = await tauriInvoke<boolean>('check_install_status', { path: currentPath });

      if (installed) {
        try {
          const version = await tauriInvoke<string>('get_installed_version', { path: currentPath });
          const updateAvailable = await tauriInvoke<boolean>('check_for_updates', { path: currentPath });
          update((s) => ({
            ...s,
            status: 'installed',
            version,
            updateAvailable,
            installPath: currentPath,
          }));
        } catch {
          update((s) => ({ ...s, status: 'installed', installPath: currentPath }));
        }
      } else {
        update((s) => ({ ...s, status: 'not_installed', installPath: currentPath }));
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
