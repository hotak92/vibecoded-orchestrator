// Tiny shared wrapper around the Tauri IPC bridge.
//
// Stores import from here instead of '@tauri-apps/api/core' directly so:
//   1. We can run `vite dev` in the browser without a hard crash on import.
//   2. We have one place to add cross-cutting concerns later (telemetry,
//      error mapping). Today there are none.

const isTauri =
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

export function tauriAvailable(): boolean {
  return isTauri;
}

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

export async function listen<T>(
  event: string,
  handler: (e: { payload: T }) => void,
): Promise<() => void> {
  if (!isTauri) return () => {};
  const { listen } = await import('@tauri-apps/api/event');
  const unlisten = await listen<T>(event, handler);
  return unlisten;
}
