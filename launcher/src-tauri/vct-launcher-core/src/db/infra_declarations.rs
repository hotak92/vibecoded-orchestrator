// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Per-project infrastructure declarations (v0.2.102 W5-A / M3, migration 049).
//!
//! What on this machine belongs to a PROJECT, not to VCO: a Weaviate class it
//! owns (`external_collection`), a container VCO must never heal, re-create,
//! label or adopt (`unmanaged_container`), a loopback port of one of its own
//! services (`local_port`). Schema and the per-kind effects:
//! `migrations/049_project_infra_declarations.sql`.
//!
//! Writers: the launcher's Identity tab (`commands::infra_declarations`).
//! Readers: this module (the hub watchdog — `unmanaged_container_reasons`; the
//! hub `/config` route — `list_infra_declarations`) and, on the Python side,
//! `vco_lib/infra_declarations.py` (the doctor, compose recovery, service
//! detection, the env projection).
//!
//! Validation is [`normalise`] — MUST MATCH
//! `vco_lib/infra_declarations.py::normalise` (pinned by the parity case
//! table in `tests/test_v02102_infra_boundary.py`, which runs the same inputs
//! through both).

use std::collections::BTreeMap;

use rusqlite::params;
use serde::{Deserialize, Serialize};

use super::Db;

pub const KIND_EXTERNAL_COLLECTION: &str = "external_collection";
pub const KIND_UNMANAGED_CONTAINER: &str = "unmanaged_container";
pub const KIND_LOCAL_PORT: &str = "local_port";
/// Must match the CHECK in migration 049 and `vco_lib.infra_declarations.KINDS`.
pub const KINDS: [&str; 3] = [KIND_EXTERNAL_COLLECTION, KIND_UNMANAGED_CONTAINER, KIND_LOCAL_PORT];

/// One project's declarations, grouped by kind (each list sorted).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct InfraDeclarations {
    pub external_collections: Vec<String>,
    pub unmanaged_containers: Vec<String>,
    pub local_ports: Vec<String>,
}

impl InfraDeclarations {
    fn push(&mut self, kind: &str, value: String) {
        let list = match kind {
            KIND_EXTERNAL_COLLECTION => &mut self.external_collections,
            KIND_UNMANAGED_CONTAINER => &mut self.unmanaged_containers,
            KIND_LOCAL_PORT => &mut self.local_ports,
            _ => return,
        };
        if !list.contains(&value) {
            list.push(value);
        }
    }

    fn sort(&mut self) {
        self.external_collections.sort();
        self.unmanaged_containers.sort();
        self.local_ports.sort_by_key(|p| p.parse::<u32>().unwrap_or(u32::MAX));
    }
}

fn is_class_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

fn is_container_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-'
}

/// The stored form of `value` for `kind`, or `None` when invalid.
///
/// MUST MATCH `vco_lib/infra_declarations.py::normalise`:
/// * external_collection — a letter then letters/digits/underscores (≤ 255
///   chars); the first letter is upper-cased (Weaviate capitalises it);
/// * unmanaged_container — an alphanumeric then alphanumerics/`_.-` (≤ 255);
/// * local_port — decimal digits naming 1..=65535, stored without leading 0s.
pub fn normalise(kind: &str, value: &str) -> Option<String> {
    // The same ASCII whitespace set the Python side trims (`_TRIM`).
    let text = value.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c'));
    if text.is_empty() || text.chars().count() > 255 {
        return None;
    }
    let mut chars = text.chars();
    let first = chars.next()?;
    match kind {
        KIND_EXTERNAL_COLLECTION => {
            if !first.is_ascii_alphabetic() || !chars.all(is_class_char) {
                return None;
            }
            let mut out = first.to_ascii_uppercase().to_string();
            out.push_str(&text[first.len_utf8()..]);
            Some(out)
        }
        KIND_UNMANAGED_CONTAINER => {
            if !first.is_ascii_alphanumeric() || !chars.all(is_container_char) {
                return None;
            }
            Some(text.to_string())
        }
        KIND_LOCAL_PORT => {
            if !text.chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            let port: u64 = text.parse().ok()?;
            (1..=65535).contains(&port).then(|| port.to_string())
        }
        _ => None,
    }
}

impl Db {
    /// This project's declarations (invalid stored values are dropped — the
    /// reader re-validates). A DB without migration 049 reads as empty.
    pub fn list_infra_declarations(&self, project_id: &str) -> Result<InfraDeclarations, String> {
        let conn = self.lock_recover();
        let mut out = InfraDeclarations::default();
        let mut stmt = match conn
            .prepare("SELECT kind, value FROM project_infra_declarations WHERE project_id = ?1")
        {
            Ok(s) => s,
            Err(e) if e.to_string().contains("no such table") => return Ok(out),
            Err(e) => return Err(format!("list infra declarations: {e}")),
        };
        let rows = stmt
            .query_map(params![project_id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("list infra declarations: {e}"))?;
        for row in rows {
            let (kind, raw) = row.map_err(|e| format!("list infra declarations: {e}"))?;
            if let Some(value) = normalise(&kind, &raw) {
                out.push(&kind, value);
            }
        }
        out.sort();
        Ok(out)
    }

    /// Replace this project's declarations of `kind` with `values` (validated
    /// and de-duplicated) in ONE transaction. Any invalid value refuses the
    /// whole write, naming it — a half-applied "do not touch" list is worse
    /// than none. Returns the stored list.
    pub fn set_infra_declarations(
        &self,
        project_id: &str,
        kind: &str,
        values: &[String],
    ) -> Result<Vec<String>, String> {
        if !KINDS.contains(&kind) {
            return Err(format!("unknown declaration kind '{kind}'"));
        }
        let mut stored: Vec<String> = Vec::new();
        for raw in values {
            let Some(value) = normalise(kind, raw) else {
                return Err(format!("'{}' is not a valid {} value", raw.trim(), kind.replace('_', " ")));
            };
            if !stored.contains(&value) {
                stored.push(value);
            }
        }
        let now = chrono::Utc::now().timestamp_millis();
        let mut conn = self.lock_recover();
        let tx = conn.transaction().map_err(|e| format!("set infra declarations: {e}"))?;
        tx.execute(
            "DELETE FROM project_infra_declarations WHERE project_id = ?1 AND kind = ?2",
            params![project_id, kind],
        )
        .map_err(|e| format!("set infra declarations: {e}"))?;
        for value in &stored {
            tx.execute(
                "INSERT INTO project_infra_declarations (project_id, kind, value, created_at) \
                 VALUES (?1, ?2, ?3, ?4)",
                params![project_id, kind, value, now],
            )
            .map_err(|e| format!("set infra declarations: {e}"))?;
        }
        tx.commit().map_err(|e| format!("set infra declarations: {e}"))?;
        let mut out = InfraDeclarations::default();
        for v in stored {
            out.push(kind, v);
        }
        out.sort();
        Ok(match kind {
            KIND_EXTERNAL_COLLECTION => out.external_collections,
            KIND_UNMANAGED_CONTAINER => out.unmanaged_containers,
            _ => out.local_ports,
        })
    }

    /// Machine-wide: every declared-unmanaged container name → the reason a
    /// consumer prints ("declared unmanaged by project 'X'"). Poison-tolerant
    /// (the hub watchdog's contract). `Err` when the table cannot be read —
    /// the watchdog then heals NOTHING that tick (an unreadable "do not
    /// touch" list is not an empty one); a DB without migration 049 is `Ok`
    /// and empty.
    pub fn unmanaged_container_reasons(&self) -> Result<BTreeMap<String, String>, String> {
        let conn = self.lock_recover();
        let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut stmt = match conn.prepare(
            "SELECT d.value, COALESCE(p.name, d.project_id) FROM project_infra_declarations d \
             LEFT JOIN projects p ON p.id = d.project_id WHERE d.kind = ?1",
        ) {
            Ok(s) => s,
            Err(e) if e.to_string().contains("no such table") => return Ok(BTreeMap::new()),
            Err(e) => return Err(format!("read unmanaged containers: {e}")),
        };
        let rows = stmt
            .query_map(params![KIND_UNMANAGED_CONTAINER], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("read unmanaged containers: {e}"))?;
        for row in rows {
            let (raw, who) = row.map_err(|e| format!("read unmanaged containers: {e}"))?;
            if let Some(name) = normalise(KIND_UNMANAGED_CONTAINER, &raw) {
                let entry = out.entry(name).or_default();
                let label = format!("project '{who}'");
                if !entry.contains(&label) {
                    entry.push(label);
                }
            }
        }
        Ok(out
            .into_iter()
            .map(|(name, mut who)| {
                who.sort();
                (name, format!("declared unmanaged by {}", who.join(", ")))
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn db_with(project_id: &str, name: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        let folder = if cfg!(windows) { format!(r"C:\tmp\{project_id}") } else { format!("/tmp/{project_id}") };
        db.insert_project(project_id, name, &folder, ProjectHost::Base, &slug).unwrap();
        db
    }

    #[test]
    fn normalise_matches_the_python_rule() {
        assert_eq!(normalise(KIND_EXTERNAL_COLLECTION, " peerRuntime_Memory "), Some("PeerRuntime_Memory".into()));
        assert_eq!(normalise(KIND_EXTERNAL_COLLECTION, "1Bad"), None);
        assert_eq!(normalise(KIND_EXTERNAL_COLLECTION, "has-dash"), None);
        assert_eq!(normalise(KIND_UNMANAGED_CONTAINER, "peer-weaviate.1"), Some("peer-weaviate.1".into()));
        assert_eq!(normalise(KIND_UNMANAGED_CONTAINER, "-lead"), None);
        assert_eq!(normalise(KIND_UNMANAGED_CONTAINER, "a b"), None);
        assert_eq!(normalise(KIND_LOCAL_PORT, "011436"), Some("11436".into()));
        assert_eq!(normalise(KIND_LOCAL_PORT, "0"), None);
        assert_eq!(normalise(KIND_LOCAL_PORT, "65536"), None);
        assert_eq!(normalise(KIND_LOCAL_PORT, "+80"), None);
        assert_eq!(normalise("nope", "x"), None);
        assert_eq!(normalise(KIND_UNMANAGED_CONTAINER, ""), None);
    }

    #[test]
    fn set_replaces_the_kind_and_lists_back_sorted() {
        let db = db_with("p1", "peer-project");
        let stored = db
            .set_infra_declarations("p1", KIND_LOCAL_PORT, &["11436".into(), "8082".into(), "8082".into()])
            .unwrap();
        assert_eq!(stored, vec!["8082".to_string(), "11436".to_string()]);
        db.set_infra_declarations("p1", KIND_UNMANAGED_CONTAINER, &["peer-weaviate".into()]).unwrap();
        let all = db.list_infra_declarations("p1").unwrap();
        assert_eq!(all.local_ports, vec!["8082", "11436"]);
        assert_eq!(all.unmanaged_containers, vec!["peer-weaviate"]);
        // Replacing one kind leaves the others alone; an empty list clears it.
        db.set_infra_declarations("p1", KIND_LOCAL_PORT, &[]).unwrap();
        let all = db.list_infra_declarations("p1").unwrap();
        assert!(all.local_ports.is_empty());
        assert_eq!(all.unmanaged_containers, vec!["peer-weaviate"]);
    }

    #[test]
    fn an_invalid_value_refuses_the_whole_write() {
        let db = db_with("p1", "peer-project");
        db.set_infra_declarations("p1", KIND_UNMANAGED_CONTAINER, &["keep-me".into()]).unwrap();
        let err = db
            .set_infra_declarations("p1", KIND_UNMANAGED_CONTAINER, &["ok".into(), "bad name".into()])
            .unwrap_err();
        assert!(err.contains("bad name"), "{err}");
        assert_eq!(db.list_infra_declarations("p1").unwrap().unmanaged_containers, vec!["keep-me"]);
        assert!(db.set_infra_declarations("p1", "bogus", &[]).is_err());
    }

    #[test]
    fn unmanaged_reasons_name_every_declarer_and_cascade_with_the_project() {
        let db = db_with("p1", "peer-project");
        let slug = db.generate_unique_slug("other").unwrap();
        db.insert_project("p2", "other", "/tmp/p2", ProjectHost::Base, &slug).unwrap();
        db.set_infra_declarations("p1", KIND_UNMANAGED_CONTAINER, &["peer-weaviate".into()]).unwrap();
        db.set_infra_declarations("p2", KIND_UNMANAGED_CONTAINER, &["peer-weaviate".into()]).unwrap();
        let reasons = db.unmanaged_container_reasons().unwrap();
        let why = reasons.get("peer-weaviate").unwrap();
        assert!(why.contains("'peer-project'") && why.contains("'other'"), "{why}");
        db.lock().execute("PRAGMA foreign_keys = ON", []).unwrap();
        db.lock().execute("DELETE FROM projects WHERE id = 'p1'", []).unwrap();
        let why = db.unmanaged_container_reasons().unwrap().remove("peer-weaviate").unwrap();
        assert!(!why.contains("peer-project"), "{why}");
    }
}
