//! Durable install-log reader + install-health inspection.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the `state/logs/install.jsonl`
//! reader (`InstallEvent`, `InstallState`, `InstallLog`, `read_install_log`,
//! `read_install_log_from`, `derive_install_state`) and the install-health
//! inspection (`InstallHealth`, `inspect_install_health_at`,
//! `check_install_health` + helpers) that previously lived inline in
//! `installer.rs`. Behaviour is unchanged; the facade re-exports every symbol
//! (including the `read_install_log` / `check_install_health` Tauri commands)
//! so `commands::installer::*` paths + the `installer::tests` module resolve
//! them via `super::*`.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::command;

// `read_install_log` resolves the repo root via the facade's canonical
// resolver, which was NOT moved (it anchors the whole installer module).
use super::find_local_repo_root;

// ---------------------------------------------------------------------------
// Durable install log reader
//
// Both `install.py` and `post-install-launcher.sh` append events to
// `<repo_root>/state/logs/install.jsonl`. Schema lives in
// `docs/INSTALL_RECOVERY.md`. The launcher reads this so:
//  - The first-start wizard can skip steps install.py already covered.
//  - A future Settings → Install Diagnostics panel can render the timeline
//    + offer "Re-run from step X" actions.
//
// This is intentionally a PULL-only API: the FE invokes `read_install_log`
// when it wants the current state. Polling/auto-refresh is out of scope
// for v1.0 — the install log only changes during install + post-install,
// which is bounded; the wizard reads it once on mount, the diagnostics
// panel can re-read on user click.
// ---------------------------------------------------------------------------

/// One event line from `state/logs/install.jsonl`.
///
/// `data` is preserved as opaque JSON (not strongly typed) because
/// different actors emit different shapes and locking the schema in
/// Rust would force a churn cycle every time install.py adds a field.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct InstallEvent {
    pub ts: String,
    pub actor: String,
    pub step: String,
    pub phase: String, // "start" | "ok" | "skip" | "error" | "warn"
    pub detail: String,
    #[serde(default, skip_serializing_if = "is_null_value")]
    pub data: serde_json::Value,
}

pub(crate) fn is_null_value(v: &serde_json::Value) -> bool {
    v.is_null()
}

/// Derived state: which steps reached terminal phases, when the last
/// session started, and a single boolean summarising "looks complete."
#[derive(Serialize, Debug, Clone)]
pub struct InstallState {
    /// ISO-8601 timestamp of the most-recent session-start event, if any.
    pub session_started: Option<String>,
    /// step IDs that reached phase=ok in the most-recent session.
    pub completed_steps: Vec<String>,
    /// step IDs that ended at phase=skip in the most-recent session.
    pub skipped_steps: Vec<String>,
    /// (step, last error detail) pairs for steps whose last phase was "error".
    pub failed_steps: Vec<(String, String)>,
    /// ISO-8601 ts of the last event in the file (any actor).
    pub last_event_ts: Option<String>,
    /// True iff the install reached a terminal-good state: install.py
    /// session-ok seen AND post-install build/spawn either ok or skipped
    /// AND no later "error" events. Heuristic; the wizard uses it to
    /// decide whether to short-circuit or fall through to per-step
    /// verification.
    pub looks_complete: bool,
}

#[derive(Serialize, Debug, Clone)]
pub struct InstallLog {
    pub events: Vec<InstallEvent>,
    pub state_summary: InstallState,
    /// Absolute path the log was read from (for display/debug).
    pub log_path: String,
    /// True iff the file exists. False → empty events + zeroed state.
    pub exists: bool,
}

/// Tauri command: read state/logs/install.jsonl and derive a summary.
///
/// On a fresh install (no log yet) returns `exists=false` with empty
/// events so the FE can render a "no install detected" state. Returns
/// Err only if we can't even resolve the repo root — the missing log
/// file itself is a normal case, not an error.
#[command]
pub fn read_install_log() -> Result<InstallLog, String> {
    let root = find_local_repo_root()?;
    let log_path = root.join("state").join("logs").join("install.jsonl");
    Ok(read_install_log_from(&log_path))
}

/// Pure helper: read + parse the log from a specific path. Split out so
/// tests can drive it with fixture files in a tempdir without needing a
/// real `vct-module.json` repo root. Returns a structurally-valid
/// `InstallLog` even on parse errors (corrupt lines are skipped); the
/// `exists` flag distinguishes "no file" from "file present but empty".
pub fn read_install_log_from(log_path: &Path) -> InstallLog {
    let exists = log_path.is_file();
    let log_path_str = log_path.to_string_lossy().to_string();

    if !exists {
        return InstallLog {
            events: Vec::new(),
            state_summary: empty_install_state(),
            log_path: log_path_str,
            exists: false,
        };
    }

    let raw = match std::fs::read_to_string(log_path) {
        Ok(s) => s,
        Err(_) => {
            return InstallLog {
                events: Vec::new(),
                state_summary: empty_install_state(),
                log_path: log_path_str,
                exists: true,
            };
        }
    };

    let mut events: Vec<InstallEvent> = Vec::new();
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(ev) = serde_json::from_str::<InstallEvent>(line) {
            events.push(ev);
        }
        // Silently skip un-parseable lines. The log is append-only and
        // best-effort; a malformed line means a writer crashed mid-write.
        // Treating that as an error would cripple recovery on the very
        // failure modes the log was designed to capture.
    }

    let state_summary = derive_install_state(&events);

    InstallLog {
        events,
        state_summary,
        log_path: log_path_str,
        exists: true,
    }
}

pub(crate) fn empty_install_state() -> InstallState {
    InstallState {
        session_started: None,
        completed_steps: Vec::new(),
        skipped_steps: Vec::new(),
        failed_steps: Vec::new(),
        last_event_ts: None,
        looks_complete: false,
    }
}

/// Compute the derived state summary from a parsed event vector.
///
/// Logic:
///   1. Find the last "session-start" emitted by install.py (step="1/10",
///      phase="start", actor="install.py"). Events before that are an
///      older session and ignored for completed/failed.
///   2. Walk forward; for each event update a per-step latest-phase map.
///   3. Bucket into completed/skipped/failed based on the LATEST phase
///      observed for each step.
///   4. `looks_complete` = (1) install.py emitted session-ok AND (2) we
///      saw build/tauri reach ok or skip OR a binary was already located
///      (binary-probe ok at start) AND (3) no later `error` event in the
///      session.
pub(crate) fn derive_install_state(events: &[InstallEvent]) -> InstallState {
    if events.is_empty() {
        return empty_install_state();
    }

    let last_event_ts = events.last().map(|e| e.ts.clone());

    // Find the last install.py session-start (step "1/10" + phase "start"
    // OR step "session" + phase "start"). We accept either: install.py
    // emits both as part of its initial flow.
    let session_start_idx = events
        .iter()
        .enumerate()
        .filter(|(_, e)| {
            e.actor == "install.py"
                && e.phase == "start"
                && (e.step == "1/10" || e.step == "session")
        })
        .map(|(i, _)| i)
        .next_back();

    let session_start_idx = match session_start_idx {
        Some(i) => i,
        // No install.py session anchor — derive what we can from all
        // events; this happens when the only writer was the bash post-
        // install script (e.g. user re-ran post-install standalone).
        None => 0,
    };

    let session_started = events.get(session_start_idx).map(|e| e.ts.clone());

    // Track latest phase per step within the session.
    let mut latest_phase: std::collections::BTreeMap<String, (String, String)> =
        std::collections::BTreeMap::new();
    let mut session_session_ok = false;

    for ev in &events[session_start_idx..] {
        // Track the install.py "session ok" terminal marker.
        if ev.actor == "install.py" && ev.step == "session" && ev.phase == "ok" {
            session_session_ok = true;
        }
        // Skip the session anchor events themselves from the per-step
        // bucket — they're meta-events, not real install steps.
        if ev.step == "session" {
            continue;
        }
        latest_phase.insert(ev.step.clone(), (ev.phase.clone(), ev.detail.clone()));
    }

    let mut completed_steps: Vec<String> = Vec::new();
    let mut skipped_steps: Vec<String> = Vec::new();
    let mut failed_steps: Vec<(String, String)> = Vec::new();

    for (step, (phase, detail)) in &latest_phase {
        match phase.as_str() {
            "ok" => completed_steps.push(step.clone()),
            "skip" => skipped_steps.push(step.clone()),
            "error" => failed_steps.push((step.clone(), detail.clone())),
            // "start" without a terminal phase = step in progress / crashed
            // mid-step. We do NOT call this completed; surfacing it as
            // failed is more accurate for the FE.
            "start" => failed_steps.push((step.clone(), format!("interrupted: {}", detail))),
            _ => {} // "warn" + unknown phases: not in any bucket
        }
    }

    // Heuristic for `looks_complete`. We want both halves of the install
    // path: install.py's 10/10 + post-install-launcher's spawn OR a
    // pre-existing binary. The FE uses this to short-circuit the wizard,
    // but the per-step verification (file exists, service responds)
    // still runs — the log signal is necessary, not sufficient.
    let install_py_done = session_session_ok
        || latest_phase
            .get("10/10")
            .map(|(p, _)| p == "ok" || p == "warn")
            .unwrap_or(false);
    let launcher_ready = latest_phase
        .get("spawn")
        .map(|(p, _)| p == "ok")
        .unwrap_or(false)
        || latest_phase
            .get("binary-probe")
            .map(|(p, _)| p == "ok")
            .unwrap_or(false)
        || latest_phase
            .get("build/tauri")
            .map(|(p, _)| p == "ok")
            .unwrap_or(false);

    let looks_complete = install_py_done && launcher_ready && failed_steps.is_empty();

    InstallState {
        session_started,
        completed_steps,
        skipped_steps,
        failed_steps,
        last_event_ts,
        looks_complete,
    }
}

// ---------------------------------------------------------------------------
// Install health gate.
//
// Concern: when we publish a GitHub Release with the launcher .exe attached,
// users may download the .exe directly and skip first-install.{bat,sh,command}.
// The .exe alone won't have a working orchestrator behind it (no Python venv,
// no Docker/Podman containers, no MCP registration, no .env). This check runs
// once at app startup and surfaces a blocking modal when the binary is
// running from inside what should-be an install root but the install never
// actually ran.
//
// Discriminators (see `check_install_health` below):
//   - .venv/                        → Python deps installed
//   - state/                        → durable install log dir created
//   - claude_mcp_servers/.venv/     → MCP server venv installed
//   - .env with KG_COLLECTION line  → orchestrator config present
//
// Developer-mode bypass: if we cannot locate an install root by walking up
// from current_exe() (i.e. running `cargo run` / `pnpm tauri dev` from the
// launcher subdir, no install context anywhere up the tree), the FE-facing
// `all_ok` is set to true so the modal never fires for devs.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallHealth {
    /// Resolved install-root path the check was run against, if found.
    /// None means we are in developer mode (no install root up the tree).
    pub install_root: Option<String>,
    /// `.venv/` directory exists in install root.
    pub has_venv: bool,
    /// `state/` directory exists in install root.
    pub has_state_dir: bool,
    /// `.env` exists AND contains a `KG_COLLECTION` line.
    pub has_env_with_kg: bool,
    /// `claude_mcp_servers/` exists AND its `.venv/` exists.
    pub mcp_servers_ok: bool,
    /// True when every signal passes, OR when we are in developer mode
    /// (no install root found). False only when we are clearly inside an
    /// install root but the install never ran.
    pub all_ok: bool,
}

/// Read a candidate `.env` file and report whether it contains a
/// non-comment `KG_COLLECTION=` line. Used as one of the install-root
/// markers so we don't false-positive on a source-repo checkout that
/// happens to have `install.py` + `CLAUDE.md` next to a launcher binary
/// inside `dist/` or `launcher/dist/`.
///
/// 2026-04-28 fix (Bug 6): completed installs always have a `.env` with
/// `KG_COLLECTION=ProjectKnowledgeGraph` (or similar) generated by
/// `install.py`. Source checkouts ship `.env.example` but never have a
/// real `.env` with this key.
pub(crate) fn env_contains_kg(env_path: &Path) -> bool {
    let Ok(contents) = std::fs::read_to_string(env_path) else {
        return false;
    };
    contents.lines().any(|line| {
        let trimmed = line.trim_start();
        // Skip comments and blank lines. Match either bare `KG_COLLECTION=`
        // or `export KG_COLLECTION=` (some env files use shell `export`).
        !trimmed.starts_with('#')
            && (trimmed.starts_with("KG_COLLECTION=")
                || trimmed.starts_with("export KG_COLLECTION="))
    })
}

/// Predicate shared between `find_install_root_from_exe` and any other
/// caller that needs to decide "is this candidate path a real, completed
/// install or just a source-repo checkout that happens to share some
/// markers?".
///
/// A path is treated as a completed install root when:
///   1. `install.py` and `CLAUDE.md` are both present (cheap pre-check
///      that filters out unrelated directories), AND
///   2. EITHER `state/` exists as a directory (real installs always
///      create this — it holds blackboard.db, sessions.json, KG cache)
///      OR `.env` exists and contains a `KG_COLLECTION=` line (a
///      completed install configured its KG collection name).
///
/// 2026-04-28 (Bug 6 root cause): the previous predicate only checked
/// (1) — but `install.py + CLAUDE.md` are BOTH present in source-repo
/// checkouts (e.g. when the launcher binary lives in `launcher/dist/`
/// of a vco source clone). The install-health gate would then mark a
/// dev-mode launcher as "incomplete install" and fire the
/// reinstall-prompt modal. Adding the (2) check tells source checkouts
/// apart from completed installs.
pub(crate) fn is_completed_install_root(p: &Path) -> bool {
    if !(p.join("install.py").is_file() && p.join("CLAUDE.md").is_file()) {
        return false;
    }
    let has_state_dir = p.join("state").is_dir();
    let has_env_with_kg = env_contains_kg(&p.join(".env"));
    has_state_dir || has_env_with_kg
}

/// Walk up from `current_exe()` looking for an orchestrator install root.
/// Unlike `find_local_repo_root` (which keys on `vct-module.json`, present
/// in any checkout — bundled or installed), this looks for the strict
/// marker set that real installs have but source-repo checkouts don't —
/// see `is_completed_install_root` for the predicate. Returns None when
/// the binary lives outside any plausible install root (typical dev path:
/// `target/debug/launcher` from the launcher subdir, OR `launcher/dist/`
/// of a source-repo checkout).
pub(crate) fn find_install_root_from_exe() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut current = exe.parent()?.to_path_buf();
    for _ in 0..10 {
        if is_completed_install_root(&current) {
            return Some(current);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// Inspect a candidate install root and report which install signals are
/// present. Pure function over `&Path` so the unit test can drive it
/// against a tmpdir without touching the real filesystem.
pub(crate) fn inspect_install_health_at(root: &Path) -> InstallHealth {
    let has_venv = root.join(".venv").is_dir();
    let has_state_dir = root.join("state").is_dir();

    let env_path = root.join(".env");
    let has_env_with_kg = match std::fs::read_to_string(&env_path) {
        Ok(contents) => contents
            .lines()
            .any(|line| line.trim_start().starts_with("KG_COLLECTION")),
        Err(_) => false,
    };

    let mcp_dir = root.join("claude_mcp_servers");
    // Accept EITHER the modern `<root>/.venv` (post-migration default created
    // by install.py Step 4) OR the legacy `<root>/claude_mcp_servers/.venv`
    // (older installs). install.py:7656-7669 documents the modern path as
    // canonical and the legacy as fallback; _resolve_venv_python_for_install
    // (install.py:9809) tries both. The launcher's health-check must follow
    // the same contract — otherwise every fresh install on every OS shows
    // the "Installation incomplete" modal even though install.py succeeded.
    let mcp_servers_ok = mcp_dir.is_dir()
        && (has_venv || mcp_dir.join(".venv").is_dir());

    let all_ok = has_venv && has_state_dir && has_env_with_kg && mcp_servers_ok;

    InstallHealth {
        install_root: Some(root.to_string_lossy().to_string()),
        has_venv,
        has_state_dir,
        has_env_with_kg,
        mcp_servers_ok,
        all_ok,
    }
}

/// Frontend-facing entry point. Resolves the install root from the running
/// binary's location and inspects it. When no install root is found
/// (developer mode), returns `all_ok: true` so the modal never fires.
#[command]
pub fn check_install_health() -> InstallHealth {
    match find_install_root_from_exe() {
        Some(root) => inspect_install_health_at(&root),
        None => InstallHealth {
            install_root: None,
            has_venv: false,
            has_state_dir: false,
            has_env_with_kg: false,
            mcp_servers_ok: false,
            // Developer mode: no install root → don't gate.
            all_ok: true,
        },
    }
}

