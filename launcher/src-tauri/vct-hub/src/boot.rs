//! Cross-OS boot-time auto-start for vct-hub. (v0.2.21 Step 11.)
//!
//! Three operations: register (install unit), unregister (remove unit),
//! status (report state). Each dispatches to a per-OS implementation
//! gated by `cfg(target_os = ...)`.
//!
//! Why this module exists: Claude Code hooks (SessionStart, PreToolUse)
//! and other clients want the hub running without manual launcher
//! intervention. The CLI subcommands `vct-hub --register-boot` /
//! `--unregister-boot` / `--boot-status` register the hub with the host
//! init system so it starts at user login.
//!
//! Per the v0.2.21 design (default-OFF boot autostart in v0.2.21):
//!
//! | OS      | Mechanism              | Location                                                           |
//! |---------|------------------------|--------------------------------------------------------------------|
//! | Linux   | systemd user unit      | `$XDG_CONFIG_HOME/systemd/user/vct-hub.service`                    |
//! | macOS   | launchd LaunchAgent    | `~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist`          |
//! | Windows | Scheduled Task         | task name `VCT-Hub` (+ `vct-hub-boot.cmd` shim next to binary)     |
//!
//! Path resolution honours `VCT_STATE_DIR` for parity with the rest of
//! the launcher — boot units always embed the state-dir-at-registration-
//! time so a dev install never contaminates prod state on boot.
//!
//! All three operations are **idempotent**. Re-registering produces the
//! same on-disk state; unregistering a never-registered unit is a no-op.
//! `--register-boot` also enables-and-starts (Linux `--now`, macOS
//! `kickstart`, Windows explicit `schtasks /Run`) so the user sees the
//! hub running on the same invocation.
//!
//! Exit-code semantics (mapped by `main.rs::exit_with`):
//!   * `--register-boot`   → 0 on success, 1 on error.
//!   * `--unregister-boot` → 0 on success, 1 on error.
//!   * `--boot-status`     → 0 enabled, 1 disabled, 2 not-installed,
//!                            3 inspection error.

use std::path::{Path, PathBuf};
use std::process::Command;

use crate::lifecycle::LifecycleResult;
use vct_launcher_core::process::CommandExt as _;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/// Three-state report consumed by the launcher GUI toggle and the
/// `--boot-status` CLI.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootStatus {
    /// Unit installed and will fire on next boot/login.
    Enabled,
    /// Unit file present but not active (e.g. `systemctl --user disable`).
    Disabled,
    /// No unit/plist/task registered for vct-hub.
    NotInstalled,
}

/// Distinct error categories so the launcher can surface tailored
/// toasts. Kept hand-rolled (no `thiserror`) to keep vct-hub's
/// dependency surface minimal — see Cargo.toml comment on "lean hub".
#[derive(Debug)]
pub enum BootError {
    /// HOME not set (Linux/macOS path resolution).
    HomeNotSet,
    /// `std::env::current_exe()` failed or could not be canonicalised.
    PathResolveFailed,
    /// Windows-only: SID lookup via `whoami /user /fo list` failed.
    #[allow(dead_code)]
    SidLookupFailed,
    /// Required external tool not on PATH (e.g. `systemctl` on a
    /// non-systemd Linux, `launchctl` on macOS, `schtasks` on Windows).
    ToolNotFound {
        tool: &'static str,
    },
    /// External tool ran but exited non-zero.
    ToolFailed {
        tool: &'static str,
        stderr: String,
    },
    /// Filesystem I/O error.
    Io(std::io::Error),
    /// Unit file present but its embedded binary path no longer
    /// resolves (uninstall-without-unregister, manual install-tree
    /// move). Surfaced by `--boot-status` so the launcher GUI can show
    /// a "Re-register" button. Not used in v0.2.21 status path (kept
    /// for the v0.2.22 self-check follow-up).
    #[allow(dead_code)]
    StaleInstallPath(String),
}

impl std::fmt::Display for BootError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BootError::HomeNotSet => write!(f, "HOME not set"),
            BootError::PathResolveFailed => write!(f, "could not resolve current executable path"),
            BootError::SidLookupFailed => write!(f, "could not resolve current Windows user SID"),
            BootError::ToolNotFound { tool } => write!(f, "required tool not on PATH: {}", tool),
            BootError::ToolFailed { tool, stderr } => {
                write!(f, "tool {} failed: {}", tool, stderr.trim())
            }
            BootError::Io(e) => write!(f, "io error: {}", e),
            BootError::StaleInstallPath(p) => {
                write!(f, "boot unit points at stale path: {}", p)
            }
        }
    }
}

impl From<std::io::Error> for BootError {
    fn from(e: std::io::Error) -> Self {
        BootError::Io(e)
    }
}

// ---------------------------------------------------------------------------
// CLI entry points (called from main.rs)
// ---------------------------------------------------------------------------

/// `vct-hub --register-boot` entry point. Idempotent. Returns the
/// `LifecycleResult` shape `main.rs` already handles, so exit-code
/// translation is centralised.
pub fn run_register_boot() -> LifecycleResult {
    match register() {
        Ok(path) => {
            eprintln!(
                "[vct-hub] boot autostart: registered ({})",
                path.display()
            );
            LifecycleResult::Ok
        }
        Err(e) => {
            eprintln!("[vct-hub] boot autostart: ERROR {}: {}", error_kind(&e), e);
            LifecycleResult::Err(format!("register-boot failed: {}", e))
        }
    }
}

/// `vct-hub --unregister-boot` entry point. Idempotent.
pub fn run_unregister_boot() -> LifecycleResult {
    match unregister() {
        Ok(()) => {
            eprintln!("[vct-hub] boot autostart: unregistered");
            LifecycleResult::Ok
        }
        Err(e) => {
            eprintln!("[vct-hub] boot autostart: ERROR {}: {}", error_kind(&e), e);
            LifecycleResult::Err(format!("unregister-boot failed: {}", e))
        }
    }
}

/// `vct-hub --boot-status` entry point. Exit codes per the v0.2.21
/// design: 0 enabled, 1 disabled, 2 not-installed, 3 inspection error.
pub fn run_boot_status() -> LifecycleResult {
    match status() {
        Ok(BootStatus::Enabled) => {
            println!("enabled");
            LifecycleResult::OkExit(0)
        }
        Ok(BootStatus::Disabled) => {
            println!("disabled");
            LifecycleResult::OkExit(1)
        }
        Ok(BootStatus::NotInstalled) => {
            println!("not-installed");
            LifecycleResult::OkExit(2)
        }
        Err(e) => {
            println!("error: {}", e);
            // Inspection error is distinct from "disabled" — exit 3 lets
            // the launcher render an "OS not supported / cannot inspect"
            // state rather than misreading as plain disabled.
            LifecycleResult::OkExit(3)
        }
    }
}

/// String tag for the second-token in the error stream so launcher /
/// install.py can pattern-match on `error_kind` without locale issues.
fn error_kind(e: &BootError) -> &'static str {
    match e {
        BootError::HomeNotSet => "home_not_set",
        BootError::PathResolveFailed => "path_resolve_failed",
        BootError::SidLookupFailed => "sid_lookup_failed",
        BootError::ToolNotFound { .. } => "tool_not_found",
        BootError::ToolFailed { .. } => "tool_failed",
        BootError::Io(_) => "io_error",
        BootError::StaleInstallPath(_) => "stale_install_path",
    }
}

// ---------------------------------------------------------------------------
// Shared dispatcher
// ---------------------------------------------------------------------------

/// Install the boot-time auto-start unit. Idempotent. Returns the path
/// to the unit/plist/task body that was written (used in the success
/// message printed by `run_register_boot`).
pub fn register() -> Result<PathBuf, BootError> {
    warn_if_non_default_state_dir();
    #[cfg(target_os = "linux")]
    {
        return linux::register();
    }
    #[cfg(target_os = "macos")]
    {
        return macos::register();
    }
    #[cfg(target_os = "windows")]
    {
        return windows::register();
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        Err(BootError::ToolNotFound {
            tool: "(unsupported OS for boot autostart)",
        })
    }
}

/// Remove the boot-time auto-start unit. Idempotent.
pub fn unregister() -> Result<(), BootError> {
    #[cfg(target_os = "linux")]
    {
        return linux::unregister();
    }
    #[cfg(target_os = "macos")]
    {
        return macos::unregister();
    }
    #[cfg(target_os = "windows")]
    {
        return windows::unregister();
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        Ok(())
    }
}

/// Report current boot auto-start state.
pub fn status() -> Result<BootStatus, BootError> {
    #[cfg(target_os = "linux")]
    {
        return linux::status();
    }
    #[cfg(target_os = "macos")]
    {
        return macos::status();
    }
    #[cfg(target_os = "windows")]
    {
        return windows::status();
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        Ok(BootStatus::NotInstalled)
    }
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/// Canonical `current_exe()`: matches the pattern in
/// `commands/desktop_shortcut.rs`. We tolerate `dunce::canonicalize`
/// failure (returns the raw `current_exe()` instead) because the boot
/// unit just needs a path that exists at registration time — UNC
/// resolution is a nice-to-have, not a correctness requirement.
fn current_exe_canonical() -> Result<PathBuf, BootError> {
    let exe = std::env::current_exe().map_err(|_| BootError::PathResolveFailed)?;
    // `dunce` is available via vct-launcher-core's transitive deps and
    // is also a direct dep of the main launcher crate. We declare it
    // explicitly in vct-hub's Cargo.toml so the dependency is
    // self-documenting.
    Ok(dunce::canonicalize(&exe).unwrap_or(exe))
}

/// Best-effort atomic write: write to a sibling `.tmp`, then rename.
/// Avoids a partially-written unit/plist confusing the init system if
/// we crash mid-write.
fn atomic_write(path: &Path, body: &[u8]) -> std::io::Result<()> {
    let mut tmp = path.to_path_buf();
    let prev_ext = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    tmp.set_extension(format!("{}.tmp", prev_ext));
    std::fs::write(&tmp, body)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Emit a stderr warning if `VCT_STATE_DIR` is set to a non-default
/// path AND we're inside a `--register-boot` invocation. Dev users
/// who registered with a /tmp state dir would otherwise wake up on
/// next reboot with a hub pointing at a deleted state directory.
/// Per design Risk 2 — warn but proceed.
fn warn_if_non_default_state_dir() {
    if let Ok(custom) = std::env::var("VCT_STATE_DIR") {
        if !custom.is_empty() {
            eprintln!(
                "[vct-hub] boot autostart: WARNING — VCT_STATE_DIR={} is non-default. \
                 The boot unit will use this state dir on subsequent logins, \
                 which may not be what you want for dev installs.",
                custom
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Pure helpers (used by template rendering — kept testable without I/O)
// ---------------------------------------------------------------------------

/// Single-quote a path for inclusion in a systemd `ExecStart=` field.
/// systemd's parser accepts single-quoted tokens with embedded single
/// quotes escaped via `\'`. Guards against installation paths
/// containing spaces or shell metacharacters.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // any-OS for unit tests (see doc)
fn shell_single_quote(path: &Path) -> String {
    let s = path.display().to_string();
    let escaped = s.replace('\'', r"'\''");
    format!("'{}'", escaped)
}

/// XML-escape a path string for inclusion in a plist `<string>` or a
/// Windows Task XML `<Command>` field. Only the five XML metacharacters
/// matter; UTF-8 byte sequences are passed through.
///
/// `dead_code` allowed on Linux: this function is only reached from
/// `render_launchd_plist` (macOS) + `render_win_task_xml` (Windows)
/// + tests. The Linux build doesn't compile the per-OS modules that
/// call it, so the compiler can't see those use sites. The function
/// itself is unconditionally compiled because the tests below
/// exercise it from any host — that's by design (the rendering is
/// pure-string, easy to unit-test cross-OS).
#[cfg_attr(not(any(target_os = "macos", target_os = "windows")), allow(dead_code))]
fn xml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(ch),
        }
    }
    out
}

/// Linux systemd unit body. Pure render — given a binary path and
/// state dir, returns the same text every time. Kept top-level (not
/// inside the `linux` module) so the unit tests can exercise it on any
/// OS, not just Linux.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // any-OS for unit tests (see doc)
fn render_systemd_unit(bin: &Path, state_dir: &Path) -> String {
    const TEMPLATE: &str = include_str!("../templates/vct-hub.service.template");
    TEMPLATE
        .replace("__VCT_HUB_BIN__", &shell_single_quote(bin))
        .replace("__VCT_STATE_DIR__", &state_dir.display().to_string())
}

/// macOS launchd plist body. Pure render. Defined at top level (not
/// inside the macos sub-module) so unit tests can exercise the
/// rendering from any host. `dead_code` allowed on non-macOS to
/// silence the cross-OS lint — real call site is in the macos module.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn render_launchd_plist(bin: &Path, state_dir: &Path) -> String {
    const TEMPLATE: &str = include_str!("../templates/com.vibecodedtools.vct-hub.plist.template");
    TEMPLATE
        .replace("__VCT_HUB_BIN__", &xml_escape(&bin.display().to_string()))
        .replace(
            "__VCT_STATE_DIR__",
            &xml_escape(&state_dir.display().to_string()),
        )
}

/// Windows Scheduled Task XML body. Pure render. Same cross-OS-lint
/// dance as `render_launchd_plist` — top-level so unit tests work
/// anywhere; allow(dead_code) on non-Windows.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn render_win_task_xml(sid: &str, shim_path: &Path, bin_dir: &Path) -> String {
    const TEMPLATE: &str = include_str!("../templates/vct-hub-task.xml.template");
    TEMPLATE
        .replace("__WIN_USER_SID__", &xml_escape(sid))
        .replace(
            "__VCT_HUB_SHIM__",
            &xml_escape(&shim_path.display().to_string()),
        )
        .replace(
            "__VCT_HUB_DIR__",
            &xml_escape(&bin_dir.display().to_string()),
        )
}

/// Windows .cmd shim body. Pure render. Same cross-OS-lint dance.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn render_win_shim(state_dir: &Path) -> String {
    const TEMPLATE: &str = include_str!("../templates/vct-hub-boot.cmd.template");
    TEMPLATE.replace("__VCT_STATE_DIR__", &state_dir.display().to_string())
}

/// Pure parse: given the stdout of `schtasks /Query /V /FO LIST`,
/// returns the BootStatus implied by the "Scheduled Task State: ..."
/// line, or `None` when neither English marker appears.
///
/// Locale-fragile in the wild: `schtasks /FO LIST` localises BOTH the
/// keys AND the values on non-English Windows (e.g. German prints
/// `Aktiviert` / `Deaktiviert`), so an English-marker miss is the
/// EXPECTED case there, not a corner case. v0.2.54 Track G (G-8): this
/// used to default to `Enabled` on a miss, and the caller early-returned
/// on `Enabled` — which made the locale-invariant PowerShell fallback in
/// `windows::status()` unreachable in exactly the localised-Windows case
/// it was written for (a Disabled task on German Windows reported as
/// Enabled). Returning `None` routes the unparseable case to the
/// PowerShell fallback instead of guessing.
///
/// `dead_code` allowed on non-Windows: top-level for unit-test
/// accessibility from any host; only invoked from the windows module.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn parse_win_status_output(text: &str) -> Option<BootStatus> {
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.ends_with(": Enabled") || trimmed.ends_with(":Enabled") {
            return Some(BootStatus::Enabled);
        }
        if trimmed.ends_with(": Disabled") || trimmed.ends_with(":Disabled") {
            return Some(BootStatus::Disabled);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Linux
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
mod linux {
    use super::*;

    pub(super) fn register() -> Result<PathBuf, BootError> {
        // Detect systemd first: non-systemd distros need a clear error.
        check_systemctl_available()?;

        let unit_path = systemd_user_unit_path()?;
        let bin = current_exe_canonical()?;
        let state_dir = vct_launcher_core::paths::vct_root_dir();
        let body = render_systemd_unit(&bin, &state_dir);

        if let Some(parent) = unit_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        atomic_write(&unit_path, body.as_bytes())?;

        // daemon-reload picks up the new unit file.
        run_systemctl(&["--user", "daemon-reload"])?;
        // enable + start (matches systemd convention; --now is the
        // canonical "enable + start"). Per design open-question 2:
        // YES, we start immediately on register.
        run_systemctl(&["--user", "enable", "--now", "vct-hub.service"])?;
        Ok(unit_path)
    }

    pub(super) fn unregister() -> Result<(), BootError> {
        let unit_path = systemd_user_unit_path()?;
        // Disable + stop. We tolerate failures here — if the unit was
        // never enabled, systemctl exits non-zero but the unregister is
        // still successful from our POV.
        let _ = run_systemctl(&["--user", "disable", "--now", "vct-hub.service"]);
        if unit_path.exists() {
            std::fs::remove_file(&unit_path)?;
        }
        let _ = run_systemctl(&["--user", "daemon-reload"]);
        Ok(())
    }

    pub(super) fn status() -> Result<BootStatus, BootError> {
        let unit_path = systemd_user_unit_path()?;
        if !unit_path.exists() {
            return Ok(BootStatus::NotInstalled);
        }
        // `systemctl --user is-enabled vct-hub.service` exits 0 if
        // enabled, non-zero with stdout "disabled" / "linked" / "masked"
        // otherwise.
        let out = Command::new("systemctl").silent()
            .args(["--user", "is-enabled", "vct-hub.service"])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "systemctl" })?;
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if out.status.success() && stdout == "enabled" {
            Ok(BootStatus::Enabled)
        } else {
            Ok(BootStatus::Disabled)
        }
    }

    fn check_systemctl_available() -> Result<(), BootError> {
        // First: is systemctl on PATH at all?
        let probe = Command::new("systemctl").silent().arg("--version").output();
        let ok = probe.as_ref().map(|o| o.status.success()).unwrap_or(false);
        if !ok {
            return Err(BootError::ToolNotFound { tool: "systemctl" });
        }
        // Second: does this distro support user-mode systemctl? Some
        // distros ship the binary but disable user mode (no user dbus).
        // We check `systemctl --user is-system-running` and tolerate
        // any of the documented states (running/degraded/maintenance/
        // starting/initializing/offline) — what we WON'T tolerate is
        // "Failed to connect to bus" which manifests as a non-zero
        // exit code AND missing stdout. The "is-system-running"
        // command on a session bus returns text like "running" even
        // when degraded, so a zero-byte stdout is the smoke signal.
        let user_probe = Command::new("systemctl").silent()
            .args(["--user", "is-system-running"])
            .output();
        if let Ok(o) = user_probe {
            // If we got ANY stdout, user mode works. Exit code may be
            // non-zero (e.g. "degraded" exits 1) but stdout is the
            // ground truth.
            if o.stdout.is_empty() && !o.status.success() {
                return Err(BootError::ToolNotFound {
                    tool: "systemctl --user (no user bus)",
                });
            }
        }
        Ok(())
    }

    fn run_systemctl(args: &[&str]) -> Result<(), BootError> {
        let out = Command::new("systemctl").silent()
            .args(args)
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "systemctl" })?;
        if !out.status.success() {
            return Err(BootError::ToolFailed {
                tool: "systemctl",
                stderr: String::from_utf8_lossy(&out.stderr).to_string(),
            });
        }
        Ok(())
    }

    fn systemd_user_unit_path() -> Result<PathBuf, BootError> {
        let base = match std::env::var_os("XDG_CONFIG_HOME") {
            Some(d) if !d.is_empty() => PathBuf::from(d),
            _ => {
                let home = std::env::var_os("HOME").ok_or(BootError::HomeNotSet)?;
                PathBuf::from(home).join(".config")
            }
        };
        Ok(base.join("systemd/user/vct-hub.service"))
    }
}

// ---------------------------------------------------------------------------
// macOS
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod macos {
    use super::*;

    const LABEL: &str = "com.vibecodedtools.vct-hub";

    pub(super) fn register() -> Result<PathBuf, BootError> {
        // Code-signing dependency — see design doc §3 "Code-signing
        // requirements". Per Step 7 (just landed), vct-hub ships
        // UNSIGNED in v0.2.21. On distros that enforce Gatekeeper,
        // launchd will load the plist but Gatekeeper will reject the
        // binary, leaving the agent in a permanent restart loop. We
        // emit a clear stderr warning rather than refusing to register
        // (user may have signed locally; we can't introspect that
        // reliably across all macOS versions).
        let plist_path = launchagent_plist_path()?;
        let bin = current_exe_canonical()?;
        let state_dir = vct_launcher_core::paths::vct_root_dir();

        warn_if_unsigned(&bin);

        // The plist references log paths under state_dir/logs/; ensure
        // they exist so launchd can write StandardOut/ErrorPath.
        std::fs::create_dir_all(state_dir.join("logs"))?;

        let body = render_launchd_plist(&bin, &state_dir);
        if let Some(parent) = plist_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        atomic_write(&plist_path, body.as_bytes())?;

        let uid = unsafe { libc::getuid() };
        let target = format!("gui/{}", uid);
        let label_target = format!("{}/{}", target, LABEL);

        // If the agent is already loaded, bootstrap fails. Bootout
        // first to make re-registration idempotent.
        let _ = Command::new("launchctl").silent()
            .args(["bootout", &label_target])
            .output();

        let out = Command::new("launchctl").silent()
            .args([
                "bootstrap",
                &target,
                plist_path.to_str().unwrap_or_default(),
            ])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "launchctl" })?;
        if !out.status.success() {
            return Err(BootError::ToolFailed {
                tool: "launchctl bootstrap",
                stderr: String::from_utf8_lossy(&out.stderr).to_string(),
            });
        }
        // Kickstart so the agent runs NOW, not only at next login.
        let _ = Command::new("launchctl").silent()
            .args(["kickstart", "-k", &label_target])
            .output();
        Ok(plist_path)
    }

    pub(super) fn unregister() -> Result<(), BootError> {
        let plist_path = launchagent_plist_path()?;
        let uid = unsafe { libc::getuid() };
        let label_target = format!("gui/{}/{}", uid, LABEL);
        // Tolerate failure here; "Boot-out failed: not loaded" is fine.
        let _ = Command::new("launchctl").silent()
            .args(["bootout", &label_target])
            .output();
        if plist_path.exists() {
            std::fs::remove_file(&plist_path)?;
        }
        Ok(())
    }

    pub(super) fn status() -> Result<BootStatus, BootError> {
        let plist_path = launchagent_plist_path()?;
        if !plist_path.exists() {
            return Ok(BootStatus::NotInstalled);
        }
        let uid = unsafe { libc::getuid() };
        let label_target = format!("gui/{}/{}", uid, LABEL);
        // `launchctl print <target>` exits 0 if loaded, non-zero
        // otherwise. We only care about the exit code; output is
        // verbose.
        let out = Command::new("launchctl").silent()
            .args(["print", &label_target])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "launchctl" })?;
        if out.status.success() {
            Ok(BootStatus::Enabled)
        } else {
            Ok(BootStatus::Disabled)
        }
    }

    fn launchagent_plist_path() -> Result<PathBuf, BootError> {
        let home = std::env::var_os("HOME").ok_or(BootError::HomeNotSet)?;
        Ok(PathBuf::from(home).join(format!("Library/LaunchAgents/{}.plist", LABEL)))
    }

    /// Stderr warning if `codesign -dv` reports the binary is not
    /// signed. Best-effort: failure to run `codesign` is silently
    /// ignored (caller may have a non-default toolchain). Per design
    /// doc and the Step 7 codesign-pipeline followup gated for
    /// v0.2.21.1.
    fn warn_if_unsigned(bin: &Path) {
        let out = Command::new("codesign").silent()
            .args(["-dv", "--verbose=2"])
            .arg(bin)
            .output();
        let signed = matches!(&out, Ok(o) if o.status.success());
        if !signed {
            eprintln!(
                "[vct-hub] boot autostart: WARNING — binary at {} appears unsigned. \
                 macOS Gatekeeper may prevent launchd from executing it, leaving the \
                 LaunchAgent in a restart loop. For dev builds, run \
                 `xattr -d com.apple.quarantine <bin>` or sign locally. \
                 See v0.2.21.1 codesigning followup.",
                bin.display()
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------

#[cfg(target_os = "windows")]
mod windows {
    use super::*;

    const TASK_NAME: &str = "VCT-Hub";

    pub(super) fn register() -> Result<PathBuf, BootError> {
        let bin = current_exe_canonical()?;
        let bin_dir = bin
            .parent()
            .ok_or(BootError::PathResolveFailed)?
            .to_path_buf();
        let shim_path = bin_dir.join("vct-hub-boot.cmd");
        let state_dir = vct_launcher_core::paths::vct_root_dir();
        let user_sid = current_user_sid()?;

        // Write the .cmd shim.
        let shim_body = render_win_shim(&state_dir);
        atomic_write(&shim_path, shim_body.as_bytes())?;

        // Build Task XML.
        let xml = render_win_task_xml(&user_sid, &shim_path, &bin_dir);

        // Write XML to a stable path next to the binary so we don't
        // need a `tempfile` dep in non-dev code. `schtasks /Create
        // /XML` reads from a file path; we keep it discoverable for
        // post-mortem debugging.
        let xml_path = bin_dir.join("vct-hub-task.xml");
        // The XML template declares UTF-16 — schtasks 2008+ accepts
        // UTF-8 too in practice, but to match the declaration we
        // encode as UTF-16LE with BOM.
        let xml_utf16 = utf16_with_bom(&xml);
        atomic_write(&xml_path, &xml_utf16)?;

        // /Create /F overwrites any existing task with the same name
        // (idempotency).
        let out = Command::new("schtasks").silent()
            .args(["/Create", "/TN", TASK_NAME, "/XML"])
            .arg(&xml_path)
            .arg("/F")
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "schtasks" })?;
        if !out.status.success() {
            return Err(BootError::ToolFailed {
                tool: "schtasks /Create",
                stderr: String::from_utf8_lossy(&out.stderr).to_string(),
            });
        }

        // Per design open-question 2: also start the task NOW so the
        // user sees the hub running. /I = run interactively as current
        // user. Failure tolerated (hub may already be running, started
        // by the launcher).
        let _ = Command::new("schtasks").silent()
            .args(["/Run", "/TN", TASK_NAME, "/I"])
            .output();
        Ok(xml_path)
    }

    pub(super) fn unregister() -> Result<(), BootError> {
        // /F suppresses confirmation prompt.
        let _ = Command::new("schtasks").silent()
            .args(["/Delete", "/TN", TASK_NAME, "/F"])
            .output();
        // Best-effort: remove the shim + xml too.
        if let Ok(bin) = current_exe_canonical() {
            if let Some(dir) = bin.parent() {
                let _ = std::fs::remove_file(dir.join("vct-hub-boot.cmd"));
                let _ = std::fs::remove_file(dir.join("vct-hub-task.xml"));
            }
        }
        Ok(())
    }

    pub(super) fn status() -> Result<BootStatus, BootError> {
        // /Query exits 0 if the task exists, 1 otherwise.
        let out = Command::new("schtasks").silent()
            .args(["/Query", "/TN", TASK_NAME])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "schtasks" })?;
        if !out.status.success() {
            return Ok(BootStatus::NotInstalled);
        }
        // Task exists. Use /V /FO LIST for parseable output.
        let detail = Command::new("schtasks").silent()
            .args(["/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "schtasks" })?;
        let text = String::from_utf8_lossy(&detail.stdout);
        // Confident parse (English `Enabled` / `Disabled` marker found) is
        // trusted directly. v0.2.54 Track G (G-8): the pre-fix flow
        // early-returned only on Enabled — and the parser DEFAULTED to
        // Enabled when it couldn't parse — so the PowerShell fallback
        // below was unreachable on non-English Windows, the exact case
        // it exists for.
        if let Some(parsed) = parse_win_status_output(&text) {
            return Ok(parsed);
        }
        // Unparseable (localised schtasks output): fall back to
        // PowerShell for a robust read. Get-ScheduledTask returns an
        // enum value that's locale-invariant (the
        // Microsoft.Management.Infrastructure CIM class).
        if let Ok(ps) = Command::new("powershell").silent()
            .args([
                "-NoProfile",
                "-Command",
                &format!(
                    "(Get-ScheduledTask -TaskName '{}' -ErrorAction SilentlyContinue).State",
                    TASK_NAME
                ),
            ])
            .output()
        {
            if ps.status.success() {
                let s = String::from_utf8_lossy(&ps.stdout).trim().to_string();
                if s.eq_ignore_ascii_case("Ready") || s.eq_ignore_ascii_case("Running") {
                    return Ok(BootStatus::Enabled);
                }
                if s.eq_ignore_ascii_case("Disabled") {
                    return Ok(BootStatus::Disabled);
                }
            }
        }
        // Last resort (PowerShell unavailable / unparseable too): the
        // task EXISTS (the /Query probe above succeeded) and we never
        // write a Disabled task ourselves — report Enabled.
        Ok(BootStatus::Enabled)
    }

    /// Encode a UTF-8 string as UTF-16LE with a BOM, so schtasks
    /// matches the XML declaration's `encoding="UTF-16"`.
    fn utf16_with_bom(s: &str) -> Vec<u8> {
        let mut out = Vec::with_capacity(2 + s.len() * 2);
        out.extend_from_slice(&[0xFF, 0xFE]); // UTF-16LE BOM
        for unit in s.encode_utf16() {
            out.extend_from_slice(&unit.to_le_bytes());
        }
        out
    }

    /// Resolve the current Windows user's SID by shelling to
    /// `whoami /user /fo list`. Per design doc §4: SID is preferred
    /// over domain\username because it's locale-invariant and survives
    /// username changes. We use whoami instead of the windows-rs FFI
    /// to keep vct-hub's dep surface lean.
    fn current_user_sid() -> Result<String, BootError> {
        let out = Command::new("whoami").silent()
            .args(["/user", "/fo", "list"])
            .output()
            .map_err(|_| BootError::ToolNotFound { tool: "whoami" })?;
        if !out.status.success() {
            return Err(BootError::SidLookupFailed);
        }
        for line in String::from_utf8_lossy(&out.stdout).lines() {
            let trimmed = line.trim();
            if let Some(rest) = trimmed.strip_prefix("SID:") {
                return Ok(rest.trim().to_string());
            }
            // Localised Windows: some locales print "SID" in their own
            // language but the value still starts with "S-1-5-".
            if let Some(idx) = trimmed.find("S-1-5-") {
                return Ok(trimmed[idx..].split_whitespace().next().unwrap_or("").to_string());
            }
        }
        Err(BootError::SidLookupFailed)
    }
}

// ---------------------------------------------------------------------------
// Tests — pure render + parse helpers
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    // ------- shell_single_quote -------

    #[test]
    fn shell_single_quote_simple_path() {
        let p = PathBuf::from("/usr/local/bin/vct-hub");
        assert_eq!(shell_single_quote(&p), "'/usr/local/bin/vct-hub'");
    }

    #[test]
    fn shell_single_quote_path_with_space() {
        let p = PathBuf::from("/home/me/My Apps/vct-hub");
        assert_eq!(shell_single_quote(&p), "'/home/me/My Apps/vct-hub'");
    }

    #[test]
    fn shell_single_quote_path_with_apostrophe() {
        // Path contains a literal single quote; must be escaped via
        // the `'\''` idiom so systemd's parser sees the whole path as
        // one token.
        let p = PathBuf::from("/home/o'brien/vct-hub");
        let q = shell_single_quote(&p);
        assert_eq!(q, r"'/home/o'\''brien/vct-hub'");
    }

    // ------- xml_escape -------

    #[test]
    fn xml_escape_passes_safe_chars() {
        assert_eq!(xml_escape("/usr/local/bin/vct-hub"), "/usr/local/bin/vct-hub");
    }

    #[test]
    fn xml_escape_escapes_five_metacharacters() {
        let s = "<>&'\"";
        let escaped = xml_escape(s);
        assert_eq!(escaped, "&lt;&gt;&amp;&apos;&quot;");
    }

    // ------- render_systemd_unit -------

    #[test]
    fn systemd_unit_contains_bin_path_quoted() {
        let bin = PathBuf::from("/opt/vct/vct-hub");
        let state = PathBuf::from("/home/me/.vct");
        let body = render_systemd_unit(&bin, &state);
        assert!(
            body.contains("ExecStart='/opt/vct/vct-hub' --foreground"),
            "ExecStart line missing or wrong:\n{}",
            body
        );
    }

    #[test]
    fn systemd_unit_embeds_state_dir_env() {
        let bin = PathBuf::from("/opt/vct/vct-hub");
        let state = PathBuf::from("/home/me/.vct-dev");
        let body = render_systemd_unit(&bin, &state);
        assert!(
            body.contains("Environment=VCT_STATE_DIR=/home/me/.vct-dev"),
            "Environment= line missing or wrong:\n{}",
            body
        );
    }

    #[test]
    fn systemd_unit_has_type_simple_and_restart() {
        let body = render_systemd_unit(
            &PathBuf::from("/opt/vct/vct-hub"),
            &PathBuf::from("/home/me/.vct"),
        );
        assert!(body.contains("Type=simple"));
        assert!(body.contains("Restart=on-failure"));
        assert!(body.contains("RestartSec=10s"));
        assert!(body.contains("WantedBy=default.target"));
    }

    #[test]
    fn systemd_unit_has_no_placeholder_leakage() {
        let body = render_systemd_unit(
            &PathBuf::from("/opt/vct/vct-hub"),
            &PathBuf::from("/home/me/.vct"),
        );
        assert!(!body.contains("__VCT_HUB_BIN__"));
        assert!(!body.contains("__VCT_STATE_DIR__"));
    }

    // ------- render_launchd_plist -------

    #[test]
    fn launchd_plist_contains_label_and_bin_path() {
        let bin = PathBuf::from("/Applications/VCT.app/Contents/MacOS/vct-hub");
        let state = PathBuf::from("/Users/me/.vct");
        let body = render_launchd_plist(&bin, &state);
        assert!(body.contains("<string>com.vibecodedtools.vct-hub</string>"));
        assert!(
            body.contains("<string>/Applications/VCT.app/Contents/MacOS/vct-hub</string>"),
            "plist body did not embed binary path:\n{}",
            body
        );
    }

    #[test]
    fn launchd_plist_run_at_load_and_keep_alive() {
        let body = render_launchd_plist(
            &PathBuf::from("/usr/local/bin/vct-hub"),
            &PathBuf::from("/Users/me/.vct"),
        );
        assert!(body.contains("<key>RunAtLoad</key>"));
        // KeepAlive must be a SuccessfulExit=false dict, NOT a bare
        // true (which would restart on clean --stop too).
        assert!(body.contains("<key>SuccessfulExit</key>"));
        assert!(body.contains("<false/>"));
    }

    #[test]
    fn launchd_plist_xml_escapes_ampersand_in_path() {
        // Pathological path with an ampersand; rare but must escape.
        let bin = PathBuf::from("/Users/me/A&B/vct-hub");
        let body = render_launchd_plist(&bin, &PathBuf::from("/Users/me/.vct"));
        assert!(
            body.contains("/Users/me/A&amp;B/vct-hub"),
            "ampersand not XML-escaped:\n{}",
            body
        );
    }

    #[test]
    fn launchd_plist_log_paths_under_state_dir() {
        let body = render_launchd_plist(
            &PathBuf::from("/usr/local/bin/vct-hub"),
            &PathBuf::from("/Users/me/.vct"),
        );
        assert!(body.contains("/Users/me/.vct/logs/vct-hub.launchd.out"));
        assert!(body.contains("/Users/me/.vct/logs/vct-hub.launchd.err"));
    }

    // ------- render_win_task_xml + shim -------

    #[test]
    fn win_task_xml_embeds_sid_and_shim_path() {
        let xml = render_win_task_xml(
            "S-1-5-21-1111-2222-3333-1001",
            &PathBuf::from(r"C:\vct\vct-hub-boot.cmd"),
            &PathBuf::from(r"C:\vct"),
        );
        assert!(xml.contains("S-1-5-21-1111-2222-3333-1001"));
        assert!(xml.contains(r"<Command>C:\vct\vct-hub-boot.cmd</Command>"));
        assert!(xml.contains(r"<WorkingDirectory>C:\vct</WorkingDirectory>"));
        // LogonTrigger present + LeastPrivilege (no UAC).
        assert!(xml.contains("<LogonTrigger>"));
        assert!(xml.contains("<RunLevel>LeastPrivilege</RunLevel>"));
        assert!(xml.contains("<Delay>PT10S</Delay>"));
    }

    #[test]
    fn win_task_xml_no_placeholder_leakage() {
        let xml = render_win_task_xml(
            "S-1-5-21-1111-2222-3333-1001",
            &PathBuf::from(r"C:\vct\vct-hub-boot.cmd"),
            &PathBuf::from(r"C:\vct"),
        );
        assert!(!xml.contains("__WIN_USER_SID__"));
        assert!(!xml.contains("__VCT_HUB_SHIM__"));
        assert!(!xml.contains("__VCT_HUB_DIR__"));
    }

    #[test]
    fn win_shim_embeds_state_dir() {
        let body = render_win_shim(&PathBuf::from(r"C:\Users\me\.vct"));
        assert!(
            body.contains(r#"set "VCT_STATE_DIR=C:\Users\me\.vct""#),
            "shim body missing or wrong VCT_STATE_DIR:\n{}",
            body
        );
        assert!(body.contains(r#""%~dp0vct-hub.exe" --foreground"#));
        assert!(!body.contains("__VCT_STATE_DIR__"));
    }

    // ------- parse_win_status_output -------

    #[test]
    fn parse_win_status_enabled() {
        let stdout = "\
HostName: PC\r\n\
TaskName: \\VCT-Hub\r\n\
Scheduled Task State: Enabled\r\n\
Status: Ready\r\n\
";
        assert_eq!(parse_win_status_output(stdout), Some(BootStatus::Enabled));
    }

    #[test]
    fn parse_win_status_disabled() {
        let stdout = "TaskName: \\VCT-Hub\r\nScheduled Task State: Disabled\r\n";
        assert_eq!(parse_win_status_output(stdout), Some(BootStatus::Disabled));
    }

    #[test]
    fn parse_win_status_unparseable_returns_none() {
        // Garbled / LOCALISED output where neither English marker appears
        // (schtasks /FO LIST localises values too: German prints
        // `Aktiviert` / `Deaktiviert`). v0.2.54 Track G (G-8): this used
        // to assert a default of Enabled — which, combined with the
        // caller's early-return-on-Enabled, made the locale-invariant
        // PowerShell fallback unreachable on non-English Windows and
        // reported Disabled tasks as Enabled there. None routes the
        // caller to the PowerShell fallback instead.
        let stdout = "Lorem ipsum dolor sit amet\r\n";
        assert_eq!(parse_win_status_output(stdout), None);
    }

    #[test]
    fn parse_win_status_localised_disabled_returns_none() {
        // Real-world German schtasks /V /FO LIST shape: localised key AND
        // value. Must be None (PowerShell fallback decides), never Enabled.
        let stdout = "Status der geplanten Aufgabe: Deaktiviert\r\n";
        assert_eq!(parse_win_status_output(stdout), None);
    }

    // ------- error_kind tag stability -------

    #[test]
    fn error_kind_tags_are_stable_strings() {
        // The launcher / install.py grep for these strings on stderr.
        // Anchor them in the test so an accidental rename breaks here
        // before it breaks the launcher.
        assert_eq!(error_kind(&BootError::HomeNotSet), "home_not_set");
        assert_eq!(error_kind(&BootError::PathResolveFailed), "path_resolve_failed");
        assert_eq!(error_kind(&BootError::SidLookupFailed), "sid_lookup_failed");
        assert_eq!(
            error_kind(&BootError::ToolNotFound { tool: "x" }),
            "tool_not_found"
        );
        assert_eq!(
            error_kind(&BootError::ToolFailed {
                tool: "x",
                stderr: String::new()
            }),
            "tool_failed"
        );
        assert_eq!(
            error_kind(&BootError::Io(std::io::Error::new(
                std::io::ErrorKind::Other,
                "x"
            ))),
            "io_error"
        );
    }
}
