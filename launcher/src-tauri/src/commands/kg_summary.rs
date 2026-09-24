//! Initial KG-summary backfill on project create (KG summary auto-backfill,
//! v0.2.3 — 2026-05-12).
//!
//! v0.2.2 added auto-sync of `knowledge/**/*.md` to Weaviate on add-project
//! (see `commands::kg_sync`). v0.2.3 closes the next gap: the orchestrator
//! also maintains a sidecar file `<project>/knowledge/.node_formats.json`
//! with LLM-generated summaries per KG node, consumed by `hybrid_search`'s
//! `summary` tier (score 0.42–0.55). Summaries are produced by
//! `.claude/scripts/generate-kg-summary.py`, invoked per-file by the
//! PostToolUse hook `kg-summary-generator.sh` — but the hook only fires on
//! Claude Code Edit/Write events, NOT when the launcher's kg-sync subprocess
//! writes embeddings. Result: a freshly-added project with 50+ pre-existing
//! KG nodes has Weaviate populated but `.node_formats.json` empty, and the
//! hook would only backfill it as the user edits each node — i.e. 50+ Claude
//! sessions to fully populate. This module closes that gap by mirroring the
//! existing `commands::kg_sync` initial-sync pattern:
//!
//!   1. `create_project_v2` calls `spawn_initial_summary` AFTER bundle
//!      install drops `generate-kg-summary.py` into the project. Fire-and-
//!      forget — project create returns immediately to the user.
//!   2. Pre-check: if `knowledge/` contains no `.md` files, status=`skipped`
//!      and we stop. Avoids needless subprocess spawns for empty projects.
//!   3. Otherwise: walk `<project>/knowledge/**/*.md` and shell out to
//!      `<venv-python> generate-kg-summary.py <file>` for each. Stream
//!      progress to the GUI banner as we go.
//!   4. The summariser has a built-in fallback chain (claude CLI → Ollama
//!      → ANTHROPIC_API_KEY → silent skip). On the "silent skip" path the
//!      script logs `KG-summary: no backend available` to stdout and
//!      exits 0; we detect that on the FIRST node, transition the row to
//!      `skipped` with an actionable error_message, and stop early (no
//!      point invoking the same script 49 more times).
//!   5. Env vars (KG_PROJECT_ROOT + KG_COLLECTION + WEAVIATE_URL +
//!      KG_SUMMARY_OLLAMA_URL + OLLAMA_URL) are passed via Command::env
//!      from a `ProjectEnvSettings::populate(...)` snapshot — the script
//!      reads `KG_PROJECT_ROOT` to scope its `.node_formats.json` write
//!      to the launcher-managed project rather than the orchestrator's
//!      own knowledge/.
//!
//! Failure isolation: ANY failure of this background task (script not
//! found, venv not found, every subprocess crashed, etc.) is recorded in
//! the row's `error_message` and emitted as a terminal `failed`/`skipped`
//! event. It is NEVER propagated to the create_project_v2 caller — the
//! user has already gotten their `ProjectView` back by the time this runs.
//!
//! Idempotency: `generate-kg-summary.py` content-hashes each node and
//! exits 0 with "unchanged (hash match), skipping" when the body is
//! identical to the last summarised version. Re-running on an already-
//! summarised project is therefore a (mostly) free no-op (counted under
//! `nodes_unchanged`). Safe to invoke repeatedly via `retry_kg_summary`.

use serde::Serialize;
use tauri::{command, AppHandle, Emitter, Manager, State};

use crate::commands::project_env_settings::{self, ProjectEnvSettings};
use crate::db::kg_summaries::{status as summary_status, KgSummaryRow};
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

const SUMMARY_EVENT: &str = "kg-summary-progress";

/// How many concurrent subprocess crashes we tolerate before bailing on
/// the rest of the walk. Set conservatively: a single missing venv or
/// missing script will hit all N nodes; failing the whole run early
/// surfaces the problem faster than chewing through 58 doomed exec()s.
const SUBPROCESS_FAIL_FAST_THRESHOLD: u32 = 3;

/// Substring the summariser writes to stdout when no backend is available.
/// Must match `select_backend()`'s log line in `generate-kg-summary.py`.
const NO_BACKEND_MARKER: &str = "no backend available";

/// Substring the summariser writes to stdout when an existing entry's
/// content-hash matches (cheap no-op skip — counts toward `nodes_unchanged`).
/// Must match `main()`'s "unchanged (hash match), skipping" line.
const UNCHANGED_MARKER: &str = "unchanged (hash match)";

/// Tauri-event payload + DTO for `get_kg_summary_status`.
///
/// Mirrors `KgSummaryRow` but in a public-API shape: timestamps in ISO
/// 8601 (so the GUI doesn't have to convert epoch-ms), explicit optionals,
/// and a `current_phase` string for live progress events. Field names
/// match `KgSummaryRow` 1:1 so the FE can union them transparently.
#[derive(Debug, Clone, Serialize)]
pub struct KgSummaryView {
    pub project_id: String,
    pub status: String,
    pub started_at_iso: Option<String>,
    pub finished_at_iso: Option<String>,
    pub duration_ms: Option<i64>,
    pub nodes_total: u32,
    pub nodes_succeeded: u32,
    pub nodes_unchanged: u32,
    pub nodes_failed: u32,
    pub nodes_skipped: u32,
    pub backend: Option<String>,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// Live phase indicator. Only populated on `running` events emitted
    /// during the backfill (e.g. "scan", "summarise"). Always None for
    /// stored rows fetched via `get_kg_summary_status`.
    pub current_phase: Option<String>,
}

impl KgSummaryView {
    fn from_row(row: KgSummaryRow) -> Self {
        Self {
            project_id: row.project_id,
            status: row.status,
            started_at_iso: row.started_at.and_then(epoch_ms_to_iso),
            finished_at_iso: row.finished_at.and_then(epoch_ms_to_iso),
            duration_ms: row.duration_ms,
            nodes_total: row.nodes_total,
            nodes_succeeded: row.nodes_succeeded,
            nodes_unchanged: row.nodes_unchanged,
            nodes_failed: row.nodes_failed,
            nodes_skipped: row.nodes_skipped,
            backend: row.backend,
            error_message: row.error_message,
            log_tail: row.log_tail,
            current_phase: None,
        }
    }
}

fn epoch_ms_to_iso(ms: i64) -> Option<String> {
    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).map(|dt| dt.to_rfc3339())
}

#[command]
pub async fn get_kg_summary_status(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Option<KgSummaryView>, String> {
    Ok(db.get_kg_summary(&project_id)?.map(KgSummaryView::from_row))
}

/// Re-run the KG-summary backfill for an existing project. Marks the row
/// as `pending` and re-spawns the background task. Safe to call while a
/// previous run is still in flight — the new spawn will overwrite the
/// row when it transitions; whichever finishes last wins. Mirrors
/// `kg_sync::retry_kg_sync` semantics.
#[command]
pub async fn retry_kg_summary(
    project_id: String,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    db.upsert_kg_summary(
        &project.id,
        summary_status::PENDING,
        Some(chrono::Utc::now().timestamp_millis()),
        None,
        None,
        0, 0, 0, 0, 0,
        None,
        None,
        None,
    )?;
    db.audit(
        "kg_summary_retry",
        Some(&project.id),
        None,
        &serde_json::json!({ "name": project.name }),
    )?;

    spawn_initial_summary(app, project.id, project.name, project.folder_path);
    Ok(())
}

// ─── v0.2.96 WP-7b: the GUI "Recheck summary backend now" action ────────
//
// WP-7a landed `python -m vco_lib.summary_health summary-recheck` (clears
// the breaker latch, re-derives the pending set from the sidecars,
// regenerates exactly it, resolves the `kg_summaries_degraded` ledger
// entry when the post-scan finds nothing pending) and the WP-7a review's
// MINOR-2 confirmed the GUI action that names it was not wired. This is
// that action: Preferences → KG Summaries invokes it through the ONE
// `python -m vco_lib.<module>` seam (env sandbox via
// `services::vco_lib_bridge`, clone PYTHONPATH via the model-gateway
// helper), awaits it off the UI thread, and returns the CLI's counts so
// the toast can say how many rows were regenerated or remain pending.

/// Deadline for the recheck subprocess. The recheck spawns one generator
/// child per pending row (each an LLM call), so the honest budget is the
/// enrichment precedent (`embedding_enrichment::ENRICHMENT_TIMEOUT_SECS`
/// = 30 min), not the 30 s gateway-CLI one. A kill at the deadline is
/// safe: the operation is idempotent and soft-fail per row — anything not
/// regenerated stays pending for the next recheck.
const RECHECK_TIMEOUT_SECS: u64 = 30 * 60;

/// Prefix of the ONE summary line the recheck CLI prints.
///
/// MUST MATCH `vco_lib/summary_health.py` — `RECHECK_LINE_PREFIX` and
/// `format_recheck_summary_line()`, which is the ONE home that renders the
/// line. The shape is "N pending before, M after; X generator run(s),
/// Y failure(s)" plus an optional "; code leg SKIPPED (…)" suffix.
///
/// v0.2.96 (duplication register D-2): before this release the Rust tests
/// HAND-WROTE the line they parse, so the parser and the producer could
/// drift with every test staying green — an unparsed run degrades silently
/// to "raw stdout tail" with no counts. Parity is now enforced by
/// `tests/test_v0296_lane_python_core.py::SummaryRecheckLineGrammarParity`,
/// which reads THIS file and compares it against the Python constants —
/// not a marker-string scan, which a name in a comment satisfies.
///
/// The last matching line wins — same rationale as
/// `parse_backend_from_stdout`: a later line is a strictly later statement
/// of the same run.
const RECHECK_LINE_PREFIX: &str = "[summary-health] recheck:";

/// Counts parsed from the recheck CLI's summary line.
#[derive(Debug, Clone, PartialEq, Eq)]
struct RecheckCounts {
    pending_before: u32,
    pending_after: u32,
    spawned: u32,
    failures: u32,
    code_leg_skipped: bool,
}

/// Leading unsigned integer of a trimmed segment, e.g. `"3 pending
/// before"` → `Some(3)`. `None` when the segment does not start with a
/// number.
fn leading_u32(segment: &str) -> Option<u32> {
    let digits: String = segment
        .trim()
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

/// Parse the CLI's fixed-format summary line out of captured stdout.
///
/// `None` when no line carries the prefix or the numbers do not parse —
/// the caller still surfaces the raw stdout tail, so an unparsed run is
/// visible, never silently blank.
fn parse_recheck_summary_line(stdout: &str) -> Option<RecheckCounts> {
    let line = stdout
        .lines()
        .rev()
        .find(|l| l.contains(RECHECK_LINE_PREFIX))?;
    let rest = line.split(RECHECK_LINE_PREFIX).nth(1)?.trim();
    let code_leg_skipped = rest.contains("code leg SKIPPED");
    // Drop the optional trailing "; code leg SKIPPED (…)" segment before
    // splitting the two count segments on ';'.
    let counts_part = rest.split("; code leg").next()?.trim();
    let mut segments = counts_part.split(';');
    let pendings = segments.next()?.trim();
    let runs = segments.next()?.trim();
    let mut pending_parts = pendings.split(',');
    let pending_before = leading_u32(pending_parts.next()?)?;
    let pending_after = leading_u32(pending_parts.next()?)?;
    let mut run_parts = runs.split(',');
    let spawned = leading_u32(run_parts.next()?)?;
    let failures = leading_u32(run_parts.next()?)?;
    Some(RecheckCounts {
        pending_before,
        pending_after,
        spawned,
        failures,
        code_leg_skipped,
    })
}

/// The recheck subcommand + its flags for one target root, as a pure argv
/// (kept separate from the `Command` build so the wiring is testable —
/// `std::process::Command` does not expose its args).
fn recheck_argv(project_root: &std::path::Path) -> Vec<String> {
    vec![
        "summary-recheck".to_string(),
        "--project-root".to_string(),
        project_root.display().to_string(),
    ]
}

/// `python -m vco_lib.summary_health summary-recheck --project-root <root>`
/// with the launcher's env sandbox and the clone's `PYTHONPATH` — the
/// established `vco_lib` spawn seam (`services::vco_lib_bridge` +
/// model_gateway's PYTHONPATH helper), so this spawn cannot drift from
/// every other `-m vco_lib.<module>` site.
///
/// Built as a `std::process::Command` because the sandbox kernel takes
/// `&mut std::process::Command`, then converted (tokio's `From` impl
/// preserves argv/env/stdio) for the awaited, deadline-capped `output()`.
/// `.silent()` carries the Windows CREATE_NO_WINDOW flag, matching the
/// sibling `invoke_summariser_once` pattern.
fn summary_health_command(
    python: &std::path::Path,
    orchestrator_root: Option<&std::path::Path>,
    project_root: &std::path::Path,
) -> tokio::process::Command {
    let mut cmd = std::process::Command::new(python).silent();
    cmd.arg("-m").arg("vco_lib.summary_health");
    for arg in recheck_argv(project_root) {
        cmd.arg(arg);
    }
    crate::services::vco_lib_bridge::reinject_minimal_env(&mut cmd);
    if let Some(root) = orchestrator_root {
        cmd.env(
            "PYTHONPATH",
            crate::commands::model_gateway::orchestrator_pythonpath(root),
        );
        // cwd at the clone root so the in-tree `vco_lib` namespace
        // package resolves even where PYTHONPATH is not honoured.
        cmd.current_dir(root);
    }
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    tokio::process::Command::from(cmd)
}

/// Result payload for `recheck_summary_backend` — the CLI's counts plus
/// the raw summary line, so the GUI toast can name what was regenerated
/// (and fall back to the verbatim line when parsing fails).
///
/// MUST MATCH the `SummaryRecheckView` type in
/// `launcher/src/routes/preferences/+page.svelte`. This repo has no
/// Rust→TS codegen surface, so the mirror is a C-tier duplication and the
/// LOCK is the minimum the repo rule requires: the parity test
/// `the_recheck_dto_field_set_matches_its_typescript_mirror` below
/// serialises this struct and diffs the wire field names against the
/// `.svelte` declaration. Add, rename or `#[serde(rename)]` a field here
/// and that test reds until the TS side is updated too (v0.2.96 D-6;
/// pre-fix the pair was hand-mirrored with nothing holding it).
#[derive(Debug, Clone, Serialize)]
pub struct SummaryRecheckView {
    pub project_root: String,
    pub exit_code: i64,
    /// Parsed from the CLI's summary line; `None` when it did not parse
    /// (see `summary_line`).
    pub pending_before: Option<u32>,
    pub pending_after: Option<u32>,
    pub spawned: Option<u32>,
    pub failures: Option<u32>,
    pub code_leg_skipped: bool,
    /// The verbatim `[summary-health] recheck: …` line ("" when absent).
    pub summary_line: String,
}

/// The "Recheck summary backend now" action (v0.2.96 WP-7b).
///
/// Runs `python -m vco_lib.summary_health summary-recheck --project-root
/// <root>` for the SELECTED project when `project_id` names one, else for
/// the orchestrator root (the CLI's own default target — its knowledge
/// tree is the shared-KG home). Async and off the UI thread; the frontend
/// holds the button in a working state and toasts the counts on
/// completion. A non-zero exit is NOT swallowed: the view carries the
/// exit code and the raw line so the error surfaces honestly.
#[command]
pub async fn recheck_summary_backend(
    project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<SummaryRecheckView, String> {
    // Target resolution first — everything after this point needs no DB.
    let orch_root =
        crate::services::vco_lib_bridge::resolve_orchestrator_root(&db);
    let (root, audit_project_id) = match project_id.as_deref() {
        Some(pid) => {
            let project = db
                .get_project(pid)?
                .ok_or_else(|| format!("project {} not found", pid))?;
            (
                std::path::PathBuf::from(&project.folder_path),
                Some(project.id),
            )
        }
        None => (
            orch_root.clone().ok_or_else(|| {
                "orchestrator root unresolvable (no DB-cached install \
                 path and no clone discoverable from the launcher \
                 binary) — select a project and retry, or run the \
                 recheck from a project folder"
                    .to_string()
            })?,
            None,
        ),
    };
    let python =
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
            .ok_or_else(|| {
                "no python interpreter found for vco_lib (checked $VCT_VENV, \
                 <VCT_INSTALL_ROOT>/.venv, then PATH). Re-run install.py to \
                 rebuild the orchestrator venv."
                    .to_string()
            })?;

    let mut cmd = summary_health_command(&python, orch_root.as_deref(), &root);
    // A dropped wait future does not kill the child by default; opt in so the
    // deadline really ends the python process rather than only ending our
    // wait for it. `kill_on_drop` reaches exactly ONE process, so the
    // generators that child spawned are handled separately — see the timeout
    // arm of `run_recheck_and_audit` (v0.2.96 L-7).
    cmd.kill_on_drop(true);

    run_recheck_and_audit(
        &db,
        audit_project_id.as_deref(),
        &root,
        cmd,
        std::time::Duration::from_secs(RECHECK_TIMEOUT_SECS),
    )
    .await
}

/// What one recheck attempt turned out to be. Every terminal state past the
/// spawn maps to exactly one variant, and `recheck_audit_detail` turns it
/// into the audit row.
///
/// v0.2.96 (L-6): pre-fix, `db.audit` sat AFTER the `?` on the timeout and
/// after the non-zero-exit `return`, so the two outcomes a reader most needs
/// in the trail — the deadline firing, and the CLI failing — were the two
/// that recorded nothing, while the child had already cleared the quota
/// breaker. An audit trail with invisible failure states does not just lose
/// detail; it misreports the successes it does hold as the whole story.
#[derive(Debug, Clone, PartialEq, Eq)]
enum RecheckOutcome {
    /// Exit 0. Counts as parsed (any of them may be `None`).
    Completed {
        pending_before: Option<u32>,
        pending_after: Option<u32>,
        failures: Option<u32>,
    },
    /// The CLI ran to completion and refused.
    ExitNonZero { exit_code: i64 },
    /// `RECHECK_TIMEOUT_SECS` elapsed. The child was killed on drop and its
    /// generator descendants signalled first (`descendants_killed` counts
    /// them — 0 is the normal case, meaning the child had none in flight).
    TimedOut {
        after_secs: u64,
        descendants_killed: usize,
    },
    /// The process could not be started at all.
    SpawnFailed { error: String },
}

impl RecheckOutcome {
    /// The stable string the audit row carries. Kept as a method so the
    /// vocabulary lives beside the enum and a new variant cannot be added
    /// without choosing one.
    fn as_str(&self) -> &'static str {
        match self {
            RecheckOutcome::Completed { .. } => "completed",
            RecheckOutcome::ExitNonZero { .. } => "exit_nonzero",
            RecheckOutcome::TimedOut { .. } => "timed_out",
            RecheckOutcome::SpawnFailed { .. } => "spawn_failed",
        }
    }
}

/// The `detail` JSON for one audited recheck. `project_root` and `outcome`
/// are present on EVERY row so the trail can be read without knowing which
/// variant produced it; the rest are the fields that variant actually has
/// (a timed-out run has no counts, and inventing zeros for it would make the
/// trail claim the run found nothing pending).
fn recheck_audit_detail(project_root: &str, outcome: &RecheckOutcome) -> serde_json::Value {
    let mut detail = serde_json::json!({
        "project_root": project_root,
        "outcome": outcome.as_str(),
    });
    let map = detail
        .as_object_mut()
        .expect("json! literal above is an object");
    match outcome {
        RecheckOutcome::Completed {
            pending_before,
            pending_after,
            failures,
        } => {
            map.insert("pending_before".into(), serde_json::json!(pending_before));
            map.insert("pending_after".into(), serde_json::json!(pending_after));
            map.insert("failures".into(), serde_json::json!(failures));
        }
        RecheckOutcome::ExitNonZero { exit_code } => {
            map.insert("exit_code".into(), serde_json::json!(exit_code));
        }
        RecheckOutcome::TimedOut {
            after_secs,
            descendants_killed,
        } => {
            map.insert("timeout_secs".into(), serde_json::json!(after_secs));
            map.insert(
                "descendants_killed".into(),
                serde_json::json!(descendants_killed),
            );
        }
        RecheckOutcome::SpawnFailed { error } => {
            map.insert("error".into(), serde_json::json!(error));
        }
    }
    detail
}

/// Run one recheck under `timeout`, then audit it — on EVERY path.
///
/// The shape is the point: the attempt is classified first, the audit is
/// written second, and the function has ONE exit. There is no place left for
/// a future edit to `return` a failure without a row, which is exactly how
/// L-6 happened. Takes `&Db` (not `State<Db>`) and the already-built command
/// so the timeout path is reachable from a unit test.
async fn run_recheck_and_audit(
    db: &Db,
    audit_project_id: Option<&str>,
    root: &std::path::Path,
    mut cmd: tokio::process::Command,
    timeout: std::time::Duration,
) -> Result<SummaryRecheckView, String> {
    let project_root = root.display().to_string();

    // Spawned explicitly (rather than through `Command::output()`) for two
    // reasons the timeout arm below needs: the child's PID, and a wait
    // future we still OWN when the deadline fires — `tokio::time::timeout`
    // drops the inner future before handing us the `Err`, and by then the
    // generators have been reparented to init and can no longer be proven
    // ours (v0.2.96 L-7).
    let spawned_at = vct_launcher_core::process::epoch_secs_now();

    let (result, outcome) = match cmd.spawn() {
        Err(e) => {
            let error = format!("summary recheck spawn failed: {}", e);
            let outcome = RecheckOutcome::SpawnFailed {
                error: error.clone(),
            };
            (Err(error), outcome)
        }
        Ok(child) => {
            let child_pid = child.id();
            let wait = child.wait_with_output();
            tokio::pin!(wait);
            let finished = tokio::select! {
                r = &mut wait => Some(r),
                _ = tokio::time::sleep(timeout) => None,
            };
            match finished {
                None => {
                    // The deadline fired and `wait` is STILL ALIVE, so the
                    // python child is too and its generators are still ITS
                    // children. Reap them now, while that parent link is the
                    // proof of ownership; the child itself dies with `wait`
                    // when this scope ends (`kill_on_drop`).
                    let descendants_killed = match child_pid {
                        Some(pid) => {
                            vct_launcher_core::process::kill_descendants(pid, spawned_at)
                        }
                        // `id()` is `None` only once the child has been
                        // reaped, which cannot be true on this arm — and
                        // guessing a PID would be worse than doing nothing.
                        None => 0,
                    };
                    if descendants_killed > 0 {
                        tracing::warn!(
                            "[kg_summary] summary recheck timed out after {} s; \
                             signalled {} generator process(es) the child had \
                             spawned",
                            timeout.as_secs(),
                            descendants_killed
                        );
                    }
                    (
                        Err(format!(
                            "summary recheck timed out after {} s — safe to \
                             re-run; anything not regenerated stayed pending",
                            timeout.as_secs()
                        )),
                        RecheckOutcome::TimedOut {
                            after_secs: timeout.as_secs(),
                            descendants_killed,
                        },
                    )
                }
                Some(Err(e)) => {
                    // An io error from the wait/pipe-drain, not from the
                    // spawn itself — same class for the caller (the process
                    // could not be run to completion) and same vocabulary.
                    let error = format!("summary recheck spawn failed: {}", e);
                    let outcome = RecheckOutcome::SpawnFailed {
                        error: error.clone(),
                    };
                    (Err(error), outcome)
                }
                Some(Ok(output)) => {
                    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
                    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
                    let counts = parse_recheck_summary_line(&stdout);
                    let summary_line = stdout
                        .lines()
                        .rev()
                        .find(|l| l.contains(RECHECK_LINE_PREFIX))
                        .unwrap_or("")
                        .trim()
                        .to_string();
                    let view = SummaryRecheckView {
                        project_root: project_root.clone(),
                        exit_code: output.status.code().unwrap_or(-1).into(),
                        pending_before: counts.as_ref().map(|c| c.pending_before),
                        pending_after: counts.as_ref().map(|c| c.pending_after),
                        spawned: counts.as_ref().map(|c| c.spawned),
                        failures: counts.as_ref().map(|c| c.failures),
                        code_leg_skipped: counts
                            .as_ref()
                            .map_or(false, |c| c.code_leg_skipped),
                        summary_line,
                    };
                    if output.status.success() {
                        let outcome = RecheckOutcome::Completed {
                            pending_before: view.pending_before,
                            pending_after: view.pending_after,
                            failures: view.failures,
                        };
                        (Ok(view), outcome)
                    } else {
                        let tail = if stderr.trim().is_empty() {
                            stdout.trim().to_string()
                        } else {
                            stderr.trim().to_string()
                        };
                        let outcome = RecheckOutcome::ExitNonZero {
                            exit_code: view.exit_code,
                        };
                        (
                            Err(format!(
                                "summary recheck exited {} — {}",
                                view.exit_code, tail
                            )),
                            outcome,
                        )
                    }
                }
            }
        }
    };

    // Soft-fail, as every audit call in this crate is: a DB that will not
    // take the row must not turn a completed recheck into an error.
    let _ = db.audit(
        "summary_recheck",
        audit_project_id,
        None,
        &recheck_audit_detail(&project_root, &outcome),
    );
    result
}

/// Public entry point used by `create_project_v2` (and the retry command).
/// Spawns a background task; never blocks. The caller has already inserted
/// a `pending` row into `kg_summaries`.
pub fn spawn_initial_summary(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    // async_runtime::spawn, not tokio::spawn — sync fn, also called from
    // setup()/main thread via the boot-resume sweep (no reactor context).
    tauri::async_runtime::spawn(async move {
        run_summary_task(app, project_id, project_name, folder_path).await;
    });
}

/// Launcher-boot resume sweep. Mirrors `kg_sync::resume_pending_syncs` 1:1
/// — see that function's docstring for the two-phase rationale (mark
/// stale-running as failed, then re-spawn pending). Soft-fail at every
/// step. Returns (swept_running, respawned_pending) for the boot-log line.
///
/// Called from `lib.rs::setup()` after migrations have run. Boot order vs.
/// the code-graph + kg-sync resume sweeps is incidental; the three are
/// independent and can run in any sequence.
///
/// Defect B (v0.2.68) — F6 boot-resume gate: `skip` is the set of project
/// IDs whose `project_setups` row is NOT terminal. Those projects are
/// re-driven by `project_setup::resume_pending_setups` (which re-runs the
/// bundle that drops the `generate-kg-summary.py` wrapper and re-queues this
/// backfill as `pending`); resuming HERE would race the wrapper back onto
/// disk. We skip them. Mirrors `codegraph::resume_pending_builds`.
pub fn resume_pending_summaries(
    app: &AppHandle,
    skip: &std::collections::HashSet<String>,
) -> (usize, usize) {
    let db = app.state::<Db>();

    // Phase 1: stale-running sweep.
    let swept = match db.mark_orphaned_running_kg_summaries_failed(
        "launcher crashed mid-run; click Retry to re-run",
    ) {
        Ok(n) => n,
        Err(e) => {
            tracing::warn!(
                "[vct] warning: kg-summary stale-running sweep failed: {}. \
                 Stale rows (if any) will appear as 'running' indefinitely; \
                 user can click Re-build KG summaries to recover.",
                e
            );
            0
        }
    };

    // Phase 2: respawn pending.
    let pending_ids = match db.list_pending_kg_summaries() {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(
                "[vct] warning: kg-summary pending-list lookup failed: {}. \
                 Queued summary backfills (if any) will not auto-resume this boot.",
                e
            );
            return (swept, 0);
        }
    };

    let mut respawned = 0usize;
    for pid in &pending_ids {
        // F6 gate: skip projects whose async setup is still incomplete —
        // `resume_pending_setups` re-drives them (re-landing the summary
        // wrapper + re-queuing this backfill in the correct order).
        if skip.contains(pid) {
            continue;
        }
        let project = match db.get_project(pid) {
            Ok(Some(p)) => p,
            Ok(None) => {
                tracing::warn!(
                    "[vct] warning: pending kg-summary references missing project {}; skipping",
                    pid
                );
                continue;
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] warning: lookup for pending kg-summary {}: {}; skipping",
                    pid, e
                );
                continue;
            }
        };
        spawn_initial_summary(
            app.clone(),
            project.id,
            project.name,
            project.folder_path,
        );
        respawned += 1;
    }
    (swept, respawned)
}

/// True when the project still exists in the launcher DB. Used by
/// `run_summary_task` to short-circuit if the user unregistered the
/// project mid-backfill — mirrors `kg_sync::project_still_exists`.
fn project_still_exists(app: &AppHandle, project_id: &str) -> bool {
    app.state::<Db>()
        .get_project(project_id)
        .map(|opt| opt.is_some())
        .unwrap_or(true)
}

/// Body of the spawned task. Errors here are recorded in the summary row,
/// never propagated. Each transition emits a `kg-summary-progress` event
/// so the GUI updates live. Structure mirrors `kg_sync::run_sync_task`.
async fn run_summary_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    let started_at = chrono::Utc::now().timestamp_millis();

    // Race check #0 (defensive): the spawn could be enqueued and the user
    // could unregister before the task picks up. Bail before any DB write
    // or event emit. Same pattern as kg_sync / codegraph.
    if !project_still_exists(&app, &project_id) {
        return;
    }

    // 1. Mark RUNNING + emit. Pre-check the directory so the user sees a
    //    "scanning…" banner the moment project create returns.
    upsert_quiet(
        &app,
        &project_id,
        summary_status::RUNNING,
        Some(started_at),
        None,
        None,
        0, 0, 0, 0, 0,
        None, None, None,
    );
    emit_summary(
        &app,
        &project_id,
        summary_status::RUNNING,
        ProgressCounts::zero(),
        None,
        Some("scan"),
        None,
    );

    // 2. Pre-check: any markdown files at all under knowledge/? Nothing
    //    to backfill in an empty project.
    let folder = std::path::Path::new(&folder_path);
    let knowledge_dir = folder.join("knowledge");
    let md_files = enumerate_markdown_files(&knowledge_dir);

    if md_files.is_empty() {
        if !project_still_exists(&app, &project_id) {
            return;
        }
        let finished_at = chrono::Utc::now().timestamp_millis();
        upsert_quiet(
            &app,
            &project_id,
            summary_status::SKIPPED,
            Some(started_at),
            Some(finished_at),
            Some(finished_at - started_at),
            0, 0, 0, 0, 0,
            None,
            Some("no knowledge/**/*.md files to summarise"),
            None,
        );
        emit_summary(
            &app,
            &project_id,
            summary_status::SKIPPED,
            ProgressCounts::zero(),
            None,
            None,
            Some("no knowledge/**/*.md files to summarise"),
        );
        return;
    }

    let nodes_total = md_files.len() as u32;

    // 3. Resolve the summariser script. Bundle install drops it at
    //    `<project>/.claude/scripts/generate-kg-summary.py`.
    let script = match resolve_summary_script(folder) {
        Some(p) => p,
        None => {
            if !project_still_exists(&app, &project_id) {
                return;
            }
            finalize_failed(
                &app,
                &project_id,
                started_at,
                nodes_total,
                "generate-kg-summary.py not found (looked in project, launcher install, $PATH). \
                 The launcher's bundle install may have failed — check the \
                 install-bundle warnings emitted during project create."
                    .to_string(),
                None,
            );
            return;
        }
    };

    // 4. Resolve the venv python (the summariser is `.py`, invoked
    //    directly as `<venv-python> <script> <file>` — same as the
    //    PostToolUse hook does it, see templates/hooks/kg-summary-generator.sh).
    let venv_python = match resolve_venv_python(folder, &script) {
        Some(p) => p,
        None => {
            if !project_still_exists(&app, &project_id) {
                return;
            }
            finalize_failed(
                &app,
                &project_id,
                started_at,
                nodes_total,
                "Python venv not found (looked at <install>/.venv and \
                 <install>/claude_mcp_servers/.venv on both POSIX and Windows \
                 layouts). The orchestrator install may be incomplete — run \
                 the launcher's first-install script."
                    .to_string(),
                None,
            );
            return;
        }
    };

    // 5. Populate env settings from launcher state. Same path
    //    `create_project_v2` uses to write the .env / .claude/env files —
    //    keeps the summariser's collection-targeting consistent with what
    //    the subsequent on-edit hook will use when the user starts a
    //    Claude session.
    let env_settings = {
        let db = app.state::<Db>();
        project_env_settings::populate(&db, &project_name, Some(&project_id))
    };

    emit_summary(
        &app,
        &project_id,
        summary_status::RUNNING,
        ProgressCounts {
            nodes_total,
            ..ProgressCounts::zero()
        },
        None,
        Some("summarise"),
        None,
    );

    // 6. Walk and invoke. Per-node subprocess, sequential — the summariser
    //    is already async-LLM-bound; running 50 concurrent Haiku calls
    //    would not be faster (rate-limited) and would obliterate the
    //    "current node X of N" progress signal in the GUI.
    let outcome = run_subprocess_loop(
        &app,
        &project_id,
        &venv_python,
        &script,
        &md_files,
        folder,
        &env_settings,
    )
    .await;

    let finished_at = chrono::Utc::now().timestamp_millis();

    // 7. Persist + emit terminal event. Race check (mirrors kg_sync):
    //    if the user unregistered while the loop was running, skip the
    //    writes quietly.
    if !project_still_exists(&app, &project_id) {
        return;
    }
    upsert_quiet(
        &app,
        &project_id,
        &outcome.status,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        outcome.counts.nodes_total,
        outcome.counts.nodes_succeeded,
        outcome.counts.nodes_unchanged,
        outcome.counts.nodes_failed,
        outcome.counts.nodes_skipped,
        outcome.backend.as_deref(),
        outcome.error_message.as_deref(),
        outcome.log_tail.as_deref(),
    );
    emit_summary(
        &app,
        &project_id,
        &outcome.status,
        outcome.counts,
        outcome.backend.as_deref(),
        None,
        outcome.error_message.as_deref(),
    );
}

/// Aggregated subprocess-loop result. Held only inside `run_summary_task`
/// so nothing externally references this struct.
struct LoopOutcome {
    status: String,
    counts: ProgressCounts,
    backend: Option<String>,
    error_message: Option<String>,
    log_tail: Option<String>,
}

#[derive(Clone, Copy, Debug)]
struct ProgressCounts {
    nodes_total: u32,
    nodes_succeeded: u32,
    nodes_unchanged: u32,
    nodes_failed: u32,
    nodes_skipped: u32,
}

impl ProgressCounts {
    fn zero() -> Self {
        Self {
            nodes_total: 0,
            nodes_succeeded: 0,
            nodes_unchanged: 0,
            nodes_failed: 0,
            nodes_skipped: 0,
        }
    }
}

/// Walk `md_files` and invoke `<venv_python> <script> <file>` for each.
/// Emits a `kg-summary-progress` event after each invocation so the GUI
/// counter advances live. Aggregates per-node outcomes into terminal
/// counts + a unified log tail.
#[allow(clippy::too_many_arguments)]
async fn run_subprocess_loop(
    app: &AppHandle,
    project_id: &str,
    venv_python: &std::path::Path,
    script: &std::path::Path,
    md_files: &[std::path::PathBuf],
    project_folder: &std::path::Path,
    env_settings: &ProjectEnvSettings,
) -> LoopOutcome {
    let nodes_total = md_files.len() as u32;
    let mut counts = ProgressCounts {
        nodes_total,
        ..ProgressCounts::zero()
    };
    let mut combined_log = String::new();
    let mut backend_seen: Option<String> = None;
    let mut consecutive_failures: u32 = 0;
    let mut early_skip_reason: Option<String> = None;

    for (idx, md_file) in md_files.iter().enumerate() {
        // Race-check inside the loop — if the user unregistered we stop
        // burning subprocesses immediately. We deliberately don't write
        // the row here (the outer task will short-circuit on the same
        // check before its terminal upsert).
        if !project_still_exists(app, project_id) {
            return LoopOutcome {
                status: summary_status::FAILED.to_string(),
                counts,
                backend: backend_seen,
                error_message: Some("project unregistered during run".to_string()),
                log_tail: Some(tail_log(&combined_log)),
            };
        }

        let node_outcome = invoke_summariser_once(
            venv_python,
            script,
            md_file,
            project_folder,
            env_settings,
        )
        .await;

        match node_outcome {
            NodeOutcome::Succeeded { backend, log } => {
                counts.nodes_succeeded = counts.nodes_succeeded.saturating_add(1);
                consecutive_failures = 0;
                if backend_seen.is_none() && !backend.is_empty() {
                    backend_seen = Some(backend);
                }
                append_log(&mut combined_log, &log);
            }
            NodeOutcome::Unchanged { log } => {
                counts.nodes_unchanged = counts.nodes_unchanged.saturating_add(1);
                consecutive_failures = 0;
                append_log(&mut combined_log, &log);
            }
            NodeOutcome::NoBackend { log } => {
                // First node already detected "no backend available" —
                // hard-stop. Re-invoking the script for every node would
                // just print the same warning N more times. Mark every
                // unvisited node as skipped so the count totals add up.
                counts.nodes_skipped = counts.nodes_skipped.saturating_add(1);
                let remaining = nodes_total.saturating_sub((idx as u32) + 1);
                counts.nodes_skipped = counts.nodes_skipped.saturating_add(remaining);
                early_skip_reason = Some(no_backend_reason(&log));
                append_log(&mut combined_log, &log);
                if backend_seen.is_none() {
                    backend_seen = Some("skip".to_string());
                }
                break;
            }
            NodeOutcome::Failed { log, error } => {
                counts.nodes_failed = counts.nodes_failed.saturating_add(1);
                consecutive_failures = consecutive_failures.saturating_add(1);
                append_log(&mut combined_log, &log);
                append_log(
                    &mut combined_log,
                    &format!("[vct] node failed: {} — {}", md_file.display(), error),
                );

                if consecutive_failures >= SUBPROCESS_FAIL_FAST_THRESHOLD {
                    let remaining = nodes_total.saturating_sub((idx as u32) + 1);
                    counts.nodes_skipped = counts.nodes_skipped.saturating_add(remaining);
                    return LoopOutcome {
                        status: summary_status::FAILED.to_string(),
                        counts,
                        backend: backend_seen,
                        error_message: Some(format!(
                            "{} consecutive subprocess failures (bailing on remaining {} nodes). \
                             Last error: {}",
                            SUBPROCESS_FAIL_FAST_THRESHOLD, remaining, error
                        )),
                        log_tail: Some(tail_log(&combined_log)),
                    };
                }
            }
        }

        // Emit progress after each node so the banner counter advances.
        emit_summary(
            app,
            project_id,
            summary_status::RUNNING,
            counts,
            backend_seen.as_deref(),
            Some("summarise"),
            None,
        );
    }

    // Done walking. Decide terminal status.
    let log_tail = tail_log(&combined_log);

    if let Some(reason) = early_skip_reason {
        // No backend → terminal skipped.
        return LoopOutcome {
            status: summary_status::SKIPPED.to_string(),
            counts,
            backend: backend_seen.or_else(|| Some("skip".to_string())),
            error_message: Some(reason),
            log_tail: Some(log_tail),
        };
    }

    // Everything failed but threshold not hit (e.g. nodes_total <
    // SUBPROCESS_FAIL_FAST_THRESHOLD and they all failed).
    if counts.nodes_failed > 0
        && counts.nodes_succeeded == 0
        && counts.nodes_unchanged == 0
    {
        return LoopOutcome {
            status: summary_status::FAILED.to_string(),
            counts,
            backend: backend_seen,
            error_message: Some(format!(
                "all {} node(s) failed to summarise — see log tail",
                counts.nodes_failed
            )),
            log_tail: Some(log_tail),
        };
    }

    LoopOutcome {
        status: summary_status::SUCCESS.to_string(),
        counts,
        backend: backend_seen,
        error_message: None,
        log_tail: Some(log_tail),
    }
}

/// Per-node summariser invocation result.
enum NodeOutcome {
    /// Script wrote a new entry. `backend` is the backend it picked
    /// (parsed out of stdout's "KG-summary backend: …" line) — empty
    /// string if the line wasn't seen.
    Succeeded { backend: String, log: String },
    /// Script detected hash-match and exited 0. No backend invocation.
    Unchanged { log: String },
    /// Script logged "no backend available" and exited 0. The first time
    /// we see this we hard-stop the loop.
    NoBackend { log: String },
    /// Subprocess exited non-zero or panicked.
    Failed { log: String, error: String },
}

/// Run `<venv_python> <script> <md_file>` once. Captures stdout+stderr
/// for log aggregation; classifies into a `NodeOutcome` by inspecting the
/// summariser's well-known stdout markers (defined as constants near the
/// top of this module to keep the contract obvious).
async fn invoke_summariser_once(
    venv_python: &std::path::Path,
    script: &std::path::Path,
    md_file: &std::path::Path,
    project_folder: &std::path::Path,
    env_settings: &ProjectEnvSettings,
) -> NodeOutcome {
    use tokio::io::AsyncReadExt;

    let mut cmd = tokio::process::Command::new(venv_python).silent();
    cmd.arg(script)
        .arg(md_file)
        // KG_PROJECT_ROOT scopes the summariser's `.node_formats.json`
        // write to this project (vs. defaulting to the orchestrator's
        // own knowledge/). See generate-kg-summary.py:45.
        .env("KG_PROJECT_ROOT", project_folder.as_os_str())
        .env("PROJECT_NAME", &env_settings.project_name)
        .env("KG_COLLECTION", &env_settings.kg_collection)
        .env("WEAVIATE_URL", &env_settings.weaviate_url)
        // The summariser reads OLLAMA_URL as a fallback when
        // KG_SUMMARY_OLLAMA_URL is unset. Pass both so the env-override
        // path matches the .claude/env block the launcher writes for
        // the on-edit hook.
        .env("KG_SUMMARY_OLLAMA_URL", &env_settings.ollama_url)
        .env("OLLAMA_URL", &env_settings.ollama_url)
        // Suppress Claude Code's auto-memory injection if the summariser
        // falls through to the `claude` CLI backend. Matches the env the
        // PostToolUse hook passes (kg-summary-generator.sh).
        .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
        // Don't inherit the launcher's working dir; pin to a neutral
        // path. The summariser resolves its own paths relative to
        // KG_PROJECT_ROOT, so cwd doesn't matter for correctness.
        .current_dir(std::env::temp_dir())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return NodeOutcome::Failed {
                log: String::new(),
                error: format!("could not spawn generate-kg-summary.py: {}", e),
            };
        }
    };

    let mut stdout_buf = String::new();
    let mut stderr_buf = String::new();

    // Bug-3 v0.2.x (2026-05-12): drain stdout + stderr concurrently.
    //
    // The previous sequential pattern (`stdout.read_to_string().await`
    // then `stderr.read_to_string().await`) is structurally identical
    // to the kg_sync.rs deadlock fixed alongside this: if the Python
    // subprocess fills the ~64 KiB stderr pipe buffer before exiting,
    // its next stderr write blocks in `anon_pipe_write`, no further
    // stdout flows, and the launcher's stdout reader hangs forever
    // because stderr is sequenced after stdout. In practice
    // generate-kg-summary.py emits very little per file and won't trip
    // the buffer, but the structural defect is identical and the fix
    // is cheap. `tokio::join!` drives both reads concurrently so
    // either side can drain without back-pressuring the other.
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdout_fut = async {
        if let Some(mut s) = stdout_pipe {
            let _ = s.read_to_string(&mut stdout_buf).await;
        }
    };
    let stderr_fut = async {
        if let Some(mut s) = stderr_pipe {
            let _ = s.read_to_string(&mut stderr_buf).await;
        }
    };
    tokio::join!(stdout_fut, stderr_fut);

    let exit_status = child.wait().await;

    // Compose the per-node log. Used to populate the aggregate log_tail
    // shown in the failure banner's "Show details" expansion.
    let mut log = String::new();
    log.push_str(&format!("--- {} ---\n", md_file.display()));
    log.push_str(&stdout_buf);
    if !stderr_buf.is_empty() {
        log.push_str("[stderr]\n");
        log.push_str(&stderr_buf);
    }

    let combined_text = format!("{}\n{}", stdout_buf, stderr_buf);

    match exit_status {
        Ok(s) if s.success() => {
            // Three "success" sub-paths to disambiguate. Order matters:
            //   1. "no backend available" — exit 0 but nothing was written.
            //   2. "unchanged (hash match)" — exit 0, cheap no-op.
            //   3. otherwise — entry was written.
            if combined_text.contains(NO_BACKEND_MARKER) {
                NodeOutcome::NoBackend { log }
            } else if combined_text.contains(UNCHANGED_MARKER) {
                NodeOutcome::Unchanged { log }
            } else {
                let backend = parse_backend_from_stdout(&stdout_buf).unwrap_or_default();
                NodeOutcome::Succeeded { backend, log }
            }
        }
        Ok(s) => {
            let exit_code = s.code().unwrap_or(-1);
            // Snip a tight error message — prefer the last stderr line,
            // fall back to last stdout line.
            let snippet = stderr_buf
                .lines()
                .rev()
                .find(|l| !l.trim().is_empty())
                .or_else(|| stdout_buf.lines().rev().find(|l| !l.trim().is_empty()))
                .unwrap_or("")
                .chars()
                .take(200)
                .collect::<String>();
            NodeOutcome::Failed {
                log,
                error: format!(
                    "exit {}: {}",
                    exit_code,
                    if snippet.is_empty() { "no output" } else { &snippet },
                ),
            }
        }
        Err(e) => NodeOutcome::Failed {
            log,
            error: format!("wait failed: {}", e),
        },
    }
}

/// Parse the summariser's "KG-summary backend: cli|ollama|api|skip (…)"
/// line out of stdout. Returns None if the line wasn't seen.
///
/// We scan every line because the backend-detection log line is buried
/// between header progress lines in the summariser's output (see
/// `generate-kg-summary.py::select_backend`). Don't `?` on
/// `strip_prefix` inside the loop — that short-circuits the WHOLE
/// function on the first non-matching line.
///
/// **The LAST match wins, not the first** (v0.2.92 WP-Q2). WP-Q's circuit
/// breaker made one node emit the line more than once: `call_llm` demotes a
/// tier that stops serving, drops the cached choice, and re-enters
/// `select_backend`, which logs the replacement tier. So a rate-limited run
/// prints `backend: cli`, then `backend 'cli' demoted`, then
/// `backend: ollama` — and first-match reported the tier that FAILED as the
/// tier that produced the summary. The sidecar was already honest here (it
/// records `_BACKEND_CACHE["choice"]`, i.e. the tier that answered); only
/// this row lagged, so the launcher pill disagreed with the file on disk.
/// Later lines in one node's stdout are strictly later selections, so the
/// last one is the tier that served.
fn parse_backend_from_stdout(stdout: &str) -> Option<String> {
    let mut last: Option<String> = None;
    for line in stdout.lines() {
        let t = line.trim_start();
        let Some(after) = t.strip_prefix("KG-summary backend:") else {
            continue;
        };
        let word: String = after
            .chars()
            .skip_while(|c| c.is_whitespace())
            .take_while(|c| c.is_ascii_alphabetic())
            .collect();
        if !word.is_empty() {
            last = Some(word);
        }
    }
    last
}

/// The install hint — correct only when nothing is installed.
const NO_BACKEND_INSTALL_HINT: &str =
    "no backend available — install the `claude` CLI (preferred), start \
     Ollama at the configured URL, or set ANTHROPIC_API_KEY. Summaries will \
     also generate incrementally as you edit nodes in Claude Code sessions.";

/// Substring that distinguishes "every tier is cooling down" from "nothing
/// is installed". Must match `summary_backends.select_backend()`'s
/// cooling-down branch (pinned by `cooling_down_marker_matches_script_log_line`).
const COOLING_DOWN_MARKER: &str = "cooling down after a usage-limit";

/// Pick the skip reason to store for a `NoBackend` outcome.
///
/// v0.2.92 WP-Q2: "install the `claude` CLI" is the WRONG advice for a user
/// whose CLI is installed and merely rate-limited — WP-Q's breaker demotes
/// that tier, and once every tier is demoted `select_backend` reports
/// `no backend available — … cooling down …, retry after the cooldown`.
/// Telling that user to install what they already have sends them to fix
/// the one thing that is not broken. When the script said "cooling down",
/// we surface ITS line verbatim (it names which tiers and why); otherwise
/// the install hint stands.
///
/// Both branches keep the `no backend available` substring, so anything
/// grepping the stored reason for it keeps working.
fn no_backend_reason(log: &str) -> String {
    for line in log.lines() {
        if line.contains(NO_BACKEND_MARKER) && line.contains(COOLING_DOWN_MARKER) {
            return line.trim().to_string();
        }
    }
    NO_BACKEND_INSTALL_HINT.to_string()
}

// ─── Helpers (DB / event / log) ──────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn upsert_quiet(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    started_at: Option<i64>,
    finished_at: Option<i64>,
    duration_ms: Option<i64>,
    nodes_total: u32,
    nodes_succeeded: u32,
    nodes_unchanged: u32,
    nodes_failed: u32,
    nodes_skipped: u32,
    backend: Option<&str>,
    error_message: Option<&str>,
    log_tail: Option<&str>,
) {
    let db = app.state::<Db>();
    if let Err(e) = db.upsert_kg_summary(
        project_id,
        status,
        started_at,
        finished_at,
        duration_ms,
        nodes_total,
        nodes_succeeded,
        nodes_unchanged,
        nodes_failed,
        nodes_skipped,
        backend,
        error_message,
        log_tail,
    ) {
        tracing::warn!(
            "[vct] warning: kg_summaries upsert failed for {}: {}",
            project_id, e
        );
    }
}

fn finalize_failed(
    app: &AppHandle,
    project_id: &str,
    started_at: i64,
    nodes_total: u32,
    error: String,
    log_tail: Option<String>,
) {
    let finished_at = chrono::Utc::now().timestamp_millis();
    upsert_quiet(
        app,
        project_id,
        summary_status::FAILED,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        nodes_total, 0, 0, 0, 0,
        None,
        Some(&error),
        log_tail.as_deref(),
    );
    emit_summary(
        app,
        project_id,
        summary_status::FAILED,
        ProgressCounts {
            nodes_total,
            ..ProgressCounts::zero()
        },
        None,
        None,
        Some(&error),
    );
}

fn emit_summary(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    counts: ProgressCounts,
    backend: Option<&str>,
    current_phase: Option<&str>,
    error: Option<&str>,
) {
    let payload = KgSummaryView {
        project_id: project_id.to_string(),
        status: status.to_string(),
        started_at_iso: None,
        finished_at_iso: None,
        duration_ms: None,
        nodes_total: counts.nodes_total,
        nodes_succeeded: counts.nodes_succeeded,
        nodes_unchanged: counts.nodes_unchanged,
        nodes_failed: counts.nodes_failed,
        nodes_skipped: counts.nodes_skipped,
        backend: backend.map(|s| s.to_string()),
        error_message: error.map(|s| s.to_string()),
        log_tail: None,
        current_phase: current_phase.map(|s| s.to_string()),
    };
    let _ = app.emit(SUMMARY_EVENT, payload);
}

/// Append `chunk` to `accum`, char-boundary-safe truncation to keep the
/// growing buffer small. We aggressively trim to ~5x the LOG_TAIL_MAX
/// during the walk so we don't accumulate megabytes for projects with
/// 1000+ nodes.
fn append_log(accum: &mut String, chunk: &str) {
    accum.push_str(chunk);
    accum.push('\n');
    const CAP: usize = crate::db::log_tail::LOG_TAIL_MAX_BYTES * 5;
    if accum.len() > CAP {
        // Slice on a char boundary to keep non-ASCII output (qwen/gemma
        // may print Unicode) from panicking.
        let cut_at = accum.len() - crate::db::log_tail::LOG_TAIL_MAX_BYTES * 3;
        let mut idx = cut_at;
        while idx < accum.len() && !accum.is_char_boundary(idx) {
            idx += 1;
        }
        let kept = format!("…\n{}", &accum[idx..]);
        *accum = kept;
    }
}

/// Tail the last N bytes of subprocess output. Slice on a char boundary
/// so non-ASCII output doesn't panic. Mirrors `kg_sync::tail_log`.
fn tail_log(s: &str) -> String {
    // v0.2.54 Track J: delegates to the shared char-boundary-safe
    // capping helper (was one of three near-identical copies across
    // the codegraph / kg_sync / kg_summary command modules).
    crate::db::log_tail::cap_log_tail(s)
}

// ─── Pre-check + script / interpreter resolution ─────────────────────────

/// Enumerate every `.md` file under `root` recursively. Returns sorted
/// absolute paths so the GUI sees a stable iteration order across reruns.
///
/// Bounded depth (16) to keep us out of pathological symlink-loop
/// disasters, but practically unreachable — knowledge/ trees are flat to
/// 2-3 levels in every project the launcher has registered. Ignore
/// patterns mirror `kg_sync::count_markdown_files` (hidden dirs +
/// node_modules / __pycache__ / venv / .venv / target / dist) so the
/// two scans agree on which files exist.
pub(crate) fn enumerate_markdown_files(root: &std::path::Path) -> Vec<std::path::PathBuf> {
    fn walk(
        dir: &std::path::Path,
        depth: usize,
        max_depth: usize,
        out: &mut Vec<std::path::PathBuf>,
    ) {
        if depth > max_depth {
            return;
        }
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name();
            let name_str = name.to_string_lossy();
            if path.is_dir() {
                if name_str.starts_with('.') {
                    continue;
                }
                if matches!(
                    name_str.as_ref(),
                    "node_modules" | "__pycache__" | "venv" | ".venv" | "target" | "dist"
                ) {
                    continue;
                }
                walk(&path, depth + 1, max_depth, out);
            } else if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                if ext.eq_ignore_ascii_case("md") {
                    out.push(path);
                }
            }
        }
    }
    let mut out = Vec::new();
    walk(root, 0, 16, &mut out);
    out.sort();
    out
}

/// Look for `generate-kg-summary.py` via the shared four-tier ladder
/// (`<project>/.claude/scripts` → `$VCT_LAUNCHER_SCRIPTS_DIR` →
/// sibling-of-exe → PATH). Unlike `kg-sync`, the summariser is the SAME
/// `.py` file on every OS — invocation goes through the venv-python
/// interpreter rather than a shell wrapper, so there is no `.ps1` variant.
///
/// v0.2.77 (Part 7c task 3): this was a byte-for-byte copy of
/// `kg_sync::resolve_kg_sync_script`'s ladder; it now delegates to
/// `vct_launcher_core::paths::resolve_installed_script` (one home).
/// Behaviour preserved (the PATH tier now uses `std::env::split_paths`).
pub(crate) fn resolve_summary_script(
    project_folder: &std::path::Path,
) -> Option<std::path::PathBuf> {
    // v0.2.92: route through the guarded resolver (same ladder + the
    // stale-wrapper marker check). `generate-kg-summary.py` carries no
    // `$VCT_INSTALL_ROOT` marker in its shipped form, so the guard derives
    // "not marker-bearing" and the project-local copy is trusted exactly as
    // before — the point is that it no longer DEPENDS on that: the day the
    // shipped script grows the ladder, this call site inherits the check
    // instead of silently staying the one unguarded spawn site.
    crate::commands::codegraph::resolve_bundled_script(
        project_folder,
        "generate-kg-summary.py",
    )
}

/// Find a venv python that has the orchestrator's dependencies installed.
/// Probes (in order):
///
///   1. The script's grandparent + venv layouts (the install root the
///      bundle-install dropped scripts under).
///   2. `<project>/.venv` (project-local OSS layout).
///   3. `<project>/claude_mcp_servers/.venv` (legacy private-orch layout).
///   4. The launcher binary's own ancestors walked up 3-5 hops (for
///      bundled launcher installs).
///
/// Matches `codegraph.rs::looks_like_install_root` cross-platform venv
/// shapes (POSIX `.venv/bin/python(3)` + Windows `.venv/Scripts/python.exe`).
/// Also mirrors `templates/scripts/kg-sync` / `kg-sync.ps1` which probe
/// the same two candidate layouts.
pub(crate) fn resolve_venv_python(
    project_folder: &std::path::Path,
    script: &std::path::Path,
) -> Option<std::path::PathBuf> {
    fn venv_python_in(root: &std::path::Path) -> Option<std::path::PathBuf> {
        for layout in [
            root.join(".venv"),
            root.join("claude_mcp_servers").join(".venv"),
        ] {
            // POSIX
            let posix1 = layout.join("bin").join("python");
            if posix1.is_file() {
                return Some(posix1);
            }
            let posix2 = layout.join("bin").join("python3");
            if posix2.is_file() {
                return Some(posix2);
            }
            // Windows
            let win = layout.join("Scripts").join("python.exe");
            if win.is_file() {
                return Some(win);
            }
        }
        None
    }

    // 1. The install root the script lives under (script -> .claude/scripts
    //    -> .claude -> install root).
    if let Some(install_root) = script
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
    {
        if let Some(py) = venv_python_in(install_root) {
            return Some(py);
        }
    }

    // 2-3. The project folder itself (in case the bundle picked a
    //    sibling-of-exe script but the project DOES have its own venv).
    if let Some(py) = venv_python_in(project_folder) {
        return Some(py);
    }

    // 4. Walk up from the launcher binary.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..6 {
                if let Some(py) = venv_python_in(&cur) {
                    return Some(py);
                }
                match cur.parent() {
                    Some(p) => cur = p.to_path_buf(),
                    None => break,
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmpdir(label: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-kgsummary-{}-{}",
            label,
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn enumerate_finds_md_files_recursively_and_sorts() {
        let d = tmpdir("enum-rec");
        fs::write(d.join("z.md"), b"# z").unwrap();
        fs::create_dir_all(d.join("sub/sub2")).unwrap();
        fs::write(d.join("sub/a.md"), b"# a").unwrap();
        fs::write(d.join("sub/sub2/m.md"), b"# m").unwrap();
        fs::write(d.join("sub/notes.txt"), b"not md").unwrap();
        let files = enumerate_markdown_files(&d);
        assert_eq!(files.len(), 3);
        // Sorted ascending — predictable iteration.
        assert!(files[0] < files[1]);
        assert!(files[1] < files[2]);
        // .txt excluded.
        for f in &files {
            assert!(f.extension().and_then(|e| e.to_str()) == Some("md"));
        }
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn enumerate_handles_missing_dir() {
        let bogus = std::env::temp_dir().join(format!(
            "definitely-not-{}",
            uuid::Uuid::new_v4()
        ));
        let files = enumerate_markdown_files(&bogus);
        assert!(files.is_empty());
    }

    #[test]
    fn enumerate_ignores_hidden_and_vendor_dirs() {
        let d = tmpdir("enum-ignore");
        fs::create_dir_all(d.join(".obsidian")).unwrap();
        fs::write(d.join(".obsidian/leak.md"), b"# leak").unwrap();
        fs::create_dir_all(d.join("node_modules")).unwrap();
        fs::write(d.join("node_modules/leak.md"), b"# leak").unwrap();
        fs::create_dir_all(d.join(".venv")).unwrap();
        fs::write(d.join(".venv/leak.md"), b"# leak").unwrap();
        fs::write(d.join("real.md"), b"# real").unwrap();
        let files = enumerate_markdown_files(&d);
        assert_eq!(files.len(), 1);
        assert!(files[0].file_name().unwrap() == "real.md");
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn enumerate_is_case_insensitive_on_extension() {
        // Same edge-case coverage as kg_sync::count_markdown_files.
        let d = tmpdir("enum-case");
        fs::write(d.join("a.md"), b"# a").unwrap();
        fs::write(d.join("b.MD"), b"# b").unwrap();
        fs::write(d.join("c.Md"), b"# c").unwrap();
        let files = enumerate_markdown_files(&d);
        assert!(!files.is_empty());
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_summary_finds_project_local_copy() {
        let d = tmpdir("resolve");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let p = scripts.join("generate-kg-summary.py");
        fs::write(&p, b"#!/usr/bin/env python3\nprint('ok')\n").unwrap();
        let resolved = resolve_summary_script(&d).expect("must resolve");
        assert_eq!(resolved, p);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_summary_returns_none_when_missing() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let d = tmpdir("resolve-miss");
        // No .claude/scripts/ at all.
        // Clear any env override so step 2 of the resolver doesn't pick
        // up someone else's stray dir.
        let saved = std::env::var("VCT_LAUNCHER_SCRIPTS_DIR").ok();
        std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR");
        let resolved = resolve_summary_script(&d);
        if let Some(prev) = saved {
            std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", prev);
        }
        // PATH may still happen to contain generate-kg-summary.py on a
        // dev machine where the orchestrator's .claude/scripts is in
        // PATH. Be tolerant — assert it's NOT the project-local path.
        if let Some(p) = resolved {
            assert!(!p.starts_with(&d), "should not resolve inside the bare project");
        }
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    #[cfg(unix)]
    fn resolve_venv_python_finds_posix_layout() {
        use std::os::unix::fs::PermissionsExt;
        let d = tmpdir("venv-posix");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let script = scripts.join("generate-kg-summary.py");
        fs::write(&script, b"#!/usr/bin/env python3\n").unwrap();

        let venv_bin = d.join(".venv").join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let py = venv_bin.join("python");
        fs::write(&py, b"#!/bin/sh\necho fake\n").unwrap();
        let mut perms = fs::metadata(&py).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&py, perms).unwrap();

        let resolved = resolve_venv_python(&d, &script).expect("must resolve");
        assert_eq!(resolved, py);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    #[cfg(unix)]
    fn resolve_venv_python_finds_legacy_mcp_servers_layout() {
        use std::os::unix::fs::PermissionsExt;
        let d = tmpdir("venv-legacy");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let script = scripts.join("generate-kg-summary.py");
        fs::write(&script, b"#!/usr/bin/env python3\n").unwrap();

        let venv_bin = d
            .join("claude_mcp_servers")
            .join(".venv")
            .join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let py = venv_bin.join("python3");
        fs::write(&py, b"#!/bin/sh\necho fake\n").unwrap();
        let mut perms = fs::metadata(&py).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&py, perms).unwrap();

        let resolved = resolve_venv_python(&d, &script).expect("must resolve");
        assert_eq!(resolved, py);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_venv_python_returns_none_when_missing() {
        let d = tmpdir("venv-miss");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let script = scripts.join("generate-kg-summary.py");
        fs::write(&script, b"#!/usr/bin/env python3\n").unwrap();
        // No venv anywhere reachable from this dir. The walk-up from the
        // launcher exe might still find one on the dev machine — we
        // tolerate that and only assert "if Some, it's at least a venv-
        // looking path".
        if let Some(py) = resolve_venv_python(&d, &script) {
            let s = py.to_string_lossy();
            assert!(
                s.contains(".venv"),
                "fallback resolution must still be a .venv path, got {}",
                s,
            );
        }
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn parse_backend_picks_word_after_colon() {
        let s = "Generating summaries for: Foo\n  KG-summary backend: ollama (qwen3.5:9b)\n";
        assert_eq!(parse_backend_from_stdout(s).as_deref(), Some("ollama"));

        let s = "  KG-summary backend: cli (claude on PATH)\n";
        assert_eq!(parse_backend_from_stdout(s).as_deref(), Some("cli"));

        let s = "  KG-summary backend: skip (forced via env)\n";
        assert_eq!(parse_backend_from_stdout(s).as_deref(), Some("skip"));
    }

    #[test]
    fn parse_backend_reports_the_tier_that_answered_not_the_one_that_failed() {
        // v0.2.92 WP-Q2. WP-Q's breaker made one node print the line twice:
        // `call_llm` demotes a tier that stops serving, drops the cached
        // choice, and re-enters `select_backend`, which logs the replacement.
        // First-match reported `cli` — the tier that FAILED — as the producer
        // of a summary Ollama actually wrote, so the launcher pill disagreed
        // with the sidecar (which was already honest).
        let demoted = "Generating summaries for: Foo\n\
                       \x20 KG-summary backend: cli (claude on PATH, smoke-test OK)\n\
                       \x20 KG-summary: backend 'cli' demoted — rate_limit/429; cooling down 900s\n\
                       \x20 KG-summary: falling through to the next backend tier\n\
                       \x20 KG-summary backend: ollama (qwen3.5:9b)\n";
        assert_eq!(parse_backend_from_stdout(demoted).as_deref(), Some("ollama"));
    }

    #[test]
    fn parse_backend_is_unchanged_when_there_was_no_demotion() {
        // The overwhelmingly common case must not move: one line, one answer.
        let s = "  KG-summary backend: cli (claude on PATH, smoke-test OK)\n\
                 \x20 Foo Title: generated in 3.2s\n";
        assert_eq!(parse_backend_from_stdout(s).as_deref(), Some("cli"));
    }

    #[test]
    fn cooling_down_marker_string_matches_script_log_line() {
        // Same defensive pin as `no_backend_marker_string_matches_script_log_line`:
        // a literal snippet of what `summary_backends.select_backend()` prints,
        // so renaming the script's message fails here instead of silently
        // reverting the launcher to the wrong advice.
        let canonical = "  KG-summary: no backend available — cli, ollama cooling \
                         down after a usage-limit / capacity failure. Nothing to \
                         install; retry after the cooldown. Skipping.";
        assert!(canonical.contains(COOLING_DOWN_MARKER));
        assert!(canonical.contains(NO_BACKEND_MARKER));
    }

    #[test]
    fn no_backend_reason_surfaces_the_cooling_down_line_not_the_install_hint() {
        // "install the `claude` CLI" is the WRONG advice for a user whose CLI
        // is installed and merely rate-limited — it sends them to fix the one
        // thing that is not broken.
        let log = "--- knowledge/a.md ---\n\
                   \x20 KG-summary: no backend available — cli, ollama cooling down \
                   after a usage-limit / capacity failure. Nothing to install; retry \
                   after the cooldown. Skipping.\n";
        let reason = no_backend_reason(log);
        assert!(reason.contains("retry after the cooldown"), "{reason}");
        assert!(!reason.contains("install the `claude` CLI"), "{reason}");
        // The substring anything greps for survives in BOTH branches.
        assert!(reason.contains(NO_BACKEND_MARKER), "{reason}");
    }

    #[test]
    fn no_backend_reason_keeps_the_install_hint_when_nothing_is_installed() {
        // The LEAVE-ALONE case: a genuinely empty machine still gets told what
        // to install.
        let log = "--- knowledge/a.md ---\n\
                   \x20 KG-summary: no backend available (no claude CLI, no Ollama at \
                   http://localhost:11435, no OPENAI_API_KEY, no ANTHROPIC_API_KEY). \
                   Skipping.\n";
        let reason = no_backend_reason(log);
        assert!(reason.contains("install the `claude` CLI"), "{reason}");
        assert!(reason.contains(NO_BACKEND_MARKER), "{reason}");
        // And a log with no marker at all falls back to the same hint.
        assert_eq!(no_backend_reason("nothing relevant here"), NO_BACKEND_INSTALL_HINT);
    }

    #[test]
    fn parse_backend_returns_none_when_marker_absent() {
        assert_eq!(parse_backend_from_stdout("some other output").as_deref(), None);
        assert_eq!(parse_backend_from_stdout("").as_deref(), None);
    }

    #[test]
    fn parse_recheck_line_parses_the_canonical_shape() {
        // Byte-for-byte the line `summary_health.py` prints (its shape is
        // pinned Python-side by tests/test_v0296_summary_quota_breaker.py;
        // this keeps the Rust parser honest against a rename).
        let line = "[summary-health] recheck: 3 pending before, 0 after; \
                    5 generator run(s), 0 failure(s)";
        let c = parse_recheck_summary_line(line).expect("must parse");
        assert_eq!(
            c,
            RecheckCounts {
                pending_before: 3,
                pending_after: 0,
                spawned: 5,
                failures: 0,
                code_leg_skipped: false,
            }
        );
    }

    #[test]
    fn parse_recheck_line_flags_the_code_leg_skip_suffix() {
        let line = "[summary-health] recheck: 2 pending before, 1 after; \
                    2 generator run(s), 1 failure(s); code leg SKIPPED \
                    (unresolved project name)";
        let c = parse_recheck_summary_line(line).expect("must parse");
        assert!(c.code_leg_skipped);
        assert_eq!((c.pending_before, c.pending_after, c.failures), (2, 1, 1));
    }

    #[test]
    fn parse_recheck_line_takes_the_last_matching_line() {
        // Last-wins, matching `parse_backend_from_stdout`: a later line is
        // a strictly later statement of the same run.
        let noisy = "node a: ok\n\
                     [summary-health] recheck: 9 pending before, 9 after; \
                     0 generator run(s), 0 failure(s)\n\
                     node b: ok\n\
                     [summary-health] recheck: 1 pending before, 0 after; \
                     1 generator run(s), 0 failure(s)\n";
        let c = parse_recheck_summary_line(noisy).expect("must parse");
        assert_eq!(c.pending_before, 1);
        assert_eq!(c.pending_after, 0);
    }

    #[test]
    fn parse_recheck_line_returns_none_when_absent_or_malformed() {
        assert!(parse_recheck_summary_line("no marker here").is_none());
        assert!(parse_recheck_summary_line("").is_none());
        // Prefix present but numbers garbled — the caller surfaces the raw
        // line rather than trusting a partial parse.
        assert!(parse_recheck_summary_line(
            "[summary-health] recheck: ??? pending before, x after"
        )
        .is_none());
        assert!(parse_recheck_summary_line(
            "[summary-health] recheck: 3 pending before"
        )
        .is_none());
    }

    #[test]
    fn recheck_argv_carries_the_verb_and_the_target_root() {
        let argv = recheck_argv(std::path::Path::new("/tmp/proj"));
        assert_eq!(argv[0], "summary-recheck");
        assert_eq!(argv[1], "--project-root");
        assert_eq!(argv[2], "/tmp/proj");
    }

    #[test]
    fn tail_log_truncates_long_output() {
        let big = "a".repeat(10_000);
        let tail = tail_log(&big);
        assert!(tail.len() < 5_000);
        assert!(tail.starts_with('…'));
    }

    #[test]
    fn tail_log_passes_through_short_output() {
        assert_eq!(tail_log("all good"), "all good");
    }

    #[test]
    fn append_log_keeps_under_cap() {
        let mut accum = String::new();
        // Push way more than the cap.
        for _ in 0..1000 {
            append_log(&mut accum, &"x".repeat(200));
        }
        // CAP = LOG_TAIL_MAX_BYTES * 5 = 20 KiB; after each append we trim
        // back to LOG_TAIL_MAX_BYTES * 3 + a tiny ellipsis prefix.
        assert!(
            accum.len() <= crate::db::kg_summaries::LOG_TAIL_MAX_BYTES * 5,
            "log should stay bounded, got {} bytes",
            accum.len()
        );
    }

    #[test]
    fn no_backend_marker_string_matches_script_log_line() {
        // Defensive: this constant must match what generate-kg-summary.py
        // actually prints. We keep a literal snippet of the canonical log
        // line here so a rename of the script's message will fail this
        // test (compared to silently mis-classifying every run as
        // "succeeded" or "failed" in production).
        let canonical = "  KG-summary: no backend available (no claude CLI, no Ollama at \
                         http://localhost:11435, no ANTHROPIC_API_KEY). Skipping.";
        assert!(canonical.contains(NO_BACKEND_MARKER));
    }

    #[test]
    fn unchanged_marker_string_matches_script_log_line() {
        let canonical = "  Foo Title: unchanged (hash match), skipping";
        assert!(canonical.contains(UNCHANGED_MARKER));
    }

    // ─── v0.2.96 (L-6): every recheck outcome reaches the audit trail ────

    #[test]
    fn audit_detail_names_the_timeout_as_the_outcome() {
        // The row that did not exist before: a deadline that fired is a
        // FACT about that project, and the child had already cleared the
        // quota breaker by the time it fired.
        let d = recheck_audit_detail(
            "/tmp/proj",
            &RecheckOutcome::TimedOut {
                after_secs: 1800,
                descendants_killed: 2,
            },
        );
        assert_eq!(d["outcome"], "timed_out");
        assert_eq!(d["timeout_secs"], 1800);
        // L-7: how many generator processes the deadline had to reap is
        // part of the record — a run that left work half-done looks
        // different from one that timed out with nothing in flight.
        assert_eq!(d["descendants_killed"], 2);
        assert_eq!(d["project_root"], "/tmp/proj");
        // No invented counts: a timed-out run did not observe "0 pending".
        assert!(d.get("pending_before").is_none());
        assert!(d.get("pending_after").is_none());
    }

    #[test]
    fn audit_detail_names_the_other_three_outcomes_too() {
        let ok = recheck_audit_detail(
            "/tmp/p",
            &RecheckOutcome::Completed {
                pending_before: Some(3),
                pending_after: Some(0),
                failures: Some(1),
            },
        );
        assert_eq!(ok["outcome"], "completed");
        assert_eq!(ok["pending_before"], 3);
        assert_eq!(ok["pending_after"], 0);
        assert_eq!(ok["failures"], 1);

        let bad = recheck_audit_detail("/tmp/p", &RecheckOutcome::ExitNonZero { exit_code: 2 });
        assert_eq!(bad["outcome"], "exit_nonzero");
        assert_eq!(bad["exit_code"], 2);

        let spawn = recheck_audit_detail(
            "/tmp/p",
            &RecheckOutcome::SpawnFailed { error: "no such file".into() },
        );
        assert_eq!(spawn["outcome"], "spawn_failed");
        assert_eq!(spawn["error"], "no such file");
    }

    #[test]
    fn a_completed_run_that_parsed_nothing_records_nulls_not_zeros() {
        // The counts are Option because the summary line may not parse. A
        // missing count must stay missing in the trail — a 0 there would
        // read as "nothing was pending", which is a different claim.
        let d = recheck_audit_detail(
            "/tmp/p",
            &RecheckOutcome::Completed {
                pending_before: None,
                pending_after: None,
                failures: None,
            },
        );
        assert_eq!(d["outcome"], "completed");
        assert!(d["pending_before"].is_null());
        assert!(d["failures"].is_null());
    }

    /// WIRING, not just the helper: drive the REAL `run_recheck_and_audit`
    /// into its timeout branch with a child that outlives a 50 ms deadline,
    /// and assert the audit row is actually there afterwards. Pre-fix this
    /// path returned through `?` before `db.audit` was ever reached, so the
    /// table stayed empty.
    ///
    /// unix-only because it needs a portable "sleep for a while" binary;
    /// the branch it exercises is platform-independent (`tokio::time::
    /// timeout` + the single-exit audit below it).
    #[cfg(unix)]
    #[tokio::test]
    async fn a_timed_out_recheck_still_writes_its_audit_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut cmd = tokio::process::Command::new("/bin/sleep");
        cmd.arg("30");
        cmd.stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        cmd.kill_on_drop(true);

        let res = run_recheck_and_audit(
            &db,
            None,
            std::path::Path::new("/tmp/some-project"),
            cmd,
            std::time::Duration::from_millis(50),
        )
        .await;

        let err = res.expect_err("the 50 ms deadline must fire");
        assert!(err.contains("timed out"), "{err}");

        let rows = db
            .audit_list(None, None, None, None, Some("summary_recheck"), 10)
            .expect("audit_list");
        assert_eq!(rows.len(), 1, "exactly one row for one attempt");
        assert_eq!(rows[0].operation, "summary_recheck");
        let detail: serde_json::Value =
            serde_json::from_str(&rows[0].detail).expect("detail is json");
        assert_eq!(detail["outcome"], "timed_out");
        assert_eq!(detail["project_root"], "/tmp/some-project");
    }

    /// The leave-alone half: a child that exits 0 still records `completed`
    /// (the behaviour that DID exist before), so the restructure did not buy
    /// the failure rows by losing the success one.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_completed_recheck_still_writes_its_audit_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut cmd = tokio::process::Command::new("/bin/sh");
        cmd.arg("-c").arg(
            "echo '[summary-health] recheck: 4 pending before, 1 after; \
             3 generator run(s), 0 failure(s)'",
        );
        cmd.stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        let view = run_recheck_and_audit(
            &db,
            None,
            std::path::Path::new("/tmp/done-project"),
            cmd,
            std::time::Duration::from_secs(30),
        )
        .await
        .expect("exit 0");
        assert_eq!(view.pending_before, Some(4));
        assert_eq!(view.pending_after, Some(1));

        let rows = db
            .audit_list(None, None, None, None, Some("summary_recheck"), 10)
            .expect("audit_list");
        assert_eq!(rows.len(), 1);
        let detail: serde_json::Value =
            serde_json::from_str(&rows[0].detail).expect("detail is json");
        assert_eq!(detail["outcome"], "completed");
        assert_eq!(detail["pending_before"], 4);
    }

    /// A non-zero exit is the other path that recorded nothing pre-fix.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_failed_recheck_still_writes_its_audit_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut cmd = tokio::process::Command::new("/bin/sh");
        cmd.arg("-c").arg("echo boom >&2; exit 3");
        cmd.stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        let err = run_recheck_and_audit(
            &db,
            None,
            std::path::Path::new("/tmp/broken-project"),
            cmd,
            std::time::Duration::from_secs(30),
        )
        .await
        .expect_err("exit 3");
        assert!(err.contains("exited 3"), "{err}");
        assert!(err.contains("boom"), "{err}");

        let rows = db
            .audit_list(None, None, None, None, Some("summary_recheck"), 10)
            .expect("audit_list");
        assert_eq!(rows.len(), 1);
        let detail: serde_json::Value =
            serde_json::from_str(&rows[0].detail).expect("detail is json");
        assert_eq!(detail["outcome"], "exit_nonzero");
        assert_eq!(detail["exit_code"], 3);
    }


    /// C-TIER PARITY LOCK for the `SummaryRecheckView` DTO (v0.2.96 D-6).
    ///
    /// The struct in this file and the `type SummaryRecheckView` in
    /// `launcher/src/routes/preferences/+page.svelte` are a hand mirror —
    /// this repo has no Rust→TS codegen surface, so the repo's duplication
    /// rule puts the pair at tier C and requires a parity test plus "must
    /// match" comments on both homes. This is that test.
    ///
    /// The Rust side is read by SERIALISING the struct, not by scanning the
    /// source: that way a `#[serde(rename)]` — which changes the wire name
    /// while leaving the field name alone — is caught too. The TS side is
    /// read from the `.svelte` file via `CARGO_MANIFEST_DIR`
    /// (compile-time-only, inside `#[cfg(test)]`; the same sanctioned use
    /// as `commands::chat_model_context`'s shipped-seed test).
    ///
    /// The scan FAILS LOUD rather than falling through green: a missing
    /// file, a missing/unterminated type block, or an empty field set each
    /// panic with what was looked for. A source-text gate that cannot find
    /// its subject must not report agreement.
    #[test]
    fn the_recheck_dto_field_set_matches_its_typescript_mirror() {
        // ── Rust side: the actual wire names, from serde ────────────────
        let sample = SummaryRecheckView {
            project_root: "/tmp/p".into(),
            exit_code: 0,
            pending_before: Some(1),
            pending_after: Some(0),
            spawned: Some(1),
            failures: Some(0),
            code_leg_skipped: false,
            summary_line: "line".into(),
        };
        let value = serde_json::to_value(&sample).expect("DTO serialises");
        let mut rust_fields: Vec<String> = value
            .as_object()
            .expect("DTO serialises to a JSON object")
            .keys()
            .cloned()
            .collect();
        rust_fields.sort();
        assert!(!rust_fields.is_empty(), "serde produced no fields");

        // ── TS side: the declaration in the preferences page ────────────
        const MIRROR_REL: &[&str] =
            &["src", "routes", "preferences", "+page.svelte"];
        let mut mirror = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("CARGO_MANIFEST_DIR (launcher/src-tauri) has a parent")
            .to_path_buf();
        for seg in MIRROR_REL {
            mirror = mirror.join(seg);
        }
        let src = std::fs::read_to_string(&mirror).unwrap_or_else(|e| {
            panic!(
                "the TS mirror {} is unreadable: {} — if the pane moved, \
                 move this lock with it rather than deleting it",
                mirror.display(),
                e
            )
        });
        const DECL: &str = "type SummaryRecheckView = {";
        let start = src.find(DECL).unwrap_or_else(|| {
            panic!(
                "{} no longer declares `{}` — the Rust DTO's mirror must \
                 stay findable or this lock silently stops locking",
                mirror.display(),
                DECL
            )
        }) + DECL.len();
        let len = src[start..].find("\n  };").unwrap_or_else(|| {
            panic!("the `{}` block in {} is unterminated", DECL, mirror.display())
        });
        let body = &src[start..start + len];

        let mut ts_fields: Vec<String> = Vec::new();
        for line in body.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with("//") || line.starts_with("*") {
                continue;
            }
            let Some((name, _)) = line.split_once(':') else {
                continue;
            };
            let name = name.trim().trim_end_matches('?');
            if !name.is_empty()
                && name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_')
            {
                ts_fields.push(name.to_string());
            }
        }
        ts_fields.sort();
        assert!(
            !ts_fields.is_empty(),
            "parsed zero fields out of the `{}` block in {} — the scan \
             broke, which is a failure, not agreement",
            DECL,
            mirror.display()
        );

        assert_eq!(
            rust_fields,
            ts_fields,
            "SummaryRecheckView has drifted. Rust (serde wire names) vs \
             {}. Update BOTH homes — the GUI reads these keys by name and \
             a missing one is `undefined` at runtime, not a build error.",
            mirror.display()
        );
    }


    /// v0.2.96 (L-7) WIRING: a timed-out recheck reaps the GRANDCHILDREN
    /// its child left running, not just the child.
    ///
    /// The shape mirrors the real one: `sh` (our child) spawns a long
    /// `sleep` (the "generator"), records its pid, then waits.
    /// `kill_on_drop` reaches the `sh` only — pre-fix the `sleep` survived
    /// the deadline, was reparented to init, and ran on with nobody waiting
    /// on it. The assertion is on actual OS state afterwards
    /// (`pid_is_alive`), not on a returned count that could be reported
    /// without anything having happened.
    ///
    /// unix-only for the portable `sh`/`sleep` pair. The code under test has
    /// no per-OS branch: `kill_descendants` is ONE implementation over
    /// `sysinfo` for Linux, macOS and Windows alike.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_timed_out_recheck_reaps_the_generator_its_child_spawned() {
        use vct_launcher_core::process::pid_is_alive;

        let dir = tmpdir("l7-grandchild");
        let pidfile = dir.join("generator.pid");
        let db = Db::open_in_memory().expect("in-memory db");

        let mut cmd = tokio::process::Command::new("/bin/sh");
        cmd.arg("-c").arg(format!(
            "sleep 45 & echo $! > '{}'; wait",
            pidfile.display()
        ));
        cmd.stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        cmd.kill_on_drop(true);

        // 2 s deadline: generous enough that the shell has certainly written
        // the pid file (it does so in the first milliseconds), short enough
        // to keep the test fast.
        let err = run_recheck_and_audit(
            &db,
            None,
            std::path::Path::new("/tmp/grandchild-project"),
            cmd,
            std::time::Duration::from_secs(2),
        )
        .await
        .expect_err("the 2 s deadline must fire");
        assert!(err.contains("timed out"), "{err}");

        let raw = fs::read_to_string(&pidfile).unwrap_or_else(|e| {
            panic!(
                "the child never recorded its generator's pid at {} ({e}) — \
                 the fixture, not the fix, is broken",
                pidfile.display()
            )
        });
        let generator_pid: u32 = raw.trim().parse().expect("a pid");

        // The generator is gone. Poll briefly: SIGTERM delivery + reaping is
        // not instantaneous, and a fixed sleep would be either flaky or slow.
        for _ in 0..40 {
            if !pid_is_alive(generator_pid) {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        assert!(
            !pid_is_alive(generator_pid),
            "the generator (pid {generator_pid}) outlived the deadline — \
             `kill_on_drop` reached the child only, which is L-7 exactly"
        );

        let rows = db
            .audit_list(None, None, None, None, Some("summary_recheck"), 10)
            .expect("audit_list");
        assert_eq!(rows.len(), 1);
        let detail: serde_json::Value =
            serde_json::from_str(&rows[0].detail).expect("detail is json");
        assert_eq!(detail["outcome"], "timed_out");
        assert_eq!(
            detail["descendants_killed"], 1,
            "the one generator the child had in flight, recorded: {detail}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

}
