//! Audit log row type (separated from the Tauri command surface so
//! vct-launcher-core's `Db::audit_list` can return it without depending
//! on launcher-only modules).
//!
//! v0.2.21 (Step 3d): hoisted from `commands::audit::AuditEvent` to break
//! the `core → commands` reverse dependency caught by `cargo check`
//! during the workspace split. The launcher's `commands::audit` re-exports
//! this type so existing imports in lib.rs / the frontend bindings stay
//! valid; only the DEFINITION moved.

use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct AuditEvent {
    pub id: i64,
    pub operation: String,
    pub project_id: Option<String>,
    pub module_id: Option<String>,
    /// JSON string. Frontend may parse it for display.
    pub detail: String,
    /// OS user who performed the operation. "system" for pre-migration
    /// rows, "unknown" if the launcher could not resolve $USER.
    pub actor: String,
    /// Unix epoch milliseconds.
    pub created_at: i64,
}
