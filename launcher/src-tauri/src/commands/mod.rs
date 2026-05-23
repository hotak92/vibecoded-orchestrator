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
pub mod embedding_catalog;
pub mod embedding_enrichment;
// v0.2.24 §A0 (2026-05-22): per-path 3-way merge for known
// user-editable files during orchestrator-root updates. Sits between
// `installer::{update_orchestrator, merge_orchestrator_with_upstream}`
// and their `git pull` invocations.
pub mod git_user_editable_merge;
pub mod hub_proxy;
pub mod installer;
pub mod kg;
pub mod kg_sync;
pub mod kg_summary;
pub mod lifecycle;
pub mod licensing;
pub mod maintenance;
pub mod manifest;
pub mod module_deprecation;
pub mod module_dispatch;
pub mod module_gui;
// v0.2.21 Stream B: per-(project × module × embedding_source) weights state.
// Tauri-commands wrapper around the `module_weights_state` table
// (migration 016). The DB plumbing lives in
// `vct-launcher-core/src/db/module_weights_state.rs`.
pub mod module_weights_state;
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
