// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! v0.2.91 WP-L (plan decision #22) — Tauri surface for the three dual
//! embedding / RL-logging flags, now that they have a HOST-WIDE default tier.
//!
//! ## Why this file exists rather than more commands in `rl_settings.rs`
//!
//! The six pre-existing `get_dual_*` / `set_dual_*` commands over in
//! `rl_settings.rs` each return or take a bare `bool`. That shape was correct
//! while the flags were per-project-only: "no row" and "off" really were the
//! same thing. Decision #22 makes them different — an explicit per-project row
//! wins over the host-wide default IN BOTH DIRECTIONS — so a bare `bool` can
//! no longer say whether a checked box is this project's choice or an
//! inherited default. Rendering one anyway is the lying-toggle shape.
//!
//! The six old commands stay registered and working (they are part of the
//! shipped IPC surface), but they now DELEGATE to the same resolver as the
//! commands here, so the two can never disagree. New GUI code calls this file.
//!
//! ## The cascade lives in ONE place
//!
//! Neither this file nor the GUI re-derives precedence. Everything routes
//! through `vct_launcher_core::db::settings`:
//!
//!   * `Db::resolve_dual_flags` — three tiers + the cross-tier log⟹write
//!     clamp; also what the hub `/config` resolver calls;
//!   * `Db::set_dual_flag_for_project` — write-or-DELETE plus the
//!     within-tier coherence cascade;
//!   * `Db::set_dual_flag_global_default` — the same cascade at the global
//!     tier.
//!
//! The Python env projection (`vco_lib/config_projection.py`) mirrors the
//! resolver; `tests/test_dual_flags_cascade_parity_v0291.py` locks the three.
//!
//! ## Soft-fail discipline (unchanged from the old setters)
//!
//! The DB write is the authoritative operation. Env re-projection runs after
//! it and its warnings ride in the result — a projection hiccup NEVER rolls
//! back the write.
//!
//! ## Turning a flag ON ensures the models it needs (v0.2.100 AD-6)
//!
//! A dual opt-in can need a second KG embedder (arctic, or qwen3 on an
//! arctic-active install) that no install ever pulled — the slot was then
//! silently skipped at write time (L1-F11). Enabling a flag now runs
//! `python -m vco_lib.embedding_pull_plan ensure --json` — the SAME plan
//! install.py step 7 runs (rule A, no Rust mirror) — in the background: a
//! pull can take minutes and must not hold the toggle. The outcome is logged,
//! and a failure lands in UPDATE_DEFERRED (`ollama_model_pull_failed` /
//! `ollama_not_ready_at_update`, written by the Python side), which the
//! deferral badge shows. Present models are skipped, never re-pulled.

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use tauri::{command, State};
use vct_launcher_core::process::CommandExt as _;

use crate::db::Db;
use vct_launcher_core::db::settings::{DualFlag, DualFlagGlobalDefaults, DualFlagsState};

/// Result of a host-wide default write.
///
/// Changing an install-wide default changes the resolved env of every
/// INHERITING project, so the write is followed by a machine-global
/// re-projection and the GUI is told what happened to the other projects. A
/// control whose displayed state runs ahead of the effective state is the
/// same class of dishonesty as a lying toggle, so "wrote it and said nothing"
/// is not an option here.
#[derive(Debug, Clone, Serialize)]
pub struct DualFlagGlobalWriteResult {
    /// Host-wide defaults AFTER the write (the global-tier cascade may have
    /// moved a second flag, so the GUI must re-render from this, not from
    /// what it optimistically set).
    pub defaults: DualFlagGlobalDefaults,
    /// Projects whose `.claude/{settings.json,env}` was rewritten.
    pub reprojected: usize,
    /// Projects that reported a soft-fail warning during re-projection.
    pub warnings: usize,
    /// Projects skipped because their folder no longer exists on disk.
    pub skipped: usize,
    /// Whether enabling the flag started the background model ensure (the
    /// outcome is logged; a failure is recorded in UPDATE_DEFERRED).
    #[serde(default)]
    pub model_ensure_started: bool,
}

/// Parsed `python -m vco_lib.embedding_pull_plan ensure --json` result.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ModelEnsureOutcome {
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub present: Vec<String>,
    #[serde(default)]
    pub pulled: Vec<String>,
    #[serde(default)]
    pub failed: BTreeMap<String, String>,
    #[serde(default)]
    pub deferral: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}

/// The one argv for the ensure CLI (cwd = install root, so `vco_lib` resolves).
pub fn model_ensure_command(python: &Path, install_root: &Path) -> std::process::Command {
    let mut cmd = std::process::Command::new(python).silent();
    cmd.arg("-m")
        .arg("vco_lib.embedding_pull_plan")
        .arg("ensure")
        .arg("--json")
        .arg("--root")
        .arg(install_root)
        .current_dir(install_root)
        .stdin(std::process::Stdio::null());
    cmd
}

/// Interpret the CLI's output. Unparseable output is a failure carrying the
/// stderr tail — never "ok" by default.
pub fn parse_model_ensure_output(
    success: bool,
    stdout: &[u8],
    stderr: &[u8],
) -> ModelEnsureOutcome {
    let text = String::from_utf8_lossy(stdout);
    let parsed = text
        .lines()
        .rev()
        .find_map(|l| serde_json::from_str::<ModelEnsureOutcome>(l.trim()).ok());
    match parsed {
        Some(mut o) => {
            o.ok = o.ok && success;
            if !o.ok && o.error.is_none() {
                o.error = Some("model ensure exited non-zero".into());
            }
            o
        }
        None => {
            let err = String::from_utf8_lossy(stderr);
            let tail: String = err.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
            ModelEnsureOutcome {
                error: Some(format!("model ensure produced no result: {tail}")),
                ..Default::default()
            }
        }
    }
}

/// Start the background ensure; `false` when python or the install root
/// cannot be resolved (logged — the next install run still derives the plan).
fn start_model_ensure(db: &Db) -> bool {
    let Some(root) = crate::commands::installer::resolve_install_root_sync(db) else {
        tracing::warn!("[vct] dual-flag model ensure skipped: install root not resolvable");
        return false;
    };
    let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() else {
        tracing::warn!("[vct] dual-flag model ensure skipped: no python for vco_lib");
        return false;
    };
    std::thread::spawn(move || {
        let outcome = match model_ensure_command(&python, &root).output() {
            Ok(o) => parse_model_ensure_output(o.status.success(), &o.stdout, &o.stderr),
            Err(e) => ModelEnsureOutcome {
                error: Some(format!("spawn failed: {e}")),
                ..Default::default()
            },
        };
        if outcome.ok {
            tracing::info!(
                "[vct] dual-flag model ensure: present={:?} pulled={:?}",
                outcome.present,
                outcome.pulled
            );
        } else {
            tracing::warn!(
                "[vct] dual-flag model ensure FAILED: {:?} (failed={:?}, deferral={:?})",
                outcome.error,
                outcome.failed,
                outcome.deferral
            );
        }
    });
    true
}

/// Resolve all three dual flags for one project, WITH provenance.
///
/// One round trip for the whole panel (the old panel made three separate
/// `get_dual_*` invokes and still could not tell inherited from chosen).
#[command]
pub async fn get_dual_flags_state(
    project_id: String,
    db: State<'_, Db>,
) -> Result<DualFlagsState, String> {
    if project_id.is_empty() {
        return Err("get_dual_flags_state: project_id required".into());
    }
    Ok(db.resolve_dual_flags(&project_id))
}

/// Set — or CLEAR — one dual flag for one project.
///
/// `value = None` deletes the per-project row, returning the project to
/// inheriting the host-wide default. That is the only way back once a user
/// has clicked anything, so it is a first-class value, not an edge case.
///
/// `flag` is validated: an unknown name is an explicit error, never a silent
/// no-op. A command that quietly ignores a value is a toggle that does
/// nothing on click, which is the same lie with extra steps.
#[command]
pub async fn set_dual_flag_for_project(
    project_id: String,
    flag: String,
    value: Option<bool>,
    db: State<'_, Db>,
) -> Result<DualFlagsState, String> {
    let flag = DualFlag::from_wire(&flag)?;
    db.set_dual_flag_for_project(&project_id, flag, value)?;
    // Soft-fail: warnings ride along, the DB write is never rolled back.
    let _ = crate::commands::projects_v2::reproject_env_soft(&db, &project_id);
    // Enabling (explicitly, or by clearing into an enabled default) may need
    // a second KG embedder — ensure it (background; see module docs).
    if value != Some(false) {
        start_model_ensure(&db);
    }
    // Return the RESOLVED state so the panel re-renders from the truth
    // (the coherence cascade may have moved a second flag).
    Ok(db.resolve_dual_flags(&project_id))
}

/// Read the three host-wide defaults for the global panel.
#[command]
pub async fn get_dual_flags_global_defaults(
    db: State<'_, Db>,
) -> Result<DualFlagGlobalDefaults, String> {
    Ok(db.dual_flag_global_defaults())
}

/// Free-function core of `set_dual_flag_global_default` — DB write + the
/// global-tier cascade + the machine-global env re-projection, testable
/// without a Tauri runtime.
pub fn set_dual_flag_global_default_with_db(
    db: &Db,
    flag: DualFlag,
    value: bool,
) -> Result<DualFlagGlobalWriteResult, String> {
    db.set_dual_flag_global_default(flag, value)?;
    // Every project WITHOUT an explicit row now resolves differently, so
    // re-project them all — that rewrite is also what lets the settings
    // watcher's diff-guard fire the guarded MCP reload.
    let report = crate::commands::projects_v2::refresh_all_projects_env_with_db(db);
    for (name, err) in &report.failed {
        tracing::warn!(
            "[vct] warning: host-wide dual-flag default ({}) env re-projection \
             failed for {}: {}",
            flag.wire_name(),
            name,
            err
        );
    }
    Ok(DualFlagGlobalWriteResult {
        defaults: db.dual_flag_global_defaults(),
        reprojected: report.refreshed.len() + report.refreshed_with_warnings.len(),
        warnings: report.refreshed_with_warnings.len() + report.failed.len(),
        skipped: report.skipped.len(),
        model_ensure_started: false,
    })
}

/// Set one host-wide dual-flag default.
///
/// Applies the log⟹write cascade at the global tier, then re-projects every
/// registered project's env so inheriting projects pick the change up now
/// rather than at some later refresh.
#[command]
pub async fn set_dual_flag_global_default(
    flag: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<DualFlagGlobalWriteResult, String> {
    let flag = DualFlag::from_wire(&flag)?;
    let mut result = set_dual_flag_global_default_with_db(&db, flag, value)?;
    if value {
        result.model_ensure_started = start_model_ensure(&db);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::settings::DualFlagSource;

    fn db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "p-cmd".to_string();
        db.insert_project(
            &id,
            "CmdProj",
            "/nonexistent/cmd-proj",
            crate::db::models::ProjectHost::Base,
            "cmdproj",
        )
        .expect("insert project");
        (db, id)
    }

    /// The command layer's own contract: an unknown flag name is refused, not
    /// silently ignored. (The cascade itself is tested in vct-launcher-core.)
    #[test]
    fn unknown_flag_names_are_refused() {
        assert!(DualFlag::from_wire("dual_write").is_err());
        assert!(DualFlag::from_wire("").is_err());
        for flag in DualFlag::ALL {
            assert!(DualFlag::from_wire(flag.wire_name()).is_ok());
        }
    }

    /// The global setter re-projects EVERY project, not just one. Proof of
    /// invocation: a project whose folder does not exist lands in `skipped`,
    /// a value only the refresh-all path computes.
    #[test]
    fn global_default_write_reprojects_all_projects() {
        let (db, _p) = db_with_project();
        let r = set_dual_flag_global_default_with_db(&db, DualFlag::ArcticSecondary, true)
            .expect("global default write must succeed");
        assert!(r.defaults.arctic_secondary);
        assert_eq!(
            r.skipped, 1,
            "the machine-global refresh must have iterated the registered \
             projects; got {r:?}",
        );
    }

    /// A global write is never rolled back by a projection outcome, and the
    /// returned defaults reflect the global-tier cascade rather than the
    /// caller's optimistic value.
    #[test]
    fn global_log_default_write_returns_the_cascaded_defaults() {
        let (db, _p) = db_with_project();
        let r = set_dual_flag_global_default_with_db(&db, DualFlag::RlLog, true)
            .expect("global default write must succeed");
        assert!(r.defaults.rl_log);
        assert!(
            r.defaults.write_all_slots,
            "the GUI must be told the prerequisite moved too, not left to \
             guess from what it asked for",
        );
    }

    /// The ensure argv is the Python plan CLI (rule A — no Rust mirror of the
    /// pull rule), run from the install root so `vco_lib` resolves.
    #[test]
    fn model_ensure_command_runs_the_python_plan() {
        let cmd = model_ensure_command(Path::new("/py"), Path::new("/root/vco"));
        let args: Vec<String> = cmd
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            args,
            [
                "-m",
                "vco_lib.embedding_pull_plan",
                "ensure",
                "--json",
                "--root",
                "/root/vco"
            ]
        );
        assert_eq!(cmd.get_current_dir(), Some(Path::new("/root/vco")));
    }

    /// Success needs BOTH a parsed `ok: true` and a zero exit; a failure keeps
    /// the Python side's error + deferral id; garbage is never "ok".
    #[test]
    fn model_ensure_output_is_parsed_strictly() {
        let ok = parse_model_ensure_output(
            true,
            br#"{"ok": true, "present": ["qwen3-embedding:0.6b"], "pulled": ["snowflake-arctic-embed2:latest"], "failed": {}, "deferral": null, "error": null, "plan": {}}"#,
            b"",
        );
        assert!(ok.ok);
        assert_eq!(
            ok.pulled,
            vec!["snowflake-arctic-embed2:latest".to_string()]
        );

        let failed = parse_model_ensure_output(
            false,
            br#"{"ok": false, "failed": {"snowflake-arctic-embed2:latest": "boom"}, "deferral": "ollama_model_pull_failed", "error": "x"}"#,
            b"",
        );
        assert!(!failed.ok);
        assert_eq!(failed.deferral.as_deref(), Some("ollama_model_pull_failed"));

        let lying = parse_model_ensure_output(false, br#"{"ok": true}"#, b"");
        assert!(!lying.ok, "a non-zero exit is never ok");
        assert!(lying.error.is_some());

        let garbage = parse_model_ensure_output(true, b"Traceback ...", b"ImportError: x");
        assert!(!garbage.ok);
        assert!(garbage.error.unwrap().contains("ImportError"));
    }

    /// The per-project setter's `None` really clears, and the returned state
    /// is the re-resolved truth (not an echo of the request).
    #[test]
    fn project_setter_clear_returns_the_inherited_state() {
        let (db, p) = db_with_project();
        db.set_dual_flag_global_default(DualFlag::ArcticSecondary, true)
            .unwrap();
        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, Some(false))
            .unwrap();
        assert!(!db.resolve_dual_flags(&p).arctic_secondary.effective);

        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, None)
            .unwrap();
        let st = db.resolve_dual_flags(&p).arctic_secondary;
        assert_eq!(st.explicit, None);
        assert!(st.effective);
        assert_eq!(st.source, DualFlagSource::InstallDefault);
    }
}
