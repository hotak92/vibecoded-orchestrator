// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Compiled mirror of `deferral_conditions.toml` — the deferral lifecycle
//! contract (v0.2.91 WP-B).
//!
//! Rust side of a tier-(B) shared-config loader (CLAUDE.md "Share, don't
//! mirror, cross-language logic"). The Python side lives at
//! `vco_lib/deferral_registry.py`; both parse the SAME
//! `vco_lib/deferral_conditions.toml` with the SAME lookup semantics, so
//! install.py and the launcher agree on every condition's DISPOSITION and
//! clear mechanism. Cross-language parity tests
//! (`tests/test_deferral_registry_parity_v0291.py` +
//! `launcher/src-tauri/tests/deferral_registry_parity.rs`) keep them in
//! lockstep — the same triangulation shape as `mcp_scan_rules.rs`.
//!
//! ## Embedding — compile-time, zero runtime failure mode
//!
//! The .toml is embedded at COMPILE time via `include_str!`, mirroring
//! `mcp_scan_rules.rs` / `bundled_versions.rs`. The launcher is the REPAIR
//! tool: it must be able to classify a deferral even when the project venv is
//! broken, so it cannot shell out to Python (tier A) for the table, and it must
//! not depend on the on-disk .toml existing at run time. A malformed embedded
//! table is caught by `cargo test` in every CI run.
//!
//! ## Why Rust needs this at all
//!
//! The launcher renders the deferral ledger and decides what to badge. Severity
//! alone cannot make that call — a `severity = info` row may be a completed
//! record ("no action needed") or genuinely pending work. `disposition_for`
//! gives the launcher the same answer Python computes, without a subprocess on
//! a boot path.
//!
//! ## Lookup semantics (MUST match `vco_lib/deferral_registry.py`)
//!
//! 1. exact table key;
//! 2. `match = "glob"` patterns by DESCENDING literal length (so
//!    `stale_unit_retired_*_backup_failed` beats `stale_unit_retired_*`);
//! 3. no match ⇒ `action_required` (conservative — an unclassified condition
//!    must surface as work, never hide in a collapsed fold).

use std::collections::BTreeMap;
use std::sync::LazyLock;

use serde::Deserialize;

/// Embedded copy of the .toml, read at compile time.
///
/// Path: 4 levels up from this file (`src` → `vct-launcher-core` →
/// `src-tauri` → `launcher` → repo root), then down into `vco_lib/` — the same
/// shape as `mcp_scan_rules.rs`'s `include_str!`. The include path must follow
/// any future move of the table in lockstep.
const DEFERRAL_CONDITIONS_TOML: &str =
    include_str!("../../../../vco_lib/deferral_conditions.toml");

/// The format version this loader knows how to read. A schema extension bumps
/// the .toml, this constant AND the Python loader in the same commit.
const SUPPORTED_FORMAT_VERSION: u32 = 1;

/// The disposition applied to any condition id absent from the table.
pub const DEFAULT_CLASS: &str = "action_required";

/// Errors raised by [`parse_str`]. No `thiserror`/`anyhow` (mirrors
/// `mcp_scan_rules.rs`).
#[derive(Debug)]
pub enum DeferralRegistryError {
    /// TOML parse error (malformed syntax or schema mismatch).
    ParseFailed { message: String },
    /// `format_version` is not the version this loader supports.
    UnsupportedVersion { found: Option<u32>, supported: u32 },
    /// A row failed schema validation (unknown class, bad match kind, …).
    InvalidRow { pattern: String, message: String },
}

impl std::fmt::Display for DeferralRegistryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ParseFailed { message } => write!(
                f,
                "Malformed deferral-conditions table: {}. This file is the \
                 cross-language source of truth for deferral dispositions and \
                 lifecycles. Re-fetch from \
                 https://github.com/hotak92/vibecoded-orchestrator.",
                message,
            ),
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "Deferral-conditions table has format_version {:?}, but this \
                 loader supports {}. Coordinate the schema bump across the \
                 Python loader (deferral_registry.py) and the parity tests.",
                found, supported,
            ),
            Self::InvalidRow { pattern, message } => write!(
                f,
                "Deferral-conditions row {:?} is invalid: {}",
                pattern, message,
            ),
        }
    }
}

impl std::error::Error for DeferralRegistryError {}

/// The four disposition tiers, in the same order as the Python `CLASSES`.
pub const CLASSES: [&str; 4] = [
    "action_required",
    "auto_retryable",
    "environmental",
    "informational_record",
];

/// One resolved registry row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConditionSpec {
    /// The table key: an exact condition id, or an fnmatch-style pattern.
    pub pattern: String,
    /// `"exact"` or `"glob"`.
    pub match_kind: String,
    /// One of [`CLASSES`].
    pub condition_class: String,
    /// Module / file that emits the condition.
    pub owner: String,
    /// A sentinel (`owned-drop-when-absent`, `bundle-reconciled`,
    /// `paired-resolution`, `manual-dismiss`) or `probe:{py,rs}:<name>`.
    pub clear_probe: String,
    /// Declared render/record surfaces.
    pub emit_surfaces: Vec<String>,
    /// Ordered stable field names forming the dismissal identity.
    pub dismiss_key: Vec<String>,
    /// `"active"` or `"retired"`.
    pub status: String,
}

impl ConditionSpec {
    /// True when install.py owns the row's drop-when-absent lifecycle.
    pub fn is_owned_by_install(&self) -> bool {
        self.clear_probe == "owned-drop-when-absent"
    }
}

/// The parsed table with its lookup order precomputed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeferralRegistry {
    exact: BTreeMap<String, ConditionSpec>,
    /// Sorted by DESCENDING literal length — most specific glob first.
    globs: Vec<ConditionSpec>,
}

impl DeferralRegistry {
    /// Resolve one concrete condition id: exact → longest glob → `None`.
    pub fn get(&self, condition_id: &str) -> Option<&ConditionSpec> {
        if let Some(hit) = self.exact.get(condition_id) {
            return Some(hit);
        }
        self.globs
            .iter()
            .find(|s| glob_matches(&s.pattern, condition_id))
    }

    /// Disposition tier for `condition_id`; unregistered ⇒ [`DEFAULT_CLASS`].
    pub fn disposition_for(&self, condition_id: &str) -> &str {
        self.get(condition_id)
            .map(|s| s.condition_class.as_str())
            .unwrap_or(DEFAULT_CLASS)
    }

    /// True when the condition asks a human to DO something.
    ///
    /// `auto_retryable` counts as work OWED (VCO can do it itself), so a
    /// badge that means "you have things to do" uses
    /// [`Self::is_actionable`] instead.
    pub fn is_action_required(&self, condition_id: &str) -> bool {
        self.disposition_for(condition_id) == "action_required"
    }

    /// True when the condition represents outstanding work of any kind
    /// (`action_required` or `auto_retryable`). MUST match the Python
    /// `split_by_disposition` partition.
    pub fn is_actionable(&self, condition_id: &str) -> bool {
        matches!(
            self.disposition_for(condition_id),
            "action_required" | "auto_retryable"
        )
    }

    /// Every table key, sorted — for parity tests and tooling.
    pub fn patterns(&self) -> Vec<String> {
        let mut out: Vec<String> =
            self.exact.keys().cloned().chain(self.globs.iter().map(|s| s.pattern.clone())).collect();
        out.sort();
        out
    }

    /// Exact ids install.py owns (drop-when-absent), sorted.
    pub fn install_owned_ids(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .exact
            .values()
            .filter(|s| s.is_owned_by_install())
            .map(|s| s.pattern.clone())
            .collect();
        out.sort();
        out
    }

    /// Owned `literal*` families as literal prefixes, sorted. Mirrors the
    /// Python accessor: a glob with an INTERIOR wildcard contributes nothing
    /// (its parent prefix already covers ownership, which is a `starts_with`
    /// test on both sides).
    pub fn install_owned_prefixes(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .globs
            .iter()
            .filter(|s| s.is_owned_by_install())
            .filter(|s| s.pattern.matches('*').count() == 1 && s.pattern.ends_with('*'))
            .map(|s| s.pattern[..s.pattern.len() - 1].to_string())
            .collect();
        out.sort();
        out.dedup();
        out
    }
}

/// Number of non-wildcard characters — the glob specificity ranking. MUST
/// match the Python `_literal_length`.
fn literal_length(pattern: &str) -> usize {
    pattern.chars().filter(|c| *c != '*').count()
}

/// Minimal fnmatch: `*` matches any run of characters (including empty);
/// every other character matches literally. The table only ever uses `*`
/// (validated by the parity test), so `?`/`[...]` are deliberately NOT
/// supported — a silent divergence from Python's `fnmatchcase` would be worse
/// than a missing feature.
fn glob_matches(pattern: &str, value: &str) -> bool {
    let parts: Vec<&str> = pattern.split('*').collect();
    if parts.len() == 1 {
        return pattern == value;
    }
    let mut rest = value;
    // First segment must be a prefix.
    let first = parts[0];
    if !rest.starts_with(first) {
        return false;
    }
    rest = &rest[first.len()..];
    // Last segment must be a suffix (checked after the middles are consumed).
    let last = parts[parts.len() - 1];
    for mid in &parts[1..parts.len() - 1] {
        if mid.is_empty() {
            continue;
        }
        match rest.find(mid) {
            Some(idx) => rest = &rest[idx + mid.len()..],
            None => return false,
        }
    }
    rest.len() >= last.len() && rest.ends_with(last)
}

// ── Wire schema (serde) ────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RawTable {
    format_version: Option<u32>,
    conditions: BTreeMap<String, RawCondition>,
}

#[derive(Debug, Deserialize)]
struct RawCondition {
    #[serde(default = "default_match")]
    r#match: String,
    class: String,
    owner: String,
    clear_probe: String,
    #[serde(default)]
    emit_surfaces: Vec<String>,
    #[serde(default)]
    dismiss_key: Vec<String>,
    #[serde(default = "default_status")]
    status: String,
    // `notes` is documentation only — tolerated and ignored.
    #[serde(default)]
    #[allow(dead_code)]
    notes: String,
}

fn default_match() -> String {
    "exact".to_string()
}

fn default_status() -> String {
    "active".to_string()
}

/// Parse a TOML string into a [`DeferralRegistry`], validating every row.
///
/// Exposed for direct testing and for the parity test's file-read path;
/// production callers go through [`REGISTRY`].
///
/// ## The validation asymmetry with Python is DELIBERATE
///
/// This loader checks what it must to build a sound lookup: known `class`,
/// known `match` kind, `match` agreeing with the presence of a `*`, and a
/// non-empty `emit_surfaces`. It deliberately does NOT re-validate what the
/// Python loader (`vco_lib/deferral_registry.py`) already validates — every
/// `emit_surfaces` value against the surface enum, the `clear_probe` sentinel /
/// `probe:{py,rs}:<name>` shape, `status` membership, and (via
/// `tests/test_deferral_registry_completeness_v0291.py`) that every
/// `probe:py:` name actually resolves to a function.
///
/// Why that is safe rather than a gap: there is ONE table, it is embedded at
/// compile time, and the Python loader + its pytest suite gate CI on every
/// push. A row that would fail the stricter checks cannot reach a release — it
/// fails CI first. Mirroring the checks here would be a second implementation
/// of the same rules in a second language, which is exactly the divergence risk
/// the tier-(B) shared-config split exists to avoid. Add a check here only when
/// this loader itself needs the guarantee to stay sound.
pub fn parse_str(toml_text: &str) -> Result<DeferralRegistry, DeferralRegistryError> {
    let raw: RawTable = toml::from_str(toml_text)
        .map_err(|e| DeferralRegistryError::ParseFailed { message: e.to_string() })?;
    if raw.format_version != Some(SUPPORTED_FORMAT_VERSION) {
        return Err(DeferralRegistryError::UnsupportedVersion {
            found: raw.format_version,
            supported: SUPPORTED_FORMAT_VERSION,
        });
    }

    let mut exact: BTreeMap<String, ConditionSpec> = BTreeMap::new();
    let mut globs: Vec<ConditionSpec> = Vec::new();

    for (pattern, row) in raw.conditions {
        if !CLASSES.contains(&row.class.as_str()) {
            return Err(DeferralRegistryError::InvalidRow {
                pattern,
                message: format!("unknown class {:?}", row.class),
            });
        }
        if row.r#match != "exact" && row.r#match != "glob" {
            return Err(DeferralRegistryError::InvalidRow {
                pattern,
                message: format!("unknown match kind {:?}", row.r#match),
            });
        }
        if row.emit_surfaces.is_empty() {
            return Err(DeferralRegistryError::InvalidRow {
                pattern,
                message: "emit_surfaces must be non-empty".to_string(),
            });
        }
        let is_glob = row.r#match == "glob";
        if is_glob != pattern.contains('*') {
            return Err(DeferralRegistryError::InvalidRow {
                pattern: pattern.clone(),
                message: format!(
                    "match={:?} disagrees with the presence of a '*' wildcard",
                    row.r#match
                ),
            });
        }
        let spec = ConditionSpec {
            pattern: pattern.clone(),
            match_kind: row.r#match,
            condition_class: row.class,
            owner: row.owner,
            clear_probe: row.clear_probe,
            emit_surfaces: row.emit_surfaces,
            dismiss_key: row.dismiss_key,
            status: row.status,
        };
        if is_glob {
            globs.push(spec);
        } else {
            exact.insert(pattern, spec);
        }
    }

    // Descending literal length, then pattern, for a deterministic order that
    // matches the Python sort key exactly.
    globs.sort_by(|a, b| {
        literal_length(&b.pattern)
            .cmp(&literal_length(&a.pattern))
            .then_with(|| a.pattern.cmp(&b.pattern))
    });

    Ok(DeferralRegistry { exact, globs })
}

/// Compile-time-embedded registry. Panics on first access if the build-time
/// table is malformed — by design (the build would otherwise ship a binary
/// that fails later with no early signal). `cargo test` catches it in CI.
pub static REGISTRY: LazyLock<DeferralRegistry> = LazyLock::new(|| {
    parse_str(DEFERRAL_CONDITIONS_TOML).unwrap_or_else(|e| {
        panic!(
            "embedded deferral_conditions.toml failed to load: {}. This is a \
             build-time error promoted to runtime; rebuild against a fixed table.",
            e
        )
    })
});

/// Disposition tier for `condition_id`; unregistered ⇒ [`DEFAULT_CLASS`].
pub fn disposition_for(condition_id: &str) -> &'static str {
    REGISTRY
        .get(condition_id)
        .map(|s| s.condition_class.as_str())
        .unwrap_or(DEFAULT_CLASS)
}

/// True when the condition represents outstanding work of any kind.
pub fn is_actionable(condition_id: &str) -> bool {
    REGISTRY.is_actionable(condition_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The embedded table loads and classifies the conditions the launcher
    /// itself emits. If a future edit changes these rows, update the
    /// assertion in the same commit (the `mcp_scan_rules` discipline).
    #[test]
    fn embedded_registry_classifies_launcher_emitted_conditions() {
        let r = &*REGISTRY;
        assert_eq!(r.disposition_for("launcher_binary_stale"), "action_required");
        assert_eq!(
            r.disposition_for("launcher_binary_handoff_skipped_dirty"),
            "action_required"
        );
        assert_eq!(
            r.disposition_for("launcher_binary_clobber_averted"),
            "informational_record"
        );
        assert_eq!(
            r.disposition_for("kg_access_phantom_repaired"),
            "informational_record"
        );
        assert_eq!(
            r.disposition_for("orchestrator_user_modified_preserved"),
            "action_required"
        );
        assert_eq!(r.disposition_for("dual_ollama_detected"), "environmental");
    }

    /// An unregistered id is `action_required`, never quietly demoted.
    #[test]
    fn unregistered_condition_is_action_required() {
        assert_eq!(disposition_for("totally_unknown_condition"), DEFAULT_CLASS);
        assert!(is_actionable("totally_unknown_condition"));
    }

    /// The longer glob wins: a retirement RECORD is informational, but a
    /// retirement whose BACKUP failed is actionable.
    #[test]
    fn longest_glob_wins() {
        let r = &*REGISTRY;
        assert_eq!(
            r.disposition_for("stale_unit_retired_vct_hub"),
            "informational_record"
        );
        assert_eq!(
            r.disposition_for("stale_unit_retired_vct_hub_backup_failed"),
            "action_required"
        );
    }

    /// `auto_retryable` is owed work, so it groups with actionable.
    #[test]
    fn auto_retryable_counts_as_actionable() {
        let r = &*REGISTRY;
        assert_eq!(
            r.disposition_for("kg_sync_no_embedding_backend"),
            "auto_retryable"
        );
        assert!(r.is_actionable("kg_sync_no_embedding_backend"));
        assert!(!r.is_action_required("kg_sync_no_embedding_backend"));
        assert!(!r.is_actionable("kg_access_phantom_repaired"));
    }

    #[test]
    fn glob_matcher_semantics() {
        assert!(glob_matches("abc_*", "abc_def"));
        assert!(glob_matches("abc_*", "abc_"));
        assert!(!glob_matches("abc_*", "abd_def"));
        assert!(glob_matches("a_*_z", "a_mid_z"));
        assert!(!glob_matches("a_*_z", "a_mid_y"));
        assert!(glob_matches("exact", "exact"));
        assert!(!glob_matches("exact", "exactly"));
    }

    #[test]
    fn parse_str_round_trip() {
        let sample = r#"
format_version = 1
[conditions.some_cid]
class = "environmental"
owner = "install.py"
clear_probe = "manual-dismiss"
emit_surfaces = ["ledger"]
[conditions."fam_*"]
match = "glob"
class = "informational_record"
owner = "install.py"
clear_probe = "owned-drop-when-absent"
emit_surfaces = ["ledger"]
"#;
        let r = parse_str(sample).expect("parse ok");
        assert_eq!(r.disposition_for("some_cid"), "environmental");
        assert_eq!(r.disposition_for("fam_x"), "informational_record");
        assert_eq!(r.install_owned_prefixes(), vec!["fam_".to_string()]);
        assert!(r.install_owned_ids().is_empty());
    }

    #[test]
    fn wrong_format_version_errors() {
        let sample = r#"
format_version = 999
[conditions.x]
class = "environmental"
owner = "o"
clear_probe = "manual-dismiss"
emit_surfaces = ["ledger"]
"#;
        match parse_str(sample).expect_err("must reject unknown version") {
            DeferralRegistryError::UnsupportedVersion { found, supported } => {
                assert_eq!(found, Some(999));
                assert_eq!(supported, 1);
            }
            other => panic!("expected UnsupportedVersion, got {:?}", other),
        }
    }

    #[test]
    fn unknown_class_is_rejected() {
        let sample = r#"
format_version = 1
[conditions.x]
class = "not_a_tier"
owner = "o"
clear_probe = "manual-dismiss"
emit_surfaces = ["ledger"]
"#;
        match parse_str(sample).expect_err("must reject unknown class") {
            DeferralRegistryError::InvalidRow { pattern, .. } => {
                assert_eq!(pattern, "x");
            }
            other => panic!("expected InvalidRow, got {:?}", other),
        }
    }

    #[test]
    fn match_kind_must_agree_with_wildcard() {
        let sample = r#"
format_version = 1
[conditions."x_*"]
class = "environmental"
owner = "o"
clear_probe = "manual-dismiss"
emit_surfaces = ["ledger"]
"#;
        match parse_str(sample).expect_err("glob pattern needs match='glob'") {
            DeferralRegistryError::InvalidRow { pattern, .. } => {
                assert_eq!(pattern, "x_*");
            }
            other => panic!("expected InvalidRow, got {:?}", other),
        }
    }
}
