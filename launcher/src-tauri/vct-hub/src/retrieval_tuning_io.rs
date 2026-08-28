// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Hub-side reader for `<vct_root_dir>/retrieval-tuning.toml`.
//!
//! v0.2.22 Item #13 (2026-05-20). The launcher's Tauri command in
//! `launcher/src-tauri/src/commands/retrieval_tuning.rs` is the
//! writer side; this module is the reader side embedded in the
//! resolver response (`config_api.rs`). Both ends read the same file
//! via the same `vct_launcher_core::paths::vct_root_dir()` lookup, so
//! a value written through the GUI is visible to every headless
//! consumer on the next resolver round-trip.
//!
//! The schema (`RetrievalTuning` struct + field defaults) is
//! deliberately duplicated between the two sides. The Tauri-side
//! crate isn't a dependency of vct-hub (and vice versa); sharing a
//! type would require either lifting the schema into
//! `vct-launcher-core` (adds a workspace-wide public surface that the
//! launcher already owns) or pulling vct-hub into the launcher crate
//! (the whole point of v0.2.21 was to detach them). The wire shape
//! is tiny — five f64s — so the duplication is auditable: edit the
//! launcher-side struct, edit this struct, the integration test in
//! `vco_lib/test_project_config_retrieval_tuning_roundtrip.py`
//! catches drift.
//!
//! Defaults: MUST match
//! `knowledge/concepts/score-driven-retrieval-tiers.md`. Pinned in
//! constants here and in the launcher-side
//! `commands::retrieval_tuning`. The values are also pinned by unit
//! tests in both crates so a one-side edit is caught by CI.

use std::path::Path;

use serde::{Deserialize, Serialize};

use vct_launcher_core::paths::vct_root_dir;

const DEFAULT_CODE_GRAPH_SCORE_FLOOR: f64 = 0.35;
const DEFAULT_KG_TIER_MIN: f64 = 0.42;
const DEFAULT_KG_TIER_SINGLE_CHUNK: f64 = 0.55;
const DEFAULT_KG_TIER_THREE_CHUNKS: f64 = 0.65;
const DEFAULT_KG_TIER_FULL: f64 = 0.75;

const RETRIEVAL_TUNING_FILENAME: &str = "retrieval-tuning.toml";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct RetrievalTuning {
    pub code_graph_score_floor: f64,
    pub kg_tier_min: f64,
    pub kg_tier_single_chunk: f64,
    pub kg_tier_three_chunks: f64,
    pub kg_tier_full: f64,
}

impl Default for RetrievalTuning {
    fn default() -> Self {
        Self {
            code_graph_score_floor: DEFAULT_CODE_GRAPH_SCORE_FLOOR,
            kg_tier_min: DEFAULT_KG_TIER_MIN,
            kg_tier_single_chunk: DEFAULT_KG_TIER_SINGLE_CHUNK,
            kg_tier_three_chunks: DEFAULT_KG_TIER_THREE_CHUNKS,
            kg_tier_full: DEFAULT_KG_TIER_FULL,
        }
    }
}

impl RetrievalTuning {
    /// Defensive validation: same invariant the launcher-side enforces.
    /// A hand-edited file with broken ordering falls back to defaults
    /// rather than poisoning the resolver response.
    fn is_consistent(&self) -> bool {
        let in_range = [
            self.code_graph_score_floor,
            self.kg_tier_min,
            self.kg_tier_single_chunk,
            self.kg_tier_three_chunks,
            self.kg_tier_full,
        ]
        .iter()
        .all(|v| v.is_finite() && (0.0..=1.0).contains(v));
        in_range
            && self.kg_tier_min < self.kg_tier_single_chunk
            && self.kg_tier_single_chunk < self.kg_tier_three_chunks
            && self.kg_tier_three_chunks < self.kg_tier_full
    }
}

/// Read the canonical retrieval tuning file. Missing OR malformed OR
/// invariant-violating → defaults. Soft-fail throughout: the resolver
/// must always return SOMETHING usable, never bubble a parse error.
pub fn read_tuning() -> RetrievalTuning {
    let path = vct_root_dir().join(RETRIEVAL_TUNING_FILENAME);
    read_tuning_from(&path)
}

/// Test-friendly seam — accepts an explicit path so unit tests can
/// drive the reader with a tempdir without touching `VCT_STATE_DIR`.
pub fn read_tuning_from(path: &Path) -> RetrievalTuning {
    let Ok(raw) = std::fs::read_to_string(path) else {
        return RetrievalTuning::default();
    };
    let Ok(parsed) = toml::from_str::<RetrievalTuning>(&raw) else {
        tracing::warn!(
            path = %path.display(),
            "[vct-hub retrieval_tuning] could not parse as TOML; using defaults"
        );
        return RetrievalTuning::default();
    };
    if !parsed.is_consistent() {
        tracing::warn!(
            path = %path.display(),
            "[vct-hub retrieval_tuning] failed invariant check; using defaults"
        );
        return RetrievalTuning::default();
    }
    parsed
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn defaults_match_calibrated_constants() {
        let d = RetrievalTuning::default();
        assert!((d.code_graph_score_floor - 0.35).abs() < 1e-9);
        assert!((d.kg_tier_min - 0.42).abs() < 1e-9);
        assert!((d.kg_tier_single_chunk - 0.55).abs() < 1e-9);
        assert!((d.kg_tier_three_chunks - 0.65).abs() < 1e-9);
        assert!((d.kg_tier_full - 0.75).abs() < 1e-9);
    }

    #[test]
    fn defaults_are_consistent() {
        assert!(RetrievalTuning::default().is_consistent());
    }

    #[test]
    fn missing_file_yields_defaults() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        let t = read_tuning_from(&path);
        assert_eq!(t, RetrievalTuning::default());
    }

    #[test]
    fn malformed_file_yields_defaults() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        std::fs::write(&path, "this is not toml [[[").unwrap();
        let t = read_tuning_from(&path);
        assert_eq!(t, RetrievalTuning::default());
    }

    #[test]
    fn well_formed_file_round_trips() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        let body = "\
code_graph_score_floor = 0.4
kg_tier_min = 0.5
kg_tier_single_chunk = 0.6
kg_tier_three_chunks = 0.7
kg_tier_full = 0.8
";
        std::fs::write(&path, body).unwrap();
        let t = read_tuning_from(&path);
        assert!((t.code_graph_score_floor - 0.4).abs() < 1e-9);
        assert!((t.kg_tier_min - 0.5).abs() < 1e-9);
        assert!((t.kg_tier_full - 0.8).abs() < 1e-9);
    }

    #[test]
    fn invariant_violation_yields_defaults() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        // kg_tier_min (0.9) > kg_tier_single_chunk (0.55) — broken.
        let body = "\
code_graph_score_floor = 0.35
kg_tier_min = 0.9
kg_tier_single_chunk = 0.55
kg_tier_three_chunks = 0.65
kg_tier_full = 0.75
";
        std::fs::write(&path, body).unwrap();
        let t = read_tuning_from(&path);
        assert_eq!(t, RetrievalTuning::default());
    }

    #[test]
    fn out_of_range_yields_defaults() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        let body = "\
code_graph_score_floor = -0.1
kg_tier_min = 0.42
kg_tier_single_chunk = 0.55
kg_tier_three_chunks = 0.65
kg_tier_full = 0.75
";
        std::fs::write(&path, body).unwrap();
        let t = read_tuning_from(&path);
        assert_eq!(t, RetrievalTuning::default());
    }
}
