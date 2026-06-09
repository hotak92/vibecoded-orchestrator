---
title: WebKit EGL Pre-Flight Probe
type: concept
tags: [low-level-implementation, VCT-Launcher, webkit, nvidia, cross-os, linux, runtime-detection, implemented]
created: 2026-05-22T18:20:00Z
updated: 2026-05-22T18:20:00Z
valid_from: 2026-05-22T00:00:00Z
valid_until: null
status: active
---

# WebKit EGL Pre-Flight Probe

Linux-only startup probe (`launcher/src-tauri/src/webkit_preflight.rs`, 589 LOC, v0.2.26) that detects WebKitGTK's GBM/EGL failure mode BEFORE Tauri spawns its webview, and sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` only when the failure is actually about to happen. Self-healing: re-runs every launch, so once the underlying driver state is fixed, the GPU/DMABUF fast path returns automatically.

## Problem statement

WebKitGTK ≥2.42 made the DMABUF renderer the default. The DMABUF path opens `/dev/dri/renderD128` (or another render node), wraps it via `libgbm`, asks `libEGL` for a `EGL_PLATFORM_GBM_KHR` display, and calls `eglInitialize()`. On a healthy system this chain succeeds; the WebView gets GPU-accelerated rendering.

The chain breaks reliably under one well-known condition: **NVIDIA proprietary userspace libraries have been upgraded but the matching `nvidia-dkms` kernel module hasn't been reloaded yet** (typical post-`apt upgrade`, pre-reboot state). `eglInitialize` returns `EGL_NOT_INITIALIZED` (`0x3001`) and WebKitGTK aborts the entire process at startup with:

```
Could not create GBM EGL display: EGL_NOT_INITIALIZED. Aborting...
```

The launcher dies before painting a single pixel; the user sees no window.

It also breaks in stranger situations: half-installed Mesa, container sandboxes that mount `/dev/dri` but not all the GL libraries, Wayland sessions with the proprietary NVIDIA driver's explicit-sync bug chain.

## Symptom-vs-cause matrix

The crucial diagnostic insight: **the launcher binary is not at fault**. The Tauri binary, the WebKit version, the launcher's own code — none of them changed between the working launch and the broken launch. What changed is the userspace EGL/GBM library stack between launches. The same binary works fine after a reboot.

The bug is in the SYSTEM driver state, not the app. This rules out app-side fixes that would treat the bug as a launcher regression (downgrading WebKitGTK, pinning a Tauri version, removing the DMABUF renderer in the build).

The live verification case from 2026-05-22: `apt` upgraded `nvidia-driver-595` from 595.58.03 → 595.71.05 with no reboot. The launcher started failing with the abort above. The probe (added in the same session) correctly identified `/dev/dri/renderD129` as the primary GPU's node, caught `eglInitialize` returning `0x3001`, set `WEBKIT_DISABLE_DMABUF_RENDERER=1`. The launcher then started normally. After a reboot, the probe will pass and the GPU path will return automatically.

## Why "set the env var unconditionally" is wrong

The obvious app-side fix is to set `WEBKIT_DISABLE_DMABUF_RENDERER=1` unconditionally in `main()`. This would solve the abort, but it would also permanently disable GPU-accelerated WebKit rendering for **every** NVIDIA user, even when their drivers are perfectly healthy. The DMABUF renderer is the modern fast path; disabling it forces WebKit onto its older non-DMABUF compositor, which is slower for video, heavy CSS, and animations.

The user explicitly asked to **keep the GPU path whenever possible** — a launcher admin GUI doesn't strictly need it, but blanket-disabling is a permanent degradation imposed on every NVIDIA user for a transient condition that affects ~1% of launches at most.

## Why the existing `webkit2gtk-nvidia-quirk` crate isn't right either

The published `webkit2gtk-nvidia-quirk` crate does a sysfs static-detection: "is there an NVIDIA GPU on this system?" → set `WEBKIT_DISABLE_DMABUF_RENDERER=1` unconditionally. Two problems:

1. **Same permanent-degradation issue.** Once flagged NVIDIA, every launch on that machine disables DMABUF, even when drivers are healthy. It's an opt-OUT-once rather than detect-broken-state.
2. **Misses non-NVIDIA failure modes.** A user with a half-installed Mesa, a flatpak'd Tauri app with a partial GL stack, a container sandbox that doesn't expose all `/dev/dri` nodes — all see the same `eglInitialize` abort, none would trigger the sysfs vendor check. A direct EGL probe catches every actual failure mode; a vendor check catches only the NVIDIA subset.

The probe was chosen over the crate for these reasons. It's also pure stdlib + `libloading` — no extra build-time dependency.

## The probe — what it does

Linux-only (`#![cfg(target_os = "linux")]`). Run from `main()` BEFORE any code that might touch GTK, WebKit, GL, or X11/Wayland state. Always safe to call: never panics, never aborts. Returns a `ProbeOutcome` enum for caller logging.

Pseudocode:

```
1. If VCT_WEBKIT_PREFLIGHT_OFF is set → return DisabledByEnv (kill-switch).
2. If WEBKIT_DISABLE_DMABUF_RENDERER is already set by the user → return UserOverrideRespected.
3. dlopen libEGL.so.1 + libgbm.so.1 via libloading.
   If either is missing → return LibrariesMissing (probably WebKit will fail too,
   but nothing we can do — let WebKit produce its own error).
4. Enumerate /dev/dri/renderD* nodes.
   Sort with the boot_vga=1 GPU's render node first (sysfs walk).
5. For each node:
     a. open(path, O_RDWR | O_CLOEXEC). EACCES → skip (user not in 'render' group).
     b. gbm_create_device(fd). NULL → failure.
     c. eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm, NULL). EGL_NO_DISPLAY → failure.
     d. eglInitialize(dpy, &major, &minor). != EGL_TRUE → failure (report EGL error code).
     e. eglTerminate(dpy).
6. If ANY node failed → set WEBKIT_DISABLE_DMABUF_RENDERER=1.
   If Wayland session → ALSO set __NV_DISABLE_EXPLICIT_SYNC=1
     (mirrors webkit2gtk-nvidia-quirk's Wayland workaround for NVIDIA's
      separate explicit-sync bug chain; safe on non-NVIDIA Wayland because
      only NVIDIA's userspace reads the var).
7. Return EglHealthy or EglFailedAppliedDmabufDisable.
```

The "test EVERY render node" semantics (rather than just the primary) is intentional: WebKit's actual device selection is influenced by `DRI_PRIME`, libEGL ICD priority, and Mesa's heuristics. We conservatively need every plausible device path to work. `boot_vga=1` is used only as an ORDERING hint, so the most-likely-broken case (the primary GPU) is tested first and reported with the most-specific error.

`probe_one_render_node()` uses RAII guards (`FdGuard`, `GbmGuard`) so each iteration cleans up its fd and gbm device even on early return.

## Self-healing property

The probe runs every launch. Reboots, driver fixes, kernel-module reloads, hardware swaps, container-sandbox config changes — all are dynamically reflected. The state set last launch (env var) does not persist across launches: `std::env::set_var` only affects the current process. The probe makes a fresh decision every time. There's no persistent flag in launcher.db, no stale-cache failure mode, no manual reset for the user.

This is a deliberate departure from sysfs-detection libraries that imply "once NVIDIA, always degraded". The probe's worldview is "broken right now → degrade right now; healthy right now → fast path right now".

## Kill-switch + user-override semantics

Two escape hatches:

- **`VCT_WEBKIT_PREFLIGHT_OFF=1`** — disables the probe entirely. Diagnostic use only — e.g. if the probe itself ever causes a regression on a distro/driver combo we haven't tested, the user can disable it from the launcher's environment file and let WebKit produce its own error.
- **User has already set `WEBKIT_DISABLE_DMABUF_RENDERER` to ANY value** (even `"0"`) — the probe is a no-op. The user knows what they want; we respect it. This also makes the probe idempotent — re-running it is harmless.

Both are checked before the probe's dlopen path, so they're cheap.

## Why `dlopen` (libloading) instead of the `khronos-egl` crate

The probe runtime-loads `libEGL.so.1` and `libgbm.so.1` via `libloading::Library::new` rather than linking against them at build time. WHY:

1. **Graceful degradation when libraries are missing.** A minimal container or stripped-down distro (rare but real) might not ship libEGL/libgbm. With a `khronos-egl` build-time dep, the launcher fails to link / fails at load with a confusing `cannot open shared object` error. With dlopen, the missing-library case is a clean `ProbeOutcome::LibrariesMissing` and the probe is a no-op.
2. **No EGL header dependency at build time.** The four function signatures (`eglGetPlatformDisplay`, `eglInitialize`, `eglTerminate`, `eglGetError`) are declared inline in the source. The four EGL constants (`EGL_NO_DISPLAY`, `EGL_TRUE`, `EGL_PLATFORM_GBM_KHR`, etc.) are hardcoded from the EGL spec. These are stable ABI parts — they don't drift.
3. **Probe-only use.** The launcher doesn't actually USE OpenGL/EGL for anything else; the probe is the only consumer. Pulling in a full EGL binding crate for one-shot probe code is overweight.

The unsafe surface is contained in `probe_egl_via_gbm()` and `probe_one_render_node()`. Soundness notes are in the source — every dlopened symbol is matched against a stable EGL ABI signature, no dangling pointers escape the function lifetime, and the `set_var` at the end runs before any other thread spawns (Tauri's runtime starts later).

## Cross-OS behavior

The whole module is `#![cfg(target_os = "linux")]`. On other platforms it's compiled out entirely:

- **macOS** — uses Apple's WebKit. No DMABUF renderer, no GBM, no EGL. The variable `WEBKIT_DISABLE_DMABUF_RENDERER` is ignored by Apple's WebKit. Probe is irrelevant; module doesn't compile in.
- **Windows** — Tauri uses WebView2 (Microsoft Edge / Chromium). No DMABUF, no EGL. Same outcome: module doesn't compile in.

Linux is the only OS with the GBM+EGL WebKit code path, so it's the only OS that needs the probe. The cross-OS contract is "no-op on non-Linux", achieved by `#[cfg]` rather than runtime branching — zero binary footprint on macOS/Windows builds.

## Why this isn't in vct-launcher-core

The probe is in `launcher/src-tauri/src/webkit_preflight.rs`, NOT in the reusable `vct-launcher-core` crate. WHY: it's WebKit/Tauri-specific. `vct-launcher-core` is the platform-agnostic logic crate (manifest parsing, DB access, services); the WebKit probe is glue between the Tauri binary and the system EGL stack. Keeping it in the Tauri shell crate matches the dependency direction (Tauri-shell depends on core, never the reverse).

## Live verification log (2026-05-22)

The probe was added during the v0.2.26 dispatcher session and exercised against a real failure on the dev machine:

```
[webkit-preflight] EGL probe failed (eglInitialize(/dev/dri/renderD129) failed
  (EGL error 0x3001 — 0x3001 is EGL_NOT_INITIALIZED, the GPU driver mismatch /
  DRI2 backend incompatibility signature) (primary GPU)); setting
  WEBKIT_DISABLE_DMABUF_RENDERER=1 to keep WebKit from aborting. The GPU path
  will return automatically once the underlying issue (often a pending reboot
  after a GPU driver upgrade) is resolved.
[webkit-preflight] also set __NV_DISABLE_EXPLICIT_SYNC=1 (Wayland session)
```

The launcher then started and rendered the GUI. After a system reboot (next session), the probe is expected to pass and the GPU path to return — no manual env-var unset required.

## Open questions / future work

- **Should the probe write its outcome to a launcher.db diagnostics row?** Currently outcomes are only logged to stderr. A row would let the Diagnostics tab show "EGL probe outcome: HEALTHY (last 5 launches)" — but adding a DB write on every startup is overweight for what's essentially a stderr log. Defer to a separate Services-tab follow-up.
- **Multi-monitor + heterogenous GPUs.** The "test every render node" logic handles multi-GPU systems correctly, but the failure mode where ONE GPU is broken and the user explicitly wants WebKit to use the OTHER (via `DRI_PRIME=1`) isn't currently distinguished. The current probe would disable DMABUF if any node fails. Acceptable for v0.2.26 (rare config); could be refined later by checking `DRI_PRIME` env var.

## References

- Tauri Issue #9394 — Documenting NVIDIA problems in Tauri.
- WebKit Bug #262607 — Disable DMABuf renderer for NVIDIA proprietary drivers.
- `webkit2gtk-nvidia-quirk` crate — the sysfs-based alternative we chose against.
- Source: `launcher/src-tauri/src/webkit_preflight.rs` (589 LOC, inline rationale).

## Related

- [[relatedTo::Cross-OS Hook Portability]] — sibling concept on Linux/macOS/Windows divergence; the probe uses `#[cfg(target_os)]` to enforce Linux-only compilation while macOS/Windows get zero footprint.
- [[relatedTo::GPU Mode Decision Policy]] — adjacent runtime-detection pattern (picking GPU variant per container at start time); the probe is the same family of "runtime-detected hardware capability" decision, applied to a different surface.
- [[uses::Tauri 2]] — the consumer that aborts if the probe doesn't run first.
- [[buildsOn::Runtime-Detected Capability Pattern]] — generic pattern: probe the actual capability you need, don't infer from vendor IDs.
