// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The per-project Secret-refs tab's "Set?" column, as a testable module.
//
// WHY THIS IS NOT INLINE IN SecretsTab.svelte: the launcher's vitest setup
// runs in a `node` environment with no DOM and no svelte component runner
// (`launcher/vitest.config.ts`), so logic living inside a `.svelte` file is
// untestable here. Every other derivation in this folder is extracted for
// the same reason (`hooks-view.ts`, `module-status-display.ts`, …).
//
// WHAT IT REPLACED: the column used to render `ProjectSecretRef.is_set` —
// a STORED column, written once by whoever last called
// `set_project_secret_ref` (the hub's .env migration passes `Some(true)` at
// registration time, `vct-hub/src/secrets_api.rs`) and never revised. It
// therefore said "set" for a key the user had since deleted, and "missing"
// for a key that resolves perfectly well from the tier-2 file store.
//
// The replacement asks the same question the sibling SecretsPanel asks —
// `get_secret_status_v2` via the shared `secrets` store — and renders it
// with the SAME `badgeOf`. One status model, two surfaces. Do not add a
// second badge derivation here or in the component: the invariants
// `badgeOf` encodes (an unreadable store is `unknown`, never "not set"; the
// file-store-present branch outranks the unknown branch) are the whole
// point, and a fork drifts off them silently.
//
// The one thing this module adds on top is `'declared-elsewhere'`, and it
// is not a fork of the derivation: `badgeOf` still decides every store
// question. A ref carries a fact no store report can — the LOCATION it
// declares — and for the two resolutions that name somewhere neither store
// covers (`file`, `env`), publishing `badgeOf`'s confident "not set" would
// be asserting an absence about a place nothing looked at. So that single
// answer is withheld; every other answer passes through untouched.

import { badgeOf, entryKey, type SecretBadge, type SecretEntry, type SecretScope } from '$lib/stores/secrets';
import type { ProjectSecretRef } from '$lib/types/project-state';

/** The keychain module slot user-authored secrets are written under.
 *
 * Must match `IMPORT_MODULE_ID` in `vct-hub/src/secrets_api.rs` and
 * `commands/secrets_import.rs` — the writers of both the keychain entry and
 * the ref row. Probing a different slot would miss the keychain entry and
 * re-create the very false "missing" this module exists to remove. */
export const USER_SECRET_MODULE_ID = 'user';

/** The subset of a ref this module needs. Keeps the tests free of the
 * unrelated ref fields (`description`, `updated_at`, …). */
export type SecretRefLike = Pick<ProjectSecretRef, 'secret_key' | 'resolution' | 'source_module'>;

/** Which keychain scope a ref's `resolution` names.
 *
 * `file` and `env` refs have no keychain scope of their own. They are
 * probed at `per_project`, which yields keychain `absent` plus an honest
 * `projects/<NAME>/<key>` file-store probe AND the `shared/<key>`
 * fall-through leg the resolvers read next — the facts the badge needs,
 * and the reason such a ref can read "set — file store" or
 * "set — shared file store" instead of "missing". */
export function scopeOf(ref: SecretRefLike): SecretScope {
  if (ref.resolution === 'keychain-shared') return 'shared';
  if (ref.resolution === 'keychain-global') return 'global';
  return 'per_project';
}

/** The module slot to probe for this ref. */
export function moduleOf(ref: SecretRefLike): string {
  return ref.source_module && ref.source_module.length > 0
    ? ref.source_module
    : USER_SECRET_MODULE_ID;
}

/** The `secrets` store key this ref maps onto. */
export function entryOf(
  ref: SecretRefLike,
  projectId: string,
): Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'> {
  return {
    project_id: projectId,
    module_id: moduleOf(ref),
    scope: scopeOf(ref),
    key: ref.secret_key,
    // Refs describe credentials; never request a preview for them.
    sensitive: true,
  };
}

/** A ref-only state on top of the shared [`SecretBadge`] vocabulary.
 *
 * `'declared-elsewhere'` exists because a ref can NAME a location this
 * module does not probe. `resolution` is constrained by the
 * `project_secret_refs` CHECK to one of five values, and two of them —
 * `'file'` (with `file_path`) and `'env'` (with `env_name`) — point outside
 * the launcher's two stores entirely. Both are reachable on a shipped
 * path: `POST /api/v1/projects/{id}/secrets` on vct-hub takes `resolution`
 * as a free string and the DB accepts all five (the launcher's own writers
 * happen to emit only `keychain-per-project`, which is a fact about today's
 * writers, not about the schema).
 *
 * For such a ref the probes still run and a HIT is reported normally — a
 * keychain or file-store copy really does resolve first, whatever the ref
 * declares. Only the MISS is unwarranted: reporting "not set" would be a
 * confident absence derived from three places, none of which is the place
 * the ref itself names. That is the defect this whole surface exists to
 * remove, so the miss gets its own honest state instead.
 *
 * Why not probe those locations and answer properly? `'env'` names a
 * variable in the CONSUMER process's environment, which the launcher
 * cannot observe at all — so the not-probed state has to exist regardless,
 * and adding a `file_path` stat would leave the same gap for `'env'` while
 * introducing a blocking read of a user-supplied absolute path (a network
 * mount would hang the tab). */
export type RefBadge = SecretBadge | 'declared-elsewhere';

/** Whether the ref resolves from somewhere neither store covers. */
function declaresUnprobedLocation(ref: SecretRefLike): boolean {
  return ref.resolution === 'file' || ref.resolution === 'env';
}

/** Live badge for one ref, from the shared store's probed entries.
 *
 * Returns `'unknown'` when no probe has landed yet. That is deliberate and
 * load-bearing: a row rendered before its round-trip returns has told us
 * nothing about either store, and flashing a confident "not set" is what
 * sends a user off to re-enter a value they already have — forking it
 * across the keychain and the file store, the exact failure `CLAUDE.md`
 * warns about. "We could not look" and "it is not there" ask for opposite
 * actions, so they must never share a badge. */
export function badgeForRef(
  ref: SecretRefLike,
  projectId: string,
  entries: ReadonlyMap<string, SecretEntry>,
): RefBadge {
  const entry = entries.get(entryKey(entryOf(ref, projectId)));
  if (!entry) return 'unknown';
  const badge = badgeOf(entry);
  // Not a second derivation: `badgeOf` still decides everything about the
  // STORES. This only refuses to publish one of its answers — the confident
  // absence — for a ref whose declared location was never among them.
  if (badge === 'not-set' && declaresUnprobedLocation(ref)) return 'declared-elsewhere';
  return badge;
}

/** Column text per badge. */
export const BADGE_LABEL: Record<RefBadge, string> = {
  set: 'set',
  unset: 'paused',
  'file-store': 'set — file store',
  'shared-file-store': 'set — shared file store',
  'shared-opted-out': 'not read here',
  unknown: 'unknown',
  'not-set': 'not set',
  'declared-elsewhere': 'not checked',
};

/** Hover text per badge. `unknown` explicitly warns the reader off the
 * re-entry that a red "not set" would invite. */
export const BADGE_TITLE: Record<RefBadge, string> = {
  set: 'Present in the OS keychain and active for this project.',
  unset:
    'The keychain holds a value but it is paused for this project (by this launcher or another one sharing the keychain), and NO file-store copy serves the key either — so every reader sees it as unset. Reactivate it in the secrets panel to restore it.',
  'file-store':
    'Not in the keychain, but present in the ~/.vct-secrets file store — every sanctioned resolver finds it.',
  'shared-file-store':
    'Neither the keychain nor this project\u2019s own ~/.vct-secrets/projects/<NAME>/ holds it, but ~/.vct-secrets/shared/ does \u2014 which is the next place every sanctioned resolver looks, so the key resolves. The value is SHARED: deleting it breaks every other project that relies on it, and it stops reaching this project if you turn on \u201CDisable shared secrets\u201D above.',
  'shared-opted-out':
    '\u201CDisable shared secrets for this project\u201D is ON above, so this project does not read the shared tier at all \u2014 whatever the shared keychain bucket or ~/.vct-secrets/shared/ holds does not reach here. Nothing is paused and nothing needs re-entering: clear that checkbox and the key resolves again. Other projects are unaffected, and per-project and global secrets are untouched by the toggle.',
  unknown:
    'A store could not be read (locked keychain, unreadable secrets directory, or the probe has not returned). This is NOT the same as absent — do not re-enter the value on the strength of this badge.',
  'not-set':
    'Absent from the OS keychain and from both file-store tiers this project reads (~/.vct-secrets/projects/<NAME>/ and, unless disabled above, ~/.vct-secrets/shared/). The project\u2019s own .env is not checked here \u2014 the launcher has no lifecycle over that file.',
  'declared-elsewhere':
    'This reference resolves from a location the launcher does not manage \u2014 the file or environment variable named in the Resolution column \u2014 and nothing here was measured against it. The keychain and both ~/.vct-secrets tiers were checked and hold no copy, which is NOT evidence the key is missing: look at the declared location to know.',
};
