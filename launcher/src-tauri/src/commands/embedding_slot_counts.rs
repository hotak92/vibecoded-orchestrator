// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Per-slot populated-vector COUNTS for a project's KG collection
//! (v0.2.71 Track T-C-modal).
//!
//! Powers the `RegenerateOrDeferModal.svelte` "Keep previous model" option:
//! when a model SWITCH is detected on an update, the modal lets the user
//! revert the active-embedding choice to the model/slot that already has the
//! MOST entries embedded (cheap re-sync: skip-by-hash + backfill gaps) rather
//! than re-embedding from scratch into the new model's empty slot. To propose
//! a *smart default* and render "X of N embedded" per model, the modal needs
//! a per-slot populated count for the project's KG collection.
//!
//! ─── Modular reuse (no duplicate count logic) ────────────────────────────
//!
//! The actual counting is done in Python by composing the two EXISTING
//! aggregate-count primitives:
//!
//!   * `vco_lib/weaviate_schema.py::_existing_slot_dim`'s v4 `include_vector`
//!     iteration pattern — a non-empty `obj.vector[slot]` means that slot is
//!     populated for that object;
//!   * `vco_lib/embedding_enrichment.py::_estimate_object_count`'s GraphQL
//!     aggregate — the collection total (the "N" denominator).
//!
//! This Rust command is a thin shell-out to
//! `python -m vco_lib.embedding_enrichment slot-counts --collection <kg>`.
//! It does NOT re-implement the count logic — see `count_populated_slots` in
//! the Python module. We resolve the project's KG collection name here (via
//! `ProjectEnvSettings::populate`, the same resolver the env-projection uses)
//! so the frontend only has to pass a `project_id`.
//!
//! ─── Soft-fail ───────────────────────────────────────────────────────────
//!
//! Counting is best-effort. A Weaviate outage, a missing collection, or a
//! half-installed venv yields `total=0, slots=[], most_populated_profile=None`
//! — the modal then degrades to its 2-option form (Regenerate / Defer) with no
//! smart default. Counting must NEVER gate the user's update flow, so this
//! command returns `Ok` with an empty result rather than `Err` on a probe
//! failure; it only returns `Err` for caller-side faults (project not found).

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, State};
use tokio::time::timeout;

use crate::commands::installer::{detect_system, find_local_repo_root};
use crate::commands::project_env_settings;
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// Hard ceiling on the count subprocess. Counting iterates the collection
/// once with `include_vector=True`; on a large (100k+) KG that read can take
/// a handful of seconds. 120s is generous enough to absorb a slow-disk
/// Weaviate while still cleaning up a genuinely stuck probe rather than
/// hanging the modal forever.
const SLOT_COUNTS_TIMEOUT_SECS: u64 = 120;

/// One slot's populated-count row. Mirrors the Python `slots[]` entry shape.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SlotPopulatedCount {
    /// The Weaviate named-vector slot (e.g. `qwen3_embed`).
    pub slot: String,
    /// The user-selectable embedding profile this slot maps to (e.g.
    /// `qwen3`). This is what the modal passes to
    /// `set_project_active_embedding` when the user picks "keep previous".
    pub profile: String,
    /// Number of objects in the collection with a NON-EMPTY vector in `slot`.
    pub populated: u64,
}

/// Per-slot populated counts for one project's KG collection, plus the smart
/// default (`most_populated_profile`). Mirrors the Python
/// `count_populated_slots` return shape.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SlotCounts {
    /// The resolved KG collection that was counted.
    pub collection: String,
    /// Aggregate total objects in the collection (the "N" denominator).
    pub total: u64,
    /// One row per slot that has ≥1 populated object AND maps to a
    /// selectable text profile. Empty when nothing is embedded yet or the
    /// probe soft-failed.
    pub slots: Vec<SlotPopulatedCount>,
    /// The profile with the MOST populated objects — the modal's "keep
    /// previous model" smart default. `None` when `slots` is empty.
    pub most_populated_profile: Option<String>,
}

impl SlotCounts {
    /// The soft-fail empty result for a given collection name.
    fn empty(collection: String) -> Self {
        SlotCounts {
            collection,
            total: 0,
            slots: Vec::new(),
            most_populated_profile: None,
        }
    }
}

/// Parse the Python `slot-counts` JSON line into a `SlotCounts`. On any
/// malformed shape returns the soft-fail empty result (the modal degrades).
fn parse_slot_counts(stdout: &str, fallback_collection: &str) -> SlotCounts {
    // The CLI emits exactly one JSON object line. Take the last non-empty
    // line to be robust against any stray leading log output.
    let line = stdout
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .next_back();
    let Some(line) = line else {
        return SlotCounts::empty(fallback_collection.to_string());
    };
    match serde_json::from_str::<SlotCounts>(line) {
        Ok(parsed) => parsed,
        Err(_) => SlotCounts::empty(fallback_collection.to_string()),
    }
}

/// Tauri command — per-slot populated counts for a project's KG collection.
///
/// Resolves the project's KG collection name, then shells to the Python
/// counter. Soft-fails to an empty `SlotCounts` on any probe failure (no
/// Python, no Weaviate, missing collection); only returns `Err` when the
/// project itself isn't in the DB.
#[command]
pub async fn project_embedding_slot_counts(
    project_id: String,
    db: State<'_, Db>,
) -> Result<SlotCounts, String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // Resolve the KG collection the same way env-projection does — sticky
    // per-project name, falling back to the sanitized `<Name>_KnowledgeGraph`.
    let settings = project_env_settings::populate(&db, &row.name, Some(&row.id));
    let collection = settings.kg_collection;

    // No Python → no probe → soft-fail empty (modal degrades to 2 options).
    let system = match detect_system().await {
        Ok(s) if s.has_python => s,
        _ => return Ok(SlotCounts::empty(collection)),
    };
    let orch_root: PathBuf = match find_local_repo_root() {
        Ok(p) => p,
        Err(_) => return Ok(SlotCounts::empty(collection)),
    };

    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.embedding_enrichment",
        "slot-counts",
        "--collection",
        &collection,
    ])
    .current_dir(&orch_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let run = async {
        cmd.output()
            .await
            .map_err(|e| format!("slot-counts failed to start: {}", e))
    };

    let out = match timeout(Duration::from_secs(SLOT_COUNTS_TIMEOUT_SECS), run).await {
        Ok(Ok(out)) => out,
        // Spawn error OR timeout → soft-fail empty. The modal still works.
        Ok(Err(_)) | Err(_) => return Ok(SlotCounts::empty(collection)),
    };

    let stdout = String::from_utf8_lossy(&out.stdout);
    Ok(parse_slot_counts(&stdout, &collection))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_populated_counts_and_smart_default() {
        // Single-line JSON — the real CLI emits exactly one object line.
        let json = concat!(
            r#"{"collection":"My_KnowledgeGraph","total":100,"slots":["#,
            r#"{"slot":"qwen3_embed","profile":"qwen3","populated":100},"#,
            r#"{"slot":"arctic2_embed","profile":"arctic","populated":30}],"#,
            r#""most_populated_profile":"qwen3"}"#,
        );
        let parsed = parse_slot_counts(json, "fallback");
        assert_eq!(parsed.collection, "My_KnowledgeGraph");
        assert_eq!(parsed.total, 100);
        assert_eq!(parsed.slots.len(), 2);
        assert_eq!(parsed.slots[0].slot, "qwen3_embed");
        assert_eq!(parsed.slots[0].profile, "qwen3");
        assert_eq!(parsed.slots[0].populated, 100);
        assert_eq!(parsed.slots[1].profile, "arctic");
        assert_eq!(parsed.slots[1].populated, 30);
        assert_eq!(parsed.most_populated_profile.as_deref(), Some("qwen3"));
    }

    #[test]
    fn empty_result_when_nothing_embedded() {
        let json =
            r#"{"collection":"Empty_KG","total":0,"slots":[],"most_populated_profile":null}"#;
        let parsed = parse_slot_counts(json, "fallback");
        assert_eq!(parsed.total, 0);
        assert!(parsed.slots.is_empty());
        assert_eq!(parsed.most_populated_profile, None);
    }

    #[test]
    fn malformed_json_soft_fails_to_empty() {
        // Garbage on stdout (e.g. a stray traceback) must not panic — the
        // modal degrades to its 2-option form.
        let parsed = parse_slot_counts("not json at all\n", "Some_KG");
        assert_eq!(parsed.collection, "Some_KG");
        assert_eq!(parsed.total, 0);
        assert!(parsed.slots.is_empty());
        assert_eq!(parsed.most_populated_profile, None);
    }

    #[test]
    fn empty_stdout_soft_fails_to_empty() {
        let parsed = parse_slot_counts("   \n  \n", "Fallback_KG");
        assert_eq!(parsed.collection, "Fallback_KG");
        assert_eq!(parsed.total, 0);
        assert!(parsed.slots.is_empty());
    }

    #[test]
    fn takes_last_line_ignoring_leading_log_noise() {
        // A leading non-JSON log line must not break parsing — we take the
        // last non-empty line.
        let stdout = "INFO some log noise\n{\"collection\":\"K\",\"total\":5,\
            \"slots\":[{\"slot\":\"qwen3_embed\",\"profile\":\"qwen3\",\
            \"populated\":5}],\"most_populated_profile\":\"qwen3\"}\n";
        let parsed = parse_slot_counts(stdout, "fb");
        assert_eq!(parsed.total, 5);
        assert_eq!(parsed.most_populated_profile.as_deref(), Some("qwen3"));
    }
}
