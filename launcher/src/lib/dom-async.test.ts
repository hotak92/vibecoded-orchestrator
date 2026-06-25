import { describe, it, expect, vi, afterEach } from 'vitest';
import { nextFrame } from './dom-async';

describe('nextFrame', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('resolves after two animation frames when rAF is available', async () => {
    const calls: Array<FrameRequestCallback> = [];
    // Stub requestAnimationFrame to capture the scheduled callbacks and drive
    // them synchronously, so we can assert the double-rAF chain deterministically.
    const raf = vi.fn((cb: FrameRequestCallback) => {
      calls.push(cb);
      return calls.length;
    });
    vi.stubGlobal('requestAnimationFrame', raf);

    let resolved = false;
    const p = nextFrame().then(() => {
      resolved = true;
    });

    // After scheduling, exactly one rAF is queued and the promise is pending.
    expect(raf).toHaveBeenCalledTimes(1);
    expect(resolved).toBe(false);

    // Fire the outer frame → it schedules the inner frame; still pending.
    calls[0](0);
    await Promise.resolve();
    expect(raf).toHaveBeenCalledTimes(2);
    expect(resolved).toBe(false);

    // Fire the inner frame → resolves.
    calls[1](0);
    await p;
    expect(resolved).toBe(true);
  });

  it('falls back to a macrotask when requestAnimationFrame is absent', async () => {
    vi.stubGlobal('requestAnimationFrame', undefined);
    // Must still resolve (never hang) in a non-DOM environment.
    await expect(nextFrame()).resolves.toBeUndefined();
  });
});
