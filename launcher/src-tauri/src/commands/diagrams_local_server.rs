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
//                                          path used elsewhere
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
//   4. No CORS headers added — only the user's own browser will hit
//      127.0.0.1, and we deliberately don't want third-party pages to
//      reach the editor server.
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

use crate::db::Db;

// ─── Module-private state ──────────────────────────────────────────

/// Lazily-initialised server-state singleton. Holds the bound port so
/// repeat `open_diagrams_editor` calls can build the URL without
/// re-checking whether the listener is alive.
pub struct DiagramsLocalServerState {
    pub port: u16,
    /// Absolute path to `<orchestrator-root>/launcher/vendor/diagrams-editor/`.
    /// Resolved once at startup so per-request handlers don't repeat the
    /// filesystem probe.
    pub vendor_root: PathBuf,
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

/// Start the server if it isn't running yet. Returns the bound port.
/// Idempotent: subsequent calls short-circuit to the cached port.
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
pub async fn ensure_started(db: Arc<Db>, vendor_root: PathBuf) -> Result<u16, String> {
    let state = SERVER_STATE
        .get_or_try_init(|| async { spawn_server(db, vendor_root).await })
        .await?;
    Ok(state.port)
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

    let final_state = Arc::new(DiagramsLocalServerState {
        port,
        vendor_root,
    });

    let app_state = AppState {
        db,
        state: final_state.clone(),
    };

    let app = Router::new()
        .route("/mermaid/", get(serve_mermaid_index))
        .route("/mermaid/{*tail}", get(serve_mermaid_asset))
        .route("/excalidraw/", get(serve_excalidraw_index))
        .route("/excalidraw/{*tail}", get(serve_excalidraw_asset))
        .route("/file", get(read_file_handler))
        .route("/save", post(save_file_handler))
        .with_state(app_state);

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            eprintln!("[diagrams-local-server] axum::serve exited: {}", e);
        }
    });

    eprintln!(
        "[diagrams-local-server] listening on http://127.0.0.1:{} (vendor_root={})",
        port,
        final_state.vendor_root.display(),
    );
    Ok(final_state)
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
    body: Bytes,
) -> Response {
    let abs = match resolve_diagrams_path(&s.db, &q.path) {
        Ok(p) => p,
        Err(e) => return (StatusCode::FORBIDDEN, e).into_response(),
    };
    match write_file_atomic(&abs, &body) {
        Ok(()) => (StatusCode::OK, "saved").into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
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
}
