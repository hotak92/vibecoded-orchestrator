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

/// An opaque token naming the process INSTANCE running as `pid`, or `None`
/// when it cannot be inspected (gone, no permission, sentinel pid).
///
/// v0.2.102 (W6-Z3): written as the third line of `<vct_root>/update.lock`
/// so `vco_lib/update_lock.py::decide` can tell the claimant from a newer
/// process handed the same pid WITHOUT comparing wall clocks (an NTP step
/// after the claim shifts a wall-clock-derived start time and read a live
/// holder as "pid reused").
///
/// MUST MATCH `vco_lib/deferral_probes.py::process_start_token` — the Python
/// home of the format; the readers compare the two:
///
/// * `proc:<ticks>` on Linux — field 22 of `/proc/<pid>/stat` (start time in
///   clock ticks since boot; not wall-clock based, so it never drifts);
/// * `wall:<seconds>.000` elsewhere — the creation timestamp the OS stored
///   when the process started, via `sysinfo` (whole seconds; the Python
///   reader `process_identity_matches` takes <= 1 s as the same process,
///   < 3 s as "cannot tell" and >= 3 s as a reused pid).
pub fn process_start_token(pid: u32) -> Option<String> {
    if pid == 0 || pid > i32::MAX as u32 {
        return None;
    }
    #[cfg(target_os = "linux")]
    {
        let raw = std::fs::read(format!("/proc/{pid}/stat")).ok()?;
        let raw = String::from_utf8_lossy(&raw);
        // comm may hold spaces/parens: the fields after the LAST ')' are
        // fixed — state is field 3, so starttime (field 22) is index 19.
        let (_, tail) = raw.rsplit_once(')')?;
        let ticks: u64 = tail.split_whitespace().nth(19)?.parse().ok()?;
        Some(format!("proc:{ticks}"))
    }
    #[cfg(not(target_os = "linux"))]
    {
        use sysinfo::{Pid, ProcessesToUpdate, System};
        let target = Pid::from_u32(pid);
        let mut sys = System::new();
        sys.refresh_processes(ProcessesToUpdate::Some(&[target]), true);
        let secs = sys.process(target)?.start_time();
        if secs == 0 {
            return None;
        }
        Some(format!("wall:{secs}.000"))
    }
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
/// `stdin` when given on its OWN thread — the write sits under the same
/// `limit` as the wait, so a child that never reads a large body cannot
/// block the caller (v0.2.102 NB-R4; `None` = the null device) — polls for
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
    // The stdin write runs on its OWN thread, under the same deadline as
    // the wait below (v0.2.102 NB-R4): pre-fix `sink.write_all(body)` ran
    // on the calling thread BEFORE the deadline loop, so a child that never
    // reads stdin, handed a body larger than the pipe buffer (≥ 64 KiB on
    // Linux), blocked the caller forever — the output drains were threaded,
    // the write was not. The write's Result is collected after the outcome
    // (a child that exits without reading its input surfaces EPIPE there,
    // the same evidence the inline write used to carry); a write still
    // blocked when the deadline fires is part of the SAME timeout — the
    // child is killed, which closes the pipe and unblocks the writer.
    let stdin_write = match (stdin, child.stdin.take()) {
        (Some(body), Some(mut sink)) => {
            // The thread must own its data (`'static`); the body is copied
            // once, bounded by the caller's input size.
            let body = body.to_vec();
            let (write_tx, write_rx) = std::sync::mpsc::channel::<std::io::Result<()>>();
            std::thread::spawn(move || {
                let r = sink.write_all(&body);
                drop(sink); // close the child's stdin (EOF for the child)
                let _ = write_tx.send(r);
            });
            Some(write_rx)
        }
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
        Ok(status) => {
            // Collect the stdin-write result: the child has exited by now,
            // which closes the read end and unblocks the writer. Bounded
            // wait for safety; a writer still stuck past the grace period
            // carries no error (the child side is gone — nothing to report).
            let stdin_error = match stdin_write {
                Some(rx) => match rx.recv_timeout(std::time::Duration::from_secs(2)) {
                    Ok(Err(e)) => Some(e),
                    Ok(Ok(())) | Err(_) => None,
                },
                None => None,
            };
            Ok(BoundedOutput { status, stdout, stderr, stdin_error })
        }
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

// ─── Code-graph walk single-flight (v0.2.103) ────────────────────────────
//
// The claim ITSELF lives in the Python SSOT (`vco_lib/codegraph_walk.py`,
// taken via `python -m vco_lib.codegraph_walk <analyzer> [args...]`); Rust
// NEVER mirrors the lock logic — it routes analyzer spawns through the
// module ([`claimed_walk_args`]) and interprets the refusal exit code.

/// Exit code the vco_lib walk-claim wrapper returns when it refuses to
/// start a twin walk of a project that is already walking (EX_TEMPFAIL:
/// transient — retry after the running walk finishes).
/// MUST MATCH `vco_lib/codegraph_walk.py::WALK_ALREADY_RUNNING_RC`.
pub const WALK_ALREADY_RUNNING_EXIT_CODE: i32 = 75;

/// True when a finished analyzer(-wrapper) child reported "another walk is
/// already running" — a refusal, not a failure: nothing was spawned.
pub fn is_walk_already_running(status: &std::process::ExitStatus) -> bool {
    status.code() == Some(WALK_ALREADY_RUNNING_EXIT_CODE)
}

/// The ONE user-facing sentence for the refusal (build toast, Re-analyze
/// modal, extra-path Sync error alike).
pub fn walk_already_running_message() -> String {
    "a code-graph walk is already running for this project — not starting a \
     second one; re-run after it finishes"
        .to_string()
}

/// The argv prefix that routes an analyzer spawn through the vco_lib walk
/// claim: `-m vco_lib.codegraph_walk <script>` (the analyzer args follow).
/// The interpreter running the module is the analyzer's interpreter — the
/// module re-spawns the script under `sys.executable` and inherits stdio, so
/// `--json-progress` streaming is unaffected. Callers MUST pass a python
/// that can import vco_lib (the RT-4 `resolve_python_for_vco_lib` ladder).
pub fn claimed_walk_args(script: &std::path::Path) -> Vec<std::ffi::OsString> {
    vec![
        std::ffi::OsString::from("-m"),
        std::ffi::OsString::from("vco_lib.codegraph_walk"),
        script.as_os_str().to_os_string(),
    ]
}

// ─── Analyzer child: concurrent pipe drain, no wall-clock deadline ───────
//
// v0.2.103 (owner rule "no timeout may cut valid long work"): analyzer
// waits have NO deadline — a code-graph walk may legitimately run for
// hours, and killing a slow-but-working walk makes it look like a
// failure so the user clicks again. With the deadline gone, an
// undrained pipe became the only way to hang forever: a piped stdout /
// stderr nobody reads fills its ~64 KiB kernel buffer and the child
// blocks on every write(2) — an unkillable deadlock, not a slow walk.
// These helpers are the ONE home for the composition every analyzer
// streaming site uses (codegraph_reanalyze.rs,
// project_codegraph_extras.rs): drain BOTH pipes concurrently for as
// long as the child lives, then reap it.

/// Drain a child's stderr while the child runs, keeping only the LAST
/// `max_bytes` as failure evidence (the end of the stream is where the
/// actual error is).
///
/// This MUST run concurrently with the child (and with the stdout
/// read): a piped stderr nobody reads fills the kernel's pipe buffer
/// (~64 KiB) and the child then blocks on every `write(2)` to it,
/// forever — with no wall-clock timeout there is nothing left to break
/// that deadlock, so the drain is what prevents it. The cap bounds
/// memory: >1 MiB of analyzer warnings still leaves a `max_bytes` tail.
pub async fn drain_child_stderr_capped(
    mut stderr: tokio::process::ChildStderr,
    max_bytes: usize,
) -> String {
    use tokio::io::AsyncReadExt as _;
    let mut chunk = [0u8; 4096];
    let mut tail: Vec<u8> = Vec::with_capacity(max_bytes);
    loop {
        match stderr.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                tail.extend_from_slice(&chunk[..n]);
                if tail.len() > max_bytes {
                    let excess = tail.len() - max_bytes;
                    tail.drain(..excess);
                }
            }
        }
    }
    String::from_utf8_lossy(&tail).into_owned()
}

/// Await an analyzer child with NO wall-clock deadline: consume both
/// stream futures concurrently (neither pipe can fill its kernel buffer
/// and wedge the child), then reap the child — however long it takes.
/// The streamed per-file progress events the caller emits through
/// `read` are the liveness signal. Returns the exit status plus the
/// capped stderr tail (failure evidence for the non-zero-exit branch).
pub async fn await_child_no_deadline<R, S, W>(
    read: R,
    drain_stderr: S,
    wait: W,
) -> Result<(std::process::ExitStatus, String), String>
where
    R: std::future::Future<Output = ()>,
    S: std::future::Future<Output = String>,
    W: std::future::Future<Output = std::io::Result<std::process::ExitStatus>>,
{
    let ((), stderr_tail) = tokio::join!(read, drain_stderr);
    let status = wait
        .await
        .map_err(|e| format!("wait analyzer: {}", e))?;
    Ok((status, stderr_tail))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── walk single-flight claim surface (v0.2.103) ─────────────────────

    /// Cross-language pin: the refusal code IS EX_TEMPFAIL and matches the
    /// Python SSOT `vco_lib/codegraph_walk.py::WALK_ALREADY_RUNNING_RC`
    /// (its own test pins the same literal on the Python side).
    #[test]
    fn walk_already_running_code_is_pinned_to_extempfail() {
        assert_eq!(WALK_ALREADY_RUNNING_EXIT_CODE, 75);
    }

    #[test]
    fn is_walk_already_running_matches_only_the_refusal_code() {
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            assert!(is_walk_already_running(
                &std::process::ExitStatus::from_raw(75 << 8)
            ));
            assert!(!is_walk_already_running(
                &std::process::ExitStatus::from_raw(1 << 8)
            ));
            // A signal kill has NO code — never mistaken for a refusal.
            assert!(!is_walk_already_running(
                &std::process::ExitStatus::from_raw(9)
            ));
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::ExitStatusExt;
            assert!(is_walk_already_running(
                &std::process::ExitStatus::from_raw(75)
            ));
            assert!(!is_walk_already_running(
                &std::process::ExitStatus::from_raw(1)
            ));
        }
    }

    #[test]
    fn walk_already_running_message_names_the_twin_refusal() {
        let msg = walk_already_running_message();
        assert!(msg.contains("already running"), "{msg}");
        assert!(msg.contains("not starting a second one"), "{msg}");
    }

    #[test]
    fn claimed_walk_args_route_through_the_vco_lib_claim_module() {
        let script = std::path::Path::new("/x/.claude/scripts/analyze_code_graph.py");
        let args = claimed_walk_args(script);
        let rendered: Vec<String> = args
            .iter()
            .map(|a| a.to_string_lossy().to_string())
            .collect();
        assert_eq!(
            rendered,
            vec![
                "-m".to_string(),
                "vco_lib.codegraph_walk".to_string(),
                script.to_string_lossy().to_string()
            ],
            "the analyzer spawn must go through the Python claim SSOT"
        );
    }

    #[test]
    fn pid_zero_is_not_alive() {
        // POSIX would treat kill(0, 0) as "the caller's process group";
        // Windows OpenProcess(0) returns NULL. Either way the sentinel
        // must short-circuit to false.
        assert!(!pid_is_alive(0));
    }

    #[test]
    fn start_token_is_stable_for_a_live_process_and_none_for_sentinels() {
        let me = std::process::id();
        let a = process_start_token(me).expect("own token readable");
        assert_eq!(process_start_token(me).as_deref(), Some(a.as_str()), "stable");
        assert!(a.starts_with("proc:") || a.starts_with("wall:"), "{a}");
        assert_eq!(process_start_token(0), None);
        assert_eq!(process_start_token(u32::MAX), None);
    }

    /// The `/proc/<pid>/stat` index matches the Python twin's
    /// (`rsplit(')', 1)[1].split()[19]`) — including a comm holding `)`.
    #[cfg(target_os = "linux")]
    #[test]
    fn start_token_reads_field_22_after_the_last_paren() {
        let raw = std::fs::read_to_string("/proc/self/stat").unwrap();
        let tail = raw.rsplit_once(')').unwrap().1;
        let want = tail.split_whitespace().nth(19).unwrap();
        assert_eq!(
            process_start_token(std::process::id()).unwrap(),
            format!("proc:{want}")
        );
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

    /// v0.2.102 NB-R4: pre-fix, `sink.write_all(body)` ran on the CALLING
    /// thread before the deadline loop — a child that never reads stdin,
    /// handed a body larger than the pipe buffer (≥ 64 KiB on Linux),
    /// parked the caller forever while the output drains were threaded.
    /// The write now runs under the same deadline: the call must return a
    /// `TimedOut` of the same shape as the output timeout. The hard outer
    /// guard (thread + channel) turns a pre-fix infinite hang into a FAILED
    /// TEST instead of a hung suite — 15 s is far above the 600 ms bound
    /// and far below "forever".
    #[cfg(unix)]
    #[test]
    fn a_child_that_never_reads_stdin_does_not_block_the_caller() {
        let body = vec![b'x'; 256 * 1024]; // > the 64 KiB Linux pipe buffer
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let r = output_bounded(&mut sh("sleep 30"), Some(&body), Duration::from_millis(600));
            let _ = tx.send(r);
        });
        let result = rx
            .recv_timeout(Duration::from_secs(15))
            .expect("output_bounded never returned — the stdin write is blocking the caller");
        let err = result.expect_err("a sleeping child must hit the deadline");
        match err {
            BoundedError::TimedOut { after, .. } => {
                assert_eq!(after, Duration::from_millis(600));
            }
            other => panic!("expected TimedOut, got {other}"),
        }
    }

    /// LEAVE-ALONE half of NB-R4: a child that DOES read stdin to EOF is
    /// unaffected by the threaded write — the body is delivered in full and
    /// no stdin error is reported (the existing `stdin_is_delivered_and_
    /// closed` covers the small body; this one is larger than the pipe
    /// buffer, so it exercises the writer thread's chunked progress).
    #[cfg(unix)]
    #[test]
    fn a_large_stdin_body_is_delivered_to_a_reading_child() {
        let body = vec![b'y'; 256 * 1024];
        let out = output_bounded(&mut sh("cat"), Some(&body), Duration::from_secs(20)).expect("ran");
        assert_eq!(out.stdout, body);
        assert!(out.stdin_error.is_none(), "stdin error: {:?}", out.stdin_error);
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
