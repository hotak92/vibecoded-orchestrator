// SPDX-License-Identifier: AGPL-3.0-or-later
//! The ONE Rust home for parsing and ordering orchestrator/module versions.
//!
//! **Rule (v0.2.100, owner ruling Q7):** a version is exactly three numeric
//! parts — `^v?(\d+)\.(\d+)\.(\d+)$` over ASCII digits. No pre-release
//! suffix, no fourth number, no surrounding whitespace. Anything else is a
//! [`VersionParseError`] carrying the offending string; it is never ranked.
//!
//! Sibling homes answer from the SAME case table,
//! `tests/fixtures/version_order_cases.json`:
//! `vco_lib/version_compare.py` (Python) and
//! `launcher/src/lib/version-compare.ts` (TypeScript).
//!
//! **Superseded (recorded):** before v0.2.100 the launcher carried seven
//! private copies, most taking each dotted part's LEADING digit run, so
//! `0.2.28-dev == 0.2.28`, `0.2 == 0.2.0` and a garbage part read as `0`.
//! Two tests pinned that tolerance as a feature
//! (`installer_engine::version_lt_tolerates_suffixes`,
//! `module_updates::semver_less_handles_prerelease_suffixes`) and
//! `module_updates::ModuleUpdateAvailable`'s docstring promised "pre-release
//! suffixes are ignored". The owner's rule replaces all three: no producer
//! emits a suffix, so a suffixed string reaching a comparison is a defect to
//! report. Callers map the error to their own `unknown` / refusal — a parse
//! error is never "up to date", never "update available", never "fresh".
//!
//! Direction helpers take `(a, b)` and read as sentences:
//! `is_older(running, on_disk)` == "running is older than on_disk".

use std::cmp::Ordering;
use std::fmt;

/// `text` is not a strict `X.Y.Z` (optionally `v`-prefixed) version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionParseError {
    /// The offending value exactly as received — put it in the message the
    /// user sees.
    pub text: String,
}

impl fmt::Display for VersionParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "version {:?} is not X.Y.Z (three numeric parts, no suffix)",
            self.text
        )
    }
}

impl std::error::Error for VersionParseError {}

/// `"0.2.100"` / `"v0.2.100"` → `(0, 2, 100)`; anything else is an error.
pub fn parse(text: &str) -> Result<(u64, u64, u64), VersionParseError> {
    let err = || VersionParseError {
        text: text.to_string(),
    };
    let body = text.strip_prefix('v').unwrap_or(text);
    let mut parts = body.split('.');
    let mut next = || -> Result<u64, VersionParseError> {
        let part = parts.next().ok_or_else(err)?;
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
            return Err(err());
        }
        // All-digit, so the only failure left is u64 overflow.
        part.parse::<u64>().map_err(|_| err())
    };
    let triple = (next()?, next()?, next()?);
    if parts.next().is_some() {
        return Err(err());
    }
    Ok(triple)
}

/// Order two versions; an error when EITHER side is unparseable.
pub fn cmp(a: &str, b: &str) -> Result<Ordering, VersionParseError> {
    Ok(parse(a)?.cmp(&parse(b)?))
}

/// `a < b` — "a is older than b".
pub fn is_older(a: &str, b: &str) -> Result<bool, VersionParseError> {
    Ok(cmp(a, b)? == Ordering::Less)
}

/// `a > b` — "a is newer than b".
pub fn is_newer(a: &str, b: &str) -> Result<bool, VersionParseError> {
    Ok(cmp(a, b)? == Ordering::Greater)
}

/// `true` iff `text` is a strict version (e.g. filtering remote tags).
pub fn is_strict(text: &str) -> bool {
    parse(text).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table() -> serde_json::Value {
        let text = include_str!("../../../../tests/fixtures/version_order_cases.json");
        serde_json::from_str(text).expect("version_order_cases.json parses")
    }

    fn ordering(n: i64) -> Ordering {
        match n {
            -1 => Ordering::Less,
            0 => Ordering::Equal,
            1 => Ordering::Greater,
            other => panic!("bad cmp value in fixture: {other}"),
        }
    }

    #[test]
    fn order_rows_from_the_shared_table() {
        let t = table();
        let rows = t["order"].as_array().expect("order");
        assert!(rows.len() >= 6, "the table shrank: {}", rows.len());
        for row in rows {
            let a = row["a"].as_str().unwrap();
            let b = row["b"].as_str().unwrap();
            let want = ordering(row["cmp"].as_i64().unwrap());
            assert_eq!(cmp(a, b), Ok(want), "cmp({a}, {b})");
            assert_eq!(cmp(b, a), Ok(want.reverse()), "cmp({b}, {a})");
            assert_eq!(is_older(a, b), Ok(want == Ordering::Less), "is_older({a}, {b})");
            assert_eq!(is_newer(a, b), Ok(want == Ordering::Greater), "is_newer({a}, {b})");
        }
    }

    #[test]
    fn reject_rows_from_the_shared_table() {
        let t = table();
        let rows = t["reject"].as_array().expect("reject");
        assert!(rows.len() >= 6, "the table shrank: {}", rows.len());
        for row in rows {
            let bad = row.as_str().unwrap();
            let e = parse(bad).expect_err(bad);
            assert_eq!(e.text, bad, "the error carries the offending string");
            assert!(!is_strict(bad));
            assert!(cmp(bad, "0.2.100").is_err(), "cmp({bad:?}, _) must not rank");
            assert!(cmp("0.2.100", bad).is_err(), "cmp(_, {bad:?}) must not rank");
            assert!(is_older(bad, "0.2.100").is_err());
            assert!(is_newer("0.2.100", bad).is_err());
        }
    }

    #[test]
    fn overflow_is_a_parse_error_not_a_panic() {
        assert!(parse("99999999999999999999999.0.0").is_err());
    }

    #[test]
    fn the_error_message_names_the_string() {
        let e = parse("0.2.100-rc1").unwrap_err();
        assert!(e.to_string().contains("\"0.2.100-rc1\""), "{e}");
    }
}
