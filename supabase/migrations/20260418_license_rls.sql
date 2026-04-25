-- 20260418_license_rls.sql
--
-- Row-level security policies for `public.profiles`.
--
-- Threat model: a logged-in user with DevTools could otherwise call
-- `supabase.from('profiles').upsert({id, apps: ['mao', 'orchestrator']})`
-- and grant themselves any paid app. The policies below restrict client
-- writes to the `name` column only; `apps` and `orchestrator_tier` are
-- writable exclusively by the service_role key used by the
-- lemon-squeezy-webhook edge function.
--
-- This migration SUPERSEDES the loose UPDATE policy created by
-- `supabase-setup.sql` ("Users can update own profile"). We drop the
-- legacy policies first so re-applying is idempotent.

alter table public.profiles enable row level security;

-- Drop legacy permissive policies from supabase-setup.sql -----------------
drop policy if exists "Users can read own profile"   on public.profiles;
drop policy if exists "Users can insert own profile" on public.profiles;
drop policy if exists "Users can update own profile" on public.profiles;
-- Drop our own (in case migration is re-run)
drop policy if exists "read_own_profile"  on public.profiles;
drop policy if exists "update_own_name"   on public.profiles;

-- SELECT: users may read their own row only -------------------------------
create policy "read_own_profile" on public.profiles
  for select
  using (auth.uid() = id);

-- UPDATE: users may update their own row, but the `apps` and
-- `orchestrator_tier` columns must be unchanged. The WITH CHECK clause
-- compares the post-image to the existing row; any attempt to modify
-- entitlement columns is rejected by Postgres.
create policy "update_own_name" on public.profiles
  for update
  using (auth.uid() = id)
  with check (
    auth.uid() = id
    and apps              is not distinct from (select apps              from public.profiles where id = auth.uid())
    and orchestrator_tier is not distinct from (select orchestrator_tier from public.profiles where id = auth.uid())
  );

-- INSERT: no client INSERT allowed. The `handle_new_user` trigger creates
-- the row server-side under SECURITY DEFINER. Absence of a policy = denied.

-- DELETE: no client DELETE allowed. ON DELETE CASCADE from auth.users
-- handles cleanup when an account is removed by Supabase Auth.

-- service_role bypasses RLS entirely (this is built into Supabase). The
-- webhook uses SUPABASE_SERVICE_ROLE_KEY → it can write `apps` and
-- `orchestrator_tier` freely. No additional policy needed for it.
