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
use std::time::{Duration, Instant, SystemTime};

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Runtime};
use vct_launcher_core::process::CommandExt as _;

use std::future::Future;

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
/// binary. Invoked by the "Restart now" banner (`updater.runRestart`) for
/// `launcher_restart_required` deferral entries and the `binary_stale` badge.
///
/// `install_root` is the path of the orchestrator clone whose update just
/// landed. v0.2.100 (L2-F01/F12): the restart goes through the relaunch
/// VERSION GUARD ([`restart_to_dist`]) — only a dist binary strictly newer
/// than the running launcher is started, and never `current_exe()` (which
/// re-executed the OLD binary whenever the running exe was not the dist
/// file). A refusal is returned to the GUI as a worded error; the launcher
/// keeps running.
#[command]
pub async fn restart_launcher<R: Runtime>(
    app: AppHandle<R>,
    install_root: String,
) -> Result<(), String> {
    restart_to_dist(
        Path::new(&install_root),
        env!("CARGO_PKG_VERSION"),
        &DetachedSpawner,
    )
    .map_err(|e| e.to_string())?;
    // Bypass the Quit-confirmation dialog: the user already clicked Restart.
    crate::quit_dialog::force_quit();
    app.exit(0);
    Ok(())
}

/// [`restart_launcher`]'s decision + act, testable with a fake spawner:
/// [`spawn_guarded`] (version guard, then the dist binary), then the
/// `launcher_restart_required` entry is cleared. The entry is also cleared
/// when the dist binary is NOT newer — the banner's promise ("a newer
/// launcher is waiting") is then false; any other refusal leaves it.
pub(crate) fn restart_to_dist(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
) -> Result<PathBuf, RelaunchError> {
    restart_to_dist_args(install_root, running, spawner, &[])
}

/// The argv-carrying core of [`restart_to_dist`] (v0.2.102, S16-13): the
/// modal's Continue path passes no extra argv; the hidden idle self-relaunch
/// passes `--start-hidden` so a tray-only launcher never flashes a window
/// across the relaunch. EVERY relaunch child additionally carries
/// [`RELAUNCH_PARENT_FLAG`] with the exiting process's pid — appended HERE,
/// the one path both flavours share — so the child waits out the parent's
/// single-instance lock (see `wait_for_relaunch_parent_before_lock`) instead
/// of losing the spawn-then-exit race and leaving no launcher running.
/// Same guard, same deferral clear — one home.
fn restart_to_dist_args(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
    extra_args: &[String],
) -> Result<PathBuf, RelaunchError> {
    let mut args: Vec<String> = extra_args.to_vec();
    args.push(relaunch_parent_arg());
    let result = spawn_guarded(install_root, running, spawner, &args);
    if matches!(
        result,
        Ok(_) | Err(RelaunchError::Refused(RelaunchRefusal::NotNewer { .. }))
    ) {
        if let Err(e) = clear_restart_deferral(install_root) {
            tracing::warn!("[restart_launcher] failed to clear deferral (non-fatal): {}", e);
        }
    }
    result
}

/// v0.2.102 (S16-13 / OD-7): the hidden idle launcher's flavour of
/// [`restart_to_dist`] — same version guard and deferral clear, plus
/// [`crate::HIDDEN_START_FLAG`] so the relaunched process stays tray-only.
fn restart_to_dist_hidden(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
) -> Result<PathBuf, RelaunchError> {
    restart_to_dist_args(
        install_root,
        running,
        spawner,
        &[crate::HIDDEN_START_FLAG.to_string()],
    )
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
        // v0.2.100 (owner rule F-W2-08(c)): a ledger with no entry is never
        // kept — the v0.2.43 `stub: true` preserve (V0243-8) is retired with
        // the stub writer it protected (install.py wrote a zero-entry stub
        // after every clean update; it no longer does).
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
pub(crate) fn strip_section(content: &str, condition_id: &str) -> String {
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
///
/// v0.2.102 (S16-13): takes the child's extra argv so the ONE guarded-spawn
/// home serves both relaunch flavours — the modal's Continue (no extra args,
/// the user is looking at the window) and the hidden idle self-relaunch
/// (`--start-hidden`, so a tray-only launcher stays tray-only across it).
/// Both flavours additionally carry [`RELAUNCH_PARENT_FLAG`] (appended by
/// [`restart_to_dist_args`], the one path) so the child waits out the
/// exiting parent's single-instance lock instead of losing to it.
pub(crate) trait Spawner {
    fn spawn_detached(&self, exe: &Path, args: &[String]) -> Result<(), String>;
}

/// The production spawner: fully detached (`setsid` / `DETACHED_PROCESS`),
/// null stdio — [`spawn_detached_launcher`].
pub(crate) struct DetachedSpawner;

impl Spawner for DetachedSpawner {
    fn spawn_detached(&self, exe: &Path, args: &[String]) -> Result<(), String> {
        spawn_detached_launcher(exe, args)
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
/// started. Nothing is spawned on any refusal. `args` are the child's extra
/// argv (the relaunch-parent flag for the modal path; plus `--start-hidden`
/// for the hidden idle self-relaunch — see [`Spawner`]).
pub(crate) fn spawn_guarded(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
    args: &[String],
) -> Result<PathBuf, RelaunchError> {
    let exe = dist_launcher_path(install_root);
    let dist_version = crate::commands::installer::read_on_disk_binary_version(install_root);
    decide_relaunch(running, dist_version.as_deref(), &exe).map_err(RelaunchError::Refused)?;
    spawner
        .spawn_detached(&exe, args)
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
    let exe = spawn_guarded(install_root, running, &DetachedSpawner, &[relaunch_parent_arg()])?;
    if let Err(e) = clear_restart_deferral(install_root) {
        tracing::warn!("[restart] relaunch: could not clear the restart deferral: {}", e);
    }
    crate::quit_dialog::force_quit();
    app.exit(0);
    Ok(RelaunchOutcome::Spawned { exe })
}

// ---------------------------------------------------------------------------
// v0.2.102 (S16-13 / OD-7): hidden idle launcher self-relaunch on binary_stale
// ---------------------------------------------------------------------------
//
// The session-start hook spawns the launcher with `--start-hidden` (see
// `templates/hooks/session-start-ensure-hub.sh`); that process lives until
// reboot and, after a terminal `install.py --update`, keeps running the OLD
// binary. The restart modal's `binary_stale` banner is seen only when a
// window opens, so a tray-only launcher sat on a stale binary indefinitely
// (register §16 / OD-7).
//
// OD-7(a): a launcher STARTED `--start-hidden` relaunches ITSELF into the
// dist binary when `launcher_binary_stale` is recorded, no window is
// visible, no update pipeline/apply is running (the update-gate lockfile —
// the one apply-in-flight source of truth) and no modal is pending. A
// visible-mode launcher the user merely hid to the tray keeps the modal
// path (owner OD-7 scope). The relaunch is the SAME guarded path the
// modal's Continue performs ([`restart_to_dist_hidden`] →
// [`spawn_guarded`] → the version guard), plus `--start-hidden` so the
// relaunched process keeps the tray-only start. The session-start hook does
// NOT kill GUI processes; this tick is the hidden launcher acting on itself.
//
// The `launcher_binary_stale` condition itself is NOT stripped here — it
// clears by its existing probe: the relaunched process's boot
// `reconcile_dist_at_rest` sees a `Fresh` verdict and retires the record
// (`binary_freshness::clear_stale_condition_if_fresh`).

/// Default cadence of the hidden-idle stale check. Generous by design: the
/// condition it heals (a tray-only launcher running yesterday's binary) is
/// wrong for hours, not seconds, and each tick reads a few small files.
pub(crate) const DEFAULT_STALE_IDLE_CHECK_INTERVAL: Duration =
    Duration::from_secs(10 * 60);

/// Env knob for [`DEFAULT_STALE_IDLE_CHECK_INTERVAL`], in seconds.
pub(crate) const ENV_STALE_IDLE_CHECK_SECS: &str = "VCT_LAUNCHER_STALE_IDLE_CHECK_SECS";

/// The tick interval: `VCT_LAUNCHER_STALE_IDLE_CHECK_SECS` seconds when set
/// to a positive integer, the generous default otherwise (absent, malformed
/// or non-positive values never shorten the cadence to something hostile).
pub(crate) fn stale_idle_check_interval() -> Duration {
    stale_idle_check_interval_from(std::env::var(ENV_STALE_IDLE_CHECK_SECS).ok().as_deref())
}

/// Pure core of [`stale_idle_check_interval`] (env reads are process-global,
/// so the parsing decision is tested without touching the environment).
fn stale_idle_check_interval_from(raw: Option<&str>) -> Duration {
    raw.and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|secs| *secs > 0)
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_STALE_IDLE_CHECK_INTERVAL)
}

/// The guards of the hidden-idle self-relaunch, already resolved —
/// injectable so every act/leave-alone arm is testable without a running
/// launcher, DB or file system.
pub(crate) struct IdleStaleGuards {
    /// A `launcher_binary_stale` record is on disk for this install.
    pub binary_stale: bool,
    /// Any launcher window is visible (the restart modal's Continue belongs
    /// to the user then, not to a background tick).
    pub window_visible: bool,
    /// The update-gate lockfile is present AND within its completion
    /// deadline — a pipeline/apply owns the tree. (The reader is
    /// deadline-based: a lockfile past its `expected_completion_by`, or
    /// unparseable/undated, reads as NOT in flight.)
    pub update_in_flight: bool,
    /// A modal-blocking file is present on disk at tick time
    /// (`update.lock.json` / `update.result.json` under the vct state dir —
    /// written mid-life by an updater handoff). Both are consumed (deleted)
    /// at every boot by `poll_update_lock_on_boot`, so a long-lived hidden
    /// launcher sees them only while an updater genuinely left them behind.
    pub modal_pending: bool,
    /// THIS process was started with `--start-hidden` (OD-7's scope). A
    /// visible-mode launcher the user hid to the tray keeps the modal path.
    pub started_hidden: bool,
}

/// What the tick decided. `LeaveAlone` carries the reason for the debug log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IdleStaleDecision {
    /// All guards pass — perform the modal's own relaunch, hidden.
    Relaunch,
    /// Leave the running launcher alone, and why.
    LeaveAlone(&'static str),
}

/// PURE decision core of the hidden-idle self-relaunch. Never relaunch with a
/// window open or an update in flight — the leave-alone arms are the point.
pub(crate) fn decide_hidden_idle_relaunch(g: &IdleStaleGuards) -> IdleStaleDecision {
    if !g.binary_stale {
        IdleStaleDecision::LeaveAlone("no launcher_binary_stale record")
    } else if !g.started_hidden {
        IdleStaleDecision::LeaveAlone("not a --start-hidden launcher; the modal stays the path")
    } else if g.window_visible {
        IdleStaleDecision::LeaveAlone("a window is visible")
    } else if g.update_in_flight {
        IdleStaleDecision::LeaveAlone("an update is in flight")
    } else if g.modal_pending {
        IdleStaleDecision::LeaveAlone("a modal is pending")
    } else {
        IdleStaleDecision::Relaunch
    }
}

/// Is a `launcher_binary_stale` section recorded in this install's
/// UPDATE_DEFERRED.md? Uses the SAME cid constant and section extractor as
/// `get_launcher_restart_status` (the modal's banner), so the tick and the
/// banner can never disagree about what "recorded" means. A missing or
/// unreadable file is simply not recorded (leave-alone).
fn binary_stale_recorded(install_root: &Path) -> bool {
    let target = install_root
        .join(".claude")
        .join("context")
        .join("UPDATE_DEFERRED.md");
    std::fs::read_to_string(&target)
        .map(|content| {
            extract_section(&content, crate::services::binary_freshness::CID_BINARY_STALE)
                .is_some()
        })
        .unwrap_or(false)
}

/// The modal-pending reader: an update-handoff file present on disk at tick
/// time. `update.lock.json` is PRESERVED while an updater may still be
/// running; `update.result.json` is written right before the updater's
/// relaunch. Both are consumed (deleted) at every boot by
/// `update_handoff::poll_update_lock_on_boot`, so mid-life presence means an
/// updater left them behind moments ago — leave-alone.
fn update_handoff_modal_pending() -> bool {
    let root = vct_launcher_core::paths::vct_root_dir();
    root.join(crate::commands::update_handoff::UPDATE_LOCK_FILE).is_file()
        || root.join(crate::commands::update_handoff::UPDATE_RESULT_FILE).is_file()
}

/// PURE visibility decision over a set of per-window visibility answers:
/// `Some(true)` visible, `Some(false)` hidden, `None` UNKNOWN (the
/// `is_visible()` call failed). Unknown counts as VISIBLE — the
/// conservative direction: a false leave-alone is recoverable (the next
/// tick retries), relaunching under a window we could not inspect is not.
pub(crate) fn visible_window_among(
    visibilities: impl IntoIterator<Item = Option<bool>>,
) -> bool {
    visibilities.into_iter().any(|v| v.unwrap_or(true))
}

/// Enumerate every window the app can see and answer "is any visible"
/// through [`visible_window_among`].
///
/// RESIDUAL (precise, review fix 1): `webview_windows()` lists windows
/// REGISTERED with the manager at build time. Two things are therefore not
/// covered: (a) a window whose builder has been constructed but whose
/// `build()` has not yet completed — Tauri registers windows at build time,
/// so a window in that sub-second construction span is invisible to this
/// probe; (b) plain non-webview windows — enumerating those needs Tauri's
/// `unstable` feature (`Manager::windows`), which this crate does not
/// enable, and the launcher creates no non-webview windows today (native
/// dialogs are plugin-owned, not tauri windows). The pre-exit re-check
/// ([`perform_hidden_idle_relaunch`]) narrows the construction span to the
/// moment between spawn and exit but cannot eliminate it.
fn any_window_visible<R: Runtime>(app: &AppHandle<R>) -> bool {
    use tauri::Manager as _;
    let windows = app.webview_windows();
    let visibilities = windows.values().map(|w| w.is_visible().ok());
    visible_window_among(visibilities)
}

/// Assemble the guards against live state. The window probe and the owning
/// argv are INJECTED (they are the only app/process-dependent inputs), so
/// the assembly is testable without a Tauri app: a test whose probe says
/// "visible" must see `window_visible: true` in the assembled guards, and
/// one whose argv lacks `--start-hidden` must see `started_hidden: false`.
pub(crate) fn guards_from_probe(
    install_root: &Path,
    window_probe: impl FnOnce() -> bool,
    own_argv: &[String],
) -> IdleStaleGuards {
    IdleStaleGuards {
        binary_stale: binary_stale_recorded(install_root),
        window_visible: window_probe(),
        // ONE apply-in-flight source of truth: the same deadline-based
        // lockfile reader the MCP servers honour. No second state machine.
        update_in_flight: crate::commands::update_gate::is_update_in_progress(),
        modal_pending: update_handoff_modal_pending(),
        started_hidden: crate::start_hidden_requested(own_argv),
    }
}

/// What the act core did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum HiddenIdleRelaunchOutcome {
    /// Spawned and the process was told to exit (the injected `exit` ran).
    Exited,
    /// Spawned, but a guard FLIPPED on the pre-exit re-check — the old
    /// process stays running. The already-spawned child carries
    /// [`RELAUNCH_PARENT_FLAG`], so it waits out its bounded lock window,
    /// sees this parent live, and gives up (logged there) — the
    /// single-instance hand-off then exits it cleanly. Nothing is lost.
    Aborted(&'static str),
    /// The version guard refused; NOTHING was spawned. The next tick
    /// retries.
    Refused(RelaunchError),
}

/// The testable ACT of the hidden-idle tick: guarded relaunch, then —
/// because the spawn takes time and the decision was taken BEFORE it —
/// RE-VALIDATE the guards immediately before the process exit and abort if
/// any flipped (a window opened, an update started, a modal landed, ...).
/// `guards` is called exactly twice: once would let a flipped guard kill a
/// freshly opened window; re-checking is the review's MEDIUM-1 fix.
pub(crate) fn perform_hidden_idle_relaunch(
    install_root: &Path,
    running: &str,
    spawner: &dyn Spawner,
    guards: impl Fn() -> IdleStaleGuards,
    exit: impl FnOnce(),
) -> HiddenIdleRelaunchOutcome {
    if let Err(e) = restart_to_dist_hidden(install_root, running, spawner) {
        return HiddenIdleRelaunchOutcome::Refused(e);
    }
    match decide_hidden_idle_relaunch(&guards()) {
        IdleStaleDecision::Relaunch => {
            exit();
            HiddenIdleRelaunchOutcome::Exited
        }
        IdleStaleDecision::LeaveAlone(reason) => HiddenIdleRelaunchOutcome::Aborted(reason),
    }
}

/// One periodic check, driven by [`drive_stale_idle_checks`] from the
/// background loop spawned at boot ([`spawn_stale_idle_checks`]).
///
/// Leave-alone arms log at debug and return; the act logs ONE line, performs
/// the modal's own guarded relaunch (hidden flavour) with the pre-exit
/// re-check, and exits the process. A typed refusal (dist not newer,
/// version unreadable, binary missing) logs a warning and returns — the
/// next tick retries, and the record stays honest on disk.
pub(crate) async fn hidden_idle_stale_relaunch_tick<R: Runtime>(app: &AppHandle<R>) {
    let Ok(install_root) = crate::commands::installer::find_local_repo_root() else {
        return; // same no-clone case the boot freshness probe skips silently
    };
    let argv: Vec<String> = std::env::args().collect();
    let guards = || guards_from_probe(&install_root, || any_window_visible(app), &argv);
    match decide_hidden_idle_relaunch(&guards()) {
        IdleStaleDecision::LeaveAlone(reason) => {
            tracing::debug!(
                "[restart] hidden idle stale check: leaving the launcher alone ({})",
                reason
            );
        }
        IdleStaleDecision::Relaunch => {
            tracing::info!(
                "[restart] hidden idle launcher: {} recorded and no window, update or \
                 modal pending — relaunching into the dist binary via the restart \
                 modal's own path",
                crate::services::binary_freshness::CID_BINARY_STALE
            );
            let outcome = perform_hidden_idle_relaunch(
                &install_root,
                env!("CARGO_PKG_VERSION"),
                &DetachedSpawner,
                guards,
                || {
                    crate::quit_dialog::force_quit();
                    app.exit(0);
                },
            );
            match outcome {
                HiddenIdleRelaunchOutcome::Exited => {}
                HiddenIdleRelaunchOutcome::Aborted(reason) => {
                    tracing::warn!(
                        "[restart] hidden idle relaunch ABORTED before exit: {} — this \
                         process stays; the spawned child gives up its bounded \
                         single-instance wait and exits",
                        reason
                    );
                }
                HiddenIdleRelaunchOutcome::Refused(e) => {
                    tracing::warn!(
                        "[restart] hidden idle relaunch refused (non-fatal, the next \
                         tick retries): {}",
                        e
                    );
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// v0.2.102 review fix 3 (coordinator ruling): the relaunch child must not
// lose the single-instance lock to the exiting parent.
// ---------------------------------------------------------------------------

/// Argv flag the ONE relaunch path ([`restart_to_dist_args`]) appends to
/// every child: `--vct-relaunch-parent=<pid of the exiting process>`. A
/// child started with it waits (bounded) for that pid to be GONE before the
/// single-instance plugin runs, instead of racing the parent's teardown and
/// handing its argv to a process that is about to exit — the failure that
/// left NO launcher running. A NORMAL second launch (no flag) keeps today's
/// immediate hand-off to the running instance.
pub(crate) const RELAUNCH_PARENT_FLAG: &str = "--vct-relaunch-parent";

/// The flag value this process attaches when relaunching a child.
fn relaunch_parent_arg() -> String {
    format!("{}={}", RELAUNCH_PARENT_FLAG, std::process::id())
}

/// PURE: extract the parent pid from an argv slice (`--vct-relaunch-parent=<pid>`).
/// `None` when the flag is absent, malformed, or names pid 0.
pub(crate) fn relaunch_parent_pid<S: AsRef<str>>(argv: &[S]) -> Option<u32> {
    argv.iter().find_map(|a| {
        let pid = a
            .as_ref()
            .strip_prefix(RELAUNCH_PARENT_FLAG)?
            .strip_prefix('=')?;
        pid.parse::<u32>().ok().filter(|p| *p != 0)
    })
}

/// Default bounded wait for the parent's exit before the single-instance
/// plugin runs. Generous: the parent has already decided to exit — the wait
/// only covers its teardown, and the act's own log line precedes it.
pub(crate) const DEFAULT_RELAUNCH_LOCK_WAIT: Duration = Duration::from_secs(30);

/// Env knob for [`DEFAULT_RELAUNCH_LOCK_WAIT`], in seconds.
pub(crate) const ENV_RELAUNCH_LOCK_WAIT_SECS: &str = "VCT_LAUNCHER_RELAUNCH_LOCK_WAIT_SECS";

/// Poll cadence of the parent-exit wait.
const RELAUNCH_LOCK_POLL: Duration = Duration::from_millis(250);

/// The wait bound: `VCT_LAUNCHER_RELAUNCH_LOCK_WAIT_SECS` seconds when set
/// to a positive integer, the generous default otherwise.
pub(crate) fn relaunch_lock_wait() -> Duration {
    relaunch_lock_wait_from(std::env::var(ENV_RELAUNCH_LOCK_WAIT_SECS).ok().as_deref())
}

/// Pure core of [`relaunch_lock_wait`].
fn relaunch_lock_wait_from(raw: Option<&str>) -> Duration {
    raw.and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|secs| *secs > 0)
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_RELAUNCH_LOCK_WAIT)
}

/// How waiting for the parent ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ParentWaitOutcome {
    /// The parent is gone — its single-instance lock is released.
    ParentGone,
    /// The bounded window expired with the parent still alive.
    TimedOut(Duration),
}

/// The bounded parent-exit wait, injectable (`is_alive` + `sleep`) so both
/// arms are testable without real processes. Checks aliveness BEFORE the
/// first sleep (a parent that is already gone costs no wait at all).
pub(crate) fn wait_for_parent_exit(
    pid: u32,
    budget: Duration,
    poll: Duration,
    mut is_alive: impl FnMut(u32) -> bool,
    mut sleep: impl FnMut(Duration),
) -> ParentWaitOutcome {
    let deadline = Instant::now() + budget;
    while is_alive(pid) {
        if Instant::now() >= deadline {
            return ParentWaitOutcome::TimedOut(budget);
        }
        sleep(poll);
    }
    ParentWaitOutcome::ParentGone
}

/// Production wrapper: called at the very top of `lib.rs::run()` (before the
/// Tauri builder, hence before the single-instance plugin) when this process
/// was started as a relaunch child. Waits the bounded window; on expiry logs
/// one line and falls through — the plugin's normal hand-off then decides,
/// exactly like a normal second launch.
pub(crate) fn wait_for_relaunch_parent_before_lock(parent_pid: u32) {
    let budget = relaunch_lock_wait();
    tracing::info!(
        "[restart] relaunch child: waiting up to {:?} for the previous launcher \
         (pid {}) to exit and release the single-instance lock",
        budget,
        parent_pid
    );
    match wait_for_parent_exit(
        parent_pid,
        budget,
        RELAUNCH_LOCK_POLL,
        process_alive,
        |d| std::thread::sleep(d),
    ) {
        ParentWaitOutcome::ParentGone => {}
        ParentWaitOutcome::TimedOut(_) => {
            tracing::warn!(
                "[restart] relaunch child: the previous launcher (pid {}) still holds \
                 the single-instance lock after {:?} — giving up the wait; the \
                 single-instance hand-off decides",
                parent_pid,
                budget
            );
        }
    }
}

/// Is `pid` a live process? One sysinfo snapshot refreshed for exactly that
/// pid (never the whole table — this polls every 250 ms while a relaunch
/// child waits). A zombie reads as alive for the moment until its reaper
/// collects it, which only lengthens the wait by one poll.
fn process_alive(pid: u32) -> bool {
    use sysinfo::{ProcessRefreshKind, ProcessesToUpdate};
    let mut sys = sysinfo::System::new();
    sys.refresh_processes_specifics(
        ProcessesToUpdate::Some(&[sysinfo::Pid::from_u32(pid)]),
        true,
        ProcessRefreshKind::nothing(),
    );
    sys.process(sysinfo::Pid::from_u32(pid)).is_some()
}

// ---------------------------------------------------------------------------
// v0.2.102 review fix 4: the stale-idle loop is INDEPENDENT of tray creation
// ---------------------------------------------------------------------------

/// Wake cadence of the standalone stale-idle loop. The STALE check itself
/// runs at [`stale_idle_check_interval`]; this is only how often the loop
/// looks at the clock (matches the tray poll's 5 s so the two loops behave
/// alike when both run).
const STALE_IDLE_POLL: Duration = Duration::from_secs(5);

/// The stale-idle check scheduler + invoker, over an INJECTED wake stream
/// and tick: for every wake instant, fire the tick iff the wake is at or
/// past the next-due instant, then push next-due a full interval past the
/// wake. The FIRST wake is never due (next-due is seeded from it) — a
/// freshly booted launcher is by definition on its own binary. Pure over
/// its inputs: the production loop feeds `Instant::now()` wakes on a
/// [`STALE_IDLE_POLL`] sleep, tests feed synthetic clocks — no source scan.
pub(crate) async fn drive_stale_idle_checks<I, T, TF>(
    wakes: I,
    interval: Duration,
    mut tick: T,
) where
    I: IntoIterator<Item = Instant>,
    T: FnMut() -> TF,
    TF: Future<Output = ()>,
{
    let mut next_due: Option<Instant> = None;
    for now in wakes {
        let due = next_due.get_or_insert(now + interval);
        if now >= *due {
            *due = now + interval;
            tick().await;
        }
    }
}

/// Spawn the tray-independent stale-idle loop: it MUST run even when the
/// system tray failed to build (a session with no StatusNotifier host —
/// review fix 4; the tick no longer lives in `tray.rs` for exactly that
/// reason). Spawned once from `lib.rs` setup beside the daily update check.
pub(crate) fn spawn_stale_idle_checks<R: Runtime + 'static>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(STALE_IDLE_POLL).await;
            let now = Instant::now();
            drive_stale_idle_checks(
                [now],
                stale_idle_check_interval(),
                || hidden_idle_stale_relaunch_tick(&app),
            )
            .await;
        }
    });
}

/// Spawn the new launcher fully detached. The current process exits
/// immediately afterward; the child must be in its own session/process
/// group so the kernel doesn't tear it down with us.
fn spawn_detached_launcher(exe: &Path, args: &[String]) -> Result<(), String> {
    let mut cmd = std::process::Command::new(exe).silent();
    cmd.args(args);
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

    // Imported-cid gate: moved in v0.2.102 (W6-Z2) to
    // `tests/test_v02102_rust_source_scan_gates.py::RealTreeGates::test_the_stale_cid_is_imported_not_retyped`,
    // which blanks each test-gated item (`tests/common/rust_source.production_source`)
    // instead of truncating.
    // The Rust copy cut this file at `mod tests` — correct today, but blind to any
    // production code added after the test module.

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
    // v0.2.100 (owner rule F-W2-08(c)): the V0243-8 stub preserve is retired.
    // -----------------------------------------------------------------

    /// A legacy `stub: true` ledger is deleted like any other once its last
    /// entry is cleared — no path may leave a zero-entry UPDATE_DEFERRED.md.
    #[test]
    fn clear_restart_deferral_deletes_legacy_stub_when_only_entry() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dot_claude = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&dot_claude).expect("mkdir");
        let target = dot_claude.join("UPDATE_DEFERRED.md");
        std::fs::write(
            &target,
            "---\ncondition_ids: [launcher_restart_required]\nstub: true\n---\n\n\
             # VCO Update Deferred\n\n## launcher_restart_required (info)\n\n**Title**: foo\n\n---\n",
        )
        .expect("write");

        clear_restart_deferral(tmp.path()).expect("clear");
        assert!(!target.exists(), "a zero-entry ledger must not survive, stub or not");
    }

    /// A normal file is deleted when its last entry is cleared.
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
        spawned: std::cell::RefCell<Vec<(PathBuf, Vec<String>)>>,
    }

    impl RecordingSpawner {
        /// How many spawns were recorded.
        fn count(&self) -> usize {
            self.spawned.borrow().len()
        }
    }

    impl Spawner for RecordingSpawner {
        fn spawn_detached(&self, exe: &Path, args: &[String]) -> Result<(), String> {
            self.spawned.borrow_mut().push((exe.to_path_buf(), args.to_vec()));
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
        let exe =
            spawn_guarded(td.path(), "0.2.99", &spawner, &[]).expect("newer dist relaunches");
        assert_eq!(exe, dist_launcher_path(td.path()));
        assert_eq!(
            *spawner.spawned.borrow(),
            vec![(dist_launcher_path(td.path()), Vec::new())]
        );
    }

    /// LEAVE-ALONE: an equal or OLDER dist binary is a typed refusal and
    /// nothing is spawned (L2-F01: the old resolver installed the older one).
    #[test]
    fn relaunch_refuses_a_not_newer_dist_binary() {
        for dist in ["0.2.99", "0.2.98", "0.2.9"] {
            let td = root_with_dist(Some(dist));
            let spawner = RecordingSpawner::default();
            let err = spawn_guarded(td.path(), "0.2.99", &spawner, &[])
                .expect_err(dist);
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

    /// v0.2.100 (L2-F01): the USER-facing restart goes through the same
    /// guard. Leave-alone: a dist not newer than the running launcher (a
    /// running 0.2.100 over an on-disk 0.2.99 — the field case) spawns
    /// nothing and is a worded refusal. Act: a newer dist is spawned.
    #[test]
    fn restart_launcher_refuses_a_not_newer_dist_and_spawns_a_newer_one() {
        let td = root_with_dist(Some("0.2.99"));
        let spawner = RecordingSpawner::default();
        let err = restart_to_dist(td.path(), "0.2.100", &spawner).expect_err("older dist");
        assert!(err.to_string().contains("not newer"), "{err}");
        assert!(spawner.spawned.borrow().is_empty(), "nothing may be spawned");

        let td = root_with_dist(Some("0.2.101"));
        let exe = restart_to_dist(td.path(), "0.2.100", &spawner).expect("newer dist");
        assert_eq!(exe, dist_launcher_path(td.path()));
        assert_eq!(spawner.count(), 1);
        // The MODAL flavour carries only the relaunch-parent flag (no
        // --start-hidden — the user is looking at the window the Continue
        // button lives in).
        assert_eq!(
            (*spawner.spawned.borrow())[0].1,
            vec![relaunch_parent_arg()],
            "the modal's child must carry the single-instance wait flag and nothing else",
        );
    }

    /// Tri-state: an absent sidecar or a non-X.Y.Z version is never "newer".
    #[test]
    fn relaunch_refuses_unknown_or_unreadable_versions() {
        let spawner = RecordingSpawner::default();
        let td = root_with_dist(None);
        assert!(matches!(
            spawn_guarded(td.path(), "0.2.99", &spawner, &[]),
            Err(RelaunchError::Refused(RelaunchRefusal::DistVersionUnknown { .. }))
        ));
        let td = root_with_dist(Some("0.2.100-rc1"));
        assert!(matches!(
            spawn_guarded(td.path(), "0.2.99", &spawner, &[]),
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

    // -----------------------------------------------------------------
    // v0.2.102 (S16-13 / OD-7): hidden idle stale self-relaunch.
    // -----------------------------------------------------------------

    /// The act state: a stale record, started hidden, no update, no modal.
    fn idle_guards() -> IdleStaleGuards {
        IdleStaleGuards {
            binary_stale: true,
            window_visible: false,
            update_in_flight: false,
            modal_pending: false,
            started_hidden: true,
        }
    }

    /// ACT: stale + started hidden + idle → the tick's decision is Relaunch.
    #[test]
    fn hidden_idle_relaunch_fires_when_hidden_and_idle() {
        assert_eq!(
            decide_hidden_idle_relaunch(&idle_guards()),
            IdleStaleDecision::Relaunch,
            "a hidden idle launcher over a recorded stale binary must self-relaunch",
        );
    }

    /// LEAVE-ALONE (OD-7 scope, review fix 4): a launcher NOT started
    /// `--start-hidden` — e.g. a visible-mode launcher the user hid to the
    /// tray — keeps the modal path; the background tick never changes its
    /// start mode from under the user.
    #[test]
    fn hidden_idle_relaunch_leaves_a_visible_mode_launcher_alone() {
        let mut g = idle_guards();
        g.started_hidden = false;
        assert_eq!(
            decide_hidden_idle_relaunch(&g),
            IdleStaleDecision::LeaveAlone(
                "not a --start-hidden launcher; the modal stays the path"
            ),
        );
        // And the other arm: the same guards WITH the flag relaunch (pinned
        // above, restated so the pair reads as one decision).
        assert_eq!(decide_hidden_idle_relaunch(&idle_guards()), IdleStaleDecision::Relaunch);
    }

    /// LEAVE-ALONE (the point of OD-7): a visible window means the modal's
    /// Continue belongs to the user — a background tick must never restart
    /// the launcher under them.
    #[test]
    fn hidden_idle_relaunch_leaves_a_visible_window_alone() {
        let mut g = idle_guards();
        g.window_visible = true;
        assert_eq!(
            decide_hidden_idle_relaunch(&g),
            IdleStaleDecision::LeaveAlone("a window is visible"),
        );
    }

    /// LEAVE-ALONE: an update pipeline/apply owns the tree — relaunching
    /// mid-update would race the swap the gate exists to serialize.
    #[test]
    fn hidden_idle_relaunch_leaves_an_inflight_update_alone() {
        let mut g = idle_guards();
        g.update_in_flight = true;
        assert_eq!(
            decide_hidden_idle_relaunch(&g),
            IdleStaleDecision::LeaveAlone("an update is in flight"),
        );
    }

    /// LEAVE-ALONE: a pending modal (the unconsumed update-handoff outcome)
    /// owes the user a surface — not a silent process replacement.
    #[test]
    fn hidden_idle_relaunch_leaves_a_pending_modal_alone() {
        let mut g = idle_guards();
        g.modal_pending = true;
        assert_eq!(
            decide_hidden_idle_relaunch(&g),
            IdleStaleDecision::LeaveAlone("a modal is pending"),
        );
    }

    /// LEAVE-ALONE: no stale record → nothing to heal.
    #[test]
    fn hidden_idle_relaunch_leaves_a_fresh_binary_alone() {
        let mut g = idle_guards();
        g.binary_stale = false;
        assert_eq!(
            decide_hidden_idle_relaunch(&g),
            IdleStaleDecision::LeaveAlone("no launcher_binary_stale record"),
        );
    }

    /// A temp install root whose dist slot is NEWER than `running` AND whose
    /// deferral ledger carries a `launcher_binary_stale` record — the full
    /// act state of the hidden idle tick, on disk.
    fn root_with_dist_and_stale_record(dist: &str) -> tempfile::TempDir {
        let td = root_with_dist(Some(dist));
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).unwrap();
        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            format!(
                "---\ncondition_ids: [{cid}]\n---\n\n# VCO Update Deferred\n\n{body}\n",
                cid = crate::services::binary_freshness::CID_BINARY_STALE,
                body = stale_doc(),
            ),
        )
        .unwrap();
        td
    }

    /// ACT leg: the relaunch the tick performs is the DIST binary, exactly
    /// once, carrying `--start-hidden` AND the relaunch-parent flag (a
    /// tray-only launcher must stay tray-only across the self-relaunch, and
    /// the child must wait out the exiting parent's single-instance lock).
    #[test]
    fn hidden_idle_act_relaunches_the_dist_binary_hidden() {
        let td = root_with_dist_and_stale_record("0.2.102");
        let spawner = RecordingSpawner::default();
        let exe =
            restart_to_dist_hidden(td.path(), "0.2.101", &spawner).expect("newer dist relaunches");
        assert_eq!(exe, dist_launcher_path(td.path()));
        assert_eq!(
            *spawner.spawned.borrow(),
            vec![(
                dist_launcher_path(td.path()),
                vec![
                    crate::HIDDEN_START_FLAG.to_string(),
                    relaunch_parent_arg(),
                ],
            )],
            "the hidden idle relaunch must spawn the dist binary --start-hidden \
             and carrying the relaunch-parent flag",
        );
    }

    /// The `launcher_binary_stale` condition clears by its EXISTING probe,
    /// not by this path: after the act the record is still on disk (we do
    /// not strip it), and the relaunched process's boot probe's own
    /// predicate (`should_clear_stale_condition`) retires it on a `Fresh`
    /// verdict — the probe-tested act leg lives in binary_freshness.
    #[test]
    fn hidden_idle_act_leaves_the_stale_record_to_its_probe() {
        let td = root_with_dist_and_stale_record("0.2.102");
        let spawner = RecordingSpawner::default();
        restart_to_dist_hidden(td.path(), "0.2.101", &spawner)
            .expect("newer dist relaunches");
        let target = td
            .path()
            .join(".claude")
            .join("context")
            .join("UPDATE_DEFERRED.md");
        let body = std::fs::read_to_string(&target).expect("ledger still readable");
        assert!(
            extract_section(&body, crate::services::binary_freshness::CID_BINARY_STALE).is_some(),
            "the tick must NOT strip the stale record — the relaunched process's \
             boot probe retires it",
        );
        assert!(
            crate::services::binary_freshness::should_clear_stale_condition(
                &crate::services::binary_freshness::FreshnessVerdict::Fresh
            ),
            "a Fresh verdict (the post-relaunch boot state) must clear the record \
             through the existing probe",
        );
    }

    /// The reader agrees with the modal's banner on what "recorded" means:
    /// same cid, same section extractor; absent file / other condition →
    /// not recorded.
    #[test]
    fn binary_stale_recorded_uses_the_modal_cid_and_section() {
        let td = tempfile::tempdir().unwrap();
        let ctx = td.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).unwrap();
        std::fs::write(ctx.join("UPDATE_DEFERRED.md"), stale_doc()).unwrap();
        assert!(binary_stale_recorded(td.path()), "a recorded entry is found");

        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            "---\ncondition_ids: [other_thing]\n---\n\n## other_thing (warning)\n\n---\n",
        )
        .unwrap();
        assert!(
            !binary_stale_recorded(td.path()),
            "an unrelated condition is not a stale record",
        );

        std::fs::remove_file(ctx.join("UPDATE_DEFERRED.md")).unwrap();
        assert!(
            !binary_stale_recorded(td.path()),
            "a missing ledger is not a stale record",
        );
    }

    /// The modal-pending reader follows the isolated state dir: a preserved
    /// updater lock (or an unconsumed outcome) is pending; a clean dir is
    /// not. Never touches the real `~/.vct`.
    #[test]
    fn update_handoff_modal_pending_follows_the_state_dir() {
        let (_guard, dir) = {
            let td = vct_launcher_core::test_env::state_dir_guard();
            let p = td.path().to_path_buf();
            (td, p)
        };
        assert!(
            !update_handoff_modal_pending(),
            "a clean state dir has no pending modal",
        );
        std::fs::write(dir.join(crate::commands::update_handoff::UPDATE_LOCK_FILE), "{}")
            .unwrap();
        assert!(
            update_handoff_modal_pending(),
            "a preserved update.lock.json means the post-update modal is still owed",
        );
        std::fs::remove_file(dir.join(crate::commands::update_handoff::UPDATE_LOCK_FILE))
            .unwrap();
        std::fs::write(
            dir.join(crate::commands::update_handoff::UPDATE_RESULT_FILE),
            "{\"success\":true}",
        )
        .unwrap();
        assert!(
            update_handoff_modal_pending(),
            "an unconsumed update.result.json means the post-update modal is still owed",
        );
    }

    /// The env knob: positive seconds win; absent / malformed / zero /
    /// negative fall back to the generous default (never a hostile cadence).
    #[test]
    fn stale_idle_check_interval_parsing() {
        assert_eq!(
            stale_idle_check_interval_from(Some("90")),
            Duration::from_secs(90),
        );
        assert_eq!(
            stale_idle_check_interval_from(Some(" 90 ")),
            Duration::from_secs(90),
            "surrounding whitespace is tolerated",
        );
        for raw in [None, Some(""), Some("abc"), Some("0"), Some("-5"), Some("1.5")] {
            assert_eq!(
                stale_idle_check_interval_from(raw),
                DEFAULT_STALE_IDLE_CHECK_INTERVAL,
                "{raw:?} must fall back to the generous default",
            );
        }
        assert_eq!(
            DEFAULT_STALE_IDLE_CHECK_INTERVAL,
            Duration::from_secs(600),
            "the default stays generous (10 min) — ratchet DOWN only",
        );
    }

    // ─── v0.2.102 review fix 2: the window seam and its safety default ────

    /// The safety-critical default: an UNKNOWN visibility answer counts as
    /// VISIBLE. Flipping `unwrap_or(true)` to `unwrap_or(false)` in
    /// `visible_window_among` makes this test fail — that flip is exactly
    /// the "guard becomes unsafe" mutation.
    #[test]
    fn an_unknown_visibility_answer_counts_as_visible() {
        assert!(
            visible_window_among([None]),
            "unknown visibility must be treated as VISIBLE (leave-alone)",
        );
        assert!(
            visible_window_among([Some(false), None, Some(false)]),
            "one unknown among hidden windows still counts as visible",
        );
    }

    #[test]
    fn no_visible_window_only_when_every_answer_is_hidden() {
        assert!(!visible_window_among([Some(false), Some(false)]));
        assert!(!visible_window_among([]), "no windows at all → not visible");
        assert!(visible_window_among([Some(false), Some(true)]));
    }

    /// The guard ASSEMBLY must consult the injected window probe — a
    /// hardcode (`window_visible: false`) fails here, because the probe
    /// says visible and the assembled guard must carry that.
    #[test]
    fn guards_assembly_reads_the_window_probe() {
        let td = tempfile::tempdir().unwrap();
        let argv = vec![
            "vct-launcher".to_string(),
            crate::HIDDEN_START_FLAG.to_string(),
        ];
        let visible = guards_from_probe(td.path(), || true, &argv);
        assert!(visible.window_visible, "the probe's answer must survive assembly");
        let hidden = guards_from_probe(td.path(), || false, &argv);
        assert!(!hidden.window_visible);
    }

    /// The assembly must derive `started_hidden` from the owning argv —
    /// both arms (OD-7 scope, review fix 4).
    #[test]
    fn guards_assembly_reads_started_hidden_from_argv() {
        let td = tempfile::tempdir().unwrap();
        let with_flag = vec![
            "vct-launcher".to_string(),
            crate::HIDDEN_START_FLAG.to_string(),
        ];
        let without_flag = vec!["vct-launcher".to_string()];
        assert!(guards_from_probe(td.path(), || false, &with_flag).started_hidden);
        assert!(!guards_from_probe(td.path(), || false, &without_flag).started_hidden);
    }

    // ─── v0.2.102 review fix 1: the pre-exit re-validation ────────────────

    /// ACT: guards still pass after the spawn → the process is told to exit,
    /// exactly once.
    #[test]
    fn the_act_exits_when_the_recheck_still_passes() {
        let td = root_with_dist_and_stale_record("0.2.102");
        let spawner = RecordingSpawner::default();
        let exits = std::cell::Cell::new(0u32);
        let outcome = perform_hidden_idle_relaunch(
            td.path(),
            "0.2.101",
            &spawner,
            || idle_guards(),
            || exits.set(exits.get() + 1),
        );
        assert_eq!(outcome, HiddenIdleRelaunchOutcome::Exited);
        assert_eq!(exits.get(), 1, "the exit must run exactly once");
        assert_eq!(spawner.count(), 1);
    }

    /// LEAVE-ALONE (the race the review found): a window opened between the
    /// decision and the exit → the re-check aborts, the exit NEVER runs, and
    /// the old process stays. The already-spawned child is not lost state:
    /// it carries the relaunch-parent flag and gives up its bounded lock
    /// wait (see the fix-3 tests).
    #[test]
    fn a_window_opened_during_the_spawn_aborts_the_exit() {
        let td = root_with_dist_and_stale_record("0.2.102");
        let spawner = RecordingSpawner::default();
        let exits = std::cell::Cell::new(0u32);
        let outcome = perform_hidden_idle_relaunch(
            td.path(),
            "0.2.101",
            &spawner,
            || {
                let mut g = idle_guards();
                g.window_visible = true; // opened while the child was spawning
                g
            },
            || exits.set(exits.get() + 1),
        );
        assert_eq!(
            outcome,
            HiddenIdleRelaunchOutcome::Aborted("a window is visible"),
        );
        assert_eq!(exits.get(), 0, "the process exit must NOT run after a flip");
        // The child WAS spawned — the abort relies on the child's bounded
        // lock wait (fix 3) to exit cleanly, and its spawn args prove the
        // flag that makes it wait is present.
        assert_eq!(spawner.count(), 1);
        assert!((*spawner.spawned.borrow())[0].1.contains(&relaunch_parent_arg()));
    }

    /// Same race, the update-in-flight flip (also re-checked pre-exit).
    #[test]
    fn an_update_started_during_the_spawn_aborts_the_exit() {
        let td = root_with_dist_and_stale_record("0.2.102");
        let spawner = RecordingSpawner::default();
        let outcome = perform_hidden_idle_relaunch(
            td.path(),
            "0.2.101",
            &spawner,
            || {
                let mut g = idle_guards();
                g.update_in_flight = true;
                g
            },
            || panic!("the exit must not run"),
        );
        assert_eq!(
            outcome,
            HiddenIdleRelaunchOutcome::Aborted("an update is in flight"),
        );
    }

    /// Refusal: a not-newer dist spawns nothing and exits nothing.
    #[test]
    fn a_refused_relaunch_exits_nothing() {
        let td = root_with_dist_and_stale_record("0.2.100"); // not newer than running
        let spawner = RecordingSpawner::default();
        let outcome = perform_hidden_idle_relaunch(
            td.path(),
            "0.2.101",
            &spawner,
            || idle_guards(),
            || panic!("the exit must not run on a refusal"),
        );
        assert!(matches!(outcome, HiddenIdleRelaunchOutcome::Refused(_)));
        assert_eq!(spawner.count(), 0, "a refusal spawns nothing");
    }

    // ─── v0.2.102 review fix 4: the tray-independent loop's scheduler ────

    /// Behavioural wiring proof (no source scan): the drive core invokes the
    /// injected tick ONLY at due wakes. Deleting the `tick().await` (or the
    /// due test) in `drive_stale_idle_checks` fails this.
    #[tokio::test]
    async fn the_stale_idle_loop_fires_the_tick_only_at_due_wakes() {
        let t0 = Instant::now();
        let interval = Duration::from_secs(600);
        // Wakes every 5 s for 25 minutes: due exactly at 600 s and 1200 s
        // (the FIRST wake seeds next-due and is never due itself).
        let wakes: Vec<Instant> = (0..=300)
            .map(|i| t0 + Duration::from_secs(5 * i))
            .collect();
        let fired = std::cell::Cell::new(0u32);
        let fired_at = std::cell::RefCell::new(Vec::<Instant>::new());
        drive_stale_idle_checks(wakes, interval, || {
            fired.set(fired.get() + 1);
            fired_at.borrow_mut().push(Instant::now());
            std::future::ready(())
        })
        .await;
        assert_eq!(
            fired.get(),
            2,
            "25 min of 5 s wakes at a 10 min interval fire the tick exactly twice",
        );
        let _ = fired_at; // (counting is the assertion; times are debug aid)
    }

    /// The first wake is never due — a freshly booted launcher is on its
    /// own binary, and the first wake also seeds the schedule when the
    /// interval is env-shortened between wakes.
    #[tokio::test]
    async fn the_first_wake_is_never_due() {
        let t0 = Instant::now();
        let fired = std::cell::Cell::new(0u32);
        drive_stale_idle_checks(
            [t0, t0 + Duration::from_secs(5)],
            Duration::from_secs(600),
            || {
                fired.set(fired.get() + 1);
                std::future::ready(())
            },
        )
        .await;
        assert_eq!(fired.get(), 0, "no wake before the first interval may fire");
    }

    // ─── v0.2.102 review fix 3: the relaunch child's bounded lock wait ────

    #[test]
    fn relaunch_parent_pid_parses_the_flag() {
        let argv = vec![
            "vct-launcher".to_string(),
            format!("{}=4242", RELAUNCH_PARENT_FLAG),
        ];
        assert_eq!(relaunch_parent_pid(&argv), Some(4242));
        // Position-independent (matches how HIDDEN_START_FLAG is matched).
        assert_eq!(
            relaunch_parent_pid(&[format!("{}=7", RELAUNCH_PARENT_FLAG)]),
            Some(7),
        );
    }

    #[test]
    fn relaunch_parent_pid_none_for_normal_launches_and_malformed_values() {
        // A NORMAL second launch keeps today's immediate hand-off: no flag,
        // no wait.
        assert_eq!(
            relaunch_parent_pid(&["vct-launcher".to_string(), "--start-hidden".to_string()]),
            None,
        );
        assert_eq!(relaunch_parent_pid::<String>(&[]), None);
        // Malformed / zero values never name a process to wait on.
        assert_eq!(relaunch_parent_pid(&[format!("{}=", RELAUNCH_PARENT_FLAG)]), None);
        assert_eq!(relaunch_parent_pid(&[format!("{}=abc", RELAUNCH_PARENT_FLAG)]), None);
        assert_eq!(relaunch_parent_pid(&[format!("{}=0", RELAUNCH_PARENT_FLAG)]), None);
        assert_eq!(
            relaunch_parent_pid(&[RELAUNCH_PARENT_FLAG.to_string()]),
            None,
            "the flag without =pid is not a wait request",
        );
    }

    /// ACT arm 1: a parent that is already gone costs no wait at all.
    #[test]
    fn the_lock_wait_returns_immediately_when_the_parent_is_gone() {
        let slept = std::cell::Cell::new(0u32);
        let outcome = wait_for_parent_exit(
            4242,
            Duration::from_secs(30),
            Duration::from_millis(250),
            |_| false, // parent gone on the very first probe
            |_| slept.set(slept.get() + 1),
        );
        assert_eq!(outcome, ParentWaitOutcome::ParentGone);
        assert_eq!(slept.get(), 0, "no sleep may happen when the parent is gone");
    }

    /// ACT arm 2: a parent that exits mid-wait is noticed on the next poll.
    #[test]
    fn the_lock_wait_polls_until_the_parent_exits() {
        let probes = std::cell::Cell::new(0u32);
        let outcome = wait_for_parent_exit(
            4242,
            Duration::from_secs(30),
            Duration::from_millis(250),
            |_| {
                let n = probes.get();
                probes.set(n + 1);
                n < 3 // alive for the first 3 probes, gone on the 4th
            },
            |_| {},
        );
        assert_eq!(outcome, ParentWaitOutcome::ParentGone);
        assert_eq!(probes.get(), 4);
    }

    /// Give-up arm: a parent that never exits exhausts the bounded window
    /// (TimedOut) — never an unbounded hang.
    #[test]
    fn the_lock_wait_gives_up_after_the_bounded_window() {
        let slept = std::cell::Cell::new(0u32);
        let outcome = wait_for_parent_exit(
            4242,
            Duration::from_millis(80),
            Duration::from_millis(10),
            |_| true, // parent never exits
            |_| slept.set(slept.get() + 1),
        );
        assert_eq!(outcome, ParentWaitOutcome::TimedOut(Duration::from_millis(80)));
        assert!(slept.get() >= 1, "the wait must have polled before giving up");
    }

    #[test]
    fn relaunch_lock_wait_parsing() {
        assert_eq!(relaunch_lock_wait_from(Some("45")), Duration::from_secs(45));
        for raw in [None, Some(""), Some("abc"), Some("0"), Some("-1")] {
            assert_eq!(
                relaunch_lock_wait_from(raw),
                DEFAULT_RELAUNCH_LOCK_WAIT,
                "{raw:?} must fall back to the generous default",
            );
        }
        assert_eq!(
            DEFAULT_RELAUNCH_LOCK_WAIT,
            Duration::from_secs(30),
            "the default bound stays generous — ratchet DOWN only",
        );
    }

    /// The production aliveness probe works both ways on this machine: our
    /// own pid is alive; a child we already reaped is not. (Unix-gated: the
    /// reap trick relies on wait() collecting the entry.)
    #[cfg(unix)]
    #[test]
    fn process_alive_sees_live_and_reaped_processes() {
        assert!(process_alive(std::process::id()), "our own pid is alive");
        let mut child = std::process::Command::new("true").spawn().expect("spawn true");
        let pid = child.id();
        child.wait().expect("reap child");
        assert!(
            !process_alive(pid),
            "a reaped child must read as gone — the wait would never end otherwise",
        );
    }
}
