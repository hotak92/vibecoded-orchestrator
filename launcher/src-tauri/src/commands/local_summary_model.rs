// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! v0.2.106 (owner ruling 2026-11-12): Preferences → KG Summaries → "Local
//! summary model" — the opt-out for VCO's one local text-generation model.
//!
//! The setting itself is an ordinary `app_state` bool
//! (`kg_summary_local_model_enabled`), written by the page through the
//! generic `app_state_set_bool`; every READ of it, the model's presence, and
//! the guarded removal live in ONE Python home, `vco_lib.local_summary_model`
//! (tier A of the A>B>C sharing rule — a user-click path, so a subprocess is
//! fine). These commands are thin calls into it through the one vco_lib JSON
//! seam, plus "Download now", which starts the SAME background ensure the
//! dual-embedding toggles use (`dual_flags::start_model_ensure`, i.e.
//! `python -m vco_lib.embedding_pull_plan ensure` — the plan decides, so a
//! switched-off model is never pulled through here either).

use tauri::{command, AppHandle, State};

use crate::db::Db;

/// The python module that owns the switch (MUST MATCH the file name).
pub const LOCAL_SUMMARY_MODULE: &str = "vco_lib.local_summary_model";

/// argv after `-m <module>` for the status read.
pub fn status_args() -> [&'static str; 2] {
    ["status", "--json"]
}

/// argv after `-m <module>` for the removal. `--yes` is passed only by the
/// command the page invokes AFTER its own confirm dialog.
pub fn remove_args() -> [&'static str; 3] {
    ["remove", "--yes", "--json"]
}

async fn run(db: &Db, args: &[&str], what: &str) -> Result<serde_json::Value, String> {
    let os: Vec<&std::ffi::OsStr> = args.iter().map(std::ffi::OsStr::new).collect();
    crate::commands::project_hooks_settings::run_vco_lib_json(
        db,
        LOCAL_SUMMARY_MODULE,
        &os,
        None,
        what,
    )
    .await
    .map_err(String::from)
}

/// `{ok, enabled, source, model, present, size_bytes, approx_size_bytes,
/// vco_owns_ollama, ...}` — see `vco_lib.local_summary_model.status`.
#[command]
pub async fn get_local_summary_model_status(
    db: State<'_, Db>,
) -> Result<serde_json::Value, String> {
    run(&db, &status_args(), "the local summary model status").await
}

/// Delete the local summary model from VCO's own Ollama. The Python side
/// refuses (and this returns its reason as the error) unless the switch is
/// OFF, the model is not in the machine's plan, and VCO owns that Ollama.
#[command]
pub async fn remove_local_summary_model(db: State<'_, Db>) -> Result<serde_json::Value, String> {
    run(&db, &remove_args(), "the local summary model removal").await
}

/// "Download now": start the machine's model ensure in the background.
/// `true` = started (its outcome arrives as `dual_flags::MODEL_ENSURE_EVENT`
/// with scope `global`); `false` = python or the install root could not be
/// resolved (the next install/update still pulls it).
#[command]
pub async fn download_local_summary_model(
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<bool, String> {
    Ok(crate::commands::dual_flags::start_model_ensure(
        &db, app, None,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_name_matches_the_python_file() {
        let rel = format!(
            "{}/../../{}.py",
            env!("CARGO_MANIFEST_DIR"),
            LOCAL_SUMMARY_MODULE.replace('.', "/")
        );
        assert!(std::path::Path::new(&rel).is_file(), "missing {rel}");
    }

    #[test]
    fn removal_is_always_confirmed_and_json() {
        assert_eq!(remove_args(), ["remove", "--yes", "--json"]);
        assert_eq!(status_args(), ["status", "--json"]);
    }
}
