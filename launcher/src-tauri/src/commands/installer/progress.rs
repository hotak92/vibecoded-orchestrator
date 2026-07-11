//! Installer progress + install-log event emitters.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the update-flow progress emitter
//! (`emit_progress`), the step→user-label mapper
//! (`installer_step_to_user_label`), the install-log appender
//! (`append_install_log_event`), and the ISO-8601 timestamp helper
//! (`chrono_iso_z`) that previously lived inline in `installer.rs`. Behaviour
//! is unchanged; the facade re-exports every symbol so the update-flow
//! orchestration (which stays in the facade) reaches them via `super::*`.

use std::path::Path;

use tauri::{Emitter, Window};

use super::InstallProgress;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

pub(crate) fn emit_progress(window: &Window, stage: &str, message: &str, percentage: f32) {
    let _ = window.emit(
        "install_progress",
        InstallProgress {
            stage: stage.to_string(),
            message: message.to_string(),
            percentage,
            error: None,
        },
    );
}

/// v0.2.49 batch 4 (sub-progress label): map an install.py `_log_
/// install_event` step tag (e.g. `"7c/10"`, `"7d/10"`) to a short
/// user-facing label for the OrchestratorUpdateProgressModal.
///
/// install.py emits events with two-character step tags like `"7/10"`,
/// `"7b/10"`, `"7c/10"`. The numeric prefix identifies the install
/// stage; sub-tags (`b`, `c`, `d`, `e`) are micro-steps within a
/// stage. We map the most user-visible ones (long-running phases the
/// user otherwise sees as a frozen progress bar) to short labels.
///
/// Returns an empty string for unrecognized steps — the caller treats
/// this as "don't surface a sub-progress for this event," keeping the
/// previous message in place.
pub(crate) fn installer_step_to_user_label(step: &str, detail: &str) -> String {
    // Step prefix is the part before the slash. Sub-tag (letter) is
    // appended if present, e.g. step="7c/10" → prefix="7", sub="c".
    let prefix = step.split('/').next().unwrap_or("");
    let (numeric, suffix): (&str, &str) = {
        let split_at = prefix
            .char_indices()
            .find(|(_, c)| !c.is_ascii_digit())
            .map(|(i, _)| i)
            .unwrap_or(prefix.len());
        (&prefix[..split_at], &prefix[split_at..])
    };

    match (numeric, suffix) {
        ("3", "") => "Creating Python virtual environment…".to_string(),
        ("4", "") => "Installing Python dependencies…".to_string(),
        ("4", "b") => "Installing Weaviate MCP package…".to_string(),
        ("5", "") => "Starting containers (Weaviate, Ollama)…".to_string(),
        ("6", "") => "Waiting for Ollama to be ready…".to_string(),
        ("7", "") => "Pulling embedding models from Ollama…".to_string(),
        ("7", "b") => "Configuring Ollama (this can take a minute)…".to_string(),
        ("7", "c") => "Seeding Weaviate KG (this can take a few minutes)…".to_string(),
        ("7", "d") => "Running schema migrations…".to_string(),
        ("7", "e") => "Self-healing KG bindings…".to_string(),
        ("8", "") => "Deploying vct-hub binary…".to_string(),
        ("9", "") => "Writing .env file…".to_string(),
        ("10", "") => "Verifying installation…".to_string(),
        _ => {
            // Unknown step: if the detail is short and human-readable
            // (sub-200 chars), surface it directly rather than dropping
            // the event. install.py uses fairly user-friendly detail
            // strings already.
            if !detail.is_empty() && detail.len() < 200 {
                detail.to_string()
            } else {
                String::new()
            }
        }
    }
}

/// Append one event to `state/logs/install.jsonl` from the launcher
/// (actor=launcher). Mirrors the Python-side `_log_install_event`
/// schema. Best-effort: the install log dir might not exist yet, in
/// which case we silently skip — matches the Python contract.
///
/// Returns `Ok(true)` iff a line was actually written.
pub(crate) fn append_install_log_event(
    log_path: &Path,
    step: &str,
    phase: &str,
    detail: &str,
    data: Option<serde_json::Value>,
) -> std::io::Result<bool> {
    use std::io::Write;
    let parent = match log_path.parent() {
        Some(p) => p,
        None => return Ok(false),
    };
    if !parent.is_dir() {
        // state/logs/ doesn't exist yet — install.py Step 8 owns its
        // creation; we never auto-create from the launcher to avoid a
        // race with the Python side.
        return Ok(false);
    }
    let ts = chrono_iso_z();
    let mut record = serde_json::json!({
        "ts": ts,
        "actor": "launcher",
        "step": step,
        "phase": phase,
        "detail": detail,
    });
    if let Some(d) = data {
        if let Some(obj) = record.as_object_mut() {
            obj.insert("data".to_string(), d);
        }
    }
    let line = format!("{}\n", serde_json::to_string(&record)?);
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)?;
    f.write_all(line.as_bytes())?;
    Ok(true)
}

/// Stdlib-only ISO-8601 UTC "Z" timestamp (matches Python's
/// `_utc_iso_now`). Avoids pulling chrono just for this. Resolution is
/// seconds, which is what the rest of the install log uses.
/// Current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.
///
/// v0.2.77 (Part 7c task 5): delegates to the shared
/// `vct_launcher_core::time` home (one place for the launcher's ISO-Z
/// timestamp). The prior hand-rolled civil-from-days implementation moved
/// there verbatim as `iso_z_from_unix_secs` + `civil_from_days`; this
/// call-site keeps the same second-granularity `Z` format.
pub(crate) fn chrono_iso_z() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    vct_launcher_core::time::iso_z_from_unix_secs(secs)
}

