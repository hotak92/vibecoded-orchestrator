// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Parked-entry store for hooks the launcher has removed from a project's
//! `.claude/settings.json` (v0.2.91, decision #27).
//!
//! ## What changed, and what this table now means
//!
//! Until v0.2.91 the Hooks tab was a full placebo: `register_project_hook` /
//! `set_project_hook_enabled` / `unregister_project_hook` wrote `project_hooks`
//! rows that NOTHING read. Claude Code's hook engine reads
//! `<project>/.claude/settings.json` directly, so unchecking a hook never
//! stopped it firing and registering one never made it fire (evidence:
//! `.claude/context/reviews/v0291-wave5-phase2-ux-completeness` P2-B2 — zero
//! Python readers of `project_hooks`; `apply_fs_disable_hook` never existed
//! while `apply_fs_disable_agent` / `apply_fs_disable_skill` did).
//!
//! Enforcement is now an actual edit to `settings.json`, performed by the ONE
//! writer `python -m vco_lib.hooks_settings`. Disabling a hook REMOVES its
//! entry from that file — so the removed entry needs a home, and this module
//! is it.
//!
//! ## The invariant
//!
//! **`settings.json` is the truth about what runs. `project_hooks` is a mirror
//! plus this parked-entry store, and never an authority.** Concretely:
//!
//! * `disabled_entry_json IS NOT NULL` — VCO holds this entry OUT of
//!   settings.json and can restore it verbatim. This is the state the Hooks
//!   tab renders as unchecked.
//! * `disabled_entry_json IS NULL` — nothing parked. Whether the hook runs is
//!   a question only settings.json can answer.
//! * The legacy `enabled` column is kept in sync for the benefit of existing
//!   readers (the hub's `/project-state` + CLI routes), but it is an advisory
//!   mirror flag, not an enforcement gate — it never was one.
//!
//! Rows are keyed by the natural key `(project_id, event, matcher, command)`,
//! which is both the table's UNIQUE constraint and the identity
//! `vco_lib.hooks_settings` uses inside settings.json — so the two sides
//! cannot disagree about which entry is which. Identity by `id` would not
//! work: a hook can exist in settings.json with no mirror row at all (a user
//! edited the file by hand and never re-scanned).

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// A hook whose settings.json entry the launcher currently holds parked.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParkedHook {
    pub id: i64,
    pub event: String,
    pub matcher: String,
    pub command: String,
    pub source: String,
    pub source_module: Option<String>,
    pub timeout_ms: Option<i64>,
    /// The parked entry exactly as `vco_lib.hooks_settings` produced it
    /// (schema 1). Opaque to Rust: it is handed straight back to the Python
    /// writer on re-enable, so nothing here needs to understand its shape —
    /// and nothing here should reshape it.
    pub disabled_entry_json: String,
}

impl Db {
    /// Park `entry_json` on the hook row for this natural key, creating the
    /// row when the launcher has never mirrored this hook (the hand-edited
    /// settings.json case). Also flips the legacy `enabled` mirror to 0.
    ///
    /// Called only AFTER `vco_lib.hooks_settings disable` has successfully
    /// removed the entry from settings.json — so a failed file edit can never
    /// leave a parked row claiming a removal that did not happen.
    pub fn park_project_hook_entry(
        &self,
        project_id: &str,
        event: &str,
        matcher: &str,
        command: &str,
        entry_json: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_hooks
                    SET disabled_entry_json = ?5, enabled = 0, updated_at = ?6
                  WHERE project_id = ?1 AND event = ?2 AND matcher = ?3
                    AND command = ?4",
                params![project_id, event, matcher, command, entry_json, now],
            )
            .map_err(|e| format!("park_project_hook_entry (update): {}", e))?;
        if n > 0 {
            return Ok(());
        }
        // No mirror row yet — the hook existed in settings.json but the
        // launcher had never scanned it. Materialise one so the disabled
        // state (and its parked entry) survives.
        guard
            .execute(
                "INSERT INTO project_hooks
                 (project_id, event, matcher, command, source, source_module,
                  enabled, timeout_ms, config_json, disabled_entry_json,
                  installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 'project', NULL, 0, NULL, '{}', ?5,
                         ?6, ?6)",
                params![project_id, event, matcher, command, entry_json, now],
            )
            .map_err(|e| format!("park_project_hook_entry (insert): {}", e))?;
        Ok(())
    }

    /// Read back a parked entry, or `None` when this hook is not parked.
    pub fn get_parked_project_hook_entry(
        &self,
        project_id: &str,
        event: &str,
        matcher: &str,
        command: &str,
    ) -> Result<Option<String>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT disabled_entry_json FROM project_hooks
                  WHERE project_id = ?1 AND event = ?2 AND matcher = ?3
                    AND command = ?4",
                params![project_id, event, matcher, command],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()
            .map_err(|e| format!("get_parked_project_hook_entry: {}", e))
            .map(Option::flatten)
    }

    /// Clear the parked entry (the hook is back in settings.json) and flip the
    /// legacy `enabled` mirror to 1.
    pub fn unpark_project_hook_entry(
        &self,
        project_id: &str,
        event: &str,
        matcher: &str,
        command: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "UPDATE project_hooks
                    SET disabled_entry_json = NULL, enabled = 1, updated_at = ?5
                  WHERE project_id = ?1 AND event = ?2 AND matcher = ?3
                    AND command = ?4",
                params![project_id, event, matcher, command, now],
            )
            .map_err(|e| format!("unpark_project_hook_entry: {}", e))?;
        Ok(())
    }

    /// Every hook this project currently has parked.
    ///
    /// These are exactly the hooks that are ABSENT from settings.json by the
    /// launcher's doing — the effective-hooks view renders them as disabled,
    /// and they are the only rows for which a re-enable is possible.
    pub fn list_parked_project_hooks(&self, project_id: &str) -> Result<Vec<ParkedHook>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, event, matcher, command, source, source_module,
                        timeout_ms, disabled_entry_json
                   FROM project_hooks
                  WHERE project_id = ?1 AND disabled_entry_json IS NOT NULL
                  ORDER BY event ASC, matcher ASC, id ASC",
            )
            .map_err(|e| format!("list_parked_project_hooks (prepare): {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                Ok(ParkedHook {
                    id: r.get(0)?,
                    event: r.get(1)?,
                    matcher: r.get(2)?,
                    command: r.get(3)?,
                    source: r.get(4)?,
                    source_module: r.get(5)?,
                    timeout_ms: r.get(6)?,
                    disabled_entry_json: r.get(7)?,
                })
            })
            .map_err(|e| format!("list_parked_project_hooks (query): {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("list_parked_project_hooks (collect): {}", e))
    }

    /// Drop the mirror row for a hook the user unregistered.
    ///
    /// Keyed by the natural key rather than `id` so it works for a hook that
    /// lives in settings.json but was never mirrored (deleting nothing is the
    /// correct outcome there). NEVER touches the hook SCRIPT file — removing
    /// the wiring is not removing the user's code.
    pub fn delete_project_hook_by_key(
        &self,
        project_id: &str,
        event: &str,
        matcher: &str,
        command: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_hooks
                  WHERE project_id = ?1 AND event = ?2 AND matcher = ?3
                    AND command = ?4",
                params![project_id, event, matcher, command],
            )
            .map_err(|e| format!("delete_project_hook_by_key: {}", e))?;
        Ok(())
    }
}

/// Build the user-facing message for a `park_project_hook_entry` failure.
///
/// `disable_hook` (launcher) and `enforce_hook_toggle`'s disable arm (hub)
/// both remove the settings.json entry FIRST, then park it — the correct
/// ordering (a failed file edit never leaves a parked row claiming a removal
/// that did not happen). But that ordering has a failure branch of its own:
/// if the park write fails, the entry is already gone from settings.json AND
/// was never recorded in the database, so `parked_json` — the writer's own
/// serialisation of the removed entry — existed only in the CLI result and
/// would otherwise be silently discarded by the error path, losing the
/// user's data with no way back. Surfacing it verbatim in the error message
/// is the cheapest possible recovery: paste it back under the matching event
/// in `.claude/settings.json` by hand.
///
/// Shared between the two Rust callers (`vct_launcher_core::db` is a common
/// dependency of both the launcher and the hub crates) so the recovery copy
/// cannot drift between them.
pub fn park_failure_message(parked_json: &str, db_error: &str) -> String {
    format!(
        "VCO removed the entry but could not park it — restore it by hand: \
         {parked_json}\n(database error: {db_error})"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn seed_project(db: &Db, id: &str) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at)
                 VALUES (?1, ?1, ?2, 'base', 0, 0)",
                params![id, format!("/tmp/vct-hooks-test-{}", id)],
            )
            .unwrap();
    }

    fn db_with_project() -> Db {
        let db = make_db();
        seed_project(&db, "p1");
        db
    }

    const PARKED: &str = r#"{"schema":1,"event":"Stop","matcher":"","item":{"command":"x"}}"#;

    #[test]
    fn park_creates_a_row_when_the_hook_was_never_mirrored() {
        let db = db_with_project();
        db.park_project_hook_entry("p1", "Stop", "", "bash .claude/hooks/a.sh", PARKED)
            .unwrap();

        let parked = db.list_parked_project_hooks("p1").unwrap();
        assert_eq!(parked.len(), 1);
        assert_eq!(parked[0].command, "bash .claude/hooks/a.sh");
        assert_eq!(parked[0].disabled_entry_json, PARKED);
        // The legacy mirror flag follows.
        let hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(hooks.len(), 1);
        assert!(!hooks[0].enabled);
    }

    #[test]
    fn park_updates_an_existing_mirror_row_rather_than_duplicating_it() {
        let db = db_with_project();
        db.register_project_hook(
            "p1",
            "Stop",
            "",
            "bash .claude/hooks/a.sh",
            "project",
            None,
            Some(5000),
            &serde_json::json!({}),
        )
        .unwrap();
        db.park_project_hook_entry("p1", "Stop", "", "bash .claude/hooks/a.sh", PARKED)
            .unwrap();

        assert_eq!(db.list_project_hooks("p1").unwrap().len(), 1, "no duplicate row");
        let parked = db.list_parked_project_hooks("p1").unwrap();
        assert_eq!(parked.len(), 1);
        assert_eq!(parked[0].timeout_ms, Some(5000), "metadata preserved");
    }

    #[test]
    fn unpark_clears_the_entry_and_restores_the_mirror_flag() {
        let db = db_with_project();
        db.park_project_hook_entry("p1", "Stop", "", "cmd", PARKED).unwrap();
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "Stop", "", "cmd").unwrap(),
            Some(PARKED.to_string())
        );

        db.unpark_project_hook_entry("p1", "Stop", "", "cmd").unwrap();
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "Stop", "", "cmd").unwrap(),
            None
        );
        assert!(db.list_parked_project_hooks("p1").unwrap().is_empty());
        assert!(db.list_project_hooks("p1").unwrap()[0].enabled);
    }

    #[test]
    fn get_parked_distinguishes_missing_row_from_unparked_row() {
        let db = db_with_project();
        // Leave-alone case: no row at all.
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "Stop", "", "nope").unwrap(),
            None
        );
        // Row exists but nothing parked — still None, and no error.
        db.register_project_hook(
            "p1",
            "Stop",
            "",
            "cmd",
            "project",
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "Stop", "", "cmd").unwrap(),
            None
        );
    }

    #[test]
    fn matcher_is_part_of_the_identity() {
        let db = db_with_project();
        db.park_project_hook_entry("p1", "PostToolUse", "Edit(*)", "cmd", PARKED)
            .unwrap();
        // Same event + command, DIFFERENT matcher → a different hook.
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "PostToolUse", "Write(*)", "cmd")
                .unwrap(),
            None
        );
        assert_eq!(
            db.get_parked_project_hook_entry("p1", "PostToolUse", "Edit(*)", "cmd")
                .unwrap(),
            Some(PARKED.to_string())
        );
    }

    #[test]
    fn delete_by_key_removes_only_the_named_hook() {
        let db = db_with_project();
        for cmd in ["a", "b"] {
            db.register_project_hook(
                "p1",
                "Stop",
                "",
                cmd,
                "project",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();
        }
        db.delete_project_hook_by_key("p1", "Stop", "", "a").unwrap();
        let left: Vec<String> = db
            .list_project_hooks("p1")
            .unwrap()
            .into_iter()
            .map(|h| h.command)
            .collect();
        assert_eq!(left, vec!["b".to_string()]);
    }

    #[test]
    fn delete_by_key_for_an_unmirrored_hook_is_a_no_op_not_an_error() {
        let db = db_with_project();
        db.delete_project_hook_by_key("p1", "Stop", "", "never-mirrored")
            .expect("must not error");
        assert!(db.list_project_hooks("p1").unwrap().is_empty());
    }

    #[test]
    fn parked_entries_are_scoped_to_their_project() {
        let db = db_with_project();
        seed_project(&db, "p2");
        db.park_project_hook_entry("p1", "Stop", "", "cmd", PARKED).unwrap();
        assert_eq!(db.list_parked_project_hooks("p2").unwrap().len(), 0);
        assert_eq!(db.list_parked_project_hooks("p1").unwrap().len(), 1);
    }

    // ── park_failure_message (wave-5 review MINOR-5) ──────────────────────
    //
    // Act case only, per the review's fix plan — the message-formatting
    // path is a pure function, independently testable without inducing a
    // real DB failure. The destructive operation itself (removing the
    // settings.json entry) is unchanged; this only pins the recovery text
    // shown when the FOLLOW-UP park write fails.

    #[test]
    fn park_failure_message_includes_the_parked_json_verbatim() {
        let msg = park_failure_message(PARKED, "disk full");
        assert!(
            msg.contains(PARKED),
            "the parked entry must be recoverable from the error message \
             alone — got: {msg}"
        );
    }

    #[test]
    fn park_failure_message_includes_the_underlying_db_error() {
        let msg = park_failure_message(PARKED, "disk full");
        assert!(
            msg.contains("disk full"),
            "the underlying db error must still be visible for debugging — got: {msg}"
        );
    }

    #[test]
    fn park_failure_message_names_the_recovery_action() {
        let msg = park_failure_message(PARKED, "disk full");
        assert!(
            msg.to_lowercase().contains("restore it by hand"),
            "the message must tell the user this is recoverable and how — got: {msg}"
        );
    }
}
