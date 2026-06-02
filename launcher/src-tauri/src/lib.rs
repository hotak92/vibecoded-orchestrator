// v0.2.21 Step 3e: shared modules moved to vct-launcher-core. The
// launcher continues to reference them via the original module paths
// (e.g. `crate::db::Db`) by means of `use ... as` re-exports below,
// so the rest of the launcher (1300+ LOC of lib.rs + every command
// module under commands/) compiles without per-file import rewrites.
//
// Launcher-only modules — these stay in this crate because they
// depend on Tauri runtime types (`AppHandle`, `Manager`, `State`,
// `Window`, `Emitter`) or on this crate's `commands::` Tauri-command
// surface.
mod commands;
mod hub_launcher;
mod hub_status;
mod installer_engine;
mod project_backfill;
mod mcp_registration;
pub mod project_naming;
mod quit_dialog;
mod tray;

// v0.2.26: WebKitGTK pre-flight probe — public so main.rs (which is a
// separate compilation unit from this lib) can call it before Tauri
// init. Linux-only at the use site; the module's own `#![cfg(...)]`
// gate makes the whole file invisible on macOS / Windows so there's
// nothing to expose there.
#[cfg(target_os = "linux")]
pub mod webkit_preflight;

// Shared modules — live in vct-launcher-core. Re-exported here so the
// existing `crate::db::Db` / `crate::manifest::*` / etc. usage across
// the launcher continues to resolve. Only the DEFINITION moved; the
// public surface is unchanged.
pub use vct_launcher_core::config;
pub use vct_launcher_core::db;
pub use vct_launcher_core::manifest;
pub use vct_launcher_core::paths;
pub use vct_launcher_core::registry;
pub use vct_launcher_core::secrets;
pub use vct_launcher_core::state;
pub use vct_launcher_core::types;

// `services::` is a HYBRID: `runtime` and `picker` live in core, while
// `adoption`, `settings_json_watcher`, and `watcher` stay in the
// launcher. The local `mod services;` declares this crate's submodule,
// which itself re-exports the core halves so `crate::services::runtime`
// + `crate::services::picker` still resolve from anywhere in the
// launcher.
mod services;

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

/// `vct-launcher --register-default-mcps <install_root> [--rewrite [--accept name1,name2,...]]`
///
/// install.py invokes this to wire the canonical bundled-orchestrator MCP
/// entries into `~/.claude.json` AND (when a project row already exists)
/// the launcher.db. Pure stdout-only output — never opens a window.
///
/// PR-33: when `--rewrite` is also passed, the call rewrites stale
/// `~/.claude.json mcpServers` entries that point outside the new
/// `install_root`. By default `--rewrite` is a SCAN-ONLY operation
/// (prints what would be rewritten) — actual writes require an explicit
/// `--accept <comma-separated-names>` list. The caller (install.py)
/// gathers per-entry consent FIRST, then passes only the accepted names
/// here. We do not re-prompt in this binary.
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
    // Parse optional --rewrite and --accept <names> from the remaining args.
    let mut rewrite_mode = false;
    let mut accept_names: Vec<String> = Vec::new();
    let mut i = 1; // skip the install_root positional we consumed above
    while i < rest.len() {
        match rest[i].as_str() {
            "--rewrite" => {
                rewrite_mode = true;
                i += 1;
            }
            "--accept" => {
                if i + 1 >= rest.len() {
                    eprintln!("[vct] --accept requires a comma-separated value");
                    return 1;
                }
                accept_names = rest[i + 1]
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect();
                i += 2;
            }
            other => {
                eprintln!("[vct] unknown arg to --register-default-mcps: {}", other);
                return 1;
            }
        }
    }
    if rewrite_mode {
        cli_rewrite_stale_mcps(&install_root, &accept_names)
    } else {
        cli_register_default_mcps(&install_root)
    }
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

/// PR-33: run the rewrite-stale-mcps flow synchronously and return the
/// process exit code. install.py invokes this AFTER gathering per-entry
/// consent on its side (see `_consent_for_stale_entries`). `accept_names`
/// is the user-approved subset; empty `accept_names` triggers a scan-only
/// dry-run that prints what WOULD be rewritten and exits 0.
fn cli_rewrite_stale_mcps(install_root: &std::path::Path, accept_names: &[String]) -> i32 {
    let db_handle = db::Db::open().ok();
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
    match mcp_registration::rewrite_stale_orchestrator_mcps(
        install_root,
        ports,
        None,
        db_handle.as_ref(),
        accept_names,
    ) {
        Ok(report) => {
            if report.stale_entries_found.is_empty() {
                println!("[vct] rewrite-stale-mcps: no stale entries found, nothing to do");
                return 0;
            }
            println!(
                "[vct] rewrite-stale-mcps: found {} stale entr{}",
                report.stale_entries_found.len(),
                if report.stale_entries_found.len() == 1 { "y" } else { "ies" }
            );
            for s in &report.stale_entries_found {
                println!("[vct]   stale {}: {}", s.name, s.stale_path);
                if !s.dropping_env_keys.is_empty() {
                    println!(
                        "[vct]         (rewrite would drop env keys: {:?})",
                        s.dropping_env_keys
                    );
                }
            }
            if accept_names.is_empty() {
                println!("[vct] rewrite-stale-mcps: scan-only mode (no --accept list); no writes");
                return 0;
            }
            if !report.rewritten.is_empty() {
                println!("[vct]   rewritten: {:?}", report.rewritten);
            }
            if !report.skipped_non_bundled.is_empty() {
                println!(
                    "[vct]   skipped (non-bundled, orchestrator owns weaviate-kg/search only): {:?}",
                    report.skipped_non_bundled
                );
            }
            if let Some(reg) = &report.registration {
                if !reg.all_succeeded() {
                    return 1;
                }
            }
            0
        }
        Err(e) => {
            eprintln!("[vct] rewrite_stale_orchestrator_mcps: {}", e);
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

    // v0.2.21 Step 3d: orchestrator-root auto-register moved out of
    // `Db::open()` because its implementation reaches into launcher-only
    // modules (commands::modules + commands::projects_v2) that cannot
    // move to vct-launcher-core without dragging Tauri deps along. It
    // runs here instead, in the launcher's setup path, where those
    // modules are freely accessible. Idempotent: no-op when the row
    // already exists OR no clone is detectable from disk. Soft-fail:
    // logs and continues — the row is a convenience, the rest of the
    // launcher works fine without it. The hub binary does NOT call this.
    if let Err(e) = commands::orchestrator_root::ensure_orchestrator_root(&db_handle) {
        eprintln!("[vct] warning: ensure_orchestrator_root failed: {}", e);
    }

    // v0.2.21 Step 19: launcher-startup project-row backfill. Sweep
    // every registered project and ensure the v0.2.21 resolver
    // endpoint's expected binding rows + module_settings exist.
    // Read-then-fill: NEVER overwrites user-set values. Backfill
    // also NEVER touches the access matrix (kg_collection_access /
    // codegraph_access) — those reflect user choices, default
    // "no access" via absence of a row. See plan §"Acceptance
    // criterion" property (2)/(3)/(4) for what we backfill and
    // §"Launcher startup backfill" for the discipline.
    {
        let report = project_backfill::backfill_all_projects(&db_handle);
        if report.touched_projects > 0 {
            eprintln!(
                "[vct] project-backfill: seeded missing binding/settings rows for {} project(s)",
                report.touched_projects
            );
        }
        for err in &report.errors {
            eprintln!("[vct] project-backfill warning: {}", err);
        }
    }

    // Load per-machine local config (env > vct-config.toml > compiled
    // defaults). Never fails — malformed/missing file falls through to
    // defaults so the launcher still boots and the operator can fix
    // the file via the GUI. See `config.rs` for the externalization
    // policy (what's IN scope vs. what stays compiled).
    let local_config = config::LocalConfig::load();

    tauri::Builder::default()
        // v0.2.21: per-user single-instance enforcement. Register BEFORE
        // any other plugins so the OS-level lock acquire happens before
        // we start tearing into state-dir setup. If this is the second
        // launcher process on the same user, the callback below fires in
        // the FIRST launcher and the second process exits cleanly without
        // ever creating a window or touching launcher.db.
        //
        // Callback semantics: invoked in the FIRST launcher's runtime when
        // another launch is attempted. Args are the second process's argv;
        // cwd is the second process's working directory. We don't use them
        // today — just focus our existing window so the user sees their
        // already-running launcher come to front. Future: route command-
        // line deep-links (vct://...) through here.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Bring the existing main window to front. If no window exists
            // (rare — possible if user closed the window but tray-icon
            // kept the process alive), show + focus the first available.
            use tauri::Manager;
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            } else if let Some((_, window)) = app.webview_windows().into_iter().next() {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
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

            // v0.2.17 (plan 0.0): cross-OS sweep of stale `<binary>.old-<pid>`
            // and `<binary>.pending-<pid>` siblings left behind by the
            // pre-pull-rename path in `update_orchestrator` (Windows path)
            // or a failed-to-revert pull on any OS. Per file:
            //   1. Parse the pid suffix.
            //   2. Check whether that PID is still alive.
            //   3. If dead, delete the file.
            // Bounded space cost: ≤1 per release in steady state.
            // Soft-fail throughout — sweep failure must NOT block boot.
            if let Ok(exe) = std::env::current_exe() {
                if let Some(dist_dir) = exe.parent() {
                    sweep_stale_binary_siblings(dist_dir);
                }
            }

            // v0.2.37 (Agent V37-E, 2026-05-27): consume the
            // install_path seed file that install.py may have written
            // alongside the orchestrator clone (see
            // `install.py::_seed_launcher_install_path` for the
            // companion writer). If `app_state['launcher.install_path']`
            // is unset AND a seed exists, write the seed value to
            // app_state and delete the seed file.
            //
            // Why this exists: the launcher's canonical
            // `resolve_orchestrator_root` resolver walks up from
            // `current_exe()` looking for orchestrator-root markers
            // (`vct-module.json` OR `install.py+CLAUDE.md`). That walk
            // FAILS when the launcher binary lives outside the clone
            // (e.g. PATH-installed wrapper at `~/bin/vct-launcher`
            // pointing at a clone in `~/dev/`). `ProjectEnvSettings::populate`
            // then returns `orchestrator_root=None` and
            // `VCT_ORCHESTRATOR_ROOT` is OMITTED from `.claude/env` —
            // exactly the bug that hit instambul_map / SD15 pre-v0.2.37.
            //
            // install.py knows the install path with certainty (it's
            // PROJECT_ROOT), so it records it out-of-band; this hook
            // promotes the recorded value into the DB on the next
            // launcher boot, where the resolver picks it up via its
            // Strategy 1 (DB cache) before ever needing the walk-up.
            //
            // SECOND ACTION on seed-consume: bulk-refresh every
            // project's `.claude/env` + `.claude/settings.json`. Existing
            // projects created BEFORE this release have stale env files
            // missing `VCT_ORCHESTRATOR_ROOT` (the pre-v0.2.37 uncached
            // resolver returned None silently). Now that the DB cache
            // is warm, re-running the project env writer picks up the
            // correct value and patches the surfaces without any user
            // action. We tie the bulk refresh to a successful seed
            // consume so this isn't a per-boot cost — only once per
            // install/update boundary.
            //
            // Soft-fail throughout: any error here MUST NOT block boot.
            // The walk-up resolver remains a working fallback for the
            // common case (binary inside the clone).
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    let consumed = consume_install_path_seed_if_present(db.inner());
                    if consumed {
                        let report = commands::projects_v2::
                            refresh_all_projects_env_with_db(db.inner());
                        if !report.refreshed.is_empty()
                            || !report.refreshed_with_warnings.is_empty()
                            || !report.failed.is_empty()
                        {
                            eprintln!(
                                "[vct] install-boundary env refresh: \
                                 refreshed={} with_warnings={} failed={} skipped={}",
                                report.refreshed.len(),
                                report.refreshed_with_warnings.len(),
                                report.failed.len(),
                                report.skipped.len(),
                            );
                            for (name, err) in &report.failed {
                                eprintln!(
                                    "[vct]   env refresh failed for {}: {}",
                                    name, err
                                );
                            }
                        }
                    }
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
            //
            // v0.2.34 (Agent B): this remains the FALLBACK path for the
            // genuinely-first-ever launcher boot, BEFORE any update cycle
            // has run. The post-update boundary trigger
            // (`consume_pending_hardware_redetect_if_set`, immediately
            // below) covers every subsequent launcher boot that follows
            // a self-update — together they make snapshot freshness an
            // invariant: a partial-schema row from an older launcher
            // version can never silently linger past one update cycle.
            // The install-time guard (`ensure_fresh_hardware_snapshot_for_install`
            // in `install_module_for_project`) is belt-and-suspenders for
            // manual binary swaps that bypassed the in-app update flow.
            commands::installer::seed_initial_hardware_snapshot_if_missing(
                app.handle().clone(),
            );

            // v0.2.34 (Agent C): launcher-version-change cache-bust for
            // the L0 module catalog. After an `Update orchestrator` the
            // running binary is a new version; the previous launcher's
            // cached L0 envelope may pre-date schema changes or new
            // module entries. Wipe `module_catalog.cache*` so the next
            // Modules-tab visit re-fetches. Same-version restarts are a
            // no-op (cache preserved → first-paint latency unchanged).
            // Soft-fails: a DB hiccup here MUST NOT block boot — the
            // worst case is "user clicks ↻ manually" or "cache survives
            // until its TTL".
            //
            // v0.2.45 V45-F: when bust_cache_if_launcher_version_changed
            // reports VersionChanged, ALSO proactively re-fetch the L0
            // catalog so V45-C's resolve_manifest_for_install (which
            // compares on-disk vs L0 version to pick the freshest source)
            // sees newly-published module versions immediately on the
            // first install/update attempt post-update. Without this,
            // the freshly-busted cache stays empty until something else
            // (UI mount, manual ↻) triggers a fetch — and any module-
            // install flow that fires in that window resolves against
            // L0Synth's empty-cache error path or a stale on-disk
            // manifest.
            //
            // The refresh is spawned non-blocking so launcher boot
            // latency is unaffected; same soft-fail discipline as the
            // bust call itself. cached_module_catalog is idempotent and
            // TTL-bounded (15-min TTL), so over-refresh is harmless if
            // a future change widens the spawn condition.
            //
            // Foundation for every future paid module (per user
            // directive 2026-06-02: "build a strong foundation for
            // every future paid module"). Periodic timer refresh
            // deferred to v0.2.46-46-4.
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    let bust_outcome =
                        commands::module_catalog_client::bust_cache_if_launcher_version_changed(
                            db.inner(),
                        );
                    if matches!(
                        bust_outcome,
                        commands::module_catalog_client::VersionBustOutcome::VersionChanged { .. }
                    ) {
                        let refresh_handle = app.handle().clone();
                        tauri::async_runtime::spawn(async move {
                            use tauri::Manager;
                            eprintln!(
                                "[v0.2.45 V45-F] refreshing L0 module catalog post launcher-version-change"
                            );
                            let db = refresh_handle.state::<crate::db::Db>();
                            match commands::module_catalog_client::cached_module_catalog(
                                db.inner(),
                            )
                            .await
                            {
                                Ok(catalog) => {
                                    eprintln!(
                                        "[v0.2.45 V45-F] L0 catalog refreshed (modules: {})",
                                        catalog.modules.len()
                                    );
                                }
                                Err(e) => {
                                    eprintln!(
                                        "[v0.2.45 V45-F] L0 catalog refresh failed (soft-fail): {}",
                                        e
                                    );
                                }
                            }
                        });
                    }
                }
            }

            // v0.2.45 V45-E: one-shot startup backfill of module_installs
            // rows stuck in the pre-v0.2.45 partial-failure state.
            //
            // Pre-v0.2.45, `start_container_after_install` failures called
            // `set_module_last_error` without flipping `status` — the row
            // sat in:
            //   status='installed' + last_error != NULL + container_name IS NULL
            // → invisible to V44-G4's `status IN ('error', 'broken')`
            // auto-retry predicate. The V45-E status-flip in
            // commands/modules.rs prevents NEW occurrences; this backfill
            // heals EXISTING rows from previous launcher versions so they
            // surface to V44-G4 on the next sweep.
            //
            // Idempotent (WHERE filters on the partial-failure shape, so
            // already-flipped rows are excluded). Non-destructive (UPDATE
            // only). Soft-fail (a DB hiccup MUST NOT block boot — the
            // manual "Reinstall" button remains the recovery path).
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    match db.backfill_partial_container_start_failures() {
                        Ok(0) => { /* clean slate; no rows to patch */ }
                        Ok(n) => {
                            eprintln!(
                                "[v0.2.45 V45-E] backfilled {} module_installs row(s) \
                                 from 'installed'+last_error partial-failure state \
                                 → 'error' (now visible to V44-G4 auto-retry)",
                                n
                            );
                            // Roll up a single audit entry rather than
                            // one-per-row — this is a system-wide
                            // migration step, not a per-(project,module)
                            // user action. `project_id`/`module_id` left
                            // None: the sweep's scope is orchestrator-wide.
                            let _ = db.audit(
                                "module_installs_partial_failure_backfill_v0245_v45_e",
                                None,
                                None,
                                &serde_json::json!({
                                    "rows_backfilled": n,
                                    "reason": "pre-v0.2.45 container-start-failure left status='installed' + last_error != NULL + container_name = NULL — invisible to V44-G4 status IN ('error','broken') auto-retry predicate",
                                    "remediation": "flipped status='installed' → 'error' so V44-G4 picks them up on the next sweep",
                                }),
                            );
                        }
                        Err(e) => {
                            eprintln!(
                                "[v0.2.45 V45-E] backfill SQL failed (soft-fail; \
                                 manual Reinstall remains the recovery path): {}",
                                e
                            );
                        }
                    }
                }
            }

            // v0.2.34 (Agent B): if the previous launcher process flagged
            // the next boot as "needs a fresh hardware redetect" (set by
            // `self_update::finish_apply_after_pull` right before
            // restart), consume the flag here and spawn a background
            // redetect job. Catches v0.2.20-style schema gaps where a
            // newly-shipped field needs to be backfilled on the user's
            // existing snapshot. Soft-fails — the manual Preferences
            // button remains the recovery path.
            commands::installer::consume_pending_hardware_redetect_if_set(
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

            // v0.2.27 (Wave 2 / agent-skill-keyword-suggest-and-fs-disable
            // plan): one-time migration sweep that moves any
            // `enabled=0` agent/skill file from `.claude/agents/`
            // (or `.claude/skills/`) into its sibling
            // `.claude/agents.disabled/` (or `.claude/skills.disabled/`)
            // directory. Pre-v0.2.27 the launcher's "disable" toggle
            // only flipped a DB flag — Claude Code still discovered the
            // file via its `.claude/agents/*.md` glob, so disabled
            // entries kept appearing in autocomplete and autonomous
            // invocation. The sibling-`.disabled/` layout takes them
            // out of Claude's discovery globs without deleting user data.
            //
            // Idempotent: `migrate_disabled_files_to_disabled_dir` is a
            // no-op for any row whose file is already in the right
            // location (counted as `already_disabled`). Safe to run on
            // every launcher boot; the steady-state cost is one bounded
            // SQLite query per registered project + zero filesystem
            // mutations.
            //
            // Soft-fail per project: one project's migration error
            // (missing folder, permission denied, partial filesystem)
            // MUST NOT block other projects' migrations or launcher
            // boot. We log + continue. Per-file errors are bundled
            // inside `MigrationReport.errors`; the sweep itself keeps
            // going across files.
            //
            // Cost: O(disabled_rows) per project on the DB side; one
            // `fs::rename` per file actually moved. After the first
            // boot post-update the work is amortised to zero (no rows
            // need moving). Bounded.
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    if let Ok(rows) = db.list_projects() {
                        let mut total_moved = 0usize;
                        for proj in &rows {
                            let folder = std::path::PathBuf::from(&proj.folder_path);
                            if !folder.is_dir() {
                                // Project row points at a folder that
                                // no longer exists on disk (deleted
                                // externally). Skip silently — the
                                // user will see the broken project in
                                // the GUI; not our problem to surface
                                // here.
                                continue;
                            }
                            match db.migrate_disabled_files_to_disabled_dir(
                                &proj.id, &folder,
                            ) {
                                Ok(report) => {
                                    total_moved += report.moved;
                                    // Only log when there's actually
                                    // something interesting — a clean
                                    // no-op (moved=0, no errors,
                                    // nothing in both_locations) is
                                    // the common steady state and
                                    // doesn't deserve log noise.
                                    if report.moved > 0
                                        || !report.errors.is_empty()
                                        || report.both_locations > 0
                                    {
                                        eprintln!(
                                            "[vct] migrate-disabled: \
                                             project={} moved={} \
                                             already_disabled={} \
                                             stale={} both={} errors={}",
                                            proj.name,
                                            report.moved,
                                            report.already_disabled,
                                            report.stale_rows,
                                            report.both_locations,
                                            report.errors.len(),
                                        );
                                        for err in &report.errors {
                                            eprintln!(
                                                "[vct]   migrate-disabled \
                                                 warning ({}): {}",
                                                proj.name, err,
                                            );
                                        }
                                    }
                                }
                                Err(e) => {
                                    eprintln!(
                                        "[vct] migrate-disabled: \
                                         project={} failed: {} \
                                         (continuing with next project)",
                                        proj.name, e,
                                    );
                                }
                            }
                        }
                        if total_moved > 0 {
                            eprintln!(
                                "[vct] migrate-disabled: total {} \
                                 agent/skill file(s) moved to \
                                 .disabled/ siblings across {} project(s)",
                                total_moved,
                                rows.len(),
                            );
                        }
                    }
                }
            }

            // NEW-12 (2026-05-28): init-time migration of legacy shared-KG
            // collection names in `kg_collection_access`.
            //
            // Pre-v0.2.12 installs may carry `VibeCodedTools_KnowledgeGraph`
            // rows; v0.2.12–v0.2.22 installs may carry the lowercase-c
            // `VibecodedOrchestrator_KnowledgeGraph`. Both are silently
            // rewritten to `VibeCodedOrchestrator_KnowledgeGraph` here.
            //
            // Dedup-then-rename: if BOTH legacy and canonical rows exist
            // for the same project_id, the legacy duplicate is deleted and
            // only the canonical row is kept. Idempotent — a second boot
            // with no legacy rows is a no-op. Soft-fail: a DB error is
            // logged and never blocks boot.
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    let canonical =
                        commands::project_env_settings::LAST_RESORT_SHARED_KG_COLLECTION;
                    match db.migrate_legacy_shared_kg_collection_names(canonical) {
                        Ok(0) => {}
                        Ok(n) => {
                            eprintln!(
                                "[vct] migrate-shared-kg: renamed {} \
                                 legacy row(s) → '{}'",
                                n, canonical
                            );
                            let _ = db.audit(
                                "kg_collection_access_legacy_migrated",
                                None,
                                None,
                                &serde_json::json!({
                                    "canonical": canonical,
                                    "renamed_count": n,
                                }),
                            );
                        }
                        Err(e) => {
                            eprintln!(
                                "[vct] migrate-shared-kg warning (non-fatal): {}",
                                e
                            );
                        }
                    }
                }
            }

            // W40-B (v0.2.40, 2026-05-30): cross-prefix KG binding
            // adoption + env regen-on-stale.
            //
            // Mirrors `install.py` W40-A's `_self_heal_kg_bindings_on_update`
            // cross-prefix extension at LAUNCHER BOOT, so users who
            // never re-run `install.py --update` still get healed on
            // the next launcher start.
            //
            // For each `project_kg_bindings` row whose `collection_name`
            // doesn't exist in Weaviate AND has no case-sibling
            // (NEW-12 / case-insensitive heal owns that path), probe
            // Weaviate for same-suffix classes (`*_KnowledgeGraph`)
            // with `row_count > 0`. Auto-adopt on a single populated
            // candidate; defer with a warning on multiple; no-op on
            // zero. Adopted rows are tagged
            // `manual_override=v0.2.40-prefix-adopt` in `config_json`
            // so the env-backfill path (`_align_env_with_db_bindings`
            // in `vco_lib/project_init.py`) trusts the new value on
            // the next `populate()`.
            //
            // After adoption: regenerate every project's env files
            // whose binding `updated_at` is newer than the env file's
            // mtime. Without this, the env files keep advertising the
            // pre-adoption name and the MCP subprocess still talks to
            // the missing collection.
            //
            // Position: AFTER NEW-12 so the canonical name is
            // finalized before we look at "still-missing" rows.
            //
            // Soft-fail throughout: Weaviate unreachable → log + skip
            // (boot continues; install.py --update or the next launch
            // will retry). Per-project env regen errors are logged
            // and never block other projects.
            //
            // Async needed because the function does HTTP probes;
            // wrap in `tauri::async_runtime::block_on` so the
            // synchronous setup closure isn't restructured.
            {
                use tauri::Manager;
                if let Some(db) = app.try_state::<db::Db>() {
                    // Weaviate URL: honour env override (matches the
                    // contract in the rest of the launcher), default
                    // to canonical localhost:8081.
                    let weaviate_url = std::env::var("WEAVIATE_URL")
                        .unwrap_or_else(|_| {
                            "http://localhost:8081".to_string()
                        });

                    // `block_on` keeps the &Db borrow valid throughout
                    // the call (the async fn doesn't hold the DB lock
                    // across .await boundaries — verified by the
                    // function's contract docstring).
                    let adopt_report =
                        tauri::async_runtime::block_on(
                            db.inner().adopt_populated_collections_at_boot(
                                &weaviate_url,
                            ),
                        );

                    match adopt_report {
                        Ok(report) => {
                            if report.adopted > 0 || report.deferred > 0 {
                                eprintln!(
                                    "[vct] adopt-populated: \
                                     adopted={} deferred={} no_change={}",
                                    report.adopted,
                                    report.deferred,
                                    report.no_change,
                                );
                                let _ = db.audit(
                                    "kg_binding_prefix_adopted_at_boot",
                                    None,
                                    None,
                                    &serde_json::json!({
                                        "adopted": report.adopted,
                                        "deferred": report.deferred,
                                        "no_change": report.no_change,
                                        "weaviate_url": weaviate_url,
                                    }),
                                );
                            }

                            // Env regen-on-stale: for each project
                            // whose binding updated_at is newer than
                            // the env file mtime, re-render env files.
                            // We trigger this regardless of whether
                            // THIS boot's adopt step rewrote any
                            // binding — a binding could have been
                            // touched by a prior boot, the GUI, or
                            // install.py --update without the env
                            // files being regenerated yet.
                            if let Ok(projects) = db.list_projects() {
                                let mut regened = 0usize;
                                for proj in &projects {
                                    let folder = std::path::Path::new(
                                        &proj.folder_path,
                                    );
                                    if !folder.is_dir() {
                                        continue;
                                    }
                                    let env_path =
                                        folder.join(".claude").join("env");
                                    if !commands::project_env_settings::
                                        should_regenerate_env_for_project(
                                            db.inner(),
                                            &proj.id,
                                            &env_path,
                                        )
                                    {
                                        continue;
                                    }
                                    match commands::projects_v2::
                                        refresh_project_env_with_db(
                                            db.inner(), &proj.id,
                                        )
                                    {
                                        Ok(_) => {
                                            regened += 1;
                                        }
                                        Err(e) => {
                                            eprintln!(
                                                "[vct] adopt-populated env \
                                                 regen failed for {}: {}",
                                                proj.name, e
                                            );
                                        }
                                    }
                                }
                                if regened > 0 {
                                    eprintln!(
                                        "[vct] adopt-populated: env \
                                         regenerated for {} project(s) \
                                         (binding newer than env file)",
                                        regened
                                    );
                                }
                            }
                        }
                        Err(e) => {
                            // Weaviate unreachable / schema fetch
                            // failed — the prefix-adopt step is best-
                            // effort; install.py --update is the
                            // canonical recovery path.
                            eprintln!(
                                "[vct] adopt-populated warning (non-fatal): {}",
                                e
                            );
                        }
                    }
                }
            }

            // v0.2.21 Step 6: bring up the detached vct-hub binary if
            // it isn't already running. `ensure_hub_running` is best-
            // effort — a missing binary or failed spawn drops the
            // launcher into "hub-unavailable degraded mode" (resolver
            // falls back to env vars; supervisor doesn't run) but
            // never blocks the GUI from coming up. Run in a blocking
            // task so its synchronous Command::status() doesn't stall
            // the Tauri runtime; `--start-if-not-running` itself
            // returns within ~100 ms (it spawns a detached child and
            // does NOT wait for the hub to bind), so this is a small
            // budget either way.
            tauri::async_runtime::spawn_blocking(|| {
                let _ = hub_launcher::ensure_hub_running();
            });

            // v0.2.33 (Agent C, architecture review §10.b): startup
            // reconciler. Walks every `module_installs` row with
            // status='installed' and verifies that the extracted
            // manifest at `~/.vct/modules/<id>/vct-module.json` is
            // present on disk. Rows missing their manifest get
            // flipped to status='broken' (CHECK extended by
            // migration 021) so the catalog tile can render
            // kind=`broken` + a Reinstall CTA.
            //
            // Runs AFTER `ensure_hub_running` was kicked (above) so
            // the hub's bearer-auth file is already being written
            // by the time we'd hit any hub-backed code paths — even
            // though the reconciler itself only reads launcher.db.
            // Runs BEFORE the GUI mounts so the user never sees a
            // stale "Installed" badge for a row whose on-disk
            // artifact is gone.
            //
            // Bounded soft-fail: per-row errors log + skip, top-
            // level DB errors return an empty report. Total runtime
            // is bounded by the number of installed module rows
            // (one fs::is_file check + one UPDATE per row), so it
            // stays under the 5 s startup budget even on slow disks.
            {
                use tauri::Manager;
                let db_ref = app.state::<crate::db::Db>();
                let report = commands::module_reconciler::reconcile_installed_modules(&db_ref);
                if !report.broken.is_empty() {
                    eprintln!(
                        "[vct] reconciler: {} module(s) marked broken (missing on-disk \
                         manifest): {:?}",
                        report.broken.len(),
                        report.broken,
                    );
                }
            }

            // v0.2.43 V0243-3: license_keys self-heal. If the table is empty
            // AND ~/.vct/license.key exists with a non-empty key (legacy file
            // written by pre-v0.2.40 installers), backfill a row and write the
            // key to the OS keychain. Soft-fail; must run before the GUI mounts
            // so the License Manager tile shows a sensible state on first boot.
            {
                use tauri::Manager;
                let db_ref = app.state::<crate::db::Db>();
                commands::licensing::backfill_license_key_from_legacy_file(&db_ref);
            }

            // System tray (v1.1)
            if let Err(e) = tray::setup(&app.handle()) {
                eprintln!("[vct] tray setup failed: {}", e);
            }
            // Daily launcher self-update check. Honors `auto_check_enabled`
            // toggle in ~/.vct/launcher-update-state.json (default ON).
            // Emits `vct-launcher-update-available` event when remote HEAD
            // has new commits — never auto-applies.
            commands::self_update::spawn_daily_check(app.handle().clone());

            // v0.2.18 Commit 3: OpenAI key startup recovery state machine.
            // Reads the keychain row at
            //   (Shared { project_id = SENTINEL_SHARED }, module_id = "user",
            //    key = "openai_api_key")
            // and validates it via the free `GET /v1/models/...` probe.
            // Drives the previously-valid-now-invalid → fallback-to-local
            // transition AND the previously-invalid-now-valid → restore
            // transition. Emits `vct-openai-key-invalidated` /
            // `vct-openai-key-restored` for the Preferences toast UI
            // (Commit 7). Soft-fails throughout — boot continues even on
            // keychain hiccups or network outages.
            //
            // Free-tier users (no key in keychain) hit the early-return at
            // the top of `run_openai_startup_recheck` — zero network cost,
            // zero state mutation.
            let openai_recheck_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = commands::openai_cmd::run_openai_startup_recheck(
                    openai_recheck_handle,
                )
                .await
                {
                    eprintln!("[vct] openai startup recheck warning (non-fatal): {}", e);
                }
            });
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

            // v0.2.21 Stream B: per-project container resume + daily
            // weights-update poll. Resume sweeps every install row with
            // a non-null container_name and restarts any whose
            // `is_container_running` probe returns false. The daily
            // poller hits /rl-latest-version every 24h per installed
            // RL reranker project; on `has_update=true` it emits
            // `module://weights-update-available` for Stream D's
            // WeightsUpdatePrompt component. Soft-fail throughout —
            // never blocks setup, never crashes the launcher.
            //
            // Step 24 Phase 2 (commit b) relocates the resume + poller
            // implementations into `vct-hub::module_supervisor`; the
            // launcher kicks the hub instead of running the supervisor
            // logic directly. See `commands::module_service` for the proxy
            // shape.
            let rl_resume_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                use tauri::Manager;
                let db = rl_resume_handle.state::<crate::db::Db>();
                crate::commands::module_service::resume_containers_on_startup(&db).await;
            });
            // Daily weights-update poll. license_reader closure returns
            // None when the user is free-tier (no key in keychain) —
            // the poller's inner loop short-circuits early in that
            // case. Step 24 commit b wired this to the real licensing
            // primitives (was `|| None` stub in commit a). The closure
            // is invoked once per daily sweep (24h cadence), so the
            // keychain read + MAC-hash compute is amortised to ~zero.
            crate::commands::module_service::spawn_daily_weights_poll(
                app.handle().clone(),
                || {
                    // L1.M (v0.2.40): canonical per-module username (was the
                    // legacy `VIBECODED_LICENSE_KEY`). Uses `keychain_username_for`
                    // helper as the single source of truth for the username
                    // shape — automatically picks up future changes.
                    let username =
                        vct_launcher_core::db::license_keys::keychain_username_for(
                            vct_launcher_core::db::license_keys::ORCHESTRATOR_MODULE_ID,
                        );
                    let key = match crate::secrets::get(
                        crate::secrets::SecretScope::Global,
                        "licensing",
                        &username,
                    ) {
                        Ok(Some(k)) if !k.trim().is_empty() => k,
                        _ => return None,
                    };
                    let hash = crate::commands::module_service::machine_id_hash_for_poll();
                    Some((key, hash))
                },
            );

            // v0.2.42 D1: daily deprecation-state poll. Fetches the L0
            // module catalog every 24h (30s boot delay) and applies the
            // catalog's `deprecated` + deprecation message fields for every
            // installed (project × module) pair via `module_update_poll`.
            // Writes last-poll timestamps to app_state. Soft-fail throughout.
            crate::commands::module_deprecation::spawn_deprecation_poll(
                app.handle().clone(),
            );

            // v0.2.6 (Bug D3): background watcher that polls services
            // every 30s and auto-restarts on running→stopped transitions.
            // Logs to <install>/state/logs/services-watcher.jsonl. User
            // can disable via Preferences → Services (writes the
            // `launcher.services_watcher_enabled` app_state row to
            // `false`); default is ENABLED. Soft-fail throughout: never
            // takes the launcher down.
            //
            // v0.2.21 W2 cutover guard: when install.py is mid-cutover
            // (writing `<vct_root_dir>/v0.2.21-cutover.flag` BEFORE it
            // starts vct-hub), skip the embedded watcher startup so
            // the old in-launcher watcher doesn't race the about-to-
            // run new install for the same containers. install.py
            // deletes the sentinel after vct-hub responds to /health
            // (typically <5 s).
            //
            // Step 25 (Reviewer B HIGH-doc fix, 2026-05-20): IMPORTANT
            // CORRECTION to the prior version of this comment. v0.2.21
            // does NOT relocate the SERVICES supervisor to vct-hub —
            // only Stream B's MODULE supervisor moved (Step 24, see
            // vct-hub/src/module_supervisor.rs). The services-watcher
            // logic still lives in `crate::services::watcher` here in
            // the launcher; vct-hub/src/lifecycle_api.rs:1-30 says so
            // explicitly. Therefore:
            //
            //   * Steady-state (sentinel deleted within ~1 s of /health
            //     response): no issue. Launcher's watcher runs as before.
            //   * install.py /health timeout (10 s) leaves the sentinel
            //     in place: the launcher skips watcher startup AND no
            //     hub-side services supervisor runs (it's a 501 stub
            //     until v0.2.22+). Services WILL NOT AUTO-RESTART until
            //     the user intervenes (delete the sentinel manually, or
            //     re-run install.py which overwrites it idempotently
            //     and deletes after success).
            //   * To bound that failure mode, we auto-delete the
            //     sentinel below if it's older than 60 seconds AND
            //     vct-hub is reachable (probed via the hub_launcher
            //     discovery chain) — at that point we know install.py
            //     long finished and the cutover is in steady state, so
            //     the sentinel is stale.
            //
            // The 60 s threshold is a compromise: long enough for a
            // genuine cutover (even a slow install.py /health probe
            // budget is 10 s; doubling to 60 s gives headroom for slow
            // disks / slow Weaviate boot under contention), short
            // enough that a user who manually killed install.py mid-
            // run doesn't lose services-watcher for hours.
            let sentinel_path = paths::vct_root_dir().join("v0.2.21-cutover.flag");
            let mut cutover_sentinel_present = sentinel_path.is_file();
            if cutover_sentinel_present {
                // Stale-sentinel auto-delete: if the sentinel is older
                // than 60 s AND vct-hub already running, install.py
                // either timed out or was killed mid-cutover. Delete
                // the sentinel and unset the flag so the watcher takes
                // over instead of leaving services unsupervised.
                let stale = std::fs::metadata(&sentinel_path)
                    .ok()
                    .and_then(|m| m.modified().ok())
                    .and_then(|m| m.elapsed().ok())
                    .map(|d| d.as_secs() >= 60)
                    .unwrap_or(false);
                let hub_reachable = matches!(
                    hub_status::probe(),
                    hub_status::HubStatus::Running { .. }
                );
                if stale && hub_reachable {
                    eprintln!(
                        "[vct] v0.2.21 stale cutover sentinel (older than 60s + \
                         hub already running); auto-deleting and starting \
                         embedded services-watcher"
                    );
                    let _ = std::fs::remove_file(&sentinel_path);
                    cutover_sentinel_present = false;
                }
            }
            if cutover_sentinel_present {
                eprintln!(
                    "[vct] v0.2.21 cutover sentinel detected at \
                     {}/v0.2.21-cutover.flag; skipping embedded \
                     services::watcher::spawn (vct-hub supervisor \
                     takes over).",
                    paths::vct_root_dir().display(),
                );
            } else {
                services::watcher::spawn(app.handle().clone());
            }

            // PR-42 (v0.2.12 / 2026-05-16): `.claude/settings.json`
            // watcher. When the user edits env in settings.json, this
            // debounces 500 ms then SIGHUPs every running orchestrator
            // MCP — they exit cleanly and Claude Code respawns them
            // with fresh env on the next request. Fixes Issue B from
            // the mcp-instability audit. POSIX-only (skipped on
            // Windows; the manual McpMaintenanceSection button stays
            // available as the cross-OS fallback).
            services::settings_json_watcher::spawn(app.handle().clone());

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
            // v0.2.35 Agent M (2026-05-26): GUI-level preflight that
            // gates `install_module_for_project` on a usable container
            // runtime being present right now. Distinct from the
            // boot-time `runtime_recheck` path (which only fires inside
            // the NoContainerRuntimeDialog modal triggered by the auto-
            // start event); this one runs on EVERY install click so the
            // gate is transactional, not boot-snapshot.
            commands::install_preflight::check_container_runtime_available,
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
            // v0.2.37 (Agent V37-E): bulk refresh for the install/update
            // boundary — re-renders `.claude/env` + `.claude/settings.json`
            // for every project so the canonical orchestrator-root
            // resolver's newly-warm DB cache propagates everywhere.
            commands::projects_v2::refresh_all_projects_env,
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
            // v0.2.33 Agent A (L0): force-refresh the paid-module catalog
            // from the public Supabase edge function. Bypasses the 15min
            // app_state cache used by `list_module_catalog`. Bound to the
            // Modules-tab `↻` refresh button. Cache-poisoning protection
            // (parse failures don't overwrite a previously-good cached
            // value) is implemented inside `module_catalog_client`.
            commands::module_catalog_client::refresh_module_catalog,
            // v0.2.33 Agent B (L0a, review §10.c): one-shot dismissal of
            // the dev-affordance hint. The catalog response includes
            // `dev_affordance_hint` exactly when (paid-modules/ exists)
            // AND (VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH unset) AND
            // (not dismissed). The renderer surfaces this as a toast;
            // clicking "Got it" calls this command.
            commands::modules::dismiss_dev_affordance_hint,
            // v0.2.33 Agent E (L9): append manifest-parse errors to
            // `<install>/state/logs/launcher_errors.jsonl` (or
            // `~/.vct/launcher_errors.jsonl` when no install root is
            // resolvable). The renderer calls this once per catalog
            // load whenever `parse_errors[]` is non-empty.
            commands::modules::log_manifest_parse_errors,
            commands::modules::install_module_for_project,
            // v0.2.31 #20-Fix-3: in-place update path (parallel to install).
            // Reads manifest.upgrade UpgradeBlock — runs pre_upgrade, re-fetches
            // the artifact, runs post_upgrade, optionally runs migration_script.
            commands::modules::update_module_for_project,
            commands::modules::uninstall_module_v2,
            commands::modules::list_installed_modules,
            commands::modules::module_status_v2,
            commands::modules::set_module_enabled_v2,
            commands::modules::module_start_v2,
            commands::modules::module_stop_v2,
            // v0.2.21 Stream B (2026-05-19): per-project RL container
            // lifecycle (Phase 1E) + weights-update polling (Phase 3C)
            // + fine-tune-after-download (Phase 4A) + dashboard widget
            // (Phase 4B scaffolding). Wired commands:
            commands::module_service::rl_is_container_running,
            commands::module_service::restart_rl_container,
            // NEW-3 (2026-05-28): generic start for service/container modules
            // whose container_name is NULL (auto-start was skipped).
            commands::module_service::start_module_container,
            commands::module_service::check_for_weights_update_now,
            commands::module_service::apply_weights_update,
            commands::module_service::get_rl_dashboard_state,
            // v0.2.32 (L7, Agent B): per-project text-embedding-source
            // resolver. Backs the ModuleConfigTab renderer's substitution
            // of `{{embedding_source_from_project_kg_binding}}` at dispatch
            // time. Reads `ACTIVE_EMBEDDING` from `.claude/env`, falls
            // back to `DEFAULT_EMBEDDING_SOURCE`.
            commands::module_service::get_project_embedding_source,
            // v0.2.44 V44-G4: orchestrator-update auto-retry toggle for
            // stuck paid-module installs (per RL-chat ask 2026-06-01).
            commands::module_service::get_auto_retry_failed_installs_setting,
            commands::module_service::set_auto_retry_failed_installs_setting,
            // v0.2.31 Agent J: module_weights_state Tauri commands removed
            // alongside migration 020. The dashboard's live reads now go
            // through `module_db_client::module_db_read_row` against the
            // container-owned `rl_weights_state` (shipped by vct-rl-
            // reranker v0.2.6 via its module-shipped migration).
            commands::module_db_client::module_db_read_row,
            // v0.2.32 #D: explicit "download default RL weights" command
            // + L6 runtime-value resolver for MultiSelectFilter.
            // Manifest buttons dispatch the first via `tauri_command`
            // action kind; the renderer calls the second when applying
            // `filter: {kind: "match", equals_runtime: "..."}` to a
            // multi_select's options list.
            commands::module_default_weights::module_download_default_weights,
            commands::module_default_weights::module_get_runtime_value,
            // v0.2.42 RT-4: reset the per-project bind-mount slot back to
            // the globally-downloaded default weights. Derives (source, version)
            // from module_settings written by the last successful download.
            // TODO (W6): bind "Reset to global defaults" button in module tile.
            commands::module_default_weights::module_reset_weights_to_global,
            // Stream 2 (2026-05-19): module-contributed GUI tabs +
            // generic per-control state. `get_module_nav_items` feeds the
            // Sidebar's module nav group; the get/set setting pair is the
            // schema renderer's default backing store.
            commands::module_gui::get_module_nav_items,
            commands::module_gui::get_module_setting,
            commands::module_gui::set_module_setting,
            // Stream 2 follow-up (v0.2.20, 2026-05-19): orchestrator-core
            // config-tab actions. Backs the controls declared in the
            // repo-root `vct-module.json::gui.config_tab` block.
            commands::orchestrator_core::kg_rebuild_current_project,
            commands::orchestrator_core::kg_check_duplicates,
            commands::orchestrator_core::code_graph_reanalyze_current,
            commands::orchestrator_core::code_graph_prune_stale,
            commands::orchestrator_core::orchestrator_health_check,
            commands::orchestrator_core::orchestrator_open_logs,
            // v0.2.24.1 (A0bis): "Clone integrity" tab — root-clone-only
            // affordances. Re-detect orchestrator root + Validate clone
            // manifest are the 2 features that don't naturally live in
            // per-project tabs or global preferences.
            commands::orchestrator_core::redetect_orchestrator_root,
            commands::orchestrator_core::validate_clone_manifest,
            // RL Reranker per-project settings (Stream 2 / 2026-05-19).
            // Backs the schema-rendered controls declared in
            // paid-modules/vct-rl-reranker/vct-module.json's
            // gui.config_tab. The four reset/retrain stubs that
            // previously lived here (rl_reset_to_global,
            // rl_reset_and_specialize, retrain_global_online,
            // retrain_global_offline) were removed in v0.2.26 —
            // the RL manifest migrates to ActionDescriptor::Http
            // entries dispatched via module_dispatch_action below.
            commands::rl_settings::set_rl_use_global,
            commands::rl_settings::set_rl_online_training_disabled,
            commands::rl_settings::set_rl_global_training_source_flag,
            // v0.2.40 H2: getter counterparts to the three setters above.
            // Back the `RlRerankerDashboardWidget` rewire — the widget
            // surfaces the persisted flags as a compact status panel in
            // the RL module's config tab.
            commands::rl_settings::get_rl_use_global,
            commands::rl_settings::get_rl_online_training_disabled,
            commands::rl_settings::get_rl_global_training_source_flag,
            commands::rl_settings::list_rl_global_training_source_projects,
            // v0.2.26 (2026-05-22): generic declarative HTTP-action
            // dispatcher. Single Tauri command that executes any
            // `ActionDescriptor::Http` descriptor declared in a
            // manifest's `gui.config_tab` — no per-module Rust code
            // required. See `commands/module_dispatch.rs` for the
            // wire contract + trust surface notes.
            commands::module_dispatch::module_dispatch_action,
            // v0.2.31: module-deprecation warning surface. Three layers
            // (GUI badge, env-var injection, audit table). The poller
            // for `runtime.update_endpoint` itself is deferred to v0.2.32 —
            // only the manual apply / seen / mark-seen Tauri entries are
            // wired here. See `commands/module_deprecation.rs`.
            commands::module_deprecation::apply_deprecation_state,
            commands::module_deprecation::has_module_deprecation_been_seen,
            commands::module_deprecation::mark_module_deprecation_seen,
            // v0.2.31: module-shipped DB migrations. Manual-repair
            // surface (re-apply on the dashboard's "Repair module DB"
            // button) + per-(module, project) access-token issue for
            // hub bearer auth. The install-time apply runs from
            // installer_engine's run_install / run_upgrade — these
            // commands are for the GUI / dashboard manual paths.
            commands::module_db::apply_module_db_migrations,
            commands::module_db::issue_module_access_token,
            // Retrieval tuning (v0.2.22 Item #13 — 2026-05-20).
            // Global thresholds for score-driven retrieval verbosity
            // (KG tier cutoffs) + codegraph injection floor. Backed by
            // <vct_root_dir>/retrieval-tuning.toml. The hub's config_api
            // reads the same file so headless / script clients see the
            // values the GUI shows.
            commands::retrieval_tuning::retrieval_tuning_get,
            commands::retrieval_tuning::retrieval_tuning_set,
            commands::retrieval_tuning::retrieval_tuning_reset,
            // Per-project orchestrator state (agents/skills/hooks/permissions/secrets/KG/codegraph)
            commands::project_state_cmd::list_project_agents,
            commands::project_state_cmd::list_project_skills,
            commands::project_state_cmd::list_project_hooks,
            commands::project_state_cmd::list_project_permissions,
            commands::project_state_cmd::list_project_secret_refs,
            commands::project_state_cmd::get_project_state_snapshot,
            commands::project_state_cmd::rescan_project_from_filesystem,
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
            // Phase 1.1 — Diagrams (Mermaid + Excalidraw) registry,
            // snapshots, cross-project access grants, per-tool MCP
            // allowlists, and per-project module-active flags. Schema:
            // migration 021. Sibling agents (1.2 wrapper MCP, 1.3
            // DiagramsTab, 1.5 indexer) consume these EXACT command
            // names; renaming would break their stubs at merge time.
            commands::diagrams_cmd::list_project_diagrams,
            commands::diagrams_cmd::register_project_diagram,
            commands::diagrams_cmd::unregister_project_diagram,
            commands::diagrams_cmd::set_project_diagram_enabled,
            commands::diagrams_cmd::list_diagram_snapshots,
            commands::diagrams_cmd::create_diagram_snapshot,
            commands::diagrams_cmd::restore_diagram_snapshot,
            commands::diagrams_cmd::delete_diagram_snapshot,
            commands::diagrams_cmd::diagram_grant_access,
            commands::diagrams_cmd::list_diagram_access,
            commands::diagrams_cmd::set_project_mcp_tool_enabled,
            commands::diagrams_cmd::list_project_mcp_tools,
            // v0.2.34 Agent E (Phase 4 generalisation, 2026-05-25):
            // PermissionsTab's "Customize" button populates the
            // per-tool allowlist from manifest-shipped defaults (or
            // the hardcoded fallback). Generalised so any MCP — not
            // just diagrams — gets the same surface.
            commands::diagrams_cmd::seed_project_mcp_tool_grants,
            commands::diagrams_cmd::set_project_module_enabled,
            commands::diagrams_cmd::list_project_modules,
            // Phase 1.5.7 wire-up: DiagramsTab calls
            // `is_project_module_active` on mount to decide whether to
            // render the diagrams UI or the "module disabled" overlay.
            // Missing → tab silently treats every project as inactive.
            commands::diagrams_cmd::is_project_module_active,
            // v0.2.34 Agent D — four missing commands the DiagramsTab +
            // ExcalidrawEditor invoke for their core load/save/open/
            // live-push paths. Pre-v0.2.34 each call threw
            // "command not found" and the frontend silently degraded
            // (empty preview, 5s polling fallback). See
            // `.claude/context/plans/diagrams-frontend-wiring-handoff-2026-05-25.md`.
            commands::diagrams_cmd::read_project_diagram_source,
            commands::diagrams_cmd::write_text_file,
            commands::diagrams_cmd::resolve_project_path,
            commands::diagram_watcher::subscribe_to_diagram_changes,
            // v0.2.36 Agent R — opens a vendored Mermaid/Excalidraw
            // editor in the user's default browser via the launcher's
            // local diagrams-editor HTTP server (lazy-started). Replaces
            // the embedded Excalidraw editor (broken on Wayland +
            // webkit2gtk) and ships a self-hosted visual Mermaid editor
            // alongside the existing text-only one.
            commands::diagrams_cmd::open_diagrams_editor,
            // PR-6 (v0.2.11): per-project .claude/env key reader+writer
            // (backs the HooksTab VCO_LEAN_CTX_DEFAULT toggle).
            commands::claude_env::get_claude_env_value,
            commands::claude_env::set_claude_env_value,
            // C8 wire-up (2026-05-25): read-only process env lookup, with
            // a credential-name blocklist. DiagramsTab calls this for the
            // Wayland-fallback decision (XDG_SESSION_TYPE).
            commands::env_cmd::read_env_var,
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
            // v0.2.36: machine-id hash for the admin-rebind UX. Pure
            // read-only command; same value `license_refresh` sends
            // to `/validate-tier`.
            commands::licensing::get_machine_id_hash,
            // v0.2.36: orchestrate the admin-token machine rebind from
            // Rust so the license key never crosses the IPC boundary.
            commands::licensing::license_rebind_admin_token,
            // v0.2.32 §D1: per-module license rows for the dialog.
            commands::licensing::get_module_licenses,
            commands::licensing::module_license_refresh,
            commands::licensing::module_license_deactivate,
            // v0.2.40 L1: multi-key licensing model — per-paid-module
            // license keys. Each paid module owns its own row keyed by
            // module_id; the reserved '__orchestrator__' slot covers the
            // legacy single-key root tier. The legacy
            // license_activate/license_deactivate path stays in place
            // for the orchestrator-tier ActivationModal; new per-module
            // UX flows through the License Manager modal go through
            // these commands.
            commands::licensing::list_license_keys,
            commands::licensing::get_module_license_key_status,
            commands::licensing::set_module_license_key,
            commands::licensing::clear_module_license_key,
            commands::licensing::validate_module_license,
            commands::licensing::list_module_license_validations,
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
            commands::telemetry_cmd::telemetry_clear_rl_local_cache,
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
            // v0.2.34 (Agent I): read-only resolver for the launcher's
            // state-root directory. Backs the Preferences → Storage
            // discoverability surface (renders the resolved path +
            // tooltip explaining VCT_STATE_DIR override).
            commands::storage_ux::get_resolved_vct_root_dir,
            commands::installer::update_orchestrator,
            // v0.2.23 (B4 / D19): divergence-recovery commands for
            // update_orchestrator. When the user's local clone has
            // diverged from upstream (typical: local edits to
            // CLAUDE.md, CONTEXT_STATE.md, KG nodes), `git pull
            // --ff-only` fails. update_orchestrator surfaces a
            // structured "orchestrator_update_non_ff" error; the
            // frontend then offers Merge / Rebase / Cancel. These
            // three commands are the resolvers.
            commands::installer::merge_orchestrator_with_upstream,
            commands::installer::rebase_orchestrator_onto_upstream,
            commands::installer::abort_orchestrator_merge_or_rebase,
            // v0.2.16 (W4 / 0.5): apply_pending_install resolves the
            // "Pulled-but-not-installed" banner state (source updated
            // via `git pull` outside the launcher; install-manifest
            // still records the previous version). Runs install.py
            // --update WITHOUT a preceding git pull. Distinct from
            // update_orchestrator (git pull + install) so we don't
            // waste ~30s pulling an already-current source tree.
            commands::installer::apply_pending_install,
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
            // OpenAI key lifecycle (v0.2.18, Commit 3). Symmetric to the
            // github_pat trio above: register / validate / recheck. Wired
            // into OnboardingWizard's OpenAI step (Commit 6) AND the
            // /preferences "OpenAI API key" section (Commit 7). The
            // startup background task in setup() below runs the recovery
            // state machine at every launcher boot.
            commands::openai_cmd::register_openai_api_key,
            commands::openai_cmd::validate_openai_api_key,
            commands::openai_cmd::recheck_openai_validity,
            // Preferences-row helpers (v0.2.18, Commit 7). Mirror the
            // github_pat trio in `commands::installer`: presence check +
            // masked preview + idempotent clear. The Preferences page
            // uses these to pre-fill the OpenAI key row with a masked
            // placeholder so the user can re-check or clear without
            // re-typing.
            commands::openai_cmd::has_openai_api_key,
            commands::openai_cmd::get_openai_api_key_preview,
            commands::openai_cmd::clear_openai_api_key,
            // Embedding catalog (v0.2.18, Commit 8). Shells out to
            // `python -m vco_lib.embedding_service discover` to enumerate
            // reachable models, then surfaces those + the project's
            // current slot bindings to the Svelte side so KG/Codegraph +
            // Preferences dropdowns can replace the legacy free-text
            // model input. Cached in-process for ~30s so rapid catalog
            // re-renders during a route change don't queue subprocess
            // spawns. See commands/embedding_catalog.rs.
            commands::embedding_catalog::get_embedding_catalog,
            commands::embedding_catalog::set_default_embedding_models,
            commands::embedding_catalog::get_default_embedding_models,
            commands::embedding_catalog::validate_model_against_catalog,
            // Embedding enrichment migration (v0.2.18, Commit 9). Spawns
            // `python -m vco_lib.embedding_enrichment enrich` with
            // --stream-progress and re-emits per-batch progress as
            // `vct-enrichment-progress` Tauri events for the
            // KgCodegraphTab progress modal. Idempotent: re-running on
            // an already-enriched collection produces 0 enriched + the
            // full skipped count. Never deletes existing slot data.
            commands::embedding_enrichment::enrich_collection_vectors,
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
            // Codegraph — v0.2.18 Plan C: Re-analyze command. Spawns
            // analyze_code_graph.py with --prune-stale + --json-progress
            // and streams per-file events on `vct-reanalysis-progress`
            // for the Svelte CodeGraphReanalysisModal. Always passes
            // --prune-stale (authoritative refresh); --language is
            // optional and scopes the re-walk to one language.
            commands::codegraph_reanalyze::reanalyze_code_graph,
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
            commands::project_identity::cleanup_orphan_codegraph_collections,
            commands::project_identity::get_legacy_codegraph_notice_dismissed,
            commands::project_identity::set_legacy_codegraph_notice_dismissed,
            // W3 / v0.2.16 (2026-05-18): wizard UX hardening (plan 0.3 + 0.9).
            // The status batched-read feeds the wizard's poll loop so it
            // shows real per-project progress instead of being stuck at
            // "Started for N/N project(s)". The force-recheck command
            // backs the Preferences "Re-check for legacy collections"
            // button.
            commands::project_identity::get_code_graph_build_status_for_projects,
            commands::project_identity::force_recheck_legacy_codegraph,
            // PR-26 / Group E (v0.2.12 / 2026-05-16): shared KG canonical-name
            // picker — surfaces orchestrator-shaped classes on Weaviate so the
            // IdentityTab can let users pick which class is canonical.
            commands::project_identity::list_orchestrator_kg_collections,
            commands::project_identity::set_shared_kg_collection_name,
            // PR-37 (v0.2.12 / 2026-05-16): GUI-surfaced maintenance ops.
            // Backs `McpMaintenanceSection.svelte` (MCP page) and
            // `ServicesSchemaSection.svelte` (Services page) — closes the
            // GUI coverage gap left by PR-23, PR-24 and PR-33 which were
            // CLI-only.
            commands::maintenance::mcp_registration_status,
            commands::maintenance::rerun_mcp_registration,
            commands::maintenance::schema_migration_status,
            commands::maintenance::issue_schema_migration_consent_token,
            commands::maintenance::run_schema_migrations,
            commands::maintenance::stale_mcp_entries,
            commands::maintenance::rewrite_stale_mcp_entries,
            // PR-42 (v0.2.12 / 2026-05-16): SIGHUP-driven MCP env reload.
            // Fixes Issue B from the mcp-instability audit — editing
            // `.claude/settings.json env` mid-chat now triggers a clean
            // MCP exit so Claude Code respawns with fresh env on the next
            // request. The launcher's settings.json watcher fires this
            // automatically; the GUI button in McpMaintenanceSection
            // gives users a manual override.
            commands::maintenance::reload_mcps_sighup,
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
            // v0.2.35 (Agent K): running-version display + post-update
            // binary-lag warning. After "Update orchestrator" pulls the
            // source tag for v0.X.Y but the matching `chore(binary):`
            // commit hasn't landed yet from CI, the restarted launcher
            // is the PREVIOUS release's binary. Surfaces a dismissible
            // banner so the user knows to click Update again in
            // 5-10 minutes. See `running_version_lags_tag` in
            // self_update.rs for the comparison rules.
            commands::self_update::get_launcher_running_version,
            commands::self_update::get_latest_source_release_tag,
            commands::self_update::check_running_version_lags_tag,
            // v0.2.15 (Agent D): launcher self-restart after binary swap.
            // Invoked by the green "Restart now" banner the FE renders for
            // `launcher_restart_required` deferral entries emitted by
            // install.py's _refresh_dist_binary_after_rebuild.
            commands::restart::restart_launcher,
            commands::restart::get_launcher_restart_status,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

// v0.2.21 Step 5: `pid_is_alive` moved to `vct-launcher-core::process`
// so the detached vct-hub binary can reuse it for its own lockfile
// state machine. Re-exported here so launcher call sites (the binary-
// sibling sweep at line ~1300, plus the test module below) continue
// to resolve via the same `pid_is_alive` symbol.
pub(crate) use vct_launcher_core::process::pid_is_alive;

/// v0.2.17 (plan 0.0): sweep stale `<binary>.old-<pid>` and
/// `<binary>.pending-<pid>` siblings from the launcher dist
/// directory. These are left behind by the pre-pull-rename path in
/// `update_orchestrator` (Windows) or a failed-revert path on any
/// OS. The PID suffix is parsed; files whose PID is no longer alive
/// are deleted. Files with malformed names, unparseable PIDs, or
/// alive PIDs are skipped.
///
/// Soft-fail throughout — sweep MUST NOT block launcher boot.
fn sweep_stale_binary_siblings(dist_dir: &std::path::Path) {
    let entries = match std::fs::read_dir(dist_dir) {
        Ok(e) => e,
        Err(_) => return, // dist_dir missing or unreadable — skip silently
    };

    for entry in entries.flatten() {
        let path = entry.path();
        let fname = match path.file_name().and_then(|s| s.to_str()) {
            Some(s) => s,
            None => continue,
        };
        // Match `*.old-<digits>` or `*.pending-<digits>`. The split is
        // on the LAST dot before the suffix so files like
        // `vct-launcher.exe.old-1234` parse correctly.
        let pid_str = if let Some((_, p)) = fname.rsplit_once(".old-") {
            p
        } else if let Some((_, p)) = fname.rsplit_once(".pending-") {
            p
        } else {
            continue;
        };
        let pid: u32 = match pid_str.parse() {
            Ok(n) => n,
            Err(_) => continue, // not a PID-suffixed file, leave alone
        };
        if pid_is_alive(pid) {
            continue;
        }
        if let Err(e) = std::fs::remove_file(&path) {
            eprintln!(
                "[vct] boot sweep: could not delete stale sibling {}: {} (will retry next boot)",
                path.display(),
                e,
            );
        } else {
            eprintln!(
                "[vct] boot sweep: removed stale {} (pid {} no longer alive)",
                path.display(),
                pid,
            );
        }
    }
}

/// v0.2.37 (Agent V37-E, 2026-05-27): seed-file consumer for
/// `app_state['launcher.install_path']`. Companion to
/// `install.py::_seed_launcher_install_path`.
///
/// Behaviour:
///   1. If `app_state['launcher.install_path']` is already set AND
///      passes `check_install_status`, do nothing. The cache is warm;
///      the seed (if any) is stale.
///   2. If unset OR stale, look for
///      `<exe-parent-walk-up>/.vct/install_path_seed.txt`. The walk
///      uses the same algorithm as `walk_for_orchestrator_root` so
///      this hook works whether the launcher binary is inside the
///      clone or in a sibling `dist/` folder.
///   3. If the seed file exists and points at a directory that passes
///      `check_install_status`, write the value to app_state and
///      delete the seed file.
///
/// Returns `true` iff the install_path was just promoted from a seed
/// file into `app_state` (signals "fresh install/update boundary" to
/// the caller — the boot hook uses this to gate the per-project env
/// refresh). Returns `false` when the DB cache was already warm OR
/// when no seed was found OR when the seed was invalid.
///
/// Soft-fail throughout: any error MUST NOT block launcher boot. The
/// `resolve_orchestrator_root` walk-up resolver remains the fallback.
///
/// Idempotent: deleting the seed after consumption prevents a stale
/// seed from overriding a user's manual GUI choice on subsequent boots.
fn consume_install_path_seed_if_present(db: &db::Db) -> bool {
    use commands::installer::APP_STATE_KEY_INSTALL_PATH;

    // Step 1: is app_state already warm with a VALID install path?
    if let Ok(Some(cached)) = db.app_state_get(APP_STATE_KEY_INSTALL_PATH) {
        if !cached.is_empty()
            && commands::installer::check_install_status(cached.clone())
        {
            // DB cache is warm + valid. If a stale seed exists, clean
            // it up so the next install.py write isn't shadowed by
            // an out-of-date one — but only if it disagrees with the
            // cache (matching seeds are a no-op).
            return false;
        }
    }

    // Step 2: locate a seed file. The seed lives at
    // `<install_root>/.vct/install_path_seed.txt`. We walk up from
    // current_exe() the same way the canonical resolver does, but
    // looking for the seed file instead of the orchestrator markers.
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(_) => return false,
    };
    let start = match exe.parent() {
        Some(p) => p.to_path_buf(),
        None => return false,
    };
    let seed_path = match locate_install_path_seed(&start) {
        Some(p) => p,
        None => return false,
    };

    process_install_path_seed(db, &seed_path)
}

/// v0.2.37 (Agent V37-E): pure helper — walks up from `start` looking
/// for `<level>/.vct/install_path_seed.txt`. Returns the first match
/// (deepest-first), or None. Bounded to 8 ancestor levels to mirror
/// `walk_for_orchestrator_root`.
///
/// Factored out of `consume_install_path_seed_if_present` so unit
/// tests can drive it deterministically without depending on
/// `current_exe()`'s real location.
fn locate_install_path_seed(start: &std::path::Path) -> Option<std::path::PathBuf> {
    let mut current = start.to_path_buf();
    for _ in 0..8 {
        let candidate = current.join(".vct").join("install_path_seed.txt");
        if candidate.is_file() {
            return Some(candidate);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// v0.2.37 (Agent V37-E): pure helper — read seed file, validate via
/// `check_install_status`, promote to `app_state['launcher.install_path']`,
/// delete on success. Soft-fail throughout: returns `true` iff the
/// app_state was just updated (i.e. install/update boundary signal),
/// `false` otherwise.
///
/// Factored out so unit tests can drive the read+validate+promote
/// path with arbitrary seed paths.
fn process_install_path_seed(db: &db::Db, seed_path: &std::path::Path) -> bool {
    use commands::installer::APP_STATE_KEY_INSTALL_PATH;

    let raw = match std::fs::read_to_string(seed_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "[vct] install-path seed at {} unreadable: {} \
                 (falling back to walk-up resolver)",
                seed_path.display(),
                e
            );
            return false;
        }
    };
    let install_path = raw.trim().to_string();
    if install_path.is_empty() {
        // Empty seed — drop it so we don't keep re-reading it.
        let _ = std::fs::remove_file(seed_path);
        return false;
    }
    if !commands::installer::check_install_status(install_path.clone()) {
        eprintln!(
            "[vct] install-path seed at {} points at {} which does \
             not pass check_install_status — leaving DB unchanged. \
             The seed file will be retried on the next launcher boot.",
            seed_path.display(),
            install_path,
        );
        // Don't delete: the install may be in progress / on a
        // momentarily-unmounted drive. install.py rewrites the seed
        // anyway on its next run.
        return false;
    }

    // Promote: write the seed value into app_state.
    if let Err(e) = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, &install_path) {
        eprintln!(
            "[vct] install-path seed: could not write {} to app_state: {} \
             (seed file left in place; will retry next boot)",
            install_path, e,
        );
        return false;
    }
    eprintln!(
        "[vct] install-path seed: promoted {} to app_state from {}",
        install_path,
        seed_path.display(),
    );

    // Delete the seed file so it doesn't shadow a future GUI override.
    if let Err(e) = std::fs::remove_file(seed_path) {
        eprintln!(
            "[vct] install-path seed: app_state updated but seed file \
             {} could not be removed: {} \
             (next boot will see the cache warm and skip the seed)",
            seed_path.display(),
            e,
        );
    }
    true
}

fn load_projects_from_disk() -> HashMap<String, types::Project> {
    let path = paths::vct_root_dir().join("projects.json");

    if !path.exists() {
        return HashMap::new();
    }
    let data = std::fs::read_to_string(&path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}

// ---------------------------------------------------------------------------
// v0.2.17 (plan 0.0) — boot-sweep helper tests
// ---------------------------------------------------------------------------
//
// Reviewer A explicitly flagged the lack of unit tests for
// `pid_is_alive` + `sweep_stale_binary_siblings` as a coverage gap.
// The v0.2.17.1 hotfix (errno read using `std::io::Error::last_os_error()`
// instead of glibc-only `libc::__errno_location`) is exactly the
// class of bug a cross-OS unit test would have caught at PR review
// time instead of at release-tag time. Closing the gap.

#[cfg(test)]
mod boot_sweep_tests {
    use super::*;
    use std::fs;

    /// `pid_is_alive(<our own pid>)` MUST return true on every OS
    /// we support. If this fails, the boot sweep will delete every
    /// `.old-<pid>` / `.pending-<pid>` file it encounters and we
    /// risk wiping the canonical binary the user is about to relaunch.
    #[test]
    fn pid_is_alive_returns_true_for_own_pid() {
        let our_pid = std::process::id();
        assert!(
            pid_is_alive(our_pid),
            "pid_is_alive({}) returned false for the current process",
            our_pid,
        );
    }

    /// `pid_is_alive(<very high pid>)` returns false. PID_MAX on Linux
    /// defaults to 32768 (configurable up to 4_194_304); on macOS the
    /// kernel limit is 99998. We use `i32::MAX` (2_147_483_647) —
    /// well above any allocatable PID on every supported OS, and
    /// (crucially) stays POSITIVE when cast to the signed `pid_t`.
    ///
    /// Do NOT use `u32::MAX`: when cast to `pid_t` (i32 on Linux/macOS)
    /// it becomes `-1`, and `kill(-1, 0)` is the POSIX "every
    /// permitted process" form which returns 0 (success) — pid_is_alive
    /// would then return TRUE for `u32::MAX`, giving a misleading
    /// false-pass even when the underlying errno read is correct.
    #[test]
    fn pid_is_alive_returns_false_for_max_pid() {
        let high_pid = i32::MAX as u32;
        // Skip if somehow matches our own PID (impossible in practice).
        if std::process::id() == high_pid {
            return;
        }
        assert!(
            !pid_is_alive(high_pid),
            "pid_is_alive(i32::MAX) returned true — boot sweep \
             would incorrectly skip stale siblings (this is the bug \
             the v0.2.17.1 hotfix targets)"
        );
    }

    /// `sweep_stale_binary_siblings` deletes dead-pid suffixes and
    /// keeps live-pid suffixes / malformed names / non-suffix files.
    /// Tempdir-based; doesn't touch real launcher state.
    #[test]
    fn sweep_deletes_dead_pid_siblings_keeps_others() {
        let dir = tempfile::tempdir().expect("tempdir");
        let our_pid = std::process::id();

        // Dead-pid file: should be deleted. Use i32::MAX so the
        // signed-cast trick in libc::kill doesn't trigger the POSIX
        // "broadcast-to-permitted-set" form (kill(-1, 0) returns 0).
        // See the pid_is_alive_returns_false_for_max_pid doc comment.
        let dead_pid_str = (i32::MAX as u32).to_string();
        let dead = dir.path().join(format!("vct-launcher.old-{}", dead_pid_str));
        fs::write(&dead, "stale bytes").unwrap();

        // Live-pid file: must be preserved.
        let live = dir.path().join(format!("vct-launcher.old-{}", our_pid));
        fs::write(&live, "current pid bytes").unwrap();

        // Pending-suffix variant for the same dead pid: also deleted.
        let dead_pending = dir.path().join(format!("vct-launcher.exe.pending-{}", dead_pid_str));
        fs::write(&dead_pending, "pending stale").unwrap();

        // Malformed names: kept.
        let malformed_no_dash = dir.path().join("vct-launcher.oldsomething");
        fs::write(&malformed_no_dash, "x").unwrap();
        let malformed_bad_pid = dir.path().join("vct-launcher.old-NOTANUMBER");
        fs::write(&malformed_bad_pid, "x").unwrap();

        // Non-suffix files (the actual launcher binary, metadata, etc.):
        // kept untouched.
        let canonical = dir.path().join("vct-launcher");
        fs::write(&canonical, "real binary").unwrap();
        let metadata = dir.path().join("vct-launcher.metadata.json");
        fs::write(&metadata, "{}").unwrap();

        sweep_stale_binary_siblings(dir.path());

        assert!(!dead.exists(), "dead-pid sibling should have been deleted");
        assert!(
            !dead_pending.exists(),
            "dead-pid .pending- sibling should have been deleted"
        );
        assert!(live.exists(), "live-pid sibling MUST be preserved");
        assert!(
            malformed_no_dash.exists(),
            "malformed (no -<pid> separator) MUST be preserved"
        );
        assert!(
            malformed_bad_pid.exists(),
            "malformed (non-numeric pid) MUST be preserved"
        );
        assert!(canonical.exists(), "canonical binary MUST be preserved");
        assert!(metadata.exists(), "metadata.json MUST be preserved");
    }

    /// `sweep_stale_binary_siblings` is a silent no-op on a missing
    /// directory (one of the soft-fail contracts in the helper).
    #[test]
    fn sweep_handles_missing_dir_silently() {
        let nonexistent = std::path::PathBuf::from("/tmp/vct-test-nonexistent-sweep-target");
        // No assertions; success = function returns without panicking.
        sweep_stale_binary_siblings(&nonexistent);
    }
}

// ---------------------------------------------------------------------------
// v0.2.37 (Agent V37-E, 2026-05-27) — install_path_seed consumer tests
// ---------------------------------------------------------------------------
//
// Companion to `install.py::_seed_launcher_install_path`. The seed file
// is the out-of-band channel that primes the launcher's DB cache for
// the canonical orchestrator-root resolver, closing the
// "binary-outside-clone" gap that produced the SD15 / instambul_map
// "missing VCT_ORCHESTRATOR_ROOT" bug.

#[cfg(test)]
mod install_path_seed_tests {
    use super::*;

    /// Build a directory tree that passes `check_install_status`:
    /// install.py + CLAUDE.md + state/install-manifest.json with
    /// `installed:true`. The seed file points to THIS directory.
    fn fake_install_with_seed(seed_content: &str) -> (std::path::PathBuf, std::path::PathBuf) {
        use std::fs;
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-seed-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(tmp.join(".vct")).unwrap();
        fs::create_dir_all(tmp.join("state")).unwrap();
        fs::write(tmp.join("install.py"), "# stub\n").unwrap();
        fs::write(tmp.join("CLAUDE.md"), "# stub\n").unwrap();
        fs::write(
            tmp.join("state/install-manifest.json"),
            "{\"installed\":true}\n",
        )
        .unwrap();
        let seed = tmp.join(".vct").join("install_path_seed.txt");
        fs::write(&seed, seed_content).unwrap();
        (tmp, seed)
    }

    /// Happy path — seed file → app_state, seed deleted, returns true.
    #[test]
    fn process_install_path_seed_promotes_valid_path_and_deletes_seed() {
        let db = db::Db::open_in_memory().expect("in-memory db");
        let (install_dir, seed) =
            fake_install_with_seed(&format!("{}\n", "TMPDIR_PLACEHOLDER"));
        // Rewrite seed content with the actual path now that we have it.
        std::fs::write(&seed, format!("{}\n", install_dir.to_string_lossy()))
            .unwrap();

        let consumed = process_install_path_seed(&db, &seed);
        assert!(consumed, "valid seed must be consumed (returns true)");

        // app_state was written with the install path.
        let cached = db
            .app_state_get(commands::installer::APP_STATE_KEY_INSTALL_PATH)
            .expect("app_state read")
            .expect("app_state value set");
        assert_eq!(cached, install_dir.to_string_lossy().to_string());

        // Seed file was deleted (idempotency — next boot is a no-op).
        assert!(
            !seed.is_file(),
            "consumed seed file must be deleted, but {} still exists",
            seed.display()
        );

        std::fs::remove_dir_all(&install_dir).ok();
    }

    /// Seed contents point at a directory that doesn't pass
    /// `check_install_status` (e.g. partial / in-progress install).
    /// Must NOT promote, must NOT delete the seed (so it can be retried
    /// on next boot).
    #[test]
    fn process_install_path_seed_skips_invalid_path_and_preserves_seed() {
        let db = db::Db::open_in_memory().expect("in-memory db");
        let bogus = std::env::temp_dir().join(format!(
            "vct-v0237-no-such-{}",
            uuid::Uuid::new_v4().simple()
        ));
        // Create the seed file in a tempdir, contents point at a
        // directory that doesn't exist.
        let seed_dir = std::env::temp_dir().join(format!(
            "vct-v0237-seed-only-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&seed_dir).unwrap();
        let seed = seed_dir.join("install_path_seed.txt");
        std::fs::write(&seed, format!("{}\n", bogus.to_string_lossy())).unwrap();

        let consumed = process_install_path_seed(&db, &seed);
        assert!(!consumed, "invalid seed must NOT be consumed");

        // app_state unchanged.
        let cached = db
            .app_state_get(commands::installer::APP_STATE_KEY_INSTALL_PATH)
            .expect("app_state read");
        assert_eq!(cached, None, "invalid seed must NOT touch app_state");

        // Seed file preserved for retry.
        assert!(
            seed.is_file(),
            "invalid seed must be preserved for next-boot retry, \
             but {} was deleted",
            seed.display()
        );

        std::fs::remove_dir_all(&seed_dir).ok();
    }

    /// Empty seed file is deleted (defensive — never re-read garbage).
    #[test]
    fn process_install_path_seed_drops_empty_seed() {
        let db = db::Db::open_in_memory().expect("in-memory db");
        let dir = std::env::temp_dir().join(format!(
            "vct-v0237-empty-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let seed = dir.join("seed.txt");
        std::fs::write(&seed, "").unwrap();

        let consumed = process_install_path_seed(&db, &seed);
        assert!(!consumed, "empty seed → returns false");
        assert!(
            !seed.is_file(),
            "empty seed must be cleaned up (no point retrying garbage)"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// `locate_install_path_seed` finds a seed in `start/.vct/`.
    #[test]
    fn locate_install_path_seed_finds_at_start() {
        let dir = std::env::temp_dir().join(format!(
            "vct-v0237-locate-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(dir.join(".vct")).unwrap();
        let seed = dir.join(".vct").join("install_path_seed.txt");
        std::fs::write(&seed, "/some/path\n").unwrap();

        let found = locate_install_path_seed(&dir);
        assert_eq!(found.as_deref(), Some(seed.as_path()));

        std::fs::remove_dir_all(&dir).ok();
    }

    /// `locate_install_path_seed` walks up to find a seed in an
    /// ancestor's `.vct/`.
    #[test]
    fn locate_install_path_seed_walks_up_to_ancestor() {
        let root = std::env::temp_dir().join(format!(
            "vct-v0237-locate-ancestor-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let nested = root.join("a").join("b").join("c");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::create_dir_all(root.join(".vct")).unwrap();
        let seed = root.join(".vct").join("install_path_seed.txt");
        std::fs::write(&seed, "/some/path\n").unwrap();

        let found = locate_install_path_seed(&nested);
        assert_eq!(
            found.as_deref(),
            Some(seed.as_path()),
            "walk-up must find seed in ancestor"
        );

        std::fs::remove_dir_all(&root).ok();
    }

    /// `locate_install_path_seed` returns None when no seed is reachable.
    #[test]
    fn locate_install_path_seed_returns_none_when_absent() {
        let dir = std::env::temp_dir().join(format!(
            "vct-v0237-no-seed-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();

        let found = locate_install_path_seed(&dir);
        assert_eq!(found, None);

        std::fs::remove_dir_all(&dir).ok();
    }
}
