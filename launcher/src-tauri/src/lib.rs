mod commands;
mod db;
mod hub;
mod installer_engine;
mod manifest;
mod mcp_registration;
mod quit_dialog;
mod registry;
mod secrets;
mod services;
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
            // Daily launcher self-update check. Honors `auto_check_enabled`
            // toggle in ~/.vct/launcher-update-state.json (default ON).
            // Emits `vct-launcher-update-available` event when remote HEAD
            // has new commits — never auto-applies.
            commands::self_update::spawn_daily_check(app.handle().clone());
            // Auto-start the shared compose stack (Weaviate / Ollama /
            // code_embed). Runs in the background — must NOT block the
            // tray or main window from rendering. Surfaces progress via
            // the `vct-services-lifecycle` event; surfaces externally-
            // managed services for the Adopt/Parallel dialog via
            // `vct-external-services-detected`. See
            // `commands::lifecycle::auto_start_on_boot` for the state
            // machine.
            let app_handle_for_services = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                commands::lifecycle::auto_start_on_boot(app_handle_for_services).await;
            });
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
            // Container-services lifecycle (Podman/Docker compose).
            // App-launch suite (launch_app/kill_app/get_app_status/etc.) was
            // archived 2026-04-28: zero FE consumers (Svelte) and zero Hub
            // consumers. The extracted source lives in the orchestrator's
            // private launch-assets archive (launcher-archived-rust/
            // lifecycle_app_process.rs) for future re-introduction.
            commands::lifecycle::services_status,
            commands::lifecycle::services_start_all,
            commands::lifecycle::services_stop_all,
            commands::lifecycle::services_restart_all,
            commands::lifecycle::service_start,
            commands::lifecycle::service_stop,
            commands::lifecycle::service_restart,
            commands::lifecycle::services_set_adoption,
            commands::lifecycle::services_get_adoption,
            commands::lifecycle::services_reset_adoption,
            commands::lifecycle::services_find_free_port,
            // Container-runtime install (no-runtime modal). Linux uses
            // pkexec to elevate apt/dnf/pacman; macOS/Windows just open
            // the canonical install page in the user's default browser.
            commands::runtime_install::runtime_install_podman_linux,
            commands::runtime_install::runtime_open_install_url,
            commands::runtime_install::runtime_recheck,
            // Projects v1 (legacy JSON-backed) was deleted 2026-04-28 — frontend
            // is 100% Svelte and uses projects_v2 exclusively. The file
            // commands/projects.rs and types CreateProjectRequest/
            // UpdateProjectRequest are gone from this tree; recover from git
            // history if v1 ever resurfaces.
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
            // Codegraph access matrix — per-project access control. UI for
            // matrix list/summary/bulk-set ships in v1.x; grant/check are
            // helper APIs used by the bulk path and reserved for future
            // single-row UX. KEEP: deliberately post-v1 surface.
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
            // MCP registration: register_module_mcp / deregister_module_mcp
            // were archived 2026-04-28. The actual write path goes through
            // `mcp_registration::register_mcp` / `deregister_mcp` invoked
            // server-side by dashboard.rs and installer.rs — the Tauri
            // command wrappers had zero FE/Hub consumers. The extracted
            // source lives in the orchestrator's private launch-assets
            // archive (launcher-archived-rust/mcp_reg.rs).
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
            commands::installer::detect_existing_install_root,
            // Durable install log reader. Backs the OnboardingWizard's
            // skip-if-installed path + a future Settings → Install
            // Diagnostics panel. Pull-only: the FE invokes on demand.
            commands::installer::read_install_log,
            // TODO(safety): wire preflight_install_safety_check to the
            //   OnboardingWizard's confirm-step. Currently `preview_install`
            //   covers the diff-mode path; preflight returns the richer
            //   SafetyReport (volumes, collections, services classification)
            //   and should run before clicking Install on a fresh path.
            commands::installer::preflight_install_safety_check,
            commands::volumes::get_volumes_config,
            commands::volumes::set_volumes_config_for_install,
            commands::volumes::set_volumes_config_dry_run,
            commands::volumes::migrate_volumes,
            commands::installer::update_orchestrator,
            commands::installer::get_local_repo_source,
            commands::installer::inspect_orchestrator_at,
            commands::installer::update_orchestrator_at,
            // GitHub PAT lifecycle. `register_github_pat` is wired in the
            // OnboardingWizard (Bug 22). The read/clear surface (has/preview/
            // clear) is registered for the v1.x "Manage Token" UI which
            // hasn't been built yet — keep registered so the next FE sweep
            // can wire it without backend churn.
            // TODO(v1.x): wire has_github_pat / get_github_pat_preview /
            //   clear_github_pat to a Manage Token settings page.
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
            // TODO(v1.x): wire save_orchestrator_config to a Settings UI
            //   "Save" button. update_orchestrator_setting handles the
            //   per-key path; this command is the bulk-write counterpart.
            commands::dashboard::save_orchestrator_config,
            commands::dashboard::update_orchestrator_setting,
            commands::dashboard::get_mcp_servers,
            commands::dashboard::toggle_mcp_server,
            commands::dashboard::update_mcp_setting,
            commands::dashboard::add_custom_mcp_server,
            commands::dashboard::remove_mcp_server,
            // Audit log read API
            commands::audit::list_audit_events,
            // Launcher self-update (git-pull based, daily check)
            commands::self_update::check_for_launcher_update,
            commands::self_update::apply_launcher_update,
            commands::self_update::get_user_owned_paths,
            commands::self_update::get_cached_update_status,
            commands::self_update::set_auto_check_enabled,
            commands::self_update::get_auto_check_enabled,
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
