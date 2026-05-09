# Releasing

Maintainer procedure for cutting a release of vibecoded-orchestrator. Tagging is manual.

## Version layout

The version number lives in three places:

| Where | Field | Notes |
|---|---|---|
| `launcher/package.json` | `version` | The launcher's npm package version. |
| `launcher/src-tauri/Cargo.toml` | `[package].version` | The launcher's Rust crate / Tauri bundle version. Must match `package.json`. |
| `CHANGELOG.md` | `## [x.y.z]` heading | Release notes following [Keep a Changelog](https://keepachangelog.com/). |

The Python orchestrator and the launcher ship together under one version.

## Cutting a release

Pre-flight checklist:

- [ ] CI is green on `main` (Rust + Python + svelte-check + **Launcher binary leak-check**). See `.github/workflows/ci.yml`.
- [ ] `cargo test --lib --manifest-path launcher/src-tauri/Cargo.toml` passes locally.
- [ ] `pytest tests/ -q` passes locally.
- [ ] `cd launcher && npm run check` passes locally.
- [ ] Smoke-test the install on a clean machine or VM: `bash first-install.sh && claude`.
- [ ] Ollama image pin in `infrastructure/docker-compose.yml` and `claude_mcp_servers/compose.yaml` is current.
- [ ] **Rebuild the launcher binaries** following the per-platform procedure in
  [`launcher/dist/README.md` § "Updating the bundled binaries"](../launcher/dist/README.md#updating-the-bundled-binaries).
  The procedure mandates `RUSTFLAGS` for path-privacy and
  `pnpm tauri build --no-bundle` (not plain `cargo build --release` —
  that produces a binary that tries to load its frontend from a
  `localhost:1420` dev server and hangs at startup). Verify each
  rebuilt binary has zero leaks via the `strings` command in that doc.

Steps:

1. **Bump the version in three places** in a single commit:
   ```bash
   # x.y.z = the new version (e.g. 0.2.0)
   sed -i 's/"version": "[^"]*"/"version": "x.y.z"/' launcher/package.json
   sed -i '0,/^version = "[^"]*"$/s//version = "x.y.z"/' launcher/src-tauri/Cargo.toml
   ```
   Move the entries under `## [Unreleased]` in `CHANGELOG.md` to a new `## [x.y.z] — YYYY-MM-DD` section, leave `## [Unreleased]` empty above it, and update the link footer.

2. **Commit** the bump:
   ```bash
   git add launcher/package.json launcher/src-tauri/Cargo.toml CHANGELOG.md
   git commit -s -m "release: vx.y.z"
   ```

3. **Tag** locally and push the tag:
   ```bash
   git tag -a vx.y.z -m "vx.y.z"
   git push origin main vx.y.z
   ```

4. **GitHub release** — create a release from the tag. Copy the `## [x.y.z]` body from CHANGELOG.md into the release notes. Check "pre-release" only if the version is a pre-release identifier (e.g. `0.2.0-rc1`).

5. **Attach per-OS launcher artifacts** to the GitHub release. The `first-install.*` entry points probe GitHub Releases for the launcher binary; without published artifacts users fall through to build-from-source (5–15 min, requires Node + Rust toolchain + system dev libs). The probes (`scripts/post-install-launcher.sh` and `first-install.bat`) match assets by:

   | OS | Probe matches |
   |---|---|
   | Linux (x64) | any asset ending in `.appimage` (case-insensitive). Convention: `VCT_Launcher_*.AppImage`. Also publish `vct-launcher_*.deb` for users who prefer apt. |
   | macOS (Apple Silicon) | any `.dmg` containing `arm64`/`aarch64`. Convention: `vct-launcher-macos-arm64.dmg`. |
   | Windows (x64) | exact name `vct-launcher-windows-x64.exe`. |

   Linux/macOS conventions are recommended for asset discoverability; the Windows name must match exactly.

## Tagging policy

A tag means "this is the commit external users should pin to". Tag manually after the pre-flight passes — never as a CI side effect.

## Hot-fix releases

For a security or critical bug fix on a tagged release:

1. Branch from the most recent tag: `git checkout -b hotfix/x.y.z+1 vx.y.z`.
2. Apply the fix, bump versions to `x.y.(z+1)`, update CHANGELOG.
3. Open a PR against `main` so the fix lands on trunk too.
4. After merge, tag `vx.y.(z+1)` from the merge commit on `main`.

## Pre-release identifiers

Use semver pre-release suffixes when shipping unstable bits to early testers:

- `0.2.0-alpha.1`, `0.2.0-alpha.2`, …
- `0.2.0-beta.1`, …
- `0.2.0-rc.1`, …

Mark these as "pre-release" on the GitHub release page. CHANGELOG entry uses the full identifier as the section heading.
