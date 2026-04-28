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

## Pending v0.1.x

- [ ] **Custom MCP tab is not populated by initial project registration** — `project_state_populate`
      mirrors `.claude/settings.json::mcpServers` into the launcher's per-project DB on `create_project_v2`,
      but doesn't flag user-added entries (anything beyond bundled `weaviate-kg` / `ollama` / `search` /
      `code-embedding`) as `is_user_added=true`. Tab reads with that filter so user-added servers show up
      blank. Workaround: re-add via the launcher's "Add MCP" button (writes the row with the correct flag),
      or click Refresh on the MCP tab. Fix on v0.1.x backlog.

- [ ] **Apple Developer enrollment / notarization pending** — already in this list under Install/first-run;
      tracked as v0.1.1 priority. Without notarization the macOS `.dmg` requires manual Gatekeeper override.

- [ ] **Lightweight Rust wiring for `--lightweight` re-install** — the Python path is shipped (`install.py
      --lightweight` skips model pulls + seeding + agent/skill copy; `--lightweight-old-path` rewrites
      absolute paths in settings/env files). The launcher's "Reinstall" button currently calls full install;
      wiring it to the lightweight path is a v0.1.x polish item.

## Recently fixed

- **Project tabs empty after wizard** — `create_project_v2` registered the project but didn't populate the
  per-project state DB; Hooks / MCP / Agents / Skills tabs read from that DB and showed empty until the
  next launcher session triggered a manual refresh. Fixed in `03eb485` by adding `project_state_populate`
  step at the tail of `create_project_v2`.

- **Browse button silently fails to open folder picker** — earlier wizard builds dynamically imported
  `@tauri-apps/plugin-dialog`. Vite couldn't bundle a dynamic import, so the dialog plugin code was missing
  at runtime; clicking Browse fired the import, errored silently, and nothing happened. Fixed in `2c3429d`
  with static imports.

- **`vct` CLI hung when launcher tab opened a Cargo test page in the browser** — fixed alongside the CLI
  rename to `vco` (kg/codegraph search hub-routed via `/cli/*` HTTP API; doesn't shell out to launcher).

- **Wizard offered onboarding step on top of an existing install** — the launcher now self-detects an
  existing `vibecoded-orchestrator` install at the chosen path and skips onboarding. Step 4 inline-install
  path also handles `InstallConflictError` cleanly via the conflict modal (commits `260d156`, `fafdc51`).

- **Re-install over an existing `.claude/` had no clear path** — earlier "skip if exists" logic left the
  user wedged. Replaced with the **conflict modal**: 4 strategies (`delete-claude` / `overwrite-all` /
  `overwrite-preserve` / `adopt-as-is`) with `overwrite-preserve` as the safe default. Preserved files
  surface a Claude self-merge contract via a marker block in `.claude/CONTEXT_STATE.md`. Shipped in
  `e801590`.

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
  the hub `/cli/*` HTTP API. KG and code-graph search are now wired
  through the hub (`/cli/kg/{collections,search}`,
  `/cli/codegraph/{collections,search}`) with strict auto-detection of
  orchestrator-shaped Weaviate collections.

- **Concurrency invalidation for multi-tab use** shipped in P7 via the
  `change_log` table + `poll_changes` Tauri command (5s polling).
