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
        if !matches!(orchestrator_tier, "free" | "pro" | "mao" | "enterprise") {
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
}
