// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 Stream D — Unit tests for the license-gate helpers.
//
// Exercises the predicates that decide whether a paid-module surface
// is rendered inert (banner + opacity 0.5 + pointer-events none + native
// disabled). The Svelte component integration is covered by the
// E2E flow higher up; these tests pin the pure predicates so future
// refactors can't silently change the gating contract.

import { describe, expect, it } from 'vitest';
import {
  bannerCopy,
  isLicenseActive,
  licenseStatus,
  moduleIsLicenseGated,
  moduleNeedsLicense,
} from './license-gate';
import type { ModuleCatalogEntry, TierCacheView } from './types/launcher';

function mkCache(partial: Partial<TierCacheView>): TierCacheView {
  return {
    orchestrator_tier: 'free',
    module_licenses: {},
    last_validated: 0,
    last_error: null,
    grace_period_remaining_ms: null,
    ...partial,
  };
}

function mkEntry(partial: Partial<ModuleCatalogEntry>): ModuleCatalogEntry {
  return {
    id: 'test-module',
    name: 'Test Module',
    version: '1.0.0',
    description: '',
    category: 'misc',
    tags: [],
    license_required: false,
    license_variant_ids: [],
    min_orchestrator_tier: 'free',
    compatibility_hosts: [],
    is_licensed: false,
    manifest_source: 'test',
    kind: 'available',
    ...partial,
  } as ModuleCatalogEntry;
}

describe('licenseStatus', () => {
  it('returns "missing" when cache is null/undefined', () => {
    expect(licenseStatus(null)).toBe('missing');
    expect(licenseStatus(undefined)).toBe('missing');
  });

  it('returns "active" when tier is non-free', () => {
    expect(licenseStatus(mkCache({ orchestrator_tier: 'pro' }))).toBe('active');
    expect(licenseStatus(mkCache({ orchestrator_tier: 'mao' }))).toBe('active');
    expect(licenseStatus(mkCache({ orchestrator_tier: 'enterprise' }))).toBe(
      'active',
    );
    expect(licenseStatus(mkCache({ orchestrator_tier: 'admin' }))).toBe(
      'active',
    );
  });

  it('returns "active" for machine_mismatch recovery branch', () => {
    // free tier server-side but last_error mentions machine → recovery
    // path (admin rebinding after platform-stable hash migration). The
    // license IS active for gating purposes — the rebind UI handles
    // the recovery flow.
    const cache = mkCache({
      orchestrator_tier: 'free',
      last_error: 'machine_mismatch',
    });
    expect(licenseStatus(cache)).toBe('active');
  });

  it('returns "expired" for free tier with a non-machine last_error', () => {
    expect(
      licenseStatus(
        mkCache({ orchestrator_tier: 'free', last_error: 'expired' }),
      ),
    ).toBe('expired');
    expect(
      licenseStatus(
        mkCache({ orchestrator_tier: 'free', last_error: 'revoked' }),
      ),
    ).toBe('expired');
    expect(
      licenseStatus(
        mkCache({
          orchestrator_tier: 'free',
          last_error: 'invalid_signature',
        }),
      ),
    ).toBe('expired');
  });

  it('returns "missing" for free tier with no last_error', () => {
    expect(licenseStatus(mkCache({ orchestrator_tier: 'free' }))).toBe(
      'missing',
    );
    expect(
      licenseStatus(
        mkCache({ orchestrator_tier: 'free', last_error: null }),
      ),
    ).toBe('missing');
    expect(
      licenseStatus(mkCache({ orchestrator_tier: 'free', last_error: '' })),
    ).toBe('missing');
  });

  it('treats whitespace-only last_error as missing (defensive)', () => {
    // Server bug or stale write — a string of spaces shouldn't pretend
    // to be an expired-license error.
    expect(
      licenseStatus(mkCache({ orchestrator_tier: 'free', last_error: '   ' })),
    ).toBe('missing');
  });
});

describe('isLicenseActive', () => {
  it('returns false for null/undefined cache (defensive default)', () => {
    expect(isLicenseActive(null)).toBe(false);
    expect(isLicenseActive(undefined)).toBe(false);
  });

  it('returns true for pro/mao/enterprise/admin tiers', () => {
    expect(isLicenseActive(mkCache({ orchestrator_tier: 'pro' }))).toBe(true);
    expect(isLicenseActive(mkCache({ orchestrator_tier: 'mao' }))).toBe(true);
    expect(
      isLicenseActive(mkCache({ orchestrator_tier: 'enterprise' })),
    ).toBe(true);
    expect(isLicenseActive(mkCache({ orchestrator_tier: 'admin' }))).toBe(true);
  });

  it('returns false for free tier with no error or non-machine error', () => {
    expect(isLicenseActive(mkCache({ orchestrator_tier: 'free' }))).toBe(false);
    expect(
      isLicenseActive(
        mkCache({ orchestrator_tier: 'free', last_error: 'expired' }),
      ),
    ).toBe(false);
  });

  it('returns true for free tier with machine_mismatch (recovery path)', () => {
    expect(
      isLicenseActive(
        mkCache({
          orchestrator_tier: 'free',
          last_error: 'Admin token is bound to a different machine.',
        }),
      ),
    ).toBe(true);
  });
});

describe('moduleNeedsLicense', () => {
  it('returns true when license_required is true', () => {
    expect(moduleNeedsLicense(mkEntry({ license_required: true }))).toBe(true);
  });

  it('returns true when min_orchestrator_tier is a paid tier', () => {
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'pro' })),
    ).toBe(true);
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'mao' })),
    ).toBe(true);
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'enterprise' })),
    ).toBe(true);
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'admin' })),
    ).toBe(true);
  });

  it('returns false for explicitly free modules', () => {
    expect(
      moduleNeedsLicense(
        mkEntry({ license_required: false, min_orchestrator_tier: 'free' }),
      ),
    ).toBe(false);
  });

  it('treats empty/whitespace tier as free (defensive)', () => {
    expect(
      moduleNeedsLicense(
        mkEntry({ license_required: false, min_orchestrator_tier: '' }),
      ),
    ).toBe(false);
    expect(
      moduleNeedsLicense(
        mkEntry({ license_required: false, min_orchestrator_tier: '   ' }),
      ),
    ).toBe(false);
  });

  it('is case-insensitive on tier comparison', () => {
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'PRO' })),
    ).toBe(true);
    expect(
      moduleNeedsLicense(mkEntry({ min_orchestrator_tier: 'Free' })),
    ).toBe(false);
  });
});

describe('moduleIsLicenseGated', () => {
  it('returns false for free modules regardless of license state', () => {
    const freeEntry = mkEntry({
      license_required: false,
      min_orchestrator_tier: 'free',
    });
    expect(moduleIsLicenseGated(freeEntry, null)).toBe(false);
    expect(moduleIsLicenseGated(freeEntry, mkCache({}))).toBe(false);
    expect(
      moduleIsLicenseGated(
        freeEntry,
        mkCache({ orchestrator_tier: 'pro' }),
      ),
    ).toBe(false);
  });

  it('returns true for paid module with no license', () => {
    const paidEntry = mkEntry({ license_required: true });
    expect(moduleIsLicenseGated(paidEntry, null)).toBe(true);
    expect(
      moduleIsLicenseGated(paidEntry, mkCache({ orchestrator_tier: 'free' })),
    ).toBe(true);
  });

  it('returns true for paid module with expired license', () => {
    const paidEntry = mkEntry({ license_required: true });
    expect(
      moduleIsLicenseGated(
        paidEntry,
        mkCache({ orchestrator_tier: 'free', last_error: 'expired' }),
      ),
    ).toBe(true);
  });

  it('returns false for paid module with active license', () => {
    const paidEntry = mkEntry({ license_required: true });
    expect(
      moduleIsLicenseGated(paidEntry, mkCache({ orchestrator_tier: 'pro' })),
    ).toBe(false);
    expect(
      moduleIsLicenseGated(paidEntry, mkCache({ orchestrator_tier: 'mao' })),
    ).toBe(false);
    expect(
      moduleIsLicenseGated(
        paidEntry,
        mkCache({ orchestrator_tier: 'admin' }),
      ),
    ).toBe(false);
  });

  it('returns false for paid module in machine_mismatch recovery', () => {
    // Pre-rebind state — the user technically has an admin key but
    // the server flipped them to free. The rebind UI handles the
    // recovery flow; we don't want to also gate every paid surface
    // during that flow (it'd be doubly confusing).
    const paidEntry = mkEntry({ license_required: true });
    expect(
      moduleIsLicenseGated(
        paidEntry,
        mkCache({
          orchestrator_tier: 'free',
          last_error: 'machine_mismatch',
        }),
      ),
    ).toBe(false);
  });

  it('returns false for null/undefined entry (still-loading catalog)', () => {
    expect(moduleIsLicenseGated(null, null)).toBe(false);
    expect(
      moduleIsLicenseGated(undefined, mkCache({ orchestrator_tier: 'free' })),
    ).toBe(false);
  });
});

describe('bannerCopy', () => {
  it('returns the expired-specific copy for expired status', () => {
    const copy = bannerCopy('expired');
    expect(copy.title).toMatch(/expired/i);
    expect(copy.actionLabel).toMatch(/license manager/i);
    expect(copy.description.length).toBeGreaterThan(0);
  });

  it('returns the missing-specific copy for missing status', () => {
    const copy = bannerCopy('missing');
    expect(copy.title).toMatch(/license required/i);
    expect(copy.actionLabel).toMatch(/activate/i);
    expect(copy.description.length).toBeGreaterThan(0);
  });

  it('returns empty copy for active status (defensive — should not be called)', () => {
    const copy = bannerCopy('active');
    expect(copy.title).toBe('');
    expect(copy.description).toBe('');
    expect(copy.actionLabel).toBe('');
  });
});
