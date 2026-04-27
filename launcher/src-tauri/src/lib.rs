mod commands;
mod db;
mod hub;
mod installer_engine;
mod manifest;
mod mcp_registration;
mod quit_dialog;
mod registry;
mod secrets;
mod state;
mod tray;
mod types;

use state::{AppManager, ProjectState, ProjectStore};
use std::collections::HashMap;
use std::sync::Mutex;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let _initial_registry = registry::load_service_registry();

    let app_manager = AppManager(Mutex::new(HashMap::new()));

    let projects = load_projects_from_disk();
    let project_store = ProjectStore(Mutex::new(ProjectState {
        projects,
        active_project: None,
    }));

    // Open / migrate the launcher DB. A failure here is fatal: the module
    // system depends on it. Log the error and abort rather than silently
    // running with stale JSON state.
    let db_handle = db::Db::open().unwrap_or_else(|e| {
        eprintln!("[vct] FATAL: cannot open launcher.db: {}", e);
        std::process::exit(1);
    });

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(app_manager)
        .manage(project_store)
        .manage(db_handle)
        .setup(|app| {
            // Start the Hub API server in the background
            tauri::async_runtime::spawn(async {
                match hub::server::start_hub_server().await {
                    Ok(port) => println!("[vct] Hub server started on port {}", port),
                    Err(e) => eprintln!("[vct] Hub server failed to start: {}", e),
                }
            });
            // System tray (v1.1)
            if let Err(e) = tray::setup(&app.handle()) {
                eprintln!("[vct] tray setup failed: {}", e);
            }
            Ok(())
        })
        // Intercept window-close (X / Cmd+Q) on the main window. We
        // prevent the default close, then defer to the same confirmation
        // dialog used by the tray Quit item. Programmatic quits set the
        // FORCE_QUIT flag (see quit_dialog::force_quit) and bypass.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                use tauri::Manager;
                if window.label() == "main" && !quit_dialog::should_skip_dialog() {
                    api.prevent_close();
                    let app = window.app_handle().clone();
                    quit_dialog::confirm_and_quit(&app);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            // Lifecycle (existing, unchanged)
            commands::lifecycle::launch_app,
            commands::lifecycle::kill_app,
            commands::lifecycle::get_app_status,
            commands::lifecycle::get_all_app_statuses,
            commands::lifecycle::check_app_health,
            commands::lifecycle::check_all_health,
            // Projects — legacy JSON-backed (kept for React components not yet migrated)
            commands::projects::create_project,
            commands::projects::get_projects,
            commands::projects::update_project,
            commands::projects::open_project,
            commands::projects::close_project,
            // Projects — v2 DB-backed
            commands::projects_v2::list_projects_v2,
            commands::projects_v2::get_project_v2,
            commands::projects_v2::get_project_by_slug,
            commands::projects_v2::create_project_v2,
            commands::projects_v2::rename_project_v2,
            commands::projects_v2::switch_project_host_v2,
            commands::projects_v2::delete_project_v2,
            commands::projects_v2::launch_project_in_editor,
            // Concurrency invalidation (P7) — change_log polling
            commands::changes_cmd::poll_changes,
            commands::changes_cmd::current_change_seq,
            // Modules — catalog + install + lifecycle
            commands::modules::list_module_catalog,
            commands::modules::install_module_for_project,
            commands::modules::uninstall_module_v2,
            commands::modules::list_installed_modules,
            commands::modules::module_status_v2,
            commands::modules::set_module_enabled_v2,
            // Per-project orchestrator state (agents/skills/hooks/permissions/secrets/KG/codegraph)
            commands::project_state_cmd::list_project_agents,
            commands::project_state_cmd::list_project_skills,
            commands::project_state_cmd::list_project_hooks,
            commands::project_state_cmd::list_project_permissions,
            commands::project_state_cmd::list_project_secret_refs,
            commands::project_state_cmd::get_project_state_snapshot,
            commands::project_state_cmd::register_project_agent,
            commands::project_state_cmd::set_project_agent_enabled,
            commands::project_state_cmd::unregister_project_agent,
            commands::project_state_cmd::register_project_skill,
            commands::project_state_cmd::set_project_skill_enabled,
            commands::project_state_cmd::unregister_project_skill,
            commands::project_state_cmd::register_project_hook,
            commands::project_state_cmd::set_project_hook_enabled,
            commands::project_state_cmd::unregister_project_hook,
            commands::project_state_cmd::add_project_permission,
            commands::project_state_cmd::delete_project_permission,
            commands::project_state_cmd::set_project_secret_ref,
            commands::project_state_cmd::delete_project_secret_ref,
            commands::project_state_cmd::set_project_kg_binding,
            commands::project_state_cmd::set_project_codegraph_binding,
            // Secrets + settings
            commands::secrets_cmd::set_secret_v2,
            commands::secrets_cmd::clear_secret_v2,
            commands::secrets_cmd::is_secret_set,
            commands::secrets_cmd::get_secret_preview,
            commands::secrets_cmd::get_setting_v2,
            commands::secrets_cmd::set_setting_v2,
            commands::secrets_cmd::list_module_settings_v2,
            // Licensing
            commands::licensing::license_get_tier,
            commands::licensing::license_is_admin,
            commands::licensing::license_refresh,
            commands::licensing::license_activate,
            commands::licensing::license_deactivate,
            // KG dashboard
            commands::kg::kg_list_collections,
            commands::kg::kg_set_collection_access,
            commands::kg::kg_load_graph,
            commands::kg::kg_search,
            commands::kg::kg_get_node,
            commands::kg::kg_promote_to_shared,
            // Codegraph access matrix
            commands::codegraph::codegraph_list_access,
            commands::codegraph::codegraph_grant_access,
            commands::codegraph::codegraph_check_access,
            commands::codegraph::codegraph_summary,
            // Coordination tab
            commands::coordination::coordination_get_config,
            commands::coordination::coordination_set_config,
            commands::coordination::coordination_test_connection,
            commands::coordination::coordination_apply_schema,
            commands::coordination::coordination_team_status,
            // MCP registration (.claude.json / .mcp.json editor)
            commands::mcp_reg::register_module_mcp,
            commands::mcp_reg::deregister_module_mcp,
            // Telemetry consent + dashboard
            commands::telemetry_cmd::telemetry_status,
            commands::telemetry_cmd::telemetry_set_consent,
            commands::telemetry_cmd::telemetry_recent_events,
            commands::telemetry_cmd::telemetry_clear_queue,
            // Orchestrator installer (existing, unchanged)
            commands::installer::detect_system,
            commands::installer::detect_existing_services,
            commands::installer::get_default_install_path,
            commands::installer::check_install_status,
            commands::installer::get_installed_version,
            commands::installer::check_for_updates,
            commands::installer::install_orchestrator,
            commands::installer::preview_install,
            commands::installer::preflight_install_safety_check,
            commands::volumes::get_volumes_config,
            commands::volumes::set_volumes_config_for_install,
            commands::volumes::set_volumes_config_dry_run,
            commands::volumes::migrate_volumes,
            commands::installer::update_orchestrator,
            commands::installer::get_local_repo_source,
            commands::installer::inspect_orchestrator_at,
            commands::installer::update_orchestrator_at,
            commands::installer::has_github_pat,
            commands::installer::get_github_pat_preview,
            commands::installer::register_github_pat,
            commands::installer::clear_github_pat,
            // KG dashboard — extended (v1.1)
            commands::kg::kg_set_collection_access_mode,
            commands::kg::kg_set_node_access,
            commands::kg::kg_set_node_access_bulk,
            commands::kg::kg_ensure_node_access_schema,
            // Codegraph — graph viz (v1.1)
            commands::codegraph::codegraph_load_graph,
            commands::codegraph::codegraph_set_entity_access_bulk,
            // Codegraph — Gap 2: initial build status + manual rebuild
            commands::codegraph::get_code_graph_build_status,
            commands::codegraph::rebuild_code_graph,
            // Hub proxy (v1.1)
            commands::hub_proxy::hub_info,
            commands::hub_proxy::hub_list_apps,
            commands::hub_proxy::hub_poll_messages,
            commands::hub_proxy::hub_data_catalog,
            // Dashboard: tier, features, MCP management
            commands::dashboard::get_feature_flags,
            commands::dashboard::get_orchestrator_config,
            commands::dashboard::save_orchestrator_config,
            commands::dashboard::update_orchestrator_setting,
            commands::dashboard::get_mcp_servers,
            commands::dashboard::toggle_mcp_server,
            commands::dashboard::update_mcp_setting,
            commands::dashboard::add_custom_mcp_server,
            commands::dashboard::remove_mcp_server,
            // Audit log read API
            commands::audit::list_audit_events,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn load_projects_from_disk() -> HashMap<String, types::Project> {
    let path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("projects.json"))
        .unwrap_or_else(|| ".vct/projects.json".into());

    if !path.exists() {
        return HashMap::new();
    }
    let data = std::fs::read_to_string(&path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}
