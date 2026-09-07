// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Bundle-staleness census for the GUI (v0.2.92 WP-D, R27's fourth surface).
//!
//! ## What this is
//!
//! A thin, READ-ONLY wrapper around `python -m vco_lib.bundle_staleness
//! --json`. The verdict logic lives entirely in Python (the ONE bundle
//! engine in dry-run IS the census — R27); nothing here re-derives it, so
//! the GUI cannot disagree with what an update would actually do. That
//! duplicated-verdict defect is precisely how the launcher told a real user
//! "up to date" for five weeks while 12 of their 13 projects carried
//! bundles from June.
//!
//! ## Two contracts this module must not break
//!
//! **1. The census must stay read-only.** `--json` is read-only by design
//! (`vco_lib/bundle_staleness.py::main` computes
//! `persist = args.refresh_ledger or not args.json`), and `--refresh-ledger`
//! is the explicit opt-in. This command NEVER passes it: a GUI that polls a
//! census must not mutate the ledger it is displaying, or the user's
//! deferral badge count flaps every time a page mounts.
//!
//! **2. `unknown` is a verdict, not a rendering detail.** Three-state in,
//! three-state out. A project VCO could not read is never reported as
//! `current`, and never folded into `stale` either (you cannot bundle-update
//! a project whose folder is gone — a remedy offered for an undetermined
//! project is a second lie). Likewise a census that could not RUN returns
//! `determined: false` with `summary: None` — an empty list and a failed
//! probe are distinguishable by the caller, because "0 stale" is a claim and
//! "I could not determine this" is the absence of one.
//!
//! ## Spawn shape
//!
//! Follows the established `python -m vco_lib.<module>` pattern (the same
//! two public helpers `commands::codegraph::configure_vco_lib_command` uses:
//! `python_resolve::resolve_python_for_vco_lib` for the interpreter and
//! `services::vco_lib_bridge::resolve_orchestrator_root` for the CWD +
//! `VCT_INSTALL_ROOT`). It is NOT env-sandboxed via
//! `vco_lib_bridge::reinject_minimal_env`: the census reads `launcher.db`
//! and the projects' own trees, matching the inheriting shape the
//! `project_init` subcommand spawns use.

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, State};
use tokio::time::timeout;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// MUST MATCH `vco_lib/bundle_staleness.py::CENSUS_SCHEMA`. A payload
/// carrying a different schema is reported as UNDETERMINED rather than
/// parsed on a guess — a launcher that half-understands a newer census
/// would be exactly the "confidently wrong" failure WP-D exists to stop.
const EXPECTED_CENSUS_SCHEMA: u32 = 1;

/// Hard ceiling on the census subprocess. The census is one engine dry-run
/// per registered project (filesystem + registry only — no Weaviate), which
/// the Python side measures at well under a second each. 180 s absorbs a
/// large population on a cold/slow disk while still reaping a genuinely
/// stuck probe instead of hanging the Projects page forever.
const CENSUS_TIMEOUT_SECS: u64 = 180;

/// The registry value that means the census actually read the project list.
/// Anything else (notably Python's `"unavailable"`, emitted when
/// `launcher.db` cannot be read — e.g. a fresh root install before the
/// first launcher boot) is NOT a population of zero; it is "could not
/// determine", and is reported as such.
const REGISTRY_OK: &str = "launcher.db";

// ─── Wire types (mirrored by `launcher/src/lib/types/launcher.ts`) ───────

/// One project row. `verdict` is one of `current` / `stale` / `unknown`
/// (normalised by [`normalize_verdict`], which fails SAFE toward `unknown`).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BundleStalenessProject {
    pub id: String,
    pub name: String,
    pub folder: String,
    pub verdict: String,
    pub reason: String,
    pub changed_files: Vec<String>,
    pub user_modified: u32,
}

#[derive(Debug, Clone, Serialize, PartialEq, Default)]
pub struct BundleStalenessSummary {
    pub current: u32,
    pub stale: u32,
    pub unknown: u32,
}

/// The command's result.
///
/// `determined == false` is the explicit "could not determine": `summary`
/// is `None` (NOT a zeroed struct) and `projects` is empty, so no caller can
/// mistake a failed probe for a clean population.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BundleStalenessCensus {
    pub determined: bool,
    pub error: Option<String>,
    pub registry: Option<String>,
    pub running_version: Option<String>,
    pub projects: Vec<BundleStalenessProject>,
    pub summary: Option<BundleStalenessSummary>,
    pub remedy_gui: Option<String>,
    pub remedy_cli: Option<String>,
}

impl BundleStalenessCensus {
    /// The "could not determine" result. Deliberately has NO summary — a
    /// zeroed summary here would be a claim we cannot support.
    pub fn undetermined(error: impl Into<String>) -> Self {
        BundleStalenessCensus {
            determined: false,
            error: Some(error.into()),
            registry: None,
            running_version: None,
            projects: Vec::new(),
            summary: None,
            remedy_gui: None,
            remedy_cli: None,
        }
    }
}

// ─── Python payload (`vco_lib/bundle_staleness.py` §5.1) ────────────────

#[derive(Deserialize)]
struct PyRunning {
    #[serde(default)]
    version: Option<String>,
}

/// Presence-only marker for the payload's `summary` block. Its FIELDS are
/// deliberately not read — the counts the GUI shows are recomputed from the
/// rows (see `census_from_stdout`) so the badge cannot disagree with the
/// list. Unknown fields are ignored by serde, so this stays a shape gate.
#[derive(Deserialize)]
struct PySummary {}

#[derive(Deserialize)]
struct PyRemedy {
    #[serde(default)]
    gui: Option<String>,
    #[serde(default)]
    cli: Option<String>,
}

#[derive(Deserialize)]
struct PyProject {
    #[serde(default)]
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    folder: String,
    #[serde(default)]
    verdict: String,
    #[serde(default)]
    reason: String,
    #[serde(default)]
    changed_files: Vec<String>,
    #[serde(default)]
    user_modified: u32,
}

#[derive(Deserialize)]
struct PyPayload {
    #[serde(default)]
    schema: u32,
    #[serde(default)]
    running: Option<PyRunning>,
    #[serde(default)]
    registry: Option<String>,
    #[serde(default)]
    projects: Vec<PyProject>,
    /// Presence-only shape gate: a payload without a summary block is
    /// truncated/foreign, and the GUI must not read its rows as gospel.
    /// The COUNTS the GUI shows are recomputed from the rows below.
    #[serde(default)]
    summary: Option<PySummary>,
    #[serde(default)]
    remedy: Option<PyRemedy>,
}

// ─── Pure parsing (unit-tested without spawning anything) ───────────────

/// Normalise a raw verdict string. Fails SAFE: anything that is not
/// positively `current` or `stale` becomes `unknown`. A verdict we do not
/// recognise is, by definition, one we could not determine.
fn normalize_verdict(raw: &str) -> &'static str {
    match raw {
        "current" => "current",
        "stale" => "stale",
        _ => "unknown",
    }
}

/// Parse the census stdout into a [`BundleStalenessCensus`].
///
/// Every failure mode — no JSON, malformed JSON, unexpected schema, a
/// registry the census could not read — returns the UNDETERMINED result
/// with a reason, never an empty-but-determined one.
pub(crate) fn census_from_stdout(stdout: &str) -> BundleStalenessCensus {
    // STRICT: parse the WHOLE stdout. No `stdout.find('{')` salvage.
    //
    // v0.2.92 (2026-09-05). This site used to scan forward to the first '{'
    // and parse from there, borrowing the rationale of `kg_check_duplicates`
    // ("a venv activation shim may prefix lines"). That rationale does not
    // transfer: `kg_check_duplicates` spawns a `.claude/scripts/` WRAPPER,
    // whereas `run_census` below spawns `<python> -m vco_lib.bundle_staleness`
    // on a resolved interpreter — there is no shim in front of it, and
    // `vco_lib/bundle_staleness.py::main` writes exactly one thing to stdout
    // (`json.dumps(payload, indent=2, sort_keys=True)`); its error path goes
    // to stderr.
    //
    // So the salvage guarded against nothing here, and cost something real:
    // this release fixed a field bug in which a LIBRARY three frames below a
    // CLI handler relayed a child process's captured stdout onto the parent's,
    // corrupting a JSON contract. The reason that was CAUGHT is that the
    // sibling parse site (`projects_v2`'s migrate-schema) was strict. A
    // forward-scanning parse is the exact shape that would swallow that class
    // silently on the census path — the engine dry-run this census runs
    // in-process (`install_project_bundle`) is a 16k-line module with a wide
    // call surface, so "nobody prints today" is a fact with an expiry date,
    // not an invariant. Strict + an actionable message is what makes the next
    // polluting emitter loud instead of invisible.
    let payload: PyPayload = match serde_json::from_str(stdout) {
        Ok(p) => p,
        Err(e) => {
            return BundleStalenessCensus::undetermined(format!(
                "bundle census (`python -m vco_lib.bundle_staleness --json`) \
                 produced unparseable output ({}); first stdout line was `{}`",
                e,
                crate::commands::subprocess_contract::stdout_parse_diagnostic(stdout),
            ))
        }
    };
    if payload.schema != EXPECTED_CENSUS_SCHEMA {
        return BundleStalenessCensus::undetermined(format!(
            "bundle census schema {} is not the schema this launcher \
             understands ({}) — update the launcher",
            payload.schema, EXPECTED_CENSUS_SCHEMA
        ));
    }
    if payload.summary.is_none() {
        return BundleStalenessCensus::undetermined(
            "bundle census payload carried no summary block",
        );
    }
    let registry = payload.registry.clone().unwrap_or_default();
    if registry != REGISTRY_OK {
        // "unavailable" is Python's word for "the project registry could not
        // be read". Zero rows from an unreadable registry is NOT a clean
        // population, and must never render as one.
        return BundleStalenessCensus::undetermined(format!(
            "the project registry was not readable (registry: {})",
            if registry.is_empty() { "missing" } else { &registry }
        ));
    }

    let projects: Vec<BundleStalenessProject> = payload
        .projects
        .into_iter()
        .map(|p| BundleStalenessProject {
            id: p.id,
            name: p.name,
            folder: p.folder,
            verdict: normalize_verdict(&p.verdict).to_string(),
            reason: p.reason,
            changed_files: p.changed_files,
            user_modified: p.user_modified,
        })
        .collect();

    // Recompute the counts from the ROWS WE ARE RETURNING rather than
    // echoing Python's summary. The two agree for every well-formed payload;
    // deriving them here makes it structurally impossible for the badge
    // count to disagree with the list rendered beneath it (including after
    // `normalize_verdict` has demoted an unrecognised verdict to `unknown`,
    // which Python's own tally would not have counted anywhere).
    let mut summary = BundleStalenessSummary::default();
    for p in &projects {
        match p.verdict.as_str() {
            "current" => summary.current += 1,
            "stale" => summary.stale += 1,
            _ => summary.unknown += 1,
        }
    }

    let (remedy_gui, remedy_cli) = match payload.remedy {
        Some(r) => (r.gui, r.cli),
        None => (None, None),
    };

    BundleStalenessCensus {
        determined: true,
        error: None,
        registry: Some(registry),
        running_version: payload.running.and_then(|r| r.version),
        projects,
        summary: Some(summary),
        remedy_gui,
        remedy_cli,
    }
}

// ─── Command ────────────────────────────────────────────────────────────

/// Run the READ-ONLY bundle-staleness census over every registered project.
///
/// Always returns `Ok`: a probe failure is data (`determined: false` plus a
/// reason), not a transport error. Returning `Err` here would invite the
/// frontend's `catch` to render "nothing to report", which is the one thing
/// this feature must never do.
#[command]
pub async fn bundle_staleness_census(db: State<'_, Db>) -> Result<BundleStalenessCensus, String> {
    Ok(run_census(&db).await)
}

async fn run_census(db: &Db) -> BundleStalenessCensus {
    let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() else {
        return BundleStalenessCensus::undetermined(
            "no vco_lib-capable python interpreter resolved (VCT_VENV / \
             <root>/.venv / <root>/claude_mcp_servers/.venv / system python3)",
        );
    };
    let Some(root): Option<PathBuf> = crate::services::vco_lib_bridge::resolve_orchestrator_root(db)
    else {
        return BundleStalenessCensus::undetermined(
            "orchestrator root unresolvable — cannot locate the vco_lib package",
        );
    };

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.bundle_staleness")
        // READ-ONLY. `--refresh-ledger` is deliberately absent: see the
        // module docs. Adding it here would make every Projects-page mount
        // rewrite the census state and flap the deferral badge.
        .arg("--json")
        .arg("--orchestrator-root")
        .arg(&root)
        .current_dir(&root)
        .env("VCT_INSTALL_ROOT", &root)
        .stdin(std::process::Stdio::null());

    let out = match timeout(Duration::from_secs(CENSUS_TIMEOUT_SECS), cmd.output()).await {
        Ok(Ok(out)) => out,
        Ok(Err(e)) => {
            return BundleStalenessCensus::undetermined(format!(
                "bundle census failed to start: {}",
                e
            ))
        }
        Err(_) => {
            return BundleStalenessCensus::undetermined(format!(
                "bundle census timed out after {}s",
                CENSUS_TIMEOUT_SECS
            ))
        }
    };
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        let head = stderr
            .lines()
            .find(|l| !l.trim().is_empty())
            .unwrap_or("no stderr")
            .to_string();
        return BundleStalenessCensus::undetermined(format!(
            "bundle census exited {}: {}",
            out.status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "signal".into()),
            head
        ));
    }
    census_from_stdout(&String::from_utf8_lossy(&out.stdout))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A well-formed payload carrying one of each verdict.
    fn three_verdict_json() -> String {
        serde_json::json!({
            "schema": 1,
            "running": {"version": "0.2.92", "commit": "abc1234"},
            "registry": "launcher.db",
            "orchestrator_root": "/opt/vco",
            "projects": [
                {"id": "a", "name": "Fresh", "folder": "/p/a", "verdict": "current",
                 "reason": "noop", "changed_files": [], "user_modified": 0},
                {"id": "b", "name": "Old", "folder": "/p/b", "verdict": "stale",
                 "reason": "files_changed",
                 "changed_files": [".claude/hooks/x.sh", ".claude/agents/y.md"],
                 "user_modified": 1},
                {"id": "c", "name": "Moved", "folder": "/p/c", "verdict": "unknown",
                 "reason": "folder_missing", "changed_files": [], "user_modified": 0}
            ],
            "summary": {"current": 1, "stale": 1, "unknown": 1},
            "remedy": {"gui": "Projects → Update all bundles", "cli": "python -m vco_lib..."}
        })
        .to_string()
    }

    #[test]
    fn propagates_all_three_verdicts_faithfully() {
        let c = census_from_stdout(&three_verdict_json());
        assert!(c.determined);
        assert_eq!(c.projects.len(), 3);
        assert_eq!(c.projects[0].verdict, "current");
        assert_eq!(c.projects[1].verdict, "stale");
        assert_eq!(c.projects[2].verdict, "unknown");
        // The unknown row keeps its cause — that is what makes it actionable.
        assert_eq!(c.projects[2].reason, "folder_missing");
        assert_eq!(c.projects[1].changed_files.len(), 2);
        let s = c.summary.expect("determined census carries a summary");
        assert_eq!(s, BundleStalenessSummary { current: 1, stale: 1, unknown: 1 });
        assert_eq!(c.running_version.as_deref(), Some("0.2.92"));
        assert_eq!(c.remedy_gui.as_deref(), Some("Projects → Update all bundles"));
    }

    #[test]
    fn unknown_is_never_collapsed_into_current_or_stale() {
        let c = census_from_stdout(&three_verdict_json());
        // Assert on the ROWS, not just the tally: a parser that folds
        // `unknown` into `stale` while echoing Python's counts would pass a
        // summary-only assertion and still render the wrong chip.
        let unknown_rows: Vec<_> =
            c.projects.iter().filter(|p| p.verdict == "unknown").collect();
        assert_eq!(unknown_rows.len(), 1, "rows: {:?}", c.projects);
        assert_eq!(unknown_rows[0].id, "c");
        assert!(!c.projects.iter().any(|p| p.id == "c" && p.verdict != "unknown"));
        let s = c.summary.unwrap();
        assert_eq!(s.unknown, 1);
        assert_eq!(s.current, 1);
        assert_eq!(s.stale, 1);
    }

    #[test]
    fn pretty_printed_multiline_json_parses() {
        // The real CLI emits `json.dumps(..., indent=2)`. Multi-line is the
        // NORMAL shape and must parse — strictness is about extra bytes
        // around the document, not about the document being one line.
        // Leading/trailing whitespace is JSON-insignificant and serde eats it.
        let raw = serde_json::to_string_pretty(
            &serde_json::from_str::<serde_json::Value>(&three_verdict_json()).unwrap(),
        )
        .unwrap();
        let c = census_from_stdout(&format!("\n{}\n", raw));
        assert!(c.determined);
        assert_eq!(c.projects.len(), 3);
    }

    /// v0.2.92: the census parse is STRICT, and this test is the inversion of
    /// what it used to assert.
    ///
    /// It previously fed `"activating venv…\n" + json` and demanded
    /// `determined == true` — i.e. it ENCODED the salvage (`stdout.find('{')`)
    /// as the contract. That is the same failure mode as a test encoding a
    /// data loss: it would have kept the census silently green through exactly
    /// the field bug this release fixed (a library relaying a child's captured
    /// stdout onto a JSON contract).
    ///
    /// Now: pollution is UNDETERMINED — never a confident population — and the
    /// error names the offending line so the next such emitter is findable
    /// from the message alone rather than by bisecting a 16k-line engine.
    ///
    /// Mutation check: restore `let Some(start) = stdout.find('{')` +
    /// `from_str(&stdout[start..])` and this test fails (it comes back
    /// `determined`).
    #[test]
    fn polluted_stdout_is_undetermined_and_names_the_polluting_line() {
        let raw = three_verdict_json();
        let c = census_from_stdout(&format!(
            "some-lib: relayed a child's progress here\n{}\n",
            raw
        ));
        assert!(
            !c.determined,
            "a polluted stdout must never yield a confident census"
        );
        assert!(c.summary.is_none(), "no summary may be claimed from a bad probe");
        let err = c.error.expect("undetermined census carries a reason");
        assert!(
            err.contains("some-lib: relayed a child's progress here"),
            "the message must NAME the polluting line; got: {}",
            err
        );
        assert!(
            err.contains("vco_lib.bundle_staleness"),
            "the message must name the command that was run; got: {}",
            err
        );
    }

    /// The empty-stdout case must read differently from a blank first line —
    /// a child that printed NOTHING is a different fault (died before its
    /// first write) from one that printed junk.
    #[test]
    fn empty_stdout_says_so_rather_than_showing_a_blank_line() {
        let c = census_from_stdout("");
        assert!(!c.determined);
        let err = c.error.expect("undetermined census carries a reason");
        assert!(
            err.contains("(stdout was empty)"),
            "empty stdout must be named as such; got: {}",
            err
        );
    }

    #[test]
    fn unrecognised_verdict_fails_safe_to_unknown() {
        let raw = three_verdict_json().replace("\"current\"", "\"probably-fine\"");
        let c = census_from_stdout(&raw);
        assert!(c.determined);
        // Demoted, not silently trusted; and the recomputed tally follows the
        // rows so the badge cannot disagree with the list.
        assert_eq!(c.projects[0].verdict, "unknown");
        assert_eq!(c.summary.unwrap().current, 0);
    }

    // ─── "could not determine" ≠ "all current" ──────────────────────────

    #[test]
    fn unparseable_output_is_undetermined_not_empty() {
        let c = census_from_stdout("{ this is not json");
        assert!(!c.determined);
        assert!(c.error.is_some());
        // The critical assertion: NOT a zeroed summary. `None` means the
        // caller cannot read a count out of a failed probe.
        assert!(c.summary.is_none());
        assert!(c.projects.is_empty());
    }

    #[test]
    fn output_with_no_json_at_all_is_undetermined() {
        let c = census_from_stdout("Traceback (most recent call last):\n  ImportError\n");
        assert!(!c.determined);
        assert!(c.summary.is_none());
    }

    #[test]
    fn unreadable_registry_is_undetermined_not_zero_stale() {
        // Python emits `registry: "unavailable"` with an all-zero summary for
        // a root install before the first launcher boot. Echoing that as a
        // determined "0 stale" is the exact five-week lie.
        let raw = serde_json::json!({
            "schema": 1,
            "running": {"version": "0.2.92"},
            "registry": "unavailable",
            "projects": [],
            "summary": {"current": 0, "stale": 0, "unknown": 0},
            "remedy": {"gui": "g", "cli": "c"}
        })
        .to_string();
        let c = census_from_stdout(&raw);
        assert!(!c.determined);
        assert!(c.summary.is_none());
        assert!(c.error.unwrap().contains("registry"));
    }

    #[test]
    fn newer_schema_is_undetermined_rather_than_guessed() {
        let raw = three_verdict_json().replace("\"schema\":1", "\"schema\":2");
        let c = census_from_stdout(&raw);
        assert!(!c.determined);
        assert!(c.summary.is_none());
    }

    #[test]
    fn missing_summary_block_is_undetermined() {
        let v: serde_json::Value = serde_json::from_str(&three_verdict_json()).unwrap();
        let mut obj = v.as_object().unwrap().clone();
        obj.remove("summary");
        let c = census_from_stdout(&serde_json::Value::Object(obj).to_string());
        assert!(!c.determined);
        assert!(c.summary.is_none());
    }

    #[test]
    fn empty_but_determined_population_is_distinguishable_from_a_failed_probe() {
        let raw = serde_json::json!({
            "schema": 1,
            "running": {"version": "0.2.92"},
            "registry": "launcher.db",
            "projects": [],
            "summary": {"current": 0, "stale": 0, "unknown": 0},
            "remedy": {"gui": "g", "cli": "c"}
        })
        .to_string();
        let ok = census_from_stdout(&raw);
        let failed = BundleStalenessCensus::undetermined("boom");
        assert!(ok.determined);
        assert_eq!(ok.summary, Some(BundleStalenessSummary::default()));
        assert!(!failed.determined);
        assert!(failed.summary.is_none());
        // Same empty `projects` — the ONLY thing separating them is the
        // determined flag + summary presence. That separation is the feature.
        assert_eq!(ok.projects, failed.projects);
        assert_ne!(ok.summary, failed.summary);
    }

    #[test]
    fn undetermined_carries_the_reason_to_the_caller() {
        let c = BundleStalenessCensus::undetermined("no python");
        assert_eq!(c.error.as_deref(), Some("no python"));
        assert!(c.remedy_gui.is_none());
    }

    /// The count the GUI shows after an orchestrator update is
    /// stale + unknown, and it must equal the rows it renders.
    #[test]
    fn attention_count_matches_the_rows() {
        let c = census_from_stdout(&three_verdict_json());
        let s = c.summary.clone().unwrap();
        // Per-verdict equality, not just the sum: an aggregate check passes
        // even when two buckets have been swapped.
        for (verdict, count) in [
            ("current", s.current),
            ("stale", s.stale),
            ("unknown", s.unknown),
        ] {
            let rows = c.projects.iter().filter(|p| p.verdict == verdict).count() as u32;
            assert_eq!(rows, count, "{} rows vs summary", verdict);
        }
        assert_eq!(s.stale + s.unknown, 2);
    }

    /// Guard the read-only contract at the argv level: the census command
    /// must never grow `--refresh-ledger`. A GUI poll that writes the ledger
    /// would flap the user's deferral badge on every page mount.
    #[test]
    fn census_argv_is_read_only() {
        let src = include_str!("bundle_staleness.rs");
        // Needles are ASSEMBLED at runtime, and no comment in this file may
        // spell the forbidden call out verbatim: a literal occurrence anywhere
        // in the source would match itself and make the guard self-defeating.
        let ledger_arg = format!(".arg(\"--{}\")", "refresh-ledger");
        let json_arg = format!(".arg(\"--{}\")", "json");
        assert!(
            !src.contains(&ledger_arg),
            "the GUI census must not opt into the ledger write"
        );
        assert!(src.contains(&json_arg), "the census must run in --json mode");
    }
}
