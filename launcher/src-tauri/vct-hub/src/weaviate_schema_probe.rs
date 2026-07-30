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
//! See: the v0.2.52 root-cause audit (Symptom B) for the full
//! root-cause walk.
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
    /// v0.2.89 (BUG 4 §5.4): whether the `/v1/schema` fetch that produced
    /// this snapshot SUCCEEDED. Before this flag, a failed fetch
    /// negative-cached an EMPTY map that was indistinguishable from a
    /// genuinely-empty schema — so "Weaviate down" and "class absent"
    /// collapsed into the same observation. `resolve_existing_casing_for_class`
    /// treats both identically (echo the candidate — correct for casing), but
    /// [`class_exists`] MUST distinguish them: warning about a "phantom"
    /// class on the evidence of a failed probe would be a false alarm on
    /// every transient Weaviate hiccup.
    probe_ok: bool,
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
            store_cached(weaviate_url, map, true);
            actual.unwrap_or_else(|| candidate.to_string())
        }
        Err(_) => {
            // Soft-fail: keep the candidate. The downstream caller will
            // surface a Weaviate-side error if the name is genuinely wrong;
            // we don't want every transient probe failure to break resolves.
            //
            // NEGATIVE-CACHE (Windows step22 hang fix): store an EMPTY
            // snapshot so the remaining probes in this same resolve —
            // `project_config` calls this 4× per request (kg / shared_kg /
            // development / diagrams) — become cache HITS (empty map ⇒ no
            // case-different sibling ⇒ `Some(candidate)`) instead of each
            // re-issuing a fresh reqwest. Without this, an unreachable
            // Weaviate cost 4× the connect stall per `/config` resolve.
            //
            // Why this matters on Windows specifically: connecting to a
            // CLOSED `localhost:8081` returns ECONNREFUSED near-instantly on
            // Linux/macOS, but Windows applies SYN-retransmit backoff to a
            // non-listening port (and `localhost` is dual-stack v4+v6), so a
            // single failed connect can ride most of `connect_timeout`. 4×
            // that budget blew past the step22 test's 5s client timeout →
            // the authed `/config` route hung while `/health` (no probe)
            // answered. One attempt + cache-fanout keeps the worst case to a
            // single bounded stall. The empty snapshot is subject to the same
            // `CACHE_TTL` (5s) as a successful one, so a Weaviate that comes
            // up shortly after is picked up on the next resolve.
            //
            // v0.2.89: `probe_ok = false` marks this as a FAILED-probe
            // snapshot so `class_exists` refuses to answer from it (probe
            // failure ≠ absence). The casing-resolve behaviour above is
            // unchanged.
            store_cached(weaviate_url, HashMap::new(), false);
            candidate.to_string()
        }
    }
}

/// v0.2.89 (BUG 4 §5.4) — cache-only existence check for a Weaviate class.
///
/// Returns:
///   * `Some(true)`  — a fresh snapshot from a SUCCESSFUL probe contains the
///     class (any casing).
///   * `Some(false)` — a fresh successful snapshot does NOT contain it: the
///     class is genuinely absent on disk.
///   * `None` — no fresh snapshot, or the fresh snapshot came from a FAILED
///     probe (negative-cache). Callers must NEVER warn on `None`: "Weaviate
///     down" is not evidence of absence.
///
/// Deliberately reads ONLY the cache (no network): the `/config` resolver
/// calls `resolve_existing_casing_for_class` several times immediately before
/// this, so within one request the snapshot is warm. A cold/expired cache
/// (only possible if >CACHE_TTL elapsed mid-request) degrades to `None` —
/// i.e. no warning — which is the conservative direction.
pub fn class_exists(weaviate_url: &str, class_name: &str) -> Option<bool> {
    if class_name.is_empty() {
        return None;
    }
    let guard = SCHEMA_CACHE.lock().ok()?;
    let cache = guard.as_ref()?;
    let entry = cache.get(weaviate_url)?;
    if entry.captured_at.elapsed() > CACHE_TTL {
        return None;
    }
    if !entry.probe_ok {
        return None;
    }
    Some(entry.lower_to_actual.contains_key(&class_name.to_lowercase()))
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
/// `probe_ok` records whether the producing fetch succeeded (v0.2.89 —
/// consumed by [`class_exists`]; the casing resolver ignores it).
fn store_cached(weaviate_url: &str, map: HashMap<String, String>, probe_ok: bool) {
    let Ok(mut guard) = SCHEMA_CACHE.lock() else {
        return;
    };
    let cache = guard.get_or_insert_with(HashMap::new);
    cache.insert(
        weaviate_url.to_string(),
        CachedSchema {
            lower_to_actual: map,
            captured_at: Instant::now(),
            probe_ok,
        },
    );
}

/// Fetch `<weaviate_url>/v1/schema` and build the lowercased→actual map.
async fn fetch_schema_map(weaviate_url: &str) -> Result<HashMap<String, String>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        // Bound the CONNECT phase separately from the total request timeout.
        // On a reachable-but-slow Weaviate the 2s total still applies; the
        // value of a discrete connect_timeout is the UNREACHABLE case — a
        // closed `localhost:8081` on Windows does not get a fast RST (SYN-
        // retransmit backoff on a non-listening port, dual-stack v4+v6), so
        // without this the connect could ride most of the 2s total budget.
        // Capping it at 1s keeps a single failed probe cheap on every OS
        // (Linux/macOS already fail near-instantly with ECONNREFUSED; this is
        // a no-op there and a safety bound on Windows). Combined with the
        // negative-cache in `resolve_existing_casing_for_class`, a `/config`
        // resolve against a down Weaviate costs one ≤1s stall, not four.
        .connect_timeout(Duration::from_secs(1))
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

    /// Windows step22-hang regression (2026-07-09): an UNREACHABLE Weaviate
    /// must be probed at most ONCE per cache-TTL window, even across the
    /// several candidate resolves a single `/config` request issues. Before
    /// the negative-cache the error branch never populated the cache, so
    /// `project_config`'s 4 sequential probe calls each re-issued a fresh
    /// reqwest — 4× the connect stall, which on Windows (slow connect to a
    /// closed dual-stack `localhost` port) blew past the test client's 5s
    /// timeout and hung the authed route while `/health` still answered.
    ///
    /// We assert the FANOUT-COLLAPSE directly: after the first (network)
    /// resolve caches an empty snapshot, a follow-up resolve for a DIFFERENT
    /// candidate against the same unreachable URL is served from cache — no
    /// second network attempt. `lookup_cached` returning `Some(_)` for the
    /// second candidate is the observable proof the probe was not re-run.
    #[tokio::test]
    async fn unreachable_weaviate_negative_caches_to_collapse_fanout() {
        _reset_cache_for_test();
        // A definitely-closed port (bind then drop).
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let url = format!("http://{}", addr);

        // Cold cache: no snapshot yet for this URL.
        assert!(
            lookup_cached(&url, "First_Development").is_none(),
            "precondition: URL must be uncached before the first resolve"
        );

        // First resolve does the (failing) network call and negative-caches.
        let first = resolve_existing_casing_for_class(&url, "First_Development").await;
        assert_eq!(first, "First_Development", "soft-fail echoes the candidate");

        // The empty snapshot is now cached, so a resolve for ANY OTHER
        // candidate (the 2nd/3rd/4th probe of one `/config` request) is a
        // cache HIT — no further network attempt. If the negative-cache
        // regressed, this would be `None` (forcing another reqwest).
        assert_eq!(
            lookup_cached(&url, "Second_Diagrams").as_deref(),
            Some("Second_Diagrams"),
            "second candidate must be served from the negative-cache, not re-probed"
        );
        assert_eq!(
            lookup_cached(&url, "Third_KnowledgeGraph").as_deref(),
            Some("Third_KnowledgeGraph"),
            "third candidate must also hit the negative-cache"
        );

        // And the full resolve path for a fresh candidate still soft-fails
        // to the candidate (cache-served, no panic, no hang).
        let fourth = resolve_existing_casing_for_class(&url, "Fourth_Development").await;
        assert_eq!(fourth, "Fourth_Development");
    }

    /// v0.2.89 (BUG 4 §5.4) — after a SUCCESSFUL probe, `class_exists`
    /// answers definitively from the cached snapshot: present class →
    /// `Some(true)`, absent class → `Some(false)`.
    #[tokio::test]
    async fn class_exists_answers_from_successful_snapshot() {
        _reset_cache_for_test();
        let (url, _server) = spawn_fake_weaviate(vec![
            "HouseOfFire_CodeModule".to_string(),
            "HouseOfFire_KnowledgeGraph".to_string(),
        ])
        .await;
        // Warm the cache with one resolve (as project_config does).
        let _ = resolve_existing_casing_for_class(&url, "HouseOfFire_KnowledgeGraph").await;

        assert_eq!(
            class_exists(&url, "HouseOfFire_CodeModule"),
            Some(true),
            "present class must be Some(true)"
        );
        // Case-insensitive: on-disk casing differences still count as present.
        assert_eq!(
            class_exists(&url, "houseoffire_codemodule"),
            Some(true),
            "existence is casing-insensitive (Weaviate class identity)"
        );
        assert_eq!(
            class_exists(&url, "HouseOfFlirt_CodeModule"),
            Some(false),
            "absent class must be Some(false) — this is the phantom signal"
        );
    }

    /// LEAVE-ALONE leg: probe failure ≠ absence. After an UNREACHABLE
    /// Weaviate negative-caches an empty snapshot, `class_exists` must
    /// return `None` for every candidate — never `Some(false)`. This is the
    /// distinction the v0.2.89 `probe_ok` flag exists for: pre-fix, the
    /// negative-cache was indistinguishable from an empty schema and any
    /// existence-check layered on it would have warned on every transient
    /// Weaviate outage.
    #[tokio::test]
    async fn class_exists_returns_none_after_failed_probe() {
        _reset_cache_for_test();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let url = format!("http://{}", addr);

        // Trigger the failing probe → negative-cache with probe_ok=false.
        let _ = resolve_existing_casing_for_class(&url, "Any_KnowledgeGraph").await;

        assert_eq!(
            class_exists(&url, "Any_KnowledgeGraph"),
            None,
            "failed-probe snapshot must yield None (probe failure ≠ absence)"
        );
        assert_eq!(
            class_exists(&url, "Other_CodeModule"),
            None,
            "every candidate against a failed-probe snapshot must be None"
        );
    }

    /// LEAVE-ALONE leg: a cold (never-probed) cache also answers `None` —
    /// `class_exists` never fetches on its own.
    #[tokio::test]
    async fn class_exists_returns_none_on_cold_cache() {
        _reset_cache_for_test();
        assert_eq!(class_exists("http://127.0.0.1:1#cold", "X_CodeModule"), None);
        // Empty class name is never answerable either.
        assert_eq!(class_exists("http://127.0.0.1:1#cold", ""), None);
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
