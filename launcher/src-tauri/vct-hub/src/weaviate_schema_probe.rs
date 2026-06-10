//! Case-insensitive Weaviate class name resolver.
//!
//! NEW-2 (v0.2.53) — port of install.py's `_resolve_existing_casing`
//! (install.py:11848) into the hub. The bug being fixed: when the hub
//! derives `development_collection` from the primary KG via suffix-swap
//! (`_KnowledgeGraph` → `_Development`), it produced a name whose casing
//! follows the launcher.db binding row (e.g. `VibeCodedOrchestrator_Development`,
//! capital-C). On installs where Weaviate's on-disk class is a different
//! casing (e.g. `Vibecodedorchestrator_Development`, lowercase-c — a legacy
//! artefact from pre-canonical installs), the hub's reply caused
//! `sync_knowledge_graph.py` to call `.exists()` (case-sensitive) → False
//! → `.create()` → Weaviate refused with "found similar class".
//!
//! See: `.claude/context/audits/fabio-v0252-rootcause-2026-06-10.md`
//! Symptom B for the full root-cause walk.
//!
//! Contract: given a candidate class name and a Weaviate URL, return:
//!   - the actual on-disk casing when a case-different sibling exists,
//!   - the input unchanged when no case-different sibling exists,
//!   - the input unchanged when Weaviate is unreachable / unparseable
//!     (fail-open: do not block resolves on transient network errors).
//!
//! The probe is cached for ~5 seconds per `(weaviate_url)` to keep the
//! cost of every `/api/v1/projects/{id}/config` resolve flat. The cache
//! TTL matches the `HUB_DISCOVERY_TTL_SECONDS`-style pattern used
//! elsewhere; short enough that a freshly-created class is picked up on
//! the next resolve, long enough that bursts (e.g. install.py spawning
//! sync_knowledge_graph.py spawning kg-sync sub-checks) don't fan out
//! into N HTTP calls.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Cache TTL: hold the schema snapshot for 5 seconds. Short enough that a
/// fresh class created via `bootstrap-collections` is visible on the next
/// resolve; long enough to coalesce bursts.
const CACHE_TTL: Duration = Duration::from_secs(5);

/// Per-Weaviate-URL cached snapshot of the lowercased→actual class map.
struct CachedSchema {
    /// Lowercased class name → actual on-disk casing.
    lower_to_actual: HashMap<String, String>,
    /// When the snapshot was captured.
    captured_at: Instant,
}

/// Static cache. We don't expect more than a couple of distinct Weaviate URLs
/// per hub instance (typically just one), so a plain `Mutex<HashMap<...>>`
/// is plenty.
static SCHEMA_CACHE: Mutex<Option<HashMap<String, CachedSchema>>> = Mutex::new(None);

/// Return the actual on-disk casing of `candidate` if a case-different
/// sibling exists in Weaviate's schema at `weaviate_url`. Otherwise return
/// `candidate` unchanged.
///
/// Fail-open: any error (network, parse, timeout) yields `candidate` so
/// the resolver never 500s on a transient Weaviate hiccup.
pub async fn resolve_existing_casing_for_class(weaviate_url: &str, candidate: &str) -> String {
    // Cheap return for the empty / trivial case.
    if candidate.is_empty() {
        return candidate.to_string();
    }

    // Check the cache first.
    if let Some(actual) = lookup_cached(weaviate_url, candidate) {
        return actual;
    }

    // Cache miss — fetch + populate.
    match fetch_schema_map(weaviate_url).await {
        Ok(map) => {
            let actual = map.get(&candidate.to_lowercase()).cloned();
            store_cached(weaviate_url, map);
            actual.unwrap_or_else(|| candidate.to_string())
        }
        Err(_) => {
            // Soft-fail: keep the candidate. The downstream caller will
            // surface a Weaviate-side error if the name is genuinely wrong;
            // we don't want every transient probe failure to break resolves.
            candidate.to_string()
        }
    }
}

/// Look up a candidate in the cache without making any network calls.
/// Returns `Some(actual_casing)` when the cache holds a fresh snapshot AND
/// the candidate has a case-different sibling there. Returns `None` to
/// signal "need to fetch fresh".
///
/// **Important**: a cache HIT with no case-different sibling returns
/// `Some(candidate)` — we must distinguish "no sibling exists in fresh
/// snapshot" from "no snapshot at all". This is why the result is wrapped
/// in `Option`; `None` means "cache miss, fetch", `Some(s)` means "cache
/// authoritative, use s".
fn lookup_cached(weaviate_url: &str, candidate: &str) -> Option<String> {
    let guard = SCHEMA_CACHE.lock().ok()?;
    let cache = guard.as_ref()?;
    let entry = cache.get(weaviate_url)?;
    if entry.captured_at.elapsed() > CACHE_TTL {
        return None;
    }
    let actual = entry
        .lower_to_actual
        .get(&candidate.to_lowercase())
        .cloned()
        .unwrap_or_else(|| candidate.to_string());
    Some(actual)
}

/// Populate / overwrite the cache entry for `weaviate_url` with `map`.
fn store_cached(weaviate_url: &str, map: HashMap<String, String>) {
    let Ok(mut guard) = SCHEMA_CACHE.lock() else {
        return;
    };
    let cache = guard.get_or_insert_with(HashMap::new);
    cache.insert(
        weaviate_url.to_string(),
        CachedSchema {
            lower_to_actual: map,
            captured_at: Instant::now(),
        },
    );
}

/// Fetch `<weaviate_url>/v1/schema` and build the lowercased→actual map.
async fn fetch_schema_map(weaviate_url: &str) -> Result<HashMap<String, String>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("reqwest::Client build failed: {}", e))?;

    let url = format!("{}/v1/schema", weaviate_url.trim_end_matches('/'));
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("GET {} failed: {}", url, e))?;

    if !resp.status().is_success() {
        return Err(format!(
            "GET {} returned non-2xx status {}",
            url,
            resp.status()
        ));
    }

    let body = resp
        .json::<serde_json::Value>()
        .await
        .map_err(|e| format!("schema body JSON parse failed: {}", e))?;

    let classes = body.get("classes").and_then(|v| v.as_array());
    let Some(classes) = classes else {
        // No classes array → empty schema.
        return Ok(HashMap::new());
    };

    let mut map = HashMap::with_capacity(classes.len());
    for c in classes {
        if let Some(name) = c.get("class").and_then(|v| v.as_str()) {
            if !name.is_empty() {
                map.insert(name.to_lowercase(), name.to_string());
            }
        }
    }
    Ok(map)
}

/// Test-only: clear the cache so unit tests start fresh.
#[doc(hidden)]
pub fn _reset_cache_for_test() {
    if let Ok(mut guard) = SCHEMA_CACHE.lock() {
        *guard = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU16, Ordering};
    use tokio::net::TcpListener;

    /// Spin up a minimal HTTP server that responds to GET /v1/schema with
    /// `{"classes": [{"class": <name>} ...]}` from the configured list.
    /// Returns the bound URL.
    async fn spawn_fake_weaviate(classes: Vec<String>) -> (String, tokio::task::JoinHandle<()>) {
        use axum::{routing::get, Json, Router};
        let classes_payload: Vec<serde_json::Value> = classes
            .iter()
            .map(|c| serde_json::json!({ "class": c }))
            .collect();
        let body = serde_json::json!({ "classes": classes_payload });
        let app = Router::new().route(
            "/v1/schema",
            get(move || {
                let body = body.clone();
                async move { Json(body) }
            }),
        );
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let url = format!("http://{}", addr);
        let handle = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (url, handle)
    }

    static NEXT_PORT: AtomicU16 = AtomicU16::new(1);

    fn unique_cache_url(base: &str) -> String {
        // Prevent cache cross-contamination between concurrent tests.
        let n = NEXT_PORT.fetch_add(1, Ordering::Relaxed);
        format!("{}#test-{}", base, n)
    }

    #[tokio::test]
    async fn case_different_sibling_returns_on_disk_casing() {
        _reset_cache_for_test();
        let (url, _server) = spawn_fake_weaviate(vec![
            "Vibecodedorchestrator_Development".to_string(),
            "VibeCodedOrchestrator_KnowledgeGraph".to_string(),
        ])
        .await;
        let cache_url = unique_cache_url(&url);
        // Patch the cache key by using the actual url for fetch but a unique
        // string in the cache layer is handled by passing a fresh URL each
        // time (NOT separate cache-keys — for THIS test we want a fresh
        // cache entry, so we reset above).
        let _ = cache_url; // unused — full reset is sufficient here.

        let candidate = "VibeCodedOrchestrator_Development"; // capital-C
        let resolved = resolve_existing_casing_for_class(&url, candidate).await;
        assert_eq!(
            resolved, "Vibecodedorchestrator_Development",
            "expected adopt-on-disk casing"
        );
    }

    #[tokio::test]
    async fn no_sibling_returns_candidate_unchanged() {
        _reset_cache_for_test();
        let (url, _server) =
            spawn_fake_weaviate(vec!["SomeOtherProject_Development".to_string()]).await;
        let candidate = "MyProject_Development";
        let resolved = resolve_existing_casing_for_class(&url, candidate).await;
        assert_eq!(resolved, candidate);
    }

    #[tokio::test]
    async fn empty_schema_returns_candidate_unchanged() {
        _reset_cache_for_test();
        let (url, _server) = spawn_fake_weaviate(vec![]).await;
        let candidate = "AnyProject_Development";
        let resolved = resolve_existing_casing_for_class(&url, candidate).await;
        assert_eq!(resolved, candidate);
    }

    #[tokio::test]
    async fn weaviate_unreachable_returns_candidate_unchanged() {
        _reset_cache_for_test();
        // Bind+drop a listener to obtain a definitely-closed port.
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let url = format!("http://{}", addr);
        let candidate = "AnyProject_Development";
        let resolved = resolve_existing_casing_for_class(&url, candidate).await;
        // Soft-fail: candidate echoed back unchanged.
        assert_eq!(resolved, candidate);
    }

    #[tokio::test]
    async fn empty_candidate_returns_empty() {
        _reset_cache_for_test();
        let (url, _server) = spawn_fake_weaviate(vec![]).await;
        let resolved = resolve_existing_casing_for_class(&url, "").await;
        assert_eq!(resolved, "");
    }

    #[tokio::test]
    async fn exact_match_returns_candidate_unchanged() {
        _reset_cache_for_test();
        let (url, _server) =
            spawn_fake_weaviate(vec!["ExactlyThis_Development".to_string()]).await;
        let candidate = "ExactlyThis_Development";
        let resolved = resolve_existing_casing_for_class(&url, candidate).await;
        assert_eq!(resolved, candidate);
    }
}
