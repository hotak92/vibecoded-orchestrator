// Unit tests for the bounded-pagination user lookup (audit N1-1).
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/lemon-squeezy-webhook/user_lookup_test.ts
//
// Same harness style as _shared/variant_map_test.ts — no Supabase project,
// no network. We inject an in-memory `FetchPage` so the page-walk logic is
// exercised deterministically. We CANNOT exercise `supabaseFetchPage` /
// `findUserIdByEmail` against a live GoTrue admin API here; those are thin
// adapters whose only logic is "throw on error, map data.users". The
// page-walk loop (the part that actually fixes the bug) is fully covered.

import {
  assertEquals,
  assertRejects,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  type AuthUserLike,
  type FetchPage,
  findUserIdByEmailPaged,
  MAX_PAGES,
  USERS_PER_PAGE,
} from "./user_lookup.ts";

// ─── Test helpers ─────────────────────────────────────────────────────────

/** Build a synthetic user with a derived email. */
function user(n: number): AuthUserLike {
  return { id: `id-${n}`, email: `user${n}@example.com` };
}

/**
 * Build an in-memory FetchPage over `total` synthetic users, slicing them
 * into pages of `perPage`. Records every page index requested in `pagesSeen`
 * so tests can assert the walk stopped early (didn't fetch every page).
 *
 * `nextPage` is derived the way GoTrue does it: non-null (the next page
 * number) whenever there are more users past the current slice, null on the
 * final/empty page. This models the SDK's server-decided cursor — the field
 * `findUserIdByEmailPaged` now keys termination off of.
 */
function pagedFetcher(
  total: number,
  pagesSeen: number[],
): FetchPage {
  const all = Array.from({ length: total }, (_, i) => user(i + 1));
  return (page: number, perPage: number) => {
    pagesSeen.push(page);
    const start = (page - 1) * perPage;
    const users = all.slice(start, start + perPage);
    // More users remain iff the next slice would be non-empty.
    const hasMore = start + perPage < total;
    return Promise.resolve({ users, nextPage: hasMore ? page + 1 : null });
  };
}

// ─── Single-page cases ──────────────────────────────────────────────────────

Deno.test("findUserIdByEmailPaged: finds a user on the only (short) page", async () => {
  const seen: number[] = [];
  const fetch = pagedFetcher(3, seen); // 3 users < perPage → one short page
  const id = await findUserIdByEmailPaged(fetch, "user2@example.com");
  assertEquals(id, "id-2");
  // Found on page 1; must not request a second page.
  assertEquals(seen, [1]);
});

Deno.test("findUserIdByEmailPaged: returns null when email absent (short page)", async () => {
  const seen: number[] = [];
  const fetch = pagedFetcher(3, seen);
  const id = await findUserIdByEmailPaged(fetch, "ghost@example.com");
  assertEquals(id, null);
  // One short page is enough to conclude not-found — no extra page walk.
  assertEquals(seen, [1]);
});

Deno.test("findUserIdByEmailPaged: empty project returns null after one page", async () => {
  const seen: number[] = [];
  const fetch = pagedFetcher(0, seen);
  const id = await findUserIdByEmailPaged(fetch, "anyone@example.com");
  assertEquals(id, null);
  assertEquals(seen, [1]);
});

// ─── Multi-page cases (the actual bug) ──────────────────────────────────────

Deno.test("findUserIdByEmailPaged: finds a user BEYOND the first full page", async () => {
  // total = exactly 2 full pages + a short third. The target sits on page 2,
  // which the old single-listUsers().find() would never have reached.
  const total = USERS_PER_PAGE * 2 + 5;
  const targetIndex = USERS_PER_PAGE + 7; // 1-based user number on page 2
  const seen: number[] = [];
  const fetch = pagedFetcher(total, seen);

  const id = await findUserIdByEmailPaged(
    fetch,
    `user${targetIndex}@example.com`,
  );
  assertEquals(id, `id-${targetIndex}`);
  // Walked page 1 (full) then page 2 (where the match is); stopped there.
  assertEquals(seen, [1, 2]);
});

Deno.test("findUserIdByEmailPaged: walks to the final short page for a last-page user", async () => {
  const total = USERS_PER_PAGE * 2 + 3; // pages: full, full, short(3)
  const lastUser = total; // 1-based, on the short page 3
  const seen: number[] = [];
  const fetch = pagedFetcher(total, seen);

  const id = await findUserIdByEmailPaged(fetch, `user${lastUser}@example.com`);
  assertEquals(id, `id-${lastUser}`);
  assertEquals(seen, [1, 2, 3]);
});

Deno.test("findUserIdByEmailPaged: absent email across multiple full pages returns null", async () => {
  const total = USERS_PER_PAGE * 2 + 1; // full, full, short(1)
  const seen: number[] = [];
  const fetch = pagedFetcher(total, seen);

  const id = await findUserIdByEmailPaged(fetch, "ghost@example.com");
  assertEquals(id, null);
  // Must walk all three pages (two full, one short) before concluding.
  assertEquals(seen, [1, 2, 3]);
});

// ─── A-1 regression: server clamps perPage below the requested value ────────

Deno.test("findUserIdByEmailPaged: continues past a SHORT-but-not-last page when nextPage is non-null (A-1 clamp-proof)", async () => {
  // This is the exact failure the old `users.length < USERS_PER_PAGE`
  // heuristic had. GoTrue clamped per_page server-side, so every page comes
  // back with FEWER rows than we requested (USERS_PER_PAGE=1000), even though
  // more pages exist. The target user lives on page 2. The old heuristic
  // would have seen page 1 (e.g. 50 < 1000) and bailed to null → 404. With
  // nextPage-driven termination the walk MUST proceed to page 2 and find it.
  const SERVER_CLAMP = 50; // effective server-side page size < requested
  const target: AuthUserLike = { id: "id-target", email: "target@example.com" };
  const seen: number[] = [];
  const fetch: FetchPage = (page, perPage) => {
    // Sanity: caller still requests the full perPage; the server is what
    // shrinks it. This documents that the short page is NOT caller-driven.
    assertEquals(perPage, USERS_PER_PAGE);
    seen.push(page);
    if (page === 1) {
      // A full (server-clamped) page: 50 unrelated users, < requested 1000,
      // but the server says there IS a next page.
      const users = Array.from({ length: SERVER_CLAMP }, (_, i) => user(i + 1));
      return Promise.resolve({ users, nextPage: 2 });
    }
    // Page 2: contains the target, and the server reports no more pages.
    return Promise.resolve({ users: [target], nextPage: null });
  };

  const id = await findUserIdByEmailPaged(fetch, "target@example.com");
  assertEquals(id, "id-target");
  // Crucially the walk did NOT stop after the short page 1.
  assertEquals(seen, [1, 2]);
});

Deno.test("findUserIdByEmailPaged: short page with nextPage==null terminates as not-found (no over-walk)", async () => {
  // Mirror of the above for the absent-email case: a single short, clamped
  // page that the server marks as the last (nextPage null) → null after one
  // fetch, no spurious page-2 request.
  const seen: number[] = [];
  const fetch: FetchPage = (page) => {
    seen.push(page);
    return Promise.resolve({
      users: Array.from({ length: 50 }, (_, i) => user(i + 1)),
      nextPage: null,
    });
  };
  const id = await findUserIdByEmailPaged(fetch, "ghost@example.com");
  assertEquals(id, null);
  assertEquals(seen, [1]);
});

// ─── Termination guarantees ─────────────────────────────────────────────────

Deno.test("findUserIdByEmailPaged: MAX_PAGES guard halts a misbehaving API that never reports a null nextPage", async () => {
  // Pathological fetcher: ALWAYS returns a full page AND always claims there
  // is a next page (nextPage never null), and never contains the target.
  // Without the MAX_PAGES guard this would loop forever; with it, the walk
  // must terminate and return null.
  const seen: number[] = [];
  const fullPage: AuthUserLike[] = Array.from(
    { length: USERS_PER_PAGE },
    (_, i) => user(i + 1),
  );
  const fetch: FetchPage = (page) => {
    seen.push(page);
    return Promise.resolve({ users: fullPage, nextPage: page + 1 });
  };

  const id = await findUserIdByEmailPaged(fetch, "never@example.com");
  assertEquals(id, null);
  // Exactly MAX_PAGES pages walked, then bailed.
  assertEquals(seen.length, MAX_PAGES);
  assertEquals(seen[0], 1);
  assertEquals(seen[seen.length - 1], MAX_PAGES);
});

// ─── Error propagation (don't mistake a fetch error for not-found) ──────────

Deno.test("findUserIdByEmailPaged: a fetch error propagates (not swallowed as not-found)", async () => {
  const fetch: FetchPage = () => Promise.reject(new Error("admin API down"));
  await assertRejects(
    () => findUserIdByEmailPaged(fetch, "user1@example.com"),
    Error,
    "admin API down",
  );
});

Deno.test("findUserIdByEmailPaged: error on a LATER page also propagates", async () => {
  const fetch: FetchPage = (page) => {
    if (page === 1) {
      // Full page AND a non-null nextPage → the walk must advance to page 2,
      // where the fetch rejects. Proves the error path is reached via the
      // nextPage cursor (not short-circuited by a length heuristic).
      return Promise.resolve({
        users: Array.from({ length: USERS_PER_PAGE }, (_, i) => user(i + 1)),
        nextPage: 2,
      });
    }
    return Promise.reject(new Error("transient on page 2"));
  };
  await assertRejects(
    () => findUserIdByEmailPaged(fetch, "ghost@example.com"),
    Error,
    "transient on page 2",
  );
});

// ─── Exact-match semantics (preserve prior .find() behaviour) ───────────────

Deno.test("findUserIdByEmailPaged: email match is exact (no case folding)", async () => {
  const seen: number[] = [];
  const fetch = pagedFetcher(3, seen);
  // Uppercased variant must NOT match the lowercase stored email.
  const id = await findUserIdByEmailPaged(fetch, "USER2@EXAMPLE.COM");
  assertEquals(id, null);
});

Deno.test("findUserIdByEmailPaged: skips users with null/undefined email", async () => {
  const fetch: FetchPage = () =>
    Promise.resolve({
      users: [
        { id: "a", email: null },
        { id: "b" }, // email undefined
        { id: "c", email: "target@example.com" },
      ],
      nextPage: null, // single final page
    });
  const id = await findUserIdByEmailPaged(fetch, "target@example.com");
  assertEquals(id, "c");
});
