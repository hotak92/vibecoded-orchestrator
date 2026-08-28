// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Retrieval tuning settings — global thresholds for score-driven
//! retrieval verbosity (KG) and codegraph injection floor.
//!
//! v0.2.22 Item #13 (2026-05-20). Surfaces five env-tunable knobs the
//! orchestrator's retrieval pipelines already honour
//! (`KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` /
//! `KG_TIER_FULL` from `score-driven-retrieval-tiers.md`, plus
//! `VCO_CODE_GRAPH_SCORE_FLOOR` from the pre-edit injection hook) as
//! GUI sliders.
//!
//! ─── Architecture ────────────────────────────────────────────────────
//!
//! Persistence: `<vct_root_dir>/retrieval-tuning.toml`. Atomic write
//! via `*.tmp` + rename. Missing / malformed file → defaults (matches
//! the storage_ux.rs pattern; we don't auto-delete bad files so the
//! user can recover them manually).
//!
//! Resolver chain (Dev Constraint #5 — root + per-project surface):
//!
//!   GUI slider → set tauri command → write TOML
//!                                   ↓
//!     hub.config_api → reads TOML → adds fields to /config response
//!                                   ↓
//!         resolver clients (.sh / .ps1 / .py) → hooks / MCPs see same
//!                                                values the GUI shows
//!
//! The hub-side resolver code lives in
//! `launcher/src-tauri/vct-hub/src/config_api.rs`. Both this Tauri
//! command AND that hub handler read the SAME file via the SAME
//! `vct_root_dir()` lookup (the path resolution function ships in
//! `vct-launcher-core/src/paths.rs` and is `pub use`d by the launcher
//! root). Single source of truth → no GUI-vs-hub drift.
//!
//! ─── Validation invariant ────────────────────────────────────────────
//!
//! The four KG tier thresholds MUST satisfy
//! `kg_tier_min < kg_tier_single_chunk < kg_tier_three_chunks < kg_tier_full`.
//! `retrieval_tuning_set` validates client-supplied values against this
//! invariant AND the [0.0, 1.0] range and refuses (Err) any violation.
//! NO auto-clamp — user data integrity. The Svelte panel performs the
//! same checks client-side so the user gets immediate inline feedback,
//! but the Rust side is the authoritative gate.
//!
//! Codegraph floor is independent (no ordering vs the KG tiers — it's
//! a separate axis); validated for [0.0, 1.0] only.
//!
//! ─── Defaults ────────────────────────────────────────────────────────
//!
//! Defaults come from `score-driven-retrieval-tiers.md` (KG tiers,
//! calibrated 2026-04-10) and the existing
//! `VCO_CODE_GRAPH_SCORE_FLOOR=0.35` used by the pre-edit hook. DO NOT
//! change these defaults — they're the calibrated values the existing
//! pipeline expects when no override is present.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::command;

use vct_launcher_core::paths::vct_root_dir;

// ─── Defaults ────────────────────────────────────────────────────────────
//
// MUST match the values referenced in
// `knowledge/concepts/score-driven-retrieval-tiers.md` and the
// pre-edit hook's `VCO_CODE_GRAPH_SCORE_FLOOR` fallback. Treated as
// the source of truth: env-var consumers (claude_mcp_servers/...,
// hook scripts) only override these when an explicit env var is set.

const DEFAULT_CODE_GRAPH_SCORE_FLOOR: f64 = 0.35;
const DEFAULT_KG_TIER_MIN: f64 = 0.42;
const DEFAULT_KG_TIER_SINGLE_CHUNK: f64 = 0.55;
const DEFAULT_KG_TIER_THREE_CHUNKS: f64 = 0.65;
const DEFAULT_KG_TIER_FULL: f64 = 0.75;

const RETRIEVAL_TUNING_FILENAME: &str = "retrieval-tuning.toml";

// ─── Wire type ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct RetrievalTuning {
    /// Min cosine score for codegraph hits to be injected by the
    /// pre-edit hook. Below this → discarded. Independent of KG tiers.
    pub code_graph_score_floor: f64,
    /// KG: below this → discard result entirely (noise).
    pub kg_tier_min: f64,
    /// KG: above this → render single matched chunk (~2000 chars).
    pub kg_tier_single_chunk: f64,
    /// KG: above this → render matched chunk + 2 neighbours.
    pub kg_tier_three_chunks: f64,
    /// KG: above this → render whole node (up to 7 chunks).
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
    /// Validate the [0.0, 1.0] range on every field and the strict-
    /// ordering invariant across the four KG tier thresholds.
    /// Returns `Err(msg)` with a human-readable reason on the first
    /// violation found (fail-fast — the GUI surfaces ONE error per
    /// submission rather than a list).
    pub fn validate(&self) -> Result<(), String> {
        let fields: [(&str, f64); 5] = [
            ("code_graph_score_floor", self.code_graph_score_floor),
            ("kg_tier_min", self.kg_tier_min),
            ("kg_tier_single_chunk", self.kg_tier_single_chunk),
            ("kg_tier_three_chunks", self.kg_tier_three_chunks),
            ("kg_tier_full", self.kg_tier_full),
        ];
        for (name, val) in fields {
            if !val.is_finite() {
                return Err(format!("{} is not a finite number: {}", name, val));
            }
            if !(0.0..=1.0).contains(&val) {
                return Err(format!(
                    "{} must be in [0.0, 1.0], got {}",
                    name, val
                ));
            }
        }
        // Strict ordering across the four KG tiers — `<`, not `<=`.
        // Equal thresholds would create a zero-width band that's
        // unreachable, so reject them as a user mistake.
        if !(self.kg_tier_min < self.kg_tier_single_chunk) {
            return Err(format!(
                "kg_tier_min ({}) must be strictly less than kg_tier_single_chunk ({})",
                self.kg_tier_min, self.kg_tier_single_chunk
            ));
        }
        if !(self.kg_tier_single_chunk < self.kg_tier_three_chunks) {
            return Err(format!(
                "kg_tier_single_chunk ({}) must be strictly less than kg_tier_three_chunks ({})",
                self.kg_tier_single_chunk, self.kg_tier_three_chunks
            ));
        }
        if !(self.kg_tier_three_chunks < self.kg_tier_full) {
            return Err(format!(
                "kg_tier_three_chunks ({}) must be strictly less than kg_tier_full ({})",
                self.kg_tier_three_chunks, self.kg_tier_full
            ));
        }
        Ok(())
    }
}

// ─── File I/O ────────────────────────────────────────────────────────────

/// Resolve `<vct_root_dir>/retrieval-tuning.toml`. Honours
/// `VCT_STATE_DIR` via `vct_launcher_core::paths`.
fn tuning_path() -> PathBuf {
    vct_root_dir().join(RETRIEVAL_TUNING_FILENAME)
}

/// Read tuning values from disk. Missing OR malformed file → defaults.
///
/// We deliberately do NOT delete a malformed file (the user may want
/// to recover it manually). The eprintln! goes to the launcher's
/// stderr — captured by the install log, not surfaced as a toast.
fn read_tuning_from(path: &Path) -> RetrievalTuning {
    match std::fs::read_to_string(path) {
        Ok(raw) => match toml::from_str::<RetrievalTuning>(&raw) {
            Ok(parsed) => {
                // Validate the file's contents — a malformed-but-
                // parseable file (e.g. someone hand-edited and broke
                // the ordering) falls back to defaults rather than
                // serving inconsistent values to the resolver.
                if parsed.validate().is_ok() {
                    parsed
                } else {
                    tracing::warn!(
                        "[retrieval_tuning] {} parsed but failed validation; using defaults",
                        path.display()
                    );
                    RetrievalTuning::default()
                }
            }
            Err(e) => {
                tracing::warn!(
                    "[retrieval_tuning] could not parse {} as TOML ({}); using defaults",
                    path.display(),
                    e
                );
                RetrievalTuning::default()
            }
        },
        Err(_) => RetrievalTuning::default(),
    }
}

/// Atomic write: serialize to TOML, write to `<path>.tmp`, rename
/// over the target. Same posture as `storage_ux::write_storage_config_to`.
fn write_tuning_to(path: &Path, tuning: &RetrievalTuning) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let body = toml::to_string_pretty(tuning)
        .map_err(|e| format!("serialize retrieval-tuning.toml: {}", e))?;
    let mut tmp = path.to_path_buf();
    tmp.set_extension("toml.tmp");
    std::fs::write(&tmp, &body)
        .map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, path).map_err(|e| {
        format!("rename {} -> {}: {}", tmp.display(), path.display(), e)
    })?;
    Ok(())
}

// ─── Public read/write helpers (used by hub + Tauri commands) ────────────

/// Read tuning from the canonical `<vct_root_dir>/retrieval-tuning.toml`.
/// Public so the hub's config_api crate can call the same code path.
///
/// In practice the vct-hub crate doesn't depend on the launcher's
/// Tauri-side crate — see `vct-hub/src/config_api.rs` for the parallel
/// reader. Both reads point at the SAME file via the SAME
/// `vct_root_dir()` resolution; the duplication is shape-only, not
/// behaviour. A future refactor that lifts a `retrieval_tuning_io`
/// module into `vct-launcher-core` could collapse them, but that's
/// out of scope for v0.2.22 Item #13.
pub fn read_tuning() -> RetrievalTuning {
    read_tuning_from(&tuning_path())
}

// ─── Tauri commands ──────────────────────────────────────────────────────

/// Read the current retrieval tuning values. Returns defaults on a
/// missing / malformed file.
#[command]
pub async fn retrieval_tuning_get() -> Result<RetrievalTuning, String> {
    Ok(read_tuning())
}

/// Persist retrieval tuning values. Validates first; rejects with
/// `Err(msg)` on any range / ordering violation (no auto-clamp).
///
/// v0.2.72 R4 (F5 residual) — NO MCP reload / env re-projection is
/// needed here, verified against every consumer of
/// `retrieval-tuning.toml`:
///   * the hub re-reads the file FRESH on every `/projects/{id}/config`
///     request (`vct-hub/src/config_api.rs` calls
///     `retrieval_tuning_io::read_tuning()` inside the request handler —
///     no startup cache), so resolver clients (`vco_lib/project_config.py`,
///     `templates/scripts/vct_retrieval_tuning_get.{sh,ps1}`) see the new
///     values on their next round-trip;
///   * the weaviate MCP does NOT consume these values at all — its KG
///     tier thresholds come from the separate, user-managed `KG_TIER_*`
///     env-var channel (`claude_mcp_servers/weaviate_mcp/server.py`,
///     `_TIER_THRESHOLDS`), which this command never writes.
/// i.e. every live consumer resolves per-query; nothing caches this file
/// at startup, so there is no staleness for a reload to fix.
#[command]
pub async fn retrieval_tuning_set(
    tuning: RetrievalTuning,
) -> Result<(), String> {
    tuning.validate()?;
    write_tuning_to(&tuning_path(), &tuning)
}

/// Reset retrieval tuning to the calibrated defaults. Convenience
/// surface for the GUI's "Reset all to defaults" button so the FE
/// doesn't have to know the canonical default values.
#[command]
pub async fn retrieval_tuning_reset() -> Result<RetrievalTuning, String> {
    let defaults = RetrievalTuning::default();
    write_tuning_to(&tuning_path(), &defaults)?;
    Ok(defaults)
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use tempfile::TempDir;

    // VCT_STATE_DIR is process-wide; serialise tests that mutate the
    // global cwd/env so parallel runs don't observe each other.
    // Mirrors the pattern in vct-launcher-core/src/paths.rs::tests.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    /// Run `f` with VCT_STATE_DIR pointing at a fresh tempdir; restore
    /// the previous env var on exit.
    fn with_state_dir<F: FnOnce(&Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var_os("VCT_STATE_DIR");
        let tmp = TempDir::new().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            f(tmp.path());
        }));
        match prev {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
        if let Err(e) = result {
            std::panic::resume_unwind(e);
        }
    }

    #[test]
    fn defaults_match_calibrated_constants() {
        // The published defaults are calibrated values from
        // `knowledge/concepts/score-driven-retrieval-tiers.md`.
        // Pin them here so an accidental edit gets caught by CI.
        let d = RetrievalTuning::default();
        assert!((d.code_graph_score_floor - 0.35).abs() < 1e-9);
        assert!((d.kg_tier_min - 0.42).abs() < 1e-9);
        assert!((d.kg_tier_single_chunk - 0.55).abs() < 1e-9);
        assert!((d.kg_tier_three_chunks - 0.65).abs() < 1e-9);
        assert!((d.kg_tier_full - 0.75).abs() < 1e-9);
    }

    #[test]
    fn default_passes_validation() {
        let d = RetrievalTuning::default();
        assert!(d.validate().is_ok(), "defaults must validate");
    }

    #[test]
    fn validation_rejects_out_of_range() {
        let mut t = RetrievalTuning::default();
        t.kg_tier_min = -0.1;
        assert!(t.validate().is_err());

        let mut t = RetrievalTuning::default();
        t.kg_tier_full = 1.5;
        assert!(t.validate().is_err());

        let mut t = RetrievalTuning::default();
        t.code_graph_score_floor = f64::NAN;
        assert!(t.validate().is_err());

        let mut t = RetrievalTuning::default();
        t.kg_tier_min = f64::INFINITY;
        assert!(t.validate().is_err());
    }

    #[test]
    fn validation_rejects_disordered_kg_tiers() {
        // Swap min and single_chunk → min > single_chunk → error.
        let mut t = RetrievalTuning::default();
        t.kg_tier_min = 0.60;
        t.kg_tier_single_chunk = 0.55;
        let err = t.validate().unwrap_err();
        assert!(err.contains("kg_tier_min"), "err was: {}", err);
        assert!(err.contains("kg_tier_single_chunk"), "err was: {}", err);

        // Equal thresholds (zero-width band) also fail.
        let mut t = RetrievalTuning::default();
        t.kg_tier_three_chunks = 0.65;
        t.kg_tier_full = 0.65;
        assert!(t.validate().is_err());
    }

    #[test]
    fn validation_rejects_disordered_middle_tiers() {
        // single_chunk > three_chunks
        let mut t = RetrievalTuning::default();
        t.kg_tier_single_chunk = 0.70;
        t.kg_tier_three_chunks = 0.65;
        assert!(t.validate().is_err());
    }

    #[test]
    fn read_missing_file_returns_defaults() {
        with_state_dir(|_dir| {
            let t = read_tuning();
            assert_eq!(t, RetrievalTuning::default());
        });
    }

    #[test]
    fn read_malformed_file_returns_defaults() {
        with_state_dir(|dir| {
            let path = dir.join(RETRIEVAL_TUNING_FILENAME);
            std::fs::write(&path, "not valid toml [[[").unwrap();
            let t = read_tuning();
            assert_eq!(t, RetrievalTuning::default());
        });
    }

    #[test]
    fn read_valid_but_invariant_violating_file_returns_defaults() {
        // A user hand-edit that breaks the ordering should fall back
        // to defaults rather than poison the resolver.
        with_state_dir(|dir| {
            let path = dir.join(RETRIEVAL_TUNING_FILENAME);
            let body = "\
code_graph_score_floor = 0.35
kg_tier_min = 0.80
kg_tier_single_chunk = 0.55
kg_tier_three_chunks = 0.65
kg_tier_full = 0.75
";
            std::fs::write(&path, body).unwrap();
            let t = read_tuning();
            assert_eq!(t, RetrievalTuning::default());
        });
    }

    #[test]
    fn write_then_read_round_trip() {
        with_state_dir(|dir| {
            let mut t = RetrievalTuning::default();
            t.code_graph_score_floor = 0.40;
            t.kg_tier_min = 0.45;
            t.kg_tier_full = 0.85;
            write_tuning_to(&dir.join(RETRIEVAL_TUNING_FILENAME), &t).unwrap();

            let read_back = read_tuning();
            assert_eq!(read_back, t);
        });
    }

    #[test]
    fn write_creates_parent_dirs() {
        with_state_dir(|dir| {
            let nested = dir.join("nested").join("under").join("here");
            let path = nested.join(RETRIEVAL_TUNING_FILENAME);
            let t = RetrievalTuning::default();
            write_tuning_to(&path, &t).unwrap();
            assert!(path.exists(), "expected file at {}", path.display());
        });
    }

    #[tokio::test]
    async fn cmd_get_returns_defaults_on_empty() {
        // Each test gets its own tempdir via VCT_STATE_DIR so concurrent
        // tests don't observe each other's file.
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var_os("VCT_STATE_DIR");
        let tmp = TempDir::new().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let got = retrieval_tuning_get().await.unwrap();
        assert_eq!(got, RetrievalTuning::default());

        match prev {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn cmd_set_persists() {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var_os("VCT_STATE_DIR");
        let tmp = TempDir::new().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let mut t = RetrievalTuning::default();
        t.code_graph_score_floor = 0.42;
        retrieval_tuning_set(t.clone()).await.unwrap();

        let read_back = retrieval_tuning_get().await.unwrap();
        assert_eq!(read_back, t);

        match prev {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn cmd_set_rejects_invalid_ordering() {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var_os("VCT_STATE_DIR");
        let tmp = TempDir::new().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let mut t = RetrievalTuning::default();
        t.kg_tier_min = 0.90;  // > kg_tier_single_chunk (0.55) → invalid
        let err = retrieval_tuning_set(t).await.unwrap_err();
        assert!(
            err.contains("kg_tier_min"),
            "expected ordering error mentioning kg_tier_min, got: {}",
            err
        );
        // And the file should NOT exist — invalid set never writes.
        let path = tmp.path().join(RETRIEVAL_TUNING_FILENAME);
        assert!(
            !path.exists(),
            "rejected set must not write the tuning file"
        );

        match prev {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn cmd_reset_writes_defaults() {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var_os("VCT_STATE_DIR");
        let tmp = TempDir::new().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // First set a non-default value...
        let mut t = RetrievalTuning::default();
        t.code_graph_score_floor = 0.5;
        retrieval_tuning_set(t).await.unwrap();

        // ...then reset.
        let defaults = retrieval_tuning_reset().await.unwrap();
        assert_eq!(defaults, RetrievalTuning::default());

        let read_back = retrieval_tuning_get().await.unwrap();
        assert_eq!(read_back, RetrievalTuning::default());

        match prev {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }
}
