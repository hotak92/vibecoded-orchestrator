// Global UI flags. Lives in the layout shell so any route can open the
// activation modal / install wizard / mcp dashboard / onboarding wizard.
//
// v0.2.23 F2 wave 2b (2026-05-21): the user-icon Settings popover was
// merged into /preferences. The SettingsPanel component is deleted; the
// `showSettings` flag + `settingsInitialSection` field were removed. The
// `openSettings(section)` action is kept as a thin compatibility shim
// that navigates to /preferences (or /preferences/secrets when the
// caller requests the 'secrets' section) so off-limits files
// (modules/+page.svelte — owned by the F2a Orchestrator Core agent)
// keep working without coordination churn. New code should call
// goto('/preferences') / goto('/preferences/secrets') directly.

import { writable } from 'svelte/store';
import { goto } from '$app/navigation';
import { clearOnboardingComplete } from '$lib/onboarding';

// v0.2.24: narrowed from the popover-era 5-value union (profile /
// downloads / secrets / preferences / about) to just 'secrets'.
// The other 4 all collapsed to goto('/preferences'), so the only
// remaining discriminator is whether the caller wants the secrets
// sub-route. New code should call goto('/preferences') /
// goto('/preferences/secrets') directly; this type only exists to
// keep the compatibility shim's signature precise.
export type SettingsSection = 'secrets';

interface UIState {
  showActivation: boolean;
  showInstallWizard: boolean;
  showMcpDashboard: boolean;
  showOnboarding: boolean;
  // v0.2.40 L1: multi-key licensing modal.
  //
  // NAMING NOTE (Agent L1, 2026-05-30): the flag is named
  // `showLicenseManager` — NOT `showLicense`, `showModal`,
  // `showKeyManager` or any short variant. Pre-push collision audit A3
  // (`.claude/context/reviews/v0240-pre-push-2026-05-30/discovery-A3-fabio-branch-collision-audit.md`)
  // identified that another contributor's parallel branch (orchestrator-update-progress
  // modal) is likely to add `showOrchestratorUpdateProgress` to this
  // same UIState interface in a future PR. The `showLicenseManager`
  // name was reserved by the A3 audit guidance to keep rebase
  // friction-free: distinct enough that there's no substring-match
  // ambiguity, distinct enough that grep/sed across the codebase won't
  // hit both flags.
  showLicenseManager: boolean;
  // v0.2.40 (contributor branch feat/orchestrator-update-progress-modal):
  // full-screen blocking overlay shown while the self-update of the
  // orchestrator is in flight (any of: update_orchestrator, apply_pending_install,
  // restart_launcher). Name was reserved by the A3 collision audit — see the
  // `showLicenseManager` comment above. The modal lives in +layout.svelte and
  // reads `$orchestrator.progress` (already populated by the install_progress
  // Tauri listener at stores/orchestrator.ts:108); UpdateBadge.svelte only
  // opens it via openOrchestratorUpdateProgress() — no duplicated listener.
  showOrchestratorUpdateProgress: boolean;
  // Cross-component trigger for ProjectSelector's "Create project" modal.
  // Set by routes that don't render their own form (e.g. /projects list
  // page's "+ Add Project" button) so the modal opens from the globally-
  // mounted ProjectSelector in MenuBar. ProjectSelector consumes and
  // resets this on close.
  showCreateProject: boolean;
  // True when the wizard was opened by an explicit user action
  // (Settings → Re-run, Preferences → Re-run) rather than the
  // automatic first-launch gate. The wizard's preflight uses this to
  // decide whether to auto-close on the "projects already exist" branch
  // — explicit re-runs must NOT auto-close even if projects exist.
  // Cleared when closeOnboarding() runs.
  onboardingForced: boolean;
}

function createUIStore() {
  const { subscribe, update, set } = writable<UIState>({
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
    showOnboarding: false,
    onboardingForced: false,
    showCreateProject: false,
    showLicenseManager: false,
    showOrchestratorUpdateProgress: false,
  });

  return {
    subscribe,
    set,
    // v0.2.23 F2 wave 2b: compatibility shim. The popover is gone; this
    // now routes to /preferences (or /preferences/secrets when the
    // caller requested the secrets tab). New callers should use
    // `goto('/preferences')` directly.
    openSettings: (section: SettingsSection | null = null) => {
      const target = section === 'secrets' ? '/preferences/secrets' : '/preferences';
      void goto(target);
    },
    openActivation: () => update((s) => ({ ...s, showActivation: true })),
    closeActivation: () => update((s) => ({ ...s, showActivation: false })),
    openInstallWizard: () => update((s) => ({ ...s, showInstallWizard: true })),
    closeInstallWizard: () =>
      update((s) => ({ ...s, showInstallWizard: false })),
    openMcpDashboard: () => update((s) => ({ ...s, showMcpDashboard: true })),
    closeMcpDashboard: () =>
      update((s) => ({ ...s, showMcpDashboard: false })),
    // Explicit re-run by the user (Settings → Re-run, Preferences →
    // Re-run). Clears the onboarding-complete flag in launcher.db, opens
    // the wizard, AND sets onboardingForced=true so the wizard's
    // preflight knows not to auto-close even if projects already
    // exist. Existing projects and settings are unaffected — only the
    // completion marker is removed so the wizard re-runs from step 1.
    //
    // Bug 14 fix (2026-05-05): the flag moved from WebView localStorage
    // to launcher.db (via $lib/onboarding) so VCT_STATE_DIR isolation
    // works. The clear is fire-and-forget — best-effort, never throws,
    // and the wizard opens regardless.
    openOnboarding: () => {
      void clearOnboardingComplete();
      update((s) => ({
        ...s,
        showOnboarding: true,
        onboardingForced: true,
      }));
    },
    // Auto-launch path used by +layout.svelte's onMount when the
    // onboarding-complete flag is missing. NOT a forced re-run — the
    // wizard's preflight may still auto-close if it discovers
    // projects already exist (e.g. localStorage was wiped but the DB
    // is intact). Internal-only; routes should call openOnboarding().
    autoOpenOnboarding: () => {
      update((s) => ({
        ...s,
        showOnboarding: true,
        onboardingForced: false,
      }));
    },
    closeOnboarding: () =>
      update((s) => ({
        ...s,
        showOnboarding: false,
        onboardingForced: false,
      })),
    openCreateProject: () =>
      update((s) => ({ ...s, showCreateProject: true })),
    closeCreateProject: () =>
      update((s) => ({ ...s, showCreateProject: false })),
    // v0.2.40 L1: per-paid-module license manager. Opens a modal that
    // lists every paid module's key with input + Validate + status
    // badge. See `LicenseManagerModal.svelte` for the UX shape and
    // `stores/moduleLicenseKeys.ts` for the data flow.
    openLicenseManager: () =>
      update((s) => ({ ...s, showLicenseManager: true })),
    closeLicenseManager: () =>
      update((s) => ({ ...s, showLicenseManager: false })),
    // v0.2.40 (contributor): orchestrator self-update progress overlay. Opened
    // by UpdateBadge.svelte right before invoking any of the three updater
    // store actions (runUpdate / applyPendingInstall / runRestart). Closed
    // by OrchestratorUpdateProgressModal.svelte itself after the completion
    // hold+fade timer expires, or by the user dismissing an error state.
    openOrchestratorUpdateProgress: () =>
      update((s) => ({ ...s, showOrchestratorUpdateProgress: true })),
    closeOrchestratorUpdateProgress: () =>
      update((s) => ({ ...s, showOrchestratorUpdateProgress: false })),
  };
}

export const ui = createUIStore();
