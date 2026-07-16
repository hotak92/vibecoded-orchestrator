// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.83 (WP-A2 / A-F3, A-RC4): tests for `safeInvoke`'s new catch behavior.
//
// Before this cycle `safeInvoke` only guarded browser mode — a real Tauri
// command Err REJECTED, propagating into the fire-and-forget
// `void orchestrator.checkStatus()` and silently killing the rest of the
// poll (A-RC4). The fix: catch the rejection, log a breadcrumb via
// `console.error(cmd, err)`, and resolve `null` (the same shape browser mode
// returns) so every caller's existing null-guard degrades ONE signal instead
// of aborting the whole chain.
//
// `safeInvoke` computes `isTauri` ONCE at module load from
// `'__TAURI_INTERNALS__' in window`, and dynamically imports
// '@tauri-apps/api/core' on each call. So the test must (1) provide a
// `window` with `__TAURI_INTERNALS__` set BEFORE importing `$lib/tauri`, and
// (2) mock the dynamic import so `invoke` rejects. We use `vi.resetModules` +
// dynamic `import()` to control load ordering per test.

import { afterEach, describe, expect, it, vi } from 'vitest';

// A controllable fake `invoke` the mocked module delegates to. Reassigned
// per test so a single module mock can serve both the reject and resolve
// cases.
let fakeInvoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (cmd: string, args?: Record<string, unknown>) => fakeInvoke(cmd, args),
}));

afterEach(() => {
  vi.restoreAllMocks();
  vi.resetModules();
  // Clean the global back to browser-mode default for the next test.
  delete (globalThis as { window?: unknown }).window;
});

/** Install a `window` with (or without) the Tauri sentinel, then import a
 *  FRESH copy of `$lib/tauri` (module-load recomputes `isTauri`). */
async function loadTauri(withTauriRuntime: boolean) {
  vi.resetModules();
  if (withTauriRuntime) {
    (globalThis as { window?: unknown }).window = { __TAURI_INTERNALS__: {} };
  } else {
    (globalThis as { window?: unknown }).window = {};
  }
  return import('./tauri');
}

describe('safeInvoke — rejected Tauri command (A-F3)', () => {
  it('resolves null and logs console.error instead of throwing', async () => {
    const boom = new Error('command failed: rev-list spawn error');
    fakeInvoke = () => Promise.reject(boom);
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { safeInvoke } = await loadTauri(true);

    // Must NOT throw — the whole point of the fix.
    const result = await safeInvoke<string>('check_for_updates', { path: '/x' });

    expect(result).toBeNull();
    // Breadcrumb: cmd + the raw error, so a developer can see WHAT failed.
    expect(errSpy).toHaveBeenCalledTimes(1);
    expect(errSpy).toHaveBeenCalledWith('check_for_updates', boom);
  });

  it('does not reject even when the command rejects with a non-Error value', async () => {
    fakeInvoke = () => Promise.reject('plain string rejection');
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { safeInvoke } = await loadTauri(true);

    await expect(
      safeInvoke('some_cmd'),
    ).resolves.toBeNull();
    expect(errSpy).toHaveBeenCalledWith('some_cmd', 'plain string rejection');
  });

  it('passes through the resolved value on success (no console.error)', async () => {
    fakeInvoke = () => Promise.resolve({ ok: true });
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { safeInvoke } = await loadTauri(true);

    const result = await safeInvoke<{ ok: boolean }>('detect_system');
    expect(result).toEqual({ ok: true });
    expect(errSpy).not.toHaveBeenCalled();
  });

  it('returns null in browser mode without touching the tauri import', async () => {
    // Would throw if the dynamic import were reached — assert it is NOT.
    fakeInvoke = () => {
      throw new Error('invoke should never be called in browser mode');
    };
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { safeInvoke } = await loadTauri(false);

    const result = await safeInvoke('anything');
    expect(result).toBeNull();
    // Browser-mode short-circuit is not an error — no breadcrumb.
    expect(errSpy).not.toHaveBeenCalled();
  });
});

describe('invoke (strict) — unchanged: still throws', () => {
  it('rejects in browser mode', async () => {
    const { invoke } = await loadTauri(false);
    await expect(invoke('x')).rejects.toThrow(/Tauri not available/);
  });

  it('propagates a command rejection (does NOT swallow like safeInvoke)', async () => {
    fakeInvoke = () => Promise.reject(new Error('strict boom'));
    const { invoke } = await loadTauri(true);
    await expect(invoke('x')).rejects.toThrow(/strict boom/);
  });
});
