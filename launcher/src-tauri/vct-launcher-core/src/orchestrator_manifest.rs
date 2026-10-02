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
//! Privacy note (2026-05-06): the clone is resolved from
//! `std::env::current_exe()` (through `services::install_root`), never
//! `env!("CARGO_MANIFEST_DIR")`, so the developer's build-host path is NOT
//! embedded as a static string in the release binary.

use std::path::{Path, PathBuf};

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
    /// v0.2.98 slot-exclusivity: a bundled secret declared with
    /// `scope == "shared" && slot_exclusive == true` OWNS its key name on
    /// the hub's `/api/v1/projects/{id}/env` merged dict — the key is
    /// served only by that declaration's own shared-slot read
    /// (`resolve_module_secret(..., "shared", ...)` at the
    /// SENTINEL_SHARED slot), never by a narrower or wider bucket under
    /// the same name (installed-module declaration, legacy slot, user
    /// bucket, cross-project grant). Absent/false keeps the historical
    /// first-wins bucket merge. Owner ruling it implements: VCO's own
    /// consumers use VCO's own slot; a project's key is the project's.
    #[serde(default)]
    pub slot_exclusive: bool,
}

fn default_orchestrator_secret_module_id() -> String {
    "user".to_string()
}

/// `vct-module.json` of the orchestrator clone this binary belongs to.
///
/// v0.2.100 (F-W1-09, AD-2): a delegation to the ONE install-root resolver
/// (`services::install_root`). This used to be a SECOND resolver — an
/// unbounded walk to the first `vct-module.json` above the exe, with no
/// identity check — so a binary sitting under ANY directory carrying a
/// module manifest (another VCT module, a user project with a bundled copy)
/// took that directory for the clone. Now: the bounded (8-level) exe walk
/// accepting only a `vct-module.json` whose id is `orchestrator`, then the
/// process-level cache the launcher sets at boot. Shipped binaries
/// (`<clone>/launcher/dist/<arch>/…`) and `cargo` builds
/// (`<clone>/launcher/src-tauri/target/<profile>/…`) both resolve as before.
pub fn find_orchestrator_manifest() -> Option<PathBuf> {
    orchestrator_install_root().map(|root| root.join("vct-module.json"))
}

/// The orchestrator clone root — the ONE core answer to "where is this
/// binary's install", used where `state/install/runtime.txt` must be read
/// (`services::runtime`, the hub supervisor) and by the hub's gateway /
/// hook-enforcement subprocess `cwd`. See [`find_orchestrator_manifest`].
pub fn orchestrator_install_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    orchestrator_install_root_for(&exe)
}

/// [`orchestrator_install_root`] for an explicit exe path (tests).
pub fn orchestrator_install_root_for(exe: &Path) -> Option<PathBuf> {
    crate::services::install_root::resolve_without_db(exe)
        .ok()
        .map(|r| r.path)
}

pub fn read_orchestrator_manifest() -> Option<OrchestratorManifest> {
    let path = find_orchestrator_manifest()?;
    let raw = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(p: &Path, body: &str) {
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, body).unwrap();
    }

    /// Identity-true case: a binary in the shipped dist layout resolves its
    /// clone, and the manifest path is that clone's `vct-module.json` — the
    /// behaviour every caller relied on.
    #[test]
    fn resolves_the_orchestrator_clone_above_a_dist_binary() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("clone");
        write(
            &root.join("vct-module.json"),
            r#"{"id":"orchestrator","version":"0.2.100","description":"x"}"#,
        );
        let exe = root.join("launcher/dist/linux-x64/vct-hub");
        write(&exe, "");
        assert_eq!(orchestrator_install_root_for(&exe), Some(root));
    }

    /// The finding (F-W1-09): the old unbounded walk took the FIRST
    /// `vct-module.json` above the exe, whatever module it described. A
    /// manifest with another id is not the clone and is never returned.
    #[test]
    fn refuses_a_directory_whose_manifest_is_not_the_orchestrator() {
        let tmp = tempfile::tempdir().unwrap();
        let other = tmp.path().join("some-module");
        write(
            &other.join("vct-module.json"),
            r#"{"id":"rl-retrieval","version":"1.0.0","description":"x"}"#,
        );
        let exe = other.join("bin/vct-hub");
        write(&exe, "");
        assert_ne!(orchestrator_install_root_for(&exe), Some(other));
    }

    /// The bound: a clone more than `MAX_WALK_LEVELS` above the exe is not
    /// reached by the walk (the old loop walked to `/`).
    #[test]
    fn the_walk_is_bounded() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("clone");
        write(
            &root.join("vct-module.json"),
            r#"{"id":"orchestrator","version":"0.2.100","description":"x"}"#,
        );
        let exe = root.join("a/b/c/d/e/f/g/h/i/j/vct-hub");
        write(&exe, "");
        assert_ne!(orchestrator_install_root_for(&exe), Some(root));
    }
}
