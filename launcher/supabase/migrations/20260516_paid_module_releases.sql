-- 20260516_paid_module_releases.sql
--
-- Phase 3C (v0.2.21): the `paid_module_releases` table that the
-- /rl-latest-version edge function reads to answer "is there a newer
-- weights snapshot for module X / embedding source Y than what the
-- client has?".
--
-- Consumed by:
--   - launcher/supabase/functions/rl-latest-version/index.ts (edge function)
--   - Stream B's launcher Rust commands (poller side) — via that function.
--
-- Schema decisions:
--   - One row per (module_id, embedding_source, version) snapshot.
--   - `is_latest` is a denormalized "is this the current head for the
--     (module_id, embedding_source) pair?" boolean. Maintained by the
--     trigger below so an INSERT/UPDATE that flips a row to
--     is_latest=true automatically demotes the previous head. Trades a
--     small write-time cost for a read-time hot path that uses the
--     partial index `paid_module_releases_latest`.
--   - `storage_path` is the path inside the private `paid-module-weights`
--     Supabase Storage bucket. The edge function generates a signed URL
--     for it on demand (15-minute TTL).
--   - `sha256` is the .pt checksum. Empty string means "skip
--     verification" (used during bootstrap before Martino uploads the
--     real artifact). Clients should warn-but-allow on empty sha256.
--   - `notes` is markdown ≤500 chars; the wire contract caps it.
--   - No FK to a `paid_modules` table — we don't currently have one,
--     and module_id is just a slug. The edge function rejects unknown
--     pairs with a server-side discovery list (no client-side enum).
--
-- RLS posture:
--   - Service role only. The /rl-latest-version edge function runs
--     under the service role and reads the table directly. No anon or
--     authenticated read/write paths.

create table if not exists public.paid_module_releases (
  id              bigserial primary key,
  module_id       text not null,
  embedding_source text not null,
  version         text not null,
  storage_path    text not null,
  sha256          text not null default '',
  released_at     timestamptz not null default now(),
  notes           text not null default '',
  is_latest       boolean not null default true
);

-- Uniqueness on (module_id, embedding_source, version).
-- A given module + embedding_source can never re-use the same version
-- string (versions are date-stamped and monotonic by convention).
create unique index if not exists paid_module_releases_uniq
  on public.paid_module_releases (module_id, embedding_source, version);

-- Hot-path index: every edge-function lookup filters on
-- (module_id, embedding_source) WHERE is_latest = true. A partial
-- index on just the latest rows keeps the scan tiny even as the table
-- grows with historical releases.
create index if not exists paid_module_releases_latest
  on public.paid_module_releases (module_id, embedding_source)
  where is_latest = true;

-- Trigger: when an INSERT or UPDATE marks a row as is_latest=true,
-- demote every OTHER row for the same (module_id, embedding_source)
-- pair to is_latest=false. This keeps "exactly one latest per pair" as
-- a denormalization invariant without forcing the writer to remember
-- to flip the predecessor.
--
-- The trigger runs AFTER INSERT OR UPDATE OF is_latest because:
--   - AFTER avoids re-firing during the cascading UPDATE this function
--     itself issues (the WHEN clause filters by new.is_latest=true on
--     the inbound row, and the cascading UPDATE only sets is_latest to
--     false, so it can never re-trigger this).
--   - UPDATE OF is_latest narrows the firing surface to writes that
--     actually changed the flag, not every UPDATE on the table.
create or replace function public.paid_module_releases_set_latest()
returns trigger language plpgsql as $$
begin
  if new.is_latest then
    update public.paid_module_releases
    set is_latest = false
    where module_id = new.module_id
      and embedding_source = new.embedding_source
      and id <> new.id;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_paid_module_releases_set_latest on public.paid_module_releases;
create trigger trg_paid_module_releases_set_latest
  after insert or update of is_latest on public.paid_module_releases
  for each row when (new.is_latest = true)
  execute function public.paid_module_releases_set_latest();

-- RLS: deny anon + authenticated; service role only.
-- No policies are created, so all non-service-role reads return zero
-- rows. The /rl-latest-version edge function uses the service-role
-- key and bypasses RLS entirely.
alter table public.paid_module_releases enable row level security;

-- Bootstrap row for vct-rl-reranker arctic v0.1.0.
-- sha256 is left empty because Martino hasn't uploaded the .pt to
-- Storage yet; the edge function will treat empty sha256 as "skip
-- verification" (client will warn but allow). Replace with the real
-- sha256 (`shasum -a 256 rl_model_arctic_1024.pt`) before public
-- release, via:
--
--   update public.paid_module_releases
--   set sha256 = '<hex64>'
--   where module_id = 'vct-rl-reranker'
--     and embedding_source = 'arctic'
--     and version = 'arctic-2026-05-19';
insert into public.paid_module_releases (module_id, embedding_source, version, storage_path, sha256, notes)
values ('vct-rl-reranker', 'arctic', 'arctic-2026-05-19', 'vct-rl-reranker/arctic/2026-05-19/rl_model_arctic_1024.pt', '', 'Initial arctic v0.1.0 trained 2026-05-19. F1 +0.39pp on held-out TEST set.')
on conflict (module_id, embedding_source, version) do nothing;
