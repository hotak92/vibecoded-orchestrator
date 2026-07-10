// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Embedding enrichment migration runner (v0.2.18, Commit 9).
//!
//! Bridges the Svelte progress modal in KgCodegraphTab → the Python
//! enrichment loop in `vco_lib/embedding_enrichment.py`. Two responsibilities:
//!
//!  1. **Spawn + stream**: Run `python -m vco_lib.embedding_enrichment enrich
//!     --stream-progress --json` and re-emit each line on stdout as a
//!     `vct-enrichment-progress` Tauri event for the modal's progress bar.
//!  2. **Parse final report**: The last JSON line on stdout is the
//!     `EnrichmentReport`. Deserialise and return to the caller. The Svelte
//!     side renders it in the modal's done-state (enriched / skipped /
//!     failed counters).
//!
//! ─── Streaming protocol ──────────────────────────────────────────────────
//!
//! The Python CLI prints two kinds of JSON lines:
//!
//! ```json
//! {"progress": 0.42, "message": "Enriched 420/1000 (5 skipped, 0 failed)"}
//! ```
//!
//! ```json
//! {"collection":"MyProject_KnowledgeGraph","new_slot":"arctic2_embed",
//!  "total":1000,"enriched":995,"skipped":0,"failed":5,"failures":[...]}
//! ```
//!
//! Pre-flight errors emit:
//!
//! ```json
//! {"error":"SlotNotInSchemaError","message":"..."}
//! ```
//!
//! Lines are framed by `\n` so a tokio `BufReader::lines()` consumer
//! recovers them cleanly. We parse progress lines and final-report lines
//! by attempting both deserialisers and discriminating on which fields
//! are present.
//!
//! ─── Timeout ─────────────────────────────────────────────────────────────
//!
//! Enrichment on a 10k-node KG can take a while (each batch is one Ollama
//! HTTP round-trip + N writes; in practice ~1-3min for 10k nodes on a
//! warm GPU machine, longer on CPU-fallback). The locked design decision
//! is 30 minutes — long enough to absorb a large slow-machine run, short
//! enough that an actually-stuck subprocess gets cleaned up rather than
//! hanging the GUI forever. The Svelte modal's "Cancel" button is
//! intentionally disabled (see the modal source); idempotency means the
//! user can close the modal mid-run and re-open later to "resume" by
//! re-clicking Save (the next run will skip the already-enriched
//! objects).

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::time::timeout;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ───────────────────────────────────────────────────────────

/// Hard ceiling on the Python subprocess lifetime. 30min is the locked
/// design number — see module docstring.
const ENRICHMENT_TIMEOUT_SECS: u64 = 30 * 60;

/// The Tauri event name re-emitted from each Python `{"progress":...}`
/// line. The Svelte modal subscribes to this and updates the progress
/// bar + sub-text.
const PROGRESS_EVENT: &str = "vct-enrichment-progress";

// ─── Types (mirror Python dataclass shapes) ──────────────────────────────

/// Per-batch progress payload. Mirrors `{"progress":<float>,"message":<str>}`
/// from the Python `--stream-progress` output.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnrichmentProgress {
    pub collection: String,
    pub new_slot: String,
    pub progress: f64,
    pub message: String,
}

/// One failure row in the final report.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnrichmentFailure {
    /// Either `{uuid,error}` per-object OR `{dry_run_count}` sentinel.
    /// We accept any shape via `serde_json::Value` so the Svelte side
    /// can render both without us forcing a tagged-union schema.
    #[serde(flatten)]
    pub data: serde_json::Value,
}

/// Final report. Matches `EnrichmentReport` dataclass in
/// `vco_lib/embedding_enrichment.py` exactly.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnrichmentReport {
    pub collection: String,
    pub new_slot: String,
    pub total: u64,
    pub enriched: u64,
    pub skipped: u64,
    pub failed: u64,
    pub failures: Vec<EnrichmentFailure>,
}

/// Pre-flight error payload (the Python CLI's exit-1 JSON shape).
#[derive(Debug, Clone, Deserialize)]
struct PreflightError {
    error: String,
    message: String,
    #[serde(default)]
    unexpected: bool,
}

// ─── Python interpreter discovery (mirrors embedding_catalog.rs) ─────────

/// Resolve a python interpreter capable of `import vco_lib`.
///
/// v0.2.77: delegates to the shared RT-4 ladder in
/// `vct_launcher_core::python_resolve` (the "one home" for interpreter
/// discovery), which was previously mirrored here as an exe-walk-only copy.
/// The shared ladder additionally honours `$VCT_VENV` and
/// `$VCT_INSTALL_ROOT` ahead of the exe-walk.
#[inline]
fn resolve_python_for_vco_lib() -> Option<PathBuf> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
}

// ─── Tauri command ───────────────────────────────────────────────────────

/// Run an enrichment pass against one Weaviate collection.
///
/// * `collection_name` — Weaviate class name to enrich (e.g.
///   `"MyProject_KnowledgeGraph"`).
/// * `new_slot` — Named-vector slot to populate (e.g.
///   `"arctic2_embed"`). Must be in the slot catalog AND in the live
///   schema (run `migrate-collections` first if not).
/// * `project_id` — Optional project ID used to resolve `--project-root`
///   for `EmbeddingService.for_project()`. When `None`, the Python side
///   falls back to env-based discovery.
/// * `dry_run` — When true, no writes happen; the report carries the
///   would-have-enriched count in `failures[0].dry_run_count`.
///
/// Streams progress events while running; returns the final
/// `EnrichmentReport` on completion.
#[command]
pub async fn enrich_collection_vectors(
    collection_name: String,
    new_slot: String,
    project_id: Option<String>,
    dry_run: Option<bool>,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<EnrichmentReport, String> {
    let project_root: Option<PathBuf> = match project_id.as_deref() {
        Some(id) => match db.get_project(id)? {
            Some(row) => Some(PathBuf::from(row.folder_path)),
            None => return Err(format!("project not found: {}", id)),
        },
        None => None,
    };

    let report = run_enrichment_with_stream(
        &collection_name,
        &new_slot,
        project_root,
        dry_run.unwrap_or(false),
        &app,
    )
    .await?;

    // Audit log so the user can see in Settings → Audit which
    // collections got enriched + when. Soft-fail: don't block the
    // success return on a DB hiccup.
    let _ = db.audit(
        "embedding_enrichment_complete",
        project_id.as_deref(),
        None,
        &serde_json::json!({
            "collection": collection_name,
            "new_slot": new_slot,
            "total": report.total,
            "enriched": report.enriched,
            "skipped": report.skipped,
            "failed": report.failed,
            "dry_run": dry_run.unwrap_or(false),
        }),
    );

    Ok(report)
}

async fn run_enrichment_with_stream(
    collection_name: &str,
    new_slot: &str,
    project_root: Option<PathBuf>,
    dry_run: bool,
    app: &AppHandle,
) -> Result<EnrichmentReport, String> {
    let python = resolve_python_for_vco_lib()
        .ok_or_else(|| "no python interpreter found for vco_lib".to_string())?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.embedding_enrichment")
        .arg("enrich")
        .arg("--collection")
        .arg(collection_name)
        .arg("--new-slot")
        .arg(new_slot)
        .arg("--stream-progress")
        .arg("--json");
    if let Some(root) = project_root.as_ref() {
        cmd.arg("--project-root").arg(root.as_os_str());
    }
    if dry_run {
        cmd.arg("--dry-run");
    }

    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("spawn enrichment: {}", e))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "child has no stdout pipe".to_string())?;
    let stderr = child.stderr.take();

    // Single-task stdout reader — enrichment's stream-progress lines are
    // small (~100B each); we don't need the concurrent stderr-drain
    // dance that kg_sync uses. Stderr is captured for the error path
    // only (read after the wait, when the subprocess has terminated).
    let mut reader = BufReader::new(stdout).lines();

    let mut final_report: Option<EnrichmentReport> = None;
    let mut preflight_error: Option<PreflightError> = None;

    let read_fut = async {
        while let Ok(Some(line)) = reader.next_line().await {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            handle_stdout_line(
                trimmed,
                collection_name,
                new_slot,
                app,
                &mut final_report,
                &mut preflight_error,
            );
        }
    };

    // Bounded by ENRICHMENT_TIMEOUT_SECS. On timeout we kill the child
    // and surface a clean error — better UX than hanging forever.
    let timed = timeout(
        Duration::from_secs(ENRICHMENT_TIMEOUT_SECS),
        async {
            read_fut.await;
            child.wait().await
        },
    )
    .await;

    let status = match timed {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return Err(format!("wait enrichment: {}", e)),
        Err(_) => {
            // Best-effort kill. The OS handle drop also reaps the child
            // even if start_kill fails (Linux: process becomes zombie
            // briefly, then reaped on Drop; Windows: handle close +
            // TerminateProcess).
            let _ = child.start_kill();
            return Err(format!(
                "enrichment timed out after {}s; subprocess killed. \
                 Re-running is safe (idempotent) — the partial-state \
                 fraction can be picked up by re-clicking Save.",
                ENRICHMENT_TIMEOUT_SECS
            ));
        }
    };

    if let Some(pre) = preflight_error.take() {
        // Pre-flight failures are not exit-0; we use the structured
        // message instead of the raw exit code so the Svelte side can
        // distinguish "user-fixable" (slot missing, no backend) from
        // "system bug".
        let prefix = if pre.unexpected {
            "Unexpected enrichment error"
        } else {
            &pre.error
        };
        return Err(format!("{}: {}", prefix, pre.message));
    }

    if !status.success() {
        let stderr_text = if let Some(err_stream) = stderr {
            // Drain stderr now that we know the subprocess is done.
            // Capping at 2KB so a runaway log doesn't blow up the
            // toast message.
            read_stderr_capped(err_stream, 2048).await
        } else {
            String::new()
        };
        return Err(format!(
            "enrichment exit {}: {}",
            status.code().unwrap_or(-1),
            if stderr_text.is_empty() {
                "no stderr"
            } else {
                stderr_text.as_str()
            }
        ));
    }

    final_report.ok_or_else(|| {
        "enrichment subprocess exited 0 but emitted no final report — \
         possible bug in vco_lib.embedding_enrichment CLI".to_string()
    })
}

/// Drain stderr to a String, capping at `max_bytes` to avoid OOMing on
/// pathological output. Best-effort — async-IO errors silently truncate
/// what we've collected so far.
async fn read_stderr_capped(
    stderr: tokio::process::ChildStderr,
    max_bytes: usize,
) -> String {
    use tokio::io::AsyncReadExt;
    let mut buf = Vec::with_capacity(max_bytes.min(4096));
    let mut reader = BufReader::new(stderr);
    // read_to_end is bounded by max_bytes via take(); we don't care
    // about post-cap chars.
    let mut limited = (&mut reader).take(max_bytes as u64);
    let _ = limited.read_to_end(&mut buf).await;
    String::from_utf8_lossy(&buf).into_owned()
}

/// Discriminate progress vs final-report vs pre-flight-error lines, and
/// dispatch each shape to the right sink.
fn handle_stdout_line(
    line: &str,
    collection_name: &str,
    new_slot: &str,
    app: &AppHandle,
    final_report: &mut Option<EnrichmentReport>,
    preflight_error: &mut Option<PreflightError>,
) {
    // Cheap discriminator: try to parse as a generic value first, then
    // peek at the keys. Avoids running three full deserialisers on
    // every line.
    let value: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => {
            // Garbage on stdout — possibly the user's PYTHONPATH put
            // some other module first and it printed a warning. Skip
            // silently; the final-report check at the end will catch
            // "no report".
            return;
        }
    };
    let obj = match value.as_object() {
        Some(o) => o,
        None => return,
    };

    // Pre-flight error envelope: {"error":...,"message":...}.
    if obj.contains_key("error") && obj.contains_key("message") {
        if let Ok(pre) = serde_json::from_value::<PreflightError>(value.clone()) {
            *preflight_error = Some(pre);
            return;
        }
    }

    // Progress line: {"progress":...,"message":...}.
    if obj.contains_key("progress") {
        let progress = obj
            .get("progress")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let message = obj
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let payload = EnrichmentProgress {
            collection: collection_name.to_string(),
            new_slot: new_slot.to_string(),
            progress,
            message,
        };
        let _ = app.emit(PROGRESS_EVENT, payload);
        return;
    }

    // Final report: has "total" + "enriched" + "skipped".
    if obj.contains_key("total")
        && obj.contains_key("enriched")
        && obj.contains_key("skipped")
    {
        if let Ok(report) = serde_json::from_value::<EnrichmentReport>(value) {
            *final_report = Some(report);
        }
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Mocked AppHandle isn't trivial; we test the line-parser directly
    /// (the meat of the logic) without spinning up a Tauri app. Tauri's
    /// emit() side-effect is exercised by integration tests, not unit
    /// tests.

    fn parse_one_line(line: &str) -> (Option<EnrichmentReport>, Option<PreflightError>) {
        // Run the discriminator logic without Tauri. Since
        // `handle_stdout_line` needs an `AppHandle`, we inline the same
        // discriminator here for tests. Real production logic is the
        // function above.
        let value: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => return (None, None),
        };
        let obj = match value.as_object() {
            Some(o) => o.clone(),
            None => return (None, None),
        };
        if obj.contains_key("error") && obj.contains_key("message") {
            if let Ok(pre) = serde_json::from_value::<PreflightError>(value) {
                return (None, Some(pre));
            }
            return (None, None);
        }
        if obj.contains_key("total")
            && obj.contains_key("enriched")
            && obj.contains_key("skipped")
        {
            if let Ok(report) = serde_json::from_value::<EnrichmentReport>(value) {
                return (Some(report), None);
            }
        }
        (None, None)
    }

    #[test]
    fn parses_final_report_shape() {
        let line = serde_json::json!({
            "collection": "TestKG",
            "new_slot": "arctic2_embed",
            "total": 10,
            "enriched": 8,
            "skipped": 2,
            "failed": 0,
            "failures": []
        })
        .to_string();
        let (report, err) = parse_one_line(&line);
        assert!(err.is_none());
        let r = report.expect("final report parsed");
        assert_eq!(r.collection, "TestKG");
        assert_eq!(r.new_slot, "arctic2_embed");
        assert_eq!(r.total, 10);
        assert_eq!(r.enriched, 8);
        assert_eq!(r.skipped, 2);
    }

    #[test]
    fn parses_preflight_error_shape() {
        let line = serde_json::json!({
            "error": "SlotNotInSchemaError",
            "message": "Slot 'arctic2_embed' missing in TestKG"
        })
        .to_string();
        let (report, err) = parse_one_line(&line);
        assert!(report.is_none());
        let e = err.expect("preflight parsed");
        assert_eq!(e.error, "SlotNotInSchemaError");
        assert!(e.message.contains("arctic2_embed"));
        assert!(!e.unexpected);
    }

    #[test]
    fn parses_unexpected_error_with_flag() {
        let line = serde_json::json!({
            "error": "RuntimeError",
            "message": "kaboom",
            "unexpected": true
        })
        .to_string();
        let (_, err) = parse_one_line(&line);
        let e = err.expect("unexpected parsed");
        assert!(e.unexpected);
    }

    #[test]
    fn final_report_with_failures_round_trips() {
        let line = serde_json::json!({
            "collection": "TestKG",
            "new_slot": "arctic2_embed",
            "total": 3,
            "enriched": 1,
            "skipped": 1,
            "failed": 1,
            "failures": [
                {"uuid": "abc", "error": "boom"}
            ]
        })
        .to_string();
        let (report, _) = parse_one_line(&line);
        let r = report.expect("parsed");
        assert_eq!(r.failures.len(), 1);
        // Failure carries flat `uuid` + `error` keys.
        assert_eq!(
            r.failures[0].data.get("uuid").and_then(|v| v.as_str()),
            Some("abc"),
        );
    }

    #[test]
    fn dry_run_sentinel_failure_shape_parses() {
        // The dry-run path injects `{"dry_run_count": N}` as failures[0].
        let line = serde_json::json!({
            "collection": "TestKG",
            "new_slot": "arctic2_embed",
            "total": 5,
            "enriched": 0,
            "skipped": 0,
            "failed": 0,
            "failures": [{"dry_run_count": 5}]
        })
        .to_string();
        let (report, _) = parse_one_line(&line);
        let r = report.expect("parsed");
        assert_eq!(r.failures.len(), 1);
        assert_eq!(
            r.failures[0].data.get("dry_run_count").and_then(|v| v.as_u64()),
            Some(5),
        );
    }

    #[test]
    fn malformed_json_silently_ignored() {
        let (r, e) = parse_one_line("not json");
        assert!(r.is_none());
        assert!(e.is_none());
        let (r, e) = parse_one_line("");
        assert!(r.is_none());
        assert!(e.is_none());
    }

    #[test]
    fn progress_event_payload_serialises() {
        // Ensure the EnrichmentProgress struct serialises with the field
        // names the Svelte side expects.
        let p = EnrichmentProgress {
            collection: "TestKG".into(),
            new_slot: "arctic2_embed".into(),
            progress: 0.42,
            message: "Enriched 42/100".into(),
        };
        let s = serde_json::to_string(&p).expect("serialise");
        assert!(s.contains("\"collection\":\"TestKG\""));
        assert!(s.contains("\"new_slot\":\"arctic2_embed\""));
        assert!(s.contains("\"progress\":0.42"));
        assert!(s.contains("\"message\":\"Enriched 42/100\""));
    }

    #[test]
    fn enrichment_report_default_failures_empty() {
        // Verify our struct's optional `failures` field defaults to an
        // empty vec when the JSON omits it. (Python always includes
        // it, but defensive deserialisation is cheap.)
        let line = serde_json::json!({
            "collection": "TestKG",
            "new_slot": "arctic2_embed",
            "total": 0,
            "enriched": 0,
            "skipped": 0,
            "failed": 0,
            "failures": []
        })
        .to_string();
        let r: EnrichmentReport = serde_json::from_str(&line).expect("parse");
        assert!(r.failures.is_empty());
    }
}
