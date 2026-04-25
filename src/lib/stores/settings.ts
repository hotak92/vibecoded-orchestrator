import { writable } from 'svelte/store';

export interface Settings {
  installPath: string;
  autoUpdate: boolean;
  launchOnStartup: boolean;
}

const STORAGE_KEY = 'vct_settings';
const DEFAULT_SETTINGS: Settings = {
  installPath: 'C:\\VCT-Tools',
  autoUpdate: true,
  launchOnStartup: false,
};

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function createSettingsStore() {
  const { subscribe, set, update } = writable<Settings>(loadSettings());

  function save(settings: Settings) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  }

  return {
    subscribe,

    updateSetting<K extends keyof Settings>(key: K, value: Settings[K]) {
      update((s) => {
        const updated = { ...s, [key]: value };
        save(updated);
        return updated;
      });
    },

    reset() {
      save(DEFAULT_SETTINGS);
      set(DEFAULT_SETTINGS);
    },
  };
}

export const settings = createSettingsStore();
