// Tiny shared wrapper around the Tauri IPC bridge.
//
// Stores import from here instead of '@tauri-apps/api/core' directly so:
//   1. We can run `vite dev` in the browser without a hard crash on import.
//   2. We have one place to add cross-cutting concerns (telemetry, error
//      mapping, browser-mode no-ops).

const isTauri =
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

export function tauriAvailable(): boolean {
  return isTauri;
}

/** Alias used by app-level code that wants a more explicit name. */
export function isTauriRuntime(): boolean {
  return isTauri;
}

/**
 * Strict invoke — throws if Tauri is not available.
 *
 * Use when the caller wants to surface a clear error (e.g. a button that
 * obviously requires Tauri).
 */
export async function invoke<T>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!isTauri) {
    throw new Error(`Tauri not available (browser mode); cannot invoke '${cmd}'`);
  }
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(cmd, args);
}

/**
 * Soft invoke — returns `null` in browser mode instead of throwing.
 *
 * Use for non-critical reads (e.g. status polls, list endpoints) where
 * silent no-op is preferable to a toast.
 */
export async function safeInvoke<T>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  if (!isTauri) return null;
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(cmd, args);
}

export async function listen<T>(
  event: string,
  handler: (e: { payload: T }) => void,
): Promise<() => void> {
  if (!isTauri) return () => {};
  const { listen } = await import('@tauri-apps/api/event');
  const unlisten = await listen<T>(event, handler);
  return unlisten;
}
