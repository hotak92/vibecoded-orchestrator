//! v0.2.97: `vct-hub --ensure-db` — the headless schema step install.py runs
//! before Python writes the first `service_endpoints` row.
//!
//! Black-box: the real binary, a scratch state dir. It must leave a migrated
//! launcher.db (the `service_endpoints` table included) and must NOT behave
//! like a hub start — no `hub.pid`, no `hub.port`, no listener.

use std::process::Command;
use std::time::{Duration, Instant};

#[test]
fn ensure_db_creates_a_migrated_db_and_starts_no_hub() {
    let state = tempfile::tempdir().unwrap();
    let home = tempfile::tempdir().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_vct-hub"))
        .arg("--ensure-db")
        .env("VCT_STATE_DIR", state.path())
        .env("HOME", home.path())
        .env("USERPROFILE", home.path())
        // An unroutable hub port: nothing here may reach a real hub.
        .env("VCT_HUB_PORT", "9")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn vct-hub --ensure-db");

    // Drain both pipes while the child runs: the migration runner logs every
    // migration's description, which can exceed a pipe buffer and would
    // otherwise block the child on a write nobody reads.
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let out_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stdout, &mut buf);
        buf
    });
    let err_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stderr, &mut buf);
        buf
    });

    // A headless step exits by itself; a server would not. Bound the wait so
    // a regression that starts the hub fails instead of hanging the suite.
    let deadline = Instant::now() + Duration::from_secs(60);
    let status = loop {
        if let Some(s) = child.try_wait().unwrap() {
            break s;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            let err = err_reader.join().unwrap_or_default();
            panic!(
                "--ensure-db did not exit within 60 s (did it start a server?) stderr={}",
                String::from_utf8_lossy(&err)
            );
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    let stdout_bytes = out_reader.join().unwrap();
    let stderr_bytes = err_reader.join().unwrap();
    assert!(
        status.success(),
        "--ensure-db failed: stderr={}",
        String::from_utf8_lossy(&stderr_bytes)
    );

    let db_path = state.path().join("launcher.db");
    assert!(db_path.is_file(), "launcher.db must exist after --ensure-db");
    assert!(!state.path().join("hub.pid").exists(), "--ensure-db must not claim the hub lockfile");
    assert!(!state.path().join("hub.port").exists(), "--ensure-db must not publish a hub port");

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let tables: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'service_endpoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(tables, 1, "the service_endpoints table must exist");
    let applied: u32 = conn
        .query_row("SELECT MAX(version) FROM _schema_migrations", [], |r| r.get(0))
        .unwrap();

    let report: serde_json::Value = serde_json::from_slice(&stdout_bytes)
        .unwrap_or_else(|e| panic!("stdout is not the JSON report ({e}): {}", String::from_utf8_lossy(&stdout_bytes)));
    assert_eq!(report["schema_version"].as_u64(), Some(applied as u64));
    assert!(applied >= 47, "migration 047 applied, got {applied}");
    assert_eq!(
        std::fs::canonicalize(report["db_path"].as_str().unwrap()).unwrap(),
        std::fs::canonicalize(&db_path).unwrap(),
        "the report names the DB it migrated"
    );
}
