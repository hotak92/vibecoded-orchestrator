// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.36 — Unit tests for the admin-rebind UI helpers.
//
// Exercises `shouldShowRebindButton` (the tier-gating predicate the
// ActivationModal uses for the "Rebind to this machine" affordance)
// and `friendlyRebindMessage` (the server-error → toast mapping).
// Pure-node tests; the modal's Svelte rendering is covered by E2E
// flows higher up.

import { describe, expect, it } from 'vitest';
import {
  friendlyRebindMessage,
  shouldShowRebindButton,
} from './admin-rebind';
import type { AdminRebindResult } from './types/launcher';

describe('shouldShowRebindButton', () => {
  it('returns true for admin tier', () => {
    expect(shouldShowRebindButton('admin')).toBe(true);
  });

  it('returns false for pro / mao / enterprise / free tiers', () => {
    expect(shouldShowRebindButton('free')).toBe(false);
    expect(shouldShowRebindButton('pro')).toBe(false);
    expect(shouldShowRebindButton('mao')).toBe(false);
    expect(shouldShowRebindButton('enterprise')).toBe(false);
  });

  it('returns false for null / undefined (no license cached yet)', () => {
    expect(shouldShowRebindButton(null)).toBe(false);
    expect(shouldShowRebindButton(undefined)).toBe(false);
  });

  it('returns false for unknown tier strings (defensive)', () => {
    // Forward-compatibility: a server-side tier addition that the
    // client doesn't recognize should NOT silently show the rebind
    // button (which exposes a server-mutation flow). Fail closed.
    expect(shouldShowRebindButton('superadmin')).toBe(false);
    expect(shouldShowRebindButton('')).toBe(false);
    expect(shouldShowRebindButton('Admin')).toBe(false); // wrong case
  });
});

describe('friendlyRebindMessage', () => {
  function makeResult(over: Partial<AdminRebindResult> = {}): AdminRebindResult {
    return {
      success: false,
      user: null,
      rebound_at: null,
      error: null,
      detail: null,
      machine_id_hash: 'a'.repeat(64),
      ...over,
    };
  }

  it('success without user → generic confirmation', () => {
    const toast = friendlyRebindMessage(
      makeResult({ success: true, rebound_at: '2026-05-26T14:22:00.000Z' }),
    );
    expect(toast.kind).toBe('success');
    expect(toast.message).toBe('Rebound to this machine');
  });

  it('success with user → confirmation includes user', () => {
    const toast = friendlyRebindMessage(
      makeResult({ success: true, user: 'admin1', rebound_at: 'ISO' }),
    );
    expect(toast.kind).toBe('success');
    expect(toast.message).toBe('Rebound to this machine (user: admin1)');
  });

  it('license_invalid → friendly mapping', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'license_invalid' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/not recognized/i);
  });

  it('not_an_admin_token → friendly mapping (rejects LS UUIDs at boundary)', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'not_an_admin_token' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/vct_admin_/);
  });

  it('no_license_key → friendly mapping', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'no_license_key' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/activate the token first/i);
  });

  it('rebind_failed → friendly mapping', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'rebind_failed' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/contact the project owner/i);
  });

  it('service_misconfigured → friendly mapping', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'service_misconfigured' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/not yet configured/i);
  });

  it('network → friendly mapping', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'network', detail: 'tcp connect: refused' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toMatch(/network/i);
    // Detail SHOULDN'T leak into the friendly path — but should be
    // preserved for callers that surface it elsewhere.
    expect(toast.message).not.toContain('tcp connect: refused');
  });

  it('unknown error code with detail → falls back to detail', () => {
    const toast = friendlyRebindMessage(
      makeResult({ error: 'something_new', detail: 'specific server detail' }),
    );
    expect(toast.kind).toBe('error');
    expect(toast.message).toBe('specific server detail');
  });

  it('unknown error code without detail → falls back to "Rebind failed (code)"', () => {
    const toast = friendlyRebindMessage(makeResult({ error: 'something_new' }));
    expect(toast.kind).toBe('error');
    expect(toast.message).toBe('Rebind failed (something_new)');
  });

  it('null error + null detail → falls back to "Rebind failed (unknown)"', () => {
    const toast = friendlyRebindMessage(makeResult({}));
    expect(toast.kind).toBe('error');
    expect(toast.message).toBe('Rebind failed (unknown)');
  });

  it('ipc_unavailable / ipc_failure → friendly mappings', () => {
    expect(friendlyRebindMessage(makeResult({ error: 'ipc_unavailable' })).message).toMatch(
      /tauri bridge/i,
    );
    expect(friendlyRebindMessage(makeResult({ error: 'ipc_failure' })).message).toMatch(
      /tauri ipc/i,
    );
  });
});
