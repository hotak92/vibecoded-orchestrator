// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Thin re-export of the single-line secret-value shape predicate (v0.2.80 A4).
//!
//! The predicate itself LIVES in `vct-launcher-core`
//! (`vct_launcher_core::secret_value_shape`) so the keychain-write chokepoint
//! `secrets::set` — which is in that lower-layer crate — can call it. A2
//! originally landed the module here in the app crate; A4 moved the body down
//! to core (core cannot call *up* into the app crate) and left this file as a
//! re-export so A2's existing call-sites keep compiling UNCHANGED:
//!
//!   * `secrets_import.rs` — `read_whole_file_trimmed` boundary #3 +
//!     the shared-fixture parity `#[test]` (calls both symbols).
//!   * `installer.rs` — `migrate_github_pat_file_to_keychain` boundary #5 +
//!     `github_pat_from_legacy_file` boundary #4.
//!
//! There is exactly ONE Rust copy of the predicate — this is just an alias into
//! it. The unit tests + the taxonomy classifier now live in the core module.
//! The cross-language parity `#[test]` against
//! `tests/fixtures/secret_value_shape_parity.json` stays in `secrets_import.rs`
//! (it exercises this re-export, so it keeps the app-crate call path covered).

// `is_single_line_secret` is called from non-test boundary code
// (secrets_import.rs #3, installer.rs #4/#5), so it's always live.
pub(crate) use vct_launcher_core::secret_value_shape::is_single_line_secret;

// `classify_secret_value` is only referenced by the shared-fixture parity
// `#[test]` in `secrets_import.rs`; re-exporting it unconditionally would be an
// unused import in release builds. `#[allow(unused_imports)]` keeps the alias
// available to the test path without a warning when tests are compiled out.
#[allow(unused_imports)]
pub(crate) use vct_launcher_core::secret_value_shape::classify_secret_value;
