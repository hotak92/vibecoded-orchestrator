// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Loader for `mcp_scan_rules.toml` — the cross-language MCP scan/
//! registration rule table (v0.2.83 WP-B4).
//!
//! Rust side of a tier-(B) shared-config loader (CLAUDE.md "Share, don't
//! mirror, cross-language logic"). The Python side lives at
//! `vco_lib/mcp_scan_rules.py`; both parse the SAME
//! `vco_lib/mcp_scan_rules.toml` with the SAME semantics so install.py and
//! the launcher agree on the MCP rule DATA (env-key allowlist, secret-shaped
//! needles, the bundled/default-composed MCP name sets, the deprecated-
//! default registry). A cross-language parity test
//! (`tests/test_mcp_scan_rules_parity.py` + `tests/mcp_scan_rules_parity.rs`)
//! keeps them in lockstep — the same triangulation shape used for
//! `bundled_versions.rs` / `orchestrator-managed-paths.txt`.
//!
//! ## Embedding — compile-time, zero runtime failure mode
//!
//! The .toml is embedded into the binary at COMPILE time via `include_str!`,
//! mirroring `bundled_versions.rs`. This is the deliberate choice for WP-B4:
//! the launcher is the REPAIR tool — it must build these rule sets even when
//! the project venv is broken, so it cannot shell out to Python (tier A) to
//! get them, and it must not depend on the on-disk .toml being present at
//! runtime. `include_str!` bakes the table into the binary from the repo the
//! launcher is built from — there is no missing-file arm at run time. A
//! malformed embedded table is caught by `cargo test` in every CI run (the
//! unit tests below) and by the compile-time-embedded `LazyLock` panicking
//! on first access.
//!
//! ## Schema
//!
//! See `mcp_scan_rules.toml` itself for the canonical schema doc. Rust models
//! only the sections it consumes today (env allowlist, needles, default entry
//! names). Unknown / not-yet-consumed sections (`[bundled]`, `[deprecated.*]`)
//! are tolerated by serde (`deny_unknown_fields` is NOT set) — add an
//! accessor when a Rust consumer grows for them.

use std::sync::LazyLock;

use serde::Deserialize;

/// Embedded copy of the .toml, read at compile time.
///
/// Path: 4 levels up from this file
/// (`src` → `vct-launcher-core` → `src-tauri` → `launcher` → repo root),
/// then down into `vco_lib/` — the SAME shape as
/// `bundled_versions.rs`'s `include_str!`. The .toml lives under `vco_lib/`
/// so it ships in the Python wheel; the include path must follow any future
/// move in lockstep.
const MCP_SCAN_RULES_TOML: &str =
    include_str!("../../../../vco_lib/mcp_scan_rules.toml");

/// The format version this loader knows how to read. A future schema
/// extension bumps the .toml version and this constant in the same commit.
const SUPPORTED_FORMAT_VERSION: u32 = 1;

/// Errors raised by [`parse_str`].
///
/// We do NOT depend on `thiserror`/`anyhow` (mirrors `bundled_versions.rs`).
#[derive(Debug)]
pub enum McpScanRulesError {
    /// TOML parse error (malformed syntax or schema mismatch).
    ParseFailed { message: String },
    /// `format_version` is not the version this loader supports.
    UnsupportedVersion { found: Option<u32>, supported: u32 },
}

impl std::fmt::Display for McpScanRulesError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ParseFailed { message } => write!(
                f,
                "Malformed MCP scan-rules table: {}. This file is the \
                 cross-language source of truth for MCP registration rule \
                 data. Re-fetch from \
                 https://github.com/hotak92/vibecoded-orchestrator.",
                message,
            ),
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "MCP scan-rules table has format_version {:?}, but this \
                 loader supports {}. Coordinate the schema bump across the \
                 Python loader (mcp_scan_rules.py) and the parity tests.",
                found, supported,
            ),
        }
    }
}

impl std::error::Error for McpScanRulesError {}

/// The parsed rule table (only the sections Rust consumes today).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpScanRules {
    /// [env].allowed_global_keys — order-significant.
    pub allowed_global_env_keys: Vec<String>,
    /// [env].secret_shaped_needles.
    pub secret_shaped_needles: Vec<String>,
    /// [entries].default_names — order-significant (builder emit order).
    pub default_mcp_entry_names: Vec<String>,
    /// [bundled].all_names — every orchestrator-shipped MCP name (superset of
    /// default_mcp_entry_names). The source-of-truth for
    /// `project_mcp_servers::BUNDLED_MCP_NAMES` (v0.2.83 WP-B5). Kept sorted.
    pub bundled_mcp_names: Vec<String>,
    /// [bundled].default_disabled — bundled MCPs that ship default-disabled
    /// per project. The source-of-truth for
    /// `project_mcp_servers::BUNDLED_MCP_DEFAULT_DISABLED` (WP-B5). Subset of
    /// bundled_mcp_names.
    pub bundled_mcp_default_disabled: Vec<String>,
}

// ── Wire schema (serde) ────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RawTable {
    format_version: Option<u32>,
    env: RawEnv,
    entries: RawEntries,
    // Optional at the WIRE level so the loader's minimal round-trip test
    // fixtures (which omit [bundled]) still parse; the REAL embedded table
    // always carries it, and `bundled_mcp_names_load_expected_values` pins
    // that. A malformed/partial [bundled] in the real file surfaces as an
    // empty slice, which the same-crate WP-B5 drift test flags immediately.
    #[serde(default)]
    bundled: RawBundled,
}

#[derive(Debug, Deserialize)]
struct RawEnv {
    allowed_global_keys: Vec<String>,
    secret_shaped_needles: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RawEntries {
    default_names: Vec<String>,
}

#[derive(Debug, Default, Deserialize)]
struct RawBundled {
    #[serde(default)]
    all_names: Vec<String>,
    #[serde(default)]
    default_disabled: Vec<String>,
}

/// Parse a TOML string into [`McpScanRules`]. Validates `format_version`.
///
/// Exposed for direct testing and for the parity test's file-read path;
/// production callers usually go through [`RULES`].
pub fn parse_str(toml_text: &str) -> Result<McpScanRules, McpScanRulesError> {
    let raw: RawTable = toml::from_str(toml_text).map_err(|e| {
        McpScanRulesError::ParseFailed { message: e.to_string() }
    })?;
    if raw.format_version != Some(SUPPORTED_FORMAT_VERSION) {
        return Err(McpScanRulesError::UnsupportedVersion {
            found: raw.format_version,
            supported: SUPPORTED_FORMAT_VERSION,
        });
    }
    Ok(McpScanRules {
        allowed_global_env_keys: raw.env.allowed_global_keys,
        secret_shaped_needles: raw.env.secret_shaped_needles,
        default_mcp_entry_names: raw.entries.default_names,
        bundled_mcp_names: raw.bundled.all_names,
        bundled_mcp_default_disabled: raw.bundled.default_disabled,
    })
}

/// Compile-time-embedded rules. Panics on first access if the build-time
/// `mcp_scan_rules.toml` is malformed — by design (the build would otherwise
/// ship a binary that crashes later with no early signal). `cargo test`
/// catches this in every CI run via the unit tests below. Mirrors
/// `bundled_versions::BUNDLED_NPM`.
pub static RULES: LazyLock<McpScanRules> = LazyLock::new(|| {
    parse_str(MCP_SCAN_RULES_TOML).unwrap_or_else(|e| {
        panic!(
            "embedded mcp_scan_rules.toml failed to load at \
             compile-time-embedded load: {}. This is a build-time error \
             promoted to runtime; rebuild the launcher binary against a \
             fixed table.",
            e
        )
    })
});

/// Env keys that MAY be written into `~/.claude.json mcpServers.*.env`.
/// Everything else is dropped. Order-significant (equality-sensitive).
pub fn allowed_global_env_keys() -> &'static [String] {
    &RULES.allowed_global_env_keys
}

/// Credential-shaped needle SEGMENTS (the DATA input for the
/// `is_secret_shaped_env_key` predicate; the segment-split + `KEY`/`*_KEY`
/// suffix logic itself stays in each language).
pub fn secret_shaped_needles() -> &'static [String] {
    &RULES.secret_shaped_needles
}

/// The MCP ids whose `~/.claude.json` entries are composed by the entry
/// builder (the registrar-owned / rewritable set). Order matches the
/// builder's emit order.
pub fn default_mcp_entry_names() -> &'static [String] {
    &RULES.default_mcp_entry_names
}

/// Every orchestrator-shipped MCP name (superset of
/// [`default_mcp_entry_names`]). Source-of-truth for
/// `project_mcp_servers::BUNDLED_MCP_NAMES` (v0.2.83 WP-B5).
pub fn bundled_mcp_names() -> &'static [String] {
    &RULES.bundled_mcp_names
}

/// Bundled MCPs that ship default-disabled per project. Source-of-truth for
/// `project_mcp_servers::BUNDLED_MCP_DEFAULT_DISABLED` (v0.2.83 WP-B5).
pub fn bundled_mcp_default_disabled() -> &'static [String] {
    &RULES.bundled_mcp_default_disabled
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Sanity: the embedded .toml loads without panic and exposes the
    /// expected values. If a future edit changes the table, update this
    /// assertion in the same commit as the .toml edit (mirrors the
    /// `bundled_npm_loads_expected_keys` discipline).
    #[test]
    fn embedded_rules_load_expected_values() {
        let r = &*RULES;
        assert_eq!(
            r.allowed_global_env_keys,
            vec![
                "WEAVIATE_URL",
                "OLLAMA_URL",
                "GRPC_PORT",
                "PYTHONPATH",
                "ACTIVE_EMBEDDING",
                "CODE_EMBED_SERVICE_URL",
            ],
        );
        assert_eq!(
            r.secret_shaped_needles,
            vec!["TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH"],
        );
        assert_eq!(
            r.default_mcp_entry_names,
            vec!["weaviate-kg", "search", "playwright", "mermaid", "excalidraw"],
        );
    }

    /// WP-B5: the [bundled] section drives project_mcp_servers'
    /// BUNDLED_MCP_NAMES / BUNDLED_MCP_DEFAULT_DISABLED. Pin the embedded
    /// values so a future table edit updates this assertion in the same
    /// commit (mirrors `embedded_rules_load_expected_values`).
    #[test]
    fn bundled_names_load_expected_values() {
        let r = &*RULES;
        assert_eq!(
            r.bundled_mcp_names,
            vec![
                "code-embedding",
                "excalidraw",
                "mermaid",
                "ollama",
                "playwright",
                "search",
                "vct-coordination",
                "weaviate-kg",
            ],
        );
        assert_eq!(r.bundled_mcp_default_disabled, vec!["excalidraw", "mermaid"]);
        // Accessors return the same slices.
        assert_eq!(bundled_mcp_names(), r.bundled_mcp_names.as_slice());
        assert_eq!(
            bundled_mcp_default_disabled(),
            r.bundled_mcp_default_disabled.as_slice()
        );
    }

    #[test]
    fn parse_str_round_trip() {
        let sample = r#"
format_version = 1
[env]
allowed_global_keys = ["A", "B"]
secret_shaped_needles = ["TOKEN"]
[entries]
default_names = ["x", "y"]
"#;
        let r = parse_str(sample).expect("parse ok");
        assert_eq!(r.allowed_global_env_keys, vec!["A", "B"]);
        assert_eq!(r.secret_shaped_needles, vec!["TOKEN"]);
        assert_eq!(r.default_mcp_entry_names, vec!["x", "y"]);
    }

    #[test]
    fn wrong_format_version_errors() {
        let sample = r#"
format_version = 999
[env]
allowed_global_keys = ["A"]
secret_shaped_needles = ["TOKEN"]
[entries]
default_names = ["x"]
"#;
        let err = parse_str(sample).expect_err("must reject unknown version");
        match err {
            McpScanRulesError::UnsupportedVersion { found, supported } => {
                assert_eq!(found, Some(999));
                assert_eq!(supported, 1);
            }
            other => panic!("expected UnsupportedVersion, got {:?}", other),
        }
    }

    #[test]
    fn missing_section_errors() {
        // No [entries] table → serde must reject.
        let sample = r#"
format_version = 1
[env]
allowed_global_keys = ["A"]
secret_shaped_needles = ["TOKEN"]
"#;
        let err = parse_str(sample).expect_err("missing [entries] must error");
        match err {
            McpScanRulesError::ParseFailed { .. } => {}
            other => panic!("expected ParseFailed, got {:?}", other),
        }
    }

    #[test]
    fn unknown_sections_tolerated() {
        // [bundled] / [deprecated.*] are in the real file and not modelled
        // by Rust today. Must not error (forward-compat).
        let sample = r#"
format_version = 1
[env]
allowed_global_keys = ["A"]
secret_shaped_needles = ["TOKEN"]
[entries]
default_names = ["x"]
[bundled]
all_names = ["x", "y", "z"]
default_disabled = ["y"]
[deprecated.ollama]
removed_in = "v0.2.11"
reason = "x"
opt_in_manifest = "y"
"#;
        let r = parse_str(sample).expect("unknown sections must parse cleanly");
        assert_eq!(r.default_mcp_entry_names, vec!["x"]);
    }
}
