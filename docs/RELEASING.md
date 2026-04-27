# Releasing

How to cut a release of vibecoded-orchestrator. Tagging is a manual step on purpose — see "Why no automated tagging?" below.

## Version layout

The repo currently has three places that carry a version number:

| Where | Field | Notes |
|---|---|---|
| `launcher/package.json` | `version` | The launcher's npm package version. |
| `launcher/src-tauri/Cargo.toml` | `[package].version` | The launcher's Rust crate / Tauri bundle version. Should match `package.json`. |
| `CHANGELOG.md` | `## [x.y.z]` heading | Human-readable release notes following [Keep a Changelog](https://keepachangelog.com/). |

The orchestrator itself (Python side) doesn't carry a separate semver yet; it tracks the launcher version because they ship together. If we split them later, the `vct-module.json` manifests gain their own versions and this section grows.

## Cutting a release

Pre-flight checklist:

- [ ] CI is green on `main` (Rust + Python + svelte-check). See `.github/workflows/ci.yml`.
- [ ] Local `cargo test --lib --manifest-path launcher/src-tauri/Cargo.toml` passes.
- [ ] Local `pytest tests/ -q` passes.
- [ ] Local `cd launcher && npm run check` passes.
- [ ] Smoke-test the install on a clean machine (or VM): `./install.sh && claude`.
- [ ] Ollama image pin in `infrastructure/docker-compose.yml` and `claude_mcp_servers/compose.yaml` is current.

Steps:

1. **Bump the version in three places** in a single commit:
   ```bash
   # x.y.z = the new version (e.g. 0.2.0)
   sed -i 's/"version": "[^"]*"/"version": "x.y.z"/' launcher/package.json
   sed -i '0,/^version = "[^"]*"$/s//version = "x.y.z"/' launcher/src-tauri/Cargo.toml
   ```
   Then move the entries under `## [Unreleased]` in `CHANGELOG.md` to a new `## [x.y.z] — YYYY-MM-DD` section, leave `## [Unreleased]` empty above it, and update the link footer.

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

4. **GitHub release** — create a release from the tag. Copy the `## [x.y.z]` body from CHANGELOG.md into the release notes. Do not check "pre-release" unless the version is a pre-release identifier (e.g. `0.2.0-rc1`).

5. **Attach per-OS launcher artifacts** to the GitHub release. The `first-install.*` entry points probe GitHub Releases for the launcher binary when it is absent locally. If the artifacts are missing from the release, the download path in `first-install.*` will fail and users will be redirected to the "build from source" fallback. Required artifact names (must match exactly):

   | OS | Artifact name |
   |---|---|
   | Windows (x64) | `vct-launcher-windows-x64.exe` |
   | Linux (x64) | `VCT_Launcher_*.AppImage` and/or `vct-launcher_*.deb` |
   | macOS (Apple Silicon) | `vct-launcher-macos-arm64.dmg` |

   These are the canonical names `first-install.*` looks for when it calls the GitHub Releases API. Renaming them breaks the download path.

## Why no automated tagging?

Tagging will be the trigger for the future GitHub release pipeline (per-OS Tauri bundle build, signing, artifact upload). Until that pipeline exists and has been smoke-tested, tagging is reserved for the maintainer to do explicitly when a release is ready — not as the side effect of a CI run. A tag means "this is the commit external users should pin to". Nothing should silently create one.

When the release pipeline lands, the rule will be: tag manually after pre-flight passes; CI then picks up the tag and builds the artifacts.

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
