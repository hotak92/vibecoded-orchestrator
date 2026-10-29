//! ONE home for Weaviate class counts — the live Aggregate fetch, the
//! launcher.db count cache (migration 050, `db::count_cache`), and the
//! live-vs-cached decision.
//!
//! WHY THIS MODULE EXISTS (v0.2.105): Weaviate 1.28 loads a collection
//! into RAM the first time anything queries it and never unloads it. The
//! launcher's KG and /codegraph pages, the legacy-codegraph wizard and the
//! hub's `/cli/kg|codegraph/collections` twins each ran one GraphQL
//! `Aggregate { <Class> { meta { count } } }` per class on the machine at
//! every render — loading EVERY project's KG and code graph into
//! Weaviate's memory, although a project only ever reads its own
//! collections plus the shared one (owner ruling 2026-10-29: "each project
//! should only look at its own (+ shared) collections").
//!
//! POLICY: a live Aggregate runs ONLY for classes in the caller-supplied
//! scope — the acting project's own collections, the shared/root KG, and
//! collections the access matrix granted the project read access to — or
//! when the user presses "Refresh counts" (which counts everything once
//! and refills the cache). Every other class is served from the cache and
//! shows "—" when it was never counted.
//!
//! CALLERS (keep this list true):
//!  * launcher `commands/kg.rs` — `kg_list_collections`, `codegraph_list_projects`,
//!    `refresh_collection_counts` (the GUI's "Refresh counts" buttons);
//!  * launcher `commands/project_identity.rs` — the legacy/orphan wizard,
//!    an explicit user action, counts live through the same home (and so
//!    refreshes the cache as a side effect);
//!  * hub `cli_api.rs` — the `/cli/kg/collections` and
//!    `/cli/codegraph/collections` twins, which carry no acting project
//!    and therefore serve cached counts only.
//!
//! `db/access.rs::adopt_populated_collections_at_boot` keeps its own
//! boot-only, suffix-limited Aggregate call by design (it runs once at
//! boot over adoption candidates, not per render).

use std::collections::HashSet;

use crate::db::Db;

/// One class's resolved count.
///
/// * `count: None` — never counted (or invalidated): the UI shows "—".
/// * `counted_at` — the epoch-ms moment the number was measured. `None`
///   exactly when `count` is `None`.
/// * `live` — true when THIS resolution ran a live Aggregate (the number
///   is fresh as of now); false when it came from the cache. A live fetch
///   that FAILED also reports `live: false` with the stale cached value,
///   if any — the conservative fallback for a display-only concern.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassCount {
    pub count: Option<u64>,
    pub counted_at: Option<i64>,
    pub live: bool,
}

impl ClassCount {
    fn never_counted() -> Self {
        ClassCount {
            count: None,
            counted_at: None,
            live: false,
        }
    }

    fn from_cache(cached: Option<crate::db::count_cache::CachedCount>) -> Self {
        match cached {
            Some(c) => ClassCount {
                count: Some(c.count),
                counted_at: Some(c.counted_at),
                live: false,
            },
            None => ClassCount::never_counted(),
        }
    }
}

/// The live Aggregate count for one class — the single live-fetch
/// implementation every interactive surface goes through. Weaviate class
/// names are restricted to `[A-Za-z][A-Za-z0-9_]*`, so the name needs no
/// quoting inside the GraphQL body.
///
/// A 200 response that carries a GraphQL `errors` array, or whose count
/// pointer is missing, is an ERROR ("unknown"), never a count: caching a
/// GraphQL error as `0` would poison the cache with a fresh-timestamped
/// zero for a class that may hold thousands of objects (v0.2.105 fix).
///
/// Replaces the former per-file copies in `commands/kg.rs`,
/// `commands/project_identity.rs` and hub `cli_api.rs` (the boot-only
/// copy in `db/access.rs` stays, see the module doc).
pub async fn fetch_class_count(
    client: &reqwest::Client,
    base_url: &str,
    class: &str,
) -> Result<u64, String> {
    let url = format!("{}/v1/graphql", base_url.trim_end_matches('/'));
    let body = serde_json::json!({
        "query": format!("{{ Aggregate {{ {class} {{ meta {{ count }} }} }} }}", class = class)
    });
    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("graphql {}: {}", class, e))?;
    if !resp.status().is_success() {
        return Err(format!(
            "graphql {}: status {}",
            class,
            resp.status().as_u16()
        ));
    }
    let v: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("parse {}: {}", class, e))?;
    if let Some(errors) = v.get("errors").and_then(|e| e.as_array()) {
        if !errors.is_empty() {
            return Err(format!("graphql {}: response carried errors", class));
        }
    }
    v.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
        .and_then(|n| n.as_u64())
        .ok_or_else(|| format!("graphql {}: no count in response", class))
}

/// Count `class` live and cache the result. On a live-fetch failure the
/// last cached value (if any) is served instead, marked `live: false` —
/// a count is display-only, so a Weaviate hiccup degrades to "stale
/// number", never to an error — and the cache is NEVER written on that
/// arm (an unknown count must not overwrite or seed a cached number).
/// Used by the legacy/orphan wizard through [`wizard_display_count`].
pub async fn count_and_cache(
    db: &Db,
    client: &reqwest::Client,
    base_url: &str,
    class: &str,
) -> ClassCount {
    match fetch_class_count(client, base_url, class).await {
        Ok(n) => {
            // Soft-fail: a cache write must never fail a live count.
            if let Err(e) = db.collection_count_set(class, n) {
                tracing::warn!("[vct] weaviate_counts: {}", e);
            }
            let counted_at = db
                .collection_count_get(class)
                .ok()
                .flatten()
                .map(|c| c.counted_at)
                .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
            ClassCount {
                count: Some(n),
                counted_at: Some(counted_at),
                live: true,
            }
        }
        Err(e) => {
            tracing::warn!("[vct] weaviate_counts: {} (serving cached count)", e);
            ClassCount::from_cache(db.collection_count_get(class).ok().flatten())
        }
    }
}

/// The legacy/orphan wizard's display rule (v0.2.105 fix): ONLY a count
/// measured LIVE right now may be shown. A live fetch that failed
/// (`live: false`, possibly carrying a stale cached number) returns
/// `None` — the caller SKIPS the class (the pre-v0.2.105 behaviour)
/// instead of showing a cached value, because the wizard's emptiness
/// decision (`count == 0`) must rest on a fresh measurement, and its
/// delete path re-verifies live anyway.
pub fn wizard_display_count(cc: &ClassCount) -> Option<u64> {
    if cc.live {
        cc.count
    } else {
        None
    }
}

/// Resolve counts for `classes` under the lazy-load policy: classes in
/// `live_scope` are counted live (and their cache entries refreshed);
/// every other class is served from the cache ("—" when never counted).
///
/// `live_scope` entries are matched against the class names by
/// byte-equality. An EMPTY scope is valid and means "cached-only" — the
/// hub twins use exactly that (they have no acting project whose own
/// collections could be counted).
///
/// Returns one [`ClassCount`] per input class, in input order.
pub async fn resolve_counts(
    db: &Db,
    client: &reqwest::Client,
    base_url: &str,
    classes: &[String],
    live_scope: &HashSet<String>,
) -> Vec<ClassCount> {
    let mut out = Vec::with_capacity(classes.len());
    for class in classes {
        if live_scope.contains(class.as_str()) {
            out.push(count_and_cache(db, client, base_url, class).await);
        } else {
            out.push(ClassCount::from_cache(
                db.collection_count_get(class).ok().flatten(),
            ));
        }
    }
    out
}

/// Count EVERY class live, once, refreshing the whole cache. This is the
/// explicit "Refresh counts" action — the only path (besides the equally
/// explicit orphan wizard) allowed to Aggregate-count classes outside any
/// project's scope, because the user asked for it.
pub async fn refresh_counts(
    db: &Db,
    client: &reqwest::Client,
    base_url: &str,
    classes: &[String],
) -> Vec<ClassCount> {
    let all: HashSet<String> = classes.iter().cloned().collect();
    resolve_counts(db, client, base_url, classes, &all).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use std::sync::{Arc, Mutex};

    /// What the fake serves for one class.
    #[derive(Clone, Debug)]
    enum Answer {
        /// A successful GraphQL count.
        Count(u64),
        /// Make the live fetch fail with a non-2xx status.
        HttpError,
        /// Serve a 200 whose body carries a GraphQL `errors` array —
        /// "unknown" under the v0.2.105 fix, never a count of 0.
        GraphqlError,
    }

    /// A minimal fake Weaviate over raw TCP (tokio, no new deps): serves
    /// `/v1/graphql` by extracting the class name out of the Aggregate
    /// query and answering with `counts[class]`, and records every class
    /// it was asked to count so tests can assert exactly which classes
    /// were Aggregate-queried.
    struct FakeWeaviate {
        url: String,
        asked: Arc<Mutex<Vec<String>>>,
    }

    impl FakeWeaviate {
        /// `counts` maps class name → what to serve. A class absent from
        /// the map gets a 200 with no count pointer (also "unknown").
        async fn start(counts: Arc<Mutex<Vec<(String, Answer)>>>) -> Self {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("bind");
            let addr = listener.local_addr().expect("addr");
            let asked: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
            let asked_srv = asked.clone();
            tokio::spawn(async move {
                loop {
                    let (mut stream, _) = match listener.accept().await {
                        Ok(s) => s,
                        Err(_) => return,
                    };
                    let counts = counts.clone();
                    let asked = asked_srv.clone();
                    tokio::spawn(async move {
                        // Read headers + body (content-length framed).
                        let mut buf = Vec::new();
                        let mut tmp = [0u8; 4096];
                        loop {
                            match stream.read(&mut tmp).await {
                                Ok(0) | Err(_) => return,
                                Ok(n) => {
                                    buf.extend_from_slice(&tmp[..n]);
                                    if let Some(hdr_end) =
                                        buf.windows(4).position(|w| w == b"\r\n\r\n")
                                    {
                                        let headers =
                                            String::from_utf8_lossy(&buf[..hdr_end]).to_string();
                                        let len = headers
                                            .lines()
                                            .find_map(|l| {
                                                let (k, v) = l.split_once(':')?;
                                                if k.trim().eq_ignore_ascii_case("content-length") {
                                                    v.trim().parse::<usize>().ok()
                                                } else {
                                                    None
                                                }
                                            })
                                            .unwrap_or(0);
                                        if buf.len() >= hdr_end + 4 + len {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                        let body = String::from_utf8_lossy(&buf).to_string();
                        // Extract the class out of "Aggregate { <Class> {".
                        let class = body
                            .split("Aggregate { ")
                            .nth(1)
                            .and_then(|rest| rest.split(" {").next())
                            .unwrap_or("")
                            .to_string();
                        asked.lock().unwrap().push(class.clone());
                        let entry = counts
                            .lock()
                            .unwrap()
                            .iter()
                            .find(|(n, _)| *n == class)
                            .cloned();
                        let (status, payload) = match entry {
                            Some((_, Answer::HttpError)) => (
                                "HTTP/1.1 500 Internal Server Error",
                                "{\"error\":\"boom\"}".to_string(),
                            ),
                            Some((_, Answer::GraphqlError)) => (
                                "HTTP/1.1 200 OK",
                                serde_json::json!({
                                    "data": null,
                                    "errors": [ { "message": "boom" } ]
                                })
                                .to_string(),
                            ),
                            Some((c, Answer::Count(n))) => (
                                "HTTP/1.1 200 OK",
                                serde_json::json!({
                                    "data": { "Aggregate": { c: [ { "meta": { "count": n } } ] } }
                                })
                                .to_string(),
                            ),
                            None => ("HTTP/1.1 200 OK", serde_json::json!({"data": {}}).to_string()),
                        };
                        let http = format!(
                            "{}\r\nContent-Type: application/json\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            status,
                            payload.len(),
                            payload
                        );
                        let _ = stream.write_all(http.as_bytes()).await;
                    });
                }
            });
            FakeWeaviate {
                url: format!("http://{}", addr),
                asked,
            }
        }
    }

    fn client() -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .unwrap()
    }

    fn classes(names: &[&str]) -> Vec<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    fn scope(names: &[&str]) -> HashSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    // ── 1. live scope: counted live AND cached ─────────────────────────
    #[tokio::test]
    async fn in_scope_classes_are_counted_live_and_cached() {
        let counts = Arc::new(Mutex::new(vec![
            ("Own_KnowledgeGraph".to_string(), Answer::Count(11)),
            ("Other_KnowledgeGraph".to_string(), Answer::Count(22)),
        ]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        let cls = classes(&["Own_KnowledgeGraph", "Other_KnowledgeGraph"]);
        let live = scope(&["Own_KnowledgeGraph"]);

        let out = resolve_counts(&db, &client(), &fake.url, &cls, &live).await;

        assert_eq!(
            out[0],
            ClassCount {
                count: Some(11),
                counted_at: out[0].counted_at,
                live: true
            },
            "the in-scope class is counted live"
        );
        assert!(out[0].counted_at.is_some());
        // The live run refreshed the cache.
        assert_eq!(
            db.collection_count_get("Own_KnowledgeGraph")
                .expect("get")
                .map(|c| c.count),
            Some(11),
            "a live count must land in the cache"
        );
    }

    // ── 2. out of scope: cached only, never Aggregate-queried ─────────
    #[tokio::test]
    async fn out_of_scope_classes_come_from_the_cache_and_are_never_queried() {
        let counts = Arc::new(Mutex::new(vec![
            ("Own_KnowledgeGraph".to_string(), Answer::Count(11)),
            ("Other_KnowledgeGraph".to_string(), Answer::Count(22)),
        ]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        // Pre-seed the cache for the out-of-scope class (as a previous
        // Refresh or a previous live run would have).
        db.collection_count_set("Other_KnowledgeGraph", 20).expect("seed");

        let cls = classes(&["Own_KnowledgeGraph", "Other_KnowledgeGraph"]);
        let live = scope(&["Own_KnowledgeGraph"]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &live).await;

        assert_eq!(out[1].count, Some(20), "cached value is served");
        assert!(!out[1].live, "the out-of-scope class was not counted live");
        assert_eq!(
            out[1].counted_at,
            db.collection_count_get("Other_KnowledgeGraph")
                .expect("get")
                .map(|c| c.counted_at),
            "the cache's timestamp is carried through"
        );
        // THE key assertion: Weaviate was Aggregate-asked ONLY for the
        // in-scope class — counting must never lazy-load the rest.
        assert_eq!(
            fake.asked.lock().unwrap().clone(),
            vec!["Own_KnowledgeGraph".to_string()],
            "no Aggregate may run for a class outside the live scope"
        );
    }

    // ── 3. never counted: "—" (None), still not queried ───────────────
    #[tokio::test]
    async fn never_counted_out_of_scope_class_reads_as_unknown() {
        let counts = Arc::new(Mutex::new(vec![(
            "Own_KnowledgeGraph".to_string(),
            Answer::Count(11),
        )]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");

        let cls = classes(&["Own_KnowledgeGraph", "Ghost_KnowledgeGraph"]);
        let live = scope(&["Own_KnowledgeGraph"]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &live).await;

        assert_eq!(
            out[1],
            ClassCount::never_counted(),
            "an out-of-scope class with no cache entry shows \"—\""
        );
        assert_eq!(
            fake.asked.lock().unwrap().clone(),
            vec!["Own_KnowledgeGraph".to_string()],
            "an unknown count must not trigger a live query"
        );
    }

    // ── 4. Refresh counts everything once ─────────────────────────────
    #[tokio::test]
    async fn refresh_counts_every_class_once() {
        let counts = Arc::new(Mutex::new(vec![
            ("A_KnowledgeGraph".to_string(), Answer::Count(1)),
            ("B_CodeModule".to_string(), Answer::Count(2)),
            ("C_CodeFunction".to_string(), Answer::Count(3)),
        ]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");

        let cls = classes(&["A_KnowledgeGraph", "B_CodeModule", "C_CodeFunction"]);
        let out = refresh_counts(&db, &client(), &fake.url, &cls).await;

        assert!(out.iter().all(|c| c.live && c.count.is_some()));
        for c in &cls {
            assert!(
                db.collection_count_get(c).expect("get").is_some(),
                "Refresh must fill the cache for {}",
                c
            );
        }
        let mut asked = fake.asked.lock().unwrap().clone();
        asked.sort();
        assert_eq!(
            asked,
            vec![
                "A_KnowledgeGraph".to_string(),
                "B_CodeModule".to_string(),
                "C_CodeFunction".to_string()
            ],
            "Refresh counts EVERY class, including out-of-scope ones"
        );
    }

    // ── 5. live fetch failure → stale cached value, live=false ────────
    #[tokio::test]
    async fn live_fetch_failure_serves_the_cached_value() {
        let counts = Arc::new(Mutex::new(vec![(
            "Flaky_KnowledgeGraph".to_string(),
            Answer::HttpError, // make the live fetch fail
        )]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        db.collection_count_set("Flaky_KnowledgeGraph", 33).expect("seed");

        let cls = classes(&["Flaky_KnowledgeGraph"]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &scope(&["Flaky_KnowledgeGraph"])).await;

        assert_eq!(
            out[0],
            ClassCount {
                count: Some(33),
                counted_at: out[0].counted_at,
                live: false
            },
            "a failed live count falls back to the cached number, marked not-live"
        );
        assert!(out[0].counted_at.is_some());
    }

    // ── 6. empty scope = cached-only (the hub twins' mode) ────────────
    #[tokio::test]
    async fn empty_scope_never_touches_weaviate() {
        let counts = Arc::new(Mutex::new(Vec::new()));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        db.collection_count_set("Any_KnowledgeGraph", 5).expect("seed");

        let cls = classes(&["Any_KnowledgeGraph"]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &HashSet::new()).await;

        assert_eq!(out[0].count, Some(5));
        assert!(!out[0].live);
        assert!(
            fake.asked.lock().unwrap().is_empty(),
            "an empty live scope must not Aggregate-query anything"
        );
    }

    // ── 7. a GraphQL error (or a missing count) is UNKNOWN, never 0 ──
    //
    // Pins the v0.2.105 fix: a 200 carrying `errors`, and a 200 whose
    // count pointer is missing, are ERRORS. The pre-fix code did
    // `.unwrap_or(0)` on the missing pointer, so Weaviate failing to
    // count a class was cached as a FRESH 0 — a lie with a brand-new
    // timestamp. Nothing may be written to the cache on that arm.
    #[tokio::test]
    async fn graphql_error_and_missing_count_are_unknown_and_never_cached() {
        let counts = Arc::new(Mutex::new(vec![
            // 200 + GraphQL errors array.
            ("Broken_KnowledgeGraph".to_string(), Answer::GraphqlError),
            // A REAL zero: a successful measurement of an empty class —
            // still a count, still cached (the leave-alone side).
            ("Empty_KnowledgeGraph".to_string(), Answer::Count(0)),
        ]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        // "Pointerless_KnowledgeGraph" is NOT in the fake's map → the
        // fake serves 200 {"data":{}} → the count pointer is missing.
        let cls = classes(&[
            "Broken_KnowledgeGraph",
            "Empty_KnowledgeGraph",
            "Pointerless_KnowledgeGraph",
        ]);
        let live = scope(&[
            "Broken_KnowledgeGraph",
            "Empty_KnowledgeGraph",
            "Pointerless_KnowledgeGraph",
        ]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &live).await;

        // ACT: the GraphQL error is unknown ("—"), not a cached 0.
        assert_eq!(out[0].count, None, "a GraphQL error must read as unknown");
        assert!(!out[0].live);
        assert!(
            db.collection_count_get("Broken_KnowledgeGraph")
                .expect("get")
                .is_none(),
            "a GraphQL error must NEVER be written to the cache (as 0 or anything else)"
        );
        // ACT: the missing count pointer is unknown too, and uncached.
        assert_eq!(out[2].count, None, "a missing count must read as unknown");
        assert!(
            db.collection_count_get("Pointerless_KnowledgeGraph")
                .expect("get")
                .is_none(),
            "a missing count must NEVER be written to the cache"
        );
        // LEAVE-ALONE: a measured zero IS a count — cached and shown.
        assert_eq!(out[1].count, Some(0));
        assert!(out[1].live, "a real zero was measured live");
        assert_eq!(
            db.collection_count_get("Empty_KnowledgeGraph")
                .expect("get")
                .map(|c| c.count),
            Some(0),
            "a real zero lands in the cache"
        );
    }

    // ── 8. the wizard rule: a failed live count never shows a cached
    //        number — the class is skipped (pre-v0.2.105 behaviour) ────
    #[tokio::test]
    async fn wizard_never_shows_a_cached_count_for_a_failed_live_count() {
        let counts = Arc::new(Mutex::new(vec![
            ("Stale_KnowledgeGraph".to_string(), Answer::HttpError),
            ("Fresh_KnowledgeGraph".to_string(), Answer::Count(7)),
        ]));
        let fake = FakeWeaviate::start(counts).await;
        let db = Db::open_in_memory().expect("in-memory db");
        // A previous Refresh cached 42 for the class whose live count
        // is about to fail — exactly the stale number the wizard must
        // NOT show.
        db.collection_count_set("Stale_KnowledgeGraph", 42).expect("seed");

        let cls = classes(&["Stale_KnowledgeGraph", "Fresh_KnowledgeGraph"]);
        let live = scope(&["Stale_KnowledgeGraph", "Fresh_KnowledgeGraph"]);
        let out = resolve_counts(&db, &client(), &fake.url, &cls, &live).await;

        // ACT: the failed live count → skip (None), never the cached 42.
        assert_eq!(
            wizard_display_count(&out[0]),
            None,
            "the wizard skips a class whose live count failed — no cached fallback"
        );
        // The cached value survives in the cache untouched (the listing
        // surfaces may still serve it, marked not-live).
        assert_eq!(out[0].count, Some(42));
        assert!(!out[0].live);
        // LEAVE-ALONE: a successful live count is shown.
        assert_eq!(wizard_display_count(&out[1]), Some(7));
    }
}
