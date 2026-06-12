// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared log-tail truncation discipline (v0.2.54 Track J).
//!
//! Before this module, the "cap stored `log_tail` at 4 KiB, slice on a
//! char boundary, prefix with `…\n`" recipe existed as SIX copies:
//! three in this crate's db writers (`code_graph_builds`, `kg_syncs`,
//! `kg_summaries` — each with its own `LOG_TAIL_MAX_BYTES` const, its
//! own `floor_char_boundary`, and an inline capping closure) and three
//! `tail_log` siblings in the launcher crate's command layer
//! (`commands/{codegraph,kg_sync,kg_summary}.rs`). The
//! `kg_syncs` copy's comment argued a shared util "would have to live
//! in a top-level place neither currently imports from" — true at two
//! copies, but the rule of three triggered when `kg_summaries` landed,
//! and the command-layer siblings made it six.
//!
//! One behavioural note from the consolidation: the db writers cut on
//! the FLOOR char boundary (keeping up to 3 bytes more than the cap)
//! while the command-layer siblings cut on the CEIL boundary (keeping
//! up to 3 bytes less). Both bound the result at `LOG_TAIL_MAX_BYTES`
//! plus the ellipsis prefix; every existing test asserts the loose
//! bound (`<= MAX + 8` / `< 5_000`), so the consolidated function
//! standardises on the floor cut.

/// Cap stored `log_tail` columns at 4 KiB. Why 4 KiB: enough for the
/// last ~50 lines of analyzer/sync output (the part with the error),
/// small enough that listing hundreds of rows in the GUI stays cheap.
pub const LOG_TAIL_MAX_BYTES: usize = 4096;

/// `std::str::floor_char_boundary` is unstable; tiny stable
/// replacement. Returns the largest valid char-boundary index `<= idx`
/// (or `s.len()` when `idx` is past the end).
pub fn floor_char_boundary(s: &str, idx: usize) -> usize {
    if idx >= s.len() {
        return s.len();
    }
    let mut i = idx;
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

/// Return `s` capped to its last [`LOG_TAIL_MAX_BYTES`] bytes, sliced
/// on a char boundary (non-ASCII log output — emoji prefixes, Unicode
/// file paths — must not panic the slice) and prefixed with `…\n` when
/// truncation occurred. Short input passes through unchanged.
pub fn cap_log_tail(s: &str) -> String {
    if s.len() <= LOG_TAIL_MAX_BYTES {
        return s.to_string();
    }
    let cut = floor_char_boundary(s, s.len() - LOG_TAIL_MAX_BYTES);
    format!("…\n{}", &s[cut..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_input_passes_through() {
        assert_eq!(cap_log_tail("all good"), "all good");
        assert_eq!(cap_log_tail(""), "");
    }

    #[test]
    fn long_input_truncated_with_ellipsis_prefix() {
        let big = "a".repeat(10_000);
        let tail = cap_log_tail(&big);
        assert!(tail.starts_with("…\n"));
        assert!(tail.len() <= LOG_TAIL_MAX_BYTES + 8);
    }

    #[test]
    fn multibyte_input_never_panics_and_stays_bounded() {
        // 4-byte emoji repeated past the cap; a naive byte slice at
        // len - MAX would land mid-codepoint and panic.
        let big = "🤖".repeat(2_000); // 8000 bytes
        let tail = cap_log_tail(&big);
        assert!(tail.starts_with("…\n"));
        assert!(tail.len() <= LOG_TAIL_MAX_BYTES + 8);
        // Every char in the tail is intact.
        assert!(tail.chars().skip(2).all(|c| c == '🤖'));
    }

    #[test]
    fn floor_char_boundary_basics() {
        assert_eq!(floor_char_boundary("abc", 1), 1);
        assert_eq!(floor_char_boundary("abc", 99), 3);
        // "é" is 2 bytes; index 1 is mid-codepoint → floor to 0.
        assert_eq!(floor_char_boundary("é", 1), 0);
    }
}
