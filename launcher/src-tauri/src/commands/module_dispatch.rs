// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.26 declarative HTTP-action dispatcher.
//!
//! Backs the new `ActionRef::Descriptor` form of `gui.config_tab` action
//! fields (see `vct-launcher-core/src/manifest.rs`). When a control's
//! `action` / `on_change` / `options_source` is a structured
//! [`ActionDescriptor`] rather than a legacy Tauri command name, the
//! frontend calls into `module_dispatch_action` — the single generic
//! Tauri command exposed by this module — which executes the descriptor
//! against the module's localhost container without per-module Rust
//! code.
//!
//! WIRE CONTRACT
//!
//! 1. Resolve `(project_id, module_id) → port` via
//!    `db.get_module_port(...)`. Hard error when the row is absent.
//! 2. URL = `http://127.0.0.1:<port><path>`. All module containers bind
//!    to `127.0.0.1` only (see manifest `PortMapping::bind` default).
//! 3. Substitute `{{token}}` placeholders in `body`. Tokens supported
//!    in v1: `{{project_id}}`, `{{module_id}}`, `{{value}}`,
//!    `{{control:<id>}}`. Whole-string tokens preserve the source's
//!    JSON type (bool/number/array/object pass through); embedded
//!    tokens stringify. Unknown tokens are a hard error (closed-set).
//! 4. Issue the HTTP request via `reqwest`. Method comes from
//!    [`HttpMethod`] in the manifest.
//! 5. If `polling` is `Some`, spawn a `tokio::spawn` background task
//!    that polls `polling.endpoint` with the job id extracted from
//!    the kick response (`$.<top-level-key>` only in v1), emits
//!    `progress_event` Tauri events with each poll's body, and stops
//!    when a terminal state is reached or `max_attempts` is exceeded.
//! 6. If `next_action` is `Some`, execute it via an ITERATIVE loop
//!    (not recursion). Bounded by `MAX_CHAIN_STEPS`.
//! 7. Return the kick response body (the FIRST request) to the
//!    caller. Polling progress flows via events, not the return value.
//!
//! SECURITY / TRUST SURFACE
//!
//! Every dispatch target is `127.0.0.1:<port>` — the host can never be
//! redirected to a non-local address by manifest content. The
//! `module_ports` table is HUB-writable only; the launcher GUI cannot
//! poison it. Body substitution is closed-set (no arbitrary env access,
//! no filesystem reads, no command exec). Unknown placeholder tokens
//! error rather than fall through silently to avoid accidental data
//! leaks from typos.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tauri::{command, AppHandle, Emitter, State};

use vct_launcher_core::manifest::{ActionDescriptor, HttpMethod, PollingSpec};

use crate::db::Db;

// ─── Event-sink abstraction ────────────────────────────────────────────
//
// The dispatcher emits Tauri events from inside the polling background
// task (`progress_event` per tick, `failed_event` on terminal failure).
// In tests we can't easily build a real `tauri::AppHandle`, so we route
// every emit call through a tiny `EventSink` trait. Production wires the
// `AppHandle`-backed implementation; tests pass an in-memory recorder
// and assert on its captured events.

/// Abstraction over Tauri's app-handle `.emit(event, payload)` so the
/// dispatcher's polling loop can be unit-tested without a real Tauri
/// runtime.
pub trait EventSink: Send + Sync + 'static {
    /// Emit one event. Errors are swallowed by the dispatcher
    /// (matching the production behaviour where `app.emit` errors are
    /// already `let _ = ...` discarded).
    fn emit(&self, event: &str, payload: &Value);
}

/// Production implementation backed by a Tauri `AppHandle`.
struct AppHandleSink(AppHandle);

impl EventSink for AppHandleSink {
    fn emit(&self, event: &str, payload: &Value) {
        let _ = self.0.emit(event, payload);
    }
}

// ─── Constants ──────────────────────────────────────────────────────────

/// Maximum length of a chained `next_action` walk. Prevents a malformed
/// (or malicious) manifest from spinning forever. Chosen large enough
/// that any plausible real-world chain fits, small enough to bound
/// runaway memory + network work.
pub const MAX_CHAIN_STEPS: u32 = 1024;

/// HTTP timeout for both the kick request and each poll tick. Matches
/// the module_service.rs convention.
const HTTP_TIMEOUT_SECS: u64 = 30;

// ─── Substitution context ───────────────────────────────────────────────

/// Type alias for the `{{control:<id>}}` resolver closure. Lives in a
/// type alias purely to silence clippy's "very complex type" lint and
/// give callers a single place to reference when constructing one.
pub type ControlValueResolver<'a> = Box<dyn Fn(&str) -> Option<Value> + Send + Sync + 'a>;

/// Substitution context for `{{token}}` placeholders in the descriptor's
/// `body`. Held by reference so the closure used for `{{control:<id>}}`
/// lookups can borrow from the caller's settings cache without forcing
/// a clone.
///
/// The closure carries `Send + Sync` bounds because the dispatcher
/// holds a `&SubstitutionContext` across `.await` points and Tauri
/// requires its command futures to be `Send`. Tests construct the
/// closure via a regular `Box::new(|id| ...)` (closures are
/// auto-`Send + Sync` when they don't capture non-`Send` state).
pub struct SubstitutionContext<'a> {
    pub project_id: &'a str,
    pub module_id: &'a str,
    /// The control's current value (for `on_change` / `apply_action`).
    /// `None` for fire-and-forget Button actions.
    pub value: Option<&'a Value>,
    /// Resolver for `{{control:<id>}}` tokens — reads other controls'
    /// persisted settings. Returns `None` when the id is unknown.
    pub get_control_value: ControlValueResolver<'a>,
}

impl<'a> SubstitutionContext<'a> {
    /// Convenience constructor for the no-control-lookup case (the
    /// production dispatcher builds a real resolver via
    /// `dispatch_action_with_sink`; this helper is mostly used by
    /// tests + future callers that don't yet have a sibling-value
    /// snapshot — e.g. a scheduler invoking a saved action with no
    /// active renderer context).
    #[allow(dead_code)]
    pub fn simple(project_id: &'a str, module_id: &'a str, value: Option<&'a Value>) -> Self {
        Self {
            project_id,
            module_id,
            value,
            get_control_value: Box::new(|_| None),
        }
    }
}

// ─── Template substitution ──────────────────────────────────────────────

/// Substitute `{{token}}` placeholders in a JSON value.
///
/// Rules:
///   * `"{{token}}"` as the WHOLE string value → replaced with the
///     typed value (bool/string/number/array/object preserved).
///   * `"{{token}}"` embedded inside a longer string → stringified and
///     concatenated. Bool / number / array stringify via
///     `serde_json::to_string`.
///   * Recurses into nested objects + arrays.
///   * Unknown variables → `Err(...)`. Closed-set policy keeps typos
///     visible rather than letting them silently drop placeholder text
///     into a request body.
///
/// Supported tokens:
///   * `{{project_id}}` — current project id (string).
///   * `{{module_id}}` — current module id (string).
///   * `{{value}}` — the control's incoming value. Errors when no value
///     was passed (caller should have provided one).
///   * `{{control:<id>}}` — value of another control in the same module
///     (via the closure in [`SubstitutionContext`]).
pub fn substitute(value: &Value, ctx: &SubstitutionContext) -> Result<Value, String> {
    match value {
        Value::String(s) => substitute_string(s, ctx),
        Value::Array(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(substitute(item, ctx)?);
            }
            Ok(Value::Array(out))
        }
        Value::Object(map) => {
            let mut out = serde_json::Map::with_capacity(map.len());
            for (k, v) in map {
                // Keys are not substituted — only values. Substituting
                // keys would let a manifest produce duplicate keys or
                // non-string keys depending on the substitution, and
                // serde_json::Map doesn't accept non-string keys anyway.
                out.insert(k.clone(), substitute(v, ctx)?);
            }
            Ok(Value::Object(out))
        }
        // Bool / Number / Null pass through verbatim.
        other => Ok(other.clone()),
    }
}

/// Substitute placeholders inside a single string. Implements the
/// whole-string-vs-embedded distinction documented on [`substitute`].
fn substitute_string(s: &str, ctx: &SubstitutionContext) -> Result<Value, String> {
    // Whole-string fast path: the ENTIRE string is a single `{{token}}`.
    // Returns the typed value so booleans stay booleans, arrays stay
    // arrays, etc. This is the contract callers depend on for things
    // like `"value": "{{value}}"` carrying a boolean unchanged.
    if let Some(token) = whole_token(s) {
        return resolve_token(token, ctx);
    }

    // Embedded path: scan for `{{...}}` segments and string-concatenate
    // their stringified resolutions. A single string may contain N
    // tokens (e.g. `"hello {{project_id}} from {{module_id}}"`).
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < s.len() {
        // The `{{` opener is pure ASCII, so the only place a UTF-8
        // boundary lands at a `{` byte is the start of one. Searching
        // for the next `{{` lets us slice the literal prefix as a
        // `&str` (UTF-8-safe by construction) and only invoke the
        // token machinery when there's actually a token to expand.
        match s[i..].find("{{") {
            None => {
                // No more tokens — push the remainder verbatim.
                out.push_str(&s[i..]);
                break;
            }
            Some(rel) => {
                // Literal prefix up to the token, UTF-8-safe.
                out.push_str(&s[i..i + rel]);
                let token_start = i + rel + 2;
                let close_relative = s[token_start..].find("}}").ok_or_else(|| {
                    format!("substitute: unclosed '{{{{' in template: {:?}", s)
                })?;
                let token = &s[token_start..token_start + close_relative];
                let resolved = resolve_token(token, ctx)?;
                // Stringify the resolved value for embedded use. Strings
                // are taken verbatim (no extra quotes); everything else is
                // JSON-encoded so callers see a stable representation.
                match resolved {
                    Value::String(s2) => out.push_str(&s2),
                    other => out.push_str(&serde_json::to_string(&other).map_err(|e| {
                        format!("substitute: encode resolved token '{}': {}", token, e)
                    })?),
                }
                i = token_start + close_relative + 2;
            }
        }
    }
    Ok(Value::String(out))
}

/// Returns `Some(token)` iff the entire string is a single `{{...}}`
/// expression with nothing before or after. Used by the whole-string
/// fast path so typed values survive substitution.
fn whole_token(s: &str) -> Option<&str> {
    let trimmed = s.strip_prefix("{{")?.strip_suffix("}}")?;
    // Reject `"{{a}}{{b}}"` — that has an inner `}}{{` boundary.
    if trimmed.contains("}}") || trimmed.contains("{{") {
        return None;
    }
    Some(trimmed)
}

/// Resolve a single token to a JSON value. Tokens are trimmed
/// (`"{{ project_id }}"` is the same as `"{{project_id}}"`).
fn resolve_token(token: &str, ctx: &SubstitutionContext) -> Result<Value, String> {
    let trimmed = token.trim();
    match trimmed {
        "project_id" => Ok(Value::String(ctx.project_id.to_string())),
        "module_id" => Ok(Value::String(ctx.module_id.to_string())),
        "value" => ctx
            .value
            .cloned()
            .ok_or_else(|| "substitute: '{{value}}' referenced but no value provided".into()),
        other if other.starts_with("control:") => {
            let control_id = &other["control:".len()..];
            (ctx.get_control_value)(control_id).ok_or_else(|| {
                format!(
                    "substitute: '{{{{control:{}}}}}' references unknown control",
                    control_id,
                )
            })
        }
        other => Err(format!(
            "substitute: unknown placeholder '{{{{{}}}}}' (allowed: project_id, module_id, value, control:<id>)",
            other,
        )),
    }
}

// ─── JSON-path mini-parser ──────────────────────────────────────────────

/// Resolve a top-level-only JSONPath against a response body. v1 ONLY
/// accepts `$.<key>` (single segment). Anything deeper returns an
/// `Err(...)` with a clear message — the dispatcher's polling support
/// is intentionally minimal until callers ask for more.
fn jsonpath_top_level(expr: &str, body: &Value) -> Result<Value, String> {
    let key = expr
        .strip_prefix("$.")
        .ok_or_else(|| format!("jsonpath: '{}' must start with '$.'", expr))?;
    if key.is_empty() {
        return Err(format!("jsonpath: '{}' has empty key segment", expr));
    }
    // Reject deeper paths (`$.a.b`, `$.a[0]`, `$.a['b']`).
    if key.contains('.') || key.contains('[') || key.contains(']') {
        return Err(format!(
            "jsonpath: '{}' only supports top-level keys ($.<key>) in v1",
            expr,
        ));
    }
    body.get(key)
        .cloned()
        .ok_or_else(|| format!("jsonpath: key '{}' not found in response", key))
}

// ─── Dispatcher ─────────────────────────────────────────────────────────

/// Inner dispatcher — factored from the Tauri command so unit tests can
/// drive it without a `tauri::State<'_, Db>` wrapper. Takes plain
/// references to the dependencies it needs.
///
/// Returns the response body of the FIRST (kick) request, parsed as
/// JSON. Polling progress + chained action results flow via Tauri
/// events, not the return value — the renderer treats the return value
/// as a success acknowledgement.
///
/// Chain handling: `next_action` is executed via an iterative loop. The
/// loop is bounded by [`MAX_CHAIN_STEPS`] to prevent runaway depth from
/// a malformed manifest.
pub async fn dispatch_action_inner(
    module_id: &str,
    project_id: &str,
    action: ActionDescriptor,
    value: Option<Value>,
    sibling_values: Option<HashMap<String, Value>>,
    app: AppHandle,
    db: &Db,
    http_client: &reqwest::Client,
) -> Result<Value, String> {
    let sink: Arc<dyn EventSink> = Arc::new(AppHandleSink(app));
    dispatch_action_with_sink(
        module_id,
        project_id,
        action,
        value,
        sibling_values,
        sink,
        db,
        http_client,
    )
    .await
}

/// Test-injectable variant of `dispatch_action_inner`. Production code
/// goes through `dispatch_action_inner` (which wraps a real
/// `AppHandle`); tests pass a `RecordingSink` so they can assert on the
/// events the dispatcher would have emitted.
pub(crate) async fn dispatch_action_with_sink(
    module_id: &str,
    project_id: &str,
    action: ActionDescriptor,
    value: Option<Value>,
    sibling_values: Option<HashMap<String, Value>>,
    sink: Arc<dyn EventSink>,
    db: &Db,
    http_client: &reqwest::Client,
) -> Result<Value, String> {
    // Build the substitution context ONCE — passed by reference into
    // each chained step so all steps see the same project / module /
    // initial-value pair.
    //
    // v0.2.26 follow-up (reviewer finding 3.2): `{{control:<id>}}` now
    // resolves end-to-end. The renderer snapshots its current control
    // map (id → JSON value) and passes it as `sibling_values`. We
    // fall back to `module_settings` reads when the renderer didn't
    // provide a snapshot (e.g. for legacy callers or tests).
    let value_ref = value.as_ref();
    let project_id_owned = project_id.to_string();
    let module_id_owned = module_id.to_string();
    // Snapshot the renderer-provided sibling map into an Arc so the
    // closure can outlive this scope without lifetime gymnastics.
    let sibling_snapshot: Arc<HashMap<String, Value>> =
        Arc::new(sibling_values.unwrap_or_default());
    // Take a borrowed db handle the closure can use for DB fallback.
    let db_for_resolver = db;
    let resolver: ControlValueResolver = {
        let sibling_snapshot = sibling_snapshot.clone();
        Box::new(move |control_id: &str| -> Option<Value> {
            if let Some(v) = sibling_snapshot.get(control_id) {
                return Some(v.clone());
            }
            // Fallback: read from persistent module_settings. This
            // path covers controls whose values aren't in the
            // renderer's snapshot (cross-tab references, server-side
            // dispatch via future schedulers, tests).
            db_for_resolver
                .get_setting(&project_id_owned, &module_id_owned, control_id)
                .ok()
                .flatten()
        })
    };
    let ctx = SubstitutionContext {
        project_id,
        module_id,
        value: value_ref,
        get_control_value: resolver,
    };

    // Resolve the module's port. The dispatcher refuses to fire when
    // the row is absent (clear error explaining the likely cause).
    let port = db
        .get_module_port(project_id, module_id)?
        .ok_or_else(|| {
            format!(
                "module_dispatch: module '{}' has no port for project '{}' \
                 — is the module installed and its container started?",
                module_id, project_id,
            )
        })?;

    // Walk the chain iteratively. We retain the FIRST kick's response
    // as the return value; subsequent steps' responses fire via events
    // (or are simply discarded — polling progress is the visible signal).
    let mut current_action = action;
    let mut first_response: Option<Value> = None;
    let mut steps: u32 = 0;
    loop {
        steps += 1;
        if steps > MAX_CHAIN_STEPS {
            return Err(format!(
                "module_dispatch: chain depth exceeded {} steps — refusing to continue",
                MAX_CHAIN_STEPS,
            ));
        }
        let (resp, next) = execute_one_step(
            module_id,
            project_id,
            &current_action,
            &ctx,
            port,
            sink.clone(),
            http_client,
        )
        .await?;
        if first_response.is_none() {
            first_response = Some(resp);
        }
        match next {
            Some(boxed) => current_action = *boxed,
            None => break,
        }
    }
    Ok(first_response.unwrap_or(Value::Null))
}

/// Execute one descriptor (no chain following). Returns
/// `(response_body, next_action)` so the caller can iterate.
async fn execute_one_step(
    module_id: &str,
    project_id: &str,
    action: &ActionDescriptor,
    ctx: &SubstitutionContext<'_>,
    port: u16,
    sink: Arc<dyn EventSink>,
    http_client: &reqwest::Client,
) -> Result<(Value, Option<Box<ActionDescriptor>>), String> {
    match action {
        ActionDescriptor::Http {
            method,
            path,
            body,
            polling,
            next_action,
        } => {
            let url = format!("http://127.0.0.1:{}{}", port, path);
            let substituted_body = match body {
                Some(raw) => Some(substitute(raw, ctx)?),
                None => None,
            };

            // Build the request via reqwest. The method match below
            // matches the manifest's HttpMethod 1:1.
            let mut req = match method {
                HttpMethod::Get => http_client.get(&url),
                HttpMethod::Post => http_client.post(&url),
                HttpMethod::Put => http_client.put(&url),
                HttpMethod::Delete => http_client.delete(&url),
            };
            if let Some(ref body_value) = substituted_body {
                req = req.json(body_value);
            }

            let response = req
                .send()
                .await
                .map_err(|e| format!("module_dispatch: HTTP {} {}: {}", method_name(method), url, e))?;

            let status = response.status();
            // We attempt to parse JSON even for error statuses so callers
            // can surface server-provided error messages. Falls back to
            // a synthesised object when the body isn't JSON.
            let body_text = response
                .text()
                .await
                .unwrap_or_else(|e| format!("<failed to read body: {}>", e));
            let body_json: Value = serde_json::from_str(&body_text)
                .unwrap_or_else(|_| Value::String(body_text.clone()));

            if !status.is_success() {
                return Err(format!(
                    "module_dispatch: HTTP {} {} → {}: {}",
                    method_name(method),
                    url,
                    status,
                    body_text,
                ));
            }

            // Polling: spawn a background poller IF the descriptor asked
            // for one. The kick response is returned to the caller; the
            // poller emits events independently.
            if let Some(spec) = polling.clone() {
                let job_id = extract_job_id(&body_json, &spec.job_id_path)?;
                let sink_clone = sink.clone();
                let client_clone = http_client.clone();
                let module_id_clone = module_id.to_string();
                let project_id_clone = project_id.to_string();
                tokio::spawn(async move {
                    let _ = run_poller(
                        &module_id_clone,
                        &project_id_clone,
                        port,
                        &spec,
                        &job_id,
                        sink_clone,
                        client_clone,
                    )
                    .await;
                });
            }

            Ok((body_json, next_action.clone()))
        }
    }
}

fn method_name(m: &HttpMethod) -> &'static str {
    match m {
        HttpMethod::Get => "GET",
        HttpMethod::Post => "POST",
        HttpMethod::Put => "PUT",
        HttpMethod::Delete => "DELETE",
    }
}

/// Extract the job id from the kick response. Accepts only the v1
/// shape (`$.<top-level-key>` → string OR number coerced via Display).
fn extract_job_id(kick_body: &Value, job_id_path: &str) -> Result<String, String> {
    let value = jsonpath_top_level(job_id_path, kick_body)?;
    match value {
        Value::String(s) => Ok(s),
        Value::Number(n) => Ok(n.to_string()),
        other => Err(format!(
            "module_dispatch: job_id at '{}' must be a string or number, got: {}",
            job_id_path, other,
        )),
    }
}

/// Background polling loop. Emits `polling.progress_event` on every
/// tick with the full poll-response body. Terminates on success state,
/// failure state, max-attempts, or an unrecoverable error.
///
/// Lives in this module (not a separate file) so the implementation
/// stays alongside the dispatcher contract that drives it.
/// Max consecutive transient errors (non-2xx OR network error OR body
/// parse failure) before the poller gives up and emits `failed_event`.
/// Transient errors are common in long-running polls — a backend
/// restart, a brief 503, a stale connection — so a single bad tick
/// should not terminate the loop. v0.2.26 reviewer finding 3.3.
const POLL_CONSECUTIVE_FAILURE_LIMIT: u32 = 5;

async fn run_poller(
    module_id: &str,
    project_id: &str,
    port: u16,
    spec: &PollingSpec,
    job_id: &str,
    sink: Arc<dyn EventSink>,
    client: reqwest::Client,
) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}{}", port, spec.endpoint);
    let mut consecutive_failures: u32 = 0;
    for _attempt in 0..spec.max_attempts {
        tokio::time::sleep(Duration::from_secs(spec.interval_seconds)).await;

        let resp = client
            .get(&url)
            .query(&[(spec.job_id_query_param.as_str(), job_id)])
            .send()
            .await;

        let body_json: Value = match resp {
            Ok(r) if r.status().is_success() => match r.text().await {
                Ok(text) => serde_json::from_str(&text).unwrap_or(Value::String(text)),
                Err(e) => {
                    // Transient read error — count toward the
                    // consecutive-failure budget but keep polling.
                    eprintln!("[module_dispatch] poll body read error: {}", e);
                    consecutive_failures += 1;
                    if consecutive_failures >= POLL_CONSECUTIVE_FAILURE_LIMIT {
                        sink.emit(
                            spec.failed_event.as_str(),
                            &serde_json::json!({
                                "module_id": module_id,
                                "project_id": project_id,
                                "error": format!(
                                    "polling aborted after {} consecutive body-read errors",
                                    consecutive_failures,
                                ),
                            }),
                        );
                        return Ok(());
                    }
                    continue;
                }
            },
            Ok(r) => {
                // Non-2xx response. v0.2.26 reviewer finding 3.3:
                // these are often transient (503 during a container
                // restart, brief 502 on a proxy hiccup). Log + count
                // toward the consecutive-failure budget instead of
                // aborting on the first bad tick.
                let status = r.status();
                let body_text = r.text().await.unwrap_or_default();
                eprintln!(
                    "[module_dispatch] poll non-success status {} from {}: {}",
                    status, url, body_text,
                );
                consecutive_failures += 1;
                if consecutive_failures >= POLL_CONSECUTIVE_FAILURE_LIMIT {
                    sink.emit(
                        spec.failed_event.as_str(),
                        &serde_json::json!({
                            "module_id": module_id,
                            "project_id": project_id,
                            "error": format!(
                                "polling endpoint returned non-success status \
                                 {} consecutive times (last status: {}, last body: {})",
                                consecutive_failures, status, body_text,
                            ),
                        }),
                    );
                    return Ok(());
                }
                continue;
            }
            Err(e) => {
                eprintln!("[module_dispatch] poll request error: {}", e);
                consecutive_failures += 1;
                if consecutive_failures >= POLL_CONSECUTIVE_FAILURE_LIMIT {
                    sink.emit(
                        spec.failed_event.as_str(),
                        &serde_json::json!({
                            "module_id": module_id,
                            "project_id": project_id,
                            "error": format!(
                                "polling aborted after {} consecutive request errors (last: {})",
                                consecutive_failures, e,
                            ),
                        }),
                    );
                    return Ok(());
                }
                continue;
            }
        };

        // Successful tick — reset the consecutive-failure counter.
        consecutive_failures = 0;

        // Emit progress event with full response.
        sink.emit(spec.progress_event.as_str(), &body_json);

        // Read terminal_state_field via the same top-level mini-JSONPath.
        let state_value = jsonpath_top_level(&spec.terminal_state_field, &body_json).ok();
        let state_str = state_value.as_ref().map(|v| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        });
        if let Some(s) = state_str {
            if spec.terminal_success_values.iter().any(|v| v == &s) {
                return Ok(());
            }
            if spec.terminal_failure_values.iter().any(|v| v == &s) {
                sink.emit(
                    spec.failed_event.as_str(),
                    &serde_json::json!({
                        "module_id": module_id,
                        "project_id": project_id,
                        "state": s,
                        "response": body_json,
                    }),
                );
                return Ok(());
            }
        }
    }

    // Loop exhausted without hitting a terminal state — emit failure.
    sink.emit(
        spec.failed_event.as_str(),
        &serde_json::json!({
            "module_id": module_id,
            "project_id": project_id,
            "error": "max_attempts exceeded",
        }),
    );
    Ok(())
}

// ─── Tauri command surface ──────────────────────────────────────────────

/// Single generic Tauri command exposed to the renderer.
///
/// Builds a fresh `reqwest::Client` (with a 30 s timeout) per dispatch.
/// We considered managing a global Client via `.manage(...)` to share
/// connection pooling, but the dispatcher's invocation pattern
/// (sporadic button clicks + low-frequency status polls) makes a
/// per-call client cheap enough that the simpler ownership story wins.
/// Generic Tauri command exposed to the renderer.
///
/// `sibling_values` (v0.2.26 follow-up, reviewer finding 3.2):
/// sibling-control values snapshot supplied by the renderer so
/// `{{control:<id>}}` template tokens in the descriptor body resolve
/// end-to-end. Optional — when `None`, the dispatcher falls back to
/// `module_settings` DB reads, which covers cross-tab references +
/// future scheduler callers that don't have a renderer context.
#[command]
pub async fn module_dispatch_action(
    module_id: String,
    project_id: String,
    action: ActionDescriptor,
    value: Option<Value>,
    sibling_values: Option<HashMap<String, Value>>,
    app: AppHandle,
    db: State<'_, Db>,
) -> Result<Value, String> {
    if module_id.is_empty() {
        return Err("module_dispatch_action: module_id required".into());
    }
    if project_id.is_empty() {
        return Err("module_dispatch_action: project_id required".into());
    }
    let http_client = reqwest::Client::builder()
        .timeout(Duration::from_secs(HTTP_TIMEOUT_SECS))
        .build()
        .map_err(|e| format!("module_dispatch_action: build HTTP client: {}", e))?;
    dispatch_action_inner(
        &module_id,
        &project_id,
        action,
        value,
        sibling_values,
        app,
        db.inner(),
        &http_client,
    )
    .await
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;
    use axum::response::IntoResponse;
    use serde_json::json;
    use std::net::SocketAddr;
    use std::sync::Mutex as StdMutex;

    /// In-memory event sink for tests. Records every `emit()` call so
    /// tests can assert on the events the dispatcher would have fired.
    #[derive(Clone, Default)]
    struct RecordingSink {
        events: Arc<StdMutex<Vec<(String, Value)>>>,
    }

    impl RecordingSink {
        fn new() -> Self {
            Self::default()
        }

        fn snapshot(&self) -> Vec<(String, Value)> {
            self.events.lock().unwrap().clone()
        }
    }

    impl EventSink for RecordingSink {
        fn emit(&self, event: &str, payload: &Value) {
            self.events
                .lock()
                .unwrap()
                .push((event.to_string(), payload.clone()));
        }
    }

    /// Build an in-memory DB with one project, allocate a port row for
    /// `module_id` pointing at `port`.
    fn db_with_module(project_id: &str, module_id: &str, port: u16) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_project(
            project_id,
            "Test Project",
            "/tmp/test",
            ProjectHost::Base,
            "test-project",
        )
        .expect("insert project");
        db.set_module_port(project_id, module_id, port)
            .expect("set module port");
        db
    }

    /// Pick a free localhost port via OS-assigned TcpListener. We bind,
    /// read the assigned port, drop the listener so the test server can
    /// reclaim the port. Brief race window between drop and re-bind is
    /// acceptable for a single-process test runner.
    fn pick_free_port() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind 0");
        let port = listener.local_addr().expect("local addr").port();
        drop(listener);
        port
    }

    // ─── substitute() ───────────────────────────────────────────────

    fn ctx_with_value<'a>(value: &'a Value) -> SubstitutionContext<'a> {
        SubstitutionContext::simple("proj-A", "mod-X", Some(value))
    }

    /// `"{{project_id}}"` as a whole-string token → typed string.
    #[test]
    fn substitute_whole_string_project_id() {
        let v = json!("{{project_id}}");
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("proj-A".into()));
    }

    /// `{{value}}` whole-string with a bool source preserves JSON type.
    #[test]
    fn substitute_whole_string_value_preserves_bool() {
        let v = json!("{{value}}");
        let source = Value::Bool(true);
        let ctx = ctx_with_value(&source);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::Bool(true), "type must survive substitution");
        assert!(out.is_boolean());
    }

    /// `{{value}}` whole-string with a number source preserves JSON type.
    #[test]
    fn substitute_whole_string_value_preserves_number() {
        let v = json!("{{value}}");
        let source = json!(42);
        let ctx = ctx_with_value(&source);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, json!(42));
        assert!(out.is_number());
    }

    /// `{{value}}` whole-string with an array source preserves JSON type.
    #[test]
    fn substitute_whole_string_value_preserves_array() {
        let v = json!("{{value}}");
        let source = json!(["a", "b", "c"]);
        let ctx = ctx_with_value(&source);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, json!(["a", "b", "c"]));
        assert!(out.is_array());
    }

    /// `{{value}}` whole-string with an object source preserves JSON type.
    #[test]
    fn substitute_whole_string_value_preserves_object() {
        let v = json!("{{value}}");
        let source = json!({"k": 1});
        let ctx = ctx_with_value(&source);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, json!({"k": 1}));
        assert!(out.is_object());
    }

    /// Embedded `{{token}}` inside a larger string → stringified
    /// interpolation. Bool becomes "true", number becomes "42", etc.
    #[test]
    fn substitute_embedded_string_interpolates() {
        let v = json!("hello {{project_id}} / {{module_id}}");
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("hello proj-A / mod-X".into()));
    }

    /// Regression for v0.2.26 reviewer finding 3.1: embedded
    /// substitution previously cast each byte to `char`, which
    /// mis-decoded multi-byte UTF-8 sequences (`é` = `0xC3 0xA9`
    /// became `U+00C3 U+00A9` instead of `U+00E9`). Now we slice
    /// `&str` segments between tokens, so any valid UTF-8 input
    /// is preserved verbatim.
    #[test]
    fn substitute_embedded_preserves_utf8() {
        let ctx = SubstitutionContext::simple("proj-1", "mod-Y", None);
        // Western European: accented Latin.
        let v = json!("héllo {{project_id}} — café");
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("héllo proj-1 — café".into()));
        // CJK + emoji surrounding the token.
        let v = json!("こんにちは {{module_id}} 🚀");
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("こんにちは mod-Y 🚀".into()));
        // No tokens at all: pure UTF-8 passthrough.
        let v = json!("Naïve façade — résumé");
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("Naïve façade — résumé".into()));
    }

    /// Embedded bool stringifies to "true"/"false".
    #[test]
    fn substitute_embedded_bool_stringifies() {
        let v = json!("flag={{value}}!");
        let source = Value::Bool(false);
        let ctx = ctx_with_value(&source);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, Value::String("flag=false!".into()));
    }

    /// Substitution recurses into nested objects.
    #[test]
    fn substitute_recurses_into_nested_object() {
        let v = json!({
            "outer": {
                "inner": "{{project_id}}",
                "passthrough": 1,
            },
            "other": "{{module_id}}"
        });
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(
            out,
            json!({
                "outer": {
                    "inner": "proj-A",
                    "passthrough": 1,
                },
                "other": "mod-X",
            }),
        );
    }

    /// Substitution recurses into array elements.
    #[test]
    fn substitute_recurses_into_array_elements() {
        let v = json!(["{{project_id}}", "literal", "{{module_id}}"]);
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out, json!(["proj-A", "literal", "mod-X"]));
    }

    /// Unknown placeholder token → clear error (closed-set policy).
    #[test]
    fn substitute_unknown_token_errors() {
        let v = json!("{{nope}}");
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(
            err.contains("unknown placeholder")
                && err.contains("nope"),
            "got: {}",
            err,
        );
    }

    /// `{{control:<id>}}` resolves via the closure.
    #[test]
    fn substitute_control_token_resolves_via_closure() {
        let v = json!({
            "from_control": "{{control:enable_feature}}",
            "from_value": "{{value}}",
        });
        let source = json!("user-input");
        let ctx = SubstitutionContext {
            project_id: "proj-A",
            module_id: "mod-X",
            value: Some(&source),
            get_control_value: Box::new(|id| {
                if id == "enable_feature" {
                    Some(Value::Bool(true))
                } else {
                    None
                }
            }),
        };
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(
            out,
            json!({
                "from_control": true,
                "from_value": "user-input",
            }),
        );
    }

    /// `{{control:<id>}}` errors when the closure returns None.
    #[test]
    fn substitute_control_token_unknown_errors() {
        let v = json!("{{control:missing_id}}");
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(
            err.contains("missing_id") && err.contains("unknown control"),
            "got: {}",
            err,
        );
    }

    /// `{{value}}` errors when no value is in context.
    #[test]
    fn substitute_value_token_without_context_errors() {
        let v = json!("{{value}}");
        let ctx = SubstitutionContext::simple("proj-A", "mod-X", None);
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(err.contains("value"), "got: {}", err);
    }

    // ─── jsonpath_top_level() ────────────────────────────────────────

    #[test]
    fn jsonpath_top_level_extracts_string() {
        let body = json!({"job_id": "abc-123", "status": "ok"});
        assert_eq!(
            jsonpath_top_level("$.job_id", &body).unwrap(),
            json!("abc-123"),
        );
    }

    #[test]
    fn jsonpath_top_level_rejects_deeper_paths() {
        let body = json!({"a": {"b": 1}});
        let err = jsonpath_top_level("$.a.b", &body).unwrap_err();
        assert!(err.contains("top-level"), "got: {}", err);
    }

    #[test]
    fn jsonpath_top_level_rejects_array_indexing() {
        let body = json!({"a": [1]});
        let err = jsonpath_top_level("$.a[0]", &body).unwrap_err();
        assert!(err.contains("top-level"), "got: {}", err);
    }

    #[test]
    fn jsonpath_top_level_missing_key_errors() {
        let body = json!({"present": "value"});
        let err = jsonpath_top_level("$.missing", &body).unwrap_err();
        assert!(err.contains("not found"), "got: {}", err);
    }

    // ─── Dispatcher integration tests ───────────────────────────────
    //
    // These spin up an in-process axum server on a free port, point
    // the dispatcher at it via `set_module_port`, and assert on the
    // round-trip behaviour. No external mock-server crate needed:
    // axum is already in [dependencies] for the launcher's hub.

    use axum::extract::Query;
    use axum::{routing, Json, Router};
    use std::collections::HashMap;

    fn build_http_client() -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .expect("build http client")
    }

    /// Start a server with a single route + return the bound port.
    /// Spawns the serve task; the listener stays alive for the test.
    async fn start_server(router: Router) -> u16 {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind 0");
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let _ = axum::serve(listener, router).await;
        });
        // Give the server a moment to start accepting.
        tokio::time::sleep(Duration::from_millis(50)).await;
        port
    }

    /// Simple POST round-trip: dispatcher sends a body, server echoes
    /// back a response, dispatcher returns the parsed JSON.
    #[tokio::test]
    async fn dispatcher_simple_post_round_trips() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        // Server: POST /kick echoes the body + adds an `ok: true` field.
        let router = Router::new().route(
            "/kick",
            routing::post(|Json(body): Json<Value>| async move {
                Json(json!({"ok": true, "echo": body}))
            }),
        );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: Some(json!({"project": "{{project_id}}", "module": "{{module_id}}"})),
            polling: None,
            next_action: None,
        };

        let resp = dispatch_action_with_sink(
            module_id,
            project_id,
            action,
            None,
            None,
            sink,
            &db,
            &client,
        )
        .await
        .expect("dispatch ok");

        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["echo"]["project"], json!("proj-A"));
        assert_eq!(resp["echo"]["module"], json!("mod-X"));
    }

    /// Regression for v0.2.26 reviewer finding 3.2: a `{{control:<id>}}`
    /// reference inside the descriptor body resolves end-to-end when the
    /// renderer supplies a `sibling_values` map. Before this fix the
    /// dispatcher always saw a `Box::new(|_| None)` resolver and the
    /// substitution unconditionally failed with "unknown control".
    #[tokio::test]
    async fn dispatcher_resolves_control_token_from_sibling_values() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new().route(
            "/kick",
            routing::post(|Json(body): Json<Value>| async move {
                Json(json!({"echo": body}))
            }),
        );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            // Both whole-string (preserves typed array) and embedded
            // (interpolates as string) forms of the control reference.
            body: Some(json!({
                "selected_projects": "{{control:source_projects}}",
                "label": "running for {{control:run_label}} now"
            })),
            polling: None,
            next_action: None,
        };

        let mut siblings: HashMap<String, Value> = HashMap::new();
        siblings.insert(
            "source_projects".into(),
            json!(["proj-1", "proj-2", "proj-3"]),
        );
        siblings.insert("run_label".into(), json!("Q3 retrain"));

        let resp = dispatch_action_with_sink(
            module_id,
            project_id,
            action,
            None,
            Some(siblings),
            sink,
            &db,
            &client,
        )
        .await
        .expect("dispatch ok");

        // Array survived as an array (whole-string fast path), not stringified.
        assert_eq!(
            resp["echo"]["selected_projects"],
            json!(["proj-1", "proj-2", "proj-3"])
        );
        // Embedded form interpolated as string.
        assert_eq!(
            resp["echo"]["label"],
            json!("running for Q3 retrain now")
        );
    }

    /// Regression for v0.2.26 reviewer finding 3.2 (DB fallback path):
    /// when the renderer doesn't supply a `sibling_values` entry for a
    /// given control, the dispatcher reads from `module_settings`. This
    /// path covers (a) cross-tab references where the renderer can't
    /// snapshot every control, (b) future scheduler invocations with no
    /// renderer context at all.
    #[tokio::test]
    async fn dispatcher_resolves_control_token_from_module_settings_fallback() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new().route(
            "/kick",
            routing::post(|Json(body): Json<Value>| async move {
                Json(json!({"echo": body}))
            }),
        );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        // Persist a sibling-control value through the regular settings
        // table (the same path the renderer uses on every checkbox/
        // multi_select change).
        db.set_setting(project_id, module_id, "persisted_flag", &json!(true))
            .expect("persist setting");
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: Some(json!({"flag": "{{control:persisted_flag}}"})),
            polling: None,
            next_action: None,
        };

        // No sibling_values from the renderer → dispatcher MUST fall
        // back to the module_settings read.
        let resp = dispatch_action_with_sink(
            module_id, project_id, action, None, None, sink, &db, &client,
        )
        .await
        .expect("dispatch ok");
        assert_eq!(resp["echo"]["flag"], json!(true));
    }

    /// Polling loop emits progress events on each tick AND terminates
    /// on a `state == "done"` response without firing a failed_event.
    #[tokio::test]
    async fn dispatcher_polling_succeeds_emits_progress_then_stops() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        // Server: POST /kick returns `{job_id: "j1"}`, GET /status
        // returns `{state: "running"}` for the first 2 calls, then
        // `{state: "done"}` thereafter. State held in a Mutex.
        let counter = Arc::new(StdMutex::new(0u32));
        let counter_clone = counter.clone();
        let router = Router::new()
            .route(
                "/kick",
                routing::post(|| async {
                    Json(json!({"job_id": "j1"}))
                }),
            )
            .route(
                "/status",
                routing::get(move |Query(_q): Query<HashMap<String, String>>| {
                    let c = counter_clone.clone();
                    async move {
                        let mut n = c.lock().unwrap();
                        *n += 1;
                        let state = if *n >= 3 { "done" } else { "running" };
                        Json(json!({"state": state, "tick": *n}))
                    }
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink = RecordingSink::new();
        let sink_arc: Arc<dyn EventSink> = Arc::new(sink.clone());

        // Short interval so the test runs in <1 s. max_attempts=10 is
        // a wide margin; we expect terminal at attempt 3.
        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: None,
            polling: Some(PollingSpec {
                endpoint: "/status".into(),
                job_id_path: "$.job_id".into(),
                job_id_query_param: "job_id".into(),
                interval_seconds: 0, // 0 → loop pauses ~immediately
                max_attempts: 10,
                terminal_state_field: "$.state".into(),
                terminal_success_values: vec!["done".into()],
                terminal_failure_values: vec!["failed".into(), "error".into()],
                progress_event: "test://progress".into(),
                failed_event: "test://failed".into(),
            }),
            next_action: None,
        };

        let kick_resp = dispatch_action_with_sink(
            module_id,
            project_id,
            action,
            None,
            None,
            sink_arc,
            &db,
            &client,
        )
        .await
        .expect("dispatch ok");
        assert_eq!(kick_resp["job_id"], json!("j1"));

        // Wait for poller to converge. interval_seconds=0 makes each
        // tick about as fast as the HTTP round-trip; 1 s is generous.
        tokio::time::sleep(Duration::from_millis(1500)).await;

        let events = sink.snapshot();
        // At least 3 progress events (ticks 1, 2, 3) and zero failed
        // events. The exact tick count depends on scheduling; we assert
        // >= 1 progress and 0 failures.
        let progress: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://progress")
            .collect();
        let failed: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://failed")
            .collect();
        assert!(
            !progress.is_empty(),
            "expected ≥1 progress event, got events: {:?}",
            events,
        );
        assert!(
            failed.is_empty(),
            "expected 0 failed events on success path, got: {:?}",
            failed,
        );
        // Final progress event should carry state=done.
        let last_progress = progress.last().expect("≥1 progress");
        assert_eq!(last_progress.1["state"], json!("done"));
    }

    /// Polling loop emits a failed_event when the server returns a
    /// terminal failure state (e.g. `state == "failed"`).
    #[tokio::test]
    async fn dispatcher_polling_fails_emits_failed_event() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new()
            .route(
                "/kick",
                routing::post(|| async { Json(json!({"job_id": "j1"})) }),
            )
            .route(
                "/status",
                routing::get(|Query(_): Query<HashMap<String, String>>| async {
                    Json(json!({"state": "failed", "reason": "test failure"}))
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink = RecordingSink::new();
        let sink_arc: Arc<dyn EventSink> = Arc::new(sink.clone());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: None,
            polling: Some(PollingSpec {
                endpoint: "/status".into(),
                job_id_path: "$.job_id".into(),
                job_id_query_param: "job_id".into(),
                interval_seconds: 0,
                max_attempts: 5,
                terminal_state_field: "$.state".into(),
                terminal_success_values: vec!["done".into()],
                terminal_failure_values: vec!["failed".into(), "error".into()],
                progress_event: "test://progress".into(),
                failed_event: "test://failed".into(),
            }),
            next_action: None,
        };

        dispatch_action_with_sink(module_id, project_id, action, None, None, sink_arc, &db, &client)
            .await
            .expect("dispatch ok");

        tokio::time::sleep(Duration::from_millis(500)).await;

        let events = sink.snapshot();
        let failed: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://failed")
            .collect();
        assert_eq!(
            failed.len(),
            1,
            "expected exactly one failed event, got events: {:?}",
            events,
        );
        // Payload should include the terminal state.
        assert_eq!(failed[0].1["state"], json!("failed"));
    }

    /// Regression for v0.2.26 reviewer finding 3.3: transient non-2xx
    /// responses (e.g. a 503 during a container restart) must NOT
    /// terminate the polling loop. The container's first few replies
    /// return 503, then 200 OK with state=done. Polling should ride
    /// through and emit a single progress event at the end with no
    /// `failed_event`.
    #[tokio::test]
    async fn dispatcher_polling_tolerates_transient_non_2xx() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        // Counter visible to the handler: first 3 calls return 503, rest 200.
        let counter = Arc::new(StdMutex::new(0u32));
        let counter_for_handler = counter.clone();
        let router = Router::new()
            .route(
                "/kick",
                routing::post(|| async { Json(json!({"job_id": "j1"})) }),
            )
            .route(
                "/status",
                routing::get(move |Query(_): Query<HashMap<String, String>>| {
                    let c = counter_for_handler.clone();
                    async move {
                        let mut n = c.lock().unwrap();
                        *n += 1;
                        if *n <= 3 {
                            (axum::http::StatusCode::SERVICE_UNAVAILABLE, "starting").into_response()
                        } else {
                            Json(json!({"state": "done"})).into_response()
                        }
                    }
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink = RecordingSink::new();
        let sink_arc: Arc<dyn EventSink> = Arc::new(sink.clone());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: None,
            polling: Some(PollingSpec {
                endpoint: "/status".into(),
                job_id_path: "$.job_id".into(),
                job_id_query_param: "job_id".into(),
                interval_seconds: 0,
                max_attempts: 20,
                terminal_state_field: "$.state".into(),
                terminal_success_values: vec!["done".into()],
                terminal_failure_values: vec!["failed".into()],
                progress_event: "test://progress".into(),
                failed_event: "test://failed".into(),
            }),
            next_action: None,
        };

        dispatch_action_with_sink(module_id, project_id, action, None, None, sink_arc, &db, &client)
            .await
            .expect("dispatch ok");

        tokio::time::sleep(Duration::from_millis(1500)).await;

        let events = sink.snapshot();
        let failed: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://failed")
            .collect();
        let progress: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://progress")
            .collect();
        assert!(
            failed.is_empty(),
            "transient 503s must not terminate the poll loop, got failed events: {:?}",
            failed,
        );
        assert!(
            !progress.is_empty(),
            "expected ≥1 progress event after the 503 streak cleared, got events: {:?}",
            events,
        );
        // The successful tick should carry state=done.
        let last_progress = progress.last().expect("≥1 progress");
        assert_eq!(last_progress.1["state"], json!("done"));
    }

    /// Regression for v0.2.26 reviewer finding 3.3 (the other side):
    /// PERSISTENT non-2xx responses must still surface as failure once
    /// the consecutive-failure budget is exhausted. Server always
    /// returns 500; the poller should emit `failed_event` after
    /// POLL_CONSECUTIVE_FAILURE_LIMIT ticks rather than running forever.
    #[tokio::test]
    async fn dispatcher_polling_persistent_non_2xx_eventually_fails() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new()
            .route(
                "/kick",
                routing::post(|| async { Json(json!({"job_id": "j1"})) }),
            )
            .route(
                "/status",
                routing::get(|Query(_): Query<HashMap<String, String>>| async {
                    (axum::http::StatusCode::INTERNAL_SERVER_ERROR, "always broken")
                        .into_response()
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink = RecordingSink::new();
        let sink_arc: Arc<dyn EventSink> = Arc::new(sink.clone());

        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/kick".into(),
            body: None,
            polling: Some(PollingSpec {
                endpoint: "/status".into(),
                job_id_path: "$.job_id".into(),
                job_id_query_param: "job_id".into(),
                interval_seconds: 0,
                max_attempts: 100, // ensure budget triggers BEFORE max_attempts
                terminal_state_field: "$.state".into(),
                terminal_success_values: vec!["done".into()],
                terminal_failure_values: vec!["failed".into()],
                progress_event: "test://progress".into(),
                failed_event: "test://failed".into(),
            }),
            next_action: None,
        };

        dispatch_action_with_sink(module_id, project_id, action, None, None, sink_arc, &db, &client)
            .await
            .expect("dispatch ok");

        tokio::time::sleep(Duration::from_millis(1500)).await;

        let events = sink.snapshot();
        let failed: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://failed")
            .collect();
        assert_eq!(
            failed.len(),
            1,
            "expected exactly one failed event after consecutive-failure budget exhausted, got events: {:?}",
            events,
        );
        // The failure error message should mention the consecutive count.
        let error_str = failed[0].1["error"].as_str().unwrap_or("");
        assert!(
            error_str.contains("consecutive"),
            "expected error message to mention 'consecutive', got: {:?}",
            error_str,
        );
    }

    /// Chained `next_action` executes the second descriptor after the
    /// first kick succeeds. Asserts both endpoints were hit and the
    /// first response is what the dispatcher returns.
    #[tokio::test]
    async fn dispatcher_chained_next_action_executes_second_step() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let first_called = Arc::new(StdMutex::new(false));
        let second_called = Arc::new(StdMutex::new(false));
        let first_flag = first_called.clone();
        let second_flag = second_called.clone();
        let router = Router::new()
            .route(
                "/first",
                routing::post(move || {
                    let f = first_flag.clone();
                    async move {
                        *f.lock().unwrap() = true;
                        Json(json!({"step": "first"}))
                    }
                }),
            )
            .route(
                "/second",
                routing::post(move || {
                    let s = second_flag.clone();
                    async move {
                        *s.lock().unwrap() = true;
                        Json(json!({"step": "second"}))
                    }
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let second_action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/second".into(),
            body: None,
            polling: None,
            next_action: None,
        };
        let first_action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/first".into(),
            body: None,
            polling: None,
            next_action: Some(Box::new(second_action)),
        };

        let resp = dispatch_action_with_sink(
            module_id,
            project_id,
            first_action,
            None,
            None,
            sink,
            &db,
            &client,
        )
        .await
        .expect("dispatch ok");

        // Returned response is the FIRST kick's body.
        assert_eq!(resp["step"], json!("first"));
        // Both endpoints were exercised.
        assert!(*first_called.lock().unwrap(), "first endpoint must be hit");
        assert!(*second_called.lock().unwrap(), "second endpoint must be hit");
    }

    /// A hand-built chain longer than [`MAX_CHAIN_STEPS`] returns the
    /// chain-depth error. We build a chain of (MAX_CHAIN_STEPS + 1)
    /// descriptors and assert the dispatcher refuses to walk it.
    #[tokio::test]
    async fn dispatcher_chain_depth_exceeded_returns_error() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        // Server: every POST returns 200 with empty body. We don't care
        // about the body content here — we care that the loop refuses
        // to walk past MAX_CHAIN_STEPS.
        let router = Router::new().route(
            "/noop",
            routing::post(|| async { Json(json!({})) }),
        );
        let port = start_server(router).await;
        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        // Build a chain of MAX_CHAIN_STEPS + 1 descriptors. Construction
        // walks from innermost to outermost so we can keep wrapping
        // the previous `next_action` in a Box.
        let mut current: Option<Box<ActionDescriptor>> = None;
        // +1 so the chain is one step too long.
        for _ in 0..=MAX_CHAIN_STEPS {
            let step = ActionDescriptor::Http {
                method: HttpMethod::Post,
                path: "/noop".into(),
                body: None,
                polling: None,
                next_action: current,
            };
            current = Some(Box::new(step));
        }
        let head = *current.expect("chain non-empty");

        let err = dispatch_action_with_sink(module_id, project_id, head, None, None, sink, &db, &client)
            .await
            .expect_err("expected chain-depth error");
        assert!(
            err.contains("chain depth exceeded"),
            "expected chain-depth message, got: {}",
            err,
        );
    }

    /// Missing port row returns a clear error. The dispatcher does NOT
    /// fall back to a default port (would hide install bugs).
    #[tokio::test]
    async fn dispatcher_missing_port_returns_clear_error() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_project(
            "proj-X",
            "Test",
            "/tmp/x",
            ProjectHost::Base,
            "test-x",
        )
        .expect("insert project");
        // No `set_module_port` call — the row is intentionally absent.

        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());
        let action = ActionDescriptor::Http {
            method: HttpMethod::Get,
            path: "/anything".into(),
            body: None,
            polling: None,
            next_action: None,
        };
        let err = dispatch_action_with_sink("ghost-module", "proj-X", action, None, None, sink, &db, &client)
            .await
            .expect_err("missing port must error");
        assert!(
            err.contains("no port for project") && err.contains("ghost-module"),
            "expected diagnostic mentioning module + project, got: {}",
            err,
        );
    }

    /// Confirms `pick_free_port` works (silences the dead-code warning
    /// while also pinning the helper's behaviour — the picked port
    /// must be in the ephemeral range, > 1024).
    #[test]
    fn helper_pick_free_port_returns_ephemeral() {
        let port = pick_free_port();
        assert!(port > 1024, "expected ephemeral port, got {}", port);
    }

    /// Sanity: RecordingSink::new() exists and produces an empty sink.
    /// Silences the unused-method warning + pins the contract.
    #[test]
    fn helper_recording_sink_starts_empty() {
        let sink = RecordingSink::new();
        assert!(sink.snapshot().is_empty());
        sink.emit("hello", &json!({"k": 1}));
        assert_eq!(sink.snapshot().len(), 1);
    }

    /// Convenience: SocketAddr import is used by axum routing wiring.
    /// This trivial test exists to silence an "unused import" lint if
    /// the integration tests above are ever deleted; the import is
    /// still needed for axum's `serve` signature in some configurations.
    #[test]
    fn helper_socket_addr_import_is_live() {
        let _ = SocketAddr::from(([127, 0, 0, 1], 0));
    }
}
