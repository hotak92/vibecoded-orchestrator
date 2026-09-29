// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.100 (WP-03a, AD-1, L2-F10, L3-F02): the ONE home for how an
//! orchestrator-update failure reaches the user.
//!
//! Two defects this file exists to make impossible:
//!
//! * **"Update failed: Update failed:"** with no reason. install.py's stdout,
//!   exit code and step marker were dropped on the way to the UI; three Rust
//!   sites formatted `"Update failed: " + stderr` (empty on a signal death or
//!   a stdout-only traceback) and the Updates page prefixed it AGAIN.
//!   [`failure_message`] now always carries the exit code (or the signal), the
//!   last `[N/M]` step, a ≤20-line tail of stderr (stdout when stderr is
//!   empty) and the install log path — and every message the pipeline returns
//!   goes through [`normalise_message`], which strips any `Update failed:`
//!   prefix and never yields an empty string. The frontend renders `message`
//!   ONCE, unprefixed.
//! * **An error string the frontend had to guess at.** The pipeline returns
//!   [`UpdateSurfaceError`], serialised by [`UpdateSurfaceError::to_json`] as
//!   `{"kind": …, "message": …, …payload}`. `kind` is one of
//!   [`FAILURE_KINDS`]; payload-carrying kinds keep the exact field names (and
//!   the `event` discriminator) the three divergence/conflict modals already
//!   parse, so the modals route on `kind` and read the same fields they do
//!   today.
//!
//! The contract is pinned by `tests/fixtures/update_failure_messages.json`,
//! which this module's tests execute and which WP-08's vitest consumes — so
//! the Rust producer and the TS consumer read the SAME examples.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::commands::update_pipeline::{InstallPyRun, UpdatePipelineError};

/// The prefix the frontend used to add and three Rust sites used to add —
/// never part of a message this module returns.
pub(crate) const FORBIDDEN_PREFIX: &str = "Update failed:";

/// Every `kind` value the error JSON can carry. MUST match
/// `tests/fixtures/update_failure_messages.json` `contract.kinds`.
pub(crate) const FAILURE_KINDS: [&str; 7] = [
    "Refused",
    "NonFastForward",
    "UntrackedCollision",
    "AutostashPop",
    "Conflict",
    "InstallFailed",
    "Raw",
];

/// Maximum number of output lines quoted in an install failure message.
pub(crate) const FAILURE_TAIL_MAX_LINES: usize = 20;

/// Byte bound on the same tail (pip can print one enormous line).
pub(crate) const FAILURE_TAIL_MAX_BYTES: usize = 4096;

/// Files named inline in a conflict/divergence message before "and N more".
const FILES_NAMED_INLINE: usize = 5;

/// `<install_root>/state/logs/install.jsonl` — where install.py writes its
/// durable log (`install.py::_install_log_path`, `vco_lib/doctor.py`
/// `INSTALL_LOG_REL`). Named in every install failure message.
pub(crate) fn install_log_path(install_root: &Path) -> PathBuf {
    install_root
        .join("state")
        .join("logs")
        .join("install.jsonl")
}

/// Why an orchestrator update stopped — the typed error of
/// `update_run::run_update`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum UpdateSurfaceError {
    /// Refused BEFORE the tree or the process table was changed (or, for the
    /// hub stop, before git ran). `code` is a stable machine-readable reason.
    Refused { code: &'static str, reason: String },
    /// The clone diverged from upstream. `payload` carries the
    /// `orchestrator_update_non_ff` fields the divergence modal renders.
    NonFastForward(Value),
    /// Untracked local files would be overwritten (`orchestrator_untracked_collision`).
    UntrackedCollision(Value),
    /// The merge landed; restoring the autostash conflicted
    /// (`orchestrator_autostash_pop_conflict`).
    AutostashPop(Value),
    /// A merge/rebase stopped on conflicts, or one is already in progress
    /// (`orchestrator_update_conflict`).
    Conflict(Value),
    /// `install.py --update` failed or could not be spawned.
    InstallFailed {
        message: String,
        log_path: PathBuf,
        exit_code: Option<i32>,
        signal: Option<i32>,
        last_step: Option<String>,
    },
    /// Anything else, already worded.
    Raw(String),
}

impl UpdateSurfaceError {
    /// The `kind` string of the wire form.
    pub(crate) fn kind(&self) -> &'static str {
        match self {
            UpdateSurfaceError::Refused { .. } => "Refused",
            UpdateSurfaceError::NonFastForward(_) => "NonFastForward",
            UpdateSurfaceError::UntrackedCollision(_) => "UntrackedCollision",
            UpdateSurfaceError::AutostashPop(_) => "AutostashPop",
            UpdateSurfaceError::Conflict(_) => "Conflict",
            UpdateSurfaceError::InstallFailed { .. } => "InstallFailed",
            UpdateSurfaceError::Raw(_) => "Raw",
        }
    }

    /// The one human sentence the frontend shows. Never empty, never
    /// prefixed with [`FORBIDDEN_PREFIX`].
    pub(crate) fn message(&self) -> String {
        match self {
            UpdateSurfaceError::Refused { reason, .. } => normalise_message(
                reason,
                "The update was refused before anything was changed.",
            ),
            UpdateSurfaceError::NonFastForward(p) => {
                let branch = str_field(p, "branch").unwrap_or("main");
                let both = list_field(p, "diverged_files");
                let upstream = p
                    .get("upstream_only_count")
                    .and_then(Value::as_u64)
                    .unwrap_or(list_field(p, "upstream_only_files").len() as u64);
                normalise_message(
                    &format!(
                        "Your orchestrator clone has diverged from upstream on branch {}: {} \
                         file(s) changed on both sides{}, {} coming in from upstream. Choose \
                         Merge, Rebase or Cancel.",
                        branch,
                        both.len(),
                        name_files(&both),
                        upstream
                    ),
                    "Your orchestrator clone has diverged from upstream.",
                )
            }
            UpdateSurfaceError::UntrackedCollision(p) => {
                let mut files = list_field(p, "divergent_files");
                files.extend(list_field(p, "identical_files"));
                normalise_message(
                    &format!(
                        "The update would overwrite {} untracked local file(s){}. Nothing was \
                         changed; choose how to resolve them.",
                        files.len(),
                        name_files(&files)
                    ),
                    "The update would overwrite untracked local files.",
                )
            }
            UpdateSurfaceError::AutostashPop(p) => {
                let files = list_field(p, "conflicted_files");
                normalise_message(
                    &format!(
                        "The update landed, but restoring your uncommitted local changes \
                         conflicted in {} file(s){}. Your changes are safe in the stash; choose \
                         which version to keep.",
                        files.len(),
                        name_files(&files)
                    ),
                    "The update landed, but restoring your local changes conflicted.",
                )
            }
            UpdateSurfaceError::Conflict(p) => {
                let operation = str_field(p, "operation").unwrap_or("update");
                let files = list_field(p, "conflicted_files");
                normalise_message(
                    &format!(
                        "The {} stopped on conflicts in {} file(s){}. Resolve them, then \
                         continue the update.",
                        operation,
                        files.len(),
                        name_files(&files)
                    ),
                    "The update stopped on conflicts.",
                )
            }
            UpdateSurfaceError::InstallFailed { message, .. } => normalise_message(
                message,
                "install.py --update failed and reported no reason.",
            ),
            UpdateSurfaceError::Raw(m) => {
                normalise_message(m, "The update failed and reported no reason.")
            }
        }
    }

    /// The wire form: `{"kind", "message", …payload}`.
    ///
    /// Payload kinds FLATTEN their payload object into the top level (so the
    /// modals read `branch`, `conflicted_files`, `event`, … where they always
    /// did); a payload key named `kind` or `message` never overrides ours.
    pub(crate) fn to_json_value(&self) -> Value {
        let mut obj = Map::new();
        match self {
            UpdateSurfaceError::Refused { code, .. } => {
                obj.insert("code".into(), json!(code));
            }
            UpdateSurfaceError::NonFastForward(p)
            | UpdateSurfaceError::UntrackedCollision(p)
            | UpdateSurfaceError::AutostashPop(p)
            | UpdateSurfaceError::Conflict(p) => {
                if let Value::Object(m) = p {
                    for (k, v) in m {
                        obj.insert(k.clone(), v.clone());
                    }
                }
            }
            UpdateSurfaceError::InstallFailed {
                log_path,
                exit_code,
                signal,
                last_step,
                ..
            } => {
                obj.insert("log_path".into(), json!(log_path.display().to_string()));
                obj.insert("exit_code".into(), json!(exit_code));
                obj.insert("signal".into(), json!(signal));
                obj.insert("last_step".into(), json!(last_step));
            }
            UpdateSurfaceError::Raw(_) => {}
        }
        debug_assert!(FAILURE_KINDS.contains(&self.kind()));
        obj.insert("kind".into(), json!(self.kind()));
        obj.insert("message".into(), json!(self.message()));
        Value::Object(obj)
    }

    /// The `Err(String)` a Tauri command returns.
    pub(crate) fn to_json(&self) -> String {
        self.to_json_value().to_string()
    }
}

/// Strip every leading [`FORBIDDEN_PREFIX`] (case-insensitive, repeated —
/// "Update failed: Update failed:" is the field string) and surrounding
/// whitespace; an empty result becomes `fallback`. The ONE place a message is
/// made fit for display.
pub(crate) fn normalise_message(raw: &str, fallback: &str) -> String {
    let mut s = raw.trim();
    loop {
        let lower = s.to_ascii_lowercase();
        if lower.starts_with(&FORBIDDEN_PREFIX.to_ascii_lowercase()) {
            s = s[FORBIDDEN_PREFIX.len()..].trim_start();
        } else {
            break;
        }
    }
    let s = s.trim();
    if s.is_empty() {
        fallback.trim().to_string()
    } else {
        s.to_string()
    }
}

/// The user-facing reason an `install.py --update` run failed. Never empty.
///
/// `install.py --update exited with code 1 at step [5/10].` (or `was killed
/// by signal 9` / `exited without an exit code`), then the last ≤20 lines of
/// stderr — of stdout when stderr is blank — then `Full log: <path>`.
pub(crate) fn failure_message(run: &InstallPyRun, log_path: &Path) -> String {
    let how = match (run.exit_code, run.signal) {
        (Some(code), _) => format!("exited with code {}", code),
        (None, Some(sig)) => format!("was killed by signal {}", sig),
        (None, None) => "exited without an exit code (killed or crashed)".to_string(),
    };
    let at = match &run.last_step {
        Some(step) => format!(" at step {}", step),
        None => " before printing a step marker".to_string(),
    };
    let (tail_src, which) = if !run.stderr.trim().is_empty() {
        (run.stderr.as_str(), "stderr")
    } else {
        (run.stdout_tail.as_str(), "output")
    };
    let tail = crate::commands::update_pipeline::bounded_tail(
        tail_src,
        FAILURE_TAIL_MAX_LINES,
        FAILURE_TAIL_MAX_BYTES,
    );
    let tail_block = if tail.trim().is_empty() {
        "install.py printed nothing.".to_string()
    } else {
        format!("Last {} lines:\n{}", which, tail)
    };
    let msg = format!(
        "install.py --update {}{}. {}\nFull log: {}",
        how,
        at,
        tail_block,
        log_path.display()
    );
    normalise_message(&msg, "install.py --update failed.")
}

/// Build the error for a failed (non-zero / signalled) install.py run.
pub(crate) fn install_failed(run: &InstallPyRun, install_root: &Path) -> UpdateSurfaceError {
    let log_path = install_log_path(install_root);
    UpdateSurfaceError::InstallFailed {
        message: failure_message(run, &log_path),
        log_path,
        exit_code: run.exit_code,
        signal: run.signal,
        last_step: run.last_step.clone(),
    }
}

/// Build the error for an install.py that could not be spawned at all.
pub(crate) fn install_spawn_failed(spawn_error: &str, install_root: &Path) -> UpdateSurfaceError {
    let log_path = install_log_path(install_root);
    UpdateSurfaceError::InstallFailed {
        message: normalise_message(
            &format!(
                "{}. The source was updated but install.py did not run. Full log: {}",
                spawn_error.trim().trim_end_matches('.'),
                log_path.display()
            ),
            "install.py --update could not be started.",
        ),
        log_path,
        exit_code: None,
        signal: None,
        last_step: None,
    }
}

/// Audit rows a pipeline error asks the caller to write (the pipeline decides,
/// the Db holder writes — `update_pipeline`'s contract).
pub(crate) type AuditRows = Vec<(String, Value)>;

/// Map the pipeline's condition enum onto the surface error. ONE rendering;
/// the per-surface `installer`/`self_update` renderers were retired with
/// their commands in v0.2.100 (WP-03b).
pub(crate) fn from_pipeline_error(err: UpdatePipelineError) -> (UpdateSurfaceError, AuditRows) {
    let mut audit = AuditRows::new();
    let clobber_row = |branch: &str, after_success: bool| {
        (
            "update_binary_clobber_averted".to_string(),
            json!({
                "branch": branch,
                "pop_conflict_after_success": after_success,
                "note": "abort tail kept the freshly-pulled binary (WI-3)",
            }),
        )
    };
    let e = match err {
        UpdatePipelineError::MergeInProgress { payload, .. } => {
            UpdateSurfaceError::Conflict(parse_payload(&payload))
        }
        UpdatePipelineError::Conflict {
            operation,
            branch,
            conflicted,
            detail,
            record_binary_clobber_averted,
        } => {
            if record_binary_clobber_averted {
                audit.push(clobber_row(&branch, false));
            }
            UpdateSurfaceError::Conflict(json!({
                "event": "orchestrator_update_conflict",
                "operation": operation,
                "branch": branch,
                "conflicted_files": conflicted,
                "git_stderr": detail,
            }))
        }
        UpdatePipelineError::AutostashPopConflict {
            branch,
            conflicted,
            detail,
            record_binary_clobber_averted,
        } => {
            if record_binary_clobber_averted {
                audit.push(clobber_row(&branch, true));
            }
            UpdateSurfaceError::AutostashPop(json!({
                "event": "orchestrator_autostash_pop_conflict",
                "branch": branch,
                "conflicted_files": conflicted,
                "git_stderr": detail,
            }))
        }
        UpdatePipelineError::UntrackedCollision { payload } => {
            UpdateSurfaceError::UntrackedCollision(parse_payload(&payload))
        }
        UpdatePipelineError::NonFastForward {
            branch,
            local_sha,
            remote_sha,
            diverged,
            upstream_only,
            local_only,
            detail,
        } => UpdateSurfaceError::NonFastForward(non_ff_payload(
            &branch,
            local_sha.as_deref(),
            remote_sha.as_deref(),
            &diverged,
            &upstream_only,
            &local_only,
            &detail,
        )),
        UpdatePipelineError::HeadDidNotAdvance { detail } => UpdateSurfaceError::Raw(detail),
        UpdatePipelineError::Raw(m) => UpdateSurfaceError::Raw(m),
    };
    (e, audit)
}

/// The `orchestrator_update_non_ff` payload (same keys as
/// the retired `installer::serialize_orchestrator_non_ff_error`, WP-03b).
pub(crate) fn non_ff_payload(
    branch: &str,
    local_sha: Option<&str>,
    remote_sha: Option<&str>,
    diverged: &[String],
    upstream_only: &[String],
    local_only: &[String],
    git_stderr: &str,
) -> Value {
    json!({
        "event": "orchestrator_update_non_ff",
        "branch": branch,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "diverged_files": diverged,
        "local_only_files": local_only,
        "upstream_only_files": upstream_only,
        "upstream_only_count": upstream_only.len(),
        "git_stderr": git_stderr,
    })
}

/// A payload string produced by a shared handler, as JSON. A string that is
/// not a JSON object is kept verbatim under `detail` rather than dropped.
fn parse_payload(payload: &str) -> Value {
    match serde_json::from_str::<Value>(payload) {
        Ok(v @ Value::Object(_)) => v,
        _ => json!({ "detail": payload }),
    }
}

fn str_field<'a>(p: &'a Value, key: &str) -> Option<&'a str> {
    p.get(key).and_then(Value::as_str).filter(|s| !s.is_empty())
}

fn list_field(p: &Value, key: &str) -> Vec<String> {
    p.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// ` (a, b, c and 2 more)` — or empty for an empty list.
fn name_files(files: &[String]) -> String {
    if files.is_empty() {
        return String::new();
    }
    let shown: Vec<&str> = files
        .iter()
        .take(FILES_NAMED_INLINE)
        .map(String::as_str)
        .collect();
    let more = files.len().saturating_sub(FILES_NAMED_INLINE);
    if more > 0 {
        format!(" ({} and {} more)", shown.join(", "), more)
    } else {
        format!(" ({})", shown.join(", "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Value {
        let text = include_str!("../../../../tests/fixtures/update_failure_messages.json");
        serde_json::from_str(text).expect("update_failure_messages.json parses")
    }

    fn run_from(v: &Value) -> InstallPyRun {
        InstallPyRun {
            success: false,
            stderr: v["stderr"].as_str().unwrap_or("").to_string(),
            exit_code: v["exit_code"].as_i64().map(|c| c as i32),
            signal: v["signal"].as_i64().map(|c| c as i32),
            last_step: v["last_step"].as_str().map(str::to_string),
            stdout_tail: v["stdout_tail"].as_str().unwrap_or("").to_string(),
        }
    }

    /// Fixture paths are written with `/`; a Windows `Path::join` renders `\\`.
    fn slashes(v: Value) -> Value {
        match v {
            Value::String(s) => Value::String(s.replace('\\', "/")),
            Value::Array(a) => Value::Array(a.into_iter().map(slashes).collect()),
            Value::Object(m) => {
                Value::Object(m.into_iter().map(|(k, v)| (k, slashes(v))).collect())
            }
            other => other,
        }
    }

    fn assert_displayable(msg: &str, ctx: &str) {
        assert!(!msg.trim().is_empty(), "{ctx}: message must never be empty");
        assert!(
            !msg.to_ascii_lowercase()
                .starts_with(&FORBIDDEN_PREFIX.to_ascii_lowercase()),
            "{ctx}: message must never carry the prefix the frontend adds: {msg:?}"
        );
    }

    /// The kinds in code and in the shared fixture are the same set.
    #[test]
    fn contract_kinds_match_the_fixture() {
        let f = fixture();
        let kinds: Vec<&str> = f["contract"]["kinds"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(kinds, FAILURE_KINDS.to_vec());
        assert_eq!(
            f["contract"]["forbidden_message_prefix"],
            json!(FORBIDDEN_PREFIX)
        );
    }

    /// Table: `failure_message` is never empty, never prefixed, and carries
    /// the facts the fixture requires (exit code / signal, step, tail, log).
    #[test]
    fn failure_message_table_never_empty_never_double_prefixed() {
        let f = fixture();
        for case in f["failure_message_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let run = run_from(&case["run"]);
            let log = PathBuf::from(case["log_path"].as_str().unwrap());
            let msg = failure_message(&run, &log).replace('\\', "/");
            assert_displayable(&msg, name);
            for needle in case["expect_contains"].as_array().unwrap() {
                let needle = needle.as_str().unwrap();
                assert!(
                    msg.contains(needle),
                    "{name}: expected {needle:?} in {msg:?}"
                );
            }
            for needle in case["expect_not_contains"].as_array().unwrap_or(&vec![]) {
                let needle = needle.as_str().unwrap();
                assert!(
                    !msg.contains(needle),
                    "{name}: unexpected {needle:?} in {msg:?}"
                );
            }
            let tail_lines = msg.lines().count();
            assert!(
                tail_lines <= FAILURE_TAIL_MAX_LINES + 3,
                "{name}: the quoted tail must be bounded ({tail_lines} lines)"
            );
        }
    }

    /// Table: the normaliser strips every leading prefix and falls back when
    /// nothing is left — the field string "Update failed: Update failed:".
    #[test]
    fn normalise_message_table() {
        let f = fixture();
        for case in f["normalise_cases"].as_array().unwrap() {
            let raw = case["raw"].as_str().unwrap();
            let fallback = case["fallback"].as_str().unwrap();
            let got = normalise_message(raw, fallback);
            assert_eq!(got, case["expect"].as_str().unwrap(), "raw={raw:?}");
            assert_displayable(&got, raw);
        }
    }

    fn build_error(spec: &Value) -> UpdateSurfaceError {
        let payload = || spec["payload"].clone();
        match spec["variant"].as_str().unwrap() {
            "Refused" => UpdateSurfaceError::Refused {
                code: match spec["code"].as_str().unwrap() {
                    "python_missing" => "python_missing",
                    "kind_not_routed" => "kind_not_routed",
                    other => panic!("add {other} to build_error"),
                },
                reason: spec["reason"].as_str().unwrap().to_string(),
            },
            "NonFastForward" => UpdateSurfaceError::NonFastForward(payload()),
            "UntrackedCollision" => UpdateSurfaceError::UntrackedCollision(payload()),
            "AutostashPop" => UpdateSurfaceError::AutostashPop(payload()),
            "Conflict" => UpdateSurfaceError::Conflict(payload()),
            "InstallFailed" => install_failed(
                &run_from(&spec["run"]),
                Path::new(spec["install_root"].as_str().unwrap()),
            ),
            "Raw" => UpdateSurfaceError::Raw(spec["raw"].as_str().unwrap().to_string()),
            other => panic!("unknown variant {other}"),
        }
    }

    /// Every example in the fixture serialises to EXACTLY the recorded JSON —
    /// the shapes WP-08's `routeUpdateError` is built against.
    #[test]
    fn surface_error_json_matches_the_recorded_contract() {
        let f = fixture();
        for case in f["surface_errors"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let err = build_error(&case["build"]);
            let got: Value = slashes(serde_json::from_str(&err.to_json()).unwrap());
            assert_eq!(
                got, case["json"],
                "{name}: wire JSON drifted from the fixture"
            );
            assert!(
                FAILURE_KINDS.contains(&got["kind"].as_str().unwrap()),
                "{name}"
            );
            assert_displayable(got["message"].as_str().unwrap(), name);
        }
    }

    /// Conversion from the pipeline keeps the modal fields and asks for the
    /// clobber-averted audit row exactly when the pipeline did.
    #[test]
    fn pipeline_conflict_maps_to_conflict_with_modal_fields_and_audit_row() {
        let (e, audit) = from_pipeline_error(UpdatePipelineError::Conflict {
            operation: "merge",
            branch: "main".into(),
            conflicted: vec!["CLAUDE.md".into()],
            detail: "CONFLICT (content)".into(),
            record_binary_clobber_averted: true,
        });
        let v = e.to_json_value();
        assert_eq!(v["kind"], "Conflict");
        assert_eq!(v["event"], "orchestrator_update_conflict");
        assert_eq!(v["conflicted_files"], json!(["CLAUDE.md"]));
        assert_eq!(audit.len(), 1);
        assert_eq!(audit[0].0, "update_binary_clobber_averted");

        let (e, audit) = from_pipeline_error(UpdatePipelineError::Raw(String::new()));
        assert!(audit.is_empty());
        assert_displayable(&e.message(), "empty Raw");
    }

    /// The RC-1 site: the merge landed and only the autostash POP conflicted.
    /// The abort tail's clobber outcome becomes the audit row with
    /// `pop_conflict_after_success: true` — and no row when nothing was
    /// averted (the leave-alone case).
    #[test]
    fn pipeline_pop_conflict_maps_to_autostash_pop_with_the_after_success_audit_row() {
        let pop = |record: bool| {
            from_pipeline_error(UpdatePipelineError::AutostashPopConflict {
                branch: "main".into(),
                conflicted: vec!["CLAUDE.md".into()],
                detail: "Applying autostash resulted in conflicts.".into(),
                record_binary_clobber_averted: record,
            })
        };
        let (e, audit) = pop(true);
        let v = e.to_json_value();
        assert_eq!(v["event"], "orchestrator_autostash_pop_conflict");
        assert_eq!(v["conflicted_files"], json!(["CLAUDE.md"]));
        assert_eq!(audit.len(), 1);
        assert_eq!(audit[0].0, "update_binary_clobber_averted");
        assert_eq!(audit[0].1["pop_conflict_after_success"], true);
        assert_eq!(audit[0].1["branch"], "main");

        let (_, audit) = pop(false);
        assert!(audit.is_empty(), "no clobber averted ⇒ no row: {audit:?}");
    }

    /// A handler payload that is not JSON is kept, not dropped.
    #[test]
    fn non_json_payload_is_kept_under_detail() {
        let (e, _) = from_pipeline_error(UpdatePipelineError::UntrackedCollision {
            payload: "not json".into(),
        });
        assert_eq!(e.to_json_value()["detail"], "not json");
    }
}
