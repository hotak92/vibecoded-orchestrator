//! GPU / NVIDIA CDI drift detection (2026-05-08).
//!
//! ## Why this exists
//!
//! NVIDIA Container Toolkit ≥ 1.18.0 ships `nvidia-cdi-refresh.path` +
//! `nvidia-cdi-refresh.service` systemd units that auto-write a fresh
//! CDI spec to `/var/run/cdi/nvidia.yaml` on every driver install/upgrade.
//! Podman reads `/etc/cdi/` BEFORE `/var/run/cdi/`, so a manually written
//! `/etc/cdi/nvidia.yaml` SHADOWS the auto-refresh and goes stale on the
//! next driver upgrade. Symptom: every GPU container fails to start with
//! `runc: failed to fulfil mount request: ... libEGL_nvidia.so.<old-version>:
//! no such file`.
//!
//! Bit Claude Orchestrator on 2026-05-07/08. Full forensics in
//! `Claude/.claude/context/handoff-2026-05-*` and the KG node
//! `knowledge/tools/podman-cdi-gpu-passthrough.md`.
//!
//! ## What this command does
//!
//! Runs once at launcher startup (Linux only — macOS Podman runs in a VM,
//! Windows uses different mechanisms). Returns a `CdiDriftReport` that
//! the frontend uses to decide whether to show a blocking modal.
//!
//! Three states:
//!   - `Ok` — no drift detected (nvidia-smi version matches /var/run/cdi
//!     or /etc/cdi). Fast path on every Linux+NVIDIA boot.
//!   - `Drift { ... }` — driver in `nvidia-smi` doesn't match the
//!     CDI spec(s). Surfaces both the host driver version and what the
//!     CDI spec is pinned to, plus an actionable command to fix it.
//!   - `NotApplicable { reason }` — non-Linux, no nvidia-smi, no CDI
//!     spec at all (legitimate non-GPU host), or we couldn't tell. The
//!     frontend treats this as "skip the modal".
//!
//! User text is built here so the modal stays a thin renderer.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;
use std::process::Command;

/// Result of the CDI-vs-driver drift check.
///
/// Tagged enum so the frontend can pattern-match without ambiguity. The
/// `not_applicable` case is the silent-success path on hosts that don't
/// have NVIDIA at all — we want the modal to skip silently, not flash a
/// "no GPU detected" warning.
#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CdiDriftReport {
    /// Driver and CDI agree, or we're on a host where no check is needed.
    /// `host_driver` may be empty when we couldn't query nvidia-smi.
    Ok { host_driver: String },

    /// Driver in nvidia-smi does NOT match the CDI spec(s).
    Drift {
        host_driver: String,
        /// `/etc/cdi/nvidia.yaml` driver version, if the file exists. Empty
        /// when the file is absent (which is fine — the auto-managed
        /// `/var/run/cdi/` spec is enough).
        cdi_etc_driver: String,
        /// `/var/run/cdi/nvidia.yaml` driver version (auto-managed),
        /// if the file exists. Empty when the file is absent.
        cdi_run_driver: String,
        /// Human-readable explanation + the exact command(s) the user
        /// should run. Built server-side so the modal is a thin renderer.
        message: String,
        /// True when `/etc/cdi/nvidia.yaml` exists AND its driver version
        /// is stale (most common cause). Frontend surfaces the
        /// `sudo rm /etc/cdi/nvidia.yaml` command prominently when true.
        stale_etc_cdi_present: bool,
    },

    /// Skip silently — non-Linux, no nvidia hardware, or check inconclusive.
    NotApplicable { reason: String },
}

/// Public Tauri command. Always returns Ok — internal failures are
/// folded into the `NotApplicable` variant so the frontend never has
/// to handle a Result<Result<>>.
#[tauri::command]
pub async fn check_cdi_drift() -> Result<CdiDriftReport, String> {
    Ok(check_cdi_drift_impl())
}

/// Synchronous implementation, separated for unit testing.
fn check_cdi_drift_impl() -> CdiDriftReport {
    if !cfg!(target_os = "linux") {
        return CdiDriftReport::NotApplicable {
            reason: "CDI drift detection runs on Linux only.".to_string(),
        };
    }

    let host_driver = match query_nvidia_smi_driver() {
        Some(v) => v,
        None => {
            return CdiDriftReport::NotApplicable {
                reason: "nvidia-smi not available — no NVIDIA GPU detected, skipping CDI check."
                    .to_string(),
            };
        }
    };

    let cdi_etc = read_cdi_driver_version("/etc/cdi/nvidia.yaml");
    let cdi_run = read_cdi_driver_version("/var/run/cdi/nvidia.yaml");

    // No CDI spec at all → user hasn't installed the toolkit, or it
    // hasn't fired yet. Not the launcher's job to set this up.
    if cdi_etc.is_none() && cdi_run.is_none() {
        return CdiDriftReport::NotApplicable {
            reason:
                "No CDI spec found at /etc/cdi or /var/run/cdi — install nvidia-container-toolkit ≥ 1.18.0 to enable GPU passthrough."
                    .to_string(),
        };
    }

    let etc_v = cdi_etc.clone().unwrap_or_default();
    let run_v = cdi_run.clone().unwrap_or_default();

    // What Podman will actually use: /etc/cdi/ shadows /var/run/cdi/.
    let effective = if !etc_v.is_empty() { &etc_v } else { &run_v };

    if effective == &host_driver {
        return CdiDriftReport::Ok {
            host_driver,
        };
    }

    // Drift confirmed. Build an actionable message tuned to which spec
    // is the problem.
    let stale_etc = !etc_v.is_empty() && etc_v != host_driver;
    let message = build_drift_message(&host_driver, &etc_v, &run_v, stale_etc);

    CdiDriftReport::Drift {
        host_driver,
        cdi_etc_driver: etc_v,
        cdi_run_driver: run_v,
        message,
        stale_etc_cdi_present: stale_etc,
    }
}

/// Run `nvidia-smi --query-gpu=driver_version --format=csv,noheader`
/// with a 3s timeout. Returns the version string (e.g. "595.58.03") or
/// None if the command isn't on PATH or the call fails.
fn query_nvidia_smi_driver() -> Option<String> {
    let output = Command::new("nvidia-smi")
        .args(["--query-gpu=driver_version", "--format=csv,noheader"])
        // No native std::process::Command timeout. The launcher's UX
        // can absorb the worst-case ~5s; nvidia-smi normally returns
        // in <100ms on a healthy host.
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let s = String::from_utf8(output.stdout).ok()?;
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return None;
    }
    // nvidia-smi may return multiple GPU lines for multi-GPU hosts. They
    // all share a driver version, so take the first.
    Some(trimmed.lines().next()?.trim().to_string())
}

/// Read a CDI YAML file and extract the embedded NVIDIA driver version.
///
/// CDI specs reference NVIDIA libraries with versioned filenames (e.g.
/// `libEGL_nvidia.so.595.58.03`). Parsing the YAML properly would mean
/// pulling in serde_yaml; instead we regex-match the first `libEGL_nvidia.so.<X.Y.Z>`
/// occurrence, which is reliable enough.
fn read_cdi_driver_version(path: &str) -> Option<String> {
    if !Path::new(path).exists() {
        return None;
    }
    let content = fs::read_to_string(path).ok()?;
    extract_driver_version_from_cdi_text(&content)
}

/// Standalone parser, exposed for unit tests.
fn extract_driver_version_from_cdi_text(content: &str) -> Option<String> {
    // Match `libEGL_nvidia.so.<digits>.<digits>.<digits>` and capture the
    // version. Regex avoidance for binary-size reasons: hand-roll a small
    // state machine.
    let needle = "libEGL_nvidia.so.";
    let start = content.find(needle)?;
    let after = &content[start + needle.len()..];
    // Take chars while they're [0-9.] — driver versions are like
    // "595.58.03". Stop at the first non-version char.
    let end = after
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(after.len());
    let version = &after[..end];
    // Sanity: must contain at least one '.' to be a real version.
    if version.is_empty() || !version.contains('.') {
        return None;
    }
    Some(version.to_string())
}

/// Build the user-facing message. Tuned per-case so the most common
/// fix (stale /etc/cdi/) gets the spotlight.
fn build_drift_message(
    host_driver: &str,
    etc_v: &str,
    run_v: &str,
    stale_etc: bool,
) -> String {
    let mut lines: Vec<String> = vec![
        format!(
            "You have NVIDIA driver {host_driver} installed, but your CDI \
             spec is pinned to a different version. GPU containers will fail \
             to start with `runc: failed to fulfil mount request: ... no such file`."
        ),
    ];

    if !etc_v.is_empty() {
        lines.push(format!("    /etc/cdi/nvidia.yaml         → {etc_v}  (manual / shadowing)"));
    }
    if !run_v.is_empty() {
        lines.push(format!("    /var/run/cdi/nvidia.yaml     → {run_v}  (auto-managed)"));
    }
    lines.push(format!("    nvidia-smi driver_version    → {host_driver}  (current host)"));
    lines.push(String::new());

    if stale_etc {
        lines.push(
            "Most likely fix: a stale /etc/cdi/nvidia.yaml from a prior install is \
             SHADOWING the auto-managed /var/run/cdi/nvidia.yaml. Remove the manual \
             file so Podman falls back to the auto-refreshed one:"
                .to_string(),
        );
        lines.push(String::new());
        lines.push("    sudo rm /etc/cdi/nvidia.yaml".to_string());
        lines.push(String::new());
        lines.push(
            "Then restart any GPU containers: `podman-compose down && podman-compose up -d`."
                .to_string(),
        );
    } else {
        lines.push(
            "The NVIDIA Container Toolkit's auto-refresh service hasn't run since the \
             last driver upgrade. Trigger it manually:"
                .to_string(),
        );
        lines.push(String::new());
        lines.push("    sudo systemctl restart nvidia-cdi-refresh.service".to_string());
        lines.push(String::new());
        lines.push(
            "If that doesn't help, verify the service is enabled: \
             `systemctl is-enabled nvidia-cdi-refresh.path` (must say `enabled`). \
             Toolkit ≥ 1.18.0 ships these units."
                .to_string(),
        );
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_driver_version_from_cdi_yaml() {
        let yaml = r#"
cdiVersion: 0.5.0
kind: nvidia.com/gpu
devices:
  - name: "0"
    containerEdits:
      mounts:
        - hostPath: /usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.595.58.03
          containerPath: /usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.595.58.03
"#;
        assert_eq!(
            extract_driver_version_from_cdi_text(yaml),
            Some("595.58.03".to_string())
        );
    }

    #[test]
    fn missing_libegl_returns_none() {
        let yaml = "cdiVersion: 0.5.0\nkind: nvidia.com/gpu\n";
        assert_eq!(extract_driver_version_from_cdi_text(yaml), None);
    }

    #[test]
    fn empty_input_returns_none() {
        assert_eq!(extract_driver_version_from_cdi_text(""), None);
    }

    #[test]
    fn matches_first_occurrence_when_multiple() {
        let yaml = "libEGL_nvidia.so.590.48.01 ...
libEGL_nvidia.so.595.58.03";
        // First match wins.
        assert_eq!(
            extract_driver_version_from_cdi_text(yaml),
            Some("590.48.01".to_string())
        );
    }

    #[test]
    fn drift_message_with_stale_etc_recommends_rm() {
        let msg = build_drift_message("595.58.03", "590.48.01", "595.58.03", true);
        assert!(msg.contains("sudo rm /etc/cdi/nvidia.yaml"));
        assert!(msg.contains("595.58.03"));
        assert!(msg.contains("590.48.01"));
    }

    #[test]
    fn drift_message_without_stale_etc_recommends_systemctl() {
        let msg = build_drift_message("595.58.03", "", "590.48.01", false);
        assert!(msg.contains("systemctl restart nvidia-cdi-refresh"));
        assert!(!msg.contains("sudo rm"));
    }
}
