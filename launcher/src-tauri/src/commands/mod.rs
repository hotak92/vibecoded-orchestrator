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
pub mod hub_proxy;
pub mod installer;
pub mod kg;
pub mod kg_sync;
pub mod kg_summary;
pub mod lifecycle;
pub mod licensing;
pub mod maintenance;
pub mod manifest;
pub mod modules;
pub mod openai_cmd;
pub mod orchestrator_root;
pub mod project_identity;
pub mod projects_v2;
pub mod project_env_settings;
pub mod project_state_cmd;
pub mod project_state_populate;
pub mod restart;
pub mod runtime_install;
pub mod secrets_cmd;
pub mod secrets_import;
pub mod self_update;
pub mod storage_ux;
pub mod telemetry_cmd;
pub mod volumes;
