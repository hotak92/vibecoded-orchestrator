-- 20260623000000_storage_paid_module_catalog_rls.sql
--
-- v0.2.65 (RLS-1 audit follow-up): assert the Storage bucket + RLS policy for
-- `paid-module-catalog` in source control.
--
-- =====================================================================
-- WHY THIS EXISTS
-- =====================================================================
-- The paid-module Storage posture was applied to the canonical Supabase project
-- via the dashboard and had NO counterpart in launcher/supabase/migrations/
-- (post-0.2.64 RLS/authz audit, finding RLS-1). A LIVE audit on 2026-06-23
-- against project ovpdtijpdchzlxbojhsg ("Orchestrator sync layer") found:
--
--   * storage.buckets contains exactly ONE bucket: `paid-module-catalog`,
--     public = false (PRIVATE). RLS is enabled on storage.objects + storage.buckets
--     (relrowsecurity = true, verified live).
--   * The only storage.objects policy is `service_role_full_access_paid_module_catalog`
--     (service_role, ALL, scoped to bucket_id = 'paid-module-catalog').
--   * NO anon/authenticated SELECT policy exists on any bucket → nothing is
--     anonymously readable. The launcher reads the catalog via the `module-catalog`
--     edge function (service-role, server-side); anonymous callers never touch the
--     bucket. So PRIVATE is correct — NOT "public by design" as the audit assumed.
--   * The buckets `paid-module-weights`, `paid-module-distribution`,
--     `paid-module-repo` DO NOT EXIST in this project (see the NOT-created note).
--
-- This migration makes that verified-secure state idempotently reproducible from
-- the repo. It is a reconciliation: re-running it on a project that already
-- matches is a no-op.
-- =====================================================================

-- 1) The catalog bucket: ensure it exists and is PRIVATE.
--    The catalog payload is non-sensitive ("marketing-tile" metadata) but it is
--    only ever served via the module-catalog edge function, never a public URL.
insert into storage.buckets (id, name, public)
values ('paid-module-catalog', 'paid-module-catalog', false)
on conflict (id) do update set public = false;

-- 2) The service-role policy on storage.objects for that bucket.
--    Dropped-then-created so the migration is idempotent and the policy text is
--    the single source of truth. No anon/authenticated policy is created — the
--    bucket is reachable only by service_role (the module-catalog edge function).
drop policy if exists "service_role_full_access_paid_module_catalog" on storage.objects;
create policy "service_role_full_access_paid_module_catalog"
  on storage.objects
  for all
  to service_role
  using (bucket_id = 'paid-module-catalog')
  with check (bucket_id = 'paid-module-catalog');

-- RLS note: storage.objects + storage.buckets already have row-level security
-- ENABLED (Supabase default; verified live 2026-06-23). No
-- `alter table ... enable row level security` is issued here, to avoid an
-- ownership error on `supabase db push` — the policy above is the load-bearing
-- assertion, and it is meaningless unless RLS is on.

-- =====================================================================
-- NOT created here (deliberate) — surfaced by the same audit:
--
--   * `paid-module-weights` — the rl-latest-weights edge function signs URLs for
--     this bucket (WEIGHTS_BUCKET default "paid-module-weights") and
--     public.paid_module_releases stores storage_path into it. The design intends
--     it PRIVATE (signed-URL only). It DOES NOT EXIST live today because the
--     default-weights auto-download flow is deferred (RL ships weights baked into
--     the GHCR image). When that flow is enabled, create the bucket PRIVATE with a
--     service-role-only policy — signed URLs bypass RLS via the signing key, so NO
--     anon/authenticated SELECT is needed:
--
--       insert into storage.buckets (id, name, public)
--       values ('paid-module-weights', 'paid-module-weights', false)
--       on conflict (id) do update set public = false;
--       drop policy if exists "service_role_full_access_paid_module_weights" on storage.objects;
--       create policy "service_role_full_access_paid_module_weights"
--         on storage.objects for all to service_role
--         using (bucket_id = 'paid-module-weights')
--         with check (bucket_id = 'paid-module-weights');
--
--   * `paid-module-distribution`, `paid-module-repo` — named in the audit but
--     have NO code references in this repo (no edge function signs URLs for them,
--     no table stores paths into them) and DO NOT EXIST live. Left uncreated
--     pending confirmation they are actually planned. If introduced later, create
--     them PRIVATE with the same service-role-only pattern as paid-module-weights.
-- =====================================================================
