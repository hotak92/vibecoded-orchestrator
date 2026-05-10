# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.5...v0.1.6
[0.1.0]: https://github.com/hotak92/vibecoded-orchestrator/releases/tag/v0.1.0
