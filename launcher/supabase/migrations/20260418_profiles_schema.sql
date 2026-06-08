-- 20260418_profiles_schema.sql
--
-- Profiles table schema. Reverse-engineered from `supabase-setup.sql` and
-- the runtime usage in `src/lib/stores/auth.ts` + `lemon-squeezy-webhook`.
-- Idempotent — safe to apply on a fresh project or on top of the
-- existing `supabase-setup.sql`.
--
-- This migration captures the BASE schema only. RLS policies are defined
-- separately in `20260418_license_rls.sql` so the security boundary is
-- reviewable in isolation.

-- 1. Profiles table -----------------------------------------------------------
create table if not exists public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  name        text,
  apps        text[]      not null default '{}',
  -- Orchestrator tier is set by the webhook based on the LS variant_id.
  -- Free users have no row entry change (default 'free'). Paid tiers gate
  -- feature access in the license validator.
  orchestrator_tier text   not null default 'free'
                           check (orchestrator_tier in ('free','pro','mao','enterprise')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- Add orchestrator_tier on existing deployments where the column doesn't yet
-- exist (e.g. environments provisioned via the legacy supabase-setup.sql).
alter table public.profiles
  add column if not exists orchestrator_tier text
    not null default 'free'
    check (orchestrator_tier in ('free','pro','mao','enterprise'));

-- 2. updated_at trigger -------------------------------------------------------
create or replace function public.update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists profiles_updated_at on public.profiles;
create trigger profiles_updated_at
  before update on public.profiles
  for each row execute function public.update_updated_at();

-- 3. Auto-create profile on signup -------------------------------------------
-- Runs as SECURITY DEFINER so it bypasses RLS (which forbids client INSERTs).
-- This is the ONLY way a profiles row gets created.
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, name, apps)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)),
    '{}'
  );
  return new;
end;
$$ language plpgsql security definer;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
