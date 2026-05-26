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
 * Whether to render the "Rebind to this machine" affordance for the
 * given orchestrator tier.
 *
 * Today: admin-tier ONLY. The Vault-admin machine binding (TOFU pattern
 * via `bind_vault_admin_machine` + explicit rebind via
 * `rebind_vault_admin_machine`) is exclusive to the Vault-admin path.
 * LS-licensed users (pro / mao / enterprise) manage activations
 * through Lemon Squeezy's dashboard at vibecodedtools.it/account — no
 * launcher-side rebind needed.
 *
 * If we ever add LS-side machine rebind, the wire shape can evolve
 * (e.g. an enum of `'admin' | 'ls-activation'`); this helper stays
 * the single decision point.
 */
export function shouldShowRebindButton(tier: string | null | undefined): boolean {
  return tier === 'admin';
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
