// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Code-graph Re-analyze runner (v0.2.18, Plan C).
//!
//! Bridges the Svelte `CodeGraphReanalysisModal` → the Python analyzer
//! `templates/scripts/analyze_code_graph.py`. Two responsibilities:
//!
//!  1. **Spawn + stream**: Run
//!     `analyze_code_graph.py <repo> --project <name> [--language <lang>]
//!     --prune-stale --json-progress` and re-emit each `{"progress": ...}`
//!     line as a `vct-reanalysis-progress` Tauri event for the modal's
//!     progress bar.
//!  2. **Parse final report**: The last JSON line on stdout is a
//!     `{"final": true, ...}` object carrying counts (files_analyzed,
//!     modules, classes, functions, apis, stale_pruned, ...). Deserialise
//!     and return to the caller. The Svelte side renders it in the done
//!     state.
//!
//! ─── Streaming protocol ──────────────────────────────────────────────────
//!
//! The Python CLI prints two kinds of JSON lines:
//!
//! ```json
//! {"progress": 0.42, "message": "Analyzing src/foo.py", "file":"src/foo.py", "lang":"python"}
//! ```
//!
//! ```json
//! {"final": true, "files_analyzed": 120, "modules": 120, "classes": 45,
//!  "functions": 380, "apis": 12, "stale_pruned": 3, "insert_errors": 0,
//!  "language": "python", "prune_stale": true}
//! ```
//!
//! Lines are framed by `\n` so a tokio `BufReader::lines()` consumer
//! recovers them cleanly. We discriminate progress vs final-report by
//! checking the `final` key.
//!
//! ─── Timeout ─────────────────────────────────────────────────────────────
//!
//! 30 minutes — large polyglot repos can take a while on first analysis,
//! especially with Joern CFG/PDG extraction enabled. The modal's Cancel
//! button is disabled with a "re-run is idempotent" tooltip (mirrors the
//! enrichment modal pattern). On timeout we kill the child and surface a
//! clean error.

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::time::timeout;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ───────────────────────────────────────────────────────────

/// Hard ceiling on the analyzer subprocess lifetime. 30min is the locked
/// design number — same as embedding_enrichment.rs.
const REANALYSIS_TIMEOUT_SECS: u64 = 30 * 60;

/// Tauri event name re-emitted from each Python `{"progress":...}` line.
/// The Svelte modal subscribes to this and updates the progress bar.
const PROGRESS_EVENT: &str = "vct-reanalysis-progress";

// ─── Types ───────────────────────────────────────────────────────────────

/// Per-file progress payload. Mirrors the Python `--json-progress` shape.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReanalysisProgress {
    /// Project name being analysed (echoed back so the modal can
    /// sanity-check the event is for the right run).
    pub project: String,
    /// `--language` flag value, or empty string for full-multi-language.
    pub language: String,
    /// Fractional progress in [0, 1]. The analyzer guarantees bounded
    /// output, but the Svelte side clamps too.
    pub progress: f64,
    /// Human-readable sub-text: e.g. `"Analyzing src/foo.py"`.
    pub message: String,
    /// Repo-relative POSIX path of the file currently being analyzed.
    /// Empty when the final emit fires after the dispatch loop.
    pub file: String,
    /// Canonical language ID for the current file (`"python"`, `"go"`, ...).
    /// Empty when the final emit fires.
    pub lang: String,
}

/// Final report. Mirrors the Python `{"final": true, ...}` shape.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReanalysisReport {
    pub files_analyzed: u64,
    pub files_skipped: u64,
    pub modules: u64,
    pub classes: u64,
    pub functions: u64,
    pub apis: u64,
    pub insert_errors: u64,
    pub stale_pruned: u64,
    /// Empty string when no `--language` filter was set (full multi-
    /// language re-walk). Non-empty when a single-language run.
    pub language: String,
    /// Whether `--prune-stale` was active for this run.
    pub prune_stale: bool,
}

// ─── Python interpreter discovery ────────────────────────────────────────

/// Resolve a python interpreter capable of running analyze_code_graph.py.
///
/// v0.2.77: delegates to the shared RT-4 ladder in
/// `vct_launcher_core::python_resolve`. The prior inline copy was exe-walk
/// ONLY — it lacked the `$VCT_VENV` and `$VCT_INSTALL_ROOT` tiers, so a
/// project without its own `.venv` fell straight to a PATH `python3` that
/// can't `import weaviate`, breaking codegraph re-analysis. Sharing the
/// full ladder fixes that gap for free. Thin shim kept so the single
/// call-site below doesn't churn.
#[inline]
fn resolve_python_for_analyzer() -> Option<PathBuf> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
}

/// Resolve the analyze_code_graph.py script. Strategy mirrors
/// `codegraph::resolve_analyzer_script`: try the project's own
/// `.claude/scripts/`, then the launcher's install root.
fn resolve_analyzer_script(project_folder: &std::path::Path) -> Option<PathBuf> {
    let candidate = project_folder
        .join(".claude")
        .join("scripts")
        .join("analyze_code_graph.py");
    if candidate.is_file() {
        return Some(candidate);
    }

    // Walk up from the launcher binary location looking for
    // `.claude/scripts/analyze_code_graph.py`.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..8 {
                let probe = cur
                    .join(".claude")
                    .join("scripts")
                    .join("analyze_code_graph.py");
                if probe.is_file() {
                    return Some(probe);
                }
                if !cur.pop() {
                    break;
                }
            }
        }
    }
    None
}

// ─── Tauri command ───────────────────────────────────────────────────────

/// Run a full re-analysis pass against one project's code graph.
///
/// * `project_id` — project to re-analyze. Resolves to a folder path
///   via the local SQLite DB. The analyzer is invoked from that folder
///   with `--project <name>`.
/// * `language` — `Some("python")` to scope the re-walk + prune to one
///   language (Plan C language-scoped prune). `None` re-walks every
///   supported language and prunes globally. The set of accepted values
///   matches analyze_code_graph.py's argparse `--language` choices.
///
/// Always passes `--prune-stale` (this is the explicit "authoritative
/// refresh" path; the user clicked the button because they want stale
/// rows cleaned). The hook path is the other way to refresh the graph.
///
/// Streams progress events while running; returns the final
/// `ReanalysisReport` on completion.
#[command]
pub async fn reanalyze_code_graph(
    project_id: String,
    language: Option<String>,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<ReanalysisReport, String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let folder = PathBuf::from(&project.folder_path);
    let lang_arg = language.as_deref().map(str::to_string);
    let report =
        run_reanalysis_with_stream(&project.name, &folder, lang_arg.as_deref(), &app).await?;

    // Audit log so Settings → Audit shows when Re-analyze was clicked.
    // Soft-fail: don't block the success return on a DB hiccup.
    let _ = db.audit(
        "code_graph_reanalysis_complete",
        Some(&project.id),
        None,
        &serde_json::json!({
            "project": project.name,
            "language": language,
            "files_analyzed": report.files_analyzed,
            "modules": report.modules,
            "classes": report.classes,
            "functions": report.functions,
            "apis": report.apis,
            "stale_pruned": report.stale_pruned,
            "insert_errors": report.insert_errors,
        }),
    );

    Ok(report)
}

async fn run_reanalysis_with_stream(
    project_name: &str,
    folder: &std::path::Path,
    language: Option<&str>,
    app: &AppHandle,
) -> Result<ReanalysisReport, String> {
    let python = resolve_python_for_analyzer()
        .ok_or_else(|| "no python interpreter found for analyzer".to_string())?;
    let script = resolve_analyzer_script(folder).ok_or_else(|| {
        "analyze_code_graph.py not found (looked in project's .claude/scripts \
         and the launcher's install root)"
            .to_string()
    })?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg(&script)
        .arg(folder.as_os_str())
        .arg("--project")
        .arg(project_name)
        // Plan C: --prune-stale is always-on for Re-analyze. The user
        // clicked the button because they want stale rows cleaned.
        .arg("--prune-stale")
        // Plan C: stream per-file JSON progress for the modal.
        .arg("--json-progress");
    if let Some(lang) = language {
        if !lang.is_empty() {
            cmd.arg("--language").arg(lang);
        }
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
        .map_err(|e| format!("spawn analyzer: {}", e))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "child has no stdout pipe".to_string())?;
    let stderr = child.stderr.take();

    let mut reader = BufReader::new(stdout).lines();
    let mut final_report: Option<ReanalysisReport> = None;
    let lang_for_event = language.unwrap_or("").to_string();

    let read_fut = async {
        while let Ok(Some(line)) = reader.next_line().await {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            handle_stdout_line(
                trimmed,
                project_name,
                &lang_for_event,
                app,
                &mut final_report,
            );
        }
    };

    let timed = timeout(
        Duration::from_secs(REANALYSIS_TIMEOUT_SECS),
        async {
            read_fut.await;
            child.wait().await
        },
    )
    .await;

    let status = match timed {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return Err(format!("wait analyzer: {}", e)),
        Err(_) => {
            let _ = child.start_kill();
            return Err(format!(
                "Re-analyze timed out after {}s; subprocess killed. \
                 Re-running is safe (idempotent) — unchanged files are skipped.",
                REANALYSIS_TIMEOUT_SECS
            ));
        }
    };

    if !status.success() {
        let stderr_text = if let Some(err_stream) = stderr {
            read_stderr_capped(err_stream, 2048).await
        } else {
            String::new()
        };
        // Map the analyzer's documented exit codes to actionable text.
        let code = status.code().unwrap_or(-1);
        let hint = match code {
            2 => " (schema case collision — see stderr)",
            3 => " (no files indexed — check repo path / filters)",
            4 => " (insert errors — analysis incomplete; see stderr)",
            _ => "",
        };
        return Err(format!(
            "analyzer exit {}{}: {}",
            code,
            hint,
            if stderr_text.is_empty() {
                "no stderr".to_string()
            } else {
                stderr_text
            }
        ));
    }

    final_report.ok_or_else(|| {
        "analyzer exited 0 but emitted no final report — \
         possible bug in analyze_code_graph.py --json-progress path"
            .to_string()
    })
}

async fn read_stderr_capped(
    stderr: tokio::process::ChildStderr,
    max_bytes: usize,
) -> String {
    let mut buf = Vec::with_capacity(max_bytes.min(4096));
    let mut reader = BufReader::new(stderr);
    let mut limited = (&mut reader).take(max_bytes as u64);
    let _ = limited.read_to_end(&mut buf).await;
    String::from_utf8_lossy(&buf).into_owned()
}

/// Discriminate progress vs final-report lines and dispatch each.
/// Final-report shape carries `"final": true`. Progress shape carries
/// `"progress"` (float) + `"message"` (string).
fn handle_stdout_line(
    line: &str,
    project_name: &str,
    language: &str,
    app: &AppHandle,
    final_report: &mut Option<ReanalysisReport>,
) {
    let value: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => {
            // Non-JSON output (e.g. analyzer's emoji-decorated
            // human-readable lines that escape the --json-progress
            // gate — should be impossible but defensive). Skip.
            return;
        }
    };
    let obj = match value.as_object() {
        Some(o) => o,
        None => return,
    };

    if obj.get("final").and_then(|v| v.as_bool()) == Some(true) {
        if let Ok(report) = serde_json::from_value::<ReanalysisReport>(value) {
            *final_report = Some(report);
        }
        return;
    }

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
        let file = obj
            .get("file")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let lang = obj
            .get("lang")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let payload = ReanalysisProgress {
            project: project_name.to_string(),
            language: language.to_string(),
            progress,
            message,
            file,
            lang,
        };
        let _ = app.emit(PROGRESS_EVENT, payload);
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Test the line-parser logic without spinning up a Tauri AppHandle.
    /// `handle_stdout_line` needs an `AppHandle` to `emit()`; we inline
    /// the discriminator here so the unit tests stay pure.
    fn parse_one_line(line: &str) -> (Option<ReanalysisReport>, Option<ReanalysisProgress>) {
        let value: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => return (None, None),
        };
        let obj = match value.as_object() {
            Some(o) => o.clone(),
            None => return (None, None),
        };
        if obj.get("final").and_then(|v| v.as_bool()) == Some(true) {
            if let Ok(report) = serde_json::from_value::<ReanalysisReport>(value) {
                return (Some(report), None);
            }
            return (None, None);
        }
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
            let file = obj
                .get("file")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let lang = obj
                .get("lang")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let p = ReanalysisProgress {
                project: "TestProj".to_string(),
                language: "".to_string(),
                progress,
                message,
                file,
                lang,
            };
            return (None, Some(p));
        }
        (None, None)
    }

    #[test]
    fn parses_final_report_shape() {
        let line = serde_json::json!({
            "final": true,
            "files_analyzed": 120,
            "files_skipped": 3,
            "modules": 120,
            "classes": 45,
            "functions": 380,
            "apis": 12,
            "insert_errors": 0,
            "stale_pruned": 7,
            "language": "python",
            "prune_stale": true
        })
        .to_string();
        let (report, prog) = parse_one_line(&line);
        assert!(prog.is_none());
        let r = report.expect("final report parsed");
        assert_eq!(r.files_analyzed, 120);
        assert_eq!(r.modules, 120);
        assert_eq!(r.classes, 45);
        assert_eq!(r.functions, 380);
        assert_eq!(r.apis, 12);
        assert_eq!(r.stale_pruned, 7);
        assert_eq!(r.language, "python");
        assert!(r.prune_stale);
    }

    #[test]
    fn parses_progress_line_shape() {
        let line = serde_json::json!({
            "progress": 0.42,
            "message": "Analyzing src/foo.py",
            "file": "src/foo.py",
            "lang": "python"
        })
        .to_string();
        let (report, prog) = parse_one_line(&line);
        assert!(report.is_none());
        let p = prog.expect("progress parsed");
        assert!((p.progress - 0.42).abs() < 1e-9);
        assert_eq!(p.message, "Analyzing src/foo.py");
        assert_eq!(p.file, "src/foo.py");
        assert_eq!(p.lang, "python");
    }

    #[test]
    fn full_relayze_final_report_no_language() {
        // The --language=None path emits language="" in the final report.
        let line = serde_json::json!({
            "final": true,
            "files_analyzed": 250,
            "files_skipped": 5,
            "modules": 250,
            "classes": 100,
            "functions": 800,
            "apis": 30,
            "insert_errors": 0,
            "stale_pruned": 15,
            "language": "",
            "prune_stale": true
        })
        .to_string();
        let (report, _) = parse_one_line(&line);
        let r = report.expect("parsed");
        assert_eq!(r.language, "");
        assert_eq!(r.stale_pruned, 15);
    }

    #[test]
    fn malformed_json_silently_ignored() {
        let (r, p) = parse_one_line("not json");
        assert!(r.is_none());
        assert!(p.is_none());
        let (r, p) = parse_one_line("");
        assert!(r.is_none());
        assert!(p.is_none());
    }

    #[test]
    fn final_report_takes_priority_over_progress_key() {
        // Defensive: if a line carries both `final: true` AND `progress`
        // (it shouldn't, but Python could emit a malformed object),
        // the discriminator MUST treat it as final.
        let line = serde_json::json!({
            "final": true,
            "progress": 1.0,
            "files_analyzed": 10,
            "files_skipped": 0,
            "modules": 10,
            "classes": 5,
            "functions": 30,
            "apis": 2,
            "insert_errors": 0,
            "stale_pruned": 0,
            "language": "go",
            "prune_stale": true
        })
        .to_string();
        let (report, prog) = parse_one_line(&line);
        assert!(prog.is_none(), "progress should not be emitted when final=true");
        let r = report.expect("final report parsed");
        assert_eq!(r.language, "go");
    }

    #[test]
    fn progress_payload_serialises_to_expected_keys() {
        let p = ReanalysisProgress {
            project: "TestProj".to_string(),
            language: "python".to_string(),
            progress: 0.5,
            message: "Analyzing foo.py".to_string(),
            file: "foo.py".to_string(),
            lang: "python".to_string(),
        };
        let s = serde_json::to_string(&p).expect("serialise");
        assert!(s.contains("\"project\":\"TestProj\""));
        assert!(s.contains("\"language\":\"python\""));
        assert!(s.contains("\"progress\":0.5"));
        assert!(s.contains("\"message\":\"Analyzing foo.py\""));
        assert!(s.contains("\"file\":\"foo.py\""));
        assert!(s.contains("\"lang\":\"python\""));
    }

    #[test]
    fn report_default_zero_counts() {
        let line = serde_json::json!({
            "final": true,
            "files_analyzed": 0,
            "files_skipped": 0,
            "modules": 0,
            "classes": 0,
            "functions": 0,
            "apis": 0,
            "insert_errors": 0,
            "stale_pruned": 0,
            "language": "",
            "prune_stale": false
        })
        .to_string();
        let (report, _) = parse_one_line(&line);
        let r = report.expect("parsed");
        assert!(!r.prune_stale);
        assert_eq!(r.files_analyzed, 0);
    }
}
