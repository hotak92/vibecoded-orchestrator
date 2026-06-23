// User-by-email lookup for the Lemon Squeezy webhook.
//
// Why this module exists (audit N1-1, 2026-06-23)
// ------------------------------------------------
// Both webhook code paths (order_created in index.ts, and the lifecycle
// handlers in orchestrator_additions.ts) need to resolve a paying
// customer's email → Supabase auth user id. The original implementation
// called `supabase.auth.admin.listUsers()` ONCE and `.find()`-ed the email
// client-side. `listUsers()` defaults to 50 users per page, so once the
// project's registered-user count crossed one page:
//   - order_created  → paying customers beyond page 1 SILENTLY failed tier
//                       activation (404 "User not found, they must register").
//   - cancel/refund/expire → lifecycle downgrades missed those same users,
//                       leaving refunded/cancelled customers on a paid tier.
//
// Constraints that rule out the obvious alternatives
// ---------------------------------------------------
// - @supabase/supabase-js@2's admin `listUsers()` has NO server-side email
//   filter parameter, so we cannot push the predicate to GoTrue.
// - The `profiles` table (20260418_profiles_schema.sql) has NO email column
//   (`profiles.id` = auth.users(id), plus name/apps/tier only), so we cannot
//   look the user up by email via PostgREST either.
//
// The fix: bounded pagination. Walk `listUsers({ page, perPage })` until the
// email is found, there is no next page, or a hard page cap is hit (runaway
// guard — never infinite-loop).
//
// Why termination is driven by the SDK's `nextPage`, NOT by page length
// ----------------------------------------------------------------------
// (audit follow-up A-1, 2026-06-23): an earlier draft terminated the walk on
// `users.length < USERS_PER_PAGE` — i.e. "a page shorter than requested means
// the last page". That couples termination to the REQUESTED perPage (1000).
// GoTrue clamps `per_page` SERVER-SIDE; if the effective server cap is ever
// < 1000, a genuinely-full page returns fewer rows than requested, the
// `< USERS_PER_PAGE` test fires on page 1, and the walk stops early —
// silently re-introducing the N1-1 "user beyond page 1 → 404" bug at a higher
// boundary. Instead we use the admin API's own pagination metadata: supabase-
// js v2 `auth.admin.listUsers()` returns `{ data: { users, nextPage,
// lastPage, total }, error }`, where `nextPage` is derived from GoTrue's
// `Link: rel="next"` header. `nextPage == null` means the SERVER says there
// are no more pages — clamp-proof, because the server decided the page size.
//
// The page-walk loop is kept network-free and injectable (the `fetchPage`
// callback) so it is unit-testable WITHOUT a live Supabase project, matching
// the pure-function test harness used across the other edge functions.

import type { SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

/** Users requested per `listUsers` page. The GoTrue admin API caps perPage
 *  server-side (historically 1000); we request the max so a typical project
 *  resolves any email in a single round-trip. */
export const USERS_PER_PAGE = 1000;

/**
 * Hard upper bound on pages walked before giving up. At USERS_PER_PAGE=1000
 * this covers 1,000,000 users — far beyond any realistic launcher tenant —
 * while guaranteeing the loop terminates even if the API misreports
 * pagination (e.g. always returns a non-null `nextPage`). A genuinely-absent
 * email exhausts the real pages first (`nextPage == null`) and returns null
 * long before this.
 */
export const MAX_PAGES = 1000;

/** Minimal shape of an auth user we depend on. */
export interface AuthUserLike {
  id: string;
  email?: string | null;
}

/**
 * One page of results, as returned by our `fetchPage` adapter.
 *
 * `nextPage` mirrors supabase-js v2's `auth.admin.listUsers()` response
 * (`{ data: { users, nextPage, lastPage, total } }`). It is the 1-indexed
 * number of the next page, or `null` when the server reports no further
 * pages. Termination keys off this (server-decided), NOT off `users.length`
 * vs the requested perPage (client-decided, clamp-fragile) — see the module
 * header for the A-1 rationale.
 */
export interface UserPage {
  users: AuthUserLike[];
  nextPage: number | null;
}

/**
 * Page fetcher. Returns one page of users (1-indexed `page`, `perPage`
 * users max) plus the SDK's `nextPage` cursor. Throwing propagates to
 * `findUserIdByEmailPaged`'s caller — a fetch error must NOT be silently
 * treated as "user not found", because that would re-introduce the
 * silent-activation-failure bug for transient errors. The Supabase-backed
 * adapter lives in `supabaseFetchPage`; tests inject a synchronous in-memory
 * fetcher.
 */
export type FetchPage = (
  page: number,
  perPage: number,
) => Promise<UserPage>;

/**
 * Walk paginated user listings until `email` is found or pages are exhausted.
 *
 * Returns the matched user's id, or null when no user has that email.
 *
 * Termination (no infinite loop):
 *   1. email found       → return that user's id.
 *   2. nextPage == null  → server says no more pages → return null.
 *   3. empty page        → no more users to scan     → return null.
 *   4. MAX_PAGES reached → runaway guard             → return null.
 *
 * We deliberately do NOT terminate on `users.length < perPage`: GoTrue clamps
 * `per_page` server-side, so a full page can be shorter than requested. Using
 * the SDK's `nextPage` cursor instead makes termination clamp-proof (A-1).
 *
 * Email comparison is exact (===), preserving the original `.find()`
 * semantics. Lemon Squeezy sends the same email the user registered with;
 * we do not case-fold (matching prior behaviour — a behavioural change here
 * would be out of scope for the audit fix).
 */
export async function findUserIdByEmailPaged(
  fetchPage: FetchPage,
  email: string,
): Promise<string | null> {
  for (let page = 1; page <= MAX_PAGES; page++) {
    const { users, nextPage } = await fetchPage(page, USERS_PER_PAGE);

    const match = users.find((u) => u.email === email);
    if (match) return match.id;

    // No further pages per the server's own cursor (clamp-proof), or this
    // page came back empty (nothing left to scan) → conclude not-found.
    if (nextPage == null || users.length === 0) return null;
  }
  // MAX_PAGES exhausted without a match or a null nextPage. Treat as
  // not-found rather than looping forever; see MAX_PAGES rationale above.
  return null;
}

/**
 * Supabase-backed adapter: build a `FetchPage` over `auth.admin.listUsers`.
 *
 * Surfaces the admin API error by throwing (so transient failures don't get
 * mistaken for "user absent"). The returned closure is what
 * `findUserIdByEmailPaged` walks.
 */
export function supabaseFetchPage(supabase: SupabaseClient): FetchPage {
  return async (page: number, perPage: number): Promise<UserPage> => {
    const { data, error } = await supabase.auth.admin.listUsers({
      page,
      perPage,
    });
    if (error) throw error;
    // supabase-js v2 returns `{ data: { users, nextPage, lastPage, total } }`.
    // `nextPage` (1-indexed, derived from GoTrue's `Link: rel="next"` header)
    // is the server's own "is there more?" cursor — clamp-proof. Coerce a
    // missing/undefined cursor to null so termination is unambiguous.
    const next = (data as { nextPage?: number | null } | null)?.nextPage;
    return {
      users: (data?.users ?? []) as AuthUserLike[],
      nextPage: next ?? null,
    };
  };
}

/**
 * Convenience: resolve email → user id over a live Supabase client using
 * bounded pagination. Throws if the admin API errors (caller maps to 500).
 *
 * Returns the promise directly (no async/await) — it's a thin adapter over
 * findUserIdByEmailPaged, which already returns Promise<string | null>.
 */
export function findUserIdByEmail(
  supabase: SupabaseClient,
  email: string,
): Promise<string | null> {
  return findUserIdByEmailPaged(supabaseFetchPage(supabase), email);
}
