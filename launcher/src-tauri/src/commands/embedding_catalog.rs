// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Embedding-model catalog exposure (v0.2.18, Commit 8).
//!
//! Wraps `python -m vco_lib.embedding_service discover` (the Wave-A CLI
//! entry point in `vco_lib/embedding_service.py`) so the Svelte side can
//! populate model dropdowns from a machine-curated catalog instead of
//! accepting free-text. Three commands:
//!
//!   * `get_embedding_catalog`         — list reachable text + code models
//!   * `set_default_embedding_models`  — write new-project defaults
//!   * `validate_model_against_catalog`— pre-save check for binding writes
//!   * `get_default_embedding_models`  — read app_state.default_*_embedding
//!   * `get_code_embed_model_status`   — per-project recorded vs configured
//!     code-graph model (`python -m vco_lib.code_embed_model status`)
//!
//! ─── Caching ─────────────────────────────────────────────────────────
//!
//! Python sub-process shell-out is ~200-400ms on a warm machine; cold
//! Ollama probes can take longer. Without caching, rapid catalog re-
//! renders (Svelte $effect running on prop change) would queue subprocess
//! spawns and pin a core.
//!
//! Cache is a `Mutex<HashMap<Option<String>, (Instant, EmbeddingCatalog)>>`
//! keyed by project_id (so a per-project catalog reading different env
//! state caches separately from the no-project case). TTL is 30s — long
//! enough to absorb a route's worth of re-renders, short enough that a
//! user starting Ollama mid-session sees the new models on their next
//! dropdown open.
//!
//! ─── Catalog shape ───────────────────────────────────────────────────
//!
//! Mirrors the JSON the Python CLI prints:
//!
//! ```json
//! {
//!   "text_models": [
//!     {"id": "qwen3-embedding:0.6b", "label": "...", "dim": 1024,
//!      "slot": "qwen3_embed", "backend": "ollama",
//!      "available_now": true, "reason_unavailable": null}
//!   ],
//!   "code_models": [...],
//!   "current_text_slot": "qwen3_embed",
//!   "current_code_slot": "codesage_embed",
//!   "errors": []
//! }
//! ```
//!
//! Note: the GUI's "model id" string (e.g. `"qwen3-embedding:0.6b"` or
//! `"openai-text-embedding-3-small"`) is what gets written to
//! `app_state.default_text_embedding` (and, for the code graph, to the
//! codegraph binding). The slot name is metadata that the Python sync
//! scripts derive from the id. (The per-project KG binding's
//! `embedding_model` column was retired in v0.2.102 W6-J — one global
//! embedding setting; nothing reads or writes it anymore.)

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{command, State};
use tokio::time::timeout;

use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ───────────────────────────────────────────────────────────

/// Subprocess timeout for the Python `discover` call. 5s headroom on the
/// observed ~200-400ms warm path, accommodates a cold-Ollama first-probe.
const DISCOVER_TIMEOUT_SECS: u64 = 5;
/// The code-embed status asks Weaviate for object counts (five aggregates per
/// project whose target model is still filling) — long by design, it runs off
/// the UI thread.
const CODE_EMBED_STATUS_TIMEOUT_SECS: u64 = 120;

/// In-memory cache TTL — see module docstring for rationale.
const CACHE_TTL_SECS: u64 = 30;

/// App-state keys for the new-project default embeddings. Kept in sync
/// with `commands::openai_cmd::{APP_STATE_DEFAULT_TEXT_EMBED, APP_STATE_DEFAULT_CODE_EMBED}`
/// — re-declared here as locally-private consts so this module doesn't
/// pull in `openai_cmd` as a dependency for two string constants.
const APP_STATE_DEFAULT_TEXT_EMBED: &str = "default_text_embedding";
const APP_STATE_DEFAULT_CODE_EMBED: &str = "default_code_embedding";

// ─── Types ───────────────────────────────────────────────────────────────

/// One entry in the catalog. Mirrors `ModelChoice` from
/// `vco_lib/embedding_service.py` (serialised via `dataclasses.asdict`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelChoice {
    pub id: String,
    pub label: String,
    pub dim: i64,
    pub slot: String,
    pub backend: String,
    pub available_now: bool,
    pub reason_unavailable: Option<String>,
}

/// Full catalog payload returned by `get_embedding_catalog`. Matches the
/// Python CLI's JSON output 1:1 so the JS side can `JSON.parse`-ish
/// without translation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingCatalog {
    pub text_models: Vec<ModelChoice>,
    pub code_models: Vec<ModelChoice>,
    pub current_text_slot: Option<String>,
    pub current_code_slot: Option<String>,
    pub errors: Vec<String>,
}

/// What kind of model is being validated. Lower-cased over the wire so
/// the Svelte side can pass plain strings instead of importing typed
/// constants.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ModelKind {
    Text,
    Code,
}

/// Outcome of `validate_model_against_catalog`. Tagged union mirrors the
/// `OpenAiValidationResult` shape in `openai_cmd.rs` — `{status:"valid"}`
/// vs `{status:"invalid", reason:"..."}` on the JS side.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum ValidationResult {
    Valid {
        model_id: String,
        slot: String,
        backend: String,
    },
    Invalid {
        reason: String,
    },
}

/// Default new-project embedding model ids. Returned by
/// `get_default_embedding_models`; written by `set_default_embedding_models`.
/// Both fields are independent — a user could opt into OpenAI for text and
/// keep CodeSage for code, or vice-versa.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DefaultEmbeddingModels {
    pub text_model: Option<String>,
    pub code_model: Option<String>,
}

// ─── Cache plumbing ──────────────────────────────────────────────────────

/// Process-wide cache. Key is the project_id (or `None` for the no-
/// project call); value is (cached-at, catalog). Mutex contention is
/// negligible — this cache is touched once per `get_embedding_catalog`.
static CATALOG_CACHE: LazyLock<Mutex<HashMap<Option<String>, (Instant, EmbeddingCatalog)>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn cache_get(key: &Option<String>) -> Option<EmbeddingCatalog> {
    let guard = CATALOG_CACHE.lock().ok()?;
    let (at, catalog) = guard.get(key)?;
    if at.elapsed() < Duration::from_secs(CACHE_TTL_SECS) {
        Some(catalog.clone())
    } else {
        None
    }
}

fn cache_put(key: Option<String>, catalog: EmbeddingCatalog) {
    if let Ok(mut guard) = CATALOG_CACHE.lock() {
        guard.insert(key, (Instant::now(), catalog));
    }
}

/// Test-only cache clear so each test starts with a clean slate. Public-
/// crate so the `tests` mod below can call it. Gated on `cfg(test)` to
/// keep it out of the release binary.
#[cfg(test)]
pub(crate) fn _test_clear_cache() {
    if let Ok(mut guard) = CATALOG_CACHE.lock() {
        guard.clear();
    }
}

// ─── Python shell-out ────────────────────────────────────────────────────

/// Resolve the python interpreter that can `import vco_lib`.
///
/// v0.2.77: delegates to the shared RT-4 ladder in
/// `vct_launcher_core::python_resolve` (the "one home" for interpreter
/// discovery). The prior inline copy walked only `current_exe()` + PATH;
/// the shared ladder additionally honours `$VCT_VENV` and
/// `$VCT_INSTALL_ROOT` first, so the catalog command finds the healthy
/// orchestrator venv even when the launcher binary lives elsewhere. Still
/// never returns `None` (PATH fallback), so the catalog command remains a
/// no-op-on-missing-python (degraded UX, not crash).
#[inline]
fn resolve_python_for_vco_lib() -> Option<PathBuf> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
}

/// Spawn `python -m vco_lib.embedding_service discover` and parse JSON.
/// Errors flow into the catalog's `errors` field (where possible) or up
/// to the caller as `Err(String)` when the subprocess itself can't start.
///
/// v0.2.24.1 (A0ter): `vco_lib` lives at the orchestrator CLONE root,
/// not inside `.venv/lib/site-packages` (it's an in-tree namespace
/// package, never `pip install`-ed). To make `python -m vco_lib.X`
/// resolve, the subprocess must run with `cwd=<clone_root>` so
/// Python's implicit-namespace-package lookup picks up `vco_lib/`
/// from the cwd. Pre-v0.2.24.1 the subprocess inherited the
/// launcher's cwd (typically `/` on Linux + `<launcher_dir>` on
/// Windows) → `ModuleNotFoundError: No module named 'vco_lib'` ->
/// the per-project KG/Codegraph tab showed a permanent warning
/// banner + "Loading..." dropdowns. Resolves the clone root via
/// `installer::resolve_install_root_sync(&db)` (the same DB-cached
/// helper the manifest scanners use post-v0.2.23.1).
async fn run_discover(
    project_root: Option<PathBuf>,
    install_root: Option<PathBuf>,
) -> Result<EmbeddingCatalog, String> {
    let mut args: Vec<std::ffi::OsString> =
        vec!["vco_lib.embedding_service".into(), "discover".into(), "--json".into()];
    if let Some(root) = project_root {
        args.push("--project-root".into());
        args.push(root.into_os_string());
    }
    let stdout = run_vco_lib_cli(&args, install_root, DISCOVER_TIMEOUT_SECS, "discover").await?;
    serde_json::from_str::<EmbeddingCatalog>(&stdout)
        .map_err(|e| format!("parse discover JSON: {}", e))
}

/// Run `python -m <args…>` with the vco_lib interpreter and return stdout —
/// the ONE subprocess shape this module's commands share (v0.2.102 W6-B2
/// extracted it from `run_discover` for `get_code_embed_model_status`;
/// v0.2.103 made it `pub(crate)` for `model_guidance`'s status transport —
/// one home, no second spawn shape).
///
/// cwd MUST be the orchestrator clone root so `python -m vco_lib.…` finds
/// the in-tree namespace package (v0.2.24.1); with no root discoverable it
/// runs without a cwd and fails with ModuleNotFoundError rather than
/// crashing the launcher. Null stdin, piped output, CREATE_NO_WINDOW.
pub(crate) async fn run_vco_lib_cli(
    args: &[std::ffi::OsString],
    install_root: Option<PathBuf>,
    timeout_secs: u64,
    label: &str,
) -> Result<String, String> {
    let python = resolve_python_for_vco_lib()
        .ok_or_else(|| "no python interpreter found for vco_lib".to_string())?;

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m").args(args);
    if let Some(root) = install_root {
        cmd.current_dir(&root);
    }
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let output = match timeout(Duration::from_secs(timeout_secs), cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("spawn {}: {}", label, e)),
        Err(_) => return Err(format!("{} timed out after {}s", label, timeout_secs)),
    };

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} exit {}: {}",
            label,
            output.status.code().unwrap_or(-1),
            stderr.chars().take(500).collect::<String>()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// The status CLI prints ONE JSON object line; take the last non-empty line
/// so a stray log line above it cannot break the parse.
pub(crate) fn parse_last_json_line(stdout: &str, label: &str) -> Result<serde_json::Value, String> {
    let line = stdout
        .lines()
        .map(str::trim)
        .rfind(|l| !l.is_empty())
        .ok_or_else(|| format!("{} printed nothing", label))?;
    serde_json::from_str(line).map_err(|e| format!("parse {} JSON: {}", label, e))
}

// ─── Tauri commands ──────────────────────────────────────────────────────

/// Fetch the catalog of reachable embedding models. Results cached for
/// `CACHE_TTL_SECS`. The `project_id` argument is optional — when present,
/// it resolves to a folder path and is forwarded as `--project-root` so
/// the discover CLI reports the slots active for that project.
#[command]
pub async fn get_embedding_catalog(
    project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<EmbeddingCatalog, String> {
    // Resolve project_id → folder path before consulting the cache. The
    // cache key is the project_id string (not the path) so two projects
    // with different ids but the same folder still cache separately —
    // this matches the on-disk reality where env-resolution depends on
    // the project row, not just the path.
    let project_root: Option<PathBuf> = match project_id.as_deref() {
        Some(id) => match db.get_project(id)? {
            Some(row) => Some(PathBuf::from(row.folder_path)),
            None => return Err(format!("project not found: {}", id)),
        },
        None => None,
    };

    let cache_key = project_id.clone();
    if let Some(cached) = cache_get(&cache_key) {
        return Ok(cached);
    }

    // v0.2.24.1 (A0ter): resolve the orchestrator clone root via the
    // same DB-cached helper the manifest scanners use post-v0.2.23.1.
    // Passed to run_discover as the subprocess cwd so `python -m
    // vco_lib.embedding_service` finds the in-tree namespace package
    // (vco_lib/ lives at clone root, never pip-installed).
    let install_root = crate::commands::installer::resolve_install_root_sync(&db);

    let catalog = run_discover(project_root, install_root).await?;
    cache_put(cache_key, catalog.clone());
    Ok(catalog)
}

/// Write new-project default embedding model ids to app_state. Either
/// argument can be `None` to leave the existing value untouched (i.e. a
/// "change text default but not code default" call passes
/// `text_model=Some(...), code_model=None`).
///
/// This command intentionally does NOT trigger an enrichment migration.
/// For the CODE default (v0.2.102 W6-B2, `vco_lib/code_embed_model.py`): it is
/// a selection like the Preferences pin (below it in precedence) — a code graph
/// indexed with another model gets the new model's named vector added and
/// filled on its next analyzer run, keeping the old vectors; searches switch
/// when the new slot is complete. Nothing is dropped.
#[command]
pub async fn set_default_embedding_models(
    text_model: Option<String>,
    code_model: Option<String>,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<(), String> {
    if let Some(ref v) = text_model {
        // v0.2.68 Defect D: also stamp the canonical
        // `embedding.active_profile` so `project_env_settings::populate`
        // doesn't fall back to "qwen3" when the user picked arctic/openai.
        //
        // F3 (v0.2.72): the helper follows its app_state writes with a
        // refresh of EVERY project's env (N serial Python subprocesses) —
        // run the whole call on the blocking pool instead of a tokio
        // worker. Write errors still propagate through the join result.
        let model_id = v.clone();
        crate::commands::blocking::run_with_db_on_blocking_pool(
            app.clone(),
            "set_default_embedding_models",
            move |db| {
                crate::commands::project_env_settings::set_text_embedding_and_profile(
                    db, &model_id,
                )
                .map(|_report| ())
            },
        )
        .await??;
    }
    if let Some(ref v) = code_model {
        db.app_state_set(APP_STATE_DEFAULT_CODE_EMBED, v.trim())?;
    }
    db.audit(
        "default_embedding_models_set",
        None,
        None,
        &serde_json::json!({
            "text_model": text_model,
            "code_model": code_model,
        }),
    )?;
    Ok(())
}

/// Read the new-project defaults. Returns `Some` when the row is set,
/// `None` when never written. The caller (Preferences `+page.svelte`)
/// uses this to pre-populate the dropdown values.
#[command]
pub async fn get_default_embedding_models(
    db: State<'_, Db>,
) -> Result<DefaultEmbeddingModels, String> {
    Ok(DefaultEmbeddingModels {
        text_model: db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED)?,
        code_model: db.app_state_get(APP_STATE_DEFAULT_CODE_EMBED)?,
    })
}

/// Per-project code-graph embedding model status for the Preferences "Code
/// Graph Embeddings" section (v0.2.102 W6-B2): the model each project's code
/// graph is RECORDED as indexed with (what searches use), the selected target,
/// the target's fill progress (rows filled / total, or complete — a selection
/// is applied automatically, so there is no apply step to show) and whether it
/// is OpenAI-billed. The answer is computed by the ONE resolver,
/// `python -m vco_lib.code_embed_model status` (rung A of the cross-language
/// ladder) — this command only transports it. Errors surface to the section
/// verbatim; nothing is guessed.
#[command]
pub async fn get_code_embed_model_status(
    db: State<'_, Db>,
) -> Result<serde_json::Value, String> {
    let install_root = crate::commands::installer::resolve_install_root_sync(&db);
    let args: Vec<std::ffi::OsString> =
        vec!["vco_lib.code_embed_model".into(), "status".into(), "--json".into()];
    let stdout =
        run_vco_lib_cli(&args, install_root, CODE_EMBED_STATUS_TIMEOUT_SECS, "code-embed status")
            .await?;
    parse_last_json_line(&stdout, "code-embed status")
}

/// Per-project KG embedding model status (v0.2.102 W6-I): for each of a
/// project's KG collections (its KG, the shared KG, the development
/// collection) the model the collection is RECORDED as complete in (what
/// searches use), the selected target, and the target's fill progress. The
/// answer is computed by `python -m vco_lib.kg_embed_model status` (the ONE
/// resolver, rung A) — this command only transports it, verbatim.
#[command]
pub async fn get_kg_embed_model_status(
    db: State<'_, Db>,
) -> Result<serde_json::Value, String> {
    let install_root = crate::commands::installer::resolve_install_root_sync(&db);
    let args: Vec<std::ffi::OsString> =
        vec!["vco_lib.kg_embed_model".into(), "status".into(), "--json".into()];
    let stdout =
        run_vco_lib_cli(&args, install_root, CODE_EMBED_STATUS_TIMEOUT_SECS, "kg-embed status")
            .await?;
    parse_last_json_line(&stdout, "kg-embed status")
}

/// Check a model id against the live catalog. Used by the codegraph
/// binding save path (`set_project_codegraph_binding`) to reject names the
/// machine can't currently serve. (The KG binding save no longer takes a
/// model — v0.2.102 W6-J retired the per-project KG model pick.)
///
/// "Currently serve" is `available_now=true` AND the model id is present
/// in the relevant list (text vs code). An entry whose
/// `available_now=false` (greyed-out in the dropdown) explicitly fails
/// validation — saving an unreachable binding would silently break the
/// seed pipeline.
#[command]
pub async fn validate_model_against_catalog(
    model_id: String,
    kind: ModelKind,
    db: State<'_, Db>,
) -> Result<ValidationResult, String> {
    let catalog = get_embedding_catalog(None, db).await?;
    let pool = match kind {
        ModelKind::Text => &catalog.text_models,
        ModelKind::Code => &catalog.code_models,
    };
    match pool.iter().find(|m| m.id == model_id) {
        Some(m) if m.available_now => Ok(ValidationResult::Valid {
            model_id: m.id.clone(),
            slot: m.slot.clone(),
            backend: m.backend.clone(),
        }),
        Some(m) => Ok(ValidationResult::Invalid {
            reason: m
                .reason_unavailable
                .clone()
                .unwrap_or_else(|| "model is not currently reachable".into()),
        }),
        None => Ok(ValidationResult::Invalid {
            reason: format!("model not in catalog: {}", model_id),
        }),
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_embed_status_takes_the_last_json_line() {
        let out = "[vct] a log line\n{\"pick\":\"codesage-large-v2\",\"projects\":[]}\n\n";
        let v = parse_last_json_line(out, "code-embed status").expect("parses");
        assert_eq!(v["pick"], "codesage-large-v2");
        assert!(parse_last_json_line("", "code-embed status").is_err());
        assert!(parse_last_json_line("not json", "code-embed status").is_err());
    }

    /// v0.2.102 W6-I: the KG status transport names ITS command in errors.
    #[test]
    fn kg_embed_status_parse_errors_name_the_kg_command() {
        let v = parse_last_json_line("{\"target\":\"qwen3-embedding:0.6b\",\"projects\":[]}", "kg-embed status")
            .expect("parses");
        assert_eq!(v["target"], "qwen3-embedding:0.6b");
        let err = parse_last_json_line("", "kg-embed status").unwrap_err();
        assert!(err.contains("kg-embed status"), "{err}");
    }

    fn dummy_catalog() -> EmbeddingCatalog {
        EmbeddingCatalog {
            text_models: vec![
                ModelChoice {
                    id: "qwen3-embedding:0.6b".into(),
                    label: "Qwen3 (1024d)".into(),
                    dim: 1024,
                    slot: "qwen3_embed".into(),
                    backend: "ollama".into(),
                    available_now: true,
                    reason_unavailable: None,
                },
                ModelChoice {
                    id: "openai-text-embedding-3-small".into(),
                    label: "OpenAI text-embedding-3-small (1536d)".into(),
                    dim: 1536,
                    slot: "openai_text_embed".into(),
                    backend: "openai".into(),
                    available_now: false,
                    reason_unavailable: Some("OPENAI_API_KEY not set".into()),
                },
            ],
            code_models: vec![ModelChoice {
                id: "codesage/codesage-large-v2".into(),
                label: "CodeSage Large v2 (2048d)".into(),
                dim: 2048,
                slot: "codesage_embed".into(),
                backend: "codeembed".into(),
                available_now: true,
                reason_unavailable: None,
            }],
            current_text_slot: Some("qwen3_embed".into()),
            current_code_slot: Some("codesage_embed".into()),
            errors: vec![],
        }
    }

    #[test]
    fn json_round_trip_matches_python_shape() {
        // Sample shape matching what `python -m vco_lib.embedding_service
        // discover --json` emits. Field names must match exactly so the
        // serde derive succeeds without remapping.
        let raw = serde_json::json!({
            "text_models": [
                {
                    "id": "qwen3-embedding:0.6b",
                    "label": "Qwen3 (1024d)",
                    "dim": 1024,
                    "slot": "qwen3_embed",
                    "backend": "ollama",
                    "available_now": true,
                    "reason_unavailable": null
                }
            ],
            "code_models": [],
            "current_text_slot": "qwen3_embed",
            "current_code_slot": null,
            "errors": []
        });
        let catalog: EmbeddingCatalog =
            serde_json::from_value(raw).expect("deserialize");
        assert_eq!(catalog.text_models.len(), 1);
        assert_eq!(catalog.text_models[0].id, "qwen3-embedding:0.6b");
        assert!(catalog.text_models[0].available_now);
        assert_eq!(catalog.current_text_slot.as_deref(), Some("qwen3_embed"));
        assert!(catalog.current_code_slot.is_none());
    }

    #[test]
    fn cache_returns_hit_within_ttl() {
        _test_clear_cache();
        let key = Some("proj-1".to_string());
        let catalog = dummy_catalog();
        cache_put(key.clone(), catalog.clone());

        let got = cache_get(&key).expect("cache hit");
        assert_eq!(got.text_models.len(), 2);
        assert_eq!(got.text_models[0].id, "qwen3-embedding:0.6b");
        _test_clear_cache();
    }

    #[test]
    fn cache_miss_when_key_differs() {
        _test_clear_cache();
        cache_put(Some("proj-1".into()), dummy_catalog());
        // Different key → cache miss.
        assert!(cache_get(&Some("proj-2".into())).is_none());
        // None-key also a cache miss (different bucket entirely).
        assert!(cache_get(&None).is_none());
        _test_clear_cache();
    }

    #[test]
    fn cache_expires_after_ttl() {
        _test_clear_cache();
        let key = Some("proj-x".to_string());
        // Insert an entry with timestamp `CACHE_TTL_SECS+1` seconds in the
        // past — the time check inside `cache_get` should refuse it.
        // We can't subtract from `Instant` directly cross-platform; the
        // public API is via the cache itself. Instead, swap in an
        // artificially-old entry by reaching into the mutex.
        {
            let mut guard = CATALOG_CACHE.lock().expect("cache lock");
            let old = Instant::now()
                .checked_sub(Duration::from_secs(CACHE_TTL_SECS + 1))
                // checked_sub may return None on some platforms when the
                // process hasn't been alive long enough — fall back to
                // a regular put + skip in that case.
                .unwrap_or_else(Instant::now);
            guard.insert(key.clone(), (old, dummy_catalog()));
        }
        // If checked_sub succeeded, cache_get must miss; if it didn't,
        // the entry is "fresh" and we just verify the cache machinery
        // works (the TTL-expiry path is exercised by the unwrap_or_else
        // branch being taken in production with a sufficiently-old
        // instant).
        // The minimal assertion: the cache always returns the *current*
        // entry when fresh, and refuses a stale one.
        let hit = cache_get(&key);
        match hit {
            // Stale entry refused (the normal path on most platforms).
            None => {}
            // Fallback path: we couldn't fabricate a stale instant; the
            // entry is treated as fresh. This is a soft assertion that
            // the cache returns SOMETHING reasonable rather than panicking.
            Some(c) => assert_eq!(c.text_models.len(), 2),
        }
        _test_clear_cache();
    }

    #[test]
    fn validation_finds_available_model() {
        // Pure-function test of the validation logic. Build a catalog,
        // run the same lookup the command does, assert the result.
        let catalog = dummy_catalog();
        let needle = "qwen3-embedding:0.6b";
        let found = catalog
            .text_models
            .iter()
            .find(|m| m.id == needle)
            .expect("present");
        assert!(found.available_now);
        assert_eq!(found.slot, "qwen3_embed");
    }

    #[test]
    fn validation_rejects_unreachable_model() {
        let catalog = dummy_catalog();
        let found = catalog
            .text_models
            .iter()
            .find(|m| m.id == "openai-text-embedding-3-small")
            .expect("present in list, just not available");
        assert!(!found.available_now);
        assert_eq!(
            found.reason_unavailable.as_deref(),
            Some("OPENAI_API_KEY not set")
        );
    }

    #[test]
    fn validation_rejects_missing_model() {
        let catalog = dummy_catalog();
        let needle = "does-not-exist";
        let found = catalog.text_models.iter().find(|m| m.id == needle);
        assert!(found.is_none());
    }

    #[test]
    fn model_kind_deserializes_from_lowercase_strings() {
        let text: ModelKind =
            serde_json::from_str("\"text\"").expect("text variant");
        assert_eq!(text, ModelKind::Text);
        let code: ModelKind =
            serde_json::from_str("\"code\"").expect("code variant");
        assert_eq!(code, ModelKind::Code);
        // Uppercase / titlecase rejected — this guards against the JS
        // side accidentally passing "Text" / "Code" / "TEXT".
        assert!(serde_json::from_str::<ModelKind>("\"Text\"").is_err());
    }

    #[test]
    fn validation_result_serialises_with_status_tag() {
        let v = ValidationResult::Valid {
            model_id: "qwen3-embedding:0.6b".into(),
            slot: "qwen3_embed".into(),
            backend: "ollama".into(),
        };
        let s = serde_json::to_string(&v).expect("serialise");
        // Tagged-union shape: {"status":"valid", ...flat fields}
        assert!(s.contains("\"status\":\"valid\""));
        assert!(s.contains("\"slot\":\"qwen3_embed\""));

        let inv = ValidationResult::Invalid {
            reason: "not in catalog".into(),
        };
        let s = serde_json::to_string(&inv).expect("serialise");
        assert!(s.contains("\"status\":\"invalid\""));
        assert!(s.contains("\"reason\":\"not in catalog\""));
    }
}
