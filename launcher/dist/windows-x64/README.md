# windows-x64 — placeholder

The Windows launcher .exe will land here once vco has tested Windows
end-to-end. Until then this directory is intentionally empty.

If you want to ship a Windows binary here:

1. Build on Windows or via WSL2 + Windows CI runner: `pnpm tauri build --no-bundle`
2. Copy the built .exe: `copy launcher\src-tauri\target\release\vct-launcher.exe launcher\dist\windows-x64\`
3. Update [../README.md](../README.md) with the testing-status note
4. Open a PR with the binary committed

The binary is unsigned — users will see SmartScreen on first run. Code signing
is on the v0.1.1 backlog. WebView2 Runtime is required and pre-installed on
Windows 10 1903+ / Windows 11. Older Windows users get an install URL when the
launcher fails to start.
