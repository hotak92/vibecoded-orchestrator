// SPDX-License-Identifier: AGPL-3.0-or-later
//
// module_catalog_client — Launcher-side client for the public `module-catalog`
// edge function (L0). See `supabase/functions/module-catalog/index.ts` for
// the endpoint and `docs/v0.2.33-l0-deploy.md` for deploy notes.
//
// Why this lives in launcher (Rust) rather than the Svelte front-end:
//   - the same fetch path drives BOTH the Modules-page render (display) AND
//     `install_module_for_project` (pre-pull install slice). Putting it in
//     Rust lets one cache layer serve both consumers.
//   - launcher.db's `app_state` table is the canonical place for short-TTL
//     ephemeral state shared across Tauri windows. The renderer doesn't have
//     direct DB access — calling through here is the only consistent path.
//   - schema_version mismatch handling (review §10.d) is easier in Rust where
//     the serde-deserialised type IS the contract version.
//
// Cache strategy:
//   - in-process: none (every call hits the DB-backed cache).
//   - launcher.db `app_state`: serialized L0 response + a `_fetched_at` epoch
//     ms key. 15min TTL on the value. On expiry: refetch; on refetch failure
//     AND cached value still present: return stale cache + log warning. On
//     refetch failure AND no cache: bubble Err. Cache poisoning protection:
//     parse failures are NEVER written back to the cache; the previous good
//     value (if any) survives.
//
// Retry / backoff:
//   - reuses Agent E's v0.2.32 UB1 pattern from `self_update::fetch_with_retry`:
//     first attempt immediate, then delays of 1s / 5s / 30s / 120s (5 attempts
//     total, ~156s upper bound). Production code interprets the values as ms,
//     tests interpret as 1/1000 of that to keep CI snappy.
//   - all non-200 HTTP responses and all network errors are retryable. We
//     don't try to distinguish — the cheapest signal is "did it succeed yet".
//   - parse failures are NOT retryable (a malformed response will be malformed
//     on the next attempt too, and we want the cache-poisoning protection
//     above to bite immediately).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::command;

use crate::db::Db;

// ─── Schema-version constants ─────────────────────────────────────────────

/// Schema version this launcher knows how to parse. Bumped whenever the L0
/// response shape changes incompatibly. See review §10.d.
pub const CURRENT_SCHEMA_VERSION: u32 = 1;

// ─── Cache constants ──────────────────────────────────────────────────────

/// In-DB cache TTL — the launcher considers the cached L0 response fresh
/// for this long. After expiry, the next read triggers a re-fetch (with the
/// previous value held as fallback if the re-fetch fails).
///
/// Chosen at 15 minutes per review §3 ("Caching invalidation rules"): long
/// enough that the catalog tab feels instant after the first visit, short
/// enough that a new module version published mid-session shows up within
/// a reasonable window without the user manually pressing `↻`.
#[allow(dead_code)] // consumed by Agent B's `list_module_catalog_impl` refactor (v0.2.33 L0a)
pub(crate) const L0_TTL_SECONDS: u64 = 15 * 60;

/// Short TTL applied when the L0 response contains zero modules
/// (v0.2.34 — dogfooded 2026-05-25 "stale-on-empty-modules trap"). An
/// empty catalog is almost always transient: the user opened the
/// Modules tab BEFORE a publisher pushed their entry, or the edge
/// function returned nothing during a brief outage. Keeping the empty
/// response cached for 15 minutes makes the launcher feel broken right
/// at the moment the user MOST wants fresh data. 60s gives the network
/// path a short cooldown without locking in an empty view.
///
/// Populated responses keep the longer [`L0_TTL_SECONDS`] — they
/// represent real work by the publisher that's unlikely to change
/// within 15 minutes, and the always-visible `↻` button in
/// `ModuleCatalog.svelte` gives the user a manual force-refresh path
/// for the rare mid-session republish case.
#[allow(dead_code)] // consumed by Agent B's `list_module_catalog_impl` refactor (v0.2.33 L0a)
pub(crate) const L0_TTL_EMPTY_SECONDS: u64 = 60;

/// `app_state` key holding the serialized L0 response (full JSON envelope).
pub(crate) const APP_STATE_KEY_CATALOG: &str = "module_catalog.cache";

/// `app_state` key holding the unix-epoch-seconds timestamp at which the
/// cache value above was written. Stored as a decimal string so it round-
/// trips through `app_state_get` / `app_state_set` (TEXT column).
pub(crate) const APP_STATE_KEY_CATALOG_AT: &str = "module_catalog.cache_fetched_at";

/// `app_state` key holding the launcher version (e.g. "0.2.34") that
/// last wrote to the L0 cache. Read at launcher startup; if it differs
/// from the running `env!("CARGO_PKG_VERSION")` we wipe
/// `module_catalog.cache*` so the fresh launcher does a clean re-fetch.
///
/// Rationale: after `Update orchestrator`, the L0 schema may have
/// changed (new optional fields, new module categories, deprecation
/// shifts). Forcing a re-fetch on the first launch of a new version
/// avoids the footgun where users wonder why their freshly-updated
/// launcher is still showing the cached pre-update view.
///
/// Same-version restarts preserve the cache (no bust), so cold-boot
/// latency is unchanged for the common case.
pub(crate) const APP_STATE_KEY_LAUNCHER_VERSION: &str = "launcher.last_seen_version";

/// LIKE pattern matching every cache key written by this module. Used
/// by [`bust_cache_if_launcher_version_changed`] to wipe the envelope
/// + fetched-at in one DB call.
pub(crate) const APP_STATE_CACHE_LIKE: &str = "module_catalog.cache%";

// ─── Retry/backoff constants ──────────────────────────────────────────────

/// Backoff schedule for transient L0 fetch failures. Identical to
/// `self_update::FETCH_RETRY_DELAYS_MS` (v0.2.32 UB1); same rationale — long
/// enough to absorb a Wi-Fi reconnect or VPN handshake, short enough to bail
/// out before the UI loses patience.
///
/// Under `cfg(test)` the unit is milliseconds × 1, so a full 5-attempt
/// exhaust takes ~156ms instead of 156s. Production code interprets the same
/// numbers as wall-clock milliseconds.
#[cfg(not(test))]
const L0_RETRY_DELAYS_MS: [u64; 4] = [1_000, 5_000, 30_000, 120_000];
#[cfg(test)]
const L0_RETRY_DELAYS_MS: [u64; 4] = [1, 5, 30, 120];

/// Per-attempt HTTP timeout. The L0 response is small (a few KB), so a slow
/// read is almost certainly a hung connection — fail fast and let the retry
/// loop back off rather than blocking the UI.
const L0_HTTP_TIMEOUT: Duration = Duration::from_secs(10);

/// Default production endpoint. Override via env `VCT_MODULE_CATALOG_URL`.
const L0_DEFAULT_URL: &str =
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/module-catalog";

// ─── Response types ───────────────────────────────────────────────────────
//
// These mirror `supabase/functions/module-catalog/index.ts` 1:1. Every
// optional field uses `#[serde(default)]` so the launcher tolerates a
// publisher who omits e.g. `homepage` without a parse failure.

/// Full L0 envelope returned by the edge function. The launcher caches the
/// entire envelope (not just `modules`) so `fetched_at` and `schema_version`
/// survive the round-trip.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0CatalogResponse {
    pub schema_version: u32,
    pub fetched_at: String,
    pub modules: Vec<L0CatalogModule>,
}

/// One paid module's L0 record. This is INTENTIONALLY a separate type from
/// `manifest::ModuleManifest` — the L0 contract is a deliberately-narrow
/// projection (display + install-slice + deprecation surface only); the
/// full `ModuleManifest` covers config-tab + runtime + db + tons more.
/// Keeping them separate prevents the install path from accidentally
/// trying to render a config tab from L0 data (which doesn't contain one).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0CatalogModule {
    // ─── Catalog-display fields ───
    pub id: String,
    pub name: String,
    pub version: String,
    pub description: String,
    pub category: String,
    pub tags: Vec<String>,
    #[serde(default)]
    pub homepage: String,
    #[serde(default)]
    pub publisher: String,

    // ─── Licensing gate ───
    pub license_required: bool,
    pub min_orchestrator_tier: String,
    #[serde(default)]
    pub license_variant_ids: Vec<String>,
    #[serde(default)]
    pub trial_days: Option<u32>,

    // ─── Host compat gate ───
    pub compatibility: L0Compatibility,

    // ─── Install-time slice ───
    pub install: L0Install,
    #[serde(default)]
    pub requirements: Option<L0Requirements>,
    #[serde(default)]
    pub runtime_hints: Option<L0RuntimeHints>,

    // ─── Deprecation surface ───
    #[serde(default)]
    pub deprecated: bool,
    #[serde(default)]
    pub deprecation_message: String,
    #[serde(default)]
    pub deprecation_eol_date: String,
    #[serde(default)]
    pub deprecation_migration_url: String,

    // ─── Post-install manifest hint ───
    #[serde(default = "default_manifest_path")]
    pub post_install_manifest_path: String,
}

fn default_manifest_path() -> String {
    "vct-module.json".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0Compatibility {
    pub hosts: Vec<String>,
    #[serde(default)]
    pub min_launcher_version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0Install {
    pub method: String,
    pub container: L0InstallContainer,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0InstallContainer {
    pub image: String,
    #[serde(default)]
    pub tag_from_version: bool,
    #[serde(default)]
    pub registry: Option<String>,
    pub pull_token_endpoint: String,
    #[serde(default = "default_pull_method")]
    pub pull_token_method: String,
}

fn default_pull_method() -> String {
    "POST".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0Requirements {
    #[serde(default)]
    pub os: Vec<String>,
    #[serde(default)]
    pub memory_mb: Option<u64>,
    #[serde(default)]
    pub disk_mb: Option<u64>,
    #[serde(default)]
    pub gpu: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct L0RuntimeHints {
    #[serde(default)]
    pub gpu_image_variants: std::collections::HashMap<String, String>,
}

// ─── Outcome variants returned to callers ─────────────────────────────────

/// Internal: classifies a single attempt result.
///
/// `Ok(envelope)` — successful fetch + parse.
/// `Transient(err)` — HTTP or network failure; retryable.
/// `Permanent(err)` — parse failure; not retryable, must NOT poison cache.
enum AttemptOutcome {
    Ok(L0CatalogResponse),
    Transient(String),
    Permanent(String),
}

// ─── Public API ───────────────────────────────────────────────────────────

/// Resolve the L0 endpoint URL, honoring `VCT_MODULE_CATALOG_URL` for dev /
/// staging overrides. Pure — returned `String` carries no secrets.
pub fn resolved_endpoint_url() -> String {
    std::env::var("VCT_MODULE_CATALOG_URL").unwrap_or_else(|_| L0_DEFAULT_URL.to_string())
}

/// Fetch the L0 catalog with retry-with-backoff. Returns Err if every
/// attempt fails or if the response cannot be parsed.
///
/// This is the LOW-LEVEL fetch — it does NOT touch the DB cache. Callers
/// that want caching should use [`cached_module_catalog`] /
/// [`refresh_module_catalog`].
pub async fn fetch_module_catalog() -> Result<L0CatalogResponse, String> {
    let url = resolved_endpoint_url();
    let client = reqwest::Client::builder()
        .timeout(L0_HTTP_TIMEOUT)
        .build()
        .map_err(|e| format!("reqwest client build: {}", e))?;
    let url_clone = url.clone();
    fetch_with_retry_async(move || {
        let url = url_clone.clone();
        let client = client.clone();
        async move { single_attempt(&client, &url).await }
    })
    .await
}

/// Read the catalog through the DB-backed 15-min TTL cache.
///
/// Behaviour:
///   - cache present + fresh   → return cached, no fetch.
///   - cache absent OR expired → fetch, write back on success.
///   - fetch fails + cache present (any age) → return stale + log warning.
///   - fetch fails + no cache  → propagate Err.
///
/// Schema-version mismatch (review §10.d) is logged at the deserialise step
/// inside `single_attempt`; the parsed envelope still flows through (we
/// render best-effort) — see `parse_response_text`.
#[allow(dead_code)] // consumed by Agent B's `list_module_catalog_impl` refactor (v0.2.33 L0a)
pub async fn cached_module_catalog(db: &Db) -> Result<L0CatalogResponse, String> {
    if let Some(envelope) = read_cache_if_fresh(db) {
        return Ok(envelope);
    }
    match fetch_module_catalog().await {
        Ok(envelope) => {
            // Best-effort cache write; do NOT fail the whole op if the cache
            // write fails (we already have the data).
            if let Err(e) = write_cache(db, &envelope) {
                eprintln!("[module-catalog] cache write failed: {}", e);
            }
            Ok(envelope)
        }
        Err(fetch_err) => {
            // Stale cache fallback: even if the value is past its TTL, returning
            // it is better than returning Err — the user sees the catalog with
            // a stale-marker badge rather than an empty list + scary banner.
            if let Some(stale) = read_cache_raw(db) {
                eprintln!(
                    "[module-catalog] fetch failed ({}); falling back to stale cache",
                    fetch_err
                );
                return Ok(stale);
            }
            Err(fetch_err)
        }
    }
}

/// Tauri command exposed to the renderer. Bypasses the 15min TTL — used by
/// the Modules-tab `↻` refresh button.
#[command]
pub async fn refresh_module_catalog(
    db: tauri::State<'_, Db>,
) -> Result<L0CatalogResponse, String> {
    let envelope = fetch_module_catalog().await?;
    if let Err(e) = write_cache(&db, &envelope) {
        // Surface to the renderer so the UI can decide whether to retry;
        // we don't fail the whole call because the data IS valid, the DB
        // write is the only loss.
        eprintln!("[module-catalog] refresh cache write failed: {}", e);
    }
    Ok(envelope)
}

// ─── Internals ────────────────────────────────────────────────────────────

/// Single HTTP attempt. Returns `Ok(envelope)` on success, `Transient(_)`
/// for network/HTTP-status failures (retryable), `Permanent(_)` for JSON
/// parse failures (NOT retryable, NOT cacheable).
async fn single_attempt(client: &reqwest::Client, url: &str) -> AttemptOutcome {
    let resp = match client.get(url).send().await {
        Ok(r) => r,
        Err(e) => return AttemptOutcome::Transient(format!("network: {}", e)),
    };
    let status = resp.status();
    if !status.is_success() {
        let body = resp
            .text()
            .await
            .unwrap_or_else(|_| "<no body>".to_string());
        // Truncate body in the error message so a malformed-edge-function
        // megabyte HTML page doesn't blow up the launcher's UI toast.
        let preview: String = body.chars().take(300).collect();
        return AttemptOutcome::Transient(format!("HTTP {}: {}", status, preview));
    }
    let text = match resp.text().await {
        Ok(t) => t,
        Err(e) => return AttemptOutcome::Transient(format!("body read: {}", e)),
    };
    match parse_response_text(&text) {
        Ok(envelope) => AttemptOutcome::Ok(envelope),
        Err(e) => AttemptOutcome::Permanent(e),
    }
}

/// Parse + log schema-version mismatch. Lifted to a free function so it's
/// directly unit-testable without an HTTP server.
pub(crate) fn parse_response_text(text: &str) -> Result<L0CatalogResponse, String> {
    let envelope: L0CatalogResponse =
        serde_json::from_str(text).map_err(|e| format!("JSON parse: {}", e))?;
    if envelope.schema_version != CURRENT_SCHEMA_VERSION {
        // Review §10.d: forward-compat. Log a warning and render best-effort.
        // We don't refuse the response — the launcher will render whatever
        // fields it does recognise (serde drops unknown fields silently with
        // our current struct layout), and the renderer-side L9 banner (Agent
        // E's scope) shows the user that an update is recommended.
        eprintln!(
            "[module-catalog] schema_version mismatch: server={}, launcher knows={}. \
             Rendering best-effort; consider updating the launcher.",
            envelope.schema_version, CURRENT_SCHEMA_VERSION
        );
    }
    Ok(envelope)
}

/// Inner retry loop, parametrised over the attempt closure so unit tests can
/// swap in failure-injecting mocks. Mirrors `self_update::fetch_with_retry`'s
/// shape — first attempt immediate, subsequent attempts wait
/// `L0_RETRY_DELAYS_MS[i-1]`.
///
/// Behaviour:
///   - `AttemptOutcome::Ok(_)`        → return Ok immediately.
///   - `AttemptOutcome::Transient(_)` → record error, sleep, retry.
///   - `AttemptOutcome::Permanent(_)` → return Err immediately (no retry).
///
/// On exhaustion: returns the last Transient error string. If every attempt
/// returned an empty error string (unlikely but possible), returns a
/// non-empty sentinel so the UI never renders a blank toast.
async fn fetch_with_retry_async<F, Fut>(mut attempt_fn: F) -> Result<L0CatalogResponse, String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = AttemptOutcome>,
{
    let mut last_err: Option<String> = None;
    for attempt in 0..=L0_RETRY_DELAYS_MS.len() {
        if attempt > 0 {
            let delay = Duration::from_millis(L0_RETRY_DELAYS_MS[attempt - 1]);
            tokio::time::sleep(delay).await;
        }
        match attempt_fn().await {
            AttemptOutcome::Ok(envelope) => {
                if attempt > 0 {
                    eprintln!(
                        "[module-catalog] fetch succeeded after {} retries",
                        attempt
                    );
                }
                return Ok(envelope);
            }
            AttemptOutcome::Transient(e) => {
                eprintln!(
                    "[module-catalog] attempt {} failed (transient): {}",
                    attempt + 1,
                    if e.is_empty() { "(no detail)" } else { &e }
                );
                if !e.is_empty() {
                    last_err = Some(e);
                }
            }
            AttemptOutcome::Permanent(e) => {
                // Parse errors are NOT retryable. Return immediately so the
                // caller (cache layer) does NOT overwrite cache with a bad
                // value.
                eprintln!("[module-catalog] permanent failure: {}", e);
                return Err(e);
            }
        }
    }
    Err(last_err.unwrap_or_else(|| "L0 fetch failed (no detail)".to_string()))
}

/// Pick the TTL appropriate for a given cached envelope.
///
/// Empty-module responses (publisher hasn't pushed yet, or transient
/// edge-function blank) use the short TTL so the launcher recovers
/// within ~1 minute. Populated responses use the long TTL — they
/// represent real publisher state and rarely change within 15 minutes,
/// and the `↻` refresh button in the renderer provides a manual escape
/// hatch for the rare mid-session republish case.
///
/// Pure function (no I/O, no clock); safe to call from any context.
pub(crate) fn ttl_for(envelope: &L0CatalogResponse) -> u64 {
    if envelope.modules.is_empty() {
        L0_TTL_EMPTY_SECONDS
    } else {
        L0_TTL_SECONDS
    }
}

/// Read the cached envelope if it exists AND is within TTL. Returns None
/// otherwise (caller should re-fetch).
///
/// v0.2.34: the TTL is now envelope-dependent (see [`ttl_for`]) — an
/// empty-modules cache expires after [`L0_TTL_EMPTY_SECONDS`] instead
/// of the longer [`L0_TTL_SECONDS`]. We have to load the envelope
/// BEFORE deciding freshness because the TTL depends on what's inside.
#[allow(dead_code)] // consumed by `cached_module_catalog` (whose only non-test caller lands in Agent B's L0a refactor)
fn read_cache_if_fresh(db: &Db) -> Option<L0CatalogResponse> {
    let fetched_at = read_cache_fetched_at(db)?;
    let envelope = read_cache_raw(db)?;
    let now = now_epoch_seconds();
    let ttl = ttl_for(&envelope);
    if now.saturating_sub(fetched_at) >= ttl {
        return None;
    }
    Some(envelope)
}

/// Read the cached envelope regardless of TTL. Used both by the fresh path
/// and by the stale-cache fallback.
#[allow(dead_code)] // consumed by `cached_module_catalog` (whose only non-test caller lands in Agent B's L0a refactor)
fn read_cache_raw(db: &Db) -> Option<L0CatalogResponse> {
    let raw = db.app_state_get(APP_STATE_KEY_CATALOG).ok().flatten()?;
    serde_json::from_str::<L0CatalogResponse>(&raw).ok()
}

/// Read the cache-fetched-at epoch seconds.
#[allow(dead_code)] // consumed by `read_cache_if_fresh` (whose only non-test caller lands in Agent B's L0a refactor)
fn read_cache_fetched_at(db: &Db) -> Option<u64> {
    let raw = db.app_state_get(APP_STATE_KEY_CATALOG_AT).ok().flatten()?;
    raw.parse::<u64>().ok()
}

/// Write the envelope + the fetched-at timestamp atomically (best-effort —
/// each write is its own SQL statement; if the first succeeds and the
/// second fails we'd have a value with stale timestamp, which would cause
/// the next read to consider it stale and refetch. Safe failure mode.)
fn write_cache(db: &Db, envelope: &L0CatalogResponse) -> Result<(), String> {
    let serialized =
        serde_json::to_string(envelope).map_err(|e| format!("L0 cache serialize: {}", e))?;
    db.app_state_set(APP_STATE_KEY_CATALOG, &serialized)?;
    db.app_state_set(APP_STATE_KEY_CATALOG_AT, &now_epoch_seconds().to_string())?;
    Ok(())
}

fn now_epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Cache-bust the L0 module catalog when the running launcher version
/// differs from the version that last wrote the cache.
///
/// Called once at launcher startup. Compares
/// `app_state.launcher.last_seen_version` against the running
/// `env!("CARGO_PKG_VERSION")`:
///   - no row recorded yet → record current version, NO bust (first boot
///     of any launcher; no prior cache to invalidate).
///   - same version → leave cache alone (steady-state cold boot).
///   - different version → delete `module_catalog.cache*` keys and
///     record the new version (post-update refresh).
///
/// Soft-fails: any DB error logs to stderr and proceeds — startup
/// MUST NOT block on this cleanup path. Returns the outcome for tests.
///
/// Pure-ish wrapper over [`bust_cache_if_version_changed_inner`] which
/// takes the version string as an argument; the wrapper supplies the
/// compile-time `CARGO_PKG_VERSION` so unit tests can inject arbitrary
/// versions without recompiling.
#[allow(dead_code)] // wired from `lib.rs::setup` startup hook
pub fn bust_cache_if_launcher_version_changed(db: &Db) -> VersionBustOutcome {
    bust_cache_if_version_changed_inner(db, env!("CARGO_PKG_VERSION"))
}

/// Outcome of the launcher-version-change cache-bust check. Exposed for
/// tests; production callers can ignore the return value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VersionBustOutcome {
    /// No prior version recorded; this is the first boot of any launcher
    /// version, and the version key has now been seeded. The cache (if
    /// any happens to exist from a pre-v0.2.34 launcher) is left alone
    /// — pre-v0.2.34 cache writes used the same envelope shape, so they
    /// remain valid.
    FirstBoot,
    /// Same version as last boot. Cache preserved.
    SameVersion,
    /// Different version detected. Cache wiped (rows_deleted = number of
    /// `module_catalog.cache*` rows removed). ``prev`` and ``running``
    /// carry the version strings so downstream consumers (e.g. the
    /// chunker-revision deferral hook in `chunker_revision_deferral.rs`)
    /// can react to specific upgrade-pair patterns.
    VersionChanged {
        rows_deleted: usize,
        prev: String,
        running: String,
    },
    /// A DB read failed; logged on stderr and treated as a no-op. We
    /// don't propagate the error because startup paths can't usefully
    /// recover.
    Skipped,
}

fn bust_cache_if_version_changed_inner(db: &Db, running_version: &str) -> VersionBustOutcome {
    let prior = match db.app_state_get(APP_STATE_KEY_LAUNCHER_VERSION) {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[module-catalog] version-change check: app_state_get failed: {}",
                e
            );
            return VersionBustOutcome::Skipped;
        }
    };
    match prior.as_deref() {
        None => {
            // Seed the key so the next boot can compare cleanly. Failure
            // here is non-fatal — next boot just goes through the same
            // FirstBoot branch.
            if let Err(e) = db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, running_version) {
                eprintln!(
                    "[module-catalog] version-change seed failed: {}",
                    e
                );
            }
            VersionBustOutcome::FirstBoot
        }
        Some(prev) if prev == running_version => VersionBustOutcome::SameVersion,
        Some(prev) => {
            // Version changed: nuke the cache + update the marker. Each
            // step soft-fails because the worst case is "cache survives
            // until its TTL or the user clicks ↻".
            let rows_deleted = match db.app_state_delete_like(APP_STATE_CACHE_LIKE) {
                Ok(n) => n,
                Err(e) => {
                    eprintln!(
                        "[module-catalog] version-change cache wipe failed ({} → {}): {}",
                        prev, running_version, e
                    );
                    0
                }
            };
            if let Err(e) = db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, running_version) {
                eprintln!(
                    "[module-catalog] version-change marker update failed: {}",
                    e
                );
            }
            if rows_deleted > 0 {
                eprintln!(
                    "[module-catalog] launcher version changed {} → {}; \
                     wiped {} stale cache row(s)",
                    prev, running_version, rows_deleted
                );
            }
            VersionBustOutcome::VersionChanged {
                rows_deleted,
                prev: prev.to_string(),
                running: running_version.to_string(),
            }
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────
//
// Strategy: the HTTP layer is NOT exercised here — instead we test:
//   1. `parse_response_text` directly with canonical / minimal / mismatched
//      fixtures (covers the L0 → struct contract).
//   2. The retry loop via `fetch_with_retry_async` with closure-driven mock
//      outcomes (covers retry-success, retry-exhaust, permanent-no-retry).
//   3. The cache layer via in-memory Db (covers TTL, stale fallback,
//      cache-poisoning protection).
//   4. `refresh_module_catalog` indirectly via the underlying write_cache
//      observation (the Tauri-State wrapper itself is too thin to need its
//      own test — its bypass-cache semantic is the absence of a TTL read).

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn canonical_fixture() -> &'static str {
        // Mirrors `docs/v0.2.33-l0-seed-vct-rl-reranker.json` wrapped in the
        // L0 envelope. If the seed file's shape changes, update both.
        r#"{
          "schema_version": 1,
          "fetched_at": "2026-05-24T12:34:56Z",
          "modules": [
            {
              "id": "vct-rl-reranker",
              "name": "RL Reranker",
              "version": "0.2.7",
              "description": "RL-based reranker",
              "category": "paid-independent",
              "tags": ["pro", "reranking"],
              "homepage": "https://example/rl",
              "publisher": "VibeCoded Tools",
              "license_required": true,
              "min_orchestrator_tier": "pro",
              "license_variant_ids": [],
              "trial_days": 7,
              "compatibility": {
                "hosts": ["base", "mao", "orchestrator_root"],
                "min_launcher_version": "0.2.33"
              },
              "install": {
                "method": "container_pull",
                "container": {
                  "image": "ghcr.io/hotak92/vct-rl-reranker",
                  "tag_from_version": true,
                  "registry": "ghcr.io",
                  "pull_token_endpoint": "https://example/pull-token",
                  "pull_token_method": "POST"
                }
              },
              "requirements": {
                "os": ["linux", "macos", "windows"],
                "memory_mb": 2048,
                "disk_mb": 1500,
                "gpu": false
              },
              "runtime_hints": {
                "gpu_image_variants": {
                  "cpu":  "{version}-cpu",
                  "cuda": "{version}-cuda",
                  "rocm": "{version}-rocm"
                }
              },
              "deprecated": false,
              "deprecation_message": "",
              "deprecation_eol_date": "",
              "deprecation_migration_url": "",
              "post_install_manifest_path": "vct-module.json"
            }
          ]
        }"#
    }

    fn minimal_fixture() -> &'static str {
        // Only the fields with no `#[serde(default)]` — every optional
        // field omitted. Validates that the launcher tolerates a publisher
        // who ships a stripped-down catalog entry.
        r#"{
          "schema_version": 1,
          "fetched_at": "2026-05-24T12:34:56Z",
          "modules": [
            {
              "id": "minimal-mod",
              "name": "Minimal",
              "version": "0.1.0",
              "description": "smallest legal entry",
              "category": "paid-independent",
              "tags": [],
              "license_required": false,
              "min_orchestrator_tier": "free",
              "compatibility": { "hosts": ["base"] },
              "install": {
                "method": "container_pull",
                "container": {
                  "image": "ghcr.io/example/minimal",
                  "pull_token_endpoint": "https://example/token"
                }
              }
            }
          ]
        }"#
    }

    fn open_db() -> Db {
        Db::open_in_memory().expect("in-memory db")
    }

    // ─── 1. parse_response_text — canonical / minimal / mismatch ─────────

    #[test]
    fn l0_response_parses_canonical_fixture() {
        let envelope =
            parse_response_text(canonical_fixture()).expect("canonical fixture must parse");
        assert_eq!(envelope.schema_version, 1);
        assert_eq!(envelope.fetched_at, "2026-05-24T12:34:56Z");
        assert_eq!(envelope.modules.len(), 1);
        let m = &envelope.modules[0];
        assert_eq!(m.id, "vct-rl-reranker");
        assert_eq!(m.name, "RL Reranker");
        assert_eq!(m.version, "0.2.7");
        assert!(m.license_required);
        assert_eq!(m.min_orchestrator_tier, "pro");
        assert_eq!(m.trial_days, Some(7));
        assert_eq!(m.compatibility.hosts, vec!["base", "mao", "orchestrator_root"]);
        assert_eq!(
            m.compatibility.min_launcher_version.as_deref(),
            Some("0.2.33")
        );
        assert_eq!(m.install.method, "container_pull");
        assert_eq!(m.install.container.image, "ghcr.io/hotak92/vct-rl-reranker");
        assert!(m.install.container.tag_from_version);
        assert_eq!(m.install.container.pull_token_method, "POST");
        assert_eq!(m.post_install_manifest_path, "vct-module.json");
        let reqs = m.requirements.as_ref().expect("requirements present");
        assert_eq!(reqs.memory_mb, Some(2048));
        assert!(!reqs.gpu);
        let hints = m.runtime_hints.as_ref().expect("runtime_hints present");
        assert_eq!(hints.gpu_image_variants.get("cpu").map(String::as_str), Some("{version}-cpu"));
        assert!(!m.deprecated);
    }

    #[test]
    fn l0_response_parses_minimal_module() {
        let envelope =
            parse_response_text(minimal_fixture()).expect("minimal fixture must parse");
        let m = &envelope.modules[0];
        assert_eq!(m.id, "minimal-mod");
        // All optional fields should hit their defaults.
        assert_eq!(m.homepage, "");
        assert_eq!(m.publisher, "");
        assert!(m.license_variant_ids.is_empty());
        assert!(m.trial_days.is_none());
        assert!(m.requirements.is_none());
        assert!(m.runtime_hints.is_none());
        assert!(!m.deprecated);
        // Default function for post_install_manifest_path:
        assert_eq!(m.post_install_manifest_path, "vct-module.json");
        // Default function for pull_token_method:
        assert_eq!(m.install.container.pull_token_method, "POST");
        // tag_from_version defaults to false.
        assert!(!m.install.container.tag_from_version);
    }

    #[test]
    fn l0_schema_version_mismatch_logs_warning() {
        // The future-launcher case: server advertises schema_version=2 but we
        // know v1. Parse must SUCCEED (graceful degradation), and a stderr
        // warning is emitted. We can't easily intercept stderr in a unit
        // test, but the explicit assertion is: parsing returns Ok and the
        // mismatch value survives.
        let mut buf = canonical_fixture().to_string();
        // Cheap rewrite: bump the schema_version literal.
        buf = buf.replace("\"schema_version\": 1,", "\"schema_version\": 2,");
        let envelope =
            parse_response_text(&buf).expect("v2 envelope must still parse (best-effort)");
        assert_eq!(envelope.schema_version, 2);
        assert_eq!(envelope.modules.len(), 1);
    }

    // ─── 2. fetch_with_retry_async — closure-driven mocks ────────────────

    #[tokio::test]
    async fn l0_retry_succeeds_on_third_attempt() {
        let calls = std::sync::Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry_async(move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                if n < 3 {
                    AttemptOutcome::Transient(format!("simulated transient {}", n))
                } else {
                    // Return a valid envelope.
                    AttemptOutcome::Ok(parse_response_text(canonical_fixture()).unwrap())
                }
            }
        })
        .await;
        assert!(result.is_ok(), "should succeed on third attempt");
        assert_eq!(
            calls.load(Ordering::SeqCst),
            3,
            "should call attempt exactly three times (2 fail + 1 success)"
        );
    }

    #[tokio::test]
    async fn l0_retry_fails_after_all_attempts() {
        let calls = std::sync::Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry_async(move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                AttemptOutcome::Transient(format!("permanent network failure {}", n))
            }
        })
        .await;
        assert!(result.is_err(), "should exhaust retries");
        // 5 attempts total: 1 immediate + 4 delayed retries.
        assert_eq!(
            calls.load(Ordering::SeqCst),
            5,
            "should call attempt 5 times (1 immediate + 4 retries)"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains("permanent network failure 5"),
            "error should carry the LAST attempt's message, got: {}",
            err
        );
    }

    #[tokio::test]
    async fn l0_permanent_failure_short_circuits_no_retry() {
        // A parse failure (Permanent) on attempt 1 must NOT trigger any
        // retries — the response will be malformed again, and the cache-
        // poisoning protection (test 7) depends on this behaviour.
        let calls = std::sync::Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry_async(move || {
            let calls_c = calls_c.clone();
            async move {
                calls_c.fetch_add(1, Ordering::SeqCst);
                AttemptOutcome::Permanent("JSON parse: malformed at line 4".into())
            }
        })
        .await;
        assert!(result.is_err());
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "permanent failure must NOT retry"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains("malformed at line 4"),
            "error should bubble the parse failure verbatim, got: {}",
            err
        );
    }

    // ─── 3. Cache layer — TTL / stale-fallback / poisoning protection ────

    #[test]
    fn l0_cache_returns_stale_on_fetch_failure_with_cache_present() {
        // Setup: write a cached envelope + an old timestamp so
        // `read_cache_if_fresh` would return None, BUT `read_cache_raw`
        // still finds it. The stale-fallback path inside cached_module_catalog
        // is what we're testing. Since cached_module_catalog calls the real
        // fetch (which would try to hit the network), we test the underlying
        // primitives directly: read_cache_raw on a populated cache returns
        // the cached value even with a stale timestamp.
        let db = open_db();
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).expect("cache write");
        // Force the timestamp to 1 year ago (definitely past TTL).
        let very_old = now_epoch_seconds().saturating_sub(365 * 24 * 60 * 60);
        db.app_state_set(APP_STATE_KEY_CATALOG_AT, &very_old.to_string())
            .unwrap();
        // Fresh-only read should now be None (TTL expired):
        assert!(read_cache_if_fresh(&db).is_none());
        // Raw read still finds it — this is what the stale fallback uses:
        let stale = read_cache_raw(&db).expect("stale value must survive TTL");
        assert_eq!(stale, envelope);
    }

    #[test]
    fn l0_cache_returns_err_when_fetch_fails_and_no_cache() {
        // The cache-empty case: read_cache_if_fresh and read_cache_raw both
        // return None. cached_module_catalog would then propagate the fetch
        // Err — we verify the read-side primitive returns None to prove the
        // contract used by that branch.
        let db = open_db();
        assert!(read_cache_if_fresh(&db).is_none());
        assert!(read_cache_raw(&db).is_none());
        assert!(read_cache_fetched_at(&db).is_none());
    }

    #[test]
    fn l0_cache_invalidates_on_15min_ttl() {
        // Write a cache entry then forge a timestamp at 16 minutes ago →
        // fresh read returns None, raw read still has the value.
        let db = open_db();
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).expect("cache write");
        // Fresh read should succeed immediately after a write:
        assert!(read_cache_if_fresh(&db).is_some());
        // Forge an expired timestamp:
        let sixteen_min_ago = now_epoch_seconds().saturating_sub(L0_TTL_SECONDS + 60);
        db.app_state_set(APP_STATE_KEY_CATALOG_AT, &sixteen_min_ago.to_string())
            .unwrap();
        // Now fresh-read should return None:
        assert!(read_cache_if_fresh(&db).is_none());
        // Raw read still finds the value (stale fallback works):
        assert!(read_cache_raw(&db).is_some());
    }

    // ─── 3b. v0.2.34 — empty-modules short TTL ───────────────────────────

    fn empty_envelope() -> L0CatalogResponse {
        // Same envelope shape as the canonical fixture but with no
        // modules. Mirrors what L0 returns when no publisher has
        // pushed an entry yet (or during a transient blank).
        L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".to_string(),
            modules: vec![],
        }
    }

    // Compile-time invariant: the short TTL MUST be strictly smaller
    // than the long TTL — otherwise the whole feature is a no-op. Lift
    // this out of the runtime test below so it's caught at compile
    // (clippy `assertions_on_constants` complains about runtime
    // asserts on compile-time-known values).
    const _: () = assert!(
        L0_TTL_EMPTY_SECONDS < L0_TTL_SECONDS,
        "the whole point is that empty TTL is SHORTER than populated TTL",
    );

    #[test]
    fn ttl_for_returns_short_ttl_on_empty_modules() {
        // v0.2.34 dogfood fix: empty envelope must NOT use the long
        // 15min TTL. The publication-window bug (user opens Modules
        // tab BEFORE publisher pushes their L0 entry) only resolves
        // when the empty response expires quickly.
        let empty = empty_envelope();
        assert_eq!(ttl_for(&empty), L0_TTL_EMPTY_SECONDS);
    }

    #[test]
    fn ttl_for_returns_long_ttl_on_populated_modules() {
        // Sanity: a populated response keeps the original 15min TTL.
        // The L0 happy-path should NOT regress to refetching every
        // minute on populated responses.
        let populated = parse_response_text(canonical_fixture()).unwrap();
        assert!(!populated.modules.is_empty());
        assert_eq!(ttl_for(&populated), L0_TTL_SECONDS);
    }

    #[test]
    fn l0_empty_cache_invalidates_at_60s_boundary() {
        // Write an empty-modules cache, then forge a timestamp 61s old:
        // fresh read returns None (short TTL has expired). Verifies the
        // boundary chosen by L0_TTL_EMPTY_SECONDS.
        let db = open_db();
        let empty = empty_envelope();
        write_cache(&db, &empty).expect("cache write");
        // Immediately after write: still fresh.
        assert!(read_cache_if_fresh(&db).is_some());
        // 61s old (past the 60s empty TTL):
        let sixty_one_s_ago = now_epoch_seconds().saturating_sub(L0_TTL_EMPTY_SECONDS + 1);
        db.app_state_set(APP_STATE_KEY_CATALOG_AT, &sixty_one_s_ago.to_string())
            .unwrap();
        assert!(read_cache_if_fresh(&db).is_none(),
            "empty cache must expire at 60s, not 15min");
        // Raw still finds the empty value for the stale-fallback path:
        assert!(read_cache_raw(&db).is_some());
    }

    #[test]
    fn l0_empty_cache_is_still_fresh_at_30s() {
        // Belt-and-suspenders: at 30s the empty cache is still inside
        // the 60s TTL window. We don't want to re-fetch on EVERY
        // catalog render — that would defeat the whole cache.
        let db = open_db();
        let empty = empty_envelope();
        write_cache(&db, &empty).expect("cache write");
        let thirty_s_ago = now_epoch_seconds().saturating_sub(30);
        db.app_state_set(APP_STATE_KEY_CATALOG_AT, &thirty_s_ago.to_string())
            .unwrap();
        assert!(read_cache_if_fresh(&db).is_some(),
            "empty cache at 30s old must still be served");
    }

    #[test]
    fn l0_populated_cache_is_still_fresh_at_5min() {
        // Belt-and-suspenders for the OTHER side: a populated cache
        // at 5min must stay fresh (long TTL = 15min). Asserts we
        // didn't accidentally shorten the populated TTL.
        let db = open_db();
        let populated = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &populated).expect("cache write");
        let five_min_ago = now_epoch_seconds().saturating_sub(5 * 60);
        db.app_state_set(APP_STATE_KEY_CATALOG_AT, &five_min_ago.to_string())
            .unwrap();
        assert!(read_cache_if_fresh(&db).is_some(),
            "populated cache at 5min old must still be served (long TTL)");
    }

    #[test]
    fn l0_empty_vs_populated_ttl_branching_at_2min() {
        // Crux test for the bug: at 2min old, an EMPTY cache must be
        // expired (60s TTL) but a POPULATED cache must NOT be expired
        // (15min TTL). Single forged timestamp, two different
        // envelopes — proves the branching logic.
        let two_min_ago = now_epoch_seconds().saturating_sub(120);

        // Empty side.
        let db_empty = open_db();
        write_cache(&db_empty, &empty_envelope()).unwrap();
        db_empty
            .app_state_set(APP_STATE_KEY_CATALOG_AT, &two_min_ago.to_string())
            .unwrap();
        assert!(
            read_cache_if_fresh(&db_empty).is_none(),
            "empty @ 2min must expire (60s TTL)"
        );

        // Populated side.
        let db_pop = open_db();
        let populated = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db_pop, &populated).unwrap();
        db_pop
            .app_state_set(APP_STATE_KEY_CATALOG_AT, &two_min_ago.to_string())
            .unwrap();
        assert!(
            read_cache_if_fresh(&db_pop).is_some(),
            "populated @ 2min must remain fresh (15min TTL)"
        );
    }

    // ─── 3c. v0.2.34 — launcher-version-change cache-bust ────────────────

    #[test]
    fn version_bust_first_boot_seeds_marker_without_busting_cache() {
        // Fresh DB: no prior version key. Function should seed the
        // running version + return FirstBoot. Any pre-existing cache
        // rows (would only be there if pre-v0.2.34 code path wrote
        // them) are LEFT ALONE — same envelope shape, still valid.
        let db = open_db();
        // Seed a cache so we can assert it's NOT wiped.
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).unwrap();
        assert!(read_cache_raw(&db).is_some());

        let outcome = bust_cache_if_version_changed_inner(&db, "0.2.34");
        assert_eq!(outcome, VersionBustOutcome::FirstBoot);
        // Marker now seeded:
        assert_eq!(
            db.app_state_get(APP_STATE_KEY_LAUNCHER_VERSION)
                .unwrap()
                .as_deref(),
            Some("0.2.34"),
        );
        // Cache preserved:
        assert!(read_cache_raw(&db).is_some());
    }

    #[test]
    fn version_bust_same_version_preserves_cache() {
        // Marker already matches running version → cache survives.
        // This is the steady-state case for users who don't update.
        let db = open_db();
        db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, "0.2.34")
            .unwrap();
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).unwrap();

        let outcome = bust_cache_if_version_changed_inner(&db, "0.2.34");
        assert_eq!(outcome, VersionBustOutcome::SameVersion);
        // Cache untouched:
        assert!(read_cache_raw(&db).is_some());
        assert!(read_cache_fetched_at(&db).is_some());
    }

    #[test]
    fn version_bust_different_version_wipes_cache() {
        // Marker says 0.2.33, running version is 0.2.34. Expect both
        // cache rows (envelope + fetched-at) gone, marker updated.
        let db = open_db();
        db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, "0.2.33")
            .unwrap();
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).unwrap();
        // Sanity: cache present BEFORE the bust.
        assert!(read_cache_raw(&db).is_some());
        assert!(read_cache_fetched_at(&db).is_some());

        let outcome = bust_cache_if_version_changed_inner(&db, "0.2.34");
        match outcome {
            VersionBustOutcome::VersionChanged {
                rows_deleted,
                prev,
                running,
            } => {
                assert_eq!(rows_deleted, 2, "should remove envelope + fetched-at rows");
                assert_eq!(prev, "0.2.33", "prev version from app_state");
                assert_eq!(running, "0.2.34", "running == arg to inner");
            }
            other => panic!("expected VersionChanged, got {:?}", other),
        }
        // Cache gone:
        assert!(read_cache_raw(&db).is_none());
        assert!(read_cache_fetched_at(&db).is_none());
        // Marker updated:
        assert_eq!(
            db.app_state_get(APP_STATE_KEY_LAUNCHER_VERSION)
                .unwrap()
                .as_deref(),
            Some("0.2.34"),
        );
    }

    #[test]
    fn version_bust_different_version_does_not_touch_unrelated_app_state() {
        // Negative: only `module_catalog.cache*` rows should disappear.
        // The LIKE pattern must NOT collateral-damage other keys.
        let db = open_db();
        db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, "0.2.33")
            .unwrap();
        write_cache(
            &db,
            &parse_response_text(canonical_fixture()).unwrap(),
        )
        .unwrap();
        // Plant a few unrelated keys that share a common prefix or
        // similar structure — they MUST survive.
        db.app_state_set("onboarding.complete", "true").unwrap();
        db.app_state_set("module_catalog_dev_affordance.dismissed", "true")
            .unwrap();
        db.app_state_set("module_catalog.something_else_entirely", "x")
            .unwrap();

        let _ = bust_cache_if_version_changed_inner(&db, "0.2.34");
        // Cache cleared:
        assert!(read_cache_raw(&db).is_none());
        // Unrelated rows preserved:
        assert_eq!(
            db.app_state_get("onboarding.complete").unwrap().as_deref(),
            Some("true"),
        );
        assert_eq!(
            db.app_state_get("module_catalog_dev_affordance.dismissed")
                .unwrap()
                .as_deref(),
            Some("true"),
        );
        // Note: "module_catalog.something_else_entirely" DOES match
        // the LIKE pattern by design — anything starting with
        // `module_catalog.cache` is owned by this module. The third
        // key here uses `module_catalog.` (no `cache` segment), so
        // it survives. Re-verify:
        assert_eq!(
            db.app_state_get("module_catalog.something_else_entirely")
                .unwrap()
                .as_deref(),
            Some("x"),
            "keys under module_catalog.* that don't start with cache must survive",
        );
    }

    #[test]
    fn version_bust_idempotent_on_repeated_calls_same_version() {
        // Calling the bust function twice in a row at the same
        // version is a steady-state no-op. Defensive — startup paths
        // might invoke this via several hooks in some future refactor.
        let db = open_db();
        bust_cache_if_version_changed_inner(&db, "0.2.34"); // FirstBoot, seeds marker
        let outcome = bust_cache_if_version_changed_inner(&db, "0.2.34");
        assert_eq!(outcome, VersionBustOutcome::SameVersion);
    }

    #[test]
    fn l0_cache_does_not_poison_on_malformed_response() {
        // Cache-poisoning protection: parse failures MUST NOT update the
        // cache. We assert this at the contract level by verifying that
        // `write_cache` requires a parseable envelope (it serializes a
        // struct, not a raw string) AND that the retry loop short-circuits
        // on Permanent (no Ok bubble that could trigger write_cache).
        let db = open_db();
        // Establish a known-good cached value.
        let good = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &good).unwrap();
        assert_eq!(read_cache_raw(&db), Some(good.clone()));

        // Now exercise the Permanent path: the retry loop returns Err
        // immediately, never reaches write_cache, so the previous good
        // value survives untouched.
        let calls = std::sync::Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(async move {
            fetch_with_retry_async(move || {
                let calls_c = calls_c.clone();
                async move {
                    calls_c.fetch_add(1, Ordering::SeqCst);
                    AttemptOutcome::Permanent("JSON parse: garbage".into())
                }
            })
            .await
        });
        assert!(result.is_err(), "permanent must propagate Err");
        // Cache value MUST still be the good one:
        assert_eq!(
            read_cache_raw(&db),
            Some(good),
            "permanent failure must not touch the cache"
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1, "no retries on permanent");
    }

    // ─── 4. refresh_module_catalog — bypass-cache semantics ──────────────
    //
    // The Tauri command is a thin wrapper that calls fetch_module_catalog
    // and writes the result to cache regardless of TTL. We can't easily
    // call the #[command]-wrapped version in a unit test (it requires a
    // tauri::State<Db> which only Tauri's runtime constructs), but we CAN
    // verify the underlying contract: a fresh write happens even if a
    // pre-existing cache entry would have been served by cached_module_catalog.

    #[test]
    fn refresh_writes_cache_even_when_one_is_already_present() {
        // Seed the cache with one envelope, then write a different one via
        // write_cache (which is what refresh_module_catalog calls under
        // the hood after a successful fetch). Assert the new value
        // overwrites the old one — that's the bypass-cache semantic.
        let db = open_db();
        let mut first = parse_response_text(canonical_fixture()).unwrap();
        first.fetched_at = "2026-01-01T00:00:00Z".into();
        write_cache(&db, &first).unwrap();
        assert_eq!(
            read_cache_raw(&db).unwrap().fetched_at,
            "2026-01-01T00:00:00Z"
        );

        let mut second = parse_response_text(canonical_fixture()).unwrap();
        second.fetched_at = "2026-12-31T23:59:59Z".into();
        write_cache(&db, &second).unwrap();
        assert_eq!(
            read_cache_raw(&db).unwrap().fetched_at,
            "2026-12-31T23:59:59Z",
            "refresh path must overwrite the previous cache entry"
        );
    }

    // ─── Misc — sanity on env-var override path ──────────────────────────

    #[test]
    fn resolved_endpoint_url_defaults_to_production() {
        // Don't set the env var; default branch must serve the prod URL.
        // Use a unique env-var name to avoid collisions if other tests set
        // it — but the constant is well-known, so just temporarily clear it
        // and restore at the end.
        let saved = std::env::var("VCT_MODULE_CATALOG_URL").ok();
        std::env::remove_var("VCT_MODULE_CATALOG_URL");
        let url = resolved_endpoint_url();
        assert_eq!(url, L0_DEFAULT_URL);
        if let Some(prev) = saved {
            std::env::set_var("VCT_MODULE_CATALOG_URL", prev);
        }
    }

    #[test]
    fn resolved_endpoint_url_honors_env_override() {
        let saved = std::env::var("VCT_MODULE_CATALOG_URL").ok();
        std::env::set_var(
            "VCT_MODULE_CATALOG_URL",
            "http://localhost:54321/functions/v1/module-catalog",
        );
        let url = resolved_endpoint_url();
        assert_eq!(url, "http://localhost:54321/functions/v1/module-catalog");
        match saved {
            Some(prev) => std::env::set_var("VCT_MODULE_CATALOG_URL", prev),
            None => std::env::remove_var("VCT_MODULE_CATALOG_URL"),
        }
    }

    // ─── 5. v0.2.45 V45-F — post-bust catalog-refresh contract ──────────
    //
    // V45-F adds a non-blocking refresh after
    // `bust_cache_if_launcher_version_changed` reports VersionChanged. We
    // can't unit-test the spawn from inside lib.rs::setup (it needs a
    // real Tauri AppHandle), and `cached_module_catalog` does real HTTP
    // when the cache is empty (no mock injection point at the
    // module_catalog_client surface). So we test the CONTRACT by piecing
    // together the two halves the production path stitches:
    //
    //   1. After a bust returns VersionChanged, the cache is GONE
    //      (precondition the V45-F spawn relies on — otherwise the
    //      spawn wouldn't have anything to refill).
    //   2. After write_cache fires with a fresh envelope (what
    //      cached_module_catalog calls on the success branch of its
    //      internal fetch+write), the cache IS populated again — i.e.
    //      the next on-disk-vs-L0 version compare in V45-C will see
    //      data instead of an empty-cache error.
    //
    // This is intentionally a contract test (the underlying calls are
    // already covered above); end-to-end coverage of the live spawn
    // path remains a hand-test until we add an HTTP injection seam to
    // module_catalog_client.

    #[test]
    fn test_v0245_v45f_post_bust_cache_refill_contract() {
        let db = open_db();

        // Seed prior-version marker + cached envelope so the bust path
        // produces VersionChanged (the only branch that triggers V45-F's
        // spawn in lib.rs::setup).
        db.app_state_set(APP_STATE_KEY_LAUNCHER_VERSION, "0.2.44")
            .unwrap();
        let stale = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &stale).unwrap();
        assert!(
            read_cache_raw(&db).is_some(),
            "test precondition: cache seeded"
        );

        // Step 1: launcher boots into v0.2.45 → bust fires.
        let outcome = bust_cache_if_version_changed_inner(&db, "0.2.45");
        assert!(
            matches!(outcome, VersionBustOutcome::VersionChanged { .. }),
            "bust must report VersionChanged so the V45-F spawn condition fires"
        );
        assert!(
            read_cache_raw(&db).is_none(),
            "post-bust precondition: cache is empty — V45-F's refresh has \
             something to do (otherwise resolve_manifest_for_install in \
             V45-C would silently fall back to the on-disk manifest's \
             version, exactly the bug V45-F prevents)"
        );

        // Step 2: V45-F's spawn runs cached_module_catalog. On the
        // success branch (which we can't trigger without a network mock)
        // it calls write_cache with the freshly-fetched envelope. We
        // simulate that here to assert the cache-refill side effect.
        let fresh = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &fresh).unwrap();

        // Postcondition: the next call to resolve_install_metadata (which
        // reads `app_state[module_catalog.cache]` synchronously inside
        // V45-C's resolve_manifest_for_install) will see fresh data.
        let cached = read_cache_raw(&db).expect("cache must be repopulated");
        assert_eq!(
            cached.modules.len(),
            1,
            "fresh envelope has the canonical-fixture module count"
        );
    }

    #[test]
    fn test_v0245_v45f_cached_module_catalog_is_ttl_bounded_warm_path() {
        // Part 2's pre-warm in update_module_for_project relies on
        // cached_module_catalog being a no-op when the cache is fresh —
        // otherwise every per-project update would do an unnecessary
        // HTTP fetch. This re-asserts the TTL contract from the V45-F
        // pre-warm perspective: a fresh cache must stay fresh, the
        // TTL gate is what makes the pre-warm cheap.
        let db = open_db();

        // Seed a fresh cache (within the 15-min TTL — write_cache uses
        // now_epoch_seconds() so the entry is fresh by construction).
        let envelope = parse_response_text(canonical_fixture()).unwrap();
        write_cache(&db, &envelope).unwrap();

        // read_cache_if_fresh is the gate cached_module_catalog uses to
        // short-circuit fetch when the cache is fresh.
        let fresh = read_cache_if_fresh(&db);
        assert!(
            fresh.is_some(),
            "freshly-written cache must satisfy read_cache_if_fresh — V45-F's \
             pre-warm in update_module_for_project depends on this being a \
             no-op for warm caches (otherwise every update path would HTTP-fetch)"
        );
        assert_eq!(
            fresh.unwrap().fetched_at, envelope.fetched_at,
            "the freshly-cached envelope is returned unchanged"
        );
    }
}
