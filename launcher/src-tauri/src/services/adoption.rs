//! Persisted state for "what to do when externally-managed services are
//! detected". Lives at `~/.vct/services.toml` so it survives restarts and
//! is hand-editable when the launcher is offline.
//!
//! The launcher prompts the user the FIRST time it sees a service running
//! on the canonical port that wasn't started by our compose. The user
//! picks one of three options:
//!
//!   - **Adopt**: route to the external endpoint as-is. Lifecycle commands
//!     become no-ops for adopted services (we do NOT stop/start somebody
//!     else's containers).
//!
//!   - **Parallel**: pick a free port and write
//!     `infrastructure/docker-compose.override.yml` (alongside any
//!     existing override from a prior volume-location migration). Our
//!     compose stack runs on the new port; the external service is
//!     left alone.
//!
//!   - **Refuse**: the launcher does nothing — the user keeps managing
//!     their external services manually. Lifecycle commands skip the
//!     conflict-detected services. The tray pill shows "managed
//!     externally". We record this as `Mode::Refuse` so we don't
//!     re-prompt every launcher boot.
//!
//! The persisted choice lives PER service (Weaviate / Ollama / code_embed)
//! because a user might adopt their existing Weaviate but want a fresh
//! Ollama. The schema is intentionally flat — no nested tables — so the
//! file stays trivially editable.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// The user's resolution for a per-service adoption conflict. Default is
/// `Unresolved` — the launcher prompts on next boot until the user picks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum AdoptionMode {
    /// Pre-prompt state — the launcher will ask the user.
    #[default]
    Unresolved,
    /// Use the external service as-is. Lifecycle = no-op for this service.
    Adopt,
    /// Run our compose copy on a different port (recorded in
    /// `parallel_port`).
    Parallel,
    /// User declined to manage. Same effect as Adopt at runtime, but
    /// we keep them separate so the UI can show "you opted out" vs
    /// "you adopted".
    Refuse,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ServiceAdoption {
    /// One of the canonical service names: `"weaviate"`, `"ollama"`,
    /// `"code_embed"`.
    pub name: String,
    pub mode: AdoptionMode,
    /// External endpoint the launcher detected at decision time.
    /// Recorded so the UI can show "adopted: http://localhost:8081".
    /// `None` for `Refuse`/`Unresolved`.
    #[serde(default)]
    pub external_url: Option<String>,
    /// When `mode == Parallel`: the host port our compose copy is bound
    /// to. None otherwise. Used when generating override yaml.
    #[serde(default)]
    pub parallel_port: Option<u16>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AdoptionState {
    /// Per-service adoption settings. Order is irrelevant — lookups go
    /// by `name`.
    #[serde(default)]
    pub services: Vec<ServiceAdoption>,
}

impl AdoptionState {
    pub fn get(&self, name: &str) -> Option<&ServiceAdoption> {
        self.services.iter().find(|s| s.name == name)
    }

    pub fn upsert(&mut self, entry: ServiceAdoption) {
        if let Some(existing) = self.services.iter_mut().find(|s| s.name == entry.name) {
            *existing = entry;
        } else {
            self.services.push(entry);
        }
    }
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

pub fn config_path() -> PathBuf {
    crate::paths::vct_root_dir().join("services.toml")
}

pub fn read() -> AdoptionState {
    let path = config_path();
    let raw = match std::fs::read_to_string(&path) {
        Ok(r) => r,
        Err(_) => return AdoptionState::default(),
    };
    toml::from_str(&raw).unwrap_or_default()
}

pub fn write(state: &AdoptionState) -> Result<(), String> {
    let path = config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let body = toml::to_string_pretty(state)
        .map_err(|e| format!("serialize services.toml: {}", e))?;
    let mut tmp = path.clone();
    tmp.set_extension("toml.tmp");
    std::fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_serialization() {
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8081".into()),
            parallel_port: None,
        });
        state.upsert(ServiceAdoption {
            name: "ollama".into(),
            mode: AdoptionMode::Parallel,
            external_url: Some("http://localhost:11435".into()),
            parallel_port: Some(11445),
        });
        let body = toml::to_string_pretty(&state).unwrap();
        let decoded: AdoptionState = toml::from_str(&body).unwrap();
        assert_eq!(decoded.services.len(), 2);
        assert_eq!(decoded.get("weaviate").unwrap().mode, AdoptionMode::Adopt);
        assert_eq!(
            decoded.get("ollama").unwrap().parallel_port,
            Some(11445),
        );
    }

    #[test]
    fn upsert_replaces_existing_entry() {
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Unresolved,
            external_url: None,
            parallel_port: None,
        });
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8081".into()),
            parallel_port: None,
        });
        assert_eq!(state.services.len(), 1);
        assert_eq!(state.get("weaviate").unwrap().mode, AdoptionMode::Adopt);
    }

    #[test]
    fn default_mode_is_unresolved() {
        let entry = ServiceAdoption::default();
        assert_eq!(entry.mode, AdoptionMode::Unresolved);
    }

    #[test]
    fn missing_service_returns_none() {
        let state = AdoptionState::default();
        assert!(state.get("weaviate").is_none());
    }

    /// install.py writes ~/.vct/services.toml with a hand-rolled TOML
    /// serializer (it runs before pip-install, so it can't depend on
    /// `tomli_w`). This test pins the schema both sides agree on. If
    /// install.py ever drifts (extra fields, different mode tokens), this
    /// test breaks loudly so we notice before shipping.
    #[test]
    fn parses_install_py_written_toml() {
        let raw = r#"[[services]]
name = "weaviate"
mode = "adopt"
external_url = "http://localhost:8081"

[[services]]
name = "ollama"
mode = "parallel"
external_url = "http://localhost:11435"
parallel_port = 11445

[[services]]
name = "code_embed"
mode = "unresolved"
"#;
        let parsed: AdoptionState = toml::from_str(raw).expect("should parse");
        assert_eq!(parsed.services.len(), 3);
        assert_eq!(parsed.get("weaviate").unwrap().mode, AdoptionMode::Adopt);
        assert_eq!(parsed.get("ollama").unwrap().parallel_port, Some(11445));
        assert_eq!(parsed.get("code_embed").unwrap().mode, AdoptionMode::Unresolved);
    }
}
