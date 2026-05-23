-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- payment_alerts: audit trail for failed-payment events (and future refund /
-- pause signals). Surfaces churn risk + lets a downstream notifier
-- email/Telegram the user.
--
-- Why a separate table (not just logs):
--   * Edge Function logs roll off after 7 days on Supabase free tier — we
--     need a durable record an ops human or a notifier worker can poll.
--   * Free-text logs can't be filtered ("show me every payment_failed
--     in the last 30 days for user X") without a structured row.
--   * A notifier transport (email/Telegram/webhook) wired in a later
--     polish item just polls `WHERE notified_at IS NULL` and updates the
--     timestamp on dispatch. No coupling to the webhook handler.
--
-- Service-role-only access by design: this table holds raw LS payloads
-- which can include email + subscription IDs we don't want users
-- inspecting via the anon key. RLS enabled with NO policies ⇒
-- service_role bypasses (it always does) and the anon key gets nothing.
--
-- (v0.2.31 #24: replace `payment_failed` silent-200 with a durable audit
-- insert. Notification transport is a follow-up — the audit row is the
-- contract.)

CREATE TABLE IF NOT EXISTS payment_alerts (
  id BIGSERIAL PRIMARY KEY,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  alert_kind TEXT NOT NULL,
  subscription_id TEXT,
  user_email TEXT,
  payload JSONB NOT NULL,
  notified_at TIMESTAMPTZ,
  CONSTRAINT payment_alerts_alert_kind_check CHECK (alert_kind IN ('payment_failed', 'refund_issued', 'subscription_paused'))
);

-- Partial index over the unnotified queue: a notifier polling worker
-- runs `SELECT * FROM payment_alerts WHERE notified_at IS NULL ORDER BY
-- occurred_at` every N seconds. The partial index keeps that query
-- O(unnotified-count) regardless of total table size — old (notified)
-- rows aren't even indexed here, so they don't slow down the poll.
CREATE INDEX IF NOT EXISTS payment_alerts_unnotified_idx ON payment_alerts (occurred_at) WHERE notified_at IS NULL;

ALTER TABLE payment_alerts ENABLE ROW LEVEL SECURITY;
-- No policies = service_role-only access (bypasses RLS).
