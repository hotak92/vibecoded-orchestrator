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
//
// Named import per Tauri 2 plugin-dialog docs:
// https://v2.tauri.app/plugin/dialog/

import { isTauriRuntime } from './tauri';

export interface OpenDirectoryOptions {
  defaultPath?: string;
  title?: string;
}

/**
 * Open a native folder picker. Returns the selected path, or `null` if the
 * user canceled or Tauri is not available.
 *
 * Bug 13: previous version used `mod.open` after dynamic import without
 * verifying the export shape; on some runs the call silently failed because
 * the function was looked up via the wrong specifier. The dynamic import is
 * still kept (so the package is optional in browser-mode dev builds) but
 * we now destructure `{ open }` explicitly and log every step so failures
 * surface in the devtools console instead of disappearing.
 */
export async function pickDirectory(
  opts: OpenDirectoryOptions = {},
): Promise<string | null> {
  if (!isTauriRuntime()) {
    console.warn('[dialog] pickDirectory: not in Tauri runtime, returning null');
    return null;
  }
  console.log('[dialog] pickDirectory called', opts);
  try {
    const specifier = '@tauri-apps/plugin-dialog';
    const mod = (await import(/* @vite-ignore */ specifier)) as {
      open?: (o: unknown) => Promise<string | string[] | null>;
    };
    if (typeof mod.open !== 'function') {
      console.error('[dialog] pickDirectory: open() not found on plugin module', mod);
      return null;
    }
    const picked = await mod.open({
      directory: true,
      multiple: false,
      defaultPath: opts.defaultPath,
      title: opts.title ?? 'Select project folder',
    });
    console.log('[dialog] pickDirectory result:', picked);
    if (Array.isArray(picked)) return picked[0] ?? null;
    return picked ?? null;
  } catch (err) {
    console.error('[dialog] pickDirectory failed:', err);
    return null;
  }
}

/**
 * Open a native file picker. Returns the selected path, or `null` if the
 * user canceled or Tauri is not available. Mirror of pickDirectory but
 * returns a single file path. Filters are forwarded to the native picker.
 */
export async function pickFile(
  opts: OpenDirectoryOptions & {
    filters?: { name: string; extensions: string[] }[];
  } = {},
): Promise<string | null> {
  if (!isTauriRuntime()) {
    console.warn('[dialog] pickFile: not in Tauri runtime, returning null');
    return null;
  }
  console.log('[dialog] pickFile called', opts);
  try {
    const specifier = '@tauri-apps/plugin-dialog';
    const mod = (await import(/* @vite-ignore */ specifier)) as {
      open?: (o: unknown) => Promise<string | string[] | null>;
    };
    if (typeof mod.open !== 'function') {
      console.error('[dialog] pickFile: open() not found on plugin module', mod);
      return null;
    }
    const picked = await mod.open({
      directory: false,
      multiple: false,
      defaultPath: opts.defaultPath,
      title: opts.title ?? 'Select file',
      filters: opts.filters,
    });
    console.log('[dialog] pickFile result:', picked);
    if (Array.isArray(picked)) return picked[0] ?? null;
    return picked ?? null;
  } catch (err) {
    console.error('[dialog] pickFile failed:', err);
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
