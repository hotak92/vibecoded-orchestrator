//! Hub-startup Weaviate class existence check (v0.2.21 Step 21).
//!
//! When the hub binds, we sweep `project_kg_bindings.collection_name`
//! and `project_codegraph_bindings.collection_prefix` from launcher.db
//! and probe Weaviate's `/v1/schema/{class}` endpoint to confirm each
//! collection actually exists. Missing classes are logged to stderr +
//! captured in `<vct_root_dir>/cache/weaviate_class_check.jsonl` so a
//! later `bootstrap-collections` invocation (Python side, owned by
//! vco_lib.weaviate_schema) can pick them up.
//!
//! We deliberately DO NOT auto-create classes from Rust:
//!   * The Python side already owns named-vector class creation +
//!     dim/embedding-model migration; reimplementing that in Rust
//!     would duplicate ~991 LOC of `vco_lib/weaviate_schema.py` and
//!     drift the two implementations.
//!   * Auto-create-on-startup hides the "no schema yet" state from
//!     the user. The launcher GUI's Update Bundle path is the right
//!     surface to drive class creation (with the deferral envelope
//!     for user consent on destructive migrations).
//!
//! What this module DOES do: detect + log + write a deferral-style
//! sidecar so the resolver endpoint can return `service_misconfigured`
//! (Step 14's existing 503 code) and the user/launcher can act.
//!
//! Soft-fail throughout. Weaviate unreachable → log + skip. DB
//! unreachable → log + skip. The hub still starts in either case.

use std::path::PathBuf;
use std::time::Duration;

use vct_launcher_core::config::LocalConfig;
use vct_launcher_core::db::Db;

/// Side-car file the probe writes after sweeping. Consumed (in v0.2.22+)
/// by an install.py / launcher GUI surface that asks the user to
/// run `bootstrap-collections` if any classes are missing.
const PROBE_LOG_FILE: &str = "cache/weaviate_class_check.jsonl";

/// Per-class probe outcome.
#[derive(Debug, serde::Serialize)]
pub struct ClassProbeResult {
    pub class: String,
    pub kind: &'static str, // "kg_primary" | "kg_shared" | "kg_archive" | "code_module" | "code_class" | "code_function" | "code_api" | "code_interaction"
    pub project_id: String,
    pub project_slug: String,
    pub exists: bool,
    pub http_status: Option<u16>,
    pub error: Option<String>,
}

/// Top-level sweep summary.
#[derive(Debug, serde::Serialize)]
pub struct ProbeSummary {
    pub ts_unix: i64,
    pub weaviate_url: String,
    pub probed_count: usize,
    pub missing_count: usize,
    pub probe_error_count: usize,
    pub results: Vec<ClassProbeResult>,
}

/// Run the sweep. Best-effort: errors at any layer (DB read, HTTP
/// connect, JSON parse) are captured into the result rather than
/// bubbled. Returns Some(summary) on success or None if we couldn't
/// even start (DB unreachable at module load).
pub async fn probe_class_existence(db: &Db, weaviate_url: &str) -> Option<ProbeSummary> {
    // 1. Enumerate every class the launcher.db references.
    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            tracing::error!(
                error = %e,
                "[vct-hub] weaviate_probe: list_projects failed; skipping class check"
            );
            return None;
        }
    };

    let mut targets: Vec<(String, &'static str, String, String)> = Vec::new();
    // (class_name, kind_tag, project_id, project_slug)
    for p in &projects {
        if let Ok(bindings) = db.list_project_kg_bindings(&p.id) {
            for b in bindings {
                let kind = match b.role.as_str() {
                    "primary" => "kg_primary",
                    "shared" => "kg_shared",
                    "archive" => "kg_archive",
                    _ => "kg_other",
                };
                targets.push((b.collection_name.clone(), kind, p.id.clone(), p.slug.clone()));
            }
        }
        if let Ok(Some(cg)) = db.get_project_codegraph_binding(&p.id) {
            // The codegraph collection NAMES are <prefix>_Code{Module,
            // Class,Function,API,Interaction}. We probe all five.
            for (suffix, kind) in [
                ("_CodeModule", "code_module"),
                ("_CodeClass", "code_class"),
                ("_CodeFunction", "code_function"),
                ("_CodeAPI", "code_api"),
                ("_CodeInteraction", "code_interaction"),
            ] {
                targets.push((
                    format!("{}{}", cg.collection_prefix, suffix),
                    kind,
                    p.id.clone(),
                    p.slug.clone(),
                ));
            }
        }
    }

    // 2. Dedup by class name (multiple projects may share a class —
    // e.g. SHARED_KG_COLLECTION refs from several projects).
    let mut seen = std::collections::HashSet::new();
    let unique_targets: Vec<_> = targets
        .into_iter()
        .filter(|(c, _, _, _)| seen.insert(c.clone()))
        .collect();

    if unique_targets.is_empty() {
        // No projects registered yet — fresh-install state. Nothing
        // to probe.
        return Some(ProbeSummary {
            ts_unix: chrono::Utc::now().timestamp(),
            weaviate_url: weaviate_url.to_string(),
            probed_count: 0,
            missing_count: 0,
            probe_error_count: 0,
            results: Vec::new(),
        });
    }

    // 3. Probe each class via HEAD /v1/schema/{class}. Weaviate
    // returns 200 if the class exists, 404 if not.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .ok();
    let Some(client) = client else {
        tracing::error!("[vct-hub] weaviate_probe: failed to build reqwest::Client; skipping");
        return None;
    };

    let mut results = Vec::with_capacity(unique_targets.len());
    let mut missing_count = 0;
    let mut probe_error_count = 0;
    for (class, kind, project_id, project_slug) in unique_targets {
        let url = format!("{}/v1/schema/{}", weaviate_url.trim_end_matches('/'), class);
        match client.get(&url).send().await {
            Ok(resp) => {
                let status = resp.status().as_u16();
                let exists = status == 200;
                if !exists {
                    missing_count += 1;
                }
                results.push(ClassProbeResult {
                    class,
                    kind,
                    project_id,
                    project_slug,
                    exists,
                    http_status: Some(status),
                    error: None,
                });
            }
            Err(e) => {
                probe_error_count += 1;
                results.push(ClassProbeResult {
                    class,
                    kind,
                    project_id,
                    project_slug,
                    exists: false,
                    http_status: None,
                    error: Some(format!("{}", e)),
                });
            }
        }
    }

    let summary = ProbeSummary {
        ts_unix: chrono::Utc::now().timestamp(),
        weaviate_url: weaviate_url.to_string(),
        probed_count: results.len(),
        missing_count,
        probe_error_count,
        results,
    };

    // 4. Write sidecar JSONL — one line per probe round so consumers
    // can read the LATEST line for fresh state.
    if let Err(e) = append_summary_sidecar(&summary) {
        tracing::warn!(
            error = %e,
            "[vct-hub] weaviate_probe: sidecar write failed (non-fatal)"
        );
    }

    // 5. Report the round. Missing classes are a WARN (the install is
    // usable but retrieval against those classes will not work); an
    // all-present round is routine INFO. The sidecar written above is the
    // durable record either way — it is not level-gated.
    if missing_count > 0 {
        tracing::warn!(
            missing = missing_count,
            probed = summary.probed_count,
            "[vct-hub] weaviate_probe: expected Weaviate classes are MISSING. \
             Run `python -m vco_lib.weaviate_schema bootstrap-collections` (or click \
             'Update Bundle' in the launcher GUI) to create them."
        );
    } else if summary.probed_count > 0 {
        tracing::info!(
            probed = summary.probed_count,
            "[vct-hub] weaviate_probe: all expected classes present"
        );
    }
    if probe_error_count > 0 {
        tracing::warn!(
            errored = probe_error_count,
            "[vct-hub] weaviate_probe: probe(s) errored (weaviate unreachable?); see sidecar"
        );
    }

    Some(summary)
}

/// Probe-log file path. Mirrors the resolver-warning sidecar shape
/// (jsonl, mode 0o600 on Unix, mkdir -p the parent dir).
fn sidecar_path() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join(PROBE_LOG_FILE)
}

fn append_summary_sidecar(summary: &ProbeSummary) -> Result<(), String> {
    let path = sidecar_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all {}: {}", parent.display(), e))?;
    }
    let line = serde_json::to_string(summary)
        .map_err(|e| format!("serialize probe summary: {}", e))?;

    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;
        writeln!(f, "{}", line).map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    #[cfg(not(unix))]
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;
        writeln!(f, "{}", line).map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    Ok(())
}

/// Convenience entry point used by `start_hub_server`. Builds the
/// weaviate_url from LocalConfig, kicks off the probe in a detached
/// task so server startup never blocks on Weaviate's response time.
pub fn spawn_startup_probe(db_handle: Db, local_config: &LocalConfig) {
    let weaviate_url = local_config.weaviate_url.clone();
    tokio::spawn(async move {
        let _ = probe_class_existence(&db_handle, &weaviate_url).await;
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidecar_path_resolves_under_vct_state_dir() {
        // Sanity: the sidecar lives under <vct_root_dir>/cache/.
        let p = sidecar_path();
        assert!(
            p.to_string_lossy().contains("cache"),
            "sidecar path should be under cache/, got: {}",
            p.display()
        );
        assert!(
            p.to_string_lossy().ends_with("weaviate_class_check.jsonl"),
            "sidecar filename mismatch: {}",
            p.display()
        );
    }

    #[tokio::test]
    async fn probe_with_empty_db_returns_zero_probed_count() {
        let db = Db::open_in_memory().expect("open in-memory db");
        // Use a definitely-unreachable URL so the network call (if it
        // happens) fails fast. With zero projects in the DB, there
        // should be zero targets and the probe short-circuits.
        let summary = probe_class_existence(&db, "http://127.0.0.1:1")
            .await
            .expect("probe returns Some on empty DB");
        assert_eq!(summary.probed_count, 0);
        assert_eq!(summary.missing_count, 0);
        assert_eq!(summary.probe_error_count, 0);
        assert!(summary.results.is_empty());
    }
}
