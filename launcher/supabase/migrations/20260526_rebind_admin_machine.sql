-- 20260526_rebind_admin_machine.sql
--
-- Adds public.rebind_vault_admin_machine() — the SECURITY DEFINER RPC
-- that backs the `rebind-admin-token` edge function (v0.2.36).
--
-- Why this RPC exists alongside `bind_vault_admin_machine`:
--
--   `bind_vault_admin_machine` is TOFU-only (Trust On First Use):
--   it ONLY mutates `machine_id_hash` when the current value is NULL.
--   That's the correct discipline for the auto-bind path on first
--   admin auth — once bound, never silently rebind.
--
--   But the v0.2.35 post-update validation revealed a recovery gap: when an admin
--   reinstalls their OS or swaps laptop, the Vault entry's
--   `machine_id_hash` is non-NULL and pinned to the old machine.
--   Every subsequent `/validate-tier` returns `machine_mismatch`,
--   and the only escape was for the project owner to manually edit
--   the Vault secret over SQL.
--
--   `rebind_vault_admin_machine` accepts ANY current binding state
--   (NULL or already-bound) and OVERWRITES with the new hash. The
--   auth boundary is enforced at the edge-function level: the caller
--   MUST present the matching `vct_admin_*` token, so possession of
--   the token IS the authorization. This is the same security model
--   the `validate-tier` edge function already trusts.
--
-- Compared to manual SQL:
--   * The edge function authenticates the token via the existing
--     `lookupVaultAdminToken` constant-time compare before calling
--     this RPC, so we don't accept a `p_machine_id_hash` for an
--     arbitrary user.
--   * Audit trail flows through `admin_auth_log` with
--     `outcome='rebind'` (new outcome value, see migration below).
--   * No SQL access required for the project owner once provisioned.

-- Extend the admin_auth_log.outcome CHECK to include 'rebind' so the
-- audit row can be appended cleanly. The existing CHECK constraint
-- name is implicit (PostgreSQL auto-named); we DROP-and-CREATE the
-- column-level CHECK by recreating the constraint.
--
-- Strategy: drop the existing CHECK (by introspecting the constraint
-- name) and add a new one. PostgreSQL syntax for this is brittle
-- across versions, so we use a DO $$ block to find + drop the
-- constraint by definition.
DO $$
DECLARE
  v_conname text;
BEGIN
  SELECT conname INTO v_conname
  FROM pg_constraint
  WHERE conrelid = 'public.admin_auth_log'::regclass
    AND contype = 'c'
    AND pg_get_constraintdef(oid) LIKE '%outcome%';

  IF v_conname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.admin_auth_log DROP CONSTRAINT %I', v_conname);
  END IF;
END $$;

ALTER TABLE public.admin_auth_log
  ADD CONSTRAINT admin_auth_log_outcome_check
  CHECK (outcome IN ('success', 'expired', 'machine_mismatch', 'rebind'));

COMMENT ON COLUMN public.admin_auth_log.outcome IS
  '''success'' = token matched + within expiry + machine OK; '
  '''expired'' = token matched but past expires_at; '
  '''machine_mismatch'' = token bound to a different machine_id_hash; '
  '''rebind'' = explicit rebind via /functions/v1/rebind-admin-token (v0.2.36).';

-- ────────────────────────────────────────────────────────────────────────────
-- Rebind RPC. Mirror the shape of bind_vault_admin_machine but DROP the
-- "only-if-NULL" guard. Authentication of the requester (token match)
-- is enforced at the edge-function layer; this RPC is the durable
-- mutation primitive.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.rebind_vault_admin_machine(
  p_user TEXT,
  p_machine_id_hash TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, vault
AS $$
DECLARE
  v_current jsonb;
  v_record  jsonb;
  v_new     jsonb;
  v_secret_id uuid;
BEGIN
  -- Defensive: refuse empty inputs.
  IF p_user IS NULL OR length(p_user) = 0 THEN
    RETURN FALSE;
  END IF;
  IF p_machine_id_hash IS NULL OR length(p_machine_id_hash) = 0 THEN
    RETURN FALSE;
  END IF;

  -- Fetch current secret + ID
  SELECT id, decrypted_secret::jsonb INTO v_secret_id, v_current
  FROM vault.decrypted_secrets
  WHERE name = 'vct_admin_tokens'
  LIMIT 1;

  IF v_current IS NULL THEN
    RETURN FALSE;
  END IF;

  v_record := v_current -> p_user;
  IF v_record IS NULL THEN
    -- User not in map — refuse. Caller pre-validated the token to a
    -- specific user; arriving here with a missing user means the
    -- Vault map shifted under us (concurrent edit, e.g. user removed).
    RETURN FALSE;
  END IF;

  -- Always overwrite — that's the entire point of the rebind RPC.
  -- (The "only-if-NULL" path lives in bind_vault_admin_machine.)
  v_record := jsonb_set(v_record, '{machine_id_hash}', to_jsonb(p_machine_id_hash));
  v_new := jsonb_set(v_current, ARRAY[p_user], v_record);

  PERFORM vault.update_secret(v_secret_id, v_new::text);

  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN
  -- Fail closed; Supabase pg log captures the actual error.
  RETURN FALSE;
END;
$$;

REVOKE ALL ON FUNCTION public.rebind_vault_admin_machine(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.rebind_vault_admin_machine(TEXT, TEXT) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rebind_vault_admin_machine(TEXT, TEXT) TO service_role;

COMMENT ON FUNCTION public.rebind_vault_admin_machine(TEXT, TEXT) IS
  'Explicit rebind of a Vault-token admin user''s machine_id_hash. '
  'Unlike bind_vault_admin_machine (TOFU-only), this RPC overwrites '
  'any existing binding. Auth boundary: the calling edge function '
  '(rebind-admin-token) MUST pre-validate the submitted vct_admin_ token '
  'before calling. Returns true on success, false on user-not-found / '
  'secret-missing / any error. Service-role only.';
