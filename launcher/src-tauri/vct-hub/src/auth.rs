//! Hub authentication: localhost auth-token gate.
//!
//! ─── Why this exists ────────────────────────────────────────────────
//!
//! The hub binds `0.0.0.0:7700` (v0.2.61, Option H — see the bind-site
//! comment in `server::start_hub_server` for the full rationale). It must
//! be reachable from a global module's CONTAINER network namespace, and
//! `host.containers.internal` maps to a different host address per
//! container runtime, so listening on all interfaces is the only
//! runtime-agnostic way to be reachable — with the BEARER TOKEN, not the
//! bind address, as the access control. Without authentication, ANY
//! process that can reach the port could curl
//! `http://<host>:7700/api/v1/projects/<id>/env` and exfiltrate every
//! secret the launcher's keychain has marked active for that project.
//! This is a real attack class (rogue `npm install`, `pip install`,
//! `cargo install`, browser extension calling fetch on localhost, etc.)
//! that has hit other localhost-bound daemons (Docker, Bun's dev server,
//! several CI-injected typosquats) — and, post-0.0.0.0, also a same-LAN
//! peer. The token gate is what stops all of them.
//!
//! The fix: every hub startup generates a fresh 32-byte token from the
//! OS CSPRNG, persists it to `<vct_root_dir>/hub.token` (mode 0o600 on
//! Unix), and requires `Authorization: Bearer <token>` on every
//! request that touches state. Same-user processes that legitimately
//! talk to the hub (the launcher GUI, the `vco` CLI, the resolver
//! helper used by bundled MCP wrappers) read the token file fresh and
//! authenticate transparently. Processes that DON'T have read access
//! to `hub.token` (different user — different home dir, different
//! umask, different keychain) get 401.
//!
//! ─── Design choices ─────────────────────────────────────────────────
//!
//! * **Regenerate every startup, not persistent.** A long-lived token
//!   widens the attack window if a rogue process briefly manages to
//!   read `hub.token` (e.g. a misconfigured tar/zip created with
//!   permissive perms, a build artifact accidentally committed). Fresh
//!   tokens mean the only window is "from launcher start to first
//!   client request" — vanishingly small.
//!
//! * **Token file mode 0o600 on Unix.** Standard for secret material in
//!   `$HOME`. Same posture as `~/.aws/credentials`,
//!   `~/.netrc`, `~/.ssh/id_rsa`. Windows: rely on default ACL
//!   (`hub.token` lands in the user's `%APPDATA%`-equivalent, which is
//!   already same-user-readable by default — Windows doesn't have a
//!   trivial chmod-600 equivalent without `icacls` + heavy lifting,
//!   and the same-user-only ACL is our threat boundary anyway).
//!
//! * **Health endpoint exempt.** `/api/v1/health` returns no secrets,
//!   only `{"status":"ok",...}`. Existing in-process clients
//!   (`hub_proxy::hub_info`) call it as a liveness check before
//!   attempting authenticated calls; gating health on the token would
//!   force every liveness-check caller to read `hub.token` first,
//!   which is wasteful and gives attackers the same probe ability via
//!   401 vs 503 timing anyway. Keep health unguarded; gate everything
//!   else.
//!
//! * **OPTIONS preflight exempt.** Browsers send OPTIONS without an
//!   Authorization header for CORS preflight; rejecting them with 401
//!   would break any future browser-side hub client. The actual
//!   request that follows IS gated. CorsLayer already returns
//!   appropriate headers for OPTIONS, so the middleware just lets it
//!   through to the layer below.
//!
//! * **Constant-time comparison.** Token comparison uses
//!   `subtle::ConstantTimeEq`-style logic via byte-by-byte XOR
//!   accumulation to avoid leaking token-prefix-match info through
//!   timing. Without `subtle` as a dep we hand-roll the same pattern;
//!   the implementation is short (single `fold` over a zip).
//!
//! ─── What this does NOT defend against ─────────────────────────────
//!
//! * Same-user attacker with arbitrary code execution — they can read
//!   `hub.token` directly. The OS-keychain values that the hub
//!   serves are derived from the OS keychain anyway, which the same
//!   attacker can also dump (libsecret, Windows Credential Manager,
//!   macOS Keychain are all per-user). The auth gate raises the bar
//!   from "any package in the dependency tree can curl localhost" to
//!   "the package needs to actively read `~/.vct/hub.token`" — same
//!   protection level as other localhost daemons (Docker socket
//!   permissions, etc.).
//!
//! * Network adversary — since v0.2.61 the hub binds `0.0.0.0` (so a
//!   global module's container can reach it; see `server::start_hub_server`),
//!   so a same-LAN peer CAN now reach the port. The bearer-token gate is
//!   what stops them: every `/api/v1/*` route requires the 256-bit
//!   `hub.token` (or, for module routes, the per-module ephemeral token),
//!   so a network peer without the token gets 401 exactly like an
//!   unauthorized local process. The token, not the bind address, is the
//!   boundary. (A LAN attacker who can ALSO read `<vct_root_dir>/hub.token`
//!   off this host is already the same-user-RCE case above.)

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use axum::{
    body::Body,
    extract::Request,
    http::{HeaderMap, Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};

use crate::modules_api::LauncherDbHandle;
use crate::project_tokens::{self, ProjectTokenRegistry};
// v0.2.54 Track I: the token primitives (CSPRNG generation, 0o600
// persistence, constant-time compare, Bearer parsing) moved to
// `vct_launcher_core::services::boot_token` so the launcher's diagrams
// local server (diagrams.token) shares the SAME implementation instead
// of growing a drifting copy. This module keeps its public API
// (generate_token, write_token_file, TOKEN_BYTES, the middleware) and
// delegates the primitives.
use vct_launcher_core::services::boot_token;

/// Length in bytes of the auth token we generate. 32 bytes = 256 bits;
/// hex-encoded → 64 chars. Standard length for opaque session tokens
/// (matches GitHub PAT classic, Vercel access tokens, etc.).
pub const TOKEN_BYTES: usize = boot_token::TOKEN_BYTES;

/// Filename inside `vct_root_dir()` where the token persists for the
/// lifetime of the hub process. Clients (resolver helpers, vct-cli,
/// hub_proxy) read this file fresh on every call.
pub const TOKEN_FILE: &str = "hub.token";

/// Generate a fresh, cryptographically-random hex token (64 hex chars
/// for 32 OS-CSPRNG bytes). Delegates to `boot_token::generate_token`.
pub fn generate_token() -> Result<String, String> {
    boot_token::generate_token()
}

/// Path to the token file under the launcher's state-root.
fn token_path() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join(TOKEN_FILE)
}

/// Persist the token to `<vct_root_dir>/hub.token` with mode 0o600 on
/// Unix (default same-user ACL on Windows). Delegates to
/// `boot_token::write_token_file` — see that function for the
/// TOCTOU-free open-with-mode sequence and the Windows ACL rationale.
pub fn write_token_file(token: &str) -> Result<(), String> {
    boot_token::write_token_file(&token_path(), token)
}

/// Shared authentication state injected as an axum extension.
///
/// `Arc<String>` so the middleware closure can `clone()` cheaply (one
/// atomic increment per request) without copying the 64-byte token.
#[derive(Clone)]
pub struct AuthState {
    pub token: Arc<String>,
}

impl AuthState {
    pub fn new(token: String) -> Self {
        Self {
            token: Arc::new(token),
        }
    }
}

/// Constant-time compare of two byte slices. Delegates to
/// `boot_token::constant_time_eq` (accumulator pattern — no early
/// exit, no prefix-length timing leak).
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    boot_token::constant_time_eq(a, b)
}

/// Extract the bearer token from `Authorization: Bearer <token>`.
///
/// Returns `None` if the header is missing, malformed, not a Bearer
/// scheme, or empty after the prefix. The HeaderMap lookup stays here
/// (boot_token is HTTP-library-free); the scheme parsing delegates to
/// `boot_token::parse_bearer` (case-insensitive scheme per RFC 7235
/// §2.1, case-sensitive token).
fn extract_bearer_token(headers: &HeaderMap) -> Option<&str> {
    let raw = headers.get(axum::http::header::AUTHORIZATION)?.to_str().ok()?;
    boot_token::parse_bearer(raw)
}

/// Whether a request path is exempt from the hub-wide bearer-token gate.
///
/// Three carve-outs:
///   * `/api/v1/health` — liveness probe, returns no secrets. Existing
///     `hub_proxy::hub_info` uses it as a same-user reachability test.
///   * `/api/v1/modules/{id}/db/...` — module-owned DB row endpoints.
///     These have their OWN bearer-scope check in
///     `module_db_api::require_module_scope` against the per-(module,
///     project) shared secret in `module_access_tokens` — the hub-wide
///     `hub.token` is the LAUNCHER's auth surface, not the container's.
///   * `/api/v1/modules/{id}/token/refresh` — same posture, exempted
///     for the same reason (the container has its scoped secret, not
///     hub.token).
///   * `/api/v1/modules/{id}/projects/{pid}/rl/events` — v0.2.61
///     (Option H): the GLOBAL RL container reads its own per-project
///     event corpus through the hub with a per-MODULE IDENTITY token
///     (minted by `module_identity`), NOT hub.token. Like the `db` /
///     `token` routes, this carve-out hands the bearer down to
///     `module_db_api::require_module_scope`, which now validates BOTH
///     the DB `module_access_tokens` path AND the ephemeral
///     identity-token path. We match the EXACT `rl/events` tail (2nd
///     segment `projects`, last two `rl` + `events`) — we do NOT
///     blanket-exempt all `.../projects/...`, which would open any
///     future `projects`-prefixed route to the outer-gate bypass.
///   * Anything not under `/api/v1/` — there's nothing else mounted
///     today, but if someone adds a `/static/*` route later we don't
///     want an empty-Authorization 401 to leak through. Auth applies
///     to the API surface; non-API paths get whatever the routing
///     layer decides (usually 404).
fn is_exempt_path(path: &str) -> bool {
    if path == "/api/v1/health" {
        return true;
    }
    // Module-owned DB endpoints: handled by module_db_api's own
    // bearer-scope middleware. Match patterns:
    //   /api/v1/modules/{module_id}/db/projects/{project_id}/rows/...
    //   /api/v1/modules/{module_id}/token/refresh
    //   /api/v1/modules/{module_id}/projects/{project_id}/rl/events
    if let Some(rest) = path.strip_prefix("/api/v1/modules/") {
        // rest like "vct-rl-reranker/db/projects/.../rows/...",
        //          "vct-rl-reranker/token/refresh", or
        //          "vct-rl-reranker/projects/{pid}/rl/events".
        let parts: Vec<&str> = rest.split('/').collect();
        if parts.len() < 2 {
            return false;
        }
        // parts[0] == module_id; parts[1] is the route family.
        //
        // v0.2.61 (Option H C-EXEMPT): match the EXACT registered shapes,
        // NOT the whole `db`/`token` route FAMILY. The old `parts[1]=="db"
        // || parts[1]=="token"` blanket-exempted any future suffix under
        // those prefixes from the outer hub.token gate — so a later route
        // added under `db/`/`token/` WITHOUT its own require_module_scope
        // layer would silently inherit the bypass (a latent 0.0.0.0-exposure
        // footgun). Tighten to the shapes module_db_api::router actually
        // registers:
        //   db   →  db/projects/{pid}/rows/{table}        (insert/list)
        //           db/projects/{pid}/rows/{table}/{key}  (get/patch/delete)
        //   token → token/refresh
        // (a trailing empty segment from a "…/" path is tolerated). Same
        // exact-tail discipline the rl/events arm below already uses.
        let trimmed_len = if parts.last() == Some(&"") { parts.len() - 1 } else { parts.len() };
        if parts[1] == "db"
            && trimmed_len >= 5
            && parts[2] == "projects"
            && parts[4] == "rows"
            && (trimmed_len == 6 || trimmed_len == 7)
        {
            // db/projects/{pid}/rows/{table}[/{key}]
            return true;
        }
        if parts[1] == "token" && trimmed_len == 3 && parts[2] == "refresh" {
            return true;
        }
        // v0.2.61 (Option H): exempt the EXACT rl/events shape only.
        // parts: [module_id, "projects", project_id, "rl", "events"]
        // (an optional trailing empty segment from a "…/events/" path
        // is tolerated, but no deeper path is). We deliberately do NOT
        // match on parts[1] == "projects" alone — only the precise
        // rl/events tail — so a future `/modules/{id}/projects/{pid}/x`
        // route does not inherit this outer-gate bypass.
        if (parts.len() == 5 || (parts.len() == 6 && parts[5].is_empty()))
            && parts[1] == "projects"
            && parts[3] == "rl"
            && parts[4] == "events"
        {
            return true;
        }
        // v0.2.61 (RL config-readback): exempt the EXACT per-project config
        // shape only — parts: [module_id, "projects", project_id, "config"]
        // (trailing empty segment tolerated). Same exact-tail discipline as
        // rl/events — NOT a blanket /projects/ match. require_module_scope
        // (identity-token, GET-only, same-module-same-pid) is the real gate;
        // the handler returns only the module's own module_settings map.
        if (parts.len() == 4 || (parts.len() == 5 && parts[4].is_empty()))
            && parts[1] == "projects"
            && parts[3] == "config"
        {
            return true;
        }
    }
    false
}

/// The per-project resolver routes that accept EITHER the global
/// `hub.token` (compat) OR the matching per-project token (v0.2.76
/// Part 4). Both carry a `{project_id}` in the SAME URL position, so one
/// parser serves both.
///
/// Returns `Some(project_id)` when `path` is exactly
/// `/api/v1/projects/{project_id}/env` or
/// `/api/v1/projects/{project_id}/config` (a trailing slash is
/// tolerated). Returns `None` for every other path — including the
/// module-scoped `/api/v1/modules/.../projects/.../config` route, which
/// starts with `/api/v1/modules/` (handled by `module_db_api`), not
/// `/api/v1/projects/`.
///
/// EXACT-tail discipline (same as the module exempt-path matcher): we
/// match only the precise `env` / `config` leaf so a future
/// `/api/v1/projects/{id}/env/sub` route can't inherit the per-project
/// token path by accident.
pub(crate) fn per_project_token_route(path: &str) -> Option<&str> {
    let rest = path.strip_prefix("/api/v1/projects/")?;
    // rest like "{project_id}/env" or "{project_id}/config" (optional
    // trailing slash). Split and tolerate a trailing empty segment.
    let mut parts: Vec<&str> = rest.split('/').collect();
    if parts.last() == Some(&"") {
        parts.pop();
    }
    if parts.len() != 2 {
        return None;
    }
    let project_id = parts[0];
    if project_id.is_empty() {
        return None;
    }
    match parts[1] {
        "env" | "config" => Some(project_id),
        _ => None,
    }
}

/// Whether the (now opt-IN) compat window accepts the global `hub.token`
/// on the per-project `/env` + `/config` routes.
///
/// ─── DEFAULT FLIPPED (v0.2.77 Part 8) ───────────────────────────────
/// Introduced in v0.2.76 Part 4 as DEFAULT-allow (a one-release compat
/// window). As of v0.2.77 the default is FLIPPED to DENY: an UNSET or
/// unrecognised `VCT_HUB_LEGACY_GLOBAL_ENV` now REFUSES the global token
/// on these two routes. The per-project scoped token
/// (`hub.token.<project_id>`) is required — the bundled resolver triplet
/// already prefers it (v0.2.76 Part 4 Task 3), the hub lazy-mints a
/// scoped token for projects added mid-session (Part 8 Task 4a), and the
/// auth layer canonicalizes id-or-slug before comparing (Part 8 Task 4d).
///
/// Operator escape hatch: set `VCT_HUB_LEGACY_GLOBAL_ENV=1` (or `true` /
/// `TRUE` / `yes`) on the HUB process to REOPEN the compat window for one
/// more release while migrating a bespoke caller. Scoped tokens are
/// automatic on the next hub restart, so most installs need nothing.
///
/// Recognised ALLOW values: exactly `"1"`, `"true"`, `"TRUE"`, `"yes"`
/// (same terse set + case convention as the bind env `VCT_HUB_BIND_ALL`).
/// EVERY other value — including unset, `"0"`, `"false"`, `"no"`, or any
/// typo — DENIES (fail-closed: an unrecognised value must not silently
/// re-open a security boundary).
fn legacy_global_env_allowed() -> bool {
    matches!(
        std::env::var("VCT_HUB_LEGACY_GLOBAL_ENV").ok().as_deref(),
        Some("1") | Some("true") | Some("TRUE") | Some("yes")
    )
}

/// Process-lifetime dedup set for the "global token used on a per-project
/// route" deprecation log. Keyed by project_id so a resolver hammering
/// ONE project's `/env` in a loop logs once, not per request — but a
/// genuinely different project still surfaces its own line. Granularity
/// is "caller-ish" (per project) rather than per-request, per the brief.
static LEGACY_GLOBAL_ENV_WARNED: Mutex<Option<HashSet<String>>> = Mutex::new(None);

/// Emit the deprecation line for `project_id` at most once per process.
fn warn_legacy_global_env_once(project_id: &str) {
    let mut guard = match LEGACY_GLOBAL_ENV_WARNED.lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    let seen = guard.get_or_insert_with(HashSet::new);
    if !seen.insert(project_id.to_string()) {
        return; // already warned for this project this process.
    }
    eprintln!(
        "[vct-hub] DEPRECATION: the global hub.token was used to read \
         /projects/{}/env or /config, accepted ONLY because you set \
         VCT_HUB_LEGACY_GLOBAL_ENV=1 to re-open the compat window (the \
         default is now DENY as of v0.2.77). This coarse credential grants \
         every project's env + config; migrate to the per-project token \
         (hub.token.{}) — the bundled resolvers already prefer it and the \
         hub mints one per project — then UNSET the flag. This escape \
         hatch will be removed in a future release.",
        project_id, project_id
    );
}

/// Outcome of evaluating a bearer against the per-project + global
/// credentials on a `/env` / `/config` route.
#[derive(Debug, PartialEq, Eq)]
enum ProjectRouteAuth {
    /// Bearer matched THIS project's per-project token — allow, no log.
    ProjectToken,
    /// Bearer matched the global token — allow (compat window) unless
    /// the legacy flag is off; caller logs the deprecation line once.
    GlobalTokenCompat,
    /// Bearer matched a DIFFERENT project's per-project token than the URL
    /// segment names — a candidate hard 403. Carries the owner id so the
    /// caller can, as a fallback, canonicalize the URL segment (which may
    /// be a SLUG — the handler accepts id-OR-slug) to an id and re-compare
    /// before actually refusing (v0.2.77 Part 8 Task 4d). A token minted
    /// for project A addressed at project B's id/slug is still a hard 403.
    WrongProject { owner: String },
    /// Bearer matched nothing — 401.
    NoMatch,
    /// Legacy global-token path disabled by `VCT_HUB_LEGACY_GLOBAL_ENV=0`
    /// AND the bearer is the global token — 403 with a migration message.
    GlobalTokenRefused,
}

/// Decide how a bearer authenticates against a per-project route.
///
/// Precedence:
///   1. per-project token for THIS project → allow;
///   2. per-project token for ANOTHER project → hard 403 (wrong project
///      — a scoped credential must never cross the project boundary);
///   3. global token → allow this release (compat) unless the legacy
///      flag is off, in which case 403 with a migration message;
///   4. anything else → 401.
///
/// Constant-time comparisons throughout (the registry reverse-lookup and
/// the global compare both accumulate).
fn evaluate_project_route_auth(
    url_project_id: &str,
    bearer: &str,
    global_token: &str,
    registry: &ProjectTokenRegistry,
) -> ProjectRouteAuth {
    // 1 + 2: does the bearer match SOME project's per-project token?
    // `project_for_token` returns an owned String (the value lives behind
    // the registry's RwLock).
    if let Some(owner) = registry.project_for_token(bearer) {
        if owner == url_project_id {
            return ProjectRouteAuth::ProjectToken;
        }
        // The URL segment may be a SLUG that canonicalizes to `owner`
        // (the handler accepts id-OR-slug). We DON'T do the DB lookup
        // here — this fn stays pure/fast — so we hand the owner back and
        // let `require_auth` canonicalize + re-compare before refusing
        // (v0.2.77 Part 8 Task 4d). The direct id match above covers the
        // hot path with no DB round-trip.
        return ProjectRouteAuth::WrongProject { owner };
    }
    // 3: global token?
    if constant_time_eq(bearer.as_bytes(), global_token.as_bytes()) {
        if legacy_global_env_allowed() {
            return ProjectRouteAuth::GlobalTokenCompat;
        }
        return ProjectRouteAuth::GlobalTokenRefused;
    }
    // 4: matches nothing.
    ProjectRouteAuth::NoMatch
}

/// v0.2.77 Part 8 Task 4d — does the scoped-token owner canonicalize to
/// the same project the URL segment names?
///
/// Called only from the `WrongProject` arm, i.e. AFTER a direct id
/// compare (`owner == url_segment`) already failed. The URL segment may
/// be a SLUG (the config handler accepts id-OR-slug), so we resolve it to
/// a canonical id via `get_project` then `get_project_by_slug` — mirroring
/// the handler — and return `true` iff that canonical id equals `owner`.
///
/// Fails CLOSED: no DB handle wired, an unresolvable segment, or a DB
/// error all return `false` (keep the 403). A token minted for project A
/// addressed at project B's id/slug therefore still hard-403s — this only
/// rescues the case where the URL's slug and the token both name the SAME
/// project.
fn scoped_token_owns_url_project(
    req: &Request<Body>,
    owner: &str,
    url_segment: &str,
) -> bool {
    let Some(db_handle) = req.extensions().get::<LauncherDbHandle>().cloned() else {
        return false; // cannot canonicalize → fail closed.
    };
    let db = &db_handle.0;
    // id first, slug fallback — same order as config_api's handler.
    let canonical = match db.get_project(url_segment) {
        Ok(Some(p)) => p.id,
        Ok(None) => match db.get_project_by_slug(url_segment) {
            Ok(Some(p)) => p.id,
            Ok(None) => return false, // unknown segment → keep the 403.
            Err(e) => {
                eprintln!(
                    "[vct-hub] auth: slug canonicalization lookup failed for {:?} ({}); \
                     refusing (fail closed).",
                    url_segment, e
                );
                return false;
            }
        },
        Err(e) => {
            eprintln!(
                "[vct-hub] auth: id canonicalization lookup failed for {:?} ({}); \
                 refusing (fail closed).",
                url_segment, e
            );
            return false;
        }
    };
    // Constant-time compare of the canonical id against the token owner —
    // consistent with the rest of the auth-boundary comparisons.
    constant_time_eq(canonical.as_bytes(), owner.as_bytes())
}

/// v0.2.77 Part 8 Task 4a — attempt a lazy-mint for a mid-session-added
/// project and report whether the current request may proceed.
///
/// Called ONLY from the `GlobalTokenRefused` arm — i.e. the bearer is the
/// global `hub.token` (same-user trust) and the compat window is closed.
/// Pulls the `LauncherDbHandle` the server injected as a request
/// extension; if it is absent (a test harness that installs only
/// AuthState + registry, or a wiring bug) we CANNOT resolve the id → do
/// NOT proceed (fail closed: keep the 403). Delegates the DB lookup +
/// mint + file write + registration to `project_tokens::lazy_mint_for_
/// project`, which returns `Some` only for a DB-known project (unknown id
/// → `None` → keep the 403).
///
/// Returns `true` iff a token was minted (or already existed) for a real
/// project, meaning this request is now authorized to proceed.
fn lazy_mint_and_proceed(
    req: &Request<Body>,
    registry: &ProjectTokenRegistry,
    url_project_id: &str,
) -> bool {
    let Some(db_handle) = req.extensions().get::<LauncherDbHandle>().cloned() else {
        // No DB handle wired — cannot resolve the project. Fail closed.
        return false;
    };
    project_tokens::lazy_mint_for_project(registry, &db_handle.0, url_project_id).is_some()
}

/// 403 for a scoped-credential boundary violation (wrong project token,
/// or the global token refused when the legacy flag is off).
fn forbidden_response(message: &str) -> Response {
    let body = serde_json::json!({
        "error": {
            "code": "forbidden",
            "message": message,
        }
    });
    (
        StatusCode::FORBIDDEN,
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        body.to_string(),
    )
        .into_response()
}

/// Axum middleware: require `Authorization: Bearer <token>` on every
/// non-exempt request.
///
/// Wired in `server.rs` after the route nest and before the CORS layer
/// so OPTIONS preflight (which never carries Authorization) bypasses
/// auth and the CORS layer can answer it. Real requests that follow
/// the preflight DO get gated.
///
/// v0.2.76 Part 4 — the per-project `/env` + `/config` routes accept
/// EITHER the matching per-project token (`hub.token.<id>`) OR the global
/// `hub.token` (one-release compat window). A token minted for a
/// different project is a hard 403. All OTHER `/api/v1/*` routes stay on
/// the global token exactly as before.
pub async fn require_auth(req: Request<Body>, next: Next) -> Response {
    // OPTIONS = CORS preflight. Always allow; the CORS layer above
    // will produce the right response with no body.
    if req.method() == Method::OPTIONS {
        return next.run(req).await;
    }

    let path = req.uri().path();
    if is_exempt_path(path) {
        return next.run(req).await;
    }

    // Pull the AuthState that the server installed as an extension.
    // If it's missing, fail closed — better to return 500 than to
    // accidentally serve secrets unauthenticated because of a wiring
    // bug.
    let state = match req.extensions().get::<AuthState>().cloned() {
        Some(s) => s,
        None => {
            eprintln!("[vct-hub] auth middleware: AuthState extension missing");
            return (StatusCode::INTERNAL_SERVER_ERROR, "auth not configured").into_response();
        }
    };

    let provided = match extract_bearer_token(req.headers()) {
        Some(t) => t,
        None => {
            return unauthorized_response();
        }
    };

    // v0.2.76 Part 4 — per-project `/env` + `/config` routes accept the
    // matching per-project token OR the global token (compat window). A
    // token minted for a DIFFERENT project is a hard 403.
    if let Some(url_project_id) = per_project_token_route(path) {
        // The registry is injected by server.rs. If it's missing (a
        // wiring bug, or a test harness that only installs AuthState),
        // treat it as empty — every bearer then evaluates against the
        // global token alone, which is the pre-Part-4 behaviour. This
        // fail-soft keeps the global-token path (and every existing
        // auth test) working when the registry extension is absent.
        let registry = req
            .extensions()
            .get::<ProjectTokenRegistry>()
            .cloned()
            .unwrap_or_else(ProjectTokenRegistry::empty);

        match evaluate_project_route_auth(
            url_project_id,
            provided,
            state.token.as_str(),
            &registry,
        ) {
            ProjectRouteAuth::ProjectToken => return next.run(req).await,
            ProjectRouteAuth::GlobalTokenCompat => {
                warn_legacy_global_env_once(url_project_id);
                return next.run(req).await;
            }
            ProjectRouteAuth::WrongProject { owner } => {
                // v0.2.77 Part 8 Task 4d — slug canonicalization. The
                // direct id compare in evaluate_project_route_auth failed,
                // but the URL segment may be a SLUG that canonicalizes to
                // `owner` (the config handler accepts id-OR-slug, so a
                // scoped-token call legitimately addressed by slug must not
                // spuriously 403). Resolve the segment → id and re-compare
                // against the token owner. Only NOW do we touch the DB
                // (the hot path — direct id match — never gets here).
                if scoped_token_owns_url_project(&req, &owner, url_project_id) {
                    return next.run(req).await;
                }
                return forbidden_response(
                    "this per-project token does not authorize the project in the \
                     URL; a token minted for project A cannot read project B",
                );
            }
            ProjectRouteAuth::GlobalTokenRefused => {
                // v0.2.77 Part 8 Task 4a — lazy-mint rescue. The bearer IS
                // the global hub.token (proven above — same-user trust,
                // 0o600), and the legacy compat window is closed. If this
                // {id} resolves to a REAL project that has no scoped token
                // yet (added while the hub was running), mint it inline,
                // write the 0o600 file + register it, and PROCEED — so this
                // first request succeeds and the resolver rides the scoped
                // token from the next request. A garbage bearer never gets
                // here (it is NoMatch), and an UNKNOWN {id} does not mint
                // (lazy_mint returns None) → we keep the 403.
                if lazy_mint_and_proceed(&req, &registry, url_project_id) {
                    return next.run(req).await;
                }
                return forbidden_response(
                    "the global hub.token is no longer accepted on /env + /config \
                     (VCT_HUB_LEGACY_GLOBAL_ENV=0); present the per-project token \
                     (hub.token.<project_id>) — the bundled resolvers already prefer it",
                );
            }
            ProjectRouteAuth::NoMatch => return unauthorized_response(),
        }
    }

    // Every other route: global hub.token only, exactly as before.
    if !constant_time_eq(provided.as_bytes(), state.token.as_bytes()) {
        return unauthorized_response();
    }

    next.run(req).await
}

/// 401 with a small JSON envelope so wrapper clients can format a
/// useful diagnostic without parsing free-form text.
fn unauthorized_response() -> Response {
    let body = serde_json::json!({
        "error": {
            "code": "unauthorized",
            "message": "missing or invalid Authorization: Bearer <token>; \
                read the launcher's hub.token file (default: ~/.vct/hub.token) \
                and resend the request",
        }
    });
    (
        StatusCode::UNAUTHORIZED,
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        body.to_string(),
    )
        .into_response()
}

// ─── Tests ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    use axum::{
        http::StatusCode,
        routing::get,
        Router,
    };
    use std::sync::Mutex;

    // Note: `Method` and `Request` come into scope via the parent
    // module's imports. `Body` is no longer used here (we use the
    // spawn-server pattern, not tower::oneshot), so we don't pull it.

    // The token-file tests mutate VCT_STATE_DIR at process scope.
    // Serialise them so parallel cargo-test runs don't observe each
    // other. Mirrors the pattern in `paths.rs`.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_state_dir<F: FnOnce(&std::path::Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        // Safety: tests are serialized by SERIALIZE; no thread
        // concurrently observes/mutates VCT_STATE_DIR.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }
        f(tmp.path());
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    // ─── Token generation ────────────────────────────────────────────

    #[test]
    fn generate_token_returns_64_hex_chars() {
        let t = generate_token().expect("rng works");
        assert_eq!(t.len(), TOKEN_BYTES * 2, "token is hex-encoded 32 bytes");
        assert!(
            t.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
            "token must be lowercase hex, got: {}",
            t
        );
    }

    #[test]
    fn generate_token_returns_distinct_tokens_across_calls() {
        // Probability of collision in 256-bit space is ~0; if this
        // ever fires, the OS CSPRNG is stuck.
        let mut seen = std::collections::HashSet::new();
        for _ in 0..20 {
            seen.insert(generate_token().expect("rng works"));
        }
        assert_eq!(seen.len(), 20, "tokens must be distinct: {:?}", seen);
    }

    // ─── Token file persistence ──────────────────────────────────────

    #[test]
    fn hub_token_file_written_with_mode_0o600_on_unix() {
        with_state_dir(|root| {
            let token = generate_token().unwrap();
            write_token_file(&token).expect("write token");

            let path = root.join(TOKEN_FILE);
            assert!(path.exists(), "token file at {}", path.display());

            let read_back = std::fs::read_to_string(&path).expect("read token");
            assert_eq!(read_back, token, "round-trip");

            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
                assert_eq!(
                    mode, 0o600,
                    "token file mode must be 0o600 (rw for owner only), got: {:o}",
                    mode
                );
            }
        });
    }

    #[test]
    fn write_token_file_overwrites_existing() {
        with_state_dir(|root| {
            // First write — sets up the file.
            write_token_file("AAAA").unwrap();
            assert_eq!(
                std::fs::read_to_string(root.join(TOKEN_FILE)).unwrap(),
                "AAAA"
            );

            // Second write — must replace, not append.
            write_token_file("BB").unwrap();
            assert_eq!(
                std::fs::read_to_string(root.join(TOKEN_FILE)).unwrap(),
                "BB",
                "must truncate-on-open, not append"
            );

            #[cfg(unix)]
            {
                // And the mode must still be 0o600 after the second
                // write (we re-create with mode each time).
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(root.join(TOKEN_FILE))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777;
                assert_eq!(mode, 0o600, "mode preserved after rewrite");
            }
        });
    }

    #[test]
    fn hub_token_regenerates_on_each_startup() {
        // "Startup" here = a generate_token + write_token_file pair.
        // We're proving the token rotates, not just that the function
        // runs twice — read both back from disk and assert they
        // differ.
        with_state_dir(|root| {
            let t1 = generate_token().unwrap();
            write_token_file(&t1).unwrap();
            let on_disk_1 = std::fs::read_to_string(root.join(TOKEN_FILE)).unwrap();

            let t2 = generate_token().unwrap();
            write_token_file(&t2).unwrap();
            let on_disk_2 = std::fs::read_to_string(root.join(TOKEN_FILE)).unwrap();

            assert_ne!(
                on_disk_1, on_disk_2,
                "every startup must rotate the token"
            );
            assert_eq!(on_disk_1, t1);
            assert_eq!(on_disk_2, t2);
        });
    }

    // ─── Constant-time compare ───────────────────────────────────────

    #[test]
    fn constant_time_eq_matches_exact() {
        assert!(constant_time_eq(b"hello", b"hello"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn constant_time_eq_rejects_mismatch() {
        assert!(!constant_time_eq(b"hello", b"hellp"));
        assert!(!constant_time_eq(b"hello", b"hell"));
        assert!(!constant_time_eq(b"hello", b"helloo"));
        assert!(!constant_time_eq(b"hello", b""));
    }

    // ─── Bearer extraction ───────────────────────────────────────────

    #[test]
    fn extract_bearer_token_canonical() {
        let mut h = HeaderMap::new();
        h.insert("Authorization", "Bearer deadbeef".parse().unwrap());
        assert_eq!(extract_bearer_token(&h), Some("deadbeef"));
    }

    #[test]
    fn extract_bearer_token_case_insensitive_scheme() {
        let mut h = HeaderMap::new();
        h.insert("Authorization", "bearer abc123".parse().unwrap());
        assert_eq!(extract_bearer_token(&h), Some("abc123"));
        h.clear();
        h.insert("Authorization", "BEARER abc123".parse().unwrap());
        assert_eq!(extract_bearer_token(&h), Some("abc123"));
    }

    #[test]
    fn extract_bearer_token_rejects_other_schemes() {
        let mut h = HeaderMap::new();
        h.insert("Authorization", "Basic dXNlcjpwYXNz".parse().unwrap());
        assert_eq!(extract_bearer_token(&h), None);
    }

    #[test]
    fn extract_bearer_token_rejects_missing_header() {
        let h = HeaderMap::new();
        assert_eq!(extract_bearer_token(&h), None);
    }

    #[test]
    fn extract_bearer_token_rejects_empty_token() {
        let mut h = HeaderMap::new();
        h.insert("Authorization", "Bearer ".parse().unwrap());
        assert_eq!(extract_bearer_token(&h), None);
    }

    // ─── Exempt-path matching ────────────────────────────────────────

    #[test]
    fn is_exempt_path_health_and_module_db_token() {
        // Health probe — always exempt.
        assert!(is_exempt_path("/api/v1/health"));
        // Module DB CRUD + token refresh — exempt (own scope middleware).
        assert!(is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/db/projects/p1/rows/rl_state/k1"
        ));
        assert!(is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/token/refresh"
        ));
        // db CRUD without the {key} suffix (insert/list) — also exempt.
        assert!(is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/db/projects/p1/rows/rl_state"
        ));
    }

    #[test]
    fn is_exempt_path_db_token_family_exact_shape_only() {
        // v0.2.61 (Option H C-EXEMPT): the db/token carve-outs match the EXACT
        // registered shapes, NOT the whole route family. A future route added
        // under db/ or token/ WITHOUT its own scope middleware must NOT inherit
        // the outer-gate bypass on the 0.0.0.0 surface.
        //
        // Out-of-shape db paths — must NOT be exempt:
        assert!(!is_exempt_path("/api/v1/modules/m/db/admin/dump"));
        assert!(!is_exempt_path("/api/v1/modules/m/db/projects/p1")); // no /rows/{table}
        assert!(!is_exempt_path("/api/v1/modules/m/db/projects/p1/rows/t/k/extra")); // too deep
        // Out-of-shape token paths — must NOT be exempt:
        assert!(!is_exempt_path("/api/v1/modules/m/token/issue"));
        assert!(!is_exempt_path("/api/v1/modules/m/token")); // bare
        // The exact registered shapes — still exempt:
        assert!(is_exempt_path("/api/v1/modules/m/db/projects/p1/rows/t"));
        assert!(is_exempt_path("/api/v1/modules/m/db/projects/p1/rows/t/k"));
        assert!(is_exempt_path("/api/v1/modules/m/token/refresh"));
    }

    #[test]
    fn is_exempt_path_rl_events_is_exempt() {
        // v0.2.61 (Option H): the exact rl/events shape must be exempt so
        // module-identity-token containers reach require_module_scope
        // instead of 401ing at the outer hub.token gate.
        assert!(is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123/rl/events"
        ));
        // Tolerate a trailing slash on the events path.
        assert!(is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123/rl/events/"
        ));
    }

    #[test]
    fn is_exempt_path_other_projects_route_is_not_exempt() {
        // A different 2nd-but-deeper segment under /projects/ must NOT
        // inherit the carve-out — only the precise rl/events tail does.
        assert!(!is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123/other"
        ));
        // /projects/{pid} with no rl/events tail.
        assert!(!is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123"
        ));
        // rl without the events leaf.
        assert!(!is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123/rl"
        ));
        // A deeper path past events must not match (only events leaf).
        assert!(!is_exempt_path(
            "/api/v1/modules/vct-rl-reranker/projects/abc-123/rl/events/extra"
        ));
        // Secret-bearing env route stays gated.
        assert!(!is_exempt_path("/api/v1/projects/abc-123/env"));
    }

    // ─── End-to-end: middleware in front of a real bound server ─────
    //
    // We follow the same spawn-on-random-port pattern that
    // `modules_api.rs` and `cli_api.rs` use for their HTTP-level
    // tests, so the test harness exercises the same axum::serve code
    // path the production hub runs and we don't need extra dev-deps
    // (tower::ServiceExt / http_body_util) for tower::oneshot.

    /// Build a minimal router with the auth middleware applied in the
    /// same shape the real server uses: nested under /api/v1, with
    /// /api/v1/health exempt.
    fn router_with_auth(token: &str) -> Router {
        let auth_state = AuthState::new(token.to_string());
        let app = Router::new()
            .route("/api/v1/health", get(|| async { "ok" }))
            .route(
                "/api/v1/projects/{id}/env",
                get(|| async { "secrets here" }),
            )
            // A NON-per-project route: the global hub.token is the correct
            // credential here, unaffected by the v0.2.77 /env+/config flip.
            // The "correct token accepted" test targets this so it asserts
            // the global-token auth path itself, not the per-project gate.
            .route("/api/v1/ping", get(|| async { "pong" }));
        app.layer(axum::middleware::from_fn(require_auth))
            .layer(axum::Extension(auth_state))
    }

    /// v0.2.76 Part 4 — router shaped like the real server: global token
    /// via `AuthState` PLUS a `ProjectTokenRegistry` extension, both
    /// `/env` and `/config` routes mounted. `projects` maps project_id →
    /// per-project token.
    fn router_with_project_tokens(
        global_token: &str,
        projects: &[(&str, &str)],
    ) -> Router {
        let auth_state = AuthState::new(global_token.to_string());
        let mut map = std::collections::HashMap::new();
        for (pid, tok) in projects {
            map.insert(pid.to_string(), tok.to_string());
        }
        let registry = ProjectTokenRegistry::from_map(map);
        let app = Router::new()
            .route("/api/v1/health", get(|| async { "ok" }))
            .route("/api/v1/projects/{id}/env", get(|| async { "env here" }))
            .route(
                "/api/v1/projects/{id}/config",
                get(|| async { "config here" }),
            )
            // A non-per-project route to prove global-token-only behaviour
            // is unchanged there.
            .route("/api/v1/projects", get(|| async { "projects list" }));
        app.layer(axum::middleware::from_fn(require_auth))
            .layer(axum::Extension(auth_state))
            .layer(axum::Extension(registry))
    }

    /// Permissive CORS layer for the SSOT-routed test routers
    /// (`server::apply_auth_layers` takes a `CorsLayer`; the test doesn't
    /// exercise CORS, so any layer that passes requests through is fine).
    fn test_cors_layer() -> tower_http::cors::CorsLayer {
        tower_http::cors::CorsLayer::new()
            .allow_origin(tower_http::cors::Any)
            .allow_methods(tower_http::cors::Any)
    }

    /// Bind on a random port, spawn the server task, return the base
    /// URL for reqwest to hit.
    async fn spawn_router(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        format!("http://{}", addr)
    }

    #[tokio::test]
    async fn hub_rejects_request_without_authorization_header() {
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let resp = reqwest::get(format!("{}/api/v1/projects/p1/env", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let body = resp.text().await.expect("body");
        assert!(
            body.contains("unauthorized"),
            "body should carry error envelope, got: {}",
            body
        );
    }

    #[tokio::test]
    async fn hub_rejects_request_with_wrong_bearer_token() {
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/p1/env", base))
            .header("Authorization", "Bearer the-wrong-token")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn hub_rejects_request_with_non_bearer_scheme() {
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/p1/env", base))
            .header("Authorization", "Basic dGhlLWNvcnJlY3QtdG9rZW4=")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn hub_accepts_request_with_correct_bearer_token() {
        // Target a NON-per-project route: the global hub.token is the
        // correct credential there and the v0.2.77 flip (which only gates
        // /env + /config) does not affect it. This test asserts the
        // global-token auth path, not the per-project gate.
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/ping", base))
            .header("Authorization", "Bearer the-correct-token")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.text().await.unwrap(), "pong");
    }

    #[tokio::test]
    async fn hub_health_endpoint_exempt_from_auth() {
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let resp = reqwest::get(format!("{}/api/v1/health", base))
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "/api/v1/health must be reachable without auth (liveness probe)"
        );
        assert_eq!(resp.text().await.unwrap(), "ok");
    }

    #[tokio::test]
    async fn hub_options_preflight_bypasses_auth() {
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        // OPTIONS without Authorization. Without the bypass this would
        // 401; with it, the underlying route returns whatever axum
        // gives for an unrouted OPTIONS (405 / 404 — either way, NOT
        // 401). The point of the test is that 401 is what we MUST
        // NOT see.
        let client = reqwest::Client::new();
        let resp = client
            .request(reqwest::Method::OPTIONS, format!("{}/api/v1/projects/p1/env", base))
            .send()
            .await
            .expect("hub reachable");
        assert_ne!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "OPTIONS preflight must bypass auth so CORS layer can answer"
        );
    }

    // ─── v0.2.76 Part 4: per-project token routing ───────────────────

    #[test]
    fn per_project_token_route_matches_env_and_config_only() {
        assert_eq!(per_project_token_route("/api/v1/projects/p1/env"), Some("p1"));
        assert_eq!(
            per_project_token_route("/api/v1/projects/p1/config"),
            Some("p1")
        );
        // Trailing slash tolerated.
        assert_eq!(
            per_project_token_route("/api/v1/projects/p1/env/"),
            Some("p1")
        );
        assert_eq!(
            per_project_token_route("/api/v1/projects/abc-123/config/"),
            Some("abc-123")
        );
        // Non-matching leaves / shapes.
        assert_eq!(per_project_token_route("/api/v1/projects/p1"), None);
        assert_eq!(per_project_token_route("/api/v1/projects/p1/env/sub"), None);
        assert_eq!(per_project_token_route("/api/v1/projects"), None);
        assert_eq!(per_project_token_route("/api/v1/projects//env"), None);
        assert_eq!(per_project_token_route("/api/v1/projects/p1/codegraph-builds"), None);
        // Module-scoped config route lives under /modules/, not /projects/.
        assert_eq!(
            per_project_token_route("/api/v1/modules/m/projects/p1/config"),
            None
        );
        assert_eq!(per_project_token_route("/api/v1/health"), None);
    }

    #[test]
    fn evaluate_project_route_auth_matrix() {
        let mut map = std::collections::HashMap::new();
        map.insert("proj-a".to_string(), "token-a".to_string());
        map.insert("proj-b".to_string(), "token-b".to_string());
        let reg = ProjectTokenRegistry::from_map(map);
        let global = "global-token";

        // Correct project token → allow.
        assert!(matches!(
            evaluate_project_route_auth("proj-a", "token-a", global, &reg),
            ProjectRouteAuth::ProjectToken
        ));
        // Wrong project's token → WrongProject carrying the real owner
        // (so the caller can attempt slug canonicalization before 403).
        assert_eq!(
            evaluate_project_route_auth("proj-a", "token-b", global, &reg),
            ProjectRouteAuth::WrongProject { owner: "proj-b".to_string() }
        );
        // (Global-token posture is flag-driven — asserted explicitly at
        // the end of this test under a held flag guard, both directions.)
        // Garbage → 401.
        assert!(matches!(
            evaluate_project_route_auth("proj-a", "nonsense", global, &reg),
            ProjectRouteAuth::NoMatch
        ));
        // A project token whose URL project has no registry entry still
        // yields WrongProject (owner = token's real project) if the token
        // belongs to some OTHER project. The caller's slug-canonicalization
        // fallback then decides — here there's no DB, so it fails closed.
        assert_eq!(
            evaluate_project_route_auth("proj-unknown", "token-a", global, &reg),
            ProjectRouteAuth::WrongProject { owner: "proj-a".to_string() }
        );

        // Global-token posture is flag-driven — assert BOTH explicitly
        // (v0.2.77 flip: default is now DENY, so we pin each side under a
        // held flag guard rather than riding an implicit default).
        {
            let g = LegacyFlagGuard::cleared(); // unset → default DENY.
            assert!(matches!(
                evaluate_project_route_auth("proj-a", global, global, &reg),
                ProjectRouteAuth::GlobalTokenRefused
            ));
            g.reset("1"); // explicit opt-in → compat allow.
            assert!(matches!(
                evaluate_project_route_auth("proj-a", global, global, &reg),
                ProjectRouteAuth::GlobalTokenCompat
            ));
        }
    }

    #[tokio::test]
    async fn env_route_accepts_matching_project_token() {
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a"), ("proj-b", "tok-b")],
        ))
        .await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.text().await.unwrap(), "env here");
    }

    #[tokio::test]
    async fn config_route_accepts_matching_project_token() {
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/config", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.text().await.unwrap(), "config here");
    }

    #[tokio::test]
    async fn env_route_rejects_wrong_project_token_with_403() {
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a"), ("proj-b", "tok-b")],
        ))
        .await;
        let client = reqwest::Client::new();
        // Project B's token on Project A's route → hard 403.
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer tok-b")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        let body = resp.text().await.unwrap();
        assert!(body.contains("forbidden"), "body: {}", body);
    }

    #[tokio::test]
    async fn env_route_accepts_global_token_compat_window() {
        // v0.2.77 flip: the compat window is now OPT-IN — the operator
        // must set VCT_HUB_LEGACY_GLOBAL_ENV=1 explicitly. This test pins
        // that the escape hatch still WORKS when re-opened.
        let _g = LegacyFlagGuard::set("1");
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();
        // With the flag opted back in, the global token works on /env.
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::OK);
    }

    /// v0.2.77 flip: the DEFAULT-path test. With NO flag set (the new
    /// default), the global token is REFUSED on /env with a 403 — this is
    /// now the out-of-the-box posture, not an opt-in. Formerly
    /// `env_route_refuses_global_token_when_flag_off` covered this only
    /// under an explicit VCT_HUB_LEGACY_GLOBAL_ENV=0; that test remains
    /// (belt + suspenders), but THIS one pins the unset-default behaviour.
    #[tokio::test]
    async fn env_route_refuses_global_token_by_default_post_flip() {
        let _g = LegacyFlagGuard::cleared(); // unset → default DENY.
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::FORBIDDEN,
            "unset flag → global token refused (v0.2.77 flipped default)"
        );
        let body = resp.text().await.unwrap();
        assert!(body.contains("VCT_HUB_LEGACY_GLOBAL_ENV"), "body: {}", body);
        // The scoped token STILL authorizes under the default.
        let resp2 = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp2.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn env_route_rejects_garbage_token_with_401() {
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer neither-project-nor-global")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn non_per_project_route_still_requires_global_token() {
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();
        // A per-project token on a NON-per-project route → 401 (only the
        // global token authorizes /api/v1/projects list).
        let resp = client
            .get(format!("{}/api/v1/projects", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        // The global token works there.
        let resp2 = client
            .get(format!("{}/api/v1/projects", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp2.status(), StatusCode::OK);
    }

    // ─── VCT_HUB_LEGACY_GLOBAL_ENV flag (DEFAULT FLIPPED v0.2.77) ─────
    //
    // Introduced v0.2.76 Part 4 as DEFAULT-allow (one-release compat).
    // As of v0.2.77 Part 8 the DEFAULT is DENY: unset/unrecognised
    // refuses the global token on /env + /config; only "1"/"true"/
    // "TRUE"/"yes" re-open the window. These tests pin the flipped
    // semantics — compat tests now set "1" EXPLICITLY (they no longer
    // ride an implicit default-allow). The env var is process-global;
    // cargo runs vct-hub tests with RUST_TEST_THREADS=1 (serialised — see
    // .cargo/config.toml), so each test sets + restores it without racing
    // siblings.

    /// Serialise the env-mutating flag tests among themselves (belt +
    /// suspenders on top of the workspace-wide single-thread setting).
    static LEGACY_FLAG_SERIALIZE: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// RAII guard: acquires the serialise lock ONCE for its lifetime and
    /// ALWAYS removes `VCT_HUB_LEGACY_GLOBAL_ENV` on drop (even on a
    /// panicking assert), so a failed flag test never leaks a "deny"
    /// value into a later compat test. The mutex is NOT reentrant, so the
    /// guard holds it for the whole test and mutations go through
    /// `set` / `clear` WITHOUT re-locking. Mirrors the `with_state_dir`
    /// discipline in this module.
    struct LegacyFlagGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
    }
    impl LegacyFlagGuard {
        /// Acquire the lock and set the flag to `value`.
        fn set(value: &str) -> Self {
            let lock = LEGACY_FLAG_SERIALIZE
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            // Safety: serialised by the held lock + RUST_TEST_THREADS=1.
            unsafe {
                std::env::set_var("VCT_HUB_LEGACY_GLOBAL_ENV", value);
            }
            Self { _lock: lock }
        }
        /// Acquire the lock with the flag cleared.
        fn cleared() -> Self {
            let lock = LEGACY_FLAG_SERIALIZE
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            unsafe {
                std::env::remove_var("VCT_HUB_LEGACY_GLOBAL_ENV");
            }
            Self { _lock: lock }
        }
        /// Mutate the flag WITHOUT re-acquiring the lock (this guard
        /// already holds it — the mutex is not reentrant). Used to sweep
        /// multiple values inside one test body.
        fn reset(&self, value: &str) {
            unsafe {
                std::env::set_var("VCT_HUB_LEGACY_GLOBAL_ENV", value);
            }
        }
        fn reclear(&self) {
            unsafe {
                std::env::remove_var("VCT_HUB_LEGACY_GLOBAL_ENV");
            }
        }
    }
    impl Drop for LegacyFlagGuard {
        fn drop(&mut self) {
            unsafe {
                std::env::remove_var("VCT_HUB_LEGACY_GLOBAL_ENV");
            }
        }
    }

    #[test]
    fn legacy_global_env_allowed_default_and_allow_values() {
        // v0.2.77 Part 8 (flip): the DEFAULT is now DENY. Unset → deny;
        // only the recognised truthy set allows; every other value —
        // including "0"/"false"/"no" AND an unrecognised typo — denies
        // (fail-closed). ONE guard holds the lock for the whole test.
        let g = LegacyFlagGuard::cleared();
        assert!(
            !legacy_global_env_allowed(),
            "unset → DENY (v0.2.77 flipped default)"
        );
        // The ONLY values that re-open the compat window.
        for allow in ["1", "true", "TRUE", "yes"] {
            g.reset(allow);
            assert!(legacy_global_env_allowed(), "{:?} → allow", allow);
        }
        // Explicit deny values AND anything unrecognised → deny.
        for deny in ["0", "false", "no", "anything-else", "", "True", "YES"] {
            g.reset(deny);
            assert!(!legacy_global_env_allowed(), "{:?} → deny (fail-closed)", deny);
        }
        g.reclear();
        assert!(!legacy_global_env_allowed(), "recleared (unset) → DENY");
    }

    #[test]
    fn evaluate_global_token_refused_when_flag_off() {
        let _g = LegacyFlagGuard::set("0");
        let mut map = std::collections::HashMap::new();
        map.insert("proj-a".to_string(), "token-a".to_string());
        let reg = ProjectTokenRegistry::from_map(map);
        let global = "global-token";

        // Global token → refused (403) when the flag is off.
        assert!(matches!(
            evaluate_project_route_auth("proj-a", global, global, &reg),
            ProjectRouteAuth::GlobalTokenRefused
        ));
        // The per-project token STILL works even with the flag off — the
        // flag only gates the GLOBAL-token path.
        assert!(matches!(
            evaluate_project_route_auth("proj-a", "token-a", global, &reg),
            ProjectRouteAuth::ProjectToken
        ));
    }

    #[tokio::test]
    async fn env_route_refuses_global_token_when_flag_off() {
        let _g = LegacyFlagGuard::set("0");
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();

        // Global token on /env → 403 (compat window closed by the flag).
        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        let body = resp.text().await.unwrap();
        assert!(
            body.contains("VCT_HUB_LEGACY_GLOBAL_ENV"),
            "403 body should name the flag; got: {}",
            body
        );

        // The per-project token STILL authorizes with the flag off.
        let resp2 = client
            .get(format!("{}/api/v1/projects/proj-a/env", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp2.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn config_route_refuses_global_token_when_flag_off() {
        let _g = LegacyFlagGuard::set("0");
        let base = spawn_router(router_with_project_tokens(
            "global-tok",
            &[("proj-a", "tok-a")],
        ))
        .await;
        let client = reqwest::Client::new();

        let resp = client
            .get(format!("{}/api/v1/projects/proj-a/config", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    // ── v0.2.77 Part 8 Task 4a: lazy-mint through the middleware ───────

    /// Router shaped like the real server for the lazy-mint path: global
    /// token via AuthState, an (initially empty) ProjectTokenRegistry, AND
    /// the `LauncherDbHandle` extension the lazy-mint helper needs to
    /// resolve the URL id → a real project. Returns the registry so the
    /// test can observe the lazy-minted entry after the request.
    fn router_with_db_for_lazy_mint(
        global_token: &str,
        db: LauncherDbHandle,
    ) -> (Router, ProjectTokenRegistry) {
        let auth_state = AuthState::new(global_token.to_string());
        let registry = ProjectTokenRegistry::empty();
        let routes = Router::new()
            .route("/api/v1/projects/{id}/env", get(|| async { "env here" }))
            .route(
                "/api/v1/projects/{id}/config",
                get(|| async { "config here" }),
            );
        // v0.2.77 F1: apply the auth stack through the PRODUCTION SSOT
        // (`server::apply_auth_layers`) rather than hand-rolling the layer
        // order here. The old hand-rolled builder declared `Extension(db)`
        // LAST (outermost → runs first), which made the DB handle visible
        // to `require_auth` even though PRODUCTION declared it inner — so
        // these tests passed while prod failed closed. Routing through the
        // SSOT makes that masking structurally impossible: if the shared
        // order regresses, these tests fail with prod.
        let app = crate::server::apply_auth_layers(
            routes,
            db,
            auth_state,
            registry.clone(),
            test_cors_layer(),
        );
        (app, registry)
    }

    fn seed_project_row(db: &vct_launcher_core::db::Db, id: &str, name: &str, folder: &str) {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?2, ?4, ?4)",
                rusqlite::params![id, name, folder, now],
            )
            .unwrap();
    }

    /// Flag OFF + global token + a project that exists in the DB but has
    /// NO registry entry (added mid-session) → lazy-mint rescues it: the
    /// request PROCEEDS (200) and the scoped token is now registered +
    /// written to disk, so the next request rides the per-project token.
    #[tokio::test]
    async fn lazy_mint_rescues_db_known_project_when_flag_off() {
        // Hold the flag lock for the whole test AND isolate VCT_STATE_DIR
        // (lazy-mint writes hub.token.<id> there). The LegacyFlagGuard
        // serialises against the other flag tests; the state-dir set is
        // safe under that same held lock.
        let _g = LegacyFlagGuard::set("0");
        let tmp = tempfile::tempdir().expect("tempdir");
        // Safety: LegacyFlagGuard holds LEGACY_FLAG_SERIALIZE + tests run
        // single-threaded (RUST_TEST_THREADS=1 for env-mutating tests).
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }

        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        seed_project_row(&db, "mid-pid", "MidSession", "/tmp/mid");
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        let (app, registry) = router_with_db_for_lazy_mint("global-tok", handle);
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        // Registry starts empty — no scoped token for mid-pid yet.
        assert_eq!(registry.token_for("mid-pid"), None);

        // First request rides the global token; flag off → would be 403,
        // but lazy-mint rescues it → 200.
        let resp = client
            .get(format!("{}/api/v1/projects/mid-pid/config", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "lazy-mint must rescue a DB-known mid-session project"
        );

        // The scoped token is now registered AND on disk.
        let minted = registry
            .token_for("mid-pid")
            .expect("lazy-mint must register the token");
        let path = tmp.path().join("hub.token.mid-pid");
        assert!(path.exists(), "lazy-minted token file must be written");
        assert_eq!(std::fs::read_to_string(&path).unwrap().trim(), minted);

        // A follow-up request presenting the freshly-minted SCOPED token
        // authorizes via the ProjectToken path (no global token needed).
        let resp2 = client
            .get(format!("{}/api/v1/projects/mid-pid/config", base))
            .header("Authorization", format!("Bearer {}", minted))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp2.status(), StatusCode::OK);

        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    /// Flag OFF + global token + an UNKNOWN id (no DB row) → NO lazy-mint;
    /// the 403 stands and no token file is written. Guards against an
    /// attacker forcing a token file for an arbitrary id.
    #[tokio::test]
    async fn lazy_mint_does_not_rescue_unknown_id_when_flag_off() {
        let _g = LegacyFlagGuard::set("0");
        let tmp = tempfile::tempdir().expect("tempdir");
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }

        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        // No projects seeded.
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        let (app, registry) = router_with_db_for_lazy_mint("global-tok", handle);
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        let resp = client
            .get(format!("{}/api/v1/projects/ghost-pid/config", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::FORBIDDEN,
            "an unknown id must NOT be lazy-minted; the 403 stands"
        );
        assert_eq!(registry.token_for("ghost-pid"), None);
        assert!(!tmp.path().join("hub.token.ghost-pid").exists());

        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    // ── v0.2.77 Part 8 Task 4d: slug canonicalization in auth ──────────

    /// Router with a DB + a PRE-SEEDED registry (project already has a
    /// scoped token) so we can address it by SLUG and prove the auth layer
    /// canonicalizes slug→id before the owner comparison.
    fn router_with_db_and_tokens(
        global_token: &str,
        db: LauncherDbHandle,
        tokens: &[(&str, &str)],
    ) -> Router {
        let auth_state = AuthState::new(global_token.to_string());
        let mut map = std::collections::HashMap::new();
        for (pid, tok) in tokens {
            map.insert(pid.to_string(), tok.to_string());
        }
        let registry = ProjectTokenRegistry::from_map(map);
        let routes = Router::new()
            .route("/api/v1/projects/{id}/env", get(|| async { "env here" }))
            .route(
                "/api/v1/projects/{id}/config",
                get(|| async { "config here" }),
            );
        // v0.2.77 F1: same SSOT routing as `router_with_db_for_lazy_mint`
        // — the slug-canonicalization (4d) helper also reads the DB handle
        // from request extensions, so it must be exercised through the real
        // production layer order, not a masking hand-rolled one.
        crate::server::apply_auth_layers(
            routes,
            db,
            auth_state,
            registry,
            test_cors_layer(),
        )
    }

    /// A scoped token addressed by the project's SLUG (which canonicalizes
    /// to the token owner's id) must AUTHORIZE — not spuriously 403. This
    /// is the exact bug 4d fixes: the handler accepts id-OR-slug, so the
    /// auth layer must canonicalize before comparing owner vs URL segment.
    #[tokio::test]
    async fn scoped_token_addressed_by_slug_is_authorized() {
        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        // seed_project_row sets slug == name.
        seed_project_row(&db, "pid-x", "myslug", "/tmp/x");
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        let app = router_with_db_and_tokens("global-tok", handle, &[("pid-x", "tok-x")]);
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        // Address by SLUG, present pid-x's scoped token. Pre-4d this was a
        // spurious WrongProject 403 ("pid-x" != "myslug"); post-4d the auth
        // layer canonicalizes "myslug" → "pid-x" and allows.
        let resp = client
            .get(format!("{}/api/v1/projects/myslug/config", base))
            .header("Authorization", "Bearer tok-x")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "a scoped token addressed by its project's slug must authorize"
        );

        // Sanity: addressing by the canonical id still works (the hot path
        // that never needed canonicalization).
        let resp_id = client
            .get(format!("{}/api/v1/projects/pid-x/config", base))
            .header("Authorization", "Bearer tok-x")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp_id.status(), StatusCode::OK);
    }

    /// A token minted for project A, addressed at project B's SLUG, is
    /// STILL a hard 403 — canonicalization rescues only the same-project
    /// case, never a genuine cross-project boundary crossing.
    #[tokio::test]
    async fn scoped_token_for_other_project_by_slug_still_403() {
        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        seed_project_row(&db, "pid-a", "slug-a", "/tmp/a");
        seed_project_row(&db, "pid-b", "slug-b", "/tmp/b");
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        let app = router_with_db_and_tokens(
            "global-tok",
            handle,
            &[("pid-a", "tok-a"), ("pid-b", "tok-b")],
        );
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        // Project A's token addressed at project B's slug → canonicalizes
        // to pid-b, which != owner pid-a → hard 403.
        let resp = client
            .get(format!("{}/api/v1/projects/slug-b/config", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::FORBIDDEN,
            "cross-project token addressed by the OTHER project's slug must still 403"
        );
    }

    /// An UNKNOWN slug (no project) with a valid-but-other-project token
    /// keeps the 403 (canonicalization fails closed on an unresolvable
    /// segment — no accidental allow).
    #[tokio::test]
    async fn scoped_token_with_unknown_slug_stays_403() {
        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        seed_project_row(&db, "pid-a", "slug-a", "/tmp/a");
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        let app = router_with_db_and_tokens("global-tok", handle, &[("pid-a", "tok-a")]);
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        let resp = client
            .get(format!("{}/api/v1/projects/no-such-slug/config", base))
            .header("Authorization", "Bearer tok-a")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    // ── v0.2.77 F1 regression: production layer order must expose the
    //    LauncherDbHandle to require_auth (lazy-mint + slug canon) ────────

    /// Pins the F1 fix at the SSOT level: a router built through the REAL
    /// `server::apply_auth_layers` (the exact function production uses)
    /// must make the `LauncherDbHandle` extension visible to `require_auth`
    /// so lazy-mint (4a) proceeds. Before the fix, `Extension(launcher_db)`
    /// was declared INNER to `require_auth`, so the handle was absent when
    /// the middleware ran and lazy-mint failed closed (hard 403) — verified
    /// reproducible: with the handle innermost this request 403s. The
    /// old bespoke test router masked it by declaring the handle OUTERMOST.
    /// This test cannot be masked because it goes through the prod SSOT.
    #[tokio::test]
    async fn f1_lazy_mint_reachable_through_production_layer_stack() {
        let _g = LegacyFlagGuard::set("0");
        let tmp = tempfile::tempdir().expect("tempdir");
        // Safety: LegacyFlagGuard serialises the flag/env-mutating tests.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }

        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        seed_project_row(&db, "f1-pid", "F1Session", "/tmp/f1");
        let handle = LauncherDbHandle(std::sync::Arc::new(db));
        // Built through the SAME `apply_auth_layers` the production server
        // calls (via router_with_db_for_lazy_mint).
        let (app, registry) = router_with_db_for_lazy_mint("global-tok", handle);
        let base = spawn_router(app).await;
        let client = reqwest::Client::new();

        assert_eq!(registry.token_for("f1-pid"), None);

        // Global token + flag off → would 403, BUT the DB handle IS in the
        // request extensions (prod layer order) so lazy-mint rescues → 200.
        let resp = client
            .get(format!("{}/api/v1/projects/f1-pid/config", base))
            .header("Authorization", "Bearer global-tok")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "F1: lazy-mint must be reachable through the production layer stack \
             (LauncherDbHandle visible to require_auth)"
        );
        assert!(
            registry.token_for("f1-pid").is_some(),
            "F1: lazy-mint must register the scoped token"
        );

        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }
}
