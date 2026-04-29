// Global UI flags. Lives in the layout shell so any route can open the
// settings panel / activation modal / install wizard / mcp dashboard.

import { writable } from 'svelte/store';

interface UIState {
  showSettings: boolean;
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
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
    showOnboarding: false,
    onboardingForced: false,
  });

  return {
    subscribe,
    set,
    openSettings: () => update((s) => ({ ...s, showSettings: true })),
    closeSettings: () => update((s) => ({ ...s, showSettings: false })),
    openActivation: () => update((s) => ({ ...s, showActivation: true })),
    closeActivation: () => update((s) => ({ ...s, showActivation: false })),
    openInstallWizard: () => update((s) => ({ ...s, showInstallWizard: true })),
    closeInstallWizard: () =>
      update((s) => ({ ...s, showInstallWizard: false })),
    openMcpDashboard: () => update((s) => ({ ...s, showMcpDashboard: true })),
    closeMcpDashboard: () =>
      update((s) => ({ ...s, showMcpDashboard: false })),
    // Explicit re-run by the user (Settings → Re-run, Preferences →
    // Re-run). Clears the onboarding-complete localStorage flag, opens
    // the wizard, AND sets onboardingForced=true so the wizard's
    // preflight knows not to auto-close even if projects already
    // exist. Existing projects and settings are unaffected — only the
    // completion marker is removed so the wizard re-runs from step 1.
    openOnboarding: () => {
      try { localStorage.removeItem('vct.onboarding_complete'); } catch {}
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
