// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The one name both sides of the `--remove-project` JSON seam spell.
//!
//! `vct-launcher --remove-project` prints its `UnregisterReport`
//! (`launcher/src-tauri/src/commands/projects_v2.rs`) as JSON on stdout; the
//! hub (`vct-hub/src/cli_api.rs`) reads that report to decide whether the
//! removal it delegated really removed THIS project. The report struct
//! serializes camelCase, so its id is `projectId` — a side that hand-spells
//! `project_id` never sees a success and answers 502 for every removal that
//! worked (v0.2.106 review H1). The two crates cannot share the struct (the
//! hub does not depend on the launcher crate), so both read the key through
//! this constant, and a launcher-crate test serializes a REAL
//! `UnregisterReport` and asserts this key carries the id.

/// The JSON key of the project id in the launcher's removal report
/// (`#[serde(rename_all = "camelCase")]` on `UnregisterReport`).
pub const REMOVAL_REPORT_PROJECT_ID_KEY: &str = "projectId";
