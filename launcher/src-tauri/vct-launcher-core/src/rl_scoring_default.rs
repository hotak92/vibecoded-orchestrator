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
use std::sync::{Arc, LazyLock, Mutex};
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

/// One in-flight lock per profile: the first caller on a miss holds it while
/// Python runs; concurrent callers for the same profile wait on it, then read
/// the answer the first one cached (single flight — one spawn).
static IN_FLIGHT: LazyLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

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

/// The answer available WITHOUT running Python: the per-thread override, or
/// a cache entry that is still valid (a success for good, a failure for
/// [`FAILURE_RETRY`]). `None` means a probe is needed.
fn answer_without_probe(key: &str) -> Option<RlScoringDefault> {
    if let Some(bundled) = THREAD_OVERRIDE.with(|c| c.get()) {
        return Some(if bundled {
            RlScoringDefault::bundled(key)
        } else {
            RlScoringDefault::not_bundled(key)
        });
    }
    let cache = CACHE.lock().ok()?;
    let hit = cache.get(key)?;
    (hit.value.error.is_none() || hit.at.elapsed() < FAILURE_RETRY).then(|| hit.value.clone())
}

fn in_flight_lock(key: &str) -> Arc<Mutex<()>> {
    let mut map = IN_FLIGHT.lock().unwrap_or_else(|p| p.into_inner());
    map.entry(key.to_string()).or_default().clone()
}

/// The production probe: the Python decision, run in the orchestrator clone.
fn probe_python(key: &str) -> RlScoringDefault {
    let root = crate::orchestrator_manifest::orchestrator_install_root();
    query_python(key, root.as_deref())
}

/// Run a blocking `f` without stalling an async worker: on a tokio
/// MULTI-THREAD runtime worker, `block_in_place` first moves that worker's
/// other tasks to another thread; anywhere else (a plain thread, the blocking
/// pool, no runtime) `f` simply runs. A current-thread runtime cannot hand
/// off — an async caller there must use [`rl_scoring_default_async`].
fn run_blocking<R>(f: impl FnOnce() -> R) -> R {
    match tokio::runtime::Handle::try_current() {
        Ok(h) if h.runtime_flavor() == tokio::runtime::RuntimeFlavor::MultiThread => {
            tokio::task::block_in_place(f)
        }
        _ => f(),
    }
}

/// Cache + single flight around `probe`. A caller that waited for the
/// profile's in-flight lock re-reads the cache once it holds it, so
/// concurrent cold misses for one profile run `probe` ONCE and all get its
/// answer. A failure is cached for [`FAILURE_RETRY`] like before.
fn resolve_with(profile: &str, probe: impl FnOnce(&str) -> RlScoringDefault) -> RlScoringDefault {
    let key = profile.trim().to_string();
    if let Some(v) = answer_without_probe(&key) {
        return v;
    }
    run_blocking(|| {
        let flight = in_flight_lock(&key);
        let _turn = flight.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(v) = answer_without_probe(&key) {
            return v;
        }
        let value = probe(&key);
        if let Some(err) = &value.error {
            tracing::error!(
                "[rl-scoring-default] cannot confirm a bundled RL model for profile {key:?}: {err}; \
                 RL scoring defaults to OFF (a broken install: re-run the orchestrator update)"
            );
        }
        if let Ok(mut cache) = CACHE.lock() {
            cache.insert(key.clone(), Cached { value: value.clone(), at: Instant::now() });
        }
        value
    })
}

/// RL scoring's default for `profile`, from the Python decision (cached; one
/// spawn per profile however many callers miss at once).
///
/// BLOCKING on a cache miss (a Python spawn, up to [`SPAWN_TIMEOUT`]). Async
/// code calls [`rl_scoring_default_async`] instead; a sync call that does
/// land on a multi-thread runtime worker hands the worker off with
/// `block_in_place`, so it cannot starve the runtime either.
pub fn rl_scoring_default_for(profile: &str) -> RlScoringDefault {
    resolve_with(profile, probe_python)
}

/// [`rl_scoring_default_for`] for async callers (the hub's `/config`, the
/// launcher's async commands): the per-thread test override or a cache hit
/// answers inline; a miss runs on the blocking pool, so no async worker ever
/// waits on Python. Same cache, same single flight.
pub async fn rl_scoring_default_async(profile: String) -> RlScoringDefault {
    rl_scoring_default_async_with(profile, probe_python).await
}

async fn rl_scoring_default_async_with(
    profile: String,
    probe: fn(&str) -> RlScoringDefault,
) -> RlScoringDefault {
    let key = profile.trim().to_string();
    if let Some(v) = answer_without_probe(&key) {
        return v;
    }
    let task_key = key.clone();
    match tokio::task::spawn_blocking(move || resolve_with(&task_key, probe)).await {
        Ok(v) => v,
        Err(e) => RlScoringDefault::unconfirmed(&key, format!("the default probe task failed: {e}")),
    }
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

    use std::sync::atomic::{AtomicUsize, Ordering};

    static SINGLE_FLIGHT_SPAWNS: AtomicUsize = AtomicUsize::new(0);
    fn slow_counting_probe(key: &str) -> RlScoringDefault {
        SINGLE_FLIGHT_SPAWNS.fetch_add(1, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(300));
        RlScoringDefault::bundled(key)
    }

    /// M1 (review W5): N concurrent cold misses for one profile run the
    /// probe ONCE and all get its answer — sync and async callers alike.
    #[test]
    fn concurrent_cold_misses_spawn_once() {
        let profile = "single-flight-test-profile";
        let threads: Vec<_> = (0..8)
            .map(|_| std::thread::spawn(move || resolve_with(profile, slow_counting_probe)))
            .collect();
        let rt = tokio::runtime::Builder::new_current_thread().build().unwrap();
        let from_async = rt.block_on(async {
            let tasks: Vec<_> = (0..4)
                .map(|_| {
                    tokio::spawn(rl_scoring_default_async_with(profile.to_string(), slow_counting_probe))
                })
                .collect();
            let mut out = Vec::new();
            for t in tasks {
                out.push(t.await.unwrap());
            }
            out
        });
        for t in threads {
            assert_eq!(t.join().unwrap(), RlScoringDefault::bundled(profile));
        }
        assert!(from_async.iter().all(|v| *v == RlScoringDefault::bundled(profile)));
        assert_eq!(SINGLE_FLIGHT_SPAWNS.load(Ordering::SeqCst), 1, "one spawn for all callers");
    }

    static FAILING_SPAWNS: AtomicUsize = AtomicUsize::new(0);
    fn failing_probe(key: &str) -> RlScoringDefault {
        FAILING_SPAWNS.fetch_add(1, Ordering::SeqCst);
        RlScoringDefault::unconfirmed(key, "boom".into())
    }

    /// A failure is OFF and reused within FAILURE_RETRY (no re-spawn storm).
    #[test]
    fn a_failure_is_off_and_reused_until_the_retry_window() {
        let profile = "failing-probe-test-profile";
        let a = resolve_with(profile, failing_probe);
        let b = resolve_with(profile, failing_probe);
        assert!(!a.default_on && a.error.is_some());
        assert_eq!(a, b);
        assert_eq!(FAILING_SPAWNS.load(Ordering::SeqCst), 1);
    }

    fn slow_probe(key: &str) -> RlScoringDefault {
        std::thread::sleep(Duration::from_millis(800));
        RlScoringDefault::not_bundled(key)
    }

    /// M1: a slow probe reached from ASYNC code never blocks the runtime —
    /// on a current-thread runtime (the worst case: one thread), another
    /// task still runs on time while the probe is in flight.
    #[test]
    fn a_slow_probe_does_not_block_other_async_work() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_time().build().unwrap();
        rt.block_on(async {
            let probe = tokio::spawn(rl_scoring_default_async_with(
                "slow-async-test-profile".to_string(),
                slow_probe,
            ));
            let t0 = Instant::now();
            tokio::time::sleep(Duration::from_millis(20)).await;
            let other = t0.elapsed();
            assert!(other < Duration::from_millis(400), "other work stalled for {other:?}");
            assert!(!probe.await.unwrap().default_on);
        });
    }

    /// M1 safety net: a SYNC call that lands on a multi-thread runtime worker
    /// hands the worker off (`block_in_place`), so with ONE worker another
    /// task still runs while the probe is in flight.
    #[test]
    fn a_sync_call_on_a_runtime_worker_does_not_starve_it() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_time()
            .build()
            .unwrap();
        rt.block_on(async {
            let probe =
                tokio::spawn(async { resolve_with("slow-sync-test-profile", slow_probe) });
            // Let the probe task take the only worker first.
            std::thread::sleep(Duration::from_millis(50));
            let t0 = Instant::now();
            let other = tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(10)).await;
                t0.elapsed()
            })
            .await
            .unwrap();
            assert!(other < Duration::from_millis(400), "the worker was pinned for {other:?}");
            assert!(!probe.await.unwrap().default_on);
        });
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
