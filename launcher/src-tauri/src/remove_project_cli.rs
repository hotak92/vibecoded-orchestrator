// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106: `vct-launcher --remove-project <id|slug|name> [--keep-collections]
//! [--leave-unremovable]` — "Remove from VCO" from a terminal.
//!
//! The operation is NOT re-implemented here: this file parses the arguments,
//! resolves the project row, opens the launcher DB headless and calls
//! [`crate::commands::projects_v2::unregister_project`] — the same function the
//! Projects-page Remove button reaches through `delete_project_v2`, with the
//! same options the GUI Remove flow sends by default
//! (`REMOVE_UNREGISTER_OPTIONS` in `launcher/src/lib/unregister-escape.ts`:
//! files AND the project's own collections). The project folder and the
//! user's own files stay; the report is printed as JSON on stdout.
//!
//! Exit codes: 0 removed; 1 usage / lookup / DB error; 2 the removal stopped
//! (a value VCO wrote could not be removed — rerun with `--leave-unremovable`
//! to finish anyway, exactly the GUI's "Unregister anyway" second action).

use crate::commands::projects_v2::{unregister_project, UnregisterOptions};
use crate::db::models::{ProjectHost, ProjectRow};
use crate::db::Db;

pub(crate) const USAGE: &str = "usage: vct-launcher --remove-project <id|slug|name> \
[--keep-collections] [--leave-unremovable]\n\
Removes the project from VCO: what VCO added to the folder goes, the folder and your own \
files (.env, secrets, settings, source, knowledge/) stay. Add the folder again any time with \
Add Project.";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RemoveProjectArgs {
    pub query: String,
    pub opts: UnregisterOptions,
}

/// Parse the arguments after `--remove-project`.
pub(crate) fn parse_args(rest: &[String]) -> Result<RemoveProjectArgs, String> {
    let mut query: Option<String> = None;
    // The GUI Remove flow's options (REMOVE_UNREGISTER_OPTIONS).
    let mut opts = UnregisterOptions {
        purge_launcher_files: true,
        purge_collections: true,
        leave_unremovable: false,
    };
    for a in rest {
        match a.as_str() {
            "--keep-collections" => opts.purge_collections = false,
            "--leave-unremovable" => opts.leave_unremovable = true,
            "-h" | "--help" => return Err(USAGE.to_string()),
            s if s.starts_with("--") => return Err(format!("unknown option {}\n{}", s, USAGE)),
            s => {
                if query.is_some() {
                    return Err(format!("one project at a time\n{}", USAGE));
                }
                query = Some(s.to_string());
            }
        }
    }
    let query = query
        .filter(|q| !q.trim().is_empty())
        .ok_or_else(|| USAGE.to_string())?;
    Ok(RemoveProjectArgs { query, opts })
}

/// Pick the ONE project `query` names: an exact id, else an exact slug, else
/// an exact name (a name two projects share is refused, never guessed). The
/// orchestrator's own row is refused, as in the GUI: its `.claude/` is the
/// live install.
pub(crate) fn resolve_target(rows: &[ProjectRow], query: &str) -> Result<ProjectRow, String> {
    let pick = rows
        .iter()
        .find(|r| r.id == query)
        .or_else(|| rows.iter().find(|r| r.slug == query))
        .cloned();
    let row = match pick {
        Some(r) => r,
        None => {
            let by_name: Vec<&ProjectRow> = rows.iter().filter(|r| r.name == query).collect();
            match by_name.as_slice() {
                [one] => (*one).clone(),
                [] => return Err(format!("no registered project is called or has the id {:?}", query)),
                many => {
                    return Err(format!(
                        "{} projects are called {:?}; name one by its id or slug: {}",
                        many.len(),
                        query,
                        many.iter()
                            .map(|r| format!("{} ({})", r.slug, r.id))
                            .collect::<Vec<_>>()
                            .join(", ")
                    ))
                }
            }
        }
    };
    if row.host == ProjectHost::OrchestratorRoot {
        return Err(
            "the orchestrator's own project cannot be removed: its .claude/ is the live install"
                .to_string(),
        );
    }
    Ok(row)
}

/// Run the command; returns the process exit code. Prints are the CLI's
/// result (see the print contract in `lib.rs`).
pub(crate) fn run(rest: &[String]) -> i32 {
    let args = match parse_args(rest) {
        Ok(a) => a,
        Err(msg) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("{}", msg);
            return 1;
        }
    };
    let db = match Db::open() {
        Ok(d) => d,
        Err(e) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("[vct] cannot open the launcher database: {}", e);
            return 1;
        }
    };
    let rows = match db.list_projects() {
        Ok(r) => r,
        Err(e) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("[vct] cannot read the registered projects: {}", e);
            return 1;
        }
    };
    let row = match resolve_target(&rows, &args.query) {
        Ok(r) => r,
        Err(msg) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("[vct] {}", msg);
            return 1;
        }
    };
    match tauri::async_runtime::block_on(unregister_project(&db, &row.id, args.opts)) {
        Ok(report) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            println!(
                "{}",
                serde_json::to_string_pretty(&report).unwrap_or_else(|_| "{}".to_string())
            );
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!(
                "[vct] removed {:?} from VCO; the folder {} and your own files stay. \
                 Restart or refresh the launcher to update its project list.",
                row.name, row.folder_path
            );
            0
        }
        Err(msg) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!(
                "[vct] the removal of {:?} stopped: {}\n\
                 To finish anyway and leave those values, rerun with --leave-unremovable.",
                row.name, msg
            );
            2
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: &str, name: &str, slug: &str, host: ProjectHost) -> ProjectRow {
        let db = Db::open_in_memory().unwrap();
        db.insert_project(id, name, &format!("/w/{}", id), host, slug).unwrap()
    }

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn defaults_match_the_gui_remove_flow_and_flags_narrow_them() {
        let a = parse_args(&s(&["alpha"])).unwrap();
        assert_eq!(a.query, "alpha");
        assert!(a.opts.purge_launcher_files && a.opts.purge_collections);
        assert!(!a.opts.leave_unremovable);
        let b = parse_args(&s(&["--keep-collections", "alpha", "--leave-unremovable"])).unwrap();
        assert!(!b.opts.purge_collections && b.opts.leave_unremovable);
        assert!(parse_args(&s(&[])).is_err());
        assert!(parse_args(&s(&["a", "b"])).is_err());
        assert!(parse_args(&s(&["--drop-everything", "a"])).is_err());
    }

    #[test]
    fn the_target_is_an_exact_id_slug_or_unique_name_and_never_the_root() {
        let rows = vec![
            row("id-1", "Alpha", "alpha", ProjectHost::Base),
            row("id-2", "Twin", "twin", ProjectHost::Base),
            row("id-3", "Twin", "twin-2", ProjectHost::Base),
            row("id-r", "VCO", "orchestrator-root", ProjectHost::OrchestratorRoot),
        ];
        assert_eq!(resolve_target(&rows, "id-1").unwrap().id, "id-1");
        assert_eq!(resolve_target(&rows, "alpha").unwrap().id, "id-1");
        assert_eq!(resolve_target(&rows, "Alpha").unwrap().id, "id-1");
        assert_eq!(resolve_target(&rows, "twin-2").unwrap().id, "id-3");
        let twin = resolve_target(&rows, "Twin").unwrap_err();
        assert!(twin.contains("2 projects") && twin.contains("id-2") && twin.contains("id-3"));
        assert!(resolve_target(&rows, "nope").is_err());
        assert!(resolve_target(&rows, "orchestrator-root")
            .unwrap_err()
            .contains("cannot be removed"));
    }
}
