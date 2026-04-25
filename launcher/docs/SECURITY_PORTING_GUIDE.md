# Security Porting Guide — Svelte → React

**Context**: During the feature/orchestrator-hub work (16-18 April 2026)
we audited the license system and fixed three critical issues. When the
Svelte `auth.ts` / `licenses.ts` stores get rewritten as React Context /
hooks, **these security properties must be preserved**. This doc lists
each property, the attack it prevents, and what the React equivalent
must enforce.

If any of the following are NOT in the React version, we have a
production security regression.

---

## Property 1 — Dev-mode activation bypass is eliminated from prod bundles

### Attack it prevents
A user in a production build types `test-mao` (or any `test-<appId>`) in
the activation modal and gets the corresponding app for free.

### Required behavior
- Dev-mode test codes (`test-<appId>`) may only be accepted on **dev
  builds**, never on production builds.
- The check must use Vite's `import.meta.env.DEV`, which is a **static**
  boolean Vite inlines at build time (`true` in `vite dev`, `false` in
  `vite build`). The dead branch is then eliminated by the bundler; it
  physically cannot be reached in a prod build.
- Do NOT gate on the presence/value of any runtime env var
  (e.g. `VITE_LEMONSQUEEZY_API_KEY`). If the env var is missing in
  production, the dev path would silently activate.

### React equivalent
```tsx
async function activateCode(code: string) {
  if (import.meta.env.DEV) {
    // Dev-only: accept test-<appId> codes for QA
    const m = code.match(/^test-(\w+)$/);
    if (m && PRODUCT_MAP[m[1]]) { /* ...activate locally... */ return; }
  }
  // Production: must call LS (or our /validate-tier endpoint)
  if (!apiKey) throw new Error("License validation not configured.");
  // ...real call...
}
```

### How to verify
Run `vite build` and grep the resulting JS bundle for the string
`"test-"`. It should not appear. If it does, the branch wasn't
eliminated and the bypass is still reachable.

---

## Property 2 — The client cannot write to `profiles.apps` or `profiles.orchestrator_tier`

### Attack it prevents
A logged-in user opens DevTools and calls
`supabase.from('profiles').update({apps: ['mao'], orchestrator_tier: 'mao'})`
(or its React Context equivalent), giving themselves every paid app.

### Required behavior
1. **No client code** anywhere in the React app may update `profiles.apps`
   or `profiles.orchestrator_tier`. Period.
2. App activation happens **exclusively through the webhook**
   (`supabase/functions/lemon-squeezy-webhook`) which uses the service
   role key and bypasses RLS.
3. The React activation flow may touch local state (for immediate UI
   feedback) but must NOT write the entitlement columns to the DB. See
   `markAppActiveLocal` pattern in the Svelte version — the rename from
   `activateApp` was deliberate to flag that this is UI-only.
4. To reflect the post-webhook state, the client should call a
   `refreshProfile()` that does a `SELECT` (never `UPDATE/UPSERT`).

### Database-side defense
Migration `20260418_license_rls.sql` enforces this at the DB level:
```sql
CREATE POLICY "update_own_name" ON public.profiles
  FOR UPDATE USING (auth.uid() = id)
  WITH CHECK (
    auth.uid() = id
    AND apps              IS NOT DISTINCT FROM (SELECT apps FROM profiles WHERE id = auth.uid())
    AND orchestrator_tier IS NOT DISTINCT FROM (SELECT orchestrator_tier FROM profiles WHERE id = auth.uid())
  );
```
Any client-initiated UPDATE that attempts to change `apps` or
`orchestrator_tier` is rejected by Postgres, regardless of what the
client code tries to do. This is the belt-and-braces; the React code
should still not attempt the write.

### How to verify
1. `grep -r "from('profiles')" src/` — every hit should be `.select(...)`
   or `.update({ name: ... })`. Never `.update({ apps: ... })` or
   `.update({ orchestrator_tier: ... })`.
2. Manual: in the running app, paste into the browser DevTools console:
   ```js
   await supabase.from('profiles').update({ apps: ['mao'] }).eq('id', user.id)
   ```
   Should return an error (RLS violation) or silently no-op.

---

## Property 3 — Webhook refuses unsigned requests

### Attack it prevents
Anyone who discovers the webhook URL POSTs a forged `order_created`
event with any email + variant_id and gets that app activated for any
account.

### Required behavior
The webhook must:
1. **Fail hard (500)** if `LEMON_SQUEEZY_WEBHOOK_SECRET` is not set in
   the Supabase function env. No signature verification is also NOT
   acceptable — the webhook must refuse to run.
2. **Require** the `x-signature` header on every request. Missing = 401.
3. Use **constant-time comparison** (`timingSafeEqual`) for the HMAC
   check, never `===`.

### Location
Already in `supabase/functions/lemon-squeezy-webhook/index.ts`. Does
NOT touch the frontend, so the React migration shouldn't affect it.

### How to verify
After deploy:
```bash
# Should return 401 (no signature)
curl -X POST "$WEBHOOK_URL" -d '{"meta":{"event_name":"order_created"}}'

# Should return 401 (bad signature)
curl -X POST "$WEBHOOK_URL" -H "x-signature: wrong" -d '{}'
```

---

## Property 4 — Log discipline: secrets never logged

### Attack it prevents
Log file exfiltration exposing license keys or the LS API key.

### Required behavior
- Never log a full `license_key`. Mask to 8 chars max (see `maskKey`
  helper in `validate-tier/index.ts`).
- Never log `LEMON_SQUEEZY_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, or
  Supabase JWTs.
- In the React client, never `console.log(code)` where `code` is the
  activation code the user typed. It's a credential until validated.
- The coord MCP server already scrubs env in the env-scrub commit —
  keep that behavior.

---

## Property 5 — Machine binding via LS instance activation

### Attack it prevents
A paying customer shares their license key with 100 friends.

### Required behavior
1. `validate-tier` edge function calls LS `/v1/licenses/activate` with
   `instance_name=machine_id_hash` (sha256 of MAC address, computed
   client-side and sent as plain hex — never the raw hardware ID).
2. LS enforces per-product activation limits (configured in the LS
   dashboard: 2 for Pro, 3 for MAO).
3. On 422 from LS (limit exceeded), the edge function returns
   `{error: "instance_limit"}` and the client shows a deactivation link.

### Where
Already in `validate-tier/index.ts`. No frontend change needed.

---

## Property 6 — Per-instance auth isolation (future multi-tenant coord)

### Attack it prevents (applies when coordination-managed ships)
Team A reading Team B's messages because both share the same managed
Supabase.

### Required behavior
When the managed coordination MCP ships (see
`Claude/docs/COORDINATION_ARCHITECTURE.md` in the orchestrator repo),
every coordination table must have a `team_id` column and RLS policies
that filter by `auth.uid()`'s team memberships. The React launcher UI
must never pass `team_id` as a filter that the client can tamper with —
filtering is an RLS concern.

Not yet applicable to the launcher; documented here so whoever lands
that work later doesn't need to rediscover the pattern.

---

## Checklist before merging the React rewrite to master

- [ ] `grep -r "test-" src/` — no prod code reachable path
- [ ] `grep -rE "\.update\(\{[^}]*apps" src/` — zero hits
- [ ] `grep -rE "\.update\(\{[^}]*orchestrator_tier" src/` — zero hits
- [ ] `grep -rE "\.upsert\([^)]*apps" src/` — zero hits
- [ ] Webhook still fails closed on missing secret (untouched is fine)
- [ ] All RLS migrations applied on the production Supabase project
- [ ] `curl` tests against the deployed webhook return 401 without
      valid signature
- [ ] No full license keys in any `console.log`
- [ ] Same for any Supabase JWT

---

## Full audit report

For the reasoning behind each property, numerical threat assessment,
and the initial findings: see
`Claude/docs/LICENSE_BACKEND_SECURITY_REVIEW.md` (in the Claude
Orchestrator meta-project repo — not pushed to VCT-Launcher).

Follow-up audit with merge notes:
`Claude/docs/LICENSE_BACKEND_REVIEW_2026-04-18.md`.
