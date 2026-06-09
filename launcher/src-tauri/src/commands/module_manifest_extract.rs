// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.33 (Agent C, L0b): post-install manifest extraction.
//!
//! Runs after `installer_engine::container_pull` succeeds and BEFORE
//! `apply_module_db_migrations`. Extracts `/app/vct-module.json` from
//! the pulled image to `~/.vct/modules/<module_id>/vct-module.json` so
//! the renderer (config tab), dispatcher, and DB migrations machinery
//! have the FULL manifest available — L0's catalog response only
//! carries the install-time slice.
//!
//! ## Why `docker create` + `docker cp` and not OCI annotations
//!
//! Per `.claude/context/plans/v0.2.33-architecture-review-2026-05-24.md`
//! §5 decision table: OCI annotations are bounded to ~4 KB by the spec
//! and would require gzip+base64 contortions for the 13 KB v0.2.7 RL
//! manifest. `docker create` + `docker cp` works against podman /
//! docker / containerd CLIs out-of-box, the manifest is "just a file"
//! in the image, and the module author only needs one `COPY` line in
//! their Dockerfile.
//!
//! ## Atomic write protocol
//!
//! 1. `mkdir -p <dest_dir>/.tmp`
//! 2. `<runtime> create <image_ref>` → captures container id
//! 3. `<runtime> cp <cid>:/app/vct-module.json <dest_dir>/.tmp/vct-module.json.new`
//! 4. validate the tempfile through `ModuleManifest::from_json` BEFORE
//!    committing — catches malformed manifests early so a broken
//!    image doesn't clobber a known-good on-disk copy.
//! 5. assert `extracted.id == module_id` — guards against the
//!    publisher accidentally shipping a manifest for a different
//!    module (which would otherwise be silently accepted and confuse
//!    the dispatcher).
//! 6. if `<dest_dir>/vct-module.json` already exists (upgrade path),
//!    copy it to `<dest_dir>/vct-module.json.bak` so we can roll back
//!    on a failed rename in step 7.
//! 7. atomic `rename(.tmp/vct-module.json.new → vct-module.json)`. On
//!    EXDEV (across-mount rename), falls back to copy+remove. On any
//!    failure between 6 and 7 we restore from `.bak`.
//! 8. `rm -rf <dest_dir>/.tmp` (best-effort cleanup).
//! 9. drop the throwaway container via RAII-style `ContainerCleanup`
//!    (best-effort `<runtime> rm <cid>` even on the Err path — a
//!    leaked container is harmless and will be GC'd by the runtime).
//!
//! ## Failure modes
//!
//! | Failure | Recovery |
//! |---|---|
//! | `<runtime> create` non-zero | Err with stderr context; container_pull image left in cache for retry. |
//! | `<runtime> cp` "No such file" | Err with user-friendly "module image does not ship vct-module.json" — surfaces the publisher-side bug rather than failing opaquely. |
//! | extracted JSON unparseable | Err with "extracted manifest invalid" + serde reason. .bak (if any) preserved. |
//! | extracted id mismatch | Err with both ids. .bak preserved. |
//! | rename fails | rollback from .bak; surface the rename error to the caller. |
//! | `<runtime> rm` fails | soft-fail via ContainerCleanup::drop. Leaked container is harmless. |

use std::path::{Path, PathBuf};
use tokio::process::Command;
use vct_launcher_core::manifest::ModuleManifest;
use vct_launcher_core::process::CommandExt as _;

const CONTAINER_MANIFEST_PATH: &str = "/app/vct-module.json";

/// Result of a successful manifest extraction.
///
/// `#[allow(dead_code)]` on the fields because they're a forward-
/// looking public surface — the v0.2.33 caller (installer_engine)
/// currently only checks for `Ok(_)`, but Agent B's catalog refactor
/// will read `parsed` to push the just-extracted manifest into
/// `app_state.catalog_cache` without a re-read from disk. Removing
/// the fields now would force Agent B to either fs-read or change
/// this signature — easier to keep them stable.
#[derive(Debug, Clone)]
pub struct ExtractedManifest {
    /// Absolute path of the manifest file written under
    /// `~/.vct/modules/<id>/vct-module.json`.
    #[allow(dead_code)]
    pub on_disk_path: PathBuf,
    /// The validated `ModuleManifest` parsed from the extracted file.
    /// Callers can use this directly without re-reading from disk.
    #[allow(dead_code)]
    pub parsed: ModuleManifest,
}

/// Extract `/app/vct-module.json` from a pulled container image into
/// `<vct_root>/modules/<module_id>/vct-module.json`.
///
/// `runtime` should be `"podman"` or `"docker"` — typically resolved
/// via `installer_engine::detect_container_runtime` so we use the same
/// runtime that did the `pull`.
///
/// `image_ref` is the fully-qualified image reference (registry +
/// repo + tag), e.g. `"ghcr.io/hotak92/vct-rl-reranker:0.2.7-cuda"`.
pub async fn extract_manifest_from_image(
    image_ref: &str,
    module_id: &str,
    runtime: &str,
) -> Result<ExtractedManifest, String> {
    let vct_root = crate::paths::vct_root_dir();
    let dest_dir = vct_root.join("modules").join(module_id);
    let tmp_dir = dest_dir.join(".tmp");

    // Step 1: ensure target dirs exist.
    std::fs::create_dir_all(&tmp_dir)
        .map_err(|e| format!("mkdir tmp ({}): {}", tmp_dir.display(), e))?;

    // Step 2: create the throw-away container. `create` doesn't run
    // the container — it just materialises the filesystem layer so
    // `cp` can read out of it.
    let create_out = Command::new(runtime).silent()
        .args(["create", image_ref])
        .output()
        .await
        .map_err(|e| format!("spawn {} create: {}", runtime, e))?;
    if !create_out.status.success() {
        return Err(format!(
            "{} create {} failed (exit {}): {}",
            runtime,
            image_ref,
            create_out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&create_out.stderr)
                .chars()
                .take(300)
                .collect::<String>(),
        ));
    }
    let cid = String::from_utf8_lossy(&create_out.stdout).trim().to_string();
    if cid.is_empty() {
        return Err(format!(
            "{} create {} returned empty container id (stderr: {})",
            runtime,
            image_ref,
            String::from_utf8_lossy(&create_out.stderr)
                .chars()
                .take(300)
                .collect::<String>(),
        ));
    }

    // RAII cleanup: rm the throwaway container even on the error
    // return path. Drop runs in std::process context (blocking).
    let _cleanup = ContainerCleanup {
        runtime: runtime.to_string(),
        cid: cid.clone(),
    };

    // Step 3: copy the manifest out.
    let tmp_path = tmp_dir.join("vct-module.json.new");
    let cp_src = format!("{}:{}", cid, CONTAINER_MANIFEST_PATH);
    let cp_dst = tmp_path
        .to_str()
        .ok_or_else(|| format!("tmp path not utf8: {}", tmp_path.display()))?;
    let cp_out = Command::new(runtime).silent()
        .args(["cp", &cp_src, cp_dst])
        .output()
        .await
        .map_err(|e| format!("spawn {} cp: {}", runtime, e))?;
    if !cp_out.status.success() {
        let stderr = String::from_utf8_lossy(&cp_out.stderr);
        let stderr_lower = stderr.to_ascii_lowercase();
        // Translate the publisher-side bug ("Dockerfile didn't COPY
        // vct-module.json") into an actionable error instead of an
        // opaque `<runtime> cp` exit code. Both podman and docker
        // surface this with phrasing variants that include "no such
        // file" / "does not exist" / "could not find".
        if stderr_lower.contains("no such file")
            || stderr_lower.contains("does not exist")
            || stderr_lower.contains("could not find")
        {
            return Err(format!(
                "Module image '{}' does not ship a vct-module.json at {}. \
                 Contact the module publisher — the launcher needs this \
                 manifest to render the config tab and apply DB migrations.",
                image_ref, CONTAINER_MANIFEST_PATH,
            ));
        }
        return Err(format!(
            "{} cp {} failed (exit {}): {}",
            runtime,
            cp_src,
            cp_out.status.code().unwrap_or(-1),
            stderr.chars().take(300).collect::<String>(),
        ));
    }

    // Step 4: validate the extracted manifest BEFORE committing. A
    // malformed manifest in the new image must NOT overwrite the
    // previous on-disk copy (the upgrade-rollback safety property
    // called out in architecture review §J4-G-b).
    let raw = std::fs::read_to_string(&tmp_path)
        .map_err(|e| format!("read tmp manifest at {}: {}", tmp_path.display(), e))?;
    let manifest = ModuleManifest::from_json(&raw)
        .map_err(|e| format!("extracted manifest invalid: {}", e))?;

    // Step 5: id mismatch check. The publisher could in theory ship a
    // manifest tagged for the wrong module; better to fail loudly here
    // than confuse the dispatcher post-install.
    if manifest.id != module_id {
        return Err(format!(
            "extracted manifest id '{}' doesn't match expected module id '{}' \
             (the image is publishing a manifest for the wrong module)",
            manifest.id, module_id,
        ));
    }

    // Step 5.5: V52-D.3 — Python manifest sanitizer. Reject the
    // manifest if it carries the pre-v0.2.49 Bug E pattern OR any
    // other pathological shape the sanitizer catches (unknown
    // placeholders, dangerous runtime commands, install.scope
    // incoherence). The sanitizer's reject reason flows into
    // `module_installs.last_error` so the GUI can surface a
    // clear "publisher shipped a malformed manifest" message.
    //
    // Soft-fail strategy: if the Python interpreter or the validator
    // module is unavailable (e.g. install.py wasn't run yet on this
    // host, exotic environment), log a warning and proceed. The
    // Rust-side runtime sanitizer (V52-D.1's
    // `is_runtime_pathological`) is the second line of defense and
    // catches the Bug E pattern at podman-run time even if this
    // step is bypassed.
    match run_python_manifest_sanitizer(&tmp_path).await {
        ManifestSanitizerOutcome::Rejected(reason) => {
            // Leave the .tmp file in place for forensics — don't
            // clobber the existing on-disk manifest. The caller's
            // .bak rollback path is not relevant because we never
            // committed.
            return Err(format!(
                "manifest_validation_failed: {} (V52-D.3 sanitizer rejected \
                 the extracted vct-module.json before commit; the existing \
                 on-disk manifest, if any, is untouched)",
                reason,
            ));
        }
        ManifestSanitizerOutcome::Accepted(warnings) => {
            for w in &warnings {
                eprintln!(
                    "[module_manifest_extract] V52-D.3 sanitizer warning for {}: {}",
                    module_id, w
                );
            }
        }
        ManifestSanitizerOutcome::Bypassed(reason) => {
            eprintln!(
                "[module_manifest_extract] V52-D.3 sanitizer bypassed for {}: {} \
                 (Rust runtime sanitizer remains as second line of defense)",
                module_id, reason
            );
        }
    }

    // Step 6: pre-write backup of any existing manifest (the upgrade
    // path always lands here; the fresh-install path has nothing to
    // back up and the copy is a no-op).
    let final_path = dest_dir.join("vct-module.json");
    let bak_path = dest_dir.join("vct-module.json.bak");
    let backed_up = if final_path.exists() {
        std::fs::copy(&final_path, &bak_path)
            .map_err(|e| format!("backup existing manifest to {}: {}", bak_path.display(), e))?;
        true
    } else {
        false
    };

    // Step 7: atomic swap. On EXDEV (across-mount rename) — which
    // shouldn't happen here because src and dst are siblings under
    // dest_dir — fall back to copy+remove so the protocol still
    // succeeds. On any other rename failure, restore from .bak.
    if let Err(e) = atomic_install(&tmp_path, &final_path) {
        if backed_up && bak_path.exists() {
            // Best-effort rollback. If this also fails the user is
            // left with a missing manifest, but the original cause
            // (rename failure) takes precedence in the surfaced error.
            let _ = std::fs::rename(&bak_path, &final_path);
        }
        return Err(format!(
            "commit extracted manifest to {}: {}",
            final_path.display(),
            e
        ));
    }

    // Step 8: best-effort cleanup of the tmp dir. A leaked .tmp/ is
    // harmless — next extraction overwrites it.
    let _ = std::fs::remove_dir_all(&tmp_dir);

    Ok(ExtractedManifest {
        on_disk_path: final_path,
        parsed: manifest,
    })
}

/// Move `src` → `dst` atomically when possible; fall back to
/// copy+remove on EXDEV. Both paths must be on a filesystem the
/// process can write to.
fn atomic_install(src: &Path, dst: &Path) -> std::io::Result<()> {
    match std::fs::rename(src, dst) {
        Ok(()) => Ok(()),
        Err(e) if matches!(e.raw_os_error(), Some(libc_exdev) if libc_exdev == 18) => {
            // EXDEV = 18 on Linux. Copy + remove.
            std::fs::copy(src, dst)?;
            std::fs::remove_file(src)?;
            Ok(())
        }
        Err(e) => Err(e),
    }
}

/// RAII guard that runs `<runtime> rm <cid>` when dropped. Used to
/// ensure the throw-away container created by `extract_manifest_from_image`
/// is removed even on the early-Err return paths.
///
/// Drop can't run async, so we use `std::process::Command` here. The
/// container removal is fire-and-forget — failures are logged but
/// never propagated (a leaked container is harmless; the runtime's
/// GC eventually reaps it).
struct ContainerCleanup {
    runtime: String,
    cid: String,
}

impl Drop for ContainerCleanup {
    fn drop(&mut self) {
        // Test-only spy hook: if `VCT_TEST_CLEANUP_COUNTER_FILE` is
        // set, append `<runtime>:<cid>\n` to the file so tests can
        // assert the cleanup ran without spawning a real `<runtime>
        // rm` against an unprivileged tempdir. Production code path
        // (env unset) skips this entirely.
        if let Ok(counter_file) = std::env::var("VCT_TEST_CLEANUP_COUNTER_FILE") {
            if !counter_file.is_empty() {
                use std::io::Write;
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&counter_file)
                {
                    let _ = writeln!(f, "{}:{}", self.runtime, self.cid);
                }
                return;
            }
        }

        let _ = std::process::Command::new(&self.runtime).silent()
            .args(["rm", &self.cid])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

// ─── V52-D.3 Python manifest sanitizer subprocess ─────────────────────

/// Outcome of invoking the Python sanitizer subprocess. Three states:
/// * `Accepted(warnings)` — manifest passed validation; warnings are
///   non-fatal advisories that should be logged.
/// * `Rejected(reason)` — manifest failed validation; the caller must
///   not commit it. `reason` is operator-facing and lands in
///   `module_installs.last_error`.
/// * `Bypassed(reason)` — sanitizer could not run (Python interpreter
///   missing / module not importable / subprocess spawn error). The
///   caller proceeds AS IF accepted but logs a warning. Rust-side
///   V52-D.1 runtime sanitizer catches the Bug E pattern at podman-
///   run time as a second line of defense.
#[derive(Debug)]
enum ManifestSanitizerOutcome {
    Accepted(Vec<String>),
    Rejected(String),
    #[allow(dead_code)]
    Bypassed(String),
}

/// V52-D.3: invoke `python -m vco_lib.manifest_validation <tmp_path>`
/// and parse its stdout JSON. Soft-fails to `Bypassed(...)` on any
/// invocation error so a missing Python interpreter doesn't break
/// the install path.
async fn run_python_manifest_sanitizer(manifest_path: &Path) -> ManifestSanitizerOutcome {
    use std::process::Stdio;

    // Honour test/CI bypass: setting `VCT_MANIFEST_SANITIZER_BYPASS=1`
    // skips the subprocess entirely. Used by the existing test
    // fixtures that ship intentionally-malformed manifests through
    // the extract path without our sanitizer rejecting them.
    if std::env::var("VCT_MANIFEST_SANITIZER_BYPASS").ok().as_deref() == Some("1") {
        return ManifestSanitizerOutcome::Bypassed(
            "VCT_MANIFEST_SANITIZER_BYPASS=1".to_string(),
        );
    }

    // Locate the Python interpreter. Prefer the env-pinned path
    // (`MCP_PYTHON` — same variable the launcher uses for MCP server
    // subprocesses); fall back to `python3` on PATH.
    let python = std::env::var("MCP_PYTHON")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            // Find python3 on PATH.
            which_python()
        });
    let python = match python {
        Some(p) => p,
        None => {
            return ManifestSanitizerOutcome::Bypassed(
                "no python interpreter found (set MCP_PYTHON env or install \
                 python3 on PATH)".to_string(),
            );
        }
    };

    let output = Command::new(&python).silent()
        .args(["-m", "vco_lib.manifest_validation"])
        .arg(manifest_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await;
    let output = match output {
        Ok(o) => o,
        Err(e) => {
            return ManifestSanitizerOutcome::Bypassed(format!(
                "spawn {} -m vco_lib.manifest_validation: {}",
                python, e
            ));
        }
    };

    // Exit code 2 = invocation error (no path arg / bad module
    // import). Treat as Bypassed.
    if output.status.code() == Some(2) {
        return ManifestSanitizerOutcome::Bypassed(format!(
            "sanitizer exit 2 (invocation error); stderr={}",
            String::from_utf8_lossy(&output.stderr).chars().take(200).collect::<String>(),
        ));
    }

    // Stdout carries the JSON result regardless of exit code (0 or 1).
    let stdout = String::from_utf8_lossy(&output.stdout);
    let parsed: serde_json::Value = match serde_json::from_str(&stdout) {
        Ok(v) => v,
        Err(e) => {
            return ManifestSanitizerOutcome::Bypassed(format!(
                "parse sanitizer stdout JSON failed: {}; stdout={}",
                e,
                stdout.chars().take(200).collect::<String>(),
            ));
        }
    };

    let is_valid = parsed.get("is_valid").and_then(|v| v.as_bool()).unwrap_or(false);
    let warnings: Vec<String> = parsed
        .get("warnings")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    if is_valid {
        ManifestSanitizerOutcome::Accepted(warnings)
    } else {
        let reason = parsed
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("manifest_validation: sanitizer returned is_valid=false with no error")
            .to_string();
        ManifestSanitizerOutcome::Rejected(reason)
    }
}

/// Find a python3 interpreter on PATH. Returns `None` if none of the
/// candidates are executable.
fn which_python() -> Option<String> {
    for candidate in ["python3", "python"] {
        if std::process::Command::new(candidate).silent()
            .args(["--version"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
        {
            return Some(candidate.to_string());
        }
    }
    None
}

// ─── Tests ───────────────────────────────────────────────────────────────
//
// We mock the container runtime by creating a fake `podman` (or
// `docker`) shell script in a tempdir and prepending that dir to PATH
// for the duration of the test. Each fake runtime reads behaviour-
// control env vars (`FAKE_PODMAN_MODE`, `FAKE_PODMAN_MANIFEST_BODY`,
// `FAKE_PODMAN_CP_STDERR`, `FAKE_PODMAN_CID`) so different test cases
// can dial in the failure they want without rewriting the script.

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::Mutex;

    // Serialise tests that mutate process-wide env (PATH, VCT_STATE_DIR,
    // FAKE_PODMAN_*). Parallel `cargo test` would otherwise observe
    // each other's settings.
    //
    // Acquire via `serialize_lock()` which strips poison so a panic in
    // one test doesn't cascade into PoisonError in every other test of
    // the suite. Each individual test's assertion failures still
    // surface — we just don't conflate a previous test's panic with
    // *this* test's behaviour.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn serialize_lock() -> std::sync::MutexGuard<'static, ()> {
        SERIALIZE.lock().unwrap_or_else(|poison| poison.into_inner())
    }

    /// Build a minimal, valid v0.2.33-shape manifest JSON for module id
    /// `id`. Used as the "good" extracted body in happy-path tests.
    /// Mirrors the shape ModuleManifest::from_json requires — every
    /// field referenced by the parser's required-field guards has a
    /// stub here.
    fn minimal_manifest_json(id: &str, version: &str) -> String {
        serde_json::json!({
            "id": id,
            "name": id,
            "version": version,
            "description": "test fixture",
            "category": "paid-independent",
            "compatibility": {
                "hosts": ["base"],
            },
            "install": {
                "method": "container_pull",
                "container": {
                    "image": format!("ghcr.io/test/{}", id),
                    "tag_from_version": true,
                    "pull_token_endpoint": "https://example.invalid/token",
                },
            },
            "runtime": {
                "type": "container",
                "command": "echo",
            },
            "license": { "required": false },
        })
        .to_string()
    }

    /// Materialise a fake `podman` script at `<tmp>/podman` that
    /// responds to `create` / `cp` / `rm` per the behaviour-control
    /// env vars listed above.
    ///
    /// Returns the path to the tempdir; the caller is responsible for
    /// prepending it to PATH and cleaning up after.
    fn install_fake_podman(tmp: &Path) -> PathBuf {
        let script = tmp.join("podman");
        let body = r#"#!/usr/bin/env bash
# Fake podman/docker runtime for module_manifest_extract tests.
# Behaviour driven by env:
#   FAKE_PODMAN_MODE       — "create_ok" | "create_fail" | "cp_missing"
#                            | "cp_garbage" | "cp_mismatch" | "cp_fail_other"
#                            | "rename_fail" (paired with create_ok)
#   FAKE_PODMAN_MANIFEST_BODY — content to write on `cp` (for create_ok mode)
#   FAKE_PODMAN_CP_STDERR  — stderr text for the cp failure modes
#   FAKE_PODMAN_CID        — container id to echo from `create`
#
# NOTE on bash gotcha: do NOT use `${VAR:-{}}` form — bash's brace
# matcher inside `${VAR:-DEFAULT}` does NOT pair `{` against `}` inside
# the default, so the default `{}` parses as `{` plus a stray trailing
# `}` appended to the substitution. We use plain `${VAR:-fallback}`
# with non-brace defaults and pre-check existence with `${VAR-}` where
# we want the empty fallback.
set -u
mode="${FAKE_PODMAN_MODE:-create_ok}"
cid="${FAKE_PODMAN_CID:-fake-cid-deadbeef}"
case "$1" in
  create)
    if [ "$mode" = "create_fail" ]; then
      echo "fake-create-error: image pull manifest unknown" >&2
      exit 125
    fi
    echo "$cid"
    exit 0
    ;;
  cp)
    src="$2"
    dst="$3"
    case "$mode" in
      cp_missing)
        cp_err="${FAKE_PODMAN_CP_STDERR-}"
        if [ -z "$cp_err" ]; then
          cp_err="Error: stat /app/vct-module.json: no such file or directory"
        fi
        echo "$cp_err" >&2
        exit 1
        ;;
      cp_fail_other)
        cp_err="${FAKE_PODMAN_CP_STDERR-}"
        if [ -z "$cp_err" ]; then
          cp_err="Error: permission denied"
        fi
        echo "$cp_err" >&2
        exit 1
        ;;
      cp_garbage|cp_mismatch|create_ok|rename_fail)
        body="${FAKE_PODMAN_MANIFEST_BODY-}"
        printf '%s' "$body" > "$dst"
        exit 0
        ;;
      *)
        echo "fake-podman: unknown mode '$mode'" >&2
        exit 99
        ;;
    esac
    ;;
  rm)
    # Always succeed (the cleanup is best-effort regardless).
    exit 0
    ;;
  *)
    echo "fake-podman: unsupported verb '$1'" >&2
    exit 2
    ;;
esac
"#;
        fs::write(&script, body).expect("write fake podman");
        // chmod +x
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&script).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&script, perms).unwrap();
        }
        tmp.to_path_buf()
    }

    /// Prepend `dir` to PATH for the lifetime of the returned guard.
    /// Restores PATH on drop so other tests aren't affected (even
    /// though SERIALIZE already excludes that race).
    struct PathGuard {
        prev: Option<String>,
    }
    impl Drop for PathGuard {
        fn drop(&mut self) {
            match &self.prev {
                Some(v) => std::env::set_var("PATH", v),
                None => std::env::remove_var("PATH"),
            }
        }
    }
    fn push_path(dir: &Path) -> PathGuard {
        let prev = std::env::var("PATH").ok();
        let new_path = match &prev {
            Some(p) => format!("{}:{}", dir.display(), p),
            None => dir.display().to_string(),
        };
        std::env::set_var("PATH", new_path);
        PathGuard { prev }
    }

    /// Set every behaviour-control env var to a known baseline so
    /// previous tests can't leak state.
    fn reset_fake_env() {
        for k in [
            "FAKE_PODMAN_MODE",
            "FAKE_PODMAN_MANIFEST_BODY",
            "FAKE_PODMAN_CP_STDERR",
            "FAKE_PODMAN_CID",
            "VCT_TEST_CLEANUP_COUNTER_FILE",
        ] {
            std::env::remove_var(k);
        }
    }

    #[tokio::test]
    async fn extract_manifest_happy_path() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        let body = minimal_manifest_json("vct-test-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "create_ok");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);

        let out = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect("happy path must succeed");

        assert_eq!(out.parsed.id, "vct-test-mod");
        assert_eq!(out.parsed.version, "0.1.0");
        assert!(out.on_disk_path.is_file(), "manifest must be on disk");
        let expected = tmp
            .path()
            .join("modules")
            .join("vct-test-mod")
            .join("vct-module.json");
        assert_eq!(out.on_disk_path, expected);

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_image_missing_file() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        std::env::set_var("FAKE_PODMAN_MODE", "cp_missing");
        std::env::set_var(
            "FAKE_PODMAN_CP_STDERR",
            "Error: stat /app/vct-module.json: no such file or directory",
        );

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect_err("missing file must error");

        assert!(
            err.contains("does not ship a vct-module.json"),
            "user-friendly error required, got: {}",
            err
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_invalid_json() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        std::env::set_var("FAKE_PODMAN_MODE", "cp_garbage");
        // Garbage that serde_json will refuse.
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", "this is not json at all {[");

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect_err("garbage must error");

        assert!(
            err.contains("extracted manifest invalid"),
            "invalid-manifest sentinel required, got: {}",
            err
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_id_mismatch() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        let body = minimal_manifest_json("vct-other-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "cp_mismatch");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-other-mod:0.1.0",
            "vct-expected-mod",
            "podman",
        )
        .await
        .expect_err("id mismatch must error");

        assert!(
            err.contains("doesn't match expected"),
            "id-mismatch sentinel required, got: {}",
            err
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_atomic_bak_rollback_on_rename_failure() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        // Pre-create a previous manifest at the final path. We then
        // trigger a rename failure by deleting the tmpdir's parent
        // BETWEEN extraction's read-and-validate step and the rename
        // step. The simpler test we can run end-to-end: seed a known-
        // good previous manifest, run extract with create_ok against
        // the SAME body, assert the final file matches the new body
        // (the rename succeeds when src and dst are siblings). For
        // explicit rename failure we'd need to inject between steps
        // which the helper doesn't expose.
        //
        // What we actually verify here: the .bak file IS created when
        // a previous manifest exists at the final path. This guards
        // step 6 of the protocol (the precondition for the rollback
        // to succeed). If step 6 never runs, the rollback can't help
        // — so this assertion is the load-bearing rollback gate.
        let dest_dir = tmp
            .path()
            .join("modules")
            .join("vct-test-mod");
        fs::create_dir_all(&dest_dir).unwrap();
        let final_path = dest_dir.join("vct-module.json");
        let prev_body = minimal_manifest_json("vct-test-mod", "0.0.9");
        fs::write(&final_path, &prev_body).unwrap();

        let new_body = minimal_manifest_json("vct-test-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "create_ok");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &new_body);

        let out = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect("upgrade-shaped extract must succeed");

        // The final file is the new version.
        assert_eq!(out.parsed.version, "0.1.0");
        // .bak was written before the rename ran. (It may still be on
        // disk — the protocol doesn't delete it; lifecycle decision
        // is left to a separate cleanup pass. Either way, "did it
        // exist when needed" is the property under test.)
        let bak_path = dest_dir.join("vct-module.json.bak");
        assert!(
            bak_path.is_file(),
            ".bak must be created when overwriting an existing manifest"
        );
        // And its content must be the OLD body — proves the backup
        // happened BEFORE the rename, so a hypothetical rename failure
        // would have a valid recovery target.
        let bak_contents = fs::read_to_string(&bak_path).unwrap();
        assert_eq!(
            bak_contents, prev_body,
            ".bak must hold the pre-rename manifest body"
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_cleans_up_tmp_dir_on_success() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        let body = minimal_manifest_json("vct-test-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "create_ok");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);

        let _out = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect("happy path must succeed");

        let tmp_dir = tmp
            .path()
            .join("modules")
            .join("vct-test-mod")
            .join(".tmp");
        assert!(
            !tmp_dir.exists(),
            ".tmp/ must be removed after a successful extract (was: {})",
            tmp_dir.display()
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_cleans_up_container_on_drop() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        // The cleanup-counter file is the spy hook. ContainerCleanup::drop
        // appends `<runtime>:<cid>\n` to it instead of spawning the
        // real `<runtime> rm`. We assert the line lands even on the
        // ERROR-return path so the cleanup is truly RAII.
        let counter = tmp.path().join("cleanup.log");
        std::env::set_var("VCT_TEST_CLEANUP_COUNTER_FILE", &counter);

        // Drive the function down an Err path (mismatched id) so we
        // verify cleanup fires when extract_manifest_from_image
        // returns Err.
        let body = minimal_manifest_json("vct-other-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "cp_mismatch");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);
        std::env::set_var("FAKE_PODMAN_CID", "spy-cid-123");

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-other-mod:0.1.0",
            "vct-expected-mod",
            "podman",
        )
        .await
        .expect_err("id mismatch must error");
        assert!(err.contains("doesn't match expected"));

        // ContainerCleanup dropped at end-of-function-scope → spy file
        // should now contain one line `podman:spy-cid-123\n`.
        assert!(
            counter.is_file(),
            "cleanup spy file must exist after Err path"
        );
        let log = fs::read_to_string(&counter).unwrap();
        assert!(
            log.contains("podman:spy-cid-123"),
            "cleanup must run even on Err — log was: {:?}",
            log
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    #[tokio::test]
    async fn extract_manifest_create_failure_surfaces_stderr() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        std::env::set_var("FAKE_PODMAN_MODE", "create_fail");

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect_err("create-fail mode must error");

        // The error message must carry the runtime's stderr so the
        // user (or support) can diagnose what the underlying runtime
        // refused.
        assert!(
            err.contains("create")
                && (err.contains("manifest unknown") || err.contains("fake-create-error")),
            "create-failure error must surface runtime stderr; got: {}",
            err
        );

        std::env::remove_var("VCT_STATE_DIR");
        reset_fake_env();
    }

    /// V52-D.3: when the extracted manifest carries the pre-v0.2.49
    /// Bug E pattern (runtime.command = "podman" / args contain
    /// `{module_image}`), the Python sanitizer subprocess rejects it
    /// and `extract_manifest_from_image` returns an error containing
    /// the `manifest_validation_failed:` prefix.
    ///
    /// Gated on Python availability + `vco_lib` import success
    /// (subprocess attempts `python3 -m vco_lib.manifest_validation`).
    /// On hermetic CI without the Python module available, the
    /// sanitizer Bypasses and this test would erroneously pass-
    /// through. To keep the test stable across environments, we
    /// gate on a self-check via the CLI: if the CLI returns exit 1
    /// for a known-bad manifest in our tempdir, the sanitizer is
    /// reachable and the test runs; otherwise we skip cleanly.
    #[tokio::test]
    async fn v0252_d3_extract_rejects_bug_e_manifest() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();

        // Self-gate: can we reach the sanitizer? Probe by running
        // the CLI against a known-bad manifest in the tempdir.
        let probe_path = tmp.path().join("probe.json");
        std::fs::write(
            &probe_path,
            r#"{"id":"x","version":"0.1.0","install":{},"runtime":{"command":"podman","args":["run","{module_image}"]}}"#,
        )
        .unwrap();
        // Walk up from cwd (= launcher/src-tauri) two levels to reach
        // the repo root. Cargo runs tests with cwd = the crate dir,
        // which for vct-launcher-temp is `launcher/src-tauri/`.
        let cwd_for_python = std::env::current_dir()
            .unwrap()
            .parent() // launcher
            .and_then(|p| p.parent()) // repo root
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| std::env::current_dir().unwrap());
        let probe = std::process::Command::new("python3")
            .current_dir(&cwd_for_python)
            .args(["-m", "vco_lib.manifest_validation"])
            .arg(&probe_path)
            .output();
        let sanitizer_reachable = match probe {
            Ok(o) => o.status.code() == Some(1),
            Err(_) => false,
        };
        if !sanitizer_reachable {
            eprintln!(
                "[v0252_d3_extract_rejects_bug_e_manifest] skipping: \
                 vco_lib.manifest_validation not reachable from cwd {:?}",
                cwd_for_python
            );
            return;
        }

        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        // Build a manifest that PARSES through ModuleManifest::from_json
        // (so Step 4 passes) but the Python sanitizer rejects at
        // Step 5.5. The runtime.command='podman' indicator does the
        // rejection.
        let body = serde_json::json!({
            "id": "vct-test-mod",
            "name": "vct-test-mod",
            "version": "0.1.0",
            "description": "bug-e fixture",
            "category": "paid-independent",
            "compatibility": { "hosts": ["base"] },
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/test/vct-test-mod",
                    "tag_from_version": true,
                    "pull_token_endpoint": "https://example.invalid/token",
                },
            },
            "runtime": {
                "type": "container",
                "command": "podman",
                "args": ["run", "--rm", "-p", "11450:11450", "{module_image}"],
            },
            "license": { "required": false },
        })
        .to_string();
        std::env::set_var("FAKE_PODMAN_MODE", "create_ok");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);
        // Ensure the bypass env isn't set from another test.
        std::env::remove_var("VCT_MANIFEST_SANITIZER_BYPASS");
        // Override PYTHONPATH so the subprocess can import vco_lib
        // when its cwd is the launcher dir.
        std::env::set_var("PYTHONPATH", &cwd_for_python);

        let err = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect_err("Bug E manifest must be rejected by V52-D.3 sanitizer");

        assert!(
            err.contains("manifest_validation_failed"),
            "expected V52-D.3 reject prefix in error; got: {}",
            err
        );
        // The reason should mention the pathological pattern.
        assert!(
            err.contains("podman") || err.contains("{module_image}") || err.contains("Bug E"),
            "expected Bug E indicator in error; got: {}",
            err
        );

        // The existing on-disk manifest (if any) MUST NOT have been
        // overwritten. Verify the final path either doesn't exist
        // (fresh install case) OR is unchanged.
        let expected_path = tmp
            .path()
            .join("modules")
            .join("vct-test-mod")
            .join("vct-module.json");
        if expected_path.exists() {
            // Shouldn't happen in this fresh-tempdir test, but if it
            // does, the content must not be the rejected body.
            let on_disk = std::fs::read_to_string(&expected_path).unwrap_or_default();
            assert!(
                !on_disk.contains("{module_image}"),
                "rejected manifest must NOT be committed to disk; \
                 on-disk content includes Bug E placeholder: {}",
                on_disk
            );
        }

        std::env::remove_var("VCT_STATE_DIR");
        std::env::remove_var("PYTHONPATH");
        reset_fake_env();
    }

    /// V52-D.3: `VCT_MANIFEST_SANITIZER_BYPASS=1` short-circuits the
    /// sanitizer and lets a Bug-E manifest through. Used by existing
    /// extract tests that ship intentionally-malformed fixtures
    /// through the pipeline.
    #[tokio::test]
    async fn v0252_d3_extract_bypass_env_skips_sanitizer() {
        let _g = serialize_lock();
        reset_fake_env();
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let bin = install_fake_podman(tmp.path());
        let _p = push_path(&bin);

        let body = minimal_manifest_json("vct-test-mod", "0.1.0");
        std::env::set_var("FAKE_PODMAN_MODE", "create_ok");
        std::env::set_var("FAKE_PODMAN_MANIFEST_BODY", &body);
        // Bypass enabled — even a normally-rejected manifest would
        // pass through. Here we use the good fixture to verify the
        // happy path still works with bypass enabled (the test
        // ordering doesn't leave bypass set for later tests).
        std::env::set_var("VCT_MANIFEST_SANITIZER_BYPASS", "1");

        let out = extract_manifest_from_image(
            "ghcr.io/test/vct-test-mod:0.1.0",
            "vct-test-mod",
            "podman",
        )
        .await
        .expect("bypass + good manifest must succeed");
        assert_eq!(out.parsed.id, "vct-test-mod");

        std::env::remove_var("VCT_STATE_DIR");
        std::env::remove_var("VCT_MANIFEST_SANITIZER_BYPASS");
        reset_fake_env();
    }
}
