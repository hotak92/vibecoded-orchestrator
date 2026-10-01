//! Cross-OS process helpers: liveness, the `.silent()` spawn flag, the ONE
//! bounded runner for blocking children ([`output_bounded`]) and the ONE
//! rendering of a failed child's evidence ([`failure_evidence`]).
//!
//! Moved to core in v0.2.21 Step 5 because both the launcher's
//! self-update pre-pull-rename sweep AND vct-hub's lockfile machinery
//! need to ask "is this PID still alive?". Identical semantics in
//! either context.

/// Extension trait that adds `.silent()` to both `std::process::Command`
/// and `tokio::process::Command`. On Windows, calling `.silent()` sets
/// the `CREATE_NO_WINDOW` (0x08000000) flag on the child so that
/// spawning a subprocess from a GUI-subsystem parent (the launcher,
/// built with `windows_subsystem = "windows"`) does NOT allocate a
/// new conhost.exe console window for the child. On Linux/macOS the
/// call is a no-op.
///
/// History: prior to v0.2.34's audit on 2026-05-26, 208 of the 221
/// `Command::new` call sites in the launcher omitted the flag. Each
/// missing site flashes a conhost console on Windows for the duration
/// of the subprocess (typically 50-500ms per `where`, `docker`, `git`,
/// `python`, etc.). With launcher boot invoking 11+ such subprocesses
/// concurrently (detect_system, hub_launcher, runtime probes,
/// install-health probes, embedding-catalog probes, ...) the user
/// sees a "fork bomb" of console windows cascading on screen.
/// Centralising the fix on a chainable method makes the call-site
/// edit a one-line append (`.silent()`) and avoids the indent-style
/// inconsistencies of inlining 208 `#[cfg(windows)]` blocks.
///
/// Usage:
/// ```ignore
/// use vct_launcher_core::process::CommandExt;
/// std::process::Command::new("git")
///     .silent()
///     .arg("status")
///     .output()?;
/// ```
/// Implementation detail: takes `self` by value and returns it so that
/// `Command::new(x).silent()` works as a fluent expression (the produced
/// temporary chains forward instead of needing a `let mut binding`).
/// Existing patterns that use a separate `let mut cmd = Command::new(x);
/// cmd.silent();` also work because `Command` implements no Deref to
/// the borrow form -- the call signature is unambiguous on a fresh
/// owned value.
pub trait CommandExt: Sized {
    fn silent(self) -> Self;
}

impl CommandExt for std::process::Command {
    // v0.2.42: `mut self` is REQUIRED on Windows because `creation_flags`
    // takes `&mut self`. The earlier cargo-fix pass stripped `mut` because
    // it ran on Linux where the cfg(windows) branch is inactive → unused_mut
    // warning. The Windows build then failed with E0596. Restored `mut` +
    // `#[allow(unused_mut)]` to silence the Linux warning cleanly.
    #[allow(unused_mut)]
    fn silent(mut self) -> Self {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt as _;
            self.creation_flags(0x0800_0000);
        }
        self
    }
}

impl CommandExt for tokio::process::Command {
    // Same v0.2.42 mut-on-Windows requirement as above.
    #[allow(unused_mut)]
    fn silent(mut self) -> Self {
        #[cfg(windows)]
        {
            // tokio::process::Command exposes `creation_flags` as an INHERENT
            // method on Windows — no std CommandExt trait import needed (the
            // import was flagged unused by the windows-gnu cross-build,
            // v0.2.80 warning sweep).
            self.creation_flags(0x0800_0000);
        }
        self
    }
}

/// Check whether a given PID is still alive.
///
/// Cross-OS: `kill(pid, 0)` on POSIX (signal 0 means "validate the
/// target without sending a signal"; ESRCH = dead, success = alive),
/// `OpenProcess` on Windows (returns NULL when the PID doesn't
/// exist). Returns `false` on any error (assume dead — the worst case
/// is we keep a stale file for one extra restart).
///
/// Defense in depth: rejects sentinel pids (0, > i32::MAX) before
/// touching libc. POSIX `kill(0, sig)` means "every process in the
/// caller's process group"; `kill(-1, sig)` means "every process the
/// caller has permission to signal"; and any u32 > i32::MAX casts to
/// a negative pid_t. None of those are the per-process liveness check
/// callers actually want — so refuse early.
pub fn pid_is_alive(pid: u32) -> bool {
    if pid == 0 || pid > i32::MAX as u32 {
        return false;
    }

    #[cfg(unix)]
    {
        // libc::kill(pid, 0) returns 0 if the process exists. -1 with
        // errno == ESRCH means dead. errno == EPERM means alive but
        // we don't have permission — still counts as "alive" (don't
        // delete its lockfile). Any other errno: be conservative, say
        // alive.
        //
        // We use `std::io::Error::last_os_error()` to read errno
        // cross-OS rather than `libc::__errno_location()` (glibc-only)
        // or `__error()` (macOS) directly.
        //
        // Safety: kill(pid, 0) is async-signal-safe per POSIX. We
        // call it before reading errno so the errno value belongs
        // to this call. The pid guard above ensures the cast to
        // pid_t is always positive on 32-bit-pid_t systems.
        unsafe {
            if libc::kill(pid as libc::pid_t, 0) == 0 {
                return true;
            }
        }
        let raw = std::io::Error::last_os_error().raw_os_error();
        raw != Some(libc::ESRCH)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        // SAFETY: OpenProcess is a thin FFI call. On failure we get
        // NULL; on success we close the returned handle immediately.
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                false
            } else {
                CloseHandle(handle);
                true
            }
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = pid;
        true
    }
}

/// Signal the transitive DESCENDANTS of `parent_pid` — the processes a
/// `Child` kill (tokio's `kill_on_drop`, `start_kill`, or a plain
/// `Child::kill`) cannot reach, because it reaches exactly one process.
///
/// v0.2.96 (L-7). Added for the summary-recheck deadline, which could
/// otherwise leave a generator running after the launcher had given up on
/// its parent. Lives HERE rather than at that call site because every
/// deadline-capped spawn seam in this workspace has the same boundary
/// (`commands::kg_sync`'s stall watchdog next in line): one home, so the
/// next caller inherits the guards below instead of re-deriving them.
///
/// ONE implementation for Linux, macOS and Windows — `sysinfo`'s process
/// table plus its portable `kill_with(Signal::Term)` / `kill()` pair, the
/// same mechanism `commands::update_gate`'s sweeps already use. A unix-only
/// `killpg` on a `setsid` process group was deliberately REJECTED: it would
/// need a Job Object twin on Windows, and a guard that silently does
/// nothing on one of the three supported platforms is worse than no guard,
/// because it reads as closed.
///
/// # Call it while the parent is STILL ALIVE
///
/// That is what makes the walk possible at all. On POSIX an orphan is
/// reparented to init (or the nearest subreaper) the instant its parent
/// exits, and from then on nothing can prove it was ours. So: snapshot and
/// signal FIRST, kill the parent SECOND. Calling this after the parent is
/// gone is not an error — it simply finds nothing.
///
/// # Conservative, because a PID is not an identity
///
/// * `min_start_time_epoch_secs` — a process whose start time is EARLIER
///   than this is never signalled. That is the PID-reuse guard: pass the
///   epoch second at which the parent was spawned, and a recycled PID
///   belonging to some unrelated older process is left alone. It errs
///   toward sparing a stranger, never toward killing one.
/// * `parent_pid` itself is never signalled — the caller owns that.
/// * Sentinel pids (0, `> i32::MAX`) are refused, same as `pid_is_alive`.
/// * Best-effort throughout: nothing here returns an error or panics. A
///   deadline that already fired must not also fail on cleanup.
///
/// # Verification status — LINUX ONLY, stated so nobody reads more into it
///
/// The "one implementation for three platforms" claim above is about the
/// SOURCE: there is no `#[cfg]` branch in this function, and `sysinfo` is
/// what carries it across OSes. It is **not** a claim that the behaviour has
/// been observed on three platforms. As of v0.2.96:
///
/// * **Linux** — runtime-verified. `kill_descendants_reaps_a_grandchild…`
///   and `…spares_processes_that_predate_the_window` below spawn a real
///   `sh` + `sleep` tree and assert both halves of the decision. They are
///   executed by `.github/workflows/ci.yml`'s `rust` job.
/// * **macOS** — NOT verified. Those two tests are `#[cfg(unix)]`, so they
///   would compile and run there — but no CI job runs `cargo test` on
///   macOS. Grep of `.github/workflows/*.yml` (2026-09-22): exactly two
///   jobs run `cargo test`, `ci.yml::rust` and
///   `manifest-validate.yml::validate-manifests`, and BOTH are
///   `ubuntu-latest`. `release.yml`'s three-OS matrix runs `cargo build
///   --release`, never `cargo test`; the tri-OS installer/access-matrix
///   workflows build `vct-hub` and exercise the installer, not this crate's
///   tests.
/// * **Windows** — NOT verified, and not even compiled into a test: the
///   fixtures are `#[cfg(unix)]` (they need `/bin/sh`), so only
///   `kill_descendants_refuses_sentinel_pids` would run there, and nothing
///   runs it there either. The parent-link walk and `kill_with` are
///   `sysinfo`'s portable surface; that is a reasoned expectation, not
///   evidence.
///
/// Closing this honestly means either a macOS/Windows `cargo test` job, or a
/// Windows fixture that does not need `/bin/sh` (e.g. `cmd /c timeout`).
/// Until one exists, do not describe this guard as cross-platform-tested.
///
/// Returns how many processes were signalled.
pub fn kill_descendants(parent_pid: u32, min_start_time_epoch_secs: u64) -> usize {
    use sysinfo::System;

    /// Tolerance on the start-time comparison, in seconds. See the guard
    /// below for why an exact comparison is wrong.
    const START_TIME_SLACK_SECS: u64 = 2;

    if parent_pid == 0 || parent_pid > i32::MAX as u32 {
        return 0;
    }

    let mut sys = System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);

    // Breadth-first over parent links. `frontier` holds pids whose children
    // we have not looked for yet; `doomed` accumulates the descendants in
    // discovery order (parents before their own children).
    let mut frontier: Vec<u32> = vec![parent_pid];
    let mut doomed: Vec<sysinfo::Pid> = Vec::new();
    let mut seen: std::collections::HashSet<u32> = std::collections::HashSet::new();
    seen.insert(parent_pid);

    while let Some(pid) = frontier.pop() {
        for (child_pid, proc) in sys.processes() {
            if proc.parent().map(|p| p.as_u32()) != Some(pid) {
                continue;
            }
            let raw = child_pid.as_u32();
            // `seen` also terminates the walk on a malformed table that
            // reports a cycle; a process cannot be its own ancestor, but
            // this function must not hang if the OS says otherwise.
            if !seen.insert(raw) {
                continue;
            }
            frontier.push(raw);
            doomed.push(*child_pid);
        }
    }

    let mut signalled = 0usize;
    for pid in doomed {
        let Some(proc) = sys.process(pid) else {
            continue;
        };
        // PID-reuse guard. `start_time()` is epoch SECONDS; a process that
        // started well before our parent did cannot be a descendant of it,
        // so a match here means the PID was recycled and we are looking at
        // a stranger.
        //
        // `START_TIME_SLACK_SECS` is not decoration. `start_time()` is a
        // whole second, and on Linux it is derived from /proc's boot-
        // relative tick count, so it can land a second either side of the
        // caller's wall-clock reading. Comparing exactly rejected genuine
        // descendants — measured, not theorised (this is what the L-7 reap
        // test caught). The slack only widens what we are willing to kill
        // by ~2 s around our own spawn; anything OLDER than that is still
        // spared, which is the direction that matters.
        if proc.start_time() + START_TIME_SLACK_SECS < min_start_time_epoch_secs {
            tracing::debug!(
                "[process] kill_descendants: leaving pid {} alone — it \
                 started at {}, before the parent's {} (recycled pid)",
                pid.as_u32(),
                proc.start_time(),
                min_start_time_epoch_secs
            );
            continue;
        }
        // SIGTERM where the platform has signals; `kill()` (TerminateProcess
        // on Windows) where it does not. Same posture as update_gate's
        // sweeps: graceful first, never a bare SIGKILL by choice.
        let sent = match proc.kill_with(sysinfo::Signal::Term) {
            Some(ok) => ok,
            None => proc.kill(),
        };
        if sent {
            signalled += 1;
        }
    }
    signalled
}

/// Epoch seconds, saturating to 0 before 1970 — the unit
/// `kill_descendants`' `min_start_time_epoch_secs` expects, and the unit
/// `sysinfo`'s `Process::start_time()` reports in. Exposed so callers do
/// not hand-roll (and mis-unit) the comparison basis.
pub fn epoch_secs_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Why [`output_bounded`] produced no [`std::process::Output`].
#[derive(Debug)]
pub enum BoundedError {
    /// The child could not be started.
    Spawn(std::io::Error),
    /// The child was still running at the deadline; it was killed and
    /// reaped. Carries what it had written by then.
    TimedOut {
        after: std::time::Duration,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// Waiting on the child failed; it was killed and reaped.
    Wait(std::io::Error),
}

impl std::fmt::Display for BoundedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BoundedError::Spawn(e) => write!(f, "spawn failed: {e}"),
            BoundedError::TimedOut { after, .. } => {
                write!(f, "timed out after {} s (killed)", after.as_secs())
            }
            BoundedError::Wait(e) => write!(f, "wait failed: {e}"),
        }
    }
}

/// What a child run by [`output_bounded`] left behind — `std::process::Output`'s
/// fields, plus the error (if any) from writing `stdin` (a child that exits
/// without reading its input is evidence a caller may want to name).
#[derive(Debug)]
pub struct BoundedOutput {
    pub status: std::process::ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub stdin_error: Option<std::io::Error>,
}

/// Run `cmd` to completion with a HARD deadline — the ONE bounded runner for
/// a blocking child process (v0.2.100 F-W4-05 / W4R-05).
///
/// `std::process::Command::output()` has no timeout: a child that hangs (a
/// Python on a stalled network filesystem, an import waiting on a lock)
/// parks its caller forever — on the quit path that is a launcher that never
/// exits, on the hub's watchdog tick a supervision loop that never ticks
/// again. This spawns `cmd` with piped stdout/stderr (drained on their own
/// threads, so a chatty child cannot deadlock on a full pipe), writes
/// `stdin` when given (then closes it; `None` = the null device), polls for
/// exit, and at `limit` kills and reaps the child.
///
/// Blocking: call it from a sync context, or from async code through
/// `tokio::task::spawn_blocking`, so a slow child never pins a runtime
/// worker. Callers that log-and-swallow get a `Display`-able error that
/// names the bound.
pub fn output_bounded(
    cmd: &mut std::process::Command,
    stdin: Option<&[u8]>,
    limit: std::time::Duration,
) -> Result<BoundedOutput, BoundedError> {
    use std::io::{Read, Write};
    use std::process::Stdio;

    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    cmd.stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() });
    // Its own process group on POSIX, so the deadline kill reaches what the
    // child started too (`sh -c 'x; sleep 30'`): a surviving grandchild
    // would hold the pipes open and the bound would not be one.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt as _;
        cmd.process_group(0);
    }
    let mut child = cmd.spawn().map_err(BoundedError::Spawn)?;

    type Shared = std::sync::Arc<std::sync::Mutex<Vec<u8>>>;
    fn drain<P: Read + Send + 'static>(pipe: Option<P>, done: std::sync::mpsc::Sender<()>) -> Shared {
        let buf: Shared = Default::default();
        let sink = buf.clone();
        std::thread::spawn(move || {
            if let Some(mut p) = pipe {
                let mut chunk = [0u8; 8192];
                loop {
                    match p.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => sink.lock().unwrap_or_else(|e| e.into_inner()).extend_from_slice(&chunk[..n]),
                    }
                }
            }
            let _ = done.send(());
        });
        buf
    }
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    let out_buf = drain(child.stdout.take(), done_tx.clone());
    let err_buf = drain(child.stderr.take(), done_tx);
    // Written, then dropped: the child reads its stdin to EOF.
    let stdin_error = match (stdin, child.stdin.take()) {
        (Some(body), Some(mut sink)) => sink.write_all(body).err(),
        _ => None,
    };

    let deadline = std::time::Instant::now() + limit;
    let outcome = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) if std::time::Instant::now() >= deadline => {
                kill_tree(&mut child);
                break Err(None);
            }
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(20)),
            Err(e) => {
                kill_tree(&mut child);
                break Err(Some(e));
            }
        }
    };
    // The pipes reach EOF once every holder exits. A detached grandchild that
    // inherited them would keep them open past the child's own exit, so the
    // wait for EOF is itself bounded; what was read by then is kept.
    let grace = std::time::Duration::from_secs(2);
    for _ in 0..2 {
        if done_rx.recv_timeout(grace).is_err() {
            break;
        }
    }
    let take = |b: &Shared| std::mem::take(&mut *b.lock().unwrap_or_else(|e| e.into_inner()));
    let (stdout, stderr) = (take(&out_buf), take(&err_buf));
    match outcome {
        Ok(status) => Ok(BoundedOutput { status, stdout, stderr, stdin_error }),
        Err(None) => Err(BoundedError::TimedOut { after: limit, stdout, stderr }),
        Err(Some(e)) => Err(BoundedError::Wait(e)),
    }
}

/// Kill `child` — on POSIX its whole process group ([`output_bounded`] made
/// it a group leader) — and reap it.
fn kill_tree(child: &mut std::process::Child) {
    #[cfg(unix)]
    {
        if let Ok(pgid) = i32::try_from(child.id()) {
            if pgid > 0 {
                // SAFETY: kill(2) with a negative pid signals that process
                // group; pgid > 0 so this is never kill(0/-1, …).
                unsafe {
                    libc::kill(-pgid, libc::SIGKILL);
                }
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// How much of a failed child's stderr [`failure_evidence`] keeps.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StderrKeep {
    /// The first non-empty line, capped at [`STDERR_LINE_CAP`] characters —
    /// for one-line log entries and toasts.
    FirstLine,
    /// The whole trimmed text as given (callers cap it while reading).
    Whole,
}

/// Character cap of a [`StderrKeep::FirstLine`] snippet.
pub const STDERR_LINE_CAP: usize = 300;

/// The ONE rendering of "why did this child fail" (v0.2.100 F-W4-02 / I-06):
/// the process status — `ExitStatus`'s Display, `exit status: 1` /
/// `signal: 9 (SIGKILL)` / Windows `exit code: 1`, never a made-up `-1` —
/// then what stderr said, or `no stderr` when it said nothing. Shape:
/// `"<status>: <stderr evidence>"`.
pub fn failure_evidence(status: &std::process::ExitStatus, stderr: &str, keep: StderrKeep) -> String {
    let text = match keep {
        StderrKeep::FirstLine => stderr
            .lines()
            .map(str::trim)
            .find(|l| !l.is_empty())
            .map(|l| l.chars().take(STDERR_LINE_CAP).collect::<String>())
            .unwrap_or_default(),
        StderrKeep::Whole => stderr.trim().to_string(),
    };
    if text.is_empty() {
        format!("{status}: no stderr")
    } else {
        format!("{status}: {text}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pid_zero_is_not_alive() {
        // POSIX would treat kill(0, 0) as "the caller's process group";
        // Windows OpenProcess(0) returns NULL. Either way the sentinel
        // must short-circuit to false.
        assert!(!pid_is_alive(0));
    }

    #[test]
    fn pid_above_i32_max_is_not_alive() {
        assert!(!pid_is_alive(u32::MAX));
        assert!(!pid_is_alive((i32::MAX as u32) + 1));
    }

    #[test]
    fn own_pid_is_alive() {
        let me = std::process::id();
        assert!(pid_is_alive(me), "our own pid {} should report alive", me);
    }

    #[test]
    fn freshly_dead_pid_reports_dead() {
        // Spawn `true` (POSIX) / `cmd /c exit` (Windows), wait for it,
        // then check the PID. The OS may recycle PIDs but not before
        // the wait returns.
        #[cfg(unix)]
        let mut child = std::process::Command::new("true")
            .spawn()
            .expect("spawn true");
        #[cfg(windows)]
        let mut child = std::process::Command::new("cmd")
            .args(["/c", "exit"])
            .spawn()
            .expect("spawn cmd /c exit");
        let pid = child.id();
        let _ = child.wait();
        // Small sleep to let the kernel reap zombie + free the PID
        // slot on Linux. 50ms is enough in practice; the test is
        // tolerant if not (we'd false-positive "alive" rarely, but
        // CI doesn't see PID-recycle pressure on a 50ms scale).
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(!pid_is_alive(pid), "pid {} should be dead", pid);
    }

    // ─── kill_descendants (v0.2.96 L-7) ──────────────────────────────────
    //
    // unix-only fixtures (`sh` + `sleep` are the portable pair here); the
    // function itself has no per-OS branch — sysinfo covers all three.
    //
    // Which means these two tests are LINUX-ONLY in practice: `#[cfg(unix)]`
    // would run them on macOS, but no CI job runs `cargo test` on macOS or
    // Windows (both `cargo test` jobs are `ubuntu-latest` — see the
    // "Verification status" section on `kill_descendants`). Don't read the
    // `#[cfg(unix)]` gate as "covered on two platforms".

    #[cfg(unix)]
    fn spawn_sh_with_grandchild() -> (std::process::Child, u32) {
        let dir = std::env::temp_dir().join(format!(
            "vct-killdesc-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).expect("tmpdir");
        let pidfile = dir.join("gc.pid");
        let child = std::process::Command::new("/bin/sh")
            .arg("-c")
            .arg(format!("sleep 45 & echo $! > '{}'; wait", pidfile.display()))
            .spawn()
            .expect("spawn sh");
        // The shell writes the pid in its first milliseconds; poll rather
        // than sleep a fixed amount.
        let mut grandchild = None;
        for _ in 0..100 {
            if let Ok(raw) = std::fs::read_to_string(&pidfile) {
                if let Ok(pid) = raw.trim().parse::<u32>() {
                    grandchild = Some(pid);
                    break;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let _ = std::fs::remove_dir_all(&dir);
        (child, grandchild.expect("the shell recorded its child's pid"))
    }

    #[cfg(unix)]
    #[test]
    fn kill_descendants_reaps_a_grandchild_the_parent_kill_would_miss() {
        let (mut child, grandchild) = spawn_sh_with_grandchild();
        assert!(pid_is_alive(grandchild), "fixture: generator must be up");

        // Window open since the epoch: nothing is "too old" here, so the
        // start-time guard cannot be what passes this test.
        let killed = kill_descendants(child.id(), 0);
        assert!(killed >= 1, "expected at least the grandchild, got {killed}");

        let mut gone = false;
        for _ in 0..40 {
            if !pid_is_alive(grandchild) {
                gone = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
        assert!(gone, "generator pid {grandchild} survived");
    }

    /// LEAVE-ALONE half of the same decision: the PID-reuse guard. With a
    /// window that opens in the future, every candidate looks older than
    /// our spawn — i.e. looks like a recycled PID — and NOTHING is killed.
    /// Without this the guard could be inverted and the happy-path test
    /// above would still pass.
    #[cfg(unix)]
    #[test]
    fn kill_descendants_spares_processes_that_predate_the_window() {
        let (mut child, grandchild) = spawn_sh_with_grandchild();
        assert!(pid_is_alive(grandchild), "fixture: generator must be up");

        let far_future = epoch_secs_now() + 3600;
        let killed = kill_descendants(child.id(), far_future);
        assert_eq!(killed, 0, "a pre-window process must be spared");
        assert!(
            pid_is_alive(grandchild),
            "generator pid {grandchild} was killed despite predating the window"
        );

        let _ = child.kill();
        let _ = child.wait();
    }

    #[test]
    fn kill_descendants_refuses_sentinel_pids() {
        // 0 means "my whole process group" to POSIX kill, and anything
        // above i32::MAX casts to a negative pid_t ("every process I may
        // signal"). Neither is a parent whose children we want.
        assert_eq!(kill_descendants(0, 0), 0);
        assert_eq!(kill_descendants(u32::MAX, 0), 0);
    }

    #[test]
    fn epoch_secs_now_is_a_plausible_epoch_second() {
        // Sanity, not precision: after 2020-01-01 and before 2100.
        let now = epoch_secs_now();
        assert!(now > 1_577_836_800, "{now}");
        assert!(now < 4_102_444_800, "{now}");
    }

}

#[cfg(test)]
mod bounded_tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn sh(script: &str) -> std::process::Command {
        if cfg!(windows) {
            let mut c = std::process::Command::new("cmd");
            c.args(["/C", script]);
            c
        } else {
            let mut c = std::process::Command::new("sh");
            c.args(["-c", script]);
            c
        }
    }

    #[test]
    fn a_child_that_finishes_returns_its_output() {
        let out = output_bounded(&mut sh("echo hi"), None, Duration::from_secs(20)).expect("ran");
        assert!(out.status.success());
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "hi");
    }

    #[cfg(unix)]
    #[test]
    fn a_hung_child_is_killed_at_the_bound() {
        let started = Instant::now();
        let err = output_bounded(&mut sh("echo partial; sleep 30"), None, Duration::from_millis(400))
            .expect_err("must time out");
        assert!(started.elapsed() < Duration::from_secs(10), "bound not honoured");
        match err {
            BoundedError::TimedOut { stdout, .. } => {
                assert_eq!(String::from_utf8_lossy(&stdout).trim(), "partial")
            }
            other => panic!("expected TimedOut, got {other}"),
        }
    }

    #[cfg(unix)]
    #[test]
    fn stdin_is_delivered_and_closed() {
        let out = output_bounded(&mut sh("cat"), Some(b"body"), Duration::from_secs(20)).expect("ran");
        assert_eq!(out.stdout, b"body");
    }

    #[cfg(unix)]
    #[test]
    fn a_large_output_does_not_deadlock_the_pipe() {
        let out = output_bounded(
            &mut sh("head -c 2000000 /dev/zero"),
            None,
            Duration::from_secs(20),
        )
        .expect("ran");
        assert_eq!(out.stdout.len(), 2_000_000);
    }

    #[test]
    fn a_missing_program_is_a_spawn_error() {
        let mut c = std::process::Command::new("/definitely/not/a/program-vco");
        assert!(matches!(output_bounded(&mut c, None, Duration::from_secs(5)), Err(BoundedError::Spawn(_))));
    }

    #[cfg(unix)]
    #[test]
    fn failure_evidence_carries_the_status_and_the_first_line() {
        let out = output_bounded(&mut sh("printf '\\n  boom  \\nmore\\n' >&2; exit 3"), None, Duration::from_secs(20))
            .expect("ran");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(failure_evidence(&out.status, &stderr, StderrKeep::FirstLine), "exit status: 3: boom");
        assert!(failure_evidence(&out.status, &stderr, StderrKeep::Whole).ends_with("boom  \nmore"));
        assert_eq!(failure_evidence(&out.status, "  \n", StderrKeep::FirstLine), "exit status: 3: no stderr");
    }

    #[test]
    fn a_long_first_line_is_capped() {
        let out = output_bounded(&mut sh("exit 1"), None, Duration::from_secs(20)).expect("ran");
        let long = "x".repeat(STDERR_LINE_CAP * 2);
        let got = failure_evidence(&out.status, &long, StderrKeep::FirstLine);
        assert!(got.ends_with(&"x".repeat(STDERR_LINE_CAP)));
        assert!(!got.ends_with(&"x".repeat(STDERR_LINE_CAP + 1)));
    }
}
