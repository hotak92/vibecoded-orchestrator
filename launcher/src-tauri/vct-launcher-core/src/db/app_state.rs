//! Generic key-value store for launcher app state — backs the Bug 14 fix
//! that moves `vct.onboarding_complete` (and any future similar flags)
//! out of WebView localStorage into launcher.db, so VCT_STATE_DIR
//! isolation works as intended.
//!
//! Schema lives at `migrations/008_app_state.sql`. Distinguishes
//! "row absent" (default behaviour applies) from "row present and
//! false" (user explicitly opted out) — frontend callers MUST treat
//! `None` and `Some(false)` differently.
//!
//! Type discipline: the on-disk column is TEXT. Boolean callers use the
//! `get_bool` / `set_bool` helpers; arbitrary string flags use the raw
//! `get` / `set` pair.

use rusqlite::params;

use super::Db;

impl Db {
    /// Read a raw string value. Returns `None` when no row exists.
    pub fn app_state_get(&self, key: &str) -> Result<Option<String>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT value FROM app_state WHERE key = ?1",
                params![key],
                |r| r.get(0),
            )
            .ok();
        Ok(row)
    }

    /// v0.2.62 (CONCERN-6 remediation): poison-tolerant boolean reader for
    /// the hub's detached infra-watchdog task, which must never panic.
    ///
    /// Same semantics as [`Db::app_state_get_bool`] (None = no row,
    /// Some(true)/Some(false) per the stored value) but acquires the lock
    /// via [`Db::lock_recover`] so a poisoned mutex recovers instead of
    /// panicking. The watchdog reads `launcher.services_watcher_enabled`
    /// through this on every tick to honor the user's auto-restart toggle.
    pub fn app_state_get_bool_nonpanicking(
        &self,
        key: &str,
    ) -> Result<Option<bool>, String> {
        let guard = self.lock_recover();
        let row: Option<String> = guard
            .query_row(
                "SELECT value FROM app_state WHERE key = ?1",
                params![key],
                |r| r.get(0),
            )
            .ok();
        Ok(row.map(|v| matches!(v.as_str(), "true" | "1")))
    }

    /// Write a raw string value (upsert). Stamps `updated_at` to now.
    pub fn app_state_set(&self, key: &str, value: &str) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO app_state (key, value, updated_at)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at",
                params![key, value, now],
            )
            .map_err(|e| format!("app_state_set({}): {}", key, e))?;
        Ok(())
    }

    /// Delete rows whose keys match a SQL LIKE pattern. Returns the number
    /// of rows removed. Used by v0.2.34 launcher-version-change cache-bust
    /// to wipe `module_catalog.cache*` entries (envelope + fetched-at)
    /// after an orchestrator update. No-op (Ok(0)) when nothing matches —
    /// callers don't need to pre-check existence.
    pub fn app_state_delete_like(&self, pattern: &str) -> Result<usize, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "DELETE FROM app_state WHERE key LIKE ?1",
                params![pattern],
            )
            .map_err(|e| format!("app_state_delete_like({}): {}", pattern, e))?;
        Ok(n)
    }

    /// Boolean convenience reader. Returns `None` (no row) vs
    /// `Some(true)` / `Some(false)` so callers can distinguish "user
    /// explicitly opted out" from "unset, apply default".
    pub fn app_state_get_bool(&self, key: &str) -> Result<Option<bool>, String> {
        match self.app_state_get(key)? {
            None => Ok(None),
            Some(v) => Ok(Some(matches!(v.as_str(), "true" | "1"))),
        }
    }

    /// Boolean convenience writer. Stores "true" / "false".
    pub fn app_state_set_bool(&self, key: &str, value: bool) -> Result<(), String> {
        self.app_state_set(key, if value { "true" } else { "false" })
    }

    /// v0.2.49 access-matrix Phase 1 (item #3): read the persisted
    /// canonical name of the orchestrator-root shared KG collection.
    ///
    /// Returns the value from the `app_state` row written by migration
    /// 028 (or whatever later override install.py wrote — see Phase 1
    /// item #2). Migration 028 INSERT-OR-IGNORE's a default of
    /// `VibeCodedOrchestrator_KnowledgeGraph`, so this getter always
    /// returns `Some(_)` on a launcher that's applied migrations through
    /// version 28+.
    ///
    /// The fallback default is duplicated here defensively for the case
    /// where the row was manually deleted (an unsupported but possible
    /// state — e.g. someone hand-edited the DB). Callers should treat
    /// `Ok(name)` as authoritative without re-checking.
    ///
    /// Closes audit finding S-1: every consumer that asks "is this
    /// collection the orchestrator-root shared one?" calls this helper
    /// and compares by byte-equality, instead of duplicating the
    /// `LAST_RESORT_SHARED_KG_COLLECTION` constant across crates.
    pub fn get_orchestrator_root_kg_collection(&self) -> Result<String, String> {
        Ok(self
            .app_state_get(ORCHESTRATOR_ROOT_KG_COLLECTION_KEY)?
            .unwrap_or_else(|| {
                DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION.to_string()
            }))
    }

    /// v0.2.49 access-matrix Phase 1 (item #2 backend): set the
    /// persisted canonical name of the orchestrator-root shared KG
    /// collection. Called by install.py at install time (via a hub or
    /// Tauri command) and by white-label installers that need a
    /// branded collection name.
    ///
    /// Idempotent upsert. Empty / whitespace-only values are refused
    /// (returns Err) to prevent accidentally clearing the canonical
    /// pointer to the empty string.
    pub fn set_orchestrator_root_kg_collection(&self, name: &str) -> Result<(), String> {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            return Err(
                "set_orchestrator_root_kg_collection: refuses empty value"
                    .to_string(),
            );
        }
        self.app_state_set(ORCHESTRATOR_ROOT_KG_COLLECTION_KEY, trimmed)
    }

    // ─── v0.2.72 P1: codegraph retrieval floors (machine-global) ─────────
    //
    // Two machine-global score floors that gate code-graph retrieval, stored
    // in `app_state` exactly like `embedding.active_profile` (a flat TEXT
    // key). The f64 is serialised as its plain decimal string on write and
    // parsed back on read; a missing row or a parse failure falls through to
    // the compiled-in default (soft-fail — a bad row must never crash an env
    // render). The Python side (`config_projection.py`, owned by T-FLOOR)
    // projects these into `VCO_CODE_GRAPH_RETRIEVAL_FLOOR` /
    // `VCO_CODE_GRAPH_POST_RERANK_FLOOR` env vars that the analyzer / MCP consume.

    /// Read the machine-global two-stage retrieval floor (pre-rerank seed
    /// cutoff). Returns [`DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR`] when the row is
    /// absent or unparseable.
    pub fn get_codegraph_retrieval_floor(&self) -> Result<f64, String> {
        Ok(self
            .app_state_get(CODEGRAPH_RETRIEVAL_FLOOR_KEY)?
            .and_then(|s| s.trim().parse::<f64>().ok())
            .unwrap_or(DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR))
    }

    /// Read the machine-global post-rerank floor (final result cutoff after
    /// reranking). Returns [`DEFAULT_CODEGRAPH_POST_RERANK_FLOOR`] when the
    /// row is absent or unparseable.
    pub fn get_codegraph_post_rerank_floor(&self) -> Result<f64, String> {
        Ok(self
            .app_state_get(CODEGRAPH_POST_RERANK_FLOOR_KEY)?
            .and_then(|s| s.trim().parse::<f64>().ok())
            .unwrap_or(DEFAULT_CODEGRAPH_POST_RERANK_FLOOR))
    }

    /// Persist both machine-global codegraph floors. Values MUST already be
    /// range-validated (`0.0..=1.0`) by the caller — this DB writer refuses
    /// out-of-range or non-finite inputs defensively so a bad row can never
    /// land (a floor > 1.0 would silently discard every result; < 0.0 is
    /// meaningless for a cosine score). Written as plain decimal strings.
    pub fn set_codegraph_floors(
        &self,
        retrieval: f64,
        post_rerank: f64,
    ) -> Result<(), String> {
        for (label, v) in [("retrieval", retrieval), ("post_rerank", post_rerank)] {
            if !v.is_finite() || !(0.0..=1.0).contains(&v) {
                return Err(format!(
                    "set_codegraph_floors: {label} floor {v} out of range (0.0..=1.0)"
                ));
            }
        }
        self.app_state_set(CODEGRAPH_RETRIEVAL_FLOOR_KEY, &retrieval.to_string())?;
        self.app_state_set(
            CODEGRAPH_POST_RERANK_FLOOR_KEY,
            &post_rerank.to_string(),
        )?;
        Ok(())
    }
}

/// `app_state` key for the machine-global two-stage retrieval floor (v0.2.72
/// P1). Consumed via the projected `VCO_CODE_GRAPH_RETRIEVAL_FLOOR` env.
pub const CODEGRAPH_RETRIEVAL_FLOOR_KEY: &str = "codegraph.retrieval_floor";

/// `app_state` key for the machine-global post-rerank floor (v0.2.72 P1).
/// Consumed via the projected `VCO_CODE_GRAPH_POST_RERANK_FLOOR` env.
pub const CODEGRAPH_POST_RERANK_FLOOR_KEY: &str = "codegraph.post_rerank_floor";

/// Default two-stage retrieval floor. MUST match the Python-side default in
/// the T-FLOOR projection + analyzer so a launcher that never touched the
/// GUI produces identical behaviour.
pub const DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR: f64 = 0.16;

/// Default post-rerank floor. MUST match the Python-side default (see above).
pub const DEFAULT_CODEGRAPH_POST_RERANK_FLOOR: f64 = 0.22;

/// `app_state` key for the persisted orchestrator-root shared KG
/// collection name (migration 028 / Phase 1 / v0.2.49 access-matrix).
pub const ORCHESTRATOR_ROOT_KG_COLLECTION_KEY: &str =
    "orchestrator_root_kg_collection";

/// Default value persisted by migration 028's INSERT OR IGNORE. Kept
/// here (rather than only in the SQL) so the Rust-side getter can
/// fall back to it for the unsupported "row manually deleted" state.
/// MUST be byte-identical to the literal in
/// `migrations/028_orchestrator_root_kg_collection.sql`.
pub const DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION: &str =
    "VibeCodedOrchestrator_KnowledgeGraph";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_state_roundtrip_string() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(db.app_state_get("nope").unwrap(), None);
        db.app_state_set("hello", "world").unwrap();
        assert_eq!(db.app_state_get("hello").unwrap().as_deref(), Some("world"));
        // upsert overwrites
        db.app_state_set("hello", "there").unwrap();
        assert_eq!(db.app_state_get("hello").unwrap().as_deref(), Some("there"));
    }

    #[test]
    fn app_state_bool_distinguishes_absent_from_false() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(db.app_state_get_bool("onboarding").unwrap(), None);
        db.app_state_set_bool("onboarding", false).unwrap();
        assert_eq!(db.app_state_get_bool("onboarding").unwrap(), Some(false));
        db.app_state_set_bool("onboarding", true).unwrap();
        assert_eq!(db.app_state_get_bool("onboarding").unwrap(), Some(true));
    }

    #[test]
    fn app_state_delete_like_removes_matching_keys() {
        // v0.2.34: launcher-version-change cache-bust wipes
        // `module_catalog.cache*` keys via LIKE pattern. Verify both
        // the envelope key and the fetched-at sibling are removed in
        // one shot.
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set("module_catalog.cache", "{}").unwrap();
        db.app_state_set("module_catalog.cache_fetched_at", "1700000000")
            .unwrap();
        db.app_state_set("unrelated.flag", "keep_me").unwrap();
        let n = db.app_state_delete_like("module_catalog.cache%").unwrap();
        assert_eq!(n, 2, "should remove envelope + fetched-at rows");
        assert_eq!(db.app_state_get("module_catalog.cache").unwrap(), None);
        assert_eq!(
            db.app_state_get("module_catalog.cache_fetched_at").unwrap(),
            None,
        );
        // Unrelated rows must survive.
        assert_eq!(
            db.app_state_get("unrelated.flag").unwrap().as_deref(),
            Some("keep_me"),
        );
        // Pattern that matches nothing: no error, returns 0.
        let n2 = db.app_state_delete_like("nope.%").unwrap();
        assert_eq!(n2, 0);
    }

    #[test]
    fn app_state_bool_accepts_either_true_form() {
        // Mostly defensive — if a future caller writes the raw string
        // "1" instead of going through `set_bool`, reading via `get_bool`
        // should still treat it as true.
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set("legacy_flag", "1").unwrap();
        assert_eq!(db.app_state_get_bool("legacy_flag").unwrap(), Some(true));
        db.app_state_set("legacy_flag", "0").unwrap();
        assert_eq!(db.app_state_get_bool("legacy_flag").unwrap(), Some(false));
    }

    // ─── v0.2.49 access-matrix Phase 1 (items #1, #3) ─────────────────────
    // Tests for migration 028 + the orchestrator_root_kg_collection getter
    // / setter pair. The migration_creates_orchestrator_root_collection_
    // setting test asserts the SQL-level INSERT-OR-IGNORE behaviour
    // independently of the Rust helpers, so we'd catch a divergence
    // between the SQL default value and the Rust DEFAULT constant.

    #[test]
    fn migration_creates_orchestrator_root_collection_setting() {
        // Fresh in-memory DB applies every migration including 028.
        // The setting row should exist with the canonical default value.
        let db = Db::open_in_memory().expect("in-memory db");
        let row = db
            .app_state_get(ORCHESTRATOR_ROOT_KG_COLLECTION_KEY)
            .expect("read app_state");
        assert_eq!(
            row.as_deref(),
            Some(DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION),
            "migration 028 must seed the canonical default value into \
             app_state on every fresh install",
        );
    }

    #[test]
    fn get_orchestrator_root_kg_collection_returns_default_when_row_present() {
        // The convenience getter wraps the raw app_state_get; on a
        // fresh DB it returns the migration-seeded value.
        let db = Db::open_in_memory().expect("in-memory db");
        let v = db
            .get_orchestrator_root_kg_collection()
            .expect("get orchestrator-root collection");
        assert_eq!(v, DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION);
    }

    #[test]
    fn get_orchestrator_root_kg_collection_falls_back_when_row_deleted() {
        // Unsupported state: someone hand-edited the DB and deleted
        // the row. The getter still returns the compiled-in default
        // (defensive fallback) — callers don't have to special-case
        // None.
        let db = Db::open_in_memory().expect("in-memory db");
        let guard = db.lock();
        guard
            .execute(
                "DELETE FROM app_state WHERE key = ?1",
                params![ORCHESTRATOR_ROOT_KG_COLLECTION_KEY],
            )
            .expect("delete row");
        drop(guard);
        let v = db
            .get_orchestrator_root_kg_collection()
            .expect("get with row deleted");
        assert_eq!(v, DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION);
    }

    #[test]
    fn set_orchestrator_root_kg_collection_persists_white_label_name() {
        // install.py / a white-label installer overrides the canonical
        // name. Subsequent reads return the override; the original
        // canonical default is no longer observable.
        let db = Db::open_in_memory().expect("in-memory db");
        db.set_orchestrator_root_kg_collection("AcmeCorp_KnowledgeGraph")
            .expect("white-label override");
        let v = db.get_orchestrator_root_kg_collection().expect("read");
        assert_eq!(v, "AcmeCorp_KnowledgeGraph");
    }

    #[test]
    fn set_orchestrator_root_kg_collection_refuses_empty_and_whitespace() {
        // Empty value would silently break every consumer that
        // compares collection names by byte-equality. The setter
        // hard-fails so the bad write never lands.
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(
            db.set_orchestrator_root_kg_collection("").is_err(),
            "empty value must be refused",
        );
        assert!(
            db.set_orchestrator_root_kg_collection("   ").is_err(),
            "whitespace-only value must be refused",
        );
        // Original default still in place — the failed write did
        // nothing.
        let v = db.get_orchestrator_root_kg_collection().expect("read");
        assert_eq!(v, DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION);
    }

    #[test]
    fn set_orchestrator_root_kg_collection_trims_surrounding_whitespace() {
        // Defensive: install.py might pipe through a path-quoted name
        // with trailing newline / whitespace. Trim before persisting.
        let db = Db::open_in_memory().expect("in-memory db");
        db.set_orchestrator_root_kg_collection("  MyCustom_KG  \n")
            .expect("trim + persist");
        let v = db.get_orchestrator_root_kg_collection().expect("read");
        assert_eq!(v, "MyCustom_KG");
    }

    // ─── v0.2.72 P1: codegraph retrieval floors (machine-global) ─────────

    #[test]
    fn codegraph_floors_default_when_unset() {
        // Fresh DB: no app_state rows for the floors → getters return the
        // compiled-in defaults. A launcher that never touched the GUI must
        // behave identically to the pre-P1 hardcoded floors.
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(
            db.get_codegraph_retrieval_floor().unwrap(),
            DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR,
        );
        assert_eq!(
            db.get_codegraph_post_rerank_floor().unwrap(),
            DEFAULT_CODEGRAPH_POST_RERANK_FLOOR,
        );
    }

    #[test]
    fn set_codegraph_floors_persists_both_keys() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.set_codegraph_floors(0.30, 0.45).expect("persist floors");
        assert_eq!(db.get_codegraph_retrieval_floor().unwrap(), 0.30);
        assert_eq!(db.get_codegraph_post_rerank_floor().unwrap(), 0.45);
        // Raw rows carry the decimal-string encoding both getters parse.
        assert_eq!(
            db.app_state_get(CODEGRAPH_RETRIEVAL_FLOOR_KEY)
                .unwrap()
                .as_deref(),
            Some("0.3"),
        );
    }

    #[test]
    fn set_codegraph_floors_accepts_boundary_values() {
        // 0.0 and 1.0 are the inclusive endpoints — both valid cosine-score
        // floors (0.0 = keep everything, 1.0 = exact match only).
        let db = Db::open_in_memory().expect("in-memory db");
        db.set_codegraph_floors(0.0, 1.0).expect("boundary floors");
        assert_eq!(db.get_codegraph_retrieval_floor().unwrap(), 0.0);
        assert_eq!(db.get_codegraph_post_rerank_floor().unwrap(), 1.0);
    }

    #[test]
    fn set_codegraph_floors_rejects_out_of_range() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(
            db.set_codegraph_floors(-0.1, 0.2).is_err(),
            "negative retrieval floor must be refused",
        );
        assert!(
            db.set_codegraph_floors(0.2, 1.5).is_err(),
            "post-rerank floor > 1.0 must be refused",
        );
        assert!(
            db.set_codegraph_floors(f64::NAN, 0.2).is_err(),
            "non-finite floor must be refused",
        );
        // A rejected write must leave NO partial state — both getters still
        // return defaults (the first key must not persist before the second
        // fails, and range checks run before any write).
        assert_eq!(
            db.get_codegraph_retrieval_floor().unwrap(),
            DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR,
        );
    }

    #[test]
    fn codegraph_floor_getter_soft_fails_on_garbage_row() {
        // A hand-corrupted / legacy non-numeric row must not crash the
        // getter — it falls through to the default (soft-fail discipline).
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set(CODEGRAPH_RETRIEVAL_FLOOR_KEY, "not-a-number")
            .unwrap();
        assert_eq!(
            db.get_codegraph_retrieval_floor().unwrap(),
            DEFAULT_CODEGRAPH_RETRIEVAL_FLOOR,
        );
    }
}
