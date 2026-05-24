// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Loader for `bundled_mcp_versions.toml` (Phase 0 of the
//! diagrams-integration plan, 2026-05-24).
//!
//! Rust side of the cross-language pinning-manifest loader. The Python
//! side lives at `vco_lib/bundled_versions.py`; both parse the SAME
//! file with the SAME semantics so install.py and the launcher agree
//! on what's pinned. A cross-language parity test
//! (`tests/test_bundled_versions_parity.py`) keeps them in lockstep —
//! same triangulation shape used for `orchestrator-managed-paths.txt`.
//!
//! ## Embedding
//!
//! The .toml is embedded into the binary at compile time via
//! `include_str!`, mirroring `installer.rs`'s pattern for
//! `orchestrator-managed-paths.txt`. This keeps the parsed-at-startup
//! defaults binary-resident and immune to drift between the launcher
//! the user installed and the .toml file in the orchestrator clone the
//! launcher is operating on. The launcher passes the bundled
//! orchestrator's freshly-read .toml across via `reload_from_path`
//! when needed (e.g. operating on an install_path different from where
//! the launcher was built).
//!
//! ## Schema
//!
//! See `bundled_mcp_versions.toml` itself for the canonical schema doc.
//! For Rust callers: every `[npm.<key>]` table parses into a
//! [`PinnedPackage`] with `package`, `version`, and `shasum` strings.
//! Unknown top-level sections (e.g. `[chromium]`) are tolerated by the
//! TOML parser but not exposed here — Rust callers today only need the
//! npm slice. Add an accessor when a new section grows a real Rust
//! consumer.

use std::collections::HashMap;
use std::path::Path;
use std::sync::LazyLock;

use serde::Deserialize;

/// Embedded copy of the .toml, read at compile time.
///
/// Path: 4 levels up from this file
/// (`src` → `vct-launcher-core` → `src-tauri` → `launcher` → repo root).
/// Same depth as `installer.rs`'s `ORCHESTRATOR_MANAGED_PATHS_TXT`
/// include path.
const BUNDLED_VERSIONS_TOML: &str =
    include_str!("../../../../bundled_mcp_versions.toml");

/// Errors raised by [`reload_from_path`] and the lazy initialiser.
///
/// We do NOT depend on `thiserror`/`anyhow` — neither is in the
/// `vct-launcher-core` workspace deps and a hand-written enum keeps
/// the dependency footprint flat. Display impls mirror the Python
/// `RuntimeError` messages so both languages produce the same
/// recovery wording.
#[derive(Debug)]
pub enum BundledVersionsError {
    /// File could not be read (missing, perms, IO error).
    ReadFailed { path: String, source: std::io::Error },
    /// TOML parse error (malformed syntax or schema mismatch).
    ParseFailed { path: String, message: String },
}

impl std::fmt::Display for BundledVersionsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ReadFailed { path, source } => write!(
                f,
                "Could not read bundled-versions manifest at {}: {}. \
                 This file pins external npm package versions for \
                 install.py. Re-fetch from \
                 https://github.com/hotak92/vibecoded-orchestrator.",
                path, source,
            ),
            Self::ParseFailed { path, message } => write!(
                f,
                "Malformed bundled-versions manifest at {}: {}.",
                path, message,
            ),
        }
    }
}

impl std::error::Error for BundledVersionsError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::ReadFailed { source, .. } => Some(source),
            Self::ParseFailed { .. } => None,
        }
    }
}

/// One pinned npm package: name + exact version + SHA-1 integrity hash.
///
/// `shasum` is npm's NATIVE integrity field (40-char SHA-1, not SHA-256
/// — see the .toml file's schema doc for rationale). Field name
/// deliberately matches the .toml key so `serde(rename)` is not needed.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct PinnedPackage {
    /// The npm key inside `[npm.<key>]` — e.g. "mermaid_mcp". Populated
    /// by [`parse_str`] after deserialisation since serde does not
    /// receive the table key as a field; never present in the .toml
    /// itself.
    #[serde(skip)]
    pub key: String,

    /// The npm package name (e.g. "claude-mermaid").
    pub package: String,

    /// Exact semver — NO leading `^` or `~`. Install-time resolver is
    /// disabled, so the installed version must match this byte-for-byte.
    pub version: String,

    /// `dist.shasum` from `npm view <pkg>@<version>`. SHA-1, 40
    /// lowercase hex chars.
    pub shasum: String,
}

/// Top-level TOML schema. Only the `npm` table is modelled in Rust
/// today; unknown sections (e.g. `[chromium]`) are tolerated by serde's
/// default behaviour (`deny_unknown_fields` is NOT set).
#[derive(Debug, Deserialize)]
struct BundledManifestRaw {
    #[serde(default)]
    npm: HashMap<String, PinnedPackageRaw>,
}

#[derive(Debug, Deserialize)]
struct PinnedPackageRaw {
    package: String,
    version: String,
    shasum: String,
}

/// Parse a TOML string into the `{key -> PinnedPackage}` map for the
/// `[npm.*]` tables.
///
/// Exposed for direct testing and for `reload_from_path`'s file-read
/// path; production callers usually go through [`BUNDLED_NPM`].
pub fn parse_str(toml_text: &str) -> Result<HashMap<String, PinnedPackage>, BundledVersionsError> {
    let raw: BundledManifestRaw = toml::from_str(toml_text).map_err(|e| {
        BundledVersionsError::ParseFailed {
            path: "<embedded>".to_string(),
            message: e.to_string(),
        }
    })?;

    let mut out: HashMap<String, PinnedPackage> = HashMap::with_capacity(raw.npm.len());
    for (key, pkg) in raw.npm {
        out.insert(
            key.clone(),
            PinnedPackage {
                key,
                package: pkg.package,
                version: pkg.version,
                shasum: pkg.shasum,
            },
        );
    }
    Ok(out)
}

/// Parse the .toml file at `path` (read from disk, NOT the embedded
/// copy). Use when operating on an orchestrator clone whose .toml may
/// differ from the launcher binary's embedded copy (e.g. the launcher
/// was built against v0.2.32 but the user just `git pull`-ed v0.2.33
/// of the orchestrator repo).
pub fn reload_from_path(path: &Path) -> Result<HashMap<String, PinnedPackage>, BundledVersionsError> {
    let text = std::fs::read_to_string(path).map_err(|source| {
        BundledVersionsError::ReadFailed {
            path: path.display().to_string(),
            source,
        }
    })?;
    parse_str(&text).map_err(|e| match e {
        // Re-tag the path so error messages name the on-disk file
        // rather than "<embedded>".
        BundledVersionsError::ParseFailed { message, .. } => {
            BundledVersionsError::ParseFailed {
                path: path.display().to_string(),
                message,
            }
        }
        other => other,
    })
}

/// Compile-time-embedded pinned npm packages. Panics on first access if
/// the build-time `bundled_mcp_versions.toml` is malformed — that is by
/// design (the build would otherwise ship a binary that crashes at
/// install time with no early signal). `cargo test` catches this in
/// every CI run via the unit tests below.
pub static BUNDLED_NPM: LazyLock<HashMap<String, PinnedPackage>> = LazyLock::new(|| {
    parse_str(BUNDLED_VERSIONS_TOML).unwrap_or_else(|e| {
        panic!(
            "embedded bundled_mcp_versions.toml failed to parse at \
             compile-time-embedded load: {}. This is a build-time \
             error promoted to runtime; rebuild the launcher binary \
             against a fixed manifest.",
            e
        )
    })
});

#[cfg(test)]
mod tests {
    use super::*;

    /// Sanity check: the embedded .toml loads without panic and exposes
    /// the expected pinned-package keys. If a future bump renames a
    /// key, update this assertion in the same commit as the .toml edit
    /// (mirrors the discipline used for `ORCHESTRATOR_MANAGED_PATHS`).
    #[test]
    fn bundled_npm_loads_expected_keys() {
        let map = &*BUNDLED_NPM;
        let mut keys: Vec<&str> = map.keys().map(|s| s.as_str()).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec![
                "excalidraw_lib",
                "excalidraw_mcp",
                "mermaid_lib",
                "mermaid_mcp",
            ],
        );
    }

    #[test]
    fn pinned_package_round_trip() {
        let sample = r#"
[npm.mermaid_mcp]
package = "claude-mermaid"
version = "1.6.3"
shasum  = "a5f1050ef7af6dc2595f5507366006489fef2879"
"#;
        let map = parse_str(sample).expect("parse_str ok");
        let entry = map.get("mermaid_mcp").expect("mermaid_mcp present");
        assert_eq!(entry.key, "mermaid_mcp");
        assert_eq!(entry.package, "claude-mermaid");
        assert_eq!(entry.version, "1.6.3");
        assert_eq!(entry.shasum, "a5f1050ef7af6dc2595f5507366006489fef2879");
    }

    #[test]
    fn empty_input_yields_empty_npm_map() {
        let map = parse_str("").expect("parse_str ok on empty input");
        assert!(map.is_empty());
    }

    #[test]
    fn unknown_top_level_sections_are_tolerated() {
        // Forward-compat: `[chromium]` is present in the real file and
        // not modelled by Rust today. Must not error.
        let sample = r#"
[npm.mermaid_mcp]
package = "claude-mermaid"
version = "1.6.3"
shasum  = "a5f1050ef7af6dc2595f5507366006489fef2879"

[chromium]
reuse_playwright = true

[future_section_we_have_not_invented_yet]
foo = "bar"
"#;
        let map = parse_str(sample).expect("unknown sections must parse cleanly");
        assert!(map.contains_key("mermaid_mcp"));
        assert_eq!(map.len(), 1, "only npm.* keys should populate the npm map");
    }

    #[test]
    fn missing_required_field_errors_out() {
        // No `shasum` → serde must reject.
        let sample = r#"
[npm.broken]
package = "x"
version = "1.0.0"
"#;
        let err = parse_str(sample).expect_err("missing shasum must error");
        match err {
            BundledVersionsError::ParseFailed { .. } => {}
            other => panic!("expected ParseFailed, got {:?}", other),
        }
    }

    #[test]
    fn reload_from_path_reads_disk_file() {
        let tmp = tempfile::NamedTempFile::new().expect("tempfile");
        std::fs::write(
            tmp.path(),
            r#"
[npm.x]
package = "x"
version = "0.0.1"
shasum  = "0000000000000000000000000000000000000000"
"#,
        )
        .expect("write");
        let map = reload_from_path(tmp.path()).expect("reload_from_path ok");
        assert!(map.contains_key("x"));
        assert_eq!(map.get("x").unwrap().version, "0.0.1");
    }

    #[test]
    fn reload_from_missing_path_errors_out() {
        let bogus = std::path::PathBuf::from("/nonexistent/bundled_mcp_versions.toml");
        let err = reload_from_path(&bogus).expect_err("missing file must error");
        match err {
            BundledVersionsError::ReadFailed { .. } => {}
            other => panic!("expected ReadFailed, got {:?}", other),
        }
    }
}
