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
pub mod coordination;
pub mod dashboard;
pub mod desktop_shortcut;
// Phase 1.1 of the diagrams (Mermaid + Excalidraw) integration plan
// (.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md).
// Sibling Phase 1.2/1.3/1.5 agents stub these command names — keep
// stable; the merge wires their tabs/wrappers against these.
pub mod diagrams_cmd;
pub mod embedding_catalog;
pub mod embedding_enrichment;
// v0.2.24 §A0 (2026-05-22): per-path 3-way merge for known
// user-editable files during orchestrator-root updates. Sits between
// `installer::{update_orchestrator, merge_orchestrator_with_upstream}`
// and their `git pull` invocations.
pub mod git_user_editable_merge;
pub mod hub_proxy;
pub mod installed_modules;
pub mod installer;
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
pub mod module_db;
// v0.2.32 #D: explicit "download default RL weights" Tauri command +
// runtime-value resolver for L6 MultiSelectFilter. Separate from
// `module_service` because the upstream flow (daily poller + apply
// + finetune) is distinct from this one-shot "give me the .pt now"
// path the manifest button dispatches.
pub mod module_default_weights;
pub mod module_deprecation;
pub mod module_dispatch;
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
pub mod projects_v2;
pub mod project_env_settings;
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
