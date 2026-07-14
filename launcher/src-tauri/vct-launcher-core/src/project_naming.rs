// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Canonical project-name → Weaviate-class-prefix sanitizer (Rust port).
//!
//! Mirror of `vco_lib/project_naming.py::canonical_class_prefix`. The
//! two implementations are pinned together by a shared JSON fixture
//! (`tests/fixtures/project_naming.json`); see the parity tests in
//! `tests/test_project_naming_parity.py` and `tests/project_naming_parity.rs`.
//!
//! See the Python docstring for the full rules-and-rationale write-up.
//! Quick summary:
//!
//! 1. Strip leading/trailing ASCII whitespace.
//! 2. Split on whitespace runs, PascalCase each part (uppercase first
//!    char, preserve rest), concatenate.
//! 3. Replace any remaining char that's not `[A-Za-z0-9_]` with `_`.
//! 4. Verify the result starts with a letter. Reject otherwise.
//!
//! Why this exists: pre-v0.2.15 the launcher's `sanitize_kg_collection`
//! and the Python analyze script's `_sanitize_collection_prefix` produced
//! DIFFERENT prefixes for the same project name. That triggered a
//! case-insensitive Weaviate class-name collision wedge (bug 0.6 / 0.7).
//! This module + the Python sibling + the shared fixture are the
//! single-source-of-truth fix.
//!
//! GAP-CG-3 (2026-07-14): PROMOTED from the Tauri-side launcher crate
//! (`launcher/src-tauri/src/project_naming.rs`) into `vct-launcher-core`
//! so `vct-hub`'s `config_api::resolve_project_config` can call the SAME
//! canonical rule over the project NAME when its code-graph binding row
//! is absent (the fallback previously used a DIVERGENT inline sanitizer
//! over the SLUG). The app crate now re-exports this module
//! (`pub use vct_launcher_core::project_naming;` in `src/lib.rs`), so the
//! six in-crate consumers and the `project_naming_parity.rs` integration
//! test (which imports via `vct_launcher_temp_lib::project_naming`)
//! compile unchanged. A > B > C: one SSOT, no third mirror.

use std::fmt;

/// Reasons `canonical_class_prefix` rejects an input.
///
/// All variants carry the offending input as `String` so error
/// messages can be propagated unchanged to users (we don't strip the
/// raw input — it's the most diagnostic info a downstream caller has).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalPrefixError {
    /// Input was empty (zero length), or whitespace-only (everything
    /// got trimmed off).
    Empty,
    /// Input had no characters left in `[A-Za-z0-9_]` after the
    /// sanitization pass. Stored: the original input.
    SanitizesToEmpty(String),
    /// Sanitized result starts with a non-letter (digit or special),
    /// which Weaviate refuses for class names. Stored: the original
    /// input and the post-sanitize result so the error includes both.
    LeadingNonLetter {
        input: String,
        sanitized: String,
    },
}

impl fmt::Display for CanonicalPrefixError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CanonicalPrefixError::Empty => {
                write!(f, "project_name is empty (or whitespace-only)")
            }
            CanonicalPrefixError::SanitizesToEmpty(input) => {
                write!(f, "project_name {:?} sanitizes to empty string", input)
            }
            CanonicalPrefixError::LeadingNonLetter { input, sanitized } => {
                write!(
                    f,
                    "project_name {:?} sanitizes to {:?}, which starts with a \
                     non-letter character — Weaviate class names must begin \
                     with a letter [A-Z]",
                    input, sanitized
                )
            }
        }
    }
}

impl std::error::Error for CanonicalPrefixError {}

/// Convert a human project name into a Weaviate class prefix. See the
/// module-level doc comment for rules. The Python sibling is the
/// canonical spec; this function MUST behave identically. The
/// `project_naming_parity` integration test (consuming
/// `tests/fixtures/project_naming.json`) catches divergence.
///
/// Returns `Ok(prefix)` on success, `Err(CanonicalPrefixError)` for
/// rejected inputs (empty / all-symbol / leading-non-letter).
pub fn canonical_class_prefix(project_name: &str) -> Result<String, CanonicalPrefixError> {
    // Step 1: strip leading/trailing whitespace.
    let stripped = project_name.trim();
    if stripped.is_empty() {
        return Err(CanonicalPrefixError::Empty);
    }

    // Step 2: split on whitespace runs into word parts. Python's
    // `str.split()` (no arg) collapses runs and drops empty parts;
    // Rust's `split_whitespace()` does exactly the same — matches by
    // Unicode whitespace, never returns empty slices.
    //
    // PascalCase each part: uppercase first char, preserve rest. This
    // is what gives "foo bar" → "FooBar" (not "Foobar"). For the
    // single-token case ("Camel_Case") we still capitalize the
    // first char of the token, which is already "S" — idempotent.
    let mut pascal = String::with_capacity(stripped.len());
    for part in stripped.split_whitespace() {
        let mut chars = part.chars();
        if let Some(first) = chars.next() {
            // ASCII-only uppercase: matches Python's `.upper()` for
            // ASCII chars exactly. For non-ASCII chars we delegate to
            // `to_uppercase` which can return multi-char sequences
            // (e.g. ß → SS) — matches Python's Unicode-aware upper().
            // In practice non-ASCII first chars get stripped by the
            // regex in step 3 anyway, so the produced output is the
            // same on both sides as long as we're consistent here.
            for upper in first.to_uppercase() {
                pascal.push(upper);
            }
            // Preserve the rest of the part verbatim.
            pascal.extend(chars);
        }
    }

    // Defensive: `split_whitespace` on a non-empty stripped string
    // returns at least one part on all inputs we've seen. Guard anyway
    // for the "stripped is non-empty but all chars are exotic
    // whitespace" case.
    if pascal.is_empty() {
        return Err(CanonicalPrefixError::SanitizesToEmpty(
            project_name.to_string(),
        ));
    }

    // Step 3: replace anything not in [A-Za-z0-9_] with a single
    // underscore. We DON'T collapse runs — "Foo--Bar" → "Foo__Bar"
    // (two underscores). Iterating char-by-char so we treat each
    // codepoint independently, exactly matching Python's regex
    // `.sub('_', ...)` semantics.
    let mut cleaned = String::with_capacity(pascal.len());
    for ch in pascal.chars() {
        // Class allowed: ASCII alphanumeric or underscore. Anything else
        // (whitespace, punctuation, non-ASCII) becomes '_'. Python's
        // regex `[^A-Za-z0-9_]` matches exactly this set.
        if (ch.is_ascii_alphanumeric()) || ch == '_' {
            cleaned.push(ch);
        } else {
            cleaned.push('_');
        }
    }

    if cleaned.is_empty() {
        return Err(CanonicalPrefixError::SanitizesToEmpty(
            project_name.to_string(),
        ));
    }

    // Step 4: first char must be an ASCII letter. Reject otherwise
    // (digits, underscores, etc. all fall through to this error).
    // Weaviate would reject server-side too, but we surface it early
    // so callers get a precise validation error rather than a
    // generic "Weaviate refused class create" buried in a 500 body.
    let first = cleaned.chars().next().expect("cleaned non-empty above");
    if !first.is_ascii_alphabetic() {
        return Err(CanonicalPrefixError::LeadingNonLetter {
            input: project_name.to_string(),
            sanitized: cleaned,
        });
    }

    Ok(cleaned)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Quick smoke-test the documented examples. The parity integration
    // test (tests/project_naming_parity.rs) covers the wider fixture.

    #[test]
    fn smoke_demo15() {
        assert_eq!(canonical_class_prefix("MyProject").unwrap(), "MyProject");
    }

    #[test]
    fn smoke_camel_case_preserves_underscore() {
        assert_eq!(
            canonical_class_prefix("Camel_Case").unwrap(),
            "Camel_Case"
        );
    }

    #[test]
    fn smoke_vibecoded_orchestrator_drops_space() {
        assert_eq!(
            canonical_class_prefix("VibeCoded Orchestrator").unwrap(),
            "VibeCodedOrchestrator"
        );
    }

    #[test]
    fn smoke_lowercase_word_uppercases_at_boundary() {
        assert_eq!(canonical_class_prefix("foo bar").unwrap(), "FooBar");
    }

    #[test]
    fn smoke_dash_becomes_underscore() {
        assert_eq!(canonical_class_prefix("Foo-Bar").unwrap(), "Foo_Bar");
    }

    #[test]
    fn smoke_extra_whitespace_collapsed() {
        assert_eq!(
            canonical_class_prefix("  spaced  out  ").unwrap(),
            "SpacedOut"
        );
    }

    #[test]
    fn error_empty_string() {
        assert_eq!(
            canonical_class_prefix(""),
            Err(CanonicalPrefixError::Empty)
        );
    }

    #[test]
    fn error_whitespace_only() {
        assert_eq!(
            canonical_class_prefix("   \t  "),
            Err(CanonicalPrefixError::Empty)
        );
    }

    #[test]
    fn error_leading_digit() {
        assert!(matches!(
            canonical_class_prefix("123abc"),
            Err(CanonicalPrefixError::LeadingNonLetter { .. })
        ));
    }

    #[test]
    fn error_only_underscores() {
        assert!(matches!(
            canonical_class_prefix("_only_"),
            Err(CanonicalPrefixError::LeadingNonLetter { .. })
        ));
    }

    #[test]
    fn error_only_symbols() {
        assert!(matches!(
            canonical_class_prefix("!!!"),
            Err(CanonicalPrefixError::LeadingNonLetter { .. })
        ));
    }

    #[test]
    fn preserves_intra_word_uppercase() {
        // Don't lowercase the middle 'C' in "VibeCoded".
        assert_eq!(canonical_class_prefix("VibeCoded").unwrap(), "VibeCoded");
    }

    #[test]
    fn consecutive_specials_not_collapsed() {
        // Each special char becomes its own underscore.
        assert_eq!(
            canonical_class_prefix("Foo--Bar").unwrap(),
            "Foo__Bar"
        );
    }

    #[test]
    fn underscore_in_single_word_lowercases_only_first() {
        // First char only — "f" → "F", but "_bar" preserved.
        assert_eq!(canonical_class_prefix("foo_bar").unwrap(), "Foo_bar");
    }

    #[test]
    fn idempotent_on_already_valid_input() {
        let inputs = [
            "VibeCodedOrchestrator",
            "Camel_Case",
            "MyProject",
            "Foo_Bar",
        ];
        for inp in inputs {
            let once = canonical_class_prefix(inp).unwrap();
            let twice = canonical_class_prefix(&once).unwrap();
            assert_eq!(once, twice, "Not idempotent for {:?}", inp);
        }
    }

    #[test]
    fn error_display_carries_input() {
        let err = canonical_class_prefix("123abc").unwrap_err();
        let msg = format!("{}", err);
        assert!(
            msg.contains("123abc"),
            "Error message should include offending input: {:?}",
            msg
        );
    }

    // ─────────────────────────────────────────────────────────────────
    // v0.2.34 B2 follow-up — underscore boundary cases.
    //
    // Phase chat's v0.2.33 review surfaced a (since-resolved) suspicion
    // that the Rust port diverged from Python on `_` handling. These
    // five tests pin the Rust-side behaviour explicitly so any future
    // refactor that re-introduces the divergence fails loudly. The
    // shared `tests/fixtures/project_naming.json` covers the same
    // cases for cross-language parity; this module-local test gives
    // a focused first-line signal independent of the fixture loader.
    // ─────────────────────────────────────────────────────────────────

    #[test]
    fn boundary_camel_case_preserves_single_underscore() {
        // Documented Phase-chat suspicion: Rust → "Camel_Case_KnowledgeGraph"
        // (correct), Python → "CamelCase_KnowledgeGraph" (wrong claim).
        // Both implementations actually produce "Camel_Case"; this
        // test pins it so a regression to either of the two legacy
        // sanitizers (which DID drop the underscore) fails here.
        assert_eq!(
            canonical_class_prefix("Camel_Case").unwrap(),
            "Camel_Case"
        );
    }

    #[test]
    fn boundary_double_underscore_preserved_verbatim() {
        // Each underscore passes through the regex unchanged (the
        // negated char class includes `_`), so "foo__bar" stays as
        // "Foo__bar". We do NOT collapse runs of underscores — that
        // would mask user intent (e.g. visually-grouped segments).
        assert_eq!(
            canonical_class_prefix("foo__bar").unwrap(),
            "Foo__bar"
        );
    }

    #[test]
    fn boundary_leading_underscore_rejected_as_non_letter() {
        // Leading `_` fails the step-4 "first char must be ASCII
        // letter" check. The Weaviate server would also reject this,
        // but we surface the error early via LeadingNonLetter so the
        // caller gets a precise validation message.
        let result = canonical_class_prefix("_leading");
        assert!(
            matches!(
                result,
                Err(CanonicalPrefixError::LeadingNonLetter { ref sanitized, .. })
                    if sanitized == "_leading"
            ),
            "Expected LeadingNonLetter with sanitized='_leading', got: {:?}",
            result
        );
    }

    #[test]
    fn boundary_trailing_underscore_preserved() {
        // Trailing underscore is a valid class-name suffix (Weaviate
        // only constrains the FIRST char). We preserve it verbatim
        // because stripping it would be a silent surprise.
        assert_eq!(
            canonical_class_prefix("trailing_").unwrap(),
            "Trailing_"
        );
    }

    #[test]
    fn boundary_three_consecutive_underscores_preserved() {
        // Three consecutive underscores stay as three underscores —
        // same rationale as `boundary_double_underscore_preserved`.
        // This is the strongest "no run-collapsing" pin.
        assert_eq!(
            canonical_class_prefix("foo___bar").unwrap(),
            "Foo___bar"
        );
    }
}
