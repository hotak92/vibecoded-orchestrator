// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! WebKitGTK + GPU EGL pre-flight probe (v0.2.26).
//!
//! ## Why this exists
//!
//! WebKitGTK ≥2.42 made the DMABUF renderer the default. The DMABUF
//! path opens `/dev/dri/renderD128`, wraps it via `libgbm`, then asks
//! libEGL for a GBM platform display and calls `eglInitialize()`.
//!
//! This whole chain is robust on a healthy system. It breaks under one
//! well-known condition: the NVIDIA proprietary userspace libraries
//! have been upgraded but the matching `nvidia-dkms` kernel module
//! hasn't been reloaded yet (typical post-`apt upgrade`, pre-reboot
//! state). In that case `eglInitialize` returns `EGL_NOT_INITIALIZED`
//! and WebKitGTK aborts the entire process at startup with:
//!
//! ```text
//! Could not create GBM EGL display: EGL_NOT_INITIALIZED. Aborting...
//! ```
//!
//! It also breaks in stranger situations: half-installed Mesa,
//! container sandboxes that mount `/dev/dri` but not all the GL libs,
//! Wayland sessions with proprietary NVIDIA + explicit-sync bugs.
//!
//! ## Strategy
//!
//! Run the same EGL initialisation that WebKit will run, FIRST, in our
//! own process. If it succeeds → great, leave the environment alone
//! and let WebKit use the GPU/DMABUF fast path. If it fails → set
//! `WEBKIT_DISABLE_DMABUF_RENDERER=1` before WebKit launches, which
//! switches WebKit to its older non-DMABUF renderer. The renderer is
//! slower for video/heavy compositing but completely fine for a Tauri
//! admin GUI like ours — and the probe re-runs every launch, so the
//! moment the user fixes their driver state (reboot, swap to AMD,
//! etc.) the GPU path comes back automatically. No persistent state,
//! no manual env var, no permanent perf cost.
//!
//! Bonus: on Wayland-NVIDIA we also set `__NV_DISABLE_EXPLICIT_SYNC=1`
//! when the probe fails, mirroring `webkit2gtk-nvidia-quirk`'s
//! Wayland-side workaround — same observed-failure motivation.
//!
//! ## Trust boundary
//!
//! This module is Linux-only (`#[cfg(target_os = "linux")]` at the use
//! sites). On macOS the system WebKit doesn't have a DMABUF renderer
//! at all; on Windows WebView2 is its own beast. Both no-op cleanly.
//!
//! ## Why not the `webkit2gtk-nvidia-quirk` crate?
//!
//! Two reasons:
//! 1. It's a static "is NVIDIA + which session" check via sysfs. It
//!    sets the env var unconditionally on every NVIDIA system, even
//!    when the EGL/GBM path actually works fine — which is the common
//!    case once drivers + kernel modules are in sync. That permanently
//!    disables GPU-accelerated WebKit rendering on NVIDIA users'
//!    machines. Acceptable for some apps; the user explicitly asked
//!    that we keep the GPU path whenever it works.
//! 2. The actual failure mode isn't NVIDIA-specific — half-broken
//!    Mesa or weird sandbox EGL setups crash the same way. A direct
//!    EGL probe catches all of them; a sysfs vendor check catches
//!    only the NVIDIA subset.
//!
//! ## References
//! - Tauri Issue #9394 — Documenting Nvidia problems in Tauri.
//! - WebKit Bug #262607 — Disable DMABuf renderer for NVIDIA proprietary drivers.
//! - webkit2gtk-nvidia-quirk crate (sysfs-based alternative we chose against).
//! - VCO_dev session 2026-05-22 — root cause traced to nvidia-driver-595
//!   userspace upgrade (595.58.03 → 595.71.05) with no reboot.

#![cfg(target_os = "linux")]

use std::ffi::{c_void, CString};

// EGL constants we need. From `EGL/egl.h` / `EGL/eglplatform.h`. Hard-
// coded here because we don't want a compile-time dep on libEGL headers.
const EGL_NO_DISPLAY: *mut c_void = std::ptr::null_mut();
const EGL_TRUE: u32 = 1;
const EGL_PLATFORM_GBM_KHR: u32 = 0x31D7;

type EglDisplay = *mut c_void;
type EglBoolean = u32;
// Function signatures from the EGL spec.
type EglGetPlatformDisplayFn =
    unsafe extern "C" fn(platform: u32, native_display: *mut c_void, attribs: *const i32) -> EglDisplay;
type EglInitializeFn =
    unsafe extern "C" fn(dpy: EglDisplay, major: *mut i32, minor: *mut i32) -> EglBoolean;
type EglTerminateFn = unsafe extern "C" fn(dpy: EglDisplay) -> EglBoolean;
type EglGetErrorFn = unsafe extern "C" fn() -> i32;

/// Outcome of `probe_and_apply_workaround_if_needed`. Returned for
/// caller logging + for unit-testability.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeOutcome {
    /// EGL works — no workaround applied; WebKit will use the GPU path.
    EglHealthy,
    /// EGL is broken — `WEBKIT_DISABLE_DMABUF_RENDERER=1` was set.
    EglFailedAppliedDmabufDisable,
    /// libEGL or libgbm isn't present on this system at all. Almost
    /// certainly means WebKit will fail too, but we have nothing to
    /// do about it — let WebKit produce its own error rather than
    /// guess. Probe is a no-op.
    LibrariesMissing,
    /// User has already set `WEBKIT_DISABLE_DMABUF_RENDERER` to ANY
    /// value (even "0"). Respect their override — they know what they
    /// want. Probe is a no-op.
    UserOverrideRespected,
    /// `VCT_WEBKIT_PREFLIGHT_OFF=1` was set. Skip everything — useful
    /// for debugging the probe itself.
    DisabledByEnv,
}

/// Public entry point. Call ONCE at the very top of `main()`, before
/// any code that might touch GTK, WebKit, GL, or X11/Wayland state.
///
/// Always safe to call: never panics, never aborts. The worst case is
/// `LibrariesMissing` (a no-op).
pub fn probe_and_apply_workaround_if_needed() -> ProbeOutcome {
    // Manual kill-switch — lets us diagnose the probe itself without
    // recompiling. If the probe ever causes a regression in some
    // distro/driver combo we haven't tested, the user can disable it.
    if std::env::var_os("VCT_WEBKIT_PREFLIGHT_OFF").is_some() {
        eprintln!("[webkit-preflight] disabled via VCT_WEBKIT_PREFLIGHT_OFF — skipping probe");
        return ProbeOutcome::DisabledByEnv;
    }

    // Respect a user-set override. If they exported the env var
    // themselves (e.g. via .desktop file) we don't touch it. This also
    // makes the probe idempotent — re-running it is a no-op.
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_some() {
        eprintln!(
            "[webkit-preflight] WEBKIT_DISABLE_DMABUF_RENDERER already set — skipping probe"
        );
        return ProbeOutcome::UserOverrideRespected;
    }

    // Run the probe. The probe is `unsafe` because we're dlopening
    // libraries and calling raw C function pointers, but the unsafety
    // is fully contained — there's no way for caller-controlled input
    // to reach it, and every failure path returns cleanly.
    let result = unsafe { probe_egl_via_gbm() };

    match result {
        Ok(()) => {
            eprintln!("[webkit-preflight] EGL probe passed — WebKit will use GPU/DMABUF path");
            ProbeOutcome::EglHealthy
        }
        Err(ProbeError::LibrariesMissing(which)) => {
            eprintln!(
                "[webkit-preflight] {} not found — leaving WebKit env untouched",
                which
            );
            ProbeOutcome::LibrariesMissing
        }
        Err(ProbeError::EglInitFailed(detail)) => {
            eprintln!(
                "[webkit-preflight] EGL probe failed ({}); setting \
                 WEBKIT_DISABLE_DMABUF_RENDERER=1 to keep WebKit from \
                 aborting. The GPU path will return automatically once \
                 the underlying issue (often a pending reboot after a \
                 GPU driver upgrade) is resolved.",
                detail
            );
            // SAFETY: set_var is sound here because we're at the very
            // top of main() before ANY other thread has been spawned.
            // Tauri's runtime starts later; tokio's runtime starts
            // later. No data race possible.
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");

            // Mirror the webkit2gtk-nvidia-quirk Wayland-side
            // workaround: on Wayland-NVIDIA, also disable NVIDIA's
            // explicit-sync path which has its own (separate) bug
            // chain. Setting both is safe on non-NVIDIA Wayland (the
            // var is read only by NVIDIA's userspace).
            if is_wayland_session() {
                std::env::set_var("__NV_DISABLE_EXPLICIT_SYNC", "1");
                eprintln!(
                    "[webkit-preflight] also set __NV_DISABLE_EXPLICIT_SYNC=1 (Wayland session)"
                );
            }
            ProbeOutcome::EglFailedAppliedDmabufDisable
        }
    }
}

#[derive(Debug)]
enum ProbeError {
    /// A required shared library (libEGL.so.1 or libgbm.so.1) wasn't
    /// loadable. The string names which one for diagnostics.
    LibrariesMissing(&'static str),
    /// EGL was found but `eglInitialize` failed. The detail is the
    /// EGL error code in hex.
    EglInitFailed(String),
}

/// Run the EGL+GBM probe.
///
/// Tests EVERY render node under /dev/dri/, prioritising the one
/// attached to the `boot_vga=1` (primary) GPU. Reports failure if the
/// primary GPU's render node fails. The "test every node" semantics
/// (rather than just the primary) is intentional: WebKit's actual
/// device selection is influenced by `DRI_PRIME`, libEGL ICD priority,
/// and Mesa's heuristics — so we conservatively need EVERY plausible
/// path to work. We use `boot_vga=1` only as a hint for ORDERING, so
/// the most-likely-broken case (the primary GPU) is tested first +
/// reported with the most specific error.
///
/// SAFETY: dlopens libraries + calls C function pointers. Sound iff:
///   1. The dlopened symbols match the C signatures declared above (they
///      do — these are stable parts of the EGL ABI).
///   2. We don't keep dangling pointers past `lib_egl` / `lib_gbm` lifetime
///      (we don't — every call happens while both Library values are alive).
///   3. We're not racing with another thread's `set_var` (we aren't — see
///      the caller's pre-thread-spawn invariant).
unsafe fn probe_egl_via_gbm() -> Result<(), ProbeError> {
    // Load libEGL.so.1. Hard-required for the probe.
    let lib_egl = libloading::Library::new("libEGL.so.1")
        .map_err(|_| ProbeError::LibrariesMissing("libEGL.so.1"))?;

    // The four EGL functions we need.
    let egl_get_platform_display: libloading::Symbol<EglGetPlatformDisplayFn> =
        lib_egl
            .get(b"eglGetPlatformDisplay\0")
            .map_err(|_| ProbeError::LibrariesMissing("eglGetPlatformDisplay"))?;
    let egl_initialize: libloading::Symbol<EglInitializeFn> = lib_egl
        .get(b"eglInitialize\0")
        .map_err(|_| ProbeError::LibrariesMissing("eglInitialize"))?;
    let egl_terminate: libloading::Symbol<EglTerminateFn> = lib_egl
        .get(b"eglTerminate\0")
        .map_err(|_| ProbeError::LibrariesMissing("eglTerminate"))?;
    let egl_get_error: libloading::Symbol<EglGetErrorFn> = lib_egl
        .get(b"eglGetError\0")
        .map_err(|_| ProbeError::LibrariesMissing("eglGetError"))?;

    // Load libgbm.so.1.
    let lib_gbm = libloading::Library::new("libgbm.so.1")
        .map_err(|_| ProbeError::LibrariesMissing("libgbm.so.1"))?;

    type GbmCreateDeviceFn = unsafe extern "C" fn(fd: i32) -> *mut c_void;
    type GbmDeviceDestroyFn = unsafe extern "C" fn(gbm: *mut c_void);
    let gbm_create_device: libloading::Symbol<GbmCreateDeviceFn> = lib_gbm
        .get(b"gbm_create_device\0")
        .map_err(|_| ProbeError::LibrariesMissing("gbm_create_device"))?;
    let gbm_device_destroy: libloading::Symbol<GbmDeviceDestroyFn> = lib_gbm
        .get(b"gbm_device_destroy\0")
        .map_err(|_| ProbeError::LibrariesMissing("gbm_device_destroy"))?;

    // Enumerate render nodes, primary-GPU-first.
    let render_nodes = enumerate_render_nodes_primary_first();
    if render_nodes.is_empty() {
        return Err(ProbeError::LibrariesMissing("/dev/dri/renderD*"));
    }

    // Test each. Bail on the FIRST failure — that's enough to know
    // WebKit will trip too, and gives us the most-specific error
    // message. (Testing all would be valid too; we go with first-fail
    // for log brevity.)
    for (path, is_primary) in &render_nodes {
        let result = probe_one_render_node(
            path,
            *is_primary,
            &gbm_create_device,
            &gbm_device_destroy,
            &egl_get_platform_display,
            &egl_initialize,
            &egl_terminate,
            &egl_get_error,
        );
        if let Err(e) = result {
            // Report with the path that failed for diagnostics.
            return Err(e);
        }
    }

    Ok(())
}

/// Probe one specific render node. Factored out so each iteration has
/// clean RAII guards for its fd + gbm device.
#[allow(clippy::too_many_arguments)]
unsafe fn probe_one_render_node(
    path: &std::path::Path,
    is_primary: bool,
    gbm_create_device: &libloading::Symbol<unsafe extern "C" fn(fd: i32) -> *mut c_void>,
    gbm_device_destroy: &libloading::Symbol<unsafe extern "C" fn(gbm: *mut c_void)>,
    egl_get_platform_display: &libloading::Symbol<EglGetPlatformDisplayFn>,
    egl_initialize: &libloading::Symbol<EglInitializeFn>,
    egl_terminate: &libloading::Symbol<EglTerminateFn>,
    egl_get_error: &libloading::Symbol<EglGetErrorFn>,
) -> Result<(), ProbeError> {
    let path_str = path.to_string_lossy();
    let primary_tag = if is_primary { " (primary GPU)" } else { "" };

    // Open the render node. O_RDWR | O_CLOEXEC.
    let c_path =
        CString::new(path_str.as_bytes()).map_err(|_| ProbeError::LibrariesMissing("path"))?;
    let fd = libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC);
    if fd < 0 {
        // EACCES on /dev/dri/renderD* is unusual but not fatal — it
        // means the user isn't in the `render` group. Don't trigger
        // the workaround for permission errors; WebKit will skip
        // this node too.
        let errno = *libc::__errno_location();
        if errno == libc::EACCES {
            eprintln!(
                "[webkit-preflight] {} not openable (EACCES) — skipping; \
                 user may not be in the 'render' group",
                path_str
            );
            return Ok(());
        }
        return Err(ProbeError::EglInitFailed(format!(
            "open({}) failed (errno={}){}",
            path_str, errno, primary_tag
        )));
    }
    struct FdGuard(i32);
    impl Drop for FdGuard {
        fn drop(&mut self) {
            unsafe {
                libc::close(self.0);
            }
        }
    }
    let _fd_guard = FdGuard(fd);

    let gbm = gbm_create_device(fd);
    if gbm.is_null() {
        return Err(ProbeError::EglInitFailed(format!(
            "gbm_create_device({}) returned NULL{}",
            path_str, primary_tag
        )));
    }
    struct GbmGuard<'a> {
        ptr: *mut c_void,
        destroy: &'a libloading::Symbol<'a, unsafe extern "C" fn(gbm: *mut c_void)>,
    }
    impl Drop for GbmGuard<'_> {
        fn drop(&mut self) {
            unsafe {
                (self.destroy)(self.ptr);
            }
        }
    }
    let _gbm_guard = GbmGuard {
        ptr: gbm,
        destroy: gbm_device_destroy,
    };

    let dpy = egl_get_platform_display(EGL_PLATFORM_GBM_KHR, gbm, std::ptr::null());
    if dpy == EGL_NO_DISPLAY {
        let err = egl_get_error();
        return Err(ProbeError::EglInitFailed(format!(
            "eglGetPlatformDisplay({}) returned EGL_NO_DISPLAY (EGL error 0x{:04X}){}",
            path_str, err, primary_tag
        )));
    }

    let mut major: i32 = 0;
    let mut minor: i32 = 0;
    if egl_initialize(dpy, &mut major, &mut minor) != EGL_TRUE {
        let err = egl_get_error();
        return Err(ProbeError::EglInitFailed(format!(
            "eglInitialize({}) failed (EGL error 0x{:04X} — \
             0x3001 is EGL_NOT_INITIALIZED, the GPU driver mismatch / \
             DRI2 backend incompatibility signature){}",
            path_str, err, primary_tag
        )));
    }

    egl_terminate(dpy);
    Ok(())
}

/// Return every `/dev/dri/renderD*` path on the system, primary-GPU
/// first. The primary GPU is identified by walking `/sys/class/drm/*`
/// and finding the card with `boot_vga=1`.
fn enumerate_render_nodes_primary_first() -> Vec<(std::path::PathBuf, bool)> {
    let dri_dir = match std::fs::read_dir("/dev/dri") {
        Ok(d) => d,
        Err(_) => return vec![],
    };
    let mut nodes: Vec<std::path::PathBuf> = dri_dir
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("renderD"))
        })
        .collect();
    nodes.sort();

    // Discover the primary card → its render node mapping. The kernel
    // exposes both `card<N>` and `renderD<N+128>` for the same physical
    // device, but `<N>` numbering isn't strictly aligned, so we read
    // the symlink target for each renderD entry to find the card it
    // belongs to.
    let primary_card_path = find_primary_card_sysfs_path();
    let primary_render_node = primary_card_path.and_then(render_node_for_card);

    nodes
        .into_iter()
        .map(|p| {
            let is_primary = primary_render_node
                .as_ref()
                .map(|pn| pn == &p)
                .unwrap_or(false);
            (p, is_primary)
        })
        .collect::<Vec<_>>()
        .into_iter()
        .fold(Vec::new(), |mut acc, item| {
            // Primary first, then the rest in alpha order.
            if item.1 {
                acc.insert(0, item);
            } else {
                acc.push(item);
            }
            acc
        })
}

/// Find `/sys/class/drm/card<N>` where `boot_vga` is `1`. Returns the
/// path to that card's sysfs directory, e.g. `/sys/class/drm/card2`.
fn find_primary_card_sysfs_path() -> Option<std::path::PathBuf> {
    for entry in std::fs::read_dir("/sys/class/drm").ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        // Only top-level cardN, skip cardN-DP-X / cardN-HDMI-X etc.
        if !(name.starts_with("card") && !name.contains('-')) {
            continue;
        }
        let boot_vga_path = entry.path().join("device/boot_vga");
        if let Ok(contents) = std::fs::read_to_string(&boot_vga_path) {
            if contents.trim() == "1" {
                return Some(entry.path());
            }
        }
    }
    None
}

/// Given a sysfs card path like `/sys/class/drm/card2`, find the
/// matching `/dev/dri/renderD*` node by listing the card's drm
/// subdirectory.
fn render_node_for_card(card_path: std::path::PathBuf) -> Option<std::path::PathBuf> {
    let drm_dir = card_path.join("device/drm");
    for entry in std::fs::read_dir(&drm_dir).ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy().into_owned();
        if name.starts_with("renderD") {
            return Some(std::path::PathBuf::from("/dev/dri").join(name));
        }
    }
    None
}

/// Cheap check for "are we on a Wayland session". Matches both
/// `WAYLAND_DISPLAY` (set by the compositor) and the older
/// `XDG_SESSION_TYPE=wayland`. Never panics.
fn is_wayland_session() -> bool {
    std::env::var_os("WAYLAND_DISPLAY").is_some()
        || std::env::var_os("XDG_SESSION_TYPE")
            .map(|v| v.eq_ignore_ascii_case("wayland"))
            .unwrap_or(false)
}

// ─── Tests ────────────────────────────────────────────────────────────
//
// The probe is hardware-dependent — proper integration tests need
// either real or simulated EGL state. We unit-test what we can: the
// env-respect logic + the Wayland-session heuristic. The actual EGL
// probe is verified by manual testing on the dev box (broken-NVIDIA
// state today + healthy state post-reboot).

#[cfg(test)]
mod tests {
    use super::*;

    /// `is_wayland_session` returns false in an environment that
    /// declares neither WAYLAND_DISPLAY nor XDG_SESSION_TYPE=wayland.
    /// We clear both env vars locally to avoid leaking the test
    /// runner's session state into the assertion.
    #[test]
    fn is_wayland_session_returns_false_when_no_wayland_env() {
        // SAFETY: tests run in serial within this module by default;
        // we restore env on drop.
        struct EnvGuard {
            saved_wd: Option<std::ffi::OsString>,
            saved_xs: Option<std::ffi::OsString>,
        }
        impl Drop for EnvGuard {
            fn drop(&mut self) {
                if let Some(v) = self.saved_wd.take() {
                    std::env::set_var("WAYLAND_DISPLAY", v);
                } else {
                    std::env::remove_var("WAYLAND_DISPLAY");
                }
                if let Some(v) = self.saved_xs.take() {
                    std::env::set_var("XDG_SESSION_TYPE", v);
                } else {
                    std::env::remove_var("XDG_SESSION_TYPE");
                }
            }
        }
        let _guard = EnvGuard {
            saved_wd: std::env::var_os("WAYLAND_DISPLAY"),
            saved_xs: std::env::var_os("XDG_SESSION_TYPE"),
        };
        std::env::remove_var("WAYLAND_DISPLAY");
        std::env::set_var("XDG_SESSION_TYPE", "x11");
        assert!(!is_wayland_session(), "x11 session should not be detected as Wayland");
    }

    /// User-set override is respected — the probe returns
    /// `UserOverrideRespected` without touching env or doing any EGL
    /// work.
    #[test]
    fn user_override_short_circuits_probe() {
        struct EnvGuard {
            saved: Option<std::ffi::OsString>,
        }
        impl Drop for EnvGuard {
            fn drop(&mut self) {
                if let Some(v) = self.saved.take() {
                    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", v);
                } else {
                    std::env::remove_var("WEBKIT_DISABLE_DMABUF_RENDERER");
                }
            }
        }
        let _guard = EnvGuard {
            saved: std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER"),
        };
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        // Also need to clear the OFF kill-switch so we don't short-
        // circuit on a different path.
        let prev_off = std::env::var_os("VCT_WEBKIT_PREFLIGHT_OFF");
        std::env::remove_var("VCT_WEBKIT_PREFLIGHT_OFF");

        assert_eq!(
            probe_and_apply_workaround_if_needed(),
            ProbeOutcome::UserOverrideRespected
        );

        if let Some(v) = prev_off {
            std::env::set_var("VCT_WEBKIT_PREFLIGHT_OFF", v);
        }
    }

    /// VCT_WEBKIT_PREFLIGHT_OFF disables the probe entirely.
    #[test]
    fn vct_off_kill_switch_disables_probe() {
        struct EnvGuard {
            saved_off: Option<std::ffi::OsString>,
            saved_render: Option<std::ffi::OsString>,
        }
        impl Drop for EnvGuard {
            fn drop(&mut self) {
                if let Some(v) = self.saved_off.take() {
                    std::env::set_var("VCT_WEBKIT_PREFLIGHT_OFF", v);
                } else {
                    std::env::remove_var("VCT_WEBKIT_PREFLIGHT_OFF");
                }
                if let Some(v) = self.saved_render.take() {
                    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", v);
                } else {
                    std::env::remove_var("WEBKIT_DISABLE_DMABUF_RENDERER");
                }
            }
        }
        let _guard = EnvGuard {
            saved_off: std::env::var_os("VCT_WEBKIT_PREFLIGHT_OFF"),
            saved_render: std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER"),
        };
        std::env::set_var("VCT_WEBKIT_PREFLIGHT_OFF", "1");
        std::env::remove_var("WEBKIT_DISABLE_DMABUF_RENDERER");

        assert_eq!(
            probe_and_apply_workaround_if_needed(),
            ProbeOutcome::DisabledByEnv
        );

        // Probe must NOT have set the render env var.
        assert!(std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none());
    }
}
