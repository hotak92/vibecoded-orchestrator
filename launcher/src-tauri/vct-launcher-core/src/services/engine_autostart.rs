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
//! The spawn is fire-and-forget by contract: stdin/stdout are null, stderr
//! is captured best-effort in the spawners' per-spawn stderr file (see
//! [`ENGINE_START_STDERR_LOG`]; review V02103-VERIFY-R31 M-1), nothing is
//! waited on, and the child is detached from the caller's process group
//! (POSIX `setsid` via the pre-exec hook; Windows `CREATE_NO_WINDOW`
//! only — never `DETACHED_PROCESS`, which flashes a console), so a kill aimed at the caller
//! (hub restart, launcher quit, a hook timeout) cannot cut a legitimate
//! engine boot short. The CLI does its own bounded liveness wait — never a
//! cap that kills work.

use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};

/// The opt-out knob 1 (env override). MUST MATCH `vco_lib
/// .container_engine_start.VCT_CONTAINER_ENGINE_AUTOSTART` (env name + falsy
/// set — a data-only mirror so an opted-out machine never pays the Python
/// spawn; the CLI re-checks it authoritatively). Documented in
/// `docs/CONFIGURATION.md`.
pub const ENV_AUTOSTART: &str = "VCT_CONTAINER_ENGINE_AUTOSTART";

/// The opt-out knob 2 (review R19 F4): the Preferences toggle row in
/// launcher.db `app_state`. MUST MATCH `vco_lib.container_engine_start
/// .APP_STATE_ENGINE_AUTOSTART` and the launcher's writer
/// (`commands::engine_autostart_pref`). Pinned by
/// `tests/test_container_engine_start.py`.
pub const APP_STATE_ENGINE_AUTOSTART: &str = "engine.autostart";

/// Shipped default: ON. MUST MATCH the Python `DEFAULT_ENGINE_AUTOSTART`.
pub const DEFAULT_ENGINE_AUTOSTART: bool = true;

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

/// The Preferences toggle (review R19 F4): an absent row, an unreadable
/// launcher.db (fresh install, never booted) or any DB error all read as
/// the shipped default rather than as "off" — the same soft-fail rule
/// `vco_lib.container_engine_start::pref_enabled` applies. `None`-row
/// semantics MUST MATCH the Python reader.
pub fn pref_enabled(db: &crate::db::Db) -> bool {
    db.app_state_get_bool(APP_STATE_ENGINE_AUTOSTART)
        .unwrap_or(None)
        .unwrap_or(DEFAULT_ENGINE_AUTOSTART)
}

/// The argv (after the interpreter) of the engine auto-start CLI. One
/// constant so the hub and the launcher spawn the identical command.
pub fn engine_start_argv() -> [&'static str; 3] {
    ["-m", "vco_lib.container_engine_start", "ensure"]
}

/// The basename of the per-spawn stderr file the SPAWNERS write (review
/// V02103-VERIFY-R31 M-1): the child's stderr is TRUNCATED into it on every
/// spawn, so a venv too broken to import vco_lib still leaves its traceback
/// somewhere (this Rust spawner is the surface that sees a fully-broken
/// venv — the hooks have already run vco_lib twice before they spawn).
/// MUST MATCH `vco_lib.container_engine_start.ENGINE_START_STDERR_LOG` and
/// the two hook siblings (`templates/hooks/ensure-containers.{sh,ps1}`).
/// The Python CLI keeps SOLE ownership of its outcome log
/// `<vct_root_dir>/logs/engine-start.log` (it appends and rotates it); the
/// spawners never touch that file — no shared handle, no rotation race,
/// and the stderr file is bounded by construction (the latest spawn's stderr).
/// Pinned by `tests/test_container_engine_start.py`.
pub const ENGINE_START_STDERR_LOG: &str = "engine-start.stderr.log";

/// Where this spawner truncates the child's stderr:
/// [`ENGINE_START_STDERR_LOG`] under the logs dir of the one root resolver
/// `crate::paths::vct_root_dir` (the mirror of `vco_lib.paths::vct_root_dir`).
fn engine_start_stderr_log_path() -> PathBuf {
    crate::paths::vct_root_dir().join("logs").join(ENGINE_START_STDERR_LOG)
}

/// Best-effort truncate handle to [`engine_start_stderr_log_path`] for the
/// child's stderr (write + truncate: OVERWRITTEN on every spawn, so the
/// file holds the latest spawn's stderr — bounded by construction). Creates
/// the logs dir if missing. An unopenable file degrades to [`Stdio::null`]
/// — a log problem must never fail the spawn (the engine start matters
/// more than its traceback).
fn engine_start_stderr_stdio() -> Stdio {
    let path = engine_start_stderr_log_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&path)
    {
        Ok(file) => file.into(),
        Err(_) => Stdio::null(),
    }
}

/// Resolve the vco_lib-capable interpreter (the canonical RT-4 ladder) and
/// spawn the engine auto-start CLI detached. Returns the child's pid, or
/// `None` when auto-start is opted out (env override, Preferences toggle,
/// or the launcher.db read failing open to the default with no DB
/// discoverable), no interpreter resolves (a BROKEN install — logged,
/// never patched over) or the spawn failed (soft-fail with one log line).
/// Never blocks on the child.
///
/// The update-in-progress gate (R19 F7) is enforced INSIDE the CLI
/// (`vco_lib.update_gate.is_update_in_progress`), so every Rust surface
/// that spawns it — hub, launcher — honours the same lockfile the hooks
/// honour without a second Rust copy of the freshness rule.
pub fn spawn_engine_start_cli() -> Option<u32> {
    if !autostart_enabled() {
        // Not a warning: an explicit user choice, and the hub/launcher
        // start path is far too early for log noise about it.
        return None;
    }
    if let Ok(db) = crate::db::Db::open() {
        if !pref_enabled(&db) {
            return None;
        }
    }
    // An unopenable launcher.db is NOT an opt-out (the Python CLI re-reads
    // the row and applies the same default) — fall through and spawn.
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

/// Win32 creation flags for the CLI child (review R19 F3).
///
/// `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` — deliberately NOT
/// `DETACHED_PROCESS`: Win32 ignores `CREATE_NO_WINDOW` when
/// `DETACHED_PROCESS` is set, and a child with NO console gives every
/// console grandchild it spawns (`cmd`, `docker info` polling, `podman`)
/// a NEW VISIBLE console — a window flash every 2 s of a Docker Desktop
/// boot. `CREATE_NO_WINDOW` alone gives the CLI a HIDDEN console its
/// children inherit; `CREATE_NEW_PROCESS_GROUP` keeps it immune to the
/// caller's Ctrl-C. The repo's own convention (`hub_launcher.rs:173`,
/// `projects_v2.rs`) is `CREATE_NO_WINDOW` alone for the same reason.
pub const WINDOWS_CREATION_FLAGS: u32 = 0x0800_0000 | 0x0000_0200;

/// Build and spawn the detached CLI for a GIVEN interpreter — the testable
/// core of [`spawn_engine_start_cli`]. Returns the child's pid without
/// waiting: stdin/stdout are null, stderr is captured best-effort in the
/// per-spawn [`ENGINE_START_STDERR_LOG`] (see [`engine_start_stderr_stdio`]),
/// and the child is detached from this process's session/group (see the
/// module doc).
///
/// The exited child is REAPED on a detached thread (R19 F6): `setsid`
/// detaches the session, not the parentage, so an unwaited child would
/// linger as a zombie for the hub's lifetime.
pub fn spawn_engine_start_cli_with(python: &Path) -> io::Result<u32> {
    // The `#[cfg(windows)]` creation_flags block sits DIRECTLY under the
    // stdio chain (before the `#[cfg(unix)]` pre_exec block) so the
    // command-silent gate sees the `.creation_flags(` marker: its lookahead
    // stops at the first new `let ` binding that does not mention `cmd`
    // (the `let _ = …last_os_error()` line inside the unix block below).
    // Pure layout — the two blocks are mutually exclusive per OS, so the
    // order between them cannot change behaviour.
    let mut cmd = Command::new(python);
    cmd.args(engine_start_argv())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(engine_start_stderr_stdio());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(WINDOWS_CREATION_FLAGS);
    }

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

    let mut child = cmd.spawn()?;
    let pid = child.id();
    // Reap the exited child so it never lingers as a zombie under the hub /
    // launcher (R19 F6). The CLI is short-lived (bounded --wait); the thread
    // parks until then and exits with it.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(pid)
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
    fn windows_flags_hide_the_console_and_never_detach_it() {
        // R19 F3: CREATE_NO_WINDOW must be effective, which requires that
        // DETACHED_PROCESS (0x8) is NOT set (Win32 ignores CREATE_NO_WINDOW
        // when it is), and CREATE_NEW_PROCESS_GROUP (0x200) keeps the child
        // immune to the caller's Ctrl-C.
        assert_ne!(WINDOWS_CREATION_FLAGS & 0x0800_0000, 0, "CREATE_NO_WINDOW must be set");
        assert_ne!(WINDOWS_CREATION_FLAGS & 0x0000_0200, 0, "CREATE_NEW_PROCESS_GROUP must be set");
        assert_eq!(WINDOWS_CREATION_FLAGS & 0x0000_0008, 0, "DETACHED_PROCESS must NOT be set");
    }

    #[test]
    fn pref_toggle_default_on_absent_row_off_when_disabled() {
        // R19 F4: absent row → shipped default (ON); an explicit "false"
        // row opts out; an unreadable DB (Err) also reads as the default.
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        assert!(pref_enabled(&db), "no row means the shipped default, which is on");

        db.app_state_set_bool(APP_STATE_ENGINE_AUTOSTART, false).unwrap();
        assert!(!pref_enabled(&db), "an explicit off must be honoured");

        db.app_state_set_bool(APP_STATE_ENGINE_AUTOSTART, true).unwrap();
        assert!(pref_enabled(&db), "an explicit on stays on");
    }

    #[test]
    fn the_app_state_key_matches_the_python_home() {
        // Cross-language pin; the Python side asserts the same literal
        // against this file (tests/test_container_engine_start.py).
        assert_eq!(APP_STATE_ENGINE_AUTOSTART, "engine.autostart");
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
    fn the_env_opt_out_stands_down_before_any_spawn() {
        // The full entry point: opted out via the env override → None, no
        // Db open, no interpreter resolution, no child.
        let _lock = crate::test_env::env_lock();
        std::env::set_var(ENV_AUTOSTART, "0");
        let spawned = spawn_engine_start_cli();
        std::env::remove_var(ENV_AUTOSTART);
        assert!(spawned.is_none(), "the env opt-out must produce no spawn");
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

    #[test]
    fn the_child_stderr_lands_in_the_per_spawn_stderr_file_truncated() {
        // V02103-VERIFY-R31 M-1: a venv too broken to import vco_lib dies
        // with its traceback on stderr — the spawn must capture it in the
        // spawners' OWN file (engine-start.stderr.log), never null it away
        // and never into the CLI's rotating engine-start.log. The file is
        // TRUNCATED on every spawn (bounded by construction): a sentinel
        // from a previous run must be GONE once the new child writes.
        let dir = std::env::temp_dir()
            .join(format!("vco-engine-autostart-stderr-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let body = "#!/bin/sh\necho 'fake broken venv: No module named vco_lib' >&2\nexit 1\n";
        let python = fake_python(&dir, "fake-python", body);

        // Point the one root resolver at a scratch dir through the SANCTIONED
        // helper (sets VCT_STATE_DIR under GLOBAL_ENV_MUTEX and restores the
        // PRIOR value on Drop — a bare set_var/remove_var pair is banned by
        // tests/state_dir_env_lint.rs and would clobber an outer redirect).
        // The spawn AND the poll stay inside the guard: the file lives in the
        // guard's tempdir, which its Drop deletes.
        let sd = crate::test_env::state_dir_guard();
        let logs = sd.path().join("logs");
        std::fs::create_dir_all(&logs).unwrap();
        let stderr_file = logs.join(ENGINE_START_STDERR_LOG);
        std::fs::write(&stderr_file, "sentinel from a previous spawn\n").unwrap();
        let pid = spawn_engine_start_cli_with(&python).expect("spawn must succeed");
        let needle = "fake broken venv: No module named vco_lib";
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        loop {
            if let Ok(text) = std::fs::read_to_string(&stderr_file) {
                if text.contains(needle) {
                    assert!(!text.contains("sentinel from a previous spawn"),
                        "the per-spawn stderr file must be TRUNCATED on every spawn");
                    break;
                }
            }
            assert!(
                std::time::Instant::now() < deadline,
                "the child's stderr never reached {stderr_file:?} — a broken-import traceback would be lost"
            );
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        // The CLI's own rotating log is NOT written by this spawner: only
        // the per-spawn stderr file exists.
        assert!(!logs.join("engine-start.log").exists(),
            "the spawner must never write the CLI's own engine-start.log");
        drop(sd);
        let _ = pid;
        let _ = std::fs::remove_dir_all(&dir);
    }

    // Linux only: the zombie is observed through /proc, which macOS/BSD do
    // not have — there the test would pass without checking anything.
    #[cfg(target_os = "linux")]
    #[test]
    fn the_exited_child_is_reaped_not_left_a_zombie() {
        // R19 F6 / R21 L2: `setsid` detaches the child's SESSION, not its
        // parentage — without the reaper thread the exited child lingers
        // as a zombie (/proc/<pid> state Z) for the caller's whole
        // lifetime. REAPED means the /proc entry is GONE (a zombie still
        // has one; only wait() removes it).
        let dir = std::env::temp_dir()
            .join(format!("vco-engine-autostart-reap-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // Exits on its own immediately — there is nothing to wait for but
        // the reap itself.
        let python = fake_python(&dir, "fake-python", "#!/bin/sh\nexit 0\n");

        let pid = spawn_engine_start_cli_with(&python).expect("spawn must succeed");

        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        loop {
            match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
                Err(_) => break, // the entry is gone — the child was reaped
                Ok(stat) => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "child {pid} is still in /proc (state {:?}) after the \
                         reaper window — it was left as a zombie",
                        proc_state(&stat),
                    );
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// /proc/<pid>/stat field 3 (state), parsed after the last `)` because
    /// the comm field (field 2) is parenthesised and may contain spaces.
    #[cfg(target_os = "linux")]
    fn proc_state(stat: &str) -> &str {
        stat.rsplit_once(')')
            .and_then(|(_, tail)| tail.split_whitespace().next())
            .unwrap_or("?")
    }
}
