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
//
// v0.2.72 (pre-gate audit F1): the diff-guard evaluation is PER-PATH, not
// last-event-only. The debounce still coalesces bursts into one surviving
// task (last-writer-wins on `last_event`), but every event's settings.json
// path is recorded in a `pending` set; at fire time the surviving task
// DRAINS the whole set and evaluates `mcp_env_changed` for EVERY drained
// path. Without this, `refresh_all_projects_env_with_db` rewriting N
// projects' settings.json inside one debounce window collapsed to a single
// decision on the LAST-written file — if THAT file's MCP-relevant subset
// was unchanged (e.g. sticky per-project ACTIVE_EMBEDDING), the SIGHUP was
// skipped and the OTHER projects' real changes never reloaded the MCP.
// One path changed → one global SIGHUP (reloads are process-wide anyway).

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
    // --- embedding selection (weaviate_mcp reads ACTIVE_EMBEDDING / EMBEDDING_MODEL /
    // EMBEDDING_SOURCE / CODE_EMBED_MODEL) ---
    "ACTIVE_EMBEDDING",
    "EMBEDDING_MODEL",
    // F-7 (v0.2.75): weaviate_mcp reads these too. EMBEDDING_SOURCE selects
    // weaviate-vs-service vectors; CODE_EMBED_MODEL names the code-embed
    // model. A user hand-editing either in settings.json env expects the
    // live MCP to pick it up — without listing them the watcher hash-matches
    // the unchanged subset and SKIPS the reload (the F-7 staleness).
    "EMBEDDING_SOURCE",
    "CODE_EMBED_MODEL",
    // --- F-7 (v0.2.75): KG retrieval tiers + hybrid tuning (weaviate_mcp's
    // hybrid_search reads all of these; CLAUDE.md advertises hand-editing
    // KG_TIER_FULL etc. in settings.json env). KG_BASE_DIR relocates where
    // the MCP writes/reads the knowledge tree. All previously missing → a
    // hand-edit silently never reloaded. ---
    "KG_BASE_DIR",
    "KG_TIER_MIN",
    "KG_TIER_SINGLE_CHUNK",
    "KG_TIER_THREE_CHUNKS",
    "KG_TIER_FULL",
    "KG_HYBRID_ALPHA",
    "KG_HYBRID_CHUNK_BUDGET",
    // v0.2.75 P3f: over-fetch multiplier (fetch N×limit from Weaviate before
    // rerank) — a hand-editable retrieval-fan-out knob, same reload class as
    // the KG_HYBRID_* / KG_TIER_* tunables above.
    "KG_OVERFETCH_MULTIPLIER",
    // --- F-7 (v0.2.75): code-graph retrieval tiers + expansion/rerank knobs
    // (weaviate_mcp's search_code_graph reads all of these). The floors are
    // already listed above (VCO_CODE_GRAPH_*_FLOOR); these are the tier
    // thresholds + expansion/sibling/truncation knobs that were still
    // missing. CODE_SIBLINGS_RANK is read as _1 / _2 (both listed — the
    // list stores concrete spellings, no wildcards). ---
    "CODE_TIER_MIN",
    "CODE_TIER_SINGLE_CHUNK",
    "CODE_TIER_THREE_CHUNKS",
    "CODE_TIER_FULL",
    "CODE_EXPANSION_LIMIT",
    "CODE_SIBLINGS_RANK_1",
    "CODE_SIBLINGS_RANK_2",
    "CODE_TRUNC_CHARS",
    // --- v0.2.72 codegraph retrieval floors (weaviate_mcp's search_code_graph
    // reads these via code_ranking.resolve_retrieval_floor /
    // resolve_post_rerank_floor). set_codegraph_floors re-projects them into
    // every project's settings.json; without listing them here the watcher
    // would hash-match an idempotent-looking write and SKIP the reload, leaving
    // the live MCP on stale floors (the F5 staleness the fold-in closes). The
    // deprecated single-floor alias is listed too (code_ranking still honours
    // it as the post-rerank gate). ---
    "VCO_CODE_GRAPH_RETRIEVAL_FLOOR",
    "VCO_CODE_GRAPH_POST_RERANK_FLOOR",
    "VCO_CODE_GRAPH_SCORE_FLOOR", // deprecated alias → post-rerank floor
    // --- dual-write / dual-log toggles ---
    // projection surface writes DUAL_EMBEDDING_WRITE_ALL_SLOTS; the MCP /
    // embedding_service read is DUAL_EMBEDDING_ENABLED — list both spellings.
    "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
    "DUAL_EMBEDDING_ENABLED",
    "DUAL_RL_LOG_ENABLED",
    // --- connection endpoints (weaviate_mcp reads WEAVIATE_URL / OLLAMA_URL /
    // OLLAMA_BASE_URL / GRPC_PORT) ---
    "WEAVIATE_URL",
    "OLLAMA_URL",
    "OLLAMA_BASE_URL", // F-7 (v0.2.75): alt Ollama endpoint spelling the MCP reads
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
    // NOTE (WP-Q item 3 / G6): RL_SERVER_URL / RL_SERVER_PORT are NOT listed
    // here on purpose. Per the H.1 contract they are NOT projected into
    // settings.json env at all — the MCP resolves the RL port live from the
    // hub's ProjectConfig.rl_server_port (module_ports SoT), so there is no
    // settings.json write to hash-gate a reload on. See
    // tests/test_f7_mcp_relevant_env_keys_coverage.py DOCUMENTED_EXCLUSIONS.
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
    /// v0.2.72 (F1) per-path pending set: every settings.json modify event
    /// inserts its path here (and bumps `last_event`). The surviving
    /// debounce task drains the WHOLE set and evaluates the diff-guard for
    /// each drained path, so a multi-project rewrite burst (e.g.
    /// `refresh_all_projects_env_with_db`) can't collapse to a single
    /// decision on the last-written file.
    pending: Mutex<HashSet<PathBuf>>,
}

impl WatchState {
    fn new() -> Self {
        Self {
            last_event: Mutex::new(None),
            watched: Mutex::new(HashSet::new()),
            last_mcp_env_hash: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashSet::new()),
        }
    }

    /// v0.2.72 (F5): forget diff-guard state for projects whose watch was
    /// just removed (project unregistered / folder gone). Without this the
    /// `last_mcp_env_hash` map (and, pathologically, the `pending` set)
    /// grows monotonically across project add/remove cycles, and a
    /// re-added project at the same path would compare against a stale
    /// baseline instead of failing open.
    async fn prune_removed_dirs(&self, removed_dirs: &[PathBuf]) {
        if removed_dirs.is_empty() {
            return;
        }
        let mut cache = self.last_mcp_env_hash.lock().await;
        let mut pending = self.pending.lock().await;
        for dir in removed_dirs {
            let settings_path = dir.join("settings.json");
            cache.remove(&settings_path);
            pending.remove(&settings_path);
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
    let removed: Vec<PathBuf> = watched.difference(&desired).cloned().collect();
    for dir in &removed {
        let _ = watcher.unwatch(dir);
    }
    *watched = desired;
    drop(watched);
    // v0.2.72 (F5): drop the removed projects' diff-guard cache entries
    // (and any not-yet-drained pending events) alongside the unwatch.
    state.prune_removed_dirs(&removed).await;
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

/// Schedule a debounced reload. Records the event's path in the pending
/// set, updates `last_event`, and spawns a task that sleeps
/// `DEBOUNCE_WINDOW` then checks whether more events arrived in the
/// meantime; only the LAST scheduled task actually fires. (The earlier
/// tasks see a newer `last_event` and bail — but their paths stay in the
/// pending set, so the surviving task still evaluates them; F1.)
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
            // F1: record the path BEFORE bumping the timestamp so that by
            // the time any task can win the debounce race, every observed
            // path is already in the set the winner will drain.
            let mut pending = state.pending.lock().await;
            pending.insert(settings_path);
        }
        {
            let mut last = state.last_event.lock().await;
            *last = Some(scheduled_at);
        }
        tokio::time::sleep(DEBOUNCE_WINDOW).await;
        // If a newer event arrived during our sleep, that newer event's
        // task will handle the reload (and drain OUR pending path). Bail.
        {
            let last = state_for_task.last_event.lock().await;
            if *last != Some(scheduled_at) {
                return;
            }
        }
        // We're the most-recent-scheduled task and the debounce window
        // elapsed. Drain the pending set, consult the diff-guard per
        // path, then maybe fire the reload.
        fire_reload(app, state_for_task).await;
    });
}

/// v0.2.72 (F1): outcome of evaluating every drained pending path against
/// its per-path diff-guard baseline.
struct DrainDecision {
    /// True when at least ONE drained path's MCP-relevant env moved (or
    /// failed open) — one global SIGHUP covers all of them.
    reload: bool,
    /// Paths whose hash moved / failed open (drove the reload).
    changed: Vec<PathBuf>,
    /// Paths whose MCP-relevant subset was provably unchanged.
    skipped: Vec<PathBuf>,
}

/// v0.2.72 (F1): per-path diff-guard evaluation over the drained pending
/// set. For EVERY drained path: read the current body (via `read_body`,
/// injected so tests don't need the filesystem), compare against that
/// path's cached baseline with `mcp_env_changed`, and refresh the cache
/// entry whenever the current body is parseable (reload or skip alike —
/// same semantics the single-path guard had; an unparseable body leaves
/// the previous baseline untouched so the next readable write compares
/// against it).
///
/// Fail-open per path is preserved: no baseline → changed; unreadable /
/// unparseable body → changed. The decision is the OR across paths — one
/// changed path is enough to reload (the SIGHUP is process-global anyway).
fn evaluate_drained_paths(
    drained: &[PathBuf],
    cache: &mut HashMap<PathBuf, u64>,
    read_body: impl Fn(&Path) -> String,
) -> DrainDecision {
    let mut decision = DrainDecision {
        reload: false,
        changed: Vec::new(),
        skipped: Vec::new(),
    };
    for path in drained {
        let body = read_body(path);
        let prev_hash = cache.get(path).copied();
        let path_changed = mcp_env_changed(prev_hash, &body);
        if let Some(now_hash) = hash_mcp_env(&body) {
            cache.insert(path.clone(), now_hash);
        }
        if path_changed {
            decision.reload = true;
            decision.changed.push(path.clone());
        } else {
            decision.skipped.push(path.clone());
        }
    }
    decision
}

/// Invoke the same `reload_mcps_with` core that the Tauri command
/// uses. We DON'T call the Tauri command directly because:
///   * we don't have a `State<Db>` handy outside an `#[command]` fn,
///   * we want the watcher's audit log entry shape to be distinct so
///     forensics can tell auto-reload events apart from manual ones.
///
/// v0.2.72 (C-P8) diff-guard + (F1) per-path evaluation: BEFORE
/// signalling, drain the pending set and, for EVERY drained settings.json
/// path, read the CURRENT file, hash its MCP-relevant env subset, and
/// compare to the cached hash for that path. Skip the SIGHUP only when
/// ALL drained paths are provably unchanged. Fail OPEN per path when
/// there is no baseline or the file is unreadable/unparseable.
///
/// (The v0.2.72 F5 fold-in — firing a guarded reload from MCP-relevant
/// DB-write commands that bypass the settings.json surface — landed as
/// env re-projection at the command layer: see
/// `projects_v2::reproject_env_soft` / `refresh_all_projects_env_with_db`
/// call sites. Those rewrites route back through THIS watcher.)
async fn fire_reload<R: Runtime + 'static>(app: AppHandle<R>, state: Arc<WatchState>) {
    // Drain everything that accumulated during the debounce window. Each
    // path is evaluated independently against its own baseline.
    let drained: Vec<PathBuf> = {
        let mut pending = state.pending.lock().await;
        pending.drain().collect()
    };
    if drained.is_empty() {
        // Nothing pending (e.g. a racing task already drained) — no-op.
        return;
    }

    // We do NOT read on a blocking pool: settings.json files are a few KB
    // each and this runs at most once per debounce window.
    let decision = {
        let mut cache = state.last_mcp_env_hash.lock().await;
        evaluate_drained_paths(&drained, &mut cache, |p| {
            std::fs::read_to_string(p).unwrap_or_default()
        })
    };

    if !decision.reload {
        eprintln!(
            "[settings_json_watcher] skip auto-reload: MCP-relevant env unchanged for {:?} \
             (idempotent settings.json write(s) — no SIGHUP)",
            decision.skipped
        );
        // Audit the SKIP too, so forensics can see the diff-guard working.
        if let Some(db) = app.try_state::<Db>() {
            let _ = db.audit(
                "settings_json_watcher_auto_reload_skipped",
                None,
                None,
                &serde_json::json!({
                    "settings_paths": decision
                        .skipped
                        .iter()
                        .map(|p| p.display().to_string())
                        .collect::<Vec<_>>(),
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
                // F1: which drained paths' MCP-relevant env actually moved
                // (the reload trigger) — forensics for multi-project bursts.
                "settings_paths_changed": decision
                    .changed
                    .iter()
                    .map(|p| p.display().to_string())
                    .collect::<Vec<_>>(),
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
    fn mcp_env_changed_f7_newly_listed_key_reloads() {
        // F-7 (v0.2.75): a hand-edit of a newly-listed key (KG_TIER_FULL —
        // CLAUDE.md advertises exactly this) MUST now trigger a reload.
        // Pre-F-7 the watcher hashed the unchanged subset and skipped it.
        let before = settings_with_env(&[("KG_TIER_FULL", "0.75")], "stable");
        let after = settings_with_env(&[("KG_TIER_FULL", "0.80")], "stable");
        let baseline = hash_mcp_env(&before);
        assert!(
            mcp_env_changed(baseline, &after),
            "editing KG_TIER_FULL (F-7 newly-listed) must trigger a reload"
        );
    }

    #[test]
    fn mcp_env_changed_f7_code_tier_and_siblings_reload() {
        // Spot-check a few more F-7 additions so a future edit that drops
        // one from the list trips here.
        for key in [
            "CODE_TIER_FULL",
            "CODE_SIBLINGS_RANK_1",
            "CODE_TRUNC_CHARS",
            "KG_HYBRID_ALPHA",
            "EMBEDDING_SOURCE",
            "OLLAMA_BASE_URL",
        ] {
            let before = settings_with_env(&[(key, "1")], "stable");
            let after = settings_with_env(&[(key, "2")], "stable");
            let baseline = hash_mcp_env(&before);
            assert!(
                mcp_env_changed(baseline, &after),
                "editing {key} (F-7 newly-listed) must trigger a reload"
            );
        }
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

    // ─── v0.2.72 (F1) per-path drain evaluation tests ────────────────────
    //
    // These pin the multi-project-burst fix: when
    // `refresh_all_projects_env_with_db` rewrites N projects' settings.json
    // inside one debounce window, the surviving debounce task must evaluate
    // EVERY pending path — not just the last-written one — and reload when
    // ANY of them changed.

    /// Build an in-memory "filesystem" for `evaluate_drained_paths`: a
    /// path → body map served through the injected reader closure. Unknown
    /// paths read as empty (→ unparseable → fail-open), matching the real
    /// `read_to_string(..).unwrap_or_default()` behaviour.
    fn body_reader(
        bodies: Vec<(PathBuf, String)>,
    ) -> impl Fn(&Path) -> String {
        move |p: &Path| {
            bodies
                .iter()
                .find(|(path, _)| path == p)
                .map(|(_, b)| b.clone())
                .unwrap_or_default()
        }
    }

    /// (a) Two paths pending; the debounce WINNER (last-written file) is
    /// unchanged but the LOSER changed → the reload must still fire. This
    /// is the exact multi-project-burst defect: pre-F1 only the winner was
    /// evaluated and the loser's real change was silently dropped.
    #[test]
    fn drained_paths_winner_unchanged_loser_changed_reloads() {
        let winner = PathBuf::from("/proj-winner/.claude/settings.json");
        let loser = PathBuf::from("/proj-loser/.claude/settings.json");

        let winner_body = settings_with_env(&[("KG_COLLECTION", "WinnerKG")], "s");
        let loser_before = settings_with_env(&[("KG_COLLECTION", "LoserKG")], "s");
        let loser_after = settings_with_env(&[("KG_COLLECTION", "LoserKG_v2")], "s");

        let mut cache = HashMap::new();
        cache.insert(winner.clone(), hash_mcp_env(&winner_body).unwrap());
        cache.insert(loser.clone(), hash_mcp_env(&loser_before).unwrap());

        // Drain order puts the winner LAST (the last-writer-wins slot) so
        // this would have been the only file evaluated pre-F1.
        let drained = vec![loser.clone(), winner.clone()];
        let decision = evaluate_drained_paths(
            &drained,
            &mut cache,
            body_reader(vec![
                (winner.clone(), winner_body),
                (loser.clone(), loser_after),
            ]),
        );

        assert!(
            decision.reload,
            "a changed loser path must fire the reload even when the \
             debounce winner is unchanged"
        );
        assert_eq!(decision.changed, vec![loser]);
        assert_eq!(decision.skipped, vec![winner]);
    }

    /// (b) Two paths pending, BOTH provably unchanged → skip (the whole
    /// point of the diff-guard survives the per-path generalisation).
    #[test]
    fn drained_paths_all_unchanged_skips() {
        let a = PathBuf::from("/proj-a/.claude/settings.json");
        let b = PathBuf::from("/proj-b/.claude/settings.json");
        let body_a = settings_with_env(&[("KG_COLLECTION", "A_KG")], "s");
        let body_b = settings_with_env(&[("KG_COLLECTION", "B_KG")], "s");

        let mut cache = HashMap::new();
        cache.insert(a.clone(), hash_mcp_env(&body_a).unwrap());
        cache.insert(b.clone(), hash_mcp_env(&body_b).unwrap());

        let decision = evaluate_drained_paths(
            &[a.clone(), b.clone()],
            &mut cache,
            body_reader(vec![(a, body_a), (b, body_b)]),
        );

        assert!(
            !decision.reload,
            "idempotent rewrites of every pending path must not SIGHUP"
        );
        assert_eq!(decision.changed.len(), 0);
        assert_eq!(decision.skipped.len(), 2);
    }

    /// (c) EVERY drained path's cache entry is refreshed after the
    /// decision — changed, unchanged, and first-seen paths alike — so the
    /// next window compares against fresh baselines. An unreadable path
    /// keeps its previous baseline (no entry clobbering with garbage).
    #[test]
    fn drained_paths_all_caches_updated_after_decision() {
        let changed = PathBuf::from("/proj-c/.claude/settings.json");
        let unchanged = PathBuf::from("/proj-u/.claude/settings.json");
        let first_seen = PathBuf::from("/proj-f/.claude/settings.json");
        let unreadable = PathBuf::from("/proj-x/.claude/settings.json");

        let changed_before = settings_with_env(&[("KG_COLLECTION", "C1")], "s");
        let changed_after = settings_with_env(&[("KG_COLLECTION", "C2")], "s");
        let unchanged_body = settings_with_env(&[("KG_COLLECTION", "U")], "s");
        let first_seen_body = settings_with_env(&[("KG_COLLECTION", "F")], "s");

        let mut cache = HashMap::new();
        cache.insert(changed.clone(), hash_mcp_env(&changed_before).unwrap());
        cache.insert(unchanged.clone(), hash_mcp_env(&unchanged_body).unwrap());
        let unreadable_baseline = 0xDEAD_BEEFu64;
        cache.insert(unreadable.clone(), unreadable_baseline);

        let decision = evaluate_drained_paths(
            &[
                changed.clone(),
                unchanged.clone(),
                first_seen.clone(),
                unreadable.clone(),
            ],
            &mut cache,
            // `unreadable` is missing from the map → reads as "" →
            // unparseable → fail-open + baseline preserved.
            body_reader(vec![
                (changed.clone(), changed_after.clone()),
                (unchanged.clone(), unchanged_body.clone()),
                (first_seen.clone(), first_seen_body.clone()),
            ]),
        );

        assert!(decision.reload, "changed + first-seen paths fail open");
        assert_eq!(
            cache.get(&changed).copied(),
            hash_mcp_env(&changed_after),
            "changed path's baseline must move to the new body"
        );
        assert_eq!(
            cache.get(&unchanged).copied(),
            hash_mcp_env(&unchanged_body),
            "unchanged path's baseline must be refreshed (same value)"
        );
        assert_eq!(
            cache.get(&first_seen).copied(),
            hash_mcp_env(&first_seen_body),
            "first-seen path must be seeded after failing open"
        );
        assert_eq!(
            cache.get(&unreadable).copied(),
            Some(unreadable_baseline),
            "unreadable path must keep its previous baseline untouched"
        );
        // Fail-open members are in `changed`.
        assert!(decision.changed.contains(&first_seen));
        assert!(decision.changed.contains(&unreadable));
    }

    /// Empty drain → no reload, nothing recorded. (fire_reload treats this
    /// as a no-op — a racing task already drained the set.)
    #[test]
    fn drained_paths_empty_is_noop() {
        let mut cache = HashMap::new();
        let decision =
            evaluate_drained_paths(&[], &mut cache, body_reader(vec![]));
        assert!(!decision.reload);
        assert!(decision.changed.is_empty());
        assert!(decision.skipped.is_empty());
    }

    // ─── v0.2.72 (F5) diff-guard cache pruning on project removal ────────

    /// When a project's watch dir is removed (`sync_watches` removal arm),
    /// its settings.json entry must vanish from BOTH the diff-guard cache
    /// and the pending set, while other projects' entries survive.
    #[tokio::test]
    async fn prune_removed_dirs_drops_cache_and_pending_entries() {
        let state = WatchState::new();
        let gone_dir = PathBuf::from("/gone-proj/.claude");
        let kept_dir = PathBuf::from("/kept-proj/.claude");
        let gone_settings = gone_dir.join("settings.json");
        let kept_settings = kept_dir.join("settings.json");

        state
            .last_mcp_env_hash
            .lock()
            .await
            .extend([(gone_settings.clone(), 1u64), (kept_settings.clone(), 2u64)]);
        state
            .pending
            .lock()
            .await
            .extend([gone_settings.clone(), kept_settings.clone()]);

        state.prune_removed_dirs(&[gone_dir]).await;

        let cache = state.last_mcp_env_hash.lock().await;
        assert!(
            !cache.contains_key(&gone_settings),
            "removed project's cache entry must be pruned"
        );
        assert!(
            cache.contains_key(&kept_settings),
            "surviving project's cache entry must be untouched"
        );
        drop(cache);
        let pending = state.pending.lock().await;
        assert!(
            !pending.contains(&gone_settings),
            "removed project's pending event must be pruned"
        );
        assert!(
            pending.contains(&kept_settings),
            "surviving project's pending event must be untouched"
        );
    }

    /// Pruning an empty removal list is a no-op (fast path — no lock churn
    /// assertions possible, but at least it must not panic or clear state).
    #[tokio::test]
    async fn prune_removed_dirs_empty_list_noop() {
        let state = WatchState::new();
        let p = PathBuf::from("/p/.claude/settings.json");
        state.last_mcp_env_hash.lock().await.insert(p.clone(), 7u64);
        state.prune_removed_dirs(&[]).await;
        assert_eq!(
            state.last_mcp_env_hash.lock().await.get(&p).copied(),
            Some(7u64)
        );
    }
}
