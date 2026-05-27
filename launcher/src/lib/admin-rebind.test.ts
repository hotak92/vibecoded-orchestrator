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
  hasActiveLicense,
  shouldShowRebindButton,
} from './admin-rebind';
import type { AdminRebindResult } from './types/launcher';

describe('shouldShowRebindButton', () => {
  it('returns true for admin tier (no last_error)', () => {
    expect(shouldShowRebindButton('admin')).toBe(true);
    expect(shouldShowRebindButton('admin', null)).toBe(true);
  });

  it('returns false for pro / mao / enterprise / free tiers with no error', () => {
    expect(shouldShowRebindButton('free')).toBe(false);
    expect(shouldShowRebindButton('pro')).toBe(false);
    expect(shouldShowRebindButton('mao')).toBe(false);
    expect(shouldShowRebindButton('enterprise')).toBe(false);
  });

  it('returns false for null / undefined (no license cached yet)', () => {
    expect(shouldShowRebindButton(null)).toBe(false);
    expect(shouldShowRebindButton(undefined)).toBe(false);
    expect(shouldShowRebindButton(null, null)).toBe(false);
  });

  it('returns false for unknown tier strings (defensive)', () => {
    // Forward-compatibility: a server-side tier addition that the
    // client doesn't recognize should NOT silently show the rebind
    // button (which exposes a server-mutation flow). Fail closed.
    expect(shouldShowRebindButton('superadmin')).toBe(false);
    expect(shouldShowRebindButton('')).toBe(false);
    expect(shouldShowRebindButton('Admin')).toBe(false); // wrong case
  });

  // v0.2.36 migration-recovery branch: existing admin Vault rows pinned
  // to the old MAC-derived machine_id_hash will /validate-tier with
  // machine_mismatch after Agent T's platform-stable hash lands. The
  // server flips tier to 'free' and writes the error into
  // tier_cache.last_error; the predicate must still surface the button
  // so the user can unstick themselves.
  it('returns true for free tier + server "machine_mismatch" error code', () => {
    expect(shouldShowRebindButton('free', 'machine_mismatch')).toBe(true);
  });

  it('returns true for free tier + friendly "different machine" message', () => {
    expect(
      shouldShowRebindButton(
        'free',
        'Admin token is bound to a different machine.',
      ),
    ).toBe(true);
  });

  it('returns false for free tier + unrelated error', () => {
    expect(shouldShowRebindButton('free', 'invalid_license_key')).toBe(false);
    expect(shouldShowRebindButton('free', 'network')).toBe(false);
    expect(shouldShowRebindButton('free', 'service_unavailable')).toBe(false);
  });

  it('returns true for pro tier + machine_mismatch (defensive show)', () => {
    // Corner case: LS pro users can't in practice produce machine_mismatch
    // (their UUIDs never hit Vault TOFU). If it ever surfaces, prefer
    // showing the button — the rebind endpoint will reject with its own
    // clear error (not_an_admin_token) rather than silently hiding an
    // actionable affordance.
    expect(shouldShowRebindButton('pro', 'machine_mismatch')).toBe(true);
  });

  it('returns false for free tier + empty/whitespace error', () => {
    expect(shouldShowRebindButton('free', '')).toBe(false);
    // Whitespace string does not contain 'machine' — predicate must reject.
    expect(shouldShowRebindButton('free', '   ')).toBe(false);
  });

  it('case-insensitive substring match on "machine"', () => {
    expect(shouldShowRebindButton('free', 'MACHINE_MISMATCH')).toBe(true);
    expect(shouldShowRebindButton('free', 'Bound To A Different Machine')).toBe(true);
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

// v0.2.37 (Bug 1): `hasActiveLicense` is the `$derived` predicate the
// ActivationModal uses to decide which branch (activation input vs
// Refresh/Rebind/Deactivate buttons) to render. Pre-v0.2.37 the modal
// used the bare `tier !== 'free'` check, which collapsed two distinct
// states — see the helper's docstring for the regression context.
//
// These tests pin the predicate's behaviour against representative
// states (genuine free, happy-path paid tiers, recovery-mode free)
// so a future tier-string addition or rebind-predicate tweak doesn't
// silently re-introduce the regression. Since the launcher's vitest
// config is node-only (no @testing-library/svelte, no jsdom), we test
// the pure helper directly rather than rendering the component.
describe('hasActiveLicense (modal gating predicate)', () => {
  it('returns false for genuinely free user (no error)', () => {
    // Pre-fix and post-fix agree: show activation input.
    expect(hasActiveLicense('free')).toBe(false);
    expect(hasActiveLicense('free', null)).toBe(false);
    expect(hasActiveLicense('free', undefined)).toBe(false);
    expect(hasActiveLicense('free', '')).toBe(false);
  });

  it('returns false when license store is empty (null/undefined tier)', () => {
    expect(hasActiveLicense(null)).toBe(false);
    expect(hasActiveLicense(undefined)).toBe(false);
    expect(hasActiveLicense(null, null)).toBe(false);
  });

  it('returns true for paid tiers (pro / mao / enterprise / admin)', () => {
    expect(hasActiveLicense('pro')).toBe(true);
    expect(hasActiveLicense('mao')).toBe(true);
    expect(hasActiveLicense('enterprise')).toBe(true);
    expect(hasActiveLicense('admin')).toBe(true);
  });

  // The load-bearing regression case: admin's machine_id_hash drifts
  // (OS reinstall, laptop swap, v0.2.35→v0.2.36 hash-algo migration).
  // /validate-tier flips tier to 'free' server-side and writes
  // last_error: machine_mismatch. Pre-fix this gave the user ONLY the
  // activation input — the Rebind button was hidden in the `{:else}`
  // branch. Post-fix the predicate returns true so the action buttons
  // render and the user can self-recover.
  it('returns true for free tier + machine_mismatch error (Bug 1 regression)', () => {
    expect(hasActiveLicense('free', 'machine_mismatch')).toBe(true);
  });

  it('returns true for free tier + friendly "different machine" message', () => {
    expect(
      hasActiveLicense('free', 'Admin token is bound to a different machine.'),
    ).toBe(true);
  });

  it('returns true for free tier + case-insensitive machine error', () => {
    expect(hasActiveLicense('free', 'MACHINE_MISMATCH')).toBe(true);
    expect(hasActiveLicense('free', 'Bound To A Different Machine')).toBe(true);
  });

  // Unrelated errors keep the user in the "no license" branch (we
  // can't recover via rebind if the issue is network / invalid key /
  // service down — the right affordance is still the activation input).
  it('returns false for free tier + unrelated error', () => {
    expect(hasActiveLicense('free', 'invalid_license_key')).toBe(false);
    expect(hasActiveLicense('free', 'network')).toBe(false);
    expect(hasActiveLicense('free', 'service_unavailable')).toBe(false);
  });

  it('stays true for paid tiers even with errors (last_error is informational)', () => {
    // A pro user with a stale last_error from a prior failed refresh
    // shouldn't suddenly see the activation input — they still have a
    // valid license, the action buttons are correct.
    expect(hasActiveLicense('pro', 'network')).toBe(true);
    expect(hasActiveLicense('admin', 'machine_mismatch')).toBe(true);
  });
});
