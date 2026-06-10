// SPDX-License-Identifier: AGPL-3.0-or-later
//
// install-state-store — per-install-root localStorage scoping (M-P1-5).
//
// Problem the helper solves:
//   Before v0.2.53, the launcher used unscoped localStorage keys for
//   per-clone state flags:
//     - vct.install_check_dismissed       (InstallHealthGate)
//     - vct.onboarding_force              (+layout.svelte)
//     - vct.show_changelog_after_update   (+layout.svelte)
//     - vct.update.seen_version           (stores/updater.ts)
//
//   When a user has two orchestrator clones on the same machine (common:
//   one /home/user/dev/vco-stable, one /home/user/dev/vco-dev), the
//   browser localStorage is shared across BOTH launcher binaries because
//   Tauri uses a single WebView2/WebKit profile per app-id. Dismissing
//   the install-incomplete banner in the "stable" clone silently
//   suppresses the banner in the "dev" clone — even if the dev clone
//   actually has a broken install. The "you have an update" toast for
//   v0.2.52 dismissed in clone A would suppress the same toast in
//   clone B, even though clone B still runs v0.2.50.
//
//   Cross-OS triage finding §P1-5 confirms this fires on macOS, Linux,
//   AND Windows (all use the shared WebView per app-id).
//
// Fix:
//   Scope each flag by the active install_root path. The key becomes
//   `<flag>:<install_root>` so two clones get distinct entries.
//   `install_root` comes from the Rust `check_install_health` probe;
//   when unknown (dev mode, browser preview), keys are scoped to
//   `unknown` so the behaviour matches the pre-v0.2.53 unscoped form
//   for a single install (the two clones case still desynchronises
//   the moment a real install_root materialises).
//
// Legacy cleanup:
//   The unscoped legacy keys are migrated on first scoped read for a
//   given (flag, install_root) pair: the legacy value is copied to
//   the scoped key AND deleted from the unscoped key. One-shot per
//   browser profile per install root. If the user runs an OLDER
//   launcher binary later, it may re-create the legacy key — the next
//   time a newer launcher reads it, the migration runs again.
//   Idempotent.

/// Build the scoped localStorage key for a given (flag, install_root)
/// pair. Falls back to a sentinel suffix when install_root is unknown
/// so we never accidentally produce `<flag>:` (an empty-string suffix
/// would alias all "unknown root" launches into one bucket).
export function scopedKey(flag: string, installRoot: string | null | undefined): string {
  const root = installRoot && installRoot.length > 0 ? installRoot : 'unknown';
  return `${flag}:${root}`;
}

/// Read a scoped install-state flag. On first read for a given (flag,
/// install_root) pair the legacy unscoped value is migrated: it is
/// copied into the scoped slot AND removed from the legacy slot.
/// Subsequent reads see only the scoped value.
///
/// Returns the stored string, or `null` if neither the scoped nor the
/// legacy key is set. Caller compares against e.g. `'true'` / `'1'`
/// per flag-specific convention.
export function getInstallScopedFlag(
  flag: string,
  installRoot: string | null | undefined,
): string | null {
  if (typeof localStorage === 'undefined') return null;
  const scoped = scopedKey(flag, installRoot);
  try {
    const scopedVal = localStorage.getItem(scoped);
    if (scopedVal !== null) {
      // Already migrated — nothing more to do.
      return scopedVal;
    }
    // Legacy fallback + one-shot migration.
    const legacyVal = localStorage.getItem(flag);
    if (legacyVal !== null) {
      try {
        localStorage.setItem(scoped, legacyVal);
        localStorage.removeItem(flag);
      } catch {
        // Quota / privacy mode: return the legacy value but skip the
        // migration. Next read will retry.
      }
      return legacyVal;
    }
    return null;
  } catch {
    return null;
  }
}

/// Write a scoped install-state flag. Never throws — quota / privacy-
/// mode failures are swallowed (the caller's state simply does not
/// persist; the launcher remains functional).
export function setInstallScopedFlag(
  flag: string,
  installRoot: string | null | undefined,
  value: string,
): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(scopedKey(flag, installRoot), value);
  } catch {
    // ignore
  }
}

/// Remove a scoped install-state flag for a given install_root. Used
/// when a flag's state has been consumed (e.g. one-shot onboarding
/// banner already shown).
export function clearInstallScopedFlag(
  flag: string,
  installRoot: string | null | undefined,
): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.removeItem(scopedKey(flag, installRoot));
  } catch {
    // ignore
  }
}

/// One-shot helper for migrating callsites that key by `vct.<flag>=1`
/// and want a simple boolean check. Returns true iff the stored value
/// equals `'1'` (mirroring the legacy +layout.svelte convention).
export function isInstallScopedFlagSet(
  flag: string,
  installRoot: string | null | undefined,
): boolean {
  return getInstallScopedFlag(flag, installRoot) === '1';
}
