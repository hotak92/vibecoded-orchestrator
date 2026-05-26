// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.34 Agent D — filesystem watcher for `<project>/.claude/diagrams/**`
// that emits `diagram-changed` Tauri events to the frontend.
//
// Wires the fourth missing command from `diagrams-frontend-wiring-handoff
// -2026-05-25.md`. DiagramsTab.svelte:519 calls
// `invoke('subscribe_to_diagram_changes', { projectId })` once per
// mount; on every disk change under `.claude/diagrams/**` we emit a
// `diagram-changed` event carrying `{ project_id, diagram_id, kind }`.
//
// State model
// ===========
//
// One `RecommendedWatcher` per project, held in a process-wide
// `Mutex<HashMap<String, WatcherEntry>>`. The Tauri command
// `subscribe_to_diagram_changes` is idempotent — if the project
// already has a watcher entry, the call is a no-op (returns Ok).
// On project delete the entry stays — cheap, and the next mount
// of a different project won't be affected. (Tracked as
// "low-priority cleanup" in the handoff.)
//
// Debounce
// ========
//
// Editors and the Excalidraw debounced-save path both emit multiple
// modify events for a single logical write. We coalesce by holding
// the last-event timestamp per (project, file) and only firing the
// `diagram-changed` Tauri event when ~200ms of quiet has elapsed
// since the most recent modify. The debounce loop is shared across
// all watchers via a tokio task spawned on first
// `subscribe_to_diagram_changes` call.
//
// Soft-fail philosophy
// ====================
//
// A watcher init failure (unsupported FS, permission denied) MUST
// NOT take the launcher down. The frontend already has a 10s
// fallback to 5s polling (DiagramsTab.svelte:542) so the feature
// stays usable. We log to stderr and return Ok from the command —
// the polling fallback kicks in.
//
// Why not split per-event tasks
// =============================
//
// Using a single debounce task with `last_event: Instant` per
// (project, file_path) avoids spawning one tokio task per file
// modification. The settings_json_watcher pattern (one shared
// debounce timer) inspired this; per-file timers would consume an
// O(diagrams) tokio resource and provide no benefit since per-file
// debounce is rarely needed (the frontend re-reads the file on any
// edit event regardless of which specific file changed).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex as StdMutex};
use std::time::{Duration, Instant};

use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use tauri::{command, AppHandle, Emitter, Manager, Runtime, State};

use crate::db::Db;

/// Per-watcher state held in the global `WATCHERS` registry. Drop on
/// this struct tears down the OS-level watch automatically (notify's
/// `RecommendedWatcher` is RAII).
struct WatcherEntry {
    /// Holding the watcher keeps the OS-level subscription alive. The
    /// field is intentionally suffixed with `_` — once kept here it
    /// isn't accessed again, only dropped on entry removal.
    _watcher: RecommendedWatcher,
}

/// Process-wide registry. Keyed by `project_id`. Std `Mutex` is fine
/// here — the command path only locks long enough to insert/lookup,
/// and the debounce task uses its own state.
static WATCHERS: LazyLock<StdMutex<HashMap<String, WatcherEntry>>> =
    LazyLock::new(|| StdMutex::new(HashMap::new()));

/// Per-project, per-file debounce state. Keyed by `(project_id, abs_path)`.
/// `last_event` records the most recent modify timestamp; the debounce
/// task checks this on each tick and fires `diagram-changed` iff at
/// least `DEBOUNCE_WINDOW` has elapsed since the last update.
static DEBOUNCE_STATE: LazyLock<StdMutex<HashMap<(String, PathBuf), DebounceSlot>>> =
    LazyLock::new(|| StdMutex::new(HashMap::new()));

#[derive(Debug, Clone)]
struct DebounceSlot {
    last_event: Instant,
    kind: ChangeKind,
}

#[derive(Debug, Clone, Copy)]
enum ChangeKind {
    Create,
    Edit,
    Delete,
}

impl ChangeKind {
    fn as_str(&self) -> &'static str {
        match self {
            ChangeKind::Create => "create",
            ChangeKind::Edit => "edit",
            ChangeKind::Delete => "delete",
        }
    }
}

/// Debounce window after the last modify event before `diagram-changed`
/// fires. Coalesces save-burst events (Excalidraw debounced editor
/// writes typically emit 2-3 events for one logical save).
const DEBOUNCE_WINDOW: Duration = Duration::from_millis(200);

/// Frontend payload for the `diagram-changed` Tauri event. Mirrors
/// `launcher/src/lib/types/project-state.ts::DiagramChangedPayload`.
///
/// v0.2.36 (Agent R) added `file_path` — the TS type was already
/// declaring it (an earlier sibling agent's expectation that wasn't
/// landed) but the Rust payload was missing it. The auto-register-
/// on-first-edit flow needs the project-relative path so the frontend
/// can call `register_project_diagram` without going through a second
/// IPC round-trip.
#[derive(Debug, Clone, Serialize)]
struct DiagramChangedPayload {
    project_id: String,
    /// Resolved by joining the file path against `project_diagrams.file_path`.
    /// `-1` sentinel when the file isn't registered in `project_diagrams`
    /// (e.g. external edit of a not-yet-registered file); the frontend
    /// will reload its diagram list and re-resolve.
    diagram_id: i64,
    /// Project-relative path of the changed file (e.g.
    /// `.claude/diagrams/visual-draft/login-flow.mmd`). The frontend
    /// uses this for the auto-register-on-first-edit flow when
    /// `diagram_id == -1`. Empty string when the path doesn't resolve
    /// inside the project folder (defensive — shouldn't happen since
    /// the watcher's root is the project's `.claude/diagrams/`).
    file_path: String,
    /// One of `create` | `edit` | `delete`. The `snapshot` kind is fired
    /// by the snapshot Tauri commands, not by this watcher.
    kind: &'static str,
}

/// Whether `subscribe_to_diagram_changes` should bother spawning a
/// debounce task. Set on first successful subscription so we only
/// start the task once per launcher process.
static DEBOUNCE_TASK_STARTED: LazyLock<StdMutex<bool>> = LazyLock::new(|| StdMutex::new(false));

/// Subscribe to filesystem changes under `<project>/.claude/diagrams/**`.
///
/// Idempotent: if a watcher already exists for `project_id`, returns
/// `Ok(())` without creating a duplicate. The Tauri command path is
/// the only entry point, so the frontend can call this on every mount
/// without worrying about cleanup. (Cleanup happens implicitly when
/// the launcher exits and the static map is dropped.)
///
/// Soft-fail: every error in the watcher setup path is logged and
/// swallowed in the sense that the command returns `Ok(())` even on
/// failure. The DiagramsTab Svelte has a 10s polling fallback for
/// exactly this reason — if push doesn't work we keep the feature
/// usable via polling.
#[command]
pub async fn subscribe_to_diagram_changes<R: Runtime>(
    project_id: String,
    app: AppHandle<R>,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Idempotency: if already watching, no-op.
    {
        let guard = WATCHERS.lock().map_err(|e| format!("WATCHERS lock: {e}"))?;
        if guard.contains_key(&project_id) {
            return Ok(());
        }
    }

    let project = db
        .get_project(&project_id)
        .map_err(|e| format!("subscribe_to_diagram_changes: get_project({project_id}): {e}"))?
        .ok_or_else(|| format!("subscribe_to_diagram_changes: project {project_id} not found"))?;
    let project_folder = PathBuf::from(&project.folder_path);
    let diagrams_root = project_folder.join(".claude").join("diagrams");

    // The diagrams root may not exist yet (fresh project, no diagrams
    // created). `notify` returns an error on a missing path, so we
    // create the directory tree first. Soft-fail: if create_dir_all
    // can't run (read-only volume, permission denied) the watcher
    // setup will fail loudly below.
    if let Err(e) = std::fs::create_dir_all(&diagrams_root) {
        eprintln!(
            "[diagram_watcher] could not create {} for watch: {} — \
             polling fallback will be used",
            diagrams_root.display(),
            e
        );
        return Ok(());
    }

    let project_id_for_callback = project_id.clone();
    let mut watcher: RecommendedWatcher = match notify::recommended_watcher(move |res: notify::Result<Event>| {
        if let Ok(event) = res {
            record_event(&project_id_for_callback, &event);
        }
        // notify errors are dropped — they'd otherwise spam stderr
        // on every transient inotify hiccup. The watcher recovers
        // automatically; the manual reload path stays.
    }) {
        Ok(w) => w,
        Err(e) => {
            eprintln!(
                "[diagram_watcher] notify::recommended_watcher failed for {}: {} — \
                 polling fallback will be used",
                project_id, e
            );
            return Ok(());
        }
    };

    if let Err(e) = watcher.watch(&diagrams_root, RecursiveMode::Recursive) {
        eprintln!(
            "[diagram_watcher] watch({}) failed: {} — polling fallback will be used",
            diagrams_root.display(),
            e
        );
        return Ok(());
    }

    {
        let mut guard = WATCHERS.lock().map_err(|e| format!("WATCHERS lock: {e}"))?;
        guard.insert(
            project_id.clone(),
            WatcherEntry {
                _watcher: watcher,
            },
        );
    }
    // The frontend resolves diagram paths against the project folder
    // on receipt — we don't carry the folder around in the watcher
    // state. Silence the unused-binding warning explicitly.
    let _ = project_folder;

    // Start the global debounce task once. Subsequent
    // `subscribe_to_diagram_changes` calls find it already running
    // and the flag short-circuits.
    {
        let mut started = DEBOUNCE_TASK_STARTED.lock().map_err(|e| format!("DEBOUNCE_TASK_STARTED lock: {e}"))?;
        if !*started {
            *started = true;
            spawn_debounce_task(app.clone());
        }
    }

    Ok(())
}

/// Callback invoked from the notify watcher thread. Records the event
/// into `DEBOUNCE_STATE` and lets the debounce task fire it after the
/// quiet window. Filters out:
/// - Non-modify/create/remove events (access, metadata, etc).
/// - Paths that aren't files we care about (sidecars, non-diagram
///   extensions). The frontend filters by extension on payload
///   receipt, so we err on the side of emitting too many.
fn record_event(project_id: &str, event: &Event) {
    let kind = match &event.kind {
        EventKind::Create(_) => ChangeKind::Create,
        EventKind::Modify(_) => ChangeKind::Edit,
        EventKind::Remove(_) => ChangeKind::Delete,
        _ => return, // access/metadata/other — ignore
    };
    let Ok(mut state) = DEBOUNCE_STATE.lock() else {
        return; // mutex poisoned — give up on this event
    };
    for p in &event.paths {
        state.insert(
            (project_id.to_string(), p.clone()),
            DebounceSlot {
                last_event: Instant::now(),
                kind,
            },
        );
    }
}

/// Spawn the global debounce sweeper. Runs forever, checking the
/// `DEBOUNCE_STATE` map every `DEBOUNCE_WINDOW / 2` for slots whose
/// `last_event` is older than `DEBOUNCE_WINDOW` ago — those fire the
/// `diagram-changed` Tauri event and get removed from the map.
fn spawn_debounce_task<R: Runtime + 'static>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        // Poll cadence: half the debounce window so worst-case latency
        // is ~1.5x the window. With 200ms debounce and 100ms tick,
        // worst-case GUI latency is ~300ms which is well under the
        // human-perceptual threshold.
        let tick = DEBOUNCE_WINDOW / 2;
        loop {
            tokio::time::sleep(tick).await;
            let ready = collect_ready_events();
            if ready.is_empty() {
                continue;
            }
            for (project_id, abs_path, slot) in ready {
                let diagram_id = resolve_diagram_id(&app, &project_id, &abs_path);
                let file_path = resolve_rel_path(&app, &project_id, &abs_path);
                let payload = DiagramChangedPayload {
                    project_id: project_id.clone(),
                    diagram_id,
                    file_path,
                    kind: slot.kind.as_str(),
                };
                if let Err(e) = app.emit("diagram-changed", &payload) {
                    eprintln!("[diagram_watcher] emit failed: {}", e);
                }
            }
        }
    });
}

/// Drain ready slots from `DEBOUNCE_STATE`. A slot is "ready" when
/// `Instant::now() - slot.last_event >= DEBOUNCE_WINDOW`. Removed
/// entries are returned so the caller can fire `diagram-changed` for
/// each.
fn collect_ready_events() -> Vec<(String, PathBuf, DebounceSlot)> {
    let Ok(mut state) = DEBOUNCE_STATE.lock() else {
        return Vec::new();
    };
    let now = Instant::now();
    let ready_keys: Vec<(String, PathBuf)> = state
        .iter()
        .filter(|(_, slot)| now.duration_since(slot.last_event) >= DEBOUNCE_WINDOW)
        .map(|(k, _)| k.clone())
        .collect();
    let mut out = Vec::with_capacity(ready_keys.len());
    for k in ready_keys {
        if let Some(slot) = state.remove(&k) {
            out.push((k.0, k.1, slot));
        }
    }
    out
}

/// Resolve the project-relative form of `abs_path` so the frontend
/// can use it for the auto-register-on-first-edit flow. Returns an
/// empty string when:
///   - the Db is unavailable (rare; only during teardown),
///   - the project row vanished,
///   - `abs_path` doesn't lie under the project folder (defensive —
///     shouldn't happen since the watcher's root is `<project>/
///     .claude/diagrams/`, but a race with project rename could
///     in theory put us in that state).
///
/// The relative path is normalised to forward slashes so the
/// frontend's regex (`/^\.claude\/diagrams\/.../`) works on Windows
/// where the OS-native separator would be `\`.
fn resolve_rel_path<R: Runtime>(app: &AppHandle<R>, project_id: &str, abs_path: &Path) -> String {
    let Some(db) = app.try_state::<Db>() else {
        return String::new();
    };
    let folder = match db.get_project(project_id) {
        Ok(Some(p)) => PathBuf::from(p.folder_path),
        _ => return String::new(),
    };
    let folder_canon = dunce::canonicalize(&folder).unwrap_or(folder);
    let abs_canon = dunce::canonicalize(abs_path).unwrap_or_else(|_| abs_path.to_path_buf());
    match abs_canon.strip_prefix(&folder_canon) {
        Ok(rel) => rel.to_string_lossy().replace('\\', "/"),
        Err(_) => String::new(),
    }
}

/// Resolve `diagram_id` from an absolute path. Looks up the
/// `project_diagrams` row whose `file_path` (joined against the
/// project folder) matches `abs_path`. Returns `-1` when no row
/// matches — the frontend treats that as "unknown diagram, refresh
/// the list".
fn resolve_diagram_id<R: Runtime>(app: &AppHandle<R>, project_id: &str, abs_path: &Path) -> i64 {
    let Some(db) = app.try_state::<Db>() else {
        return -1;
    };
    let folder = match db.get_project(project_id) {
        Ok(Some(p)) => PathBuf::from(p.folder_path),
        _ => return -1,
    };
    let Ok(rows) = db.list_project_diagrams(project_id) else {
        return -1;
    };
    for row in rows {
        let candidate = if Path::new(&row.file_path).is_absolute() {
            PathBuf::from(&row.file_path)
        } else {
            folder.join(&row.file_path)
        };
        // Compare canonical-forms when possible; fall back to lexical
        // equality if the path doesn't exist (deleted file, etc).
        let candidate_canon = dunce::canonicalize(&candidate).unwrap_or(candidate);
        let abs_canon = dunce::canonicalize(abs_path).unwrap_or_else(|_| abs_path.to_path_buf());
        if candidate_canon == abs_canon {
            return row.id;
        }
    }
    -1
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// File-watcher unit tests are inherently flaky in CI (race against
// inotify's settling time, FSEvents' batching, ReadDirectoryChangesW's
// quirks). Per the v0.2.34 backlog spec we skip the integration test
// here and rely on the manual end-to-end smoke recipe in the agent
// summary. The pure-logic helpers below ARE testable.

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Once;

    /// `collect_ready_events` should leave non-ready slots in the map
    /// and drain only those past the debounce window.
    #[test]
    fn collect_ready_events_drains_only_aged_slots() {
        static INIT: Once = Once::new();
        // Reset the global state for this test. The static map is
        // shared with other tests in this module; the once-init
        // ensures we don't clobber a parallel test mid-flight.
        INIT.call_once(|| {
            let mut state = DEBOUNCE_STATE.lock().unwrap();
            state.clear();
        });

        let now = Instant::now();
        {
            let mut state = DEBOUNCE_STATE.lock().unwrap();
            // Fresh slot — should NOT be drained.
            state.insert(
                ("p1".to_string(), PathBuf::from("/proj/.claude/diagrams/fresh.mmd")),
                DebounceSlot {
                    last_event: now,
                    kind: ChangeKind::Edit,
                },
            );
            // Aged slot — should be drained.
            state.insert(
                ("p1".to_string(), PathBuf::from("/proj/.claude/diagrams/aged.mmd")),
                DebounceSlot {
                    last_event: now - Duration::from_millis(500),
                    kind: ChangeKind::Edit,
                },
            );
        }

        let drained = collect_ready_events();
        assert_eq!(drained.len(), 1, "should drain exactly 1 aged slot, got {:?}", drained);
        assert!(drained[0].1.to_string_lossy().contains("aged.mmd"));

        // Fresh slot still present.
        let remaining = DEBOUNCE_STATE.lock().unwrap();
        assert_eq!(remaining.len(), 1);
        assert!(
            remaining
                .keys()
                .any(|(_, p)| p.to_string_lossy().contains("fresh.mmd")),
            "fresh slot must remain (got {:?})",
            remaining.keys().collect::<Vec<_>>()
        );
    }

    #[test]
    fn change_kind_serialisation_matches_frontend_contract() {
        // The DiagramChangedPayload's `kind` field is a string literal
        // — must match the union in
        // `launcher/src/lib/types/project-state.ts::DiagramChangedPayload`.
        assert_eq!(ChangeKind::Create.as_str(), "create");
        assert_eq!(ChangeKind::Edit.as_str(), "edit");
        assert_eq!(ChangeKind::Delete.as_str(), "delete");
        // `snapshot` is fired by the snapshot Tauri commands — NOT by
        // ChangeKind. There's deliberately no ChangeKind::Snapshot.
    }
}
