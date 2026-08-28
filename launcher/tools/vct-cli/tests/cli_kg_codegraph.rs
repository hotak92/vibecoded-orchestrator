//! Integration tests for the KG / codegraph CLI subcommands.
//!
//! Strategy
//! --------
//! - Build the `vco` binary via `env!("CARGO_BIN_EXE_vco")` (Cargo
//!   provides this for `[[bin]]` crates so we don't have to shell out
//!   to `cargo run`).
//! - For arg-parsing checks: invoke `vco <subcommand> --help` and grep
//!   the output. Cheap, deterministic, no network.
//! - For end-to-end behaviour: spawn a tiny axum stub on a random port
//!   that records the incoming request and returns a canned JSON body,
//!   point the CLI at it via `--port`, run it as a subprocess, parse
//!   stdout. Per the user's brief, no Weaviate mocks — but the *hub* is
//!   the binary boundary the CLI cares about, so a stub-hub is the
//!   right level of abstraction. (The hub-side cli_api.rs tests cover
//!   the real-Weaviate path.)

use std::io::Read;
use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

fn vco_bin() -> &'static str {
    env!("CARGO_BIN_EXE_vco")
}

/// Run `vco <args>` and return (stdout, stderr, exit_code).
fn run_vco(args: &[&str]) -> (String, String, i32) {
    let output = Command::new(vco_bin())
        .args(args)
        .env_remove("VCT_HUB_PORT")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("spawn vco");
    (
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
        output.status.code().unwrap_or(-1),
    )
}

#[test]
fn kg_subcommand_listed_in_top_help() {
    let (out, _err, code) = run_vco(&["--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("kg"), "top help missing 'kg'\n{}", out);
    assert!(out.contains("codegraph"), "top help missing 'codegraph'");
}

#[test]
fn kg_search_help_advertises_required_flags() {
    let (out, _err, code) = run_vco(&["kg", "search", "--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("--project"));
    assert!(out.contains("--collections"));
    assert!(out.contains("--limit"));
    assert!(out.contains("auto-detect"));
}

#[test]
fn codegraph_search_help_lists_scope_choices() {
    let (out, _err, code) = run_vco(&["codegraph", "search", "--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("--scope"));
    assert!(out.contains("CodeModule") || out.contains("code"));
    assert!(out.contains("interaction"));
}

#[test]
fn kg_search_requires_project_flag() {
    // Clap should reject the call with a non-zero exit and an error
    // mentioning the missing flag.
    let (_out, err, code) = run_vco(&["kg", "search", "test query"]);
    assert_ne!(code, 0);
    assert!(
        err.contains("--project") || err.contains("project"),
        "stderr should mention missing --project flag, got: {}",
        err
    );
}

#[test]
fn codegraph_search_rejects_unknown_subcommand() {
    let (_out, err, code) = run_vco(&["codegraph", "blarg"]);
    assert_ne!(code, 0);
    assert!(err.contains("error") || err.contains("Unknown") || err.contains("unrecognized"));
}

// ─── hooks enable/disable — `--project` is REQUIRED (v0.2.91 wave 5) ────
//
// Pre-fix `--project` was `Option<String>` and silently unused when
// omitted — the hub route toggled a DB mirror flag that nothing
// downstream read, so "which project" never mattered. Real enforcement
// edits the owning project's `.claude/settings.json`, so the hub cannot
// act without knowing the project; clap now refuses locally (never even
// reaches the network) rather than the hub having to refuse it remotely.
// Same shape as `kg_search_requires_project_flag` above.

#[test]
fn hooks_enable_requires_project_flag() {
    let (_out, err, code) = run_vco(&["hooks", "enable", "5"]);
    assert_ne!(code, 0);
    assert!(
        err.contains("--project") || err.contains("project"),
        "stderr should mention missing --project flag, got: {}",
        err
    );
}

#[test]
fn hooks_disable_requires_project_flag() {
    let (_out, err, code) = run_vco(&["hooks", "disable", "5"]);
    assert_ne!(code, 0);
    assert!(
        err.contains("--project") || err.contains("project"),
        "stderr should mention missing --project flag, got: {}",
        err
    );
}

#[test]
fn hooks_enable_help_advertises_required_project_flag() {
    let (out, _err, code) = run_vco(&["hooks", "enable", "--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("--project"), "help missing --project\n{}", out);
    assert!(out.contains("REQUIRED"), "help should say REQUIRED, not leave it implicit\n{}", out);
}

#[test]
fn hooks_disable_help_advertises_required_project_flag() {
    let (out, _err, code) = run_vco(&["hooks", "disable", "--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("--project"), "help missing --project\n{}", out);
    assert!(out.contains("REQUIRED"), "help should say REQUIRED, not leave it implicit\n{}", out);
}

// ─── End-to-end: CLI → stub hub ──────────────────────────────────────────
//
// We can't stand up the full Tauri launcher in a test, but we can
// reproduce the HTTP shape the CLI talks to. The stub records the
// request body so we can verify the CLI built the right payload.

struct StubHub {
    port: u16,
    last_body: Arc<Mutex<Option<String>>>,
    last_path: Arc<Mutex<Option<String>>>,
    _shutdown: std::sync::mpsc::Sender<()>,
}

impl StubHub {
    fn start_returning(canned: serde_json::Value) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        listener.set_nonblocking(false).ok();
        let port = listener.local_addr().unwrap().port();
        let last_body = Arc::new(Mutex::new(None));
        let last_path = Arc::new(Mutex::new(None));
        let (tx, rx) = std::sync::mpsc::channel::<()>();

        let body_clone = last_body.clone();
        let path_clone = last_path.clone();
        std::thread::spawn(move || {
            // Single-shot request handler is enough — each test issues
            // exactly one HTTP call. We loop in case the CLI follows up
            // with a project-resolve call (looks_like_uuid is a simple
            // dash-count heuristic; UUIDs slip through with no extra
            // request).
            listener
                .set_nonblocking(false)
                .ok();
            for stream in listener.incoming() {
                if rx.try_recv().is_ok() {
                    break;
                }
                let mut stream = match stream {
                    Ok(s) => s,
                    Err(_) => continue,
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .ok();

                let mut buf = [0u8; 8192];
                let n = stream.read(&mut buf).unwrap_or(0);
                let raw = String::from_utf8_lossy(&buf[..n]).to_string();

                // Crude HTTP/1.1 request parser — request line + headers
                // + body separated by `\r\n\r\n`.
                let (head, body) = raw.split_once("\r\n\r\n").unwrap_or((&raw, ""));
                let path = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("")
                    .to_string();
                *path_clone.lock().unwrap() = Some(path);
                if !body.is_empty() {
                    *body_clone.lock().unwrap() = Some(body.to_string());
                }

                let response = serde_json::to_string(&canned).unwrap();
                let http = format!(
                    "HTTP/1.1 200 OK\r\n\
                     Content-Type: application/json\r\n\
                     Content-Length: {}\r\n\
                     Connection: close\r\n\r\n{}",
                    response.len(),
                    response
                );
                let _ = std::io::Write::write_all(&mut stream, http.as_bytes());
            }
        });

        Self {
            port,
            last_body,
            last_path,
            _shutdown: tx,
        }
    }
}

#[test]
fn kg_collections_calls_correct_endpoint_and_emits_valid_json() {
    let canned = serde_json::json!({
        "collections": [
            {"name": "VibecodedOrchestrator_KnowledgeGraph", "node_count": 71}
        ],
        "count": 1
    });
    let hub = StubHub::start_returning(canned.clone());
    let port = hub.port.to_string();

    let (out, err, code) = run_vco(&["--port", &port, "kg", "collections"]);
    assert_eq!(code, 0, "stderr: {}", err);

    // Path must be the cli endpoint under /api/v1.
    let path = hub.last_path.lock().unwrap().clone().unwrap_or_default();
    assert_eq!(path, "/api/v1/cli/kg/collections");

    // stdout must be valid JSON matching the canned response.
    let parsed: serde_json::Value = serde_json::from_str(out.trim()).expect("parseable JSON");
    assert_eq!(parsed, canned);
}

#[test]
fn kg_search_serialises_collections_and_query_to_post_body() {
    // CLI under test: with --collections arg the body must contain the
    // explicit list (NOT auto-detect). project_id is the resolved UUID.
    // We bypass the project-by-slug lookup by passing a UUID-shaped
    // string (looks_like_uuid returns true → no extra HTTP call).
    let canned = serde_json::json!({
        "hits": [],
        "count": 0,
        "collections_searched": ["FooKG", "BarKG"],
        "auto_detected_collections": null
    });
    let hub = StubHub::start_returning(canned);
    let port = hub.port.to_string();

    let project_uuid = "12345678-1234-1234-1234-123456789012";
    let (out, err, code) = run_vco(&[
        "--port",
        &port,
        "kg",
        "search",
        "test query",
        "--project",
        project_uuid,
        "--collections",
        "FooKG,BarKG",
        "--limit",
        "5",
    ]);
    assert_eq!(code, 0, "stderr: {}", err);

    let path = hub.last_path.lock().unwrap().clone().unwrap_or_default();
    assert_eq!(path, "/api/v1/cli/kg/search");

    let body_str = hub.last_body.lock().unwrap().clone().unwrap_or_default();
    let body: serde_json::Value = serde_json::from_str(&body_str).expect("body is JSON");
    assert_eq!(body["project_id"].as_str(), Some(project_uuid));
    assert_eq!(body["query"].as_str(), Some("test query"));
    assert_eq!(body["limit"].as_u64(), Some(5));
    let cols: Vec<&str> = body["collections"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(cols, vec!["FooKG", "BarKG"]);

    // Output is parseable JSON.
    let _: serde_json::Value = serde_json::from_str(out.trim()).expect("stdout JSON");
}

#[test]
fn codegraph_search_passes_scope_in_body() {
    let canned = serde_json::json!({
        "hits": [],
        "count": 0,
        "scope": "code",
        "collections_searched": []
    });
    let hub = StubHub::start_returning(canned);
    let port = hub.port.to_string();

    let project_uuid = "abcdef12-3456-7890-abcd-ef1234567890";
    let (_out, err, code) = run_vco(&[
        "--port",
        &port,
        "codegraph",
        "search",
        "auth middleware",
        "--project",
        project_uuid,
        "--scope",
        "code",
    ]);
    assert_eq!(code, 0, "stderr: {}", err);

    let body_str = hub.last_body.lock().unwrap().clone().unwrap_or_default();
    let body: serde_json::Value = serde_json::from_str(&body_str).expect("body is JSON");
    assert_eq!(body["scope"].as_str(), Some("code"));
    assert_eq!(body["query"].as_str(), Some("auth middleware"));
    assert_eq!(body["project_id"].as_str(), Some(project_uuid));
}

#[test]
fn kg_search_without_collections_omits_field_so_hub_auto_detects() {
    // When --collections is missing, the body must NOT carry a
    // `collections` key. The hub uses absence as the auto-detect
    // trigger.
    let canned = serde_json::json!({
        "hits": [],
        "count": 0,
        "collections_searched": ["AutoKG"],
        "auto_detected_collections": ["AutoKG"]
    });
    let hub = StubHub::start_returning(canned);
    let port = hub.port.to_string();

    let project_uuid = "11111111-2222-3333-4444-555555555555";
    let (out, err, code) = run_vco(&[
        "--port",
        &port,
        "kg",
        "search",
        "x",
        "--project",
        project_uuid,
    ]);
    assert_eq!(code, 0, "stderr: {}", err);

    let body_str = hub.last_body.lock().unwrap().clone().unwrap_or_default();
    let body: serde_json::Value = serde_json::from_str(&body_str).expect("body is JSON");
    assert!(
        body.get("collections").is_none(),
        "body unexpectedly contains 'collections': {}",
        body_str
    );

    // Output should preserve the auto_detected_collections so a piped
    // `jq` consumer can see what was searched.
    let parsed: serde_json::Value = serde_json::from_str(out.trim()).expect("stdout JSON");
    assert!(parsed.get("auto_detected_collections").is_some());
}
