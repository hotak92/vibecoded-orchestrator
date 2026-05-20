//! Row-level CRUD for `module_weights_state` (migration 016, Phase 3C).
//!
//! Tracks the per-(project × module × embedding_source) state for the RL
//! reranker's downloadable model weights. Per-embedding-source NN weights
//! → one row per source per (project × module). The launcher's daily
//! poller writes `last_checked_at` on every poll attempt; the user's
//! response to the weights-update prompt writes `version` and (when fine-
//! tuning) `last_finetuned_at`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::models::WeightsStateRow;
use super::Db;

impl Db {
    /// Read the state row for a (project_id, module_id, embedding_source)
    /// triple. Returns `Ok(None)` when no row exists (the launcher's
    /// helpers treat that as "never polled, never fine-tuned, no
    /// active version").
    pub fn get_weights_state(
        &self,
        project_id: &str,
        module_id: &str,
        embedding_source: &str,
    ) -> Result<Option<WeightsStateRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, module_id, embedding_source, version,
                        last_checked_at, last_finetuned_at
                 FROM module_weights_state
                 WHERE project_id = ?1 AND module_id = ?2 AND embedding_source = ?3",
                params![project_id, module_id, embedding_source],
                |row| {
                    Ok(WeightsStateRow {
                        project_id: row.get(0)?,
                        module_id: row.get(1)?,
                        embedding_source: row.get(2)?,
                        version: row.get(3)?,
                        last_checked_at: row.get(4)?,
                        last_finetuned_at: row.get(5)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("get_weights_state: {}", e))
    }

    /// Upsert the full row. Used by tests / migration tooling. Day-to-day
    /// callers should prefer the narrower `set_*` helpers below so they
    /// only touch one column at a time.
    pub fn upsert_weights_state(
        &self,
        project_id: &str,
        module_id: &str,
        embedding_source: &str,
        version: &str,
        last_checked_at: i64,
        last_finetuned_at: i64,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_weights_state
                   (project_id, module_id, embedding_source, version, last_checked_at, last_finetuned_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(project_id, module_id, embedding_source) DO UPDATE SET
                   version           = excluded.version,
                   last_checked_at   = excluded.last_checked_at,
                   last_finetuned_at = excluded.last_finetuned_at",
                params![
                    project_id,
                    module_id,
                    embedding_source,
                    version,
                    last_checked_at,
                    last_finetuned_at,
                ],
            )
            .map_err(|e| format!("upsert_weights_state: {}", e))?;
        Ok(())
    }

    /// Stamp `last_checked_at = ts_ms` on the row, creating it (with
    /// empty version + zero last_finetuned_at) when absent. Idempotent.
    pub fn set_last_checked_at(
        &self,
        project_id: &str,
        module_id: &str,
        embedding_source: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_weights_state
                   (project_id, module_id, embedding_source, version, last_checked_at, last_finetuned_at)
                 VALUES (?1, ?2, ?3, '', ?4, 0)
                 ON CONFLICT(project_id, module_id, embedding_source) DO UPDATE SET
                   last_checked_at = excluded.last_checked_at",
                params![project_id, module_id, embedding_source, now],
            )
            .map_err(|e| format!("set_last_checked_at: {}", e))?;
        Ok(())
    }

    /// Stamp `last_finetuned_at = now()`. Idempotent — caller doesn't
    /// need to pre-check existence; row is created if absent.
    pub fn set_last_finetuned_at(
        &self,
        project_id: &str,
        module_id: &str,
        embedding_source: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_weights_state
                   (project_id, module_id, embedding_source, version, last_checked_at, last_finetuned_at)
                 VALUES (?1, ?2, ?3, '', 0, ?4)
                 ON CONFLICT(project_id, module_id, embedding_source) DO UPDATE SET
                   last_finetuned_at = excluded.last_finetuned_at",
                params![project_id, module_id, embedding_source, now],
            )
            .map_err(|e| format!("set_last_finetuned_at: {}", e))?;
        Ok(())
    }

    /// Persist a new locally-active weights version. Idempotent — row is
    /// created if absent, otherwise only the `version` column is updated.
    pub fn set_weights_version(
        &self,
        project_id: &str,
        module_id: &str,
        embedding_source: &str,
        version: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_weights_state
                   (project_id, module_id, embedding_source, version, last_checked_at, last_finetuned_at)
                 VALUES (?1, ?2, ?3, ?4, 0, 0)
                 ON CONFLICT(project_id, module_id, embedding_source) DO UPDATE SET
                   version = excluded.version",
                params![project_id, module_id, embedding_source, version],
            )
            .map_err(|e| format!("set_weights_version: {}", e))?;
        Ok(())
    }

    /// List every weights state row for a project. Used by the Phase 4B
    /// dashboard widget to show the full per-embedding-source matrix.
    /// Ordered by (module_id, embedding_source) for deterministic
    /// UI rendering.
    pub fn list_weights_state_for_project(
        &self,
        project_id: &str,
    ) -> Result<Vec<WeightsStateRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, module_id, embedding_source, version,
                        last_checked_at, last_finetuned_at
                 FROM module_weights_state
                 WHERE project_id = ?1
                 ORDER BY module_id ASC, embedding_source ASC",
            )
            .map_err(|e| format!("prepare list_weights_state: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |row| {
                Ok(WeightsStateRow {
                    project_id: row.get(0)?,
                    module_id: row.get(1)?,
                    embedding_source: row.get(2)?,
                    version: row.get(3)?,
                    last_checked_at: row.get(4)?,
                    last_finetuned_at: row.get(5)?,
                })
            })
            .map_err(|e| format!("query list_weights_state: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_weights_state: {}", e))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;

    /// Seed a minimal project row so the FK on module_weights_state.project_id
    /// holds. Returns the project_id.
    fn seed_project(db: &Db, project_id: &str) {
        let now = Utc::now().timestamp_millis();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, 'P', '/tmp/p', 'base', ?2, ?3, ?3)",
                params![project_id, format!("slug-{}", project_id), now],
            )
            .expect("seed project");
    }

    #[test]
    fn get_weights_state_returns_none_for_missing_row() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-1");
        let got = db
            .get_weights_state("proj-1", "vct-rl-reranker", "qwen3")
            .expect("query");
        assert!(got.is_none(), "expected None for missing row");
    }

    #[test]
    fn upsert_returns_existing_row_after_insert() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-2");
        db.upsert_weights_state("proj-2", "vct-rl-reranker", "qwen3", "v1", 100, 50)
            .expect("upsert");
        let got = db
            .get_weights_state("proj-2", "vct-rl-reranker", "qwen3")
            .expect("query")
            .expect("row present");
        assert_eq!(got.version, "v1");
        assert_eq!(got.last_checked_at, 100);
        assert_eq!(got.last_finetuned_at, 50);
    }

    #[test]
    fn upsert_overwrites_existing_row() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-3");
        db.upsert_weights_state("proj-3", "vct-rl-reranker", "qwen3", "v1", 100, 50)
            .expect("first upsert");
        db.upsert_weights_state("proj-3", "vct-rl-reranker", "qwen3", "v2", 200, 100)
            .expect("second upsert");
        let got = db
            .get_weights_state("proj-3", "vct-rl-reranker", "qwen3")
            .expect("query")
            .expect("row present");
        assert_eq!(got.version, "v2");
        assert_eq!(got.last_checked_at, 200);
        assert_eq!(got.last_finetuned_at, 100);
    }

    #[test]
    fn set_last_checked_at_updates_existing_row() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-4");
        db.upsert_weights_state("proj-4", "vct-rl-reranker", "qwen3", "v1", 100, 50)
            .expect("seed");
        let before = db
            .get_weights_state("proj-4", "vct-rl-reranker", "qwen3")
            .expect("get")
            .expect("row");
        std::thread::sleep(std::time::Duration::from_millis(5));
        db.set_last_checked_at("proj-4", "vct-rl-reranker", "qwen3")
            .expect("set_last_checked");
        let after = db
            .get_weights_state("proj-4", "vct-rl-reranker", "qwen3")
            .expect("get")
            .expect("row");
        // last_checked_at moves forward; version + last_finetuned_at preserved.
        assert!(
            after.last_checked_at > before.last_checked_at,
            "last_checked_at must advance ({} -> {})",
            before.last_checked_at,
            after.last_checked_at,
        );
        assert_eq!(after.version, "v1", "version must not change");
        assert_eq!(after.last_finetuned_at, 50, "last_finetuned_at must not change");
    }

    #[test]
    fn set_last_checked_at_creates_row_when_absent() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-5");
        db.set_last_checked_at("proj-5", "vct-rl-reranker", "qwen3")
            .expect("set_last_checked");
        let got = db
            .get_weights_state("proj-5", "vct-rl-reranker", "qwen3")
            .expect("get")
            .expect("row");
        assert!(got.last_checked_at > 0);
        assert_eq!(got.version, "");
        assert_eq!(got.last_finetuned_at, 0);
    }

    #[test]
    fn set_weights_version_preserves_timestamps() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-6");
        db.upsert_weights_state("proj-6", "vct-rl-reranker", "qwen3", "v1", 999, 500)
            .expect("seed");
        db.set_weights_version("proj-6", "vct-rl-reranker", "qwen3", "v2")
            .expect("set version");
        let got = db
            .get_weights_state("proj-6", "vct-rl-reranker", "qwen3")
            .expect("get")
            .expect("row");
        assert_eq!(got.version, "v2");
        assert_eq!(got.last_checked_at, 999, "timestamps preserved on version update");
        assert_eq!(got.last_finetuned_at, 500);
    }

    #[test]
    fn list_weights_state_for_project_orders_by_module_then_source() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-7");
        // Insert in non-sorted order to verify ORDER BY.
        db.upsert_weights_state("proj-7", "vct-rl-reranker", "qwen3", "v1", 1, 0)
            .expect("ins a");
        db.upsert_weights_state("proj-7", "another-mod", "qwen3", "v2", 2, 0)
            .expect("ins b");
        db.upsert_weights_state("proj-7", "vct-rl-reranker", "arctic", "v3", 3, 0)
            .expect("ins c");
        let rows = db.list_weights_state_for_project("proj-7").expect("list");
        assert_eq!(rows.len(), 3);
        // Order: (another-mod, qwen3), (vct-rl-reranker, arctic), (vct-rl-reranker, qwen3).
        assert_eq!(rows[0].module_id, "another-mod");
        assert_eq!(rows[1].module_id, "vct-rl-reranker");
        assert_eq!(rows[1].embedding_source, "arctic");
        assert_eq!(rows[2].module_id, "vct-rl-reranker");
        assert_eq!(rows[2].embedding_source, "qwen3");
    }

    #[test]
    fn cascade_delete_when_project_dropped() {
        let db = Db::open_in_memory().expect("DB");
        seed_project(&db, "proj-cascade");
        db.upsert_weights_state("proj-cascade", "vct-rl-reranker", "qwen3", "v1", 1, 0)
            .expect("seed");
        // Delete the project — ON DELETE CASCADE should drop the
        // weights state row.
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM projects WHERE id = ?1", params!["proj-cascade"])
                .expect("delete project");
        }
        let got = db
            .get_weights_state("proj-cascade", "vct-rl-reranker", "qwen3")
            .expect("get");
        assert!(got.is_none(), "FK cascade must drop weights state row");
    }
}
