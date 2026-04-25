//! Module settings — non-sensitive per-project config values stored as JSON.
//! Secrets use the OS keychain (see `crate::secrets`), not this table.

use rusqlite::{params, OptionalExtension};
use serde_json::Value;

use super::Db;

impl Db {
    pub fn get_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<Option<Value>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2 AND setting_key = ?3",
                params![project_id, module_id, key],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("get setting: {}", e))?;

        match row {
            None => Ok(None),
            Some(s) => serde_json::from_str(&s)
                .map(Some)
                .map_err(|e| format!("parse setting json: {}", e)),
        }
    }

    pub fn set_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
        value: &Value,
    ) -> Result<(), String> {
        let encoded = serde_json::to_string(value)
            .map_err(|e| format!("encode setting: {}", e))?;
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_id, setting_key)
                 DO UPDATE SET setting_value = excluded.setting_value",
                params![project_id, module_id, key, encoded],
            )
            .map_err(|e| format!("set setting: {}", e))?;
        Ok(())
    }

    pub fn list_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Vec<(String, Value)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT setting_key, setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2
               ORDER BY setting_key ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id, module_id], |r| {
                let key: String = r.get(0)?;
                let raw: String = r.get(1)?;
                Ok((key, raw))
            })
            .map_err(|e| format!("query: {}", e))?;

        let mut out = Vec::new();
        for row in rows {
            let (key, raw) = row.map_err(|e| format!("row: {}", e))?;
            let val: Value =
                serde_json::from_str(&raw).map_err(|e| format!("parse '{}': {}", key, e))?;
            out.push((key, val));
        }
        Ok(out)
    }

    pub fn clear_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
            )
            .map_err(|e| format!("clear settings: {}", e))?;
        Ok(())
    }
}
