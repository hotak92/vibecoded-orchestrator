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

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    body::Body,
    extract::Request,
    http::{HeaderMap, Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};
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
        // parts[0] == module_id; parts[1] is the route family.
        if parts.len() >= 2 && (parts[1] == "db" || parts[1] == "token") {
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
    }
    false
}

/// Axum middleware: require `Authorization: Bearer <token>` on every
/// non-exempt request.
///
/// Wired in `server.rs` after the route nest and before the CORS layer
/// so OPTIONS preflight (which never carries Authorization) bypasses
/// auth and the CORS layer can answer it. Real requests that follow
/// the preflight DO get gated.
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
            );
        app.layer(axum::middleware::from_fn(require_auth))
            .layer(axum::Extension(auth_state))
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
        let base = spawn_router(router_with_auth("the-correct-token")).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/api/v1/projects/p1/env", base))
            .header("Authorization", "Bearer the-correct-token")
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.text().await.unwrap(), "secrets here");
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
}
