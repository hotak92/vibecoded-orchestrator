// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Read-only env-var lookup for the launcher frontend.
//!
//! Backs the DiagramsTab Wayland fallback (plan §4 Risk 5 + docs/
//! EXCALIDRAW_WAYLAND_TEST.md): the Svelte needs to read
//! `XDG_SESSION_TYPE` to decide whether to switch from the embedded
//! Excalidraw canvas to "open in browser" mode. Browser/webview
//! JavaScript has no portable way to ask the host OS for an env var
//! on its own, so we expose a thin Tauri command for it.
//!
//! Security posture
//! ----------------
//! This command intentionally exposes a NARROW surface:
//!
//!   * Read-only — there is no `write_env_var` counterpart, and there
//!     never should be. The process env is shared with every spawned
//!     subprocess (MCP servers, vct-hub, hooks); mutating it from a
//!     JS context would have undefined cross-thread effects on Linux.
//!   * Empty string for unset values — callers can't distinguish
//!     "missing" from "set to empty", which is fine for the current
//!     consumer (XDG_SESSION_TYPE: any non-"wayland" value disables
//!     the fallback) and prevents a side-channel where the frontend
//!     probes `is_set` to inventory the user's env.
//!   * Secret-name blocklist — even though the frontend runs in the
//!     same process, we treat it as a separate trust boundary
//!     (third-party JS deps, future plugin loading, dev-tools
//!     remote-debug attaches). If the frontend asks for a name that
//!     matches the secret-shape blocklist, we return "" without ever
//!     touching `std::env::var`. The blocklist is purposefully broad:
//!     prefer false-positives (frontend can't read a non-secret
//!     misnamed `*_KEY` var) over false-negatives (frontend reads a
//!     credential because the regex missed a case).
//!
//! The blocklist is implemented as a simple suffix/substring check —
//! no regex crate dependency, no allocation in the common path. The
//! comparison is case-insensitive: env-var convention is uppercase,
//! but we don't want to trust that convention.

use tauri::command;

/// Extra credential-shaped SEGMENTS beyond the canonical classifier in
/// `mcp_registration::is_secret_shaped_env_key`. The canonical needle
/// list there is [TOKEN, SECRET, PAT, PASSWORD, PASS, AUTH] (+ exact
/// `KEY` / `*_KEY` suffix); this webview-facing blocklist additionally
/// flags PASSWD / CREDENTIAL / CREDENTIALS, which the ~/.claude.json
/// filter never needed but the "broad blocklist" posture here wants.
const EXTRA_SECRET_SEGMENTS: &[&str] = &["PASSWD", "CREDENTIAL", "CREDENTIALS"];

/// Check whether a variable name looks like a credential.
///
/// v0.2.54 (S-4 sibling): UNIFIED on the segment-based classifier in
/// `mcp_registration::is_secret_shaped_env_key`. The previous
/// suffix-only match missed `GH_PAT`, `DB_PASS`, and `AUTH_HEADER`
/// (ends with `_HEADER`, not `_AUTH`) — those round-tripped cleartext
/// to the webview while `mcp_registration`'s own classifier flagged
/// them as secrets. Segment matching (split on `_`/`-`, exact-match
/// each segment) catches all of those while still passing legitimate
/// names like `KEY_FILE_PATH` (segment `KEY` is only flagged as exact
/// name or `_KEY` suffix, mirroring the canonical classifier).
///
/// Posture note: segment matching DOES flag names like
/// `PASSWORD_RESET_URL` (segment `PASSWORD`). That is an accepted
/// false-positive — the doc-comment contract above prefers
/// false-positives over a credential reaching the webview, and the
/// canonical classifier makes the same trade.
fn is_secret_shaped_name(name: &str) -> bool {
    // Canonical segment-based classifier (TOKEN/SECRET/PAT/PASSWORD/
    // PASS/AUTH segments + KEY exact / `_KEY` suffix). Single source
    // of truth — keep behaviour changes THERE, not here.
    if crate::mcp_registration::is_secret_shaped_env_key(name) {
        return true;
    }
    let upper = name.to_ascii_uppercase();
    let segments: Vec<&str> = upper.split(['_', '-']).collect();
    if EXTRA_SECRET_SEGMENTS
        .iter()
        .any(|needle| segments.iter().any(|s| s == needle))
    {
        return true;
    }
    // Whole-name exact match for a few extra well-known credentials,
    // kept as defence-in-depth (all are also segment-caught today).
    const EXACT: &[&str] = &[
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "SUPABASE_KEY",
        "VERCEL_TOKEN",
    ];
    EXACT.iter().any(|e| upper == *e)
}

/// Read a single environment variable from the launcher process and
/// return its value as a string. Unset variables return "". Variables
/// whose name matches the credential-shape blocklist also return ""
/// — the frontend never sees the value, and the redaction is
/// invisible to it (no error signal that would let it probe what's
/// blocked vs what's unset).
///
/// The empty-on-blocklist behaviour is intentional: if we returned
/// an error for blocked names, the frontend could enumerate which
/// names exist on the host by checking which queries error vs which
/// return "" — a side channel we don't want to expose.
#[command]
pub fn read_env_var(name: String) -> Result<String, String> {
    if is_secret_shaped_name(&name) {
        return Ok(String::new());
    }
    Ok(std::env::var(&name).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unset_returns_empty() {
        // Use a name we know is not set in any sane test env.
        let r = read_env_var("VCT_TEST_DEFINITELY_UNSET_zzzz9".into()).unwrap();
        assert_eq!(r, "");
    }

    #[test]
    fn set_returns_value() {
        // Set a one-off env var so the test is hermetic.
        std::env::set_var("VCT_TEST_READ_ENV_VAR_OK", "hello-world");
        let r = read_env_var("VCT_TEST_READ_ENV_VAR_OK".into()).unwrap();
        assert_eq!(r, "hello-world");
        std::env::remove_var("VCT_TEST_READ_ENV_VAR_OK");
    }

    #[test]
    fn blocklist_redacts_token_suffix() {
        std::env::set_var("VCT_TEST_BLOCKED_TOKEN", "supersecret");
        let r = read_env_var("VCT_TEST_BLOCKED_TOKEN".into()).unwrap();
        assert_eq!(r, "", "_TOKEN suffix must be redacted");
        std::env::remove_var("VCT_TEST_BLOCKED_TOKEN");
    }

    #[test]
    fn blocklist_redacts_key_suffix() {
        std::env::set_var("VCT_TEST_BLOCKED_KEY", "supersecret");
        let r = read_env_var("VCT_TEST_BLOCKED_KEY".into()).unwrap();
        assert_eq!(r, "");
        std::env::remove_var("VCT_TEST_BLOCKED_KEY");
    }

    #[test]
    fn blocklist_redacts_password_suffix() {
        std::env::set_var("VCT_TEST_DB_PASSWORD", "hunter2");
        let r = read_env_var("VCT_TEST_DB_PASSWORD".into()).unwrap();
        assert_eq!(r, "");
        std::env::remove_var("VCT_TEST_DB_PASSWORD");
    }

    #[test]
    fn blocklist_redacts_secret_suffix() {
        std::env::set_var("VCT_TEST_FOO_SECRET", "shh");
        let r = read_env_var("VCT_TEST_FOO_SECRET".into()).unwrap();
        assert_eq!(r, "");
        std::env::remove_var("VCT_TEST_FOO_SECRET");
    }

    #[test]
    fn blocklist_is_case_insensitive() {
        std::env::set_var("vct_test_lowercase_token", "shh");
        let r = read_env_var("vct_test_lowercase_token".into()).unwrap();
        assert_eq!(r, "");
        std::env::remove_var("vct_test_lowercase_token");
    }

    #[test]
    fn blocklist_redacts_exact_named_secrets() {
        // GITHUB_TOKEN is in the EXACT list; it should be redacted even
        // though the suffix-only match would also catch it (defence-
        // in-depth). The exact-list path covers names that don't carry
        // a recognisable suffix at all.
        std::env::set_var("GITHUB_TOKEN", "ghp_xxxx");
        let r = read_env_var("GITHUB_TOKEN".into()).unwrap();
        assert_eq!(r, "");
        std::env::remove_var("GITHUB_TOKEN");
    }

    #[test]
    fn non_secret_names_pass_through() {
        // Verify the blocklist doesn't over-block legitimate non-secret
        // names. The intended consumer (XDG_SESSION_TYPE) is the
        // canonical case — must NOT be blocked.
        std::env::set_var("XDG_SESSION_TYPE", "wayland");
        let r = read_env_var("XDG_SESSION_TYPE".into()).unwrap();
        assert_eq!(r, "wayland");
        std::env::remove_var("XDG_SESSION_TYPE");
    }

    #[test]
    fn blocklist_redacts_segment_shapes_previously_missed() {
        // v0.2.54 regression (S-4 sibling): GH_PAT / DB_PASS /
        // AUTH_HEADER round-tripped cleartext under the old
        // suffix-only classifier while mcp_registration flagged them.
        for name in ["VCT_TEST_GH_PAT", "VCT_TEST_DB_PASS", "VCT_TEST_AUTH_HEADER"] {
            std::env::set_var(name, "supersecret");
            let r = read_env_var(name.to_string()).unwrap();
            assert_eq!(r, "", "{name} must be redacted (segment classifier)");
            std::env::remove_var(name);
        }
    }

    #[test]
    fn blocklist_redacts_extra_segments_passwd_credential() {
        for name in ["VCT_TEST_PASSWD_X", "VCT_TEST_CREDENTIAL_BLOB"] {
            std::env::set_var(name, "shh");
            let r = read_env_var(name.to_string()).unwrap();
            assert_eq!(r, "", "{name} must be redacted (extra segments)");
            std::env::remove_var(name);
        }
    }

    #[test]
    fn classifier_agrees_with_mcp_registration_on_its_own_needles() {
        // The unification contract: anything mcp_registration flags,
        // this blocklist must also flag.
        for name in [
            "GH_PAT", "DB_PASS", "AUTH_HEADER", "MY_TOKEN", "X_SECRET",
            "POSTGRES_PASSWORD", "STRIPE_KEY", "KEY",
        ] {
            assert!(
                crate::mcp_registration::is_secret_shaped_env_key(name),
                "precondition: mcp_registration flags {name}"
            );
            assert!(
                is_secret_shaped_name(name),
                "env_cmd blocklist must flag {name} too"
            );
        }
    }

    #[test]
    fn non_secret_names_substring_does_not_over_block() {
        // Names that CONTAIN secret-shaped substrings but don't END
        // with the suffix should pass through. "KEY_FILE_PATH" is not
        // a key; "PASSWORD_RESET_URL" is not a password.
        std::env::set_var("VCT_TEST_KEY_FILE_PATH", "/tmp/k.pem");
        let r = read_env_var("VCT_TEST_KEY_FILE_PATH".into()).unwrap();
        assert_eq!(r, "/tmp/k.pem");
        std::env::remove_var("VCT_TEST_KEY_FILE_PATH");
    }
}
