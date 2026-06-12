// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.54 Track I — per-boot bearer-token primitives, extracted from
// `vct-hub/src/auth.rs` so a second local HTTP surface (the launcher's
// diagrams editor server, `<vct_root_dir>/diagrams.token`) can reuse
// the exact same generate / persist / compare discipline instead of
// growing a near-identical copy. vct-hub's `auth.rs` now delegates to
// this module for `hub.token`; keep the two call-sites on the SAME
// implementation — drift between token surfaces is how subtle auth
// bugs are born.
//
// Scope: deliberately tiny and dependency-light (rand + hex only — no
// axum/http types). HTTP-layer concerns (HeaderMap extraction,
// middleware, error envelopes) stay in each server; this module owns:
//
//   * token generation from the OS CSPRNG,
//   * 0o600-mode persistence (Unix; default ACL on Windows),
//   * constant-time comparison,
//   * Bearer-scheme parsing from a raw header VALUE string.

use std::path::Path;

use rand::TryRngCore;

/// Token entropy: 32 bytes from the OS CSPRNG, hex-encoded → 64 chars.
/// Standard length for opaque session tokens (matches GitHub PAT
/// classic, Vercel access tokens, etc.).
pub const TOKEN_BYTES: usize = 32;

/// Generate a fresh, cryptographically-random hex token.
///
/// Uses `OsRng` directly so we get bytes from the OS CSPRNG without
/// going through ThreadRng's reseed-from-OS-on-startup dance — for a
/// once-per-process-startup operation that's wasted machinery.
///
/// Returns the lowercase hex encoding (64 chars for 32 bytes).
pub fn generate_token() -> Result<String, String> {
    let mut bytes = [0u8; TOKEN_BYTES];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|e| format!("OS CSPRNG unavailable: {}", e))?;
    Ok(hex::encode(bytes))
}

/// Persist a token to `path` with mode 0o600 on Unix.
///
/// Sequence (Unix):
///   1. mkdir -p the parent directory.
///   2. Open with O_CREAT|O_TRUNC|O_WRONLY and mode 0o600 in a single
///      syscall (`OpenOptions::mode`) — the canonical way to avoid the
///      classic write-then-chmod TOCTOU window where the file briefly
///      exists with the umask's default mode.
///   3. Write the token bytes.
///
/// On Windows we fall through to a plain `std::fs::write`. The default
/// ACL on a file under the user's profile dir is already same-user-only
/// (the user owns it; siblings on the same machine can't read it
/// without admin). Setting NTFS ACLs from Rust without the `windows`
/// crate is heavy; we accept the default-ACL posture and document it.
pub fn write_token_file(path: &Path, token: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all {}: {}", parent.display(), e))?;
    }

    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;

        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;
        f.write_all(token.as_bytes())
            .map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    #[cfg(not(unix))]
    {
        std::fs::write(path, token.as_bytes())
            .map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    Ok(())
}

/// Read a token file, trimming trailing whitespace/newline. Clients
/// that write the file with an editor (debugging) tend to leave a
/// trailing `\n`; trimming makes the round-trip robust.
pub fn read_token_file(path: &Path) -> Result<String, String> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| format!("read {}: {}", path.display(), e))?;
    Ok(raw.trim().to_string())
}

/// Constant-time compare of two byte slices.
///
/// Returns true iff slices are byte-equal. The accumulator pattern
/// (XOR-or each pair, then check the final zero) avoids the early-exit
/// short-circuit that `==` has, which would leak prefix-match length
/// through timing. Both slices are walked in full length-min(a, b);
/// length-mismatch is always false but still does the full walk to
/// avoid leaking via the path that returns early on length difference.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        // Walk anyway to keep timing roughly in line with the matched
        // path — we're returning false either way.
        let mut acc: u8 = 0;
        let n = a.len().min(b.len());
        for i in 0..n {
            acc |= a[i] ^ b[i];
        }
        // Fold in a length-difference signal so the optimizer can't
        // notice the early answer.
        let _ = acc | ((a.len() ^ b.len()) as u8);
        return false;
    }
    let acc = a.iter().zip(b.iter()).fold(0u8, |a, (x, y)| a | (x ^ y));
    acc == 0
}

/// Parse the token out of a raw `Authorization` header VALUE
/// (`"Bearer <token>"`).
///
/// Returns `None` if the value is malformed, not a Bearer scheme, or
/// empty after the prefix. Case-insensitive on the scheme (per RFC
/// 7235 §2.1) but case-sensitive on the token itself. Takes the raw
/// `&str` rather than an HTTP-library HeaderMap so this crate stays
/// free of axum/http dependencies — each server does its own
/// `headers.get(AUTHORIZATION)?.to_str()` dance and hands us the value.
pub fn parse_bearer(raw: &str) -> Option<&str> {
    // Canonical form: "Bearer <token>". We accept any ASCII whitespace
    // between scheme and token, single space being the canonical case.
    let mut parts = raw.splitn(2, char::is_whitespace);
    let scheme = parts.next()?;
    let token = parts.next()?.trim();
    if !scheme.eq_ignore_ascii_case("Bearer") {
        return None;
    }
    if token.is_empty() {
        return None;
    }
    Some(token)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_token_returns_64_hex_chars() {
        let t = generate_token().unwrap();
        assert_eq!(t.len(), 64);
        assert!(t.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn generate_token_returns_distinct_tokens_across_calls() {
        let a = generate_token().unwrap();
        let b = generate_token().unwrap();
        assert_ne!(a, b);
    }

    #[test]
    fn write_then_read_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested").join("x.token");
        write_token_file(&path, "abc123").unwrap();
        assert_eq!(read_token_file(&path).unwrap(), "abc123");
    }

    #[test]
    fn read_token_file_trims_trailing_newline() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("y.token");
        std::fs::write(&path, "tok\n").unwrap();
        assert_eq!(read_token_file(&path).unwrap(), "tok");
    }

    #[cfg(unix)]
    #[test]
    fn token_file_written_with_mode_0o600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("z.token");
        write_token_file(&path, "secret").unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }

    #[test]
    fn write_token_file_overwrites_existing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("w.token");
        write_token_file(&path, "first").unwrap();
        write_token_file(&path, "second").unwrap();
        assert_eq!(read_token_file(&path).unwrap(), "second");
    }

    #[test]
    fn constant_time_eq_basic_cases() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"ab"));
        assert!(!constant_time_eq(b"", b"x"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn parse_bearer_canonical() {
        assert_eq!(parse_bearer("Bearer tok123"), Some("tok123"));
    }

    #[test]
    fn parse_bearer_case_insensitive_scheme() {
        assert_eq!(parse_bearer("bearer tok123"), Some("tok123"));
        assert_eq!(parse_bearer("BEARER tok123"), Some("tok123"));
    }

    #[test]
    fn parse_bearer_rejects_other_schemes_and_empty() {
        assert_eq!(parse_bearer("Basic dXNlcjpwdw=="), None);
        assert_eq!(parse_bearer("Bearer "), None);
        assert_eq!(parse_bearer("Bearer"), None);
        assert_eq!(parse_bearer(""), None);
    }
}
