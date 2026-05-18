# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (v0.2.16 W4)

- `apply_pending_install` Tauri command: resolves the "Pulled-but-not-installed"
  banner state by running `install.py --update` against the existing install root
  WITHOUT a preceding `git pull`. Distinct from `update_orchestrator` (which does
  both) so manual `git pull` workflows don't waste ~30s pulling an already-current
  source tree.
- `/preferences/weaviate-untracked` advanced route: surfaces the full Weaviate
  code-graph collection inventory, including prefixes whose project is no longer
  registered with the launcher. Each row exposes a per-prefix delete affordance.
  Reachable from Preferences → Storage → "Show untracked Weaviate collections".

### Changed (v0.2.16 W4)

- `check_for_updates` now returns a full `UpdateStatus` struct (replacing the
  legacy `bool`) with three independent flags:
  - `remote_ahead` — local git branch behind `origin/main` (resolves via
    `update_orchestrator`: git pull + install.py --update).
  - `install_stale` — `vct-module.json::version` ahead of
    `state/install-manifest.json::version` (resolves via `apply_pending_install`).
  - `binary_stale` — running launcher version differs from
    `launcher/dist/<arch>/vct-launcher.metadata.json::launcher_version`
    on disk (resolves via `restart_launcher`).
- `UpdateBadge.svelte` renders one state at a time in priority order
  (`binary_stale` > `install_stale` > `remote_ahead`), each wired to the correct
  resolver. v0.2.15 shipped the binary-restart half via `LauncherRestartBanner`
  but the install-stale path was missing — visible install-was-out-of-sync for
  24+ hours after a manual `git pull` with no UI signal.
- `list_legacy_codegraph_collections` and `codegraph_list_projects` accept a new
  `include_untracked_projects: Option<bool>` parameter (default `false`). The GUI
  legacy-collections wizard + Code Graph dashboard now filter Weaviate collections
  by currently-tracked projects, hiding dead-project leftovers
  (`MediaLibrary_*`, `ARTup_*`, `Agape_*`, etc.). Data stays in Weaviate for
  potential re-import; the advanced /preferences/weaviate-untracked route
  surfaces the full inventory.

## [0.2.15] — 2026-05-17

Codegraph-wedge release. v0.2.14's testing on the maintainer machine
surfaced a chain of bugs that combined to deadlock the launcher's
"Re-build code graph" path for the orchestrator-root project: two
project-name → class-prefix sanitizers in the codebase produced
different prefixes for the same project, multiple naming-generations
of code-graph classes co-existed in Weaviate from prior VCO releases,
case-insensitive collisions caused Weaviate to reject schema creates,
and the analyze script retried the rejected creates indefinitely.
This release fixes all four links in the chain.

It also ships the user-anticipated launcher-restart UX after Update
Orchestrator swaps the on-disk binary, finishes the
`weaviate_claude` → `vco_weaviate` container-naming cleanup the
maintainer-machine leak had been hiding, and adds Windows path
support to the install-time legacy-volume probes.

### Major themes

- **Codegraph wedge eliminated**: orphan-collection detection in the
  wizard, fail-fast on case-collision in the analyze script,
  single canonical sanitize function shared between Python + Rust.
- **Update-orchestrator UX**: green sticky "Restart now" banner after
  binary swap; Windows ERROR_SHARING_VIOLATION rename-fallback.
- **Container-naming cleanup**: install.py / hooks / MCP / tests stop
  hardcoding `weaviate_claude`. Single registry at
  `vco_lib/containers.py`. `vct_code_embed` → `vco_code_embed` for
  naming consistency.
- **Cross-OS path support**: install.py path probes now generate
  Windows `%USERPROFILE%\...` + `%LOCALAPPDATA%\...` variants
  alongside POSIX `~/...`. `_find_lean_ctx_binary` adds Windows
  candidates (cargo / scoop / chocolatey / Program Files).

### Added

- `vco_lib/containers.py`: central canonical-name + historical-alias
  registry for the three orchestrator containers (Weaviate, Ollama,
  code-embed). New helpers `canonical_name()`, `all_known_names()`,
  `find_existing_container()` (honors `VCT_CONTAINER_RUNTIME`).
- `vco_lib/project_naming.py`: canonical project-name →
  Weaviate-class-prefix sanitizer. Single source of truth shared
  with `launcher/src-tauri/src/project_naming.rs` (Rust port pinned
  by `tests/fixtures/project_naming.json` parity tests in both
  languages).
- Launcher: `restart_launcher` + `get_launcher_restart_status` Tauri
  commands. `LauncherRestartBanner.svelte` polls every 5s + on
  mount; renders green sticky banner with "Restart now" for
  `launcher_restart_required` deferrals OR red banner with recovery
  steps for `launcher_binary_swap_failed_locked` (Windows lock).
- Launcher wizard: orphan code-graph detection (cross-references
  Weaviate prefixes against known projects via case-insensitive
  normalised-name match) + `cleanup_orphan_codegraph_collections`
  Tauri command (per-group consent, no auto-delete).
- Wizard extension UI: per-group checkboxes + dedicated delete
  button for orphan groups; double-confirmation contract preserved.
- `install.py`: emits `launcher_restart_required` deferral after
  binary swap. Windows rename-fallback for ERROR_SHARING_VIOLATION
  produces `vct-launcher.exe.old-<version>`.
- Tests: `test_container_naming.py`, `test_project_naming.py`,
  `test_project_naming_parity.py`,
  `test_analyze_code_graph_retry_cap.py`,
  `test_binary_swap_deferral.py`. +13 cargo tests for orphan
  helpers + cleanup-verification path.
- Early CI visibility for unset `DIST_COMMIT_TOKEN` — the release
  workflow now emits a clear `::warning::` annotation BEFORE the
  build matrix runs (saves ~25 min of wasted CI on a missing-secret
  release).
- CLAUDE.md: parallel-agent worktree-hygiene + spawn-from-right-repo
  guidance (lessons from this release cycle).

### Changed

- `analyze_code_graph.py`: imports `canonical_class_prefix` from
  `vco_lib.project_naming` (with inline fallback for standalone runs).
  Schema-create retry loop now caps at 3 attempts with exponential
  backoff for transient errors; **fails fast IMMEDIATELY on
  case-collision errors** with an actionable message directing the
  user to the launcher's wizard. Exits with code 2 on collision so
  the launcher IPC surfaces a failure toast instead of hanging.
- `install.py::_refresh_dist_binary_after_rebuild` now takes a
  `deferral_report=` parameter and emits the restart-required
  deferral after a successful swap. Windows path detects
  ERROR_SHARING_VIOLATION and attempts rename-fallback.
- `install.py` self-heal for `weaviate_unreachable_at_update`: uses
  `find_existing_container()` to discover the actual Weaviate
  container name on the host, AND `_detect_container_runtime()` to
  use the right runtime (`podman` vs `docker` vs whatever
  `VCT_CONTAINER_RUNTIME` says). User-facing recovery hints quote
  the resolved runtime + name instead of literal
  `podman start weaviate_claude`.
- `install.py::_build_legacy_volume_probes` generates Windows path
  forms in addition to POSIX. New helper `_expand_path_token()`
  expands both `~/...` and `%VAR%\...` heads transparently.
- `install.py::_find_lean_ctx_binary`: Windows branch with cargo /
  scoop / chocolatey / Program Files candidates. `os.access(X_OK)`
  skipped on Windows (no executable bit).
- `infrastructure/docker-compose.yml`: `vct_code_embed` →
  `vco_code_embed` for naming consistency. Legacy name kept in
  `HISTORICAL_ALIASES` so existing installs migrate cleanly.
- Cleanup paths (both legacy + new orphan): post-delete schema
  re-query verifies each "deleted" class is actually gone; survivors
  move from `deleted` to `failed` with cache-lag explanation. Stops
  the wizard from claiming "4 deleted" when only 2 went through.
- Launcher wizard's `current prefix` display now uses
  `canonical_class_prefix` (shared with the Python side) instead of
  the legacy `sanitize_kg_collection` that produced different output
  for the same project name.

### Fixed

- `storage_ux::cli_helper_tests` intra-process race: switched from
  `process::id()` to `uuid::Uuid::new_v4()` for tempdir naming AND
  added a process-wide `Mutex` around `with_state_dir`
  to serialise env-var mutation. (The UUID swap alone wasn't
  enough — the actual race was on the shared `VCT_STATE_DIR` env
  var.) Verified flake-free across 5 consecutive runs.
- 12+ leak sites where `weaviate_claude` was hardcoded as the
  container fallback (install.py self-heal commands, deferral
  messages, the MCP server's user-facing error hint, both bash +
  ps1 verify-container-ports hooks, two test files). Now sourced
  from `vco_lib/containers.py`.
- 4 leak sites discovered during the audit:
  `vco_lib/project_init.py::_DEFAULT_RESTART_CONTAINER` (flipped to
  lazy lookup), `templates/hooks/_lib/container-names.{sh,ps1}` env
  var (renamed to `vco_code_embed`), `templates/hooks/verify-
  container-ports.{sh,ps1}` compose-service derivation (handles
  both `vco_` and `vct_` prefixes), `test_pr2_templates_portability`
  tokenization bug (substring-match `'weaviate' in 'vco_weaviate'`
  was True; now uses shell-delimiter tokenisation).
- `test_install_storage_prompt::test_detect_uses_user_relative_paths_not_hardcoded`
  broadened to accept Windows `%VAR%` heads (was POSIX-only).

### Known issues

- macOS-arm64 binary lookup (v0.2.14 Bug 1 fix) still not exercised
  on a real macOS host — code-path-correct + unit-tested but no end-
  to-end validation. Will be covered by v0.2.16 candidate 1.1.
- Windows PS1 wrapper (v0.2.14 Bug 2 fix, 756 lines from Agent E)
  still not exercised on a real Windows host. v0.2.16 candidate 1.2.
- `DIST_COMMIT_TOKEN` is now visible in workflow logs but still
  requires a manual `gh secret set` to actually fix. The doc lives
  at `docs/MAINTAINER_GUIDE.md` Option B; until set, every release
  tag-push needs the manual ruleset-disable trick.

### Migration

- Maintainers who installed VCO from a fork pre-v0.2.x and still have
  `weaviate_claude` / `ollama_claude` containers on disk: the new
  `find_existing_container()` recognises them as legacy aliases, so
  no manual rename needed. The shipped compose creates `vco_*`
  containers; both can co-exist if you have ancient data to migrate.
- Project authors whose project name is "Foo Bar" (with a space): the
  new canonical sanitizer produces `FooBar` (drops space). If you
  already have `Foo_Bar_*` collections from the old sanitizer, the
  launcher wizard will flag them as orphans and offer cleanup OR
  re-analyze. No data loss without explicit user consent.
- `SimRacing_AI` and `SD15` project class names are unchanged.

## [0.2.14] — 2026-05-17

Cross-OS hardening release on top of v0.2.13. Surfaces fixed: a Windows
boot-service regression that's been latent since the v0.2.x launcher
shipped, a macOS bundled-binary dir mismatch (tier-1 always missed),
4 long-standing cargo-test flakes that gave false CI signal under
parallel test load, and a `VCT_CONTAINER_RUNTIME` env-var contract
that was honored in 4 of 8 surfaces (split-brain risk for users with
both runtimes installed).

Also: a generic paid-module installer framework lands publicly. The
launcher can now install Pro-tier modules via signed-URL container
pulls (first concrete consumer: the RL Reranker module, distributed
separately from the AGPL public repo).

**Major theme — Windows actually works**: the launcher's `find_stack_wrapper`
was probing for `scripts/launch-claude-mcp-stack.ps1` on Windows since
v0.2.x, but the file never existed — launcher silently fell back to
direct compose calls, losing CDI-wait, runtime validation, and override-
file logic. Boot Task XML required Git Bash / WSL on PATH because it
invoked the `.sh` via bash. Fix: shipped a 756-line PowerShell sibling
of the bash wrapper (full functional parity) + 19 PS1 tests + updated
Task XML to invoke PowerShell directly. No Git Bash / WSL dependency
on Windows anymore.

**Major theme — env-var contract consolidation**: `VCT_CONTAINER_RUNTIME`
was honored in hooks + launcher's `services::runtime.rs::resolve_runtime`
(PR-43 v0.2.12) but ignored in `install.py::_detect_container_runtime`,
`install.py::_detect_installed_runtime`, the boot wrapper's
`detect_runtime`, and `project_env_settings.rs::detect_runtime_sync`.
On a host with both podman + docker installed, hooks would pick one,
install + boot would pick the other — split-brain. Now consolidated:
all 8 surfaces consult the env var first (accepted values:
`podman|docker|auto`, case-insensitive, trimmed; unknown values log
to stderr + fall through to auto-detect — lenient).

**Major theme — cargo-test flakes fixed at root cause**: the 4 documented
flakes in `commands::kg_sync::tests` + `commands::installer::tests::
github_pat_keychain_tests` shipped through v0.2.12 + v0.2.13 as
"pass on retry, pass with `--test-threads=1`". Real fixes landed (no
`#[ignore]` markers):
- kg_sync: new `spawn_sh_with_retry` helper uses absolute `/bin/sh`
  (skips `execvp`'s `$PATH` traversal — the actual race source) + 3-attempt
  retry-with-backoff on transient `ErrorKind::NotFound`.
- keychain: `keychain_serialize_lock` upgraded to a `KeychainGuard` that
  bundles the in-process mutex with a cross-process `flock(LOCK_EX)` on
  `/tmp/vct-keychain-test.lock` (Unix) / `LockFileEx` (Windows inline FFI).
  Concurrent `cargo test --lib` invocations from different terminals now
  serialise on the OS keychain slot.
  9-batch parallel reproduction (3 batches × 3 concurrent runs each) post-fix:
  zero flakes.

### Added

- **`scripts/launch-claude-mcp-stack.ps1`** (756 lines, audit Bug #2):
  PowerShell 5.1+ port of the bash wrapper. Full functional parity for
  runtime detection, daemon-access validation, GPU/CDI handling
  (Windows: GPU passthrough is WSL2-mediated; no Linux-host CDI probes),
  compose-override resolution, and soft-fail discipline. Honors
  `VCT_CONTAINER_RUNTIME` env var first per the v0.2.14 contract.
- **`tests/test_launch_claude_mcp_stack_ps1.py`** (343 lines, 19 tests):
  mirrors the bash sibling's test matrix; auto-skips when no
  `pwsh` / `powershell.exe` on PATH.
- **`scripts/launch-claude-mcp-stack.sh::detect_runtime`** step 0:
  `VCT_CONTAINER_RUNTIME` consulted BEFORE runtime.txt + auto-detect.
  Unknown values log to stderr and fall through (lenient).
- **`install.py::_runtime_preference_from_env()`** helper: parses
  `VCT_CONTAINER_RUNTIME`; threaded into both
  `_detect_container_runtime` (probes the explicit runtime first;
  falls through to auto if not reachable) and `_detect_installed_runtime`
  (returns the explicit runtime if installed, else auto-detect).
- **`vco_lib/project_init.py::_hook_globs_for_os()`**: returns BOTH
  `*.sh` and `*.ps1` instead of host-OS only. Bundle installer now
  ships both flavours so cross-OS workflows (dual-boot, WSL crossover,
  network-mounted projects) don't leave stale orphan hooks from the
  unexpected flavour. Hooks are text files (~few KB each); shipping
  both is cheap + correct.
- **`docs/MAINTAINER_GUIDE.md`** (new): DIST_COMMIT_TOKEN setup runbook
  (Option A bypass_actors for org repos; Option B PAT secret for
  user repos), ruleset-disable trick for one-off pushes, full
  release-workflow flow + auto-commit failure recovery.

### Changed

- **`install.py::_launcher_binary_relative_path()`** (audit Bug #1):
  Darwin branch returns `("macos-arm64", "vct-launcher")` (was
  `("experimental_macOS", ...)` which pointed at an empty placeholder
  dir). Tier-1 bundled lookup now finds the macOS binary; tier-2
  download lands in the right dir; release-artifact name mapping
  pass-through.
- **`templates/windows/claude-mcp-containers.task.xml.template`**: invokes
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` against the
  new `.ps1` wrapper (no longer requires Git Bash / WSL). Falls back
  to the `.sh` via bash if the `.ps1` isn't shipped (e.g. early v0.2.14
  partial installs).
- **`install.py::_materialize_boot_service_windows`**: prefers `.ps1`
  when shipped; falls back to `.sh` if absent (defense in depth).
- **`launcher/src-tauri/src/commands/lifecycle.rs::run_stack_wrapper`**:
  adds `-NoProfile` to the PowerShell invocation for parity with the
  Task XML.
- **`launcher/src-tauri/src/commands/project_env_settings.rs::detect_runtime_sync`**:
  honors `VCT_CONTAINER_RUNTIME` env var (audit Bug #3).
- **README.md**: "5 MCP servers" → "3 default + 2 opt-in"; directory
  tree drops `ollama_mcp/` (opt-in via launcher Modules → `vct-ollama`);
  narrows search description to "academic-paper search" only. Closes
  the stale v0.2.11-removed surface that was misleading users on the
  public landing page.
- **`templates/agents/free/prompt-engineer.md`** + **`knowledge-curator.md`**:
  stale Ollama MCP refs cleaned. Tool-card sections for `chat` /
  `read_document` now note opt-in-only status via `vct-ollama`; decision
  trees route quick-analysis tasks to Claude's native reasoning rather
  than to a removed MCP tool.
- **`templates/scripts/claude_token_counter.py`** `WEB_TOOL_NAMES`:
  removed names of v0.2.11-dropped Search MCP tools
  (`mcp__search__web_search`, `mcp__search__fetch_page`,
  `mcp__search__search_code`). Kept `mcp__search__search_papers` (the
  only surviving search tool).

### Fixed

- **Windows boot-service silently failed on logon** (audit Bug #2):
  Task XML invoked `bash <wrapper.sh>` which required Git Bash / WSL
  on PATH. Install-time logged a soft warning; the Scheduled Task
  itself surfaced no diagnostic per failed logon. Now uses PowerShell
  natively; bash on Windows is optional.
- **macOS tier-1 launcher-binary lookup always returned None** (audit
  Bug #1): `_launcher_binary_relative_path()` returned
  `("experimental_macOS", ...)` but binaries shipped in
  `launcher/dist/macos-arm64/`. Fresh macOS installs fell through to
  GitHub download (tier 2) which then landed in the wrong dir too,
  leaving the desktop shortcut + the rest of the codebase
  inconsistent across two dist subdirs.
- **`VCT_CONTAINER_RUNTIME` split-brain on hosts with both runtimes
  installed** (audit Bug #3): see Major theme above.
- **Cross-OS workflows lost stale orphan hooks** (audit Concern #2):
  the bundle installer copied only host-OS hook flavours. A user
  with a project folder opened from both POSIX and Windows shells
  got stale orphan `.sh` files on Windows + stale orphan `.ps1`
  files on Linux/macOS that the unexpected shell could still invoke.
  Now both flavours ship always; the runtime picks which to invoke.
- **4 cargo-test flakes from v0.2.12** (`kg_sync::concurrent_drain`,
  `kg_sync::stall_watchdog`, `installer::pat_file_to_keychain_migration`,
  `installer::register_github_pat_preserves_existing_user_file`): see
  Major theme above. Both pairs fixed at root cause; no `#[ignore]`
  markers.

### Security

No new security advisories in v0.2.14. v0.2.13's Svelte XSS bump
(5.55.7 + Kit 2.60.1 + devalue 5.8.1) carries forward; `npm audit`
remains clean.

### Known issues / deferred to v0.2.15

- **`DIST_COMMIT_TOKEN` repo secret not yet set**: until configured per
  `docs/MAINTAINER_GUIDE.md`, the release-workflow `commit-dist-binaries`
  job 403s on push to protected `main`. Workaround: the ruleset-disable
  trick (also documented). Auto-commit code path verified manually on
  the v0.2.13 release tag.
- **`commands::storage_ux::cli_helper_tests::cli_helper_*`** (bonus
  finding from Agent B's flake audit): intra-process race in
  `with_state_dir` (uses `process::id()` as tempdir suffix → multiple
  parallel tests in one binary race on the same path). Reproducible
  via `cargo test --lib commands::storage_ux::cli_helper_tests`. One-
  line fix (`uuid::Uuid::new_v4()` instead of `process::id()`) deferred
  to v0.2.15.

## [0.2.13] — 2026-05-16

Same-day patch release on top of v0.2.12. Discovered during a real
v0.2.10 → v0.2.12 launcher-update test on the maintainer's dev machine:
8 distinct bugs in the update pipeline, all of which would silently
leave end users on a stale binary, with deprecated MCP entries
preserved, and with stale UI labels. v0.2.13 makes the update flow
self-healing.

**Major theme — update flow self-heals**: install.py's 4-tier launcher
binary resolution now post-rebuild-auto-swaps fresh binaries into
`launcher/dist/<os>-<arch>/` (Fix 1), retries the launcher-CLI against
the freshly-built binary when the bundled tier-1 binary times out
(Fix 5), and always touches `UPDATE_DEFERRED.md` so the user sees the
final union of all deferral entries (not just the ones generated
before `_register_mcps` runs, as in v0.2.12) (Fix 6).

**Major theme — deprecated MCP cleanup**: when `install.py` drops an
MCP from the default set (e.g. Ollama MCP in v0.2.11), existing user
installs were left with the stale entry in `~/.claude.json`. New
`--remove-deprecated-mcps` consent-prompted removal path (PR-34)
detects + offers removal. Detection is unconditional on `--update`;
removal requires the explicit flag. User-customised entries at paths
outside the install_root are never touched.

**Major theme — version-string consistency**: `vct-module.json` was
last bumped to `0.2.4`, never tracking subsequent releases. Bumped
to `0.2.13` + added to the canonical-release-bump checklist (Fix 9).
The launcher Store-page no longer hardcodes `version: '0.1.6'` for
the orchestrator + Pro cards — empty string with `{#if version}`
guard so the catalog override is the only version source (Fix 3).

**Major theme — security**: bumped Svelte `5.0.0` → `5.55.7` and
`@sveltejs/kit` `2.59.1` → `2.60.1` + `devalue` `>=5.8.1` override
to close 4 moderate Svelte XSS CVEs (DOM clobbering, SSR spread
attributes, SSR Promise serialization) AND 1 high `devalue` DoS CVE
(Fix 7). `npm audit` is now clean.

**Major theme — release-pipeline hardening**: new
`commit-dist-binaries` job in `.github/workflows/release.yml`
auto-commits freshly-built binaries back to `launcher/dist/` on every
release-tag push — eliminating the "committed binary lags release"
class of bug that surfaced in v0.2.12 (Fix 8). The auto-commit job
requires `bypass_actors` configured on the branch-protection ruleset
(github-actions[bot] integration); on personal-account repos the
auto-commit job will fail until a PAT-backed `DIST_COMMIT_TOKEN`
secret is wired (deferred to v0.2.14 — see Known issues).

### Added

- **Post-cargo-rebuild dist-binary auto-swap** (Fix 1, `install.py`):
  new `_refresh_dist_binary_after_rebuild()` helper, wired into
  `--update` immediately before `_register_mcps`. Conservative
  4-condition gate (src exists, src mtime > dist mtime, src produced
  in this run OR version-stale, `--no-binary-swap` flag not set). The
  version-stale fallback uses `tauri.conf.json` mtime as a cheap
  proxy for "the dist binary is built from a version older than the
  current tauri config". New flag `--no-binary-swap` to opt out.
  Module-level `_INSTALL_START_TS: Optional[float]` set at the top of
  `_run_install` provides the "produced in this run" signal.

- **Tier-3 retry of launcher CLI after timeout/non-zero-exit** (Fix 5,
  `install.py::_register_mcps`): when `Path A` (launcher binary CLI)
  fails transiently (timeout or non-zero exit) against a tier-1
  binary, install.py now invokes `_try_cargo_tauri_build()`
  explicitly to produce a fresh binary, then retries the CLI ONCE
  with the new binary before falling through to the pure-Python
  fallback (Path B). Emits `register_mcps_tier3_retry` events for
  observability. New flags `--prefer-only-bundled` (skip tier-2/3
  resolution AND the retry) and `--no-rebuild-on-stale` (skip
  ONLY the retry; resolution unaffected).

- **`UPDATE_DEFERRED.md` always populated on `--update`** (Fix 6,
  `install.py`): second `_deferral_report.write()` call at the very
  end of `_run_install` captures entries added AFTER the original
  write (which ran before `_register_mcps`, the deprecated-MCP
  detection, `_check_searxng_remnants`, `_check_ollama_mcp_remnants`,
  `_check_search_mcp_env_obsolete`, `_materialize_boot_service`,
  and `_rewrite_stale_mcp_entries`). On `--update` runs with zero
  deferral entries, writes a stub file with `entries: 0` frontmatter
  so the user has a paper trail that the run completed cleanly.

- **Deprecated-MCP detection + consent-prompted removal** (Fix 4 /
  PR-34, `install.py`): new `_DEPRECATED_DEFAULT_MCPS` registry maps
  removed MCP names to their `removed_in` version + reason + opt-in
  manifest path. New helpers `_scan_deprecated_mcp_entries`,
  `_detect_deprecated_mcp_entries`, `_remove_deprecated_mcp_entries`
  follow the same scan/detect/remove pattern as PR-33 stale-rewrite.
  Detection runs unconditionally on `--update` (hooked into
  `_register_mcps` after both Path A and Path B). Only entries
  whose `command` path is INSIDE the current install_root are
  flagged; user-customised entries at unrelated paths are left
  alone. New flag `--remove-deprecated-mcps` (per-entry consent
  prompt) + env override `VCT_REMOVE_DEPRECATED_MCPS=all` for CI.
  Two-level backup before any write, atomic `lock + tmp + os.replace`
  discipline. Composes with PR-33 `--rewrite-stale-mcps`. First
  entry in the registry: `ollama` (removed in v0.2.11).

- **CI auto-commit of dist binaries** (Fix 8,
  `.github/workflows/release.yml`): new `commit-dist-binaries` job
  (`needs: build`) downloads the 3 OS binaries from the matrix
  artifacts, skips byte-identical (`cmp -s`) ones, stages ONLY
  `launcher/dist/*/vct-launcher[.exe]` + `.metadata.json` paths
  (guard errors if any non-`launcher/dist/` file sneaks in), and
  commits `chore(binary): refresh vct-launcher dist binaries for
  v<version>` to main. New `workflow_dispatch` input
  `skip_dist_commit` for dry-runs.

- **2 Rust regression tests for the catalog-version invariants**
  (`launcher/src-tauri/src/commands/modules.rs`):
  `builtin_catalog_orchestrator_entry_has_non_empty_version` and
  `builtin_catalog_launcher_entry_has_non_empty_version` — guard
  against future regressions where `vct-module.json` or
  `CARGO_PKG_VERSION` returns empty.

- **42 new Python regression tests** across
  `tests/test_install_binary_resolution.py` (19),
  `tests/test_install_deprecated_mcps.py` (23). All passing.
  Full pytest suite: 1264 passed, 5 skipped, 50 subtests.

### Changed

- **`vct-module.json` bumped 0.2.4 → 0.2.13** (Fix 9). Description
  fixed: "5 MCP servers (4 containerized + Playwright)" →
  "4 MCP servers (3 containerized + Playwright)" — post-PR-14a
  Ollama MCP removal. `_canonical_counts_2026_05_16` comment
  refreshed to reflect PR-39 v0.2.12 ground truth: hooks live in
  `templates/hooks/*.sh` and are rendered into `.claude/hooks/` at
  install time, not source-of-truth there. The comment now
  explicitly lists `vct-module.json` as a release-bump-required file
  (alongside Cargo.toml + tauri.conf.json + package.json).

- **Svelte `5.0.0` → `5.55.7`** + **`@sveltejs/kit` `2.59.1` →
  `2.60.1`** + **`devalue` `>=5.8.1`** floor via `overrides`
  (`launcher/package.json`). Closes the 4 moderate Svelte XSS CVEs
  (GHSA-pr6f, GHSA-f3cj, GHSA-rcqx, GHSA-9rmh) and the 1 high
  `devalue` DoS CVE (GHSA-77vg). `npm audit` returns 0
  vulnerabilities. svelte-check still passes with 0 errors (the 45
  pre-existing a11y warnings on the dev-only Preferences /
  Projects / Services pages are unaffected).

- **Store-page `version: '0.1.6'` hardcode removed**
  (`launcher/src/routes/store/+page.svelte`): the orchestrator and
  Pro card now default to empty string `version: ''`. The catalog
  override (`list_module_catalog` → `builtin_catalog_entries` →
  `vct-module.json`) supplies the real version for the orchestrator
  card; the orchestrator-pro card has no catalog entry yet and
  shows no version label until one is added. The version rendering
  is now guarded by `{#if (catalogVersionById[app.id] ?? app.version)}`
  so an empty version never renders a bare `v` glyph.

### Fixed

- **Stale `dist/<os>-<arch>/vct-launcher` after `--update`** (Fix 1):
  in v0.2.12 the bundled binary at `launcher/dist/<os>-<arch>/`
  could be older than the orchestrator's source tree (e.g.
  v0.1.6-era), with `install.py --update` doing nothing to refresh
  it. Users who relied on the desktop icon (which targets
  `dist/<os>-<arch>/vct-launcher`) saw the wrong version
  post-update. Now auto-refreshed.

- **Launcher-CLI timeout against stale tier-1 binary fell straight
  to Python fallback** (Fix 5): when the v0.2.10 tier-1 binary did
  not recognize the v0.2.12 `--register-default-mcps` flag, it
  launched the GUI instead, hit the 30s timeout, and install.py
  fell to the Python writer — missing the tier-3 rebuild that
  would have produced a binary that DOES recognize the flag. Now
  retries with the freshly-built binary first.

- **`UPDATE_DEFERRED.md` missing entries from late-stage helpers**
  (Fix 6): in v0.2.12, deferrals generated by `_register_mcps`,
  the deprecated-MCP / SearXNG / Ollama / search-env remnant
  checks, `_materialize_boot_service`, and `_rewrite_stale_mcp_entries`
  were ALL lost because the single `_deferral_report.write()` ran
  before any of them. Today's launcher-update test produced 0
  files instead of the expected ~3-4 entries. Fixed.

- **Deprecated MCPs not auto-removed on `--update`** (Fix 4): in
  v0.2.12, an end user updating from v0.2.10 to v0.2.12 kept their
  `~/.claude.json mcpServers.ollama` entry (Ollama MCP was dropped
  in v0.2.11 PR-14a but install.py didn't detect or offer removal).
  Now flagged via deferral on every `--update`, removable via
  `--remove-deprecated-mcps`.

- **Store-page showed `v0.1.6` for orchestrator + Pro cards** (Fix 3):
  hardcoded default in `allApps[]` was rendered whenever the
  catalog override returned empty (which is always, for `orchestrator-pro`
  — no catalog entry exists). Fixed by setting the hardcoded
  default to empty + guarding the render.

- **`vct-module.json` reported stale version + wrong MCP count**
  (Fix 9): `0.2.4` since v0.2.4-era; `5 MCP servers` since v0.2.10-era
  (pre-PR-14a). Bumped + corrected.

### Security

- **Svelte XSS** (Fix 7, 4 moderate CVEs): bumped to 5.55.7. Patches:
  DOM clobbering of internal framework state (GHSA-pr6f), SSR XSS
  via spread attributes (GHSA-f3cj), SSR XSS via insecure Promise
  serialization in hydratable (GHSA-rcqx), and the related GHSA-9rmh
  family. None of the patches affected behavior we depend on
  (validated by svelte-check pass + visual inspection of the
  Promise-serialization use-sites).

- **`devalue` DoS** (Fix 7, 1 high CVE): pulled in via `@sveltejs/kit`
  bump to 2.60.1 + `overrides.devalue: ">=5.8.1"` floor. Closes
  GHSA-77vg.

### Known issues / deferred to v0.2.14

- **CI auto-commit job needs `bypass_actors`** (Fix 10, partial):
  GitHub's branch-protection ruleset API rejected the
  `bypass_actors` PUT on our personal-account repo with "Actor
  integration must be part of the ruleset source or owner
  organization". The auto-commit job in release.yml will silently
  fail until either (a) the repo is moved to an org where the
  Integration-type bypass actor pattern works, or (b) a
  `DIST_COMMIT_TOKEN` secret is created with a fine-grained PAT
  having `Contents: Write` on this repo, and the auto-commit job
  is reworked to use that secret in place of the default
  `GITHUB_TOKEN`. v0.2.13 release will use the manual
  ruleset-disable trick (same as v0.2.12) for the version-bump push.

- **macOS Intel x64 not built** (carried forward from v0.2.12).
  Tier-3 cargo rebuild fallback works on Intel Macs but is slow.

## [0.2.12] — 2026-05-16

Stability + UX release on top of v0.2.11. Closes 11 of 13 MCP-instability
bugs identified in the post-v0.2.11 audit (1 deferred to v0.2.13+, 1
worked around via allowlist tightening). Public repo hygiene: 97
shipped-duplicate `.claude/{hooks,scripts,settings.json}` files
deleted — install.py now renders them from `templates/` at install
time, so the public repo no longer ships rendered-output files that
would override user customizations on `--update`. Cross-OS scope kept:
every hook change shipped both `.sh` and `.ps1` bodies, with bash 3.2
compatibility documented as policy (macOS Apple Silicon ships bash
3.2.57; users may not have brew bash).

**Major theme — MCP wiring works end-to-end on fresh install**:
v0.2.11 silently shipped without writing any `mcpServers` entries to
`~/.claude.json` (PR-23 fixes this). On `--update` we also detect
stale entries pointing at moved/deleted install paths and offer a
consent-prompted rewrite (PR-33). Live env reload via SIGHUP + a
launcher file watcher on `.claude/settings.json` means changing per-
project env no longer requires restarting Claude Code (PR-42). The
`~/.claude.json` env allowlist is tightened so per-project values in
`.claude/settings.json env` can take effect (PR-43) — the spawn-time
env precedence in Claude Code applies `~/.claude.json mcpServers.*.env`
LAST, so any key present in both wins on the global side; removing
per-project-varying keys from the global allowlist is the only way to
let per-project overrides work today.

**Major theme — launcher GUI maintenance panels**: register-MCPs,
schema-migration, stale-MCP-rewrite, and reload-MCPs all surfaced as
buttons on the MCP page (`McpMaintenanceSection`) and Services page
(`ServicesSchemaSection`) of the launcher. 8 new Tauri commands wire
to the same install-time helpers, with backend-issued consent tokens
for destructive operations (PR-37 + PR-42).

**Major theme — schema correctness for temporal queries**: the
Development collection was missing 4 temporal properties (`created`,
`updated`, `valid_from`, `valid_until`) and both KG + Development
were missing `indexNullState=True`. Every `hybrid_search` that used
the stale-data filter (`valid_until is_none OR > now`) returned 0
results on those collections with a cryptic "build inverted filter
allow list" error. Idempotent migration scripts ship for existing
installs; new installs get the right schema at create time (PR-24).

**Major theme — public-repo hygiene**: 12 internal dev artifacts
removed from the public clone; root CLAUDE.md (orchestrator-self's
own large dev-context instructions) removed from the install whitelist
so user projects don't get it dropped on top of their own CLAUDE.md
(PR-31). 97 `.claude/{hooks,scripts,settings.json}` files that were
silently duplicated between source and shipped location were deleted
from the public repo — install.py renders all of them from `templates/`
at install time, eliminating the template↔runtime drift class of bugs
(PR-39).

### Added

- **Install-time MCP registration** (PR-23, `install.py`
  + `launcher/src-tauri/src/commands/installer.rs`
  + `launcher/src-tauri/src/mcp_registration.rs`): fresh installs
  (and `--update` reinstalls) now write the 4 default `mcpServers`
  entries (`weaviate-kg`, `coordination`, `search`, `vct-coordination`)
  to `~/.claude.json` via a launcher CLI subcommand
  `--register-default-mcps`. 4-tier launcher-binary resolution
  inside `install.py`: shipped bundled binary
  (`launcher/dist/<os>-<arch>/vct-launcher`) → on-demand GitHub
  release download → `cargo tauri build` rebuild (LAST resort,
  15-25 min) → pure-Python fallback (`launcher/src-tauri/src/mcp_registration.rs`
  ported as a Python helper). macOS ships only `macos-arm64`
  (Intel Macs not built per `.github/workflows/release.yml`). On
  upgrade, `--allow-rewrite-mcps` (or the consent-prompted
  `--rewrite-stale-mcps`) repoints existing `~/.claude.json`
  entries from `commands::installer::run_install_orchestrator_lightweight`
  too, so the "lightweight reinstall" path is consistent with the
  full install path.

- **Stale-MCP-entry detection + consent-prompted rewrite** (PR-33,
  `install.py`): on `--update`, scans `~/.claude.json` for
  `mcpServers.*.env` entries whose `command` or `args` point at
  paths outside the current install root. Reports each stale
  entry on stderr with the proposed rewrite, then waits for an
  explicit per-entry yes/no consent prompt (or `--yes` for
  unattended mode). Auto-rewriting without consent risked
  overwriting deliberate user customizations (e.g. a power user
  pointing at a different venv); per-entry granularity preserves
  intent. `--no-prompt` prints the proposed rewrites without
  applying them (CI / dry-run mode).

- **Launcher-centralized stale-MCP UX** (PR-37 + PR-42,
  `launcher/src-tauri/src/commands/maintenance.rs` +
  `launcher/src/lib/components/{McpMaintenanceSection,StaleMcpModal,
  SchemaMigrationModal,ServicesSchemaSection}.svelte`): 8 new
  Tauri commands surfaced on the launcher MCP + Services pages:
    - `mcp_registration_status` — read which MCPs are registered
      in `~/.claude.json` + their current path validity.
    - `rerun_mcp_registration` — invoke `--register-default-mcps`
      on demand from GUI.
    - `schema_migration_status` — read whether KG / Development
      collections need temporal-property or indexNullState
      migration.
    - `issue_schema_migration_consent_token` — backend-issued
      single-use UUID token; FE doesn't generate its own (preserves
      single-source-of-truth for the destructive op).
    - `run_schema_migrations` — apply pending migrations after
      consent.
    - `stale_mcp_entries` — list stale `~/.claude.json` entries
      with their proposed rewrites.
    - `rewrite_stale_mcp_entries` — apply per-entry consent
      `Vec<(name, bool)>` rewrites; preserves audit trail of
      unchecked entries.
    - `reload_mcps_sighup` (PR-42) — send SIGHUP to all running
      MCP server PIDs to force a clean-exit + Claude-Code respawn,
      picking up new `.claude/settings.json env` values without
      restarting Claude Code.

- **SIGHUP-driven MCP env reload + launcher file watcher** (PR-42,
  `claude_mcp_servers/_lib/sighup_handler.py` +
  `launcher/src-tauri/src/services/settings_json_watcher.rs`):
  every Python MCP server registers a SIGHUP handler that calls
  `sys.exit(0)` (clean exit, not raise) — Claude Code's MCP
  process supervisor respawns on clean exit, picking up the new
  env. Launcher's `notify` crate watcher debounces
  `.claude/settings.json` changes (1s) and dispatches the SIGHUP
  batch automatically. Same code path is exposed via the manual
  "Reload MCPs" button in the launcher MCP page for cases where
  the watcher isn't running (e.g. launcher closed).

- **Weaviate-MCP error classifier hardening** (PR-41,
  `claude_mcp_servers/weaviate_mcp/server.py`): new
  `WeaviateSchemaError` class distinguishes schema-not-found /
  schema-mismatch errors from connectivity (`WeaviateUnreachable`)
  and auth (`WeaviateAuthError`). Schema-not-found now invalidates
  the per-collection schema cache (Issue A: schema-cache stale
  after `--migrate-schema` ran in another process). Error
  messages surfaced to Claude include the distinguishing path so
  users (and future-Claude) can act on the right failure mode
  rather than misattributing schema problems to connectivity.

- **Development collection temporal properties + indexNullState**
  (PR-24, `vco_lib/project_init.py`
  + `scripts/migrate-development-temporal-props.{sh,ps1}`
  + `scripts/migrate-shared-kg-schema.{sh,ps1}`): adds 4
  temporal properties (`created`, `updated`, `valid_from`,
  `valid_until`) to `development_class_definition()` so KG-style
  stale-data filters work on Development collections.
  `invertedIndexConfig.indexNullState=True` added to BOTH KG and
  Development at create time. Migration scripts retro-add the 4
  properties to existing Development collections (POST to
  `/schema/<class>/properties` works on existing collections);
  shared-KG-schema migration drops + recreates the shared
  collection if `indexNullState=False` (Weaviate ≤1.30 can't
  retro-add it; shared KG is typically empty in user installs so
  this is non-destructive). `install.py --update` runs both
  migrations idempotently.

- **Hook dual-layout venv resolution** (PR-25,
  `templates/hooks/{code-graph-incremental,kg-summary-generator,
  pre-edit-context-inject}.{sh,ps1}`): the 3 hooks that need
  Python (for KG/codegraph work) now try `$REPO_ROOT/.venv` first,
  fall back to `$REPO_ROOT/claude_mcp_servers/.venv`, with
  `$VCT_VENV` as explicit override. Previously hardcoded
  `claude_mcp_servers/.venv` only — silently fell through to
  system python on installs using the modern top-level `.venv`
  layout. Cross-OS via `.ps1` siblings with the same fallback
  chain.

- **Shared-KG canonical-name alignment + partial-match picker**
  (PR-26 + PR-34, `vco_lib/project_init.py` + 5 launcher Rust /
  Svelte surfaces): canonical name is now
  `VibecodedOrchestrator_KnowledgeGraph` across all surfaces
  (install pipeline, launcher GUI default, MCP env, tests). Old
  alias `VibeCodedTools_KnowledgeGraph` kept for ~3 releases as a
  fallback in the schema-detection helper. New
  `SharedKgPicker.svelte` GUI lets users adopt a same-suffix
  legacy collection without forcing them to rename. The 5
  surfaces consolidated into a single `VIBECODED_TOOLS_KG_COLLECTION`
  constant.

- **Install-time storage-location prompt** (PR-28, `install.py`):
  on installs with legacy bind-mount paths in
  `$VCO_LEGACY_VOLUME_DIR` (or detected via the same logic as
  PR-10A's GUI), the CLI now prompts at install start with three
  options: keep the legacy path, migrate to the runtime-managed
  named volume (recommended), or pick a new bind path. Same
  3-shape `compose.override.yaml` generator as PR-10A's GUI; both
  paths go through the unified launcher-binary resolver added in
  PR-36. `--storage-mode {named,bind,legacy}` flag for unattended
  installs.

- **Cross-OS cache layer for pre-edit hook** (PR-38,
  `templates/hooks/pre-edit-context-inject.sh`): bash port of the
  `.ps1` cache layer for cross-OS perf parity. Cache key is
  `<file_path>:<offset>:<limit>` (block-atomic dedup — KG/CODE
  result blocks are emitted whole or suppressed whole, never
  partial — so a re-read with a different offset gets a fresh
  retrieval rather than a stale subset). **bash 3.2 compatible**:
  uses file-backed seen-set + `grep -Fxq` rather than `declare -A`,
  so the hook runs on macOS Apple Silicon (default `/bin/bash`
  is 3.2.57). Reduces re-read overhead from ~2.7s to ~31ms when
  the cache is warm.

- **VCT_CONTAINER_RUNTIME env honored in launcher GUI** (PR-43,
  `launcher/src-tauri/src/services/runtime.rs`): the
  `resolve_runtime()` function now honors a per-process
  `VCT_CONTAINER_RUNTIME=podman|docker|auto` override before
  falling back to the auto-detection order. Unrecognized values
  log a warning to stderr and fall back to default detection.
  Hooks already honored this env var; the launcher GUI parity is
  the missing piece.

- **KG node `claude-code-mcp-env-reversed-precedence`** documents
  the spawn-time env-application order in Claude Code's MCP
  supervisor for future reference. Anthropic's documented
  "project scope overrides user scope" semantics applies to chat-
  process settings but NOT to `mcpServers.*.env` — those entries
  are applied LAST in the MCP subprocess spawn, so any key
  present in both `~/.claude.json mcpServers.<name>.env` and
  `.claude/settings.json env` wins on the global side. The
  detection heuristic is "would two different projects on the
  same machine ever want different values for this key?" — if
  yes, the key must not appear in `~/.claude.json mcpServers.*.env`.

### Changed

- **Public repo no longer ships `.claude/{hooks,scripts,settings.json}`
  rendered files** (PR-39): 97 duplicated files deleted from the
  public clone. `install.py` renders all of them from
  `templates/` at install time with the appropriate path
  substitutions (`$INSTALL_ROOT`, `$VENV_PYTHON`, etc.). Source
  of truth is the `templates/` directory; the rendered output
  lives in the target install's `.claude/` and is no longer a
  candidate for source-control drift between the template and
  the shipped copy.

- **Root CLAUDE.md no longer in install whitelist** (PR-31): the
  orchestrator-self's own dev-context CLAUDE.md (which serves the
  dev project, not user projects) is no longer copied into user
  projects at install time. User projects get the project-
  template CLAUDE.md from `templates/CLAUDE.md.template` instead.
  Existing user CLAUDE.md is preserved across `--update` (the
  manifest-based drift detector classifies it as user-modified).

- **`docker-compose.override.yml` → `compose.override.yaml`**
  (PR-22, `launcher/src-tauri/src/commands/storage_ux.rs` +
  `scripts/launch-claude-mcp-stack.sh` + Windows task XML
  template): the dotted `.yaml` form is the one podman-compose
  actually loads. The dot-yml form was silently ignored, so
  bind-mount storage UX (PR-10A) shipped non-functional in
  v0.2.11. Boot script now explicitly adds
  `-f compose.override.yaml` when present (env-overridable via
  `VCT_STACK_COMPOSE_OVERRIDE`). Systemd unit + Windows task
  templates carry the env var. `install.py --update` detects the
  legacy `.yml` filename + renames it (idempotent) with a
  `compose_override_renamed` deferral.

- **`.vscode/settings.json claude-code.env` no longer written**
  (PR-27, `vco_lib/project_init.py`): the channel was empirically
  shown not to propagate env into MCP server subprocesses on
  Linux Claude Code 2.1.143. Removed from `write_project_env_files`
  to avoid creating the false impression that VS Code's
  `claude-code.env` block was a viable env-override mechanism.
  The KG node `vscode-claude-code-env-not-propagated-to-mcp-linux.md`
  documents the empirical finding for future reference.

- **`~/.claude.json mcpServers.*.env` allowlist tightened** (PR-43,
  `install.py` + `launcher/src-tauri/src/mcp_registration.rs`):
  removed `EMBEDDING_MODEL` and `RL_SERVER_URL` from
  `_ALLOWED_GLOBAL_ENV_KEYS`. Both keys legitimately vary per-
  project. Keeping them in the global allowlist meant the
  reversed-precedence behavior (`~/.claude.json` wins) shadowed
  any per-project value. Allowlist now contains only keys whose
  values are machine-invariant: `WEAVIATE_URL`, `OLLAMA_URL`,
  `GRPC_PORT`, `PYTHONPATH`, `ACTIVE_EMBEDDING`,
  `CODE_EMBED_SERVICE_URL`. Per-project-varying keys live ONLY
  in `.claude/settings.json env`. The detection heuristic for
  whether a key belongs in the global allowlist is recorded in
  the KG node `claude-code-mcp-env-reversed-precedence.md`.

- **Launcher-binary resolution DRY'd up across install + storage
  prompt** (PR-36): PR-23 (MCP registration) and PR-28 (storage
  prompt) both needed to resolve a launcher binary in the same
  4-tier order. Consolidated into a single helper. Behavior
  unchanged; pure refactor.

- **Pre-edit-context-inject cache-replay dead code removed**
  (PR-35, `templates/hooks/pre-edit-context-inject.sh`): the
  hook had an unreachable `replay_cached_results()` branch left
  over from an earlier design. Removed; PR-38's new cache layer
  is the only cache path.

- **Windows hooks `.ps1` body parity audit** (PR-32, 4 hooks):
  audited every `.ps1` hook for body-logic drift vs its `.sh`
  sibling. The only legitimate backport was `check-no-fork-bomb`
  env-scrub (the `.sh` had it from v0.2.11, the `.ps1` didn't).
  Other 3 were already in parity; documented in
  `windows-templates-parity-audit-2026-05-16.md` context note.
  Windows task XML wrapper now uses `cmd.exe /c "set ..."` for
  env propagation (the previous `bash -c "export ..."` approach
  failed on Windows hosts without WSL2 / Git-Bash on PATH).
  UTF-8 encoding declaration in the task XML (was UTF-16, fixed
  in fixup commit `8d32c16`).

### Fixed

- **Fresh install left Claude Code with zero MCP servers wired**
  (PR-23): v0.2.11 install pipeline populated everything except
  the `~/.claude.json mcpServers` block. Users on a fresh
  v0.2.11 install would see no orchestrator MCPs in Claude Code
  and had to hand-edit `~/.claude.json` — the #1 first-install
  UX failure. Now the install pipeline writes them via the
  launcher CLI subcommand.

- **`hybrid_search` returned 0 results with cryptic error on
  Development collection + shared KG** (PR-24): the MCP's
  stale-data filter (`valid_until is_none OR > now`) hit a
  Weaviate "build inverted filter allow list" error because
  Development collections didn't have the 4 temporal props at
  all, and shared KG didn't have `indexNullState=True` so
  `is_none` queries failed. Migration script fixes existing
  installs; new installs get the right schema at create time.

- **Schema-not-found error from MCP misattributed to "Weaviate
  unreachable"** (PR-41): when a project's KG / Dev collection
  didn't exist in Weaviate (typically because user hadn't run
  `install.py --update` after a schema migration), the MCP
  returned a generic "WeaviateUnreachable" error to Claude,
  which then suggested checking the container — wasting cycles
  before the user realized the right action was to run the
  migration. Now distinguished: `WeaviateSchemaError` with the
  collection name + suggested migration command.

- **MCP env changes required Claude Code restart to take effect**
  (PR-42): editing `.claude/settings.json env` to update a per-
  project KG collection, RL server URL, etc. required quitting
  and reopening Claude Code to pick up the change (because the
  MCP subprocess inherits env at spawn time). Now the launcher
  watches `.claude/settings.json` and SIGHUPs the running MCP
  PIDs (clean exit → Claude Code respawns) so changes apply
  within ~1s. Manual button for users without the launcher open.

- **Per-project MCP env values shadowed by `~/.claude.json`** (PR-43):
  see Changed section. `EMBEDDING_MODEL` and `RL_SERVER_URL` were
  unconditionally written to `~/.claude.json mcpServers.*.env` at
  install time, and the reversed-precedence behavior in Claude
  Code's MCP spawn made those values win against per-project
  overrides. Allowlist now excludes per-project-varying keys.

- **Stale `~/.claude.json mcpServers` entries from prior install
  locations** (PR-33): users who relocated their orchestrator
  clone had `~/.claude.json` entries pointing at the old path.
  Claude Code would silently fail to spawn the MCP. Now
  `--update` detects + offers per-entry consent-prompted
  rewrite.

- **`code-graph-incremental` + `kg-summary-generator` +
  `pre-edit-context-inject` hooks silently fell through to
  system python on modern installs** (PR-25): the hooks hardcoded
  `claude_mcp_servers/.venv` only. Installs using the modern
  top-level `.venv` layout (since the v0.2.4 install overhaul)
  saw the hooks "succeed" with system python that didn't have
  Weaviate / sentence-transformers installed → the hook returned
  empty results silently. Now tries `$REPO_ROOT/.venv` first.

- **Cache-layer non-determinism in `pre-edit-context-inject.sh`**
  (PR-38): the bash port of the `.ps1` cache could under
  concurrent reads emit a partial result block (cache hit on
  chunk N but cache miss on chunk N+1 of the same KG node, where
  the node was emitted in 2 chunks). Now block-atomic: the cache
  key is the file path + offset + limit, and a result block is
  emitted whole or not at all.

- **Cargo.lock missed `notify` crate dep refresh** (commit
  `98df441`): PR-42 added the `notify` crate for the file
  watcher; Cargo.lock wasn't regenerated in the PR. Fixed in a
  trailing chore commit before push.

- **3 installer conflict-strategy tests asserted root CLAUDE.md
  was in the install whitelist** (commit `a5b8432`): PR-31
  removed it from the whitelist but the test fixtures still
  expected it to be overwritten by `install --strategy overwrite`.
  Tests updated to assert CLAUDE.md survives independent of
  strategy (it's now outside the allowlist).

- **4 test fixups for PR-34 (shared-KG canonical name) + PR-42
  (`reload_mcps_sighup` Tauri command) integration** (commit
  `952fa96`).

### Removed

- 97 shipped-duplicate `.claude/{hooks,scripts,settings.json}` files
  from the public repo (PR-39). Source of truth is `templates/`;
  rendering at install time eliminates the source↔shipped drift
  bug class.
- 12 internal dev artifacts from the public repo (PR-31): test
  scaffolding, dev-only context docs, and dev-laced reference
  notes that had crept into the shipped tree.
- Root `CLAUDE.md` from the install whitelist (PR-31). Orchestrator-
  self's own CLAUDE.md is dev-only; user projects get the project-
  template CLAUDE.md.
- `EMBEDDING_MODEL` and `RL_SERVER_URL` from the
  `_ALLOWED_GLOBAL_ENV_KEYS` allowlist (PR-43). These keys vary
  per-project; the reversed-precedence behavior in Claude Code
  meant including them in the global block shadowed per-project
  values.

### Security

- **Env-scrub on `lean-ctx-rewrite.{sh,ps1}` hooks** (post-v0.2.11
  patch commit `cb7ff88`): the hook now ships with full env-scrub
  for parity with every other VCO hook. Defense-in-depth: the
  hook doesn't process secrets directly, but `unset SUPABASE_KEY
  GITHUB_TOKEN ...` removes the exposure surface if a future
  change to the hook were to log the environment.
- **Backend-issued consent tokens for schema-migration
  destructive op** (PR-37 + PR-42): the schema-migration modal
  fetches the consent token from `issue_schema_migration_consent_token`
  rather than generating its own UUID. Single source of truth
  for the destructive-op gate; FE typos can't drift away from
  the contract.

### Known issues / deferred

- **Per-session MCP subprocesses** (Issue G from the v0.2.12 audit):
  Claude Code's MCP supervisor spawns a fresh MCP subprocess per
  chat session. The MCP spec doesn't currently support a daemon
  mode where multiple sessions share one MCP process. Cold-start
  latency (~300ms) repeats per session. 4 alternative
  approaches documented in
  `.claude/context/plans/v0.2.13-candidates-2026-05-16.md`;
  recommendation is to wait for upstream HTTP-MCP spec movement
  rather than build a custom daemon shim.
- **2 cargo test parallelism-sensitive flakes** (pre-existing
  from v0.2.11): `commands::kg_sync::tests::concurrent_drain_does_not_deadlock_on_large_stderr`
  + `stall_watchdog_kills_silent_subprocess` (subprocess
  fork/exec ENOENT under high concurrency). Pass with
  `--test-threads=1` and in isolation. Documented in
  `.claude/context/plans/v0.2.13-candidates-2026-05-16.md`.
- **2 OS-keychain shared-state cargo test flakes**:
  `commands::installer::tests::github_pat_keychain_tests::pat_file_to_keychain_migration_uses_user_slot_after_module_id_flip`
  + `register_github_pat_preserves_existing_user_file`. Both
  pass in isolation; race is on the OS keychain backend's
  shared state under parallel `cargo test`.

## [0.2.11] — 2026-05-16

Stability release. The orchestrator's own infrastructure (compose
files, boot-service auto-wiring, container lifecycle, lean-ctx
integration, project bundle migration, MCP server surface) had a
series of latent bugs that surfaced during the 2026-05-15 / 2026-05-16
debugging session. All fixed with cross-OS coverage on Linux + macOS +
Windows and "no hardcoded user paths" discipline. Goal: a release
that doesn't need to be upgraded for a while.

**Major theme — MCP simplification**: Ollama MCP and 3 of 4 Search
MCP tools were removed as redundant with Claude's native capabilities.
Ollama remains as embedding infrastructure (Weaviate vectorizers); the
MCP wrapper layer is gone. Search MCP narrowed to just `search_papers`
(OpenAlex + arXiv). SearXNG container also dropped (no surviving tool
needed it). Visible deferral entries surface in
`<project>/.claude/context/UPDATE_DEFERRED.md` for upgraders with
existing `~/.claude.json` entries.

**Major theme — container lifecycle hardening**: PR-12 + PR-13 + PR-15
form a defense-in-depth stack around the 2026-05-16 boot-cascade
failure mode (docker-without-daemon-access → CDI race → conmon killed
→ state-DB desync). bash hooks recover at SessionStart, launcher Rust
recovers continuously while open, install.py auto-repairs stale
systemd unit `WorkingDirectory=` paths on `--update`.

**Major theme — pytest test-isolation**: PR-16 fixes a class of bug
where tests called real install code without sandboxing the
`~/.config/systemd/user/` write path. Every developer running
`pytest tests/test_materialize_boot_service.py` had been silently
corrupting their own systemd unit since PR-12 landed. New
`VCT_USER_HOME_OVERRIDE` env var + autouse fixture make this
impossible going forward — prevention, not recovery.

### Added

- **Ensure-containers zombie recovery hook** (PR-13,
  `templates/hooks/ensure-containers.{sh,ps1}`): detects Podman
  state-DB-desync containers (PID dead per `/proc/<pid>` while
  `podman ps` still reports "Up") at Claude Code SessionStart.
  Force-removes the zombie via `runc delete --force` + `podman rm
  --force`, then recreates via the `launch-claude-mcp-stack.sh`
  wrapper if shipped (preserves CDI-wait + daemon-access semantics).
  Audit log to `~/.local/state/vct/container-recovery.jsonl`
  (Linux) / `$LOCALAPPDATA\vct\container-recovery.jsonl` (Windows).

- **Launcher Rust container-lifecycle defense layer** (PR-15,
  `launcher/src-tauri/src/services/runtime.rs` +
  `commands/lifecycle.rs`):
    - G1: `daemon_usable_probe()` validates `docker info` Server:
      section / `podman info` exit 0 before committing to a runtime.
      Prevents the 2026-05-16 cascade-class bug where a user with
      Docker Desktop installed but not in the docker group got
      runtime detection succeeding silently while every subsequent
      compose call failed.
    - G2: zombie-container detection in `services_status()` plus a
      new `recover_zombie(container_name)` Tauri command. Frontend
      can now show a "stuck" indicator with a Recover button.
      `ServiceRuntimeState` gets a new `zombie: bool` field
      (serde-defaulted for backward-compat).
    - G3: launcher's `services_start_all` / `services_restart_all`
      prefer the `launch-claude-mcp-stack.{sh,ps1}` wrapper over
      direct `compose up -d`. Prevents the `vco_code_embed` boot
      race where GPU container compose-up beats the NVIDIA CDI
      refresh, leaving the container stuck in "CREATED" state.

- **Boot-service auto-repair** (PR-12, `install.py`): new
  `_repair_systemd_unit_working_dir()` helper fires on
  `install.py --update`. Detects stale `WorkingDirectory=` values
  pointing at moved / deleted install paths (a common failure mode
  for users who relocated their orchestrator clone) and re-renders
  the unit at the correct path. Emits `boot_service_path_repaired`
  deferral so Claude Code's next-session check surfaces the change
  with the exact `systemctl --user daemon-reload` command to apply
  it.

- **Runtime detection daemon-access check (bash side)** (PR-12,
  `scripts/launch-claude-mcp-stack.sh`): new `_runtime_usable()`
  validates `docker info` / `podman info` actually exit-0 before
  the wrapper commits to that runtime. Pairs with PR-15 G1
  (launcher Rust side).

- **Runtime.txt multi-path search** (PR-12): the wrapper now probes
  `$VCT_STACK_RUNTIME_FILE` → `$VCT_STACK_WORKING_DIR/state/install/`
  → `$VCT_ORCHESTRATOR_ROOT/state/install/` → `<script_dir>/../state/
  install/` for the persisted runtime choice. Makes the wrapper
  resilient to a stale systemd unit `WorkingDirectory=` pointing at
  a deleted path.

- **Global lean-ctx detection** (PR-11, `install.py::_check_global_lean_ctx_hooks()`):
  defensive check at install start for global lean-ctx artifacts
  (`~/.claude/settings.json` `hooks.PreToolUse` entries containing
  "lean-ctx", `~/.claude/hooks/lean-ctx-*` files). When detected,
  emits a LOUD warning to stderr + `global_lean_ctx_hooks_detected`
  deferral entry with the exact removal commands. The 2026-04-30
  and 2026-05-15 fork-bomb incidents both involved global lean-ctx
  hooks; this surface lets future users hit the same trap less
  often. New CLI flag `--suppress-lean-ctx-warning` for users who
  intentionally keep them (e.g. lean-ctx development).

- **Storage UX choice (named vs bind mount)** (PR-10A,
  `launcher/src-tauri/src/commands/storage_ux.rs` + Svelte
  Settings → Storage card): user can pick between runtime-managed
  named volumes (default, recommended) and a user-chosen bind path
  via the Settings GUI, with auto-detection of pre-existing legacy
  volumes from prior installs. STRICT volume allowlist (`vco_*`
  prefix + curated legacy names) prevents the launcher from ever
  offering to alias another project's data into our compose
  mountpoints. Override.yml generator covers 3 shapes: empty
  (named-default), bind-mount per service, external-alias per
  service. 24 Rust unit tests cover the allowlist boundary +
  render idempotency + atomic write.

- **Legacy KG / codegraph collection detection on Add Project**
  (PR-10B, `vco_lib/project_init.py`): when adding a new project,
  the install pipeline now detects pre-existing same-suffix
  legacy KG / codegraph collections (Levenshtein-based prefix
  similarity check + Weaviate REST API call for object counts +
  embedding-dim sniff via schema). Emits per-project deferral
  entries listing the candidates so the user can decide to adopt
  / archive / drop. 35 new tests.

- **GUI lean-ctx toggle in HooksTab** (PR-6,
  `launcher/src/lib/project-state/HooksTab.svelte` +
  `launcher/src-tauri/src/commands/claude_env.rs`): per-project
  3-state toggle (off / on / default) for `VCO_LEAN_CTX_DEFAULT`.
  Writes `.claude/env`. 19 Rust unit tests for the
  `get_claude_env_value` / `set_claude_env_value` Tauri commands.

- **Pytest test-isolation sandbox** (PR-16, `install.py` +
  `tests/test_materialize_boot_service.py`): new
  `_user_home_for_install()` helper consolidates all
  systemd-unit / launchd-plist / repair-function writes through a
  single env-overridable lookup (`VCT_USER_HOME_OVERRIDE`). New
  autouse fixture sandboxes every test in
  `test_materialize_boot_service.py` so a forgotten
  manual-monkeypatch by a future test author can no longer corrupt
  developer machines' real systemd state. Includes a regression
  guard test that proves the real user systemd unit `mtime` is
  unchanged across a full test-suite run.

### Changed

- **MCP server surface simplified** (PR-14a + PR-14b + PR-14c):
    - Ollama MCP server removed entirely from default install
      (`claude_mcp_servers/ollama_mcp/` deleted,
      `mcpServers.ollama` block removed from both Linux + Windows
      settings templates, manifest `vct-ollama.json` deleted).
      Ollama as embedding **infrastructure** (Weaviate qwen3-
      embedding vectorizer + code_embedding_service fallback)
      remains unchanged.
    - Search MCP narrowed to only `search_papers` (OpenAlex +
      arXiv academic paper search). The dropped tools
      (`web_search`, `search_code`, `fetch_page`) are all
      redundant with Claude's native WebSearch / WebFetch + the
      `site:github.com lang:X` qualifier pattern. Manifest
      `vct-search.json` updated: removed `github_pat` secret +
      `SEARXNG_URL` setting; version bumped 0.3.1 → 0.4.0.
    - SearXNG container removed from default compose
      (`searxng_data` volume gone; `templates/searxng/` template
      deleted; `install.py::_materialize_searxng_settings()`
      function removed).
    - CLAUDE.md / README.md / `docs/features/02-mcps-and-agents.md`
      / 18 agent templates / 2 skill templates / 8 KG nodes
      updated to reflect the new shape. New KG node
      `knowledge/concepts/mcp-simplification-v0211.md` documents
      the decision rationale.
    - 3 visible deferral entries for upgraders with existing
      `~/.claude.json` `ollama` blocks or SearXNG remnants:
      `searxng_removed_from_default_install`,
      `ollama_mcp_deprecated`, `search_mcp_simplified`. Nothing
      auto-removed from user state — clear messages with the
      exact cleanup commands.
    - Search MCP wrapper `claude_mcp_servers/search_mcp/server.py`
      net -322 LOC after stripping dropped tools.

- **SSRF whitelist in `pre-tool-use.{sh,ps1}` hooks** (PR-17):
  removed `localhost:8888` (SearXNG, no longer ships) and the
  `mcp__search__fetch_page` tool-name guard (tool removed in
  PR-14a). Added `localhost:11440` (`code_embed`) which was missing.
  Explanatory comment retained pointing maintainers at PR-14a
  for the v0.2.11 deprecation rationale.

- **Effort-level guidance for ad-hoc agent spawning** (PR-14c,
  `CLAUDE.md`): default recommendation flipped from `xhigh` to
  `high` for substantive work. Reserve `xhigh` / `max` only for
  genuinely deep-reasoning tasks where `high` has empirically
  fallen short. `high` is the right default for ad-hoc spawned
  agents; `xhigh` is overkill in most cases and adds latency /
  token cost without proportional quality gain.

- **`vct-module.json` canonical counts updated**: 5 MCP servers →
  4 (3 Python-stack containerized — weaviate_mcp, search_mcp,
  code_embedding_service — plus 1 system-managed Playwright). The
  comment field documents the v0.2.11 simplification.

### Fixed

- **`vco_code_embed` stuck in CREATED state after boot** (PR-15
  G3 + PR-12 wrapper changes): GPU container compose-up was racing
  the NVIDIA CDI refresh (`/var/run/cdi/nvidia.yaml` not yet
  populated). Now the launcher invokes the wrapper script which
  waits up to ~10s for CDI to be ready before bringing GPU
  containers up. Prevention, not recovery.

- **Docker-without-daemon-access cascade** (PR-12 + PR-15 G1):
  users with Docker Desktop installed but not in the `docker`
  group used to get runtime detection succeeding silently while
  every subsequent compose call failed permission-denied. Now
  rejected at detection time, runtime falls through to Podman.

- **Test corruption of user's real systemd unit** (PR-16): see
  Added section. Was silently corrupting developer machines
  since PR-12 landed.

- **Stale `WorkingDirectory=` in systemd unit across install
  moves** (PR-12): when users relocated their orchestrator clone,
  the systemd unit kept pointing at the old path. Now auto-
  repaired on `install.py --update` with a visible deferral.

- **Search MCP `vct-search.json` declared `github_pat` secret it
  no longer needed** (PR-19): cargo test asserted the secret was
  declared; PR-14a + PR-14b removed it (since `search_code` is
  gone). Test inverted to assert the secret is NOT declared
  (regression guard).

- **`check-no-fork-bomb.sh` missing env-scrub line** (PR-19): the
  hook had the `VCT_DISABLE_HOOKS` opt-out but not the standard
  `unset SUPABASE_KEY GITHUB_TOKEN ...` scrub. Added for parity
  with every other VCO hook (defense-in-depth, not active
  vulnerability — the hook doesn't process secrets).

### Removed

- `claude_mcp_servers/ollama_mcp/` (entire directory + Python module +
  requirements.txt). MCP layer dropped; Ollama as infrastructure
  stays.
- `claude_mcp_servers/search_mcp/server.py` tools: `web_search`,
  `search_code`, `fetch_page` (only `search_papers` survives).
- `templates/searxng/` (template directory) + SearXNG service from
  `claude_mcp_servers/compose.yaml` + `install.py::_materialize_searxng_settings()`
  + `templates/settings.json.*.template` `SEARXNG_URL` env vars +
  `launcher/bundled_manifests/vct-ollama.json` manifest.
- `tests/test_ollama_vision_gating.py` (tested the deleted MCP).

### Migration notes (for users on v0.2.5–v0.2.10)

- **CHANGELOG re-anchoring**: v0.2.5 through v0.2.10 shipped without
  per-release CHANGELOG entries during a period of fast iteration
  (see git tags + merged PRs at each version for content). v0.2.11
  re-establishes the Keep-a-Changelog discipline going forward.
- **`~/.claude.json` cleanup**: if your file has an `ollama` block
  under `mcpServers`, you'll see an `ollama_mcp_deprecated` deferral
  notice on next `install.py --update`. Remove the block manually
  for a clean state. Ollama-as-infrastructure (the container) is
  unaffected.
- **SearXNG container**: if you had it running locally, you'll see
  a `searxng_removed_from_default_install` deferral with the
  cleanup commands. No urgency; the container just becomes orphaned
  cruft.
- **Stale systemd unit**: `install.py --update` will detect + repair.
  If you see a `boot_service_path_repaired` deferral, run
  `systemctl --user daemon-reload` to apply.

### Skipped this release (deferred to v0.2.12)

- Launcher-centralized MCP env architecture (one `~/.claude.json`
  MCP entry per server + wrapper.sh trampoline reading per-project
  env from a launcher hub API). 2-3 day Rust + SQL + shell work;
  too big for the stability release.
- Install-time storage prompt in install.py CLI (PR-10A ships the
  launcher GUI surface; CLI prompt requires `install.py` to shell
  out to the launcher binary).
- `lean-ctx-rewrite.{sh,ps1}` envelope-coverage runtime test
  (requires installing lean-ctx in CI; exempted from envelope-
  literal test for now since envelope is produced by upstream
  binary not by the wrapper).

## [0.2.5] — [0.2.10] — internal iterations

Multiple releases between 0.2.4 (2026-05-12) and 0.2.11 (2026-05-16)
shipped without per-release CHANGELOG entries. See `git log
v0.2.4..v0.2.10` for the commit history and the corresponding GitHub
release pages for shipped artifacts. v0.2.11 re-establishes the
Keep-a-Changelog discipline going forward.

## [0.2.4] — 2026-05-12

Same-day follow-up to 0.2.3. Three bugs surfaced while adding SD15 (a
project registered by a pre-v0.2.0 orchestrator) via the v0.2.3
launcher.

### Fixed

- **Schema incompatibility on pre-existing Weaviate collections**
  (blocking). Two distinct mismatches stopped add-project for users
  with old orchestrator state:
    1. **Case-only naming conflict** — old lowercase `<Project>_development`
       blocked the new capital `<Project>_Development` (Weaviate
       treats case-only differences as "similar"). POST /v1/schema
       returned 422 "class already exists: found similar class".
    2. **Multi-named-vector mismatch** — pre-2026-04 collections
       carried both `ollama_embed` (legacy snowflake) AND
       `qwen3_embed` named vectors. Current sync sends a single
       vector. Insert returned 422 "configured with multiple named
       vectors, but received a single vector".
  `vco_lib.project_init.bootstrap_collections` now diffs the actual
  schema of any pre-existing target against the current spec.
  Detection is narrow: additive property drift still fixes via the
  in-place `patch_props` path (Weaviate 1.28.4 allows it); regen is
  reserved for what genuinely cannot be patched on a running
  collection (vectorConfig slot changes, indexNullState, legacy
  no-vectorConfig). When regen IS needed, the collection is dropped
  + recreated with current spec; the existing post-bootstrap
  `kg-sync` task re-ingests from `knowledge/**/*.md` (the lossless
  source of truth). Sub-second per collection on a healthy Weaviate.
  Surfaced via the existing `warnings[]` channel of
  `run_bootstrap_collections` — no new banner state (JSON envelope's
  `regenerated[]` array IS forward-compatible with a real banner if
  promoted later).

- **Optimistic counter lie on subprocess crash**. `kg_syncs` rows
  could report `kg_succeeded: N` despite the script crashing on the
  very first insert — every `🔄 Syncing node:` log line incremented
  the optimistic counter; the final `📊 KG: X succeeded, Y failed`
  summary never emitted; the optimistic count persisted as truth.
  `commands::kg_sync.rs::run_sync_task` now reconciles on crash: if
  the subprocess exits non-zero AND the canonical summary line was
  not parsed, the not-yet-summarized stage's counters reset to
  (succeeded=0, failed=total). Stage-aware: KG-completed-then-Docs-
  crashed only resets the Docs counters. Investigation finding:
  `commands::kg_summary.rs` and `commands::codegraph.rs` do NOT have
  this pattern — kg_summary returns a per-invocation `NodeOutcome`
  enum (always confirmed outcomes); codegraph parses
  `files_analyzed` only on `out.status.success()` (failure branch
  explicitly uses 0). Fix scoped to kg_sync only.

- **AddProject dialog "previous orchestrator content" banner
  rendered as 3 broken columns**. The pre-existing-content sentence
  was passed as separate Svelte children into a flex container,
  splitting "X agents, Y skills will be" / "preserved" / "; hooks…"
  into adjacent boxes — "preserved" hidden behind the X close
  button. Fix: `leftoverSummaryText()` TypeScript helper assembles
  the full sentence as a single string; rendered as plain text in a
  single block. The inline `<strong>preserved</strong>` emphasis is
  dropped (reintroducing it would require `@html` plus explicit
  escaping of every dynamic count — risky for decorative emphasis).
  Plain sentence reads cleanly and matches the dialog's flat tone.

### Tests

- **+ 23 new tests** (Rust 579, was 575; Python 837, was 818 net of
  one pre-existing unrelated failure):
  - 19 Python tests in `test_vco_lib_project_init.py` covering
    `_schema_incompatible` decision matrix,
    `_extract_similar_class_name` (both JSON-escaped + unescaped 422
    bodies), `_drop_and_recreate` happy path + Weaviate-down failure
    path, and the end-to-end bootstrap regen flow.
  - 4 Rust tests in `commands::kg_sync` for
    `reconcile_optimistic_counts_on_crash` (KG-only, Docs-only,
    both-stages, summary-parsed-no-reset).

### Notes for upgraders

- **Existing projects with stale schemas** will trigger one regen
  pass on their next bootstrap (via the `Re-sync KG` header button
  or a manual `python -m vco_lib.project_init
  bootstrap-collections`). The regen is lossless — Weaviate is
  re-populated from `knowledge/**/*.md` immediately afterward. No
  data on disk is touched.
- The `regenerated[]` JSON envelope field is new in 0.2.4. External
  tooling parsing the bootstrap-collections JSON output should
  ignore unknown fields (the existing `actions[]` / `errors[]`
  channels are unchanged).

---

## [0.2.3] — 2026-05-12

Same-day follow-up to 0.2.2. Adds the third instance of the
"background task with banner + pill + retry + resume" pattern: auto-
backfill of `<project>/knowledge/.node_formats.json` (the LLM-generated
summary sidecar consumed by `hybrid_search`'s `summary` tier, score
0.42–0.55).

Before 0.2.3, that sidecar was only populated lazily by the
`PostToolUse` hook `kg-summary-generator.{sh,ps1}` when a user edited
each KG node in a Claude session. A project with 50 pre-existing
nodes therefore needed 50 Claude sessions to fully populate. With
0.2.3 the launcher walks `knowledge/**/*.md` once on add-project and
shells out to `templates/scripts/generate-kg-summary.py` for each
file, in the background.

### Added

- **KG-summary auto-backfill on add-project**
  (`commands::kg_summary`, `db::kg_summaries`, migration 012). After
  bundle install, `create_project_v2` queues a `kg_summaries` row and
  spawns the summariser as a background task (in parallel with the
  kg-sync task added in 0.2.2 — not chained; the summariser reads the
  `.md` file body directly and only needs Weaviate for the optional
  per-chunk path on multi-chunk nodes). Progress streams to the GUI
  via the `kg-summary-progress` Tauri event with live `nodes_total`,
  `nodes_succeeded`, `nodes_unchanged`, `nodes_failed`,
  `nodes_skipped`, and `backend` counters. Mirrors
  `commands::kg_sync::spawn_initial_sync` line-by-line — same DB
  shape, same `current_dir(std::env::temp_dir())` discipline, same
  `CREATE_NO_WINDOW` on Windows, same race-checks for
  user-unregister-mid-run.

- **Three-tier backend fallback in `generate-kg-summary.py`**:
  `claude` CLI on PATH → Ollama at `KG_SUMMARY_OLLAMA_URL` (default
  `http://localhost:11435`, model `KG_SUMMARY_OLLAMA_MODEL`,
  default `qwen3.5:9b`) → `ANTHROPIC_API_KEY` direct → silent skip
  (`exit 0` with `KG-summary: no backend available`). Forced backend
  via `KG_SUMMARY_BACKEND=cli|ollama|api|skip`. Per-call timeout via
  `KG_SUMMARY_TIMEOUT` (default 180 s). The launcher detects the
  no-backend marker on the first node, hard-stops the walk, and
  transitions the row to `skipped` with an actionable hint
  (install `claude` CLI / start Ollama / set `ANTHROPIC_API_KEY`) —
  no point invoking the same script for the remaining N nodes.

- **`Re-build KG summaries` header button** on `/project/[id]`,
  third in the row next to `Re-build code graph` (0.2.x) and
  `Re-sync KG` (0.2.2). Mirrors `resyncKg` end-to-end (same
  `.rebuild-btn` style, `rebuildingSummaries` loading-state guard,
  `retry_kg_summary` Tauri command, toast convention). The
  summariser content-hashes each node, so repeated clicks on an
  already-summarised project are a cheap no-op (logged as
  `nodes_unchanged`).

- **Third banner on `/project/[id]`** (`KgSummaryBanner.svelte`),
  mounted on top of the existing pair (`KgSummaryBanner` →
  `KgSyncBanner` → `CodeGraphBuildBanner`, top-down). Newest task
  on top, matching the add-project spawn order (codegraph → kg-sync
  → kg-summary). Self-managed visibility: `pending`/`running`/
  `failed` always visible; `success`/`skipped` auto-hide 30 s after
  `finished_at_iso`. The `skipped` state surfaces the
  `Show details` button so a user without a summariser backend
  sees the install hint inline.

- **Third pill on `/project`** (`KgSummaryPill.svelte`), mounted
  next to `CodeGraphBuildPill` and `KgSyncPill` in each project's
  list row. Passive read-only — the project page is the action
  surface.

- **Resume-after-crash extended to a third task type.** On launcher
  boot (in `lib.rs::setup()`), the existing two-phase sweep
  (introduced 0.2.2) now also runs `kg_summary::resume_pending_summaries`:
  phase 1 marks any `kg_summaries.status='running'` rows as `failed`
  with `"launcher crashed mid-run; click Retry to re-run"`; phase 2
  re-spawns `status='pending'` rows. The boot-log line now reports
  three task types:
  `[vct] resume-sweep: code-graph (...); kg-sync (...); kg-summary (running→failed: N, pending respawned: M)`.

- **Marker-string drift guards.** `commands::kg_summary` ships two
  unit tests (`no_backend_marker_string_matches_script_log_line`
  and `unchanged_marker_string_matches_script_log_line`) that hold
  a literal snippet of the canonical log lines from
  `generate-kg-summary.py` and assert that the `NO_BACKEND_MARKER`
  and `UNCHANGED_MARKER` constants still substring-match. A future
  rename of the script's log message will fail the test loudly
  rather than silently mis-classifying every run as `succeeded`.

### Changed

- **Boot-log resume-sweep line now reports all three task types**
  (code-graph + kg-sync + kg-summary). 0.2.2 reported two.

### Tests

- **+25 new tests** (575 total, was 550 after 0.2.2). New coverage:
  `db::kg_summaries` (9 — row CRUD, state transitions,
  invalid-status, `log_tail` truncation, FK cascade, resume sweep);
  `commands::kg_summary` (16 — enumerate-walk × 4,
  `resolve_summary_script` × 2, `resolve_venv_python` × 3,
  `parse_backend_from_stdout` × 2, `tail_log` × 2, `append_log` × 1,
  marker-string drift × 2). `cargo test --lib`: 575 passed, 0 failed,
  1 ignored. `cargo check`: clean. `svelte-check`: 0 errors.

### Notes for upgraders

- **No migration required for users.** Migration 012 creates
  `kg_summaries` on first boot; pre-0.2.3 projects can opt-in via
  the new `Re-build KG summaries` header button. The summariser
  content-hashes nodes, so existing projects with the sidecar
  partially populated by the on-edit hook will re-run as
  `unchanged` (cheap no-op) for any node whose body hasn't changed.
- **No backend?** Users without `claude` CLI installed and without
  Ollama running on `KG_SUMMARY_OLLAMA_URL` will see the third
  banner go yellow `skipped` after the first node's subprocess
  detects "no backend available". The `Show details` toggle shows
  the install hint. Summaries also still backfill incrementally on
  each subsequent `knowledge/**/*.md` edit via the PostToolUse hook
  `kg-summary-generator.{sh,ps1}` — the 0.2.3 work is purely a
  startup optimisation; it doesn't change the lazy path.
- **First boot after upgrade** may sweep stale `running` rows
  across all three task types from a previous launcher session.

## [0.2.2] — 2026-05-12

Single-commit cycle focused on closing the add-project KG-sync gap and
elevating background-task status from pills to full-width banners.

Originated from the SimRacing_AI revival (2026-05-12): user added a
project with ~58 pre-existing `knowledge/**/*.md` nodes via the launcher
GUI, but `SimRacingAI_KnowledgeGraph` stayed empty until a manual
`.claude/scripts/kg-sync --all`. The launcher add-project flow now
handles this automatically — same lifecycle pattern as the existing
code-graph build.

### Added

- **KG auto-sync on add-project** (`commands::kg_sync`,
  `db::kg_syncs`, migration 011). After bundle install,
  `create_project_v2` queues a `kg_syncs` row and spawns
  `.claude/scripts/kg-sync --all` as a background task. Progress
  streams to the GUI via the `kg-sync-progress` Tauri event;
  failure surfaces a Retry button. Mirrors
  `commands::codegraph::spawn_initial_build` line-by-line — same DB
  shape, same cross-platform script resolution
  (`kg-sync.ps1` on Windows, `kg-sync` POSIX wrapper elsewhere),
  same `CREATE_NO_WINDOW` Windows handling, same
  `eq_ignore_ascii_case` for macOS HFS+ folding.

- **`Re-sync KG` header button** on `/project/[id]`, next to
  the existing `Re-build code graph`. Mirrors `rebuildCodeGraph`
  end-to-end (same `.rebuild-btn` style, loading-state guard,
  toast convention).

- **Resume-after-crash** for both background task types. On
  launcher boot (in `lib.rs::setup()`), two-phase sweep:
  Phase 1 marks any `status='running'` rows as `failed` with
  `"launcher crashed mid-run; click Retry to re-run"` — the
  banner renders the failed state so the broken lifecycle stays
  visible (silent re-spawn would mask the crash). Phase 2
  re-spawns `status='pending'` rows via the same mechanism as
  `create_project_v2`. Honors the long-standing
  `list_pending_code_graph_builds` docstring contract that
  'running' rows are NOT auto-resumed.

### Changed

- **Pills → full-width banners** on `/project/[id]`. New
  `KgSyncBanner.svelte` + `CodeGraphBuildBanner.svelte` replace
  the previous pills above the tab-nav, stacked vertically
  (KG-on-top — newer task). Self-managed visibility:
  `pending`/`running`/`failed` always visible; `success`/`skipped`
  auto-hide 30 s after `finished_at_iso`. Inline expand-on-click
  for failure detail + Retry. Modeled after
  `BrowserModeBanner` (row decoration) + `.orch-banner` (action-row
  layout). Slimmed-down passive pills remain on the projects-list
  page (`/project`) where a banner-per-row would break the grid.

- `list_pending_code_graph_builds` is no longer dead code —
  `#[allow(dead_code)]` stripped now that
  `codegraph::resume_pending_builds` wires it from launcher boot.

### Tests

- **+ 7 new tests** (550 total, was 543). New coverage:
  `mark_orphaned_running_*` helpers (insert 'running' row → marked
  failed with crash-recovery message, no new task spawned);
  `list_pending_*` round-trip (insert 'pending' → respawn yields
  fresh task); kg_syncs row CRUD + state transitions +
  `log_tail` truncation + project FK cascade.

### Notes for upgraders

- **No migration required for users**. Existing projects keep
  their `code_graph_builds` rows; new `kg_syncs` table is created
  on first boot via migration 011 (no data backfill — pre-existing
  projects can opt-in via the new `Re-sync KG` header button).
- **First boot after upgrade** may sweep stale `running` rows from
  a previous launcher session (now marked failed with the recovery
  message). The boot log line
  `[vct] resume-sweep: code-graph (running→failed: N, pending respawned: M); kg-sync (...)`
  reports the counts.

---

## [0.2.1] — 2026-05-10

26 commits since 0.2.0 (#172–#197). Themes: per-project secret grants
+ per-requester active gate (the headline 0.2.1 work, #187/#188),
launcher GUI polish from the post-0.2.0 backlog (#196/#197),
hook-contract audit findings (#190 + sweep), release-pipeline
modernisation (uniform `.zip`, externalized local config, archive
contents), and a substantial doc-audit pass (#180–#183).

### Added

- **Per-project secret grants + per-requester active gate** (#187, #188).
  Migration 009 extends `secret_active_state` with a
  `requester_project_id` PK column and adds a new `secret_grants`
  table. The hub `project_env` resolver walks `list_grants_by_grantee`
  for the consuming project and threads the project_id as the
  per-(secret × requester) gate, so a per-project pause on a shared
  secret takes effect even though the keychain row is shared, and a
  per-project grant lets one project see another's secret without
  re-entry. Five new Tauri commands surface the grants/pause UX:
  `grant_secret`, `revoke_secret_grant_cmd`, `list_grants_for_project`,
  `pause_secret_for_project`, `resume_secret_for_project`. Cross-
  launcher schema-compat path probes a sibling DB's
  `secret_active_state` for the `requester_project_id` column via
  `PRAGMA table_info` and falls back to the legacy single-row query
  when the sibling pre-dates migration 009.
- **`vct-config.toml` local-machine config** (#189, #192). New
  `LocalConfig` loaded at launcher startup from next to the binary.
  Resolution order: env-var override → `vct-config.toml` →
  compiled default. First externalized field is `weaviate_url` (the
  user's local KG instance address). Product-fixed URLs (license
  validate, GitHub repo) stay baked in. Release archive ships
  `vct-config.toml` (renamed from `vct-config.example.toml` source)
  alongside the binary.
- **GUI: shared-tab key-collision badge, "Update all projects" button,
  per-project MCP toggle UI** (#197). Three post-0.2.0 backlog items:
  - SecretsPanel rows render a "shadowed" badge when a key is set at
    multiple scopes (`per_project > shared > global` precedence). New
    `list_user_secret_keys_v2` returns `is_shadowed` + `winning_scope`
    per row.
  - New `update_all_projects(opts)` Tauri command iterates registered
    projects sequentially. New three-phase `UpdateAllProjectsModal`
    Svelte component drives the UX (confirm → running → report).
  - PermissionsTab gains an MCP-servers section above the generic
    permissions table. New `list_project_mcp_permissions` +
    `set_project_mcp_permission` commands. Default-enabled semantic:
    enable=DELETE the row, disable=UPSERT explicit
    `config.enabled=false`. Env-writer mirrors disabled state into
    `.claude/settings.json::disabledMcpjsonServers`.
- **Custom MCP tab now populated by initial project registration** (#196).
  Migration 010 adds `project_mcp_servers` table, populated by a new
  `populate_mcp_servers` step in `project_state_populate.rs` that
  mirrors `<folder>/.claude/settings.json::mcpServers` AND
  `<folder>/.mcp.json` into the table. `is_user_added=true` flag
  driven by a bundled-allowlist (`weaviate-kg`, `ollama`, `search`,
  `code-embedding`, `playwright`, `vct-coordination`). Startup
  backfill in `lib.rs::run` re-runs the populate step for projects
  registered before migration 010 (idempotent — preserves user
  toggles).
- **Lightweight Rust wiring for `--lightweight` re-install** (#196).
  `InstallConfig` carries `lightweight: bool` and
  `lightweight_old_path: Option<String>`; `install_orchestrator`
  honours them by skipping the file-copy stage and invoking
  `install.py --lightweight [--lightweight-old-path <path>]`
  directly. The OnboardingWizard's re-install conflict modal exposes
  a "Fast reinstall" checkbox + optional previous-path text input.
- **Code-graph rank-tier helper extraction** (#191). New
  `_format_code_result_by_rank` shared helper in
  `claude_mcp_servers/weaviate_mcp/server.py`; used by both the MCP
  `search_code_graph` path and the `.claude/scripts/query_code_graph.py`
  CLI (which the pre-edit hook invokes). Result content is now
  byte-identical across the two surfaces. Rank-keyed tier policy
  preserved (top-2 = full + siblings; rank 3-4 = truncated 1200 chars;
  rank 5+ = ref-only).
- **Hook-contract audit fixes** (#190). Five-fix wave: (1)
  `config-change-audit` reads stdin JSON instead of empty
  `$CLAUDE_TOOL_NAME`/`$CLAUDE_TOOL_ARGS` env vars (audit log was
  silently empty before); (2) `post-tool-security` routes credential
  alerts through `hookSpecificOutput.additionalContext` so the model
  actually sees them (was plain stdout, dropped by the PostToolUse
  contract); (3) `post-file-edit` rewrite — pure-status banners
  deleted, three real LLM nudges (CONTEXT_STATE expert-skill,
  workflow-system file edits, code-edit reminder) routed through
  `additionalContext`; (4) env-scrub + `VCT_DISABLE_HOOKS` short-
  circuit added to three hooks that lacked them; (5) new
  `LEAK_TEST_KEY` recognizer pattern so smoke tests use a synthetic
  marker instead of real-looking AWS-key fixtures.

### Changed

- **Release archives are uniform `.zip` for all OSes** (#192).
  Linux + macOS switch from `.tar.gz` to `.zip`; `.sha256` sidecar
  per archive. `zip` is preinstalled on `ubuntu-latest` and
  `macos-latest` runners; Windows continues to use `Compress-Archive`.
- **Release pipeline ships only per-OS archives + sha256** (#175).
  Standalone launcher binaries dropped from Release assets — they
  now live only inside the per-OS archive. Workflow artifacts retain
  the standalone binary for QA.
- **Cross-OS staging via `tar+excludes` instead of `rsync`** (#172).
  `rsync` is not preinstalled on the Windows GitHub-hosted runner;
  switching to `tar -cf - --excludes ... | tar -xf - ...` lets the
  same staging logic run on all three runner OSes.
- **Release archives now ship `docs/`, root `*.md`, `CLAUDE.md`,
  `KNOWN_ISSUES.md`, `MIGRATION-<version>.md`** (#185). Users get the
  full doc surface on extract; previous archives shipped only
  `BOOTSTRAP.md` + `LICENSE`. Doc-leak scrubber runs in CI before
  upload.
- **Mass-rename `background: true` → `async: true`** in all settings
  files (#190, 36 occurrences). Per current docs, `async` is the
  canonical hook-handler field; `background` was at best an
  undocumented alias.
- **Stale OS-EXEMPT-PARITY markers cleaned** (#191, 5 hooks). The
  `.sh` siblings of `pre-compact-save`, `code-graph-incremental`,
  `cost-tracker`, `kg-update-nudge`, `kg-summary-generator` had
  parity markers that were superseded by genuine cross-OS parity
  via `find-python.{sh,ps1}` helpers.

### Fixed

- **`register_github_pat` ↔ SecretsPanel `module_id` unification**
  (post-0.2.0 backlog #6, #195). The OnboardingWizard /
  `/preferences` Manage Token flow wrote the PAT at
  `vct._user_shared_.shared.installer/github_pat` while the SecretsPanel
  "Shared (this user)" tab wrote at
  `vct._user_shared_.shared.user/github_pat`. A user who registered via
  the wizard and later edited via the SecretsPanel ended up with two
  divergent keychain rows; reads from either path returned only that
  path's value, so the alternate row sat as a stale shadow. Both
  writers now use `module_id="user"` (the canonical user-bucket
  enforced by `is_user_emit_bucket`) and `vct-module.json::bundled_secrets[0].module_id`
  matches. Existing 0.2.0 installs are migrated on next
  `register_github_pat` call by `migrate_github_pat_installer_to_user_module_id`,
  which copies the value at the old `installer/` slot into the new
  `user/` slot (or, if both slots have values, keeps the user-bucket
  one because the SecretsPanel write happened later in time) and
  deletes the old keychain row plus its active-flag entry. Audited as
  `github_pat_module_id_migration` for traceability.
- **Pre-edit hook cache typo: `$KG_TMP_RAW` / `$CODE_TMP_RAW` →
  `$KG_RAW` / `$CODE_RAW`**. The cache file was always empty
  post-search because the wrong variable names were referenced; every
  subsequent edit of the same file re-ran the live search instead of
  replaying the cached blocks. Now the cache populates as designed
  and TTL replays work.
- **Pre-edit hook KG/codegraph injection dedup correctness +
  session_id-from-stdin sweep** (#176, #186). Producer/consumer header
  drift fixed: `rl_kg_search.py --hook-format` and
  `query_code_graph.py search --hook-format` now emit `KG: <title>`
  and `CODE: <full_name>` headers the pre-edit hook's regex actually
  matches. Whitespace-only filtered output no longer surfaces an
  empty `[Pre-edit context for ...]:` system-reminder. Cache stores
  raw pre-dedup blocks so replays apply the current seen-list.
  Universal stdin-JSON sweep across 13 hooks via
  `_lib/find-python.{sh,ps1}` resolves cross-OS Python invocation
  (`python3` is missing on Windows; `md5sum` and `stat -c %Y` are
  GNU-only).
- **CONTEXT_STATE.md staleness nudge: counter-based + session_id
  keying** (#177). The KG-update-nudge hook now triggers once per
  session_id when the cumulative work-units counter crosses
  thresholds, instead of firing repeatedly on every prompt. Atomic
  state persistence under `~/.claude/projects/<project>/state/`.
- **Column-rename migration recorded for column drift** (#184). A
  prior in-place column rename in `launcher.db` was never captured in
  the migrations registry; this PR adds the retroactive entry so
  fresh installs match upgraded ones byte-for-byte.
- **`post-git-commit-kg-sync` echoed "KG sync agent spawned" to stdout
  under `async: true`** (#193). PostToolUse plain stdout is silently
  dropped per the v2.1.x contract AND the wiring is fire-and-forget,
  so the line was doubly discarded. Replaced with a contract-explainer
  comment so future audits don't flag it.
- **`cookie@0.6.0` CVE-2024-47764 npm audit alert** (#194).
  `launcher/package.json` carries `overrides: { "cookie": "^0.7.0" }`
  pinning the lockfile to `cookie@0.7.2`; `npm audit` from `launcher/`
  reports 0 vulnerabilities.
- **Latent `query_code_graph.py::search_by_concept` bug**: was
  calling `near_vector` without `target_vector` on the dual-vector
  collections (which the new Weaviate client rejects). Fixed alongside
  the helper extraction (#191).

### Docs

- **Doc-audit Groups A–D** (#180, #181, #182, #183). Four-pass sweep
  across user-facing root + core (Group A), license module (Group C),
  features pages (Group B), and maintainer-doc triage (Group D).
  Factual + tone refresh; maintainer-internal references scrubbed
  from public files; `internal/` directory split for repo-only
  content.
- **README rewrite + verified comparison table** (#179). New
  comparison table grounded in feature-by-feature verification rather
  than marketing claims.
- **Post-ship 0.2.0 sweep** (#173). Version-ref drift, roadmap-label
  consistency, hub-auth section refresh.
- **CLAUDE.md guidance**: agents fix outdated knowledge they retrieve
  in the same turn rather than acting on stale info (#174); warning
  about parallel-agent worktree isolation failure modes (#178). The
  worktree-isolation warning was load-bearing for this 0.2.1 cycle —
  see the fresh KG node `parallel-pr-coordination-gotchas-2026-05-10`
  for the failure modes hit during the parallel-PR push.

### Notes for upgraders from 0.2.0

- The `register_github_pat` migration is automatic and runs on first
  invocation of the wizard / Manage Token flow after upgrade; the
  legacy `installer/` keychain slot is read-fallback-then-removed.
  No user action required.
- The migration-010 `project_mcp_servers` backfill runs at startup
  for every registered project that has zero rows (i.e. every
  pre-0.2.1 project). Per-project, idempotent; user toggles are
  preserved across runs.
- `vct-config.toml` is shipped at the archive root; users can edit it
  in place after install. Env-var overrides (`VCT_WEAVIATE_URL`,
  legacy `WEAVIATE_URL`) take precedence.
- Settings files use `async: true` instead of `background: true` —
  no behaviour change today (both currently work) but the doc-aligned
  spelling de-risks future versions.

## [0.2.0] — 2026-05-08

Iteration since v0.1.6. Substantial install-flow architectural
overhaul, secrets-architecture overhaul, freeze-investigation
hardening, drift-guard work, and maintenance. Headline themes:

- **Secrets architecture overhaul** (PR #171, 2026-05-08):
  keychain-canonical for everything written through the launcher
  (Onboarding `register_github_pat` and the new `/preferences`
  Manage Token panel both write to the OS keychain, not to disk).
  Hub `/projects/{id}/env` resolver path is the canonical reader;
  bundled wrappers consume it via the new
  `templates/scripts/vct_secrets_resolve.{sh,ps1}` helper. Items
  H1-H4 close the per-project / cross-launcher / shared-tab gaps;
  the in-tree `claude_mcp_servers/search_mcp/wrapper.sh` reads
  `$GITHUB_TOKEN` env first, hub resolver second. The legacy
  `~/.vct-secrets/git-credential-vct` helper is retired in favour
  of per-project `GITHUB_TOKEN` env propagation. The user-facing
  `vct` CLI under `tools/vct-secrets/` is unchanged. See
  [docs/MIGRATION-0.2.0.md](docs/MIGRATION-0.2.0.md) for the full
  matrix and migration recipe.
- **Hub authentication gate** (item H5, 2026-05-08): the launcher
  hub at `127.0.0.1:7700` now requires
  `Authorization: Bearer <token>` on every `/api/v1/*` route except
  `/api/v1/health`. The token is a fresh 32-byte CSPRNG value
  regenerated per launcher startup, persisted to
  `<vct_root_dir>/hub.token` (mode `0o600` on Unix). Closes the
  "any same-user process can curl localhost and exfiltrate the
  active secrets set" attack class. In-tree clients (resolver
  helpers, `vco` CLI, `hub_proxy`) read the token transparently.
- **Per-OS release archives** (`.github/workflows/release.yml`,
  2026-05-08): tag push (`v*.*.*`) builds and publishes
  `vibecoded-orchestrator-<version>-{linux-x64,macos-arm64,windows-x64}.{tar.gz,zip}`
  to GitHub Releases, with a `.sha256` sidecar each, alongside the
  existing standalone launcher binaries. SLSA build provenance
  attestation (Sigstore-signed) generated for the launcher binary
  inside each archive.
- **Non-destructive contract on user-owned files**: launcher
  unregister and `clear_github_pat` no longer rewrite or delete
  `.claude/env` outside the managed BEGIN/END block; user shell
  exports survive. The launcher never touches user-owned
  `~/.vct-secrets/projects/<project>/<key>` files written by the
  user-facing `vct` CLI.
- **Replace-existing guard on `register_github_pat`**: returns
  `EXISTS_DIFFERENT:<masked-prefix>` instead of silently
  overwriting a different PAT; GUI surfaces a confirm dialog.
  `force=true` proceeds with overwrite.
- **`/preferences` Manage Token UI**: rotate or clear the GitHub
  PAT outside the OnboardingWizard. Routes through the
  replace-existing guard above; "Clear" strips `GITHUB_TOKEN`
  from every registered project's env surface.
- **Install-flow overhaul** (PRs #139–#152, 2026-05-06): the
  user's "ONE VCO clone shared by all projects" model fully
  implemented. `ORCHESTRATOR_MANAGED_PATHS` as single source of
  truth (Rust + Python parse the same file). `ProjectEnvSettings`
  plumbs launcher state into per-project envs. Gitignore-aware
  `copy_recursive_sync`. Inspector + `update_orchestrator_at`
  gated by `validate_source_repo`. Codegraph venv resolution
  prefers `VCT_INSTALL_ROOT`.
- **New-user readiness** (PR #153, 2026-05-06/07): drift gate
  for `.claude/` ↔ `templates/` lockstep; GUI Option γ for non-FF
  auto-resync; `start-launcher.sh` build-hint fix.
- **Security + tightening** (PRs #154–#157, #163, 2026-05-07):
  tauri 2.11.0 → 2.11.1 CVE bump (origin confusion); cookie
  0.7.2 via npm overrides (Dependabot #1); BOM-strip in
  managed-paths parsers; `.vco-manifest.json` purge on unregister;
  codegraph mid-build unregister race silent.
- **Drift-guard fragility** (PRs #158, #162, #165, 2026-05-07):
  canonical env-key NAMES single source of truth (install ↔
  unregister structurally locked); CI cross-language
  managed-paths diff; full hook lockstep + sentinel rewire blocks
  for 7 PR-2-rewired scripts (drift gate's `EXPECTED_ASYMMETRIC`
  now empty).
- **UX polish + cleanliness** (PRs #159–#161, #164, 2026-05-07):
  `{{PROJECT_ROOT}}` placeholder in agent template subs;
  orchestrator-update banner clarification on per-project page;
  previously-registered leftovers banner in Add Project; gitignore
  hardening to prevent accidental RL Pro-tier IP leak.

Filed upstream:
- `anthropics/claude-code#56876` — `/tmp/claude-{uid}/.../tasks/`
  null-padded files (missing `ftruncate`).
- `tauri-apps/tauri#15353` — `generate_context!()` embeds
  `CARGO_MANIFEST_DIR` despite RUSTFLAGS remap.

Net change since v0.1.6: 23+ PRs to main; install flow
architecturally clean; all 4 declared Dependabot alerts triaged
or fixed.

## [0.1.1] – [0.1.5] — internal iterations

These versions existed only as internal pre-release iterations
during the 2026-04 → 2026-05 window. Substantive work happened
across many small commits without numbered releases; the
collected work landed as v0.1.6. Future released versions will
follow Keep a Changelog discipline more strictly per release.

## [0.1.6] — 2026-05-02

### Changed
- **Project install/update overhaul.** `create_project_v2` now bundles all
  orchestrator infrastructure on first install (hooks, scripts, agents,
  skills, MCP servers, per-project Weaviate collection bootstrap). New
  `update_project_v2` keeps installed projects in sync non-destructively
  via a manifest-based drift detector — user-modified files are preserved,
  conflicts produce a `.md` reference note for Claude Code to resolve.
  Schema migrations port existing collection data instead of recomputing
  embeddings. Install/init logic extracted into `vco_lib/project_init.py`
  as a single source of truth shared by launcher and CLI.
- **Asymmetric SHARED_KG semantics.** All projects can READ the shared
  knowledge graph; per-project toggle now gates only WRITES. Lets a
  project consume cross-project patterns without polluting the shared
  collection.
- **KG-update-nudge counter (v10.1).** Replaced the v9 `cache_creation`
  proxy (cost-accurate but work-blind) with a `work_units_total` formula
  that sums output tokens + bounded intake from Read/Write/Edit/Web/
  Agent/Bash. Thresholds: 175k first nudge, 50k subsequent. Per-tool
  caps prevent any single call from triggering on its own. Two
  adversarial Opus reviews validated the design.

### Added
- **Full cross-OS hook parity.** Every `.sh` hook now ships with a
  matching `.ps1` sibling. macOS bash 3.2 quirks fixed (no `[[ ]]`
  regex, no `${var,,}`, no associative arrays). Zero `OS-EXEMPT-PARITY`
  shortcuts.
- **Bytecode pre-compile (Step 11b).** New default-on install step runs
  `python -m compileall` on orchestrator Python directories so first
  import is ~50-200ms faster per cold module. Opt-out via `--no-compile`
  (Linux/macOS) or `-NoCompile` (Windows). Best-effort: per-directory
  failures warn but never abort. Cross-OS via stdlib `compileall`.
- Vercel token-leak guard hook bundled into installed projects.
- AMD ROCm overlay + VRAM-aware Ollama model selection.
- Phase 1 `~/.vct-secrets/` shared/per-project secret layout with
  legacy-path fallback.

### Fixed
- Bare `KG_COLLECTION` bug across all 4 env surfaces; canonical
  per-project collection naming end-to-end.
- KG-nudge baseline reset on `Edit`/`Write` to any `**/knowledge/**.md`
  (not only project-local).
- Agent effort frontmatter recalibrated per-model (Opus 4.7 caps
  `xhigh`, Haiku → `high`).

### Security
- Setup docs use placeholders (`<YOUR_LS_WEBHOOK_SIGNING_SECRET>`,
  `<YOUR_SUPABASE_PROJECT_REF>`) with instructions to generate via
  `openssl rand -hex 32`.
- Added `scripts/check-no-secrets.sh` pre-commit grep guard with a
  blocklist of token patterns to keep them out of commits.

## [0.1.0] — 2026-04-26 — Initial private release

### Added
- VCT Launcher v1.0 + v1.1 (Tauri 2 desktop app): 17 GUI screens including project management, KG / code-graph dashboards, audit log, modules catalog, license activation, settings, onboarding wizard.
- Knowledge Graph with semantic search (Weaviate + qwen3-embedding 1024-dim, optional OpenAI vectors, GraphRAG-style WikiLink traversal).
- Code Graph (Python AST + regex-based analysis across 9 languages, 5 entity types: `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`; optional Joern integration for CFG / PDG).
- 20 automation hooks (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `PreCompact`, `PostCompact`, `StopFailure`, `SessionEnd`, …).
- 4 MCP servers: `weaviate-kg`, `ollama`, `search`, `code-embedding`.
- 19 agents + 28 skills as templates dropped into `.claude/` at install time.
- Multi-surface compatibility: Claude Code CLI + VS Code extension + Claude Code Desktop app.
- Per-project env injection via `.claude/settings.json` (Anthropic-canonical) + `.vscode/settings.json` (VS Code) + `.claude/env` (CLI shell). User code is never touched.
- Container runtime support for podman and docker; shared services across projects on a single machine.
- License validator: free tier fail-open (so a flaky network never breaks startup), optional Pro / Admin tiers via Lemon Squeezy with a 3-day offline grace window.
- Local-only architecture: Weaviate + Ollama on-device; no data leaves the machine without explicit user consent.

### Security
- Hard whitelist for orchestrator-managed paths (zero-touch user code).
- Pre-flight install-safety check (`preflight_install_safety_check` Tauri command).
- Read-merge-write for all settings files (preserves user content).
- Atomic write patterns for `~/.claude.json` and `.vscode/settings.json`.
- No destructive container ops anywhere in install path (audit-tested).
- OS-keychain-backed secret storage (no plaintext on disk).
- Default-OFF telemetry (explicit opt-in required; default `.env` writes `VIBECODED_TELEMETRY=false`).
- Public alias for license validation (`https://api.vibecodedtools.it/validate-tier`); internal Supabase URLs are not committed to public source.

[Unreleased]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.5...v0.1.6
[0.1.0]: https://github.com/hotak92/vibecoded-orchestrator/releases/tag/v0.1.0
