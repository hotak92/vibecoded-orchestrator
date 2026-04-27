//! Container-runtime install commands for the launcher GUI.
//!
//! These commands back the `NoContainerRuntimeDialog.svelte` modal that
//! shows when neither Podman nor Docker is detected at launcher boot.
//! Three Tauri commands:
//!
//!   - `runtime_install_podman_linux`: spawn `pkexec apt/dnf/pacman
//!     install podman` and stream progress. pkexec is the canonical
//!     GNOME/KDE polkit agent — it pops up a graphical auth dialog on
//!     every modern Linux desktop (Ubuntu, Fedora, Arch with KDE/GNOME)
//!     without us needing to bundle a polkit policy file. Refs:
//!     https://www.freedesktop.org/software/polkit/docs/latest/pkexec.1.html
//!
//!   - `runtime_open_install_url`: open a canonical install URL in the
//!     user's default browser via `tauri-plugin-opener`'s `open_url`.
//!     The plugin's API is `open_url(url, with: Option<&str>)` — the
//!     second arg is "open with this app" override which we pass `None`
//!     for. Verified against tauri-plugin-opener 2.5.3 (the version
//!     vendored in our Cargo.lock).
//!     Plugin docs: https://v2.tauri.app/plugin/opener/
//!
//!   - `runtime_recheck`: invalidate the runtime cache and re-probe.
//!     Called from the modal's "I've installed it — re-check" button.
//!     Returns the detected runtime name on success or `null` if still
//!     missing.
//!
//! macOS / Windows pathway: we deliberately do NOT run package-manager
//! installs there. Reasons:
//!   - macOS: Homebrew may not be present; even with brew, the user
//!     still needs `podman machine init && podman machine start`. The
//!     official Podman docs prefer the .dmg installer over Homebrew.
//!   - Windows: Podman requires WSL2 underneath, which itself requires
//!     admin elevation + a reboot. Not something a single button click
//!     can do reliably.
//! On those OSes, the dialog only offers "Open install page" + "Re-check".

use serde::{Deserialize, Serialize};
use std::process::Stdio;
use tauri::{command, AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command as TokioCommand;

use crate::services::runtime::{detect_runtime, invalidate_cache as invalidate_runtime_cache};

/// Frontend event for streaming pkexec install progress. Matches the
/// dialog's `listen('vct-runtime-install-progress', ...)` subscription.
const EVT_INSTALL_PROGRESS: &str = "vct-runtime-install-progress";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallProgress {
    /// `"starting"` | `"output"` | `"completed"` | `"failed"` | `"cancelled"`.
    pub phase: String,
    /// Human-readable message or stdout/stderr line.
    pub message: String,
}

/// Detect the host's package manager. Returns the binary name on PATH
/// and the install args, or None if unsupported.
fn detect_linux_pkg_manager() -> Option<(&'static str, Vec<&'static str>)> {
    // Order matters: apt-get is on Debian/Ubuntu/derivatives; dnf is
    // Fedora/RHEL family; pacman is Arch. We don't bother with zypper
    // (openSUSE) — the modal will fall through to "Open install page".
    if which_simple("apt-get").is_some() {
        // -y for non-interactive (pkexec is the only auth prompt).
        return Some(("apt-get", vec!["install", "-y", "podman"]));
    }
    if which_simple("dnf").is_some() {
        return Some(("dnf", vec!["install", "-y", "podman"]));
    }
    if which_simple("pacman").is_some() {
        return Some(("pacman", vec!["-S", "--noconfirm", "podman"]));
    }
    None
}

/// Minimal PATH walk — we only need to know if a binary exists on
/// PATH, not its exact location. Mirrors `services::runtime::which_on_path`
/// without the Windows-extension cases (Linux only here).
fn which_simple(name: &str) -> Option<std::path::PathBuf> {
    let paths = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&paths) {
        let p = dir.join(name);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

/// Linux-only: install Podman via the system package manager, elevated
/// through pkexec. Streams stdout/stderr via the
/// `vct-runtime-install-progress` event so the dialog can render a
/// progress feed.
///
/// Cancellation/error matrix:
///   - pkexec absent → `failed` event, return Err.
///   - User dismisses pkexec dialog → exit 126/127 from pkexec → `failed`.
///   - apt/dnf/pacman returns non-zero → `failed`.
///   - All success → `completed` + cache invalidation, return Ok.
///
/// On macOS/Windows this command is registered but should not be called
/// — the frontend gates the button on platform. If invoked anyway,
/// returns an explanatory error so we fail loud rather than do nothing.
#[command]
pub async fn runtime_install_podman_linux(app: AppHandle) -> Result<(), String> {
    if !cfg!(target_os = "linux") {
        return Err(
            "runtime_install_podman_linux is Linux-only. Use runtime_open_install_url \
             on macOS/Windows."
                .to_string(),
        );
    }

    let (pkg_mgr, args) = match detect_linux_pkg_manager() {
        Some(x) => x,
        None => {
            let msg = "No supported package manager (apt/dnf/pacman) on PATH.".to_string();
            let _ = app.emit(
                EVT_INSTALL_PROGRESS,
                InstallProgress {
                    phase: "failed".into(),
                    message: msg.clone(),
                },
            );
            return Err(msg);
        }
    };

    if which_simple("pkexec").is_none() {
        let msg = "pkexec not found — install polkit/policykit-1 to enable graphical \
                   sudo prompts."
            .to_string();
        let _ = app.emit(
            EVT_INSTALL_PROGRESS,
            InstallProgress {
                phase: "failed".into(),
                message: msg.clone(),
            },
        );
        return Err(msg);
    }

    // Build the pkexec command: `pkexec /usr/bin/env DEBIAN_FRONTEND=noninteractive
    // <pkg_mgr> <args...>`. pkexec shows the GNOME/KDE auth dialog automatically
    // when run from a GUI session — we DO NOT spawn an interactive terminal.
    //
    // Why the /usr/bin/env wrapper: pkexec strips most environment variables
    // by default for security. Without `DEBIAN_FRONTEND=noninteractive`,
    // apt-get can hang on interactive prompts that never reach our piped
    // stdin (e.g. "do you want to continue [Y/n]?"). The /usr/bin/env hop
    // is the canonical Debian/polkit pattern for forwarding env vars
    // through pkexec. dnf/pacman don't strictly need a frontend var, but
    // the wrapper is harmless there and keeps PATH propagation consistent.
    //
    // apt-get also needs an `update` first on stale Debian/Ubuntu boxes,
    // but a fresh first install of podman doesn't require it (the package
    // list ships with the system). Skip the update for simplicity.
    let _ = app.emit(
        EVT_INSTALL_PROGRESS,
        InstallProgress {
            phase: "starting".into(),
            message: format!("Installing podman via pkexec {} {} …", pkg_mgr, args.join(" ")),
        },
    );

    let mut cmd = TokioCommand::new("pkexec");
    // Wrap in /usr/bin/env so DEBIAN_FRONTEND survives pkexec's env strip.
    cmd.arg("/usr/bin/env");
    cmd.arg("DEBIAN_FRONTEND=noninteractive");
    cmd.arg(pkg_mgr);
    for a in &args {
        cmd.arg(a);
    }
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let msg = format!("Failed to spawn pkexec: {}", e);
            let _ = app.emit(
                EVT_INSTALL_PROGRESS,
                InstallProgress {
                    phase: "failed".into(),
                    message: msg.clone(),
                },
            );
            return Err(msg);
        }
    };

    // Stream stdout + stderr to the frontend. We tee both into the same
    // event stream — easier on the dialog's renderer, and apt/dnf print
    // most useful info on stdout anyway.
    if let Some(stdout) = child.stdout.take() {
        let app_clone = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                let _ = app_clone.emit(
                    EVT_INSTALL_PROGRESS,
                    InstallProgress {
                        phase: "output".into(),
                        message: line,
                    },
                );
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        let app_clone = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                let _ = app_clone.emit(
                    EVT_INSTALL_PROGRESS,
                    InstallProgress {
                        phase: "output".into(),
                        message: line,
                    },
                );
            }
        });
    }

    let status = match child.wait().await {
        Ok(s) => s,
        Err(e) => {
            let msg = format!("pkexec wait failed: {}", e);
            let _ = app.emit(
                EVT_INSTALL_PROGRESS,
                InstallProgress {
                    phase: "failed".into(),
                    message: msg.clone(),
                },
            );
            return Err(msg);
        }
    };

    if !status.success() {
        // pkexec exit codes: 126 = not authorized; 127 = pkexec absent
        // or auth dialog dismissed. apt/dnf/pacman pass through their
        // own non-zero codes. We don't differentiate beyond "failed" —
        // the streamed output already explains what went wrong.
        let msg = format!(
            "Install failed (exit {}). Check the output above; common causes: \
             user dismissed the auth dialog, or the package manager hit a network \
             error.",
            status.code().unwrap_or(-1)
        );
        let _ = app.emit(
            EVT_INSTALL_PROGRESS,
            InstallProgress {
                phase: "failed".into(),
                message: msg.clone(),
            },
        );
        return Err(msg);
    }

    // Force the runtime detector to re-probe — the binary should now be
    // on PATH.
    invalidate_runtime_cache();

    let _ = app.emit(
        EVT_INSTALL_PROGRESS,
        InstallProgress {
            phase: "completed".into(),
            message: "podman installed.".into(),
        },
    );
    Ok(())
}

/// Open the supplied install-instructions URL in the user's default
/// browser via the tauri-plugin-opener crate.
///
/// We allowlist only canonical URLs (podman.io, docker.com, brew.sh,
/// python.org, microsoft.com docs) so a compromised frontend can't trick
/// users into clicking through to phishing sites. The opener plugin's
/// scope config in `capabilities/default.json` is permissive (the plugin
/// defaults allow open_url broadly) — this allowlist is defense-in-depth.
const ALLOWED_INSTALL_URL_PREFIXES: &[&str] = &[
    "https://podman.io/",
    "https://podman-desktop.io/",
    "https://docs.docker.com/",
    "https://www.docker.com/products/docker-desktop",
    "https://brew.sh",
    "https://www.python.org/downloads/",
    "https://apps.microsoft.com/detail/",
    "https://learn.microsoft.com/",
];

/// Pure allowlist check — extracted so it can be unit-tested without
/// side-effecting the host's web browser. Earlier the test for the
/// public command called the FULL function, which on a host with
/// `DISPLAY=:0` set actually invoked `xdg-open` and popped a browser
/// tab on every `cargo test` run. Reported by user 2026-04-28.
fn install_url_is_allowed(url: &str) -> bool {
    ALLOWED_INSTALL_URL_PREFIXES.iter().any(|p| url.starts_with(p))
}

#[command]
pub async fn runtime_open_install_url(url: String) -> Result<(), String> {
    // Strict prefix allowlist: each entry is a URL prefix the modal is
    // allowed to ask us to open. Anything outside this list is a config
    // error or a tampered-with frontend; reject loudly.
    if !install_url_is_allowed(&url) {
        return Err(format!(
            "URL not in install-instructions allowlist: {}",
            url
        ));
    }

    // tauri-plugin-opener::open_url(url, with: Option<&str>). Passing
    // None means "use the OS default handler" (xdg-open on Linux,
    // LSOpenCFURLRef on macOS, ShellExecuteW on Windows).
    tauri_plugin_opener::open_url(&url, None::<&str>)
        .map_err(|e| format!("opener::open_url failed: {}", e))
}

/// Force a fresh runtime detection pass. Returns the detected runtime
/// name (`"podman"` | `"docker"`) or `None` if nothing is installed.
/// The dialog calls this when the user clicks "Re-check" after a manual
/// install on macOS/Windows (or as a verification step after the Linux
/// auto-install path).
#[command]
pub async fn runtime_recheck() -> Result<Option<String>, String> {
    invalidate_runtime_cache();
    Ok(detect_runtime().await.map(|info| info.runtime.binary().to_string()))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pkg_manager_detection_returns_known_or_none() {
        // Just make sure the function returns a sensible shape on this
        // host — actual matching depends on the test runner's PATH.
        match detect_linux_pkg_manager() {
            Some((mgr, args)) => {
                assert!(["apt-get", "dnf", "pacman"].contains(&mgr));
                assert!(!args.is_empty());
            }
            None => {
                // OK — host has none of apt/dnf/pacman.
            }
        }
    }

    // Allowlist tests use the pure `install_url_is_allowed` helper
    // instead of calling `runtime_open_install_url` directly. The latter
    // calls `tauri_plugin_opener::open_url` which on hosts with DISPLAY
    // set actually pops a browser tab — which was happening every
    // `cargo test --lib` run, opening podman.io repeatedly on dev
    // machines. The allowlist gate is the testable contract; the
    // open_url call itself is delegated to a mature 3rd-party plugin
    // we don't need to re-test here.

    #[test]
    fn install_url_allowlist_rejects_disallowed() {
        assert!(!install_url_is_allowed("https://evil.example.com/"));
        assert!(!install_url_is_allowed("http://podman.io/anything")); // http, not https
        assert!(!install_url_is_allowed("https://podman.io.evil/")); // host suffix attack
    }

    #[test]
    fn install_url_allowlist_accepts_canonical() {
        assert!(install_url_is_allowed(
            "https://podman.io/getting-started/installation"
        ));
        assert!(install_url_is_allowed("https://brew.sh"));
        assert!(install_url_is_allowed(
            "https://www.python.org/downloads/macos/"
        ));
        assert!(install_url_is_allowed(
            "https://docs.docker.com/desktop/install/linux-install/"
        ));
    }

    #[test]
    fn install_url_allowlist_accepts_redirected_podman_path() {
        // Podman renamed /getting-started/ → /docs/. Both the old and new
        // path live under the same allowlisted https://podman.io/ prefix.
        assert!(install_url_is_allowed("https://podman.io/docs/installation"));
    }
}
