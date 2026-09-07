// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The per-project Secret-refs tab used to render its "Set?" column from
// `ProjectSecretRef.is_set` — a STORED column written once by whoever last
// called `set_project_secret_ref` and never revised. So it reported a
// value's presence from a cached boolean nothing kept current: "set" for a
// key since deleted, "missing" for a key that resolves from the file store.
//
// These tests pin the replacement, which reuses the sibling panel's status
// model (`get_secret_status_v2` → `StoreReport` → `badgeOf`) rather than
// forking a second one.

import { describe, it, expect } from 'vitest';
import {
  scopeOf,
  moduleOf,
  entryOf,
  badgeForRef,
  BADGE_LABEL,
  BADGE_TITLE,
  USER_SECRET_MODULE_ID,
  type SecretRefLike,
} from './secret-ref-status';
import { entryKey, type SecretEntry } from '$lib/stores/secrets';

function ref(over: Partial<SecretRefLike> = {}): SecretRefLike {
  return {
    secret_key: 'GITHUB_TOKEN',
    resolution: 'keychain-per-project',
    source_module: 'user',
    ...over,
  };
}

/** A probed store entry. Only the fields the badge reads matter. */
function probed(over: Partial<SecretEntry> = {}): SecretEntry {
  return {
    project_id: 'proj-1',
    module_id: 'user',
    scope: 'per_project',
    key: 'GITHUB_TOKEN',
    sensitive: true,
    is_set: true,
    is_active: true,
    has_saved_value: true,
    preview: null,
    is_shadowed: false,
    winning_scope: 'per_project',
    keychain: 'present',
    file_store: 'absent',
    shared_file_store: 'absent',
    values_diverge: null,
    file_store_path: null,
    shared_file_store_path: null,
    winning_store: 'keychain',
    has_launcher_row: true,
    shared_read_disabled: false,
    shared_file_fallback_disabled: false,
    ...over,
  } as SecretEntry;
}

function storeWith(e: SecretEntry): Map<string, SecretEntry> {
  return new Map([[entryKey(e), e]]);
}

describe('scope + module mapping', () => {
  it('maps the three keychain resolutions onto their scopes', () => {
    expect(scopeOf(ref({ resolution: 'keychain-per-project' }))).toBe('per_project');
    expect(scopeOf(ref({ resolution: 'keychain-shared' }))).toBe('shared');
    expect(scopeOf(ref({ resolution: 'keychain-global' }))).toBe('global');
  });

  it('probes file/env refs at per_project so the file store is consulted', () => {
    // The whole point: a `file` ref has no keychain scope, but it DOES have
    // a projects/<NAME>/<key> file-store location. Probing it anywhere else
    // would report the file store as absent and re-create the false
    // "missing" this module exists to remove.
    expect(scopeOf(ref({ resolution: 'file' }))).toBe('per_project');
    expect(scopeOf(ref({ resolution: 'env' }))).toBe('per_project');
  });

  it('uses the ref\'s own source_module, falling back to the writer\'s id', () => {
    expect(moduleOf(ref({ source_module: 'vct-search' }))).toBe('vct-search');
    expect(moduleOf(ref({ source_module: null }))).toBe(USER_SECRET_MODULE_ID);
    expect(moduleOf(ref({ source_module: '' }))).toBe(USER_SECRET_MODULE_ID);
  });

  it('never asks for a preview of a credential', () => {
    expect(entryOf(ref(), 'proj-1').sensitive).toBe(true);
  });
});

describe('badgeForRef — a rendered status must be a measured one', () => {
  it('reports set when the keychain holds an active value', () => {
    expect(badgeForRef(ref(), 'proj-1', storeWith(probed()))).toBe('set');
  });

  it('reports a file-store-only key as resolvable, never as absent', () => {
    const badge = badgeForRef(
      ref(),
      'proj-1',
      storeWith(probed({ keychain: 'absent', file_store: 'present' })),
    );
    expect(badge).toBe('file-store');
    expect(badge).not.toBe('not-set');
  });

  it('keeps the file-store branch ABOVE the unknown branch', () => {
    // An unreadable keychain does not make a readable file-store copy any
    // less real. If these two branches were reordered the user would be
    // told "unknown" about a key that demonstrably resolves.
    expect(
      badgeForRef(
        ref(),
        'proj-1',
        storeWith(probed({ keychain: 'unknown', file_store: 'present' })),
      ),
    ).toBe('file-store');
  });

  it('reports a SHARED-only key as resolvable — the residual this closes', () => {
    // A per-project ref whose value lives only in `~/.vct-secrets/shared/`.
    // `projects/<NAME>/<key>` misses; `shared/<key>` is the next place
    // every sanctioned resolver looks, so the key resolves — and the
    // column used to say "not set" for it because the probe stopped at the
    // project's own directory.
    const badge = badgeForRef(
      ref(),
      'proj-1',
      storeWith(
        probed({ keychain: 'absent', file_store: 'absent', shared_file_store: 'present' }),
      ),
    );
    expect(badge).toBe('shared-file-store');
    expect(badge).not.toBe('not-set');
  });

  it('MARKER-GATED: an opted-out project is not told a shared value serves it', () => {
    // With `~/.vct-secrets/projects/<NAME>/.no-shared-fallback` present the
    // backend reports the shared leg `absent` — the file exists, but this
    // project's resolvers skip it, so a "resolves" badge would be a lie in
    // the opposite direction.
    expect(
      badgeForRef(
        ref(),
        'proj-1',
        storeWith(
          probed({ keychain: 'absent', file_store: 'absent', shared_file_store: 'absent' }),
        ),
      ),
    ).toBe('not-set');
  });

  it('prefers the project\'s OWN file-store copy over the shared one', () => {
    // Resolver order: `projects/<NAME>/` is exhausted before `shared/`.
    expect(
      badgeForRef(
        ref(),
        'proj-1',
        storeWith(
          probed({ keychain: 'absent', file_store: 'present', shared_file_store: 'present' }),
        ),
      ),
    ).toBe('file-store');
  });

  it('reports an unreadable store as unknown, never as not set', () => {
    for (const stores of [
      { keychain: 'unknown', file_store: 'absent', shared_file_store: 'absent' },
      { keychain: 'absent', file_store: 'unknown', shared_file_store: 'absent' },
      { keychain: 'absent', file_store: 'absent', shared_file_store: 'unknown' },
    ] as const) {
      const badge = badgeForRef(ref(), 'proj-1', storeWith(probed(stores)));
      expect(badge).toBe('unknown');
      expect(badge).not.toBe('not-set');
    }
  });

  it('reports not-set only when EVERY tier answered "absent"', () => {
    expect(
      badgeForRef(
        ref(),
        'proj-1',
        storeWith(
          probed({ keychain: 'absent', file_store: 'absent', shared_file_store: 'absent' }),
        ),
      ),
    ).toBe('not-set');
  });

  it('reports a paused keychain entry as paused, not as absent', () => {
    expect(
      badgeForRef(
        ref(),
        'proj-1',
        storeWith(probed({ is_active: false, is_set: false })),
      ),
    ).toBe('unset');
  });

  it('a PAUSED key that the file store still serves reads as resolving', () => {
    // The resolvers fall through to tier 2 on `key_not_active`, so this
    // key works — and the column said "paused", which sends the user to
    // Reactivate a keychain entry that is not what their tools are
    // reading. Same defect class as the file-store branch itself, on the
    // arm that predates it.
    const badge = badgeForRef(
      ref(),
      'proj-1',
      storeWith(probed({ is_active: false, is_set: false, file_store: 'present' })),
    );
    expect(badge).toBe('file-store');
    expect(badge).not.toBe('unset');
  });

  it('a shared ref this project has opted out of reads "not read here"', () => {
    // `keychain-shared` ref + the project's "Disable shared secrets"
    // toggle: the keychain row is live and `is_set` is true (the
    // permission gate does not model the bulk opt-out, by design), but
    // this project resolves nothing from the shared tier.
    const entry = probed({
      scope: 'shared',
      keychain: 'present',
      is_set: true,
      shared_read_disabled: true,
      shared_file_fallback_disabled: true,
    });
    const badge = badgeForRef(
      ref({ resolution: 'keychain-shared' }),
      'proj-1',
      storeWith(entry),
    );
    expect(badge).toBe('shared-opted-out');
    expect(badge).not.toBe('set');
  });
});

describe('badgeForRef — a ref can name a location neither store covers', () => {
  // `resolution` accepts five values (the `project_secret_refs` CHECK), and
  // `file` / `env` point outside both launcher stores. Reachable on a
  // shipped path: vct-hub's `POST /api/v1/projects/{id}/secrets` takes
  // `resolution` as a free string. So the "not set" answer — a confident
  // absence assembled from three places, none of them the declared one —
  // must not be published for those refs.

  for (const resolution of ['file', 'env'] as const) {
    it(`a ${resolution} ref with no store copy reads "not checked", never "not set"`, () => {
      const badge = badgeForRef(
        ref({ resolution }),
        'proj-1',
        storeWith(
          probed({ keychain: 'absent', file_store: 'absent', shared_file_store: 'absent', is_set: false }),
        ),
      );
      expect(badge).toBe('declared-elsewhere');
      expect(badge).not.toBe('not-set');
    });

    it(`a ${resolution} ref whose value IS in a store still reports that store`, () => {
      // Tier 1 and tier 2 are consulted before tier 3 whatever the ref
      // declares, so a hit is a real answer and must pass through.
      expect(
        badgeForRef(
          ref({ resolution }),
          'proj-1',
          storeWith(probed({ keychain: 'absent', file_store: 'present', is_set: false })),
        ),
      ).toBe('file-store');
      expect(
        badgeForRef(ref({ resolution }), 'proj-1', storeWith(probed())),
      ).toBe('set');
    });

    it(`a ${resolution} ref does not launder "unknown" into "not checked"`, () => {
      // Only the confident-absence answer is withheld. An unreadable store
      // is still an unreadable store, and its advice (unlock, retry) is
      // not the advice this state gives.
      expect(
        badgeForRef(
          ref({ resolution }),
          'proj-1',
          storeWith(probed({ keychain: 'unknown', is_set: false })),
        ),
      ).toBe('unknown');
    });
  }

  it('a KEYCHAIN ref with no copy anywhere still reads "not set"', () => {
    // The withholding is scoped to refs that declare an unprobed location.
    // A `keychain-*` ref names a place we DID measure, so its absence is
    // measured too and must keep saying so.
    expect(
      badgeForRef(
        ref({ resolution: 'keychain-per-project' }),
        'proj-1',
        storeWith(
          probed({ keychain: 'absent', file_store: 'absent', shared_file_store: 'absent', is_set: false }),
        ),
      ),
    ).toBe('not-set');
  });

  it('reports unknown — not "not set" — before any probe has landed', () => {
    // The row exists (the ref list arrived) but its round-trip has not.
    // A confident red "not set" here is what sends users off to re-enter a
    // value they already have, forking it across both stores.
    const badge = badgeForRef(ref(), 'proj-1', new Map());
    expect(badge).toBe('unknown');
    expect(badge).not.toBe('not-set');
  });

  it('does not read a DIFFERENT key\'s probe', () => {
    // Keyed by (project, scope, module, key): a probe for another key must
    // not be mistaken for this one's answer.
    const other = storeWith(probed({ key: 'OPENAI_API_KEY' }));
    expect(badgeForRef(ref({ secret_key: 'GITHUB_TOKEN' }), 'proj-1', other)).toBe(
      'unknown',
    );
  });

  it('does not read another PROJECT\'s probe', () => {
    expect(badgeForRef(ref(), 'proj-2', storeWith(probed()))).toBe('unknown');
  });
});

describe('badge copy', () => {
  it('labels every badge the derivation can produce', () => {
    for (const b of [
      'set',
      'unset',
      'file-store',
      'shared-file-store',
      'shared-opted-out',
      'unknown',
      'not-set',
      'declared-elsewhere',
    ] as const) {
      expect(BADGE_LABEL[b]).toBeTruthy();
      expect(BADGE_TITLE[b]).toBeTruthy();
    }
    // The list above must stay EXHAUSTIVE over SecretBadge. A missing
    // member would silently drop out of the loop and ship an undefined
    // label — the column would render blank for a state it can produce.
    // `Record<RefBadge, string>` makes the tables total, so their key
    // sets ARE the union.
    expect(Object.keys(BADGE_LABEL).sort()).toEqual(Object.keys(BADGE_TITLE).sort());
    expect(Object.keys(BADGE_LABEL)).toHaveLength(8);
  });

  it('distinguishes the shared copy from the project\'s own, in words', () => {
    // Same underlying answer ("it resolves"), different consequence: the
    // shared copy is other projects' too, so a user must not read the two
    // badges as interchangeable.
    expect(BADGE_LABEL['shared-file-store']).not.toBe(BADGE_LABEL['file-store']);
    expect(BADGE_LABEL['shared-file-store'].toLowerCase()).toContain('shared');
    expect(BADGE_LABEL['shared-file-store'].toLowerCase()).not.toContain('missing');
    expect(BADGE_LABEL['shared-file-store'].toLowerCase()).not.toContain('not set');
    // The tooltip must name where it lives and what turns it off, or the
    // badge is a claim the user cannot act on.
    const title = BADGE_TITLE['shared-file-store'].toLowerCase();
    expect(title).toContain('shared');
    expect(title).toContain('disable shared secrets');
  });

  it('words the two not-a-value states so neither invites re-entry', () => {
    // Both mean "no value arrives here", and NEITHER means "type one in":
    // the opt-out is undone with a checkbox, and the unprobed-location
    // state was never a measurement at all.
    for (const b of ['shared-opted-out', 'declared-elsewhere'] as const) {
      expect(BADGE_LABEL[b].toLowerCase()).not.toContain('not set');
      expect(BADGE_LABEL[b].toLowerCase()).not.toContain('missing');
    }
    expect(BADGE_TITLE['shared-opted-out'].toLowerCase()).toContain(
      'disable shared secrets',
    );
    // …and it must not claim the key is gone: the value is untouched.
    expect(BADGE_TITLE['shared-opted-out'].toLowerCase()).toContain('nothing is paused');
    // The unprobed-location tooltip must say what was NOT measured, or it
    // is just a quieter version of the same false absence.
    expect(BADGE_TITLE['declared-elsewhere'].toLowerCase()).toContain('not evidence');
  });

  it('never calls a resolving key "missing" and never calls unknown absent', () => {
    expect(BADGE_LABEL['file-store'].toLowerCase()).not.toContain('missing');
    expect(BADGE_LABEL['file-store'].toLowerCase()).not.toContain('not set');
    expect(BADGE_LABEL.unknown.toLowerCase()).not.toContain('not set');
    // The unknown tooltip must actively warn against re-entry, because the
    // user's instinct on any non-"set" badge is to type the value in.
    expect(BADGE_TITLE.unknown.toLowerCase()).toContain('not the same as absent');
  });

  it('carries no value or digest — only presence vocabulary', () => {
    const copy = Object.values(BADGE_LABEL).concat(Object.values(BADGE_TITLE)).join(' ');
    expect(copy).not.toMatch(/[A-Za-z0-9_-]{32,}/);
  });
});
