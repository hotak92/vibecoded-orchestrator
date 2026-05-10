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

- [ ] **macOS support is experimental** — only minimal smoke-tested on a single Apple
      Silicon machine (Bash 3.2 empty-array fix landed during that test, see commit `cb3df13`).
      Known macOS-specific gotchas: Apple ships Bash 3.2 (the rest of the world uses 4.x+), Finder
      strips the exec bit on zip downloads, `.command` files need `xattr -dr com.apple.quarantine`
      after zip extraction, and Homebrew is not installed by default. The full Linux path is
      validated; the macOS path beyond `first-install.command` reaching `install.sh` is
      not. Linux is the recommended platform for v0.2.x; macOS Tier-2.

- [ ] **Launcher binary not yet code-signed (Windows + macOS)** — Windows shows SmartScreen "Windows
      protected your PC"; macOS Gatekeeper shows "damaged and can't be opened". Both are expected for
      v0.2.x. Workarounds documented in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#first-install-issues).
      Code signing is on the post-0.2.0 backlog.

- [ ] **Apple Developer enrollment / notarization pending** — the macOS `.dmg` is built unattended in
      CI without notarization. Intel Mac users must build from source for v0.2.x; a Universal binary
      is on the post-0.2.0 backlog.

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
      Vocabulary cleanup of seed nodes is on the v0.2.x chore backlog.

- [ ] **First-install grew by ~150 MB for Playwright MCP** — the default-enabled
      `playwright` MCP entry pre-caches Chromium during `install.py` so the first
      browser-automation call doesn't stall on a 150 MB download. Bandwidth-constrained
      users can opt out by exporting `VCT_SKIP_PLAYWRIGHT=1` before running
      `first-install.sh` / `install.py`; the MCP will then lazy-install Chromium on
      its first browser-launch instead. The pre-cache is non-fatal — if `npx` is
      missing or the download fails, the install logs a warn event and continues.

## Dev-only / upstream-blocked security alerts

- [ ] **`cookie@0.6.0` (low) — out-of-bounds chars in name/path/domain
      ([GHSA-pxg6-pf52-xh8x](https://github.com/advisories/GHSA-pxg6-pf52-xh8x))**.
      Pulled in transitively via `@sveltejs/kit@2.58.0` (latest), which
      pins `cookie@^0.6.0`. No in-range fix: `npm audit fix --force`
      would downgrade `@sveltejs/kit` to `0.0.30` (breaking). Tracked
      upstream; will pick up the patch when SvelteKit bumps its `cookie`
      dependency. Surfaces as 3 transitive low alerts (`cookie`,
      `@sveltejs/kit`, `@sveltejs/adapter-static`). No runtime cookie
      handling in our launcher (`@sveltejs/adapter-static` builds to a
      static SPA — no SSR cookie path is exercised at runtime).

## Pending v0.2.x

- [ ] **Apple Developer enrollment / notarization pending** — already in this list under Install/first-run;
      deferred from 0.2.0, tracked for a future minor release. Without notarization the macOS `.dmg` requires manual Gatekeeper override.

## Recently fixed

- **`register_github_pat` ↔ SecretsPanel write-path module_id unification** —
  the OnboardingWizard / `/preferences` Manage Token flow wrote the PAT at
  `vct._user_shared_.shared.installer/github_pat` while the SecretsPanel
  "Shared (this user)" tab wrote at `vct._user_shared_.shared.user/github_pat`.
  A user who registered via the wizard and later edited via the
  SecretsPanel ended up with two divergent keychain rows; reads through
  either path returned only that path's value, so the alternate row sat
  as a stale shadow. Both writers now use `module_id="user"` (matching
  the SecretsPanel's `UI_MODULE_BUCKET` constant); existing 0.2.0 installs
  are migrated on next `register_github_pat` call by
  `migrate_github_pat_installer_to_user_module_id` (audited as
  `github_pat_module_id_migration`). Hub resolver + `github_pat_for_env`
  fall back to the legacy slot during the upgrade window so existing
  tokens stay reachable until the migration runs.

- **Custom MCP tab is not populated by initial project registration** — fixed 2026-05-10 by adding
  migration `010_project_mcp_servers.sql` (per-project `project_mcp_servers` table) plus a
  `populate_mcp_servers` step in `project_state_populate.rs` that mirrors
  `<folder>/.claude/settings.json::mcpServers` AND `<folder>/.mcp.json` into the table. Each entry is
  flagged `is_user_added=true` when its name is not in the bundled allowlist (`weaviate-kg` /
  `ollama` / `search` / `code-embedding` / `playwright` / `vct-coordination`). A startup backfill
  in `lib.rs::run` re-runs the populate step for projects registered before migration 010
  (idempotent — preserves user toggles). Tauri commands `list_project_mcp_servers`,
  `list_user_added_project_mcp_servers`, `set_project_mcp_server_enabled`, and
  `unregister_project_mcp_server` are wired for the Custom MCP tab.

- **Lightweight Rust wiring for `--lightweight` re-install** — fixed 2026-05-10. `InstallConfig`
  now carries optional `lightweight: bool` and `lightweight_old_path: Option<String>` fields;
  `install_orchestrator` honours them by skipping the file-copy stage and invoking
  `install.py --lightweight [--lightweight-old-path <path>]` directly. The OnboardingWizard's
  re-install conflict modal exposes a "Fast reinstall" checkbox + optional previous-path text input
  so the user can opt into the lightweight path. Strategy radio is ignored when lightweight is on
  (no copy = no conflict to resolve).

- **Wizard step 3 install-path field allowed orphan installs** — the install-path text input + Browse button let
  users target any empty folder, after which the installer copied a SUBSET of files (per `ORCHESTRATOR_MANAGED_PATHS`)
  in but left out the launcher/, `first-install.sh`, and `start-launcher.sh`. End users got a half-installed orphan
  they couldn't run. The wizard now derives the install path from the launcher's source-repo location (VCO installs
  in-place) and the install button opens an explicit confirmation modal. `install_orchestrator` (Rust) and `install.py`
  (CLI) now both refuse non-source paths via `validate_source_repo()`. Wizard step 3 shows a read-only
  `Installing into <source-repo>` line; the field and Browse button are gone. Fixed in `fix/wizard-install-path-lockdown`.

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
  `~/bin/joern/joern-cli/` regardless. Now probes 3 known locations + falls back to PATH.

- **`_development` collection skipped when other projects had theirs** — adopt-mode logic
  incorrectly treated per-project `<Project>_development` collections as a shared namespace. If
  the host had any `_development` collection from a sibling project, VCO's was skipped, leaving
  `docs/` content unseeded (Step 7c exited 1). Fixed: `_development` is project-scoped, always
  created.

- **Bash 3.2 empty-array expansion crash on macOS** — `first-install.{sh,command}` used
  `"${INSTALL_ARGS[@]}"` and `"${HELPER_FLAGS[@]}"` under `set -euo pipefail`. Bash 3.2 (Apple's
  shipped default) trips "unbound variable" on empty-array expansion. Now guarded with
  `[ ${#ARR[@]} -gt 0 ]` (fixed in `cb3df13`).

- **Joern installer hang** — `first-install.*` could hang indefinitely while the Joern JVM installer
  ran without a timeout. Fixed in commit `64d5804`: streams installer output, 900s timeout.

- **macOS Gatekeeper quarantine on downloaded binary** — `first-install.command` now strips
  `com.apple.quarantine` xattr from any launcher binary it downloads from GitHub Releases before
  attempting to launch it.

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
