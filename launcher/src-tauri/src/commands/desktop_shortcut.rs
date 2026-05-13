//! Desktop-shortcut lifecycle (Bug C, v0.2.6).
//!
//! Mirrors the behavior of `scripts/post-install-launcher.sh::
//! _create_linux_desktop_entry` and `_create_macos_app_link` in Rust so the
//! launcher itself can refresh the icon AFTER an update (when the binary
//! path may have shifted, e.g. the user copied their install elsewhere).
//!
//! Why a Rust mirror of the shell script:
//!   - `apply_launcher_update` (self-update) and `update_orchestrator_at`
//!     ("Check Update") both run in-process; re-exec'ing post-install-
//!     launcher.sh from a long-running Tauri app is fiddly (we'd have to
//!     locate the script relative to the running binary, then shell-out
//!     into bash, and PATH may have been stripped by the spawn parent).
//!   - The icon-write logic itself is short (write a `.desktop` file or
//!     create a symlink). Re-implementing it in Rust keeps the launcher
//!     self-contained.
//!   - install.py (C1) still shells out to the .sh — at install time bash
//!     is guaranteed and post-install-launcher.sh has the full T1/T3/T4
//!     auto-install ladder we don't want to duplicate.
//!
//! Soft-fail throughout: icon refresh NEVER blocks an update. Failures are
//! logged via `eprintln!` and the caller continues.

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Refresh the per-user desktop shortcut(s) to point at the given launcher
/// binary. Picks the highest-resolution icon shipped with `install_path`'s
/// launcher tree.
///
/// Returns `Ok(())` on success, `Err(String)` on failure. Callers should
/// typically log and ignore — failure here is non-fatal for the update
/// flow that called us.
///
/// Per-OS behavior:
///   * Linux:   writes `$XDG_DATA_HOME/applications/vct-launcher.desktop`
///              (defaulting to `~/.local/share/applications`), AND copies
///              it to `$HOME/Desktop/vct-launcher.desktop` when `~/Desktop`
///              exists.
///   * macOS:   refreshes the `$HOME/Applications/<bundle>` symlink when
///              `launcher_binary` is inside a `.app` bundle. No-op
///              otherwise (bare binary, e.g. `cargo run` / dev build).
///   * Windows: TODO — needs a separate PowerShell helper to write a .lnk.
///              Current behavior: returns Ok(()) without writing anything
///              so the caller's soft-fail discipline stays clean.
pub fn refresh_desktop_shortcut(
    install_path: &Path,
    launcher_binary: &Path,
) -> Result<(), String> {
    if std::env::var("VCT_NO_DESKTOP_ICON").as_deref() == Ok("1") {
        return Ok(());
    }
    #[cfg(target_os = "linux")]
    {
        return refresh_linux(install_path, launcher_binary);
    }
    #[cfg(target_os = "macos")]
    {
        let _ = install_path;
        return refresh_macos(launcher_binary);
    }
    #[cfg(target_os = "windows")]
    {
        // No-op for now. Windows shortcut creation requires PowerShell
        // (CreateShortcut COM call); first-install.bat already handles
        // initial creation, and refresh-on-update is best deferred to a
        // dedicated PS helper. See KNOWN_ISSUES.md.
        let _ = (install_path, launcher_binary);
        return Ok(());
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        let _ = (install_path, launcher_binary);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Linux
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
fn refresh_linux(install_path: &Path, launcher_binary: &Path) -> Result<(), String> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "HOME not set".to_string())?;
    let apps_dir = match std::env::var_os("XDG_DATA_HOME") {
        Some(d) if !d.is_empty() => PathBuf::from(d).join("applications"),
        _ => home.join(".local/share/applications"),
    };
    std::fs::create_dir_all(&apps_dir)
        .map_err(|e| format!("create {}: {}", apps_dir.display(), e))?;

    let icon_path = pick_linux_icon(install_path);
    let body = build_desktop_entry(launcher_binary, icon_path.as_deref());

    let primary = apps_dir.join("vct-launcher.desktop");
    write_desktop_file(&primary, &body)
        .map_err(|e| format!("write {}: {}", primary.display(), e))?;

    // Optional copy to ~/Desktop/ — only if the directory exists. We do
    // NOT create it (some users intentionally have no Desktop folder).
    let desktop_dir = home.join("Desktop");
    if desktop_dir.is_dir() {
        let dest = desktop_dir.join("vct-launcher.desktop");
        if let Err(e) = write_desktop_file(&dest, &body) {
            eprintln!(
                "[desktop_shortcut] failed to write {}: {} (continuing)",
                dest.display(),
                e
            );
        }
    }

    // Best-effort: refresh the desktop-entry cache so the new entry
    // appears in the system menu without a re-login. Failure is
    // intentionally swallowed — the file is already written, which is
    // the contract.
    let _ = std::process::Command::new("update-desktop-database")
        .arg(&apps_dir)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();

    Ok(())
}

#[cfg(target_os = "linux")]
fn pick_linux_icon(install_path: &Path) -> Option<PathBuf> {
    let icon_root = install_path.join("launcher/src-tauri/icons");
    // Order matches post-install-launcher.sh: prefer the canonical
    // multi-size icon.png, then the 256@2x, then 128@2x, then 128.
    let candidates = [
        "icon.png",
        "256x256@2x.png",
        "128x128@2x.png",
        "128x128.png",
    ];
    for name in candidates {
        let p = icon_root.join(name);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

/// Build the `.desktop` file body. Format matches post-install-launcher.sh
/// EXACTLY so reinstall over a launcher-refresh-written file is a no-op.
fn build_desktop_entry(launcher_binary: &Path, icon_path: Option<&Path>) -> String {
    let exec = launcher_binary.display().to_string();
    let icon_line = match icon_path {
        Some(p) => format!("Icon={}\n", p.display()),
        None => String::new(),
    };
    format!(
        "[Desktop Entry]\n\
         Type=Application\n\
         Name=VCT Launcher\n\
         GenericName=VibeCoded Tools Launcher\n\
         Comment=Project manager + KG/codegraph dashboards for vibecoded-orchestrator\n\
         Exec=\"{}\"\n\
         {}Terminal=false\n\
         Categories=Development;IDE;\n\
         StartupWMClass=vct-launcher\n\
         StartupNotify=true\n",
        exec, icon_line
    )
}

/// Write the .desktop body. On Unix we also chmod +x so the file-manager
/// recognizes it as launchable (GNOME 42+ still requires "Allow Launching"
/// on first use, but the executable bit is a prerequisite).
fn write_desktop_file(path: &Path, body: &str) -> std::io::Result<()> {
    // Atomic-ish write: write to a sibling .tmp, then rename. This avoids a
    // partially-written .desktop file confusing the desktop environment if
    // we crash mid-write.
    let mut tmp = path.to_path_buf();
    // Preserve parent + filename, just swap the extension. Using
    // `set_extension("desktop.tmp")` on a `.desktop` file yields
    // `.desktop.tmp` per std::path semantics.
    tmp.set_extension("desktop.tmp");
    std::fs::write(&tmp, body.as_bytes())?;
    #[cfg(unix)]
    {
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755))?;
    }
    std::fs::rename(&tmp, path)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// macOS
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
fn refresh_macos(launcher_binary: &Path) -> Result<(), String> {
    // Only meaningful when the launcher is inside a `.app` bundle. Bare
    // binaries (dev builds, cargo runs) don't have an .app to symlink.
    let bin_str = launcher_binary.display().to_string();
    let Some(app_root) = bin_str.find("/Contents/MacOS/").map(|idx| {
        PathBuf::from(&bin_str[..idx])
    }) else {
        return Ok(());
    };
    if !app_root.is_dir() {
        return Ok(());
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "HOME not set".to_string())?;
    let apps_dir = home.join("Applications");
    std::fs::create_dir_all(&apps_dir)
        .map_err(|e| format!("create {}: {}", apps_dir.display(), e))?;
    let bundle_name = app_root
        .file_name()
        .ok_or_else(|| format!("no basename for {}", app_root.display()))?;
    let link = apps_dir.join(bundle_name);
    if link.exists() || link.symlink_metadata().is_ok() {
        let _ = std::fs::remove_file(&link);
    }
    std::os::unix::fs::symlink(&app_root, &link)
        .map_err(|e| format!("symlink {} -> {}: {}", link.display(), app_root.display(), e))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// The .desktop body must include the binary path verbatim and the
    /// canonical "Name=VCT Launcher" line. install.py-side post-install-
    /// launcher.sh writes the same fields, so a launcher-side refresh
    /// must not silently change the contract that the user's file
    /// manager has indexed.
    #[test]
    fn desktop_entry_contains_exec_and_name() {
        let bin = Path::new("/home/u/.local/share/vct-launcher/vct-launcher");
        let body = build_desktop_entry(bin, None);
        assert!(body.contains("Name=VCT Launcher"));
        assert!(body.contains(
            "Exec=\"/home/u/.local/share/vct-launcher/vct-launcher\""
        ));
        assert!(body.contains("Type=Application"));
        assert!(body.contains("Categories=Development;IDE;"));
        // No icon line when icon path is None.
        assert!(!body.contains("Icon="));
    }

    #[test]
    fn desktop_entry_includes_icon_when_provided() {
        let bin = Path::new("/usr/bin/vct-launcher");
        let icon = PathBuf::from("/opt/vct/launcher/src-tauri/icons/icon.png");
        let body = build_desktop_entry(bin, Some(&icon));
        assert!(body.contains(
            "Icon=/opt/vct/launcher/src-tauri/icons/icon.png"
        ));
    }

    #[test]
    fn desktop_entry_paths_with_spaces_are_quoted() {
        // The user's install path may contain spaces (macOS-style or
        // intentional). Exec= field uses quotes so the spec-compliant
        // parser in the DE doesn't tokenize "VCT Launcher.app" into two
        // argv entries.
        let bin = Path::new("/home/user/My Apps/vct-launcher");
        let body = build_desktop_entry(bin, None);
        assert!(body.contains("Exec=\"/home/user/My Apps/vct-launcher\""));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn refresh_linux_writes_into_xdg_data_home() {
        let tmp = tempfile::tempdir().unwrap();
        // Build a fake install layout with an icon file so pick_linux_icon
        // finds something.
        let icons_dir = tmp.path().join("launcher/src-tauri/icons");
        std::fs::create_dir_all(&icons_dir).unwrap();
        std::fs::write(icons_dir.join("icon.png"), b"\x89PNG\r\n").unwrap();

        // Re-root HOME + XDG_DATA_HOME to the tmpdir so we don't write into
        // the developer's real ~/.local/share.
        let home = tmp.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
        std::fs::create_dir_all(home.join("Desktop")).unwrap();
        let xdg = tmp.path().join("xdg");

        // env::set_var is global to the process — serialize the
        // XDG/HOME-mutating tests via the dedicated mutex below.
        let _g = crate::secrets::test_serialize::keychain_serialize_lock();
        let prev_home = std::env::var_os("HOME");
        let prev_xdg = std::env::var_os("XDG_DATA_HOME");
        let prev_optout = std::env::var_os("VCT_NO_DESKTOP_ICON");
        std::env::set_var("HOME", &home);
        std::env::set_var("XDG_DATA_HOME", &xdg);
        std::env::remove_var("VCT_NO_DESKTOP_ICON");

        let bin = tmp.path().join("launcher/src-tauri/target/release/vct-launcher");
        std::fs::create_dir_all(bin.parent().unwrap()).unwrap();
        std::fs::write(&bin, b"#!/bin/sh\necho launcher\n").unwrap();

        let result = refresh_desktop_shortcut(tmp.path(), &bin);

        // Restore env BEFORE asserting so a panic doesn't leak.
        match prev_home { Some(v) => std::env::set_var("HOME", v), None => std::env::remove_var("HOME") }
        match prev_xdg  { Some(v) => std::env::set_var("XDG_DATA_HOME", v), None => std::env::remove_var("XDG_DATA_HOME") }
        match prev_optout { Some(v) => std::env::set_var("VCT_NO_DESKTOP_ICON", v), None => std::env::remove_var("VCT_NO_DESKTOP_ICON") }

        result.expect("refresh should succeed");
        let primary = xdg.join("applications/vct-launcher.desktop");
        assert!(primary.is_file(), "expected primary entry at {}", primary.display());
        let body = std::fs::read_to_string(&primary).unwrap();
        assert!(body.contains("Exec=\""));
        assert!(body.contains("Icon="));

        let on_desktop = home.join("Desktop/vct-launcher.desktop");
        assert!(on_desktop.is_file(), "expected Desktop copy at {}", on_desktop.display());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn refresh_linux_respects_vct_no_desktop_icon_env() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        std::fs::create_dir_all(&home).unwrap();

        let _g = crate::secrets::test_serialize::keychain_serialize_lock();
        let prev_home = std::env::var_os("HOME");
        let prev_optout = std::env::var_os("VCT_NO_DESKTOP_ICON");
        std::env::set_var("HOME", &home);
        std::env::set_var("VCT_NO_DESKTOP_ICON", "1");

        let bin = tmp.path().join("vct-launcher");
        std::fs::write(&bin, b"#!/bin/sh\n").unwrap();
        let result = refresh_desktop_shortcut(tmp.path(), &bin);

        match prev_home { Some(v) => std::env::set_var("HOME", v), None => std::env::remove_var("HOME") }
        match prev_optout { Some(v) => std::env::set_var("VCT_NO_DESKTOP_ICON", v), None => std::env::remove_var("VCT_NO_DESKTOP_ICON") }

        assert!(result.is_ok(), "opt-out must not error");
        // No .desktop file should have been created anywhere we look.
        let on_desktop = home.join("Desktop/vct-launcher.desktop");
        assert!(!on_desktop.exists(), "opt-out must skip Desktop write");
    }

    // HOME/XDG_DATA_HOME mutations are serialized via the shared
    // `crate::secrets::test_serialize::keychain_serialize_lock` mutex so
    // we don't race against the dashboard tests' EnvGuard (which also
    // mutates HOME). A module-private mutex would only block intra-module
    // races and would still race vs dashboard::tests cross-module — that
    // race was the cause of a flaky cargo-test pre-merge.
}
