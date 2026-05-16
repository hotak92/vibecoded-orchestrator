# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
