//! Tier cache read/write. Exactly one row (id=1) always exists (seeded in
//! migration 001 with orchestrator_tier='free').

use chrono::Utc;
use rusqlite::params;
use serde_json::Value;

use super::models::TierCacheRow;
use super::Db;

impl Db {
    pub fn get_tier_cache(&self) -> Result<TierCacheRow, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT orchestrator_tier, module_licenses, last_validated, last_error
                 FROM tier_cache WHERE id = 1",
                [],
                |r| {
                    let licenses_s: String = r.get(1)?;
                    let licenses: Value =
                        serde_json::from_str(&licenses_s).unwrap_or(Value::Object(Default::default()));
                    Ok(TierCacheRow {
                        orchestrator_tier: r.get(0)?,
                        module_licenses: licenses,
                        last_validated: r.get(2)?,
                        last_error: r.get(3)?,
                    })
                },
            )
            .map_err(|e| format!("get tier_cache: {}", e))
    }

    pub fn set_tier_cache(
        &self,
        orchestrator_tier: &str,
        module_licenses: &Value,
        error: Option<&str>,
    ) -> Result<(), String> {
        // Bug 33: "admin" is server-classified via LS_ADMIN_VARIANT_IDS.
        // Treated as a strict superset of "enterprise" by feature gates.
        if !matches!(
            orchestrator_tier,
            "free" | "pro" | "mao" | "enterprise" | "admin"
        ) {
            return Err(format!("invalid tier: {}", orchestrator_tier));
        }
        let guard = self.lock();
        guard
            .execute(
                "UPDATE tier_cache
                    SET orchestrator_tier = ?1,
                        module_licenses = ?2,
                        last_validated = ?3,
                        last_error = ?4
                  WHERE id = 1",
                params![
                    orchestrator_tier,
                    module_licenses.to_string(),
                    Utc::now().timestamp_millis(),
                    error,
                ],
            )
            .map_err(|e| format!("set tier_cache: {}", e))?;
        Ok(())
    }

    /// Atomic read-modify-write for `tier_cache`. Acquires the DB mutex
    /// ONCE, reads the current row, passes it to the closure, then writes
    /// the (possibly-mutated) row back — all under the same lock guard.
    ///
    /// This prevents torn writes when two tasks race to update `tier_cache`
    /// concurrently (e.g. a timer-driven `license_refresh` and a
    /// user-initiated `validate_module_license`). Without this helper each
    /// caller does get → mutate → set across two separate lock acquisitions,
    /// which opens a window where the second writer overwrites the first
    /// writer's changes with a stale copy of the row.
    ///
    /// The closure receives a mutable reference to the row and may modify
    /// any field. Validation (tier allowlist check) is applied to the
    /// post-closure row before the write-back.
    ///
    /// Returns the row as it was written back.
    pub fn with_tier_cache_mut<F>(&self, f: F) -> Result<TierCacheRow, String>
    where
        F: FnOnce(&mut TierCacheRow),
    {
        // Single lock acquisition for the entire RMW sequence.
        let guard = self.lock();

        // Read current row under the lock.
        let mut row = guard
            .query_row(
                "SELECT orchestrator_tier, module_licenses, last_validated, last_error
                 FROM tier_cache WHERE id = 1",
                [],
                |r| {
                    let licenses_s: String = r.get(1)?;
                    let licenses: Value = serde_json::from_str(&licenses_s)
                        .unwrap_or(Value::Object(Default::default()));
                    Ok(TierCacheRow {
                        orchestrator_tier: r.get(0)?,
                        module_licenses: licenses,
                        last_validated: r.get(2)?,
                        last_error: r.get(3)?,
                    })
                },
            )
            .map_err(|e| format!("with_tier_cache_mut read: {}", e))?;

        // Apply caller's mutation.
        f(&mut row);

        // Validate the tier value before writing back.
        if !matches!(
            row.orchestrator_tier.as_str(),
            "free" | "pro" | "mao" | "enterprise" | "admin"
        ) {
            return Err(format!("invalid tier: {}", row.orchestrator_tier));
        }

        // Write back under the same lock.
        guard
            .execute(
                "UPDATE tier_cache
                    SET orchestrator_tier = ?1,
                        module_licenses = ?2,
                        last_validated = ?3,
                        last_error = ?4
                  WHERE id = 1",
                params![
                    &row.orchestrator_tier,
                    row.module_licenses.to_string(),
                    Utc::now().timestamp_millis(),
                    row.last_error.as_deref(),
                ],
            )
            .map_err(|e| format!("with_tier_cache_mut write: {}", e))?;

        Ok(row)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// RT-2: `with_tier_cache_mut` direct round-trip — closure mutation
    /// is persisted and the returned row matches what was written.
    #[test]
    fn with_tier_cache_mut_direct_round_trip() {
        let db = Db::open_in_memory().expect("in-memory");
        db.set_tier_cache("free", &serde_json::json!({}), None).unwrap();

        let written = db
            .with_tier_cache_mut(|row| {
                row.orchestrator_tier = "pro".to_string();
                let map = row.module_licenses.as_object_mut().unwrap();
                map.insert(
                    "vct-rl-reranker".to_string(),
                    serde_json::json!({ "tier": "pro", "source": "per-module" }),
                );
                row.last_error = None;
            })
            .unwrap();

        assert_eq!(written.orchestrator_tier, "pro");
        assert!(written.module_licenses.get("vct-rl-reranker").is_some());

        let persisted = db.get_tier_cache().unwrap();
        assert_eq!(persisted.orchestrator_tier, "pro");
        assert!(persisted.module_licenses.get("vct-rl-reranker").is_some());
    }

    /// RT-2: concurrent RMW race — two tasks interleave their
    /// read-modify-write without tearing each other's fields.
    ///
    /// Task A writes `orchestrator_tier = "pro"`.
    /// Task B writes `last_error = Some("net")`.
    ///
    /// With two sequential `with_tier_cache_mut` calls (the mutex
    /// serialises them), BOTH writes must survive in the final row.
    #[tokio::test]
    async fn with_tier_cache_mut_concurrent_no_torn_write() {
        use std::sync::Arc;

        let db = Arc::new(Db::open_in_memory().expect("in-memory"));
        db.set_tier_cache("free", &serde_json::json!({}), None).unwrap();

        let db_a = Arc::clone(&db);
        let db_b = Arc::clone(&db);

        let task_a = tokio::task::spawn_blocking(move || {
            db_a.with_tier_cache_mut(|row| {
                row.orchestrator_tier = "pro".to_string();
            })
        });
        let task_b = tokio::task::spawn_blocking(move || {
            db_b.with_tier_cache_mut(|row| {
                row.last_error = Some("net".to_string());
            })
        });

        let (r_a, r_b) = tokio::join!(task_a, task_b);
        r_a.expect("task_a join").expect("task_a RMW");
        r_b.expect("task_b join").expect("task_b RMW");

        // Final state: BOTH mutations must be present. The mutex serialises
        // A and B; whichever runs second sees the other's write in the
        // read-phase of its own RMW — no torn write possible.
        let final_row = db.get_tier_cache().unwrap();
        assert_eq!(
            final_row.orchestrator_tier, "pro",
            "task_a's tier upgrade must survive"
        );
        assert_eq!(
            final_row.last_error.as_deref(),
            Some("net"),
            "task_b's error must survive"
        );
    }
}
