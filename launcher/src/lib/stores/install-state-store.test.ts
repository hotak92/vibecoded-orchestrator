// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.53 M-P1-5: tests for per-install-root localStorage scoping.
//
// vitest.config.ts runs tests in `node` (no DOM). The store helper
// guards every access on `typeof localStorage !== 'undefined'`, so a
// trivial in-memory polyfill lets us exercise the migration path
// without pulling in jsdom.

import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const store = new Map<string, string>();
    const polyfill: Storage = {
      get length() {
        return store.size;
      },
      clear() {
        store.clear();
      },
      getItem(key: string) {
        return store.has(key) ? (store.get(key) as string) : null;
      },
      key(index: number) {
        return Array.from(store.keys())[index] ?? null;
      },
      removeItem(key: string) {
        store.delete(key);
      },
      setItem(key: string, value: string) {
        store.set(key, String(value));
      },
    };
    (globalThis as { localStorage?: Storage }).localStorage = polyfill;
  }
});

import {
  scopedKey,
  getInstallScopedFlag,
  setInstallScopedFlag,
  clearInstallScopedFlag,
  isInstallScopedFlagSet,
} from './install-state-store';

describe('install-state-store: scopedKey', () => {
  it('joins flag and install_root with a colon', () => {
    expect(scopedKey('vct.foo', '/home/user/vco-stable')).toBe(
      'vct.foo:/home/user/vco-stable',
    );
  });

  it('uses the "unknown" sentinel when install_root is null', () => {
    expect(scopedKey('vct.foo', null)).toBe('vct.foo:unknown');
  });

  it('uses the "unknown" sentinel when install_root is undefined', () => {
    expect(scopedKey('vct.foo', undefined)).toBe('vct.foo:unknown');
  });

  it('uses the "unknown" sentinel when install_root is an empty string', () => {
    expect(scopedKey('vct.foo', '')).toBe('vct.foo:unknown');
  });
});

describe('install-state-store: cross-clone scoping (M-P1-5)', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('keeps two clones distinct: setting one does not leak to the other', () => {
    setInstallScopedFlag('vct.install_check_dismissed', '/home/user/clone-a', 'true');
    expect(
      getInstallScopedFlag('vct.install_check_dismissed', '/home/user/clone-a'),
    ).toBe('true');
    expect(
      getInstallScopedFlag('vct.install_check_dismissed', '/home/user/clone-b'),
    ).toBeNull();
  });

  it('clearing one clone preserves the other', () => {
    setInstallScopedFlag('vct.x', '/a', 'true');
    setInstallScopedFlag('vct.x', '/b', 'true');
    clearInstallScopedFlag('vct.x', '/a');
    expect(getInstallScopedFlag('vct.x', '/a')).toBeNull();
    expect(getInstallScopedFlag('vct.x', '/b')).toBe('true');
  });

  it('separates the "unknown" bucket from a real install_root', () => {
    setInstallScopedFlag('vct.update.seen_version', '/real/path', 'merge:0.2.52');
    setInstallScopedFlag('vct.update.seen_version', null, 'install_stale:0.2.50');
    expect(getInstallScopedFlag('vct.update.seen_version', '/real/path')).toBe(
      'merge:0.2.52',
    );
    expect(getInstallScopedFlag('vct.update.seen_version', null)).toBe(
      'install_stale:0.2.50',
    );
  });
});

describe('install-state-store: legacy unscoped key migration', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('first scoped read promotes an unscoped legacy value to the scoped slot', () => {
    // Simulate a launcher running pre-v0.2.53 that wrote the unscoped key.
    localStorage.setItem('vct.install_check_dismissed', 'true');

    const v = getInstallScopedFlag('vct.install_check_dismissed', '/clone-a');
    expect(v).toBe('true');

    // Scoped key now populated.
    expect(localStorage.getItem('vct.install_check_dismissed:/clone-a')).toBe('true');
    // Legacy key cleared.
    expect(localStorage.getItem('vct.install_check_dismissed')).toBeNull();
  });

  it('migration is one-shot: subsequent reads see only the scoped value', () => {
    localStorage.setItem('vct.flag', 'legacy-val');

    const first = getInstallScopedFlag('vct.flag', '/r');
    expect(first).toBe('legacy-val');

    // Mutate the scoped slot; the legacy slot is gone — second call
    // must NOT resurrect it.
    setInstallScopedFlag('vct.flag', '/r', 'new-val');
    expect(getInstallScopedFlag('vct.flag', '/r')).toBe('new-val');
    expect(localStorage.getItem('vct.flag')).toBeNull();
  });

  it('a second install_root reading the same flag does NOT inherit the migrated legacy', () => {
    // Set legacy.
    localStorage.setItem('vct.flag', 'legacy-val');
    // First read migrates for clone /a.
    getInstallScopedFlag('vct.flag', '/a');
    // Second clone /b sees null — the legacy migrated INTO /a, not into /b.
    expect(getInstallScopedFlag('vct.flag', '/b')).toBeNull();
  });

  it('returns null when neither scoped nor legacy is present', () => {
    expect(getInstallScopedFlag('vct.never_set', '/somewhere')).toBeNull();
  });
});

describe('install-state-store: isInstallScopedFlagSet', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns true only when value is exactly "1"', () => {
    setInstallScopedFlag('vct.onboarding_force', '/r', '1');
    expect(isInstallScopedFlagSet('vct.onboarding_force', '/r')).toBe(true);
  });

  it('returns false for "true" (not the conventional value for this helper)', () => {
    setInstallScopedFlag('vct.onboarding_force', '/r', 'true');
    expect(isInstallScopedFlagSet('vct.onboarding_force', '/r')).toBe(false);
  });

  it('returns false for missing key', () => {
    expect(isInstallScopedFlagSet('vct.onboarding_force', '/r')).toBe(false);
  });
});
