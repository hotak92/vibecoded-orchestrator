// v0.2.15 (Agent D, 2026-05-17): launcher self-restart after binary swap.
//
// Background
// ----------
// When `install.py --update` succeeds AND the dist binary at
// `launcher/dist/<arch>/vct-launcher[.exe]` gets refreshed by
// `_refresh_dist_binary_after_rebuild`, the launcher binary on disk is
// new but the *running* launcher process keeps executing the OLD code
// in memory until it restarts.
//
// On Linux/macOS the file swap actually succeeds (the open inode is held
// by the running process; the unlink+rewrite produces a new inode at the
// same path) but the user sees no signal that they need to restart. They
// click "Update", a toast says "success", and they keep using the old
// version indefinitely. On Windows the swap usually fails up-front with
// ERROR_SHARING_VIOLATION; install.py has a rename-then-write fallback
// that succeeds in most cases.
//
// install.py emits a `launcher_restart_required` deferral entry on
// successful swap. The GUI surfaces a green sticky banner with a
// "Restart now" button which invokes this command.
//
// What this does
// --------------
// 1. Read+rewrite `<install_root>/.claude/context/UPDATE_DEFERRED.md` to
//    drop the `launcher_restart_required` entry. Skipping this step means
//    the next launcher start would render the banner again — perpetual
//    nag loop. Best-effort: a write failure is logged but does NOT block
//    the restart itself.
//
// 2. Locate the launcher binary. The dist path
//    (`launcher/dist/<arch>/vct-launcher[.exe]`) is the freshly-swapped
//    binary. `std::env::current_exe()` returns the path the OS used to
//    launch us — on Linux/macOS this equals the dist path after the
//    inode swap; on Windows the rename-fallback may have moved us aside
//    so we re-resolve from the dist path explicitly.
//
// 3. Spawn the new binary FULLY DETACHED. Critical: a child process that
//    inherits stdin/stdout/stderr from the about-to-exit parent will
//    have its handles closed when we call `app.exit(0)`. The new
//    launcher must be its own process group leader (Unix) /
//    detached-process (Windows) so the kernel doesn't tear it down with
//    us.
//
// 4. Call `app.exit(0)` to terminate the current process. The new
//    process is already running.
//
// Cross-OS notes
// --------------
// Unix (Linux + macOS): `pre_exec` runs `setsid(2)` in the forked child
// before exec. This makes the child a new session leader, detaching it
// from our controlling terminal and process group. `nix` crate is NOT
// in our dep tree (we use libc directly for the few syscalls we need
// elsewhere); we call libc::setsid here too.
//
// Windows: creation flags `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`
// achieve the same thing. CREATE_NEW_PROCESS_GROUP prevents the child
// from being signalled when we receive Ctrl+C; DETACHED_PROCESS detaches
// it from our console (the launcher is a GUI app so we shouldn't have
// one, but defense in depth).
//
// State loss across restart
// -------------------------
// The new launcher is a fresh process. State that does NOT survive:
//   - WebView's localStorage scoped to the prior process. We moved most
//     state into launcher.db (`app_state` table) for exactly this
//     reason (Bug 14, v0.2.5). Anything still relying on localStorage
//     resets to its default.
//   - In-progress background tasks (kg-sync runs, codegraph rebuilds).
//     The next launcher start re-spawns them per project; users see a
//     ~5-30s pause before the "Updating KG" badge clears.
//   - The Tauri event subscribers (tray-pill probes, settings.json
//     watcher) — re-attached on fresh start.
//
// State that DOES survive:
//   - launcher.db (`~/.vct/launcher.db`) is on disk; SQLite handles
//     reopen.
//   - Per-project `.claude/CONTEXT_STATE.md`, projects.json — on disk.
//   - VCO_dev-style secrets in `~/.vct-secrets/` and OS keychain —
//     untouched.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{Duration, SystemTime};

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Runtime};
use vct_launcher_core::process::CommandExt as _;

/// v0.2.54 Track D (Theme 5): process boot instant, initialized in
/// `lib.rs::setup` (and defensively at first use here). Used to detect
/// STALE `launcher_restart_required` entries: an entry written BEFORE
/// this process started means this process already loaded the post-swap
/// binary, so the "restart now" nag is satisfied and the entry can
/// self-clear. Pre-fix, a manual quit+relaunch (anything but the green
/// banner button) left the entry on disk forever — the new launcher
/// re-rendered the restart banner on every boot, and install.py's
/// `--apply-deferred` had no handler either.
pub static LAUNCHER_BOOT_TIME: OnceLock<SystemTime> = OnceLock::new();

/// Safety margin for the staleness comparison. The deferral file's
/// mtime must precede the boot instant by at least this much before we
/// self-clear — protects against same-second writes and coarse
/// filesystem timestamp granularity. False-keep is the safe direction
/// (banner persists; the button path still clears it); false-clear is
/// the one we must never take.
const RESTART_ENTRY_STALE_MARGIN: Duration = Duration::from_secs(2);

/// True iff the `launcher_restart_required` entry on disk predates this
/// launcher process (entry written, then the launcher restarted by ANY
/// means) — i.e. the running binary is already the post-swap one.
fn restart_entry_is_stale(deferred_md: &Path) -> bool {
    let boot = *LAUNCHER_BOOT_TIME.get_or_init(SystemTime::now);
    restart_entry_is_stale_at(deferred_md, boot)
}

/// Boot-instant-parameterised core of `restart_entry_is_stale` (split
/// out so tests can exercise the comparison without mutating the
/// process-global `LAUNCHER_BOOT_TIME` OnceLock).
fn restart_entry_is_stale_at(deferred_md: &Path, boot: SystemTime) -> bool {
    let mtime = match std::fs::metadata(deferred_md).and_then(|m| m.modified()) {
        Ok(t) => t,
        Err(_) => return false, // cannot read mtime → conservative keep
    };
    match boot.duration_since(mtime) {
        Ok(age_at_boot) => age_at_boot >= RESTART_ENTRY_STALE_MARGIN,
        Err(_) => false, // file written after boot → entry is fresh
    }
}

/// Fallback signal for install.py's `launcher_restart_required` handler:
/// when the boot-time self-clear could not rewrite UPDATE_DEFERRED.md
/// (I/O failure, permissions), drop the documented marker file so the
/// next `install.py --update --apply-deferred` run consumes it and
/// clears the entry instead. See install.py::_apply_deferred_entries.
fn write_restart_marker(install_root: &Path) {
    let marker = install_root
        .join(".claude")
        .join("context")
        .join("launcher-restart-marker");
    if let Some(parent) = marker.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let stamp = chrono::Utc::now().to_rfc3339();
    if let Err(e) = std::fs::write(&marker, format!("restarted-at: {}\n", stamp)) {
        eprintln!(
            "[restart] could not write launcher-restart-marker at {}: {}",
            marker.display(),
            e,
        );
    }
}

/// Result of `get_launcher_restart_status`: presence + details of a
/// `launcher_restart_required` or `launcher_binary_swap_failed_locked`
/// deferral entry in the orchestrator's UPDATE_DEFERRED.md.
///
/// Empty struct (None for every field) when no such entries exist — the
/// FE renders nothing in that case.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LauncherRestartStatus {
    /// True iff a `launcher_restart_required` entry is present.
    pub restart_required: bool,
    /// True iff a `launcher_binary_swap_failed_locked` entry is present
    /// (Windows-only path).
    pub swap_failed_locked: bool,
    /// New launcher version parsed from the entry title (best-effort —
    /// None when the entry's title doesn't match the expected pattern).
    pub new_version: Option<String>,
    /// Path of the newly-swapped binary, parsed from the entry's
    /// "Detected" field. None when unparseable.
    pub new_binary_path: Option<String>,
    /// Full "Detected" prose for the swap-failed case — surfaced verbatim
    /// in the red recovery banner.
    pub failure_detail: Option<String>,
}

/// Tauri command: read `<install_root>/.claude/context/UPDATE_DEFERRED.md`
/// and return whether a launcher-restart or binary-swap-locked entry is
/// present. Polled by the FE banner on mount + every ~5s to stay in sync
/// with install.py runs that may write the entry mid-session.
///
/// Returns an all-false struct when the file doesn't exist or contains
/// no relevant entries — never errors on a missing file.
#[command]
pub async fn get_launcher_restart_status(
    install_root: String,
) -> Result<LauncherRestartStatus, String> {
    let install_root_path = PathBuf::from(&install_root);
    let target = install_root_path
        .join(".claude")
        .join("context")
        .join("UPDATE_DEFERRED.md");
    if !target.is_file() {
        return Ok(LauncherRestartStatus::default());
    }

    let content = std::fs::read_to_string(&target)
        .map_err(|e| format!("read {}: {}", target.display(), e))?;

    let mut restart_section = extract_section(&content, "launcher_restart_required");
    let locked_section = extract_section(&content, "launcher_binary_swap_failed_locked");

    // v0.2.54 Track D (Theme 5): stale-entry self-clear. If the
    // restart entry was written BEFORE this launcher process started,
    // the running process already loaded the post-swap binary — the
    // restart the entry asks for has happened (manual quit+relaunch,
    // OS reboot, anything but the banner button, whose own path strips
    // the entry directly). Clear it instead of re-rendering the banner
    // forever. Margin + mtime-after-boot cases conservatively KEEP the
    // entry — false-keep is recoverable (button click), false-clear is
    // not.
    if restart_section.is_some() && restart_entry_is_stale(&target) {
        eprintln!(
            "[restart] launcher_restart_required entry predates this \
             process (running binary is post-swap) — self-clearing",
        );
        match clear_restart_deferral(&install_root_path) {
            Ok(()) => {
                restart_section = None;
            }
            Err(e) => {
                // Could not rewrite the deferral file — leave the entry
                // (the banner shows; the button path may still succeed)
                // and drop the documented marker so install.py's
                // `--apply-deferred` handler clears it on its side.
                eprintln!(
                    "[restart] self-clear failed (non-fatal): {} — \
                     writing launcher-restart-marker fallback",
                    e,
                );
                write_restart_marker(&install_root_path);
            }
        }
    }

    let mut status = LauncherRestartStatus {
        restart_required: restart_section.is_some(),
        swap_failed_locked: locked_section.is_some(),
        ..Default::default()
    };

    if let Some(section) = restart_section.as_deref() {
        // Title format: "Launcher binary updated to <version>"
        status.new_version = section
            .lines()
            .find(|l| l.starts_with("**Title**:"))
            .and_then(|l| l.split("updated to").nth(1))
            .map(|s| s.trim().to_string());
        // Detected: "...swapped into `<path>`..."
        status.new_binary_path = section
            .lines()
            .find(|l| l.contains("swapped into `"))
            .and_then(|l| {
                let after = l.split("swapped into `").nth(1)?;
                after.split('`').next().map(|s| s.to_string())
            });
    }

    if let Some(section) = locked_section.as_deref() {
        status.failure_detail = section
            .lines()
            .find(|l| l.starts_with("**Detected**:"))
            .map(|l| l.trim_start_matches("**Detected**:").trim().to_string());
        if status.new_binary_path.is_none() {
            status.new_binary_path = section
                .lines()
                .find(|l| l.contains("launcher binary at `"))
                .and_then(|l| {
                    let after = l.split("launcher binary at `").nth(1)?;
                    after.split('`').next().map(|s| s.to_string())
                });
        }
    }

    Ok(status)
}

/// Return the body text of a single `## <condition_id> (sev)` section,
/// from the header line through the section terminator `---`. None when
/// the section is absent. Used by `get_launcher_restart_status` to pull
/// title/detected fields per-entry without re-parsing the whole file.
fn extract_section(content: &str, condition_id: &str) -> Option<String> {
    let header_prefix = format!("## {} (", condition_id);
    let start = content.find(&header_prefix)?;
    let rest = &content[start..];
    let end = rest
        .find("\n## ")
        .or_else(|| rest.find("\n---\n").map(|i| i + 5))
        .unwrap_or(rest.len());
    Some(rest[..end].to_string())
}

/// Tauri command: restart the launcher process to load a freshly-swapped
/// binary. Invoked by the green "Restart now" banner the GUI renders for
/// `launcher_restart_required` deferral entries.
///
/// `install_root` is the path of the orchestrator clone whose update
/// just landed (passed by the frontend; it comes from the same store
/// the "Update orchestrator" button uses). Used to locate UPDATE_DEFERRED.md
/// and the dist binary.
#[command]
pub async fn restart_launcher<R: Runtime>(
    app: AppHandle<R>,
    install_root: String,
) -> Result<(), String> {
    let install_root_path = PathBuf::from(&install_root);

    // Step 1: clear the launcher_restart_required entry from
    // UPDATE_DEFERRED.md so the next launcher start doesn't re-render
    // the banner. Best-effort: failures here are logged but don't block
    // the restart.
    if let Err(e) = clear_restart_deferral(&install_root_path) {
        eprintln!(
            "[restart_launcher] failed to clear deferral (non-fatal): {}",
            e
        );
    }

    // Step 2: pick the binary path to spawn. Prefer the dist path under
    // install_root (this is what install.py just refreshed). Fall back
    // to current_exe() if dist is missing — exotic case (someone
    // deleted the dist tree between install + restart click).
    let exe = resolve_target_binary(&install_root_path)
        .or_else(|_| std::env::current_exe().map_err(|e| e.to_string()))?;

    if !exe.is_file() {
        return Err(format!("launcher binary not found at {}", exe.display()));
    }

    // Step 3: spawn the new launcher detached.
    spawn_detached_launcher(&exe)?;

    // Step 4: programmatic quit. Bypass the Quit-confirmation dialog
    // (the user already clicked Restart; a second confirmation would
    // be confusing and could orphan the new launcher if dismissed).
    crate::quit_dialog::force_quit();
    app.exit(0);
    Ok(())
}

/// Read `<install_root>/.claude/context/UPDATE_DEFERRED.md`, strip the
/// `## launcher_restart_required (...)` section, and write back. If the
/// file ends up with zero entries, delete it (matches the
/// `DeferralReport.write` contract on the Python side).
///
/// This is intentionally a simple text-level edit rather than a full
/// re-implementation of the deferral parser — we only need to remove
/// one well-formed section. The Python writer always emits sections
/// terminated by a literal `---\n` line per `_render_entry`, so the
/// regex below is safe.
///
/// Returns Ok(()) on success OR when the file doesn't exist (nothing
/// to clear). Returns Err(String) on I/O failure mid-write.
fn clear_restart_deferral(install_root: &Path) -> Result<(), String> {
    let target = install_root
        .join(".claude")
        .join("context")
        .join("UPDATE_DEFERRED.md");
    if !target.is_file() {
        return Ok(());
    }

    let content =
        std::fs::read_to_string(&target).map_err(|e| format!("read {}: {}", target.display(), e))?;

    // Find and strip the launcher_restart_required section. The section
    // header pattern matches `## launcher_restart_required (<severity>)`
    // anchored at the start of a line; the section runs until the next
    // `## ` header OR end-of-file. The `---` separator after each entry
    // (`_SECTION_SEP` in Python) is part of the section's tail.
    let updated = strip_section(&content, "launcher_restart_required");

    // If no sections remain (only frontmatter + header), delete the file
    // entirely to match the Python `DeferralReport.write` contract
    // (empty entries → unlink). Detection heuristic: the body after the
    // YAML frontmatter contains no `## <cid>` header.
    let has_any_entry = updated
        .lines()
        .any(|line| line.starts_with("## ") && !line.starts_with("## VCO Update"));

    if !has_any_entry {
        // v0.2.43 V0243-8: preserve stub files. When the frontmatter
        // declares `stub: true` this file is a test fixture or a
        // synthetic placeholder that must survive the clear operation.
        // Deleting it would cause the next launcher boot to lose the
        // stub entry and re-render the restart banner spuriously.
        if frontmatter_has_stub_flag(&updated) {
            eprintln!(
                "[restart] UPDATE_DEFERRED.md at {} has stub:true — \
                 preserving file rather than unlinking (no real entries remain)",
                target.display(),
            );
            // Write the stripped content so the launcher_restart_required
            // section is gone, but the stub file itself stays on disk.
            std::fs::write(&target, updated)
                .map_err(|e| format!("write (stub preserve) {}: {}", target.display(), e))?;
            return Ok(());
        }

        // Sweep the file. Strip the CLAUDE.md reminder block too — keep
        // parity with the Python writer. We do not modify CLAUDE.md
        // from Rust here; the next install.py run will strip the block
        // via _strip_claude_md_reminder. Acceptable lag: the reminder
        // says "go read UPDATE_DEFERRED.md" but the file is gone — the
        // user sees the stale block at most once.
        std::fs::remove_file(&target)
            .map_err(|e| format!("unlink {}: {}", target.display(), e))?;
        return Ok(());
    }

    std::fs::write(&target, updated).map_err(|e| format!("write {}: {}", target.display(), e))?;
    Ok(())
}

/// Strip the `## <condition_id> (<severity>) ... ---\n` section from the
/// deferral markdown body. The Python writer's `_render_entry` always
/// terminates each entry with `\n---\n` (`_SECTION_SEP`). We anchor on
/// the next `\n## ` header OR end-of-file to handle the last-entry case.
fn strip_section(content: &str, condition_id: &str) -> String {
    let header_prefix = format!("## {} (", condition_id);
    let Some(start) = content.find(&header_prefix) else {
        return content.to_string();
    };

    // Find the end: either the next `## ` header (start of another
    // section) or end of file. We search from `start + 1` to skip the
    // current header.
    let search_from = start + 1;
    let rest = &content[search_from..];
    let end = rest
        .find("\n## ")
        .map(|idx| search_from + idx + 1) // +1 to include the newline before `##`
        .unwrap_or_else(|| content.len());

    // Trim a trailing blank line so we don't leave double-blank gaps.
    let mut prefix = content[..start].to_string();
    let suffix = &content[end..];
    if prefix.ends_with("\n\n") {
        prefix.pop();
    }
    prefix.push_str(suffix);
    prefix
}

/// v0.2.43 V0243-8: return true when the YAML frontmatter of a deferral
/// document contains `stub: true`.
///
/// The frontmatter is the `---`-delimited block at the top of the file.
/// We look for a line matching `stub: true` (with optional surrounding
/// whitespace) within that block only — not in section bodies. This
/// guards against pathological manifests where a section body happens to
/// contain the string.
///
/// Returns false when the file has no frontmatter, the frontmatter does
/// not contain the stub key, or the value is anything other than `true`.
fn frontmatter_has_stub_flag(content: &str) -> bool {
    // Frontmatter is bracketed by two `---` lines. The leading `---` must
    // be at position 0 (very start of the file); the closing `---` ends
    // the block.
    if !content.starts_with("---") {
        return false;
    }
    // Find the closing delimiter. Skip the opening `---`.
    let after_open = &content[3..];
    let close_pos = after_open.find("\n---")
        .map(|i| 3 + i + 1) // absolute start of `---\n` in `content`
        .unwrap_or(0);
    if close_pos == 0 {
        return false; // no closing delimiter found
    }
    let frontmatter = &content[3..close_pos]; // between the two `---` markers
    frontmatter
        .lines()
        .any(|line| matches!(line.trim(), "stub: true" | "stub:true"))
}

/// Resolve the dist binary path under `install_root` for the current OS.
/// Mirrors `install.py::_launcher_binary_relative_path`.
fn resolve_target_binary(install_root: &Path) -> Result<PathBuf, String> {
    let (subdir, fname) = launcher_binary_relative_path();
    Ok(install_root
        .join("launcher")
        .join("dist")
        .join(subdir)
        .join(fname))
}

/// Mirror of `install.py::_launcher_binary_relative_path`. Keep in sync.
fn launcher_binary_relative_path() -> (&'static str, &'static str) {
    #[cfg(target_os = "windows")]
    {
        ("windows-x64", "vct-launcher.exe")
    }
    #[cfg(target_os = "macos")]
    {
        ("macos-arm64", "vct-launcher")
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        ("linux-x64", "vct-launcher")
    }
}

/// Spawn the new launcher fully detached. The current process exits
/// immediately afterward; the child must be in its own session/process
/// group so the kernel doesn't tear it down with us.
fn spawn_detached_launcher(exe: &Path) -> Result<(), String> {
    let mut cmd = std::process::Command::new(exe).silent();
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: setsid(2) is signal-safe and async-signal-safe on every
        // POSIX system. The pre_exec closure runs in the forked child
        // after fork() but before exec() — at that point the child has a
        // single thread (the forking one) so no synchronization primitives
        // are at risk. Returning Ok keeps the exec path; returning an
        // io::Error would abort the spawn.
        unsafe {
            cmd.pre_exec(|| {
                // setsid() makes the child a new session leader, which
                // also detaches it from the controlling terminal of the
                // parent. Failing here would still leave the child
                // alive (just not detached) — choose to log+continue
                // by returning Ok regardless. The detach is belt-and-
                // braces with stdin/stdout/stderr null.
                let _ = libc::setsid();
                Ok(())
            });
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
        const DETACHED_PROCESS: u32 = 0x00000008;
        cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);
    }

    cmd.spawn()
        .map(|_child| ())
        .map_err(|e| format!("failed to spawn new launcher at {}: {}", exe.display(), e))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_section_removes_named_block_and_keeps_others() {
        let body = "\
---
title: VCO Update Deferred
condition_ids: [launcher_restart_required, schema_drift_rebuild_required]
---

# VCO Update Deferred

## launcher_restart_required (info)

**Title**: Launcher binary updated to 0.2.15

**Detected**: blah.

**To apply**:
```bash
echo restart
```

**Detected at**: 2026-05-17T12:00:00Z

---

## schema_drift_rebuild_required (warning)

**Title**: Schema rebuild required

**Detected**: drift detected.

---
";
        let out = strip_section(body, "launcher_restart_required");
        // The `## launcher_restart_required (info)` section + body must be
        // gone, but the frontmatter still mentions the condition_id in
        // its `condition_ids:` list (we don't regenerate frontmatter — the
        // next install.py run will rewrite the whole file fresh).
        assert!(!out.contains("## launcher_restart_required"),
                "section header still present: {}", out);
        assert!(!out.contains("Launcher binary updated to 0.2.15"),
                "section body still present: {}", out);
        assert!(out.contains("schema_drift_rebuild_required"),
                "other entry must be preserved: {}", out);
        assert!(out.contains("## schema_drift_rebuild_required"),
                "other section header must remain: {}", out);
    }

    #[test]
    fn strip_section_handles_only_entry_case() {
        let body = "\
---
condition_ids: [launcher_restart_required]
---

# VCO Update Deferred

## launcher_restart_required (info)

**Title**: foo

---
";
        let out = strip_section(body, "launcher_restart_required");
        // After stripping, only frontmatter + header should remain.
        assert!(!out.contains("## launcher_restart_required"));
        assert!(out.contains("VCO Update Deferred"));
    }

    #[test]
    fn strip_section_unknown_condition_is_noop() {
        let body = "## something_else (warning)\n\n---\n";
        let out = strip_section(body, "launcher_restart_required");
        assert_eq!(out, body);
    }

    #[test]
    fn clear_restart_deferral_unlinks_when_only_entry() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        std::fs::write(
            &target,
            "---\ncondition_ids: [launcher_restart_required]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        )
        .expect("write");

        clear_restart_deferral(tmp.path()).expect("clear");
        assert!(!target.exists(), "file should be removed when no entries remain");
    }

    #[test]
    fn clear_restart_deferral_rewrites_when_other_entries_present() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        let original = "---\ncondition_ids: [launcher_restart_required, other_thing]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n\n## other_thing (warning)\n\n**Title**: bar\n\n---\n";
        std::fs::write(&target, original).expect("write");

        clear_restart_deferral(tmp.path()).expect("clear");
        assert!(target.exists(), "file should remain when other entries exist");
        let new_content = std::fs::read_to_string(&target).expect("read");
        // The launcher_restart_required SECTION must be gone (header + body);
        // the frontmatter still lists the condition_id but that's regenerated
        // on the next install.py run.
        assert!(!new_content.contains("## launcher_restart_required"));
        assert!(new_content.contains("other_thing"));
        assert!(new_content.contains("## other_thing"));
    }

    #[test]
    fn clear_restart_deferral_missing_file_is_ok() {
        let tmp = tempfile::tempdir().expect("tempdir");
        // No file created — must not error.
        clear_restart_deferral(tmp.path()).expect("missing-file must be Ok");
    }

    // -----------------------------------------------------------------
    // v0.2.43 V0243-8: stub-protect tests.
    // -----------------------------------------------------------------

    /// V0243-8 T1: a file with `stub: true` in its frontmatter is NOT
    /// deleted even when no real entries remain after stripping.
    #[test]
    fn clear_restart_deferral_preserves_stub_file_when_only_entry() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        // Frontmatter with stub: true
        let stub_content = "\
---
condition_ids: [launcher_restart_required]
stub: true
---

# VCO Update Deferred

## launcher_restart_required (info)

**Title**: foo

---
";
        std::fs::write(&target, stub_content).expect("write");

        clear_restart_deferral(tmp.path()).expect("clear");

        // File must NOT be deleted because stub: true.
        assert!(target.exists(), "stub file must be preserved, not deleted");
        let after = std::fs::read_to_string(&target).expect("read after");
        // The launcher_restart_required section must have been stripped.
        assert!(!after.contains("## launcher_restart_required"),
                "section must still be removed from stub file");
    }

    /// V0243-8 T2: `frontmatter_has_stub_flag` returns true for stub files.
    #[test]
    fn frontmatter_has_stub_flag_returns_true_for_stub_files() {
        let stub = "---\ncondition_ids: [x]\nstub: true\n---\n\n# body";
        assert!(frontmatter_has_stub_flag(stub));
    }

    /// V0243-8 T3: `frontmatter_has_stub_flag` returns false for normal files.
    #[test]
    fn frontmatter_has_stub_flag_returns_false_for_normal_files() {
        let normal = "---\ncondition_ids: [x]\n---\n\n# body";
        assert!(!frontmatter_has_stub_flag(normal));
    }

    /// V0243-8 T4: normal file (no stub flag) is still deleted when empty.
    #[test]
    fn clear_restart_deferral_deletes_non_stub_empty_file() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        std::fs::write(
            &target,
            "---\ncondition_ids: [launcher_restart_required]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        ).expect("write");

        clear_restart_deferral(tmp.path()).expect("clear");
        assert!(!target.exists(), "non-stub empty file must be deleted");
    }

    // v0.2.54 Track D (Theme 5): stale-entry self-clear probes.

    #[test]
    fn restart_entry_fresh_when_written_after_boot() {
        // Boot happened 1h BEFORE the entry was written → fresh →
        // must NOT self-clear (this is the normal "install.py just
        // swapped the binary under the running launcher" case).
        let tmp = tempfile::tempdir().expect("tmpdir");
        let f = tmp.path().join("UPDATE_DEFERRED.md");
        std::fs::write(&f, "## launcher_restart_required (info)\n").expect("write");
        let boot = SystemTime::now() - Duration::from_secs(3600);
        assert!(
            !restart_entry_is_stale_at(&f, boot),
            "entry written after boot must be kept",
        );
    }

    #[test]
    fn restart_entry_same_instant_within_margin_is_kept() {
        // mtime == boot (same instant) is inside the 2s safety margin →
        // conservative keep.
        let tmp = tempfile::tempdir().expect("tmpdir");
        let f = tmp.path().join("UPDATE_DEFERRED.md");
        std::fs::write(&f, "## launcher_restart_required (info)\n").expect("write");
        assert!(!restart_entry_is_stale_at(&f, SystemTime::now()));
    }

    #[test]
    fn restart_entry_stale_when_predating_boot() {
        // Entry written, THEN the launcher restarted (boot 1h later) →
        // the running binary is post-swap → stale → self-clear.
        let tmp = tempfile::tempdir().expect("tmpdir");
        let f = tmp.path().join("UPDATE_DEFERRED.md");
        std::fs::write(&f, "## launcher_restart_required (info)\n").expect("write");
        let boot = SystemTime::now() + Duration::from_secs(3600);
        assert!(
            restart_entry_is_stale_at(&f, boot),
            "entry predating boot by 1h must be classified stale",
        );
    }

    #[test]
    fn restart_entry_missing_file_is_not_stale() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let f = tmp.path().join("UPDATE_DEFERRED.md");
        assert!(
            !restart_entry_is_stale_at(&f, SystemTime::now()),
            "unreadable mtime → keep",
        );
    }

    #[test]
    fn write_restart_marker_creates_documented_path() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        write_restart_marker(tmp.path());
        let marker = tmp
            .path()
            .join(".claude")
            .join("context")
            .join("launcher-restart-marker");
        assert!(marker.is_file(), "marker must land at the documented path");
        let body = std::fs::read_to_string(&marker).expect("read marker");
        assert!(body.starts_with("restarted-at: "));
    }

    #[test]
    fn launcher_binary_relative_path_matches_python_helper() {
        // Sanity: the (subdir, fname) tuple must match
        // install.py::_launcher_binary_relative_path or downstream paths
        // diverge silently.
        let (subdir, fname) = launcher_binary_relative_path();
        #[cfg(target_os = "windows")]
        {
            assert_eq!(subdir, "windows-x64");
            assert_eq!(fname, "vct-launcher.exe");
        }
        #[cfg(target_os = "macos")]
        {
            assert_eq!(subdir, "macos-arm64");
            assert_eq!(fname, "vct-launcher");
        }
        #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
        {
            assert_eq!(subdir, "linux-x64");
            assert_eq!(fname, "vct-launcher");
        }
    }
}
