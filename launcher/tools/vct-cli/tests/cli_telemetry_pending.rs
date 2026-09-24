//! v0.2.97 — `vct-cli telemetry pending` prints the events the telemetry
//! uploader parked in `~/.vibecoded/telemetry_pending.jsonl`.
//!
//! The uploader's docstring and docs/TELEMETRY.md both pointed users at the
//! CLI to inspect those events, and the CLI had no such command. It reads the
//! file directly, so it must answer with the launcher down: every run here
//! points the hub at the discard port 9 and a scratch state dir.
#![cfg(unix)]

use std::path::Path;
use std::process::Command;

fn run_pending(home: &Path) -> (serde_json::Value, String, i32) {
    let out = Command::new(env!("CARGO_BIN_EXE_vct-cli"))
        .args(["telemetry", "pending"])
        .env("HOME", home)
        .env("VCT_HUB_PORT", "9")
        .env("VCT_STATE_DIR", home.join("state"))
        .env_remove("VCT_HUB_TOKEN")
        .output()
        .expect("spawn vct-cli");
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let json = serde_json::from_str(&stdout).unwrap_or(serde_json::Value::Null);
    (
        json,
        String::from_utf8_lossy(&out.stderr).to_string(),
        out.status.code().unwrap_or(-1),
    )
}

fn scratch(tag: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("vct-cli-{}-{}", tag, std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn prints_the_parked_events_with_the_launcher_down() {
    let home = scratch("pending-home");
    let parked = home.join(".vibecoded");
    std::fs::create_dir_all(&parked).unwrap();
    std::fs::write(
        parked.join("telemetry_pending.jsonl"),
        "{\"event\":\"install_start\"}\n{\"event\":\"install_finish\"}\n",
    )
    .unwrap();

    let (json, err, code) = run_pending(&home);
    assert_eq!(code, 0, "stderr: {err}");
    assert_eq!(json["count"], 2);
    assert_eq!(json["events"][0]["event"], "install_start");
    assert!(json["path"]
        .as_str()
        .unwrap()
        .ends_with(".vibecoded/telemetry_pending.jsonl"));
    std::fs::remove_dir_all(&home).unwrap();
}

#[test]
fn nothing_parked_is_an_empty_answer_not_an_error() {
    let home = scratch("no-pending-home");
    let (json, err, code) = run_pending(&home);
    assert_eq!(code, 0, "stderr: {err}");
    assert_eq!(json["exists"], false);
    assert_eq!(json["count"], 0);
    std::fs::remove_dir_all(&home).unwrap();
}
