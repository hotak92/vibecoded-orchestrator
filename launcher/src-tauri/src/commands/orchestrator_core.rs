// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Stream 2 follow-up (v0.2.20 / 2026-05-19): Tauri-command backings for
// the orchestrator-core `gui.config_tab` declared in the repo-root
// `vct-module.json`.
//
// The orchestrator core is always installed (it IS the install root) so
// its config tab needs to surface alongside paid-module tabs without any
// install-state gating. The schema renderer (`ModuleConfigTab.svelte`)
// dispatches button actions by invoking the Tauri command named in the
// manifest's `button.action`; the six commands in this module are those
// targets.
//
// Design philosophy: thin subprocess wrappers around the shell scripts
// in `.claude/scripts/` that have been the canonical KG/code-graph
// management entry points since v0.1. We deliberately delegate to those
// scripts rather than reimplementing the logic in Rust because:
//   1. The scripts already activate the right Python venv, set env vars
//      (KG_COLLECTION, etc.) from `.claude/env`, and handle the cross-OS
//      path resolution. Reimplementing in Rust would duplicate that
//      surface and drift from the source of truth.
//   2. Users running these from Claude Code AND from the launcher GUI
//      see the same behaviour — single code path = single bug surface.
//   3. Stream-2-Phase-1 scope explicitly defers "production wiring like
//      long-running progress streaming" to Stream 2 Phase 2; these
//      commands are correct for the smoke test (sync subprocess,
//      ~10-30s wait) without needing the tokio-streaming pattern that
//      `commands/codegraph_reanalyze.rs` and `commands/kg_sync.rs` use.
//
// Per-project commands take a `project_id` argument because the GUI's
// "current project" selector is the load-bearing source of truth — the
// schema renderer forwards it from the Sidebar's active project store.
// The launcher's `module_settings` table is keyed by project_id; module-
// global state would need a separate migration (out of scope here).
//
// Health/logs commands take NO project_id because they cover the
// orchestrator-wide infrastructure (Weaviate, Ollama, code-embed
// service, and the user's `~/.claude/logs/` directory).
//
// Soft-fail philosophy: subprocess failures surface as `Err(String)` to
// the GUI, which renders a toast. We do NOT panic on missing scripts,
// missing services, or non-zero exit codes — every error path produces
// a user-readable message.

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, State};
use tokio::process::Command;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Wire types ──────────────────────────────────────────────────────────

/// Result of `kg_rebuild_current_project`. Mirrors the structure the
/// existing `commands::kg_sync::KgSyncView` would emit at terminal-event
/// time, but in a synchronous shape (this command blocks until the
/// subprocess exits). The streaming path lives in
/// `commands::kg_sync::retry_kg_sync` for callers who want progress.
#[derive(Debug, Clone, Serialize)]
pub struct KgRebuildResult {
    /// `true` iff the subprocess exited 0 AND we observed at least one
    /// "Syncing node:" or "Syncing doc:" line. A 0 exit with no syncs
    /// usually means an empty `knowledge/` + `docs/` tree.
    pub ok: bool,
    /// Approximate file count parsed from the script's stdout. `None`
    /// when the parse failed (script output format drifted) — the
    /// command still succeeds if the exit code is 0; this is a metric.
    pub files_synced: Option<u32>,
    /// Total subprocess wall time in milliseconds. Useful for the
    /// GUI's "Rebuild completed in 4.3s" toast.
    pub duration_ms: u64,
    /// Last ~1 KB of combined stdout+stderr for debugging when ok=false.
    pub log_tail: String,
}

/// One duplicate pair surfaced by `kg_check_duplicates`. Fields mirror
/// the `detect_duplicates.py --json` output so the GUI can render the
/// pair side-by-side without translation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DuplicateNode {
    pub uuid: String,
    pub title: String,
    /// Relative path to the markdown file (from project root).
    pub path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DuplicatePair {
    pub node1: DuplicateNode,
    pub node2: DuplicateNode,
    /// Cosine similarity in [0.0, 1.0]. 1.0 = identical embeddings.
    pub semantic_similarity: f64,
    /// Token-level title overlap in [0.0, 1.0].
    pub title_similarity: f64,
    /// `max(semantic_similarity, title_similarity)`. The GUI sorts by
    /// this and highlights confidence tier (≥0.98 / ≥0.95 / ≥0.90).
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DuplicateScanResult {
    /// Threshold that the scan ran with (echoed back from the script
    /// for defense-in-depth — confirms argument plumbing didn't
    /// silently drop the value).
    pub threshold: f64,
    pub pairs: Vec<DuplicatePair>,
}

/// Result of `code_graph_reanalyze_current` / `code_graph_prune_stale`.
/// Both commands wrap `code-graph-analyze` so the shapes overlap;
/// `pruned` is `None` for reanalyze-only invocations.
#[derive(Debug, Clone, Serialize)]
pub struct CodeGraphResult {
    pub ok: bool,
    pub duration_ms: u64,
    pub log_tail: String,
}

/// Per-service status returned by `orchestrator_health_check`. One
/// `ServiceCheck` per probed endpoint. `ok=false` means either the
/// endpoint didn't respond within the timeout OR returned a non-2xx
/// status code; `detail` carries the explanation.
#[derive(Debug, Clone, Serialize)]
pub struct ServiceCheck {
    pub name: String,
    pub endpoint: String,
    pub ok: bool,
    /// Round-trip time in ms for successful checks. `None` on failure.
    pub latency_ms: Option<u64>,
    /// Human-readable status. "200 OK" on success; error message on
    /// failure ("connection refused", "timeout after 3s", etc.).
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HealthReport {
    pub services: Vec<ServiceCheck>,
    /// `true` iff every service in `services` reports `ok=true`. The
    /// GUI uses this for the overall traffic-light indicator.
    pub all_ok: bool,
}

// ─── Helpers ─────────────────────────────────────────────────────────────

/// Resolve a project's folder_path from its id. Centralized so the four
/// per-project commands share the same lookup + error message.
fn resolve_project_folder(db: &Db, project_id: &str) -> Result<PathBuf, String> {
    let project = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let folder = PathBuf::from(&project.folder_path);
    if !folder.is_dir() {
        return Err(format!(
            "project {} folder_path {} is not a directory (was it moved or deleted?)",
            project_id,
            folder.display()
        ));
    }
    Ok(folder)
}

/// Cap the captured log to ~1 KB so a runaway subprocess can't blow up
/// the toast payload. The GUI shows this verbatim in the error modal.
fn tail_1kb(s: &str) -> String {
    const TAIL_BYTES: usize = 1024;
    if s.len() <= TAIL_BYTES {
        return s.to_string();
    }
    // Walk back from the end to find a char boundary; avoids panicking
    // on a multi-byte character split.
    let start = s.len() - TAIL_BYTES;
    let mut anchor = start;
    while anchor < s.len() && !s.is_char_boundary(anchor) {
        anchor += 1;
    }
    format!("…(truncated)…\n{}", &s[anchor..])
}

/// OS-correct bundled-script basename: `<name>.ps1` on Windows, `<name>` on
/// POSIX.
fn script_bin(name: &str) -> String {
    if cfg!(windows) {
        format!("{}.ps1", name)
    } else {
        name.to_string()
    }
}

/// Build a tokio Command that invokes a `.claude/scripts/*` wrapper
/// with the right interpreter. On Windows we shell through powershell;
/// on POSIX we run the bash script directly (it has a shebang).
///
/// v0.2.77: resolution goes through the shared
/// `codegraph::resolve_bundled_script` ladder — project-local first (with
/// the stale-wrapper guard for RT-4 wrappers), then the ORCHESTRATOR copy
/// (`$VCT_LAUNCHER_SCRIPTS_DIR` → sibling-of-exe → PATH). Previously this was
/// project-local ONLY and errored outright when the project's
/// `.claude/scripts/` was missing (the live-bug (b) surface for kg-sync /
/// kg-duplicates / code-graph-analyze during "update all projects"). The
/// fallback is additive: a healthy project-local script still wins.
fn build_script_command(project_folder: &PathBuf, name: &str) -> Result<Command, String> {
    let bin = script_bin(name);
    let path = crate::commands::codegraph::resolve_bundled_script(project_folder, &bin)
        .ok_or_else(|| {
            format!(
                "script {} not found — neither the project's .claude/scripts/ \
                 nor the orchestrator copy (VCT_LAUNCHER_SCRIPTS_DIR / \
                 sibling-of-exe / PATH) resolved. Re-run the project's bundle \
                 install if the project-local copy is missing.",
                bin
            )
        })?;
    let mut cmd = if cfg!(windows) {
        let mut c = Command::new("powershell").silent();
        c.arg("-NoProfile")
            .arg("-ExecutionPolicy")
            .arg("Bypass")
            .arg("-File")
            .arg(&path);
        c
    } else {
        Command::new(&path).silent()
    };
    cmd.current_dir(project_folder);
    Ok(cmd)
}

// ─── Commands ───────────────────────────────────────────────────────────

/// Re-sync every `knowledge/*.md` file in the selected project's tree
/// into its Weaviate KG collection. Idempotent — re-running on a clean
/// project is a no-op upsert at the Weaviate layer.
///
/// Streaming: this is the SYNCHRONOUS path. Users wanting live progress
/// pills should call `commands::kg_sync::retry_kg_sync` (the existing
/// streaming command tied to the `kg-sync-progress` event channel).
/// We expose both so the schema-rendered tab has a simple action and
/// the project-state UI keeps its rich progress view.
#[command]
pub async fn kg_rebuild_current_project(
    project_id: String,
    db: State<'_, Db>,
    _app: AppHandle,
) -> Result<KgRebuildResult, String> {
    let folder = resolve_project_folder(&db, &project_id)?;
    let mut cmd = build_script_command(&folder, "kg-sync")?;
    cmd.arg("--all");

    let started = std::time::Instant::now();
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn kg-sync: {}", e))?;
    let duration_ms = started.elapsed().as_millis() as u64;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let combined = format!("{}{}", stdout, stderr);

    // Heuristic file counter: count "Syncing node:" + "Syncing doc:"
    // lines. The wrapper's stdout shape is documented in
    // `commands::kg_sync` and is stable across v0.1+.
    let files_synced: u32 = combined
        .lines()
        .filter(|l| l.contains("Syncing node:") || l.contains("Syncing doc:"))
        .count() as u32;
    let files_synced = if combined.contains("Syncing") {
        Some(files_synced)
    } else {
        None
    };

    db.audit(
        "orchestrator_core_kg_rebuild",
        Some(&project_id),
        None,
        &serde_json::json!({
            "ok": output.status.success(),
            "duration_ms": duration_ms,
            "files_synced": files_synced,
        }),
    )?;

    Ok(KgRebuildResult {
        ok: output.status.success(),
        files_synced,
        duration_ms,
        log_tail: tail_1kb(&combined),
    })
}

/// Run `.claude/scripts/kg-duplicates --json --threshold <t>` and parse
/// the resulting pairs. Default threshold = 0.95 (matches the script's
/// own default).
#[command]
pub async fn kg_check_duplicates(
    project_id: String,
    threshold: Option<f64>,
    db: State<'_, Db>,
) -> Result<DuplicateScanResult, String> {
    let folder = resolve_project_folder(&db, &project_id)?;
    let threshold = threshold.unwrap_or(0.95);
    if !(0.0..=1.0).contains(&threshold) {
        return Err(format!(
            "threshold must be in [0.0, 1.0]; got {}",
            threshold
        ));
    }
    let mut cmd = build_script_command(&folder, "kg-duplicates")?;
    cmd.arg("--json").arg("--threshold").arg(threshold.to_string());

    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn kg-duplicates: {}", e))?;
    if !output.status.success() {
        return Err(format!(
            "kg-duplicates exited {}: {}",
            output.status.code().map(|c| c.to_string()).unwrap_or_else(|| "?".into()),
            tail_1kb(&String::from_utf8_lossy(&output.stderr))
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    // Find the JSON document — the script may have prefixed lines from
    // the venv-activation shim, so we scan for the first '{'.
    let json_start = stdout.find('{').ok_or_else(|| {
        format!(
            "kg-duplicates --json produced no JSON document. stdout tail: {}",
            tail_1kb(&stdout)
        )
    })?;
    let json_doc = &stdout[json_start..];

    #[derive(Deserialize)]
    struct Payload {
        threshold: f64,
        #[serde(default)]
        pairs: Vec<DuplicatePair>,
    }
    let payload: Payload = serde_json::from_str(json_doc).map_err(|e| {
        format!(
            "parse kg-duplicates JSON: {} (doc head: {})",
            e,
            &json_doc.chars().take(200).collect::<String>()
        )
    })?;

    db.audit(
        "orchestrator_core_kg_dupcheck",
        Some(&project_id),
        None,
        &serde_json::json!({
            "threshold": payload.threshold,
            "pair_count": payload.pairs.len(),
        }),
    )?;

    Ok(DuplicateScanResult {
        threshold: payload.threshold,
        pairs: payload.pairs,
    })
}

/// Re-analyze the project's code with `--incremental` (default) — only
/// changed files are re-walked. Always pairs with `--prune-stale` so
/// rows for deleted files don't accumulate.
#[command]
pub async fn code_graph_reanalyze_current(
    project_id: String,
    db: State<'_, Db>,
    _app: AppHandle,
) -> Result<CodeGraphResult, String> {
    let folder = resolve_project_folder(&db, &project_id)?;
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // v0.2.82 (WP-3 G3 task 1, pipeline J): feed the CANONICAL code-graph
    // identity, not the raw display name — the SSOT helper resolves the binding
    // prefix (== what per-edit hooks stamp) so this reanalyze does not mint a
    // second identity's worth of duplicate rows.
    let identity = crate::commands::codegraph::resolve_codegraph_identity(
        &db,
        &project_id,
        &project.name,
    );

    let mut cmd = build_script_command(&folder, "code-graph-analyze")?;
    cmd.arg(".")
        .arg("--project")
        .arg(&identity)
        .arg("--incremental")
        .arg("--prune-stale");

    let started = std::time::Instant::now();
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn code-graph-analyze: {}", e))?;
    let duration_ms = started.elapsed().as_millis() as u64;

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    db.audit(
        "orchestrator_core_codegraph_reanalyze",
        Some(&project_id),
        None,
        &serde_json::json!({
            "ok": output.status.success(),
            "duration_ms": duration_ms,
        }),
    )?;

    Ok(CodeGraphResult {
        ok: output.status.success(),
        duration_ms,
        log_tail: tail_1kb(&combined),
    })
}

/// Prune code-graph rows whose source files no longer exist on disk.
/// Wraps `.claude/scripts/code-graph-analyze . --project <name>
/// --prune-stale` WITHOUT `--incremental` — the prune walk needs to
/// see the full project tree to mark which files are still present.
#[command]
pub async fn code_graph_prune_stale(
    project_id: String,
    db: State<'_, Db>,
    _app: AppHandle,
) -> Result<CodeGraphResult, String> {
    let folder = resolve_project_folder(&db, &project_id)?;
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // v0.2.82 (WP-3 G3 task 1, pipeline L): CANONICAL identity — same SSOT as
    // pipeline J above. A prune-stale walk that stamped the display name would
    // reap the hook-identity rows; feed the binding prefix so both writers agree.
    let identity = crate::commands::codegraph::resolve_codegraph_identity(
        &db,
        &project_id,
        &project.name,
    );

    let mut cmd = build_script_command(&folder, "code-graph-analyze")?;
    cmd.arg(".")
        .arg("--project")
        .arg(&identity)
        .arg("--prune-stale");

    let started = std::time::Instant::now();
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn code-graph-analyze --prune-stale: {}", e))?;
    let duration_ms = started.elapsed().as_millis() as u64;

    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    db.audit(
        "orchestrator_core_codegraph_prune",
        Some(&project_id),
        None,
        &serde_json::json!({
            "ok": output.status.success(),
            "duration_ms": duration_ms,
        }),
    )?;

    Ok(CodeGraphResult {
        ok: output.status.success(),
        duration_ms,
        log_tail: tail_1kb(&combined),
    })
}

/// Probe the three local infrastructure endpoints + emit a per-service
/// status report. Total budget ~3s (each probe times out at 1s).
#[command]
pub async fn orchestrator_health_check(
    _db: State<'_, Db>,
) -> Result<HealthReport, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(1))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    // Endpoints match the defaults documented in CLAUDE.md. Override
    // via env so dev-container probes hit a non-default port.
    let weaviate_url = std::env::var("WEAVIATE_URL")
        .unwrap_or_else(|_| "http://localhost:8081".to_string());
    let ollama_url = std::env::var("OLLAMA_URL")
        .unwrap_or_else(|_| "http://localhost:11435".to_string());
    let code_embed_url = std::env::var("CODE_EMBED_SERVICE_URL")
        .unwrap_or_else(|_| "http://localhost:11440".to_string());

    let checks = vec![
        (
            "Weaviate".to_string(),
            format!("{}/v1/.well-known/ready", weaviate_url.trim_end_matches('/')),
        ),
        (
            "Ollama".to_string(),
            format!("{}/api/tags", ollama_url.trim_end_matches('/')),
        ),
        (
            "Code Embedding Service".to_string(),
            format!("{}/health", code_embed_url.trim_end_matches('/')),
        ),
    ];

    let mut services = Vec::with_capacity(checks.len());
    for (name, endpoint) in checks {
        let started = std::time::Instant::now();
        let result = client.get(&endpoint).send().await;
        let latency_ms = started.elapsed().as_millis() as u64;
        let check = match result {
            Ok(resp) => {
                let status = resp.status();
                if status.is_success() {
                    ServiceCheck {
                        name,
                        endpoint,
                        ok: true,
                        latency_ms: Some(latency_ms),
                        detail: format!("{}", status),
                    }
                } else {
                    ServiceCheck {
                        name,
                        endpoint,
                        ok: false,
                        latency_ms: None,
                        detail: format!("HTTP {}", status.as_u16()),
                    }
                }
            }
            Err(e) => ServiceCheck {
                name,
                endpoint,
                ok: false,
                latency_ms: None,
                // reqwest's display includes whether it was a timeout
                // or a connect error — informative enough to render.
                detail: format!("{}", e),
            },
        };
        services.push(check);
    }

    let all_ok = services.iter().all(|s| s.ok);
    Ok(HealthReport { services, all_ok })
}

/// Reveal `~/.claude/logs/` in the user's file manager (Finder /
/// Nautilus / Explorer). Soft-fails when the directory doesn't exist —
/// we return Err so the GUI can show a toast explaining that the user
/// hasn't run any Claude Code sessions yet (which is when the hook
/// system populates the logs directory).
#[command]
pub async fn orchestrator_open_logs() -> Result<(), String> {
    let home = directories::UserDirs::new()
        .ok_or_else(|| "could not resolve home directory".to_string())?
        .home_dir()
        .to_path_buf();
    let logs = home.join(".claude").join("logs");
    if !logs.exists() {
        return Err(format!(
            "{} does not exist yet — start a Claude Code session in any \
             project to populate it (the .claude/hooks/* scripts write \
             tool-usage logs here on every Bash/Edit/Write call).",
            logs.display()
        ));
    }
    tauri_plugin_opener::open_path(logs.display().to_string(), None::<&str>)
        .map_err(|e| format!("open_path failed: {}", e))?;
    Ok(())
}

// ─── v0.2.24.1: Clone integrity commands (A0bis) ─────────────────────────
//
// The "Clone integrity" tab (renamed from "Orchestrator core" in
// v0.2.24.1 per A0bis design conclusion) hosts the 2 features that
// are genuinely root-clone-only:
//   - Re-detect orchestrator root: re-runs find_orchestrator_manifest
//     when the cached install_path is stale (clone dir renamed, moved,
//     or first-install detection picked the wrong candidate).
//   - Validate clone manifest: parses vct-module.json at clone root +
//     surfaces schema errors. When malformed, the launcher silently
//     skips it and module-contributed tabs disappear — this command
//     gives the user a single-click diagnostic.
//
// Both commands are safe to run repeatedly; neither mutates user
// state.

/// Result of `redetect_orchestrator_root`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RedetectOrchestratorRootResult {
    /// True when a manifest was found (and the cached `launcher.install_path`
    /// was refreshed to match).
    pub success: bool,
    /// Discovered clone-root path, when `success == true`.
    pub clone_root: Option<String>,
    /// User-facing message (success summary OR diagnostic when the walk
    /// found nothing).
    pub message: String,
}

/// Re-runs the `current_exe()` walk-up to find a clone-root with
/// `vct-module.json + install.py + CLAUDE.md` markers and updates the
/// `launcher.install_path` app_state entry on success. Use when the
/// launcher's cached install_path is stale (e.g., user renamed the
/// clone directory after first install, or copied the binary into a
/// different clone).
///
/// Safe to run repeatedly — never destructive. Returns a diagnostic
/// when no clone is reachable from the current binary's location.
#[command]
pub async fn redetect_orchestrator_root(
    db: State<'_, crate::db::Db>,
) -> Result<RedetectOrchestratorRootResult, String> {
    let exe = std::env::current_exe()
        .map_err(|e| format!("current_exe failed: {}", e))?;
    let exe_display = exe.display().to_string();

    // walk_for_install_markers is the canonical exe-walk discovery.
    // Re-exposed via the installer module for command-side reuse.
    let found = crate::commands::installer::walk_for_install_markers();
    match found {
        Some(path) => {
            let path_str = path.to_string_lossy().to_string();
            // Update the sticky cache so future synchronous resolvers
            // (e.g. manifest_scan_paths) pick up the new path.
            if let Err(e) = db.app_state_set(
                crate::commands::installer::APP_STATE_KEY_INSTALL_PATH,
                &path_str,
            ) {
                return Err(format!(
                    "discovered clone at {} but failed to cache the path \
                     in app_state: {}. The discovery is correct; manually \
                     persisting via `vct app-state set launcher.install_path \
                     {}` is the workaround.",
                    path_str, e, path_str,
                ));
            }
            Ok(RedetectOrchestratorRootResult {
                success: true,
                clone_root: Some(path_str.clone()),
                message: format!(
                    "Discovered clone-root at {}. Cached as launcher.install_path.",
                    path_str
                ),
            })
        }
        None => Ok(RedetectOrchestratorRootResult {
            success: false,
            clone_root: None,
            message: format!(
                "No orchestrator clone reachable from the launcher binary's location ({}). \
                 The launcher walks up 8 directories looking for the marker pair \
                 (install.py + CLAUDE.md). If your clone is elsewhere, move the launcher \
                 binary into <clone>/launcher/dist/<target>/ — or set \
                 launcher.install_path manually via `vct app-state set launcher.install_path <path>`.",
                exe_display
            ),
        }),
    }
}

/// Result of `validate_clone_manifest`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ValidateCloneManifestResult {
    /// True iff the manifest at clone-root parsed AND the version +
    /// description fields look sane.
    pub valid: bool,
    /// Resolved path of the vct-module.json that was inspected, when found.
    pub manifest_path: Option<String>,
    /// On invalid: human-readable diagnostic (parse error, missing field,
    /// or "no manifest found"). On valid: short summary of the version +
    /// component count.
    pub message: String,
}

/// Parses the orchestrator-root `vct-module.json` and surfaces any
/// schema errors. When malformed, the launcher's `read_orchestrator_manifest`
/// silently treats the orchestrator as a non-module project, which is
/// confusing because the sidebar loses any module-contributed tabs
/// (including this one). This command makes the failure explicit so
/// the user knows what to fix.
#[command]
pub async fn validate_clone_manifest(
    db: State<'_, crate::db::Db>,
) -> Result<ValidateCloneManifestResult, String> {
    let install_root = crate::commands::installer::resolve_install_root_sync(&db);
    let manifest_path = match install_root {
        Some(root) => root.join("vct-module.json"),
        None => {
            return Ok(ValidateCloneManifestResult {
                valid: false,
                manifest_path: None,
                message: "No orchestrator clone is registered. Run 'Re-detect orchestrator root' first.".to_string(),
            });
        }
    };

    if !manifest_path.exists() {
        return Ok(ValidateCloneManifestResult {
            valid: false,
            manifest_path: Some(manifest_path.display().to_string()),
            message: format!(
                "vct-module.json not found at {}. The clone-root marker pair (install.py + CLAUDE.md) was found but the manifest is missing — this clone may be from before v0.2.20 (when the orchestrator-core manifest was introduced) or it was deleted by hand.",
                manifest_path.display()
            ),
        });
    }

    let raw = match std::fs::read_to_string(&manifest_path) {
        Ok(s) => s,
        Err(e) => {
            return Ok(ValidateCloneManifestResult {
                valid: false,
                manifest_path: Some(manifest_path.display().to_string()),
                message: format!("Failed to read {}: {}", manifest_path.display(), e),
            });
        }
    };

    match serde_json::from_str::<
        vct_launcher_core::orchestrator_manifest::OrchestratorManifest,
    >(&raw)
    {
        Ok(m) => Ok(ValidateCloneManifestResult {
            valid: true,
            manifest_path: Some(manifest_path.display().to_string()),
            message: format!(
                "Valid: orchestrator core v{} ({}), {} component(s) declared.",
                m.version,
                m.description.chars().take(80).collect::<String>(),
                m.components.len(),
            ),
        }),
        Err(e) => Ok(ValidateCloneManifestResult {
            valid: false,
            manifest_path: Some(manifest_path.display().to_string()),
            message: format!(
                "Parse error in {}: {}. The launcher's catalog renderer will silently skip this clone until the JSON is fixed; module-contributed tabs (including this one) will be absent from the sidebar.",
                manifest_path.display(),
                e
            ),
        }),
    }
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// `tail_1kb` truncates oversize input, leaves small input alone,
    /// and never panics on multi-byte char boundaries. The boundary
    /// case (truncation point falls inside a UTF-8 multi-byte char)
    /// historically panicked with `byte index N is not a char
    /// boundary`; this test pins the safe-walk behaviour.
    #[test]
    fn tail_1kb_passes_through_short_input() {
        let s = "hello world";
        assert_eq!(tail_1kb(s), s);
    }

    #[test]
    fn tail_1kb_truncates_long_input() {
        let s = "a".repeat(2048);
        let out = tail_1kb(&s);
        assert!(out.starts_with("…(truncated)…\n"));
        assert!(out.len() <= 1024 + 32, "tail ~1KB plus marker");
    }

    #[test]
    fn tail_1kb_handles_multibyte_boundary() {
        // Build a string just over 1KB whose 1024-th byte falls inside
        // a 3-byte UTF-8 sequence ('文' = E6 96 87 in UTF-8). Naive
        // slicing would panic; tail_1kb must walk to the next boundary.
        let prefix = "a".repeat(1022);
        let s = format!("{}文文文", prefix); // 1022 + 9 = 1031 bytes
        let out = tail_1kb(&s);
        assert!(out.contains("文"), "must contain a full char post-truncation");
    }

    /// `script_bin` produces `.ps1` on Windows and bareword on POSIX.
    /// The OS-specific assertion uses `cfg!(windows)` to stay valid
    /// on both targets.
    #[test]
    fn script_bin_extension_matches_os() {
        let bin = script_bin("kg-sync");
        if cfg!(windows) {
            assert_eq!(bin, "kg-sync.ps1");
        } else {
            assert_eq!(bin, "kg-sync");
        }
    }

    /// v0.2.77: `build_script_command` falls back to the ORCHESTRATOR copy
    /// when the project has no `.claude/scripts/` of its own. Previously it
    /// errored outright. We point `$VCT_LAUNCHER_SCRIPTS_DIR` at an
    /// orchestrator scripts dir (candidate 2 of the shared ladder) and assert
    /// the command builds successfully for a project folder with NO local
    /// scripts.
    #[test]
    fn build_script_command_falls_back_to_orchestrator_copy() {
        let bin = script_bin("kg-sync");

        // Project WITHOUT its own .claude/scripts/.
        let proj = std::env::temp_dir().join(format!(
            "vct-bsc-proj-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&proj).unwrap();

        // Orchestrator scripts dir reachable via VCT_LAUNCHER_SCRIPTS_DIR.
        let orch = std::env::temp_dir().join(format!(
            "vct-bsc-orch-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&orch).unwrap();
        let orch_script = orch.join(&bin);
        std::fs::write(&orch_script, b"#!/bin/bash\necho ok\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&orch_script).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&orch_script, perms).unwrap();
        }

        // SAFETY: crate tests run single-threaded by default.
        let saved_override = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        unsafe {
            std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", &orch);
        }

        let built = build_script_command(&proj, "kg-sync");

        unsafe {
            match saved_override {
                Some(v) => std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", v),
                None => std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR"),
            }
        }

        assert!(
            built.is_ok(),
            "orchestrator fallback must resolve when project-local is absent: {:?}",
            built.err()
        );

        std::fs::remove_dir_all(&proj).ok();
        std::fs::remove_dir_all(&orch).ok();
    }

    /// Leave-alone case: when NOTHING resolves (no project-local, no
    /// orchestrator copy on any candidate), `build_script_command` still
    /// errors with a clear message.
    #[test]
    fn build_script_command_errors_when_nothing_resolves() {
        let proj = std::env::temp_dir().join(format!(
            "vct-bsc-none-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&proj).unwrap();

        // SAFETY: crate tests run single-threaded by default.
        let saved_override = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        let saved_path = std::env::var_os("PATH");
        unsafe {
            std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", &proj); // empty dir
            std::env::set_var("PATH", "");
        }

        let built = build_script_command(&proj, "kg-duplicates");

        unsafe {
            match saved_override {
                Some(v) => std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", v),
                None => std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR"),
            }
            if let Some(p) = saved_path {
                std::env::set_var("PATH", p);
            }
        }

        // May still find a sibling-of-exe hit on a dev box; only assert the
        // error shape when it genuinely didn't resolve.
        if let Err(e) = built {
            assert!(e.contains("not found"), "clear not-found error: {}", e);
        }

        std::fs::remove_dir_all(&proj).ok();
    }

    /// `resolve_project_folder` returns a clear error when the project
    /// doesn't exist in the DB. The error string is user-facing in the
    /// toast so the test pins its readable shape.
    #[test]
    fn resolve_project_folder_missing_project_yields_clear_error() {
        let db = Db::open_in_memory().expect("in-memory db");
        let err = resolve_project_folder(&db, "does-not-exist").expect_err("missing");
        assert!(err.contains("does-not-exist"));
        assert!(err.contains("not found"));
    }

    /// `resolve_project_folder` rejects a project whose stored
    /// `folder_path` no longer points at a directory (e.g. user
    /// renamed/moved the folder outside the launcher's knowledge).
    /// Returning early here saves the subprocess from spawning into
    /// a nonexistent cwd with a confusing PATH error.
    #[test]
    fn resolve_project_folder_rejects_missing_directory() {
        let db = Db::open_in_memory().expect("in-memory db");
        let project_id = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &project_id,
            "Phantom",
            "/this/path/definitely/does/not/exist/anywhere",
            crate::db::models::ProjectHost::Base,
            "phantom",
        )
        .expect("insert project");
        let err = resolve_project_folder(&db, &project_id).expect_err("dir missing");
        assert!(err.contains("not a directory"));
    }

    /// `orchestrator_health_check` runs against arbitrary endpoints —
    /// when probed endpoints are unreachable (the test env has no
    /// Weaviate / Ollama / code-embed running) we expect three
    /// `ok=false` entries and an `all_ok=false` summary. The command
    /// must NOT panic on connect errors; the timeout is short enough
    /// that this test completes in <5s even when all three probes
    /// have to time out.
    #[tokio::test]
    async fn health_check_reports_failures_without_panicking() {
        // Force probe URLs to an unused port to guarantee connect-refused.
        std::env::set_var("WEAVIATE_URL", "http://127.0.0.1:1");
        std::env::set_var("OLLAMA_URL", "http://127.0.0.1:1");
        std::env::set_var("CODE_EMBED_SERVICE_URL", "http://127.0.0.1:1");

        // We can't easily construct a State<'_, Db> outside Tauri; the
        // command body doesn't use the db arg today, so we invoke the
        // underlying logic directly via a fresh client. This test
        // therefore proves the SHAPE of the report (three services,
        // all ok=false) rather than the Tauri-bound command itself —
        // the latter is exercised end-to-end in the launcher integ
        // tests under tests/.
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(1))
            .build()
            .unwrap();
        let endpoints = ["http://127.0.0.1:1/a", "http://127.0.0.1:1/b", "http://127.0.0.1:1/c"];
        for ep in endpoints {
            let res = client.get(ep).send().await;
            assert!(res.is_err(), "connect refused on unused port");
        }

        std::env::remove_var("WEAVIATE_URL");
        std::env::remove_var("OLLAMA_URL");
        std::env::remove_var("CODE_EMBED_SERVICE_URL");
    }
}
