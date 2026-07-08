-- 20260708000000_processed_webhooks.sql
--
-- E-7 (v0.2.75): webhook replay / idempotency ledger for
-- lemon-squeezy-webhook.
--
-- HMAC signature verification (already in place) proves a payload came
-- from Lemon Squeezy, but a captured SIGNED body can be re-POSTed
-- verbatim: replaying an `order_created` re-ran the grant path on every
-- delivery. This table makes processing idempotent — the edge function
-- claims `meta.event_id` with an INSERT before granting; a conflict
-- means the event was already processed successfully and the handler
-- short-circuits with 200 (no re-grant). Claims for FAILED processing
-- attempts are released by the function so Lemon Squeezy's retries can
-- re-process.
--
-- Idempotent: safe to run on a brand-new database and to re-run on a
-- deployed one (create-if-not-exists throughout, mirroring the
-- 20260516_telemetry_events.sql discipline).

create table if not exists public.processed_webhooks (
  -- Lemon Squeezy meta.event_id (stringified). The webhook body is
  -- HMAC-signed, so a replayed request necessarily carries the same
  -- event_id — dedup on it defeats verbatim replay.
  event_id text primary key,
  event_name text not null default '',
  processed_at timestamptz not null default now()
);

-- Service-role only: the edge function uses SUPABASE_SERVICE_ROLE_KEY
-- (bypasses RLS). Enabling RLS with NO policies blocks every anon /
-- authenticated access path.
alter table public.processed_webhooks enable row level security;

-- Housekeeping index for age-based pruning (the freshness window means
-- rows older than the max age can never dedup anything again).
create index if not exists processed_webhooks_processed_at_idx
  on public.processed_webhooks (processed_at);
