// SPDX-License-Identifier: AGPL-3.0-or-later
// Part of VibeCoded Orchestrator.
//! [`CheckState`] — the ONE Rust shape for "what did this probe actually
//! establish?".
//!
//! ## Why this type exists (v0.2.92, WP-13)
//!
//! A field tester ran five weeks without a single source update while every
//! surface reported healthy. The mechanism was not one bug but one SHAPE,
//! repeated: a probe that could not complete collapsed into the same value as
//! a probe that completed and found nothing wrong.
//!
//! ```text
//! let commit_count = count_commits_behind_upstream(&repo, &branch)
//!     .await
//!     .unwrap_or(0);                       // a `fatal:` from git becomes 0
//! let available = remote_sha != local_sha && commit_count > 0;   // ⇒ false
//! ```
//!
//! The number `0` there meant two irreconcilable things — "upstream has
//! nothing for you" and "I asked git and git refused to answer" — and the
//! expression below it could not tell them apart. Every downstream surface
//! (tray label, Updates page, the persisted state file) then repeated the
//! false verdict, consistently, which is why nothing invited suspicion.
//!
//! **The rule this type makes structural: a check that cannot distinguish "I
//! could not determine this" from "this is fine" is not a check.**
//!
//! ## Three states, because two are not enough
//!
//! The codebase already had a two-thirds-correct version of this — the
//! `remote_check_ok: bool` + `remote_check_error: Option<String>` pair added
//! to `installer::UpdateStatus` in v0.2.83. It distinguished "failed" from
//! "fine" but had no way to say **not applicable**, so a non-git install
//! (where there is no remote to check and never will be) had to borrow
//! `ok = true` and render as a green "checked, fine". That is a third true
//! state wearing the first one's clothes.
//!
//! * [`CheckState::Ok`] — the probe ran and the answer it produced is usable.
//! * [`CheckState::NotApplicable`] — the probe does not apply to this
//!   install. A determinate fact, with its own user-facing copy; NOT a
//!   success and NOT a failure.
//! * [`CheckState::Unknown`] — the probe could not complete. Carries the
//!   error so the surface can say *why* it could not tell.
//!
//! ## The default is `Unknown`, deliberately
//!
//! [`Default`] yields `Unknown { error: "not checked" }` so a struct that
//! forgets to populate its check field cannot read as healthy. The failure
//! mode of forgetting is then "the GUI says it could not check" — visible,
//! annoying, and correct — rather than "the GUI says you are up to date" —
//! invisible, pleasant, and wrong.
//!
//! ## Wire shape
//!
//! Internally tagged (`#[serde(tag = "state")]`), snake_case:
//!
//! ```json
//! {"state": "ok"}
//! {"state": "not_applicable"}
//! {"state": "unknown", "error": "rev-list: fatal: ambiguous argument …"}
//! ```
//!
//! A TypeScript reader switches on `.state`; there is no field-absence case
//! to guess at, because the enum always serialises the tag. (Contrast the
//! bool pair, whose *absence* the frontend had to interpret — and interpreted
//! as healthy.)
//!
//! ## Enforcement
//!
//! `launcher/src-tauri/tests/check_state_lint.rs` fails the build if any
//! `*Status` struct under `launcher/src-tauri/src/commands/` grows an
//! `*_ok: bool` next to an `*_error: Option<String>` again. The shape is
//! banned by test, not by memory.

use serde::{Deserialize, Serialize};

/// What a probe actually established. See the module docs for the rationale.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum CheckState {
    /// The probe ran to completion; the accompanying value is usable.
    Ok,
    /// The probe does not apply here (e.g. a remote-currency check on an
    /// install that is not a git checkout). A determinate answer — render it
    /// as its own state, never as a success.
    NotApplicable,
    /// The probe could not complete. `error` is a concise, human-readable
    /// reason (a stage label plus the last stderr line is the house style).
    Unknown { error: String },
}

impl Default for CheckState {
    /// `Unknown`, so a struct that forgets to set the field cannot read as
    /// healthy. See the module docs.
    fn default() -> Self {
        CheckState::Unknown {
            error: "not checked".to_string(),
        }
    }
}

impl CheckState {
    /// Build an [`CheckState::Unknown`] from anything string-shaped.
    pub fn unknown(error: impl Into<String>) -> Self {
        CheckState::Unknown {
            error: error.into(),
        }
    }

    /// `true` when the probe produced a DETERMINATE answer — either it ran
    /// (`Ok`) or it established that it does not apply (`NotApplicable`).
    ///
    /// This is the guard to put in front of "compute the verdict": a verdict
    /// derived while `is_known()` is false is a guess wearing a fact's
    /// clothes.
    pub fn is_known(&self) -> bool {
        !matches!(self, CheckState::Unknown { .. })
    }

    /// `true` only for [`CheckState::Unknown`]. The inverse of
    /// [`Self::is_known`], spelled out because `!x.is_known()` reads as a
    /// double negative at call sites that branch on failure.
    pub fn is_unknown(&self) -> bool {
        matches!(self, CheckState::Unknown { .. })
    }

    /// The error text when [`CheckState::Unknown`], else `None`.
    pub fn error(&self) -> Option<&str> {
        match self {
            CheckState::Unknown { error } => Some(error.as_str()),
            _ => None,
        }
    }

    /// One-line human description, suitable for a log line or a tray label.
    /// Deliberately NOT the GUI copy — surfaces own their own wording; this
    /// is for the places that have room for exactly one string.
    pub fn describe(&self) -> String {
        match self {
            CheckState::Ok => "checked".to_string(),
            CheckState::NotApplicable => "not applicable".to_string(),
            CheckState::Unknown { error } => format!("could not check: {error}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_unknown_not_ok() {
        // The whole point: a struct that forgets its check field must not
        // read as healthy.
        let d = CheckState::default();
        assert!(d.is_unknown(), "default must be Unknown, got {d:?}");
        assert!(!d.is_known());
        assert_eq!(d.error(), Some("not checked"));
    }

    #[test]
    fn not_applicable_is_known_but_is_not_ok() {
        let na = CheckState::NotApplicable;
        assert!(na.is_known(), "NotApplicable is a determinate answer");
        assert!(!na.is_unknown());
        assert_ne!(na, CheckState::Ok, "NotApplicable must not equal Ok");
        assert_eq!(na.error(), None);
    }

    #[test]
    fn unknown_carries_its_reason() {
        let u = CheckState::unknown("rev-list: fatal: ambiguous argument");
        assert!(u.is_unknown());
        assert_eq!(u.error(), Some("rev-list: fatal: ambiguous argument"));
        assert!(u.describe().contains("could not check"));
        assert!(u.describe().contains("ambiguous argument"));
    }

    #[test]
    fn wire_shape_is_internally_tagged_snake_case() {
        assert_eq!(
            serde_json::to_string(&CheckState::Ok).unwrap(),
            r#"{"state":"ok"}"#
        );
        assert_eq!(
            serde_json::to_string(&CheckState::NotApplicable).unwrap(),
            r#"{"state":"not_applicable"}"#
        );
        assert_eq!(
            serde_json::to_string(&CheckState::unknown("boom")).unwrap(),
            r#"{"state":"unknown","error":"boom"}"#
        );
    }

    #[test]
    fn wire_shape_round_trips() {
        for original in [
            CheckState::Ok,
            CheckState::NotApplicable,
            CheckState::unknown("network unreachable"),
        ] {
            let json = serde_json::to_string(&original).unwrap();
            let back: CheckState = serde_json::from_str(&json).unwrap();
            assert_eq!(original, back, "round-trip lost information for {json}");
        }
    }

    #[test]
    fn a_struct_that_omits_the_field_deserialises_as_unknown() {
        // Belt and braces for the "older writer" direction: a persisted blob
        // from a build that predates the field must NOT rehydrate as healthy.
        #[derive(Deserialize)]
        struct Holder {
            #[serde(default)]
            check: CheckState,
        }
        let h: Holder = serde_json::from_str("{}").unwrap();
        assert!(
            h.check.is_unknown(),
            "an absent check field must rehydrate as Unknown, got {:?}",
            h.check
        );
    }
}
