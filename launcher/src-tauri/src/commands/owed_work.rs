// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! v0.2.107 (A-003): Preferences → Services → "Owed background work" — the
//! read-only line that shows the machine's owed-work queue (projects whose
//! skipped KG seed / backfill / code-graph walk waits for Weaviate and a free
//! slot) and whether its one worker is running.
//!
//! The queue, its worker and the status reading live in ONE Python home,
//! `vco_lib.owed_work` (tier A of the A>B>C sharing rule — a page-open read,
//! so a subprocess is fine). This command is a thin call into it through the
//! one vco_lib JSON seam.

use tauri::{command, State};

use crate::db::Db;

/// The python module that owns the queue (MUST MATCH the file name).
pub const OWED_WORK_MODULE: &str = "vco_lib.owed_work";

/// argv after `-m <module>` for the status read.
pub fn status_args() -> [&'static str; 2] {
    ["status", "--json"]
}

/// `{ok, queued, worker_running, projects: [{key, folder, enqueued_at, tries}]}`
/// — see `vco_lib.owed_work.status`.
#[command]
pub async fn get_owed_work_status(db: State<'_, Db>) -> Result<serde_json::Value, String> {
    let args = status_args();
    let os: Vec<&std::ffi::OsStr> = args.iter().map(std::ffi::OsStr::new).collect();
    crate::commands::project_hooks_settings::run_vco_lib_json(
        &db,
        OWED_WORK_MODULE,
        &os,
        None,
        "the owed-work queue status",
    )
    .await
    .map_err(String::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_name_matches_the_python_file() {
        let rel = format!(
            "{}/../../{}.py",
            env!("CARGO_MANIFEST_DIR"),
            OWED_WORK_MODULE.replace('.', "/")
        );
        assert!(std::path::Path::new(&rel).is_file(), "missing {rel}");
    }

    #[test]
    fn status_is_a_json_read() {
        assert_eq!(status_args(), ["status", "--json"]);
    }
}
