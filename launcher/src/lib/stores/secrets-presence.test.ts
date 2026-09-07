// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.3.0 — the Secrets panel must say where a value ACTUALLY lives.
//
// The defect these tests pin: the panel's badge was derived from
// `lifecycleOf`, which reads only the OS keychain, while the sanctioned
// resolvers (`vco_lib/agent_secrets.py::get`,
// `templates/scripts/vct_secrets_resolve.sh`) fall back to the tier-2 file
// store at `$VCT_SECRETS_DIR`. A key held only there resolved perfectly for
// every consumer and rendered as "NOT SET" — and `CLAUDE.md` warns that a
// GUI save and a `vct set` are different stores, so the user's natural
// response (re-type it here) forks the value into two copies that drift.
//
// v0.3.0, second pass — the same lie one scope down: `read_store_report`
// mapped `per_project` onto `projects/<NAME>/` and stopped, but the
// resolvers read `shared/<key>` next. A per-project ref whose value lives
// only in `shared/` therefore resolved for every consumer and badged
// "not set". `shared_file_store` is that missing leg, and it is
// marker-gated: a project holding `.no-shared-fallback` genuinely does not
// read the shared file, so it must not be told the key resolves.
//
// The store logic is driven through its PRODUCTION entry points
// (`secrets.loadFromBackend` / `secrets.refresh`) over a mocked `invoke`,
// not by re-deriving the mapping inline.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const calls: Array<{ cmd: string; args: Record<string, unknown> | undefined }> = [];
let handler: (cmd: string, args?: Record<string, unknown>) => unknown = () => undefined;

vi.mock('$lib/tauri', () => ({
  invoke: (cmd: string, args?: Record<string, unknown>) => {
    calls.push({ cmd, args });
    try {
      return Promise.resolve(handler(cmd, args));
    } catch (e) {
      return Promise.reject(e);
    }
  },
  tauriAvailable: () => true,
}));

async function freshStore() {
  vi.resetModules();
  return await import('./secrets');
}

afterEach(() => {
  calls.length = 0;
  handler = () => undefined;
});

describe('badgeOf — the badge must never call a resolving key "not set"', () => {
  let badgeOf: typeof import('./secrets').badgeOf;
  let isForked: typeof import('./secrets').isForked;

  beforeEach(async () => {
    const m = await freshStore();
    badgeOf = m.badgeOf;
    isForked = m.isForked;
  });

  /** A fully-probed row that resolves nowhere. Every case below states
   * ONLY the fields it is about, so a reader can see what drives it. */
  function row(
    over: Partial<import('./secrets').BadgeInput> = {},
  ): import('./secrets').BadgeInput {
    return {
      is_set: false,
      keychain: 'absent',
      file_store: 'absent',
      shared_file_store: 'absent',
      shared_read_disabled: false,
      shared_file_fallback_disabled: false,
      ...over,
    };
  }

  it('keychain present + readable by this project → set', () => {
    expect(badgeOf(row({ keychain: 'present', is_set: true }))).toBe('set');
  });

  it('keychain present + paused, nothing else serves → unset', () => {
    expect(badgeOf(row({ keychain: 'present', is_set: false }))).toBe('unset');
  });

  it('a key paused by ANOTHER launcher is not "set" either', () => {
    // `is_active` is this launcher's own row; `is_set` is the
    // cross-launcher answer every consumer actually receives. Gating the
    // badge on the former promised a value no consumer gets.
    expect(badgeOf(row({ keychain: 'present', is_set: false }))).not.toBe('set');
  });

  it('THE PAUSED-KEY DEFECT: paused keychain + file-store copy → "file-store", never "unset"', () => {
    // The resolvers fall through to tier 2 on `key_not_active` by
    // documented design (`agent_secrets.get`, `vct_secrets_resolve.sh`:
    // "the hub can't tell explicitly paused from never declared"). So a
    // paused key with a file copy RESOLVES — and the row said "unset",
    // the same lie the file-store branch was added to remove, on the one
    // arm that predates it.
    const badge = badgeOf(row({ keychain: 'present', is_set: false, file_store: 'present' }));
    expect(badge).toBe('file-store');
    expect(badge).not.toBe('unset');
  });

  it('THE PAUSED-KEY DEFECT, shared leg: paused keychain + shared/ copy → "shared-file-store"', () => {
    const badge = badgeOf(
      row({ keychain: 'present', is_set: false, shared_file_store: 'present' }),
    );
    expect(badge).toBe('shared-file-store');
    expect(badge).not.toBe('unset');
  });

  it('"unset" survives only where it is TRUE — both tier-2 legs known absent', () => {
    // A paused key with an UNREADABLE file store cannot be called unset:
    // the copy that would rescue it may well be there.
    expect(
      badgeOf(row({ keychain: 'present', is_set: false, file_store: 'unknown' })),
    ).toBe('unknown');
    expect(
      badgeOf(row({ keychain: 'present', is_set: false, shared_file_store: 'unknown' })),
    ).toBe('unknown');
  });

  it('THE DEFECT: keychain absent + file store present → "file-store", never "not-set"', () => {
    const badge = badgeOf(row({ file_store: 'present' }));
    expect(badge).toBe('file-store');
    expect(badge).not.toBe('not-set');
  });

  it('THE SECOND DEFECT: only the SHARED leg holds it → "shared-file-store", never "not-set"', () => {
    // `projects/<NAME>/<key>` misses, `shared/<key>` hits — which is the
    // next place every sanctioned resolver looks, so the key resolves.
    const badge = badgeOf(row({ shared_file_store: 'present' }));
    expect(badge).toBe('shared-file-store');
    expect(badge).not.toBe('not-set');
  });

  it('the OWN namespace outranks the shared leg, in the resolvers\' own order', () => {
    // `projects/<NAME>/` is exhausted before `shared/` is read, so a row
    // holding both must name its own copy — that is the one an edit here
    // would collide with.
    expect(
      badgeOf(row({ file_store: 'present', shared_file_store: 'present' })),
    ).toBe('file-store');
  });

  it('an UNREADABLE keychain does not make a readable file-store copy less real', () => {
    // Ordering matters: the file-store branch must outrank the unknown
    // branch, or a locked keychain would hide a key that resolves.
    expect(badgeOf(row({ keychain: 'unknown', file_store: 'present' }))).toBe('file-store');
    // …and the same must hold for the shared leg, or a locked keychain
    // would hide a key that resolves from `shared/`.
    expect(
      badgeOf(row({ keychain: 'unknown', file_store: 'unknown', shared_file_store: 'present' })),
    ).toBe('shared-file-store');
  });

  it('a store that could not be read reports unknown, not absence', () => {
    expect(badgeOf(row({ keychain: 'unknown' }))).toBe('unknown');
    expect(badgeOf(row({ file_store: 'unknown' }))).toBe('unknown');
    // The added leg gets the same treatment: an unevaluable
    // `.no-shared-fallback` gate is not evidence the key is missing.
    expect(badgeOf(row({ shared_file_store: 'unknown' }))).toBe('unknown');
  });

  it('MARKER-GATED: an opted-out project sees "not set", not a shared value it cannot read', () => {
    // The backend reports `shared_file_store: 'absent'` when the project
    // holds `.no-shared-fallback` — the file exists, but this project's
    // resolvers skip it. Badging it as resolving would be the same class
    // of lie in the opposite direction.
    expect(badgeOf(row({ shared_file_store: 'absent' }))).toBe('not-set');
  });

  it('genuinely nowhere → not set', () => {
    expect(badgeOf(row())).toBe('not-set');
  });

  // ── The project-wide shared opt-out (GAP-2) ───────────────────────────
  //
  // "Disable shared secrets for this project" drops the keychain's
  // user-shared bucket AND `~/.vct-secrets/shared/` for one reader. The
  // permission gate `is_set` does NOT model it (deliberately: it is a bulk
  // policy, not a per-(secret × requester) flag), so a shared row on an
  // opted-out project came back `is_set: true` and badged "set" for a value
  // that project will never receive.

  it('THE OPT-OUT DEFECT: a live shared keychain row the project cannot read is NOT "set"', () => {
    const badge = badgeOf(
      row({
        keychain: 'present',
        is_set: true,
        shared_read_disabled: true,
        shared_file_fallback_disabled: true,
      }),
    );
    expect(badge).toBe('shared-opted-out');
    expect(badge).not.toBe('set');
  });

  it('…nor is the shared FILE copy, once the marker gates it too', () => {
    expect(
      badgeOf(row({ file_store: 'present', shared_read_disabled: true, shared_file_fallback_disabled: true })),
    ).toBe('shared-opted-out');
  });

  it('the two gates are independent: a failed marker write leaves the FILE serving', () => {
    // `set_shared_secrets_read_disabled` writes the DB flag, then the
    // `.no-shared-fallback` marker best-effort, and warns when the second
    // fails. In that state tier 2 still serves this project — so claiming
    // the key does not reach it would be the false-negative-about-
    // resolution this whole badge exists to remove.
    expect(
      badgeOf(row({
        keychain: 'present',
        is_set: true,
        file_store: 'present',
        shared_read_disabled: true,
        shared_file_fallback_disabled: false,
      })),
    ).toBe('file-store');
  });

  it('…and the mirror case: marker written, DB flag off → the keychain still serves', () => {
    expect(
      badgeOf(row({
        keychain: 'present',
        is_set: true,
        file_store: 'present',
        shared_read_disabled: false,
        shared_file_fallback_disabled: true,
      })),
    ).toBe('set');
  });

  it('an opted-out project with NOTHING in the shared tier still reads "not set"', () => {
    // The opt-out explains why a value does not arrive; it does not
    // manufacture one. Absence is still absence.
    expect(
      badgeOf(row({ shared_read_disabled: true, shared_file_fallback_disabled: true })),
    ).toBe('not-set');
  });

  it('a GATED store that could not be read does not force "unknown" — it is irrelevant here', () => {
    // Nothing about that store can change this project's answer, so
    // "unlock your keychain" would be useless advice…
    expect(
      badgeOf(row({
        keychain: 'unknown',
        shared_read_disabled: true,
        shared_file_fallback_disabled: true,
      })),
    ).toBe('shared-opted-out');
    // …while an UNGATED store that could not be read still does.
    expect(
      badgeOf(row({
        keychain: 'unknown',
        file_store: 'unknown',
        shared_read_disabled: true,
        shared_file_fallback_disabled: false,
      })),
    ).toBe('unknown');
  });

  it('isForked is true only when BOTH stores hold the key', () => {
    expect(isForked({ keychain: 'present', file_store: 'present' })).toBe(true);
    expect(isForked({ keychain: 'present', file_store: 'absent' })).toBe(false);
    expect(isForked({ keychain: 'absent', file_store: 'present' })).toBe(false);
    expect(isForked({ keychain: 'unknown', file_store: 'present' })).toBe(false);
  });

  it('isForked stays keychain × OWN namespace — the shared collision is the shadow badge', () => {
    // Deliberate, and verified rather than assumed: `list_user_secret_keys_v2`
    // builds its rows from the union of the launcher's rows and the file
    // store's FILES, so a `shared/<key>` file produces a shared-scope row,
    // both rows come back `is_shadowed`, and each renders a badge naming
    // the winner and the loser (pinned backend-side by
    // `a_shared_file_copy_collides_across_scopes_and_both_rows_say_so`).
    // Widening `isForked` here would double-report that one fact — and its
    // wording ("also in file store" → delete one) is the wrong advice for a
    // shared file that other projects read.
    expect(isForked({ keychain: 'present', file_store: 'absent' })).toBe(false);
  });
});

describe('secrets store — store presence flows through the production paths', () => {
  it('loadFromBackend carries the two-store report onto every row', async () => {
    const { secrets, badgeOf } = await freshStore();
    handler = (cmd) => {
      if (cmd !== 'list_user_secret_keys_v2') return undefined;
      return [
        {
          scope: 'shared',
          project_id: '_user_shared_',
          module_id: 'user',
          key: 'github_pat',
          is_set: false,
          is_active: true,
          has_saved_value: false,
          is_shadowed: false,
          winning_scope: 'shared',
          keychain: 'absent',
          file_store: 'present',
          values_diverge: null,
          file_store_path: '/home/u/.vct-secrets/shared/github_pat',
          shared_file_store: 'absent',
          shared_file_store_path: null,
          winning_store: 'file_store',
          has_launcher_row: true,
        },
        {
          // The residual this pass closes: a per-project row the launcher
          // knows about, with no keychain value and nothing in its own
          // file-store namespace — satisfied only by `shared/`.
          scope: 'per_project',
          project_id: 'p1',
          module_id: 'user',
          key: 'SHARED_ONLY_KEY',
          is_set: false,
          is_active: true,
          has_saved_value: false,
          is_shadowed: true,
          winning_scope: 'shared',
          keychain: 'absent',
          file_store: 'absent',
          values_diverge: null,
          file_store_path: null,
          shared_file_store: 'present',
          shared_file_store_path: '/home/u/.vct-secrets/shared/SHARED_ONLY_KEY',
          winning_store: 'file_store',
          has_launcher_row: true,
        },
      ];
    };

    await secrets.loadFromBackend('p1');
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'github_pat');
    expect(row).toBeDefined();
    expect(row!.file_store).toBe('present');
    expect(row!.winning_store).toBe('file_store');
    expect(row!.file_store_path).toBe('/home/u/.vct-secrets/shared/github_pat');
    // The user-visible consequence: this row renders as resolving, and the
    // legacy keychain-only lifecycle would have said "empty" → "not set".
    expect(badgeOf(row!)).toBe('file-store');

    const shared = [...state.entries.values()].find((e) => e.key === 'SHARED_ONLY_KEY');
    expect(shared).toBeDefined();
    expect(shared!.shared_file_store).toBe('present');
    expect(shared!.shared_file_store_path).toBe(
      '/home/u/.vct-secrets/shared/SHARED_ONLY_KEY',
    );
    // Both stores of its OWN scope are empty, and it still resolves.
    expect(shared!.keychain).toBe('absent');
    expect(shared!.file_store).toBe('absent');
    expect(badgeOf(shared!)).toBe('shared-file-store');
    expect(badgeOf(shared!)).not.toBe('not-set');
  });

  it('refresh surfaces the divergent-copy state without ever receiving a value', async () => {
    const { secrets } = await freshStore();
    secrets.register({
      project_id: '_user_shared_',
      module_id: 'user',
      scope: 'shared',
      key: 'FORKED_KEY',
      sensitive: true,
    });
    handler = (cmd) => {
      if (cmd !== 'get_secret_status_v2') return undefined;
      return {
        is_set: true,
        is_active: true,
        has_saved_value: true,
        keychain: 'present',
        file_store: 'present',
        values_diverge: true,
        file_store_path: '/home/u/.vct-secrets/shared/FORKED_KEY',
        shared_file_store: 'absent',
        shared_file_store_path: null,
      };
    };

    await secrets.refresh({
      project_id: '_user_shared_',
      module_id: 'user',
      scope: 'shared',
      key: 'FORKED_KEY',
      sensitive: true,
    });
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'FORKED_KEY')!;
    expect(row.values_diverge).toBe(true);
    // Sensitive entries never request a preview — the panel renders dots.
    expect(calls.some((c) => c.cmd === 'get_secret_preview')).toBe(false);
    expect(row.preview).toBeNull();
  });

  it('a failed status probe resets BOTH stores to unknown — no stale confident claim', async () => {
    const { secrets, badgeOf } = await freshStore();
    const entry = {
      project_id: '_user_shared_',
      module_id: 'user',
      scope: 'shared' as const,
      key: 'LOCKED_KEY',
      sensitive: true,
    };
    secrets.register(entry);

    // First probe SUCCEEDS, so the row holds a confident 'present'. This
    // is what makes the assertion below load-bearing: without the reset,
    // a later failure would leave the panel asserting "set" on the
    // strength of a reading it can no longer make.
    handler = () => ({
      is_set: true,
      is_active: true,
      has_saved_value: true,
      keychain: 'present',
      file_store: 'absent',
      values_diverge: null,
      file_store_path: null,
      shared_file_store: 'absent',
      shared_file_store_path: null,
    });
    await secrets.refresh(entry);
    {
      const seeded = (await import('svelte/store')).get(secrets);
      const row = [...seeded.entries.values()].find((e) => e.key === 'LOCKED_KEY')!;
      expect(badgeOf(row)).toBe('set');
    }

    handler = () => {
      throw new Error('keychain locked');
    };
    await secrets.refresh(entry);
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'LOCKED_KEY')!;
    expect(row.keychain).toBe('unknown');
    expect(row.file_store).toBe('unknown');
    // The added leg must be reset too, or a probe that could not run would
    // leave a stale confident 'present'/'absent' driving the badge.
    expect(row.shared_file_store).toBe('unknown');
    expect(badgeOf(row)).toBe('unknown');
    expect(badgeOf(row)).not.toBe('not-set');
    expect(state.error).toContain('keychain locked');
  });

  it('refresh carries the SHARED fall-through leg onto a per-project entry', async () => {
    // This is the exact surface the residual named: the per-project
    // Secret-refs tab probes one `per_project` entry through
    // `secrets.refresh` → `get_secret_status_v2`, with no cross-scope list
    // to fall back on. If the leg does not survive this hop, the tab shows
    // "not set" for a key every consumer resolves.
    const { secrets, badgeOf } = await freshStore();
    const entry = {
      project_id: 'p1',
      module_id: 'user',
      scope: 'per_project' as const,
      key: 'REF_FROM_SHARED',
      sensitive: true,
    };
    secrets.register(entry);
    handler = (cmd) => {
      if (cmd !== 'get_secret_status_v2') return undefined;
      return {
        is_set: false,
        is_active: true,
        has_saved_value: false,
        keychain: 'absent',
        file_store: 'absent',
        values_diverge: null,
        file_store_path: null,
        shared_file_store: 'present',
        shared_file_store_path: '/home/u/.vct-secrets/shared/REF_FROM_SHARED',
      };
    };

    await secrets.refresh(entry);
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'REF_FROM_SHARED')!;
    expect(row.shared_file_store).toBe('present');
    expect(row.shared_file_store_path).toBe('/home/u/.vct-secrets/shared/REF_FROM_SHARED');
    expect(badgeOf(row)).toBe('shared-file-store');
    expect(badgeOf(row)).not.toBe('not-set');
    // The permission gate is untouched: `is_set` stays keychain × active.
    expect(row.is_set).toBe(false);
    expect(row.has_saved_value).toBe(false);
  });

  it('a freshly registered, never-probed entry is unknown rather than "not set"', async () => {
    const { secrets, badgeOf } = await freshStore();
    secrets.register({
      project_id: '_global_',
      module_id: 'licensing',
      scope: 'global',
      key: 'license_key____orchestrator__',
      sensitive: true,
    });
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()][0];
    expect(badgeOf(row)).toBe('unknown');
  });
});

describe('the shared-tier opt-out reaches the badge through the production paths', () => {
  // The two gates are computed server-side per READER, so the reader has to
  // survive every hop. It does not travel on the entry: a shared row is
  // owned by the `_user_shared_` sentinel, which names no project.

  it('loadFromBackend carries both gates onto the row, and the badge acts on them', async () => {
    const { secrets, badgeOf } = await freshStore();
    handler = (cmd) => {
      if (cmd !== 'list_user_secret_keys_v2') return undefined;
      return [
        {
          scope: 'shared',
          project_id: '_user_shared_',
          module_id: 'user',
          key: 'TEAM_TOKEN',
          // The permission gate says YES — it does not model the bulk
          // opt-out, deliberately — and the row must still not read "set".
          is_set: true,
          is_active: true,
          has_saved_value: true,
          is_shadowed: false,
          winning_scope: 'shared',
          keychain: 'present',
          file_store: 'absent',
          values_diverge: null,
          file_store_path: null,
          shared_file_store: 'absent',
          shared_file_store_path: null,
          winning_store: 'keychain',
          has_launcher_row: true,
          shared_read_disabled: true,
          shared_file_fallback_disabled: true,
        },
      ];
    };

    await secrets.loadFromBackend('p-opted-out');
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'TEAM_TOKEN')!;
    expect(row.shared_read_disabled).toBe(true);
    expect(row.shared_file_fallback_disabled).toBe(true);
    // The permission gate is untouched — that is the whole point of
    // carrying the opt-out separately.
    expect(row.is_set).toBe(true);
    expect(badgeOf(row)).toBe('shared-opted-out');
    expect(badgeOf(row)).not.toBe('set');
  });

  it('refresh names the READER, so a sentinel-owned row is not answered about the sentinel', async () => {
    const { secrets } = await freshStore();
    const entry = {
      project_id: '_user_shared_',
      module_id: 'user',
      scope: 'shared' as const,
      key: 'TEAM_TOKEN',
      sensitive: true,
    };
    secrets.register(entry);
    secrets.setRequesterProject('p-opted-out');
    handler = (cmd) => {
      if (cmd !== 'get_secret_status_v2') return undefined;
      return {
        is_set: true,
        is_active: true,
        has_saved_value: true,
        keychain: 'present',
        file_store: 'absent',
        values_diverge: null,
        file_store_path: null,
        shared_file_store: 'absent',
        shared_file_store_path: null,
        shared_read_disabled: true,
        shared_file_fallback_disabled: true,
      };
    };

    await secrets.refresh(entry);
    const probe = calls.find((c) => c.cmd === 'get_secret_status_v2')!;
    // The OWNER stays the sentinel (that is which keychain slot to read)…
    expect(probe.args!.projectId).toBe('_user_shared_');
    // …and the READER is the project whose view is on screen. Without
    // this the backend answers about the all-readers `*` row and neither
    // gate below could ever be true.
    expect(probe.args!.requesterProjectId).toBe('p-opted-out');
  });

  it('a per-key refresh does NOT overwrite the gates loadFromBackend just computed', async () => {
    // `SecretsPanel` calls `loadFromBackend` and then `refreshAll`. If the
    // second hop dropped the reader, every gated row would flip straight
    // back to "set" — the mechanism would be credited and never fire.
    const { secrets, badgeOf } = await freshStore();
    const shared = {
      scope: 'shared',
      project_id: '_user_shared_',
      module_id: 'user',
      key: 'TEAM_TOKEN',
      is_set: true,
      is_active: true,
      has_saved_value: true,
      is_shadowed: false,
      winning_scope: 'shared',
      keychain: 'present',
      file_store: 'absent',
      values_diverge: null,
      file_store_path: null,
      shared_file_store: 'absent',
      shared_file_store_path: null,
      winning_store: 'keychain',
      has_launcher_row: true,
      shared_read_disabled: true,
      shared_file_fallback_disabled: true,
    };
    handler = (cmd, args) => {
      if (cmd === 'list_user_secret_keys_v2') return [shared];
      if (cmd !== 'get_secret_status_v2') return undefined;
      // Stand in for the backend: the gates hold only when a reader is
      // named, exactly as `read_store_report` computes them.
      const reader = args?.requesterProjectId;
      const gated = reader === 'p-opted-out';
      return {
        is_set: true,
        is_active: true,
        has_saved_value: true,
        keychain: 'present',
        file_store: 'absent',
        values_diverge: null,
        file_store_path: null,
        shared_file_store: 'absent',
        shared_file_store_path: null,
        shared_read_disabled: gated,
        shared_file_fallback_disabled: gated,
      };
    };

    await secrets.loadFromBackend('p-opted-out');
    await secrets.refreshAll();
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'TEAM_TOKEN')!;
    expect(badgeOf(row)).toBe('shared-opted-out');
  });

  it('a failed probe clears the gates rather than keeping an unverified suppression', async () => {
    const { secrets, badgeOf } = await freshStore();
    const entry = {
      project_id: '_user_shared_',
      module_id: 'user',
      scope: 'shared' as const,
      key: 'TEAM_TOKEN',
      sensitive: true,
    };
    secrets.register(entry);
    secrets.setRequesterProject('p-opted-out');
    handler = () => ({
      is_set: true,
      is_active: true,
      has_saved_value: true,
      keychain: 'present',
      file_store: 'absent',
      values_diverge: null,
      file_store_path: null,
      shared_file_store: 'absent',
      shared_file_store_path: null,
      shared_read_disabled: true,
      shared_file_fallback_disabled: true,
    });
    await secrets.refresh(entry);

    handler = () => {
      throw new Error('hub down');
    };
    await secrets.refresh(entry);
    const state = (await import('svelte/store')).get(secrets);
    const row = [...state.entries.values()].find((e) => e.key === 'TEAM_TOKEN')!;
    expect(row.shared_read_disabled).toBe(false);
    expect(row.shared_file_fallback_disabled).toBe(false);
    // …and with every store now unreadable the row says so, rather than
    // asserting either "set" or "not read here" on a reading it no longer
    // has.
    expect(badgeOf(row)).toBe('unknown');
  });
});
