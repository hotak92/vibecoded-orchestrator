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

/// v0.2.27: resolver for `{{events_paths_for:<control_id>}}`. Looks up
/// the referenced control's array-of-UUIDs value, walks each UUID via
/// `db.get_project()`, applies the active module's `log_path_template`,
/// and returns a JSON array of paths. Returns an `Err` with a clear
/// message on any failure (control id unknown, value not an array of
/// strings, UUID doesn't resolve to a project, module has no template).
///
/// Held by reference so the dispatcher can wire it once at the
/// `dispatch_action_with_sink` boundary; tests can install a synthetic
/// resolver that bypasses the DB.
pub type EventsPathsResolver<'a> = Box<dyn Fn(&str) -> Result<Value, String> + Send + Sync + 'a>;

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
    /// v0.2.27: resolver for `{{events_paths_for:<control_id>}}`. When
    /// None, the dispatcher rejects any use of that token with a clear
    /// "events_paths_for unavailable — module declares no log_path_template
    /// OR no DB context" error. The production dispatcher always wires
    /// this when the module has `runtime.log_path_template` set.
    pub get_events_paths_for: Option<EventsPathsResolver<'a>>,
    /// v0.2.32 (CHAINED_ACTION): the response body of the IMMEDIATELY
    /// PREVIOUS step in a `ChainedAction` execution. Populated by the
    /// chained-action loop before substituting each step's body; the
    /// resolver consumes it via the `{{previous_step.<field>}}` token.
    /// `None` outside chained-action context (single-step dispatch) or
    /// during the first step of a chain.
    pub previous_step: Option<&'a Value>,
    /// v0.2.32 (CHAINED_ACTION): all step responses gathered so far in
    /// the chain, indexed by zero-based step position. Backs the
    /// `{{step.N.<field>}}` absolute-index token form. Empty outside
    /// chained-action context.
    pub step_results: &'a [Value],
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
            get_events_paths_for: None,
            previous_step: None,
            step_results: &[],
        }
    }
}

/// v0.2.32 (CHAINED_ACTION): empty slice constant used as the default
/// `step_results` when callers don't have any previous-step data.
/// Lifts the empty-slice value out of inline literals so chained-action
/// builders can reference the same `'static [Value]` shape.
const EMPTY_STEP_RESULTS: &[Value] = &[];

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
                // v0.2.27: `events_paths_for:<id>` resolves to a JSON
                // array — embedding an array inside a longer string is
                // ambiguous (JSON-stringify it? join with what
                // separator?) and almost certainly an authoring
                // mistake. Reject loudly at the embedded site so the
                // module author gets a clear "use whole-string form"
                // pointer instead of a silently-garbled body.
                if token.trim().starts_with("events_paths_for:") {
                    return Err(format!(
                        "substitute: '{{{{{}}}}}' must be the WHOLE string value of a body \
                         field (embedded form not supported because the token resolves to a \
                         JSON array). Re-shape the body so the value is `\"{{{{{}}}}}\"` \
                         with nothing before or after.",
                        token.trim(),
                        token.trim(),
                    ));
                }
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
        // v0.2.27: events_paths_for resolves a control's array-of-UUIDs
        // value into an array of host paths via the module's
        // `runtime.log_path_template`. Returns a `JsonValue::Array`.
        // This is the FIRST token that returns a non-scalar JSON value;
        // the caller (`substitute_string`) enforces whole-string-only
        // for this case (embedding an array into a longer string makes
        // no sense and is rejected loudly).
        other if other.starts_with("events_paths_for:") => {
            let control_id = &other["events_paths_for:".len()..];
            match ctx.get_events_paths_for.as_ref() {
                Some(resolver) => resolver(control_id),
                None => Err(format!(
                    "substitute: '{{{{events_paths_for:{}}}}}' referenced but module declares no \
                     runtime.log_path_template (or DB context unavailable)",
                    control_id,
                )),
            }
        }
        // v0.2.32 (CHAINED_ACTION): {{previous_step.<field>}} resolves
        // the IMMEDIATELY PRECEDING step's response field. Only valid
        // inside a chained_action body — outside that context the
        // dispatcher never sets `ctx.previous_step`, so this token
        // errors with a clear "outside chained_action" pointer rather
        // than silently resolving to null.
        other if other.starts_with("previous_step.") => {
            let field = &other["previous_step.".len()..];
            if field.is_empty() {
                return Err(
                    "substitute: '{{previous_step.}}' must name a field (e.g. {{previous_step.local_path}})"
                        .into(),
                );
            }
            let prev = ctx.previous_step.ok_or_else(|| format!(
                "substitute: '{{{{previous_step.{}}}}}' referenced outside a chained_action \
                 (or on the FIRST step of a chain — previous_step is only available from step 2 onwards)",
                field,
            ))?;
            // Use the same top-level-only projection rule the polling
            // job_id_path enforces, so behaviour stays uniform.
            prev.get(field).cloned().ok_or_else(|| {
                format!(
                    "substitute: '{{{{previous_step.{}}}}}' — field not found in previous step's response \
                     (response: {})",
                    field, prev,
                )
            })
        }
        // v0.2.32 (CHAINED_ACTION): {{step.N.<field>}} resolves by
        // absolute index into the chain's response array. Useful when
        // step K needs to reference step (K-2)'s output rather than
        // (K-1)'s. N is zero-based — `step.0` = the first step's
        // response. Out-of-range indices error cleanly.
        other if other.starts_with("step.") => {
            let rest = &other["step.".len()..];
            // Split at the FIRST '.' — everything before is the index,
            // everything after is the field name. `step.0.local_path`
            // → idx=0, field="local_path".
            let dot = rest.find('.').ok_or_else(|| format!(
                "substitute: '{{{{step.{}}}}}' missing field segment \
                 (expected '{{{{step.N.<field>}}}}')",
                rest,
            ))?;
            let idx_str = &rest[..dot];
            let field = &rest[dot + 1..];
            if field.is_empty() {
                return Err(format!(
                    "substitute: '{{{{step.{}.}}}}' must name a field",
                    idx_str,
                ));
            }
            let idx: usize = idx_str.parse().map_err(|_| format!(
                "substitute: '{{{{step.{}.<field>}}}}' — '{}' is not a valid zero-based step index",
                idx_str, idx_str,
            ))?;
            let step_value = ctx.step_results.get(idx).ok_or_else(|| format!(
                "substitute: '{{{{step.{}.{}}}}}' — chain has only {} step results so far",
                idx, field, ctx.step_results.len(),
            ))?;
            step_value.get(field).cloned().ok_or_else(|| format!(
                "substitute: '{{{{step.{}.{}}}}}' — field not found in step {}'s response \
                 (response: {})",
                idx, field, idx, step_value,
            ))
        }
        other => Err(format!(
            "substitute: unknown placeholder '{{{{{}}}}}' (allowed: project_id, module_id, value, control:<id>, events_paths_for:<id>, previous_step.<field>, step.N.<field>)",
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
    log_path_template: Option<String>,
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
        log_path_template,
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
    log_path_template: Option<String>,
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

    // v0.2.27: `{{events_paths_for:<control_id>}}` resolver. Wired only
    // when the module's manifest declared `runtime.log_path_template`.
    // The resolver closure reads the sibling control's value (must be
    // an array of UUID strings), walks each UUID via `db.get_project`,
    // applies the template via `render_log_path_template`, and returns
    // a JSON array.
    let events_paths_resolver: Option<EventsPathsResolver> = log_path_template.map(|template| {
        let project_id_owned = project_id.to_string();
        let module_id_owned = module_id.to_string();
        let sibling_snapshot = sibling_snapshot.clone();
        let db_for_resolver = db;
        let resolver: EventsPathsResolver = Box::new(move |control_id: &str| -> Result<Value, String> {
            // Read the referenced control's value. Try the renderer
            // snapshot first (matches the {{control:<id>}} resolver's
            // ordering), then fall back to module_settings DB.
            let raw_value = match sibling_snapshot.get(control_id) {
                Some(v) => v.clone(),
                None => db_for_resolver
                    .get_setting(&project_id_owned, &module_id_owned, control_id)
                    .map_err(|e| format!(
                        "events_paths_for: DB read failed for control '{}': {}",
                        control_id, e,
                    ))?
                    .ok_or_else(|| format!(
                        "events_paths_for: control '{}' has no value (not in renderer snapshot AND no module_settings row)",
                        control_id,
                    ))?,
            };

            // Must be a JSON array.
            let uuid_array = raw_value.as_array().ok_or_else(|| format!(
                "events_paths_for: control '{}' value must be an array, got {:?}",
                control_id,
                raw_value,
            ))?;

            // Each element must be a string (project UUID).
            let mut paths: Vec<Value> = Vec::with_capacity(uuid_array.len());
            for (idx, elem) in uuid_array.iter().enumerate() {
                let uuid = elem.as_str().ok_or_else(|| format!(
                    "events_paths_for: control '{}' array element {} is not a string (expected project UUID)",
                    control_id, idx,
                ))?;
                // Walk UUID → ProjectRow to get the slug.
                let project = db_for_resolver
                    .get_project(uuid)
                    .map_err(|e| format!(
                        "events_paths_for: DB lookup for project '{}' failed: {}",
                        uuid, e,
                    ))?
                    .ok_or_else(|| format!(
                        "events_paths_for: project UUID '{}' (from control '{}') not found in DB",
                        uuid, control_id,
                    ))?;
                let path = vct_launcher_core::manifest::render_log_path_template(
                    &template, uuid, &project.slug,
                );
                paths.push(Value::String(path));
            }
            Ok(Value::Array(paths))
        });
        resolver
    });

    // Build a default SubstitutionContext used by single-step + the
    // legacy `next_action`-chain path. The chained_action path builds
    // per-step contexts so it can populate `previous_step` /
    // `step_results` between iterations.
    let default_ctx = SubstitutionContext {
        project_id,
        module_id,
        value: value_ref,
        get_control_value: resolver,
        get_events_paths_for: events_paths_resolver,
        previous_step: None,
        step_results: EMPTY_STEP_RESULTS,
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

    // v0.2.32 (CHAINED_ACTION): top-level dispatch on the action shape.
    // ChainedAction has different chaining semantics from the legacy
    // `Http { next_action: ... }` form (it threads step responses into
    // subsequent step bodies, while next_action just runs them serially
    // without inter-step data flow). Handled by a dedicated executor
    // that builds per-step contexts.
    match action {
        ActionDescriptor::ChainedAction { steps, polling, rollback_on_step_failure } => {
            execute_chained_action(
                module_id,
                project_id,
                steps,
                polling,
                rollback_on_step_failure,
                &default_ctx,
                port,
                sink,
                http_client,
            )
            .await
        }
        other_action => {
            // Legacy single-action + `next_action` chain path. Walk
            // iteratively, retaining the FIRST kick's response as the
            // return value (subsequent steps' responses fire via events
            // or are discarded). Bounded by MAX_CHAIN_STEPS.
            let mut current_action = other_action;
            let mut first_response: Option<Value> = None;
            let mut steps_walked: u32 = 0;
            loop {
                steps_walked += 1;
                if steps_walked > MAX_CHAIN_STEPS {
                    return Err(format!(
                        "module_dispatch: chain depth exceeded {} steps — refusing to continue",
                        MAX_CHAIN_STEPS,
                    ));
                }
                let (resp, next) = execute_one_step(
                    module_id,
                    project_id,
                    &current_action,
                    &default_ctx,
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
    }
}

/// v0.2.32 (CHAINED_ACTION): execute a sequence of step descriptors,
/// threading each step's response into the next step's body via the
/// `{{previous_step.<field>}}` and `{{step.N.<field>}}` placeholder
/// tokens.
///
/// Failure semantics for v0.2.32:
///   * On any step's failure, LOG via `eprintln!` (the dispatcher's
///     standard error sink — matches the polling loop's error logging)
///     and PROPAGATE the error to the caller. Previous steps' side
///     effects (downloaded files, persisted state, container DB rows)
///     are NOT rolled back — the user gets a clear "step N failed"
///     error and can retry from the point of failure.
///   * `rollback_on_step_failure: true` parses but has no effect
///     in v0.2.32. The flag is reserved for v0.2.33+ when each action
///     kind grows a rollback companion.
///
/// Polling:
///   * `polling` (if Some) attaches to the FINAL step's response.
///   * Intermediate steps' `polling` declarations (if any nested
///     within a step's own `Http` descriptor) execute normally —
///     they fire their own background polling task as usual. The
///     chained-action `polling` is layered on top of the final step.
async fn execute_chained_action(
    module_id: &str,
    project_id: &str,
    steps: Vec<ActionDescriptor>,
    chain_polling: Option<PollingSpec>,
    _rollback_on_step_failure: bool, // reserved for v0.2.33+
    parent_ctx: &SubstitutionContext<'_>,
    port: u16,
    sink: Arc<dyn EventSink>,
    http_client: &reqwest::Client,
) -> Result<Value, String> {
    if steps.is_empty() {
        return Err(
            "module_dispatch: chained_action.steps is empty (a chained_action with no steps is meaningless)"
                .into(),
        );
    }
    if steps.len() > MAX_CHAIN_STEPS as usize {
        return Err(format!(
            "module_dispatch: chained_action has {} steps, exceeds MAX_CHAIN_STEPS={}",
            steps.len(),
            MAX_CHAIN_STEPS,
        ));
    }
    // Accumulator for step responses. Indexed by step position so the
    // {{step.N.<field>}} resolver can address arbitrary prior steps.
    let mut step_results: Vec<Value> = Vec::with_capacity(steps.len());

    // The chain executes serially. We rebuild the SubstitutionContext
    // per step (cheap — it just holds references) so `previous_step` /
    // `step_results` reflect the chain's running state at substitute
    // time. The closures live on the parent_ctx and don't get cloned.
    let total_steps = steps.len();
    for (step_idx, step_action) in steps.into_iter().enumerate() {
        // Disallow nested ChainedAction-inside-ChainedAction in v0.2.32.
        // Nesting is technically expressible in the JSON shape but the
        // semantics get confusing fast (which polling block wins? which
        // previous_step is referenced? does inner failure roll back the
        // outer chain?). Reject at execute time so manifest authors get
        // a clear pointer rather than an opaque dispatch error.
        if matches!(step_action, ActionDescriptor::ChainedAction { .. }) {
            return Err(format!(
                "module_dispatch: chained_action.steps[{}] is itself a chained_action — \
                 nesting is not supported in v0.2.32 (flatten the inner chain into the outer one)",
                step_idx,
            ));
        }

        // For the final step, if a chain-level polling block was
        // declared, layer it on top of the step's own descriptor.
        // We do this by deconstructing the step's `Http` variant and
        // replacing its `polling` field with the chain-level spec
        // (overwriting any step-level polling on the final step, which
        // would be redundant — the chain owns the user-visible
        // polling concern).
        let is_final = step_idx + 1 == total_steps;
        let step_action = if is_final && chain_polling.is_some() {
            attach_chain_polling_to_final_step(step_action, chain_polling.clone())?
        } else {
            step_action
        };

        // Execute the step inside a scoped block so the per-step
        // SubstitutionContext (which holds an immutable borrow of
        // `step_results`) drops BEFORE we push the new response.
        // Otherwise the borrow checker rejects the push as a
        // mutable-while-immutable conflict.
        let (resp, next_in_step) = {
            // Build a per-step SubstitutionContext. The previous_step
            // reference is the last response in `step_results` (or
            // None on the first step). step_results is the whole
            // accumulator slice the {{step.N.<field>}} resolver
            // indexes into.
            let previous_step = step_results.last();
            let step_ctx = SubstitutionContext {
                project_id: parent_ctx.project_id,
                module_id: parent_ctx.module_id,
                value: parent_ctx.value,
                // Reuse the parent's resolvers via thin closures that
                // delegate. Box::new'ing a closure that captures
                // &mut refs to closures isn't viable, so we just
                // rebuild by-reference accessors that punt through
                // to the parent.
                get_control_value: {
                    let parent = &parent_ctx.get_control_value;
                    Box::new(move |id| parent(id))
                },
                get_events_paths_for: parent_ctx.get_events_paths_for.as_ref().map(|parent| {
                    let resolver: EventsPathsResolver = Box::new(move |id: &str| parent(id));
                    resolver
                }),
                previous_step,
                step_results: &step_results,
            };

            execute_one_step(
                module_id,
                project_id,
                &step_action,
                &step_ctx,
                port,
                sink.clone(),
                http_client,
            )
            .await
            .map_err(|e| {
                // v0.2.32 failure-mode contract: log + propagate. The
                // {step_idx + 1} formatting is one-based for user-
                // facing error clarity (matches "step 2 failed" in
                // toasts).
                eprintln!(
                    "[module_dispatch] chained_action step {} of {} failed: {}",
                    step_idx + 1,
                    total_steps,
                    e,
                );
                format!("chained_action step {} of {} failed: {}", step_idx + 1, total_steps, e)
            })?
            // step_ctx dropped here — releases the immutable borrow.
        };

        // A step's OWN `next_action` chain (the legacy Http-level
        // chain) is forbidden inside a chained_action because the
        // semantics overlap with the outer chain (both run more
        // actions, but only the outer chain threads responses). If a
        // step's Http descriptor declares its own next_action, we
        // refuse rather than execute it silently — keeps the data
        // flow story unambiguous.
        if next_in_step.is_some() {
            return Err(format!(
                "module_dispatch: chained_action.steps[{}] has its own next_action — \
                 use a single flat chained_action.steps array instead of nesting next_action chains \
                 (response data flow would be ambiguous)",
                step_idx,
            ));
        }

        step_results.push(resp);
    }

    // Return the LAST step's response (matches the orchestrator's
    // expectation that the chain's "result" = the result of its
    // final step). For chains with polling, this is the kick body of
    // the final step; the actual long-running job's progress flows
    // via the polling-event channel.
    step_results.pop().ok_or_else(|| {
        // Defensive: shouldn't reach here (we error on empty above)
        // but keep the compiler happy without unwrap.
        "module_dispatch: chained_action produced no step results (empty steps?)".into()
    })
}

/// v0.2.32 (CHAINED_ACTION): attach a chain-level `polling` block to
/// the final step's `Http` descriptor, OVERWRITING any step-level
/// polling that step may already declare (the chain owns the
/// user-visible long-running progress concern on the final step).
///
/// For non-`Http` final steps (e.g. a hypothetical future `Tauri`
/// step kind), polling currently has no meaning — we error rather
/// than silently drop the polling block.
fn attach_chain_polling_to_final_step(
    step: ActionDescriptor,
    chain_polling: Option<PollingSpec>,
) -> Result<ActionDescriptor, String> {
    match step {
        ActionDescriptor::Http {
            method,
            path,
            body,
            polling: _step_polling, // intentionally discarded — chain owns the polling now
            next_action,
        } => Ok(ActionDescriptor::Http {
            method,
            path,
            body,
            polling: chain_polling,
            next_action,
        }),
        ActionDescriptor::ChainedAction { .. } => Err(
            // Defensive: the loop already rejects nested chained_action
            // before this point, but pattern-completeness makes the
            // compiler happy AND surfaces a clear error if a future
            // refactor accidentally inverts the order of checks.
            "module_dispatch: chain-level polling cannot attach to a nested chained_action final step"
                .into(),
        ),
    }
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
        // v0.2.32 (CHAINED_ACTION): `execute_one_step` is the single-
        // step executor used by the legacy `Http { next_action: ... }`
        // chain path AND by the chained-action loop. The chained-action
        // loop itself decomposes its top-level descriptor BEFORE
        // calling here (so each iteration's `action` is one of the
        // inner steps), and it also rejects nested chained_action
        // upstream. So reaching this arm means a code-path invariant
        // was violated — return a clear error rather than silently
        // returning Ok with a dummy response.
        ActionDescriptor::ChainedAction { .. } => Err(
            "module_dispatch: internal — execute_one_step received a ChainedAction \
             (the dispatch loop should have intercepted it; this is a bug)"
                .into(),
        ),
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

    // v0.2.27: load the module's manifest (best-effort) to extract
    // `runtime.log_path_template`. Used by the dispatcher's
    // `{{events_paths_for:<control_id>}}` resolver. Soft-fail: if the
    // manifest can't be loaded, the resolver simply isn't wired and any
    // use of the token returns a clear "events_paths_for unavailable"
    // error rather than poisoning the dispatch entirely.
    let log_path_template = load_module_log_path_template(db.inner(), &project_id, &module_id);

    dispatch_action_inner(
        &module_id,
        &project_id,
        action,
        value,
        sibling_values,
        log_path_template,
        app,
        db.inner(),
        &http_client,
    )
    .await
}

/// v0.2.27: load a module's `runtime.log_path_template` from its
/// installed `vct-module.json`. Returns None on any failure (module not
/// installed, manifest missing, parse error, template not set) — the
/// dispatcher handles None as "events_paths_for unavailable" and rejects
/// the token cleanly at dispatch time.
fn load_module_log_path_template(db: &Db, project_id: &str, module_id: &str) -> Option<String> {
    let install_row = db.get_module_install(project_id, module_id).ok().flatten()?;
    let manifest_path = std::path::Path::new(&install_row.install_path).join("vct-module.json");
    let raw = std::fs::read_to_string(&manifest_path).ok()?;
    let manifest = vct_launcher_core::manifest::ModuleManifest::from_json(&raw).ok()?;
    manifest.runtime.log_path_template
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
            get_events_paths_for: None,
            previous_step: None,
            step_results: EMPTY_STEP_RESULTS,
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

    // ─── v0.2.27: {{events_paths_for:<control_id>}} ──────────────────

    /// Helper: build a SubstitutionContext with a synthetic
    /// events_paths_for resolver that mimics the DB-backed production
    /// resolver without needing a real Db. The closure parameter is
    /// the resolver behaviour the test wants to exercise.
    fn ctx_with_events_paths_resolver<'a, F>(resolver: F) -> SubstitutionContext<'a>
    where
        F: Fn(&str) -> Result<Value, String> + Send + Sync + 'a,
    {
        SubstitutionContext {
            project_id: "proj-A",
            module_id: "mod-X",
            value: None,
            get_control_value: Box::new(|_| None),
            get_events_paths_for: Some(Box::new(resolver)),
            previous_step: None,
            step_results: EMPTY_STEP_RESULTS,
        }
    }

    /// Happy path: token resolves to an array of paths via the closure.
    /// The dispatcher's production resolver does this by walking UUIDs
    /// through `db.get_project()` and applying `render_log_path_template`;
    /// here the closure just returns a pre-built array.
    #[test]
    fn substitute_events_paths_for_resolves_to_array() {
        let v = json!("{{events_paths_for:src_projects}}");
        let ctx = ctx_with_events_paths_resolver(|control_id| {
            assert_eq!(control_id, "src_projects");
            Ok(json!([
                "/data/logs/rl_events_proj1.jsonl",
                "/data/logs/rl_events_proj2.jsonl",
            ]))
        });
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(
            out,
            json!([
                "/data/logs/rl_events_proj1.jsonl",
                "/data/logs/rl_events_proj2.jsonl",
            ]),
        );
    }

    /// Error: module declares no log_path_template (resolver is None).
    /// Token in body → clear "events_paths_for unavailable" error.
    #[test]
    fn substitute_events_paths_for_no_resolver_errors() {
        let v = json!("{{events_paths_for:src_projects}}");
        let ctx = SubstitutionContext {
            project_id: "proj-A",
            module_id: "mod-X",
            value: None,
            get_control_value: Box::new(|_| None),
            get_events_paths_for: None,
            previous_step: None,
            step_results: EMPTY_STEP_RESULTS,
        };
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(err.contains("events_paths_for"), "got: {}", err);
        assert!(err.contains("log_path_template"), "got: {}", err);
    }

    /// Error: resolver returns Err with a clear message (control id
    /// unknown, value not an array, UUID not in DB — these are all
    /// inside-resolver decisions that bubble up unchanged).
    #[test]
    fn substitute_events_paths_for_resolver_error_propagates() {
        let v = json!("{{events_paths_for:src_projects}}");
        let ctx = ctx_with_events_paths_resolver(|_| {
            Err("control 'src_projects' value is not an array".to_string())
        });
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(err.contains("not an array"), "got: {}", err);
    }

    /// Whole-string-only: embedding `{{events_paths_for:<id>}}` inside
    /// a longer string is rejected at dispatch time because the token
    /// resolves to a JSON array (can't be stringified into a longer
    /// string meaningfully).
    #[test]
    fn substitute_events_paths_for_rejects_embedded_form() {
        let v = json!("paths: {{events_paths_for:src_projects}} (count varies)");
        let ctx = ctx_with_events_paths_resolver(|_| Ok(json!([])));
        let err = substitute(&v, &ctx).unwrap_err();
        // Should reject BEFORE calling the resolver — error message
        // names the token and explains the whole-string requirement.
        assert!(err.contains("events_paths_for"), "got: {}", err);
        assert!(err.contains("WHOLE string"), "got: {}", err);
    }

    /// Recursion into nested objects: a body field that is exactly
    /// the token resolves to the array, even when nested deeply.
    #[test]
    fn substitute_events_paths_for_inside_nested_object() {
        let v = json!({
            "mode": "offline",
            "options": {
                "project_ids": "{{events_paths_for:src_projects}}",
                "max_epochs": 3,
            },
        });
        let ctx = ctx_with_events_paths_resolver(|_| {
            Ok(json!(["/data/a.jsonl", "/data/b.jsonl"]))
        });
        let out = substitute(&v, &ctx).unwrap();
        assert_eq!(out["options"]["project_ids"], json!(["/data/a.jsonl", "/data/b.jsonl"]));
        assert_eq!(out["mode"], json!("offline"));
        assert_eq!(out["options"]["max_epochs"], json!(3));
    }

    /// Unknown token form starting with `events_` but not the canonical
    /// `events_paths_for:` prefix falls through to the "unknown placeholder"
    /// branch with a clear error.
    #[test]
    fn substitute_unknown_events_prefix_errors() {
        let v = json!("{{events_for_paths:src_projects}}");
        let ctx = ctx_with_events_paths_resolver(|_| Ok(json!([])));
        let err = substitute(&v, &ctx).unwrap_err();
        assert!(err.contains("unknown placeholder"), "got: {}", err);
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
            None,
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
            module_id, project_id, action, None, None, None, sink, &db, &client,
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

        dispatch_action_with_sink(module_id, project_id, action, None, None, None, sink_arc, &db, &client)
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

        dispatch_action_with_sink(module_id, project_id, action, None, None, None, sink_arc, &db, &client)
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

        dispatch_action_with_sink(module_id, project_id, action, None, None, None, sink_arc, &db, &client)
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

        let err = dispatch_action_with_sink(module_id, project_id, head, None, None, None, sink, &db, &client)
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
        let err = dispatch_action_with_sink("ghost-module", "proj-X", action, None, None, None, sink, &db, &client)
            .await
            .expect_err("missing port must error");
        assert!(
            err.contains("no port for project") && err.contains("ghost-module"),
            "expected diagnostic mentioning module + project, got: {}",
            err,
        );
    }

    // ─── v0.2.32 (CHAINED_ACTION, 2026-05-24): chained_action executor ───
    //
    // The chained_action primitive executes a sequence of step
    // descriptors serially, threading each step's response into the
    // next step's body via the `{{previous_step.<field>}}` token. The
    // tests below pin the three load-bearing invariants:
    //
    //   1. Two-step chain threads previous_step.local_path correctly.
    //   2. Chain-level polling attaches to the FINAL step only.
    //   3. Second-step failure preserves the first step's effect (no
    //      auto-rollback in v0.2.32 — the user retries from step 2).

    /// chained_action with two steps where step 2 references
    /// `{{previous_step.local_path}}` from step 1's response. Verifies
    /// the substitution threading end-to-end.
    #[tokio::test]
    async fn chained_action_two_step_threads_previous_step_local_path() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        // Capture the body step 2 receives so we can assert the
        // {{previous_step.local_path}} substitution worked.
        let received_step2_body: Arc<StdMutex<Option<Value>>> = Arc::new(StdMutex::new(None));
        let body_capture = received_step2_body.clone();

        let router = Router::new()
            .route(
                "/download_default",
                routing::post(|| async {
                    // Step 1 response — exactly what Agent J's
                    // module_download_default_weights would return.
                    Json(json!({"local_path": "/data/weights/v0.2.6/arctic_1024.pt", "version": "0.2.6"}))
                }),
            )
            .route(
                "/finetune",
                routing::post(move |Json(body): Json<Value>| {
                    let cap = body_capture.clone();
                    async move {
                        *cap.lock().unwrap() = Some(body);
                        Json(json!({"job_id": "ft-job-1"}))
                    }
                }),
            );
        let port = start_server(router).await;

        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::ChainedAction {
            steps: vec![
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/download_default".into(),
                    body: None,
                    polling: None,
                    next_action: None,
                },
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/finetune".into(),
                    body: Some(json!({
                        "mode": "offline",
                        "starting_checkpoint": "{{previous_step.local_path}}",
                    })),
                    polling: None,
                    next_action: None,
                },
            ],
            polling: None,
            rollback_on_step_failure: false,
        };

        let resp = dispatch_action_with_sink(
            module_id, project_id, action, None, None, None, sink, &db, &client,
        )
        .await
        .expect("chained_action dispatch ok");

        // Return value is the FINAL step's response (chain semantics:
        // the chain's result = the final step's kick body).
        assert_eq!(resp["job_id"], json!("ft-job-1"));

        // Step 2's body MUST contain the substituted local_path from
        // step 1's response, not the literal placeholder string.
        let step2_body = received_step2_body.lock().unwrap().clone().expect("step 2 body captured");
        assert_eq!(
            step2_body["starting_checkpoint"],
            json!("/data/weights/v0.2.6/arctic_1024.pt"),
            "{{previous_step.local_path}} must substitute step 1's local_path",
        );
        assert_eq!(step2_body["mode"], json!("offline"), "literal body fields preserved");
    }

    /// Chain-level polling block attaches to the FINAL step ONLY. We
    /// verify by sending a chain whose final step's kick response
    /// includes a job_id, then waiting for the polling endpoint to fire
    /// terminal=done. The earlier step does NOT carry polling; if the
    /// dispatcher mis-attached the polling to the wrong step, the test
    /// would see polling hit the wrong endpoint or no polling at all.
    #[tokio::test]
    async fn chained_action_polling_attaches_to_last_step() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let poll_counter = Arc::new(StdMutex::new(0u32));
        let poll_counter_clone = poll_counter.clone();

        let router = Router::new()
            .route(
                "/quick_step",
                routing::post(|| async { Json(json!({"step": "quick", "ok": true})) }),
            )
            .route(
                "/long_step",
                routing::post(|| async {
                    // Final step returns the job_id the chain-level
                    // polling spec will track.
                    Json(json!({"job_id": "chain-final-job"}))
                }),
            )
            .route(
                "/chain_status",
                routing::get(move |Query(_): Query<HashMap<String, String>>| {
                    let c = poll_counter_clone.clone();
                    async move {
                        let mut n = c.lock().unwrap();
                        *n += 1;
                        // Done after a few ticks.
                        let state = if *n >= 2 { "done" } else { "running" };
                        Json(json!({"state": state, "tick": *n}))
                    }
                }),
            );
        let port = start_server(router).await;
        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink = RecordingSink::new();
        let sink_arc: Arc<dyn EventSink> = Arc::new(sink.clone());

        let action = ActionDescriptor::ChainedAction {
            steps: vec![
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/quick_step".into(),
                    body: None,
                    polling: None,
                    next_action: None,
                },
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/long_step".into(),
                    body: None,
                    polling: None, // chain-level polling layered on
                    next_action: None,
                },
            ],
            polling: Some(PollingSpec {
                endpoint: "/chain_status".into(),
                job_id_path: "$.job_id".into(),
                job_id_query_param: "job_id".into(),
                interval_seconds: 0,
                max_attempts: 10,
                terminal_state_field: "$.state".into(),
                terminal_success_values: vec!["done".into()],
                terminal_failure_values: vec!["failed".into()],
                progress_event: "test://chain-progress".into(),
                failed_event: "test://chain-failed".into(),
            }),
            rollback_on_step_failure: false,
        };

        let resp = dispatch_action_with_sink(
            module_id, project_id, action, None, None, None, sink_arc, &db, &client,
        )
        .await
        .expect("dispatch ok");

        // Return value is the FINAL step's kick response, which had
        // the job_id (because polling was layered onto step 2).
        assert_eq!(resp["job_id"], json!("chain-final-job"));

        // Wait for the polling loop to converge.
        tokio::time::sleep(Duration::from_millis(1500)).await;

        let events = sink.snapshot();
        let progress: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://chain-progress")
            .collect();
        let failed: Vec<_> = events
            .iter()
            .filter(|(e, _)| e == "test://chain-failed")
            .collect();
        assert!(
            !progress.is_empty(),
            "chain-level polling must produce ≥1 progress event on the final step, got events: {:?}",
            events,
        );
        assert!(failed.is_empty(), "no failed event expected on success path");

        // The progress event's response shape carries `state` / `tick`
        // from /chain_status, confirming the poller hit the right
        // endpoint (i.e. polling was correctly attached to the final
        // step's kick response).
        let last_progress = progress.last().expect("≥1 progress");
        assert_eq!(last_progress.1["state"], json!("done"));
    }

    /// Step 2 failure does NOT roll back step 1's effect (v0.2.32 has
    /// no automatic rollback). The first step's side effect must
    /// remain observable AFTER the chain returns its error.
    ///
    /// We simulate "step 1 effect" with a side-effect counter the
    /// server increments on /step1_with_effect; the assertion is that
    /// counter is exactly 1 after step 2 returns 500 — step 1 ran AND
    /// was not undone.
    #[tokio::test]
    async fn chained_action_second_step_failure_preserves_first_step_result() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let side_effect_counter = Arc::new(StdMutex::new(0u32));
        let counter_for_step1 = side_effect_counter.clone();

        let router = Router::new()
            .route(
                "/step1_with_effect",
                routing::post(move || {
                    let c = counter_for_step1.clone();
                    async move {
                        *c.lock().unwrap() += 1;
                        Json(json!({"step": "first", "effect_applied": true}))
                    }
                }),
            )
            .route(
                "/step2_fails",
                routing::post(|| async {
                    // Always 500 — step 2 fails.
                    (axum::http::StatusCode::INTERNAL_SERVER_ERROR, "step 2 broken").into_response()
                }),
            );
        let port = start_server(router).await;
        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::ChainedAction {
            steps: vec![
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/step1_with_effect".into(),
                    body: None,
                    polling: None,
                    next_action: None,
                },
                ActionDescriptor::Http {
                    method: HttpMethod::Post,
                    path: "/step2_fails".into(),
                    body: None,
                    polling: None,
                    next_action: None,
                },
            ],
            polling: None,
            rollback_on_step_failure: false,
        };

        let err = dispatch_action_with_sink(
            module_id, project_id, action, None, None, None, sink, &db, &client,
        )
        .await
        .expect_err("step 2 must fail and propagate");

        // Error message names the failing step number (1-based) and
        // total count.
        assert!(
            err.contains("step 2 of 2") || err.contains("step 2"),
            "error must name failing step, got: {}",
            err,
        );

        // Step 1's side effect is preserved (counter incremented
        // exactly once, NOT rolled back even though step 2 failed).
        let final_count = *side_effect_counter.lock().unwrap();
        assert_eq!(
            final_count, 1,
            "step 1 ran exactly once and its effect is NOT rolled back \
             on step 2 failure (v0.2.32 has no auto-rollback)",
        );
    }

    /// Empty `steps` is rejected at execute time — a chained_action
    /// with zero steps is meaningless and almost certainly a manifest
    /// authoring bug.
    #[tokio::test]
    async fn chained_action_empty_steps_errors() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new();
        let port = start_server(router).await;
        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        let action = ActionDescriptor::ChainedAction {
            steps: vec![],
            polling: None,
            rollback_on_step_failure: false,
        };

        let err = dispatch_action_with_sink(
            module_id, project_id, action, None, None, None, sink, &db, &client,
        )
        .await
        .expect_err("empty steps must error");
        assert!(err.contains("empty"), "expected 'empty' in error, got: {}", err);
    }

    /// {{previous_step.<field>}} OUTSIDE a chained_action (i.e.
    /// referenced inside a single-step dispatch) errors cleanly. The
    /// dispatcher must not silently resolve it to null — manifests
    /// that use the token must do so inside a chain.
    #[tokio::test]
    async fn previous_step_token_outside_chain_errors() {
        let project_id = "proj-A";
        let module_id = "mod-X";

        let router = Router::new().route("/x", routing::post(|| async { Json(json!({})) }));
        let port = start_server(router).await;
        let db = db_with_module(project_id, module_id, port);
        let client = build_http_client();
        let sink: Arc<dyn EventSink> = Arc::new(RecordingSink::new());

        // Single-step Http with {{previous_step.foo}} in the body —
        // the resolver should reject because previous_step is None
        // outside a chained_action context.
        let action = ActionDescriptor::Http {
            method: HttpMethod::Post,
            path: "/x".into(),
            body: Some(json!({"ref": "{{previous_step.foo}}"})),
            polling: None,
            next_action: None,
        };
        let err = dispatch_action_with_sink(
            module_id, project_id, action, None, None, None, sink, &db, &client,
        )
        .await
        .expect_err("previous_step outside chain must error");
        assert!(
            err.contains("previous_step") && err.contains("chained_action"),
            "expected 'previous_step' + 'chained_action' in error, got: {}",
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
