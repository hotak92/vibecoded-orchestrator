## Known Issues

Tracking polish-grade items that ship with the launcher but are worth
flagging for early adopters and the next iteration.

## Visual / UX

- [ ] **Tauri visual QA of per-project accent** — verify by running
      `npm run tauri:dev`, creating 3 projects, switching between them
      in the MenuBar selector, and confirming the 5px strip color +
      tinted project-name pill change distinctly per project (Wong 2011
      colorblind-safe palette). Browser preview confirms the CSS
      plumbing; only the bundled WebKit render path remains untested
      end-to-end. Delete this entry after manual verification.

## Install / first-run

- [ ] **macOS support is experimental for v1.0** — only minimal smoke-tested on a single Apple
      Silicon machine (Bash 3.2 empty-array fix landed during that test, see commit `cb3df13`).
      Known macOS-specific gotchas: Apple ships Bash 3.2 (the rest of the world uses 4.x+), Finder
      strips the exec bit on zip downloads, `.command` files need `xattr -dr com.apple.quarantine`
      after zip extraction, and Homebrew is not installed by default. The full Linux path is
      validated; the macOS path beyond `first-install.command` reaching `install.sh` is
      not. Linux is the recommended platform for v1.0; macOS Tier-2.

- [ ] **Launcher binary not yet code-signed (Windows + macOS)** — Windows shows SmartScreen "Windows
      protected your PC"; macOS Gatekeeper shows "damaged and can't be opened". Both are expected for
      v0.1.0. Workarounds documented in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#first-install-issues).
      Code signing is on the v0.1.1 backlog.

- [ ] **Apple Developer enrollment / notarization pending** — the macOS `.dmg` is built unattended in
      CI without notarization. Intel Mac users must build from source for v0.1.0; a Universal binary
      is planned for v0.1.1.

- [ ] **Linux .desktop double-click requires per-file-manager config** — documented in
      [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#linux-desktop-file-doesnt-open-on-double-click).
      No code fix pending; terminal fallback (`bash first-install.sh`) always works.

- [ ] **Container runtime install on macOS/Windows is URL-only** — `first-install.*` cannot auto-install
      Podman/Docker on macOS or Windows; it prints the URL and exits. Linux uses pkexec for interactive
      install. No change planned for v1.0 — container runtimes on those platforms require user consent
      GUI steps that can't be scripted portably.

- [ ] **Cosmetic warnings during seed (non-blocking)** — `AuthlibDeprecationWarning` from a
      transitive dep of `weaviate-client`, and several "No abstraction level tag" /
      "Tag 'LoRA' uses camelCase" vocabulary warnings from the bundled seed nodes in
      `knowledge/concepts/`. None affect correctness; the install completes successfully.
      Vocabulary cleanup of seed nodes is a v0.1.1 chore.

## Recently fixed

- **Joern installer ignored `--dir` flag, post-install detection failed** — install.py probed only
  the directory we asked the installer to use. Recent Joern installers ignore `--dir` and land at
  `~/bin/joern/joern-cli/` regardless. Now probes 3 known locations + falls back to PATH. Reported
  by user during real-machine test 2026-04-27.

- **`_development` collection skipped when other projects had theirs** — adopt-mode logic
  incorrectly treated per-project `<Project>_development` collections as a shared namespace. If
  the host had any `_development` collection from a sibling project, vco's was skipped, leaving
  `docs/` content unseeded (Step 7c exited 1). Fixed: `_development` is project-scoped, always
  created. Reported by user during real-machine test 2026-04-27.

- **Bash 3.2 empty-array expansion crash on macOS** — `first-install.{sh,command}` used
  `"${INSTALL_ARGS[@]}"` and `"${HELPER_FLAGS[@]}"` under `set -euo pipefail`. Bash 3.2 (Apple's
  shipped default) trips "unbound variable" on empty-array expansion. Now guarded with
  `[ ${#ARR[@]} -gt 0 ]`. Reported by macOS tester 2026-04-27, fixed in `cb3df13`.

- **Joern installer hang** — `first-install.*` could hang indefinitely while the Joern JVM installer
  ran without a timeout. Fixed in commit `64d5804`: streams installer output, 900s timeout.

- **macOS Gatekeeper quarantine on downloaded binary** — `first-install.command` now strips
  `com.apple.quarantine` xattr from any launcher binary it downloads from GitHub Releases before
  attempting to launch it. Fixed alongside the download-path work in this sprint.

- **Audit log filters pushed into SQL.** `Db::audit_list` now accepts
  `project_id`, `actor`, `since_ms`, `until_ms`, `search` (substring
  match against `operation` OR `detail`) and a per-call `limit` capped
  at 10000. The `/audit` route, the `list_audit_events` Tauri command
  and the hub `/cli/audit` endpoint all forward these directly to the
  query; the frontend no longer post-filters a 500-row window in JS.
  Free-text inputs are debounced (250ms) to avoid spamming SQL on each
  keystroke.

- **Per-project URL-addressable routes** at `/p/<slug>/...` shipped in
  P5 (migration 003 + slug resolution).

- **CLI escape hatch** shipped in P6 as `launcher/tools/vct-cli/` plus
  the hub `/cli/*` HTTP API.

- **Concurrency invalidation for multi-tab use** shipped in P7 via the
  `change_log` table + `poll_changes` Tauri command (5s polling).
