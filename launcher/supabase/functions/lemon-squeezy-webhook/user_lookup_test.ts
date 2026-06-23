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
 */
function pagedFetcher(
  total: number,
  pagesSeen: number[],
): FetchPage {
  const all = Array.from({ length: total }, (_, i) => user(i + 1));
  return (page: number, perPage: number) => {
    pagesSeen.push(page);
    const start = (page - 1) * perPage;
    return Promise.resolve({ users: all.slice(start, start + perPage) });
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

// ─── Termination guarantees ─────────────────────────────────────────────────

Deno.test("findUserIdByEmailPaged: MAX_PAGES guard halts a misbehaving API that always returns a full page", async () => {
  // Pathological fetcher: ALWAYS returns a full page (never a short one) and
  // never contains the target. Without the MAX_PAGES guard this would loop
  // forever; with it, the walk must terminate and return null.
  const seen: number[] = [];
  const fullPage: AuthUserLike[] = Array.from(
    { length: USERS_PER_PAGE },
    (_, i) => user(i + 1),
  );
  const fetch: FetchPage = (page) => {
    seen.push(page);
    return Promise.resolve({ users: fullPage });
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
      return Promise.resolve({
        users: Array.from({ length: USERS_PER_PAGE }, (_, i) => user(i + 1)),
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
    });
  const id = await findUserIdByEmailPaged(fetch, "target@example.com");
  assertEquals(id, "c");
});
