// Global UI flags. Lives in the layout shell so any route can open the
// settings panel / activation modal / install wizard / mcp dashboard.

import { writable } from 'svelte/store';
import { clearOnboardingComplete } from '$lib/onboarding';

// Sections rendered by SettingsPanel.svelte's left nav. Exported so
// callers (e.g. SecretsTab "Open secrets panel") can request a specific
// initial section when opening Settings without going through a
// window-event bus.
export type SettingsSection =
  | 'profile'
  | 'downloads'
  | 'secrets'
  | 'preferences'
  | 'about';

interface UIState {
  showSettings: boolean;
  // When non-null, SettingsPanel jumps to this section on open instead
  // of defaulting to 'profile'. Consumed once and cleared by
  // closeSettings(). Replaces an earlier 'vct-open-secrets' window
  // event that SecretsTab used to dispatch.
  settingsInitialSection: SettingsSection | null;
  showActivation: boolean;
  showInstallWizard: boolean;
  showMcpDashboard: boolean;
  showOnboarding: boolean;
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
    showSettings: false,
    settingsInitialSection: null,
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
    showOnboarding: false,
    onboardingForced: false,
  });

  return {
    subscribe,
    set,
    // Open the Settings dialog. Pass `section` to jump straight to a
    // specific tab (e.g. 'secrets' from the per-project SecretsTab
    // "Open secrets panel" button). Defaults to null, which lets
    // SettingsPanel keep its previous activeSection (typically
    // 'profile' on first open).
    openSettings: (section: SettingsSection | null = null) =>
      update((s) => ({
        ...s,
        showSettings: true,
        settingsInitialSection: section,
      })),
    closeSettings: () =>
      update((s) => ({
        ...s,
        showSettings: false,
        settingsInitialSection: null,
      })),
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
  };
}

export const ui = createUIStore();
