// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Where each BUNDLED module setting's live value actually lives (v0.2.97).
//!
//! A manifest `settings` entry is a promise that the value matters. The
//! launcher's settings editor (Preferences → Modules → Module settings)
//! must never offer a field whose stored value nothing reads, and must never
//! keep a second copy of a value that has its own home. So every setting the
//! bundled manifests declare is bound here to exactly one of:
//!
//! * [`SettingBinding::Stored`] — the value lives in `module_settings` and a
//!   named reader reads it there; the editor edits it (and
//!   `set_module_setting` stores it).
//! * [`SettingBinding::Elsewhere`] — the live value has a canonical home
//!   outside `module_settings` (a project's KG binding, `app_state`, the
//!   machine's service configuration). The editor shows the LIVE value
//!   read-only and names the home; `set_module_setting` refuses to store a
//!   copy.
//!
//! A test fails when a bundled setting is missing from the table, so a new
//! manifest setting cannot ship without a decision about its reader.
//!
//! Catalog (installed) modules' settings are all [`SettingBinding::Stored`]:
//! their module reads them through the hub's `/env` (per project) or its
//! runtime environment.

use serde::Serialize;

/// Where an [`SettingBinding::Elsewhere`] value is read from, for display.
/// The launcher resolves each variant (`commands::module_gui`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LiveSource {
    /// The project's primary KG binding (`project_kg_bindings`, role
    /// `primary`) — what the hub's `/config` serves as `kg_collection`.
    ProjectKgCollection,
    /// The machine's shared KG collection (`app_state`
    /// `shared_kg.collection_name`, else the orchestrator root's binding) —
    /// `shared_kg_binding::resolve_shared_kg_collection`.
    SharedKgCollection,
    /// The Weaviate URL the hub's `/config` serves and every project's env
    /// carries (`services::service_endpoints::machine_weaviate_url`: the
    /// launcher.db `service_endpoints` row, else the compiled default).
    WeaviateUrl,
    /// The code-embedding service port (its `service_endpoints` row, else
    /// 11440), as the project env projection resolves it.
    CodeEmbedPort,
    /// `CODE_EMBED_BACKEND` in `<orchestrator root>/infrastructure/.env` —
    /// the value docker-compose gives the code-embedding container.
    CodeEmbedBackend,
    /// The device the code-embedding container is started with; the compose
    /// file fixes it to `auto` (the service auto-detects).
    CodeEmbedDevice,
}

/// How one setting's value reaches the thing that uses it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SettingBinding {
    /// Stored in `module_settings`; `reader` names what reads it there.
    Stored { reader: &'static str },
    /// The live value lives elsewhere: shown read-only, never stored here.
    Elsewhere {
        live: LiveSource,
        /// Where the value is set, in plain words (shown under the field).
        home: &'static str,
        /// A launcher route that edits it, when one exists. `{project_id}`
        /// is replaced with the picked project.
        editor_route: Option<&'static str>,
        /// The button label for `editor_route`.
        editor_label: Option<&'static str>,
    },
}

impl SettingBinding {
    /// True when the settings editor may write this value.
    pub fn is_stored(&self) -> bool {
        matches!(self, SettingBinding::Stored { .. })
    }
}

/// The binding of every setting an installed (catalog) module declares.
pub const CATALOG_BINDING: SettingBinding = SettingBinding::Stored {
    reader: "the module itself: the hub's /env for each project it is installed in, \
             or its runtime environment",
};

/// Every setting the bundled manifests (`launcher/bundled_manifests/`)
/// declare, with its binding. One row per (module id, key).
pub const BUNDLED_SETTING_BINDINGS: &[(&str, &str, SettingBinding)] = &[
    (
        "vct-hub-api",
        "VCT_HUB_PORT",
        SettingBinding::Stored {
            reader: "vct-hub at start (server.rs::bind_port reads the machine-wide row)",
        },
    ),
    (
        "vct-session-state",
        "CONTEXT_STATE_MAX_LINES",
        SettingBinding::Stored {
            reader: "the context-size-check hook, through the hub's /env",
        },
    ),
    (
        "vct-session-state",
        "MEMORY_MAX_LINES",
        SettingBinding::Stored {
            reader: "the context-size-check hook, through the hub's /env",
        },
    ),
    (
        "vct-kg",
        "KG_COLLECTION",
        SettingBinding::Elsewhere {
            live: LiveSource::ProjectKgCollection,
            home: "The project's primary KG binding, which the hub serves to the KG MCP. \
                   Edited on the project's Identity tab.",
            editor_route: Some("/project/{project_id}"),
            editor_label: Some("Open the project (Identity tab)"),
        },
    ),
    (
        "vct-kg",
        "SHARED_KG_COLLECTION",
        SettingBinding::Elsewhere {
            live: LiveSource::SharedKgCollection,
            home: "One shared collection for this computer. Picked with “Manage shared KG \
                   collection” on a project's Identity tab.",
            editor_route: Some("/project/{project_id}"),
            editor_label: Some("Open the project (Identity tab)"),
        },
    ),
    (
        "vct-kg",
        "WEAVIATE_URL",
        SettingBinding::Elsewhere {
            live: LiveSource::WeaviateUrl,
            home: "One Weaviate for this computer, recorded in the launcher database when \
                   VCO is installed or updated (the Weaviate it runs, or the one it adopted); \
                   default http://localhost:8081. `python -m vco_lib.service_endpoints show` \
                   prints it.",
            editor_route: None,
            editor_label: None,
        },
    ),
    (
        "vct-code-embedding",
        "CODE_EMBED_BACKEND",
        SettingBinding::Elsewhere {
            live: LiveSource::CodeEmbedBackend,
            home: "Chosen at install time and written to CODE_EMBED_BACKEND in the \
                   orchestrator's infrastructure/.env, which the code-embedding container \
                   reads when it is created. Re-run the installer to change it.",
            editor_route: None,
            editor_label: None,
        },
    ),
    (
        "vct-code-embedding",
        "CODE_EMBED_DEVICE",
        SettingBinding::Elsewhere {
            live: LiveSource::CodeEmbedDevice,
            home: "Fixed to `auto` by the code-embedding service's compose file; the service \
                   picks cuda / mps / cpu itself.",
            editor_route: None,
            editor_label: None,
        },
    ),
    (
        "vct-code-embedding",
        "CODE_EMBED_PORT",
        SettingBinding::Elsewhere {
            live: LiveSource::CodeEmbedPort,
            home: "The port the code-embedding service runs on, recorded in the launcher \
                   database when VCO is installed or updated (default 11440). \
                   `python -m vco_lib.service_endpoints show` prints it.",
            editor_route: Some("/services"),
            editor_label: Some("Open Services"),
        },
    ),
    (
        "vct-codegraph",
        "CODE_EMBED_BACKEND",
        SettingBinding::Elsewhere {
            live: LiveSource::CodeEmbedBackend,
            home: "The code-embedding service's backend (CODE_EMBED_BACKEND in the \
                   orchestrator's infrastructure/.env), chosen at install time. Re-run the \
                   installer to change it.",
            editor_route: None,
            editor_label: None,
        },
    ),
];

/// The binding of a BUNDLED module's setting, `None` when the table has no
/// row for it (a test keeps that from happening for a declared setting).
pub fn bundled_binding(module_id: &str, key: &str) -> Option<SettingBinding> {
    BUNDLED_SETTING_BINDINGS
        .iter()
        .find(|(m, k, _)| *m == module_id && *k == key)
        .map(|(_, _, b)| *b)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::module_settings_schema::bundled_module_settings;

    /// THE guard: a setting a bundled manifest declares must have a row here
    /// — either a named reader of the stored value, or the canonical home of
    /// its live value. A new manifest setting without one fails this test.
    #[test]
    fn every_bundled_setting_has_a_reader_or_a_canonical_home() {
        let mut missing = Vec::new();
        for m in bundled_module_settings() {
            for s in &m.settings {
                if bundled_binding(&m.module_id, &s.key).is_none() {
                    missing.push(format!("{}/{}", m.module_id, s.key));
                }
            }
        }
        assert!(missing.is_empty(), "bundled settings with no reader and no home: {missing:?}");
    }

    /// And the table names nothing the manifests do not declare (a stale row
    /// would describe a setting that no longer exists).
    #[test]
    fn every_table_row_is_a_declared_bundled_setting() {
        let modules = bundled_module_settings();
        for (m, k, _) in BUNDLED_SETTING_BINDINGS {
            assert!(
                crate::module_settings_schema::find_setting(&modules, m, k).is_some(),
                "{m}/{k} is in the binding table but no bundled manifest declares it"
            );
        }
        let mut keys: Vec<(&str, &str)> = BUNDLED_SETTING_BINDINGS.iter().map(|(m, k, _)| (*m, *k)).collect();
        keys.sort();
        keys.dedup();
        assert_eq!(keys.len(), BUNDLED_SETTING_BINDINGS.len(), "one row per setting");
    }

    /// A Stored binding names its reader; an Elsewhere one names its home.
    #[test]
    fn every_binding_explains_itself() {
        for (m, k, b) in BUNDLED_SETTING_BINDINGS {
            match b {
                SettingBinding::Stored { reader } => assert!(!reader.trim().is_empty(), "{m}/{k}"),
                SettingBinding::Elsewhere { home, editor_route, editor_label, .. } => {
                    assert!(!home.trim().is_empty(), "{m}/{k}");
                    assert_eq!(editor_route.is_some(), editor_label.is_some(), "{m}/{k}: route and label go together");
                }
            }
        }
    }
}
