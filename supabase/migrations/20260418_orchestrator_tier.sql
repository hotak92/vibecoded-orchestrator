-- Orchestrator tier supporting objects.
--
-- The orchestrator_tier column itself is defined in
-- 20260418_profiles_schema.sql. This migration only adds the analytics
-- index + column comment, to keep the concern separated.

-- Analytics index — only index paid tiers, free is the vast majority.
CREATE INDEX IF NOT EXISTS idx_profiles_orchestrator_tier
  ON profiles(orchestrator_tier)
  WHERE orchestrator_tier != 'free';

COMMENT ON COLUMN profiles.orchestrator_tier IS
  'Orchestrator subscription tier. Service-role-only writes; clients may only SELECT.';
