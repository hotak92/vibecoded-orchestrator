-- Scheduled tier downgrade timestamp.
--
-- Set when subscription_cancelled webhook fires: user cancelled but the
-- paid period continues until ends_at. Cleared on subscription_expired
-- (downgrade applied) or on a fresh order_created (re-subscribed).
--
-- Read by clients to display "Cancels on YYYY-MM-DD" in the account UI.
-- Written ONLY by service role (webhook).

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS orchestrator_tier_ends_at TIMESTAMPTZ;

COMMENT ON COLUMN profiles.orchestrator_tier_ends_at IS
  'When the current orchestrator_tier expires (cancelled subscription). NULL = active or free.';
