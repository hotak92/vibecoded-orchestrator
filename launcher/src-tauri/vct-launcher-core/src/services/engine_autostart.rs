// SPDX-License-Identifier: AGPL-3.0-or-later
//! The ONE Rust spawner of the container-engine auto-start CLI.
//!
//! Owner ruling 2026-10-15: the container engine "should be made to
//! auto-start with launcher/hub/VS Code (if opening a VCO project)". The
//! LOGIC lives in exactly one place, Python —
//! `python -m vco_lib.container_engine_start ensure` (idempotent: an engine
//! that answers is a no-op; starts only the runtime this install uses;
//! soft-fail; honours the opt-out env). Rust only SPAWNS that CLI
//! detached, from two surfaces: the hub's startup
//! (`vct-hub/src/server.rs::start_hub_server`) and the launcher's setup
//! (`src/lib.rs`, beside `ensure_hub_running`). This module is that one
//! spawn, so the two cannot drift in argv or detach semantics.
//!
//! The spawn is fire-and-forget by contract: stdio is null, nothing is
//! waited on, and the child is detached from the caller's process group
//! (POSIX `setsid` via the pre-exec hook, Windows
//! `DETACHED_PROCESS | CREATE_NO_WINDOW`), so a kill aimed at the caller
//! (hub restart, launcher quit, a hook timeout) cannot cut a legitimate
//! engine boot short. The CLI does its own bounded liveness wait — never a
//! cap that kills work.

use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};

/// The opt-out knob. MUST MATCH `vco_lib.container_engine_start
/// .VCT_CONTAINER_ENGINE_AUTOSTART` (env name + falsy set — a data-only
/// mirror so an opted-out machine never pays the Python spawn; the CLI
/// re-checks it authoritatively). Documented in `docs/CONFIGURATION.md`.
pub const ENV_AUTOSTART: &str = "VCT_CONTAINER_ENGINE_AUTOSTART";

/// The falsy values that disable auto-start (lowercase). Kept in step with
/// the Python `_FALSY` set.
const FALSY: [&str; 4] = ["0", "false", "no", "off"];

/// True unless [`ENV_AUTOSTART`] is set to a falsy value.
pub fn autostart_enabled() -> bool {
    match std::env::var(ENV_AUTOSTART) {
        Ok(v) => !FALSY.contains(&v.trim().to_ascii_lowercase().as_str()),
        Err(_) => true,
    }
}

/// The argv (after the interpreter) of the engine auto-start CLI. One
/// constant so the hub and the launcher spawn the identical command.
pub fn engine_start_argv() -> [&'static str; 3] {
    ["-m", "vco_lib.container_engine_start", "ensure"]
}

/// Resolve the vco_lib-capable interpreter (the canonical RT-4 ladder) and
/// spawn the engine auto-start CLI detached. Returns the child's pid, or
/// `None` when auto-start is opted out, no interpreter resolves (a BROKEN
/// install — logged, never patched over) or the spawn failed (soft-fail
/// with one log line). Never blocks on the child.
pub fn spawn_engine_start_cli() -> Option<u32> {
    if !autostart_enabled() {
        // Not a warning: an explicit user choice, and the hub/launcher
        // start path is far too early for log noise about it.
        return None;
    }
    let python = resolve_python();
    let Some(python) = python else {
        tracing::warn!(
            "[engine-autostart] no Python interpreter could be resolved for \
             `vco_lib.container_engine_start` — broken VCO install? The \
             container engine was NOT auto-started."
        );
        return None;
    };
    match spawn_engine_start_cli_with(&python) {
        Ok(pid) => {
            tracing::info!(
                "[engine-autostart] spawned `vco_lib.container_engine_start ensure` \
                 detached (pid {pid})"
            );
            Some(pid)
        }
        Err(e) => {
            tracing::warn!("[engine-autostart] cannot spawn the engine auto-start CLI: {e}");
            None
        }
    }
}

/// The interpreter ladder, as its own seam so tests can pin the argv the
/// real spawn builds without depending on where a Python lives.
fn resolve_python() -> Option<PathBuf> {
    crate::python_resolve::resolve_python_for_vco_lib()
}

/// Build and spawn the detached CLI for a GIVEN interpreter — the testable
/// core of [`spawn_engine_start_cli`]. Returns the child's pid without
/// waiting, collecting stdin/stdout/stderr to null and detaching the child
/// from this process's session/group (see the module doc).
pub fn spawn_engine_start_cli_with(python: &Path) -> io::Result<u32> {
    let mut cmd = Command::new(python);
    cmd.args(engine_start_argv())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Safety: setsid() is async-signal-safe per POSIX; called in the
        // forked child pre-exec where only async-signal-safe calls are
        // allowed. Failure (already a session leader) is harmless.
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() == -1 {
                    let _ = std::io::Error::last_os_error();
                }
                Ok(())
            });
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // DETACHED_PROCESS | CREATE_NO_WINDOW — no console, no conhost.exe
        // flash when the caller is a GUI-subsystem process.
        cmd.creation_flags(0x0000_0008 | 0x0800_0000);
    }

    cmd.spawn().map(|child| child.id())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn fake_python(dir: &std::path::Path, name: &str, body: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&p, perms).unwrap();
        }
        p
    }

    #[test]
    fn argv_is_the_one_ensure_verb() {
        assert_eq!(engine_start_argv(), ["-m", "vco_lib.container_engine_start", "ensure"]);
    }

    #[test]
    fn opt_out_env_disables() {
        // The process env is shared by every test in the binary — hold THE
        // lock for the whole mutation window (test_env::GLOBAL_ENV_MUTEX,
        // the one the repo's env-hygiene gate requires).
        let _lock = crate::test_env::env_lock();
        for falsy in ["0", "false", "no", "off", "OFF"] {
            std::env::set_var(ENV_AUTOSTART, falsy);
            assert!(!autostart_enabled(), "{falsy} must disable");
        }
        std::env::set_var(ENV_AUTOSTART, "1");
        assert!(autostart_enabled());
        std::env::remove_var(ENV_AUTOSTART);
        assert!(autostart_enabled());
    }

    #[test]
    fn spawn_uses_the_given_interpreter_and_does_not_block() {
        // A fake interpreter that records its argv and then sleeps far
        // longer than this test would tolerate if the spawn waited: the
        // spawn must return before the child ends (fire-and-forget).
        let dir = std::env::temp_dir().join(format!("vco-engine-autostart-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join("argv.log");
        let body = format!("#!/bin/sh\nprintf '%s\\n' \"$*\" > {:?}\nsleep 30\n", marker);
        let python = fake_python(&dir, "fake-python", &body);

        let start = std::time::Instant::now();
        let pid = spawn_engine_start_cli_with(&python).expect("spawn must succeed");
        let elapsed = start.elapsed();
        assert!(elapsed < std::time::Duration::from_secs(10), "spawn must not wait for the child");
        assert!(pid > 0);

        // The child writes its argv quickly (before its sleep).
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let argv = loop {
            if let Ok(text) = std::fs::read_to_string(&marker) {
                break text;
            }
            if std::time::Instant::now() > deadline {
                panic!("fake interpreter never wrote its argv");
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        };
        assert_eq!(argv.trim(), "-m vco_lib.container_engine_start ensure");

        // Do not leave the sleeping child behind for the whole 30 s: kill
        // it by pid. Best-effort — the test asserts the spawn, not the kill.
        #[cfg(unix)]
        unsafe {
            libc::kill(pid as i32, libc::SIGKILL);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
