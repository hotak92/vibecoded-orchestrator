// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared ISO-8601 UTC timestamp helpers.
//!
//! ## Why this module exists (v0.2.77 Part 7c task 5)
//!
//! Two launcher command files each grew their own "now, as
//! `YYYY-MM-DDTHH:MM:SSZ`" helper that produced byte-identical output by
//! two DIFFERENT routes:
//!
//!   * `commands::installer::chrono_iso_z` — a hand-rolled implementation
//!     using Howard Hinnant's civil-from-days algorithm over
//!     `SystemTime::now()`, deliberately avoiding chrono.
//!   * `commands::manifest::chrono_iso_z_now` — `chrono::Utc::now()
//!     .format("%Y-%m-%dT%H:%M:%SZ")`. Its own doc-comment lamented "the
//!     rest of the codebase has its own helper but it lives in
//!     installer.rs and we don't want a circular module dependency."
//!
//! The circular-dependency worry that kept them apart evaporates once the
//! helper lives in `vct-launcher-core` (a leaf crate both command files
//! already depend on). This module is the ONE home. chrono is already a
//! core dependency, so [`chrono_iso_z_now`] uses it; the pure
//! [`civil_from_days`] / [`iso_z_from_unix_secs`] pair is preserved for
//! any future no-chrono / from-a-fixed-instant caller and keeps the
//! algorithm under a unit test.

/// Current UTC time formatted as `YYYY-MM-DDTHH:MM:SSZ`.
///
/// The canonical "timestamp now" helper for the launcher. Second
/// granularity, always the `Z` suffix (UTC).
pub fn chrono_iso_z_now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// Format a unix-epoch-seconds instant as `YYYY-MM-DDTHH:MM:SSZ` WITHOUT
/// chrono, via the civil-from-days algorithm. Kept as the pure,
/// dependency-free path (and so a fixed `secs` can be formatted
/// deterministically in tests).
pub fn iso_z_from_unix_secs(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let secs_of_day = (secs % 86_400) as u32;
    let hh = secs_of_day / 3600;
    let mm = (secs_of_day % 3600) / 60;
    let ss = secs_of_day % 60;
    let (y, m, d) = civil_from_days(days);
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, m, d, hh, mm, ss)
}

/// Howard Hinnant's days-from-civil inverse: returns `(year, month, day)`
/// for a count of days since the unix epoch (day 0 = 1970-01-01).
pub fn civil_from_days(z: i64) -> (i32, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097) as u32; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe as i32 + (era as i32) * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_from_days_known_dates() {
        // Unix epoch is day 0.
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        // 2000-03-01 is day 11017 (a leap-cycle boundary case).
        assert_eq!(civil_from_days(11017), (2000, 3, 1));
    }

    #[test]
    fn iso_z_from_unix_secs_formats_epoch() {
        assert_eq!(iso_z_from_unix_secs(0), "1970-01-01T00:00:00Z");
        // 2021-01-01T00:00:00Z = 1609459200.
        assert_eq!(iso_z_from_unix_secs(1_609_459_200), "2021-01-01T00:00:00Z");
    }

    #[test]
    fn chrono_iso_z_now_has_z_suffix_and_length() {
        let s = chrono_iso_z_now();
        assert!(s.ends_with('Z'), "expected Z suffix, got {s}");
        assert_eq!(s.len(), 20, "expected YYYY-MM-DDTHH:MM:SSZ, got {s}");
    }
}
