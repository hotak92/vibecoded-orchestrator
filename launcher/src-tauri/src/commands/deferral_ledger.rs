// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The deferral-ledger GUI backend (v0.2.91 WP-I).
//!
//! Renders `UPDATE_DEFERRED` as a two-group panel — "Action needed" vs
//! "Records / by-design" — with a per-entry Dismiss and the retry trail the
//! WP-H driver writes. Two Tauri commands read it (one per SCOPE), one
//! dismisses a single entry.
//!
//! ## The JSON sidecar is the SSOT — the Markdown is never parsed here
//!
//! `UPDATE_DEFERRED.json` is the lossless store (`vco_lib/deferral_report.py`
//! A-3): multi-line `command_to_apply` blocks with `#` comment lines survive it
//! verbatim, and it carries the machine-only fields (`disposition`,
//! `dismiss_fields`) that the `.md` deliberately does not render. The `.md` is a
//! HUMAN render whose round-trip is lossy by design, so a Rust markdown parser
//! here would (a) be a second, weaker implementation of a parser Python already
//! owns and (b) silently corrupt exactly the multi-line command blocks this
//! panel exists to display faithfully. When the sidecar is absent or carries an
//! unknown `schema_version`, this module reports `source = "unavailable"` and
//! renders NOTHING rather than guessing — same posture as
//! `_parse_json_sidecar`'s `None` return, minus the markdown fallback (which
//! belongs to Python, whose parser it is).
//!
//! ## Disposition resolution — one rule, three inputs
//!
//! Mirrors `DeferralEntry.resolved_disposition` exactly:
//!
//! 1. the entry's own `disposition` field, when present AND a known class;
//! 2. otherwise `vct_launcher_core::deferral_registry::disposition_for(cid)`;
//! 3. an unregistered cid falls out of (2) as `action_required` (the registry's
//!    conservative `DEFAULT_CLASS`) — an unclassified condition surfaces as
//!    work, it never hides in the collapsed fold.
//!
//! The GROUP split then applies `action_required | auto_retryable ⇒ actionable`,
//! which is `deferral_registry::is_actionable`'s partition and Python's
//! `split_by_disposition`. `is_actionable` keys on the cid alone, so it cannot
//! see an entry's explicit override; [`is_actionable_disposition`] takes the
//! RESOLVED tier instead and a unit test pins the two agreeing for every
//! registry pattern. Result: the panel's "Action needed" group, the CLAUDE.md
//! reminder's list, and `vco doctor`'s set hold the same entries by construction.
//!
//! ## The BADGE is a narrower number than the group (USER DECISION, 2026-08-27)
//!
//! [`DeferralLedgerView::action_required_count`] counts `action_required` ONLY.
//! `auto_retryable` entries are conditions VCO retries by itself, so nagging the
//! MenuBar about them asks the user to act on work already in hand. They stay
//! IN the "Action needed" group — the panel is where you go to see what is
//! outstanding, including what VCO is handling — they simply do not badge.
//! [`DeferralLedgerView::actionable_count`] therefore remains the GROUP size
//! (and the complement of `record_count`), and the two are deliberately
//! different numbers whenever an auto_retryable entry is present.
//!
//! ## Scope is explicit everywhere (decision #6 UX rider)
//!
//! Every view carries its [`LedgerScope`] and a human `scope_label`. A
//! per-project ledger is read from THAT project's folder and rendered only on
//! that project's Settings panel; the orchestrator root's ledger is read from
//! the clone root and rendered only on the global surface. Dismissal takes the
//! scope explicitly rather than inferring it, so a Dismiss can never act on the
//! wrong folder — and the returned [`DismissOutcome`] echoes the folder it
//! touched so the confirmation toast can name it.
//!
//! ## Why the retry trail is rendered per entry
//!
//! `auto_retryable` entries are worked on by the WP-H driver
//! (`vco_lib/deferral_retry.py`). Without the trail, a user watching an entry
//! sit there has no way to tell "nothing tried" from "tried three times and the
//! backend is still down". [`RetrySummary`] renders the honest version,
//! including `inconclusive` as a FIRST-CLASS outcome distinct from `failed`:
//! the handler ran and exited 0 but its condition is still in the ledger, so
//! something happened and nothing is proven. Collapsing that into "failed"
//! would claim knowledge the driver explicitly refused to claim.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::command;

use vct_launcher_core::db::Db;
use vct_launcher_core::deferral_registry;
// Brings `.silent()` (CREATE_NO_WINDOW on Windows) onto `std::process::Command`
// — required by the `command_silent_gate` integration test, which scans this
// file by path along with every other .rs under src-tauri.
use vct_launcher_core::process::CommandExt as _;

// ═══════════════════════════════════════════════════════════════════════
// Cross-language mirrors (tier C — pinned by a parity test)
// ═══════════════════════════════════════════════════════════════════════

/// Sidecar schema this reader understands.
///
/// MUST MATCH `vco_lib/deferral_report.py::_JSON_SCHEMA_VERSION`. A newer
/// sidecar is REFUSED (not partially read) exactly as `_parse_json_sidecar`
/// refuses it — guessing at an unknown shape is how a reader invents entries.
pub(crate) const SIDECAR_SCHEMA_VERSION: u64 = 1;

/// Per (folder, condition) retry ceiling.
///
/// MUST MATCH `vco_lib/deferral_retry.py::MAX_ATTEMPTS`. The jsonl trail does
/// not carry the cap, so the panel has to know it to say "the cap is reached,
/// VCO has stopped trying". Mirrored rather than resolved through Python
/// because the launcher is the REPAIR tool: it must render this panel on an
/// install whose venv is broken, which is precisely when the ledger matters
/// most. `tests/test_v0291_deferral_ledger_parity.py` fails if the two drift.
pub(crate) const RETRY_MAX_ATTEMPTS: u32 = 3;

/// Attempt-row status recorded BEFORE a handler runs — one per dispatch, and
/// the ONLY row `attempt_count` counts. MUST MATCH `deferral_retry.STARTED`.
pub(crate) const RETRY_STATUS_STARTED: &str = "started";
/// Handler ran AND the child cleared its condition. `deferral_retry.RETRIED`.
pub(crate) const RETRY_STATUS_RETRIED: &str = "retried";
/// Handler ran and did not succeed. `deferral_retry.FAILED`.
pub(crate) const RETRY_STATUS_FAILED: &str = "failed";
/// Handler exited 0 but the condition is still in the ledger — ran, unproven.
/// `deferral_retry.INCONCLUSIVE`.
pub(crate) const RETRY_STATUS_INCONCLUSIVE: &str = "inconclusive";
/// Precondition absent (backend down, cap reached). `deferral_retry.SKIPPED`.
pub(crate) const RETRY_STATUS_SKIPPED: &str = "skipped";

/// `<folder>/.claude/context/UPDATE_DEFERRED.json` — `_DEFERRED_JSON_REL`.
fn sidecar_path(folder: &Path) -> PathBuf {
    folder.join(".claude").join("context").join("UPDATE_DEFERRED.json")
}

/// `<folder>/.claude/logs/deferral-retries.jsonl` — `deferral_retry.attempts_path`.
fn retries_path(folder: &Path) -> PathBuf {
    folder.join(".claude").join("logs").join("deferral-retries.jsonl")
}

// ═══════════════════════════════════════════════════════════════════════
// Wire types
// ═══════════════════════════════════════════════════════════════════════

/// Which ledger a view describes. Serialized as `"project"` /
/// `"orchestrator_root"` — the FE renders the label from this, never from a
/// guess about which page it happens to be mounted on.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LedgerScope {
    Project,
    OrchestratorRoot,
}

/// Where an entry's disposition came from — surfaced so a reader can tell a
/// deliberate emitter classification from the registry's table lookup from the
/// conservative default for an id nobody registered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DispositionSource {
    /// The sidecar entry carried an explicit, valid `disposition`.
    Entry,
    /// Resolved from `deferral_conditions.toml`.
    Registry,
    /// Not in the registry ⇒ `action_required` (conservative default).
    Default,
}

/// One row of `deferral-retries.jsonl`, narrowed to this entry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RetryAttempt {
    pub ts: String,
    /// `started` | `retried` | `failed` | `inconclusive` | `skipped`.
    pub status: String,
    pub detail: String,
}

/// What VCO has already tried for one condition, in this folder.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RetrySummary {
    /// Dispatched handler invocations = `started` rows (what the cap counts).
    pub attempts: u32,
    /// [`RETRY_MAX_ATTEMPTS`], echoed so the FE never hard-codes it.
    pub cap: u32,
    /// True once `attempts >= cap` — VCO has stopped retrying this one.
    pub cap_reached: bool,
    pub succeeded: u32,
    pub failed: u32,
    /// Ran, exited 0, condition still present — honest "unproven", NOT failed.
    pub inconclusive: u32,
    pub skipped: u32,
    /// OUTCOME rows only (the `started` bookkeeping rows are excluded — they
    /// carry no verdict and would double every line in the UI), oldest first.
    pub outcomes: Vec<RetryAttempt>,
}

/// One ledger entry as the panel renders it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LedgerEntry {
    pub condition_id: String,
    pub title: String,
    pub detected: String,
    pub why_deferred: String,
    /// Verbatim, multi-line, `#`-comment lines intact. Rendered in a `<pre>`.
    pub command_to_apply: String,
    /// `critical` | `warning` | `info`.
    pub severity: String,
    pub detected_at: String,
    pub kg_node_refs: Vec<String>,
    /// Resolved tier: entry field → registry → `action_required`.
    pub disposition: String,
    pub disposition_source: DispositionSource,
    /// `action_required | auto_retryable` — the badge/group partition.
    pub actionable: bool,
    /// True for `auto_retryable`: VCO retries this itself; the panel says so
    /// instead of telling the user to run something.
    pub auto_retryable: bool,
    pub retries: RetrySummary,
}

/// One scope's whole ledger.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeferralLedgerView {
    pub scope: LedgerScope,
    /// Human scope name: the project's name, or "Orchestrator root".
    pub scope_label: String,
    /// Absolute folder whose ledger this is. Shown in the panel header AND in
    /// every Dismiss confirmation, so an action can never be mis-attributed.
    pub folder: String,
    /// `"sidecar"` (read + parsed) | `"absent"` (nothing deferred) |
    /// `"unavailable"` (present but unreadable / unknown schema).
    pub source: String,
    pub entries: Vec<LedgerEntry>,
    /// `entries.iter().filter(|e| e.actionable).count()` — the "Action needed"
    /// GROUP size for THIS surface (the complement of `record_count`). NOT the
    /// badge: see [`Self::action_required_count`].
    pub actionable_count: usize,
    /// Entries whose resolved disposition is strictly `action_required` — the
    /// BADGE number for THIS surface only (user decision, 2026-08-27). Always
    /// `<= actionable_count`; the gap is the auto_retryable entries VCO is
    /// retrying by itself, which are shown in the group but never badged.
    pub action_required_count: usize,
    /// The collapsed "Records / by-design" group size.
    pub record_count: usize,
    /// Soft-fail diagnostics (unreadable sidecar, skipped malformed rows…).
    /// Never an error: a broken ledger must still render the rest.
    pub warnings: Vec<String>,
}

/// Result of one `dismiss-deferral --json` invocation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DismissOutcome {
    pub condition_id: String,
    pub scope: LedgerScope,
    pub scope_label: String,
    /// The folder the dismissal acted on — echoed for the toast.
    pub folder: String,
    /// True when an entry was present and removed; false on the idempotent
    /// no-op paths (`no_deferrals_file`, `no_match`).
    pub dismissed: bool,
    /// Entries still on disk after the call, per the CLI's JSON contract.
    pub remaining: u32,
    /// `dismissed` | `no_match` | `no_deferrals_file` | … (CLI-provided).
    pub reason: String,
}

// ═══════════════════════════════════════════════════════════════════════
// Pure core — everything below is disk-free and unit-tested on fixtures
// ═══════════════════════════════════════════════════════════════════════

/// True when a RESOLVED disposition means outstanding work.
///
/// The partition is `action_required | auto_retryable`, matching Python's
/// `split_by_disposition` and `deferral_registry::is_actionable`. It takes the
/// resolved TIER (not a cid) because an entry may carry an explicit
/// disposition that the cid-keyed registry helper cannot see;
/// `is_actionable_matches_registry_for_every_pattern` pins the two agreeing
/// wherever both can answer.
pub(crate) fn is_actionable_disposition(disposition: &str) -> bool {
    matches!(disposition, "action_required" | "auto_retryable")
}

/// True when a RESOLVED disposition should raise the MenuBar BADGE.
///
/// Strictly `action_required` (user decision, 2026-08-27) — deliberately
/// NARROWER than [`is_actionable_disposition`]. An `auto_retryable` condition is
/// one VCO is already retrying on its own schedule; badging it asks the user to
/// act on work that is in hand, which trains them to ignore the badge. It still
/// renders in the "Action needed" group so the panel stays an honest inventory.
///
/// Written as an equality rather than a one-arm `matches!` on purpose: the
/// cross-language parity test scrapes the two-class `matches!` literal out of
/// [`is_actionable_disposition`], and a second `matches!(disposition, …)` here
/// would give that scan a decoy to find.
pub(crate) fn is_badge_disposition(disposition: &str) -> bool {
    disposition == "action_required"
}

/// Resolve one entry's disposition. Mirrors `DeferralEntry.resolved_disposition`.
///
/// An explicit value that is NOT a known class is treated as ABSENT (Python's
/// `_coerce_disposition` does the same) — a typo in the sidecar must not pin an
/// entry to a tier that does not exist.
pub(crate) fn resolve_disposition(
    explicit: Option<&str>,
    condition_id: &str,
) -> (String, DispositionSource) {
    if let Some(raw) = explicit {
        let value = raw.trim();
        if deferral_registry::CLASSES.contains(&value) {
            return (value.to_string(), DispositionSource::Entry);
        }
    }
    let tier = deferral_registry::disposition_for(condition_id);
    let source = if deferral_registry::REGISTRY.get(condition_id).is_some() {
        DispositionSource::Registry
    } else {
        DispositionSource::Default
    };
    (tier.to_string(), source)
}

/// Build a [`RetrySummary`] for one condition from the raw jsonl text.
///
/// Every parse problem is skipped, never fatal: the trail is observability and
/// a corrupt line must not blank the entry it describes. Attempt COUNT uses
/// `started` rows only — the same rule `deferral_retry.attempt_count` uses, so
/// a handler that crashed (leaving no outcome row) still shows as an attempt
/// and the cap engages here exactly when it engages there.
pub(crate) fn summarize_retries(jsonl: &str, condition_id: &str) -> RetrySummary {
    let mut out = RetrySummary { cap: RETRY_MAX_ATTEMPTS, ..Default::default() };
    for line in jsonl.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if row.get("condition_id").and_then(|v| v.as_str()) != Some(condition_id) {
            continue;
        }
        let status = row.get("status").and_then(|v| v.as_str()).unwrap_or("");
        if status.is_empty() {
            continue;
        }
        if status == RETRY_STATUS_STARTED {
            out.attempts = out.attempts.saturating_add(1);
            continue;
        }
        match status {
            RETRY_STATUS_RETRIED => out.succeeded = out.succeeded.saturating_add(1),
            RETRY_STATUS_FAILED => out.failed = out.failed.saturating_add(1),
            RETRY_STATUS_INCONCLUSIVE => {
                out.inconclusive = out.inconclusive.saturating_add(1)
            }
            RETRY_STATUS_SKIPPED => out.skipped = out.skipped.saturating_add(1),
            // An unknown status is still shown — the driver may have grown a
            // state this build predates, and hiding it would be a lie of
            // omission. It simply lands in no counter.
            _ => {}
        }
        out.outcomes.push(RetryAttempt {
            ts: row.get("ts").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            status: status.to_string(),
            detail: row.get("detail").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        });
    }
    out.cap_reached = out.attempts >= out.cap;
    out
}

/// Parse one sidecar entry object. `None` when the load-bearing `condition_id`
/// is missing — mirrors `_entry_from_dict`, which skips a malformed entry
/// rather than failing the whole read.
fn entry_from_json(value: &serde_json::Value) -> Option<LedgerEntry> {
    let cid = value.get("condition_id").and_then(|v| v.as_str())?.to_string();
    if cid.is_empty() {
        return None;
    }
    let s = |key: &str| -> String {
        value.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
    };
    // `_entry_from_dict` coerces an unknown severity to "warning"; matching it
    // keeps the panel's chip and the .md header in agreement.
    let severity = match value.get("severity").and_then(|v| v.as_str()) {
        Some(sev @ ("critical" | "warning" | "info")) => sev.to_string(),
        _ => "warning".to_string(),
    };
    let kg_node_refs = value
        .get("kg_node_refs")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter().filter_map(|v| v.as_str()).map(|s| s.to_string()).collect()
        })
        .unwrap_or_default();
    let (disposition, disposition_source) =
        resolve_disposition(value.get("disposition").and_then(|v| v.as_str()), &cid);
    let actionable = is_actionable_disposition(&disposition);
    let title = {
        let t = s("title");
        if t.is_empty() { cid.replace('_', " ") } else { t }
    };
    Some(LedgerEntry {
        condition_id: cid,
        title,
        detected: s("detected"),
        why_deferred: s("why_deferred"),
        command_to_apply: s("command_to_apply"),
        severity,
        detected_at: s("detected_at"),
        kg_node_refs,
        auto_retryable: disposition == "auto_retryable",
        disposition,
        disposition_source,
        actionable,
        retries: RetrySummary { cap: RETRY_MAX_ATTEMPTS, ..Default::default() },
    })
}

/// The whole view, built from raw text. No disk, no subprocess — the entire
/// decision surface is exercisable from fixtures.
///
/// * `sidecar` — `None` when the file does not exist (nothing is deferred).
/// * `retries` — `None` when no trail exists yet (nothing has been retried).
pub(crate) fn build_view(
    scope: LedgerScope,
    scope_label: &str,
    folder: &Path,
    sidecar: Option<&str>,
    retries: Option<&str>,
) -> DeferralLedgerView {
    let mut warnings: Vec<String> = Vec::new();
    let mut entries: Vec<LedgerEntry> = Vec::new();
    let source = match sidecar {
        None => "absent",
        Some(text) => match serde_json::from_str::<serde_json::Value>(text) {
            Err(e) => {
                warnings.push(format!(
                    "UPDATE_DEFERRED.json is not valid JSON ({e}). The Markdown \
                     render is still on disk; `vco doctor` re-reads both."
                ));
                "unavailable"
            }
            Ok(payload) => {
                let version = payload.get("schema_version").and_then(|v| v.as_u64());
                if version != Some(SIDECAR_SCHEMA_VERSION) {
                    warnings.push(format!(
                        "UPDATE_DEFERRED.json declares schema_version {:?}; this \
                         launcher reads {}. Update the launcher rather than \
                         reading it as if it were the older shape.",
                        version, SIDECAR_SCHEMA_VERSION,
                    ));
                    "unavailable"
                } else {
                    match payload.get("entries").and_then(|v| v.as_array()) {
                        None => {
                            warnings.push(
                                "UPDATE_DEFERRED.json has no `entries` array — \
                                 treating as unreadable rather than empty."
                                    .to_string(),
                            );
                            "unavailable"
                        }
                        Some(raw) => {
                            let mut skipped = 0usize;
                            for item in raw {
                                match entry_from_json(item) {
                                    Some(e) => entries.push(e),
                                    None => skipped += 1,
                                }
                            }
                            if skipped > 0 {
                                warnings.push(format!(
                                    "{skipped} malformed entr{} skipped (no \
                                     condition_id).",
                                    if skipped == 1 { "y was" } else { "ies were" },
                                ));
                            }
                            "sidecar"
                        }
                    }
                }
            }
        },
    };

    if let Some(trail) = retries {
        for entry in entries.iter_mut() {
            entry.retries = summarize_retries(trail, &entry.condition_id);
        }
    }

    let actionable_count = entries.iter().filter(|e| e.actionable).count();
    let action_required_count = entries
        .iter()
        .filter(|e| is_badge_disposition(&e.disposition))
        .count();
    DeferralLedgerView {
        scope,
        scope_label: scope_label.to_string(),
        folder: folder.to_string_lossy().to_string(),
        source: source.to_string(),
        record_count: entries.len() - actionable_count,
        actionable_count,
        action_required_count,
        entries,
        warnings,
    }
}

/// The view for a ledger that EXISTS but could not be read (permission denied,
/// an IO error, a directory where the file should be).
///
/// The IO error is surfaced VERBATIM. The previous shape fabricated a
/// `{"schema_version":null,"io_error":…}` document and fed it to [`build_view`],
/// which then rendered a permission error as "declares schema_version None …
/// Update the launcher" — a remedy with nothing to do with the actual failure —
/// and produced a *different* wrong message when the error text contained a `"`,
/// because the fabricated JSON then failed to parse. Inventing a document to
/// describe the failure to read a document is one indirection too many.
pub(crate) fn unreadable_view(
    scope: LedgerScope,
    scope_label: &str,
    folder: &Path,
    io_error: &str,
) -> DeferralLedgerView {
    DeferralLedgerView {
        scope,
        scope_label: scope_label.to_string(),
        folder: folder.to_string_lossy().to_string(),
        source: "unavailable".to_string(),
        entries: Vec::new(),
        actionable_count: 0,
        action_required_count: 0,
        record_count: 0,
        warnings: vec![format!(
            "UPDATE_DEFERRED.json exists but could not be read: {io_error}. \
             Nothing is listed below — that is unknown state, not 'all clear'."
        )],
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Disk I/O
// ═══════════════════════════════════════════════════════════════════════

/// Read one folder's ledger. Soft-fail: an unreadable file becomes a warning
/// on an otherwise-valid view, never an `Err` — the panel must render.
pub(crate) fn read_ledger(
    scope: LedgerScope,
    scope_label: &str,
    folder: &Path,
) -> DeferralLedgerView {
    let sidecar = match std::fs::read_to_string(sidecar_path(folder)) {
        Ok(text) => Some(text),
        // Absent vs unreadable are DIFFERENT answers: absent means nothing is
        // deferred (the common, healthy case); a permission/IO error means we
        // do not know. Feeding the error case an empty-but-valid document
        // would render "all clear" over an unknown state.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return unreadable_view(scope, scope_label, folder, &e.to_string()),
    };
    let retries = std::fs::read_to_string(retries_path(folder)).ok();
    build_view(scope, scope_label, folder, sidecar.as_deref(), retries.as_deref())
}

/// Resolve a project's folder + display name from the DB.
fn project_target(db: &Db, project_id: &str) -> Result<(PathBuf, String), String> {
    let row = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {project_id} not found"))?;
    Ok((PathBuf::from(&row.folder_path), row.name))
}

/// Resolve the orchestrator clone root (DB cache first, then the walk-up).
fn root_target(db: &Db) -> Result<PathBuf, String> {
    crate::services::vco_lib_bridge::resolve_orchestrator_root(db).ok_or_else(|| {
        "orchestrator root unresolvable (no DB-cached install path and no clone \
         discoverable from the launcher binary) — the global deferral ledger \
         lives in the clone's .claude/context/"
            .to_string()
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Tauri commands
// ═══════════════════════════════════════════════════════════════════════

/// One PROJECT's ledger. Renders only on that project's Settings panel.
#[command]
pub async fn deferral_ledger_for_project(
    project_id: String,
    db: tauri::State<'_, Db>,
) -> Result<DeferralLedgerView, String> {
    let (folder, name) = project_target(db.inner(), &project_id)?;
    Ok(read_ledger(LedgerScope::Project, &name, &folder))
}

/// The ORCHESTRATOR ROOT's ledger — install/update-wide conditions. Renders
/// only on the global surface (Preferences → Updates) and behind the MenuBar
/// badge, never inside a project's Settings.
#[command]
pub async fn deferral_ledger_for_root(
    db: tauri::State<'_, Db>,
) -> Result<DeferralLedgerView, String> {
    let root = root_target(db.inner())?;
    Ok(read_ledger(LedgerScope::OrchestratorRoot, "Orchestrator root", &root))
}

/// Dismiss ONE entry, in ONE scope.
///
/// Delegates to `python -m vco_lib.project_init dismiss-deferral --json`, which
/// is the generic contract built "for a future launcher GUI button" — and which
/// also records the generalized dismissal key (`vco_lib.deferral_dismissal`) so
/// the condition stays silenced until its declared state actually changes. Not
/// re-implemented in Rust for exactly that reason: the keying rule, the legacy
/// migration arm and the manifest write are Python's, and a second
/// implementation would drift from the suppression check that reads them.
///
/// `scope` is taken, never inferred. `project_id` is required for
/// [`LedgerScope::Project`] and ignored for the root scope.
#[command]
pub async fn dismiss_deferral_entry(
    scope: LedgerScope,
    project_id: Option<String>,
    condition_id: String,
    db: tauri::State<'_, Db>,
) -> Result<DismissOutcome, String> {
    let (folder, label) = match scope {
        LedgerScope::Project => {
            let id = project_id
                .filter(|s| !s.is_empty())
                .ok_or_else(|| "project scope requires a project_id".to_string())?;
            project_target(db.inner(), &id)?
        }
        LedgerScope::OrchestratorRoot => {
            (root_target(db.inner())?, "Orchestrator root".to_string())
        }
    };
    // The CLI lives in the orchestrator clone; a project dismissal still
    // imports vco_lib from there (and runs with the clone as CWD, since
    // vco_lib is an implicit-namespace package, not a pip install).
    let root = root_target(db.inner())?;
    let payload = run_dismiss_cli(&root, &folder, &condition_id)?;
    Ok(DismissOutcome {
        condition_id,
        scope,
        scope_label: label,
        folder: folder.to_string_lossy().to_string(),
        dismissed: payload.dismissed,
        remaining: payload.remaining,
        reason: payload.reason,
    })
}

/// The `dismiss-deferral --json` stdout contract.
#[derive(Debug, Clone, Deserialize)]
struct DismissPayload {
    #[serde(default)]
    dismissed: bool,
    #[serde(default)]
    remaining: u32,
    #[serde(default)]
    reason: String,
}

/// Spawn the dismissal CLI and parse its single stdout JSON object.
///
/// Exit 0 covers both the happy path and the idempotent no-ops; a non-zero exit
/// means the file exists but is structurally malformed (exit 1) or the argv was
/// rejected (exit 2), and both are surfaced to the user rather than swallowed —
/// a Dismiss that silently did nothing is worse than an error message.
fn run_dismiss_cli(
    root: &Path,
    folder: &Path,
    condition_id: &str,
) -> Result<DismissPayload, String> {
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
        .ok_or_else(|| {
            "no vco_lib-capable python interpreter resolved — dismissal needs the \
             orchestrator venv (VCT_VENV / <root>/.venv)"
                .to_string()
        })?;
    let output = std::process::Command::new(&python)
        .silent()
        .arg("-m")
        .arg("vco_lib.project_init")
        .arg("dismiss-deferral")
        .arg("--folder")
        .arg(folder)
        .arg("--condition-id")
        .arg(condition_id)
        .arg("--json")
        .current_dir(root)
        .env("VCT_INSTALL_ROOT", root)
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("dismiss-deferral failed to spawn: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let first = stderr.lines().find(|l| !l.trim().is_empty()).unwrap_or("no stderr");
        return Err(format!(
            "dismiss-deferral exited {}: {}",
            output.status.code().unwrap_or(-1),
            first,
        ));
    }
    serde_json::from_slice::<DismissPayload>(&output.stdout).map_err(|e| {
        format!(
            "dismiss-deferral returned unparseable JSON ({e}): {}",
            String::from_utf8_lossy(&output.stdout).trim(),
        )
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Boot doctor + retry dispatch (wave-3 spec, wired from lib.rs setup())
// ═══════════════════════════════════════════════════════════════════════

/// Run the cheap `--scope boot` doctor pass, then fire the owed-work retry
/// driver DETACHED.
///
/// Called from ONE `tauri::async_runtime::spawn` in `lib.rs::setup()`, beside
/// the binary-freshness probe. Three contract points, all deliberate:
///
/// * **exit 0 and exit 1 are both valid.** `run_from_args` exits 1 iff a probe
///   reported a PROBLEM — that is the doctor working, not the doctor failing.
///   Treating exit 1 as an error would log a scary line on exactly the installs
///   the probe exists to help.
/// * **the launcher emits nothing.** No Tauri event, no toast, no banner. The
///   doctor's own `--json` path already writes the owed ledger entries
///   (`emit_findings`), and THOSE are what the WP-I panel renders. A second
///   notification channel for the same facts is how a UI starts contradicting
///   itself.
/// * **the retry dispatch fires unconditionally.** `deferral_retry` self-gates
///   on positive backend evidence AND a per-folder pidfile, so calling it when
///   nothing is owed is a cheap no-op — while gating it here on the doctor's
///   findings would duplicate a decision Python already owns.
///
/// Fully soft-fail: every failure path logs one line and returns.
pub(crate) async fn run_boot_doctor_and_retries(root: PathBuf) {
    let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
    else {
        tracing::warn!(
            "[vct] boot doctor: no vco_lib-capable python resolved — skipping \
             (the on-demand `vco doctor` still works once a venv exists)"
        );
        return;
    };

    // The doctor INHERITS the environment on purpose: its probes answer
    // questions ABOUT this machine's environment (is npx on PATH, are the npm
    // pins present), so the `vco_lib_bridge` env sandbox — which exists to stop
    // a stray KG_COLLECTION reaching a config writer — would distort the very
    // answers being asked for.
    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.doctor")
        .arg("--folder")
        .arg(&root)
        .arg("--scope")
        .arg("boot")
        .arg("--json")
        .current_dir(&root)
        .env("VCT_INSTALL_ROOT", &root)
        .stdin(std::process::Stdio::null());
    match cmd.output().await {
        Ok(out) => {
            let problems = summarize_boot_findings(&out.stdout);
            match problems {
                Some(0) => {}
                Some(n) => tracing::warn!(
                    "[vct] boot doctor: {n} problem(s) recorded in the deferral \
                     ledger — see the launcher's Updates page"
                ),
                None => tracing::warn!(
                    "[vct] boot doctor: report unparseable (exit {}) — no findings \
                     applied",
                    out.status.code().unwrap_or(-1),
                ),
            }
        }
        Err(e) => {
            tracing::warn!("[vct] boot doctor: failed to spawn ({e}) — skipping");
            return;
        }
    }

    // Detached: no `.await` on completion, stdio nulled. The driver holds its
    // own single-instance pidfile, so two boots a second apart cannot run two
    // KG seeds over one tree.
    let mut retry = tokio::process::Command::new(&python).silent();
    retry
        .arg("-m")
        .arg("vco_lib.deferral_retry")
        .arg("--folder")
        .arg(&root)
        .current_dir(&root)
        .env("VCT_INSTALL_ROOT", &root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        // Explicit for the reader: the child must OUTLIVE this task (that is
        // what "detached" means here). tokio reaps the dropped handle in its
        // orphan queue, so nothing zombies.
        .kill_on_drop(false);
    match retry.spawn() {
        Ok(child) => {
            // Drop the handle without waiting — the child outlives this task.
            std::mem::drop(child);
        }
        Err(e) => tracing::warn!(
            "[vct] deferral retry: failed to spawn ({e}) — owed work stays owed \
             and the session-start hook retries it"
        ),
    }
}

/// Count PROBLEM findings in a `--json` doctor report. `None` when the payload
/// is not a report this build understands (never a guess of zero).
pub(crate) fn summarize_boot_findings(stdout: &[u8]) -> Option<usize> {
    let payload: serde_json::Value = serde_json::from_slice(stdout).ok()?;
    let findings = payload.get("findings")?.as_array()?;
    Some(
        findings
            .iter()
            .filter(|f| f.get("status").and_then(|s| s.as_str()) == Some("problem"))
            .count(),
    )
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    fn sidecar(entries: &str) -> String {
        format!("{{\"schema_version\":1,\"generated_at\":\"t\",\"severity_max\":\"warning\",\"entries\":[{entries}]}}")
    }

    fn entry_json(cid: &str, extra: &str) -> String {
        format!(
            "{{\"condition_id\":\"{cid}\",\"title\":\"T {cid}\",\"detected\":\"D\",\
             \"why_deferred\":\"W\",\"command_to_apply\":\"cmd\",\"severity\":\"warning\",\
             \"kg_node_refs\":[],\"detected_at\":\"2026-08-27T00:00:00Z\"{extra}}}"
        )
    }

    // ── disposition resolution ────────────────────────────────────────

    /// ABSENT disposition ⇒ the registry answers. The default arm of the
    /// resolution chain, and the one every Rust-emitted entry takes.
    #[test]
    fn absent_disposition_falls_back_to_the_registry() {
        let (tier, src) = resolve_disposition(None, "launcher_binary_clobber_averted");
        assert_eq!(tier, "informational_record");
        assert_eq!(src, DispositionSource::Registry);
        assert!(!is_actionable_disposition(&tier));
    }

    /// ABSENT + UNREGISTERED ⇒ `action_required`, and it counts as work. The
    /// conservative default: an unclassified condition never hides in the fold.
    #[test]
    fn absent_disposition_on_unregistered_cid_is_action_required() {
        let (tier, src) = resolve_disposition(None, "a_cid_nobody_registered_v0291");
        assert_eq!(tier, deferral_registry::DEFAULT_CLASS);
        assert_eq!(src, DispositionSource::Default);
        assert!(is_actionable_disposition(&tier));
    }

    /// An EXPLICIT valid class wins over the registry — the same precedence
    /// `DeferralEntry.resolved_disposition` applies.
    #[test]
    fn explicit_disposition_overrides_the_registry() {
        let (tier, src) =
            resolve_disposition(Some("environmental"), "launcher_binary_stale");
        assert_eq!(tier, "environmental");
        assert_eq!(src, DispositionSource::Entry);
        // Registry alone would have said action_required.
        assert_eq!(deferral_registry::disposition_for("launcher_binary_stale"), "action_required");
    }

    /// An explicit value that is NOT a known class is treated as ABSENT
    /// (Python's `_coerce_disposition` posture) — a typo cannot invent a tier.
    #[test]
    fn unknown_explicit_disposition_is_ignored() {
        let (tier, src) =
            resolve_disposition(Some("urgent_ish"), "launcher_binary_stale");
        assert_eq!(tier, "action_required");
        assert_eq!(src, DispositionSource::Registry);
    }

    /// USER DECISION (2026-08-27): the BADGE counts `action_required` ONLY.
    /// `auto_retryable` is work VCO is doing itself — badging it nags the user
    /// about something already in hand.
    #[test]
    fn badge_disposition_is_action_required_only() {
        assert!(is_badge_disposition("action_required"));
        // The leave-alone half, and the whole point of the decision:
        assert!(!is_badge_disposition("auto_retryable"));
        assert!(!is_badge_disposition("environmental"));
        assert!(!is_badge_disposition("informational_record"));
        // The GROUP is unchanged — auto_retryable is still shown, just unbadged.
        assert!(is_actionable_disposition("auto_retryable"));
    }

    /// The two counts are DIFFERENT numbers whenever an auto_retryable entry is
    /// present, and the group / record split is untouched by the badge change.
    #[test]
    fn badge_count_is_narrower_than_the_group_count() {
        let text = sidecar(&format!(
            "{},{},{},{}",
            entry_json("launcher_binary_stale", ""),
            entry_json("a_cid_nobody_registered_v0291", ""),
            entry_json("kg_sync_no_embedding_backend", ""),
            entry_json("kg_access_phantom_repaired", ""),
        ));
        let v = build_view(
            LedgerScope::OrchestratorRoot,
            "Orchestrator root",
            Path::new("/root"),
            Some(&text),
            None,
        );
        // 2 action_required (one registered, one conservative default)
        // + 1 auto_retryable = 3 in the group; the record is outside it.
        assert_eq!(v.actionable_count, 3, "the GROUP keeps auto_retryable");
        assert_eq!(v.action_required_count, 2, "the BADGE does not");
        assert_eq!(v.record_count, 1);
        // The per-entry group flag is the group's, not the badge's.
        let retryable = v
            .entries
            .iter()
            .find(|e| e.condition_id == "kg_sync_no_embedding_backend")
            .unwrap();
        assert!(retryable.actionable);
        assert!(!is_badge_disposition(&retryable.disposition));
    }

    /// An explicit `auto_retryable` override on a cid the registry calls
    /// `action_required` must UNBADGE it — the badge reads the RESOLVED tier,
    /// not the registry.
    #[test]
    fn explicit_auto_retryable_override_leaves_the_badge_alone() {
        let text = sidecar(&entry_json(
            "launcher_binary_stale",
            ",\"disposition\":\"auto_retryable\"",
        ));
        let v = build_view(LedgerScope::Project, "P", Path::new("/p"), Some(&text), None);
        assert_eq!(deferral_registry::disposition_for("launcher_binary_stale"), "action_required");
        assert_eq!(v.actionable_count, 1, "still in the group");
        assert_eq!(v.action_required_count, 0, "but the override unbadges it");
    }

    /// The panel's partition and the registry's badge helper must agree for
    /// every pattern in the shipped table — that agreement is what makes the
    /// GUI group and the CLAUDE.md list hold the same entries.
    #[test]
    fn is_actionable_matches_registry_for_every_pattern() {
        for pattern in deferral_registry::REGISTRY.patterns() {
            // Glob patterns are turned into a concrete id by replacing the
            // wildcard, so the lookup exercises the same path a real cid takes.
            let cid = pattern.replace('*', "x");
            let tier = deferral_registry::disposition_for(&cid);
            assert_eq!(
                is_actionable_disposition(tier),
                deferral_registry::is_actionable(&cid),
                "partition disagreement on {cid} (tier {tier})",
            );
        }
    }

    // ── view building ─────────────────────────────────────────────────

    #[test]
    fn absent_sidecar_renders_an_empty_all_clear_view() {
        let v = build_view(
            LedgerScope::Project,
            "Proj",
            Path::new("/p"),
            None,
            None,
        );
        assert_eq!(v.source, "absent");
        assert!(v.entries.is_empty());
        assert_eq!(v.actionable_count, 0);
        assert_eq!(v.record_count, 0);
        assert!(v.warnings.is_empty());
    }

    #[test]
    fn entries_split_into_actionable_and_records() {
        let text = sidecar(&format!(
            "{},{},{}",
            entry_json("launcher_binary_stale", ""),
            entry_json("kg_access_phantom_repaired", ""),
            entry_json("kg_sync_no_embedding_backend", ""),
        ));
        let v = build_view(
            LedgerScope::OrchestratorRoot,
            "Orchestrator root",
            Path::new("/root"),
            Some(&text),
            None,
        );
        assert_eq!(v.source, "sidecar");
        assert_eq!(v.entries.len(), 3);
        // action_required + auto_retryable = actionable; the record is not.
        assert_eq!(v.actionable_count, 2);
        assert_eq!(v.record_count, 1);
        // …and the BADGE is the narrower number: the auto_retryable entry is in
        // the group but does not badge.
        assert_eq!(v.action_required_count, 1);
        let retryable = v
            .entries
            .iter()
            .find(|e| e.condition_id == "kg_sync_no_embedding_backend")
            .unwrap();
        assert!(retryable.auto_retryable, "auto_retryable must be flagged for the panel");
        assert!(retryable.actionable);
        let record = v
            .entries
            .iter()
            .find(|e| e.condition_id == "kg_access_phantom_repaired")
            .unwrap();
        assert!(!record.actionable);
        assert!(!record.auto_retryable);
    }

    /// Multi-line `command_to_apply` with `#` comment lines must survive
    /// VERBATIM — the whole reason the sidecar, not the .md, is the source.
    #[test]
    fn multiline_command_blocks_survive_verbatim() {
        let cmd = "# Restart the launcher: the pass re-runs at every boot\n\
                   # and clears this entry once it completes cleanly.\n\
                   python install.py --update";
        let text = sidecar(&format!(
            "{{\"condition_id\":\"convergence_pending\",\"title\":\"T\",\
             \"command_to_apply\":{}}}",
            serde_json::to_string(cmd).unwrap(),
        ));
        let v = build_view(
            LedgerScope::OrchestratorRoot,
            "Orchestrator root",
            Path::new("/root"),
            Some(&text),
            None,
        );
        assert_eq!(v.entries[0].command_to_apply, cmd);
        assert_eq!(v.entries[0].command_to_apply.lines().count(), 3);
    }

    /// A malformed ENTRY is skipped with a warning; its siblings still render.
    #[test]
    fn malformed_entry_is_skipped_not_fatal() {
        let text = sidecar(&format!(
            "{},{{\"title\":\"no cid\"}}",
            entry_json("launcher_binary_stale", ""),
        ));
        let v = build_view(LedgerScope::Project, "P", Path::new("/p"), Some(&text), None);
        assert_eq!(v.entries.len(), 1);
        assert_eq!(v.warnings.len(), 1);
        assert!(v.warnings[0].contains("malformed"));
    }

    /// A newer schema is REFUSED, not half-read. Nothing renders and the
    /// warning says why — the same "don't guess" rule `_parse_json_sidecar` uses.
    #[test]
    fn unknown_schema_version_renders_nothing_and_warns() {
        let text = "{\"schema_version\":99,\"entries\":[{\"condition_id\":\"x\"}]}";
        let v = build_view(LedgerScope::Project, "P", Path::new("/p"), Some(text), None);
        assert_eq!(v.source, "unavailable");
        assert!(v.entries.is_empty());
        assert!(v.warnings[0].contains("schema_version"));
    }

    #[test]
    fn unparseable_sidecar_warns_and_renders_nothing() {
        let v = build_view(LedgerScope::Project, "P", Path::new("/p"), Some("{nope"), None);
        assert_eq!(v.source, "unavailable");
        assert!(v.entries.is_empty());
        assert!(!v.warnings.is_empty());
    }

    /// Scope is carried on the wire, never inferred by the FE.
    #[test]
    fn view_carries_its_scope_and_folder() {
        let v = build_view(
            LedgerScope::Project,
            "My Project",
            Path::new("/home/u/proj"),
            None,
            None,
        );
        assert_eq!(v.scope, LedgerScope::Project);
        assert_eq!(v.scope_label, "My Project");
        assert_eq!(v.folder, "/home/u/proj");
    }

    /// The WIRE strings the FE branches on. Asserted through serde (not a
    /// parallel helper) so this pins what actually crosses the boundary.
    #[test]
    fn scope_serializes_to_the_snake_case_wire_names() {
        assert_eq!(
            serde_json::to_string(&LedgerScope::Project).unwrap(),
            "\"project\""
        );
        assert_eq!(
            serde_json::to_string(&LedgerScope::OrchestratorRoot).unwrap(),
            "\"orchestrator_root\""
        );
        assert_eq!(
            serde_json::to_string(&DispositionSource::Entry).unwrap(),
            "\"entry\""
        );
        assert_eq!(
            serde_json::to_string(&DispositionSource::Registry).unwrap(),
            "\"registry\""
        );
        assert_eq!(
            serde_json::to_string(&DispositionSource::Default).unwrap(),
            "\"default\""
        );
    }

    // ── retry trail ───────────────────────────────────────────────────

    fn trail() -> String {
        [
            r#"{"ts":"t1","condition_id":"kg_sync_no_embedding_backend","status":"started","detail":"handler kg_seed"}"#,
            r#"{"ts":"t2","condition_id":"kg_sync_no_embedding_backend","status":"inconclusive","detail":"ran, condition still present"}"#,
            r#"{"ts":"t3","condition_id":"other_condition","status":"started","detail":"nope"}"#,
            r#"{"ts":"t4","condition_id":"kg_sync_no_embedding_backend","status":"started","detail":"handler kg_seed"}"#,
            r#"{"ts":"t5","condition_id":"kg_sync_no_embedding_backend","status":"failed","detail":"exit 1"}"#,
            "",
            "{not json at all",
        ]
        .join("\n")
    }

    /// Attempts count `started` rows ONLY (matching `attempt_count`), outcome
    /// rows carry the verdicts, and another condition's rows never leak in.
    #[test]
    fn retry_summary_counts_started_rows_and_isolates_the_condition() {
        let s = summarize_retries(&trail(), "kg_sync_no_embedding_backend");
        assert_eq!(s.attempts, 2);
        assert_eq!(s.cap, RETRY_MAX_ATTEMPTS);
        assert!(!s.cap_reached);
        assert_eq!(s.inconclusive, 1);
        assert_eq!(s.failed, 1);
        assert_eq!(s.succeeded, 0);
        assert_eq!(s.outcomes.len(), 2, "started rows are not outcome rows");
        assert_eq!(s.outcomes[0].status, "inconclusive");
        assert_eq!(s.outcomes[1].status, "failed");
        // Leave-alone: the other condition's summary is untouched by these rows.
        let other = summarize_retries(&trail(), "other_condition");
        assert_eq!(other.attempts, 1);
        assert_eq!(other.outcomes.len(), 0);
    }

    /// `inconclusive` is its OWN counter — never folded into `failed`. The
    /// driver refused to claim failure and the panel must not claim it either.
    #[test]
    fn inconclusive_is_not_counted_as_failed() {
        let rows = r#"{"ts":"t","condition_id":"c","status":"inconclusive","detail":"d"}"#;
        let s = summarize_retries(rows, "c");
        assert_eq!(s.inconclusive, 1);
        assert_eq!(s.failed, 0);
    }

    #[test]
    fn cap_reached_when_started_rows_hit_the_ceiling() {
        let mut rows = String::new();
        for i in 0..RETRY_MAX_ATTEMPTS {
            rows.push_str(&format!(
                "{{\"ts\":\"t{i}\",\"condition_id\":\"c\",\"status\":\"started\",\"detail\":\"d\"}}\n"
            ));
        }
        let s = summarize_retries(&rows, "c");
        assert_eq!(s.attempts, RETRY_MAX_ATTEMPTS);
        assert!(s.cap_reached);
    }

    #[test]
    fn empty_or_absent_trail_is_an_empty_summary() {
        let s = summarize_retries("", "c");
        assert_eq!(s.attempts, 0);
        assert!(s.outcomes.is_empty());
        assert!(!s.cap_reached);
        assert_eq!(s.cap, RETRY_MAX_ATTEMPTS);
    }

    /// An unknown status still RENDERS (no counter, but visible) — a driver
    /// that grows a new state must not go silent in an older launcher.
    #[test]
    fn unknown_status_is_rendered_without_a_counter() {
        let rows = r#"{"ts":"t","condition_id":"c","status":"quarantined","detail":"d"}"#;
        let s = summarize_retries(rows, "c");
        assert_eq!(s.outcomes.len(), 1);
        assert_eq!(s.outcomes[0].status, "quarantined");
        assert_eq!(s.failed + s.succeeded + s.inconclusive + s.skipped, 0);
    }

    /// The trail is attached to the matching entry only.
    #[test]
    fn build_view_attaches_retries_per_entry() {
        let text = sidecar(&format!(
            "{},{}",
            entry_json("kg_sync_no_embedding_backend", ""),
            entry_json("launcher_binary_stale", ""),
        ));
        let v = build_view(
            LedgerScope::Project,
            "P",
            Path::new("/p"),
            Some(&text),
            Some(&trail()),
        );
        let retried = v
            .entries
            .iter()
            .find(|e| e.condition_id == "kg_sync_no_embedding_backend")
            .unwrap();
        assert_eq!(retried.retries.attempts, 2);
        let untouched = v
            .entries
            .iter()
            .find(|e| e.condition_id == "launcher_binary_stale")
            .unwrap();
        assert_eq!(untouched.retries.attempts, 0, "leave-alone: no rows, no attempts");
        assert!(
            untouched.retries.outcomes.is_empty(),
            "leave-alone: another entry's outcome rows must not attach here",
        );
    }

    // ── disk read ─────────────────────────────────────────────────────

    #[test]
    fn read_ledger_reads_sidecar_and_trail_from_disk() {
        let td = tempfile::TempDir::new().unwrap();
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).unwrap();
        std::fs::write(
            ctx.join("UPDATE_DEFERRED.json"),
            sidecar(&entry_json("kg_sync_no_embedding_backend", "")),
        )
        .unwrap();
        let logs = td.path().join(".claude").join("logs");
        std::fs::create_dir_all(&logs).unwrap();
        std::fs::write(logs.join("deferral-retries.jsonl"), trail()).unwrap();

        let v = read_ledger(LedgerScope::Project, "P", td.path());
        assert_eq!(v.source, "sidecar");
        assert_eq!(v.actionable_count, 1);
        // The single entry is auto_retryable: in the group, not on the badge.
        assert_eq!(v.action_required_count, 0);
        assert_eq!(v.entries[0].retries.attempts, 2);
    }

    /// NIT (wave-4): an UNREADABLE sidecar surfaces its own IO error. It used to
    /// be wrapped in a fabricated `{"schema_version":null,…}` document, so a
    /// permission error rendered as "declares schema_version None … Update the
    /// launcher" — a remedy for a different problem entirely.
    ///
    /// RED-PROOF: against the pre-fix body the warning below contains
    /// "schema_version" and "Update the launcher", and both assertions fail.
    /// The unreadable file here is a DIRECTORY at the sidecar's path, which
    /// errors for every uid (a chmod-000 fixture is a no-op under root).
    #[test]
    fn unreadable_sidecar_surfaces_the_io_error_not_a_schema_complaint() {
        let td = tempfile::TempDir::new().unwrap();
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(ctx.join("UPDATE_DEFERRED.json")).unwrap();

        let v = read_ledger(LedgerScope::Project, "P", td.path());
        assert_eq!(v.source, "unavailable");
        assert!(v.entries.is_empty());
        assert_eq!(v.warnings.len(), 1);
        let w = &v.warnings[0];
        assert!(
            w.contains("could not be read"),
            "the notice must say what actually happened, got: {w}"
        );
        assert!(
            !w.contains("schema_version"),
            "an IO failure is not a schema complaint, got: {w}"
        );
        assert!(
            !w.contains("Update the launcher"),
            "and 'update the launcher' is not its remedy, got: {w}"
        );
        assert_eq!(v.actionable_count, 0);
        assert_eq!(v.action_required_count, 0);
    }

    /// The IO text is carried VERBATIM — including a `"`, which used to break
    /// the fabricated JSON and produce a third, differently-wrong message.
    #[test]
    fn unreadable_view_carries_a_quoted_io_error_verbatim() {
        let v = unreadable_view(
            LedgerScope::OrchestratorRoot,
            "Orchestrator root",
            Path::new("/root"),
            "Permission denied (os error 13) reading \"UPDATE_DEFERRED.json\"",
        );
        assert_eq!(v.source, "unavailable");
        assert!(v.warnings[0].contains("Permission denied (os error 13)"));
        assert!(v.warnings[0].contains("\"UPDATE_DEFERRED.json\""));
        assert!(v.entries.is_empty());
    }

    /// A folder with NO ledger is the healthy case, not an error.
    #[test]
    fn read_ledger_on_a_clean_folder_is_absent_not_error() {
        let td = tempfile::TempDir::new().unwrap();
        let v = read_ledger(LedgerScope::OrchestratorRoot, "Orchestrator root", td.path());
        assert_eq!(v.source, "absent");
        assert_eq!(v.actionable_count, 0);
        assert!(v.warnings.is_empty());
    }

    // ── boot doctor payload ───────────────────────────────────────────

    #[test]
    fn boot_findings_count_problems_only() {
        let payload = br#"{"schema_version":1,"ok":false,"findings":[
            {"probe":"a","status":"problem"},
            {"probe":"b","status":"unknown"},
            {"probe":"c","status":"ok"},
            {"probe":"d","status":"problem"}]}"#;
        assert_eq!(summarize_boot_findings(payload), Some(2));
    }

    /// An unparseable report yields `None` — never a fabricated zero, which
    /// would read as "the doctor found nothing wrong".
    #[test]
    fn boot_findings_unparseable_is_none_not_zero() {
        assert_eq!(summarize_boot_findings(b"not json"), None);
        assert_eq!(summarize_boot_findings(b"{\"schema_version\":1}"), None);
    }

    /// A clean report is a real zero — distinguishable from `None`.
    #[test]
    fn boot_findings_clean_report_is_zero() {
        assert_eq!(
            summarize_boot_findings(br#"{"ok":true,"findings":[{"probe":"a","status":"ok"}]}"#),
            Some(0)
        );
    }
}
