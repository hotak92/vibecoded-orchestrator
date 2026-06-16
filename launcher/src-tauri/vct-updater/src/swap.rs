// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// The per-OS BINARY SWAP MECHANISM — extracted so BOTH the live swap-only
// `main()` (main.rs) AND the (dormant) cross-OS engine (engine.rs) call the
// SAME code instead of duplicating it.
//
// ============================================================================
// PER-OS BY DESIGN (knowledge/concepts/binary-swap-per-os-strategy-...md).
// ============================================================================
// The "replace the launcher/hub binary during update" step is fundamentally
// different by OS, and that difference is CORRECT, not a porting gap:
//
//   * WINDOWS: mandatory file-locking FORBIDS overwriting a running `.exe`.
//     The launcher renames the running `.exe` aside pre-pull and stages the
//     new bytes as `<target>.new`; THIS module's `swap_binary` does
//     `MoveFileEx(<target>.new → <target>, REPLACE_EXISTING | WRITE_THROUGH)`
//     once the parent PID has exited (lock released). The LIVE `main()` path
//     uses exactly this.
//
//   * POSIX (Linux/macOS): there is NO discrete swap step. `git pull`
//     overwrites the on-disk binary IN PLACE (the kernel ref-counts the old
//     inode for the running process; the new bytes land for next launch).
//     The "swap" on POSIX is therefore a NO-OP — imposing a `.new`/MoveFileEx
//     ceremony here would be a needless regression. The relaunch step
//     (re-exec the freshly-overwritten on-disk binary) is what completes the
//     POSIX update; that is the launcher's existing `restart_launcher` /
//     `WaitForBinaryRefresh` semantics, NOT a swap.
//
// The behaviour of `swap_binary` + `wait_for_parent_exit` here is the SAME
// logic that previously lived inline in main.rs (relocated verbatim, not
// reimplemented): on Windows it waits + MoveFileEx; on POSIX it is an explicit
// no-op stub. Relocating (not duplicating) keeps a single source of truth for
// the swap mechanism so the engine cannot drift from the live `main()` path.

// Some functions here are used only by `main()` on Windows, others only by
// the (dormant) engine — so per-OS some are "never used" in the live build.
// The allow keeps the dormant-on-this-OS halves warning-free; the LIVE
// Windows main() path uses `swap_binary`/`wait_for_parent_exit`/`spawn_detached`.
#![allow(dead_code)]

use std::path::Path;
#[cfg(target_os = "windows")]
use std::time::{Duration, Instant};

/// Maximum time (in seconds) we'll wait for the parent process to exit.
/// 30s is generous — typical launcher shutdown is sub-second; this guards
/// against a hung parent that would block the swap forever.
#[cfg(target_os = "windows")]
pub const PARENT_WAIT_TIMEOUT_SECS: u64 = 30;

/// Polling interval while waiting for the parent.
#[cfg(target_os = "windows")]
const PARENT_WAIT_POLL_MS: u64 = 200;

/// Outcome of a single swap attempt.
#[allow(dead_code)] // Swapped is constructed only on Windows
pub enum SwapResult {
    /// `<target>.new` was found and successfully renamed to `<target>`.
    Swapped,
    /// `<target>.new` did not exist — nothing to do (e.g. the launcher's
    /// canonical path already holds the new bytes via the pre-pull rename,
    /// or — on POSIX — git pull already overwrote in place).
    NoOpMissingNew,
}

#[cfg(target_os = "windows")]
pub enum WaitError {
    Timeout,
    AlreadyGone,
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
pub enum WaitError {
    Timeout,
    AlreadyGone,
}

// -----------------------------------------------------------------------------
// Windows: wait for the parent PID, then MoveFileEx the staged `.new` over the
// canonical target. (Relocated verbatim from the original main.rs.)
// -----------------------------------------------------------------------------

#[cfg(target_os = "windows")]
pub fn wait_for_parent_exit(pid: u32) -> Result<Duration, WaitError> {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0, WAIT_TIMEOUT};
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
    };

    let started = Instant::now();
    let deadline = started + Duration::from_secs(PARENT_WAIT_TIMEOUT_SECS);

    // SAFETY: OpenProcess is safe to call with any DWORD pid. A NULL return
    // indicates the process is gone or we lack permissions. Either way we
    // can't wait on it — treat as "already gone" so the swap proceeds.
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        return Err(WaitError::AlreadyGone);
    }

    loop {
        // Poll in small chunks so we can check our own deadline cleanly.
        // SAFETY: handle is valid (non-null check above) until CloseHandle.
        let wait_result = unsafe { WaitForSingleObject(handle, PARENT_WAIT_POLL_MS as u32) };
        if wait_result == WAIT_OBJECT_0 {
            unsafe { CloseHandle(handle) };
            return Ok(started.elapsed());
        }
        if wait_result != WAIT_TIMEOUT {
            // Unexpected wait state (handle invalidated mid-poll). Treat as
            // "already gone" so we proceed with the swap.
            unsafe { CloseHandle(handle) };
            return Err(WaitError::AlreadyGone);
        }
        if Instant::now() >= deadline {
            unsafe { CloseHandle(handle) };
            return Err(WaitError::Timeout);
        }
    }
}

#[cfg(target_os = "windows")]
pub fn swap_binary(target: &Path) -> Result<SwapResult, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    // Compute <target>.new — RELOCATED VERBATIM from the pre-extraction
    // main.rs (byte-for-byte behaviour preserved, incl. the no-extension
    // quirk below). On Windows — the ONLY OS that runs this — every swap
    // target carries a `.exe` extension, so the `if let Some(ext)` branch is
    // the one ever exercised in production; it yields `<name>.exe.new`,
    // matching `update_handoff.rs::with_new_suffix`. The `else` (no-extension)
    // branch is dead on Windows but kept identical to the original so this
    // extraction cannot alter any observable behaviour.
    let staged = target.with_extension(format!(
        "{}.new",
        target.extension().and_then(|s| s.to_str()).unwrap_or("")
    ));
    let staged = if let Some(ext) = target.extension().and_then(|s| s.to_str()) {
        let mut s = target.to_path_buf();
        s.set_extension(format!("{}.new", ext));
        s
    } else {
        staged
    };

    if !staged.exists() {
        return Ok(SwapResult::NoOpMissingNew);
    }

    // Convert paths to UTF-16 null-terminated strings (Windows API
    // requirement). encode_wide() yields the code units; append the null
    // terminator manually.
    let staged_w: Vec<u16> = staged
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let target_w: Vec<u16> = target
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    let flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH;

    // SAFETY: both pointers point to null-terminated UTF-16 strings valid
    // for the duration of the call (owned by `staged_w` / `target_w`).
    let ok = unsafe { MoveFileExW(staged_w.as_ptr(), target_w.as_ptr(), flags) };

    if ok == 0 {
        let err = unsafe { GetLastError() };
        return Err(format!("MoveFileExW failed: GetLastError={}", err));
    }
    Ok(SwapResult::Swapped)
}

/// Spawn a detached process (the relaunch). Windows: DETACHED_PROCESS +
/// CREATE_NEW_PROCESS_GROUP. POSIX: a plain detached spawn (setsid is the
/// launcher's `restart_launcher` job — the engine relaunch on POSIX uses the
/// re-exec path, see engine.rs).
#[cfg(target_os = "windows")]
pub fn spawn_detached(exe: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    use std::process::{Command, Stdio};

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
    const DETACHED_PROCESS: u32 = 0x00000008;

    let mut cmd = Command::new(exe);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);

    cmd.spawn()
        .map(|_child| ())
        .map_err(|e| format!("spawn {}: {}", exe.display(), e))
}

// -----------------------------------------------------------------------------
// POSIX stubs: the swap is a NO-OP (git pull overwrote in place; the kernel
// ref-counts the running inode). Kept compiling cross-platform so the engine
// doesn't have to gate every call by target_os.
// -----------------------------------------------------------------------------

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
pub fn wait_for_parent_exit(_pid: u32) -> Result<std::time::Duration, WaitError> {
    // POSIX: the engine's relaunch is a re-exec of the already-overwritten
    // on-disk binary; there is no parent-PID wait needed for a swap (there
    // is no swap). Stub for compile parity.
    Err(WaitError::AlreadyGone)
}

#[cfg(not(target_os = "windows"))]
pub fn swap_binary(_target: &Path) -> Result<SwapResult, String> {
    // POSIX: NO discrete swap step. `git pull` already overwrote the binary
    // in place. Returning NoOpMissingNew encodes "nothing to swap" — the
    // engine treats this as success and proceeds to the re-exec relaunch.
    // Imposing a `.new`/MoveFileEx ceremony here would be a regression.
    Ok(SwapResult::NoOpMissingNew)
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
pub fn spawn_detached(exe: &Path) -> Result<(), String> {
    use std::process::{Command, Stdio};
    // POSIX detached spawn WITHOUT libc/setsid: vct-updater deliberately
    // carries no `libc` dependency (it stays dependency-light per the
    // <2 MB constraint in Cargo.toml). Full session detachment (setsid) is
    // the launcher's `restart_launcher` job; for the dormant engine's POSIX
    // relaunch a null-stdio spawn is sufficient (the engine is itself a
    // detached process by the time it relaunches).
    let mut cmd = Command::new(exe);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    cmd.spawn()
        .map(|_child| ())
        .map_err(|e| format!("spawn {}: {}", exe.display(), e))
}
