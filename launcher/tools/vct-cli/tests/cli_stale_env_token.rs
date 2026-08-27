//! v0.2.91 (WP-D item 4) — stale-env hub-token fallback in the `vco` CLI.
//!
//! THE SEAM
//! --------
//! `resolve_token()` prefers `$VCT_HUB_TOKEN` over `<vct_root>/hub.token`.
//! The hub regenerates `hub.token` on every start, so a shell that
//! exported the token BEFORE an update presents a value the hub refuses —
//! and every `vco` invocation from that shell died with
//! `hub error 401 Unauthorized`, pointing the user at the launcher rather
//! than at their own environment.
//!
//! PINNED HERE
//! -----------
//! * a PROVABLE refusal (401/403) + a provably-stale pin ⇒ ONE retry with
//!   the on-disk token, one definitive stderr line, success;
//! * `VCT_HUB_TOKEN_STRICT=1` ⇒ the pin is authoritative (leave-alone);
//! * identical tokens / no on-disk token ⇒ exactly ONE request, and the
//!   pre-v0.2.91 error text.
//!
//! Strategy mirrors `cli_kg_codegraph.rs`: a hand-rolled single-threaded
//! HTTP stub on a random port, the CLI pointed at it with `--port`. The
//! stub records every bearer it sees so the retry is observable. All
//! tokens are obviously synthetic.

use std::io::Read;
use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const STALE_ENV_TOKEN: &str = "stale-env-token-v0291-not-a-real-secret";
const FRESH_DISK_TOKEN: &str = "fresh-disk-token-v0291-not-a-real-secret";
const DEFINITIVE_LINE: &str = "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token";

fn vco_bin() -> &'static str {
    env!("CARGO_BIN_EXE_vco")
}

/// A stub hub that 401s every bearer except `expected`, and records the
/// bearer of every request it served.
struct AuthStubHub {
    port: u16,
    bearers: Arc<Mutex<Vec<String>>>,
}

impl AuthStubHub {
    fn start(expected: &str) -> Self {
        Self::start_answering(
            expected,
            "200 OK",
            serde_json::json!({"projects": [], "count": 0}).to_string(),
        )
    }

    /// Same stub, but the ACCEPTED bearer gets `ok_status` / `ok_body`.
    ///
    /// v0.2.91 wave-3 (MINOR-1): lets a test express "the hub refuses the
    /// stale pin, then answers the fallback with something that is NOT
    /// proof the credential was accepted".
    fn start_answering(expected: &str, ok_status: &str, ok_body: String) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().unwrap().port();
        let bearers = Arc::new(Mutex::new(Vec::new()));
        let seen = bearers.clone();
        let expected = expected.to_string();
        let ok_status = ok_status.to_string();

        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream = match stream {
                    Ok(s) => s,
                    Err(_) => continue,
                };
                stream.set_read_timeout(Some(Duration::from_secs(2))).ok();
                let mut buf = [0u8; 8192];
                let n = stream.read(&mut buf).unwrap_or(0);
                let raw = String::from_utf8_lossy(&buf[..n]).to_string();

                let bearer = raw
                    .lines()
                    .find(|l| l.to_ascii_lowercase().starts_with("authorization:"))
                    .and_then(|l| l.split_whitespace().nth(2))
                    .unwrap_or("")
                    .to_string();
                seen.lock().unwrap().push(bearer.clone());

                let (status, body) = if bearer == expected {
                    (ok_status.clone(), ok_body.clone())
                } else {
                    (
                        "401 Unauthorized".to_string(),
                        serde_json::json!({
                            "error": {"code": "unauthorized", "message": "bad token"}
                        })
                        .to_string(),
                    )
                };
                let http = format!(
                    "HTTP/1.1 {}\r\n\
                     Content-Type: application/json\r\n\
                     Content-Length: {}\r\n\
                     Connection: close\r\n\r\n{}",
                    status,
                    body.len(),
                    body
                );
                let _ = std::io::Write::write_all(&mut stream, http.as_bytes());
            }
        });

        Self { port, bearers }
    }

    fn bearers(&self) -> Vec<String> {
        self.bearers.lock().unwrap().clone()
    }
}

/// Run `vco project list` against `port` with a fully-controlled env.
fn run_vco(
    port: u16,
    state_dir: &std::path::Path,
    env_token: Option<&str>,
    strict: bool,
) -> (String, String, i32) {
    let mut cmd = Command::new(vco_bin());
    cmd.args(["--port", &port.to_string(), "project", "list"])
        .env_remove("VCT_HUB_PORT")
        .env_remove("VCT_HUB_TOKEN")
        .env_remove("VCT_HUB_TOKEN_STRICT")
        .env("VCT_STATE_DIR", state_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(t) = env_token {
        cmd.env("VCT_HUB_TOKEN", t);
    }
    if strict {
        cmd.env("VCT_HUB_TOKEN_STRICT", "1");
    }
    let out = cmd.output().expect("spawn vco");
    (
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
        out.status.code().unwrap_or(-1),
    )
}

/// Minimal RAII temp dir (this crate has no `tempfile` dev-dependency and
/// the test needs only a directory that disappears afterwards).
struct TempStateDir(std::path::PathBuf);

impl TempStateDir {
    fn new(token: Option<&str>) -> Self {
        let unique = format!(
            "vct-cli-stale-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let path = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&path).expect("create temp state dir");
        if let Some(t) = token {
            std::fs::write(path.join("hub.token"), t).expect("write hub.token");
        }
        Self(path)
    }

    fn path(&self) -> &std::path::Path {
        &self.0
    }
}

impl Drop for TempStateDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn state_dir_with_token(token: Option<&str>) -> TempStateDir {
    TempStateDir::new(token)
}

#[test]
fn stale_env_token_is_retried_once_with_the_on_disk_token() {
    // RED-PROOF: pre-v0.2.91 the CLI made ONE request with the stale
    // token and exited 1 with `hub error 401 Unauthorized`.
    let hub = AuthStubHub::start(FRESH_DISK_TOKEN);
    let state = state_dir_with_token(Some(FRESH_DISK_TOKEN));

    let (out, err, code) = run_vco(hub.port, state.path(), Some(STALE_ENV_TOKEN), false);

    assert_eq!(code, 0, "stdout={out} stderr={err}");
    assert!(out.contains("count"), "expected the hub's JSON body: {out}");
    assert_eq!(
        hub.bearers(),
        vec![STALE_ENV_TOKEN.to_string(), FRESH_DISK_TOKEN.to_string()],
        "the retry must present the ON-DISK token",
    );
    assert!(
        err.contains(DEFINITIVE_LINE),
        "expected the definitive stderr line, got: {err}",
    );
    // The values themselves must never reach a diagnostic.
    assert!(!err.contains(FRESH_DISK_TOKEN) && !err.contains(STALE_ENV_TOKEN));
}

#[test]
fn strict_guard_keeps_the_401_path() {
    // LEAVE-ALONE: a harness pinning a deliberately-wrong token still
    // observes the refusal, and no extra request is made.
    let hub = AuthStubHub::start(FRESH_DISK_TOKEN);
    let state = state_dir_with_token(Some(FRESH_DISK_TOKEN));

    let (_out, err, code) = run_vco(hub.port, state.path(), Some(STALE_ENV_TOKEN), true);

    assert_ne!(code, 0, "strict mode must keep the failure");
    assert!(err.contains("401"), "expected the 401 error text: {err}");
    assert!(!err.contains(DEFINITIVE_LINE));
    assert_eq!(hub.bearers(), vec![STALE_ENV_TOKEN.to_string()]);
}

#[test]
fn identical_tokens_make_exactly_one_request() {
    // LEAVE-ALONE: the pin is not stale — nothing to fall back to.
    let hub = AuthStubHub::start(FRESH_DISK_TOKEN);
    let state = state_dir_with_token(Some(FRESH_DISK_TOKEN));

    let (_out, err, code) = run_vco(hub.port, state.path(), Some(FRESH_DISK_TOKEN), false);

    assert_eq!(code, 0, "stderr={err}");
    assert_eq!(hub.bearers(), vec![FRESH_DISK_TOKEN.to_string()]);
    assert!(!err.contains(DEFINITIVE_LINE));
}

#[test]
fn a_5xx_on_the_retry_keeps_the_original_401_and_prints_no_definitive_line() {
    // v0.2.91 wave-3 (MINOR-1). RED pre-fix: ANY non-401/403 retry answer
    // was adopted, so a hub that refused the stale pin and then hiccuped a
    // 503 printed "stale VCT_HUB_TOKEN…" and surfaced `hub error 503` —
    // sending the user after an environment problem that may not exist,
    // and hiding the real 401.
    let hub = AuthStubHub::start_answering(
        FRESH_DISK_TOKEN,
        "503 Service Unavailable",
        serde_json::json!({"error": {"code": "unavailable"}}).to_string(),
    );
    let state = state_dir_with_token(Some(FRESH_DISK_TOKEN));

    let (_out, err, code) = run_vco(hub.port, state.path(), Some(STALE_ENV_TOKEN), false);

    assert_ne!(code, 0);
    assert!(
        err.contains("401"),
        "the ORIGINAL refusal must be what the user sees: {err}",
    );
    assert!(!err.contains("503"), "the unproven answer must not surface: {err}");
    assert!(!err.contains(DEFINITIVE_LINE), "no definitive claim on no evidence: {err}");
    assert_eq!(
        hub.bearers(),
        vec![STALE_ENV_TOKEN.to_string(), FRESH_DISK_TOKEN.to_string()],
        "the retry still happens — only its ANSWER is rejected",
    );
}

#[test]
fn a_404_on_the_retry_is_adopted_because_it_is_a_post_auth_answer() {
    // LEAVE-ALONE half: the hub routes only AFTER its auth middleware
    // accepted the bearer, so a 404 PROVES the fallback token worked.
    let hub = AuthStubHub::start_answering(
        FRESH_DISK_TOKEN,
        "404 Not Found",
        serde_json::json!({"error": {"code": "not_found"}}).to_string(),
    );
    let state = state_dir_with_token(Some(FRESH_DISK_TOKEN));

    let (_out, err, code) = run_vco(hub.port, state.path(), Some(STALE_ENV_TOKEN), false);

    assert_ne!(code, 0, "a 404 is still an error for this command");
    assert!(err.contains("404"), "the adopted answer must surface: {err}");
    assert!(err.contains(DEFINITIVE_LINE), "expected the definitive line: {err}");
}

#[test]
fn no_on_disk_token_keeps_the_401_path() {
    // LEAVE-ALONE: nothing better to try → today's error, one request.
    let hub = AuthStubHub::start(FRESH_DISK_TOKEN);
    let state = state_dir_with_token(None);

    let (_out, err, code) = run_vco(hub.port, state.path(), Some(STALE_ENV_TOKEN), false);

    assert_ne!(code, 0);
    assert!(err.contains("401"), "expected the 401 error text: {err}");
    assert_eq!(hub.bearers(), vec![STALE_ENV_TOKEN.to_string()]);
}
