// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.36 Agent R — local HTTP server for the vendored diagrams editors
// (Mermaid + Excalidraw). Lazy-started on the first `open_diagrams_editor`
// invocation and lives for the lifetime of the launcher process.
//
// Architecture
// ============
//
// The launcher ships two static-build editors under
// `launcher/vendor/diagrams-editor/{mermaid,excalidraw}/`. Rather than
// open them inside the Tauri WebView (which has Wayland+webkit2gtk
// rendering bugs for both libraries — see
// docs/EXCALIDRAW_WAYLAND_TEST.md), we serve them on a free port at
// 127.0.0.1 and call `tauri-plugin-opener::open_url` so the user's
// DEFAULT BROWSER takes over.
//
// Routes
// ------
//
//   GET  /                              → 404 (no implicit redirect; the
//                                          frontend always passes the
//                                          /mermaid/ or /excalidraw/
//                                          subpath explicitly)
//   GET  /mermaid/[*path]               → serves files under
//                                          vendor/diagrams-editor/mermaid/
//                                          (default: index.html)
//   GET  /excalidraw/[*path]            → serves files under
//                                          vendor/diagrams-editor/excalidraw/
//   GET  /file?path=<rel_path>          → reads
//                                          <project>/<rel_path> (must be
//                                          inside `.claude/diagrams/`)
//                                          and returns the UTF-8 body
//   POST /save?path=<rel_path>          → writes the body verbatim to
//                                          <project>/<rel_path> via the
//                                          atomic sibling-tmp + rename
//                                          path used elsewhere.
//                                          REQUIRES `Authorization:
//                                          Bearer <per-boot token>` and
//                                          an allowlisted (or absent)
//                                          `Origin` header — see below.
//
// Security
// --------
//
//   1. Bound to 127.0.0.1 ONLY — no LAN exposure.
//   2. Path-traversal guard on every static-asset request: the resolved
//      path must `starts_with` the relevant vendor subdirectory after
//      lexical normalisation. Symlink escapes are rejected because we
//      use lexical normalisation, not `canonicalize` (no symlink
//      following).
//   3. Path-traversal guard on /file and /save: the resolved absolute
//      path must live inside ANY registered project's
//      `<project>/.claude/diagrams/` directory. Mirrors the per-call
//      guard used by `commands::diagrams_cmd::read_project_diagram_source`
//      and `write_text_file`.
//   4. POST /save is gated by BOTH a per-boot bearer token AND an
//      Origin-header check (v0.2.54 Track I, C-EX-3). Why both — and
//      why "no CORS headers" was NEVER a write-path defence:
//
//      The threat is a drive-by malicious web page in the user's own
//      browser. A cross-origin POST with a "simple" content type
//      (text/plain) is sent WITHOUT a CORS preflight — the absence of
//      CORS response headers only stops the attacker page from
//      READING our response; the write itself would have already
//      happened. (An earlier revision of this comment claimed the
//      missing CORS headers kept third-party pages out — that was
//      factually wrong for simple-request writes. CORS gates response
//      reads, not request delivery.) GET /file stays unauthenticated
//      for exactly that reason: a cross-origin reader gets an opaque
//      response it cannot read, so reads ARE protected by the missing
//      CORS headers.
//
//      Layer A — Origin check: browsers attach `Origin` to every
//      cross-origin POST and to same-origin fetch() POSTs; it is not
//      script-spoofable. We allow only our own origin
//      (`http://127.0.0.1:<port>` / `http://localhost:<port>` — the
//      editor pages are served by this very server) plus the Tauri
//      webview origins (`tauri://localhost` on Linux/macOS WebKit,
//      `http(s)://tauri.localhost` on Windows WebView2) in case the
//      launcher UI ever POSTs directly. `Origin: null` (sandboxed
//      iframes, file:// pages — attacker-producible) is REJECTED.
//      Requests with NO Origin header pass this layer: they cannot
//      come from a browser page (which always attaches Origin to
//      POSTs), so they're non-browser local clients — which the next
//      layer still gates.
//
//      Layer B — per-boot bearer token: minted from the OS CSPRNG at
//      server start (same `boot_token` primitives as vct-hub's
//      hub.token), persisted to `<vct_root_dir>/diagrams.token` mode
//      0o600, handed to the editor page via the URL fragment
//      (`#token=…` — fragments are never sent in HTTP requests, so
//      the token stays out of request lines and server logs). A web
//      attacker cannot read the token file, cannot read our responses
//      (no CORS headers), and cannot see the fragment of another
//      tab's URL — so even a request that somehow presented a clean
//      Origin would still fail without the token. Conversely, if the
//      token ever leaked, the Origin check still blocks browser-borne
//      writes from foreign pages. Defence in depth: each layer covers
//      the other's residual risk.
//
//      The Svelte frontend can obtain the token via the
//      `get_diagrams_token` Tauri command (reads the token file);
//      it is NOT baked into any bundle.
//
// State model
// -----------
//
// `DiagramsLocalServerState` lives behind a `tokio::sync::OnceCell` so
// the first call to `ensure_started` spawns the server, stores the
// port + cancellation channel, and subsequent calls short-circuit.
// The cell is never reset for the launcher process lifetime — we
// don't bother with shutdown; the tokio task is killed implicitly
// when the process exits.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio::sync::OnceCell;

use vct_launcher_core::services::boot_token;

use crate::db::Db;

// ─── Module-private state ──────────────────────────────────────────

/// Filename inside `vct_root_dir()` where the per-boot save token
/// persists for the lifetime of the launcher process. Mirrors the
/// `hub.token` pattern (vct-hub/src/auth.rs::TOKEN_FILE). Read back by
/// the `get_diagrams_token` Tauri command for the Svelte frontend.
pub const TOKEN_FILE: &str = "diagrams.token";

/// Lazily-initialised server-state singleton. Holds the bound port so
/// repeat `open_diagrams_editor` calls can build the URL without
/// re-checking whether the listener is alive.
pub struct DiagramsLocalServerState {
    pub port: u16,
    /// Absolute path to `<orchestrator-root>/launcher/vendor/diagrams-editor/`.
    /// Resolved once at startup so per-request handlers don't repeat the
    /// filesystem probe.
    pub vendor_root: PathBuf,
    /// Per-boot bearer token gating POST /save (see module Security
    /// notes, layer B). Regenerated on every launcher start; also
    /// persisted to `<vct_root_dir>/diagrams.token` (mode 0o600).
    pub token: String,
}

/// Process-wide singleton. The `OnceCell` provides "spawn on first use"
/// semantics; subsequent calls return the same Arc.
static SERVER_STATE: OnceCell<Arc<DiagramsLocalServerState>> = OnceCell::const_new();

/// Shared axum state carried into every handler. The Db is wrapped in
/// `Arc` because `Db` (rusqlite Connection inside a Mutex) is not
/// `Clone`. The vendor_root + port live inside the inner state arc so
/// we only clone two Arcs per request, never the heavyweight bits.
#[derive(Clone)]
struct AppState {
    db: Arc<Db>,
    state: Arc<DiagramsLocalServerState>,
}

// ─── Public entry points ───────────────────────────────────────────

/// Start the server if it isn't running yet. Returns the shared state
/// (bound port + per-boot save token). Idempotent: subsequent calls
/// short-circuit to the cached state.
///
/// `db` is an `Arc<Db>` rather than a borrowed reference so the spawn
/// closure can capture it cheaply. The Db is the same instance shared
/// with the rest of the Tauri command surface (we don't open a second
/// connection here — that would risk migration-ordering surprises).
///
/// Soft-fail policy: the caller is `commands::diagrams_cmd::open_diagrams_editor`,
/// which surfaces our `Err` as a toast in the UI. We don't auto-retry on
/// port-bind failure — the user clicks Draw again if something transient
/// stole every port in our scan range.
pub async fn ensure_started(
    db: Arc<Db>,
    vendor_root: PathBuf,
) -> Result<Arc<DiagramsLocalServerState>, String> {
    let state = SERVER_STATE
        .get_or_try_init(|| async { spawn_server(db, vendor_root).await })
        .await?;
    Ok(state.clone())
}

/// Resolve the absolute path to `launcher/vendor/diagrams-editor/`.
///
/// The launcher binary is typically at `<orchestrator-root>/launcher/dist/...`
/// (dev: `<orchestrator-root>/launcher/src-tauri/target/.../vct-launcher-temp`).
/// We walk up from `std::env::current_exe()` until we find a directory
/// containing `vendor/diagrams-editor/VENDORED.md` (the sentinel file we
/// shipped), capping the walk at 8 levels.
///
/// On failure we return an Err so the caller can surface a clear toast
/// (the editor server can't start without its static-asset directory).
pub fn resolve_vendor_root() -> Result<PathBuf, String> {
    let exe = std::env::current_exe()
        .map_err(|e| format!("current_exe failed: {}", e))?;
    let sentinel_rel = Path::new("vendor")
        .join("diagrams-editor")
        .join("VENDORED.md");

    // Walk up from the executable's parent looking for a sibling
    // `vendor/diagrams-editor/VENDORED.md`. Cover the dev + prod layouts:
    //   dev:  <root>/launcher/src-tauri/target/debug/vct-launcher-temp
    //         walks up to <root>/launcher/, finds vendor/...
    //   prod: <root>/launcher/dist/vct-launcher
    //         walks up to <root>/launcher/, finds vendor/...
    //
    // Also try `<exe-parent>/../launcher/vendor/...` to cover the
    // case where the binary moved out of the launcher subtree (uncommon
    // but possible).
    let mut cursor = exe.parent().map(|p| p.to_path_buf());
    for _ in 0..8 {
        let Some(dir) = cursor.clone() else { break };

        // (a) `<dir>/vendor/diagrams-editor/`
        let direct = dir.join(&sentinel_rel);
        if direct.is_file() {
            return Ok(dir.join("vendor").join("diagrams-editor"));
        }

        // (b) `<dir>/launcher/vendor/diagrams-editor/`
        let nested = dir.join("launcher").join(&sentinel_rel);
        if nested.is_file() {
            return Ok(dir.join("launcher").join("vendor").join("diagrams-editor"));
        }

        cursor = dir.parent().map(|p| p.to_path_buf());
    }

    Err(format!(
        "resolve_vendor_root: could not find vendor/diagrams-editor/ \
         within 8 parents of {}",
        exe.display(),
    ))
}

// ─── Server lifecycle ──────────────────────────────────────────────

async fn spawn_server(
    db: Arc<Db>,
    vendor_root: PathBuf,
) -> Result<Arc<DiagramsLocalServerState>, String> {
    if !vendor_root.is_dir() {
        return Err(format!(
            "diagrams local server: vendor root {} does not exist",
            vendor_root.display(),
        ));
    }

    // Bind FIRST so we know the port, then build the router with the
    // final state. We pick a port range above the well-known bands
    // (Weaviate 8081, Ollama 11435, hub 7700, code-embed 11440) to
    // reduce collision noise; the actual port is reported in the URL
    // we hand to `open_url`, so it doesn't matter which we land on.
    let (listener, port) = try_bind_in_range(22000, 20).await?;

    // Mint the per-boot save token and persist it for the Svelte
    // frontend (`get_diagrams_token` command). Same generate + 0o600
    // persistence primitives as vct-hub's hub.token. A persist failure
    // is fatal for the server start: a token that exists in memory but
    // not on disk would leave the frontend permanently unable to
    // authenticate, which is worse than a clear startup error.
    let token = boot_token::generate_token()?;
    let token_path = vct_launcher_core::paths::vct_root_dir().join(TOKEN_FILE);
    boot_token::write_token_file(&token_path, &token)?;

    let final_state = Arc::new(DiagramsLocalServerState {
        port,
        vendor_root,
        token,
    });

    let app_state = AppState {
        db,
        state: final_state.clone(),
    };

    let app = build_router(app_state);

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!("[diagrams-local-server] axum::serve exited: {}", e);
        }
    });

    tracing::info!(
        "[diagrams-local-server] listening on http://127.0.0.1:{} (vendor_root={})",
        port,
        final_state.vendor_root.display(),
    );
    Ok(final_state)
}

/// Build the axum router. Factored out of `spawn_server` so the
/// HTTP-level tests can mount the exact production route table +
/// auth gate on a test listener without going through the singleton.
fn build_router(app_state: AppState) -> Router {
    Router::new()
        .route("/mermaid/", get(serve_mermaid_index))
        .route("/mermaid/{*tail}", get(serve_mermaid_asset))
        .route("/excalidraw/", get(serve_excalidraw_index))
        .route("/excalidraw/{*tail}", get(serve_excalidraw_asset))
        .route("/file", get(read_file_handler))
        .route("/save", post(save_file_handler))
        .with_state(app_state)
}

/// Bind to the first available port in `[base, base+span)`. We probe
/// the range in order so the first available port wins; ports already
/// taken (other launchers, hub, etc.) are skipped silently. Returns the
/// listener and the port we bound to.
async fn try_bind_in_range(base: u16, span: u16) -> Result<(TcpListener, u16), String> {
    let mut last_err: Option<std::io::Error> = None;
    for offset in 0..span {
        let port = base + offset;
        let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
        match TcpListener::bind(addr).await {
            Ok(l) => return Ok((l, port)),
            Err(e) => {
                last_err = Some(e);
                continue;
            }
        }
    }
    Err(format!(
        "diagrams-local-server: every port in {}..{} was busy (last error: {:?})",
        base,
        base + span,
        last_err,
    ))
}

// ─── Handlers: static asset serving ────────────────────────────────

async fn serve_mermaid_index(State(s): State<AppState>) -> Response {
    serve_static_file(&s.state.vendor_root.join("mermaid").join("index.html"), "text/html; charset=utf-8").await
}

async fn serve_mermaid_asset(
    State(s): State<AppState>,
    axum::extract::Path(tail): axum::extract::Path<String>,
) -> Response {
    serve_under(&s.state.vendor_root.join("mermaid"), &tail).await
}

async fn serve_excalidraw_index(State(s): State<AppState>) -> Response {
    serve_static_file(
        &s.state.vendor_root.join("excalidraw").join("index.html"),
        "text/html; charset=utf-8",
    )
    .await
}

async fn serve_excalidraw_asset(
    State(s): State<AppState>,
    axum::extract::Path(tail): axum::extract::Path<String>,
) -> Response {
    serve_under(&s.state.vendor_root.join("excalidraw"), &tail).await
}

/// Serve a file under `subdir`. The `tail` is the URL path segment
/// AFTER the editor prefix (`/mermaid/foo/bar.js` → tail=`foo/bar.js`).
/// We reject any tail that contains `..` segments or whose lexical
/// normalisation escapes `subdir`.
async fn serve_under(subdir: &Path, tail: &str) -> Response {
    // Reject `..` traversal at the URL level. axum's path extractor
    // already strips leading `/` but doesn't reject `..` segments.
    if tail.split('/').any(|seg| seg == ".." || seg == ".") {
        return (StatusCode::FORBIDDEN, "path traversal").into_response();
    }
    if tail.is_empty() {
        return serve_static_file(&subdir.join("index.html"), "text/html; charset=utf-8").await;
    }
    let candidate = subdir.join(tail);
    // Lexical normalisation + starts_with check is the same defence as
    // commands::diagrams_cmd::resolve_inside_project — we don't follow
    // symlinks so an attacker who somehow plants a symlink in the
    // vendor tree can't escape into the user's home dir.
    let normalised = lexical_normalize(&candidate);
    let subdir_normalised = lexical_normalize(subdir);
    if !normalised.starts_with(&subdir_normalised) {
        return (StatusCode::FORBIDDEN, "path escapes vendor subdir").into_response();
    }
    let mime = mime_for_ext(&normalised);
    serve_static_file(&normalised, mime).await
}

async fn serve_static_file(path: &Path, mime: &str) -> Response {
    match tokio::fs::read(path).await {
        Ok(bytes) => {
            let mut headers = HeaderMap::new();
            headers.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_str(mime).unwrap_or(HeaderValue::from_static("application/octet-stream")),
            );
            // No-cache on the editor pages: the user expects edits to
            // mermaid/index.html to reload on a refresh during dev.
            headers.insert(
                header::CACHE_CONTROL,
                HeaderValue::from_static("no-cache"),
            );
            (StatusCode::OK, headers, bytes).into_response()
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            (StatusCode::NOT_FOUND, format!("not found: {}", path.display())).into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("read failed: {}", e),
        )
            .into_response(),
    }
}

fn mime_for_ext(path: &Path) -> &'static str {
    match path.extension().and_then(|s| s.to_str()).unwrap_or("") {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "map" => "application/json; charset=utf-8",
        _ => "application/octet-stream",
    }
}

// ─── Handlers: file IO ─────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct FilePathQuery {
    path: String,
}

async fn read_file_handler(
    State(s): State<AppState>,
    Query(q): Query<FilePathQuery>,
) -> Response {
    let abs = match resolve_diagrams_path(&s.db, &q.path) {
        Ok(p) => p,
        Err(e) => return (StatusCode::FORBIDDEN, e).into_response(),
    };
    match tokio::fs::read_to_string(&abs).await {
        Ok(s) => (StatusCode::OK, s).into_response(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Blank-new-file path: the launcher creates the file
            // before opening the editor, but a race (or a user deleting
            // it) can land us here. Returning 404 with an empty body
            // tells the editor's JS to boot empty.
            (StatusCode::NOT_FOUND, "").into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("read failed: {}", e),
        )
            .into_response(),
    }
}

async fn save_file_handler(
    State(s): State<AppState>,
    Query(q): Query<FilePathQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // Auth gate FIRST — before any path resolution or disk work. See
    // the module-level Security notes (item 4) for why both layers.
    if let Err(resp) = authorize_save(&headers, &s.state.token, s.state.port) {
        return resp;
    }
    let abs = match resolve_diagrams_path(&s.db, &q.path) {
        Ok(p) => p,
        Err(e) => return (StatusCode::FORBIDDEN, e).into_response(),
    };
    match write_file_atomic(&abs, &body) {
        Ok(()) => (StatusCode::OK, "saved").into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

// ─── POST /save auth gate ──────────────────────────────────────────

/// Validate the Origin header + per-boot bearer token for a /save
/// request. Returns `Err(response)` with the appropriate status on
/// rejection:
///
///   * 403 — Origin present but not allowlisted (browser-borne
///     cross-origin write attempt, the exact drive-by the gate exists
///     to stop). Includes `Origin: null` (sandboxed iframes / file://
///     pages — attacker-producible, so never allowlisted).
///   * 401 — missing / malformed / wrong bearer token.
///
/// Order: Origin first (cheapest, catches the browser drive-by class
/// outright), token second.
fn authorize_save(headers: &HeaderMap, expected_token: &str, port: u16) -> Result<(), Response> {
    // Layer A — Origin allowlist.
    match headers.get(header::ORIGIN) {
        None => {
            // No Origin → cannot be a browser-page POST (browsers
            // always attach Origin to fetch/XHR/form POSTs). A local
            // non-browser client; layer B still gates it.
        }
        Some(raw) => {
            let origin = raw.to_str().unwrap_or("");
            if !origin_allowed(origin, port) {
                return Err((
                    StatusCode::FORBIDDEN,
                    "origin not allowed for /save".to_string(),
                )
                    .into_response());
            }
        }
    }

    // Layer B — per-boot bearer token (constant-time compare; same
    // primitives as vct-hub's hub.token middleware).
    let provided = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(boot_token::parse_bearer);
    match provided {
        Some(tok) if boot_token::constant_time_eq(tok.as_bytes(), expected_token.as_bytes()) => {
            Ok(())
        }
        _ => Err((
            StatusCode::UNAUTHORIZED,
            "missing or invalid bearer token for /save".to_string(),
        )
            .into_response()),
    }
}

/// Whether a (present, non-empty) Origin header value is allowed to
/// POST /save. Allowlist:
///
///   * our own origin — the editor pages are served by this server,
///     so their fetch() POSTs carry `http://127.0.0.1:<port>` (or
///     `http://localhost:<port>` if the user retyped the URL).
///   * the Tauri webview origins, in case the launcher UI ever POSTs
///     directly: `tauri://localhost` (Linux/macOS WKWebView/WebKitGTK)
///     and `http://tauri.localhost` / `https://tauri.localhost`
///     (Windows WebView2).
///
/// Everything else — including `null` — is rejected.
fn origin_allowed(origin: &str, port: u16) -> bool {
    if origin == format!("http://127.0.0.1:{}", port)
        || origin == format!("http://localhost:{}", port)
    {
        return true;
    }
    matches!(
        origin,
        "tauri://localhost" | "http://tauri.localhost" | "https://tauri.localhost"
    )
}

// ─── Path resolution + atomic write ────────────────────────────────

/// Resolve a project-relative path under SOME registered project's
/// `.claude/diagrams/` directory. Returns the absolute path on success.
///
/// Security: identical contract to
/// `commands::diagrams_cmd::read_project_diagram_source` — the resolved
/// path must:
///   1. Lexically normalise to a path that doesn't traverse out of any
///      registered project's `.claude/diagrams/` directory.
///   2. Match at least one such project.
///
/// We scan every registered project rather than requiring a `project_id`
/// query parameter because the editor URL contract (passed verbatim
/// through `?file=<rel_path>` from open_diagrams_editor) doesn't carry
/// the project id. The /file and /save handlers are stateless — they
/// see the rel_path and find which project owns it. If two projects
/// shared a path prefix (rare but possible — e.g. `/tmp/a/.claude/diagrams/x`
/// and `/tmp/a/.claude/diagrams/x` overlap because two project rows
/// point at the same folder) the first match wins; both projects would
/// observe the write since the filesystem path is identical.
fn resolve_diagrams_path(db: &Db, rel_path: &str) -> Result<PathBuf, String> {
    // Bail fast on absolute paths — the contract is project-relative.
    if Path::new(rel_path).is_absolute() {
        return Err(format!(
            "rel_path must be project-relative (got absolute: {})",
            rel_path,
        ));
    }
    // Refuse `..` segments at the URL level — these would lexically
    // normalise to a path OUTSIDE `.claude/diagrams/` and the loop
    // below would also refuse it, but failing fast at the syntax level
    // gives a clearer error.
    if rel_path.split('/').any(|s| s == "..") {
        return Err(format!("`..` not allowed in rel_path: {}", rel_path));
    }

    let projects = db
        .list_projects()
        .map_err(|e| format!("list_projects: {}", e))?;
    for p in projects {
        let folder = PathBuf::from(&p.folder_path);
        let candidate = folder.join(rel_path);
        let normalised = lexical_normalize(&candidate);
        let diagrams_root = lexical_normalize(&folder.join(".claude").join("diagrams"));
        if normalised.starts_with(&diagrams_root) {
            return Ok(normalised);
        }
    }
    Err(format!(
        "rel_path {} did not resolve inside any project's .claude/diagrams/",
        rel_path,
    ))
}

/// Lexical (no-disk-touch) path normaliser. Mirror of the same fn in
/// `commands::diagrams_cmd::lexical_normalize`. We duplicate rather than
/// re-export to avoid pulling that module's private surface into our
/// public API. The normalisation rules MUST match exactly — if they
/// drift, the two modules will validate paths inconsistently.
fn lexical_normalize(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Atomic file write via sibling-tmp + rename. Mirror of the same fn in
/// `commands::diagrams_cmd::write_file_atomic`. Cross-OS atomic via
/// `fs::rename` (POSIX rename, Windows MoveFileExW with REPLACE_EXISTING).
fn write_file_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::fs;
    use std::io::Write;

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }
    let mut tmp = path.to_path_buf();
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "diagram".to_string());
    tmp.set_file_name(format!(".{}.vct-editor.tmp", name));
    {
        let mut f = fs::File::create(&tmp)
            .map_err(|e| format!("create {}: {}", tmp.display(), e))?;
        f.write_all(bytes)
            .map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        f.sync_all()
            .map_err(|e| format!("sync {}: {}", tmp.display(), e))?;
    }
    fs::rename(&tmp, path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("rename {} -> {}: {}", tmp.display(), path.display(), e)
    })?;
    Ok(())
}

// ─── Tests ──────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn lexical_normalize_collapses_dot_segments() {
        assert_eq!(
            lexical_normalize(Path::new("/p/.claude/diagrams/../x.mmd")),
            PathBuf::from("/p/.claude/x.mmd"),
        );
        assert_eq!(
            lexical_normalize(Path::new("/p/./a/./b")),
            PathBuf::from("/p/a/b"),
        );
        assert_eq!(
            lexical_normalize(Path::new("/p/a/../../etc/passwd")),
            PathBuf::from("/etc/passwd"),
        );
    }

    #[test]
    fn mime_for_ext_maps_common_types() {
        assert_eq!(mime_for_ext(Path::new("a.html")), "text/html; charset=utf-8");
        assert_eq!(
            mime_for_ext(Path::new("b.js")),
            "application/javascript; charset=utf-8",
        );
        assert_eq!(mime_for_ext(Path::new("c.css")), "text/css; charset=utf-8");
        assert_eq!(mime_for_ext(Path::new("d.svg")), "image/svg+xml");
        assert_eq!(
            mime_for_ext(Path::new("unknown.xyz")),
            "application/octet-stream",
        );
    }

    #[test]
    fn write_file_atomic_creates_and_replaces() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("nested").join("scene.excalidraw");
        write_file_atomic(&target, br#"{"version":2}"#).unwrap();
        assert_eq!(fs::read(&target).unwrap(), br#"{"version":2}"#);
        write_file_atomic(&target, br#"{"version":3}"#).unwrap();
        assert_eq!(fs::read(&target).unwrap(), br#"{"version":3}"#);
        // Tmp sibling cleaned up.
        let siblings: Vec<_> = fs::read_dir(target.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".vct-editor.tmp"))
            .collect();
        assert!(siblings.is_empty(), "tmp sibling should be cleaned up");
    }

    #[test]
    fn resolve_diagrams_path_rejects_absolute_paths() {
        let db = Db::open_in_memory().unwrap();
        let abs = if cfg!(windows) {
            r"C:\etc\passwd"
        } else {
            "/etc/passwd"
        };
        let err = resolve_diagrams_path(&db, abs).unwrap_err();
        assert!(err.contains("absolute"));
    }

    #[test]
    fn resolve_diagrams_path_rejects_dotdot_segments() {
        let db = Db::open_in_memory().unwrap();
        let err = resolve_diagrams_path(&db, ".claude/diagrams/../../etc/passwd").unwrap_err();
        assert!(err.contains(".."));
    }

    #[test]
    fn resolve_diagrams_path_accepts_path_inside_registered_project() {
        use crate::db::models::ProjectHost;
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        std::fs::create_dir_all(project.join(".claude").join("diagrams").join("g")).unwrap();
        let db = Db::open_in_memory().unwrap();
        let slug = db.generate_unique_slug("Acme").unwrap();
        db.insert_project(
            "p1",
            "Acme",
            project.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        let resolved = resolve_diagrams_path(&db, ".claude/diagrams/g/x.mmd").unwrap();
        let expected = lexical_normalize(&project.join(".claude/diagrams/g/x.mmd"));
        assert_eq!(resolved, expected);
    }

    #[test]
    fn resolve_diagrams_path_rejects_path_outside_diagrams_root() {
        use crate::db::models::ProjectHost;
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        std::fs::create_dir_all(project.join(".claude").join("diagrams")).unwrap();
        std::fs::create_dir_all(project.join("secrets")).unwrap();
        let db = Db::open_in_memory().unwrap();
        let slug = db.generate_unique_slug("Acme").unwrap();
        db.insert_project(
            "p1",
            "Acme",
            project.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        // Inside project but outside .claude/diagrams/ — must fail.
        let err = resolve_diagrams_path(&db, "secrets/creds.txt").unwrap_err();
        assert!(err.contains("did not resolve inside any project"));
    }

    // ─── POST /save auth gate (v0.2.54 Track I, C-EX-3) ─────────────

    #[test]
    fn origin_allowed_accepts_own_and_tauri_origins() {
        assert!(origin_allowed("http://127.0.0.1:22003", 22003));
        assert!(origin_allowed("http://localhost:22003", 22003));
        assert!(origin_allowed("tauri://localhost", 22003));
        assert!(origin_allowed("http://tauri.localhost", 22003));
        assert!(origin_allowed("https://tauri.localhost", 22003));
    }

    #[test]
    fn origin_allowed_rejects_foreign_null_and_port_mismatch() {
        assert!(!origin_allowed("https://evil.example", 22003));
        // `Origin: null` is attacker-producible (sandboxed iframe,
        // file:// page) — must never be allowlisted.
        assert!(!origin_allowed("null", 22003));
        // Same host, wrong port → a DIFFERENT 127.0.0.1 service's page
        // (or a stale tab from a previous boot's port). Reject.
        assert!(!origin_allowed("http://127.0.0.1:22004", 22003));
        assert!(!origin_allowed("", 22003));
        // https on loopback is not how we serve — reject.
        assert!(!origin_allowed("https://127.0.0.1:22003", 22003));
    }

    // ── HTTP-level tests: production router on a real listener ──
    //
    // Same spawn-on-random-port + reqwest pattern as vct-hub's
    // auth.rs tests — exercises the same axum::serve code path the
    // production server runs, no extra dev-deps.

    /// Bind a listener, build the production router around a fresh
    /// in-memory Db with one registered project, serve it. Returns
    /// (base_url, port, project_dir_guard). The tempdir guard must
    /// stay alive for the duration of the test (the save handler
    /// writes into it).
    async fn spawn_test_server(token: &str) -> (String, u16, tempfile::TempDir) {
        use crate::db::models::ProjectHost;

        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        std::fs::create_dir_all(project.join(".claude").join("diagrams")).unwrap();

        let db = Db::open_in_memory().unwrap();
        let slug = db.generate_unique_slug("Acme").unwrap();
        db.insert_project(
            "p1",
            "Acme",
            project.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        let app_state = AppState {
            db: Arc::new(db),
            state: Arc::new(DiagramsLocalServerState {
                port,
                vendor_root: dir.path().join("vendor-unused"),
                token: token.to_string(),
            }),
        };
        let app = build_router(app_state);
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (format!("http://127.0.0.1:{}", port), port, dir)
    }

    const SAVE_PATH: &str = "/save?path=.claude/diagrams/x.mmd";

    #[tokio::test]
    async fn save_rejects_request_without_bearer_token() {
        let (base, _port, _dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .body("flowchart TD")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn save_rejects_request_with_wrong_bearer_token() {
        let (base, _port, _dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .header("Authorization", "Bearer tok-wrong")
            .body("flowchart TD")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn save_rejects_foreign_origin_even_with_correct_token() {
        // Defence in depth: a leaked token presented from a browser
        // page on a foreign origin must STILL be rejected.
        let (base, _port, _dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .header("Authorization", "Bearer tok-secret")
            .header("Origin", "https://evil.example")
            .body("flowchart TD")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn save_rejects_null_origin() {
        let (base, _port, _dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .header("Authorization", "Bearer tok-secret")
            .header("Origin", "null")
            .body("flowchart TD")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn save_accepts_correct_token_with_own_origin_and_writes_file() {
        // The happy path the mermaid editor page exercises: fetch()
        // from the page this server itself served → Origin is our own
        // origin, Authorization carries the fragment token.
        let (base, port, dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .header("Authorization", "Bearer tok-secret")
            .header("Origin", format!("http://127.0.0.1:{}", port))
            .body("flowchart TD\n  A --> B")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let written = dir.path().join("proj/.claude/diagrams/x.mmd");
        assert_eq!(
            std::fs::read_to_string(written).unwrap(),
            "flowchart TD\n  A --> B",
        );
    }

    #[tokio::test]
    async fn save_accepts_correct_token_without_origin_header() {
        // Non-browser local client (no Origin) — layer A passes it
        // through, layer B (token) is the gate. reqwest sends no
        // Origin header by default.
        let (base, _port, dir) = spawn_test_server("tok-secret").await;
        let resp = reqwest::Client::new()
            .post(format!("{}{}", base, SAVE_PATH))
            .header("Authorization", "Bearer tok-secret")
            .body("graph LR")
            .send()
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        assert!(dir.path().join("proj/.claude/diagrams/x.mmd").is_file());
    }

    #[tokio::test]
    async fn file_read_stays_unauthenticated() {
        // GET /file is deliberately outside the token gate: reads are
        // already protected cross-origin by the ABSENCE of CORS
        // headers (the attacker page gets an opaque response). See the
        // module Security notes, item 4.
        let (base, _port, dir) = spawn_test_server("tok-secret").await;
        std::fs::write(
            dir.path().join("proj/.claude/diagrams/x.mmd"),
            "flowchart TD",
        )
        .unwrap();
        let resp = reqwest::get(format!("{}/file?path=.claude/diagrams/x.mmd", base))
            .await
            .expect("server reachable");
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        assert_eq!(resp.text().await.unwrap(), "flowchart TD");
    }
}
