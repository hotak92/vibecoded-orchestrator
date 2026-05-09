-- 20260509_rename_tier_expires_to_ends.sql
--
-- Reconciles a column-name divergence between code and remote DB.
--
-- Code path (lemon-squeezy-webhook/orchestrator_additions.ts) reads/writes
-- `orchestrator_tier_ends_at`. The remote `ovpdtijpdchzlxbojhsg` Supabase
-- project was set up via an out-of-tree migration that named the column
-- `orchestrator_tier_expires_at`. Existing 20260418_tier_ends_at.sql tries
-- to ADD COLUMN IF NOT EXISTS the canonical `_ends_at`, which would create
-- a duplicate column on the remote.
--
-- Verified empty (no rows in profiles) before designing this migration —
-- safe to rename without data migration.
--
-- Idempotent: only renames if the legacy column exists; no-op otherwise.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'profiles'
      AND column_name = 'orchestrator_tier_expires_at'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'profiles'
      AND column_name = 'orchestrator_tier_ends_at'
  ) THEN
    ALTER TABLE public.profiles
      RENAME COLUMN orchestrator_tier_expires_at TO orchestrator_tier_ends_at;
  END IF;
END $$;
