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
        tracing::warn!(
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

    // -- v0.2.91 WP-A/WI-1 surfacing (wave-2 carry-over i) ----------------
    /// True iff a `launcher_binary_stale` entry is present: the binary on
    /// disk is NOT the binary this process is executing.
    ///
    /// Distinct from `restart_required`, which means "an update just swapped
    /// a NEW binary in, click to restart". This one means "the running
    /// process is behind what git says is on disk" — detected AT REST by the
    /// boot / update-check freshness probe, with no update in flight and no
    /// button that can fix it: replacing a running executable is the user's
    /// call (standing no-auto-restart ruling), so the banner is honest and
    /// persistent rather than actionable.
    pub binary_stale: bool,
    /// Version this launcher PROCESS is running, parsed from the entry.
    pub stale_running_version: Option<String>,
    /// Version the dist sidecar ON DISK declares, parsed from the entry.
    pub stale_on_disk_version: Option<String>,
    /// Full "Detected" prose for the stale-binary case (names all three
    /// signals + what was staged). Rendered behind a disclosure.
    pub stale_detail: Option<String>,
}

/// Parse the `launcher_binary_stale` section into the banner's fields.
///
/// Split out as a pure fn so the parse is unit-testable without a temp
/// install root or the async command wrapper. Every field is best-effort:
/// an entry whose prose changed shape still sets `binary_stale = true` and
/// the banner falls back to generic copy.
fn binary_stale_fields(section: &str) -> (Option<String>, Option<String>, Option<String>) {
    let detail = section
        .lines()
        .find(|l| l.starts_with("**Detected**:"))
        .map(|l| l.trim_start_matches("**Detected**:").trim().to_string());

    // Emitted by `binary_freshness::emit_binary_stale_condition` as:
    //   "The launcher process is running v<A>, the dist sidecar on disk
    //    declares v<B>, and `git status ...`"
    // MUST MATCH that format string; a wording change degrades to None
    // (generic banner copy), never to a wrong version.
    let grab = |after: &str| -> Option<String> {
        let tail = detail.as_deref()?.split(after).nth(1)?;
        let v: String = tail
            .chars()
            .take_while(|c| !c.is_whitespace() && *c != ',')
            .collect();
        if v.is_empty() {
            None
        } else {
            Some(v)
        }
    };
    let running = grab("process is running v");
    let on_disk = grab("on disk declares v");
    (running, on_disk, detail)
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
    // v0.2.91 (wave-2 carry-over i): the WI-1 at-rest condition. The cid
    // constant is IMPORTED from its emitter rather than retyped — the two
    // sides of this string cannot drift.
    let stale_section = extract_section(
        &content,
        crate::services::binary_freshness::CID_BINARY_STALE,
    );

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
        tracing::info!(
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
                tracing::warn!(
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
        binary_stale: stale_section.is_some(),
        ..Default::default()
    };

    if let Some(section) = stale_section.as_deref() {
        let (running, on_disk, detail) = binary_stale_fields(section);
        status.stale_running_version = running;
        status.stale_on_disk_version = on_disk;
        status.stale_detail = detail;
    }

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
        tracing::warn!(
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
    // v0.2.83 WP-B6: hold the shared UPDATE_DEFERRED lock around the whole
    // read → strip → rewrite/delete cycle so a concurrent writer (a Python
    // `deferral_emit` writer, or another Rust direct writer) cannot interleave
    // and drop entries / resurrect the banner. Best-effort: on POSIX this is a
    // real flock; on Windows it degrades to no-lock (symmetric with the Python
    // side). Held for the function's whole lifetime via `_deferral_lock`.
    let _deferral_lock =
        vct_launcher_core::services::deferral_lock::lock_folder(install_root);

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
    //
    // MUST MATCH vco_lib/deferral_report.py `_RUST_STRIPPABLE_CONDITION_IDS`
    // (P2a v0.2.75): the Python read() honours a Markdown-only strip (drops
    // the JSON-sidecar entry) ONLY for the condition IDs listed there. If
    // you add another strip_section(...) call for a different condition_id
    // here (or anywhere else that edits the .md without the .json), add
    // that ID to the Python frozenset too — otherwise the strip resurrects
    // from the JSON sidecar on the next Python read.
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
            tracing::warn!(
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
        // v0.2.73 Stage-1 DESIGN F2 (resurrection): A-3 made the JSON sidecar
        // AUTHORITATIVE — `deferral_report.read()` prefers UPDATE_DEFERRED.json
        // and falls back to the Markdown only when the JSON is absent. If we
        // unlink ONLY the .md here, the .json survives with the just-cleared
        // launcher_restart_required entry, so the next Python read() RESURRECTS
        // the restart banner (the common single-entry case, which is exactly
        // this branch). Dual-unlink the JSON sidecar so the clear actually
        // clears. Best-effort: a missing sidecar is fine (older installs
        // predate A-3); a remove error is logged, not fatal (the .md is already
        // gone, so the banner won't re-render from Markdown at least).
        let json_sidecar = target.with_file_name("UPDATE_DEFERRED.json");
        if json_sidecar.is_file() {
            if let Err(e) = std::fs::remove_file(&json_sidecar) {
                tracing::warn!(
                    "[restart] could not unlink JSON deferral sidecar at {} \
                     (banner may resurrect from JSON): {}",
                    json_sidecar.display(),
                    e,
                );
            }
        }
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
    Ok(dist_launcher_path(install_root))
}

/// `<install_root>/launcher/dist/<os-arch>/vct-launcher[.exe]` — the binary
/// `install.py` refreshes and the release commits.
///
/// v0.2.100 (WP-03a): the `(subdir, filename)` pair is no longer a private
/// mirror here — it comes from `installer::version_info`
/// (`launcher_dist_subdir` / `launcher_binary_filename`), the same pair the
/// sidecar reader uses, so the binary relaunched and the version checked
/// cannot name different files.
pub(crate) fn dist_launcher_path(install_root: &Path) -> PathBuf {
    install_root
        .join("launcher")
        .join("dist")
        .join(crate::commands::installer::launcher_dist_subdir())
        .join(crate::commands::installer::launcher_binary_filename())
}

// ---------------------------------------------------------------------------
// v0.2.100 (WP-03a, AD-1 phase 13, L2-F01/F12) — the pipeline's relaunch
// ---------------------------------------------------------------------------

/// What the pipeline relaunches into. One target today; the enum is the
/// contract that a relaunch always names what it starts (never
/// `current_exe()`, which re-executes the OLD binary whenever the running exe
/// is not the dist file — L2-F12).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RelaunchTarget {
    /// `launcher/dist/<os-arch>/vct-launcher[.exe]` under the install root.
    Dist,
}

/// Why the relaunch was refused. Every arm is a typed refusal, never a
/// silent relaunch of whatever happens to be on disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum RelaunchRefusal {
    /// The dist sidecar (`vct-launcher*.metadata.json`) carries no version.
    DistVersionUnknown { binary: PathBuf },
    /// A version on either side is not strict X.Y.Z.
    VersionUnreadable { detail: String },
    /// The dist binary is not strictly newer than the running one —
    /// relaunching would load the same or an OLDER launcher (L2-F01).
    NotNewer { running: String, dist: String },
    /// The dist binary itself is missing.
    BinaryMissing { binary: PathBuf },
}

impl std::fmt::Display for RelaunchRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RelaunchRefusal::DistVersionUnknown { binary } => write!(
                f,
                "the launcher binary at {} has no readable version sidecar, so it cannot be \
                 proven newer than the running launcher — not relaunching",
                binary.display()
            ),
            RelaunchRefusal::VersionUnreadable { detail } => {
                write!(f, "cannot order launcher versions ({}) — not relaunching", detail)
            }
            RelaunchRefusal::NotNewer { running, dist } => write!(
                f,
                "the launcher binary on disk (v{}) is not newer than the running launcher \
                 (v{}) — not relaunching",
                dist, running
            ),
            RelaunchRefusal::BinaryMissing { binary } => {
                write!(f, "no launcher binary at {} — not relaunching", binary.display())
            }
        }
    }
}

/// A relaunch that did not happen, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum RelaunchError {
    Refused(RelaunchRefusal),
    SpawnFailed(String),
}

impl std::fmt::Display for RelaunchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RelaunchError::Refused(r) => r.fmt(f),
            RelaunchError::SpawnFailed(e) => f.write_str(e),
        }
    }
}

/// What [`relaunch`] did. The caller returns right after either — the process
/// is exiting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum RelaunchOutcome {
    /// Windows stage-1 handoff: `vct-updater` owns the swap and the relaunch.
    HandoffExit,
    /// The dist binary was spawned detached; this process is exiting.
    Spawned { exe: PathBuf },
}

/// Starts a launcher binary. Injected so the guard's act/leave-alone arms are
/// tested without ever starting a real launcher.
pub(crate) trait Spawner {
    fn spawn_detached(&self, exe: &Path) -> Result<(), String>;
}

/// The production spawner: fully detached (`setsid` / `DETACHED_PROCESS`),
/// null stdio — [`spawn_detached_launcher`].
pub(crate) struct DetachedSpawner;

impl Spawner for DetachedSpawner {
    fn spawn_detached(&self, exe: &Path) -> Result<(), String> {
        spawn_detached_launcher(exe)
    }
}

/// The version guard, pure: may the pipeline relaunch into a dist binary at
/// `dist_version`, given the `running` version? Only when the dist is
/// STRICTLY newer (`version::is_older(running, dist)`); an absent or
/// unparseable version is a refusal, never "fresh" (AD-8 tri-state rule).
pub(crate) fn decide_relaunch(
    running: &str,
    dist_version: Option<&str>,
    binary: &Path,
) -> Result<(), RelaunchRefusal> {
    let Some(dist) = dist_version.filter(|d| !d.trim().is_empty()) else {
        return Err(RelaunchRefusal::DistVersionUnknown {
            binary: binary.to_path_buf(),
        });
    };
    match vct_launcher_core::version::is_older(running, dist) {
        Err(e) => Err(RelaunchRefusal::VersionUnreadable {
            detail: e.to_string(),
        }),
        Ok(false) => Err(RelaunchRefusal::NotNewer {
            running: running.to_string(),
            dist: dist.to_string(),
        }),
        Ok(true) if !binary.is_file() => Err(RelaunchRefusal::BinaryMissing {
            binary: binary.to_path_buf(),
        }),
        Ok(true) => Ok(()),
    }
}

/// Guard, then spawn: the testable ACT of a relaunch. Returns the binary it
/// started. Nothing is spawned on any refusal.
pub(crate) fn spawn_guarded(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
) -> Result<PathBuf, RelaunchError> {
    let exe = dist_launcher_path(install_root);
    let dist_version = crate::commands::installer::read_on_disk_binary_version(install_root);
    decide_relaunch(running, dist_version.as_deref(), &exe).map_err(RelaunchError::Refused)?;
    spawner
        .spawn_detached(&exe)
        .map_err(RelaunchError::SpawnFailed)?;
    Ok(exe)
}

/// The ONE relaunch of the update pipeline (`update_run` phase 13).
///
/// 1. Version guard ([`decide_relaunch`]) — a refusal returns before anything
///    else happens, so the caller still owns the hub restart.
/// 2. Windows stage-1 branch: `binary_freshness::stage_and_handoff_after_update`
///    stages binaries git could not write and, when it fires, `vct-updater`
///    owns the swap and the relaunch — this process exits. No-op on POSIX.
/// 3. Otherwise `before_spawn` runs (the caller's hub restart on Windows,
///    where it must follow the handoff decision — v0.2.54 C-1), the dist
///    binary is spawned DETACHED, the `launcher_restart_required` entry is
///    cleared, and this process exits.
pub(crate) async fn relaunch<R: Runtime>(
    app: &AppHandle<R>,
    target: RelaunchTarget,
    install_root: &Path,
    running: &str,
    before_spawn: impl FnOnce(),
) -> Result<RelaunchOutcome, RelaunchError> {
    let RelaunchTarget::Dist = target;
    let exe = dist_launcher_path(install_root);
    let dist_version = crate::commands::installer::read_on_disk_binary_version(install_root);
    decide_relaunch(running, dist_version.as_deref(), &exe).map_err(RelaunchError::Refused)?;

    let handoff = crate::services::binary_freshness::stage_and_handoff_after_update(
        install_root,
        &install_root.display().to_string(),
    )
    .await;
    if handoff.handoff_active {
        tracing::info!(
            "[restart] relaunch: stage-1 handoff active (lock={:?}); exiting so vct-updater \
             swaps the locked binaries and relaunches",
            handoff.lock_path
        );
        crate::quit_dialog::force_quit();
        app.exit(0);
        return Ok(RelaunchOutcome::HandoffExit);
    }
    if let Some(reason) = handoff.skip_reason.as_deref() {
        tracing::debug!("[restart] relaunch: stage-1 handoff skipped ({})", reason);
    }

    before_spawn();
    let exe = spawn_guarded(install_root, running, &DetachedSpawner)?;
    if let Err(e) = clear_restart_deferral(install_root) {
        tracing::warn!("[restart] relaunch: could not clear the restart deferral: {}", e);
    }
    crate::quit_dialog::force_quit();
    app.exit(0);
    Ok(RelaunchOutcome::Spawned { exe })
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

    // -----------------------------------------------------------------
    // v0.2.91 wave-2 carry-over (i): `launcher_binary_stale` banner slice.
    //
    // Pre-fix `LauncherRestartStatus` had no `binary_stale` field and the
    // command never looked for the section, so the WI-1 condition was
    // durable-on-disk and invisible in the GUI.
    // -----------------------------------------------------------------

    /// Realistic entry, byte-shaped like `emit_binary_stale_condition`'s
    /// output rendered by the deferral writer.
    ///
    /// wave-2 NIT-9: the `**Disposition**:` line is part of the real wire —
    /// `deferral_report._render_entry` writes it between `**Title**` and
    /// `**Detected**` for EVERY entry. A fixture missing it would let a parser
    /// that (wrongly) assumed Detected follows Title directly pass here and
    /// fail on the real file.
    fn stale_doc() -> String {
        format!(
            "---\ncondition_ids: [{cid}]\n---\n\n# VCO Update Deferred\n\n\
             ## {cid} (warning)\n\n\
             **Title**: Launcher is running an older binary than the one on disk\n\n\
             **Disposition**: action_required\n\n\
             **Detected**: The launcher process is running v0.2.88, the dist sidecar on disk \
             declares v0.2.91, and `git status --porcelain -- launcher/dist/windows-x64/` \
             reports the dist tree as DIRTY (diverged from HEAD). That means the binary git \
             says should be on disk is NOT the binary that is executing. Staged this pass: \
             launcher/dist/windows-x64/vct-launcher.exe.\n\n\
             **Detected at**: 2026-08-26T10:00:00Z\n\n---\n",
            cid = crate::services::binary_freshness::CID_BINARY_STALE,
        )
    }

    /// The cid must be SHARED with its emitter, not retyped. A literal here
    /// looks identical today and silently stops matching the day the emitter
    /// renames the condition — the banner would then just never appear again,
    /// with no failing test and no error anywhere.
    #[test]
    fn the_stale_cid_is_imported_not_retyped() {
        let src = include_str!("restart.rs");
        let code = src.split("mod tests").next().unwrap();
        assert!(
            code.contains(concat!("binary_freshness::CID_BINARY", "_STALE")),
            "restart.rs must read the cid from its emitter",
        );
        assert!(
            !code.contains(concat!("\"launcher_binary", "_stale\"")),
            "a hardcoded cid literal can drift away from the emitter unnoticed",
        );
    }

    #[test]
    fn binary_stale_section_is_extracted_by_the_emitters_own_cid() {
        let doc = stale_doc();
        let section = extract_section(&doc, crate::services::binary_freshness::CID_BINARY_STALE)
            .expect("the stale section must be found by the shared cid constant");
        assert!(section.contains("**Detected**:"));
    }

    #[test]
    fn binary_stale_fields_parse_both_versions_and_the_prose() {
        let doc = stale_doc();
        let section =
            extract_section(&doc, crate::services::binary_freshness::CID_BINARY_STALE).unwrap();
        let (running, on_disk, detail) = binary_stale_fields(&section);
        assert_eq!(running.as_deref(), Some("0.2.88"));
        assert_eq!(on_disk.as_deref(), Some("0.2.91"));
        assert!(
            detail.unwrap().contains("NOT the binary that is executing"),
            "the full prose must survive for the disclosure",
        );
    }

    /// Leave-alone leg: a reworded entry must NOT produce a wrong version —
    /// it degrades to None (generic banner copy) while still flagging stale.
    #[test]
    fn binary_stale_fields_degrade_to_none_on_unknown_prose() {
        let section = "## launcher_binary_stale (warning)\n\n**Detected**: something else \
                       entirely.\n\n---\n";
        let (running, on_disk, detail) = binary_stale_fields(section);
        assert_eq!(running, None);
        assert_eq!(on_disk, None);
        assert_eq!(detail.as_deref(), Some("something else entirely."));
    }

    #[test]
    fn binary_stale_absent_when_no_such_entry() {
        let body = "---\ncondition_ids: [other_thing]\n---\n\n## other_thing (warning)\n\n---\n";
        assert!(
            extract_section(body, crate::services::binary_freshness::CID_BINARY_STALE).is_none(),
        );
    }

    /// The three banner states are independent: a stale-binary entry must not
    /// make the FE think a one-click restart is available.
    #[test]
    fn stale_entry_does_not_imply_restart_required() {
        let doc = stale_doc();
        assert!(extract_section(&doc, "launcher_restart_required").is_none());
        assert!(extract_section(&doc, "launcher_binary_swap_failed_locked").is_none());
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
    fn clear_restart_deferral_also_unlinks_json_sidecar() {
        // v0.2.73 Stage-1 DESIGN F2: A-3 made UPDATE_DEFERRED.json authoritative
        // (Python read() prefers it). Clearing the last MD entry must ALSO unlink
        // the JSON sidecar — else the cleared restart banner resurrects from JSON.
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let md = dot_claude.join("UPDATE_DEFERRED.md");
        let json = dot_claude.join("UPDATE_DEFERRED.json");
        std::fs::write(
            &md,
            "---\ncondition_ids: [launcher_restart_required]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        )
        .expect("write md");
        std::fs::write(
            &json,
            "{\"schema_version\":1,\"entries\":[{\"condition_id\":\"launcher_restart_required\",\"title\":\"foo\"}]}",
        )
        .expect("write json");

        clear_restart_deferral(tmp.path()).expect("clear");
        assert!(!md.exists(), "MD removed when no entries remain");
        assert!(
            !json.exists(),
            "JSON sidecar must ALSO be removed — else the banner resurrects from JSON",
        );
    }

    #[test]
    fn clear_restart_deferral_json_absent_is_ok() {
        // Older installs predate A-3 (no JSON sidecar) — clearing must still
        // succeed and unlink the MD without erroring on the absent JSON.
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let md = dot_claude.join("UPDATE_DEFERRED.md");
        std::fs::write(
            &md,
            "---\ncondition_ids: [launcher_restart_required]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        )
        .expect("write md");
        clear_restart_deferral(tmp.path()).expect("clear (no json is fine)");
        assert!(!md.exists());
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

    // ---- v0.2.100 WP-03a: the pipeline relaunch's version guard -----------

    /// Records every spawn instead of starting a process — no test may ever
    /// start a real launcher.
    #[derive(Default)]
    struct RecordingSpawner {
        spawned: std::cell::RefCell<Vec<PathBuf>>,
    }

    impl Spawner for RecordingSpawner {
        fn spawn_detached(&self, exe: &Path) -> Result<(), String> {
            self.spawned.borrow_mut().push(exe.to_path_buf());
            Ok(())
        }
    }

    /// A temp install root whose dist slot holds a binary + sidecar at `dist`.
    fn root_with_dist(dist: Option<&str>) -> tempfile::TempDir {
        let td = tempfile::tempdir().unwrap();
        let exe = dist_launcher_path(td.path());
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, b"not a real launcher").unwrap();
        if let Some(v) = dist {
            let meta = exe.with_file_name(format!(
                "{}.metadata.json",
                exe.file_name().unwrap().to_string_lossy()
            ));
            std::fs::write(&meta, format!("{{\"launcher_version\": \"{}\"}}", v)).unwrap();
        }
        td
    }

    /// ACT: a dist binary strictly newer than the running one is spawned —
    /// exactly once, and it is the DIST path (never `current_exe()`).
    #[test]
    fn relaunch_proceeds_for_a_newer_dist_binary() {
        let td = root_with_dist(Some("0.2.100"));
        let spawner = RecordingSpawner::default();
        let exe = spawn_guarded(td.path(), "0.2.99", &spawner).expect("newer dist relaunches");
        assert_eq!(exe, dist_launcher_path(td.path()));
        assert_eq!(*spawner.spawned.borrow(), vec![dist_launcher_path(td.path())]);
    }

    /// LEAVE-ALONE: an equal or OLDER dist binary is a typed refusal and
    /// nothing is spawned (L2-F01: the old resolver installed the older one).
    #[test]
    fn relaunch_refuses_a_not_newer_dist_binary() {
        for dist in ["0.2.99", "0.2.98", "0.2.9"] {
            let td = root_with_dist(Some(dist));
            let spawner = RecordingSpawner::default();
            let err = spawn_guarded(td.path(), "0.2.99", &spawner).expect_err(dist);
            assert_eq!(
                err,
                RelaunchError::Refused(RelaunchRefusal::NotNewer {
                    running: "0.2.99".into(),
                    dist: dist.into(),
                })
            );
            assert!(spawner.spawned.borrow().is_empty(), "{dist}: nothing may be spawned");
        }
    }

    /// Tri-state: an absent sidecar or a non-X.Y.Z version is never "newer".
    #[test]
    fn relaunch_refuses_unknown_or_unreadable_versions() {
        let spawner = RecordingSpawner::default();
        let td = root_with_dist(None);
        assert!(matches!(
            spawn_guarded(td.path(), "0.2.99", &spawner),
            Err(RelaunchError::Refused(RelaunchRefusal::DistVersionUnknown { .. }))
        ));
        let td = root_with_dist(Some("0.2.100-rc1"));
        assert!(matches!(
            spawn_guarded(td.path(), "0.2.99", &spawner),
            Err(RelaunchError::Refused(RelaunchRefusal::VersionUnreadable { .. }))
        ));
        assert!(spawner.spawned.borrow().is_empty());
        // A missing binary with a newer sidecar is refused too.
        let td = root_with_dist(Some("0.2.100"));
        std::fs::remove_file(dist_launcher_path(td.path())).unwrap();
        assert!(matches!(
            decide_relaunch("0.2.99", Some("0.2.100"), &dist_launcher_path(td.path())),
            Err(RelaunchRefusal::BinaryMissing { .. })
        ));
    }

    #[test]
    fn launcher_binary_relative_path_matches_python_helper() {
        // Sanity: the (subdir, fname) tuple must match
        // install.py::_launcher_binary_relative_path or downstream paths
        // diverge silently.
        // v0.2.100: the pair now has ONE home (`installer::version_info`);
        // `dist_launcher_path` is built from it, asserted here end to end.
        let subdir = crate::commands::installer::launcher_dist_subdir();
        let fname = crate::commands::installer::launcher_binary_filename();
        assert_eq!(
            dist_launcher_path(Path::new("root")),
            Path::new("root").join("launcher").join("dist").join(subdir).join(fname)
        );
        #[cfg(target_os = "windows")]
        {
            assert_eq!(subdir, "windows-x64");
            assert_eq!(fname, "vct-launcher.exe");
        }
        // v0.2.54 Track C: arch-aware on macOS (Intel-Mac fix).
        #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
        {
            assert_eq!(subdir, "macos-x64");
            assert_eq!(fname, "vct-launcher");
        }
        #[cfg(all(target_os = "macos", not(target_arch = "x86_64")))]
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

    /// WP-B6 (v0.2.83): `clear_restart_deferral` must hold the shared
    /// UPDATE_DEFERRED flock across its read → strip → rewrite/delete cycle.
    /// Observable proof of serialization: hold the SAME folder's flock on a
    /// background thread for a fixed window, then call `clear_restart_deferral`
    /// on the main thread — it must BLOCK on the flock until the window ends,
    /// so the call's wall-clock duration is >= the hold window. POSIX-only
    /// (the flock is a no-op on Windows, matching the Python side). ms-scaled.
    #[cfg(unix)]
    #[test]
    fn clear_restart_deferral_serializes_on_shared_lock() {
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        std::fs::write(
            &target,
            "---\ncondition_ids: [launcher_restart_required]\n---\n\n# VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        )
        .expect("write");

        const HOLD: Duration = Duration::from_millis(300);
        let folder = tmp.path().to_path_buf();
        let (ready_tx, ready_rx) = mpsc::channel::<()>();

        // Background thread grabs the shared lock, signals ready, holds it, releases.
        let holder = std::thread::spawn(move || {
            let guard =
                vct_launcher_core::services::deferral_lock::lock_folder(&folder);
            ready_tx.send(()).expect("signal ready");
            std::thread::sleep(HOLD);
            drop(guard); // release → the main thread's blocked flock returns.
        });

        // Wait until the holder actually owns the lock before we race for it.
        ready_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("holder must acquire the lock");

        let started = Instant::now();
        clear_restart_deferral(tmp.path()).expect("clear");
        let waited = started.elapsed();

        // The clear could only proceed AFTER the holder released, so it must
        // have blocked for ~the remaining hold window. Allow scheduling slack.
        assert!(
            waited >= HOLD - Duration::from_millis(80),
            "clear_restart_deferral did not block on the shared lock \
             (waited {waited:?}, expected >= ~{HOLD:?}) — the flock is not held \
             across the read-modify-write cycle",
        );
        assert!(!target.exists(), "clear should have unlinked the solo entry");
        holder.join().expect("holder joins");
    }
}
