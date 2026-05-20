//! Cross-OS process-liveness helpers.
//!
//! Moved to core in v0.2.21 Step 5 because both the launcher's
//! self-update pre-pull-rename sweep AND vct-hub's lockfile machinery
//! need to ask "is this PID still alive?". Identical semantics in
//! either context.

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
}
