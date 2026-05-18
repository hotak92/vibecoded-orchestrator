// SPDX-License-Identifier: AGPL-3.0-or-later
// TS shapes for `validate_openai_api_key` and `register_openai_api_key`
// (v0.2.18, Commit 3 / openai_cmd.rs).
//
// Mirror of the Rust types in
// `launcher/src-tauri/src/commands/openai_cmd.rs` — keep them in sync.
// The Rust side serialises `OpenAiValidationResult` with
// `#[serde(tag = "status", rename_all = "snake_case")]`, producing a
// discriminated union the GUI can switch on.

/**
 * Outcome of a `validate_openai_api_key` call. The `status` field is
 * the discriminator; downstream code switches on it.
 *
 *   - `valid`   — key + model usable. `rate_limited=true` when the
 *                 probe got 429 (key is fine, API is throttling).
 *   - `invalid` — key is unusable. `reason` is a short human-readable
 *                 string for the UI; `http_status` may be null when
 *                 the request never reached HTTP (empty-key fast path).
 *   - `error`   — network / DNS / TLS / parse failure. `detail` carries
 *                 the raw error string for debugging. Distinct from
 *                 `invalid`: an Error is "we couldn't decide", not
 *                 "the key is bad".
 */
export type OpenAiValidationResult =
  | { status: 'valid'; model: string; rate_limited: boolean }
  | { status: 'invalid'; reason: string; http_status: number | null }
  | { status: 'error'; detail: string };

/**
 * Success payload from `register_openai_api_key`. `masked_key` is the
 * preview the GUI can echo back (e.g. `sk-...abcd`) without re-querying
 * the keychain. `default_set` mirrors the caller's `set_as_default`
 * argument so dropdown surfaces know whether to refresh.
 */
export interface RegisterOpenAiResponse {
  masked_key: string;
  default_set: boolean;
}
