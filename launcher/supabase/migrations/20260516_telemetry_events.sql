-- 20260516_telemetry_events.sql
--
-- Phase 2B (2026-05-16): create the telemetry_events table that the
-- /telemetry edge function inserts into. Consumed by:
--   - VCThelpers/telemetry/uploader.py::upload_pending() (client)
--   - launcher/supabase/functions/telemetry/index.ts (edge function)
--   - .claude/hooks/session-stop-telemetry-upload.sh (trigger)
--
-- Schema decisions:
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

-- Indexes for the queries we expect:
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
