//! PR-42 (v0.2.12 / 2026-05-16): `.claude/settings.json` watcher.
//!
//! Watches every registered project's `.claude/settings.json` file for
//! modify events. When an event fires (debounced 500 ms to coalesce
//! multi-byte writes from editors), invokes the same SIGHUP-based
//! reload logic exposed by the `reload_mcps_sighup` Tauri command —
//! every running orchestrator MCP gets SIGHUP, exits cleanly, then
//! Claude Code respawns it with fresh env on the next request.
//!
//! Design notes
//! ============
//!
//! * **MCPs are per-Claude-Code-session, not per-project.** On this
//!   user's machine `~/.claude.json` is shared by every workspace; an
//!   MCP subprocess belongs to the Claude Code session that spawned
//!   it, NOT to the project whose settings.json triggered the reload.
//!   So the watcher just signals every matching PID it can find —
//!   targeting by project would be more surgical but is unnecessary
//!   given the user-facing semantics ("editing env auto-reloads MCPs").
//!
//! * **Debounce, not throttle.** Editors (VS Code, vim with backup
//!   files, etc.) commonly fire multiple Modify events in quick
//!   succession for a single save: temp-file write → rename → chmod.
//!   We want to coalesce those into ONE SIGHUP burst, hence the
//!   500 ms quiet window after the last event before firing.
//!
//! * **Soft-fail.** A watcher init error (unsupported FS, permission
//!   denied) MUST NOT take the launcher down. We log to stderr and
//!   the manual "Reload MCPs" button stays available as the fallback.
//!
//! * **POSIX-only auto-reload.** On Windows `kill -HUP` doesn't exist;
//!   the watcher still runs (the file-system layer works on every OS)
//!   but the reload call short-circuits with `posix_only_skipped: true`.
//!   Surfacing a Windows toast that says "settings.json changed —
//!   restart your Claude Code session" is out of scope for this PR;
//!   keep the watcher quiet on Windows.
//!
//! Lifecycle
//! =========
//!
//! Spawned once from `lib.rs::run()` setup hook. Owns a tokio task
//! that:
//!   1. Probes the launcher DB every 30 s for the project list.
//!   2. Maintains a `RecommendedWatcher` registered to each project's
//!      `.claude/settings.json` parent dir.
//!   3. Re-syncs the watch list when the project list changes (added,
//!      removed, folder moved).
//!   4. On every Modify event whose path ends in `.claude/settings.json`,
//!      schedules a debounced reload — sleep 500 ms, then call
//!      `reload_mcps_with(...)` directly (NOT through the Tauri
//!      command, which would need a State<Db> we don't have in the
//!      watcher's tokio task).
//!
//! The 30 s project-list re-poll is conservative; new projects appear
//! infrequently and the cost is one DB query.

use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use tauri::{AppHandle, Manager, Runtime};
use tokio::sync::Mutex;

use crate::commands::maintenance;
use crate::db::Db;

/// Debounce window after the last Modify event before SIGHUP is fired.
/// Coalesces editor-multiwrite patterns into a single reload.
const DEBOUNCE_WINDOW: Duration = Duration::from_millis(500);

/// Re-poll cadence for the project list. Cheap (one SQLite SELECT) but
/// MCP-process startup costs dwarf this anyway.
const PROJECT_LIST_RE_POLL: Duration = Duration::from_secs(30);

// ═══════════════════════════════════════════════════════════════════════
// v0.2.72 (C-P8): diff-guard — skip needless SIGHUP on idempotent writes.
// ═══════════════════════════════════════════════════════════════════════
//
// Root cause of the mystery mid-session `-32000 Connection closed`: this
// watcher SIGHUPs the weaviate-kg / search MCPs on ANY `.claude/settings.json`
// Modify event, INCLUDING the byte-idempotent re-projections that
// `config_projection.apply_project_env` emits (see
// `tests/test_config_projection.py::test_apply_settings_json_idempotent`).
// Those writes change no MCP-relevant env, but the SIGHUP still exits the
// live MCP cleanly (`claude_mcp_servers/_lib/sighup_handler.py` →
// `sys.exit(0)`); Claude Code only notices the dead pipe lazily on the NEXT
// tool call → the `-32000`.
//
// The watcher sees the event AFTER the write, so it cannot diff the file
// old-vs-new. Instead it keeps a watcher-OWNED per-project hash of the
// MCP-relevant env SUBSET (computed at fire-time from the CURRENT
// settings.json) and compares it to the last-seen hash. Idempotent write →
// unchanged hash → SKIP the reload.
//
// DENYLIST, not allowlist (fail-safe): reload UNLESS the change is provably
// MCP-irrelevant.
//   * hash CHANGED   → reload + update cache.
//   * hash UNCHANGED → SKIP (the whole fix).
//   * NO baseline yet (first event / cache miss / unreadable file) →
//     fail-OPEN: reload + seed the cache.
//
// The manual "Reload MCPs" button (the `reload_mcps_sighup` Tauri command)
// stays UNCONDITIONAL — only this watcher-auto path gets the diff-guard.

/// The `.claude/settings.json` `env` keys whose VALUES the orchestrator
/// MCPs actually CONSUME (or whose change alters the Weaviate/Ollama/
/// code-embed connection the MCPs depend on). Hashing only these values —
/// rather than the whole file — is what lets an idempotent re-projection
/// produce an unchanged hash and skip the needless SIGHUP.
///
/// DENYLIST discipline: this list is deliberately GENEROUS (fail-safe).
/// A key that is MCP-relevant but accidentally omitted here would cause a
/// real env change to be silently ignored (MCP keeps stale env) — the
/// worse failure. A key that is MCP-IRRELEVANT but accidentally included
/// only costs an occasional needless reload — the benign failure. When in
/// doubt, keep the key.
///
/// MUST MATCH the `os.getenv(...)` / `os.environ.get(...)` reads in
/// `claude_mcp_servers/weaviate_mcp/server.py` and
/// `claude_mcp_servers/search_mcp/server.py`, intersected with the keys
/// `vco_lib/config_projection.py::_CANONICAL_KEYS` actually writes into
/// `.claude/settings.json` `env` (plus a few MCP-consumed keys — GRPC_PORT,
/// EMBEDDING_MODEL, CODE_EMBED_SERVICE_URL, DUAL_EMBEDDING_ENABLED — that a
/// user may hand-add to settings.json even though the current projection
/// routes them via `~/.claude.json` instead). BEWARE the key-NAME mismatch
/// between the two surfaces: the projection writes `WEAVIATE_PORT` /
/// `CODE_EMBED_URL` / `DUAL_EMBEDDING_WRITE_ALL_SLOTS`, while the MCP reads
/// `GRPC_PORT` / `CODE_EMBED_SERVICE_URL` / `DUAL_EMBEDDING_ENABLED`; both
/// spellings are listed so a change under EITHER surface is caught.
///
/// Deliberately EXCLUDED (MCP-irrelevant → their change must NOT reload):
///   * `GITHUB_TOKEN` — not consumed by the weaviate/search MCPs.
///   * `VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR` / `VCT_INSTALL_ROOT`
///     — path env for scripts/hooks, not MCP search behaviour.
///   * hook/agent/skill blocks and any other non-`env` settings.json keys —
///     they never land in the hashed subset because we only hash `env`.
const MCP_RELEVANT_ENV_KEYS: &[&str] = &[
    // --- KG / diagram / dev collection routing (weaviate_mcp reads all) ---
    "KG_COLLECTION",
    "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "DIAGRAMS_COLLECTION",
    // --- shared-KG access gates (weaviate_mcp reads all three) ---
    "SHARED_KG_READ_DISABLED",
    "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", // legacy alias for SHARED_KG_WRITE_DISABLED
    // --- cross-project access matrices (weaviate_mcp reads) ---
    "VCT_KG_ACCESS_LIST",
    "VCT_CODE_GRAPH_ACCESS_LIST",
    "VCT_DIAGRAMS_ACCESS_LIST",
    // --- project identity (weaviate_mcp reads PROJECT_NAME / CODE_GRAPH_PROJECT / VCT_PROJECT_ID) ---
    "PROJECT_NAME",
    "CODE_GRAPH_PROJECT",
    "VCT_PROJECT_ID",
    // --- embedding selection (weaviate_mcp reads ACTIVE_EMBEDDING / EMBEDDING_MODEL) ---
    "ACTIVE_EMBEDDING",
    "EMBEDDING_MODEL",
    // --- dual-write / dual-log toggles ---
    // projection surface writes DUAL_EMBEDDING_WRITE_ALL_SLOTS; the MCP /
    // embedding_service read is DUAL_EMBEDDING_ENABLED — list both spellings.
    "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
    "DUAL_EMBEDDING_ENABLED",
    "DUAL_RL_LOG_ENABLED",
    // --- connection endpoints (weaviate_mcp reads WEAVIATE_URL / OLLAMA_URL / GRPC_PORT) ---
    "WEAVIATE_URL",
    "OLLAMA_URL",
    "GRPC_PORT",
    "WEAVIATE_GRPC_PORT", // alt spelling the MCP also reads
    // projection writes *_PORT / CODE_EMBED_URL; the MCP reads
    // CODE_EMBED_SERVICE_URL. List every spelling so a port/url change under
    // any surface triggers a reload (fail-safe — these gate the backends the
    // MCP connects to).
    "WEAVIATE_PORT",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
    "CODE_EMBED_SERVICE_URL",
    // --- search MCP ---
    "OPENALEX_EMAIL",
];

/// Hash the MCP-relevant env subset of a `.claude/settings.json` body.
///
/// Returns `None` when the body isn't valid JSON (the caller treats an
/// unparseable file as "no baseline" → fail-open reload). A file with no
/// `env` object, or an `env` that contains none of the relevant keys,
/// hashes to a stable value (the empty selection) — two such writes in a
/// row therefore compare equal and correctly skip the reload.
///
/// The hash is order-independent: we collect `(key, value)` pairs for the
/// present relevant keys, sort them, and feed them to the hasher. This
/// makes the result robust to JSON key-ordering differences between the
/// writer and any re-serialisation.
fn hash_mcp_env(settings_json: &str) -> Option<u64> {
    let parsed: serde_json::Value = serde_json::from_str(settings_json).ok()?;
    let env = parsed.get("env").and_then(|e| e.as_object());

    let mut pairs: Vec<(&str, String)> = Vec::new();
    if let Some(env) = env {
        for &key in MCP_RELEVANT_ENV_KEYS {
            if let Some(val) = env.get(key) {
                // Stringify the value canonically. `env` values are almost
                // always JSON strings, but stringify defensively so a
                // number/bool value also hashes stably.
                let val_str = match val {
                    serde_json::Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                pairs.push((key, val_str));
            }
        }
    }
    pairs.sort();

    let mut hasher = DefaultHasher::new();
    for (k, v) in &pairs {
        k.hash(&mut hasher);
        v.hash(&mut hasher);
    }
    Some(hasher.finish())
}

/// Pure decision function: should the watcher fire an MCP reload for a
/// `.claude/settings.json` write, given the last-seen MCP-env hash for
/// this project and the CURRENT file body?
///
/// DENYLIST / fail-safe semantics:
///   * `old_hash == None` (no baseline yet)               → `true`  (fail-open reload).
///   * current body unparseable (`hash_mcp_env` → None)   → `true`  (fail-open reload).
///   * hash CHANGED vs baseline                            → `true`  (reload).
///   * hash UNCHANGED vs baseline                          → `false` (SKIP — the fix).
///
/// Extracted as a free function (no Tauri / DB / FS state) so it is unit
/// testable without a running launcher. The caller is responsible for
/// updating its per-project cache with `hash_mcp_env(new_settings_json)`
/// whenever this returns `true` (or whenever the file first becomes
/// readable) — see `fire_reload`.
fn mcp_env_changed(old_hash: Option<u64>, new_settings_json: &str) -> bool {
    match (old_hash, hash_mcp_env(new_settings_json)) {
        // Unparseable current file → fail-open (can't prove irrelevance).
        (_, None) => true,
        // No baseline → fail-open (first event / cache miss).
        (None, Some(_)) => true,
        // Baseline present → reload iff the relevant-env hash moved.
        (Some(prev), Some(now)) => prev != now,
    }
}

/// Per-process state shared between the file-watcher's blocking
/// callback thread and the tokio task that schedules debounced reloads.
struct WatchState {
    /// Timestamp of the most recent settings.json modify event. The
    /// debounce task checks this and fires SIGHUP iff at least
    /// `DEBOUNCE_WINDOW` has elapsed since the last update.
    last_event: Mutex<Option<Instant>>,
    /// Set of project dirs we're currently watching, for incremental
    /// add/remove logic on project-list changes.
    watched: Mutex<HashSet<PathBuf>>,
    /// v0.2.72 (C-P8) diff-guard cache: per settings.json path, the hash
    /// of the MCP-relevant env subset as of the LAST time we fired (or
    /// seeded a baseline for) this file. Keyed by the canonical
    /// `.claude/settings.json` PathBuf from the modify event. A cache miss
    /// (first-ever event for a path) fails OPEN — see `mcp_env_changed`.
    last_mcp_env_hash: Mutex<HashMap<PathBuf, u64>>,
}

impl WatchState {
    fn new() -> Self {
        Self {
            last_event: Mutex::new(None),
            watched: Mutex::new(HashSet::new()),
            last_mcp_env_hash: Mutex::new(HashMap::new()),
        }
    }
}

/// Spawn the settings.json watcher in the background.
///
/// Idempotent at the API level — `lib.rs::run()` calls this once per
/// launcher process. Soft-fails on `notify` init errors (logs +
/// returns); the manual "Reload MCPs" button stays as the fallback.
pub fn spawn<R: Runtime + 'static>(app: AppHandle<R>) {
    // Windows skip: the OS-level file watcher works, but the
    // downstream `reload_mcps_with` call will short-circuit because
    // `kill -HUP` is POSIX-only. No point burning a task on a no-op.
    if cfg!(windows) {
        eprintln!(
            "[settings_json_watcher] skipped on Windows (SIGHUP is POSIX-only); \
             use the manual 'Reload MCPs' button or restart your Claude Code session"
        );
        return;
    }

    tauri::async_runtime::spawn(async move {
        if let Err(e) = run_loop(app).await {
            eprintln!("[settings_json_watcher] loop exited: {} (manual reload still available)", e);
        }
    });
}

/// Watcher main loop. Holds the `notify::RecommendedWatcher` for the
/// task's lifetime (dropping it tears down the OS-level watch).
async fn run_loop<R: Runtime + 'static>(app: AppHandle<R>) -> Result<(), String> {
    let state = Arc::new(WatchState::new());

    // Channel that the notify callback writes Modify events into.
    // We use std::sync::mpsc because notify's callback is a `Fn`
    // closure executed on a non-tokio thread; the receiver side runs
    // on a tokio task that bridges into the debounce logic.
    let (tx, rx) = std::sync::mpsc::channel::<notify::Result<Event>>();

    let mut watcher: RecommendedWatcher = notify::recommended_watcher(move |res| {
        // Soft-fail: a send error means the receiver is gone, which
        // means we're shutting down. Drop silently.
        let _ = tx.send(res);
    })
    .map_err(|e| format!("notify::recommended_watcher: {}", e))?;

    // Initial project list sync. Failure here is non-fatal — we'll
    // retry on the next 30 s tick.
    if let Err(e) = sync_watches(&app, &mut watcher, &state).await {
        eprintln!("[settings_json_watcher] initial sync failed: {} (will retry)", e);
    }

    // Bridge: a blocking-thread receiver that pumps events into a
    // tokio-friendly channel. We can't directly `await rx.recv()`
    // because std::sync::mpsc::Receiver is sync-only.
    let (evt_tx, mut evt_rx) = tokio::sync::mpsc::unbounded_channel::<Event>();
    std::thread::spawn(move || {
        while let Ok(res) = rx.recv() {
            if let Ok(event) = res {
                if evt_tx.send(event).is_err() {
                    break; // tokio receiver dropped — we're shutting down
                }
            }
            // notify errors are swallowed — the watcher would log them
            // on its own backend, and a single FS-level error shouldn't
            // take down the whole watcher loop.
        }
    });

    let mut re_poll_deadline = Instant::now() + PROJECT_LIST_RE_POLL;
    loop {
        // Race three things:
        //   1. New filesystem event from notify (most frequent).
        //   2. The 30 s project-list re-poll tick (re-sync watches).
        //   3. (No explicit shutdown signal — the launcher exits will
        //      drop the AppHandle and the task naturally terminates
        //      when the next operation fails.)
        let now = Instant::now();
        let until_re_poll = re_poll_deadline.saturating_duration_since(now);

        tokio::select! {
            maybe_evt = evt_rx.recv() => {
                let Some(event) = maybe_evt else { break }; // sender dropped
                if !is_modify(&event) {
                    continue;
                }
                // Extract the concrete `.claude/settings.json` path from the
                // event so the diff-guard can read THAT file at fire-time and
                // key its per-project hash cache on it. `None` here means the
                // event touched only sibling files → not our concern.
                let Some(settings_path) = first_settings_json_path(&event.paths) else {
                    continue;
                };
                schedule_debounced_reload(app.clone(), state.clone(), settings_path);
            }
            _ = tokio::time::sleep(until_re_poll) => {
                re_poll_deadline = Instant::now() + PROJECT_LIST_RE_POLL;
                if let Err(e) = sync_watches(&app, &mut watcher, &state).await {
                    eprintln!("[settings_json_watcher] re-sync failed: {} (will retry)", e);
                }
            }
        }
    }
    Ok(())
}

/// Sync the watcher's registered paths against the current project
/// list. Adds new project parent dirs, removes stale ones.
async fn sync_watches<R: Runtime + 'static>(
    app: &AppHandle<R>,
    watcher: &mut RecommendedWatcher,
    state: &WatchState,
) -> Result<(), String> {
    let db = app
        .try_state::<Db>()
        .ok_or_else(|| "launcher.db not available".to_string())?;
    let projects = db.list_projects().map_err(|e| format!("list_projects: {}", e))?;

    let desired: HashSet<PathBuf> = projects
        .iter()
        .map(|p| settings_json_dir_for(&p.folder_path))
        .filter(|p| p.is_dir())
        .collect();

    let mut watched = state.watched.lock().await;

    // Add new.
    for dir in desired.difference(&watched) {
        if let Err(e) = watcher.watch(dir, RecursiveMode::NonRecursive) {
            eprintln!(
                "[settings_json_watcher] watch({}) failed: {} (skipping)",
                dir.display(),
                e
            );
            continue;
        }
    }
    // Remove gone.
    for dir in watched.difference(&desired).cloned().collect::<Vec<_>>() {
        let _ = watcher.unwatch(&dir);
    }
    *watched = desired;
    Ok(())
}

/// `<project>/.claude/` — the parent dir of settings.json. We watch
/// the parent because some editors swap files via rename (which
/// `notify` reports as Create+Remove on the watched parent rather than
/// Modify on the watched file).
fn settings_json_dir_for(project_folder: &str) -> PathBuf {
    Path::new(project_folder).join(".claude")
}

/// True when the event represents a write/touch on a file. We
/// deliberately do NOT match Create/Remove/Rename here — those fire
/// during editor swap-saves and would cause spurious reloads if the
/// editor writes a backup file or a temp file in the same dir.
fn is_modify(event: &Event) -> bool {
    matches!(event.kind, EventKind::Modify(_))
}

/// True when at least one path in the event is `.claude/settings.json`.
/// We're watching the `.claude/` parent dir, so we'll also see events
/// for sibling files (hooks/*.sh, agents/*.md, etc.) — those must NOT
/// trigger an MCP reload.
/// Boolean predicate form of `first_settings_json_path`. Retained for the
/// existing predicate tests; the hot path now uses
/// `first_settings_json_path` (it needs the concrete path for the
/// diff-guard cache key), so this is test-only.
#[cfg(test)]
fn any_path_is_settings_json(paths: &[PathBuf]) -> bool {
    first_settings_json_path(paths).is_some()
}

/// Return the first path in the event that is a `.claude/settings.json`
/// file, cloned. Used by the diff-guard to know WHICH project's
/// settings.json to read + hash at fire-time (the per-project cache is
/// keyed on this path). Returns `None` when no path in the event is the
/// target file (sibling writes → ignored).
fn first_settings_json_path(paths: &[PathBuf]) -> Option<PathBuf> {
    paths
        .iter()
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n == "settings.json")
                .unwrap_or(false)
        })
        .cloned()
}

/// Schedule a debounced reload. Updates `last_event` and spawns a
/// task that sleeps `DEBOUNCE_WINDOW` then checks whether more events
/// arrived in the meantime; only the LAST scheduled task actually
/// fires SIGHUP. (The earlier tasks see a newer `last_event` and bail.)
///
/// This is the lowest-cost debounce pattern that doesn't require a
/// JoinHandle accounting scheme — overlapping tasks are cheap (one
/// sleep + one timestamp compare each).
fn schedule_debounced_reload<R: Runtime + 'static>(
    app: AppHandle<R>,
    state: Arc<WatchState>,
    settings_path: PathBuf,
) {
    let scheduled_at = Instant::now();
    let state_for_task = state.clone();
    tauri::async_runtime::spawn(async move {
        {
            let mut last = state.last_event.lock().await;
            *last = Some(scheduled_at);
        }
        tokio::time::sleep(DEBOUNCE_WINDOW).await;
        // If a newer event arrived during our sleep, that newer event's
        // task will handle the reload. Bail.
        {
            let last = state_for_task.last_event.lock().await;
            if *last != Some(scheduled_at) {
                return;
            }
        }
        // We're the most-recent-scheduled task and the debounce window
        // elapsed. Consult the diff-guard, then maybe fire the reload.
        fire_reload(app, state_for_task, settings_path).await;
    });
}

/// Invoke the same `reload_mcps_with` core that the Tauri command
/// uses. We DON'T call the Tauri command directly because:
///   * we don't have a `State<Db>` handy outside an `#[command]` fn,
///   * we want the watcher's audit log entry shape to be distinct so
///     forensics can tell auto-reload events apart from manual ones.
///
/// v0.2.72 (C-P8) diff-guard: BEFORE signalling, read the CURRENT
/// settings.json for `settings_path`, hash its MCP-relevant env subset,
/// and compare to the cached hash for this path. Skip the SIGHUP when the
/// hash is unchanged (idempotent write → no MCP-relevant change). Fail
/// OPEN when there is no baseline or the file is unreadable/unparseable.
///
// TODO(v0.2.72 integrator): also fire a guarded reload from MCP-relevant
// DB-write commands (hub-precedence F5). When the launcher writes a
// per-project config change through vct-hub / launcher.db (Identity tab,
// embedding selector, access-matrix edits) WITHOUT going through the
// settings.json surface, the running MCP still holds stale env until the
// next settings.json write. The clean fix is to have those DB-write
// commands call a guarded reload too — but they live in other command
// modules (projects_v2 / mcp_registration / the hub client) and the diff
// baseline for that path is the resolved ProjectConfig, not settings.json.
// That's a SEPARATE, larger change; scope it deliberately, don't
// half-build it here.
async fn fire_reload<R: Runtime + 'static>(
    app: AppHandle<R>,
    state: Arc<WatchState>,
    settings_path: PathBuf,
) {
    // --- Diff-guard: decide whether this write is MCP-relevant. ---
    // Read the file that triggered us. A read error is treated as "no
    // proof of irrelevance" → fail open (reload) via `mcp_env_changed`'s
    // unparseable/None branch (we pass an empty body, which parses to
    // None → true). We do NOT read on a blocking pool: settings.json is a
    // few KB and this runs at most once per debounce window.
    let current_body = std::fs::read_to_string(&settings_path).unwrap_or_default();

    let prev_hash = {
        let cache = state.last_mcp_env_hash.lock().await;
        cache.get(&settings_path).copied()
    };

    let should_reload = mcp_env_changed(prev_hash, &current_body);

    // Update the cache with the current hash whenever we can compute one.
    // We seed/refresh on EVERY fire decision (reload or skip) so the next
    // event compares against the freshest baseline. A `None` (unparseable)
    // leaves the previous baseline untouched — the next readable write
    // then compares against it.
    if let Some(now_hash) = hash_mcp_env(&current_body) {
        let mut cache = state.last_mcp_env_hash.lock().await;
        cache.insert(settings_path.clone(), now_hash);
    }

    if !should_reload {
        eprintln!(
            "[settings_json_watcher] skip auto-reload: MCP-relevant env unchanged for {} \
             (idempotent settings.json write — no SIGHUP)",
            settings_path.display()
        );
        // Audit the SKIP too, so forensics can see the diff-guard working.
        if let Some(db) = app.try_state::<Db>() {
            let _ = db.audit(
                "settings_json_watcher_auto_reload_skipped",
                None,
                None,
                &serde_json::json!({
                    "settings_path": settings_path.display().to_string(),
                    "reason": "mcp_env_unchanged",
                }),
            );
        }
        return;
    }

    // Run the pgrep/kill on a blocking thread — the system calls are
    // synchronous and short, but routing through `spawn_blocking` keeps
    // the tokio worker pool free for other tasks.
    let report = tokio::task::spawn_blocking(maintenance::reload_mcps_via_shell_for_watcher)
        .await
        .unwrap_or_else(|e| {
            eprintln!("[settings_json_watcher] reload task join failed: {}", e);
            maintenance::ReloadReport {
                signaled_count: 0,
                pids: Vec::new(),
                errors: vec![format!("watcher join error: {}", e)],
                posix_only_skipped: false,
            }
        });

    if !report.errors.is_empty() {
        eprintln!(
            "[settings_json_watcher] reload completed with errors: {:?}",
            report.errors
        );
    }
    if report.signaled_count > 0 {
        eprintln!(
            "[settings_json_watcher] auto-reload: signaled {} MCP process(es) [{:?}]",
            report.signaled_count, report.pids
        );
    }

    // Audit log via the DB if available. Soft-fail.
    if let Some(db) = app.try_state::<Db>() {
        let _ = db.audit(
            "settings_json_watcher_auto_reload",
            None,
            None,
            &serde_json::json!({
                "signaled_count": report.signaled_count,
                "pids": report.pids,
                "errors": report.errors,
            }),
        );
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use notify::event::{CreateKind, DataChange, ModifyKind};

    /// Construct a Modify event with the given paths. Uses the
    /// `Event::new(kind).add_path(p)` builder so we don't depend on
    /// `EventAttributes` visibility, which has varied across notify
    /// versions.
    fn modify_event(paths: Vec<PathBuf>) -> Event {
        let mut e = Event::new(EventKind::Modify(ModifyKind::Data(DataChange::Any)));
        for p in paths {
            e = e.add_path(p);
        }
        e
    }

    fn create_event(paths: Vec<PathBuf>) -> Event {
        let mut e = Event::new(EventKind::Create(CreateKind::File));
        for p in paths {
            e = e.add_path(p);
        }
        e
    }

    #[test]
    fn settings_json_dir_for_appends_claude_dir() {
        let p = settings_json_dir_for("/home/foo/proj");
        assert_eq!(p, PathBuf::from("/home/foo/proj/.claude"));
    }

    #[test]
    fn any_path_is_settings_json_matches_target_file() {
        assert!(any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/settings.json"
        )]));
    }

    #[test]
    fn any_path_is_settings_json_ignores_siblings() {
        // Sibling files in .claude/ MUST NOT trigger reload — we'd
        // re-spawn MCPs every time a hook script or agent .md was
        // edited otherwise.
        assert!(!any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/hooks/post-edit.sh"
        )]));
        assert!(!any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/agents/architect.md"
        )]));
    }

    #[test]
    fn any_path_is_settings_json_handles_empty_paths() {
        assert!(!any_path_is_settings_json(&[]));
    }

    #[test]
    fn is_modify_accepts_modify_events_only() {
        assert!(is_modify(&modify_event(vec![PathBuf::from(
            "/x/.claude/settings.json"
        )])));
        // Create events are NOT modify — editors creating temp files
        // during atomic save should not trigger reload directly.
        assert!(!is_modify(&create_event(vec![PathBuf::from(
            "/x/.claude/settings.json.tmp"
        )])));
    }

    #[test]
    fn first_settings_json_path_returns_the_target() {
        let paths = vec![
            PathBuf::from("/p/.claude/hooks/x.sh"),
            PathBuf::from("/p/.claude/settings.json"),
        ];
        assert_eq!(
            first_settings_json_path(&paths),
            Some(PathBuf::from("/p/.claude/settings.json"))
        );
    }

    #[test]
    fn first_settings_json_path_none_for_siblings_only() {
        let paths = vec![PathBuf::from("/p/.claude/agents/architect.md")];
        assert_eq!(first_settings_json_path(&paths), None);
    }

    // ─── v0.2.72 (C-P8) diff-guard decision tests ───────────────────────
    //
    // These exercise the PURE `mcp_env_changed` / `hash_mcp_env` core
    // without a running Tauri app, DB, or file watcher. They are the
    // load-bearing tests for "idempotent settings.json write must NOT
    // SIGHUP the MCP".

    /// A minimal settings.json with an `env` block containing the given
    /// key/value pairs plus a non-MCP setting, so we can flip either
    /// independently.
    fn settings_with_env(pairs: &[(&str, &str)], extra_non_env: &str) -> String {
        let env_body: String = pairs
            .iter()
            .map(|(k, v)| format!("    {:?}: {:?}", k, v))
            .collect::<Vec<_>>()
            .join(",\n");
        format!(
            "{{\n  \"env\": {{\n{}\n  }},\n  \"_note\": {:?}\n}}",
            env_body, extra_non_env
        )
    }

    #[test]
    fn mcp_env_changed_byte_identical_no_reload() {
        // Same MCP env, byte-identical body → the whole fix: no reload.
        let body = settings_with_env(&[("KG_COLLECTION", "MyKG")], "stable");
        let baseline = hash_mcp_env(&body);
        assert!(baseline.is_some());
        assert!(
            !mcp_env_changed(baseline, &body),
            "idempotent settings.json write must not trigger a reload"
        );
    }

    #[test]
    fn mcp_env_changed_relevant_key_changed_reloads() {
        // KG_COLLECTION is MCP-relevant → its change must reload.
        let before = settings_with_env(&[("KG_COLLECTION", "MyKG")], "stable");
        let after = settings_with_env(&[("KG_COLLECTION", "OtherKG")], "stable");
        let baseline = hash_mcp_env(&before);
        assert!(
            mcp_env_changed(baseline, &after),
            "changing an MCP-relevant env key must trigger a reload"
        );
    }

    #[test]
    fn mcp_env_changed_irrelevant_key_changed_no_reload() {
        // Only a NON-MCP key (`_note`, not in MCP_RELEVANT_ENV_KEYS, and
        // not even under `env`) changed → hash of the relevant subset is
        // stable → no reload. This is the false-positive the diff-guard
        // exists to suppress.
        let before = settings_with_env(&[("KG_COLLECTION", "MyKG")], "note-A");
        let after = settings_with_env(&[("KG_COLLECTION", "MyKG")], "note-B");
        let baseline = hash_mcp_env(&before);
        assert!(
            !mcp_env_changed(baseline, &after),
            "changing an MCP-IRRELEVANT setting must not trigger a reload"
        );
    }

    #[test]
    fn mcp_env_changed_irrelevant_env_key_changed_no_reload() {
        // A key that lives under `env` but is NOT MCP-relevant
        // (GITHUB_TOKEN is deliberately excluded) → no reload.
        let before = settings_with_env(
            &[("KG_COLLECTION", "MyKG"), ("GITHUB_TOKEN", "ghp_aaa")],
            "stable",
        );
        let after = settings_with_env(
            &[("KG_COLLECTION", "MyKG"), ("GITHUB_TOKEN", "ghp_bbb")],
            "stable",
        );
        let baseline = hash_mcp_env(&before);
        assert!(
            !mcp_env_changed(baseline, &after),
            "changing an env key outside the MCP-relevant denylist must not reload"
        );
    }

    #[test]
    fn mcp_env_changed_no_baseline_fails_open() {
        // First-ever event for a path (cache miss) → fail-open reload.
        let body = settings_with_env(&[("KG_COLLECTION", "MyKG")], "stable");
        assert!(
            mcp_env_changed(None, &body),
            "no baseline must fail OPEN (reload + seed the cache)"
        );
    }

    #[test]
    fn mcp_env_changed_unparseable_current_fails_open() {
        // A truncated / mid-write / corrupted settings.json → fail-open.
        assert!(
            mcp_env_changed(Some(12345), "not-valid-json{{{"),
            "unparseable current settings.json must fail OPEN (reload)"
        );
    }

    #[test]
    fn hash_mcp_env_is_key_order_independent() {
        // The SAME relevant pairs in a different JSON key order must hash
        // equal — robust to writer/re-serialiser ordering differences.
        let a = "{\n  \"env\": {\n    \"KG_COLLECTION\": \"K\",\n    \"WEAVIATE_URL\": \"http://x\"\n  }\n}";
        let b = "{\n  \"env\": {\n    \"WEAVIATE_URL\": \"http://x\",\n    \"KG_COLLECTION\": \"K\"\n  }\n}";
        assert_eq!(hash_mcp_env(a), hash_mcp_env(b));
        assert!(hash_mcp_env(a).is_some());
    }

    #[test]
    fn hash_mcp_env_missing_env_block_is_stable() {
        // No `env` object at all → stable empty-selection hash; two such
        // writes compare equal (no reload).
        let a = "{ \"hooks\": {} }";
        let b = "{ \"hooks\": { \"Stop\": [] } }";
        assert_eq!(hash_mcp_env(a), hash_mcp_env(b));
        assert!(!mcp_env_changed(hash_mcp_env(a), b));
    }

    #[test]
    fn hash_mcp_env_unparseable_is_none() {
        assert_eq!(hash_mcp_env("garbage{{"), None);
        assert_eq!(hash_mcp_env(""), None);
    }
}
