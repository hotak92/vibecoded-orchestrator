# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.28] — 2026-05-23

Same-day chained release after v0.2.27 (see
[`knowledge/concepts/same-day-chained-release-pattern.md`](knowledge/concepts/same-day-chained-release-pattern.md)
for why), shipping: (a) Wave 2 D — `install-bundle --update` no longer
resurrects user-disabled agents/skills (the install-side companion to
the Wave 1 FS-disable mechanism shipped in v0.2.27); (b) Wave 2 E —
launcher-startup migration that converts any legacy `enabled=0` rows
from older installs into the `.disabled/` filesystem layout + hook
registration in both OS settings templates; (c) **launcher KG-binding
seed-guard** that stops every launcher boot from clobbering a
user-customized `project_kg_bindings(primary)` row with the
orchestrator-root default (live-reproduced bug: two parallel chats
seeing 0 KG-search results because the binding had been silently
rewritten from `VCODev_KnowledgeGraph` to
`VibeCodedOrchestrator_KnowledgeGraph` on every boot); (d) **`.claude/settings.json
env` KG-keys backfill** — `install-bundle --update` and `install.py --update`
now populate `KG_COLLECTION` / `SHARED_KG_COLLECTION` / `DEVELOPMENT_COLLECTION`
into the canonical per-project env channel from launcher.db's
`project_kg_bindings` table (source of truth); (e) two RL telemetry
fixes from the `rl-logging-audit-report-2026-05-23` audit: project-name
canonicalization via slug (was producing 4 distinct cohort labels for
the same project) and asyncio strong-ref for the citation monitor task
(was hitting 97.7% orphan-citation rate from GC dropping the
unreferenced `create_task` handles); (f) per-model chunker preset in
the KG sync script (was hardcoded to qwen3-specific `max_tokens=2500`
regardless of the active embedding model); (g) Windows-side hardening:
UTF-8 BOM added to `agent-skill-keyword-suggest.ps1` so PowerShell 5.1
on Win10/11 can parse the non-ASCII characters in it.

### Added

- **`feat(install)` `install-bundle --update` no longer resurrects disabled agents/skills** (Wave 2 D). Pre-v0.2.28 a bundle update would re-copy any template file missing from `.claude/agents/` or `.claude/skills/`, silently undoing a user's disable choice. Both install paths (Rust launcher populate in `project_state_populate.rs`, Python `install-bundle` in `vco_lib/project_init.py`) now check both the enabled location AND the `.disabled/` sibling before copying. A new `skip-disabled` action bucket in the bundle-op schema explicitly documents the no-op. 20 new tests covering single-file (agent) and whole-directory (skill) cases, POSIX + Windows path separators, and the corrupt "both locations exist" defensive skip.

- **`feat(launcher,install,docs)` Launcher-startup migration + hook registration for the FS-disable mechanism** (Wave 2 E). A one-time idempotent migration runs once per registered project on launcher startup to convert any existing `enabled=0` rows from older installs into the new `.disabled/` layout. The `agent-skill-keyword-suggest` hook is now wired into both `templates/settings.json.{linux,windows}.template` UserPromptSubmit blocks alongside its sibling hooks. Docs in the existing FS-disable section extended with the migration story + the keyword-suggest hook registration.

### Fixed

- **`fix(launcher-core)` KG-binding seed-guard: stop clobbering user-customized `project_kg_bindings(primary)` on every boot.** Pre-v0.2.28 `ensure_orchestrator_root_kg_binding` (in `commands/orchestrator_root.rs`) called `set_project_kg_binding` unconditionally; the underlying SQL is `INSERT ... ON CONFLICT(project_id, role) DO UPDATE SET collection_name = excluded.collection_name`, which clobbered any user-customized binding with the orchestrator-root literal default on every launcher boot. **Symptom**: KG searches silently returning 0 results across the board — the MCP would resolve `KG_COLLECTION` via the hub (or `.claude/settings.json env`), and the launcher kept pointing both at the wrong collection (`VibeCodedOrchestrator_KnowledgeGraph`) while the actual project nodes lived in `VCODev_KnowledgeGraph`. Violated Dev Constraint #8 ("User choices survive all updates"). The guard now reads the existing binding first via `list_project_kg_bindings`; only re-seeds when no binding exists OR the existing one still carries the auto-seed sentinel `auto_seeded_by: "ensure_orchestrator_root_kg_binding"`. Anything else (manual override, prior migration, future GUI picker) wins. 3 new regression tests (`kg_seed_guard_preserves_manual_override`, `kg_seed_guard_writes_when_absent`, `kg_seed_guard_idempotent_on_prior_auto_seed`).

- **`fix(install)` `.claude/settings.json env` KG-keys backfill.** New idempotent helper `_backfill_kg_collection_env_in_project` in `vco_lib/project_init.py` reads the launcher.db's `project_kg_bindings` table (canonical source of truth) and writes any missing `KG_COLLECTION` / `SHARED_KG_COLLECTION` / `DEVELOPMENT_COLLECTION` keys into the per-project `.claude/settings.json env` block. Wired into both install routes (Dev Constraint #5): per-project `install-bundle --update` calls it alongside the v0.2.11 `_backfill_code_graph_project_env_in_project`; the orchestrator-root `install.py --update` path calls it after its own backfill. User-set values are never overwritten — pure additive idempotent fill. Resolution chain when DB unavailable: existing `env.KG_COLLECTION` suffix-swap → explicit `project_name` arg → `env.PROJECT_NAME` → `folder.name` sanitized. `SHARED_KG_COLLECTION=""` (empty-string user-disable) is respected and never auto-filled. 11 new pytests covering missing file, unparseable JSON, all-three-present noop, partial-fill, db-source-of-truth, db-absent fallback, explicit project_name override, empty-string preservation.

- **`fix(weaviate-mcp)` RL telemetry project-name canonicalization via slug (audit finding #3).** Pre-v0.2.28 `_get_rl_telemetry_writer` preferred `project_display_name` from the hub, which produced 4 distinct cohort labels for the same project (`Claude`, `VibeCoded Orchestrator`, `VibeCodedOrchestrator`, `VCODev`) — depending on which workspace was opened when, plus migration history. Now prefers `project_slug` (stable lowercase-hyphen identifier, the same key `module_settings` uses for the global-training-source flag); env-fallback path sanitizes via `sanitize_for_weaviate_class` so multi-workspace setups still produce one cohort. Existing JSONL events with the old project labels need a separate one-shot migration (out of scope; tracked in `rl-logging-audit-report-2026-05-23.md`).

- **`fix(weaviate-mcp)` RL citation monitor asyncio strong-ref (audit finding #1).** Pre-v0.2.28 `_rl_cache_and_rerank` did `asyncio.create_task(_rl_answer_monitor(...))` without keeping a reference. Python's asyncio runtime tracks tasks in a `WeakSet`, so GC could (and did) collect them mid-poll, producing the "Task was destroyed but it is pending!" warning and silently dropping the citation event. Symptom: 97.7% orphan-citation rate (897 / 918 retrievals had no matching citation event in the trailing 50 MB of `rl_events.jsonl`). Fix follows the [standard pattern](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task): hold a module-level `set[asyncio.Task]` and `add_done_callback(set.discard)` so completed tasks are removed cleanly.

- **`fix(scripts)` Per-model chunker preset in the KG sync script.** Pre-v0.2.28 `templates/scripts/sync_knowledge_graph.py` hardcoded `Chunker(min_tokens=1500, max_tokens=MAX_EMBEDDING_TOKENS=2500, target_tokens=2500)` for every node and doc. That was correct for qwen3-embedding:0.6b (8k context, 2500 working limit) but wrong for 512-token models (would over-chunk ~5x) and wasteful for 32k+ models (under-uses capacity). Now uses two new helpers `_chunker_for(server)` and `_max_chunk_tokens_for(server)` that delegate to `Chunker.for_model(server.embedding_service.text_model_id)` and `chunking_preset_for_model(...)` respectively. The "fits in one chunk?" gate and the actual chunk size now come from the SAME preset (cannot drift). Legacy hardcoded value preserved as a fallback when `embedding_service` is None (early-init paths only).

- **`fix(hooks)` UTF-8 BOM added to `agent-skill-keyword-suggest.ps1`.** The file contains non-ASCII bytes; without a BOM, PowerShell 5.1 (the default on Win10/Win11) mis-decodes it as Windows-1252 and fails to parse. Pre-existing test `tests/test_ps1_utf8_bom.py` was already catching this — the file was committed without a BOM in v0.2.27. Re-saved with the BOM; test passes.

## [0.2.27] — 2026-05-22

Follow-up release the same day as v0.2.26, shipping: (a) two
post-tag-discovered Windows install-path bugs from v0.2.25/v0.2.26
(Fabio's BOM fix + Python cp1252 reconfigure), (b) the
`events_paths_for` template token the RL module's v0.2.1 manifest
requires, (c) a divergence-modal rewrite that correctly handles
local-only files + retry state, (d) a KG env-propagation safety net
in the Weaviate MCP server, (e) Wave 1 of the agent/skill UX work
(FS-disable on toggle + keyword-suggest hook on UserPromptSubmit +
10 seeded agents/skills), and (f) docs updates clarifying the
canonical per-project env channel. (Wave 2 D + E — install-bundle
preservation + launcher startup migration + hook registration —
shipped in [0.2.28] same-day.)

### Added

- **`feat(launcher-core)` New `runtime.log_path_template` manifest field.** Optional. Single-brace closed-set tokens `{project_slug}` / `{project_id}`. Validated at `ModuleManifest::from_json` time via `validate_log_path_template` (rejects empty, double-brace, unknown placeholders, unclosed braces, no-placeholder templates). Render helper `render_log_path_template` does the per-project substitution. 12 new unit tests.

- **`feat(launcher)` New `{{events_paths_for:<control_id>}}` dispatcher template token.** Whole-string-only (embedded form rejected with a clear "must be the WHOLE string value" error because the resolution returns a JSON array). Resolution pipeline: read referenced control's array of UUIDs → walk each UUID through `db.get_project()` to get the slug → apply the module's `runtime.log_path_template` → return `JsonValue::Array`. 6 new dispatcher tests covering happy path + 5 error cases + nested-object recursion + unknown-token-prefix rejection. Implements the spec the RL chat shared in `rl-events-paths-for-template-spec-2026-05-22.md`; unblocks the 2 stubbed retrain buttons in RL module v0.2.1.

- **`feat(launcher)` WebKit divergence-modal rewrite** (`OrchestratorUpdateDivergenceModal.svelte`, 325 → 788 lines). Sticky footer keeps action buttons visible when file lists expand; retry-aware state machine (after Merge fails once, Rebase becomes the primary suggestion + button labels swap to "Try X again"); separate sections for "Files where both sides have diverging history" vs "Files only on your clone" (the latter is collapsed by default since they can't merge-conflict); git stderr rendered in its own labelled `<details>` block, never concatenated into the file list; new "Open clone folder" link uses `@tauri-apps/plugin-opener::openPath` with clipboard fallback. Resolves 4 a11y warnings (backdrop click/keydown, modal role/aria, focus management). 0 svelte-check errors. (Frontend-specialist Opus agent, 2026-05-22.)

- **`feat(launcher)` Generic per-(project × module) divergence-files split.** The `update_orchestrator` Tauri command's `collect_diverged_files` previously used `git diff HEAD..upstream/branch --name-only`, which lists every file different between the two tips. Forks tracking paths the public repo doesn't (e.g. VCO_dev's `other_projects_knowledge/`) saw all those paths flagged as "diverged" in the modal — confusing because they can't merge-conflict (no upstream version). Rewrite anchors on the merge-base: returns `(upstream_changed, local_only)` tuple where `upstream_changed` = files upstream touched since fork (real merge candidates) and `local_only` = files locally touched that upstream never had (pure-local content). New `local_only_files` JSON field on the `orchestrator_update_non_ff` payload; the rewritten modal renders the two categories as distinct collapsible sections.

- **`feat(launcher)` Launcher-GUI "disable agent/skill" toggle now physically moves files.** Pre-v0.2.27 the per-project disable toggle only flipped a DB column with no effect on Claude Code itself (which discovers agents/skills by globbing the filesystem; it had no awareness of the launcher DB). Disabled items kept appearing in autocomplete, autonomous invocation, and `/agents` listing. The toggle now physically moves the `.md` file to a sibling `.claude/agents.disabled/<name>.md` (or `.claude/skills.disabled/<name>/`) directory that falls outside Claude's discovery globs. Re-enable reverses the move. ~1k LOC changes in `db/project_state.rs` + 14 new fs_disable_tests + 6 new populate_disabled_tests. (Wave 2 — install-bundle preservation + launcher-startup migration of legacy `enabled=0` rows + hook registration in settings templates — shipped in [0.2.28] same-day.)

- **`feat(hooks)` New `agent-skill-keyword-suggest` UserPromptSubmit hook** surfaces relevant agents/skills based on case-sensitive whole-word matches against their `keywords:` frontmatter. Pure filesystem contract: globs `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` — no DB lookup. Disabled items naturally fall outside the glob (per the FS-disable change above). Paired cross-OS scripts `templates/hooks/agent-skill-keyword-suggest.{sh,ps1}` shell out to `templates/scripts/agent-skill-keyword-match.py` (stdlib-only Python matcher with explicit word-boundary regex). Seeded discriminative keyword lists on 10 representative agents (brand-identity-architect, kg-navigator, landing-page-critic, etc.) and 10 representative skills (accessibility-checker, hpc-submit, k8s-manifest-reviewer, etc.) as proof of concept; rollout to the remaining ~35 agents and ~42 skills is incremental as owners pick keywords. 46 new tests (Python matcher + hook output shape + edge cases). (Settings-template registration in both OS templates shipped in [0.2.28] same-day.)

### Fixed

- **`fix(launcher)` Windows install.py crash at step `[5b/10]` after BOM fix** (commit `a5b2971`). `install.py` contained ~660 non-ASCII characters (arrows, em-dashes, check marks) in user-facing prints. Python on Windows defaults stdout to the locale's legacy ANSI code page (cp1252 on Western locales); printing `→` (U+2192) crashed the codec with `UnicodeEncodeError`. Reconfigures `sys.stdout` + `sys.stderr` to UTF-8 with `errors="replace"` immediately after the Python version sentinel, before any other import or print. POSIX no-op. (Originally Fabio's commit `12dd3e3`; cherry-picked as `a5b2971` with author preserved.)

- **`fix(launcher)` Launcher belt-and-braces for the Windows install.py crash.** The launcher's `update_orchestrator` flow spawns Python via `tokio::process::Command`. To protect upgrades from v0.2.25 / v0.2.26 where the on-disk `install.py` pre-dates the in-Python UTF-8 reconfigure block, sets the Python I/O encoding env vars on the child env at all 6 Python spawn sites (`update_at`, `run_install_orchestrator_lightweight`, `run_hardware_reconfig`, `update_orchestrator`, both fallback re-spawns). POSIX no-op.

- **`fix(weaviate-mcp)` Empty-env safety + resolution-source tracking for `KG_COLLECTION`.** When the user's workspace declared `KG_COLLECTION=""` (or whitespace-only), the MCP server's `os.getenv("KG_COLLECTION", default)` returned the literal empty string — NOT the default — and the empty class name propagated into Weaviate, causing schema-fail. `_config_field(empty_means_unset=True)` now coerces empty/whitespace values to the documented default for keys where empty doesn't have semantic meaning (`KG_COLLECTION`, `DEVELOPMENT_COLLECTION`); keeps `SHARED_KG_COLLECTION=""` semantically (means "shared search disabled"). New resolution-source tracking: every collection name now carries an annotation (`src=env` / `src=hub` / `src=default`) that surfaces in (a) a startup log line `weaviate-kg: resolved collections (kg='...' src=..., ...)` so operators know what the subprocess actually picked up, and (b) annotated error messages on schema-fail (`Tried 2 collections: 'X' [self/KG_COLLECTION src=hub], 'Y' [peer/VCT_KG_ACCESS_LIST] — ...`) so the failure points at the right config layer. 13 new unit tests.

- **`fix(hooks)` `agent-skill-keyword-suggest` hardened with `command -v python3` / `Get-Command` fallback** when `_lib/find-python` is missing (partial-install edge case flagged in Wave 1 review). The hook no longer hard-fails on a half-set-up project that lacks the find-python helper.

### Changed

- **`docs(claude-md)` Clarified canonical per-project env channel.** Made it explicit that `.claude/settings.json env` is the canonical channel and that `.vscode/settings.json claude-code.env` does NOT propagate to MCP subprocesses on Linux (sentinel testing 2026-05-16 against Claude Code 2.1.143; pre-existing limitation, now surfaced in CLAUDE.md rather than only in PR-27's commit message). New precedence-order list (5 layers, highest-to-lowest) covering vct-hub → `.claude/settings.json env` → `.claude/env` → `~/.claude.json mcpServers.weaviate-kg.env` → bundled defaults. Also fixed 3 instances of `Vibecoded` → `VibeCoded` typo (drifted since v0.2.23).

- **`docs(troubleshooting)` New Windows recovery section** for v0.2.25 / v0.2.26 users hit by the BOM / cp1252 install crash. Three recovery options: (1) `git pull origin main` + re-run `first-install.bat`; (2) manual env-var bootstrap before `python install.py --update`; (3) download the v0.2.27+ release zip directly. Cross-references the in-launcher belt-and-braces protection added this release.

- **`docs(kg)` `module-contributed-gui-tabs.md` extended** with a v0.2.27 evolution subsection: `log_path_template` field + `events_paths_for` token semantics + closed-set token table (now 5 supported tokens) + RL manifest example showing how `control:src_projects` (raw UUID array) and `events_paths_for:src_projects` (per-project log-path array) compose cleanly + enumeration of the 5 dispatcher error cases.

### Migration notes

- **End users on Windows v0.2.25 / v0.2.26 stuck on `first-install.bat`**: see `docs/TROUBLESHOOTING.md` § "Windows: `first-install.bat` crashes with `UnicodeEncodeError`". Three recovery paths documented; recommended is `git pull origin main` + re-run.

- **RL module authors**: `runtime.log_path_template` is the new place to declare your per-project log-path convention. Closed-set tokens `{project_slug}` / `{project_id}` only. Use `{{events_paths_for:<control_id>}}` (whole-string only) in any descriptor `body` field to inject the resolved array. See `module-contributed-gui-tabs.md` § "v0.2.27 evolution" + the RL spec in VCO_dev's `.claude/context/plans/`.

- **Operators hitting "every configured collection schema-failed" on `hybrid_search`**: the MCP error message now points at the resolution-source for each tried collection. Look for the `weaviate-kg: resolved collections` log line at MCP startup (Claude Code's MCP log panel, or `~/.claude/logs/`) to see what the subprocess actually picked up + the source layer. Common cause: workspace env is in `.vscode/settings.json claude-code.env` (does not propagate); move to `.claude/settings.json env` via the launcher's Identity tab.

- **End users with previously toggled-off agents/skills**: the FS-disable migration runs automatically once per project on the first v0.2.27 launcher startup. No user action required. Toggled-off items move from being DB-flagged-but-still-discovered to physically out of Claude Code's filesystem globs. Re-enable in the launcher GUI to reverse.

- **No DB schema breakage**. The FS-disable migration is additive: existing toggled-off agents/skills with `enabled=0` are migrated to the `.disabled/` directory at first launch post-update; the DB column stays for back-compat with older launcher binaries.

## [0.2.26] — 2026-05-22

Headline release: a **generic declarative HTTP-action dispatcher** that
lets paid modules add new GUI controls without launcher rebuilds.
Every future paid module (`vct-coordination`, `vct-transcrypt`,
`mao`, …) now declares its config tab entirely in its
`vct-module.json` manifest; the launcher renders + executes
everything generically. The four reset/retrain Tauri command stubs
that were placeholders for exactly this dispatcher are deleted.

Also: a WebKitGTK + EGL pre-flight probe so a stale post-driver-upgrade
NVIDIA driver state no longer aborts the launcher at startup. The
probe is per-launch — once the user reboots and the driver state
clears, the GPU/DMABUF fast path returns automatically with no
persistent perf cost.

### Added

- **`feat(launcher)` Generic declarative HTTP-action dispatcher** (the v0.2.26 headline). One new Tauri command `module_dispatch_action(moduleId, projectId, action, value, siblingValues?)` executes any `ActionDescriptor::Http { method, path, body, polling?, next_action? }` declared in a paid module's `vct-module.json`. The launcher's renderer (`ModuleConfigTab.svelte`) routes legacy string-form actions to the existing `invoke(action, ...)` path and descriptor-form actions to the new generic command; back-compat is permanent for v0.2.20–v0.2.25 manifests. The descriptor supports `{{...}}` template substitution (closed set: `project_id` / `module_id` / `value` / `control:<id>`), polling with progress + failed Tauri events, and arbitrary-depth chained `next_action` bounded by `MAX_CHAIN_STEPS = 1024`. Implementation lives at `launcher/src-tauri/src/commands/module_dispatch.rs` (~1500 LOC + 31 tests including in-process axum mock servers). See [`knowledge/concepts/module-contributed-gui-tabs.md`](knowledge/concepts/module-contributed-gui-tabs.md) for the full wire shape + `docs/PAID_MODULE_DEV_CHECKLIST.md` for the module-author integration recipe.

- **`feat(launcher-ui)` Five new schema-rendered control kinds**: `text_input` (with optional apply/validate action), `number_input` (min/max/step), `status_display` (polled GET source + `render_template`), `file_picker` (Tauri native dialog, optional extension filter, directory mode), `link` (external via tauri-plugin-opener / internal via SvelteKit goto). New components under `launcher/src/lib/components/module-controls/`. Each is documented in `docs/PAID_MODULE_DEV_CHECKLIST.md`.

- **`feat(launcher-core)` Generic per-(project × module) port table** — migration 017 adds `module_ports(project_id, module_id, port, updated_at)` with `INSERT OR IGNORE` backfill from the existing `projects.rl_port` column. New helpers `db.get_module_port(...)` / `set_module_port(...)` / `ensure_module_port(...)`. The legacy `get_project_rl_port` / `set_project_rl_port` pair becomes thin wrappers — every v0.2.21+ hub call site compiles unchanged. Unblocks coordination + transcrypt without per-module schema changes. See [`knowledge/concepts/generic-per-module-db-architecture.md`](knowledge/concepts/generic-per-module-db-architecture.md).

- **`feat(launcher)` WebKitGTK + EGL pre-flight probe** at `launcher/src-tauri/src/webkit_preflight.rs`. Linux-only (`#[cfg(target_os = "linux")]`); macOS/Windows no-op. Called from `main()` BEFORE Tauri init. Walks `/sys/class/drm/*` to find the primary GPU (`boot_vga=1`), maps to its `/dev/dri/renderD*` node, dlopens `libEGL.so.1` + `libgbm.so.1`, and calls `eglInitialize` against the GBM platform display. On the well-known `EGL_NOT_INITIALIZED` (0x3001) failure signature — most commonly: NVIDIA `apt`-upgraded its proprietary userspace without a kernel-module reload — sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` (and `__NV_DISABLE_EXPLICIT_SYNC=1` on Wayland) so WebKit falls back to its legacy renderer instead of aborting the process. Kill-switch: `VCT_WEBKIT_PREFLIGHT_OFF=1`. User-set `WEBKIT_DISABLE_DMABUF_RENDERER` is respected (the probe is a no-op when the user already chose). Live-verified against the broken NVIDIA driver state on the dev box on 2026-05-22. See [`knowledge/concepts/webkit-egl-preflight-probe.md`](knowledge/concepts/webkit-egl-preflight-probe.md).

- **`feat(launcher-core)` Manifest schema additions**: new `ActionRef` enum (untagged: `Legacy(String) | Descriptor(ActionDescriptor)`), new `ActionDescriptor::Http`, new `HttpMethod` / `PollingSpec` types, and the 5 new `ConfigControl` variants. The existing variants' `action` / `on_change` / `options_source` field types change from `String` to `ActionRef` — back-compat preserved by `#[serde(untagged)]`. 30 new unit tests covering each control kind, descriptor JSON deserialization, polling-spec defaults, chained-action depth, and legacy-form back-compat.

### Changed

- **`refactor(launcher)` Renamed `commands::rl_service` → `commands::module_service`** (~10 files, ~40 textual refs updated). The file's internals were already generic — v0.2.21's header noted "Today this module supports exactly one consumer — `vct-rl-reranker` — but the helpers are written against `ModuleManifest` so future container modules drop in without a code change". Now that v0.2.26 has dispatcher + module_ports for coordination + transcrypt, the file name matches the scope. Pure rename — no behavioural change.

- **`docs(paid-modules)` Extended `docs/PAID_MODULE_DEV_CHECKLIST.md`** with two new sections: "GUI tab integration via the declarative dispatcher (v0.2.26+)" (declarative-vs-legacy decision matrix, minimum example, polling example, port registration contract, template grammar reference, what-still-requires-rebuild list) and updates to the "When you add a new paid module" procedure (now includes explicit steps for declaring the GUI tab in the manifest + wiring port registration via `ensure_module_port`).

### Removed

- **`refactor(launcher)` Removed four RL stub Tauri commands** (`rl_reset_to_global`, `rl_reset_and_specialize`, `retrain_global_online`, `retrain_global_offline`) from `launcher/src-tauri/src/commands/rl_settings.rs` along with their `invoke_handler!` registrations and the now-orphan `RetrainResult` struct. They were placeholders for the now-shipped declarative dispatcher; the RL chat migrates `paid-modules/vct-rl-reranker/vct-module.json` to `ActionDescriptor::Http` entries on its side. Preserved: `set_rl_use_global` / `set_rl_online_training_disabled` / `set_rl_global_training_source_flag` / `list_rl_global_training_source_projects` (these read launcher-side DB state the HTTP-only dispatcher can't express; legacy-routed permanently).

### Fixed

- **`fix(launcher)` Three pre-release review findings** (committed post-cross-agent merge as a single follow-up): (a) `substitute_string` previously cast each byte to `char`, mangling multi-byte UTF-8 sequences in embedded templates — switched to `&str` slicing between token boundaries; (b) `{{control:<id>}}` was documented but unreachable end-to-end because `SubstitutionContext::simple()` hard-wired a `|_| None` resolver — plumbed `sibling_values: Option<HashMap<String, Value>>` through the Tauri command + dispatcher and built a real resolver backed by the renderer snapshot with `module_settings` DB fallback; (c) `run_poller` terminated on a single non-2xx tick — added a consecutive-failure budget (`POLL_CONSECUTIVE_FAILURE_LIMIT = 5`) so transient 503s during container restarts no longer kill long-running polls. Five new regression tests covering all three.

### Migration notes

- **Module authors**: see `docs/PAID_MODULE_DEV_CHECKLIST.md`'s new "GUI tab integration via the declarative dispatcher" section + the dispatcher KG node for the wire shape. The default answer for "I want to add a setting to my module" is now **declare it in the manifest, not in launcher Rust**. Pre-existing modules using `ActionRef::Legacy(String)` keep working; migration is opt-in and per-control.

- **End users**: no action required. The migration 017 backfill is idempotent (`INSERT OR IGNORE`) and the legacy `projects.rl_port` column stays in place for back-compat. NVIDIA users who experience a launcher startup abort after an `apt upgrade` of `nvidia-driver-*` (the `EGL_NOT_INITIALIZED` crash) will now see the launcher start automatically — the GPU/DMABUF path returns on the next reboot.

- **No DB schema breakage**. Migration 017 only adds a new table; no existing tables modified.

## [0.2.25] — 2026-05-22

Follow-up release for the v0.2.24 main feature drop. Originally
attempted as `v0.2.24.1` (matching the v0.2.23.1 precedent of a
point-release tag), but the release-CI's tag-vs-Cargo-version match
check rejects 4-segment tags because SemVer (and therefore Cargo)
doesn't allow them. Versions properly bumped to 0.2.25 across the
6 standard pins.

### Fixed

- **`fix(launcher)` embedding-catalog Tauri command `ModuleNotFoundError: vco_lib`** (A0ter). The per-project Settings → KG/Codegraph tab previously showed a permanent warning banner ("discover exit 1: ... No module named 'vco_lib'") and "Loading…" dropdowns stuck indefinitely. Root cause: `commands::embedding_catalog::run_discover` spawned `python -m vco_lib.embedding_service discover` without setting `cwd` to the orchestrator clone root, so Python's implicit-namespace-package resolution couldn't find the in-tree `vco_lib/` directory. Fix: resolve the clone root via the existing DB-cached `installer::resolve_install_root_sync(&db)` helper (the same pattern the v0.2.23.1 manifest scanners use) and pass it as `cwd`. Best-effort: if no clone root is discoverable, falls through with no cwd set — matches the pre-fix failure mode rather than degrading further.

### Changed

- **`feat(launcher)` Orchestrator Core tab → "Clone integrity"** (A0bis). The Orchestrator Core tab (shipped in v0.2.20, fixed in v0.2.23.1) previously hosted per-project actions (Rebuild KG / Check duplicates / Re-analyze code / Prune stale codegraph) that DUPLICATED controls already on the KG/Codegraph tab, plus a Diagnostics section (health-probe + open-logs). Honest scope audit found only 2 features that are genuinely root-clone-only:
  - **Re-detect orchestrator root** — re-runs the launcher's `current_exe()`-walk discovery + refreshes the cached `launcher.install_path` app_state entry. Use when the launcher's cached install path is stale (clone dir renamed, moved, or copied to a different location).
  - **Validate clone manifest** — parses `vct-module.json` at the active clone root and surfaces schema errors. The launcher's catalog renderer silently skips a malformed manifest (module-contributed tabs disappear); this command makes the failure explicit.

  Tab renamed `"Orchestrator core"` → `"Clone integrity"` to reflect the new scope. Per-project actions removed from the manifest (duplicates with KG/Codegraph tab). Diagnostics deferred to a Services-tab follow-up; the underlying `orchestrator_health_check` + `orchestrator_open_logs` Tauri commands stay registered. Manifest test (`orchestrator_core_manifest_with_gui_tab_deserializes`) updated to pin the new 2-section / 2-button shape.

- **`feat(launcher)` B4 modal cosmetic pass** — `OrchestratorUpdateDivergenceModal.svelte` + `OrchestratorUpdateConflictModal.svelte` now use VCT color tokens (`--color-bg2`, `--color-text`, `--color-mid`, `--color-teal`, `--color-pink`, `--color-card`, `--color-border`) instead of hardcoded hex values (`#1a1a24`, `#e8e8ee`, `#ccc`, etc.). Fix for the header-truncation symptom (modal could overflow viewport when the diverged-files `<details>` was expanded): added `max-height: 90vh` + `overflow-y: auto` to the modal containers. Primary action button switched from generic blue to `--color-teal`; pink accent retained for the conflict modal (conflict severity signalling).

### Migration notes

- Existing projects with the "Orchestrator Core" tab visible in per-project Settings will see it renamed to "Clone integrity" on next launcher restart. The tab's contents change from 6 buttons + 3 sections to 2 buttons + 2 sections — the per-project actions removed live in the KG/Codegraph tab (same `Re-build code graph` / `Re-sync KG` / `Re-build KG summaries` controls); the Diagnostics health-probe + log-dir actions are still available via the orchestrator-health-check + orchestrator-open-logs Tauri commands (a Services-tab UI for them is tracked as a follow-up for v0.2.25).

- The embedding-catalog dropdowns ("Embedding model" in both Knowledge Graph + Code Graph sub-sections of the KG/Codegraph tab) will populate correctly on next launcher restart. No user action required — the fix is launcher-side only.

- No DB schema changes. No template propagation needed (per-project install-bundle is unaffected).

## [0.2.24] — 2026-05-22

> CHANGELOG drift note: 0.2.21, 0.2.22, 0.2.23 ship in git history but
> aren't expanded in this file yet. The git tags (`v0.2.21`, `v0.2.22`,
> `v0.2.23`) + GitHub Release notes are the interim source of truth.
> Backfill tracked as a follow-up.

This release lands two architectural fixes plus a handful of
peer-review-deferred cleanups. The headline items: a per-path 3-way
merge for user-editable orchestrator-root files (§A0 — solves the
"git pull would overwrite your CLAUDE.md edits" wall every 3rd-party
user hit) and a per-collection schema-skip + failure-mode telemetry
fix in the Weaviate MCP (RL-defect-2026-05-22 — restores the
`rl_events.jsonl` corpus that the RL module's qwen3 training run
depends on).

### Added

- **`feat(launcher)` per-path 3-way merge during orchestrator-root updates (§A0)** (`0f751da`, `a6144c5`, `af423e8`, `2b3d757`, `97f8aa8`, `89f2410`, `348fef1`, `632c43e`). New `claude_mcp_servers/.../git_user_editable_merge` module + integration into `update_orchestrator` and `merge_orchestrator_with_upstream`. For each file in the diff between `HEAD` and `vco_upstream/<branch>` that matches a hardcoded allowlist of user-editable paths (`CLAUDE.md`, `CLAUDE.local.md`, `knowledge/**/*.md`, `.claude/CONTEXT_STATE.md`, `.claude/MEMORY.md`, `HANDOFF-*.md`), runs `git merge-file --stdout` with BASE/OURS/THEIRS. Clean merges land in the working tree and get staged + committed via a synthetic `vco: pre-merge user-editable files via A0 (<ts>)` commit (`VCO Orchestrator <orchestrator@vibecoded.tools>` author, `--no-verify`). Conflicts write the upstream content to `<path>.from-upstream-<sha>` sidecar files + emit `orchestrator_user_modified_preserved` deferral entries (via the existing `UPDATE_DEFERRED.md` surface) — local content is NEVER auto-overwritten. After pre-merge, `update_orchestrator` smart-routes through `git pull --rebase` (replays the synthetic commit onto upstream tip → linear history) when a synthetic commit exists, else `--ff-only` (preserves the original strict semantics). The B4 divergence modal still fires for genuine divergence beyond the allowlist. 14 Rust tests (10 unit + 4 integration covering FF / merge / sidecar / non-FF-then-merge paths).

- **`feat(install-bundle)` orchestrator-orphan-deletion handling** (`af423e8`, `2b3d757`). Sibling to §A0: when an `install-bundle --update` finds a file in the project's `.vco-manifest.json` that no longer exists in the orchestrator's `templates/` set, the orphan-detection loop checks whether the installed file matches the manifest's prior-shipped hash. Identical → SAFE_DELETE. Divergent → PRESERVE + emit `bundle_user_modified_deletion_preserved` deferral. 7 new tests.

- **`feat(weaviate-mcp)` per-collection schema-skip + failure-mode telemetry (RL-defect-2026-05-22)** (`bbbcab8`). `_hybrid_search_body` and `_semantic_graph_search_body` no longer bubble `WeaviateSchemaError` from a single missing collection; they classify per-target, skip the offending class, continue with the rest of the fan-out, and surface the partial failure via new `failure_mode: str | None` + `failed_collections: list[str] | None` fields. When EVERY collection schema-fails, a degraded-mode telemetry event lands BEFORE the bubble re-raises. `_rl_cache_and_rerank` now always calls `writer.log_retrieval()` — including on the free-tier early-return path and with empty all_nodes. Unblocks the `~/.claude/retrieval_rl_data/rl_events.jsonl` corpus that the RL module's qwen3 training run depends on. 5 new tests pinning the contract (`test_weaviate_mcp_telemetry_on_failure.py`).

- **`feat(install-bundle)` detect + defer legacy `.vscode/settings.json` MCP_* keys (RL-defect Fix 2)** (`37d6c98`). Detection-only with deferral. Pre-v0.2.12 the launcher wrote MCP_WEAVIATE_SERVER / MCP_PYTHON / MCP_OLLAMA_SERVER / MCP_PYTHONPATH absolute paths into `.vscode/settings.json claude-code.env`. PR-27 (v0.2.12) removed that write because the `claude-code.env` channel didn't propagate to MCP subprocesses on Linux Claude Code, but the stale keys remain in pre-v0.2.12 projects, baking the user's on-disk layout into the project tree. `_detect_legacy_vscode_mcp_env_keys` + `_emit_legacy_vscode_mcp_env_deferral` surface the issue via a `legacy_vscode_mcp_env_keys_present` info-severity entry with an operator-driven `jq` cleanup recipe that preserves the rest of the file. Per user policy 2026-05-22: never auto-overwrite user-edited files. 8 new tests.

- **`feat(claude)` ship `autonomous-orchestrator` output-style** (`5160294`). New `.claude/output-styles/autonomous-orchestrator.md` static asset. Activates via `claude --style autonomous-orchestrator` or by copy/customize.

### Changed

- **`feat(resolver)` rate-limit schema-version drift warning across invocations (§A4)** (`3ad8fd9`). Hooks call `vct_project_config.{sh,ps1}` dozens of times per Claude Code session; the previous one-shot guard reset per-invocation, producing a stderr flood when the hub reports a schema_version higher than the client's `RESOLVER_PROTOCOL_VERSION`. Now routed through the existing `_emit_warning` + JSONL-backed suppression infrastructure with a stable cross-PID suppression key (`schema_version_drift_<hub_version>`); 5-min window per key; `VCO_HOOK_DEBUG=1` bypasses. Sibling `.ps1` got the cross-invocation rate-limit (it had no equivalent infra pre-v0.2.24). 7 new tests.

- **`refactor(install)` extract `_rebind_collection_names_to_on_disk_casing` helper (§B3)** (`288e0d9`). Deduplicates the two healing paths in `_self_heal_kg_bindings_on_update` (`project_kg_bindings` + `kg_collection_access`) into a generic `(db, table, project_id_col, collection_name_col, *, conflict_resolver=None)` helper. Privilege-rank collision resolution stays at the call site for `kg_collection_access` (write > read > none). 3 new helper-contract tests; existing 11 stay green.

- **`refactor(launcher)` extract `is_shared_kg_class_name` helper for case-tolerant shared-KG matching (§B4)** (`8635d65`). New helper in `vct-launcher-core::project_env_settings` consolidates the case-insensitive matching of `DEFAULT_SHARED_KG_COLLECTION` + `LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C` + `LEGACY_SHARED_KG_COLLECTION` (pre-v0.2.12 `VibeCodedTools_KnowledgeGraph`). Refactor of `commands/kg.rs::kg_list_collections` + `commands/maintenance.rs::parse_schema_response`. Closes a pre-existing strict-equality bug in `kg.rs` that demoted the shared KG from the "shared first" sort priority on case-different installs. 7 new unit tests.

- **`chore(launcher)` cleanup batch — subscribe-leak fix + dead-shim removal + SPDX sweep (§A2/A3/A5)** (`7a54986`). `/preferences/+page.svelte`'s two popover-era `.subscribe()` calls switched to `$effect` (auto-unsubscribes on component destroy — eliminates the per-mount leak). `ui.ts::closeSettings` removed (no callers anywhere); `SettingsSection` narrowed from 5-value union to just `'secrets'`. SPDX-License-Identifier header added to 5 `templates/scripts/*.py` files.

### Migration notes

- After installing v0.2.24, the FIRST `Settings → Updates → Update orchestrator` click that runs over a working tree with user-modified allowlisted files will produce a synthetic `vco: pre-merge user-editable files via A0` commit on the local clone. Subsequent updates with no allowlisted-file diff produce no synthetic commit. Users running `git log` will see this commit — that's expected and documented in the orchestrator-install-flow KG node.

- Free-tier installs now accumulate `rl_events.jsonl` events on every `hybrid_search` / `semantic_graph_search` call regardless of whether reranking happens (it doesn't on free tier) — the local-JSONL pipeline is gated only by the existing `RL_LOCAL_LOGGING_DISABLED` env-var opt-out (Preferences → "Collect retrieval data locally"). Users who opted out pre-v0.2.24 are unaffected.

- The new `failure_mode` + `failed_collections` fields are additive in the `rl_events.jsonl` schema. Offline trainers should filter events where `failure_mode is not None` out of training-pair construction (no positive/negative target signal) but keep them as a failure-rate / query-distribution metric.

- The legacy `.vscode/settings.json claude-code.env` MCP_* key detection emits an info-severity deferral on every `install-bundle --update` until the user removes the keys (or the whole `claude-code.env` block). The deferral provides an operator-driven `jq` recipe; the launcher does NOT auto-clean.

## [0.2.20] — 2026-05-19

This release lands the client-side support for the first paid module
(vct-rl-reranker) + a module-extensible GUI tab framework + AMD/ROCm
GPU support across the orchestrator's container stack. The paid module
itself ships from `hotak92/vct-rl-reranker` (private); the orchestrator
core (this repo) only knows how to talk to it.

### Added

- **`feat(rl_client)` AGPL-side adapter for the vct-rl-reranker paid module** (`467f681`). New `claude_mcp_servers/rl_client/` package with: `RLClient` async HTTP client (httpx) with disabled-mode + per-call fallback semantics; `schemas.py` Pydantic wire contract pinning `/cache_nodes`, `/rl_update`, `/health` shapes; `RLTelemetryWriter` fanning events to local JSONL AND the existing telemetry queue when upload consent is granted (query text OMITTED from queue payload — replaced by `query_length` summary; node titles + embeddings + scores preserved); `rl_logger.py` slim copy of the canonical AGPL logger. `claude_mcp_servers/weaviate_mcp/server.py::_rl_cache_and_rerank` now routes through `RLClient` instead of inline aiohttp POSTs. Free-tier users get "disabled mode" (returns inputs unchanged); Pro/MAO with container running gets real reranking. Cross-OS templates at `templates/scripts/rl_client_setup.{sh,ps1}` for per-project install.

- **`feat(launcher)` module-contributed GUI tabs framework** (`c17765c`). Modules can now declare a `gui.config_tab` block in their `vct-module.json` that the launcher renders into the sidebar without bundling Svelte. Five widget kinds (checkbox / multi_select / button / select / info), each carrying `tooltip: Option<String>` so authors can always provide mouseover help. Schema lives in `manifest.rs::GuiBlock`; renderer in `ModuleConfigTab.svelte`; merge logic in `Sidebar.svelte`; generic settings persistence via `get_module_setting` / `set_module_setting` Tauri commands. RL Reranker is the first consumer (its manifest ships from the private repo); orchestrator-core demonstrates the framework with its own KG + Code Graph + Diagnostics controls.

- **`feat(v0.2.20)` orchestrator-core gui.config_tab demo** (`4c2df3f`). Proves the framework generalizes beyond paid modules. New `commands/orchestrator_core.rs` exposes 6 Tauri commands: `kg_rebuild_current_project`, `kg_check_duplicates`, `code_graph_reanalyze_current`, `code_graph_prune_stale`, `orchestrator_health_check`, `orchestrator_open_logs`. Thin tokio-subprocess wrappers around `.claude/scripts/{kg-sync,kg-duplicates,code-graph-analyze}` + reqwest probes for Weaviate / Ollama / code-embed health. `templates/scripts/detect_duplicates.py` gained a `--json` flag for machine-readable consumption.

- **`feat(v0.2.20)` AMD/ROCm support + per-module VRAM threshold + ROCm overlay** (`75317eb`). Closes the AMD-vendor gap inherited from v0.2.9's Bug-K work. `gpu_policy::decide_gpu_mode` now takes `has_amd: bool` between `has_nvidia` and `has_apple_silicon`. `GpuMode::Gpu` renamed to `GpuMode::Cuda`; new `GpuMode::Rocm` variant. Precedence: user_override (explicit) → Apple Silicon → NVIDIA+VRAM → AMD+VRAM → CPU. NVIDIA wins when both NVIDIA + AMD present. `gpu.rs::query_amd_gpu_present()` probes `rocminfo` (canonical) + `/sys/class/drm/card*/device/vendor=0x1002` (fallback). `HardwareSnapshot.has_amd_gpu` added. `manifest::RuntimeBlock` extended with `min_gpu_vram_gb: Option<f64>` (per-module override, None → global 8 GB default), `gpu_optional: bool`, `gpu_image_variants: Option<GpuImageVariants>` (cpu / cuda / rocm tags). `install.py::_decide_gpu_mode` mirror updated; overlay picker prefers `docker-compose.rocm.yml` then falls back to legacy `amd-rocm.yml`. New `infrastructure/docker-compose.rocm.yml` overlay for orchestrator-core's `ollama` + `code_embed` services: `/dev/kfd` + `/dev/dri` device passthrough, `group_add: ["video", "render"]`, `cap_add: ["SYS_PTRACE"]`, `HSA_OVERRIDE_GFX_VERSION="10.3.0"` (conservative RX 6000-class default). 86 new/updated tests across `gpu_policy` + `gpu` + `manifest` + pytest.

- **`feat(v0.2.20)` gpu_image_variants → image tag dispatch** (`6d11024`). Final piece of the GPU plumbing. `installer_engine::run_install` now consults the host's `GpuMode` at container_pull time and picks the right variant tag (cpu / cuda / rocm) when the manifest declares `gpu_image_variants`. `install_module_for_project` reads the persisted `HardwareSnapshot` from app_state; falls back to `Cpu` when no snapshot exists. New helper `resolve_variant_tag(manifest, base_tag, gpu_mode)` returns the right suffix. Module-side: paid modules ship 3 OCI image tags per release (`:0.1.0-{cpu,cuda,rocm}`); the launcher picks the correct one. Legacy modules without `gpu_image_variants` continue using the single-tag flow unchanged. 5 new tests covering Cuda / Rocm / Cpu / Metal / no-variants paths.

### Migration notes

- Existing module manifests without `gpu_image_variants` are unaffected — the legacy single-tag image pull path remains the default. Only modules that opt into per-variant builds (today: vct-rl-reranker) trigger the dispatch.
- Modules that need a per-module VRAM threshold can declare `runtime.min_gpu_vram_gb` (e.g. `4.0`). Unset → global 8 GB default (calibrated for the Ollama + CodeSage + qwen3.5:9b stack).
- AMD users with sufficient VRAM who previously got "CPU-only" mode will now get ROCm-accelerated containers on next `redetect_hardware` + reinstall.
- The `RL_LOCAL_LOGGING_DISABLED` env var lets users opt out of local rl_events.jsonl collection via the new Preferences → "Local data collection" section.
- The orchestrator-core's own `gui.config_tab` adds a "Module configuration" sidebar group with the launcher's own surface. The group is hidden when no modules expose tabs (i.e. on a fresh free-tier install before any paid module is registered locally).

## [0.2.19] — 2026-05-19

### Added

- **`feat(templates)` 7 role-specialist agent/skill packs + KG nodes** (`0c1d285`). 83 new files across `templates/agents/free/` + `templates/skills/` covering 7 senior professional roles built via 7 parallel Opus subagent runs: Consulting CTO (portfolio, SOW, due-diligence, employee-impersonator); Senior Designer/UX (brand identity, enterprise UX, design tokens, AI imagery, Photoshop scripting); Vendor/Sales+Marketing (inbox triage, outbound sequences, SEO, content calendar, sales-call prep); Senior Scientist (paper triage, experiment design, stats consultation, equation check, HPC, repro audit); Automation/AI Engineer (workflow design, API scaffolding, idempotency, webhook security, cost estimation); Solo SaaS Founder (pricing strategy, metrics health-check, landing critique, launch orchestration, build-vs-buy); Senior DevOps/SRE (incident response, post-mortem, IaC review, k8s review, SLO design, observability). Each template uses `opus + effort:high` (or `xhigh` for the few deep-reasoning agents: experiment-designer, equation-check, consulting-sow-drafter). Also adds `knowledge/tools/terraform-opentofu.md` as a foundation node for the existing terraform-plan-reviewer skill.

- **`feat(kg)` 42 new KG nodes + 1 expanded merge** (`32df8aa`). Crystallizes the substantive knowledge that previously lived only in the role-pack skill/agent template bodies into KG nodes, so it surfaces via `hybrid_search` regardless of whether the user invokes the specific skill/agent. Built-in agents (general-purpose, coder, Explore) rarely auto-load custom skills, so knowledge stuck in a skill body is invisible by default — KG nodes are not. Built via 7 parallel Opus extractors (one per role), each in its own isolated worktree. Two merges performed to avoid near-duplicates: `equation-verification-methodology` merged INTO existing `dimensional-analysis-as-debugging` (now covers all 4 sanity checks); `role-impersonation-archetypes` + `wear-the-hat-discipline-specialist` merged into single `wear-the-hat-pattern`. Cross-link added between `sre-incident-response-playbook` and `incident-communication-tempo`.

### Fixed

- **`docs(deferral)` schema_migration_required: explicit VCT_ORCHESTRATOR_ROOT cd** (`195fe9a`). The v0.2.18 `update_project_v2` flow emitted a `schema_migration_required` deferral with a "To apply" command that read `python -m vco_lib.project_init migrate-collections --name 'X' ...`. LLM agents in per-project workspaces (and humans copying-pasting) ran this from the project directory + project venv, triggering `ModuleNotFoundError: No module named 'vco_lib'` because `vco_lib` lives in the orchestrator clone's venv only. Fix: corrected deferral text to `cd "$VCT_ORCHESTRATOR_ROOT" && .venv/bin/python -m vco_lib.project_init migrate-collections --name 'X' ...`. The `--name` flag still scopes the migration to the project's collections; running from the orchestrator clone does NOT migrate the orchestrator's own.

### Migration notes

- v0.2.19 is a templates + KG + doc-fix release. No binary code changes (the launcher binary functionally matches v0.2.18). The 3-OS rebuild fires only because the release workflow rebuilds on every tag — the produced binaries are byte-equivalent in behaviour to v0.2.18's. Users who don't need the new templates/agents/skills/KG nodes can stay on v0.2.18 indefinitely.
- The new templates auto-propagate to per-project installs via Update Bundle (per-project Settings → "Update bundle"). The new KG nodes seed at next `install.py --update` step 7c, OR via per-project `kg-sync` for projects that bind the shared KG.
- The deferral-doc fix only affects FUTURE deferrals (existing v0.2.18 deferrals on user machines have the stale text; they can hand-edit or wait for `install.py --update` to re-emit on the next schema-drift detection).

## [0.2.18] — 2026-05-19

### Added

- **`feat(embed)` `vco_lib.embedding_service.EmbeddingService` — central multi-provider embedding dispatcher** (Wave A Commit 2). Replaces ~7 consumer scripts that each read `EMBEDDING_MODEL` / `ACTIVE_EMBEDDING` directly. Per-project instances via `EmbeddingService.for_project(project_root)` resolve text + code backends from env, expose `.text_vector_slot` / `.code_vector_slot` for active-slot dispatch, and provide `embed_text` / `embed_code` (single + batched). Multi-slot writes via `embed_text_all_configured` / `embed_code_all_configured` preserve search continuity through model changes. Backend adapters live under `vco_lib/embedding_providers/{ollama,codeembed,openai}.py`. CLI: `python -m vco_lib.embedding_service discover --json` for the GUI catalog. Failure capture writes 3 surfaces: `~/.claude/metrics/embedding_failures.jsonl` + `<install_root>/.claude/context/EMBEDDING_FAILURES.md` (Claude-readable hint, auto-cleared on success) + `UPDATE_DEFERRED.md` deferral entry for the GUI banner.

- **`feat(secrets)` OpenAI API key as bundled secret** (Wave A Commit 3). New entry in `vct-module.json::bundled_secrets` (key `openai_api_key`, scope `shared`, module_id `user`). Three Tauri commands in `launcher/src-tauri/src/commands/openai_cmd.rs`: `register_openai_api_key(value, set_as_default)`, `validate_openai_api_key(value, model?)`, `recheck_openai_validity()`. Validation uses the FREE `GET https://api.openai.com/v1/models/text-embedding-3-small` endpoint (no token consumption, no billing entry — verified against OpenAI docs). Startup re-check task in `lib.rs::setup()` runs the recovery state machine: previously-valid-now-invalid → fallback to local + stash openai-defaults in `app_state.openai_fallback_pending`; previously-invalid-now-valid → restore from stash + clear. Tauri events `vct-openai-key-invalidated` / `vct-openai-key-restored` drive Preferences toasts. Keychain access via the existing `keyring` crate — cross-OS (macOS Keychain, Windows Credential Manager, Linux Secret Service).

- **`feat(weaviate)` multi-slot named-vector schema** (Wave A Commit 4). KG-shaped collections (per-project KG, shared KG, Development) gain `arctic2_embed` (1024d) + `openai_text_embed` (1536d) slots alongside the existing `qwen3_embed` / `ollama_embed` / `openai_embed`. Code collections (`CodeModule` / `CodeClass` / `CodeFunction` / `CodeAPI` / `CodeInteraction`) gain `qwen3_embed` (CPU fallback) + `jina_embed` (768d) + `openai_code_embed` (1536d) alongside the existing `codesage_embed` + `ollama_code_embed` + `openai_embed`. UNION strategy preserves all legacy slot data through the migration (additive, non-destructive). New `vco_lib/weaviate_schema.py` module exposes the canonical slot list (`KG_NAMED_VECTORS`, `CODE_NAMED_VECTORS`) + extensibility helpers (`add_named_vector_slot`, `migrate_collection_to_target`). `migrate-collections --all-projects` walks every KG-shaped + code-shaped collection on the server.

- **`feat(gui-dropdowns)` KG/Codegraph + Preferences embed-model dropdowns** (Wave B Commit 8). Per-project KG/Codegraph binding fields swap free-text inputs for `<select>` populated from `get_embedding_catalog` Tauri command. Preferences gains "Default text embedding for new projects" + "Default code embedding for new projects" dropdowns. New project page pre-populates from `app_state.default_text_embedding` / `default_code_embedding`. Bare-name legacy code-graph classes (`CodeFunction` etc. without project prefix) hidden from GUI enumeration via `is_codegraph_class` filter — data preserved, just not exposed.

- **`feat(observability)` embedding-failure SessionStart hook** (Wave B Commit 11). `.claude/hooks/embedding-failures-surface.sh` + `.ps1` surface `EMBEDDING_FAILURES.md` content to Claude on next chat start. `vco_lib.embedding_service` extended to write 3 failure surfaces (JSONL log + hint .md + deferral entry); auto-clears on next successful construction. Hook registered in `templates/settings.json.linux.template` + `settings.json.windows.template`.

- **`feat(wizard)` OpenAI key step in OnboardingWizard** (Wave C Commit 6). New wizard step after GitHub PAT: password-masked API key input with Show/Hide toggle, Validate button (free `/v1/models` probe), checkbox "Use OpenAI as the default embedding provider for new projects" (disabled until validated). Skip path always available. Inline feedback in 6 states (idle / validating / valid / valid-rate-limited / invalid / network-error). Full ARIA + keyboard navigation. No auto-switch outside the checkbox — locked design decision.

- **`feat(preferences)` OpenAI key row + Re-check + styled modal + recovery toasts** (Wave C Commit 7). Preferences gains OpenAI section symmetric to GitHub PAT: password-masked input, Apply + Re-check + Clear buttons, 7-state status indicator (idle / unvalidated / working / valid / previously_valid_failing / invalid / network-error). Apply with valid key prompts via styled Svelte modal "Set OpenAI as default for new projects?" — no auto-switch. Recovery toasts wired to Wave A's `openai_key_invalidated` / `openai_key_restored` Tauri events.

- **`feat(install)` install.py writes preset defaults to launcher app_state** (Wave C Commit 10). Detected hardware preset (gpu/cpu/openai/low_resource) → default text + code embedding model IDs → `app_state.default_text_embedding` / `default_code_embedding`. GUI dropdowns on new project pages pre-populate from these. Idempotent: only sets if currently NULL (preserves user manual selections). DB path resolved via existing `vco_lib.paths.vct_root_dir` helper (cross-OS: `~/.vct/launcher.db` on Linux/macOS, `%USERPROFILE%\.vct\launcher.db` on Windows; `VCT_STATE_DIR` env override supported).

- **`feat(enrichment)` idempotent enrichment migration for embed-slot changes** (Wave D Commit 9). New `vco_lib/embedding_enrichment.py::enrich_collection_vectors` walks one Weaviate collection adding new-slot vectors to objects that lack them. Preserves all other slots (never deletes). Pre-checks: collection exists, slot is in the schema (else raises `SlotNotInSchemaError` pointing to `migrate-collections`), backend reachable. Batches in groups of 100. Soft-fail per object — whole run continues. Tauri command `enrich_collection_vectors` + Svelte `EnrichmentProgressModal` mount when user changes a model dropdown + clicks Save. **Multi-class codegraph sweep**: codegraph Save runs enrichment sequentially across ALL 5 sibling classes (`CodeModule` + `CodeClass` + `CodeFunction` + `CodeAPI` + `CodeInteraction`) — closes the correctness gap where search against 4 of 5 returned nothing until manual re-enrich.

- **`feat(dev-collection)` Development collection `content_hash` + `status` parity with KG** (Wave D bonus). Pre-v0.2.18 Dev collections lacked the embed-skip fast-path property — every install re-embedded every `docs/` file. New `content_hash` property mirrors the v0.2.17 KG addition; sync_doc checks hash + active-slot population before re-embedding. New `status` property allows archived/draft docs to be excluded from `hybrid_search` via the existing stale-filter. KG-only graph properties (`tags`, `links`, `typed_links`, `node_type`) explicitly NOT mirrored. Smoke test confirmed 820x speedup on unchanged content (896ms → 1ms second sync).

- **`feat(embed)` code backend fallback chain — codesage → qwen3 → jina** (Wave D tail). When CodeEmbed FastAPI service is down AND `code_model_id="codesage-large-v2"` (the GPU-accelerated default), prior versions failed every embed call (Ollama doesn't have codesage). New chain probes backends at construction time: (1) CodeEmbed service `/health`, (2) Ollama `qwen3-embedding:0.6b`, (3) Ollama `unclemusclez/jina-embeddings-v2-base-code:latest`. First reachable wins. `code_vector_slot` updates to match the resolved backend so search-by-active-slot stays correct.

- **`feat(codegraph)` language-scoped prune + Re-analyze button (Plan C)** (Wave D tail). `language` property added to `CodeClass` / `CodeFunction` / `CodeAPI` / `CodeInteraction` (already on `CodeModule`). Analyzer auto-stamps the canonical language id on every insert via a dispatcher-level `_current_language` field. Post-edit hook (`code-graph-incremental.{sh,ps1}`) now detects edited file's language from extension + invokes `analyze --incremental --language <lang> --prune-stale` — language-scoped prune correctly cleans up deleted-file entries WITHOUT touching other languages' rows. New Tauri command `reanalyze_code_graph` + Svelte `CodeGraphReanalysisModal` for the user-triggered explicit refresh (no `--language`, full multi-language walk + global prune). The pre-Plan-C warning about `--prune-stale + --language` deletion is removed — the combo is now the correct mode.

### Fixed

- **`fix(install)` 0.0 A3 routing — always run git-pull-case deferral helper** (Commit 1, pre-Wave-A landed early). Earlier v0.2.17 `_refresh_dist_binary_after_rebuild` used `if not src.is_file()` as a routing gate ("src exists ⇒ cargo path will emit"). False — cargo path silently bails on Gate 2 when `src_mtime <= dist_mtime` (common when a stale `target/release/vct-launcher-temp` from a prior local build exists), leaving the deferral unemitted on git-pull updates. Fix: run the helper unconditionally; idempotent via existing version-equality check.

- **`refactor(scripts)` migrate embed consumers to EmbeddingService** (Wave B Commit 5). `sync_knowledge_graph.py` deletes the qwen3-only `RuntimeError` assertion (audit finding KG-W1, 2026-04-30). `analyze_code_graph.py`, `maintain_knowledge_graph.py`, `process_documents.py`, `search_knowledge.py`, `query_code_graph.py`, `claude_mcp_servers/weaviate_mcp/server.py`, `claude_mcp_servers/scripts/migrate_to_new_embeddings.py` all migrated to `EmbeddingService.for_project()`. After migration: only `vco_lib/embedding_service.py` reads `EMBEDDING_MODEL` / `ACTIVE_EMBEDDING` directly.

- **`chore(audit)` OpenAI dropdown ID prefix unification** (Wave D tail). Catalog emission boundary translates raw OpenAI model names (`text-embedding-3-small`) to prefixed form (`openai-text-embedding-3-small`) for the GUI; the HTTP-call boundary strips the prefix back to raw for OpenAI's `/v1/embeddings` API. Closes the cross-commit bug where dropdown pre-select compared `app_state` prefixed-form values against catalog raw-form IDs and silently mismatched.

- **`chore(cleanup)` minor polish items bundled** (Wave D tail). `ensure_collection_exists` in `sync_knowledge_graph.py` now reads `KG_NAMED_VECTORS` from `vco_lib.weaviate_schema` (mirrors the Dev variant). `install.py::_write_preset_defaults_to_app_state` drops unused `install_root` parameter (test injection via `VCT_STATE_DIR` env override). `focusTrap` + `focusOnMount` Svelte actions extracted from Preferences page to `$lib/actions/focusManagement.ts` for reuse.

### Migration notes

- **Schema migration via existing `patch_props` flow** (additive, non-destructive). Add Project + Update Bundle routes already trigger `migrate-collections --dry-run` and emit `schema_migration_required` deferral when drift detected. User runs `migrate-collections` (no `--dry-run`) to apply: existing v0.2.17 KG collections gain `arctic2_embed` + `openai_text_embed`; existing code-graph collections gain the new code slots + `language` property; existing Dev collections gain `content_hash` + `status`. All data preserved.
- **OpenAI integration is opt-in.** No-key installs continue to use local models (qwen3 + codesage). Key entered in wizard with checkbox = sets OpenAI as default for new projects. Key entered in Preferences without checkbox = key stored but defaults unchanged (no auto-switch). Validity-recheck cadence: launcher startup + on-demand Preferences button.
- **Failure surfaces.** Embedding backend unreachable now writes 3 surfaces (JSONL log + Claude-readable hint + GUI deferral banner) — auto-cleared on success.

## [0.2.17] — 2026-05-18

### Fixed

- **`fix(release)` commit-dist-binaries race-tolerant push**: the
  job now checks out `main` (not the tag's pre-squash commit) and
  refetches + rebases before pushing. Resolves the non-fast-forward
  push rejection seen on v0.2.16 release run 26045579108, where PR
  #245's squash-merge left the tag's commit on a divergent ancestry
  even though its tree was identical to the merge commit on main.
  Also handles the rare race where main advances during the
  15-20 min build matrix. Anchors expected for v0.2.17 to land
  auto-binary-commit end-to-end without manual fallback.

- **`fix(launcher)` Update Orchestrator end-to-end on Windows + git-pull-case restart detection on all OSes** (v0.2.17 plan 0.0 + 0.0.B). Two coupled bugs:

  **0.0.B (Windows blocker):** `update_orchestrator` runs `git pull --ff-only` from inside the launcher's own working directory. On Windows, the running launcher binary `launcher/dist/windows-x64/vct-launcher.exe` is OS-locked. Git fails atomically with `ERROR_SHARING_VIOLATION`, reverting the entire pull — no source update, no binary update, no deferral entry. Update Orchestrator was therefore totally broken on Windows for any release touching the launcher binary. Fix: before `git pull`, rename the running binary aside as `<binary>.old-<pid>` (Windows allows rename-while-running; only overwrite is forbidden — same trick `_refresh_dist_binary_after_rebuild`'s post-rebuild swap uses at lines 8941-8993). Git can then write the new binary at the canonical path freely. No-op on Linux/macOS (kernels handle running-binary overwrite via inode/vnode ref-counting). On pull failure, the pre-pull rename is reverted (best-effort).

  **0.0 (cross-OS restart detection):** `_refresh_dist_binary_after_rebuild` ONLY emitted the `launcher_restart_required` deferral on the cargo-rebuild path (`launcher/src-tauri/target/release/vct-launcher-temp` → dist swap). The COMMON end-user case — `git pull` lands a pre-built binary directly at `launcher/dist/<arch>/vct-launcher` — never triggered the deferral, so the launcher's banner stayed silent while the on-disk binary diverged from the running PID's binary. Added `_maybe_emit_running_stale_deferral` helper that compares `vct-module.json::version` (source-of-truth post-pull) against `state/install-manifest.json::version` (what install.py wrote last time). On mismatch + `dist_path.is_file()`, emits the deferral. Mutually-exclusive routing with the cargo-rebuild path (Reviewer A finding A3): runs ONLY when `target/release/vct-launcher-temp` is absent, so the two emit sites never double-fire.

  **Auto-restart (replaces banner ceremony):** the launcher is a process manager / dashboard with no in-flight user state — DB writes are transactional, container processes are independent of launcher lifetime, hub HTTP connections reconnect within 5s. `update_orchestrator` now auto-restarts the launcher after install.py exits successfully. Reuses v0.2.15's `spawn_detached_launcher` for cross-OS spawn (`setsid` on POSIX, `DETACHED_PROCESS` on Windows). When `VCT_AUTO_RESTART_LAUNCHER=1` is set (Rust caller), install.py skips emitting the deferral (auto-restart makes it redundant). Manual `python install.py --update` from terminal still emits the deferral so a running launcher's W4 banner picks it up.

  **Auto-restart failure-path fallback** (Reviewer A finding A2): if `restart_launcher` fails after install.py succeeded, `update_orchestrator` re-spawns `install.py --update` with `VCT_AUTO_RESTART_LAUNCHER` unset AND a new `VCT_FORCE_RESTART_DEFERRAL=1` env. The force-emit env tells `_maybe_emit_running_stale_deferral` to land the deferral even when source-vs-manifest comparison would otherwise skip (the first install.py pass already bumped the manifest to match the source). Without this fallback, the user would see "Orchestrator updated successfully!" while running stale in-memory binary with zero signal. On fallback-spawn failure, `update_orchestrator` returns a real `Err` to the GUI with manual-restart guidance.

  **Boot sweep:** `lib.rs::run`'s setup hook now sweeps `launcher/dist/<arch>/*.old-<pid>` and `*.pending-<pid>` siblings, deleting files whose PID is no longer alive (cross-OS PID check: `kill(pid, 0)` on Unix, `OpenProcess` on Windows). Bounded space cost: ≤1 sibling per release in steady state. Replaces the prior fixed-name `.old.exe` sweep at the same call site.

- **`fix(sync)` content-hash skip avoids re-embedding unchanged KG nodes** (v0.2.17 plan 0.2). Every `install.py --update` step 7c (`Seeding Weaviate with bundled knowledge/ + docs/`) hammered Ollama's `/api/embeddings` for ~1500 vectors (per-project KG + shared KG, ~767 each) for several minutes of sustained GPU work even when zero `knowledge/**/*.md` or `docs/**/*.md` content changed between releases. Observed during v0.2.15 → v0.2.16 upgrade from VCO_dev's launcher GUI (sustained 5-10 embed calls/sec, manifest finally flipped to 0.2.16 with `uuid_scheme=v2` correctly, but cost ~1500 wasted embeddings). Root cause (`templates/scripts/sync_knowledge_graph.py:1225-1296`): `sync_node()` unconditionally deletes the existing Weaviate object, regenerates the embedding, and re-inserts — there was NO content-hash skip at the embedding level. The earlier `_update_frontmatter_timestamp` skip only prevented the file-system write of the `updated:` timestamp. Fix: add `content_hash` property to the KG schema (in BOTH `templates/scripts/sync_knowledge_graph.py`'s `ensure_collection_exists` schema-create + migration loop AND `vco_lib/project_init.py::kg_class_definition` so fresh per-project KGs ship with it from day 0 instead of acquiring it lazily on first sync); on every insert (single-chunk + multi-chunk paths), populate it with `_content_signature_excluding_updated(content)` (same hash function as the file-write skip); BEFORE the delete-and-embed pipeline, query existing objects with the same `file_path` and skip the entire pipeline when every chunk's `content_hash` matches the current source's hash AND is non-empty AND chunk-count matches `total_chunks` (Reviewer A finding E2: the chunk-count check guards against partial-write states from a prior mid-write crash). Soft-fail throughout — any exception in the skip check falls through to the original re-embed path. Pre-v0.2.17 objects (without `content_hash`) re-embed on next touch and get tagged, so the run AFTER that one then skips.

## [0.2.16] — 2026-05-18

### Fixed

- **`fix(hook)` post-tool-security smoke-test marker rename**: the
  prior smoke-test sentinel in
  `templates/hooks/post-tool-security.{sh,ps1}` was a common-English-
  looking bare-word token that matched a legitimate CHANGELOG
  release-note entry documenting the marker itself, so every CHANGELOG
  edit triggered a false-positive "Possible credential" alert. The
  sentinel is now a VCT-prefixed, `_PROBE`-suffixed identifier with a
  6-char random hex tail — unique enough that it cannot accidentally
  appear in docs or prose. See the hook source for the actual literal
  (intentionally not quoted here to prevent the same release-note
  collision recurring). The simple bare-pattern check is preserved (no
  regex loosening). No external test fixtures referenced the old name.
- **Code-graph analyzer integrity** (W1 / plan 0.1 + 0.2 + 0.7 + 0.8
  + addendum D + 1.4/H). Five silent-correctness bugs in
  `templates/scripts/analyze_code_graph.py` that combined to make
  the analyzer report success while producing incomplete or empty
  per-project code-graph collections. Edit target is the canonical
  `templates/scripts/` (the installer renders into each project's
  `.claude/scripts/` on `install.py --update`).
    - **0.1 `_dedup_insert` is now actually idempotent**: switched the
      internal call from `collection.data.insert(uuid=...)` (POST-only —
      raises 422 on the second run with the same UUID) to
      `collection.data.replace()` (PUT-upsert — idempotent by contract).
      The previous behaviour swallowed every re-run collision in the
      outer per-file try/except, silently flagging files as "skipped"
      and exiting 0; the wizard then rendered green-toast success while
      most objects never landed. All 25 call sites benefit from the
      single internal change.
    - **0.7 UUID identity-key includes file path**: `_deterministic_uuid`
      signature is now `(project, file_path_rel, full_name)`. Two
      genuinely-different files defining the same `module-stem.symbol`
      (e.g. `server.handler` in `claude_mcp_servers/weaviate_mcp/server.py`
      AND `docs/research/probes/server.py`) no longer collide on the same
      UUID. Cross-OS contract: callers pass `Path(...).as_posix()` so
      Windows backslashes don't produce different UUIDs from the same
      file. All 11 per-language `_analyze_*_file` methods normalise
      `relative_path` via `.as_posix()` before threading it through.
    - **0.8 NameError on `class_uuid` in `_extract_class`**: the
      `self._dedup_insert(...)` call's return value was discarded and
      the next line referenced an unbound `class_uuid`. Every Python
      file containing a class hit a `NameError` that the outer
      try/except swallowed into `files_skipped`. Fixed by capturing the
      return: `class_uuid = self._dedup_insert(...)`.
    - **0.2 non-zero exit code on insert failures**: `stats['insert_errors']`
      now tracks per-file write-to-Weaviate failures separately from
      generic parse/IO errors via the new `_DedupInsertError` wrapper.
      `main()` returns exit code `3` (`E_NO_FILES_INDEXED`) when no
      files succeeded, `4` (`E_PARTIAL_INSERT_FAILURES`) when at least
      one insert failed. Launcher's `rebuild_code_graph` IPC can now
      surface warning toasts instead of silent success.
    - **Addendum D worktree-skip + ignore_dirs refactor**: module-level
      `_COMMON_IGNORE_DIRS` frozenset + `_ignore_dirs_for(language)`
      helper replaces 11 near-duplicate inline sets. Adds `"worktrees"`
      — skips `.claude/worktrees/agent-<hex>/` git-worktree clones that
      would otherwise be re-analyzed alongside the main repo.
    - Tests added: `tests/test_analyze_code_graph_v0_2_16.py` (9 tests,
      all pure-Python unit tests against the analyzer module — no
      Weaviate required). Existing `tests/test_analyze_code_graph_retry_cap.py`
      still passes against a live Weaviate.
- **`fix(release)`** (W2 / plan 0.4): `commit-dist-binaries` Stage step
  now reads the flat artifact layout `actions/upload-artifact@v7`
  actually produces (`<artifact_dir>/<binary>`), not the nested
  `<artifact_dir>/launcher/dist/<target>/<binary>` the pre-v0.2.16 code
  assumed. Root cause of the v0.2.15 auto-commit failure
  (`Expected artifact not found: _dist-artifacts/linux-x64/launcher/dist/linux-x64/vct-launcher`)
  — the build matrix succeeded and the Release page got all three OS
  zips; only the auto-binary-commit step failed, which the maintainer
  worked around via the manual binary-commit recipe (commit 0d24812).
  Verified by inspection against `actions/upload-artifact@v7`'s flat-
  file layout for individual-file `path:` entries. Will be exercised
  end-to-end on the v0.2.16 release run.

### Added

- **`feat(install)` uuid_scheme manifest marker** (W2 / addendum F):
  `state/install-manifest.json` now writes `uuid_scheme = "v2"` on
  every install/update/lightweight path. Pairs with W1's analyzer
  changes that key code-graph UUIDs on `(project, file_path, full_name)`
  rather than the pre-v0.2.16 `(project, full_name)` tuple. Pre-v0.2.16
  manifests have no `uuid_scheme` field — readers MUST treat the
  absence as the implicit `"v1"` scheme. Future migration tooling
  consults this marker to decide whether existing code-graph
  collections need a `--force-recreate` rebuild.
- **`feat(install)` migrate-uuid-scheme stub flag** (W2): stub
  `--migrate-uuid-scheme` flag on `install.py` for the upcoming
  code-graph UUID-key migrator. Currently a no-op beyond the manifest
  marker above; real migration tool ships post-v0.2.16.
- **`feat(analyzer)` `--prune-stale` flag** (W1 + W3 wire-up / plan 1.4
  + addendum H): opt-in tracking of every UUID visited during the
  analyze run; at end, every per-project collection's unvisited UUIDs
  are deleted. Closes the "shrunken codebase leaves orphan rows" gap
  that switching to `replace()` upsert semantics would otherwise
  create. Wizard's Re-analyze button passes `--prune-stale` by default
  (checkbox "Clean stale entries during re-analysis"); first-time
  builds (`create_project_v2`) and boot-resume sweeps pass `false` to
  preserve conservative semantics. Skips with a stderr warning when
  combined with `--language` (would falsely delete other-language
  objects).
- **`feat(wizard)` per-project poll status** (W3 / plan 0.3):
  legacy-collections wizard now polls per-project rebuild status
  until terminal. The kickoff counter used to read "Started 3 / 3
  project(s)" forever because the Rust handler returns immediately
  after spawning the analyzer subprocess; we now invoke a new
  `get_code_graph_build_status_for_projects` Tauri command every
  2 seconds and render a per-project row with icon + status label +
  files-analyzed count + any error message. The wizard does NOT
  auto-close on completion — the user can review the final
  per-project status before dismissing.
- **`feat(wizard)` session-scoped dismissal + Re-check button** (W3 /
  plan 0.9): "Dismiss" button renamed to "Dismiss for now" + companion
  auto-reset of `legacy_codegraph_notice_dismissed` on every
  `rebuild_code_graph` invocation + new "Re-check for legacy
  collections" button in Preferences. Together these change the
  dismissal from "permanently suppress until manually flipped" to
  session-scoped with multiple re-arm paths (re-analyze a project, or
  click Preferences → Code-graph collections → Re-check). New Tauri
  command `force_recheck_legacy_codegraph` backs the Preferences
  button.

### Changed

- `commands::codegraph::rebuild_code_graph` now resets
  `app_state[legacy_codegraph_notice_dismissed]` to `false` after
  inserting the pending row (W3 / plan 0.9). Re-analyzing a project
  is the most common cause of new orphan generations appearing, so
  the wizard should re-detect on the next launcher start. Soft-fail
  — a hiccup writing the flag is logged but does not block the
  rebuild.
- `check_for_updates` now returns a full `UpdateStatus` struct (W4 /
  plan 0.5) replacing the legacy `bool` with three independent flags:
  - `remote_ahead` — local git branch behind `origin/main` (resolves
    via `update_orchestrator`: git pull + install.py --update).
  - `install_stale` — `vct-module.json::version` ahead of
    `state/install-manifest.json::version` (resolves via
    `apply_pending_install`).
  - `binary_stale` — running launcher version differs from
    `launcher/dist/<arch>/vct-launcher.metadata.json::launcher_version`
    on disk (resolves via `restart_launcher`).
- `UpdateBadge.svelte` renders one state at a time in priority order
  (W4 / plan 0.5): `binary_stale` > `install_stale` > `remote_ahead`,
  each wired to the correct resolver. v0.2.15 shipped the
  binary-restart half via `LauncherRestartBanner` but the
  install-stale path was missing — visible install-was-out-of-sync
  for 24+ hours after a manual `git pull` with no UI signal.
- `list_legacy_codegraph_collections` and `codegraph_list_projects`
  accept a new `include_untracked_projects: Option<bool>` parameter
  (default `false`) (W4 / plan 0.11). The GUI legacy-collections
  wizard + Code Graph dashboard now filter Weaviate collections by
  currently-tracked projects, hiding dead-project leftovers
  (`MediaLibrary_*`, `ARTup_*`, `Agape_*`, etc.). Data stays in
  Weaviate for potential re-import; the advanced
  `/preferences/weaviate-untracked` route surfaces the full
  inventory.

### Added (continued)

- **`feat(launcher)` apply_pending_install Tauri command** (W4 / plan
  0.5): resolves the "Pulled-but-not-installed" banner state by
  running `install.py --update` against the existing install root
  WITHOUT a preceding `git pull`. Distinct from `update_orchestrator`
  (which does both) so manual `git pull` workflows don't waste ~30s
  pulling an already-current source tree.
- **`feat(launcher)` /preferences/weaviate-untracked advanced route**
  (W4 / plan 0.11): surfaces the full Weaviate code-graph collection
  inventory, including prefixes whose project is no longer registered
  with the launcher. Each row exposes a per-prefix delete affordance.
  Reachable from Preferences → "Show untracked Weaviate collections".

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
