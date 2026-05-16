mod commands;
mod config;
mod db;
mod hub;
mod installer_engine;
mod manifest;
mod mcp_registration;
mod paths;
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

// ---------------------------------------------------------------------------
// v0.2.12 — install-time CLI subcommands
// ---------------------------------------------------------------------------
//
// The launcher binary doubles as a CLI tool for headless install flows.
// install.py shells out here to perform JSON-merge / storage-config writes
// without spinning up the Tauri GUI / system tray.
//
// Two subcommands are wired today (originally separate PRs, unified at
// merge time on `integration/v0.2.12`):
//
//   * `--register-default-mcps <install_root>` (PR-23, Group B): writes the
//     canonical bundled-orchestrator MCP entries into `~/.claude.json` AND
//     (when a project row already exists) the launcher.db. Ports forwarded
//     by install.py via WEAVIATE_PORT / OLLAMA_PORT / CODE_EMBED_PORT /
//     WEAVIATE_GRPC_PORT env vars.
//
//   * `--set-storage-config <named|bind|deferred> [--bind-path service=path]...`
//     (PR-28, Group G): persists the user's storage-mode decision from the
//     install.py interactive prompt to `~/.vct/storage.toml` and regenerates
//     `infrastructure/compose.override.yaml` if needed.
//
// Contract: when `handle_cli_args()` returns `Some(exit_code)` we exit
// immediately. The GUI startup path is gated on `None`. This MUST stay
// gated before `tauri::Builder::default()` so a CLI invocation in CI /
// install.py never tries to materialize a window.
//
// Manual arg parsing (no clap dep). Each subcommand has a dedicated
// handler. To add a new subcommand: extend the `match` arm in
// `handle_cli_args()`.
fn handle_cli_args() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        return None;
    }
    match args[1].as_str() {
        "--set-storage-config" => Some(handle_set_storage_config_cli(&args[2..])),
        "--register-default-mcps" => Some(handle_register_default_mcps_cli(&args[2..])),
        _ => None,
    }
}

/// `vct-launcher --register-default-mcps <install_root>`
///
/// install.py invokes this to wire the canonical bundled-orchestrator MCP
/// entries into `~/.claude.json` AND (when a project row already exists)
/// the launcher.db. Pure stdout-only output — never opens a window.
///
/// Exit codes:
///   0 — at least one MCP registered (or zero MCPs requested, edge case)
///   1 — fatal error (no venv-python, JSON write failure, etc.) OR partial
///       success (install.py treats non-zero as a soft-fail and falls
///       through to the Python JSON path).
fn handle_register_default_mcps_cli(rest: &[String]) -> i32 {
    let install_root = match rest.first() {
        Some(p) if !p.starts_with("--") => std::path::PathBuf::from(p),
        _ => {
            eprintln!(
                "[vct] --register-default-mcps requires a path argument, e.g. \
                 vct-launcher --register-default-mcps /path/to/orchestrator/install"
            );
            return 1;
        }
    };
    cli_register_default_mcps(&install_root)
}

/// Run the `--register-default-mcps` flow synchronously and return the
/// process exit code. No GUI, no Tauri builder, no tokio runtime
/// (mcp_registration is fully sync).
fn cli_register_default_mcps(install_root: &std::path::Path) -> i32 {
    // Open the launcher DB best-effort; failure is non-fatal here (the
    // JSON write is the primary contract, DB sync is the bonus).
    let db_handle = db::Db::open().ok();

    // No services state available in the CLI path — install.py forwards
    // the chosen ports via env vars (WEAVIATE_PORT / OLLAMA_PORT /
    // CODE_EMBED_PORT / WEAVIATE_GRPC_PORT) the same way it does for
    // the launcher's `install_orchestrator()` invocation. We mirror
    // that lookup here so a multi-stack adoption stays consistent.
    let ports = mcp_registration::ServicePorts {
        weaviate_port: std::env::var("WEAVIATE_PORT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(mcp_registration::DEFAULT_WEAVIATE_PORT),
        ollama_port: std::env::var("OLLAMA_PORT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(mcp_registration::DEFAULT_OLLAMA_PORT),
        grpc_port: std::env::var("WEAVIATE_GRPC_PORT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(mcp_registration::DEFAULT_GRPC_PORT),
        code_embed_port: std::env::var("CODE_EMBED_PORT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(mcp_registration::DEFAULT_CODE_EMBED_PORT),
    };

    match mcp_registration::register_default_orchestrator_mcps(
        install_root,
        ports,
        None,
        db_handle.as_ref(),
    ) {
        Ok(report) => {
            println!(
                "[vct] MCP registration: wrote {} entr{} to {}",
                report.success_count(),
                if report.success_count() == 1 { "y" } else { "ies" },
                report.claude_json_path.display()
            );
            for o in &report.outcomes {
                if o.ok {
                    println!("[vct]   ok    {}", o.name);
                } else {
                    eprintln!(
                        "[vct]   FAIL  {} : {}",
                        o.name,
                        o.error.as_deref().unwrap_or("unknown error")
                    );
                }
                if !o.dropped_keys.is_empty() {
                    println!(
                        "[vct]         (dropped {} secret/non-allowlisted env key(s): {:?})",
                        o.dropped_keys.len(),
                        o.dropped_keys
                    );
                }
            }
            for w in &report.db_warnings {
                eprintln!("[vct] db warning: {}", w);
            }
            if report.all_succeeded() {
                0
            } else {
                1
            }
        }
        Err(e) => {
            eprintln!("[vct] register_default_orchestrator_mcps: {}", e);
            1
        }
    }
}

/// `vct-launcher --set-storage-config <named|bind|deferred>
///                  [--bind-path service=path]...`
///
/// Exit codes:
///   0 — config persisted (or `deferred` no-op).
///   1 — validation / I/O error (printed to stderr).
///   2 — usage error (missing mode arg).
fn handle_set_storage_config_cli(rest: &[String]) -> i32 {
    if rest.is_empty() {
        eprintln!(
            "usage: vct-launcher --set-storage-config <named|bind|deferred> \
             [--bind-path service=path]..."
        );
        return 2;
    }
    let mode = &rest[0];
    let mut bind_paths: Vec<(String, std::path::PathBuf)> = Vec::new();
    let mut i = 1;
    while i < rest.len() {
        if rest[i] == "--bind-path" && i + 1 < rest.len() {
            // Format: `service=/abs/path`. Anything malformed is logged
            // + skipped — the helper's normalizer will reject the call
            // if no valid path survives.
            if let Some((service, path)) = rest[i + 1].split_once('=') {
                let s = service.trim();
                let p = path.trim();
                if s.is_empty() || p.is_empty() {
                    eprintln!(
                        "[vct] warning: --bind-path arg {:?} has empty service or path; \
                         skipping",
                        rest[i + 1]
                    );
                } else {
                    bind_paths.push((s.to_string(), std::path::PathBuf::from(p)));
                }
            } else {
                eprintln!(
                    "[vct] warning: --bind-path arg {:?} missing `=`; expected \
                     `service=/abs/path` — skipping",
                    rest[i + 1]
                );
            }
            i += 2;
        } else if rest[i] == "--bind-path" {
            eprintln!("[vct] warning: --bind-path missing value; ignoring trailing flag");
            i += 1;
        } else {
            i += 1;
        }
    }
    match commands::storage_ux::set_storage_config_from_cli(mode, bind_paths) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("error: {}", e);
            1
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // v0.2.12 (unified PR-23 + PR-28 dispatch): CLI subcommand dispatch
    // BEFORE Tauri builder. Exits the process if a subcommand matched
    // (--register-default-mcps for MCP installation, --set-storage-config
    // for storage prompt persistence); falls through to the GUI startup
    // path when args[1] is absent or unrecognised.
    if let Some(exit_code) = handle_cli_args() {
        std::process::exit(exit_code);
    }

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

    // Load per-machine local config (env > vct-config.toml > compiled
    // defaults). Never fails — malformed/missing file falls through to
    // defaults so the launcher still boots and the operator can fix
    // the file via the GUI. See `config.rs` for the externalization
    // policy (what's IN scope vs. what stays compiled).
    let local_config = config::LocalConfig::load();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(app_manager)
        .manage(project_store)
        .manage(db_handle)
        .manage(local_config)
        .setup(|app| {
            // Windows-only: clean up the previous launcher's .old.exe
            // left behind by `apply_launcher_update`'s rename-then-build
            // workaround for the running-binary lock. Best effort —
            // silently swallow errors (file missing, still locked, etc).
            #[cfg(windows)]
            {
                if let Ok(exe) = std::env::current_exe() {
                    let old_path = exe.with_extension("old.exe");
                    let _ = std::fs::remove_file(&old_path);
                }
            }

            // P1-B (2026-05-08): one-shot migration of plaintext MCP-server
            // secret settings from `~/.vct/orchestrator.json` into the OS
            // keychain. Self-gated by an `app_state` flag — runs at most
            // once per install and skips silently when nothing needs
            // moving. Soft-fail: the launcher must boot even if the
            // keychain backend is unreachable (the migration just
            // postpones itself to the next start).
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    match commands::dashboard::migrate_plaintext_mcp_secrets_to_keychain(
                        db.inner(),
                    ) {
                        Ok(report) => {
                            if !report.migrated_keys.is_empty() {
                                eprintln!(
                                    "[vct] migrated {} MCP secret(s) to keychain: {:?}",
                                    report.migrated_keys.len(),
                                    report.migrated_keys,
                                );
                            }
                            if !report.skipped_keys.is_empty() {
                                eprintln!(
                                    "[vct] warning: {} MCP secret(s) could not be migrated \
                                     to the keychain (will retry on next boot): {:?}",
                                    report.skipped_keys.len(),
                                    report.skipped_keys,
                                );
                            }
                        }
                        Err(e) => {
                            eprintln!(
                                "[vct] warning: MCP secret migration failed: {}. \
                                 Will retry on next boot.",
                                e
                            );
                        }
                    }
                }
            }

            // Bug B (v0.2.5): seed the initial hardware snapshot on
            // first launcher boot. Soft-fails so a detection hiccup
            // (e.g. nvidia-smi not on PATH) never blocks boot. The
            // Preferences → Hardware "Re-detect" button overwrites this
            // later with a fresh snapshot.
            commands::installer::seed_initial_hardware_snapshot_if_missing(
                app.handle().clone(),
            );

            // Migration 010 follow-up (2026-05-10): backfill the
            // `project_mcp_servers` table for projects registered before
            // this migration shipped. For each existing project with zero
            // MCP rows, re-run `populate_project_state_from_filesystem`
            // to seed the `.claude/settings.json::mcpServers` +
            // `.mcp.json` mirror. The populate function is idempotent
            // and preserves user toggles — running it on already-seeded
            // projects is a no-op for non-MCP rows.
            //
            // Soft-fail: a single project's populate hiccup MUST NOT
            // block launcher boot. We log + continue.
            //
            // Cost: O(projects) on disk reads, capped at ~10ms per
            // project on healthy disks. Fine for a startup hook.
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    if let Ok(rows) = db.list_projects() {
                        let mut seeded = 0usize;
                        for proj in &rows {
                            // Only act on projects that haven't been
                            // populated yet (count==0). New projects
                            // created post-010 already have rows from
                            // create_project_v2's populate call.
                            let needs_seed = matches!(
                                db.count_project_mcp_servers(&proj.id),
                                Ok(0)
                            );
                            if !needs_seed {
                                continue;
                            }
                            let folder = std::path::Path::new(&proj.folder_path);
                            if !folder.is_dir() {
                                continue;
                            }
                            let report = crate::commands::project_state_populate::
                                populate_project_state_from_filesystem(
                                    &proj.id,
                                    &proj.name,
                                    folder,
                                    db.inner(),
                                );
                            if report.mcp_servers_inserted > 0 {
                                seeded += 1;
                            }
                            for w in &report.warnings {
                                eprintln!(
                                    "[vct] mcp-backfill warning ({}): {}",
                                    proj.id, w
                                );
                            }
                        }
                        if seeded > 0 {
                            eprintln!(
                                "[vct] mcp-backfill: seeded MCP servers for {} project(s) (migration 010)",
                                seeded
                            );
                        }
                    }
                }
            }

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

            // v0.2.6 (Bug D3): background watcher that polls services
            // every 30s and auto-restarts on running→stopped transitions.
            // Logs to <install>/state/logs/services-watcher.jsonl. User
            // can disable via Preferences → Services (writes the
            // `launcher.services_watcher_enabled` app_state row to
            // `false`); default is ENABLED. Soft-fail throughout: never
            // takes the launcher down.
            services::watcher::spawn(app.handle().clone());

            // Resume background tasks left behind by a previous launcher
            // process (crash, force-quit, OOM). Two-phase, soft-fail —
            // see `codegraph::resume_pending_builds` and
            // `kg_sync::resume_pending_syncs` for the contract:
            //   1. status='running' rows → marked failed with a clear
            //      "launcher crashed mid-run; click Retry to re-run"
            //      message. The GUI banner renders the failed state
            //      with a Retry button, so the user sees the broken
            //      lifecycle (silent re-spawn would mask the crash).
            //   2. status='pending' rows → re-spawned via the same
            //      mechanism as `create_project_v2`.
            //
            // Runs INSIDE setup() (not a spawned task) because:
            //   - both functions return after enqueuing tokio::spawn —
            //     no long-lived work blocks setup.
            //   - we want the sweep to land before the GUI mounts, so
            //     the user never sees a stale 'running' banner.
            //   - if a project's create_project_v2 was mid-flight when
            //     the launcher crashed, that row was 'pending' (the
            //     RUNNING transition is the first thing
            //     `run_build_task` / `run_sync_task` does after
            //     spawn) — so phase 2 above is what actually picks it up.
            let resume_handle = app.handle();
            let (cg_swept, cg_resumed) =
                commands::codegraph::resume_pending_builds(resume_handle);
            let (kg_swept, kg_resumed) =
                commands::kg_sync::resume_pending_syncs(resume_handle);
            // KG summary backfill (v0.2.3 / 2026-05-12) — extends the
            // resume sweep to a third task type. Same two-phase contract:
            //   (1) running rows → marked failed with "launcher crashed
            //       mid-run; click Retry to re-run".
            //   (2) pending rows → re-spawned via spawn_initial_summary.
            // See commands::kg_summary::resume_pending_summaries.
            let (sum_swept, sum_resumed) =
                commands::kg_summary::resume_pending_summaries(resume_handle);
            if cg_swept + cg_resumed + kg_swept + kg_resumed + sum_swept + sum_resumed > 0 {
                eprintln!(
                    "[vct] resume-sweep: code-graph (running→failed: {}, pending respawned: {}); \
                     kg-sync (running→failed: {}, pending respawned: {}); \
                     kg-summary (running→failed: {}, pending respawned: {})",
                    cg_swept, cg_resumed, kg_swept, kg_resumed, sum_swept, sum_resumed
                );
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
            // App-state key-value table — backs the Bug 14 fix that moved
            // `vct.onboarding_complete` and similar flags out of the
            // WebView's localStorage into launcher.db so VCT_STATE_DIR
            // isolation works. See db/app_state.rs + commands/app_state_cmd.rs.
            commands::app_state_cmd::app_state_get,
            commands::app_state_cmd::app_state_set,
            commands::app_state_cmd::app_state_get_bool,
            commands::app_state_cmd::app_state_set_bool,
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
            // PR-15 G2 (v0.2.11): zombie container recovery — Tauri
            // command driven by the "Recover" button next to a service
            // marked `zombie: true` in `services_status`.
            commands::lifecycle::recover_zombie,
            commands::lifecycle::service_start,
            commands::lifecycle::service_stop,
            commands::lifecycle::service_restart,
            commands::lifecycle::services_set_adoption,
            commands::lifecycle::services_get_adoption,
            commands::lifecycle::services_reset_adoption,
            commands::lifecycle::services_find_free_port,
            commands::lifecycle::services_enumerate_candidates,
            commands::lifecycle::services_pick_container,
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
            commands::projects_v2::update_project_v2,
            commands::projects_v2::rename_project_v2,
            commands::projects_v2::set_shared_kg_write_disabled,
            // Deprecated alias — delegates to set_shared_kg_write_disabled,
            // logs a deprecation warning. Slated for removal ~2026-08.
            commands::projects_v2::set_shared_kg_opt_out,
            // P1-D (2026-05-08): re-run env writers for a project so the
            // current state of the launcher's access matrix lands in the
            // 3 surfaces. Auto-invoked by access-matrix setters; FE may
            // also call directly after bulk edits.
            commands::projects_v2::refresh_project_env,
            commands::projects_v2::switch_project_host_v2,
            commands::projects_v2::delete_project_v2,
            commands::projects_v2::launch_project_in_editor,
            // Orchestrator-root view (v0.2.11, 2026-05-15) — exposes the
            // auto-registered `host=orchestrator_root` project row to
            // the UI so Settings / Dashboard can render a card for the
            // clone itself. Falls back to a synthetic view when the row
            // is absent (standalone binary, no clone findable on disk).
            commands::orchestrator_root::get_orchestrator_root_view,
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
            commands::modules::module_start_v2,
            commands::modules::module_stop_v2,
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
            // 0.2.x backlog #5: per-project MCP toggle UI.
            commands::project_state_cmd::list_project_mcp_permissions,
            commands::project_state_cmd::set_project_mcp_permission,
            commands::project_state_cmd::set_project_secret_ref,
            commands::project_state_cmd::delete_project_secret_ref,
            commands::project_state_cmd::set_project_kg_binding,
            commands::project_state_cmd::delete_project_kg_binding,
            commands::project_state_cmd::set_project_codegraph_binding,
            commands::project_state_cmd::delete_project_codegraph_binding,
            // Per-project MCP servers (migration 010 — Custom MCP tab feed).
            commands::project_state_cmd::list_project_mcp_servers,
            commands::project_state_cmd::list_user_added_project_mcp_servers,
            commands::project_state_cmd::set_project_mcp_server_enabled,
            commands::project_state_cmd::unregister_project_mcp_server,
            // PR-6 (v0.2.11): per-project .claude/env key reader+writer
            // (backs the HooksTab VCO_LEAN_CTX_DEFAULT toggle).
            commands::claude_env::get_claude_env_value,
            commands::claude_env::set_claude_env_value,
            // Secrets + settings
            commands::secrets_cmd::set_secret_v2,
            commands::secrets_cmd::clear_secret_v2,
            commands::secrets_cmd::reactivate_secret_v2,
            commands::secrets_cmd::remove_secret_v2,
            commands::secrets_cmd::is_secret_set,
            commands::secrets_cmd::get_secret_status_v2,
            commands::secrets_cmd::get_secret_preview,
            commands::secrets_cmd::get_setting_v2,
            commands::secrets_cmd::set_setting_v2,
            commands::secrets_cmd::list_module_settings_v2,
            // 0.2.1 grants & per-requester pause API
            commands::secrets_cmd::grant_secret,
            commands::secrets_cmd::revoke_secret_grant_cmd,
            commands::secrets_cmd::list_grants_for_project,
            commands::secrets_cmd::pause_secret_for_project,
            commands::secrets_cmd::resume_secret_for_project,
            // 0.2.x backlog #3: shared-tab key-collision shadow badge.
            commands::secrets_cmd::list_user_secret_keys_v2,
            // Bug H (v0.2.8 / Phase 5): register secrets by KEY only.
            // The launcher reads the value from the source itself; no
            // value ever crosses the IPC boundary. See
            // commands/secrets_import.rs for the value-handling rules.
            commands::secrets_import::list_importable_secret_keys,
            commands::secrets_import::register_secret_from_source,
            // 0.2.x backlog #4: Update-all sequential iteration.
            commands::projects_v2::update_all_projects,
            // Licensing
            commands::licensing::license_get_tier,
            commands::licensing::license_is_admin,
            commands::licensing::license_refresh,
            commands::licensing::license_activate,
            commands::licensing::license_deactivate,
            // KG dashboard
            commands::kg::kg_list_collections,
            commands::kg::codegraph_list_projects,
            // (kg_set_collection_access — the singular per-row setter — is
            //  not registered: the GUI uses kg_set_collection_access_mode
            //  exclusively, which is the higher-level mode-based API that
            //  internally calls db.kg_set_access. The Rust function stays
            //  in commands/kg.rs as the underlying primitive; dropped from
            //  the Tauri invoke surface 2026-05-09 to reduce attack surface.)
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
            // GPU/CDI drift check (Linux+NVIDIA only). Runs once at app
            // startup from +layout.svelte. Returns a tagged enum:
            // Ok | Drift{...} | NotApplicable{reason}. Never errors.
            commands::gpu::check_cdi_drift,
            commands::installer::get_installed_version,
            commands::installer::check_for_updates,
            commands::installer::install_orchestrator,
            commands::installer::preview_install,
            commands::installer::detect_existing_install_root,
            // Bug A (v0.2.5): path-agnostic install discovery. FE's
            // `checkStatus()` calls this BEFORE falling back to
            // `get_default_install_path`. See commands::installer for
            // the two-strategy contract.
            commands::installer::get_known_install_path,
            // Bug B (v0.2.5): re-detect hardware + apply reconfig
            // (Preferences → Hardware). See commands::installer for the
            // HardwareSnapshot / HardwareDetectionDiff / ReconfigReport
            // wire types.
            commands::installer::redetect_hardware,
            commands::installer::apply_hardware_reconfig,
            // Install health gate. Runs once at app startup from
            // `+layout.svelte` to detect the .exe-only install scenario
            // (user downloads launcher binary from a Release, skips
            // first-install.{bat,sh,command}). Returns `all_ok: true` for
            // dev builds running outside any install root.
            commands::installer::check_install_health,
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
            // PR-10A storage UX — separate surface from `volumes.rs`'s
            // install-time picker. Owns Settings -> Storage. STRICT
            // allowlist enforced in storage_ux::is_recognized_legacy_volume.
            commands::storage_ux::get_storage_config,
            commands::storage_ux::set_storage_config,
            commands::storage_ux::detect_legacy_volumes,
            commands::storage_ux::migrate_to_named_volume,
            commands::storage_ux::migrate_to_bind_path,
            commands::installer::update_orchestrator,
            commands::installer::get_local_repo_source,
            commands::installer::inspect_orchestrator_at,
            commands::installer::inspect_project_leftovers,
            commands::installer::update_orchestrator_at,
            // GitHub PAT lifecycle. `register_github_pat` is wired in the
            // OnboardingWizard (Bug 22) for first-run capture, AND in the
            // /preferences "GitHub access token" section for ongoing
            // status / replace / clear (added 2026-05-09 alongside the
            // non-destructive secrets fix).
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
            // KG auto-sync (2026-05-12): initial knowledge/ + docs/ sync
            // status + manual retry. Mirrors the codegraph Gap-2 pattern —
            // spawned from `create_project_v2` after bundle install so
            // pre-existing markdown nodes land in Weaviate without the
            // user having to manually run `.claude/scripts/kg-sync --all`.
            commands::kg_sync::get_kg_sync_status,
            commands::kg_sync::retry_kg_sync,
            // KG summary auto-backfill (v0.2.3 / 2026-05-12): initial
            // generate-kg-summary.py pass over knowledge/**/*.md. Mirrors
            // the kg-sync pattern — spawned from `create_project_v2`
            // alongside kg-sync so pre-existing markdown nodes get their
            // .node_formats.json sidecar entry without the user having to
            // edit each node in a Claude session for the PostToolUse hook
            // to backfill it lazily.
            commands::kg_summary::get_kg_summary_status,
            commands::kg_summary::retry_kg_summary,
            // PR-8 (v0.2.11 / 2026-05-15): per-project Identity tab +
            // legacy-collection cleanup. Surfaces `name`,
            // `KG_COLLECTION`, `CODE_GRAPH_PROJECT` editing and detects
            // pre-0.2.11 `ClaudeOrchestrator_*` Weaviate collections so
            // users hit by the PR-7 hardcoded-name bug can re-analyze
            // affected projects and (optionally, explicitly) clean up
            // the stale classes. See commands/project_identity.rs.
            commands::project_identity::get_project_identity,
            commands::project_identity::update_project_identity,
            commands::project_identity::redetect_project_identity,
            commands::project_identity::list_legacy_codegraph_collections,
            commands::project_identity::cleanup_legacy_codegraph_collections,
            commands::project_identity::get_legacy_codegraph_notice_dismissed,
            commands::project_identity::set_legacy_codegraph_notice_dismissed,
            // PR-26 / Group E (v0.2.12 / 2026-05-16): shared KG canonical-name
            // picker — surfaces orchestrator-shaped classes on Weaviate so the
            // IdentityTab can let users pick which class is canonical.
            commands::project_identity::list_orchestrator_kg_collections,
            commands::project_identity::set_shared_kg_collection_name,
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
            commands::self_update::force_resync_launcher,
            commands::self_update::get_user_owned_paths,
            commands::self_update::get_cached_update_status,
            commands::self_update::set_auto_check_enabled,
            commands::self_update::get_auto_check_enabled,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn load_projects_from_disk() -> HashMap<String, types::Project> {
    let path = paths::vct_root_dir().join("projects.json");

    if !path.exists() {
        return HashMap::new();
    }
    let data = std::fs::read_to_string(&path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}
