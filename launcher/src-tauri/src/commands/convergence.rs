// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.91 WP-E item 2 — the CONVERGENCE ENGINE (skeleton + first write
//! tenant).
//!
//! ## Why an engine instead of another `*_reconcile.rs`
//!
//! Every VCO generation so far grew its own bespoke one-off reconciler for
//! "the shipped defaults moved; existing installs still carry the old shape":
//! `project_backfill` (v0.2.21), the migration-010 MCP backfill
//! (`lib.rs`, v0.2.x), `binding_reconcile` (v0.2.89), the codegraph registry
//! reconcile. Each one re-derived the same two invariants from scratch, and
//! each one got them slightly differently right. The migration-010 backfill
//! is the cautionary case: it ran on every boot for a year and converged
//! NOTHING, because it was double-gated into uselessness (only projects with
//! zero rows, and its only action was to re-run a pure disk mirror that had
//! nothing left to mirror once bundled MCPs moved to the GLOBAL
//! `~/.claude.json`). Nobody noticed, because a bespoke reconciler has no
//! shared contract to violate.
//!
//! This module is that shared contract. It owns:
//!   * a DECLARATIVE table of tenants ([`CONVERGENCE_TENANTS`]) — "what
//!     current defaults exist, and who may write them";
//!   * the two hard invariants below, applied identically to every tenant;
//!   * one soft-fail boot pass with audit rows and one honest deferral
//!     channel.
//!
//! ## The two hard invariants
//!
//! 1. **PROVENANCE WINS.** A row whose provenance is the user
//!    (`is_user_added = 1`, or `source = "user"`) is NEVER touched — not
//!    seeded over, not retired, not re-enabled. Neither is an explicit
//!    disable: the engine never flips `enabled` back on. Concretely, seeding
//!    is INSERT-IF-ABSENT (existing rows are not even UPSERTed, so their
//!    `updated_at` does not move), which is strictly stronger than relying on
//!    the enabled-preserving `DO UPDATE`.
//! 2. **POSITIVE EVIDENCE ONLY — probe failure is never absence.** If a read
//!    that would tell the engine what a project HAS fails, the engine does
//!    nothing for that project. A failed `list_project_mcp_servers` is not
//!    evidence that the project has no rows; treating it as such would seed
//!    duplicates over a live catalog. This is `binding_reconcile`'s §5.4
//!    lesson, lifted verbatim.
//!
//! Two consequences follow and are tested as leave-alone cases: the engine is
//! IDEMPOTENT (a second pass decides `Leave` everywhere) and it is
//! NON-DESTRUCTIVE (retirement disables + badges; it never DELETEs — deletion
//! stays behind install.py's consent-gated `--remove-deprecated-mcps`, per
//! CLAUDE.md's no-auto-destroy rule).
//!
//! ## Tenant states (v0.2.91, decision #8)
//!
//! * [`TenantRun::Write`] — the engine converges this tenant's rows now.
//!   `project_mcp_servers` is the FIRST and, this cycle, ONLY write tenant.
//! * [`TenantRun::ReportOnly`] — the tenant still runs its own bespoke
//!   reconciler; the engine only reports drift it can positively detect, and
//!   write-enables in v0.2.92. A report-only tenant with no detector written
//!   yet declares `None` and contributes NOTHING — an honest silence rather
//!   than a fabricated "pending" record that would silt the ledger (report 2's
//!   central finding).
//!
//! ## Deferral channel
//!
//! One registered condition, `convergence_pending` (see
//! `vco_lib/deferral_conditions.toml`). It is emitted ONLY when a pass ends
//! with real pending work — a write tenant that could not complete its
//! writes, or a report-only tenant whose detector found drift. A clean pass
//! RESOLVES it (paired-resolution), and the resolve call is itself gated on
//! the entry actually being present on disk so healthy installs never spawn
//! the Python settle helper.
//!
//! Called once per boot from `lib.rs::setup()` inside
//! `tauri::async_runtime::spawn` (never `tokio::spawn` — a bare tokio spawn
//! from a sync `setup()` has no reactor; the v0.2.90 boot-death class).

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::Value as JsonValue;

use crate::db::project_mcp_servers::{
    is_default_disabled_mcp, ProjectMcpServer, MCP_RETIRED_CONFIG_KEY,
};
use crate::db::Db;
use vct_launcher_core::mcp_scan_rules::{self, DeprecatedMcp};

/// The registered deferral condition this engine emits. Registered in
/// `vco_lib/deferral_conditions.toml`; the completeness gate hard-fails an
/// unregistered cid.
pub(crate) const CID_CONVERGENCE_PENDING: &str = "convergence_pending";

/// `source_file` stamped on rows the engine seeds. Distinguishes them from
/// populate's disk-mirror rows (`.claude/settings.json` / `.mcp.json`) and
/// from the registration DB-sync's (`install.py:_register_mcps`) in the audit
/// trail and in the Custom-MCP tab's provenance column.
pub(crate) const SEED_SOURCE_FILE: &str = "convergence:mcp_rows";

// ═══════════════════════════════════════════════════════════════════════
// Declarative tenant table
// ═══════════════════════════════════════════════════════════════════════

/// A read-only drift detector for a report-only tenant. Returns one
/// human-readable line per detected drift. MUST NOT write.
pub(crate) type TenantDetector = fn(&Db) -> Result<Vec<String>, String>;

/// How the engine treats a tenant this cycle.
pub(crate) enum TenantRun {
    /// Write-enabled: the closure converges the tenant's rows, appending its
    /// own audit rows, and returns any items it could NOT converge.
    Write(fn(&Db) -> TenantOutcome),
    /// Report-only: read-only detection, no writes. `None` = no detector has
    /// been written yet (the tenant is listed so the migration debt is
    /// visible, not so it can invent findings).
    ReportOnly(Option<TenantDetector>),
}

/// One row of the current-defaults table.
pub(crate) struct TenantSpec {
    /// Stable id — appears in audit rows and in the deferral body.
    pub id: &'static str,
    /// One line describing what converging this tenant means.
    pub what: &'static str,
    pub run: TenantRun,
}

/// The table. Adding a tenant is a table edit plus its `run` implementation —
/// no new boot hook, no new reconciler module, no re-derived invariants.
pub(crate) const CONVERGENCE_TENANTS: &[TenantSpec] = &[
    TenantSpec {
        id: "project_mcp_servers",
        what: "seed the current default MCP set into every project's rows and \
               retire rows for MCPs that left the default set",
        run: TenantRun::Write(converge_mcp_rows),
    },
    TenantSpec {
        id: "project_backfill",
        what: "KG / codegraph binding rows + module_settings seeds",
        // Still converged by `project_backfill::backfill_all_projects`, which
        // runs its own boot pass. Migrating it here is v0.2.92 (decision #8);
        // until then there is nothing this engine can positively detect that
        // that pass has not already fixed, so: no detector, no findings.
        run: TenantRun::ReportOnly(None),
    },
    TenantSpec {
        id: "codegraph_bindings",
        what: "half-renamed codegraph binding prefixes + phantom KG access rows",
        // Owned by `binding_reconcile`, which is evidence-gated on a live
        // Weaviate schema probe. Folding that probe into the engine is
        // v0.2.92; duplicating it here would double the boot-time HTTP work.
        run: TenantRun::ReportOnly(None),
    },
    TenantSpec {
        id: "module_settings",
        what: "per-module setting rows whose shipped defaults have moved",
        run: TenantRun::ReportOnly(None),
    },
];

// ═══════════════════════════════════════════════════════════════════════
// Reports
// ═══════════════════════════════════════════════════════════════════════

/// What one tenant did on one pass.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct TenantOutcome {
    /// Rows created because a current default was missing.
    pub seeded: usize,
    /// Rows disabled + badged because the MCP left the default set.
    pub retired: usize,
    /// Projects skipped because a READ failed (probe failure ≠ absence).
    pub skipped_unreadable: usize,
    /// Human-readable pending items — the deferral body. Non-empty means the
    /// tenant did NOT fully converge.
    pub pending: Vec<String>,
}

/// Whole-pass summary for the boot log line.
#[derive(Debug, Default)]
pub(crate) struct ConvergenceReport {
    pub seeded: usize,
    pub retired: usize,
    pub skipped_unreadable: usize,
    pub pending: Vec<String>,
    /// Tenants listed as report-only with no detector yet (migration debt,
    /// surfaced in the audit row only — never in the user's ledger).
    pub report_only_without_detector: Vec<&'static str>,
}

impl ConvergenceReport {
    fn absorb(&mut self, tenant: &str, outcome: TenantOutcome) {
        self.seeded += outcome.seeded;
        self.retired += outcome.retired;
        self.skipped_unreadable += outcome.skipped_unreadable;
        for line in outcome.pending {
            self.pending.push(format!("[{}] {}", tenant, line));
        }
    }

    /// True when the pass changed nothing and has nothing outstanding.
    pub fn is_quiet(&self) -> bool {
        self.seeded == 0
            && self.retired == 0
            && self.skipped_unreadable == 0
            && self.pending.is_empty()
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tenant 1 (WRITE): project_mcp_servers
// ═══════════════════════════════════════════════════════════════════════

/// Why a row was left alone. Every variant is a tested leave-alone case.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LeaveReason {
    /// `is_user_added = 1` or `source = "user"` — invariant 1.
    UserProvenance,
    /// The project's OWN mcp config authors this name right now, so the row
    /// mirrors a user-authored entry regardless of what its `is_user_added`
    /// column says — invariant 1, via [`ProjectAuthoredMcps`].
    UserAuthoredOnDisk,
    /// The project's mcp config files could not be read, so provenance is
    /// unknown — invariant 2. Retirement waits for evidence.
    ProvenanceUnknown,
    /// Already carries the retirement badge for this `removed_in` — the
    /// retire pass acts exactly once, so a later user re-enable stands.
    AlreadyRetired,
    /// A current, non-deprecated bundled row. Its `enabled` state — including
    /// an explicit user disable — is none of the engine's business.
    Current,
}

/// The MCP names a project's OWN config files author right now.
///
/// v0.2.91 wave-3 (MINOR-6). `populate_mcp_servers` classifies provenance as
/// `is_user_added = !is_bundled_mcp(name)` — a NAME test, not a provenance
/// test. A user who hand-writes an `ollama` entry into their own
/// `.claude/settings.json` therefore gets `is_user_added = 0`,
/// `source = "bundled"`, and invariant 1 does not fire for their row: the
/// engine retires the USER'S MCP.
///
/// The positive evidence that fixes it is on disk, in the two files populate
/// mirrors. If the deprecated name is in them, a human put it there and the
/// row is theirs — hands off. (It is also the only way to break the composed
/// loop: populate re-UPSERTs from those files on every bundle update, so
/// retiring a row the disk keeps re-asserting is a fight the engine cannot
/// win and should not pick.) If the name is absent, the row is a leftover from
/// a registration VCO performed — the case the tenant exists for.
///
/// `conclusive` is the invariant-2 half: a file that EXISTS but cannot be read
/// or parsed means "cannot tell", and the engine retires nothing for that
/// project. A file that does not exist is genuine evidence — it authors
/// nothing.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct ProjectAuthoredMcps {
    names: std::collections::BTreeSet<String>,
    conclusive: bool,
}

impl ProjectAuthoredMcps {
    /// Read `.claude/settings.json` + `.mcp.json` — the exact two files, in
    /// the exact order, that `project_state_populate::populate_mcp_servers`
    /// mirrors into these rows.
    pub(crate) fn read(folder: &Path) -> Self {
        let mut out = ProjectAuthoredMcps {
            names: Default::default(),
            conclusive: true,
        };
        for rel in [
            folder.join(".claude").join("settings.json"),
            folder.join(".mcp.json"),
        ] {
            if !rel.is_file() {
                continue; // absent ⇒ authors nothing. That IS evidence.
            }
            let Ok(raw) = std::fs::read_to_string(&rel) else {
                out.conclusive = false;
                continue;
            };
            let Ok(parsed) = serde_json::from_str::<JsonValue>(&raw) else {
                out.conclusive = false;
                continue;
            };
            if let Some(obj) = parsed.get("mcpServers").and_then(|v| v.as_object()) {
                out.names.extend(obj.keys().cloned());
            }
        }
        out
    }

    /// Test seam: a conclusive read that found exactly these names.
    #[cfg(test)]
    pub(crate) fn authoring(names: &[&str]) -> Self {
        ProjectAuthoredMcps {
            names: names.iter().map(|s| (*s).to_string()).collect(),
            conclusive: true,
        }
    }

    /// Test seam: the "could not tell" shape.
    #[cfg(test)]
    pub(crate) fn unreadable() -> Self {
        ProjectAuthoredMcps {
            names: Default::default(),
            conclusive: false,
        }
    }

    fn authors(&self, name: &str) -> bool {
        self.names.contains(name)
    }
}

/// The decision for one MCP row / one missing default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum McpRowDecision {
    Seed {
        name: String,
        default_disabled: bool,
    },
    Retire {
        name: String,
        removed_in: String,
        reason: String,
    },
    Leave {
        name: String,
        why: LeaveReason,
    },
}

/// True iff `config` already carries a retirement badge stamped with
/// `removed_in`. Drives the "exactly once" property.
pub(crate) fn has_retired_badge(config: &JsonValue, removed_in: &str) -> bool {
    config
        .get(MCP_RETIRED_CONFIG_KEY)
        .and_then(|b| b.get("removed_in"))
        .and_then(|v| v.as_str())
        == Some(removed_in)
}

/// Compose the badge stored on a retired row. `badge` is the ready-to-render
/// GUI string; the structured fields let a future surface format it itself.
pub(crate) fn retired_badge(removed_in: &str, reason: &str) -> JsonValue {
    serde_json::json!({
        "removed_in": removed_in,
        "reason": reason,
        "badge": format!("retired in {}: {}", removed_in, reason),
    })
}

/// The whole MCP-rows policy as a PURE function — every gate unit-testable
/// without a DB.
///
/// * `existing` — the project's current rows. The caller must NOT call this
///   at all when the read failed (invariant 2).
/// * `default_names` — `[entries].default_names` from the shared table.
/// * `deprecated` — `[deprecated.*]` from the same table.
/// * `authored` — what the project's OWN mcp config files say (see
///   [`ProjectAuthoredMcps`]). Passed in rather than read here so this stays a
///   pure function of facts the caller gathered.
///
/// Output order: seeds in `default_names` order, then one decision per
/// existing row in the order given. Deterministic, so audit rows are stable.
pub(crate) fn decide_mcp_rows(
    existing: &[ProjectMcpServer],
    default_names: &[String],
    deprecated: &BTreeMap<String, DeprecatedMcp>,
    authored: &ProjectAuthoredMcps,
) -> Vec<McpRowDecision> {
    let mut out = Vec::new();

    for name in default_names {
        if existing.iter().any(|r| &r.mcp_name == name) {
            continue;
        }
        // Belt-and-braces: a name that is BOTH a current default and declared
        // deprecated is a table bug (pinned by a unit test in
        // mcp_scan_rules.rs). Never seed a row we would retire on the same
        // pass — leave it out and let the table fix itself.
        if deprecated.contains_key(name) {
            continue;
        }
        out.push(McpRowDecision::Seed {
            name: name.clone(),
            default_disabled: is_default_disabled_mcp(name),
        });
    }

    for row in existing {
        // Invariant 1 — provenance wins, checked FIRST so it can never be
        // overridden by a later rule. A user-added MCP that happens to be
        // named like a bundled one (a hand-rolled `ollama` entry) is the
        // exact case that must survive the retire pass untouched.
        if row.is_user_added || row.source == "user" {
            out.push(McpRowDecision::Leave {
                name: row.mcp_name.clone(),
                why: LeaveReason::UserProvenance,
            });
            continue;
        }
        match deprecated.get(&row.mcp_name) {
            Some(dep) if has_retired_badge(&row.config, &dep.removed_in) => {
                out.push(McpRowDecision::Leave {
                    name: row.mcp_name.clone(),
                    why: LeaveReason::AlreadyRetired,
                });
            }
            // Invariant 1, second half: the `is_user_added` column is a NAME
            // test upstream, so it cannot be the only provenance evidence.
            Some(_) if authored.authors(&row.mcp_name) => {
                out.push(McpRowDecision::Leave {
                    name: row.mcp_name.clone(),
                    why: LeaveReason::UserAuthoredOnDisk,
                });
            }
            // Invariant 2: no readable provenance ⇒ no retirement.
            Some(_) if !authored.conclusive => {
                out.push(McpRowDecision::Leave {
                    name: row.mcp_name.clone(),
                    why: LeaveReason::ProvenanceUnknown,
                });
            }
            Some(dep) => out.push(McpRowDecision::Retire {
                name: row.mcp_name.clone(),
                removed_in: dep.removed_in.clone(),
                reason: dep.reason.clone(),
            }),
            None => out.push(McpRowDecision::Leave {
                name: row.mcp_name.clone(),
                why: LeaveReason::Current,
            }),
        }
    }
    out
}

/// Apply [`decide_mcp_rows`] across every registered project.
///
/// Soft-fail per project AND per row: one bad project can never stop the
/// sweep, and nothing here can block boot.
pub(crate) fn converge_mcp_rows(db: &Db) -> TenantOutcome {
    let mut outcome = TenantOutcome::default();

    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            // Invariant 2 at the top level: no project list, no evidence,
            // no writes.
            outcome
                .pending
                .push(format!("could not list projects ({}); nothing converged", e));
            return outcome;
        }
    };

    for project in &projects {
        converge_mcp_rows_for_project(
            db,
            &project.id,
            &project.name,
            Path::new(&project.folder_path),
            &mut outcome,
        );
    }
    outcome
}

/// Converge ONE project's MCP rows, appending to `outcome`.
///
/// The per-project unit both entry points share: the boot sweep loops it, and
/// the post-bundle re-walk calls it for the single project it just updated
/// (`projects_v2::apply_post_bundle_steps`). A bundle update is exactly when
/// the shipped default set may have moved under a project, so converging
/// there closes the window between the update and the user's next launcher
/// restart.
pub(crate) fn converge_mcp_rows_for_project(
    db: &Db,
    project_id: &str,
    project_name: &str,
    folder: &Path,
    outcome: &mut TenantOutcome,
) {
    let default_names = mcp_scan_rules::default_mcp_entry_names();
    let deprecated = mcp_scan_rules::deprecated_default_mcps();
    // Provenance evidence for the retire half (v0.2.91 wave-3, MINOR-6).
    let authored = ProjectAuthoredMcps::read(folder);

    let existing = match db.list_project_mcp_servers(project_id) {
        Ok(rows) => rows,
        Err(e) => {
            // Invariant 2 — a failed read is NOT "this project has no rows".
            // Seeding here would duplicate a live catalog.
            tracing::warn!(
                "[vct] convergence: mcp rows unreadable for {:?} ({}); \
                 skipping — a failed read is not evidence of absence",
                project_name, e
            );
            outcome.skipped_unreadable += 1;
            outcome.pending.push(format!(
                "project {:?}: MCP rows could not be read ({}), so the current \
                 default set was not applied",
                project_name, e
            ));
            return;
        }
    };

    for decision in decide_mcp_rows(&existing, default_names, deprecated, &authored) {
        match decision {
            McpRowDecision::Leave { .. } => {}
            McpRowDecision::Seed {
                name,
                default_disabled,
            } => {
                // The shared helper is the ONE home for the fresh-insert
                // default-disabled rule (WP-E item 3) — the engine does not
                // re-implement it.
                match db.register_project_mcp_server_honoring_defaults(
                    project_id,
                    &name,
                    false,
                    "bundled",
                    None,
                    Some(SEED_SOURCE_FILE),
                    None,
                    &JsonValue::Object(serde_json::Map::new()),
                ) {
                    Ok(seeded) => {
                        outcome.seeded += 1;
                        db.audit(
                            "convergence_mcp_row_seeded",
                            Some(project_id),
                            None,
                            &serde_json::json!({
                                "mcp": name,
                                "default_disabled": default_disabled,
                                "enabled_applied": !seeded.default_disabled_applied,
                                "reason": "current default set missing from this project's rows",
                            }),
                        )
                        .ok();
                    }
                    Err(e) => {
                        tracing::warn!(
                            "[vct] convergence: seed {}/{} failed: {}",
                            project_id, name, e
                        );
                        outcome.pending.push(format!(
                            "project {:?}: could not seed MCP row {:?} ({})",
                            project_name, name, e
                        ));
                    }
                }
            }
            McpRowDecision::Retire {
                name,
                removed_in,
                reason,
            } => {
                let badge = retired_badge(&removed_in, &reason);
                match db.retire_project_mcp_server(project_id, &name, &badge) {
                    // `false` = the row vanished between read and write.
                    // Nothing to do, nothing wrong.
                    Ok(false) => {}
                    Ok(true) => {
                        outcome.retired += 1;
                        db.audit(
                            "convergence_mcp_row_retired",
                            Some(project_id),
                            None,
                            &serde_json::json!({
                                "mcp": name,
                                "removed_in": removed_in,
                                "reason": reason,
                                "action": "disabled + badged; row PRESERVED \
                                           (deletion stays behind \
                                           --remove-deprecated-mcps)",
                            }),
                        )
                        .ok();
                        tracing::warn!(
                            "[vct] convergence: retired MCP row {:?} for {:?} \
                             (left the default set in {})",
                            name, project_name, removed_in
                        );
                    }
                    Err(e) => {
                        tracing::warn!(
                            "[vct] convergence: retire {}/{} failed: {}",
                            project_id, name, e
                        );
                        outcome.pending.push(format!(
                            "project {:?}: could not retire deprecated MCP row \
                             {:?} ({})",
                            project_name, name, e
                        ));
                    }
                }
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Boot pass
// ═══════════════════════════════════════════════════════════════════════

/// Run every tenant once. Pure of Tauri types so it is directly testable.
///
/// `repo_root` is the orchestrator clone root used as BOTH the `sys.path`
/// root and the deferral report folder (convergence is an orchestrator-wide
/// condition, not a per-project one). `None` → the deferral channel is
/// skipped entirely; the audit rows and the log line still happen.
pub(crate) fn converge_at_boot(db: &Db, repo_root: Option<&Path>) -> ConvergenceReport {
    let mut report = ConvergenceReport::default();

    for tenant in CONVERGENCE_TENANTS {
        match &tenant.run {
            TenantRun::Write(run) => {
                report.absorb(tenant.id, run(db));
            }
            TenantRun::ReportOnly(None) => {
                report.report_only_without_detector.push(tenant.id);
            }
            TenantRun::ReportOnly(Some(detect)) => {
                let mut outcome = TenantOutcome::default();
                match detect(db) {
                    Ok(findings) => outcome.pending = findings,
                    Err(e) => {
                        // Invariant 2: a detector that could not read reports
                        // nothing. It does not report "no drift" either.
                        tracing::warn!(
                            "[vct] convergence: detector for tenant {} failed \
                             ({}); reporting nothing this pass",
                            tenant.id, e
                        );
                    }
                }
                report.absorb(tenant.id, outcome);
            }
        }
    }

    // The audit row carries the TABLE itself, not just the counters: a human
    // reading `audit_log` months later can see which tenants were
    // write-enabled at that point in time and what each one claimed to own,
    // without cross-referencing the binary's source.
    //
    // Written only when the pass DID something (v0.2.91 wave-3 NIT). The
    // steady state is a quiet pass on every boot, forever; a ~1KB row per boot
    // recording "nothing happened, and here is the static tenant table again"
    // is exactly the kind of log that makes the interesting rows unfindable.
    // The tenant table is a property of the BINARY, so the last non-quiet row
    // already documents it for every boot since.
    if !report.is_quiet() {
        let tenants: Vec<serde_json::Value> = CONVERGENCE_TENANTS
            .iter()
            .map(|t| {
                serde_json::json!({
                    "id": t.id,
                    "state": match t.run {
                        TenantRun::Write(_) => "write",
                        TenantRun::ReportOnly(Some(_)) => "report_only",
                        TenantRun::ReportOnly(None) => "report_only_no_detector",
                    },
                    "what": t.what,
                })
            })
            .collect();
        db.audit(
            "convergence_pass",
            None,
            None,
            &serde_json::json!({
                "seeded": report.seeded,
                "retired": report.retired,
                "skipped_unreadable": report.skipped_unreadable,
                "pending": report.pending,
                "report_only_without_detector": report.report_only_without_detector,
                "tenants": tenants,
            }),
        )
        .ok();
    }

    match repo_root {
        Some(root) => settle_deferral(root, &report),
        None if !report.pending.is_empty() => {
            // Honest about the dropped channel rather than silent: a
            // standalone-binary install has no clone to write the ledger
            // into. The audit row above still carries every pending item.
            tracing::warn!(
                "[vct] convergence: {} pending item(s) but no orchestrator clone \
                 was located, so no deferral was written (see the \
                 convergence_pass audit row)",
                report.pending.len()
            );
        }
        None => {}
    }
    report
}

/// Post-bundle-update convergence for ONE project.
///
/// A bundle update is the other moment the shipped default set can move under
/// a project, so `projects_v2::apply_post_bundle_steps` runs the MCP-rows
/// tenant for the project it just updated instead of waiting for the user's
/// next launcher restart. Same invariants, same idempotence.
///
/// No deferral is emitted here: `convergence_pending` is an orchestrator-wide
/// condition owned by the boot pass. Anything this call could not do comes
/// back as warning strings, which the caller surfaces in the update toast.
pub(crate) fn converge_project_after_bundle_update(
    db: &Db,
    project_id: &str,
    project_name: &str,
    folder: &Path,
) -> Vec<String> {
    let mut outcome = TenantOutcome::default();
    converge_mcp_rows_for_project(db, project_id, project_name, folder, &mut outcome);
    if outcome.seeded > 0 || outcome.retired > 0 {
        tracing::info!(
            "[vct] convergence (post-bundle, {}): seeded {} MCP row(s), retired {}",
            project_id, outcome.seeded, outcome.retired
        );
    }
    outcome
        .pending
        .into_iter()
        .map(|line| format!("convergence (mcp rows): {}", line))
        .collect()
}

/// Emit or resolve `convergence_pending` for this pass.
///
/// Emission is gated on REAL pending work. Resolution is gated on the entry
/// actually being on disk, so a healthy install never spawns the Python
/// settle helper.
fn settle_deferral(root: &Path, report: &ConvergenceReport) {
    if !report.pending.is_empty() {
        let detected = format!(
            "The launcher's convergence pass could not bring {} item(s) to the \
             current shipped defaults:\n{}",
            report.pending.len(),
            report
                .pending
                .iter()
                .map(|l| format!("  - {}", l))
                .collect::<Vec<_>>()
                .join("\n"),
        );
        let fields = crate::services::deferral::DeferralEntryFields {
            condition_id: CID_CONVERGENCE_PENDING,
            title: "Launcher could not converge some project rows to the current defaults",
            detected: &detected,
            why_deferred:
                "The convergence pass is fully soft-fail: it never blocks boot and \
                 never guesses. Every item above is one the launcher declined to \
                 write because a read failed or a write errored — not because the \
                 default was wrong.",
            command_to_apply:
                "# Restart the launcher: the pass re-runs at every boot and clears \
                 this entry\n# once it completes cleanly. If it persists, the \
                 launcher DB is likely locked or\n# read-only — check \
                 ~/.vct/launcher.db permissions.",
            severity: "warning",
        };
        if let Err(e) = crate::services::deferral::emit_deferral_entry(root, root, &fields) {
            tracing::warn!(
                "[vct] convergence: pending-deferral emit failed (non-fatal): {}",
                e
            );
        }
        return;
    }

    // Clean pass — settle a previously-emitted entry (paired-resolution).
    if !deferral_entry_present(root, CID_CONVERGENCE_PENDING) {
        return;
    }
    if let Err(e) = crate::services::deferral::resolve_deferral_conditions(
        root,
        root,
        &[CID_CONVERGENCE_PENDING],
    ) {
        tracing::warn!(
            "[vct] convergence: settling stale convergence deferral failed \
             (non-fatal): {}",
            e
        );
    }
}

/// Cheap on-disk check for one condition id in the ledger.
///
/// Deliberately conservative: an unreadable / absent report means "not
/// present", so the worst case is skipping a settle that the next clean boot
/// retries — never a spurious Python spawn on every boot of a healthy install.
fn deferral_entry_present(root: &Path, condition_id: &str) -> bool {
    let md = root
        .join(".claude")
        .join("context")
        .join("UPDATE_DEFERRED.md");
    match std::fs::read_to_string(&md) {
        Ok(text) => text.contains(&format!("## {} (", condition_id)),
        Err(_) => false,
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn row(name: &str, is_user_added: bool, source: &str, config: JsonValue) -> ProjectMcpServer {
        ProjectMcpServer {
            project_id: "p1".into(),
            mcp_name: name.into(),
            is_user_added,
            source: source.into(),
            source_module: None,
            source_file: None,
            enabled: true,
            command: None,
            config,
            installed_at: 0,
            updated_at: 0,
        }
    }

    fn defaults() -> Vec<String> {
        mcp_scan_rules::default_mcp_entry_names().to_vec()
    }

    fn deprecated() -> &'static BTreeMap<String, DeprecatedMcp> {
        mcp_scan_rules::deprecated_default_mcps()
    }

    /// A CONCLUSIVE read of a project whose own mcp config authors nothing —
    /// the shape of every modern install (bundled MCPs live in the GLOBAL
    /// ~/.claude.json, so the per-project files carry no `mcpServers`).
    fn nobody() -> ProjectAuthoredMcps {
        ProjectAuthoredMcps::authoring(&[])
    }

    fn db_with_project(id: &str, name: &str, folder: &Path) -> Db {
        let db = Db::open_in_memory().unwrap();
        let slug = db.generate_unique_slug(name).unwrap();
        db.insert_project(
            id,
            name,
            &folder.to_string_lossy(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();
        db
    }

    // ── pure-decision matrix ──────────────────────────────────────────

    /// (d) A project with ZERO rows gains the whole current default set —
    /// the field state of three zero-row field projects, which the
    /// double-gated migration-010 backfill left empty forever.
    #[test]
    fn case_d_zero_row_project_gains_the_default_set() {
        let out = decide_mcp_rows(&[], &defaults(), deprecated(), &nobody());
        let seeded: Vec<&str> = out
            .iter()
            .filter_map(|d| match d {
                McpRowDecision::Seed { name, .. } => Some(name.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(
            seeded,
            defaults().iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            "every current default must be seeded, in table order"
        );
        // …and the default-disabled ones are flagged as such.
        for d in &out {
            if let McpRowDecision::Seed {
                name,
                default_disabled,
            } = d
            {
                assert_eq!(
                    *default_disabled,
                    is_default_disabled_mcp(name),
                    "{} default_disabled flag must come from the shared table",
                    name
                );
            }
        }
    }

    /// (a) LEAVE-ALONE: a bundled row the user DISABLED survives untouched —
    /// the engine never re-enables, and never even UPSERTs an existing row.
    #[test]
    fn case_a_user_disabled_bundled_row_is_left_alone() {
        let mut r = row("weaviate-kg", false, "bundled", serde_json::json!({}));
        r.enabled = false;
        let existing = vec![r];
        let out = decide_mcp_rows(&existing, &defaults(), deprecated(), &nobody());
        assert!(
            out.contains(&McpRowDecision::Leave {
                name: "weaviate-kg".into(),
                why: LeaveReason::Current,
            }),
            "a disabled current bundled row must be Leave(Current): {:?}",
            out
        );
        assert!(
            !out.iter().any(|d| matches!(
                d,
                McpRowDecision::Seed { name, .. } if name == "weaviate-kg"
            )),
            "an existing row must never be re-seeded"
        );
    }

    /// (b) LEAVE-ALONE: a USER-ADDED row named exactly like a deprecated
    /// bundled MCP (`ollama`) is never retired. Provenance wins, and it is
    /// checked before the deprecated lookup so no later rule can override it.
    #[test]
    fn case_b_user_added_row_named_like_a_bundled_one_is_untouched() {
        let existing = vec![row("ollama", true, "user", serde_json::json!({}))];
        let out = decide_mcp_rows(&existing, &defaults(), deprecated(), &nobody());
        assert!(
            out.contains(&McpRowDecision::Leave {
                name: "ollama".into(),
                why: LeaveReason::UserProvenance,
            }),
            "user-added `ollama` must be Leave(UserProvenance): {:?}",
            out
        );
    }

    /// (b′) The same guard via `source` alone, for a row whose
    /// `is_user_added` discriminator was never set (pre-migration shape).
    #[test]
    fn case_b_source_user_alone_is_enough_to_protect_a_row() {
        let existing = vec![row("ollama", false, "user", serde_json::json!({}))];
        let out = decide_mcp_rows(&existing, &defaults(), deprecated(), &nobody());
        assert!(out.contains(&McpRowDecision::Leave {
            name: "ollama".into(),
            why: LeaveReason::UserProvenance,
        }));
    }

    /// (c) ACT: a BUNDLED deprecated row is retired — once.
    #[test]
    fn case_c_deprecated_bundled_row_is_retired() {
        let existing = vec![row("ollama", false, "bundled", serde_json::json!({}))];
        let out = decide_mcp_rows(&existing, &defaults(), deprecated(), &nobody());
        let dep = &deprecated()["ollama"];
        assert!(
            out.contains(&McpRowDecision::Retire {
                name: "ollama".into(),
                removed_in: dep.removed_in.clone(),
                reason: dep.reason.clone(),
            }),
            "bundled `ollama` must be retired with the table's version+reason: {:?}",
            out
        );
    }

    /// (c) LEAVE-ALONE half: a row already badged for the same `removed_in`
    /// is NOT retired again. This is what makes the pass idempotent, and it
    /// is also what lets a deliberate post-retirement re-enable stand.
    #[test]
    fn case_c_retire_is_exactly_once_and_idempotent() {
        let dep = &deprecated()["ollama"];
        let cfg = serde_json::json!({
            MCP_RETIRED_CONFIG_KEY: retired_badge(&dep.removed_in, &dep.reason),
        });
        let mut r = row("ollama", false, "bundled", cfg);
        r.enabled = true; // user re-enabled it after the retirement
        let out = decide_mcp_rows(&[r], &defaults(), deprecated(), &nobody());
        assert!(
            out.contains(&McpRowDecision::Leave {
                name: "ollama".into(),
                why: LeaveReason::AlreadyRetired,
            }),
            "an already-badged row must be Leave(AlreadyRetired): {:?}",
            out
        );
    }

    /// A badge stamped with a DIFFERENT `removed_in` does not count — if the
    /// table's retirement version changes, the row is re-badged once more.
    #[test]
    fn stale_badge_version_does_not_suppress_retire() {
        let cfg = serde_json::json!({
            MCP_RETIRED_CONFIG_KEY: retired_badge("v0.0.1", "ancient"),
        });
        let out = decide_mcp_rows(
            &[row("ollama", false, "bundled", cfg)],
            &defaults(),
            deprecated(),
            &nobody(),
        );
        assert!(out.iter().any(|d| matches!(
            d,
            McpRowDecision::Retire { name, .. } if name == "ollama"
        )));
    }

    /// (e) LEAVE-ALONE: a project that already holds the full default set
    /// gets NO decisions other than Leave — the orchestrator root's rows are
    /// not re-stamped, not re-sourced, not touched.
    #[test]
    fn case_e_fully_seeded_project_is_left_entirely_alone() {
        let existing: Vec<ProjectMcpServer> = defaults()
            .iter()
            .map(|n| row(n, false, "bundled", serde_json::json!({})))
            .collect();
        let out = decide_mcp_rows(&existing, &defaults(), deprecated(), &nobody());
        assert!(
            out.iter()
                .all(|d| matches!(d, McpRowDecision::Leave { .. })),
            "a fully-seeded project must produce only Leave decisions: {:?}",
            out
        );
        assert_eq!(out.len(), existing.len());
    }

    // ── end-to-end over a fixture DB ──────────────────────────────────

    /// ACT + idempotence, against a real in-memory DB: a zero-row project
    /// gains the default set with the default-disabled rule applied, and a
    /// second pass writes nothing more.
    #[test]
    fn converge_seeds_then_is_a_no_op_on_the_second_pass() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Empty Project", td.path());

        let first = converge_mcp_rows(&db);
        assert_eq!(first.seeded, mcp_scan_rules::default_mcp_entry_names().len());
        assert_eq!(first.retired, 0);
        assert!(first.pending.is_empty());

        let rows = db.list_project_mcp_servers("p1").unwrap();
        let by_name: std::collections::HashMap<&str, &ProjectMcpServer> =
            rows.iter().map(|r| (r.mcp_name.as_str(), r)).collect();
        for name in mcp_scan_rules::default_mcp_entry_names() {
            let r = by_name.get(name.as_str()).expect("seeded row");
            assert!(!r.is_user_added, "seeded rows are bundled provenance");
            assert_eq!(r.source, "bundled");
            assert_eq!(r.source_file.as_deref(), Some(SEED_SOURCE_FILE));
            assert_eq!(
                r.enabled,
                !is_default_disabled_mcp(name),
                "{} must honor BUNDLED_MCP_DEFAULT_DISABLED on the fresh insert",
                name
            );
        }

        let second = converge_mcp_rows(&db);
        assert_eq!(second.seeded, 0, "second pass must seed nothing");
        assert_eq!(second.retired, 0);
    }

    /// ACT: a stale bundled `ollama` row (two legacy field projects' shape) is
    /// disabled + badged, NOT deleted — and a re-run does not touch it again.
    #[test]
    fn converge_retires_stale_bundled_row_without_deleting_it() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Legacy Project", td.path());
        db.register_project_mcp_server(
            "p1",
            "ollama",
            false,
            "bundled",
            None,
            Some(".claude/settings.json"),
            None,
            &serde_json::json!({"command": "ollama"}),
        )
        .unwrap();

        let first = converge_mcp_rows(&db);
        assert_eq!(first.retired, 1);

        let rows = db.list_project_mcp_servers("p1").unwrap();
        let ollama = rows.iter().find(|r| r.mcp_name == "ollama").expect(
            "the row MUST still exist — retirement disables + badges, it never deletes",
        );
        assert!(!ollama.enabled);
        let badge = &ollama.config[MCP_RETIRED_CONFIG_KEY];
        assert_eq!(badge["removed_in"], "v0.2.11");
        assert!(badge["badge"]
            .as_str()
            .unwrap()
            .starts_with("retired in v0.2.11: "));
        // Prior config survived the merge.
        assert_eq!(ollama.config["command"], "ollama");

        let second = converge_mcp_rows(&db);
        assert_eq!(second.retired, 0, "retire must act exactly once");
    }

    /// LEAVE-ALONE, end-to-end: a USER-ADDED `ollama` row keeps its enabled
    /// state and gains no badge.
    #[test]
    fn converge_never_touches_a_user_added_row() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "User MCP Project", td.path());
        db.register_project_mcp_server(
            "p1",
            "ollama",
            true,
            "user",
            None,
            Some(".mcp.json"),
            Some("/usr/local/bin/my-ollama"),
            &serde_json::json!({"command": "/usr/local/bin/my-ollama"}),
        )
        .unwrap();

        let outcome = converge_mcp_rows(&db);
        assert_eq!(outcome.retired, 0);

        let rows = db.list_project_mcp_servers("p1").unwrap();
        let ollama = rows.iter().find(|r| r.mcp_name == "ollama").unwrap();
        assert!(ollama.enabled, "a user-added row must keep its enabled state");
        assert!(
            ollama.config.get(MCP_RETIRED_CONFIG_KEY).is_none(),
            "a user-added row must never be badged"
        );
        assert_eq!(ollama.command.as_deref(), Some("/usr/local/bin/my-ollama"));
    }

    /// COMPOSED REGRESSION (v0.2.91 wave-3, MAJOR-3): populate's UPSERT must
    /// not wipe the retirement badge, or `apply_post_bundle_steps` (populate →
    /// converge, back to back) silently re-disables an MCP the user
    /// deliberately re-enabled, on EVERY bundle update, with a fresh audit row
    /// each time.
    ///
    /// The full lifecycle, in one test: retire → user re-enables → populate
    /// re-UPSERTs from the project's own file → converge.
    #[test]
    fn a_user_re_enable_survives_populate_then_converge() {
        let td = tempfile::TempDir::new().unwrap();
        let folder = td.path();
        // The project's own `.mcp.json` still names the deprecated MCP — the
        // shape that makes populate re-UPSERT it every bundle update.
        std::fs::write(
            folder.join(".mcp.json"),
            r#"{"mcpServers": {"ollama": {"command": "ollama"}}}"#,
        )
        .unwrap();
        let db = db_with_project("p1", "Re-enabled Project", folder);
        db.register_project_mcp_server(
            "p1", "ollama", false, "bundled", None, Some(".mcp.json"),
            Some("ollama"), &serde_json::json!({"command": "ollama"}),
        )
        .unwrap();

        // 1. The engine retires it (as it would on the boot that upgraded).
        let dep = &deprecated()["ollama"];
        assert!(db
            .retire_project_mcp_server(
                "p1", "ollama", &retired_badge(&dep.removed_in, &dep.reason),
            )
            .unwrap());

        // 2. The user deliberately turns it back on.
        db.set_project_mcp_server_enabled("p1", "ollama", true).unwrap();

        // 3. A bundle update: populate re-mirrors the disk entry…
        db.register_project_mcp_server(
            "p1", "ollama", false, "bundled", None, Some(".mcp.json"),
            Some("ollama"), &serde_json::json!({"command": "ollama"}),
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        let after_populate = rows.iter().find(|r| r.mcp_name == "ollama").unwrap();
        assert!(
            after_populate.config.get(MCP_RETIRED_CONFIG_KEY).is_some(),
            "the badge must survive the re-UPSERT: {:?}",
            after_populate.config,
        );
        assert!(after_populate.enabled, "…and so must the user's toggle");

        // 4. …and the engine runs right after it.
        let mut outcome = TenantOutcome::default();
        converge_mcp_rows_for_project(&db, "p1", "Re-enabled Project", folder,
                                      &mut outcome);
        assert_eq!(outcome.retired, 0, "the row must NOT be retired again");

        let rows = db.list_project_mcp_servers("p1").unwrap();
        let ollama = rows.iter().find(|r| r.mcp_name == "ollama").unwrap();
        assert!(
            ollama.enabled,
            "a deliberate post-retirement re-enable must stand across a \
             bundle update",
        );
        assert_eq!(ollama.config["command"], "ollama", "prior config kept");
    }

    /// MINOR-6, ACT half: a deprecated row the project's OWN files do NOT
    /// author is VCO's leftover, and still retires.
    #[test]
    fn a_vco_seeded_deprecated_row_still_retires() {
        let out = decide_mcp_rows(
            &[row("ollama", false, "bundled", serde_json::json!({}))],
            &defaults(),
            deprecated(),
            &ProjectAuthoredMcps::authoring(&["weaviate-kg"]),
        );
        assert!(out.iter().any(|d| matches!(
            d, McpRowDecision::Retire { name, .. } if name == "ollama"
        )), "{:?}", out);
    }

    /// MINOR-6, LEAVE-ALONE half. RED pre-fix: `populate_mcp_servers` computes
    /// `is_user_added = !is_bundled_mcp(name)` — a NAME test — so a user's own
    /// hand-written `ollama` entry lands `is_user_added = 0` /
    /// `source = "bundled"`, invariant 1 does not fire, and the engine retires
    /// the USER'S MCP.
    #[test]
    fn a_user_authored_row_is_never_retired_whatever_its_columns_say() {
        let out = decide_mcp_rows(
            &[row("ollama", false, "bundled", serde_json::json!({}))],
            &defaults(),
            deprecated(),
            &ProjectAuthoredMcps::authoring(&["ollama"]),
        );
        assert!(
            out.contains(&McpRowDecision::Leave {
                name: "ollama".into(),
                why: LeaveReason::UserAuthoredOnDisk,
            }),
            "a row the project's own mcp config authors must be Leave: {:?}",
            out
        );
    }

    /// MINOR-6, invariant 2: unreadable provenance is not evidence of VCO
    /// provenance.
    #[test]
    fn unreadable_project_config_suppresses_retirement() {
        let out = decide_mcp_rows(
            &[row("ollama", false, "bundled", serde_json::json!({}))],
            &defaults(),
            deprecated(),
            &ProjectAuthoredMcps::unreadable(),
        );
        assert!(out.contains(&McpRowDecision::Leave {
            name: "ollama".into(),
            why: LeaveReason::ProvenanceUnknown,
        }), "{:?}", out);
    }

    /// The disk reader itself: absent files are conclusive evidence of
    /// "authors nothing"; a malformed one is NOT.
    #[test]
    fn project_authored_reader_separates_absent_from_unreadable() {
        let td = tempfile::TempDir::new().unwrap();
        let empty = ProjectAuthoredMcps::read(td.path());
        assert!(empty.conclusive, "no files ⇒ they author nothing");
        assert!(empty.names.is_empty());

        std::fs::create_dir_all(td.path().join(".claude")).unwrap();
        std::fs::write(
            td.path().join(".claude").join("settings.json"),
            r#"{"mcpServers": {"ollama": {}, "my-thing": {}}}"#,
        )
        .unwrap();
        let read = ProjectAuthoredMcps::read(td.path());
        assert!(read.conclusive);
        assert!(read.authors("ollama"));
        assert!(read.authors("my-thing"));
        assert!(!read.authors("weaviate-kg"));

        std::fs::write(td.path().join(".mcp.json"), "{ not json").unwrap();
        let broken = ProjectAuthoredMcps::read(td.path());
        assert!(
            !broken.conclusive,
            "a file that exists but will not parse means CANNOT TELL",
        );
    }

    /// End-to-end LEAVE-ALONE over a real DB + real files.
    #[test]
    fn converge_leaves_a_user_authored_deprecated_row_alone() {
        let td = tempfile::TempDir::new().unwrap();
        let folder = td.path();
        std::fs::write(
            folder.join(".mcp.json"),
            r#"{"mcpServers": {"ollama": {"command": "/opt/my-ollama"}}}"#,
        )
        .unwrap();
        let db = db_with_project("p1", "Hand Rolled Project", folder);
        // Exactly what populate writes for this entry today.
        db.register_project_mcp_server(
            "p1", "ollama", false, "bundled", None, Some(".mcp.json"),
            Some("/opt/my-ollama"),
            &serde_json::json!({"command": "/opt/my-ollama"}),
        )
        .unwrap();

        let mut outcome = TenantOutcome::default();
        converge_mcp_rows_for_project(&db, "p1", "Hand Rolled Project", folder,
                                      &mut outcome);
        assert_eq!(outcome.retired, 0);
        let rows = db.list_project_mcp_servers("p1").unwrap();
        let ollama = rows.iter().find(|r| r.mcp_name == "ollama").unwrap();
        assert!(ollama.enabled);
        assert!(ollama.config.get(MCP_RETIRED_CONFIG_KEY).is_none());
    }

    /// The deleted migration-010 backfill must be GONE from the boot path, and
    /// the engine must stand where it did (v0.2.91 wave-3 NIT — the dossier
    /// claimed a characterization test that did not exist).
    #[test]
    fn the_migration_010_backfill_gate_is_gone_and_the_engine_took_its_place() {
        let lib = include_str!("../lib.rs");
        // The GATE, not the narrative comment that records why it is gone.
        assert!(
            !lib.contains(".count_project_mcp_servers("),
            "the double-gate that converged NOTHING for a year (only projects \
             with zero rows, acting by re-running a pure disk mirror) must not \
             come back",
        );
        assert!(
            !lib.contains("populate_project_state_from_filesystem"),
            "…and the boot path must not re-acquire a pure disk mirror as its \
             convergence action",
        );
        assert!(
            lib.contains("crate::convergence::converge_at_boot"),
            "the engine must own the boot pass the backfill used to hold",
        );
        assert!(
            lib.contains("tauri::async_runtime::spawn"),
            "and it must be spawned through Tauri's runtime, never a bare \
             tokio::spawn from the sync setup() (the v0.2.90 boot-death class)",
        );
    }

    /// LEAVE-ALONE: a user's explicit DISABLE of a current bundled MCP
    /// survives a converge pass (the seeded-then-disabled lifecycle).
    #[test]
    fn converge_never_re_enables_a_user_disabled_row() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Opinionated Project", td.path());
        converge_mcp_rows(&db);
        db.set_project_mcp_server_enabled("p1", "playwright", false)
            .unwrap();

        converge_mcp_rows(&db);

        let rows = db.list_project_mcp_servers("p1").unwrap();
        let pw = rows.iter().find(|r| r.mcp_name == "playwright").unwrap();
        assert!(
            !pw.enabled,
            "an explicit user disable must survive every later pass"
        );
    }

    /// The engine's boot entry runs the write tenant and records the
    /// report-only tenants without inventing findings for them.
    #[test]
    fn boot_pass_runs_write_tenant_and_lists_report_only_debt() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Boot Pass Project", td.path());

        // repo_root = None → no deferral channel, no Python spawn in tests.
        let report = converge_at_boot(&db, None);
        assert_eq!(report.seeded, mcp_scan_rules::default_mcp_entry_names().len());
        assert!(report.pending.is_empty());
        assert!(!report.is_quiet());
        assert_eq!(
            report.report_only_without_detector,
            vec!["project_backfill", "codegraph_bindings", "module_settings"],
        );

        let quiet = converge_at_boot(&db, None);
        assert!(
            quiet.is_quiet(),
            "a converged install must produce a quiet pass: {:?}",
            quiet
        );
    }

    /// The post-bundle entry converges the ONE project it is given, leaves
    /// every other project alone, and reports no warnings on a clean run.
    #[test]
    fn post_bundle_entry_converges_only_the_named_project() {
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Updated Project", td.path());
        let other_folder = td.path().join("other");
        std::fs::create_dir_all(&other_folder).unwrap();
        let slug = db.generate_unique_slug("Other Project").unwrap();
        db.insert_project(
            "p2",
            "Other Project",
            &other_folder.to_string_lossy(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        let warnings = crate::convergence::converge_project_after_bundle_update(
            &db,
            "p1",
            "Updated Project",
            td.path(),
        );
        assert!(warnings.is_empty(), "clean run reports nothing: {:?}", warnings);
        assert_eq!(
            db.list_project_mcp_servers("p1").unwrap().len(),
            mcp_scan_rules::default_mcp_entry_names().len()
        );
        assert_eq!(
            db.list_project_mcp_servers("p2").unwrap().len(),
            0,
            "the post-bundle entry must not touch other projects"
        );

        // Idempotent on a second bundle update.
        let again = crate::convergence::converge_project_after_bundle_update(
            &db,
            "p1",
            "Updated Project",
            td.path(),
        );
        assert!(again.is_empty());
        assert_eq!(
            db.list_project_mcp_servers("p1").unwrap().len(),
            mcp_scan_rules::default_mcp_entry_names().len()
        );
    }

    /// The table is the contract: exactly one WRITE tenant this cycle
    /// (decision #8), and it is the MCP-rows one.
    #[test]
    fn exactly_one_write_tenant_this_cycle() {
        let write: Vec<&str> = CONVERGENCE_TENANTS
            .iter()
            .filter(|t| matches!(t.run, TenantRun::Write(_)))
            .map(|t| t.id)
            .collect();
        assert_eq!(
            write,
            vec!["project_mcp_servers"],
            "v0.2.91 decision #8: MCP rows are the FIRST and only write tenant; \
             write-enabling another one is a v0.2.92 change with its own \
             five-case matrix"
        );
    }

    /// A report-only detector that ERRORS reports nothing — it must not be
    /// read as "no drift", and it must not become a pending item either
    /// (invariant 2 at the tenant level).
    #[test]
    fn failing_report_only_detector_reports_nothing() {
        fn boom(_db: &Db) -> Result<Vec<String>, String> {
            Err("probe unavailable".into())
        }
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Detector Project", td.path());
        let mut report = ConvergenceReport::default();
        let mut outcome = TenantOutcome::default();
        match (boom as TenantDetector)(&db) {
            Ok(f) => outcome.pending = f,
            Err(_) => {}
        }
        report.absorb("synthetic", outcome);
        assert!(report.pending.is_empty());
    }

    /// A report-only detector that FINDS drift flows into the pending list
    /// (and therefore into the `convergence_pending` body) without writing.
    #[test]
    fn report_only_detector_findings_become_pending_items() {
        fn drift(_db: &Db) -> Result<Vec<String>, String> {
            Ok(vec!["module_settings row X is on a superseded default".into()])
        }
        let td = tempfile::TempDir::new().unwrap();
        let db = db_with_project("p1", "Detector Project", td.path());
        let mut report = ConvergenceReport::default();
        let mut outcome = TenantOutcome::default();
        outcome.pending = (drift as TenantDetector)(&db).unwrap();
        report.absorb("module_settings", outcome);
        assert_eq!(
            report.pending,
            vec!["[module_settings] module_settings row X is on a superseded default"],
        );
        assert_eq!(report.seeded, 0, "report-only tenants never write");
    }

    /// The ledger probe recognises a real emitted section and nothing else,
    /// so a clean pass on a healthy install never spawns the settle helper.
    #[test]
    fn deferral_entry_present_matches_only_a_real_section() {
        let td = tempfile::TempDir::new().unwrap();
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).unwrap();
        assert!(!deferral_entry_present(td.path(), CID_CONVERGENCE_PENDING));

        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            "# Deferred\n\n## some_other_condition (warning)\n\nbody\n",
        )
        .unwrap();
        assert!(!deferral_entry_present(td.path(), CID_CONVERGENCE_PENDING));

        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            "# Deferred\n\n## convergence_pending (warning)\n\nbody\n",
        )
        .unwrap();
        assert!(deferral_entry_present(td.path(), CID_CONVERGENCE_PENDING));
    }
}
