// Global UI flags. Lives in the layout shell so any route can open the
// settings panel / activation modal / install wizard / mcp dashboard.

import { writable } from 'svelte/store';

interface UIState {
  showSettings: boolean;
  showActivation: boolean;
  showInstallWizard: boolean;
  showMcpDashboard: boolean;
  showOnboarding: boolean;
}

function createUIStore() {
  const { subscribe, update, set } = writable<UIState>({
    showSettings: false,
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
    showOnboarding: false,
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
    // Clears the onboarding-complete localStorage flag and opens the wizard.
    // Existing projects and settings are unaffected — only the completion
    // marker is removed so the wizard re-runs from step 1.
    openOnboarding: () => {
      try { localStorage.removeItem('vct.onboarding_complete'); } catch {}
      update((s) => ({ ...s, showOnboarding: true }));
    },
    closeOnboarding: () => update((s) => ({ ...s, showOnboarding: false })),
  };
}

export const ui = createUIStore();
