//! Orchestrator manifest (`vct-module.json`) parsing.
//!
//! Moved to `vct-launcher-core` in v0.2.21 (Step 4a) because both the
//! launcher's `commands::modules` (catalog rendering) AND the detached
//! `vct-hub` binary (resolver / `/projects/{id}/env`) consume this
//! manifest. Pre-Step-4 these lived in `launcher/src-tauri/src/commands/
//! modules.rs` and the in-launcher hub reached across with
//! `crate::commands::modules::read_orchestrator_manifest`. Once the hub
//! becomes a separate binary that crate-cross is gone.
//!
//! Privacy note (2026-05-06): `find_orchestrator_manifest` deliberately
//! uses `std::env::current_exe()` rather than `env!("CARGO_MANIFEST_DIR
//! ")` so the developer's build-host path is NOT embedded as a static
//! string in the release binary.

use std::path::PathBuf;

use serde::Deserialize;

/// Subset of `vct-module.json` the orchestrator core actually reads.
///
/// Deliberately a SUPERSET-tolerant deserializer: anything extra in the
/// JSON is ignored. Only `version`, `description`, `components`, and
/// `bundled_secrets` are load-bearing. `id` + `name` exist in
/// `vct-module.json` but the launcher only renders version/description
/// + components in the catalog.
#[derive(Debug, Deserialize)]
pub struct OrchestratorManifest {
    pub version: String,
    pub description: String,
    #[serde(default)]
    pub components: Vec<OrchestratorComponent>,
    /// Orchestrator-level secrets surfaced by the launcher core. Read
    /// by the hub's `/api/v1/projects/{id}/env` resolver for every
    /// `host=base` project.
    #[serde(default)]
    pub bundled_secrets: Vec<OrchestratorBundledSecret>,
}

#[derive(Debug, Deserialize)]
pub struct OrchestratorComponent {
    pub id: String,
    pub name: String,
    pub description: String,
}

/// Single entry in `OrchestratorManifest::bundled_secrets`. Declares a
/// secret the orchestrator core knows about and the hub should resolve
/// against the launcher's keychain for every base-host project.
#[derive(Debug, Deserialize)]
pub struct OrchestratorBundledSecret {
    pub key: String,
    pub scope: String,
    #[serde(default = "default_orchestrator_secret_module_id")]
    pub module_id: String,
    #[serde(default)]
    #[allow(dead_code)]
    pub description: String,
}

fn default_orchestrator_secret_module_id() -> String {
    "user".to_string()
}

/// Find `vct-module.json` at the repo root by walking up from the
/// running binary. Handles both shipped binaries (`<clone>/launcher/
/// dist/<arch>/vct-launcher`, walks up 4 levels) and `cargo run` builds
/// (`<clone>/launcher/src-tauri/target/<profile>/`, walks up 4-5
/// levels — both find the clone root).
pub fn find_orchestrator_manifest() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut p = exe.parent()?;
    loop {
        let candidate = p.join("vct-module.json");
        if candidate.is_file() {
            return Some(candidate);
        }
        match p.parent() {
            Some(parent) => p = parent,
            None => return None,
        }
    }
}

pub fn read_orchestrator_manifest() -> Option<OrchestratorManifest> {
    let path = find_orchestrator_manifest()?;
    let raw = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}
