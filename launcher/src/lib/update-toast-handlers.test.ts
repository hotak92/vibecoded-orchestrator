// SPDX-License-Identifier: AGPL-3.0-or-later
//
// V52-AH-FE (v0.2.53 Track E) — unit tests for the pure-function
// handlers behind `UpdateToast.svelte`.
//
// The Svelte component itself is a thin shell that subscribes to the
// Tauri events `vct-update-recovered` / `vct-update-failed` and
// delegates to these handlers. Testing the handlers in isolation
// gives us confidence in the message composition + dedup-key contract
// without needing a DOM (the launcher's vitest config is pure-Node by
// design — see `vitest.config.ts` for the rationale).
//
// Coverage:
//   * `buildRecoveredMessage` — version present, version empty, version
//     with leading 'v', whitespace handling.
//   * `buildFailedMessage` — both fields present, both fields null,
//     reason-only null, lock_path-only null.
//   * `handleRecovered` — verifies the dedup key and message routing.
//   * `handleFailed` — verifies the dedup key and the error-channel
//     routing (so the bell inbox receives the entry).
//   * `handleReport` (v0.2.54 Track C FE C-1) — the single router both
//     delivery paths (pull-on-mount + events) go through: routing,
//     empty-payload no-op, and the boolean dedup contract.

import { describe, expect, it, vi } from 'vitest';
import {
  buildRecoveredMessage,
  buildFailedMessage,
  handleRecovered,
  handleFailed,
  handleReport,
  type UpdateRecoveryPayload,
  type ToastSurface,
} from './update-toast-handlers';

function makeStubToast(): ToastSurface & {
  successCalls: Array<{ msg: string; opts?: { key?: string } }>;
  errorCalls: Array<{ msg: string; opts?: { key?: string } }>;
} {
  const successCalls: Array<{ msg: string; opts?: { key?: string } }> = [];
  const errorCalls: Array<{ msg: string; opts?: { key?: string } }> = [];
  return {
    successCalls,
    errorCalls,
    success: (msg, opts) => {
      successCalls.push({ msg, opts });
    },
    error: (msg, opts) => {
      errorCalls.push({ msg, opts });
    },
  };
}

describe('update-toast-handlers / buildRecoveredMessage', () => {
  it('returns a versioned message when a clean version is given', () => {
    expect(buildRecoveredMessage('0.2.53')).toBe('Launcher updated to v0.2.53.');
  });

  it('strips a leading "v" to avoid double-prefixing', () => {
    expect(buildRecoveredMessage('v0.2.53')).toBe('Launcher updated to v0.2.53.');
  });

  it('falls back to the generic phrase when the version is empty', () => {
    expect(buildRecoveredMessage('')).toBe('Launcher update completed.');
  });

  it('falls back to the generic phrase when the version is whitespace-only', () => {
    expect(buildRecoveredMessage('   ')).toBe('Launcher update completed.');
  });
});

describe('update-toast-handlers / buildFailedMessage', () => {
  it('includes both reason and lock path when both are present', () => {
    const msg = buildFailedMessage(
      'lock_file_too_old_or_undated',
      '/home/user/.vct/update.lock.json',
    );
    expect(msg).toContain('lock_file_too_old_or_undated');
    expect(msg).toContain('/home/user/.vct/update.lock.json');
    expect(msg).toContain('update.log');
  });

  it('substitutes "unknown" when reason is null', () => {
    const msg = buildFailedMessage(null, '/path/to/lock');
    expect(msg).toContain('(unknown)');
    expect(msg).toContain('/path/to/lock');
  });

  it('substitutes "(unknown path)" when lock_path is null', () => {
    const msg = buildFailedMessage('parse failed: ...', null);
    expect(msg).toContain('parse failed: ...');
    expect(msg).toContain('(unknown path)');
  });

  it('handles both fields being null', () => {
    const msg = buildFailedMessage(null, null);
    expect(msg).toContain('(unknown)');
    expect(msg).toContain('(unknown path)');
  });
});

describe('update-toast-handlers / handleRecovered', () => {
  it('routes through toast.success with the recovered dedup key', () => {
    const stub = makeStubToast();
    const payload: UpdateRecoveryPayload = {
      recovered: true,
      stale_or_invalid: false,
      lock_path: '/home/user/.vct/update.lock.json',
      reason: null,
    };
    handleRecovered(payload, '0.2.53', stub);
    expect(stub.successCalls).toHaveLength(1);
    expect(stub.errorCalls).toHaveLength(0);
    expect(stub.successCalls[0].msg).toBe('Launcher updated to v0.2.53.');
    expect(stub.successCalls[0].opts?.key).toBe('vct-update-recovered');
  });

  it('still routes a generic success message when version is empty', () => {
    const stub = makeStubToast();
    const payload: UpdateRecoveryPayload = {
      recovered: true,
      stale_or_invalid: false,
      lock_path: null,
      reason: null,
    };
    handleRecovered(payload, '', stub);
    expect(stub.successCalls).toHaveLength(1);
    expect(stub.successCalls[0].msg).toBe('Launcher update completed.');
  });
});

describe('update-toast-handlers / handleFailed', () => {
  it('routes through toast.error with the failed dedup key', () => {
    const stub = makeStubToast();
    const payload: UpdateRecoveryPayload = {
      recovered: false,
      stale_or_invalid: true,
      lock_path: '/home/user/.vct/update.lock.json',
      reason: 'lock_file_too_old_or_undated',
    };
    handleFailed(payload, stub);
    expect(stub.errorCalls).toHaveLength(1);
    expect(stub.successCalls).toHaveLength(0);
    expect(stub.errorCalls[0].opts?.key).toBe('vct-update-failed');
    expect(stub.errorCalls[0].msg).toContain('lock_file_too_old_or_undated');
    expect(stub.errorCalls[0].msg).toContain('/home/user/.vct/update.lock.json');
  });

  it('emits an error message even when both fields are null (defense)', () => {
    const stub = makeStubToast();
    const payload: UpdateRecoveryPayload = {
      recovered: false,
      stale_or_invalid: true,
      lock_path: null,
      reason: null,
    };
    handleFailed(payload, stub);
    expect(stub.errorCalls).toHaveLength(1);
    expect(stub.errorCalls[0].msg).toContain('(unknown)');
    expect(stub.errorCalls[0].msg).toContain('(unknown path)');
  });
});

// v0.2.54 Track C (FE C-1): the single router both delivery paths use.
describe('update-toast-handlers / handleReport', () => {
  it('routes recovered payloads to toast.success and returns true', () => {
    const stub = makeStubToast();
    const handled = handleReport(
      {
        recovered: true,
        stale_or_invalid: false,
        lock_path: '/home/user/.vct/update.lock.json',
        reason: null,
      },
      '0.2.54',
      stub,
    );
    expect(handled).toBe(true);
    expect(stub.successCalls).toHaveLength(1);
    expect(stub.errorCalls).toHaveLength(0);
    expect(stub.successCalls[0].msg).toBe('Launcher updated to v0.2.54.');
  });

  it('routes failed payloads to toast.error and returns true', () => {
    const stub = makeStubToast();
    const handled = handleReport(
      {
        recovered: false,
        stale_or_invalid: true,
        lock_path: '/home/user/.vct/update.lock.json',
        reason: 'swap_failures=1 of 2 swap(s) — see update.log',
      },
      '0.2.54',
      stub,
    );
    expect(handled).toBe(true);
    expect(stub.errorCalls).toHaveLength(1);
    expect(stub.successCalls).toHaveLength(0);
    expect(stub.errorCalls[0].msg).toContain('swap_failures=1');
  });

  it('is a no-op for the empty default payload (post-consume pulls)', () => {
    const stub = makeStubToast();
    const handled = handleReport(
      {
        recovered: false,
        stale_or_invalid: false,
        lock_path: null,
        reason: null,
      },
      '0.2.54',
      stub,
    );
    expect(handled).toBe(false);
    expect(stub.successCalls).toHaveLength(0);
    expect(stub.errorCalls).toHaveLength(0);
  });

  it('is a no-op for null/undefined payloads', () => {
    const stub = makeStubToast();
    expect(handleReport(null, '0.2.54', stub)).toBe(false);
    expect(handleReport(undefined, '0.2.54', stub)).toBe(false);
    expect(stub.successCalls).toHaveLength(0);
    expect(stub.errorCalls).toHaveLength(0);
  });

  it('prefers recovered over stale_or_invalid when both set (defense)', () => {
    const stub = makeStubToast();
    const handled = handleReport(
      {
        recovered: true,
        stale_or_invalid: true,
        lock_path: null,
        reason: null,
      },
      '',
      stub,
    );
    expect(handled).toBe(true);
    expect(stub.successCalls).toHaveLength(1);
    expect(stub.errorCalls).toHaveLength(0);
  });
});
