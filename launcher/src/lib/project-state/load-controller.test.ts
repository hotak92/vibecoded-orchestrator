// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import { createLoader, errorMessage, type LoadStatus } from './load-controller';

/** A fake invoke: scripts one result per call. */
function fakeInvoke<T>(script: Array<T | Error>): () => Promise<T> {
  let call = 0;
  return () => {
    const step = script[Math.min(call, script.length - 1)]!;
    call++;
    return step instanceof Error ? Promise.reject(step) : Promise.resolve(step);
  };
}

/** Every status the controller emits, in order. */
function recorded<T>(loader: ReturnType<typeof createLoader<T>>): LoadStatus<T>[] {
  const seen: LoadStatus<T>[] = [];
  loader.subscribe(() => seen.push({ ...loader.get() }));
  return seen;
}

describe('load controller (review R8 m1)', () => {
  it('maps an Error rejection to its message', () => {
    expect(errorMessage(new Error('db locked'))).toBe('db locked');
    expect(errorMessage('plain string')).toBe('plain string');
  });

  it('reject → error surfaced, value null, onError called', async () => {
    const errors: unknown[] = [];
    const loader = createLoader(
      fakeInvoke([new Error('project not found')]),
      (e) => errors.push(e),
    );
    const seen = recorded(loader);
    await loader.retry();
    const last = loader.get();
    expect(last.value).toBeNull();
    expect(last.error).toBe('project not found');
    expect(errors).toHaveLength(1);
    // Status sequence: error cleared (loading again), then the failure.
    expect(seen.map((s) => s.error)).toEqual([null, null, 'project not found']);
  });

  it('retry after a failure → success clears the error and sets the value', async () => {
    let fail = true;
    const loader = createLoader(() =>
      fail ? Promise.reject(new Error('boom')) : Promise.resolve({ id: 'p1' }),
    );
    await loader.retry();
    expect(loader.get().error).toBe('boom');
    fail = false;
    await loader.retry();
    const last = loader.get();
    expect(last.error).toBeNull();
    expect(last.value).toEqual({ id: 'p1' });
  });

  it('a reload after success KEEPS the previous value while the new one is pending (no flash, never cut)', async () => {
    let resolveSecond!: (v: { id: string }) => void;
    let call = 0;
    const loader = createLoader(() => {
      call++;
      if (call === 1) return Promise.resolve({ id: 'first' });
      return new Promise((res) => (resolveSecond = res));
    });
    await loader.retry();
    expect(loader.get().value).toEqual({ id: 'first' });
    const pending = loader.retry(); // slow second load
    expect(loader.get().value).toEqual({ id: 'first' }); // still the old row
    expect(loader.get().error).toBeNull();
    resolveSecond({ id: 'second' });
    await pending;
    expect(loader.get().value).toEqual({ id: 'second' });
  });

  it('subscribe fires once immediately (catch-up) and on every change; unsubscribes cleanly', async () => {
    const loader = createLoader(fakeInvoke(['v']));
    let fires = 0;
    const off = loader.subscribe(() => fires++);
    expect(fires).toBe(1); // catch-up
    await loader.retry();
    expect(fires).toBe(3); // + retry start, + settle
    off();
    await loader.retry();
    expect(fires).toBe(3); // no more after unsubscribe
  });
});
