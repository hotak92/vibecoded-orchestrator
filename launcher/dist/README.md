# launcher/dist/ — Prebuilt Launcher Binaries

This directory ships precompiled launcher binaries alongside the source so a
fresh `bash first-install.sh` can use the launcher GUI **without** needing to
build it from source. Build-from-source is still supported for contributors,
custom architectures (ARM64, NixOS, BSDs), and anyone who wants a launcher
built from the exact commit they cloned.

## Layout

```
launcher/dist/
├── README.md                       (this file)
├── THIRD_PARTY_LICENSES.txt        (license notice for bundled binaries)
├── linux-x64/
│   └── vct-launcher                (~30 MB, dynamically links system webkit2gtk-4.1)
├── windows-x64/
│   └── vct-launcher.exe            (populated when Windows path is validated)
└── experimental_macOS/
    └── vct-launcher.app            (when populated, experimental — see notes)
```

## How `post-install-launcher.sh` uses this

Discovery order (highest priority first):

1. **`launcher/src-tauri/target/release/vct-launcher{,-temp}`** — locally built
   binary (developers running `pnpm tauri build` directly).
2. **`launcher/dist/<os-arch>/vct-launcher{,.exe}`** — bundled prebuilt (this
   directory). Default for end users.
3. **GitHub Releases asset** for the user's OS/arch. Used when the bundled
   binary is missing or when an update is available.
4. **Build from source** (`pnpm tauri build`). Last resort; needs Node + Rust +
   webkit2gtk-dev + ~5–15 min.

## Runtime requirements (on top of the bundled binary)

The binaries are **dynamically linked** — they need certain system libraries to
already be installed on the user's machine. `first-install.sh` audits these and
prompts to install missing packages where possible.

### Linux x64
Required runtime libraries (most distros have them; minimal/server installs may
not):
- `libwebkit2gtk-4.1.so.0` (Debian/Ubuntu: `libwebkit2gtk-4.1-0`)
- `libgtk-3.so.0` (`libgtk-3-0`)
- `libayatana-appindicator3.so.1` (`libayatana-appindicator3-1`)
- `librsvg2-2.so.2` (`librsvg2-2`)
- `libsoup-3.0.so.0` (`libsoup-3.0-0`)

Older Ubuntu (20.04 LTS) ships webkit2gtk-**4.0**, not 4.1 — that distribution
needs to either build from source or use the AppImage (TBD on GitHub Releases).

### Windows x64
WebView2 Runtime (Microsoft Evergreen). Pre-installed on Windows 10 1903+ and
Windows 11. Older Windows installs need:
https://developer.microsoft.com/microsoft-edge/webview2/

### macOS arm64 (experimental)
WKWebView ships with macOS — no extra runtime. The bundled binary at
`experimental_macOS/` is not signed or notarized; Gatekeeper will warn on
first run. Strip the quarantine xattr to bypass:

```bash
xattr -cr launcher/dist/experimental_macOS/vct-launcher.app
```

This path is **experimental until vco has tested macOS end-to-end**. Use at
your own risk; file an issue if it fails to launch.

## Updating the bundled binaries

Maintainers cutting a vco release should rebuild + restage all platform
binaries:

```bash
# Linux x64 (run on a Linux x64 host or in a Linux container)
cd launcher
pnpm install
pnpm tauri build --no-bundle
cp src-tauri/target/release/vct-launcher dist/linux-x64/vct-launcher
chmod +x dist/linux-x64/vct-launcher

# Windows x64 (run on Windows or via a Windows CI runner)
cd launcher
pnpm tauri build --no-bundle
copy src-tauri\target\release\vct-launcher.exe dist\windows-x64\

# macOS arm64 (run on Apple Silicon)
cd launcher
pnpm tauri build --no-bundle
cp -R src-tauri/target/release/bundle/macos/vct-launcher.app dist/experimental_macOS/
xattr -cr dist/experimental_macOS/vct-launcher.app
```

Then regenerate the third-party license inventory:
```bash
cd launcher/src-tauri
cargo install cargo-about
cargo about generate -c about.toml -o ../dist/THIRD_PARTY_LICENSES.html
```

Commit the new binaries + updated license file. Push to main. End users on
their next `git pull` get the new binaries automatically — no Releases asset
download needed (though Releases are still published for users who don't want
to clone the full repo).

## Why we don't ship `.deb` / `.rpm` / `.dmg` / `.msi` here

This directory ships the **executable binary**, not redistribution-grade
installer bundles. Reasons:

- Bundles need extra system tooling (rpmbuild, WiX, codesign + notarization)
  that's awkward to require on contributor machines.
- Bundles are larger (AppImage ~80–100 MB self-contained, .deb ~50 MB with deps).
- Most users running `bash first-install.sh` don't need an installer — they're
  already inside the cloned repo.

The proper installer bundles are published to GitHub Releases on each tagged
version (see [docs/RELEASING.md](../../docs/RELEASING.md)) for users who prefer
a one-click install workflow.
