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
 *
 * v0.2.83 (WP-A2, A-F3 / A-RC4): also swallows a *rejected* Tauri command.
 * Before this, `safeInvoke` only guarded browser mode — a real command Err
 * (e.g. a mid-`checkStatus()` `check_for_updates` failure) rejected all the
 * way up into `void orchestrator.checkStatus()` and silently killed the rest
 * of the chain, so the store update at the tail never ran and the update
 * badge went dark with no breadcrumb. Now a rejection is caught, logged to
 * the console (a breadcrumb the developer/user can reach), and mapped to
 * `null` — the same shape browser mode returns — so every caller's existing
 * null-guard degrades that one signal instead of aborting the whole poll.
 * The strict `invoke()` below is intentionally left throwing for callers
 * (buttons, mutations) that want to surface the error.
 */
export async function safeInvoke<T>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  if (!isTauri) return null;
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke<T>(cmd, args);
  } catch (err) {
    // Breadcrumb only — never rethrow. A soft read that fails must look the
    // same to the caller as "not available" (null), not blow up the chain.
    console.error(cmd, err);
    return null;
  }
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
