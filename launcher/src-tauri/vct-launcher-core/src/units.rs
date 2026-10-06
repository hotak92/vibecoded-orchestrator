// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Human-readable units — the ONE byte-size rendering, and the ONE
//! elapsed-duration rendering for progress lines.
//!
//! v0.2.100 (F-W4-09): moved from `commands/volumes.rs` (private) when the
//! ResetHard backup's progress line ("Saving your work … (N MB)") needed the
//! same rendering and had formatted it inline.

/// `bytes` as `"<n> B"` / `"<x.y> KB|MB|GB"` (binary multiples, one decimal).
pub fn human_bytes(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = KB * 1024;
    const GB: u64 = MB * 1024;
    if bytes >= GB {
        format!("{:.1} GB", bytes as f64 / GB as f64)
    } else if bytes >= MB {
        format!("{:.1} MB", bytes as f64 / MB as f64)
    } else if bytes >= KB {
        format!("{:.1} KB", bytes as f64 / KB as f64)
    } else {
        format!("{} B", bytes)
    }
}

/// `secs` as `"<s>s"` under a minute, `"<m>m <ss>s"` under an hour, and
/// `"<h>h <mm>m"` above — the shape a progress line ("2m 05s of up to 30m")
/// reads at a glance. v0.2.101 (review S7): the volume-migration health
/// wait's progress events are the first caller.
pub fn human_duration_secs(secs: u64) -> String {
    if secs < 60 {
        format!("{}s", secs)
    } else if secs < 3600 {
        format!("{}m {:02}s", secs / 60, secs % 60)
    } else {
        format!("{}h {:02}m", secs / 3600, (secs % 3600) / 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn human_bytes_formats_thresholds_correctly() {
        assert_eq!(human_bytes(0), "0 B");
        assert_eq!(human_bytes(512), "512 B");
        assert_eq!(human_bytes(2048), "2.0 KB");
        assert_eq!(human_bytes(2 * 1024 * 1024), "2.0 MB");
        assert_eq!(human_bytes(3 * 1024 * 1024 * 1024), "3.0 GB");
    }

    #[test]
    fn human_duration_secs_formats_each_band() {
        assert_eq!(human_duration_secs(0), "0s");
        assert_eq!(human_duration_secs(59), "59s");
        assert_eq!(human_duration_secs(60), "1m 00s");
        assert_eq!(human_duration_secs(125), "2m 05s");
        assert_eq!(human_duration_secs(3599), "59m 59s");
        assert_eq!(human_duration_secs(3600), "1h 00m");
        assert_eq!(human_duration_secs(5430), "1h 30m");
    }
}
