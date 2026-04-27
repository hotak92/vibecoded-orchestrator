// Thin wrapper around the Tauri dialog plugin.
//
// Falls back to `null` in browser mode (vite preview, Playwright). Static
// imports for the plugin so Vite bundles the code into the chunk; an
// earlier dynamic-import-with-/* @vite-ignore */ pattern (Bug 13 era)
// produced runtime "Module name '@tauri-apps/plugin-dialog' does not
// resolve to a valid URL" errors in webkit2gtk because the bundler was
// told to keep the bare specifier raw — which works in dev (where Vite
// has a live module-resolver) but breaks in production-built webview
// pages where bare specifiers are NOT a valid HTTP URL.
//
// Tauri 2: `tauri-plugin-dialog` is a separate plugin from
// `@tauri-apps/api`. The Rust side is registered in `src-tauri/src/lib.rs`
// and the capability `dialog:default` is granted in
// `src-tauri/capabilities/default.json`.
//
// Named import per Tauri 2 plugin-dialog docs:
// https://v2.tauri.app/plugin/dialog/

import { isTauriRuntime } from './tauri';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { homeDir } from '@tauri-apps/api/path';

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
  if (!isTauriRuntime()) {
    console.warn('[dialog] pickDirectory: not in Tauri runtime, returning null');
    return null;
  }
  console.log('[dialog] pickDirectory called', opts);
  try {
    const picked = await openDialog({
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
    const picked = await openDialog({
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
    return await homeDir();
  } catch (err) {
    console.warn('[dialog] homeDir failed:', err);
    return null;
  }
}

/**
 * Suggest a default project-creation folder.
 *
 * Tries the user's home + `code` subdir. Best-effort — returns empty
 * string if nothing is reachable.
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
