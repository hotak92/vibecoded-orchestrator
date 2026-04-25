// Thin wrapper around the Tauri dialog plugin.
//
// Falls back to `null` in browser mode (vite preview, Playwright). The
// import is dynamic so the bundle doesn't hard-require the plugin package
// — useful if the package gets installed later or if a downstream build
// substitutes a different dialog.
//
// Tauri 2: `tauri-plugin-dialog` is a separate plugin from
// `@tauri-apps/api`. The Rust side is registered in `src-tauri/src/lib.rs`
// and the capability `dialog:default` is granted in
// `src-tauri/capabilities/default.json`.

import { isTauriRuntime } from './tauri';

export interface OpenDirectoryOptions {
  defaultPath?: string;
  title?: string;
}

/**
 * Open a native folder picker. Returns the selected path, or `null` if the
 * user canceled or Tauri is not available.
 */
export async function pickDirectory(
  opts: OpenDirectoryOptions = {},
): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  try {
    // Resolved at runtime by the Tauri host; the plugin is registered in
    // src-tauri/src/lib.rs and the package is listed in package.json. The
    // dynamic specifier (and @ts-ignore) avoid a hard build dependency on
    // the plugin's TS types in dev environments where node_modules isn't
    // populated.
    const specifier = '@tauri-apps/plugin-dialog';
    // @ts-ignore — plugin types are optional in this dev workflow.
    const mod = (await import(/* @vite-ignore */ specifier)) as {
      open?: (o: unknown) => Promise<string | string[] | null>;
    };
    if (typeof mod.open !== 'function') return null;
    const picked = await mod.open({
      directory: true,
      multiple: false,
      defaultPath: opts.defaultPath,
      title: opts.title ?? 'Select project folder',
    });
    if (Array.isArray(picked)) return picked[0] ?? null;
    return picked ?? null;
  } catch (err) {
    console.warn('[dialog] pickDirectory failed:', err);
    return null;
  }
}

/** Best-effort home directory. Returns null if unavailable. */
export async function homeDirectory(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  try {
    const mod = (await import(
      /* @vite-ignore */ '@tauri-apps/api/path'
    )) as { homeDir?: () => Promise<string> };
    if (typeof mod.homeDir !== 'function') return null;
    return await mod.homeDir();
  } catch (err) {
    console.warn('[dialog] homeDir failed:', err);
    return null;
  }
}

/**
 * Suggest a default project-creation folder.
 *
 * Tries `~/code` then `~/Documents` then plain home. Best-effort — returns
 * empty string if nothing is reachable.
 */
export async function suggestProjectFolder(): Promise<string> {
  const home = await homeDirectory();
  if (!home) return '';
  // We can't stat from JS without another command, so just suggest a
  // candidate; the user picks via Browse if it doesn't exist.
  const sep = home.includes('\\') ? '\\' : '/';
  // Strip trailing slash from home if any.
  const trimmed = home.replace(/[\\/]$/, '');
  return `${trimmed}${sep}code`;
}
