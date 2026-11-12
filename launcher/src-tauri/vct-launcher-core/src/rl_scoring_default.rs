// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! RL scoring's DEFAULT — what an unset toggle resolves to (owner ruling
//! 2026-11-12: "RL scoring default follows the bundled weights").
//!
//! The decision lives ONCE, in Python: `vco_lib/rl_scoring_default.py`. This
//! module is its Rust caller (tier A of the A>B>C rule): it runs
//! `python -m vco_lib.rl_scoring_default --profile <p> --json` once per
//! embedding profile and caches the answer for the life of the process. The
//! manifest only changes with an orchestrator update, which restarts the hub
//! and the launcher.
//!
//! * A trained model bundled for the ACTIVE profile (the bundled-weights
//!   manifest names a `current` version) → default ON.
//! * Otherwise → default OFF. That includes every failure: no `vco_lib`
//!   Python, a spawn error, a timeout, unreadable output, a broken manifest
//!   — "a trained model ships" cannot be positively confirmed, so the
//!   conservative answer is off. A failure is logged loudly (it means a
//!   broken install) and retried after [`FAILURE_RETRY`], never cached for
//!   good.
//!
//! An explicit user choice (the project's own row, else the host-wide row)
//! always wins over this default; the cascade that applies it is
//! [`crate::db::Db::module_effective_enabled`].

use std::collections::HashMap;
use std::io::Read;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::process::CommandExt;

/// How long a spawn may take before it counts as failed (default OFF).
const SPAWN_TIMEOUT: Duration = Duration::from_secs(30);

/// How long a failed answer is reused before the next attempt.
pub const FAILURE_RETRY: Duration = Duration::from_secs(60);

/// RL scoring's "nothing chosen" answer for one embedding profile — the wire
/// shape the GUI renders its default text from.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RlScoringDefault {
    /// The active embedding profile the answer is for (e.g. `qwen3`).
    pub profile: String,
    /// A trained model is bundled for `profile`.
    pub bundled_model: bool,
    /// The default: ON exactly when a trained model is bundled.
    pub default_on: bool,
    /// Why the answer could not be confirmed (the default is then OFF);
    /// `None` when the manifest was read.
    #[serde(default)]
    pub error: Option<String>,
}

impl RlScoringDefault {
    /// The conservative answer when the Python decision could not run.
    pub fn unconfirmed(profile: &str, error: String) -> Self {
        Self {
            profile: profile.to_string(),
            bundled_model: false,
            default_on: false,
            error: Some(error),
        }
    }

    /// The answer as it stands today for every profile (no trained model
    /// ships). For tests and callers that inject the default explicitly.
    pub fn not_bundled(profile: &str) -> Self {
        Self { profile: profile.to_string(), bundled_model: false, default_on: false, error: None }
    }

    /// A trained model is bundled for `profile` (injected in tests).
    pub fn bundled(profile: &str) -> Self {
        Self { profile: profile.to_string(), bundled_model: true, default_on: true, error: None }
    }
}

/// Parse the CLI's `--json` line. The default is recomputed from
/// `bundled_model` so a payload can never claim "on" without a model.
pub fn parse_cli_output(stdout: &str) -> Result<RlScoringDefault, String> {
    let line = stdout.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let mut parsed: RlScoringDefault = serde_json::from_str(line.trim())
        .map_err(|e| format!("unreadable `vco_lib.rl_scoring_default` output ({e}): {line:?}"))?;
    parsed.default_on = parsed.bundled_model;
    Ok(parsed)
}

struct Cached {
    value: RlScoringDefault,
    at: Instant,
}

static CACHE: LazyLock<Mutex<HashMap<String, Cached>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

thread_local! {
    static THREAD_OVERRIDE: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
}

/// Restores the previous per-thread answer when dropped.
pub struct OverrideGuard(Option<bool>);

impl Drop for OverrideGuard {
    fn drop(&mut self) {
        THREAD_OVERRIDE.with(|c| c.set(self.0));
    }
}

/// Answer "is a trained model bundled?" with `bundled` on THIS thread until
/// the guard drops, without running Python — the injection seam for tests of
/// the callers (the hub's `/config`, the launcher commands), in the same
/// per-thread shape as `paths::with_lookup_path`. Production never sets it.
pub fn override_bundled_model_for_thread(bundled: bool) -> OverrideGuard {
    let prev = THREAD_OVERRIDE.with(|c| c.replace(Some(bundled)));
    OverrideGuard(prev)
}

/// RL scoring's default for `profile`, from the Python decision (cached).
pub fn rl_scoring_default_for(profile: &str) -> RlScoringDefault {
    let key = profile.trim().to_string();
    if let Some(bundled) = THREAD_OVERRIDE.with(|c| c.get()) {
        return if bundled {
            RlScoringDefault::bundled(&key)
        } else {
            RlScoringDefault::not_bundled(&key)
        };
    }
    if let Ok(cache) = CACHE.lock() {
        if let Some(hit) = cache.get(&key) {
            if hit.value.error.is_none() || hit.at.elapsed() < FAILURE_RETRY {
                return hit.value.clone();
            }
        }
    }
    let root = crate::orchestrator_manifest::orchestrator_install_root();
    let value = query_python(&key, root.as_deref());
    if let Some(err) = &value.error {
        tracing::error!(
            "[rl-scoring-default] cannot confirm a bundled RL model for profile {key:?}: {err}; \
             RL scoring defaults to OFF (a broken install: re-run the orchestrator update)"
        );
    }
    if let Ok(mut cache) = CACHE.lock() {
        cache.insert(key, Cached { value: value.clone(), at: Instant::now() });
    }
    value
}

/// `root` is the orchestrator clone: the child runs there so `python -m`
/// imports THAT checkout's `vco_lib` (cwd is first on `sys.path`) and reads
/// the manifest shipped beside it.
fn query_python(profile: &str, root: Option<&std::path::Path>) -> RlScoringDefault {
    if profile.is_empty() {
        return RlScoringDefault::unconfirmed(profile, "no active embedding profile".to_string());
    }
    let Some(python) = crate::python_resolve::resolve_python_for_vco_lib() else {
        return RlScoringDefault::unconfirmed(
            profile,
            "no Python interpreter with vco_lib was found".to_string(),
        );
    };
    let mut cmd = std::process::Command::new(&python).silent();
    cmd.args(["-m", "vco_lib.rl_scoring_default", "--profile", profile, "--json"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    if let Some(root) = root {
        cmd.current_dir(root);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return RlScoringDefault::unconfirmed(
                profile,
                format!("cannot run {}: {e}", python.display()),
            )
        }
    };
    let deadline = Instant::now() + SPAWN_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return RlScoringDefault::unconfirmed(
                    profile,
                    format!("`vco_lib.rl_scoring_default` did not finish within {SPAWN_TIMEOUT:?}"),
                );
            }
            Err(e) => return RlScoringDefault::unconfirmed(profile, format!("wait failed: {e}")),
        }
    };
    let mut stdout = String::new();
    let mut stderr = String::new();
    if let Some(mut out) = child.stdout.take() {
        let _ = out.read_to_string(&mut stdout);
    }
    if let Some(mut err) = child.stderr.take() {
        let _ = err.read_to_string(&mut stderr);
    }
    if !status.success() {
        return RlScoringDefault::unconfirmed(
            profile,
            format!("`vco_lib.rl_scoring_default` exited {status}: {}", stderr.trim()),
        );
    }
    match parse_cli_output(&stdout) {
        Ok(v) if v.profile == profile => v,
        Ok(v) => RlScoringDefault::unconfirmed(
            profile,
            format!("answer for profile {:?}, asked for {profile:?}", v.profile),
        ),
        Err(e) => RlScoringDefault::unconfirmed(profile, e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_reads_the_cli_line_and_never_trusts_a_bare_default_on() {
        let v = parse_cli_output(
            "{\"bundled_model\": false, \"default_on\": true, \"error\": null, \"profile\": \"qwen3\"}\n",
        )
        .unwrap();
        assert_eq!(v, RlScoringDefault::not_bundled("qwen3"));
        let v = parse_cli_output(
            "{\"bundled_model\": true, \"default_on\": true, \"error\": null, \"profile\": \"arctic\"}",
        )
        .unwrap();
        assert_eq!(v, RlScoringDefault::bundled("arctic"));
        assert!(parse_cli_output("Traceback ...").is_err());
    }

    #[test]
    fn unconfirmed_is_off() {
        let v = RlScoringDefault::unconfirmed("qwen3", "x".into());
        assert!(!v.default_on && !v.bundled_model);
    }

    /// End to end through the real Python decision and the shipped manifest
    /// (no trained model ships today → OFF). Skips without a vco_lib Python.
    #[test]
    fn the_shipped_manifest_defaults_off_today() {
        if crate::python_resolve::resolve_python_for_vco_lib().is_none() {
            eprintln!("skip: no vco_lib python (set VCT_VENV)");
            return;
        }
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        for profile in ["qwen3", "arctic"] {
            let v = query_python(profile, Some(&root));
            assert_eq!(v.error, None, "{profile}: {v:?}");
            assert!(!v.default_on, "{profile}: no trained model ships today");
        }
        // An unknown profile is OFF, not an error.
        let v = query_python("openai", Some(&root));
        assert!(!v.default_on);
    }
}
