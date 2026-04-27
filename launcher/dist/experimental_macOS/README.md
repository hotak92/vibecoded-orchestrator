# experimental_macOS — placeholder

The macOS launcher binary will land here once vco has tested macOS end-to-end.
Until then this directory is intentionally empty.

If you want to ship a launcher binary here:

1. Build on Apple Silicon: `pnpm tauri build --no-bundle` in launcher/
2. Copy the built `.app` bundle: `cp -R launcher/src-tauri/target/release/bundle/macos/*.app launcher/dist/experimental_macOS/`
3. Strip Gatekeeper quarantine: `xattr -cr launcher/dist/experimental_macOS/*.app`
4. Update [../README.md](../README.md) with the testing-status note
5. Open a PR with the binary committed

The binary is **experimental** until at least one validated end-to-end install +
launcher session on Apple Silicon hardware. Until validated, users running on
macOS will see the wizard-driven build-from-source path.
