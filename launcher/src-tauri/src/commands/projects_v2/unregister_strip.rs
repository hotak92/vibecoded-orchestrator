//! Unregister-flow launcher-file purge.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the launcher-artifact filesystem
//! purge (`purge_launcher_files_from_project`) that previously lived inline in
//! `projects_v2.rs`. The caller-supplied-key-set NAME strippers that also
//! lived here (`strip_named_keys_from_{env_text,claude_env_text,env_object}`)
//! were retired in v0.2.97: unregister removes a secret value only on value
//! evidence (`projects_v2::strip_proven_secret_values`), never by name. The
//! facade re-exports every symbol. `UNREGISTER_PURGE_PATHS` stays in the
//! facade (shared with the
//! unregister command surface) and is pulled in via `super::`.
//!
//! v0.2.97 (review R4 F25): the unregister's step 1 — env strip, stop
//! decision, file purge — lives here as `unregister_purge_folder`, so the
//! order (never purge files, forget secret rows or delete the row over a
//! proven value that is still on disk) is testable without a Tauri state.

use std::path::Path;

use super::{UnregisterReport, UNREGISTER_PURGE_PATHS};

/// Surgically remove every entry in `UNREGISTER_PURGE_PATHS` from
/// `<folder>/`. Returns `(relative_paths_removed, warnings)`.
///
/// Soft-fail discipline: per-path failures (permission denied, ENOENT
/// race, etc.) land in `warnings`; the next path is still attempted.
/// ENOENT is silent — a missing path on a folder that never had the
/// bundle installed is the expected case for legacy projects, not a
/// warning condition.
///
/// Note: this is the FILE / DIRECTORY purge. The env-surface strip
/// runs separately via `surgically_strip_env_surfaces` so that surfaces
/// containing user-added keys can be partially preserved.
pub(crate) fn purge_launcher_files_from_project(
    folder: &Path,
) -> (Vec<String>, Vec<String>) {
    let mut purged: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();

    for rel in UNREGISTER_PURGE_PATHS {
        let target = folder.join(rel);
        if !target.exists() {
            continue; // silent skip
        }
        let meta = match std::fs::symlink_metadata(&target) {
            Ok(m) => m,
            Err(e) => {
                warnings.push(format!(
                    "could not stat {} for unregister purge: {}",
                    target.display(), e
                ));
                continue;
            }
        };

        let result = if meta.is_dir() {
            std::fs::remove_dir_all(&target)
        } else {
            std::fs::remove_file(&target)
        };

        match result {
            Ok(()) => purged.push((*rel).to_string()),
            Err(e) => warnings.push(format!(
                "could not remove {}: {}", target.display(), e
            )),
        }
    }

    (purged, warnings)
}


/// The unregister's stop decision after the env strip (review R4 F25): `Ok`
/// when every proven secret value was removed; otherwise `Err` with the
/// message the GUI shows. Once the project's secret rows are forgotten and
/// its row deleted, a stored value can never be looked up again, so a value
/// VCO wrote but could not remove would be stranded for good — stopping
/// BEFORE the file purge, the forgotten rows and the DB delete keeps it
/// removable, and a retry after fixing the cause completes the unregister
/// (every step is idempotent).
pub(crate) fn unregister_may_continue(not_removed: &[String], warnings: &[String]) -> Result<(), String> {
    if not_removed.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} — the project is still registered (its routing keys were \
         already stripped; any env refresh restores them). These values VCO wrote \
         could not be removed: {}. {} Fix the cause (a read-only folder, a full disk, \
         a file another program holds) and unregister again; stopping keeps them \
         removable, because once the project is gone VCO can no longer check them. \
         If you cannot fix it, choose \"Unregister anyway — leave these values\": \
         VCO finishes the unregister and writes a note listing each key and file to \
         clean by hand (names only, never a value).",
        UNREGISTER_STOPPED_PREFIX,
        not_removed.join("; "),
        warnings.join(" ")
    ))
}

/// How the stop error opens — the GUI offers "Unregister anyway — leave these
/// values" on exactly this (owner ruling, review R5 F39). MUST MATCH
/// `UNREGISTER_STOPPED_PREFIX` in `launcher/src/lib/unregister-escape.ts`
/// (pinned there by `unregister-escape.test.ts`, here by
/// `the_stop_error_opens_with_the_prefix_the_gui_keys_on`).
pub(crate) const UNREGISTER_STOPPED_PREFIX: &str = "Unregister stopped";

/// The note an "Unregister anyway" leaves: `<folder>/.claude/<this>`, or —
/// when that cannot be written (the very read-only folder that stopped the
/// unregister) — `<vct_root_dir>/unregister-leftovers/<project_id>.md`.
pub(crate) const LEFTOVERS_NOTE_NAME: &str = "VCO-UNREGISTER-LEFTOVERS.md";

/// The note's text: key NAMES and files (each entry is `KEY in <file>`) —
/// never a value.
pub(crate) fn leftovers_note_text(project_name: &str, project_id: &str, left: &[String]) -> String {
    let mut text = format!(
        "## {} — unregistered with \"leave these values\"\n\n\
         The project \"{}\" (id `{}`) was unregistered although VCO could not remove \
         these values it had written. Remove each line by hand — the values are not \
         listed here:\n\n",
        chrono::Utc::now().format("%Y-%m-%d %H:%M UTC"),
        project_name,
        project_id,
    );
    for entry in left {
        text.push_str(&format!("- {}\n", entry));
    }
    text.push('\n');
    text
}

/// Append the note (a re-run adds a dated section; nothing is overwritten)
/// to the project's `.claude/`, else to the launcher's own state dir.
/// Returns the path written.
pub(crate) fn write_leftovers_note(
    folder: &Path,
    fallback_dir: &Path,
    project_id: &str,
    text: &str,
) -> Result<std::path::PathBuf, String> {
    use std::io::Write as _;
    let append = |path: &Path| -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut file = std::fs::OpenOptions::new().create(true).append(true).open(path)?;
        file.write_all(text.as_bytes())
    };
    let primary = folder.join(".claude").join(LEFTOVERS_NOTE_NAME);
    match append(&primary) {
        Ok(()) => Ok(primary),
        Err(first) => {
            let fallback = fallback_dir.join("unregister-leftovers").join(format!("{}.md", project_id));
            append(&fallback).map(|_| fallback.clone()).map_err(|second| {
                format!(
                    "could not write the leftovers note to {} ({}) nor to {} ({})",
                    primary.display(), first, fallback.display(), second
                )
            })
        }
    }
}

/// Step 1 of `delete_project_v2` for an existing folder: `strip` removes
/// the launcher's env keys and proven secret values (returning `(keys,
/// warnings, not_removed)` — production passes
/// `surgically_strip_env_surfaces_checked`), then the launcher files are
/// purged. `Err` — the command's return — when a proven value could not be
/// removed: nothing is purged and `report` is untouched, so the caller stops
/// before forgetting secret rows or deleting the project row.
///
/// `leave_unremovable` (owner ruling, review R5 F39) is the explicit escape
/// from that stop — "Unregister anyway — leave these values": the unregister
/// finishes, the unremovable entries go to `report.left_in_place`, and a note
/// listing them (names and files only) is written AFTER the purge, to
/// `<folder>/.claude/VCO-UNREGISTER-LEFTOVERS.md` or, when that is not
/// writable, under `fallback_dir` (the launcher's state dir); its path is in
/// `report.leftovers_note` and a warning. The stop stays the default.
pub(crate) fn unregister_purge_folder(
    folder: &Path,
    strip: impl FnOnce(&Path) -> (Vec<String>, Vec<String>, Vec<String>),
    report: &mut UnregisterReport,
    leave_unremovable: bool,
    fallback_dir: &Path,
) -> Result<(), String> {
    let (keys, env_warnings, not_removed) = strip(folder);
    if !leave_unremovable {
        unregister_may_continue(&not_removed, &env_warnings)?;
    }
    for k in keys {
        if !report.keys_purged_from_env.contains(&k) {
            report.keys_purged_from_env.push(k);
        }
    }
    report.warnings.extend(env_warnings);
    let (files, file_warnings) = purge_launcher_files_from_project(folder);
    report.files_purged = files;
    report.warnings.extend(file_warnings);
    if !not_removed.is_empty() {
        let text = leftovers_note_text(&report.project_name, &report.project_id, &not_removed);
        match write_leftovers_note(folder, fallback_dir, &report.project_id, &text) {
            Ok(path) => {
                report.warnings.push(format!(
                    "Unregistered anyway: {} value(s) VCO wrote were left in place ({}). \
                     The keys and files to clean by hand are listed in {}.",
                    not_removed.len(),
                    not_removed.join("; "),
                    path.display()
                ));
                report.leftovers_note = Some(path.display().to_string());
            }
            Err(e) => report.warnings.push(format!(
                "Unregistered anyway, leaving: {}. {}",
                not_removed.join("; "),
                e
            )),
        }
        report.left_in_place = not_removed;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder_with_a_manifest() -> (tempfile::TempDir, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let manifest = tmp.path().join(".claude/.vco-manifest.json");
        std::fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        std::fs::write(&manifest, "{}").unwrap();
        (tmp, manifest)
    }

    /// Act: every proven value removed ⇒ the keys are reported and the
    /// launcher files purged.
    #[test]
    fn the_unregister_purges_once_every_proven_value_is_gone() {
        let (tmp, manifest) = folder_with_a_manifest();
        let mut report = UnregisterReport::default();
        let strip = |_: &Path| (vec!["KG_COLLECTION".to_string()], vec!["w".to_string()], Vec::new());
        unregister_purge_folder(tmp.path(), strip, &mut report, false, tmp.path()).unwrap();
        assert!(!manifest.exists());
        assert_eq!(report.files_purged, vec![".claude/.vco-manifest.json".to_string()]);
        assert_eq!(report.keys_purged_from_env, vec!["KG_COLLECTION".to_string()]);
        assert_eq!(report.warnings, vec!["w".to_string()]);
    }

    /// Leave alone (review R4 F25): a proven value still on disk stops the
    /// unregister BEFORE the file purge, naming key and file (never a value).
    #[test]
    fn the_unregister_stops_on_a_proven_value_it_could_not_remove() {
        let (tmp, manifest) = folder_with_a_manifest();
        let reply = serde_json::json!({
            "removed": {".env": ["A"]},
            "not_removed": {".claude/env": ["OPENAI_API_KEY"]},
            "errors": ["could not rewrite /p/.claude/env: Permission denied"]
        });
        let not_removed = super::super::not_removed_secret_values(&reply);
        assert_eq!(not_removed, vec!["OPENAI_API_KEY in .claude/env".to_string()]);
        let mut report = UnregisterReport::default();
        let strip = |_: &Path| {
            (vec!["A".to_string()], vec!["could not rewrite /p/.claude/env".to_string()], not_removed)
        };
        let msg = unregister_purge_folder(tmp.path(), strip, &mut report, false, tmp.path())
            .expect_err("a stranded proven value must stop the unregister");
        assert!(manifest.exists(), "no file is purged before the stop");
        assert!(report.files_purged.is_empty() && report.keys_purged_from_env.is_empty());
        assert!(msg.starts_with("Unregister stopped — the project is still registered"), "{}", msg);
        assert!(msg.contains("OPENAI_API_KEY in .claude/env") && msg.contains("could not rewrite"), "{}", msg);
        assert!(msg.contains("Unregister anyway — leave these values"), "the escape is named: {}", msg);
        assert!(!tmp.path().join(".claude").join(LEFTOVERS_NOTE_NAME).exists());
    }

    #[test]
    fn the_stop_error_opens_with_the_prefix_the_gui_keys_on() {
        let msg = unregister_may_continue(&["A in .env".to_string()], &[]).unwrap_err();
        assert!(msg.starts_with(UNREGISTER_STOPPED_PREFIX), "{}", msg);
        assert_eq!(UNREGISTER_STOPPED_PREFIX, "Unregister stopped");
    }

    /// Owner ruling F39: with the escape the unregister FINISHES, and the note
    /// names the key and file — never the value still on disk.
    #[test]
    fn unregister_anyway_finishes_and_leaves_a_names_only_note() {
        let (tmp, manifest) = folder_with_a_manifest();
        let canary = "sk-canary-not-a-real-key-5e0d";
        std::fs::write(tmp.path().join(".env"), format!("OPENAI_API_KEY={}\n", canary)).unwrap();
        let mut report = UnregisterReport {
            project_id: "pid-1".into(),
            project_name: "Gamma".into(),
            ..Default::default()
        };
        let strip = |_: &Path| (vec!["KG_COLLECTION".to_string()], vec![], vec!["OPENAI_API_KEY in .env".to_string()]);
        let state = tempfile::tempdir().unwrap();
        unregister_purge_folder(tmp.path(), strip, &mut report, true, state.path()).unwrap();

        assert!(!manifest.exists(), "the purge ran");
        assert_eq!(report.left_in_place, vec!["OPENAI_API_KEY in .env".to_string()]);
        let note_path = tmp.path().join(".claude").join(LEFTOVERS_NOTE_NAME);
        assert_eq!(report.leftovers_note.as_deref(), Some(note_path.display().to_string().as_str()));
        let note = std::fs::read_to_string(&note_path).unwrap();
        assert!(note.contains("- OPENAI_API_KEY in .env") && note.contains("Gamma"), "{}", note);
        assert!(!note.contains(canary));
        assert!(report.warnings.iter().all(|w| !w.contains(canary)));
        assert!(report.warnings.iter().any(|w| w.contains(&note_path.display().to_string())));
    }

    /// The note falls back to the launcher's state dir when the project's
    /// `.claude/` cannot be written — and says where it went.
    #[cfg(unix)]
    #[test]
    fn the_note_falls_back_to_the_state_dir_when_claude_is_unwritable() {
        use std::os::unix::fs::PermissionsExt;
        let (tmp, _manifest) = folder_with_a_manifest();
        let claude = tmp.path().join(".claude");
        // A FILE where the note would go makes the primary write fail even
        // for a privileged test runner (permissions alone would not).
        std::fs::create_dir_all(claude.join(LEFTOVERS_NOTE_NAME)).unwrap();
        std::fs::set_permissions(&claude, std::fs::Permissions::from_mode(0o755)).unwrap();
        let state = tempfile::tempdir().unwrap();
        let text = leftovers_note_text("Gamma", "pid-2", &["GITHUB_TOKEN in .claude/settings.json".to_string()]);
        let path = write_leftovers_note(tmp.path(), state.path(), "pid-2", &text).unwrap();
        assert_eq!(path, state.path().join("unregister-leftovers").join("pid-2.md"));
        assert!(std::fs::read_to_string(&path).unwrap().contains("- GITHUB_TOKEN in .claude/settings.json"));
    }
}
