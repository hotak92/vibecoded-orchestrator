import { writable } from 'svelte/store';

export interface Settings {
  installPath: string;
  autoUpdate: boolean;
  launchOnStartup: boolean;
}

const STORAGE_KEY = 'vct_settings';
// Bug A (v0.2.5): nothing should depend on a hard-coded default install
// path — the install script + wizard each provide the path on first use,
// and `get_known_install_path` + `get_default_install_path` (OS-aware)
// fill this in afterwards. Hard-coding `C:\VCT-Tools` (a) advertised the
// wrong OS on non-Windows, (b) misled the wizard pre-fill, and (c)
// confused the "checkStatus" probe into thinking a Windows-only path
// was the install root.
const DEFAULT_SETTINGS: Settings = {
  installPath: '',
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
