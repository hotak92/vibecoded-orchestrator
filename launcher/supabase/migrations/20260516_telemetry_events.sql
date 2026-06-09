-- 20260516_telemetry_events.sql
--
-- Phase 2B (2026-05-16): create the telemetry_events table that the
-- /telemetry edge function inserts into. Consumed by:
--   - VCThelpers/telemetry/uploader.py::upload_pending() (client)
--   - launcher/supabase/functions/telemetry/index.ts (edge function)
--   - .claude/hooks/session-stop-telemetry-upload.sh (trigger)
--
-- =====================================================================
-- V52-G REWRITE (2026-06-09) — DRIFT-TOLERANT IDEMPOTENT VARIANT
-- =====================================================================
--
-- Why this file was rewritten:
--
-- Discovered during the 2026-06-09 paid_module_releases deploy that the
-- deployed `telemetry_events` table predates this file and is missing
-- columns this migration originally assumed (most notably
-- `server_received_at`, used by index
-- `telemetry_events_event_type_received_idx` + by GDPR/abuse queries).
-- The original `CREATE INDEX` on an absent column made `supabase db push`
-- fail every time, blocking unrelated migrations (e.g. paid_module_releases).
--
-- Rather than reconcile via destructive DROP + recreate (which would
-- erase 277+ already-collected production events as of 2026-06-05), we
-- rewrite this migration as a fully-idempotent ALTER + CREATE-INDEX-IF-NOT-EXISTS
-- script. It is safe to run against:
--
--   (a) a brand-new database — the `create table if not exists` block
--       below produces the full target schema in one shot.
--   (b) a deployed database that already has SOME of these columns —
--       the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block adds only
--       the missing ones. Columns present with the right types are
--       left untouched.
--   (c) a deployed database that already has ALL columns — every
--       statement is a no-op. Safe to re-run.
--
-- ASSUMPTION: the columns the edge function inserts into
-- (`event_type`, `client_created_at`, `client_timestamp`, `machine_hash`,
-- `orchestrator_version`, `os_name`, `os_version`, `python_version`,
-- `user_id_sha256`, `payload`) DO exist on the deployed table. The drift
-- is specifically around `server_received_at` + the indexes that depend
-- on it. If MORE columns are missing on the deployed table, the
-- `ADD COLUMN IF NOT EXISTS` block below will reconcile them too — but
-- if the deployed table is missing a NOT-NULL column with no default,
-- the ADD COLUMN will fail on existing rows. None of the columns below
-- are NOT NULL without a default for exactly this reason.
--
-- Schema decisions (unchanged from original 2026-05-16 design):
--   - One row per event. Bulk-insert from the edge function.
--   - `payload` is jsonb so event-specific shape varies per event_type
--     without schema churn. The edge function validates event_type
--     against an allowlist; PG enforces only column types.
--   - `user_id_sha256` (Phase 2C) is indexed for GDPR data-deletion:
--     a user requests deletion → server computes SHA256 of their
--     license key → DELETE FROM telemetry_events WHERE user_id_sha256 = ?.
--   - `client_created_at` is the client's reported timestamp (when
--     the event was enqueued). `server_received_at` is the PG-side
--     `now()` (when the row was inserted). The two together let us
--     measure client→server latency + detect clock skew.
--   - No FK to a `users` table because we don't have one: license keys
--     live in Lemon Squeezy, not Supabase. SHA256 is opaque.

-- ─── Table (no-op if already exists) ───────────────────────────────────
create table if not exists public.telemetry_events (
  id                    bigserial primary key,
  event_type            text not null,
  client_created_at     timestamptz not null,
  client_timestamp      timestamptz,
  server_received_at    timestamptz not null default now(),

  -- Machine + version envelope (TelemetryEvent fields).
  machine_hash          text not null,
  orchestrator_version  text not null default '',
  os_name               text not null default '',
  os_version            text not null default '',
  python_version        text not null default '',

  -- Phase 2C: GDPR data-deletion handle. Empty string for free-tier
  -- users (no license key). SHA256(license_key.utf8) when present.
  user_id_sha256        text not null default '',

  -- Event-specific payload. Schema validated at the edge function
  -- (event_type allowlist) but stored as opaque jsonb here.
  payload               jsonb not null default '{}'::jsonb
);

-- ─── Reconcile drifted columns on existing deployments ─────────────────
--
-- These `ADD COLUMN IF NOT EXISTS` statements are the V52-G fix. On a
-- fresh DB they're no-ops (the `create table if not exists` above already
-- produced these columns). On the drifted production DB they add the
-- columns the original migration assumed existed but didn't.
--
-- Every column has a default so the ADD COLUMN succeeds against tables
-- that already have rows. `server_received_at`'s default of `now()`
-- means existing rows get the time-of-migration as their server_received_at
-- (best-effort; we lost the actual receive time for legacy rows but at
-- least the column becomes queryable for new rows + index creation).

alter table public.telemetry_events
  add column if not exists client_created_at    timestamptz;

alter table public.telemetry_events
  add column if not exists client_timestamp     timestamptz;

alter table public.telemetry_events
  add column if not exists server_received_at   timestamptz not null default now();

alter table public.telemetry_events
  add column if not exists orchestrator_version text not null default '';

alter table public.telemetry_events
  add column if not exists os_name              text not null default '';

alter table public.telemetry_events
  add column if not exists os_version           text not null default '';

alter table public.telemetry_events
  add column if not exists python_version       text not null default '';

alter table public.telemetry_events
  add column if not exists user_id_sha256       text not null default '';

alter table public.telemetry_events
  add column if not exists payload              jsonb not null default '{}'::jsonb;

-- ─── Indexes (idempotent) ──────────────────────────────────────────────
--
-- All `create index if not exists` — safe to re-run; the original V52-G
-- failure mode was specifically the (a) index trying to reference a
-- column that didn't exist yet. With the ADD COLUMN IF NOT EXISTS block
-- above, the column is guaranteed present before we get here.
--
--   (a) Most-recent events by type: dashboard "what's been collected"
create index if not exists telemetry_events_event_type_received_idx
  on public.telemetry_events (event_type, server_received_at desc);

--   (b) GDPR data-deletion lookup by user. Conditional partial index —
--       skips the huge majority of rows where user_id_sha256 is empty
--       (free-tier no-key events).
create index if not exists telemetry_events_user_id_idx
  on public.telemetry_events (user_id_sha256)
  where user_id_sha256 <> '';

--   (c) Per-machine breakdown for abuse-detection + rate-limit-by-machine.
create index if not exists telemetry_events_machine_hash_idx
  on public.telemetry_events (machine_hash, server_received_at desc);

-- ─── RLS ─────────────────────────────────────────────────────────────────
--
-- The /telemetry edge function uses the service-role key for inserts,
-- so it bypasses RLS by design. We enable RLS anyway to deny ALL
-- access from anon/authenticated keys — telemetry is service-role-only.
-- (If we ever want to expose a "show me my telemetry" UI, we'd add a
-- policy keyed on user_id_sha256 matching a logged-in user's hash.)
--
-- `enable row level security` is idempotent — re-running has no effect
-- if RLS is already on.

alter table public.telemetry_events enable row level security;

-- No grants to anon or authenticated. Service role bypasses RLS.

-- ─── Retention ──────────────────────────────────────────────────────────
--
-- Defer auto-retention to a future migration. For v0.1.0 we keep all
-- events indefinitely so the offline RL retrainer has full history.
-- Once the table grows past ~10M rows we should add a pg_cron job:
--   DELETE FROM telemetry_events WHERE server_received_at < now() - interval '180 days';
-- and bump the trainer to only consume <180-day events.

comment on table public.telemetry_events is
  'Opt-in telemetry events from VCT orchestrator + launcher clients. ' ||
  'Service-role-only writes via the /telemetry edge function. ' ||
  'user_id_sha256 is SHA256(license_key) for GDPR data-deletion support.';
