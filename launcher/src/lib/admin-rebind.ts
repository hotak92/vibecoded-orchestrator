// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.36 — Pure helpers for the "Rebind to this machine" admin
// affordance in ActivationModal.svelte.
//
// Split into a standalone module so the visibility predicate and the
// server-error → friendly-message mapping can be exercised with
// vitest in the pure-node environment (the launcher's vitest config
// doesn't ship @testing-library/svelte — see vitest.config.ts).
//
// The Svelte component imports `shouldShowRebindButton` and
// `friendlyRebindMessage` directly; keeping them here means any
// future tier-gating change has exactly one test surface.

import type { AdminRebindResult } from '$lib/types/launcher';

/**
 * Whether to render the "Rebind to this machine" affordance.
 *
 * Visibility rules:
 *   1. `tier === 'admin'` — happy path. Token validated, currently bound
 *      to this machine; the button is an "if I move machines later, I
 *      know where it lives" affordance.
 *   2. `tier !== 'admin'` BUT `lastError` mentions "machine" — recovery
 *      path. v0.2.36's platform-stable machine_id_hash migration (Agent
 *      T) re-computes every host's hash from MachineGuid /
 *      IOPlatformUUID / `/etc/machine-id` rather than MAC address.
 *      Existing admin Vault rows pinned to the old MAC-derived hash
 *      now /validate-tier with `error: "machine_mismatch"` →
 *      server flips client to `tier='free'` server-side and writes
 *      the human message into `tier_cache.last_error`. Without this
 *      branch the rebind button stays hidden and the user is stuck.
 *      Substring match on 'machine' captures both the server's error
 *      code ("machine_mismatch") and the friendly message ("Admin
 *      token is bound to a different machine.").
 *
 * Defensive note: in the corner case `tier === 'pro'` + machine error,
 * we still show the button. LS pro users cannot in practice produce
 * a machine_mismatch error (their UUIDs never hit Vault TOFU), so the
 * branch is effectively unreachable for them; if it ever fires the
 * rebind endpoint will reject with its own clear error
 * (`not_an_admin_token`) which is preferable to silently hiding an
 * actionable affordance.
 *
 * The Vault-admin machine binding (TOFU pattern via
 * `bind_vault_admin_machine` + explicit rebind via
 * `rebind_vault_admin_machine`) is exclusive to the Vault-admin path.
 * LS-licensed users (pro / mao / enterprise) manage activations
 * through Lemon Squeezy's dashboard at vibecodedtools.it/account — no
 * launcher-side rebind needed in the happy path.
 */
export function shouldShowRebindButton(
  tier: string | null | undefined,
  lastError?: string | null,
): boolean {
  if (tier === 'admin') return true;
  if (typeof lastError === 'string' && lastError.toLowerCase().includes('machine')) {
    return true;
  }
  return false;
}

/**
 * v0.2.37 (Bug 1): "Does the user have an active orchestrator-tier license?"
 *
 * Mirrors the `hasLicense` `$derived` in `ActivationModal.svelte`. Exported
 * as a pure helper so the gating logic can be exercised in vitest without
 * standing up `@testing-library/svelte` (the launcher's vitest config is
 * node-only — see `vitest.config.ts`).
 *
 * Returns `true` when:
 *   - tier is anything other than 'free' (happy path: pro / mao / admin / etc),
 *     OR
 *   - the rebind affordance should be shown (recovery path: tier flipped to
 *     'free' server-side because of `machine_mismatch`, but the user DOES
 *     have a license activated).
 *
 * Why both branches: pre-v0.2.37 the modal used the bare `tier !== 'free'`
 * predicate and hid the Refresh/Rebind/Deactivate buttons in the recovery
 * case — the user saw only the activation input, with no path forward.
 *
 * Keeping this in sync with the Svelte `$derived`: this helper IS the
 * predicate; the component just imports + binds it.
 */
export function hasActiveLicense(
  tier: string | null | undefined,
  lastError?: string | null,
): boolean {
  if (tier !== 'free' && tier !== null && tier !== undefined) {
    return true;
  }
  return shouldShowRebindButton(tier, lastError);
}

/**
 * Map an `AdminRebindResult` to the toast message the dialog renders.
 * Success returns a positive confirmation; failure maps the server's
 * `error` code to a user-readable message, falling back to the raw
 * `detail` or the error code itself.
 *
 * The mapping table is kept in sync with the edge function's response
 * shapes (`launcher/supabase/functions/rebind-admin-token/index.ts`)
 * and the Rust orchestration layer's synthesized error codes
 * (`commands/licensing.rs::license_rebind_admin_token`).
 */
export function friendlyRebindMessage(result: AdminRebindResult): {
  kind: 'success' | 'error';
  message: string;
} {
  if (result.success) {
    return {
      kind: 'success',
      message: result.user
        ? `Rebound to this machine (user: ${result.user})`
        : 'Rebound to this machine',
    };
  }
  const friendly: Record<string, string> = {
    no_license_key: 'No license key activated. Activate the token first.',
    not_an_admin_token:
      'Machine rebind is only available for vct_admin_ tokens.',
    license_invalid:
      'Token not recognized by the server. Check the activated key.',
    rebind_failed:
      'Server-side rebind failed. Contact the project owner.',
    service_misconfigured:
      'Rebind endpoint not yet configured server-side.',
    network: 'Could not reach the rebind endpoint. Check your network.',
    license_key_invalid_format:
      'Activated key has the wrong shape for the rebind endpoint.',
    machine_id_hash_invalid_format:
      'Computed machine_id_hash has the wrong shape (internal error).',
    ipc_unavailable: 'Tauri bridge not initialised; reload the launcher.',
    ipc_failure: 'Tauri IPC call failed; reload and try again.',
  };
  const code = result.error ?? 'unknown';
  const friendlyMessage = friendly[code];
  const message = friendlyMessage ?? result.detail ?? `Rebind failed (${code})`;
  return { kind: 'error', message };
}
