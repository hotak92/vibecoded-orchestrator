// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Shared pause-marker mechanism for the hub-side infra-container watchdog
//! (v0.2.62).
//!
//! ## Why this module exists (the producer/consumer split)
//!
//! The hub watchdog (`vct-hub::infra_watchdog`) skips a service when a
//! marker file exists at `<vct_root>/state/watchdog-paused/<service>` —
//! that marker is the explicit "this service was stopped on purpose, stop
//! restarting it" signal. The CONSUMER (the watchdog, hub process) and the
//! PRODUCER (the launcher's `service_stop` / `services_stop_all` Tauri
//! commands, launcher process) are SEPARATE processes. If each derived the
//! marker path from its own copy of the string, the two would silently
//! drift (different `state/` sub-path, a typo, a future rename) and the
//! producer would write a marker the consumer never reads — re-introducing
//! the very bug this primitive exists to fix (the watchdog fighting a
//! deliberate stop).
//!
//! So the path logic lives HERE, in the shared `vct-launcher-core`, and
//! both the hub and the launcher call the same functions. This mirrors the
//! same shared-path discipline already applied to `services::adoption`
//! (read by both processes) and `paths::finetune_sentinel_path`
//! (writer = launcher, reader = hub).
//!
//! ## Lifecycle contract
//!
//! - A deliberate STOP of a VCO-managed service (`Unresolved` adoption
//!   mode) CREATES its marker → the watchdog leaves it down.
//! - A deliberate START / RESTART of the same service REMOVES its marker →
//!   the watchdog resumes supervision.
//! - A RAW external stop (the user runs `podman stop` themselves, a crash,
//!   an OOM kill) leaves NO marker → the watchdog restarts it (the desired
//!   self-healing behavior).
//!
//! ## Safety
//!
//! `service` is always one of the compiled-in canonical service names
//! ("weaviate" / "ollama" / "code_embed"); callers MUST validate before
//! reaching these functions so the marker name can never be an attacker-
//! controlled path component. All operations are soft-fail: a stat/create/
//! remove error is reported to the caller but never panics.

use std::io;
use std::path::PathBuf;

/// Directory holding per-service pause markers:
/// `<vct_root>/state/watchdog-paused/`.
pub fn pause_dir() -> PathBuf {
    crate::paths::vct_root_dir()
        .join("state")
        .join("watchdog-paused")
}

/// Path to the pause marker for `service`. Its mere EXISTENCE means
/// "paused" — file content is ignored. `service` must be a validated
/// canonical service name (a single safe path component).
pub fn pause_marker_path(service: &str) -> PathBuf {
    pause_dir().join(service)
}

/// Is `service` currently paused (marker file present)?
pub fn is_service_paused(service: &str) -> bool {
    pause_marker_path(service).exists()
}

/// CREATE the pause marker for `service` (deliberate-stop signal).
///
/// Idempotent: creating an existing marker is a no-op success. The marker
/// is an empty file; only its existence matters. Returns `Err` only when
/// the directory cannot be created or the file cannot be written — the
/// caller (a launcher stop command) treats this as soft (logs + continues;
/// failing to drop the marker only means the watchdog might restart a
/// service the user stopped, which is recoverable, not data loss).
pub fn create_pause_marker(service: &str) -> io::Result<()> {
    let dir = pause_dir();
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(service);
    // `create_new` would error if it already exists; we want idempotent
    // "ensure present", so a plain create/truncate of an empty file is
    // fine — the content is never read.
    match std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&path)
    {
        Ok(_) => Ok(()),
        Err(e) => Err(e),
    }
}

/// REMOVE the pause marker for `service` (deliberate-start signal).
///
/// Idempotent: removing an absent marker is a no-op success (NotFound is
/// swallowed). Returns `Err` only on a real filesystem error (permission,
/// I/O) — again soft for the caller.
pub fn remove_pause_marker(service: &str) -> io::Result<()> {
    let path = pause_marker_path(service);
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    /// Redirect `vct_root_dir()` at a temp dir for the duration of a test
    /// by setting `VCT_STATE_DIR`. Tests are `#[serial]` because they
    /// mutate a process-global env var.
    struct TempRoot {
        _dir: tempfile::TempDir,
        prev: Option<std::ffi::OsString>,
    }
    impl TempRoot {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let prev = std::env::var_os("VCT_STATE_DIR");
            std::env::set_var("VCT_STATE_DIR", dir.path());
            TempRoot { _dir: dir, prev }
        }
    }
    impl Drop for TempRoot {
        fn drop(&mut self) {
            match &self.prev {
                Some(v) => std::env::set_var("VCT_STATE_DIR", v),
                None => std::env::remove_var("VCT_STATE_DIR"),
            }
        }
    }

    #[test]
    #[serial]
    fn marker_path_is_under_watchdog_paused_dir() {
        let _root = TempRoot::new();
        let p = pause_marker_path("weaviate");
        assert!(p.ends_with("state/watchdog-paused/weaviate"), "got {:?}", p);
    }

    #[test]
    #[serial]
    fn create_then_present_then_remove_then_absent() {
        let _root = TempRoot::new();
        assert!(!is_service_paused("ollama"), "must start absent");
        create_pause_marker("ollama").expect("create marker");
        assert!(is_service_paused("ollama"), "must be present after create");
        // Idempotent create.
        create_pause_marker("ollama").expect("second create is no-op");
        assert!(is_service_paused("ollama"));
        remove_pause_marker("ollama").expect("remove marker");
        assert!(!is_service_paused("ollama"), "must be absent after remove");
        // Idempotent remove (absent → Ok).
        remove_pause_marker("ollama").expect("second remove is no-op");
    }

    #[test]
    #[serial]
    fn markers_are_per_service_independent() {
        let _root = TempRoot::new();
        create_pause_marker("weaviate").unwrap();
        assert!(is_service_paused("weaviate"));
        assert!(!is_service_paused("code_embed"));
        assert!(!is_service_paused("ollama"));
    }
}
