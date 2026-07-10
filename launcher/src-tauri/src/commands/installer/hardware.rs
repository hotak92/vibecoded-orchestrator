//! Hardware & runtime probes for the installer/wizard flow.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the GPU enumeration, RAM
//! detection, PCI/lspci parsing, and command/python/runtime probes that
//! previously lived inline in `installer.rs`. Behaviour is unchanged; the
//! facade re-exports every symbol so existing call-sites and the
//! `installer::tests` module keep resolving them via `super::*`.
//!
//! CROSS-LANGUAGE PARITY NOTE: `detect_python`'s POSIX `vec![...]` python
//! candidate list is one leg of the three-way mirror locked by
//! `tests/test_python_candidate_parity.py` (install.sh / install.ps1 /
//! this file). The parity test globs the installer submodule set, so this
//! list is still discovered here.

use std::path::Path;

use vct_launcher_core::process::CommandExt as _;

/// Detects NVIDIA GPU + total VRAM (across all GPUs) in GB.
/// Returns (has_gpu, first_gpu_name, total_vram_gb).
/// Enumerate EVERY NVIDIA GPU (one `GpuCandidate` per device). All NVIDIA
/// cards are discrete (HW constraint: NVIDIA makes no iGPU). Mirror of
/// `vco_lib.gpu_device._enumerate_nvidia`. Soft-fails to `[]`.
pub(crate) async fn enumerate_nvidia_cards() -> Vec<crate::commands::gpu_policy::GpuCandidate> {
    let result = tokio::process::Command::new("nvidia-smi")
        .silent()
        .args([
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .await;
    let mut out = Vec::new();
    if let Ok(output) = result {
        if output.status.success() {
            let raw = String::from_utf8_lossy(&output.stdout);
            for line in raw.lines() {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();
                // index, name, memory.total(MiB)
                let name = parts.get(1).copied().unwrap_or("").to_string();
                let vram_gb = parts
                    .get(2)
                    .and_then(|m| m.parse::<f64>().ok())
                    .map(|mib| (mib / 1024.0 * 100.0).round() / 100.0)
                    .unwrap_or(0.0);
                if !name.is_empty() {
                    out.push(crate::commands::gpu_policy::GpuCandidate {
                        vendor: "nvidia".to_string(),
                        name,
                        vram_gb,
                        is_integrated: false,
                    });
                }
            }
        }
    }
    out
}

/// Enumerate AMD GPUs (one `GpuCandidate` per rocm-smi card). iGPU
/// classification is refined later in `enumerate_gpus` via lspci. Mirror
/// of `vco_lib.gpu_device._enumerate_amd`. Soft-fails to `[]`.
pub(crate) async fn enumerate_amd_cards() -> Vec<crate::commands::gpu_policy::GpuCandidate> {
    let result = tokio::process::Command::new("rocm-smi")
        .silent()
        .args(["--showmeminfo", "vram", "--csv"])
        .output()
        .await;
    let mut out = Vec::new();
    if let Ok(output) = result {
        if output.status.success() {
            let raw = String::from_utf8_lossy(&output.stdout);
            let lines: Vec<&str> = raw.lines().filter(|l| !l.trim().is_empty()).collect();
            if lines.len() >= 2 {
                // Find the "VRAM Total Memory" column from the header.
                let header: Vec<String> =
                    lines[0].split(',').map(|h| h.trim().to_ascii_lowercase()).collect();
                let vram_col = header
                    .iter()
                    .position(|h| h.contains("vram") && h.contains("total"));
                if let Some(col) = vram_col {
                    for row in &lines[1..] {
                        let values: Vec<&str> = row.split(',').map(|v| v.trim()).collect();
                        if let Some(cell) = values.get(col) {
                            if let Ok(bytes) = cell.parse::<f64>() {
                                let vram_gb = (bytes / 1024.0 / 1024.0 / 1024.0 * 100.0)
                                    .round()
                                    / 100.0;
                                out.push(crate::commands::gpu_policy::GpuCandidate {
                                    vendor: "amd".to_string(),
                                    name: "AMD GPU (ROCm)".to_string(),
                                    vram_gb,
                                    is_integrated: false,
                                });
                            }
                        }
                    }
                }
            }
        }
    }
    if !out.is_empty() {
        return out;
    }
    // v0.2.68 (Defect Y, SF-3): text fallback for older rocm-smi that lacks
    // `--csv`. Mirrors `vco_lib.gpu_device._enumerate_amd`'s text path so the
    // launcher snapshot agrees with the Python install on legacy-rocm-smi AMD
    // hosts (without this the snapshot returned [] → "no GPU" while install.py
    // enumerated the card → ROCm). One "Total Memory" line per card.
    let result = tokio::process::Command::new("rocm-smi")
        .silent()
        .args(["--showmeminfo", "vram"])
        .output()
        .await;
    if let Ok(output) = result {
        if output.status.success() {
            let raw = String::from_utf8_lossy(&output.stdout);
            for line in raw.lines() {
                let ll = line.to_ascii_lowercase();
                if ll.contains("total") && ll.contains("memory") && line.contains(':') {
                    let after = line.splitn(2, ':').nth(1).unwrap_or("").trim();
                    let first = after.split_whitespace().next().unwrap_or("");
                    if let Ok(bytes_val) = first.parse::<f64>() {
                        // Heuristic: huge → bytes; mid → MB; small → already GB.
                        let vram_gb = if bytes_val > 1024.0_f64.powi(3) {
                            (bytes_val / 1024.0 / 1024.0 / 1024.0 * 100.0).round() / 100.0
                        } else if bytes_val > 1024.0 {
                            (bytes_val / 1024.0 * 100.0).round() / 100.0
                        } else {
                            (bytes_val * 100.0).round() / 100.0
                        };
                        out.push(crate::commands::gpu_policy::GpuCandidate {
                            vendor: "amd".to_string(),
                            name: "AMD GPU (ROCm)".to_string(),
                            vram_gb,
                            is_integrated: false,
                        });
                    }
                }
            }
        }
    }
    out
}

/// Enumerate ALL GPUs across vendors and classify Intel / iGPU. Mirror of
/// `vco_lib.gpu_device.enumerate_gpus`. Cross-references lspci (Linux) for
/// vendor + PCI-bus iGPU discrimination + explicit Intel detection.
/// Soft-fails throughout — NEVER panics.
pub(crate) async fn enumerate_gpus() -> Vec<crate::commands::gpu_policy::GpuCandidate> {
    use crate::commands::gpu_policy::GpuCandidate;

    let nvidia = enumerate_nvidia_cards().await;
    let amd_raw = enumerate_amd_cards().await;

    // lspci VGA/3D lines (Linux only; empty elsewhere).
    let vga_lines = lspci_vga_lines().await;

    // Refine AMD iGPU classification: if lspci shows BOTH an AMD root-bus
    // (00:) VGA device AND an AMD discrete-bus device, a UMA-sized
    // (<= 2 GB) rocm-smi card is the iGPU. Mirrors
    // `_classify_amd_integrated` in the Python module.
    let amd_root = vga_lines.iter().any(|l| {
        vendor_from_lspci(l) == "amd" && bus_is_integrated(&pci_bus_from_lspci(l)) == Some(true)
    });
    let amd_discrete = vga_lines.iter().any(|l| {
        vendor_from_lspci(l) == "amd" && bus_is_integrated(&pci_bus_from_lspci(l)) == Some(false)
    });
    let amd: Vec<GpuCandidate> = amd_raw
        .into_iter()
        .map(|c| {
            if amd_root && amd_discrete && c.vram_gb > 0.0 && c.vram_gb <= 2.0 {
                GpuCandidate { is_integrated: true, ..c }
            } else {
                c
            }
        })
        .collect();

    // Intel GPUs are invisible to nvidia-smi/rocm-smi → lspci is the only
    // way to SEE them and explicitly exclude them downstream.
    let mut intel: Vec<GpuCandidate> = Vec::new();
    for line in &vga_lines {
        if vendor_from_lspci(line) != "intel" {
            continue;
        }
        let bus = pci_bus_from_lspci(line);
        let integrated = bus_is_integrated(&bus).unwrap_or(true);
        intel.push(GpuCandidate {
            vendor: "intel".to_string(),
            name: "Intel GPU".to_string(),
            vram_gb: 0.0,
            is_integrated: integrated,
        });
    }

    let mut all = nvidia;
    all.extend(amd);
    all.extend(intel);
    all
}

/// lspci `-nn` VGA/3D/Display lines. Empty on non-Linux / missing lspci /
/// failure. Mirror of `vco_lib.gpu_device._lspci_vga_lines`.
pub(crate) async fn lspci_vga_lines() -> Vec<String> {
    if std::env::consts::OS != "linux" {
        return Vec::new();
    }
    let result = tokio::process::Command::new("lspci")
        .silent()
        .arg("-nn")
        .output()
        .await;
    let mut out = Vec::new();
    if let Ok(output) = result {
        if output.status.success() {
            let raw = String::from_utf8_lossy(&output.stdout);
            for line in raw.lines() {
                let low = line.to_ascii_lowercase();
                if low.contains("vga compatible controller")
                    || low.contains("3d controller")
                    || low.contains("display controller")
                {
                    out.push(line.to_string());
                }
            }
        }
    }
    out
}

/// Map an lspci `-nn` line to "nvidia"/"amd"/"intel"/"unknown". Prefers
/// the bracketed [VEN:DEV] id. Mirror of `_vendor_from_lspci_line`.
pub(crate) fn vendor_from_lspci(line: &str) -> &'static str {
    let low = line.to_ascii_lowercase();
    if low.contains("[8086:") {
        return "intel";
    }
    if low.contains("[10de:") {
        return "nvidia";
    }
    if low.contains("[1002:") {
        return "amd";
    }
    if low.contains("intel") {
        return "intel";
    }
    if low.contains("nvidia") {
        return "nvidia";
    }
    if low.contains("amd") || low.contains("ati") || low.contains("advanced micro devices") {
        return "amd";
    }
    "unknown"
}

/// Extract the leading PCI bus token (`03:00.0`) from an lspci line.
pub(crate) fn pci_bus_from_lspci(line: &str) -> String {
    // lspci lines start with "BB:DD.F " (hex bus:device.function).
    let trimmed = line.trim_start();
    let token: String = trimmed
        .chars()
        .take_while(|c| !c.is_whitespace())
        .collect();
    // Validate shape "xx:yy.z" loosely; otherwise return empty.
    if token.contains(':') && token.contains('.') {
        token.to_ascii_lowercase()
    } else {
        String::new()
    }
}

/// Classify a PCI bus token: integrated (bus 00) / discrete / unknown.
/// Mirror of `vco_lib.gpu_device._bus_is_integrated`.
pub(crate) fn bus_is_integrated(pci_bus: &str) -> Option<bool> {
    if pci_bus.is_empty() {
        return None;
    }
    let bus = pci_bus.split(':').next().unwrap_or("");
    u32::from_str_radix(bus, 16).ok().map(|n| n == 0)
}

/// `which <cmd>` then `<cmd> --version` → "<cmd> <version>" or None if not
/// installed. We swallow parse errors and fall back to just the command
/// name so the UI never shows "podman " with a trailing space.
pub(crate) async fn detect_runtime_version(cmd: &str) -> Option<String> {
    if !check_command_exists(cmd).await {
        return None;
    }
    let out = tokio::process::Command::new(cmd).silent()
        .arg("--version")
        .output()
        .await
        .ok()?;
    if !out.status.success() {
        return Some(cmd.to_string());
    }
    let raw = String::from_utf8_lossy(&out.stdout).trim().to_string();
    // Typical: "podman version 4.7.0" / "Docker version 27.0.3, build abc"
    let version = raw
        .split_whitespace()
        .find(|tok| tok.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false))
        .map(|s| s.trim_end_matches(',').to_string());
    Some(match version {
        Some(v) => format!("{} {}", cmd, v),
        None => cmd.to_string(),
    })
}

/// True if `path` exists, is a directory, and contains at least one entry.
/// Returns false if path doesn't exist, isn't a directory, or read fails.
#[allow(dead_code)]
pub(crate) async fn dir_has_entries(path: &Path) -> bool {
    match tokio::fs::read_dir(path).await {
        Ok(mut rd) => match rd.next_entry().await {
            Ok(Some(_)) => true,
            _ => false,
        },
        Err(_) => false,
    }
}

/// Total system RAM in GB (rounded).
///
/// Bug 18: sysinfo's `total_memory()` returns AVAILABLE physical RAM
/// after kernel reservations (~1-2 GB on Linux), so a 64 GB machine
/// shows up as 62 GB. Read `/proc/meminfo`'s `MemTotal:` line directly
/// on Linux (matches `free -h` and the spec sticker on the box).
/// macOS uses `sysctl hw.memsize`. Windows falls back to sysinfo for
/// now (Windows has its own kernel-reserve quirks; can use
/// `GetPhysicallyInstalledSystemMemory` later if it matters).
pub(crate) fn detect_ram_gb() -> u64 {
    if let Some(gb) = detect_ram_gb_native() {
        return gb;
    }
    detect_ram_gb_sysinfo()
}

#[cfg(target_os = "linux")]
pub(crate) fn detect_ram_gb_native() -> Option<u64> {
    parse_meminfo_total_kb(&std::fs::read_to_string("/proc/meminfo").ok()?)
        .map(snap_to_common_ram_gb)
}

#[cfg(target_os = "macos")]
pub(crate) fn detect_ram_gb_native() -> Option<u64> {
    let out = std::process::Command::new("sysctl").silent()
        .args(["-n", "hw.memsize"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let raw = String::from_utf8_lossy(&out.stdout);
    let bytes: u64 = raw.trim().parse().ok()?;
    // sysctl reports the actual installed bytes; convert bytes → kB
    // and snap to the marketed-stick value the same as Linux does.
    Some(snap_to_common_ram_gb(bytes / 1024))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub(crate) fn detect_ram_gb_native() -> Option<u64> {
    None
}

pub(crate) fn detect_ram_gb_sysinfo() -> u64 {
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_memory();
    let bytes = sys.total_memory();
    if bytes == 0 {
        return 0;
    }
    (bytes as f64 / 1024.0 / 1024.0 / 1024.0).round() as u64
}

/// Parse the `MemTotal:` line out of `/proc/meminfo` content.
/// Returns the value in kB. Public for testing.
pub fn parse_meminfo_total_kb(meminfo: &str) -> Option<u64> {
    for line in meminfo.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            // Expected: "MemTotal:       65857132 kB"
            let kb: u64 = rest
                .split_whitespace()
                .next()?
                .parse()
                .ok()?;
            return Some(kb);
        }
    }
    None
}

/// Snap MemTotal (kB) to the closest common DDR stick capacity in
/// decimal GB. Linux's `MemTotal` reports physical RAM minus kernel
/// reserves, which on a "64 GB" machine yields ~62.4 GiB. We snap UP
/// to the bucket the user actually paid for.
///
/// Match window: `bucket * 0.93 ≤ approx_gib ≤ bucket + 0.5`. The 0.93
/// floor accommodates kernel reserves up to ~7%; the +0.5 ceiling lets
/// a slightly-over MemTotal still hit the bucket cleanly.
pub fn snap_to_common_ram_gb(meminfo_kb: u64) -> u64 {
    let approx_gib = meminfo_kb as f64 / (1024.0 * 1024.0);
    // Common DDR4/DDR5 capacities (and a couple of legacy values).
    let buckets: &[u64] = &[
        1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024,
    ];
    for &b in buckets {
        let bf = b as f64;
        if approx_gib >= bf * 0.93 && approx_gib <= bf + 0.5 {
            return b;
        }
    }
    // Fallback: round to nearest GiB.
    approx_gib.round() as u64
}

pub(crate) async fn check_command_exists(cmd: &str) -> bool {
    // CREATE_NO_WINDOW (0x08000000) on Windows: `where` spawned from a
    // GUI-subsystem parent flashes a conhost.exe console for its
    // ~200ms lifetime. detect_system() calls this 3 times concurrently
    // for {claude,git,node} at boot, and check_command_exists also has
    // other callers (~9 visible console flashes per launcher start
    // observed via EnumWindows snapshot, 2026-05-26). Suppress.
    let check = if cfg!(windows) {
        let mut cmd_builder = tokio::process::Command::new("where");
        cmd_builder.arg(cmd);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd_builder.creation_flags(0x0800_0000);
        }
        cmd_builder.output().await
    } else {
        tokio::process::Command::new("which").silent()
            .arg(cmd)
            .output()
            .await
    };

    matches!(check, Ok(output) if output.status.success())
}

pub(crate) async fn detect_python() -> (bool, String, String) {
    // On Windows, prefer `py` (the Python launcher — bundled with
    // python.org installer) over bare `python` because `python` on
    // Windows can be the Microsoft Store stub at
    // C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe
    // which redirects to the Store on first run instead of executing.
    // `py` always points to a real interpreter when one's installed
    // via python.org or PythonManager. Reported 2026-04-28: the wizard
    // got stuck in "Creating…" with a flashing python.exe console
    // window because the picked python_cmd was a Store-managed stub.
    // CROSS-LANGUAGE PARITY (v0.2.53 NEW-3): the POSIX `else { vec![...] }`
    // branch below is a MIRROR — it must stay identical, in order, to the
    // other two bootstrap Python probes:
    //   * install.sh   → find_python `for cmd in ...`
    //   * install.ps1  → Find-Python `$candidates`
    // The mirror is deliberate (C-tier, justified): install.sh / install.ps1
    // run at bootstrap on a fresh machine with NO jq / interpreter / launcher
    // available, so a shared data file cannot be safely parsed there — the
    // three lists are locked instead by tests/test_python_candidate_parity.py
    // (extracts all three literal lists, asserts sh == ps1 == rs for POSIX).
    // Edit all three + keep that test green when this list changes.
    //
    // WHY the POSIX list carries `python3.13`: a Linux box where the user has
    // ONLY python3.13 (no `python3` alias — Fedora / some Arch derivatives)
    // must report has_python=true for the launcher-driven detect_system()
    // path, matching what install.sh already accepts.
    //
    // Windows INTENTIONALLY diverges (NOT part of the parity assertion):
    // prefer `py` (the python.org launcher) over bare `python`, because
    // `python` on Windows is often the Microsoft Store stub at
    // C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe, which
    // redirects to the Store on first run instead of executing. Reported
    // 2026-04-28: the wizard got stuck in "Creating…" with a flashing
    // python.exe console window because the picked python_cmd was that stub.
    let candidates = if cfg!(windows) {
        vec!["py", "python3", "python"]
    } else {
        vec!["python3.13", "python3.12", "python3.11", "python3", "python"]
    };

    for cmd in candidates {
        let mut tcmd = tokio::process::Command::new(cmd);
        tcmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"]);
        // Suppress empty cmd window flash on Windows.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            tcmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let result = tcmd.output().await;

        if let Ok(output) = result {
            if output.status.success() {
                let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
                // Check >= 3.11
                let parts: Vec<&str> = version.split('.').collect();
                if parts.len() >= 2 {
                    let major: u32 = parts[0].parse().unwrap_or(0);
                    let minor: u32 = parts[1].parse().unwrap_or(0);
                    if major >= 3 && minor >= 11 {
                        return (true, version, cmd.to_string());
                    }
                }
            }
        }
    }

    (false, String::new(), String::new())
}

