// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106 (review L7 + owner ruling 2026-11-11): what "Remove from VCO"
//! WOULD remove, for the terminal's "are you sure?".
//!
//! `vct-launcher --remove-project` and `vct-cli project delete` remove VCO's
//! files and, by default, drop the project's collections — a script that
//! used delete + create to re-register a project would lose its knowledge
//! graph and code graph without a word. Both now print this preview and ask
//! y/N first (or need `--yes`). The preview runs the SAME two decisions the
//! removal runs, each in its dry-run form, so it can never describe a
//! different removal: `vco_lib.unregister_files purge --dry-run` (by the
//! manifest evidence) and `vco_lib.project_init drop-collections --dry-run`
//! (after every keep rule). Nothing is changed.

use std::path::Path;

use super::{drop_owned_collections, file_purge_from_reply, UnregisterOptions};
use crate::db::Db;

/// What a removal with these options would do, names only.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub(crate) struct RemovalPreview {
    pub project_id: String,
    pub project_name: String,
    pub folder: String,
    /// Files and folders VCO would remove (proven to be VCO's).
    pub files: Vec<String>,
    /// Files VCO shipped that the user changed: they would stay.
    pub files_kept_modified: Vec<String>,
    /// `<rel> — <reason>` for what VCO would leave in place (its backups, a
    /// linked folder, a setting a user may also have chosen) — review L8:
    /// the preview used to drop these reasons.
    pub kept: Vec<String>,
    /// Files whose VCO entries would be taken out (the files stay).
    pub settings_entries: Vec<String>,
    /// The project's own collections that would be dropped.
    pub collections: Vec<String>,
    /// `false` when `--keep-collections` was given.
    pub drops_collections: bool,
    /// What the preview could not check, and collections it would keep.
    pub notes: Vec<String>,
    /// The human-readable form both terminal surfaces print.
    pub text: String,
}

/// Build the preview (see the module doc). `Err` only when the project row
/// cannot be read.
pub(crate) async fn removal_preview(
    db: &Db,
    id: &str,
    opts: &UnregisterOptions,
) -> Result<RemovalPreview, String> {
    db.ensure_live()?;
    let row = db
        .get_project(id)?
        .ok_or_else(|| format!("project {} not found", id))?;
    let mut p = RemovalPreview {
        project_id: row.id.clone(),
        project_name: row.name.clone(),
        folder: row.folder_path.clone(),
        drops_collections: opts.purge_collections,
        ..Default::default()
    };
    if opts.purge_launcher_files {
        let folder = Path::new(&row.folder_path);
        if folder.is_dir() {
            let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(db);
            match crate::services::vco_lib_bridge::preview_vco_project_files(root.as_deref(), folder) {
                Ok(reply) => {
                    let fp = file_purge_from_reply(&reply);
                    p.files = fp.removed;
                    p.files_kept_modified = fp.kept_modified;
                    p.kept = fp.kept;
                    p.settings_entries = fp.settings_removed;
                    p.notes.extend(fp.warnings);
                }
                Err(e) => p.notes.push(format!(
                    "could not list which of the folder's files are VCO's ({}); the removal \
                     takes out only what it can prove is VCO's",
                    e
                )),
            }
            p.settings_entries
                .push("the lines VCO wrote to the project's env files (.env, .claude/env)".to_string());
        } else {
            p.notes.push(format!(
                "the folder {} no longer exists: only the registration goes",
                row.folder_path
            ));
        }
        // Review L8: the removal also takes the per-project MCP opt-outs VCO
        // wrote to ~/.claude.json — the preview names them (read before the
        // row goes, exactly as the removal does).
        match crate::commands::project_state_cmd::disabled_mcp_names(db, id) {
            Ok(names) if !names.is_empty() => p.settings_entries.push(format!(
                "the MCP opt-outs VCO wrote to ~/.claude.json: {}",
                names.join(", ")
            )),
            Ok(_) => {}
            Err(e) => p.notes.push(format!(
                "could not read which MCP servers VCO disabled for this project: {}",
                e
            )),
        }
    }
    if opts.purge_collections {
        let (names, notes) = drop_owned_collections(&row.name, &row.id, true).await;
        p.collections = names;
        p.notes.extend(notes);
    }
    p.text = render(&p);
    Ok(p)
}

fn list(out: &mut String, title: &str, items: &[String]) {
    if items.is_empty() {
        return;
    }
    out.push_str(title);
    out.push('\n');
    for i in items {
        out.push_str("  - ");
        out.push_str(i);
        out.push('\n');
    }
}

/// The text the terminal prints above the y/N question.
pub(crate) fn render(p: &RemovalPreview) -> String {
    let mut out = format!(
        "Remove {:?} from VCO. The folder {} stays, with your own files (.env, secrets, \
         settings, source, knowledge/).\n",
        p.project_name, p.folder
    );
    list(&mut out, "These files and folders VCO put there would be removed:", &p.files);
    list(&mut out, "VCO's entries would be taken out of:", &p.settings_entries);
    // Review L8: the purge's kept reasons belong in the preview too.
    list(&mut out, "These would stay, and why:", &p.kept);
    // Review L8: a kept copy can be your edit OR an older VCO version that
    // no template or git history proves VCO's (a parked copy on an install
    // without history) — the label names both.
    list(
        &mut out,
        "These you changed — or an older VCO version shipped — would stay:",
        &p.files_kept_modified,
    );
    if p.drops_collections {
        if p.collections.is_empty() {
            out.push_str("No Weaviate collection would be dropped.\n");
        } else {
            list(
                &mut out,
                "These Weaviate collections would be DROPPED (their data is deleted; \
                 --keep-collections keeps them):",
                &p.collections,
            );
        }
    } else {
        out.push_str("The project's Weaviate collections are kept (--keep-collections).\n");
    }
    list(&mut out, "Notes:", &p.notes);
    out
}

/// The answer to "Remove …? [y/N]": only an explicit yes removes.
pub(crate) fn is_yes(answer: &str) -> bool {
    matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_an_explicit_yes_confirms() {
        for yes in ["y", "Y", "yes", " YES \n"] {
            assert!(is_yes(yes), "{:?}", yes);
        }
        for no in ["", "\n", "n", "no", "yess", "sure"] {
            assert!(!is_yes(no), "{:?}", no);
        }
    }

    #[test]
    fn the_text_names_the_files_and_the_collections_it_would_drop() {
        let p = RemovalPreview {
            project_name: "Alpha".into(),
            folder: "/w/alpha".into(),
            files: vec![".claude/hooks/pre-tool-use.sh".into()],
            settings_entries: vec!["VCO's entries in .claude/settings.json".into()],
            files_kept_modified: vec![".claude/agents/planner.md".into()],
            kept: vec![".claude/backups/ — VCO's update backups hold your own earlier \
                        versions of files"
                .into()],
            collections: vec!["Alpha_KnowledgeGraph".into(), "Alpha_CodeFunction".into()],
            drops_collections: true,
            ..Default::default()
        };
        let text = render(&p);
        assert!(text.contains("/w/alpha stays"));
        assert!(text.contains(".claude/hooks/pre-tool-use.sh"));
        assert!(text.contains("DROPPED") && text.contains("Alpha_KnowledgeGraph"));
        // Review L8: the kept side is named with both reasons, never just
        // "you changed".
        assert!(text.contains("you changed — or an older VCO version shipped"));
        assert!(text.contains("These would stay, and why:") && text.contains(".claude/backups/"));
        let kept = render(&RemovalPreview { drops_collections: false, ..p });
        assert!(kept.contains("collections are kept") && !kept.contains("DROPPED"));
    }
}
