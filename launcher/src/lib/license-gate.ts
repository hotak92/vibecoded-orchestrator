// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 Stream D — License-gating helpers for the launcher GUI.
//
// Single source of truth for "should this paid-module surface be
// rendered inert because the orchestrator-tier license is missing or
// expired?". Centralising the predicates here means:
//
//   1. Every consumer (ModuleCatalog tile, ModuleConfigTab, weights
//      reset button, per-project enable toggle) derives the same
//      boolean from the same `TierCacheView`.
//   2. The expired-vs-missing distinction is encoded once (used only
//      to switch the banner copy — the gating BEHAVIOUR is identical).
//   3. The existing `$lib/admin-rebind.ts` `hasActiveLicense` predicate
//      stays untouched — we re-use it for the active-vs-not check so
//      the machine_mismatch recovery branch is shared.
//
// User decision (locked-in for v0.2.49, verbatim):
//   "this but also show the tab grayed out with no ability to change
//    config/update"
//
// Translation: license_missing OR license_expired → Configure tab is
// VISIBLE in the sidebar, tab content renders but ALL interactive
// controls are inert (buttons disabled, inputs read-only, selects
// disabled), and a prominent banner at the top of the tab says
// "License expired — re-activate to continue" (or "License required"
// for the missing case) with a link/button to open the License
// Manager modal.

import type { ModuleCatalogEntry, TierCacheView } from '$lib/types/launcher';
import { hasActiveLicense } from '$lib/admin-rebind';

/** Possible license states the GUI needs to discriminate. */
export type LicenseStatus = 'active' | 'missing' | 'expired';

/**
 * Discriminate between active / missing / expired.
 *
 * - `active`: the user has a working license. Either the tier is
 *   non-free, OR it's free server-side because of a machine_mismatch
 *   (recovery path, see `hasActiveLicense`).
 * - `expired`: tier is free AND `last_error` is set AND the error
 *   isn't a `machine_mismatch` recovery. A previously-working license
 *   has been revoked/expired/invalidated server-side.
 * - `missing`: tier is free AND no last_error. No key has been
 *   activated, or the activation was never attempted.
 *
 * The expired-vs-missing distinction ONLY affects the banner copy.
 * Both states gate controls identically.
 */
export function licenseStatus(cache: TierCacheView | null | undefined): LicenseStatus {
  if (!cache) return 'missing';
  if (hasActiveLicense(cache.orchestrator_tier, cache.last_error)) {
    return 'active';
  }
  // tier === 'free' AND not the recovery path. Discriminate by last_error.
  if (cache.last_error && cache.last_error.trim().length > 0) {
    return 'expired';
  }
  return 'missing';
}

/**
 * Is the license currently active?
 *
 * Thin wrapper around `hasActiveLicense` that accepts the full
 * `TierCacheView` so callers don't have to destructure manually.
 * Returns `false` when no cache is loaded yet (defensive default —
 * treats "unknown" as "not active" so paid modules stay gated until
 * the license store has finished loading).
 */
export function isLicenseActive(cache: TierCacheView | null | undefined): boolean {
  if (!cache) return false;
  return hasActiveLicense(cache.orchestrator_tier, cache.last_error);
}

/**
 * Does a module require an orchestrator-tier license?
 *
 * `license_required` is the canonical wire field from
 * `module_catalog_client.rs::ModuleCatalogEntry`. `min_orchestrator_tier`
 * is a defence-in-depth signal — some legacy manifests set the tier
 * floor without flipping `license_required=true`, so we accept either
 * as evidence of "this is a paid module".
 *
 * Treats `'free'` as non-paid (case-insensitive). Anything else (`pro`,
 * `mao`, `enterprise`, `admin`) is paid.
 */
export function moduleNeedsLicense(entry: ModuleCatalogEntry): boolean {
  if (entry.license_required) return true;
  const tier = (entry.min_orchestrator_tier ?? 'free').toLowerCase().trim();
  return tier !== 'free' && tier !== '';
}

/**
 * Is THIS paid-module surface license-gated right now?
 *
 * Combines `moduleNeedsLicense(entry)` AND `!isLicenseActive(cache)`.
 * Returns `false` for free modules regardless of license state — the
 * orchestrator-core Configure tab must NEVER be gated, no matter what.
 *
 * Defensive: if `entry` is null/undefined (e.g. the catalog hasn't
 * loaded yet and the route is still resolving), returns `false` so
 * the GUI doesn't flash a gate on a still-loading tab.
 */
export function moduleIsLicenseGated(
  entry: ModuleCatalogEntry | null | undefined,
  cache: TierCacheView | null | undefined,
): boolean {
  if (!entry) return false;
  if (!moduleNeedsLicense(entry)) return false;
  return !isLicenseActive(cache);
}

/**
 * Same as `moduleIsLicenseGated` but takes the module id + an
 * `installed?` flag. Used by surfaces that don't have direct access to
 * the full `ModuleCatalogEntry` (e.g. the Configure-tab route only
 * receives a moduleId). The catalog itself is consulted by the caller
 * who passes us the resolved `entry`; this overload is intentionally
 * NOT a separate lookup path — every surface must funnel through the
 * same catalog-driven decision.
 */

/**
 * Banner copy for the license-gated surfaces.
 *
 * Returns the user-facing title + the action button label. The
 * caller is responsible for wiring the action to
 * `ui.openLicenseManager()` (or `openActivation()`).
 *
 * Copy decisions:
 *   - `missing`: positive framing — "License required to use this
 *     module" implies "and here's how to add one". Action: "Activate
 *     license".
 *   - `expired`: stronger framing — the user previously had access
 *     and that access has lapsed. Action: "Open License Manager"
 *     (consistent with the locked-in spec wording).
 *
 * Active state never renders a banner; the caller should not call
 * this function in that case but if they do we return a safe empty
 * shape rather than throw.
 */
export interface LicenseBannerCopy {
  title: string;
  description: string;
  actionLabel: string;
}

export function bannerCopy(status: LicenseStatus): LicenseBannerCopy {
  if (status === 'expired') {
    return {
      title: 'License expired — re-activate to continue',
      description:
        'The license for this module has lapsed. Configuration controls are read-only until the license is re-activated.',
      actionLabel: 'Open License Manager',
    };
  }
  if (status === 'missing') {
    return {
      title: 'License required to configure this module',
      description:
        'This is a paid module. Activate a license to enable the controls below.',
      actionLabel: 'Activate license',
    };
  }
  // `active`: caller shouldn't be rendering a banner. Return empty
  // strings rather than throw so a misuse doesn't crash the GUI.
  return {
    title: '',
    description: '',
    actionLabel: '',
  };
}
