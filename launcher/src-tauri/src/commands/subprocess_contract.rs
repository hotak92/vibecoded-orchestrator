// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Shared helpers for "a child process's stdout is a machine contract".
//!
//! ## Why this module exists
//!
//! v0.2.92 field bug (2026-09-05): a LIBRARY function three frames below a
//! CLI handler relayed a child process's CAPTURED stdout onto the parent's
//! own stdout. The parent's stdout was a JSON contract, so the relayed
//! progress line ("4_to_5: … at v5 shape") landed in front of the payload
//! and `serde_json::from_str` failed with `trailing characters at line 1
//! column 2` — the leading `4` had parsed as a complete JSON number.
//!
//! Two things followed from that incident, and both live here:
//!
//! 1. **Parse the WHOLE stdout, strictly.** Strictness is what SURFACED the
//!    bug. A tolerant parser — "scan forward to the first `{` and parse from
//!    there" — would have swallowed the pollution silently and let the next
//!    misbehaving emitter ship unnoticed. A salvage parse converts a loud,
//!    once-per-release defect into a permanent blind spot.
//! 2. **Say what was actually on stdout.** The original notice pointed only
//!    at stderr, which on that run was EMPTY: the offending bytes were on
//!    stdout and were never shown to anyone. [`stdout_parse_diagnostic`] is
//!    the fragment that names the offender.
//!
//! One home, because both `projects_v2`'s three `migrate-schema` parse sites
//! and `bundle_staleness`'s census parse need the identical diagnostic; two
//! copies of a guard drift, and the drift is invisible until the next
//! incident.

/// The diagnostic fragment for a stdout-JSON parse failure: the first
/// NON-EMPTY line of the child's stdout, truncated at 160 chars.
///
/// Distinguishes the two failure shapes a caller must not conflate:
///
/// * **empty stdout** — the child printed nothing at all (crashed before its
///   first write, or was killed). Rendered `(stdout was empty)`.
/// * **a blank first line followed by content** — the child DID write; the
///   first thing worth showing is the first non-blank line, because that is
///   the line that names the polluting emitter.
///
/// Pure function. Unit-tested in this module's `tests`.
pub(crate) fn stdout_parse_diagnostic(stdout: &str) -> String {
    match stdout.lines().find(|l| !l.trim().is_empty()) {
        None => "(stdout was empty)".to_string(),
        Some(line) => {
            let trimmed = line.trim();
            if trimmed.chars().count() > 160 {
                let head: String = trimmed.chars().take(160).collect();
                format!("{}…", head)
            } else {
                trimmed.to_string()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── v0.2.92: actionable stdout-parse diagnostic (field bug) ────────
    //
    // The reported message pointed the user at stderr, which was EMPTY on
    // that run — the polluting bytes were on stdout and never shown. These
    // pin that the notice now carries the bit that names the offender.
    //
    // (Moved verbatim from `commands::projects_v2::tests` when the helper
    // was extracted here so `bundle_staleness` could stop salvaging.)

    #[test]
    fn stdout_parse_diagnostic_shows_the_polluting_first_line() {
        let polluted = "4_to_5: Foo_CodeModule at v5 shape (props present/added)\n\
                        EDGE_APPLIED=1\n{\"applied\": 5}\n";
        assert_eq!(
            stdout_parse_diagnostic(polluted),
            "4_to_5: Foo_CodeModule at v5 shape (props present/added)"
        );
    }

    #[test]
    fn stdout_parse_diagnostic_names_the_empty_case_explicitly() {
        // A silent subprocess is a DIFFERENT fault (crashed before printing)
        // and must not read as "the first line was blank".
        assert_eq!(stdout_parse_diagnostic(""), "(stdout was empty)");
        assert_eq!(stdout_parse_diagnostic("\n  \n\t\n"), "(stdout was empty)");
    }

    #[test]
    fn stdout_parse_diagnostic_truncates_a_runaway_line() {
        let long = "x".repeat(500);
        let got = stdout_parse_diagnostic(&long);
        assert!(got.ends_with('…'));
        assert_eq!(got.chars().count(), 161, "160 chars + the ellipsis");
    }

    /// A blank FIRST line is not an empty stdout — the caller must still be
    /// shown the first line that carries bytes, because that is the one that
    /// names the emitter. Pins the two cases apart.
    #[test]
    fn stdout_parse_diagnostic_skips_a_leading_blank_line() {
        assert_eq!(
            stdout_parse_diagnostic("\n\nsome-lib: relayed child output\n{}"),
            "some-lib: relayed child output"
        );
    }
}
