import { defineConfig } from "vite";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const host = process.env.TAURI_DEV_HOST;

// Phase 1 of the diagrams-integration plan (2026-05-24):
// `bundled_mcp_versions.toml` at repo root is the single source of truth
// for every pinned external dependency. The launcher embeds Mermaid as
// an npm package — the version in `launcher/package.json` MUST match the
// `[npm.mermaid_lib].version` in the manifest, or the embedded preview
// would silently drift from the version that ships with the MCP wrappers.
//
// We resolve the pin at dev / build time (zero runtime cost) and expose
// it to the frontend as `import.meta.env.VITE_MERMAID_PIN`. The
// DiagramsTab asserts at runtime that the loaded `mermaid` package
// version matches the pin — drift produces a clear console error and a
// toast, rather than a confusing rendering glitch later. Chose this
// over a separate CI lint because the assertion lives next to the code
// that depends on it and surfaces drift at the earliest possible point
// (dev server start / production build).
//
// Phase 2 (2026-05-25) generalises this to also read `[npm.excalidraw_lib]`
// for the embedded Excalidraw editor's `@excalidraw/excalidraw` pin —
// same shape, same drift-warning contract.
/**
 * Read a pinned version string from bundled_mcp_versions.toml.
 * @param {string} blockName - TOML block name under [npm.<blockName>], e.g. "mermaid_lib".
 * @returns {string | null} The pinned version, or null if the block / version is absent.
 */
function readNpmPin(blockName) {
  try {
    const here = dirname(fileURLToPath(import.meta.url));
    const tomlPath = resolve(here, "..", "bundled_mcp_versions.toml");
    const text = readFileSync(tomlPath, "utf8");
    // Minimal TOML scan — we only need one string value out of one
    // table. Pulling a full TOML parser into vite.config purely for
    // this is overkill; the regex is bounded to a `[npm.<block>]` block
    // so it's robust against other tables moving around the file.
    const blockRe = new RegExp(`\\[npm\\.${blockName}\\][^[]*`);
    const block = text.match(blockRe);
    if (!block) return null;
    const m = block[0].match(/^\s*version\s*=\s*"([^"]+)"/m);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

const MERMAID_PIN = readNpmPin("mermaid_lib");
const EXCALIDRAW_PIN = readNpmPin("excalidraw_lib");

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [sveltekit(), tailwindcss()],

  define: {
    // String-encoded so Vite literal-substitutes (no env-var indirection).
    // Falls back to "unknown" rather than failing the build — the frontend
    // assertion logs an error in that case so the failure is visible but
    // doesn't block all UI for an unrelated workflow.
    "import.meta.env.VITE_MERMAID_PIN": JSON.stringify(MERMAID_PIN ?? "unknown"),
    "import.meta.env.VITE_EXCALIDRAW_PIN": JSON.stringify(EXCALIDRAW_PIN ?? "unknown"),
  },

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
  },
}));
