import { defineConfig } from "vite";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";

const host = process.env.TAURI_DEV_HOST;

// v0.2.102 (diagrams dropped, owner ruling 2026-10-07): the
// VITE_MERMAID_PIN / VITE_EXCALIDRAW_PIN defines and their
// `readNpmPin` TOML scan were removed with the Diagrams tab — the only
// consumer of the pins. `bundled_mcp_versions.toml` no longer carries
// an [npm] table at all (Lane 1 trimmed the four diagram pins).

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [sveltekit(), tailwindcss()],

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
