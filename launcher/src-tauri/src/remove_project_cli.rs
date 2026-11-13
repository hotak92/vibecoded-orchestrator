// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106: `vct-launcher --remove-project <id|slug|name> [--keep-collections]
//! [--leave-unremovable] [--yes] [--preview]` — "Remove from VCO" from a
//! terminal.
//!
//! The operation is NOT re-implemented here: this file parses the arguments,
//! resolves the project row, opens the launcher DB headless and calls
//! [`crate::commands::projects_v2::unregister_project`] — the same function the
//! Projects-page Remove button reaches through `delete_project_v2`, with the
//! same options the GUI Remove flow sends by default — both read them from
//! `launcher/src/lib/remove-unregister-options.json` (files AND the project's
//! own collections; `REMOVE_UNREGISTER_OPTIONS` in `unregister-escape.ts`
//! imports the same file). The project folder and the user's own files stay;
//! the report is printed as JSON on stdout.
//!
//! It ASKS first (owner ruling 2026-11-11, review L7): in an interactive
//! terminal it prints what would be removed — the files and the collections,
//! from [`crate::commands::projects_v2::removal_preview`] — and removes only
//! on an explicit y. `--yes` skips the question; without a terminal and
//! without `--yes` it refuses and changes nothing (never a silent drop).
//! `--preview` prints that preview as JSON and changes nothing (the hub's
//! `GET /cli/projects/{id}/removal-preview`, so `vct-cli` asks the same way).
//!
//! Exit codes (the hub maps each to its own HTTP answer, `vct-hub cli_api`):
//! * 0 — removed (or `--help` / `--preview`, printed on stdout);
//! * 1 — usage / lookup / DB-open error;
//! * 2 — the removal STOPPED on a value VCO wrote and could not remove: rerun
//!   with `--leave-unremovable` to finish anyway, exactly the GUI's
//!   "Unregister anyway" second action (the only error that names the flag);
//! * 3 — the removal failed for another reason (e.g. an update holds the
//!   launcher database); the message says which;
//! * 4 — not confirmed: nothing was changed.

use crate::commands::projects_v2::{
    is_yes, removal_preview, unregister_project, UnregisterOptions, UNREGISTER_STOPPED_PREFIX,
};
use crate::db::models::{ProjectHost, ProjectRow};
use crate::db::Db;

pub(crate) const USAGE: &str = "usage: vct-launcher --remove-project <id|slug|name> \
[--keep-collections] [--leave-unremovable] [--yes] [--preview]\n\
Removes the project from VCO: what VCO added to the folder goes, the folder and your own \
files (.env, secrets, settings, source, knowledge/) stay, and the project's own Weaviate \
collections are dropped unless --keep-collections is given. It shows what it would remove \
and asks first; --yes removes without asking (needed when not run from a terminal); \
--preview prints what it would remove as JSON and changes nothing. Add the folder again any \
time with Add Project. On Windows, without --yes and without a console of its own, it opens \
a console window to ask in.";

/// Exit code: the removal stopped on an unremovable value (see the module doc).
pub(crate) const EXIT_STOPPED: i32 = 2;
/// Exit code: the removal failed for another reason.
pub(crate) const EXIT_FAILED: i32 = 3;
/// Exit code: not confirmed, nothing changed.
pub(crate) const EXIT_NOT_CONFIRMED: i32 = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RemoveProjectArgs {
    pub query: String,
    pub opts: UnregisterOptions,
    /// `--yes`: remove without asking.
    pub yes: bool,
    /// `--preview`: print what would be removed (JSON), change nothing.
    pub preview: bool,
}

/// What the arguments ask for: a removal, or the usage text (review B-L3: a
/// deliberate `--help` prints to stdout and exits 0, unlike a usage error).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Parsed {
    Run(RemoveProjectArgs),
    Help,
}

/// The GUI Remove flow's options, read from the ONE file both surfaces use
/// (review B-L1): `launcher/src/lib/remove-unregister-options.json`, which
/// `REMOVE_UNREGISTER_OPTIONS` in `unregister-escape.ts` imports. Fields the
/// file does not name take [`UnregisterOptions`]' own defaults; the first
/// call never leaves unremovable values (the stop stays the default).
pub(crate) fn remove_flow_options() -> UnregisterOptions {
    const SHARED: &str = include_str!("../../src/lib/remove-unregister-options.json");
    let mut opts: UnregisterOptions =
        serde_json::from_str(SHARED).expect("remove-unregister-options.json is valid JSON");
    opts.leave_unremovable = false;
    opts
}

/// Parse the arguments after `--remove-project`.
pub(crate) fn parse_args(rest: &[String]) -> Result<Parsed, String> {
    let mut query: Option<String> = None;
    let mut opts = remove_flow_options();
    let (mut yes, mut preview) = (false, false);
    for a in rest {
        match a.as_str() {
            "--keep-collections" => opts.purge_collections = false,
            "--leave-unremovable" => opts.leave_unremovable = true,
            "--yes" | "-y" => yes = true,
            "--preview" => preview = true,
            "-h" | "--help" => return Ok(Parsed::Help),
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
    Ok(Parsed::Run(RemoveProjectArgs { query, opts, yes, preview }))
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

/// The exit code and the message for a removal that returned `Err` (review
/// L5): only the unremovable-value stop names `--leave-unremovable`; every
/// other failure (an update holding the database, an audit or row-delete
/// error) says what failed and exits [`EXIT_FAILED`].
pub(crate) fn failure(name: &str, msg: &str) -> (i32, String) {
    if msg.starts_with(UNREGISTER_STOPPED_PREFIX) {
        (
            EXIT_STOPPED,
            format!(
                "[vct] the removal of {:?} stopped: {}\n\
                 To finish anyway and leave those values, rerun with --leave-unremovable.",
                name, msg
            ),
        )
    } else {
        (EXIT_FAILED, format!("[vct] the removal of {:?} failed: {}", name, msg))
    }
}

/// Whether to go ahead without asking, ask, or refuse (owner ruling
/// 2026-11-11): `--yes` goes ahead; an interactive terminal asks; anything
/// else refuses — a removal that drops collections is never silent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Confirm {
    Go,
    Ask,
    Refuse,
}

pub(crate) fn confirm_mode(yes: bool, interactive: bool) -> Confirm {
    if yes {
        Confirm::Go
    } else if interactive {
        Confirm::Ask
    } else {
        Confirm::Refuse
    }
}

fn interactive_terminal() -> bool {
    use std::io::IsTerminal;
    std::io::stdin().is_terminal() && std::io::stderr().is_terminal()
}

/// Windows console plumbing for review L6. A RELEASE build is a
/// GUI-subsystem binary (`#![windows_subsystem = "windows"]` in main.rs):
/// started from a GUI parent (Explorer, a shortcut, the hub's windowless
/// spawn) it gets NO console handles at all, so `interactive_terminal()` is
/// false there, the y/N prompt could never be read, and even the refusal
/// printed nowhere — the removal looked like it silently did nothing.
#[cfg(windows)]
mod win_console {
    /// Every std handle absent — no console AND no redirection?
    pub fn std_streams_absent() -> bool {
        use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
        use windows_sys::Win32::System::Console::{
            GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
        };
        unsafe {
            [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE].iter().all(|which| {
                let h = GetStdHandle(*which);
                h.is_null() || h == INVALID_HANDLE_VALUE
            })
        }
    }

    /// Attach to the parent process's console (a `start`ed console keeps no
    /// handle inheritance). True when a console is attached afterwards.
    pub fn attach_parent() -> bool {
        use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
        unsafe { AttachConsole(ATTACH_PARENT_PROCESS) != 0 }
    }

    /// Allocate a fresh console (a new console window opens for the prompt).
    pub fn allocate() -> bool {
        use windows_sys::Win32::System::Console::AllocConsole;
        unsafe { AllocConsole() != 0 }
    }
}

/// Review L6: give the ask path a console to ask in (Windows only).
///
/// Called only when the run may need to ask or to refuse — never for
/// `--yes` / `--preview` / `--help`: the hub pipes those runs, and opening a
/// console there would flash a window on the desktop for every hub-driven
/// removal. Nothing happens when a terminal or redirected streams already
/// exist (a script's captured stderr is exactly where the refusal belongs).
/// After a successful attach/allocate `interactive_terminal()` answers true
/// and the flow ASKS instead of refusing; when no console can be had the
/// refusal still fires, unchanged.
#[cfg(windows)]
fn ensure_console_for_ask(yes: bool, preview: bool) {
    if yes || preview || interactive_terminal() || !win_console::std_streams_absent() {
        return;
    }
    if win_console::attach_parent() {
        return;
    }
    win_console::allocate();
}

/// POSIX: a terminal or a redirected stream is always inherited — nothing
/// to attach or allocate.
#[cfg(not(windows))]
fn ensure_console_for_ask(_yes: bool, _preview: bool) {}

/// Print the preview on stderr and read the y/N answer from stdin.
fn ask(db: &Db, row: &ProjectRow, opts: &UnregisterOptions) -> Result<bool, String> {
    use std::io::Write;
    let preview = tauri::async_runtime::block_on(removal_preview(db, &row.id, opts))?;
    // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
    eprint!("{}Remove {:?} from VCO? [y/N] ", preview.text, row.name);
    let _ = std::io::stderr().flush();
    let mut answer = String::new();
    std::io::stdin()
        .read_line(&mut answer)
        .map_err(|e| format!("could not read the answer: {}", e))?;
    Ok(is_yes(&answer))
}

/// Run the command; returns the process exit code. Prints are the CLI's
/// result (see the print contract in `lib.rs`).
pub(crate) fn run(rest: &[String]) -> i32 {
    let args = match parse_args(rest) {
        Ok(Parsed::Run(a)) => a,
        Ok(Parsed::Help) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            println!("{}", USAGE);
            return 0;
        }
        Err(msg) => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("{}", msg);
            return 1;
        }
    };
    // Review L6 (Windows): a release build started without a console gets
    // one to ask in, before the confirm gate below reads it as "no terminal"
    // and refuses into the void. No-op with --yes / --preview / on POSIX.
    ensure_console_for_ask(args.yes, args.preview);
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
    if args.preview {
        return match tauri::async_runtime::block_on(removal_preview(&db, &row.id, &args.opts)) {
            Ok(p) => {
                // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
                println!("{}", serde_json::to_string_pretty(&p).unwrap_or_else(|_| "{}".to_string()));
                0
            }
            Err(msg) => {
                // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
                eprintln!("[vct] could not list what the removal of {:?} would do: {}", row.name, msg);
                EXIT_FAILED
            }
        };
    }
    match confirm_mode(args.yes, interactive_terminal()) {
        Confirm::Go => {}
        Confirm::Refuse => {
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!(
                "[vct] not removing {:?}: this is not an interactive terminal, so VCO cannot ask \
                 first, and the removal drops the project's collections unless \
                 --keep-collections is given. Nothing was changed. Run it from a terminal to see \
                 what would be removed, or rerun with --yes.",
                row.name
            );
            return EXIT_NOT_CONFIRMED;
        }
        Confirm::Ask => match ask(&db, &row, &args.opts) {
            Ok(true) => {}
            Ok(false) => {
                // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
                eprintln!("[vct] not removed; nothing was changed.");
                return EXIT_NOT_CONFIRMED;
            }
            Err(msg) => {
                // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
                eprintln!("[vct] not removed ({}); nothing was changed.", msg);
                return EXIT_NOT_CONFIRMED;
            }
        },
    }
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
            let (code, text) = failure(&row.name, &msg);
            // [vct-print-contract] CLI output — see handle_cli_args() in lib.rs.
            eprintln!("{}", text);
            code
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

    fn run_args(v: &[&str]) -> RemoveProjectArgs {
        match parse_args(&s(v)).unwrap() {
            Parsed::Run(a) => a,
            Parsed::Help => panic!("expected a removal, got help"),
        }
    }

    #[test]
    fn defaults_match_the_gui_remove_flow_and_flags_narrow_them() {
        let a = run_args(&["alpha"]);
        assert_eq!(a.query, "alpha");
        assert!(a.opts.purge_launcher_files && a.opts.purge_collections);
        assert!(!a.opts.leave_unremovable);
        let b = run_args(&["--keep-collections", "alpha", "--leave-unremovable"]);
        assert!(!b.opts.purge_collections && b.opts.leave_unremovable);
        assert!(parse_args(&s(&[])).is_err());
        assert!(parse_args(&s(&["a", "b"])).is_err());
        assert!(parse_args(&s(&["--drop-everything", "a"])).is_err());
    }

    #[test]
    fn the_defaults_come_from_the_file_the_gui_imports() {
        // Review B-L1: one source. The JSON is what unregister-escape.ts
        // imports as REMOVE_UNREGISTER_OPTIONS; every key it names reaches
        // the CLI's options.
        let raw: serde_json::Value = serde_json::from_str(include_str!(
            "../../src/lib/remove-unregister-options.json"
        ))
        .unwrap();
        let opts = remove_flow_options();
        assert_eq!(raw["purgeLauncherFiles"].as_bool(), Some(opts.purge_launcher_files));
        assert_eq!(raw["purgeCollections"].as_bool(), Some(opts.purge_collections));
        assert!(!opts.leave_unremovable);
    }

    /// Owner ruling 2026-11-11 (review L7): the removal asks unless `--yes`;
    /// with no terminal and no `--yes` it refuses — never a silent drop.
    #[test]
    fn the_removal_asks_unless_yes_and_refuses_without_a_terminal() {
        let a = run_args(&["alpha"]);
        assert!(!a.yes && !a.preview);
        assert!(run_args(&["alpha", "--yes"]).yes);
        assert!(run_args(&["-y", "alpha"]).yes);
        assert!(run_args(&["alpha", "--preview"]).preview);
        assert_eq!(confirm_mode(true, false), Confirm::Go);
        assert_eq!(confirm_mode(true, true), Confirm::Go);
        assert_eq!(confirm_mode(false, true), Confirm::Ask);
        assert_eq!(confirm_mode(false, false), Confirm::Refuse);
    }

    /// Review L6: `ensure_console_for_ask` never changes the confirm
    /// decision's INPUTS — on POSIX it does nothing at all (a terminal or a
    /// redirected stream is always inherited there), and on Windows it
    /// returns at once for --yes / --preview / an existing terminal. The
    /// ask/refuse logic is the one tested above, unchanged.
    #[test]
    fn ensure_console_leaves_the_confirm_decision_alone() {
        ensure_console_for_ask(false, false);
        ensure_console_for_ask(true, true);
        assert_eq!(confirm_mode(false, false), Confirm::Refuse);
        assert_eq!(confirm_mode(false, true), Confirm::Ask);
        assert_eq!(confirm_mode(true, false), Confirm::Go);
    }

    /// Review L5: only the unremovable-value stop names --leave-unremovable
    /// and exits 2; an update holding the database (or any other failure)
    /// exits 3 with its own message.
    #[test]
    fn only_the_stop_names_leave_unremovable() {
        let (code, text) = failure("Alpha", &format!("{} — values left", UNREGISTER_STOPPED_PREFIX));
        assert_eq!(code, EXIT_STOPPED);
        assert!(text.contains("--leave-unremovable"));
        let (code, text) = failure("Alpha", "the launcher database is held by install.py --update");
        assert_eq!(code, EXIT_FAILED);
        assert!(!text.contains("--leave-unremovable") && text.contains("held by install.py"));
    }

    #[test]
    fn help_is_not_a_usage_error() {
        assert_eq!(parse_args(&s(&["--help"])).unwrap(), Parsed::Help);
        assert_eq!(parse_args(&s(&["-h", "alpha"])).unwrap(), Parsed::Help);
        assert_eq!(run(&s(&["--help"])), 0);
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
