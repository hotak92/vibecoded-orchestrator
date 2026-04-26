-- 20260427_admin_auth_log.sql
--
-- Audit log for Vault-token admin authentications.
--
-- The Vault-token admin path (separate from Bug 33's LS-variant path)
-- accepts a high-entropy random token and resolves it to a username via
-- the `vct_admin_tokens` Vault secret (JSON map). This table records
-- *every successful admin authentication* so a leaked token can be
-- traced (which user, which machine, when) and retrospectively
-- investigated.
--
-- See: launcher/supabase/functions/_shared/variant_map.ts ::
--      lookupVaultAdminToken()
--      docs/ADMIN_LICENSE.md  for the operational runbook.

CREATE TABLE IF NOT EXISTS public.admin_auth_log (
  id              BIGSERIAL PRIMARY KEY,
  admin_user      TEXT NOT NULL,
  machine_id_hash TEXT NOT NULL,
  authenticated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome         TEXT NOT NULL DEFAULT 'success'
                  CHECK (outcome IN ('success', 'expired', 'machine_mismatch')),
  ip_hash         TEXT,           -- sha256 of request IP, optional; NULL if function didn't capture
  user_agent      TEXT            -- short prefix only, NULL allowed
);

-- Lookup by user (forensic — "show me Fabio's auth history")
CREATE INDEX IF NOT EXISTS idx_admin_auth_log_user_time
  ON public.admin_auth_log (admin_user, authenticated_at DESC);

-- Lookup by machine (forensic — "what tokens authenticated from this machine?")
CREATE INDEX IF NOT EXISTS idx_admin_auth_log_machine_time
  ON public.admin_auth_log (machine_id_hash, authenticated_at DESC);

-- RLS: service-role only. Admin users themselves should not be able to
-- read this table from a client (the launcher) — it's a forensic trail
-- for the project owner / Supabase admin.
ALTER TABLE public.admin_auth_log ENABLE ROW LEVEL SECURITY;

-- Explicit DENY for anon + authenticated; only service_role bypasses RLS.
DROP POLICY IF EXISTS "deny_all_to_clients" ON public.admin_auth_log;
CREATE POLICY "deny_all_to_clients"
  ON public.admin_auth_log
  FOR ALL
  TO anon, authenticated
  USING (false)
  WITH CHECK (false);

COMMENT ON TABLE public.admin_auth_log IS
  'Audit log of Vault-token admin authentications. Append-only; '
  'service-role-only. Useful for tracing leaked-token usage.';

COMMENT ON COLUMN public.admin_auth_log.outcome IS
  '''success'' = token matched + within expiry + machine OK; '
  '''expired'' = token matched but past expires_at; '
  '''machine_mismatch'' = token bound to a different machine_id_hash.';

-- ────────────────────────────────────────────────────────────────────────────
-- Vault accessor function for the validate-tier edge function.
--
-- Edge functions cannot directly query `vault.decrypted_secrets`
-- because that view is restricted to the postgres role. The standard
-- Supabase pattern is a SECURITY DEFINER function owned by postgres
-- that selectively exposes the required value to service_role calls.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_vault_admin_tokens()
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, vault
AS $$
DECLARE
  v_secret text;
BEGIN
  SELECT decrypted_secret INTO v_secret
  FROM vault.decrypted_secrets
  WHERE name = 'vct_admin_tokens'
  LIMIT 1;
  RETURN v_secret;
END;
$$;

-- Lock down: only service_role can call this function. anon /
-- authenticated cannot — the edge function uses service_role
-- automatically.
REVOKE ALL ON FUNCTION public.get_vault_admin_tokens() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_vault_admin_tokens() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_vault_admin_tokens() TO service_role;

COMMENT ON FUNCTION public.get_vault_admin_tokens() IS
  'Returns the decrypted value of the vct_admin_tokens Vault secret '
  '(JSON map of {username: VaultAdminTokenRecord}). Service-role only. '
  'Used by validate-tier edge function to support the Vault-token admin '
  'path (separate from Bug 33 LS-variant path).';

-- ────────────────────────────────────────────────────────────────────────────
-- Vault writer function — used by validate-tier to bind a token to a
-- machine on first use (TOFU pattern).
--
-- This is the ONLY mutation the edge function performs against the
-- Vault secret. All other mutations (adding users, rotating tokens,
-- setting expirations) are manual SQL run by the project owner.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.bind_vault_admin_machine(
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
    -- User not in map — refuse (someone is trying to bind a non-existent user)
    RETURN FALSE;
  END IF;

  -- Only bind if currently NULL — never overwrite an existing binding
  -- via this function. (Rebinding requires explicit SQL by the project owner.)
  IF v_record -> 'machine_id_hash' IS NOT NULL
     AND v_record ->> 'machine_id_hash' <> 'null' THEN
    RETURN FALSE;
  END IF;

  -- Set machine_id_hash
  v_record := jsonb_set(v_record, '{machine_id_hash}', to_jsonb(p_machine_id_hash));
  v_new := jsonb_set(v_current, ARRAY[p_user], v_record);

  -- Update the Vault secret (must use vault.update_secret because
  -- vault.secrets is encrypted and not a plain table).
  PERFORM vault.update_secret(v_secret_id, v_new::text);

  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN
  -- Fail closed; logging is via Supabase's pg log
  RETURN FALSE;
END;
$$;

REVOKE ALL ON FUNCTION public.bind_vault_admin_machine(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.bind_vault_admin_machine(TEXT, TEXT) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bind_vault_admin_machine(TEXT, TEXT) TO service_role;

COMMENT ON FUNCTION public.bind_vault_admin_machine(TEXT, TEXT) IS
  'TOFU machine binding for Vault-token admin tokens. Sets the '
  'machine_id_hash field for a given user IFF the current binding is '
  'NULL. Returns true on bind, false on no-op (already bound, user '
  'not found, or any error). Service-role only.';
