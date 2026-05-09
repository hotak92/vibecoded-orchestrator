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

## Versioning metadata

Every bundled binary ships with a `<binary>.metadata.json` sidecar containing
the source SHA, build timestamp, and a content hash of the launcher subtree
(`launcher/src-tauri/src/`, `launcher/src/`, `Cargo.{toml,lock}`,
`package.json`). Schema:

```json
{
  "source_sha":         "501cd831c9d1...",   // git rev-parse HEAD at build time
  "source_short_sha":   "501cd83",
  "source_hash":        "b150d4b6...",       // git ls-tree hash of the launcher subtree
  "built_at":           "2026-04-28T00:53:35Z",
  "launcher_version":   "0.1.0",
  "host_target":        "linux-x64",
  "binary_name":        "vct-launcher",
  "binary_size_bytes":  31435576
}
```

`post-install-launcher.sh` reads the metadata at install time. If
`source_hash` doesn't match the current clone's launcher subtree hash, the
bundled binary is treated as stale (= built from older sources) and the
install falls through to the download/build path. This prevents the
class of regression where a bundled binary lacks commands the source
already advertises.

To regenerate metadata after a rebuild, use `scripts/build-bundled-launcher.sh`
— it writes both the binary and the sidecar atomically.

## Updating the bundled binaries

Maintainers cutting a vco release should rebuild + restage all platform
binaries.

### Privacy: ALWAYS set RUSTFLAGS before building

Rust release binaries embed the build host's absolute paths (panic-message
metadata, `cargo registry` source paths, etc.) inside `rodata`. The
checked-in `launcher/src-tauri/.cargo/config.toml` has remaps for
`/home`, `/Users`, and `C:\Users` prefixes — those are **not enough**.
The username segment (`/home/<your-user>/.cargo/...`) survives without
an additional env-var-driven remap.

**Every release rebuild MUST set RUSTFLAGS** to include the per-user
remaps before invoking `tauri build`:

```bash
# Linux / macOS:
export RUSTFLAGS="--remap-path-prefix=$HOME=<home> --remap-path-prefix=$HOME/.cargo=<cargo>"

# Windows (PowerShell):
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=<home> --remap-path-prefix=$env:USERPROFILE\.cargo=<cargo>"
```

Cargo concatenates the env-var RUSTFLAGS with the `[build] rustflags`
in `.cargo/config.toml`, so both sets of remaps are applied.

The `Launcher binary leak-check` CI job (in `.github/workflows/ci.yml`)
verifies the produced binary has zero matches for `^/home/<user>/`,
`^/Users/<user>/`, or `C:\\Users\\<user>\\` patterns. CI builds on
GitHub-hosted runners use the generic username `runner` so they pass
without the per-user RUSTFLAGS, but **local dev rebuilds need it
explicitly** — without it, the binary committed to `dist/` will leak
your username.

### NEVER use plain `cargo build --release` to produce the dist binary

`cargo build --release` produces a binary that loads its frontend from
`http://localhost:1420` (the Vite dev server) — that binary will hang
with "Could not connect to localhost: Connection refused" when run
without `pnpm tauri dev` running in another terminal.

`pnpm tauri build --no-bundle` is the only command that produces a
**release binary that embeds the static frontend** (built via `npm run
build` into `launcher/build/`) and serves it via `tauri://localhost/`
at runtime. The `--no-bundle` flag skips installer-bundle generation
(`.deb`, `.AppImage`, `.dmg`, `.msi`) which we don't need for the
`dist/` checkin.

### Build commands (per platform)

**Recommended: use the wrapper script** at
[`launcher/scripts/rebuild-dist-binary.sh`](../scripts/rebuild-dist-binary.sh).
It encapsulates the privacy + correctness invariants AND verifies zero
leaks AND verifies the binary actually contains the embedded static
frontend (catches the `cargo build --release` mistake) before staging.

```bash
# Linux + macOS:
cd launcher
bash scripts/rebuild-dist-binary.sh
git add dist/<arch>/
```

If you'd rather invoke the underlying commands directly:

```bash
# Linux x64 (run on a Linux x64 host or in a Linux container)
cd launcher
pnpm install
export RUSTFLAGS="--remap-path-prefix=$HOME=<home> --remap-path-prefix=$HOME/.cargo=<cargo>"
pnpm tauri build --no-bundle
cp src-tauri/target/release/vct-launcher dist/linux-x64/vct-launcher
chmod +x dist/linux-x64/vct-launcher
# Verify zero leaks:
strings dist/linux-x64/vct-launcher | grep -E "^/home/[^/]+/" | wc -l   # must be 0
# Verify it's a tauri build (NOT cargo-only):
strings dist/linux-x64/vct-launcher | grep -q "tauri://localhost" && echo OK

# Windows x64 (run on Windows or via a Windows CI runner)
cd launcher
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=<home> --remap-path-prefix=$env:USERPROFILE\.cargo=<cargo>"
pnpm tauri build --no-bundle
copy src-tauri\target\release\vct-launcher.exe dist\windows-x64\
# Verify zero leaks (PowerShell):
strings.exe dist\windows-x64\vct-launcher.exe | Select-String "^C:\\Users\\[^\\]+\\" | Measure-Object   # Count must be 0

# macOS arm64 (run on Apple Silicon)
cd launcher
export RUSTFLAGS="--remap-path-prefix=$HOME=<home> --remap-path-prefix=$HOME/.cargo=<cargo>"
pnpm tauri build --no-bundle
cp -R src-tauri/target/release/bundle/macos/vct-launcher.app dist/experimental_macOS/
xattr -cr dist/experimental_macOS/vct-launcher.app
# Verify zero leaks:
strings dist/experimental_macOS/vct-launcher.app/Contents/MacOS/vct-launcher | grep -E "^/Users/[^/]+/" | wc -l   # must be 0
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
version (see [internal/RELEASING.md](../../internal/RELEASING.md)) for users
who prefer a one-click install workflow.
