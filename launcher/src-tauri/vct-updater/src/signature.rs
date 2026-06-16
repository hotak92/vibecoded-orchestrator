// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Artifact trust verification for the inverted updater (DESIGN-v0300 §8 R3).
//
// ============================================================================
// DORMANT in v0.2.60.
// ============================================================================
// Nothing in the live v0.2.60 update path calls this module. It is the
// trust layer the v0.3.0 bootstrap stub uses BEFORE it execs a fetched
// engine binary: the stub fetches the single small `vct-updater` artifact,
// verifies it here, and only then spawns it (DESIGN §3.1 step 2, §3.2).
//
// ============================================================================
// What is REAL vs TODO in v0.2.60 (read this before auditing).
// ============================================================================
//
//   REAL (fully implemented + tested here):
//     * `sha256_file` — streaming sha256 of an on-disk artifact.
//     * `verify_sha256` — constant-context comparison of an artifact's
//       sha256 against a pinned expected digest. This is the load-bearing
//       integrity check: the release pipeline already publishes a `.sha256`
//       sidecar per archive (`.github/workflows/release.yml` "Compute archive
//       checksum"), so the digest the stub pins is a value we ALREADY emit.
//       An attacker who swaps the artifact bytes fails this check.
//     * `parse_sha256_sidecar` — parse a `sha256sum`-format sidecar line
//       (`<hex>  <filename>`), the exact format release.yml writes.
//
//   TODO (interface designed, key-management half deliberately deferred —
//   SAID SO in the summary, NOT a silent no-op):
//     * `verify_detached_signature` — verifies an ed25519 detached signature
//       of the `.sha256` MANIFEST against a public key compiled into the
//       binary. The SCHEME is fixed here (sign the sha256 manifest, not the
//       multi-MB binary, so the signed payload is tiny + the integrity check
//       stays sha256); the missing half is (a) the ed25519 verify
//       implementation (needs an audited crypto dep — `ed25519-dalek` —
//       which is NOT yet added to keep the frozen stub dependency-light until
//       the key-management decision lands) and (b) the actual pinned public
//       key + a release-pipeline signing step. Until that lands,
//       `verify_detached_signature` returns `SignatureCheck::NotConfigured`
//       and the caller treats the artifact as sha256-verified-only.
//
// Why sign the manifest, not the binary (the scheme decision):
//   * The integrity guarantee already comes from sha256 (an attacker must
//     produce a binary whose sha256 matches the pinned digest — infeasible).
//   * The signature's job is AUTHENTICITY of the digest list: it proves the
//     `.sha256` values came from the VibeCoded release key, so an attacker
//     who controls the release host cannot serve a different (validly-hashed)
//     binary by also rewriting the sidecar. Signing the small text manifest
//     keeps the signed payload bytes tiny and the verify cheap.
//   * This mirrors minisign/signify (sign a digests file) + apt's
//     `Release.gpg` over the `Release` (which lists package hashes) — the
//     pattern the design's companion research cites.

// DORMANT in v0.2.60: nothing live calls this trust layer (the engine +
// bootstrap that use it are themselves dormant). dead_code is intentional
// until v0.3.0 wires the inverted updater.
#![allow(dead_code)]

use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

use sha2::{Digest, Sha256};

/// Result of the detached-signature check. Distinguishes "no signature
/// infra configured yet" (v0.2.60 — sha256-only trust) from a genuine
/// verify pass/fail, so the caller never confuses an unconfigured layer
/// with a verified one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SignatureCheck {
    /// The detached ed25519 signature verified against the pinned public
    /// key. (Unreachable in v0.2.60 — the key-management half is TODO.)
    Verified,
    /// A signature was present but did NOT verify — REFUSE the artifact.
    Invalid(String),
    /// No signing infrastructure is configured in this build (v0.2.60).
    /// The caller falls back to sha256-only trust and SAYS SO in its log;
    /// it does NOT treat this as "verified".
    NotConfigured,
}

/// Outcome of the full artifact trust check (sha256 + optional signature).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrustOutcome {
    /// True iff the sha256 matched the pinned digest. REQUIRED to proceed.
    pub sha256_ok: bool,
    /// The detached-signature result (advisory in v0.2.60: NotConfigured).
    pub signature: SignatureCheck,
    /// Human-readable detail for the engine log / forensic trail.
    pub detail: String,
}

impl TrustOutcome {
    /// The caller may exec the artifact iff sha256 matched AND the signature
    /// is not an explicit `Invalid` (a `NotConfigured` signature is allowed
    /// in v0.2.60 — sha256-only trust; once signing lands, the caller can
    /// tighten this to require `Verified`).
    ///
    /// TODO(v0.3.0-R3): BEFORE the inverted updater path (bootstrap → fetch
    /// → exec) is ever wired LIVE, this gate MUST be tightened to require
    /// `SignatureCheck::Verified` — NOT merely `!Invalid`. As-is,
    /// `NotConfigured` (the dormant-v0.2.60 state, since no ed25519 key is
    /// wired) passes `may_exec`, so sha256-only trust is the effective bar.
    /// That does NOT mitigate the design's R3 threat: a compromised release
    /// host can serve a malicious binary AND a matching `.sha256` sidecar
    /// (the sha pin is unauthenticated). Shipping the inverted path live
    /// while this still accepts `NotConfigured` would bake in a
    /// HIGH-severity supply-chain hole under a green test suite. When
    /// activating in v0.3.0: require `Verified` here (gate sha256-only behind
    /// an explicit `--allow-unsigned` dev flag), wire the pinned public key,
    /// and add the signing step to the release pipeline. This is INERT today
    /// only because nothing calls into bootstrap/engine in v0.2.60.
    pub fn may_exec(&self) -> bool {
        self.sha256_ok && !matches!(self.signature, SignatureCheck::Invalid(_))
    }
}

/// Streaming sha256 of a file. 64 KiB chunks so a multi-MB binary never
/// loads fully into memory. REAL — this is the load-bearing integrity hash.
pub fn sha256_file(path: &Path) -> Result<String, String> {
    let file = File::open(path).map_err(|e| format!("open {}: {}", path.display(), e))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| format!("read {}: {}", path.display(), e))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Verify an artifact's sha256 against a pinned `expected` hex digest.
/// Case-insensitive hex compare; trims whitespace. REAL.
pub fn verify_sha256(path: &Path, expected: &str) -> Result<bool, String> {
    let actual = sha256_file(path)?;
    let expected = expected.trim().to_ascii_lowercase();
    Ok(actual.eq_ignore_ascii_case(&expected) && !expected.is_empty())
}

/// Parse a `sha256sum`-format sidecar line into its hex digest. The release
/// pipeline writes `<64-hex>  <filename>\n` (GNU coreutils / shasum -a 256).
/// We tolerate one or more spaces and an optional `*` binary marker. REAL —
/// this is exactly the format `.github/workflows/release.yml` emits.
pub fn parse_sha256_sidecar(content: &str) -> Result<String, String> {
    let first = content
        .lines()
        .next()
        .ok_or_else(|| "empty sha256 sidecar".to_string())?;
    let hex = first
        .split_whitespace()
        .next()
        .ok_or_else(|| "malformed sha256 sidecar (no digest token)".to_string())?
        .trim_start_matches('*');
    if hex.len() != 64 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(format!(
            "sha256 sidecar digest is not 64 hex chars: {:?}",
            hex
        ));
    }
    Ok(hex.to_ascii_lowercase())
}

/// Verify a detached ed25519 signature of the sha256 MANIFEST against a
/// pinned public key.
///
/// TODO (v0.3.0 — key management half): wire in an audited ed25519 verify
/// (`ed25519-dalek`) + the pinned `VCT_RELEASE_PUBKEY` + a release-pipeline
/// signing step. Until that lands this returns `NotConfigured` so the caller
/// uses sha256-only trust and logs that the signature layer is dormant. This
/// is NOT a silent no-op: the distinct `NotConfigured` variant forces the
/// caller to acknowledge the absence rather than mistaking it for a pass.
///
/// `_manifest_bytes` is the exact bytes that were signed (the `.sha256`
/// sidecar). `_signature` is the detached signature bytes. Both are accepted
/// now so the call-site + wire shape are stable; they are unused until the
/// crypto half lands.
pub fn verify_detached_signature(
    _manifest_bytes: &[u8],
    _signature: Option<&[u8]>,
) -> SignatureCheck {
    // v0.2.60: no pinned public key + no audited ed25519 dep is wired yet.
    // Return NotConfigured — the caller falls back to sha256-only trust.
    //
    // When activated, the body becomes (sketch):
    //   let Some(sig) = _signature else { return SignatureCheck::NotConfigured };
    //   let pubkey = VerifyingKey::from_bytes(&VCT_RELEASE_PUBKEY)?;
    //   match pubkey.verify_strict(_manifest_bytes, &Signature::from_slice(sig)?) {
    //       Ok(()) => SignatureCheck::Verified,
    //       Err(e) => SignatureCheck::Invalid(e.to_string()),
    //   }
    SignatureCheck::NotConfigured
}

/// Full trust check for a fetched engine artifact: sha256 (REAL, required)
/// + detached signature (TODO key-mgmt → NotConfigured in v0.2.60). The
/// engine/bootstrap caller refuses to exec unless `TrustOutcome::may_exec()`.
///
/// `expected_sha256` is the pinned digest the stub carries in the plan
/// (or read from a fetched `.sha256` sidecar). `manifest_bytes` +
/// `signature` feed the (dormant) signature layer.
pub fn verify_artifact(
    artifact: &Path,
    expected_sha256: &str,
    manifest_bytes: Option<&[u8]>,
    signature: Option<&[u8]>,
) -> Result<TrustOutcome, String> {
    let sha256_ok = verify_sha256(artifact, expected_sha256)?;
    let signature = match manifest_bytes {
        Some(mb) => verify_detached_signature(mb, signature),
        None => SignatureCheck::NotConfigured,
    };
    let detail = match (&sha256_ok, &signature) {
        (true, SignatureCheck::Verified) => "sha256 + ed25519 signature verified".to_string(),
        (true, SignatureCheck::NotConfigured) => {
            "sha256 verified; ed25519 signature layer NOT configured in this build \
             (v0.2.60 — sha256-only trust)"
                .to_string()
        }
        (true, SignatureCheck::Invalid(e)) => {
            format!("sha256 matched but signature INVALID — refusing: {}", e)
        }
        (false, _) => format!(
            "sha256 MISMATCH for {} (expected {}) — refusing",
            artifact.display(),
            expected_sha256.trim()
        ),
    };
    Ok(TrustOutcome {
        sha256_ok,
        signature,
        detail,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn tmp_with(bytes: &[u8]) -> (tempfile::TempDir, std::path::PathBuf) {
        let td = tempfile::tempdir().unwrap();
        let p = td.path().join("artifact.bin");
        let mut f = File::create(&p).unwrap();
        f.write_all(bytes).unwrap();
        (td, p)
    }

    #[test]
    fn sha256_of_known_input() {
        // sha256("abc") = ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
        let (_td, p) = tmp_with(b"abc");
        let digest = sha256_file(&p).unwrap();
        assert_eq!(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha256_of_empty_file() {
        // sha256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
        let (_td, p) = tmp_with(b"");
        assert_eq!(
            sha256_file(&p).unwrap(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn verify_sha256_matches_and_mismatches() {
        let (_td, p) = tmp_with(b"abc");
        assert!(verify_sha256(
            &p,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
        .unwrap());
        // Case-insensitive.
        assert!(verify_sha256(
            &p,
            "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
        )
        .unwrap());
        // Mismatch.
        assert!(!verify_sha256(&p, "0".repeat(64).as_str()).unwrap());
        // Empty expected → never matches.
        assert!(!verify_sha256(&p, "").unwrap());
    }

    #[test]
    fn parse_sidecar_gnu_format() {
        // GNU coreutils format: "<hex>  <filename>"
        let line = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  vct-orchestrator-linux-x64.tar.gz\n";
        assert_eq!(
            parse_sha256_sidecar(line).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn parse_sidecar_binary_marker() {
        // shasum -a 256 binary marker: "*filename"
        let line = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad *foo.bin";
        assert_eq!(
            parse_sha256_sidecar(line).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn parse_sidecar_rejects_garbage() {
        assert!(parse_sha256_sidecar("").is_err());
        assert!(parse_sha256_sidecar("notahash file").is_err());
        assert!(parse_sha256_sidecar("abc123 file").is_err()); // too short
    }

    // The signature layer is DORMANT but must be honest: it returns
    // NotConfigured, never a spurious Verified.
    #[test]
    fn signature_layer_is_not_configured_in_v0260() {
        assert_eq!(
            verify_detached_signature(b"any manifest", Some(b"any sig")),
            SignatureCheck::NotConfigured
        );
        assert_eq!(
            verify_detached_signature(b"any manifest", None),
            SignatureCheck::NotConfigured
        );
    }

    // The full trust check: sha256 pass + NotConfigured signature → may_exec
    // (sha256-only trust in v0.2.60), and the detail string SAYS the sig
    // layer is unconfigured so an operator reading the log is never misled.
    #[test]
    fn verify_artifact_sha256_only_trust_is_explicit() {
        let (_td, p) = tmp_with(b"abc");
        let outcome = verify_artifact(
            &p,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            Some(b"sha256sum-manifest-bytes"),
            None,
        )
        .unwrap();
        assert!(outcome.sha256_ok);
        assert_eq!(outcome.signature, SignatureCheck::NotConfigured);
        assert!(outcome.may_exec(), "sha256-only trust allows exec in v0.2.60");
        assert!(
            outcome.detail.contains("NOT configured"),
            "the detail must explicitly state the signature layer is dormant, got: {}",
            outcome.detail
        );
    }

    // A sha256 MISMATCH must block exec regardless of the signature layer.
    #[test]
    fn verify_artifact_sha256_mismatch_blocks_exec() {
        let (_td, p) = tmp_with(b"abc");
        let outcome = verify_artifact(&p, &"f".repeat(64), None, None).unwrap();
        assert!(!outcome.sha256_ok);
        assert!(!outcome.may_exec(), "sha256 mismatch must refuse exec");
        assert!(outcome.detail.contains("MISMATCH"));
    }
}
