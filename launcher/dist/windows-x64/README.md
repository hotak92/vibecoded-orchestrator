# windows-x64 — bundled prebuilt binaries

This directory ships the Windows x64 prebuilt binaries:

- `vct-launcher.exe` — the Tauri launcher GUI
- `vct-hub.exe` — the detached background config/service hub
- `vct-updater.exe` — the self-update helper
- `*.metadata.json` — build sidecars (`source_hash`, `source_sha`,
  `launcher_version`, `built_at`, …) written by the bundled-launcher build
  script. `first-install.bat` / `start-launcher.bat` compare
  `source_hash` against the live launcher subtree's git hash and fall
  back to download/build when the bundled binary is stale.

(An earlier revision of this file said the directory was "intentionally
empty" — stale since the binaries landed; corrected v0.2.54 G-1.)

To refresh the binaries:

1. Build on Windows: `pnpm tauri build --no-bundle`
2. Copy: `copy launcher\src-tauri\target\release\vct-launcher.exe launcher\dist\windows-x64\`
3. Regenerate the `.metadata.json` sidecars (see `scripts/build-bundled-launcher.sh`'s
   manifest format) so the staleness check accepts the new build
4. Open a PR with the binaries committed

The binaries are unsigned — users will see SmartScreen on first run. Code
signing is on the backlog. WebView2 Runtime is required and pre-installed on
Windows 10 1903+ / Windows 11. Older Windows users get an install URL when the
launcher fails to start.
