// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Canonical `module_settings` addressing constants — ONE home for the
//! module-id namespace and the per-project setting-key names that MULTIPLE
//! surfaces (Rust launcher writers, Rust hub readers, Python env-projection
//! readers) must agree on byte-for-byte.
//!
//! ## Why this module exists (2026-07-14 split-brain incident)
//!
//! The shared-KG gate flags (`shared_kg_read_disabled` /
//! `shared_kg_write_disabled`) were stored under TWO DIFFERENT module_id
//! namespaces by two different populations:
//!
//!   * WRITER — the launcher GUI setter/getter
//!     (`commands/projects_v2.rs`, `set_shared_kg_read_disabled` /
//!     `set_shared_kg_write_disabled` / `get_shared_kg_read_disabled`)
//!     wrote to `module_settings(project_id, "__project__", key)`.
//!   * READERS — the hub `/config` resolver
//!     (`vct-hub/src/config_api.rs`) and the Python env projection
//!     (`vco_lib/config_projection.py`) both read
//!     `module_settings(project_id, "orchestrator-core", key)`.
//!
//! Same key, two module_id namespaces, two reader populations. The GUI
//! toggle wrote a row the readers never looked at, so the flag read as a
//! permanently-false toggle on the hub + Python side. The launcher-side
//! Python env writer (`commands/project_env_settings.rs`, via
//! `get_shared_kg_*_disabled`) DID read `"__project__"`, so the split was
//! partial: `.claude/env` reflected the toggle, but the hub `/config`
//! resolver and the standalone Python projection did not.
//!
//! The canonical module_id is [`ORCHESTRATOR_CORE_MODULE_ID`]
//! (`"orchestrator-core"`) — chosen because it is what the hub resolver +
//! Python projection already read, and what the hub's own round-trip tests
//! pin (`config_api.rs` `config_emits_shared_kg_read_disabled_when_set`).
//! The writers in `projects_v2.rs` are repointed to it; a launcher.db
//! migration MOVES existing `"__project__"` rows for these keys onto the
//! canonical id (migration 040).
//!
//! ## Consumer inventory (every surface that must use these constants)
//!
//!   * WRITERS (Rust launcher app crate — repointed to this module):
//!       - `commands/projects_v2.rs::set_shared_kg_write_disabled`
//!       - `commands/projects_v2.rs::set_shared_kg_read_disabled`
//!       - `commands/projects_v2.rs::get_shared_kg_read_disabled_cmd` (getter)
//!       - `commands/projects_v2/shared_kg_settings.rs` (get/migrate helpers)
//!       - `db/secret_scope_policy.rs` (the GAP-2 secrets read gate — new)
//!   * READERS (Rust hub — swapped to this module at integration):
//!       - `vct-hub/src/config_api.rs` (the `orchestrator-core` literals)
//!   * READERS (Python — locked to these values by a parity test):
//!       - `vco_lib/config_projection.py` (`_fetch_module_setting_bool`
//!         calls with `"orchestrator-core"` + the key strings).
//!         Cross-language lock: `tests/test_module_settings_keys_parity.py`
//!         fails if the Python literals ever diverge from these consts
//!         (tier-B of the A>B>C sharing rule: one committed rule table both
//!         sides read, guarded by a parity test).
//!
//! Do NOT reintroduce a second module_id string for any of these keys. If a
//! new per-project flag joins this family, add its key here and wire every
//! surface to this module.

/// Canonical `module_settings.module_id` for per-project orchestrator-core
/// settings (shared-KG gates, embedding pick, dual-write flags, and — since
/// 2026-07-14 — the shared-secrets read gate).
///
/// This REPLACES the divergent `"__project__"` sentinel that
/// `projects_v2.rs::PROJECT_SETTINGS_MODULE_ID` used for the KG gates. The
/// two strings were the split-brain root; `"orchestrator-core"` wins because
/// it is what the hub resolver + Python projection already read.
pub const ORCHESTRATOR_CORE_MODULE_ID: &str = "orchestrator-core";

/// Per-project key: gate WRITES to the cross-project shared KG
/// (`store_knowledge_node(scope='shared')`). Reads stay unconditional.
pub const SETTING_KEY_SHARED_KG_WRITE_DISABLED: &str = "shared_kg_write_disabled";

/// Per-project key: gate READS of the cross-project shared KG (v0.2.46
/// Decision B). When `true`, the MCP drops the shared collection from
/// hybrid_search / semantic_graph_search fan-out for this project.
pub const SETTING_KEY_SHARED_KG_READ_DISABLED: &str = "shared_kg_read_disabled";

/// Per-project key: bulk opt-out from the SHARED user-secrets bucket
/// (GAP-2, 2026-07-14). When `true`, the resolver omits the shared bucket
/// from this project's `/env` user-secret pairs — the secrets analog of
/// [`SETTING_KEY_SHARED_KG_READ_DISABLED`]. Infrastructure/module-manifest
/// shared secrets are NOT gated (per-key pause covers those).
pub const SETTING_KEY_SHARED_SECRETS_READ_DISABLED: &str = "shared_secrets_read_disabled";

#[cfg(test)]
mod tests {
    use super::*;

    /// Pin the canonical strings so any accidental rename surfaces loudly
    /// here AND in the cross-language parity test.
    #[test]
    fn canonical_constants_are_stable() {
        assert_eq!(ORCHESTRATOR_CORE_MODULE_ID, "orchestrator-core");
        assert_eq!(SETTING_KEY_SHARED_KG_WRITE_DISABLED, "shared_kg_write_disabled");
        assert_eq!(SETTING_KEY_SHARED_KG_READ_DISABLED, "shared_kg_read_disabled");
        assert_eq!(
            SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
            "shared_secrets_read_disabled"
        );
    }

    /// The canonical module_id must NOT be the legacy split-brain sentinel.
    /// A regression that repoints this back to `"__project__"` would silently
    /// re-open the 2026-07-14 divergence.
    #[test]
    fn canonical_module_id_is_not_the_legacy_sentinel() {
        assert_ne!(
            ORCHESTRATOR_CORE_MODULE_ID, "__project__",
            "orchestrator-core module_id must not regress to the split-brain sentinel"
        );
    }
}
