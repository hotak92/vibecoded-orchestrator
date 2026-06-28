// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Severity classification for the SYNCHRONOUS `update_project_v2` warning
// stream (plain `Vec<String>` on `UpdateProjectResult.warnings`).
//
// WHY this exists: the async setup path (`project://setup-progress`) already
// carries STRUCTURED `SetupWarning { message, severity }` items and the
// frontend routes them info/error in `project-setup.ts`. The synchronous
// `projects.ts::update()` path, however, gets PLAIN strings and historically
// rendered EVERY one as a red `toast.error()` — so the informational
// "additive schema migration auto-applied (… vectors preserved)" SUCCESS
// notice mis-signalled as an error (v0.2.70 NIT, A2-NIT1).
//
// We classify by string CONTENT, mirroring the Rust source-of-truth
// `classify_warning` in
// `launcher/src-tauri/src/commands/project_setup.rs`.
//
// ⚠️ MUST MATCH `project_setup.rs::classify_warning`. The two markers lists
// below are duplicated there (Rust `is_error` / `is_deferral`). If you add a
// marker on one side, add it on the other or the sync + async toast streams
// will disagree on the same message text.

/** Genuine-failure markers (red toast). Mirrors Rust `is_error`. */
const ERROR_MARKERS = [
  'error',
  'failed to start',
  'subprocess failed',
  'unparseable',
  'did not become healthy',
];

/**
 * Clean-deferral markers. Mirrors Rust `is_deferral`. A string that contains
 * BOTH an error marker and a deferral marker is treated as a deferral (info) —
 * same precedence as the Rust side, where `is_error && !is_deferral` gates red.
 */
const DEFERRAL_MARKERS = [
  'bootstrap deferred',
  'collections will be created when',
  'safe_add_skipped_env_merge',
];

/**
 * True when a synchronous update warning string should render as a red error
 * toast; false → informational (amber/info) toast.
 *
 * Mirror of `classify_warning` (Rust): `is_error && !is_deferral`.
 */
export function isErrorWarning(raw: string): boolean {
  const lower = raw.toLowerCase();
  const isError = ERROR_MARKERS.some((m) => lower.includes(m));
  const isDeferral = DEFERRAL_MARKERS.some((m) => lower.includes(m));
  return isError && !isDeferral;
}
