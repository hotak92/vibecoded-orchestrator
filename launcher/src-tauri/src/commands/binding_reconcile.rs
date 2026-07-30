// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.89 (BUG 4 §5.3) — boot-time, evidence-gated repair for installs that
//! were HALF-RENAMED by the pre-.89 rename machinery.
//!
//! ## What broke, historically
//!
//! Until v0.2.89, `rename_project_v2` MOVED the code-graph binding prefix to
//! the new-name-derived prefix (v0.2.75 C-10) and rewrote
//! `kg_collection_access` rows to new-name-derived collection names
//! (v0.2.49). After v0.2.84 made every read path BINDING-first, those moves
//! pointed consumers at classes that don't exist: the CG binding tracked an
//! empty `NewName_Code*` set while the populated rows stayed under
//! `OldName_Code*` (the field reporter's 'HouseOfFlirt' phantom vs the real
//! 'HouseOfFire_*' classes), and access rows referenced phantom
//! `{new_sanitized}_KnowledgeGraph`-shaped names. v0.2.89 makes names
//! IMMUTABLE post-creation (rename = display name + slug only), which stops
//! NEW damage; this module heals the EXISTING damage.
//!
//! ## Discipline
//!
//! * **Positive evidence only, never guess.** Every repair requires the full
//!   gate chain to pass; any miss → no write (an honest
//!   `codegraph_binding_phantom` deferral at most).
//! * **Probe failure ≠ absence.** The Weaviate `/v1/schema` snapshot is
//!   fetched ONCE per boot; if the fetch fails we do NOTHING — an
//!   unreachable Weaviate is not evidence that classes are missing (the
//!   §5.4 negative-cache lesson).
//! * **Fully soft-fail.** Any per-project error logs and moves on; the
//!   sweep can never block launcher boot. Idempotent: after a successful
//!   repair the "prefix classes absent" gate fails on the next boot.
//!
//! Called once from `lib.rs::setup()` (spawned async task, after migrations,
//! alongside the resume sweeps).

use std::collections::HashSet;
use std::path::Path;

use crate::db::Db;

/// Outcome summary for the boot log line.
#[derive(Debug, Default)]
pub(crate) struct BindingReconcileReport {
    /// The `/v1/schema` fetch failed — nothing was inspected or written.
    pub probe_failed: bool,
    /// CG bindings restored to their evidence-backed historical prefix.
    pub bindings_repaired: usize,
    /// `codegraph_binding_phantom` deferrals actually WRITTEN (wave-2 F8:
    /// emissions only — a phantom project with no resolvable repo root, or
    /// a failed emit, is logged but not counted).
    pub phantom_deferrals: usize,
    /// `kg_collection_access` rows rewritten back from v0.2.49 phantom
    /// names to their binding-backed siblings.
    pub access_rows_restored: usize,
}

/// The repair decision for one project's code-graph binding. Pure output of
/// [`decide_binding_repair`] so every gate is unit-testable without HTTP/DB.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum RepairDecision {
    /// No CG binding row — nothing to reconcile.
    NoBinding,
    /// The binding's prefix has populated classes (case-insensitive) — leave
    /// it alone.
    Healthy,
    /// All gates passed: restore the binding prefix to `candidate`.
    Repair {
        candidate: String,
        evidence: Evidence,
    },
    /// The prefix is a phantom (no classes) but no candidate satisfied the
    /// evidence chain — emit the honest deferral, write nothing.
    Phantom,
}

/// Which evidence leg produced the restoration candidate (recorded in the
/// audit row + deferral so a human can retrace the repair).
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub(crate) enum Evidence {
    /// §5.3.3a — the project's `codegraph_rename_split_pending` deferral
    /// named `cg_prefix_old`.
    RenameDeferral,
    /// §5.3.3b — the stem of the primary KG binding
    /// (`X_KnowledgeGraph` → `X`). CAVEAT: KG stems use the
    /// underscore-DROPPING sanitizer while CG prefixes use the
    /// underscore-PRESERVING one, so for underscore-bearing names the stem
    /// is NOT the historical prefix and the candidate-classes-exist gate
    /// simply fails (honest phantom, no repair) — by design.
    KgStem,
}

impl Evidence {
    fn as_str(&self) -> &'static str {
        match self {
            Evidence::RenameDeferral => "rename-deferral cg_prefix_old",
            Evidence::KgStem => "primary KG binding stem",
        }
    }
}

/// True when either `<prefix>_CodeModule` or `<prefix>_CodeFunction` exists
/// in the schema snapshot. Case-insensitive: a case-different sibling means
/// reads DO resolve (the hub's casing rebind adopts on-disk casing), so it
/// is not a phantom.
fn prefix_has_classes(prefix: &str, classes_lower: &HashSet<String>) -> bool {
    classes_lower.contains(&format!("{}_codemodule", prefix.to_lowercase()))
        || classes_lower.contains(&format!("{}_codefunction", prefix.to_lowercase()))
}

/// §5.3 evidence chain, as a pure function.
///
/// * `binding_prefix` — `None` = no binding row.
/// * `deferral_prefix_old` — the `cg_prefix_old` recovered from the
///   project's `codegraph_rename_split_pending` deferral, if any (leg 3a).
/// * `kg_primary_collection` — the primary KG binding's collection name, if
///   any (leg 3b takes its `_KnowledgeGraph` stem).
/// * `classes_lower` — lowercased class names from ONE successful
///   `/v1/schema` fetch. The caller must NOT call this at all when the
///   fetch failed (probe failure ≠ absence).
/// * `owned_by_other_project` — cross-tenant guard: true when the candidate
///   prefix is the binding prefix of a DIFFERENT project (never steal).
pub(crate) fn decide_binding_repair(
    binding_prefix: Option<&str>,
    deferral_prefix_old: Option<&str>,
    kg_primary_collection: Option<&str>,
    classes_lower: &HashSet<String>,
    owned_by_other_project: impl Fn(&str) -> bool,
) -> RepairDecision {
    // Gate 1: a binding must exist.
    let prefix = match binding_prefix {
        Some(p) if !p.is_empty() => p,
        _ => return RepairDecision::NoBinding,
    };

    // Gate 2: the binding's prefix must be a phantom (no populated classes).
    if prefix_has_classes(prefix, classes_lower) {
        return RepairDecision::Healthy;
    }

    // Gate 3: restoration candidates in evidence order — the rename-time
    // deferral's cg_prefix_old first (it names the EXACT pre-rename
    // prefix), the KG stem second (coincides with the historical CG prefix
    // only for underscore-free names; gate 4 rejects the divergent case).
    let candidates: [(Option<String>, Evidence); 2] = [
        (
            deferral_prefix_old
                .filter(|c| !c.is_empty())
                .map(str::to_string),
            Evidence::RenameDeferral,
        ),
        (
            kg_primary_collection
                .and_then(|c| c.strip_suffix("_KnowledgeGraph"))
                .filter(|c| !c.is_empty())
                .map(str::to_string),
            Evidence::KgStem,
        ),
    ];

    for (candidate, evidence) in candidates {
        let Some(candidate) = candidate else { continue };
        // Gate 4: candidate must differ from the phantom, its classes must
        // POSITIVELY exist, and it must not belong to another project.
        if candidate == prefix {
            continue;
        }
        if !prefix_has_classes(&candidate, classes_lower) {
            continue;
        }
        if owned_by_other_project(&candidate) {
            continue;
        }
        return RepairDecision::Repair { candidate, evidence };
    }

    RepairDecision::Phantom
}

/// §5.3 (second half) — should a v0.2.49 phantom access row be rewritten
/// back to its binding-backed sibling? Pure gates:
/// the two names differ, a row exists at the phantom name, the phantom
/// class is POSITIVELY absent, and the binding-backed class POSITIVELY
/// exists. (Caller guarantees the schema snapshot came from a successful
/// probe.)
pub(crate) fn should_repair_access_row(
    phantom_name: &str,
    binding_backed: &str,
    phantom_row_exists: bool,
    classes_lower: &HashSet<String>,
) -> bool {
    phantom_name != binding_backed
        && phantom_row_exists
        && !classes_lower.contains(&phantom_name.to_lowercase())
        && classes_lower.contains(&binding_backed.to_lowercase())
}

/// Recover `cg_prefix_old` from a project's `UPDATE_DEFERRED.md` (§5.3.3a).
///
/// The pre-.89 `codegraph_rename_split_pending` emitter wrote (verbatim
/// format string, now deleted from projects_v2.rs):
/// ``The existing code-graph rows live under the `X_Code*` classes.``
/// We scan for that stable sentence rather than parsing the whole report.
/// Returns `None` when the file lacks the condition, the sentence, or the
/// recovered prefix contains non-identifier characters (a user-edited file
/// falls back to the KG-stem leg — conservative).
pub(crate) fn extract_cg_prefix_old(deferred_md: &str) -> Option<String> {
    if !deferred_md.contains("codegraph_rename_split_pending") {
        return None;
    }
    const MARKER: &str = "live under the `";
    const SUFFIX: &str = "_Code*`";
    let start = deferred_md.find(MARKER)? + MARKER.len();
    let rest = &deferred_md[start..];
    let end = rest.find(SUFFIX)?;
    let prefix = &rest[..end];
    if prefix.is_empty()
        || !prefix
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return None;
    }
    Some(prefix.to_string())
}

/// Fetch `/v1/schema` once and return the LOWERCASED class-name set.
/// `Err` = probe failed (the caller must then do nothing at all).
async fn fetch_schema_classes_lower(weaviate_url: &str) -> Result<HashSet<String>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| format!("binding_reconcile: reqwest client: {}", e))?;
    let url = format!("{}/v1/schema", weaviate_url.trim_end_matches('/'));
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("binding_reconcile: GET {}: {}", url, e))?;
    if !resp.status().is_success() {
        return Err(format!(
            "binding_reconcile: {} returned status {}",
            url,
            resp.status().as_u16()
        ));
    }
    let schema: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("binding_reconcile: parse {}: {}", url, e))?;
    Ok(schema
        .get("classes")
        .and_then(|c| c.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|c| c.get("class").and_then(|v| v.as_str()))
                .map(|s| s.to_lowercase())
                .collect()
        })
        .unwrap_or_default())
}

/// Boot entry point. One schema fetch, then a per-project sweep applying
/// [`decide_binding_repair`] + the access-row reconcile. Every write is
/// audit-logged; user-visible outcomes land as deferral entries in the
/// affected project's `UPDATE_DEFERRED.md` (best-effort — a deferral-emit
/// failure never blocks the repair itself).
pub(crate) async fn reconcile_half_renamed_bindings_at_boot(
    db: &Db,
    weaviate_url: &str,
) -> BindingReconcileReport {
    let mut report = BindingReconcileReport::default();

    // ONE schema fetch per boot. Failure → do nothing (probe ≠ absence).
    let classes_lower = match fetch_schema_classes_lower(weaviate_url).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!(
                "[vct] binding-reconcile: schema probe failed ({}); skipping — \
                 probe failure is not evidence of absence, next boot retries",
                e
            );
            report.probe_failed = true;
            return report;
        }
    };

    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[vct] binding-reconcile: list_projects failed: {}", e);
            return report;
        }
    };

    // sys_path_root for the Python deferral bridge; deferrals are skipped
    // (soft-fail) when the orchestrator clone can't be located.
    let repo_root = crate::commands::installer::find_local_repo_root().ok();

    for project in &projects {
        // ── CG binding repair ────────────────────────────────────────────
        let binding = match db.get_project_codegraph_binding(&project.id) {
            Ok(b) => b,
            Err(e) => {
                eprintln!(
                    "[vct] binding-reconcile: get binding for {}: {}",
                    project.id, e
                );
                None
            }
        };

        let deferral_prefix_old = std::fs::read_to_string(
            Path::new(&project.folder_path)
                .join(".claude")
                .join("context")
                .join("UPDATE_DEFERRED.md"),
        )
        .ok()
        .as_deref()
        .and_then(extract_cg_prefix_old);

        let kg_bindings = db
            .list_project_kg_bindings(&project.id)
            .unwrap_or_default();
        let kg_primary = kg_bindings
            .iter()
            .find(|b| b.role == "primary")
            .map(|b| b.collection_name.clone());

        let decision = decide_binding_repair(
            binding.as_ref().map(|b| b.collection_prefix.as_str()),
            deferral_prefix_old.as_deref(),
            kg_primary.as_deref(),
            &classes_lower,
            |candidate| {
                matches!(
                    db.find_project_by_codegraph_prefix(candidate),
                    Ok(Some(owner)) if owner != project.id
                )
            },
        );

        match decision {
            RepairDecision::NoBinding | RepairDecision::Healthy => {}
            RepairDecision::Repair { candidate, evidence } => {
                // Unwrap is safe: Repair implies a binding row (gate 1).
                let b = binding.as_ref().expect("Repair decision requires binding");
                let old_prefix = b.collection_prefix.clone();
                if let Err(e) = db.set_project_codegraph_binding(
                    &project.id,
                    &candidate,
                    b.embedding_model.as_deref(),
                    b.embedding_dim,
                    b.last_analyzed_commit.as_deref(),
                    b.last_analyzed_at,
                    b.enabled,
                    &b.config,
                ) {
                    eprintln!(
                        "[vct] binding-reconcile: restore {} → {} for {} failed: {}",
                        old_prefix, candidate, project.id, e
                    );
                    continue;
                }
                db.audit(
                    "codegraph_binding_update",
                    Some(&project.id),
                    None,
                    &serde_json::json!({
                        "field": "collection_prefix",
                        "old_value": old_prefix,
                        "new_value": candidate,
                        "reason": "binding_reconcile_half_rename_repair",
                        "evidence": evidence.as_str(),
                    }),
                )
                .ok();
                eprintln!(
                    "[vct] binding-reconcile: restored codegraph binding for {:?} \
                     ({} → {}, evidence: {})",
                    project.name,
                    old_prefix,
                    candidate,
                    evidence.as_str()
                );
                report.bindings_repaired += 1;

                if let Some(root) = &repo_root {
                    let folder = Path::new(&project.folder_path);
                    let detected = format!(
                        "A pre-v0.2.89 rename left this project's code-graph \
                         binding on the phantom prefix {:?} (no populated \
                         classes) while the real rows live under {:?}. The \
                         launcher restored the binding automatically at boot \
                         (evidence: {}).",
                        old_prefix,
                        candidate,
                        evidence.as_str()
                    );
                    let fields = crate::services::deferral::DeferralEntryFields {
                        condition_id: "codegraph_binding_repaired",
                        title: "Code-graph binding restored after a half-completed rename",
                        detected: &detected,
                        why_deferred: "Informational record of an automatic, \
                             evidence-gated repair — collection names are \
                             immutable post-creation as of v0.2.89, so this \
                             cannot recur.",
                        command_to_apply: "# No action needed — the binding was \
                             already restored. This entry is an informational \
                             record of the automatic repair.",
                        severity: "info",
                    };
                    if let Err(e) =
                        crate::services::deferral::emit_deferral_entry(root, folder, &fields)
                    {
                        eprintln!(
                            "[vct] binding-reconcile: repair deferral emit failed \
                             (non-fatal): {}",
                            e
                        );
                    }
                    // Settle the stale pre-.89 rename deferral: its "rebuild
                    // under the new name" remediation is now wrong (the
                    // binding is back on the populated historical prefix).
                    if let Err(e) = crate::services::deferral::resolve_deferral_conditions(
                        root,
                        folder,
                        &["codegraph_rename_split_pending"],
                    ) {
                        eprintln!(
                            "[vct] binding-reconcile: settling stale rename \
                             deferral failed (non-fatal): {}",
                            e
                        );
                    }
                }
            }
            RepairDecision::Phantom => {
                eprintln!(
                    "[vct] binding-reconcile: project {:?} has a phantom \
                     codegraph prefix {:?} and no evidence-backed restoration \
                     candidate; leaving it alone",
                    project.name,
                    binding
                        .as_ref()
                        .map(|b| b.collection_prefix.as_str())
                        .unwrap_or("")
                );
                if let Some(root) = &repo_root {
                    let folder = Path::new(&project.folder_path);
                    let prefix = binding
                        .as_ref()
                        .map(|b| b.collection_prefix.clone())
                        .unwrap_or_default();
                    let detected = format!(
                        "This project's code-graph binding points at prefix {:?}, \
                         but no {}_CodeModule/_CodeFunction classes exist on \
                         Weaviate and no restoration candidate passed the \
                         evidence gates (rename-deferral prefix, KG-binding \
                         stem). The graph is effectively empty until a rebuild.",
                        prefix, prefix
                    );
                    // POSIX single-quote the display name for the emitted
                    // rebuild command (same escape as every other emitter).
                    let name_sh = format!("'{}'", project.name.replace('\'', r"'\''"));
                    let cmd = format!(
                        "# Rebuild this project's code graph (fills the bound \
                         prefix), or set the correct\n# prefix in the \
                         launcher's Identity tab (code-graph prefix) first:\n\
                         cd {}\n\
                         .claude/scripts/code-graph-analyze . --project {}",
                        project.folder_path, name_sh
                    );
                    let fields = crate::services::deferral::DeferralEntryFields {
                        condition_id: "codegraph_binding_phantom",
                        title: "Code-graph binding points at classes that do not exist",
                        detected: &detected,
                        why_deferred: "Repair requires positive evidence of the \
                             historical prefix; guessing could steal another \
                             project's collections or bind to the wrong data. \
                             A rebuild (or a manual prefix fix in the Identity \
                             tab) resolves it.",
                        command_to_apply: &cmd,
                        severity: "warning",
                    };
                    // Wave-2 review F8: count only ACTUAL emissions — the
                    // boot log line reports "phantom deferrals: N" as
                    // written entries, so a missing repo root or a failed
                    // emit must not inflate the counter.
                    match crate::services::deferral::emit_deferral_entry(root, folder, &fields)
                    {
                        Ok(()) => report.phantom_deferrals += 1,
                        Err(e) => eprintln!(
                            "[vct] binding-reconcile: phantom deferral emit failed \
                             (non-fatal): {}",
                            e
                        ),
                    }
                }
            }
        }

        // ── kg_collection_access phantom reconcile (v0.2.49 rewrites) ────
        // Rows the old rename machinery pointed at
        // `{sanitize(current display)}{suffix}` names that don't exist on
        // Weaviate, while the binding-backed sibling does → rewrite back.
        let Some(kg_primary) = kg_primary else {
            continue; // no primary binding → no binding-backed target names
        };
        let name_derived =
            crate::commands::projects_v2::sanitize_kg_collection(&project.name);
        let mut restored_here: Vec<(String, String)> = Vec::new();
        for suffix in ["_KnowledgeGraph", "_Development", "_Diagrams"] {
            let phantom_name = format!("{}{}", name_derived, suffix);
            let binding_backed = if suffix == "_KnowledgeGraph" {
                kg_primary.clone()
            } else {
                vct_launcher_core::collection_naming::derive_sibling_collection(
                    &kg_primary,
                    suffix,
                    &project.slug,
                )
            };
            let phantom_row_exists = matches!(
                db.kg_get_access(&project.id, &phantom_name),
                Ok(Some(_))
            );
            if should_repair_access_row(
                &phantom_name,
                &binding_backed,
                phantom_row_exists,
                &classes_lower,
            ) {
                match db.kg_rename_access(&project.id, &phantom_name, &binding_backed) {
                    Ok(n) if n > 0 => {
                        db.audit(
                            "kg_access_phantom_repaired",
                            Some(&project.id),
                            None,
                            &serde_json::json!({
                                "from": phantom_name,
                                "to": binding_backed,
                                "reason": "binding_reconcile_v0249_rename_rewrite",
                            }),
                        )
                        .ok();
                        restored_here.push((phantom_name, binding_backed));
                        report.access_rows_restored += 1;
                    }
                    Ok(_) => {}
                    Err(e) => {
                        eprintln!(
                            "[vct] binding-reconcile: kg_rename_access({} → {}) \
                             for {} failed: {}",
                            phantom_name, binding_backed, project.id, e
                        );
                    }
                }
            }
        }
        if !restored_here.is_empty() {
            eprintln!(
                "[vct] binding-reconcile: rewrote {} phantom kg_collection_access \
                 row(s) for {:?}: {:?}",
                restored_here.len(),
                project.name,
                restored_here
            );
            if let Some(root) = &repo_root {
                let folder = Path::new(&project.folder_path);
                let detected = format!(
                    "Pre-v0.2.89 renames rewrote this project's KG access rows \
                     to name-derived collection names that do not exist on \
                     Weaviate. The launcher rewrote them back to the \
                     binding-backed names at boot: {:?}.",
                    restored_here
                );
                let fields = crate::services::deferral::DeferralEntryFields {
                    condition_id: "kg_access_phantom_repaired",
                    title: "KG access rows restored to binding-backed collection names",
                    detected: &detected,
                    why_deferred: "Informational record of an automatic, \
                         evidence-gated repair (phantom name absent on \
                         Weaviate, binding-backed sibling present).",
                    command_to_apply: "# No action needed — the access rows were \
                         already restored.",
                    severity: "info",
                };
                if let Err(e) =
                    crate::services::deferral::emit_deferral_entry(root, folder, &fields)
                {
                    eprintln!(
                        "[vct] binding-reconcile: access-repair deferral emit \
                         failed (non-fatal): {}",
                        e
                    );
                }
            }
        }
    }

    report
}

#[cfg(test)]
mod tests {
    use super::*;

    fn classes(names: &[&str]) -> HashSet<String> {
        names.iter().map(|s| s.to_lowercase()).collect()
    }

    // ── decide_binding_repair: the §5.3 gate chain ──────────────────────

    /// ACT: phantom prefix + rename-deferral names the old prefix + old
    /// classes present → restore from the deferral evidence (leg 3a wins
    /// over the KG stem).
    #[test]
    fn repair_fires_on_deferral_evidence() {
        let cls = classes(&["HouseOfFire_CodeModule", "HouseOfFire_KnowledgeGraph"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            Some("HouseOfFire"),
            Some("SomethingElse_KnowledgeGraph"),
            &cls,
            |_| false,
        );
        assert_eq!(
            d,
            RepairDecision::Repair {
                candidate: "HouseOfFire".to_string(),
                evidence: Evidence::RenameDeferral,
            }
        );
    }

    /// ACT: no deferral evidence, but the KG-binding stem's classes exist →
    /// restore from the stem (leg 3b, the no-underscore field case).
    #[test]
    fn repair_falls_back_to_kg_stem() {
        let cls = classes(&["HouseOfFire_CodeFunction"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            None,
            Some("HouseOfFire_KnowledgeGraph"),
            &cls,
            |_| false,
        );
        assert_eq!(
            d,
            RepairDecision::Repair {
                candidate: "HouseOfFire".to_string(),
                evidence: Evidence::KgStem,
            }
        );
    }

    /// LEAVE-ALONE (gate 2): the binding's own classes exist → Healthy, no
    /// repair even when a tempting candidate is on offer.
    #[test]
    fn healthy_prefix_is_left_alone() {
        let cls = classes(&["HouseOfFlirt_CodeModule", "HouseOfFire_CodeModule"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            Some("HouseOfFire"),
            Some("HouseOfFire_KnowledgeGraph"),
            &cls,
            |_| false,
        );
        assert_eq!(d, RepairDecision::Healthy);
    }

    /// LEAVE-ALONE (gate 2, case-insensitive): a case-different sibling of
    /// the binding's classes counts as populated — the hub's casing rebind
    /// makes reads work, so it is NOT a phantom.
    #[test]
    fn case_different_own_classes_count_as_healthy() {
        let cls = classes(&["houseofflirt_codemodule"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            Some("HouseOfFire"),
            None,
            &cls,
            |_| false,
        );
        assert_eq!(d, RepairDecision::Healthy);
    }

    /// LEAVE-ALONE (gate 4a): candidate classes absent → honest Phantom, no
    /// guessing. This is also the underscore-divergence outcome: the
    /// KG stem 'HouseOfFire' cannot restore a historical 'House_Of_Fire'
    /// prefix because the stem's classes don't exist under the stem name.
    #[test]
    fn missing_candidate_classes_yield_phantom_not_guess() {
        let cls = classes(&["House_Of_Fire_CodeModule"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            None,
            Some("HouseOfFire_KnowledgeGraph"),
            &cls,
            |_| false,
        );
        assert_eq!(d, RepairDecision::Phantom);
    }

    /// LEAVE-ALONE (gate 4b): the candidate prefix belongs to ANOTHER
    /// project → never steal a tenant's collections; honest Phantom.
    #[test]
    fn candidate_owned_by_other_project_is_rejected() {
        let cls = classes(&["HouseOfFire_CodeModule"]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            Some("HouseOfFire"),
            None,
            &cls,
            |candidate| candidate == "HouseOfFire",
        );
        assert_eq!(d, RepairDecision::Phantom);
    }

    /// LEAVE-ALONE (gate 1): no binding row → nothing to reconcile, no
    /// deferral noise for never-analyzed projects.
    #[test]
    fn no_binding_is_a_noop() {
        let cls = classes(&["Anything_CodeModule"]);
        let d = decide_binding_repair(None, Some("Anything"), None, &cls, |_| false);
        assert_eq!(d, RepairDecision::NoBinding);
    }

    /// Gate 4 also rejects a candidate equal to the phantom itself (e.g. a
    /// KG stem that matches the current prefix) — otherwise we'd "repair" a
    /// binding to its own phantom value.
    #[test]
    fn candidate_equal_to_phantom_is_skipped() {
        let cls = classes(&[]);
        let d = decide_binding_repair(
            Some("HouseOfFlirt"),
            Some("HouseOfFlirt"),
            Some("HouseOfFlirt_KnowledgeGraph"),
            &cls,
            |_| false,
        );
        assert_eq!(d, RepairDecision::Phantom);
    }

    // ── extract_cg_prefix_old ───────────────────────────────────────────

    #[test]
    fn extracts_prefix_from_rename_deferral_sentence() {
        // Verbatim shape the (now deleted) v0.2.75 emitter produced.
        let md = "## codegraph_rename_split_pending (info)\n\
                  Project renamed from \"HouseOfFire\" to \"HouseOfFlirt\". \
                  The code-graph binding prefix (the source of truth \
                  consumers read) now tracks the NEW name, so reads target an \
                  initially-empty class set until a rebuild fills it. The \
                  existing code-graph rows live under the `HouseOfFire_Code*` \
                  classes.";
        assert_eq!(
            extract_cg_prefix_old(md).as_deref(),
            Some("HouseOfFire")
        );
    }

    #[test]
    fn extract_returns_none_without_the_condition_or_sentence() {
        // No condition id at all.
        assert_eq!(
            extract_cg_prefix_old("live under the `X_Code*` classes."),
            None
        );
        // Condition present but the never-analyzed variant (no prefix line).
        let md = "## codegraph_rename_split_pending (info)\n\
                  This project has no code-graph binding yet (nothing to \
                  rebuild until the first analyze runs).";
        assert_eq!(extract_cg_prefix_old(md), None);
    }

    #[test]
    fn extract_rejects_non_identifier_prefixes() {
        // A user-mangled file must not smuggle arbitrary text into a
        // binding write (conservative: fall back to the KG-stem leg).
        let md = "codegraph_rename_split_pending\n\
                  rows live under the `weird name!$_Code*` classes.";
        assert_eq!(extract_cg_prefix_old(md), None);
    }

    #[test]
    fn extract_handles_underscore_bearing_prefixes() {
        let md = "codegraph_rename_split_pending\n\
                  rows live under the `House_Of_Fire_Code*` classes.";
        assert_eq!(
            extract_cg_prefix_old(md).as_deref(),
            Some("House_Of_Fire")
        );
    }

    // ── should_repair_access_row ────────────────────────────────────────

    #[test]
    fn access_repair_gates() {
        let cls = classes(&["Bound_KnowledgeGraph"]);
        // ACT: row exists at absent phantom, binding-backed class present.
        assert!(should_repair_access_row(
            "Phantom_KnowledgeGraph",
            "Bound_KnowledgeGraph",
            true,
            &cls
        ));
        // LEAVE-ALONE: no row at the phantom name.
        assert!(!should_repair_access_row(
            "Phantom_KnowledgeGraph",
            "Bound_KnowledgeGraph",
            false,
            &cls
        ));
        // LEAVE-ALONE: the "phantom" class actually exists (not a phantom).
        let cls2 = classes(&["Phantom_KnowledgeGraph", "Bound_KnowledgeGraph"]);
        assert!(!should_repair_access_row(
            "Phantom_KnowledgeGraph",
            "Bound_KnowledgeGraph",
            true,
            &cls2
        ));
        // LEAVE-ALONE: binding-backed class absent (no positive target).
        let cls3 = classes(&[]);
        assert!(!should_repair_access_row(
            "Phantom_KnowledgeGraph",
            "Bound_KnowledgeGraph",
            true,
            &cls3
        ));
        // LEAVE-ALONE: names identical — nothing to rewrite.
        assert!(!should_repair_access_row(
            "Same_KnowledgeGraph",
            "Same_KnowledgeGraph",
            true,
            &cls
        ));
    }

    // ── full-path tests (in-memory DB + fake / unreachable Weaviate) ────

    use crate::db::models::ProjectHost;
    use crate::db::Db;

    fn spawn_fake_weaviate_schema(
        class_names: Vec<String>,
    ) -> (String, tokio::task::JoinHandle<()>) {
        use axum::{routing::get, Json, Router};
        let payload: Vec<serde_json::Value> = class_names
            .iter()
            .map(|c| serde_json::json!({ "class": c }))
            .collect();
        let body = serde_json::json!({ "classes": payload });
        let app: Router = Router::new().route(
            "/v1/schema",
            get(move || {
                let b = body.clone();
                async move { Json(b) }
            }),
        );
        let (tx, rx) = std::sync::mpsc::channel();
        let handle = tokio::spawn(async move {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            tx.send(listener.local_addr().unwrap()).unwrap();
            let _ = axum::serve(listener, app).await;
        });
        let addr = rx.recv().unwrap();
        (format!("http://{}", addr), handle)
    }

    /// ACT (end-to-end): phantom prefix + deferral naming the old prefix +
    /// old classes present on (fake) Weaviate → the binding is restored with
    /// every provenance column preserved.
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_restores_half_renamed_binding() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let td = tempfile::TempDir::new().unwrap();
        db.insert_project(
            &pid,
            "HouseOfFlirt",
            &td.path().to_string_lossy(),
            ProjectHost::Base,
            "houseofflirt",
        )
        .unwrap();
        db.set_project_codegraph_binding(
            &pid,
            "HouseOfFlirt", // the phantom the pre-.89 rename minted
            Some("codesage-large-v2"),
            Some(2048),
            Some("cafebabe"),
            Some(1_700_000_000),
            true,
            &serde_json::json!({"k": "v"}),
        )
        .unwrap();
        // Evidence 3a: the pre-.89 rename deferral naming the old prefix.
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).unwrap();
        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            "## codegraph_rename_split_pending (info)\n\
             The existing code-graph rows live under the `HouseOfFire_Code*` \
             classes.\n",
        )
        .unwrap();

        let (url, _server) =
            spawn_fake_weaviate_schema(vec!["HouseOfFire_CodeModule".to_string()]);
        let report = reconcile_half_renamed_bindings_at_boot(&db, &url).await;

        assert!(!report.probe_failed);
        assert_eq!(report.bindings_repaired, 1);
        assert_eq!(report.phantom_deferrals, 0);
        let after = db.get_project_codegraph_binding(&pid).unwrap().unwrap();
        assert_eq!(after.collection_prefix, "HouseOfFire");
        // Provenance columns preserved through the repair.
        assert_eq!(after.embedding_model.as_deref(), Some("codesage-large-v2"));
        assert_eq!(after.embedding_dim, Some(2048));
        assert_eq!(after.last_analyzed_commit.as_deref(), Some("cafebabe"));
        assert_eq!(after.last_analyzed_at, Some(1_700_000_000));
        assert!(after.enabled);
        assert_eq!(after.config, serde_json::json!({"k": "v"}));
    }

    /// LEAVE-ALONE (the load-bearing §5.4 leg): Weaviate unreachable →
    /// probe failure ≠ absence — the phantom-looking binding is NOT touched
    /// and no deferral machinery fires.
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_does_nothing_when_probe_fails() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let td = tempfile::TempDir::new().unwrap();
        db.insert_project(
            &pid,
            "HouseOfFlirt",
            &td.path().to_string_lossy(),
            ProjectHost::Base,
            "houseofflirt",
        )
        .unwrap();
        db.set_project_codegraph_binding(
            &pid,
            "HouseOfFlirt",
            None,
            None,
            None,
            None,
            true,
            &serde_json::Value::Null,
        )
        .unwrap();
        // Access row at a phantom-shaped name — must ALSO stay untouched.
        db.kg_set_access(&pid, "Houseofflirt_KnowledgeGraph", "write")
            .unwrap();

        // Definitely-closed port.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);

        let report =
            reconcile_half_renamed_bindings_at_boot(&db, &format!("http://{}", addr)).await;

        assert!(report.probe_failed);
        assert_eq!(report.bindings_repaired, 0);
        assert_eq!(report.phantom_deferrals, 0);
        assert_eq!(report.access_rows_restored, 0);
        let after = db.get_project_codegraph_binding(&pid).unwrap().unwrap();
        assert_eq!(
            after.collection_prefix, "HouseOfFlirt",
            "probe failure must never mutate a binding"
        );
        assert_eq!(
            db.kg_get_access(&pid, "Houseofflirt_KnowledgeGraph")
                .unwrap()
                .as_deref(),
            Some("write"),
            "probe failure must never touch access rows"
        );
        // No deferral file may appear on the probe-failure path.
        assert!(
            !td.path()
                .join(".claude")
                .join("context")
                .join("UPDATE_DEFERRED.md")
                .exists()
        );
    }

    /// LEAVE-ALONE: a healthy binding (own classes present) is untouched
    /// end-to-end, even with a colliding KG stem candidate available.
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_leaves_healthy_binding_alone() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let td = tempfile::TempDir::new().unwrap();
        db.insert_project(
            &pid,
            "Acme",
            &td.path().to_string_lossy(),
            ProjectHost::Base,
            "acme",
        )
        .unwrap();
        db.set_project_codegraph_binding(
            &pid,
            "Acme",
            None,
            None,
            None,
            None,
            true,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.set_project_kg_binding(
            &pid,
            "primary",
            "SomethingElse_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();

        let (url, _server) = spawn_fake_weaviate_schema(vec![
            "Acme_CodeModule".to_string(),
            "SomethingElse_CodeModule".to_string(),
            "SomethingElse_KnowledgeGraph".to_string(),
        ]);
        let report = reconcile_half_renamed_bindings_at_boot(&db, &url).await;

        assert_eq!(report.bindings_repaired, 0);
        assert_eq!(report.phantom_deferrals, 0);
        let after = db.get_project_codegraph_binding(&pid).unwrap().unwrap();
        assert_eq!(after.collection_prefix, "Acme");
    }

    /// LEAVE-ALONE: the restoration candidate is owned by ANOTHER project →
    /// no write end-to-end (cross-tenant steal guard through the real DB
    /// ownership lookup).
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_never_steals_another_projects_prefix() {
        let db = Db::open_in_memory().unwrap();
        let victim = uuid::Uuid::new_v4().to_string();
        let phantom = uuid::Uuid::new_v4().to_string();
        let td_v = tempfile::TempDir::new().unwrap();
        let td_p = tempfile::TempDir::new().unwrap();
        db.insert_project(
            &victim,
            "HouseOfFire",
            &td_v.path().to_string_lossy(),
            ProjectHost::Base,
            "houseoffire",
        )
        .unwrap();
        db.set_project_codegraph_binding(
            &victim,
            "HouseOfFire",
            None,
            None,
            None,
            None,
            true,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.insert_project(
            &phantom,
            "HouseOfFlirt",
            &td_p.path().to_string_lossy(),
            ProjectHost::Base,
            "houseofflirt",
        )
        .unwrap();
        db.set_project_codegraph_binding(
            &phantom,
            "HouseOfFlirt",
            None,
            None,
            None,
            None,
            true,
            &serde_json::Value::Null,
        )
        .unwrap();
        // KG stem of the PHANTOM project points at the victim's prefix.
        db.set_project_kg_binding(
            &phantom,
            "primary",
            "HouseOfFire_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();

        let (url, _server) =
            spawn_fake_weaviate_schema(vec!["HouseOfFire_CodeModule".to_string()]);
        let report = reconcile_half_renamed_bindings_at_boot(&db, &url).await;

        assert_eq!(report.bindings_repaired, 0, "steal must be rejected");
        let after = db.get_project_codegraph_binding(&phantom).unwrap().unwrap();
        assert_eq!(after.collection_prefix, "HouseOfFlirt");
        let victim_after = db.get_project_codegraph_binding(&victim).unwrap().unwrap();
        assert_eq!(victim_after.collection_prefix, "HouseOfFire");
    }

    /// ACT: the v0.2.49 access-row phantom is rewritten back to the
    /// binding-backed name; unrelated rows stay untouched.
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_restores_phantom_access_rows() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let td = tempfile::TempDir::new().unwrap();
        // Display name "Beta" → sanitize → "Beta"; the v0.2.49 rename
        // rewrite pointed the access row at Beta_KnowledgeGraph while the
        // binding (immutable) stayed on Acme_KnowledgeGraph.
        db.insert_project(
            &pid,
            "Beta",
            &td.path().to_string_lossy(),
            ProjectHost::Base,
            "beta",
        )
        .unwrap();
        db.set_project_kg_binding(
            &pid,
            "primary",
            "Acme_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.kg_set_access(&pid, "Beta_KnowledgeGraph", "write").unwrap();
        // Unrelated row (shared) must survive untouched.
        db.kg_set_access(&pid, "VibeCodedOrchestrator_KnowledgeGraph", "write")
            .unwrap();

        let (url, _server) = spawn_fake_weaviate_schema(vec![
            "Acme_KnowledgeGraph".to_string(),
            "VibeCodedOrchestrator_KnowledgeGraph".to_string(),
        ]);
        let report = reconcile_half_renamed_bindings_at_boot(&db, &url).await;

        assert_eq!(report.access_rows_restored, 1);
        assert_eq!(
            db.kg_get_access(&pid, "Beta_KnowledgeGraph").unwrap(),
            None,
            "phantom row must be gone"
        );
        assert_eq!(
            db.kg_get_access(&pid, "Acme_KnowledgeGraph")
                .unwrap()
                .as_deref(),
            Some("write"),
            "row rewritten back to the binding-backed name, level preserved"
        );
        assert_eq!(
            db.kg_get_access(&pid, "VibeCodedOrchestrator_KnowledgeGraph")
                .unwrap()
                .as_deref(),
            Some("write"),
            "unrelated shared row untouched"
        );
    }

    /// LEAVE-ALONE: when the name-derived class actually EXISTS on Weaviate
    /// it is not a phantom — the access row must stay where it is.
    #[tokio::test(flavor = "multi_thread")]
    async fn boot_reconcile_leaves_existing_named_access_rows_alone() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let td = tempfile::TempDir::new().unwrap();
        db.insert_project(
            &pid,
            "Beta",
            &td.path().to_string_lossy(),
            ProjectHost::Base,
            "beta",
        )
        .unwrap();
        db.set_project_kg_binding(
            &pid,
            "primary",
            "Acme_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.kg_set_access(&pid, "Beta_KnowledgeGraph", "read").unwrap();

        // BOTH classes exist → the Beta row is legitimate data, not a
        // phantom (the user may genuinely use that collection).
        let (url, _server) = spawn_fake_weaviate_schema(vec![
            "Acme_KnowledgeGraph".to_string(),
            "Beta_KnowledgeGraph".to_string(),
        ]);
        let report = reconcile_half_renamed_bindings_at_boot(&db, &url).await;

        assert_eq!(report.access_rows_restored, 0);
        assert_eq!(
            db.kg_get_access(&pid, "Beta_KnowledgeGraph")
                .unwrap()
                .as_deref(),
            Some("read"),
            "an existing-on-Weaviate name is not a phantom — leave it alone"
        );
    }
}
