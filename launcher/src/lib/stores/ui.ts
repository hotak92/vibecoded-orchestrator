// Global UI flags. Lives in the layout shell so any route can open the
// settings panel / activation modal / install wizard / mcp dashboard.

import { writable } from 'svelte/store';

interface UIState {
  showSettings: boolean;
  showActivation: boolean;
  showInstallWizard: boolean;
  showMcpDashboard: boolean;
}

function createUIStore() {
  const { subscribe, update, set } = writable<UIState>({
    showSettings: false,
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
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
  };
}

export const ui = createUIStore();
