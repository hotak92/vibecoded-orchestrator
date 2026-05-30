//! Per-paid-module license keys (v0.2.40 L1).
//!
//! Source-of-truth side of the multi-key licensing model: each paid
//! module (RL Reranker, MAO, future agent packs) owns one row keyed by
//! `module_id`. The reserved value `module_id = '__orchestrator__'`
//! identifies the root orchestrator-tier key (the legacy single-key
//! slot from v0.2.39 and earlier).
//!
//! The raw key BYTES never live in SQLite — only `key_prefix` (first
//! 12 chars for the GUI's "ends in ..." display) and the keychain
//! coordinates needed to re-fetch the secret. The OS keychain holds
//! the actual key at:
//!
//!   service = "vct.global.licensing"
//!   username = <keychain_username column>
//!
//! For new per-module rows the username is `license_key__<module_id>`;
//! for migrated legacy rows it's the constant `VIBECODED_LICENSE_KEY`
//! so the existing keychain entry is reused without rewriting.
//!
//! Why this is a separate table from `tier_cache`
//!   `tier_cache` has the `id INTEGER PRIMARY KEY CHECK (id = 1)`
//!   single-row invariant baked into migrations 001 and 005. It
//!   represents the EFFECTIVE projected state (orchestrator_tier +
//!   `module_licenses` JSON overlay). `license_keys` holds the SOURCE
//!   of raw user-provided keys. The refresh flow in
//!   `commands/licensing.rs` validates each row independently and
//!   writes per-module entries into `tier_cache.module_licenses` —
//!   keeping the two layers separate avoids touching every
//!   tier_cache reader (which would be a much larger v0.2.40 surface
//!   than the user signed up for).

use chrono::Utc;
use rusqlite::params;

use super::Db;

/// Reserved `module_id` value for the legacy single-key slot. Mirrors
/// the v0.2.39-and-earlier behaviour where ONE key drove the
/// orchestrator tier; the L1 model keeps that key reachable as a
/// special module_id so existing installs upgrade cleanly.
pub const ORCHESTRATOR_MODULE_ID: &str = "__orchestrator__";

/// Legacy keychain username (single-key model). When a row is created
/// by the v0.2.40 migration path, `keychain_username` carries this
/// value so the existing OS keychain entry is reused without a
/// one-time rewrite.
pub const LEGACY_KEYCHAIN_USERNAME: &str = "VIBECODED_LICENSE_KEY";

/// Compose the per-module keychain username for new per-module rows.
/// Legacy single-key rows stay on `LEGACY_KEYCHAIN_USERNAME`.
pub fn keychain_username_for(module_id: &str) -> String {
    if module_id == ORCHESTRATOR_MODULE_ID {
        // Promotion path: when the user explicitly re-activates the
        // orchestrator key through the new GUI, we WRITE to the legacy
        // username so a downgrade to a pre-L1 launcher still finds it.
        LEGACY_KEYCHAIN_USERNAME.to_string()
    } else {
        format!("license_key__{}", module_id)
    }
}

/// In-memory row shape for `license_keys`. The raw key VALUE is never
/// part of this struct — callers that need it must reach into the OS
/// keychain at (service='vct.global.licensing', username=keychain_username).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LicenseKeyRow {
    pub module_id: String,
    /// First 12 characters of the key (for the GUI's "ends in ..." display).
    pub key_prefix: String,
    pub keychain_username: String,
    /// Last successful validation's server-returned tier. None when never
    /// validated or when every validation has failed since insertion.
    pub tier: Option<String>,
    /// Unix milliseconds of the last validation attempt (success OR failure).
    pub validated_at: Option<i64>,
    /// Human-readable error from the last validation attempt. None on success.
    pub last_validation_error: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
}

/// Append-only audit entry per validation round-trip. Capped at the
/// application layer (see `Db::trim_license_key_validations`).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct LicenseKeyValidationRow {
    pub id: i64,
    pub module_id: String,
    pub validated_at: i64,
    pub tier: Option<String>,
    pub http_status: i64,
    pub error_message: Option<String>,
}

/// Compute the display prefix for a raw key. Stable shape: the first
/// 12 chars, never the whole key. We deliberately do NOT show the
/// suffix — a "key ends in XYZ" UX would tempt users to share screen-
/// shots showing the discriminating bits; "starts with ABC" is much
/// harder to weaponise because LS-generated keys share a common
/// prefix shape.
pub fn key_prefix_of(key: &str) -> String {
    key.chars().take(12).collect()
}

impl Db {
    /// Upsert a `license_keys` row. The raw key is NOT written here;
    /// the caller is responsible for storing the actual key bytes in
    /// the OS keychain BEFORE invoking this (so a SQL-only commit
    /// without the keychain side never leaves a row pointing at a
    /// missing secret).
    ///
    /// `key_prefix` should be `key_prefix_of(raw_key)`. We pass it
    /// explicitly rather than hashing the raw key here so this layer
    /// stays secret-free — easier to reason about in audits.
    pub fn upsert_license_key(
        &self,
        module_id: &str,
        key_prefix: &str,
        keychain_username: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO license_keys (
                    module_id, key_prefix, keychain_username,
                    tier, validated_at, last_validation_error,
                    created_at, updated_at
                 ) VALUES (?1, ?2, ?3, NULL, NULL, NULL, ?4, ?4)
                 ON CONFLICT(module_id) DO UPDATE SET
                    key_prefix = excluded.key_prefix,
                    keychain_username = excluded.keychain_username,
                    -- Key rotation: clear stale validation state so the
                    -- next refresh re-validates against the server
                    -- instead of carrying the previous key's tier.
                    tier = NULL,
                    validated_at = NULL,
                    last_validation_error = NULL,
                    updated_at = excluded.updated_at",
                params![module_id, key_prefix, keychain_username, now],
            )
            .map_err(|e| format!("upsert license_keys[{}]: {}", module_id, e))?;
        Ok(())
    }

    /// Update the validation outcome for a previously-inserted row.
    /// Called by `commands::licensing::validate_module_license` after
    /// the `/validate-tier` HTTP round-trip.
    pub fn record_license_key_validation(
        &self,
        module_id: &str,
        tier: Option<&str>,
        error: Option<&str>,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "UPDATE license_keys SET
                    tier = ?1,
                    validated_at = ?2,
                    last_validation_error = ?3,
                    updated_at = ?2
                  WHERE module_id = ?4",
                params![tier, now, error, module_id],
            )
            .map_err(|e| format!("record validation for {}: {}", module_id, e))?;
        Ok(())
    }

    /// Append an entry to `license_key_validations`. Best-effort capped
    /// at 50 most-recent rows per module — older rows are trimmed by
    /// `trim_license_key_validations`.
    pub fn append_license_key_validation(
        &self,
        module_id: &str,
        tier: Option<&str>,
        http_status: i64,
        error_message: Option<&str>,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO license_key_validations
                    (module_id, validated_at, tier, http_status, error_message)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![module_id, now, tier, http_status, error_message],
            )
            .map_err(|e| format!("append validation row for {}: {}", module_id, e))?;
        Ok(())
    }

    /// Trim older entries in `license_key_validations`, keeping only the
    /// most recent `keep` rows per `module_id`. Best-effort: failure
    /// here is non-fatal (just leaves more audit rows).
    pub fn trim_license_key_validations(&self, module_id: &str, keep: i64) -> Result<(), String> {
        let guard = self.lock();
        // SQLite supports correlated DELETE with a subselect ranking by
        // validated_at. Keep the `keep` most-recent rows; delete the rest.
        guard
            .execute(
                "DELETE FROM license_key_validations
                  WHERE module_id = ?1
                    AND id NOT IN (
                        SELECT id FROM license_key_validations
                         WHERE module_id = ?1
                         ORDER BY validated_at DESC, id DESC
                         LIMIT ?2
                    )",
                params![module_id, keep],
            )
            .map_err(|e| format!("trim validations for {}: {}", module_id, e))?;
        Ok(())
    }

    /// Read one `license_keys` row by module_id. None if absent.
    pub fn get_license_key(&self, module_id: &str) -> Result<Option<LicenseKeyRow>, String> {
        let guard = self.lock();
        let row = guard
            .query_row(
                "SELECT module_id, key_prefix, keychain_username,
                        tier, validated_at, last_validation_error,
                        created_at, updated_at
                   FROM license_keys
                  WHERE module_id = ?1",
                params![module_id],
                |r| {
                    Ok(LicenseKeyRow {
                        module_id: r.get(0)?,
                        key_prefix: r.get(1)?,
                        keychain_username: r.get(2)?,
                        tier: r.get(3)?,
                        validated_at: r.get(4)?,
                        last_validation_error: r.get(5)?,
                        created_at: r.get(6)?,
                        updated_at: r.get(7)?,
                    })
                },
            )
            .ok();
        Ok(row)
    }

    /// List every `license_keys` row, ordered by module_id (the
    /// reserved `__orchestrator__` slot sorts first thanks to the
    /// double-underscore prefix in ASCII order, which is the order the
    /// GUI wants — root key at the top, then per-module add-ons).
    pub fn list_license_keys(&self) -> Result<Vec<LicenseKeyRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT module_id, key_prefix, keychain_username,
                        tier, validated_at, last_validation_error,
                        created_at, updated_at
                   FROM license_keys
                  ORDER BY module_id ASC",
            )
            .map_err(|e| format!("prepare list_license_keys: {}", e))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(LicenseKeyRow {
                    module_id: r.get(0)?,
                    key_prefix: r.get(1)?,
                    keychain_username: r.get(2)?,
                    tier: r.get(3)?,
                    validated_at: r.get(4)?,
                    last_validation_error: r.get(5)?,
                    created_at: r.get(6)?,
                    updated_at: r.get(7)?,
                })
            })
            .map_err(|e| format!("query list_license_keys: {}", e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_license_keys: {}", e))?;
        Ok(rows)
    }

    /// Delete a `license_keys` row + its validation history. The caller
    /// is responsible for removing the keychain entry SEPARATELY (this
    /// layer stays secret-free).
    pub fn delete_license_key(&self, module_id: &str) -> Result<bool, String> {
        let guard = self.lock();
        // Validation history goes first (no FK between the tables on
        // purpose — append-only audit is independent from the source
        // row's lifecycle, but deleting the source row should drop the
        // dead history rather than leave them orphaned).
        guard
            .execute(
                "DELETE FROM license_key_validations WHERE module_id = ?1",
                params![module_id],
            )
            .map_err(|e| format!("delete validations for {}: {}", module_id, e))?;
        let n = guard
            .execute(
                "DELETE FROM license_keys WHERE module_id = ?1",
                params![module_id],
            )
            .map_err(|e| format!("delete license_keys[{}]: {}", module_id, e))?;
        Ok(n > 0)
    }

    /// Read the most recent N `license_key_validations` rows for a
    /// given module, newest first. For the GUI's per-module timeline.
    pub fn recent_license_key_validations(
        &self,
        module_id: &str,
        limit: i64,
    ) -> Result<Vec<LicenseKeyValidationRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, module_id, validated_at, tier, http_status, error_message
                   FROM license_key_validations
                  WHERE module_id = ?1
                  ORDER BY validated_at DESC, id DESC
                  LIMIT ?2",
            )
            .map_err(|e| format!("prepare recent_validations: {}", e))?;
        let rows = stmt
            .query_map(params![module_id, limit], |r| {
                Ok(LicenseKeyValidationRow {
                    id: r.get(0)?,
                    module_id: r.get(1)?,
                    validated_at: r.get(2)?,
                    tier: r.get(3)?,
                    http_status: r.get(4)?,
                    error_message: r.get(5)?,
                })
            })
            .map_err(|e| format!("query recent_validations: {}", e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect recent_validations: {}", e))?;
        Ok(rows)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upsert_and_get_round_trip() {
        let db = Db::open_in_memory().expect("in-memory");
        db.upsert_license_key("vct-rl-reranker", "vct_pro_abc", "license_key__vct-rl-reranker")
            .expect("upsert");
        let row = db.get_license_key("vct-rl-reranker").expect("get").expect("row");
        assert_eq!(row.module_id, "vct-rl-reranker");
        assert_eq!(row.key_prefix, "vct_pro_abc");
        assert_eq!(row.keychain_username, "license_key__vct-rl-reranker");
        assert!(row.tier.is_none());
        assert!(row.validated_at.is_none());
        assert!(row.last_validation_error.is_none());
    }

    /// L1 core contract: setting a key for module A must not touch module B.
    /// Mirrors `tests/test_license_per_module_keys.py::T1`.
    #[test]
    fn upsert_module_a_does_not_affect_module_b() {
        let db = Db::open_in_memory().expect("in-memory");
        db.upsert_license_key("vct-rl-reranker", "AAA", "license_key__vct-rl-reranker")
            .expect("upsert A");
        db.record_license_key_validation("vct-rl-reranker", Some("pro"), None)
            .expect("validate A");
        db.upsert_license_key("vct-mao", "BBB", "license_key__vct-mao")
            .expect("upsert B");
        // B starts fresh; A's tier survives.
        let a = db.get_license_key("vct-rl-reranker").expect("get A").expect("A row");
        let b = db.get_license_key("vct-mao").expect("get B").expect("B row");
        assert_eq!(a.tier.as_deref(), Some("pro"));
        assert!(b.tier.is_none(), "module B must start without an inherited tier");
        assert_eq!(b.key_prefix, "BBB");
    }

    /// Key rotation: re-upserting clears stale validation state so the
    /// next refresh re-validates from scratch.
    #[test]
    fn upsert_existing_module_clears_validation_state() {
        let db = Db::open_in_memory().expect("in-memory");
        db.upsert_license_key("vct-rl-reranker", "OLD", "license_key__vct-rl-reranker")
            .expect("upsert");
        db.record_license_key_validation("vct-rl-reranker", Some("pro"), None)
            .expect("validate");
        // Rotate: new prefix, same module.
        db.upsert_license_key("vct-rl-reranker", "NEW", "license_key__vct-rl-reranker")
            .expect("rotate");
        let row = db.get_license_key("vct-rl-reranker").expect("get").expect("row");
        assert_eq!(row.key_prefix, "NEW");
        assert!(row.tier.is_none(), "rotated key must re-validate before claiming a tier");
    }

    #[test]
    fn delete_removes_row_and_validation_history() {
        let db = Db::open_in_memory().expect("in-memory");
        db.upsert_license_key("vct-rl-reranker", "AAA", "license_key__vct-rl-reranker")
            .expect("upsert");
        db.append_license_key_validation("vct-rl-reranker", Some("pro"), 200, None)
            .expect("audit");
        let removed = db.delete_license_key("vct-rl-reranker").expect("delete");
        assert!(removed);
        assert!(db.get_license_key("vct-rl-reranker").expect("get").is_none());
        let history = db
            .recent_license_key_validations("vct-rl-reranker", 10)
            .expect("recent");
        assert!(history.is_empty(), "validation history must be dropped with the row");
    }

    #[test]
    fn list_returns_orchestrator_slot_first() {
        let db = Db::open_in_memory().expect("in-memory");
        db.upsert_license_key("vct-rl-reranker", "AAA", "license_key__vct-rl-reranker")
            .expect("upsert A");
        db.upsert_license_key(ORCHESTRATOR_MODULE_ID, "ORC", LEGACY_KEYCHAIN_USERNAME)
            .expect("upsert root");
        let rows = db.list_license_keys().expect("list");
        // '__orchestrator__' < 'vct-rl-reranker' in ASCII, so it sorts first.
        assert_eq!(rows[0].module_id, ORCHESTRATOR_MODULE_ID);
        assert_eq!(rows[1].module_id, "vct-rl-reranker");
    }

    #[test]
    fn trim_keeps_most_recent_n_rows() {
        let db = Db::open_in_memory().expect("in-memory");
        for status in [200, 401, 503, 200, 200] {
            db.append_license_key_validation(
                "vct-rl-reranker",
                if status == 200 { Some("pro") } else { None },
                status,
                if status == 200 { None } else { Some("err") },
            )
            .expect("append");
            // Spread the validated_at timestamps so DESC ordering is unambiguous.
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        db.trim_license_key_validations("vct-rl-reranker", 3).expect("trim");
        let rows = db
            .recent_license_key_validations("vct-rl-reranker", 10)
            .expect("recent");
        assert_eq!(rows.len(), 3, "trim must cap at 3 most-recent rows");
        // The two latest are both http_status=200 per the seed order.
        assert_eq!(rows[0].http_status, 200);
        assert_eq!(rows[1].http_status, 200);
    }

    #[test]
    fn keychain_username_for_orchestrator_returns_legacy_constant() {
        // Promotion path: rewriting the orchestrator slot keeps the
        // legacy keychain entry so downgrades to pre-L1 launcher still
        // find it under 'VIBECODED_LICENSE_KEY'.
        assert_eq!(
            keychain_username_for(ORCHESTRATOR_MODULE_ID),
            LEGACY_KEYCHAIN_USERNAME
        );
        assert_eq!(
            keychain_username_for("vct-rl-reranker"),
            "license_key__vct-rl-reranker"
        );
    }

    #[test]
    fn key_prefix_of_takes_first_12_chars() {
        assert_eq!(key_prefix_of("vct_pro_abcdefghij"), "vct_pro_abcd");
        // Short keys pass through unchanged.
        assert_eq!(key_prefix_of("short"), "short");
    }
}
