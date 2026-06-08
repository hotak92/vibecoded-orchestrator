pub mod app_state_cmd;
pub mod audit;
pub mod changes_cmd;
pub mod claude_env;
pub mod gpu;
pub mod gpu_policy;
pub mod codegraph;
// v0.2.18 (Plan C): Re-analyze runner. Forks the Tauri-streaming pattern
// from `embedding_enrichment` against `analyze_code_graph.py
// --json-progress`. Re-analyze always passes `--prune-stale` (it's the
// "authoritative refresh" path); `--language` is optional and scopes the
// re-walk + prune to one language.
pub mod codegraph_reanalyze;
// v0.2.47 RL-7.5 (2026-06-04): chunker-revision deferral. Writes an
// UPDATE_DEFERRED.md row when an upgrade crosses the v0.2.46 chunker
// boundary so the user re-syncs KG / codegraph against the new presets.
pub mod chunker_revision_deferral;
pub mod coordination;
pub mod dashboard;
pub mod desktop_shortcut;
// Phase 1.1 of the diagrams (Mermaid + Excalidraw) integration plan
// (.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md).
// Sibling Phase 1.2/1.3/1.5 agents stub these command names — keep
// stable; the merge wires their tabs/wrappers against these.
pub mod diagrams_cmd;
// v0.2.34 Agent D — `subscribe_to_diagram_changes` + filesystem watcher
// that emits `diagram-changed` Tauri events. Split from `diagrams_cmd`
// because it owns process-wide state (`HashMap<project_id,
// RecommendedWatcher>` + debounce slots) that doesn't belong on the
// per-command shape. See
// `.claude/context/plans/diagrams-frontend-wiring-handoff-2026-05-25.md`.
pub mod diagram_watcher;
// v0.2.36 Agent R — local HTTP server that serves the vendored
// Mermaid/Excalidraw editors at 127.0.0.1:<free-port>. Lazy-started
// on the first `open_diagrams_editor` Tauri command. Replaces the
// embedded Excalidraw editor (broken on Wayland+webkit2gtk) and adds
// a visual Mermaid editor alongside the existing text-only one.
pub mod diagrams_local_server;
pub mod embedding_catalog;
pub mod embedding_enrichment;
// C8 wire-up (2026-05-25): Tauri command `read_env_var` consumed by the
// DiagramsTab Wayland fallback (read XDG_SESSION_TYPE). Secret-shaped
// names are redacted to "" before reaching std::env::var; the FE never
// sees credential-looking values.
pub mod env_cmd;
// v0.2.24 §A0 (2026-05-22): per-path 3-way merge for known
// user-editable files during orchestrator-root updates. Sits between
// `installer::{update_orchestrator, merge_orchestrator_with_upstream}`
// and their `git pull` invocations.
pub mod git_user_editable_merge;
pub mod hub_proxy;
pub mod installed_modules;
pub mod installer;
// v0.2.35 Agent M (2026-05-26): GUI-level preflight check for the install
// pipeline. Runs in `ModuleCatalog.svelte::handleInstall` BEFORE the
// `install_module_for_project` invoke, so the user sees an actionable
// "Install Podman" modal instead of the cryptic "no container runtime
// found" error that `installer_engine::detect_container_runtime` would
// otherwise produce deep inside `run_install`.
pub mod install_preflight;
pub mod kg;
pub mod kg_sync;
pub mod kg_summary;
pub mod lifecycle;
pub mod licensing;
pub mod maintenance;
pub mod manifest;
// v0.2.33 Agent A (L0): public-catalog endpoint client. Fetches paid-module
// catalog metadata from the launcher-controlled Supabase edge function with
// retry-with-backoff + 15min app_state-backed cache + schema_version
// mismatch handling. Consumed by `list_module_catalog` (Agent B's L0a
// refactor) and by the renderer's `↻` button via `refresh_module_catalog`.
pub mod module_catalog_client;
// v0.2.33 Agent B2 (cold-start synth): builds a thin in-memory ModuleManifest
// from the L0 install-slice so `installer_engine::run_install` can drive
// `container_pull` BEFORE the real manifest is extracted from the pulled
// image. The synth is replaced on disk by Agent C's extract step immediately
// after pull succeeds — its lifespan is the install run only.
pub mod l0_manifest_synth;
pub mod module_db;
// v0.2.32 #D: explicit "download default RL weights" Tauri command +
// runtime-value resolver for L6 MultiSelectFilter. Separate from
// `module_service` because the upstream flow (daily poller + apply
// + finetune) is distinct from this one-shot "give me the .pt now"
// path the manifest button dispatches.
pub mod module_default_weights;
pub mod module_deprecation;
pub mod module_dispatch;
// v0.2.49 Stream B: per-project enable toggle for global-scope modules.
// Closes the gap where `vct-rl-reranker`-shape modules (one install on
// the host, visible across every project) had no way to be silenced
// per-project. See `module_enabled.rs` for the full design notes.
pub mod module_enabled;
pub mod module_gui;
// v0.2.33 Agent C (L0b): post-install manifest extraction +
// startup reconciler. `module_manifest_extract` runs after
// `installer_engine::container_pull` to copy /app/vct-module.json
// out of the pulled image to ~/.vct/modules/<id>/vct-module.json
// (atomic write with .bak rollback). `module_reconciler` walks
// every status='installed' row at launcher boot and flips rows
// whose on-disk manifest is missing to status='broken' so the
// catalog tile renders "Reinstall needed" instead of a misleading
// "Open dashboard".
pub mod module_manifest_extract;
pub mod module_reconciler;
// v0.2.31 Agent J: `module_weights_state` Tauri-command surface removed
// alongside migration 020 (which DROPs the underlying table). Weights
// state is now container-owned in `rl_weights_state` (shipped by
// vct-rl-reranker v0.2.6 via its own module-shipped migration). The
// launcher's dashboard reads go through the hub's typed REST surface
// via `module_db_client`.
pub mod module_db_client;
pub mod modules;
// v0.2.21 Stream B: per-project container lifecycle for `runtime.type ==
// "container"` modules (Phase 1E) + weights update poller (Phase 3C) +
// fine-tune-after-download flow (Phase 4A) + dashboard widget commands
// (Phase 4B scaffolding). Phase 1 of Step 24 keeps the supervisor logic
// in this file; Phase 2 (commit b) relocates the supervisor into
// `vct-hub::module_supervisor` and converts the Tauri commands into HTTP
// proxies to the hub's lifecycle endpoints.
pub mod module_service;
pub mod openai_cmd;
// Stream 2 follow-up (v0.2.20 / 2026-05-19): Tauri-command backings for
// the orchestrator-core `gui.config_tab` declared in `vct-module.json`.
// Wraps `.claude/scripts/kg-sync`, `kg-duplicates`, and
// `code-graph-analyze` plus a health-check / open-logs pair.
pub mod orchestrator_core;
pub mod orchestrator_root;
pub mod project_identity;
// v0.2.47 — per-project read-only filesystem paths contributing entities
// to the project's codegraph collection. Backed by `vct-launcher-core::
// db::codegraph_extras` (migration 026). Tauri command surface for the
// launcher GUI's Identity-tab "Extra codegraph paths" panel; resolver
// field `code_graph_extra_paths` on `/api/v1/projects/{id}/config`.
// Plan: .claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md.
pub mod project_codegraph_extras;
pub mod projects_v2;
pub mod project_env_settings;
// v0.2.49 Phase 6 S-4 — boot sanity check that walks every project row,
// verifies the registered folder_path still exists on disk, and stamps
// the `folder_missing_at_last_boot` column. Exposes a Tauri read command
// (`read_project_folder_missing_flags`) the frontend consumes to render
// a non-blocking warning banner on each affected project card.
pub mod project_folder_health;
pub mod project_state_cmd;
pub mod project_state_populate;
pub mod restart;
pub mod retrieval_tuning;
pub mod rl_settings;
pub mod runtime_install;
pub mod secrets_cmd;
pub mod secrets_import;
pub mod self_update;
pub mod storage_ux;
pub mod telemetry_cmd;
pub mod volumes;
