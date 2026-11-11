// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The hub owns how long Ollama keeps a model loaded (v0.2.106, owner ruling
//! 2026-11-11: "the hub manages unloading").
//!
//! ## Mechanism
//!
//! * **Callers do not own the timer.** Every VCO request to Ollama carries the
//!   same `keep_alive` (`caller_keep_alive` in `vco_lib/ollama_model_lifetime.toml`,
//!   45 min) — longer than any idle time the hub accepts (30 min at most), so
//!   Ollama never unloads first while the hub runs, and short enough that a
//!   model still unloads on its own when the hub is down (fail-safe).
//! * **The hub decides.** Every few seconds (a quarter of the idle time,
//!   5–30 s) this task reads Ollama's `GET /api/ps`. Ollama sets a model's
//!   `expires_at` to "end of the last request + that request's keep_alive"
//!   each time a request finishes (observed read-only on this machine:
//!   `expires_at` ≈ now + the 10 m the callers sent seconds before), so a
//!   change of `expires_at` between two reads means the model was used, and
//!   `expires_at − caller_keep_alive` is the moment it was last used. When
//!   that is longer ago than the idle time from Preferences
//!   (`app_state[ollama.idle_unload]`, 10 min when unset), the hub unloads the
//!   model the way `ollama stop` does — `POST /api/generate` with the model
//!   and `keep_alive: 0`, no prompt — and logs it.
//! * **A model someone else pinned is left alone.** When the `expires_at` a
//!   request left behind does not match the callers' keep_alive, the request
//!   carried another value: a user's own `VCO_OLLAMA_KEEP_ALIVE`, or a client
//!   that is not VCO. That model is shown as held by its caller and Ollama's
//!   own timer applies — the explicit override wins over the hub.
//! * **Reading never loads.** `/api/ps` lists what is resident; it loads
//!   nothing. The service probes (`/api/tags`,
//!   `vct_launcher_core::services::service_status::health_path`) do not load
//!   a model either.
//!
//! What it cannot see: a single request that runs longer than the idle time
//! (Ollama refreshes `expires_at` when a request finishes). Unloading then
//! does not break the request — Ollama unloads the model after it finishes —
//! but the next request reloads it (about 2 s). A model whose `expires_at`
//! has already passed while it is still listed is treated as busy, never
//! idle.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use vct_launcher_core::services::ollama_lifetime as lt;
use vct_launcher_core::services::service_endpoints;

use super::modules_api::LauncherDbHandle;
use crate::http_error::error_response;

/// Tolerance when matching an `expires_at` against the callers' keep_alive
/// (clock skew between host and container, request duration).
pub const CLASSIFY_SLACK_MS: i64 = 120_000;
/// An `expires_at` this far in the past, on a model still listed, means a
/// request is still running (Ollama refreshes it only when one finishes).
pub const BUSY_PAST_MS: i64 = 5_000;
/// After asking Ollama to unload a model, wait this long before asking again.
pub const UNLOAD_RETRY_MS: i64 = 60_000;
pub const MIN_POLL_SECS: u64 = 5;
pub const MAX_POLL_SECS: u64 = 30;
const RECENT_UNLOADS_CAP: usize = 20;
const PS_TIMEOUT: Duration = Duration::from_secs(5);
const UNLOAD_TIMEOUT: Duration = Duration::from_secs(30);

/// How often the hub reads `/api/ps` for a given idle time.
pub fn poll_secs(idle_secs: u64) -> u64 {
    (idle_secs / 4).clamp(MIN_POLL_SECS, MAX_POLL_SECS)
}

// ─── Ollama wire shapes ─────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct PsModel {
    pub name: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub size_vram: u64,
    #[serde(default)]
    pub expires_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PsResponse {
    #[serde(default)]
    models: Vec<PsModel>,
}

fn parse_ms(rfc3339: Option<&str>) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(rfc3339?).ok().map(|d| d.timestamp_millis())
}

fn iso(ms: i64) -> Option<String> {
    chrono::DateTime::from_timestamp_millis(ms)
        .map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
}

pub fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

// ─── The pure decision ──────────────────────────────────────────────────

/// Who decides when a loaded model goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Holder {
    /// Loaded by VCO's callers with the shared keep_alive: the hub unloads it
    /// after the idle time.
    Hub,
    /// The last request carried another keep_alive (`VCO_OLLAMA_KEEP_ALIVE`
    /// or a non-VCO client): Ollama's own timer applies; the hub leaves it.
    Caller,
    /// Still listed after its `expires_at` passed: a request is running.
    Busy,
}

#[derive(Debug, Clone)]
struct Track {
    expires_at_ms: Option<i64>,
    expires_at_raw: Option<String>,
    last_use_ms: i64,
    holder: Holder,
    unload_requested_ms: Option<i64>,
    size: u64,
    size_vram: u64,
}

/// What the Services card shows for one loaded model.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelView {
    pub name: String,
    pub size: u64,
    pub size_vram: u64,
    pub holder: Holder,
    /// Ollama's own expiry for the model (its keep_alive timer).
    pub ollama_expires_at: Option<String>,
    /// When a VCO request last used it, as far as the hub can tell.
    pub last_use: Option<String>,
    /// When the hub will unload it (`holder == hub` only).
    pub unload_at: Option<String>,
    pub unload_requested: bool,
}

#[derive(Debug, Default)]
pub struct Tracker {
    tracks: HashMap<String, Track>,
}

impl Tracker {
    /// Fold one `/api/ps` reading in. `poll_ms` bounds how long ago a
    /// request seen as a changed `expires_at` can have finished.
    pub fn observe(&mut self, models: &[PsModel], now: i64, caller_ka_ms: i64, poll_ms: i64) {
        self.tracks.retain(|name, _| models.iter().any(|m| &m.name == name));
        for m in models {
            let exp = parse_ms(m.expires_at.as_deref());
            let busy = exp.map(|e| e < now - BUSY_PAST_MS).unwrap_or(false);
            match self.tracks.get_mut(&m.name) {
                None => {
                    // First sight (a new load, or the hub just started): the
                    // last use is not knowable, so count it as now — the hub
                    // never unloads a model sooner than the idle time after
                    // it first sees it.
                    let holder = if busy {
                        Holder::Busy
                    } else {
                        match exp {
                            Some(e) if e - now <= caller_ka_ms + CLASSIFY_SLACK_MS => Holder::Hub,
                            _ => Holder::Caller,
                        }
                    };
                    self.tracks.insert(
                        m.name.clone(),
                        Track {
                            expires_at_ms: exp,
                            expires_at_raw: m.expires_at.clone(),
                            last_use_ms: now,
                            holder,
                            unload_requested_ms: None,
                            size: m.size,
                            size_vram: m.size_vram,
                        },
                    );
                }
                Some(t) => {
                    t.size = m.size;
                    t.size_vram = m.size_vram;
                    let changed = match (t.expires_at_ms, exp) {
                        (Some(a), Some(b)) => (a - b).abs() > 1_000,
                        (a, b) => a.is_some() != b.is_some(),
                    };
                    t.expires_at_raw = m.expires_at.clone();
                    t.expires_at_ms = exp;
                    if busy {
                        t.holder = Holder::Busy;
                        t.last_use_ms = now;
                        continue;
                    }
                    if !changed {
                        if t.holder == Holder::Busy {
                            // Was running, expiry unchanged: count it as just used.
                            t.holder = Holder::Hub;
                            t.last_use_ms = now;
                        }
                        continue;
                    }
                    // A request finished since the last reading.
                    t.unload_requested_ms = None;
                    match exp {
                        Some(e) => {
                            let remaining = e - now;
                            let hub_shaped = remaining <= caller_ka_ms + CLASSIFY_SLACK_MS
                                && remaining >= caller_ka_ms - poll_ms - CLASSIFY_SLACK_MS;
                            if hub_shaped {
                                t.holder = Holder::Hub;
                                t.last_use_ms = (e - caller_ka_ms).min(now);
                            } else {
                                t.holder = Holder::Caller;
                                t.last_use_ms = now;
                            }
                        }
                        None => {
                            t.holder = Holder::Caller;
                            t.last_use_ms = now;
                        }
                    }
                }
            }
        }
    }

    /// Models the hub should unload now.
    pub fn due(&self, now: i64, idle_ms: i64) -> Vec<String> {
        let mut out: Vec<String> = self
            .tracks
            .iter()
            .filter(|(_, t)| {
                t.holder == Holder::Hub
                    && now - t.last_use_ms >= idle_ms
                    && t.unload_requested_ms.map(|r| now - r >= UNLOAD_RETRY_MS).unwrap_or(true)
            })
            .map(|(n, _)| n.clone())
            .collect();
        out.sort();
        out
    }

    pub fn mark_unload_requested(&mut self, name: &str, now: i64) {
        if let Some(t) = self.tracks.get_mut(name) {
            t.unload_requested_ms = Some(now);
        }
    }

    pub fn views(&self, idle_ms: i64) -> Vec<ModelView> {
        let mut out: Vec<ModelView> = self
            .tracks
            .iter()
            .map(|(name, t)| ModelView {
                name: name.clone(),
                size: t.size,
                size_vram: t.size_vram,
                holder: t.holder,
                ollama_expires_at: t.expires_at_raw.clone(),
                last_use: iso(t.last_use_ms),
                unload_at: (t.holder == Holder::Hub).then(|| iso(t.last_use_ms + idle_ms)).flatten(),
                unload_requested: t.unload_requested_ms.is_some(),
            })
            .collect();
        out.sort_by(|a, b| a.name.cmp(&b.name));
        out
    }
}

// ─── Shared state + I/O ─────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct UnloadEvent {
    pub model: String,
    pub at: Option<String>,
    /// `"idle"` (the hub's timer) or `"manual"` (Services → Unload now).
    pub reason: String,
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Debug, Default)]
pub struct LifetimeState {
    tracker: Tracker,
    reachable: Option<bool>,
    last_error: Option<String>,
    last_poll_ms: Option<i64>,
    recent_unloads: VecDeque<UnloadEvent>,
}

impl LifetimeState {
    fn record(&mut self, ev: UnloadEvent) {
        self.recent_unloads.push_front(ev);
        self.recent_unloads.truncate(RECENT_UNLOADS_CAP);
    }
}

#[derive(Clone, Default)]
pub struct LifetimeHandle(pub Arc<Mutex<LifetimeState>>);

impl LifetimeHandle {
    fn lock(&self) -> std::sync::MutexGuard<'_, LifetimeState> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }
}

/// Router state: the launcher DB (idle time, Ollama URL) and the tracker.
#[derive(Clone)]
pub struct OllamaLifetimeApi {
    pub db: LauncherDbHandle,
    pub life: LifetimeHandle,
    /// `None` in production: the Ollama URL is this machine's
    /// `service_endpoints` row. Tests pin a fake server here.
    pub ollama_url: Option<String>,
}

impl OllamaLifetimeApi {
    pub fn new(db: LauncherDbHandle) -> Self {
        Self { db, life: LifetimeHandle::default(), ollama_url: None }
    }

    fn base_url(&self) -> String {
        self.ollama_url
            .clone()
            .unwrap_or_else(|| service_endpoints::machine_ollama_url(&self.db.0))
            .trim_end_matches('/')
            .to_string()
    }

    fn idle(&self) -> lt::IdleSetting {
        lt::idle_from_db(&self.db.0)
    }
}

pub async fn fetch_ps(base: &str) -> Result<Vec<PsModel>, String> {
    let url = format!("{base}/api/ps");
    let client = vct_launcher_core::services::loopback_http::client_for(&url, PS_TIMEOUT)?;
    let resp = client.get(&url).send().await.map_err(|e| format!("GET {url}: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("GET {url}: HTTP {}", resp.status().as_u16()));
    }
    resp.json::<PsResponse>()
        .await
        .map(|r| r.models)
        .map_err(|e| format!("GET {url}: unreadable reply: {e}"))
}

/// Unload `model` the way `ollama stop` does: a generate request with no
/// prompt and `keep_alive: 0`. Loads nothing (Ollama answers it before it
/// schedules a runner) and works for embedding models too.
pub async fn send_unload(base: &str, model: &str) -> Result<(), String> {
    let url = format!("{base}/api/generate");
    let client = vct_launcher_core::services::loopback_http::client_for(&url, UNLOAD_TIMEOUT)?;
    let resp = client
        .post(&url)
        .json(&serde_json::json!({ "model": model, "keep_alive": 0 }))
        .send()
        .await
        .map_err(|e| format!("POST {url}: {e}"))?;
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("POST {url}: HTTP {code} {}", body.trim()));
    }
    Ok(())
}

/// One reading + the unloads it makes due. Returns the poll interval to
/// sleep before the next one. Never fails: an unreachable Ollama is recorded
/// and retried next tick.
pub async fn tick(api: &OllamaLifetimeApi, now: i64) -> u64 {
    let idle = api.idle();
    let poll = poll_secs(idle.secs);
    let caller_ka_ms = (lt::table().caller_keep_alive_secs * 1000) as i64;
    let idle_ms = (idle.secs * 1000) as i64;
    let base = api.base_url();
    let models = match fetch_ps(&base).await {
        Ok(m) => m,
        Err(e) => {
            let mut st = api.life.lock();
            if st.reachable != Some(false) {
                tracing::debug!("[vct-hub] ollama lifetime: Ollama not reachable ({e}); retrying");
            }
            st.reachable = Some(false);
            st.last_error = Some(e);
            st.last_poll_ms = Some(now);
            return poll;
        }
    };
    let due = {
        let mut st = api.life.lock();
        st.reachable = Some(true);
        st.last_error = None;
        st.last_poll_ms = Some(now);
        st.tracker.observe(&models, now, caller_ka_ms, (poll * 1000) as i64);
        let due = st.tracker.due(now, idle_ms);
        for name in &due {
            st.tracker.mark_unload_requested(name, now);
        }
        due
    };
    for model in due {
        let result = send_unload(&base, &model).await;
        match &result {
            Ok(()) => tracing::info!(
                model = %model,
                idle = %lt::format_secs(idle.secs),
                "[vct-hub] ollama lifetime: unloaded {model} — no VCO request used it for {} \
                 (Preferences → Ollama model unloading)",
                lt::format_secs(idle.secs)
            ),
            Err(e) => tracing::warn!(
                model = %model,
                "[vct-hub] ollama lifetime: could not unload {model}: {e}; retrying in {}s",
                UNLOAD_RETRY_MS / 1000
            ),
        }
        api.life.lock().record(UnloadEvent {
            model,
            at: iso(now),
            reason: "idle".into(),
            ok: result.is_ok(),
            error: result.err(),
        });
    }
    poll
}

/// Start the unloader. Runs for the hub's lifetime; every failure is soft.
pub fn spawn_ollama_lifetime(api: OllamaLifetimeApi) {
    tracing::info!(
        caller_keep_alive = %lt::format_secs(lt::table().caller_keep_alive_secs),
        "[vct-hub] ollama lifetime: the hub unloads idle Ollama models (idle time: \
         Preferences → Ollama model unloading)"
    );
    tokio::spawn(async move {
        loop {
            let poll = tick(&api, now_ms()).await;
            tokio::time::sleep(Duration::from_secs(poll)).await;
        }
    });
}

// ─── Routes ─────────────────────────────────────────────────────────────

pub fn router() -> Router<OllamaLifetimeApi> {
    Router::new()
        .route("/ollama/models", get(get_models))
        .route("/ollama/models/unload", post(post_unload))
}

#[derive(Debug, Serialize)]
pub struct ModelsStatus {
    pub reachable: bool,
    pub error: Option<String>,
    pub idle_secs: u64,
    pub idle: String,
    pub idle_stored_invalid: bool,
    pub caller_keep_alive: String,
    pub poll_secs: u64,
    pub models: Vec<ModelView>,
    pub recent_unloads: Vec<UnloadEvent>,
}

async fn get_models(State(api): State<OllamaLifetimeApi>) -> impl IntoResponse {
    let idle = api.idle();
    let poll = poll_secs(idle.secs);
    let now = now_ms();
    // A fresh reading, folded into the same tracker the unloader uses, so
    // the card never shows a stale list. Reading /api/ps loads nothing.
    let fetched = fetch_ps(&api.base_url()).await;
    let mut st = api.life.lock();
    let (reachable, error) = match fetched {
        Ok(models) => {
            st.tracker.observe(
                &models,
                now,
                (lt::table().caller_keep_alive_secs * 1000) as i64,
                (poll * 1000) as i64,
            );
            (true, None)
        }
        Err(e) => (false, Some(e)),
    };
    Json(ModelsStatus {
        reachable,
        error,
        idle_secs: idle.secs,
        idle: lt::format_secs(idle.secs),
        idle_stored_invalid: idle.stored_invalid,
        caller_keep_alive: lt::format_secs(lt::table().caller_keep_alive_secs),
        poll_secs: poll,
        models: if reachable { st.tracker.views((idle.secs * 1000) as i64) } else { Vec::new() },
        recent_unloads: st.recent_unloads.iter().cloned().collect(),
    })
}

#[derive(Debug, Deserialize)]
struct UnloadRequest {
    model: String,
}

async fn post_unload(
    State(api): State<OllamaLifetimeApi>,
    Json(req): Json<UnloadRequest>,
) -> impl IntoResponse {
    let model = req.model.trim().to_string();
    if model.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "model_required", "name the model to unload");
    }
    let now = now_ms();
    api.life.lock().tracker.mark_unload_requested(&model, now);
    let result = send_unload(&api.base_url(), &model).await;
    match &result {
        Ok(()) => tracing::info!(model = %model, "[vct-hub] ollama lifetime: unloaded {model} on request (Services → Unload now)"),
        Err(e) => tracing::warn!(model = %model, "[vct-hub] ollama lifetime: Unload now failed for {model}: {e}"),
    }
    api.life.lock().record(UnloadEvent {
        model: model.clone(),
        at: iso(now),
        reason: "manual".into(),
        ok: result.is_ok(),
        error: result.as_ref().err().cloned(),
    });
    match result {
        Ok(()) => Json(serde_json::json!({ "ok": true, "model": model })).into_response(),
        Err(e) => error_response(
            StatusCode::BAD_GATEWAY,
            "ollama_unload_failed",
            format!("Ollama did not unload {model}: {e}"),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::{get as aget, post as apost};

    const K: i64 = 45 * 60 * 1000;
    const POLL: i64 = 30_000;
    const MIN: i64 = 60_000;

    fn ps(name: &str, expires_ms: i64) -> PsModel {
        PsModel { name: name.into(), size: 1_000, size_vram: 500, expires_at: iso_ms(expires_ms) }
    }

    fn iso_ms(ms: i64) -> Option<String> {
        chrono::DateTime::from_timestamp_millis(ms)
            .map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))
    }

    #[test]
    fn poll_interval_follows_the_idle_time() {
        assert_eq!(poll_secs(0), MIN_POLL_SECS);
        assert_eq!(poll_secs(120), 30);
        assert_eq!(poll_secs(60), 15);
        assert_eq!(poll_secs(1800), MAX_POLL_SECS);
    }

    /// The core claim: a VCO-loaded model is unloaded after the idle time,
    /// not before.
    #[test]
    fn unloads_after_idle_not_before() {
        let idle = 10 * MIN;
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        t.observe(&[ps("arctic", t0 + K)], t0, K, POLL); // first sight
        t.observe(&[ps("arctic", t0 + 5 * MIN + K)], t0 + 5 * MIN + 10_000, K, POLL); // used at t0+5m
        assert!(t.due(t0 + 14 * MIN, idle).is_empty(), "9 min idle is not 10");
        assert_eq!(t.due(t0 + 15 * MIN + 1_000, idle), vec!["arctic".to_string()]);
        let v = &t.views(idle)[0];
        assert_eq!(v.holder, Holder::Hub);
        assert_eq!(v.last_use, iso(t0 + 5 * MIN));
    }

    /// A model in use keeps being refreshed and is never due.
    #[test]
    fn keeps_a_model_in_use() {
        let idle = 2 * MIN;
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        for i in 0..20 {
            let now = t0 + i * POLL;
            t.observe(&[ps("qwen3", now - 1_000 + K)], now, K, POLL);
            assert!(t.due(now, idle).is_empty(), "used every 30 s, never idle (step {i})");
        }
    }

    /// First sight (hub restart) never unloads before a full idle time.
    #[test]
    fn first_sight_counts_as_now() {
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        // Last used 40 min ago by the expiry, but the hub just started.
        t.observe(&[ps("arctic", t0 + 5 * MIN)], t0, K, POLL);
        assert!(t.due(t0 + 9 * MIN, 10 * MIN).is_empty());
        assert_eq!(t.due(t0 + 10 * MIN, 10 * MIN).len(), 1);
    }

    /// A user's own VCO_OLLAMA_KEEP_ALIVE (or another client) wins: a model
    /// whose expiry does not match the callers' keep_alive is left alone.
    #[test]
    fn a_caller_pinned_model_is_left_alone() {
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        t.observe(&[ps("arctic", t0 + K)], t0, K, POLL);
        // VCO_OLLAMA_KEEP_ALIVE=2h on the next request.
        t.observe(&[ps("arctic", t0 + MIN + 120 * MIN)], t0 + MIN, K, POLL);
        assert!(t.due(t0 + 100 * MIN, 0).is_empty());
        // ...and =-1 (never): Ollama reports a far-future expiry.
        let mut t2 = Tracker::default();
        t2.observe(&[PsModel { expires_at: Some("2318-02-23T00:00:00Z".into()), ..ps("m", 0) }], t0, K, POLL);
        assert_eq!(t2.views(0)[0].holder, Holder::Caller);
        assert!(t2.due(t0 + 1000 * MIN, 0).is_empty());
        // ...and a shorter one (2m): Ollama's timer, not the hub's.
        t.observe(&[ps("arctic", t0 + 3 * MIN + 2 * MIN)], t0 + 3 * MIN, K, POLL);
        assert_eq!(t.views(0)[0].holder, Holder::Caller);
    }

    /// A model still listed after its expiry is running a request: busy.
    #[test]
    fn a_running_request_is_never_idle() {
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        t.observe(&[ps("big", t0 - MIN)], t0, K, POLL);
        assert_eq!(t.views(0)[0].holder, Holder::Busy);
        assert!(t.due(t0 + 100 * MIN, 0).is_empty());
    }

    #[test]
    fn a_failed_unload_is_retried_after_a_minute() {
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        t.observe(&[ps("m", t0 + K)], t0, K, POLL);
        t.mark_unload_requested("m", t0 + 10 * MIN);
        assert!(t.due(t0 + 10 * MIN + 30_000, 10 * MIN).is_empty());
        assert_eq!(t.due(t0 + 11 * MIN, 10 * MIN).len(), 1);
    }

    #[test]
    fn a_model_gone_from_ps_is_forgotten() {
        let mut t = Tracker::default();
        let t0 = 1_800_000_000_000;
        t.observe(&[ps("m", t0 + K)], t0, K, POLL);
        t.observe(&[], t0 + POLL, K, POLL);
        assert!(t.views(0).is_empty());
    }

    // ── Against a fake Ollama ──────────────────────────────────────────

    #[derive(Clone, Default)]
    struct Fake {
        models: Arc<Mutex<Vec<PsModel>>>,
        unloads: Arc<Mutex<Vec<serde_json::Value>>>,
        fail_unload: Arc<Mutex<bool>>,
    }

    async fn spawn_fake(fake: Fake) -> String {
        let f1 = fake.clone();
        let f2 = fake.clone();
        let app = Router::new()
            .route(
                "/api/ps",
                aget(move || {
                    let f = f1.clone();
                    async move { Json(serde_json::json!({ "models": *f.models.lock().unwrap() })) }
                }),
            )
            .route(
                "/api/generate",
                apost(move |Json(body): Json<serde_json::Value>| {
                    let f = f2.clone();
                    async move {
                        if *f.fail_unload.lock().unwrap() {
                            return (StatusCode::INTERNAL_SERVER_ERROR, "boom").into_response();
                        }
                        let name = body["model"].as_str().unwrap_or("").to_string();
                        f.unloads.lock().unwrap().push(body);
                        f.models.lock().unwrap().retain(|m| m.name != name);
                        Json(serde_json::json!({ "done": true, "done_reason": "unload" })).into_response()
                    }
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}")
    }

    fn api_for(base: &str) -> OllamaLifetimeApi {
        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        OllamaLifetimeApi {
            db: LauncherDbHandle(Arc::new(db)),
            life: LifetimeHandle::default(),
            ollama_url: Some(base.to_string()),
        }
    }

    /// End to end against a fake Ollama: the hub reads /api/ps, unloads the
    /// idle model with keep_alive 0, and leaves the model in use loaded.
    #[tokio::test]
    async fn tick_unloads_the_idle_model_and_keeps_the_used_one() {
        let fake = Fake::default();
        let base = spawn_fake(fake.clone()).await;
        let api = api_for(&base);
        api.db.0.app_state_set(lt::IDLE_APP_STATE_KEY, "2m").unwrap();
        let t0 = now_ms();
        *fake.models.lock().unwrap() = vec![ps("idle-model", t0 + K), ps("busy-model", t0 + K)];
        assert_eq!(tick(&api, t0).await, 30, "2 min idle → 30 s poll");
        assert!(fake.unloads.lock().unwrap().is_empty(), "first sight never unloads");

        // 3 minutes on: busy-model was used a second ago, idle-model not.
        let t1 = t0 + 3 * MIN;
        *fake.models.lock().unwrap() = vec![ps("idle-model", t0 + K), ps("busy-model", t1 - 1_000 + K)];
        tick(&api, t1).await;
        let unloads = fake.unloads.lock().unwrap().clone();
        assert_eq!(unloads, vec![serde_json::json!({ "model": "idle-model", "keep_alive": 0 })]);
        let names: Vec<String> = fake.models.lock().unwrap().iter().map(|m| m.name.clone()).collect();
        assert_eq!(names, vec!["busy-model".to_string()]);
        let ev = &api.life.lock().recent_unloads[0];
        assert_eq!((ev.model.as_str(), ev.reason.as_str(), ev.ok), ("idle-model", "idle", true));
    }

    /// Unset preference = 10 minutes, exactly the pre-v0.2.106 behaviour.
    #[tokio::test]
    async fn unset_preference_means_ten_minutes() {
        let fake = Fake::default();
        let base = spawn_fake(fake.clone()).await;
        let api = api_for(&base);
        let t0 = now_ms();
        *fake.models.lock().unwrap() = vec![ps("m", t0 + K)];
        tick(&api, t0).await;
        tick(&api, t0 + 9 * MIN).await;
        assert!(fake.unloads.lock().unwrap().is_empty());
        tick(&api, t0 + 10 * MIN).await;
        assert_eq!(fake.unloads.lock().unwrap().len(), 1);
    }

    /// Fail-safe: Ollama down → nothing breaks, nothing is unloaded, the
    /// status says unreachable. (With the hub down entirely, the callers'
    /// 45-minute keep_alive expires on its own — pinned in the Python suite.)
    #[tokio::test]
    async fn an_unreachable_ollama_is_reported_not_fatal() {
        let api = api_for("http://127.0.0.1:9");
        assert_eq!(tick(&api, now_ms()).await, poll_secs(600));
        let st = api.life.lock();
        assert_eq!(st.reachable, Some(false));
        assert!(st.last_error.is_some());
    }

    #[tokio::test]
    async fn a_failed_unload_is_logged_and_retried() {
        let fake = Fake::default();
        *fake.fail_unload.lock().unwrap() = true;
        let base = spawn_fake(fake.clone()).await;
        let api = api_for(&base);
        api.db.0.app_state_set(lt::IDLE_APP_STATE_KEY, "0").unwrap();
        let t0 = now_ms();
        *fake.models.lock().unwrap() = vec![ps("m", t0 + K)];
        tick(&api, t0).await; // first sight; idle 0 → due at once
        let ev = api.life.lock().recent_unloads.front().cloned().unwrap();
        assert!(!ev.ok && ev.error.is_some());
        tick(&api, t0 + 10_000).await;
        assert_eq!(api.life.lock().recent_unloads.len(), 1, "no retry within a minute");
        tick(&api, t0 + 61_000).await;
        assert_eq!(api.life.lock().recent_unloads.len(), 2, "retried after a minute");
    }

    /// The routes: the status lists the loaded models with the hub's plan,
    /// and Unload now unloads through Ollama.
    #[tokio::test]
    async fn routes_report_and_unload() {
        let fake = Fake::default();
        let base = spawn_fake(fake.clone()).await;
        let api = api_for(&base);
        *fake.models.lock().unwrap() = vec![ps("arctic", now_ms() + K)];
        let app = router().with_state(api.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = reqwest::Client::new();

        let status: serde_json::Value = client
            .get(format!("http://{addr}/ollama/models"))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(status["reachable"], true);
        assert_eq!(status["idle"], "10m");
        assert_eq!(status["caller_keep_alive"], "45m");
        assert_eq!(status["models"][0]["name"], "arctic");
        assert_eq!(status["models"][0]["holder"], "hub");
        assert!(status["models"][0]["unload_at"].is_string());

        let resp = client
            .post(format!("http://{addr}/ollama/models/unload"))
            .json(&serde_json::json!({ "model": "arctic" }))
            .send()
            .await
            .unwrap();
        assert!(resp.status().is_success());
        assert_eq!(
            fake.unloads.lock().unwrap().clone(),
            vec![serde_json::json!({ "model": "arctic", "keep_alive": 0 })]
        );
        assert_eq!(api.life.lock().recent_unloads[0].reason, "manual");

        let bad = client
            .post(format!("http://{addr}/ollama/models/unload"))
            .json(&serde_json::json!({ "model": "  " }))
            .send()
            .await
            .unwrap();
        assert_eq!(bad.status().as_u16(), 400);
    }

    /// The hub's service probe for Ollama must not load a model.
    #[test]
    fn the_ollama_health_probe_does_not_load_a_model() {
        use vct_launcher_core::services::service_endpoints::CoreService;
        let path = vct_launcher_core::services::service_status::health_path(CoreService::Ollama);
        assert!(
            !["/api/generate", "/api/embed", "/api/embeddings", "/api/chat"].contains(&path),
            "{path} would load a model just to probe"
        );
    }
}
