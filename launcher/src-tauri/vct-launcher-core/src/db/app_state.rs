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
}

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
}
