// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.97 R12 (owner ruling "Consolidate now", 2026-09-25): the ONE Rust
//! client for the container-runtime verdict.
//!
//! Every Rust surface that runs AFTER install — the launcher's infra stack
//! (`services::runtime`), the module plane (`services::container_runtime`),
//! the install preflight, the boot path, the hub's infra watchdog and module
//! supervisor — used to re-derive "which container runtime do I drive?" from
//! a Rust mirror of `vco_lib.runtime_reconcile`'s rules (the stale-record
//! arm, the bind-folder verdict, the one-engine probe). Those mirrors are
//! RETIRED. This module asks Python for the ONE answer:
//!
//! ```text
//! python -m vco_lib.runtime_reconcile decide --json \
//!        [--root <install_root>] [--mode read-only|install] [--purpose infra|module]
//! ```
//!
//! Tier A (shared code): the path is user-action-triggered and ms-scale, so
//! a subprocess is the right shape. The schema (`"schema": 1`) is documented
//! at the top of `vco_lib/runtime_reconcile.py`; the contract fixture is
//! `tests/fixtures/runtime_decide_cases.json`, whose `_comment` describes the
//! protocol this module's tests follow (temp install root, stub podman/docker
//! scripts, controlled PATH). Exit 0 = resolved, 3 = refused — both print the
//! JSON verdict; anything else is an internal error and surfaces as `Err`.
//!
//! ## Loud-fail rule
//!
//! After install, a missing or broken Python is a BROKEN INSTALL, never a
//! fallback case (`CLAUDE.md`, "VCO-internal code rules"): a spawn failure,
//! a timeout, an unparseable reply or a non-0/3 exit code returns `Err` with
//! a clear message naming the interpreter and the stderr. Callers surface
//! that error; NOTHING here falls back to a Rust copy of the decision.
//!
//! ## `search_path`
//!
//! The verdict's `search_path` is the PATH the caller should export so the
//! runtime tools resolve BY NAME to what was probed
//! (`vco_lib.tool_search_dirs.reachable_path`). The launcher and the hub
//! already augment their process PATH at startup from the SAME shared table
//! (`runtime::augment_path_for_graphical_launch` ↔
//! `vco_lib/tool_search_dirs.toml`), so a name spawned by the process
//! resolves to the same binary Python probed; [`RuntimeVerdict::search_path`]
//! is carried for callers that build a child PATH explicitly. The infra plane
//! spawns by absolute path ([`RuntimeVerdict::binary_path`]) anyway.
//!
//! ## Cache
//!
//! The hub's infra watchdog asks on a 45s tick; a verdict must not spawn
//! Python every tick. ONE TTL cache ([`CACHE_TTL`]) keyed by
//! (root, mode, purpose) serves every caller; `invalidate()` clears it (the
//! launcher's Re-detect button and the install preflight route through
//! `runtime::invalidate_cache`, which calls it).

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::process::CommandExt as _;

/// How the verdict may treat the install's runtime record.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Mode {
    /// Write nothing, start nothing (the default; every post-install
    /// Rust surface uses this — only install.py re-records).
    ReadOnly,
    /// install.py's form: the record may be re-written, the ledger
    /// appended. No Rust surface passes this today; it exists so the
    /// client covers the CLI contract.
    Install,
}

impl Mode {
    fn as_str(self) -> &'static str {
        match self {
            Mode::ReadOnly => "read-only",
            Mode::Install => "install",
        }
    }
}

/// Which plane the verdict is for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Purpose {
    /// The infra stack: compose is probed, `compose`/`compose_form` are set.
    Infra,
    /// The module plane (single containers): compose is NOT probed and the
    /// compose fields are `None`.
    Module,
}

impl Purpose {
    fn as_str(self) -> &'static str {
        match self {
            Purpose::Infra => "infra",
            Purpose::Module => "module",
        }
    }
}

/// The schema-1 verdict, parsed. Field semantics: the `decide` table at the
/// top of `vco_lib/runtime_reconcile.py` is the contract; these doc-comments
/// only name the Rust-side use.
#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeVerdict {
    /// The runtime to drive (`"podman"` / `"docker"`), or `None`.
    pub runtime: Option<String>,
    /// `resolved` / `absent` / `unknown` — the tri-state, verbatim (Rust code
    /// must not collapse it to a bool; `resolved` below is a convenience).
    pub state: String,
    /// `true` when `state` is `resolved` (something may be driven).
    pub resolved: bool,
    /// Compose argv prefix (`["podman", "compose"]`), infra purpose only.
    pub compose: Option<Vec<String>>,
    /// `"subcommand"` / `"standalone"`, infra purpose only.
    pub compose_form: Option<String>,
    /// Absolute path of the runtime binary Python resolved.
    pub binary_path: Option<PathBuf>,
    /// The PATH to export so runtime names resolve to what was probed.
    pub search_path: Option<String>,
    /// The first candidate binary installed, usable or not (`None` = no
    /// runtime is installed at all — M4: the boot path offers the install
    /// dialog on this, pin or not).
    pub installed: Option<String>,
    /// The pinned runtime (`None` = auto-detect).
    pub requested: Option<String>,
    /// `"env"` / `"record"` / `"confirmed"` / `"auto"`.
    pub requested_via: String,
    /// Whether the pinned binary is installed at all.
    pub requested_installed: bool,
    /// The OTHER runtime when it is usable and the pinned one is not.
    pub alternative_usable: Option<String>,
    /// The stale record was answered with the other runtime, read-only.
    pub record_reconciled: bool,
    /// The reconcile's outcome for the record.
    pub outcome: String,
    /// Why a stale record was NOT switched (a `[not_switched]` key), if any.
    pub not_switched_key: Option<String>,
    /// That reason rendered (Python's text, verbatim).
    pub not_switched: Option<String>,
    /// The two names are ONE engine (podman-docker shim): no "both", no
    /// switch; the recorded runtime is kept.
    pub same_engine: bool,
    /// `true` unless resolved.
    pub refused: bool,
    /// The full user-facing refusal text when refused — shown VERBATIM (M3:
    /// never re-derived from PATH state on the Rust side).
    pub refusal: Option<String>,
    /// Always set: why this runtime (or why none).
    pub reason: String,
}

impl RuntimeVerdict {
    fn parse(v: serde_json::Value) -> Result<Self, String> {
        let get_str = |k: &str| {
            v.get(k)
                .and_then(serde_json::Value::as_str)
                .map(|s| s.to_string())
        };
        let schema = v.get("schema").and_then(serde_json::Value::as_i64);
        if schema != Some(1) {
            return Err(format!(
                "runtime_reconcile decide: unsupported schema {:?} (expected 1)",
                schema
            ));
        }
        let state = get_str("state").unwrap_or_default();
        let compose = match v.get("compose") {
            Some(serde_json::Value::Array(a)) => Some(
                a.iter()
                    .filter_map(|s| s.as_str().map(|s| s.to_string()))
                    .collect::<Vec<_>>(),
            ),
            _ => None,
        };
        Ok(RuntimeVerdict {
            runtime: get_str("runtime").filter(|s| !s.is_empty()),
            state: state.clone(),
            resolved: state == "resolved",
            compose,
            compose_form: get_str("compose_form"),
            binary_path: get_str("binary_path").filter(|s| !s.is_empty()).map(PathBuf::from),
            search_path: get_str("search_path").filter(|s| !s.is_empty()),
            installed: get_str("installed"),
            requested: get_str("requested"),
            requested_via: get_str("requested_via").unwrap_or_else(|| "auto".to_string()),
            requested_installed: v
                .get("requested_installed")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false),
            alternative_usable: get_str("alternative_usable"),
            record_reconciled: v
                .get("record_reconciled")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false),
            outcome: get_str("outcome").unwrap_or_default(),
            not_switched_key: get_str("not_switched_key"),
            not_switched: get_str("not_switched"),
            same_engine: v
                .get("same_engine")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false),
            refused: v
                .get("refused")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(true),
            refusal: get_str("refusal"),
            reason: get_str("reason").ok_or("verdict lacks `reason`")?,
        })
    }

    /// The pin that chose the runtime, mapped to the module plane's pin
    /// source enum (`requested_via` `"env"` → the env override; `"record"`
    /// / `"confirmed"` → the runtime.txt record; `"auto"` → no pin).
    pub fn pin_source(
        &self,
    ) -> Option<(&str, super::container_runtime::RuntimePinSource)> {
        let name = self.requested.as_deref()?;
        match self.requested_via.as_str() {
            "env" => Some((name, super::container_runtime::RuntimePinSource::EnvOverride)),
            "record" | "confirmed" => {
                Some((name, super::container_runtime::RuntimePinSource::RuntimeTxt))
            }
            _ => None,
        }
    }
}

/// How long one `decide` spawn may take. Every probe inside the child is
/// already time-bounded per call (`_LIST_TIMEOUT_S` and the containers
/// module's probe timeouts); past this the child is stuck and the caller
/// sees a loud error instead of a hang.
const DECIDE_TIMEOUT: Duration = Duration::from_secs(60);

/// TTL for the ONE verdict cache. The hub's infra watchdog ticks at 45s;
/// a 60s TTL means Python runs at most once a minute per (root, mode,
/// purpose) however many surfaces ask in between — "short" enough that a
/// user fixing their runtime sees the change on the next tick's neighbour.
const CACHE_TTL: Duration = Duration::from_secs(60);

static CACHE: std::sync::Mutex<Option<HashMap<CacheKey, (Instant, RuntimeVerdict)>>> =
    std::sync::Mutex::new(None);

/// Clear the verdict cache. `services::runtime::invalidate_cache` (the
/// Re-detect button) and the install preflight call this so an explicit
/// user action re-probes immediately.
pub fn invalidate() {
    if let Ok(mut g) = CACHE.lock() {
        *g = None;
    }
}

/// Everything one `decide` spawn needs. The defaults live in [`decide`];
/// the contract tests override `python`, `path_env` and `env_pairs` so a
/// Rust test never mutates the process PATH or env.
#[derive(Debug, Clone, Default)]
pub struct VerdictOpts<'a> {
    /// Install root to decide for. `None` → `orchestrator_install_root()`
    /// (still `None` → the child resolves its own root).
    pub install_root: Option<&'a Path>,
    pub mode: Option<Mode>,
    pub purpose: Option<Purpose>,
    /// Test injection: run THIS interpreter instead of the RT-4 ladder.
    pub python: Option<&'a Path>,
    /// Test injection: the child's PATH (stub runtime scripts live here).
    pub path_env: Option<&'a OsString>,
    /// Extra child env pairs (e.g. `VCT_CONTAINER_RUNTIME`, `HOME`). Values
    /// are passed verbatim — an EMPTY string is a real value (the tests rely
    /// on `VCT_TOOL_SEARCH_DIRS=""` REPLACING the search table), so unsetting
    /// a key the process env carries is `unset_keys`' job.
    pub env_pairs: Vec<(String, String)>,
    /// Keys to REMOVE from the inherited allowlist (test isolation: drop the
    /// host's `VCT_CONTAINER_RUNTIME` so a case's "no pin" is really no pin).
    pub unset_keys: Vec<String>,
}

impl VerdictOpts<'_> {
    fn mode(&self) -> Mode {
        self.mode.unwrap_or(Mode::ReadOnly)
    }
    fn purpose(&self) -> Purpose {
        self.purpose.unwrap_or(Purpose::Infra)
    }
}

type CacheKey = (String, String, String);

fn cache_key(
    install_root: Option<&Path>,
    mode: Mode,
    purpose: Purpose,
) -> Option<CacheKey> {
    Some((
        install_root?.display().to_string(),
        mode.as_str().to_string(),
        purpose.as_str().to_string(),
    ))
}

/// The ONE verdict, from Python (TTL-cached). See [`VerdictOpts`] for the
/// knobs; production callers pass mode/purpose and nothing else. A `None`
/// install root is never cached (it means "no clone found" — an unstable
/// answer that must re-probe).
pub async fn decide(
    install_root: Option<&Path>,
    mode: Mode,
    purpose: Purpose,
) -> Result<RuntimeVerdict, String> {
    decide_cached(VerdictOpts {
        install_root,
        mode: Some(mode),
        purpose: Some(purpose),
        ..VerdictOpts::default()
    })
    .await
}

/// The cached entry point with full opts (the contract tests drive the stub
/// machine through this so the TTL cache is exercised by the same key the
/// production callers use).
pub async fn decide_cached(opts: VerdictOpts<'_>) -> Result<RuntimeVerdict, String> {
    let key = cache_key(opts.install_root, opts.mode(), opts.purpose());
    if let Some(k) = &key {
        if let Ok(g) = CACHE.lock() {
            if let Some((at, v)) = g.as_ref().and_then(|m| m.get(k)) {
                if at.elapsed() < CACHE_TTL {
                    return Ok(v.clone());
                }
            }
        }
    }
    let verdict = decide_uncached(opts).await?;
    if let Some(k) = key {
        if let Ok(mut g) = CACHE.lock() {
            g.get_or_insert_with(HashMap::new)
                .insert(k, (Instant::now(), verdict.clone()));
        }
    }
    Ok(verdict)
}

/// The uncached spawn. Returns `Err` for every loud-fail shape (missing
/// Python, spawn failure, timeout, bad exit code, unparseable JSON).
pub async fn decide_uncached(opts: VerdictOpts<'_>) -> Result<RuntimeVerdict, String> {
    let mode = opts.mode();
    let purpose = opts.purpose();
    let root = opts
        .install_root
        .map(Path::to_path_buf)
        .or_else(crate::orchestrator_manifest::orchestrator_install_root);

    let python = match opts.python {
        Some(p) => p.to_path_buf(),
        None => crate::python_resolve::resolve_python_for_vco_lib().ok_or_else(
            || {
                // Loud-fail: a post-install surface with no vco_lib-capable
                // Python is a broken install, never a Rust fallback.
                "broken install: no Python environment with VCO's dependencies \
                 found (VCT_VENV / <install-root>/.venv ladder) — cannot run \
                 `python -m vco_lib.runtime_reconcile decide`, the ONE \
                 container-runtime verdict"
                    .to_string()
            },
        )?,
    };

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.runtime_reconcile")
        .arg("decide")
        .arg("--json")
        .arg("--mode")
        .arg(mode.as_str())
        .arg("--purpose")
        .arg(purpose.as_str());
    if let Some(root) = &root {
        cmd.arg("--root").arg(root);
        // `python -m` puts the CWD first on sys.path, so the child imports
        // THIS clone's vco_lib ahead of anything the venv carries (the same
        // rule `services::vco_lib_bridge` documents).
        cmd.current_dir(root);
    }

    // Env: the child probes runtime binaries and reads the pin, so it
    // needs PATH, the home/temp/system family and VCT_CONTAINER_RUNTIME /
    // VCT_TOOL_SEARCH_DIRS. The home family comes from the ONE shared
    // child-env table (`services::child_env`, R12-bis P2-1) — per-OS, so
    // a Windows child gets USERPROFILE/APPDATA/LOCALAPPDATA/
    // HOMEDRIVE/HOMEPATH plus SYSTEMROOT/COMSPEC (it normally has no
    // HOME; `Path.home()` and the runtime CLIs' config lookups read the
    // Windows family, and without SYSTEMROOT a spawned python.exe fails
    // to initialize). Nothing else is a decision input; per-launcher
    // quirks must not leak in.
    cmd.env_clear();
    if let Some(path) = opts.path_env {
        cmd.env("PATH", path);
    } else if let Some(p) = crate::paths::lookup_path() {
        // The ONE process-PATH read (injectable per thread via
        // `paths::with_lookup_path`) — not a hand-rolled var_os walk.
        cmd.env("PATH", p);
    }
    for (key, value) in super::child_env::present_pairs() {
        if key == "PATH" {
            // Set above from the injectable lookup path — never re-read
            // from the raw process env.
            continue;
        }
        if opts.unset_keys.iter().any(|k| k == key) {
            continue;
        }
        cmd.env(key, value);
    }
    for key in ["VCT_CONTAINER_RUNTIME", "VCT_TOOL_SEARCH_DIRS"] {
        if opts.unset_keys.iter().any(|k| k == key) {
            continue;
        }
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    for (k, v) in &opts.env_pairs {
        cmd.env(k, v);
    }

    let out = tokio::time::timeout(DECIDE_TIMEOUT, cmd.output())
        .await
        .map_err(|_| {
            format!(
                "runtime_reconcile decide: timed out after {}s ({} {})",
                DECIDE_TIMEOUT.as_secs(),
                python.display(),
                root.as_ref().map(|r| r.display().to_string()).unwrap_or_default()
            )
        })?
        .map_err(|e| {
            format!(
                "broken install: cannot spawn `{} -m vco_lib.runtime_reconcile \
                 decide` ({}). After install this Python must exist and run — \
                 the container-runtime decision has ONE home in vco_lib and no \
                 Rust fallback",
                python.display(),
                e
            )
        })?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
    let verdict = RuntimeVerdict::parse(
        serde_json::from_str(stdout.trim()).map_err(|e| {
            format!(
                "runtime_reconcile decide produced unreadable output ({}). \
                 stdout: {} stderr: {}",
                e,
                stdout.trim(),
                stderr
            )
        })?,
    )?;
    match out.status.code() {
        Some(0) | Some(3) => Ok(verdict),
        code => Err(format!(
            "runtime_reconcile decide exited with {:?} (expected 0 resolved / \
             3 refused): {}",
            code, stderr
        )),
    }
}

// ---------------------------------------------------------------------------
// Contract-test support — shared with `services::runtime`'s parity tests so
// the stub grammar lives in ONE place (the same fixture lane both read).
// ---------------------------------------------------------------------------
#[cfg(test)]
pub(crate) mod contract_support {
    use super::RuntimeVerdict;
    use std::path::{Path, PathBuf};

    pub(crate) fn repo_root() -> PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap()
            .to_path_buf()
    }

    pub(crate) fn fixture_json(rel: &str) -> serde_json::Value {
        let path = repo_root().join(rel);
        serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture readable"))
            .expect("fixture parses")
    }

    /// Single-quote a value for the stub scripts (the fixtures' strings carry
    /// no quotes; the escape keeps that an invariant, not an assumption).
    pub(crate) fn sh_quote(s: &str) -> String {
        format!("'{}'", s.replace('\'', "'\\''"))
    }

    #[cfg(unix)]
    pub(crate) fn make_executable(path: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let mut perm = std::fs::metadata(path).unwrap().permissions();
        perm.set_mode(0o755);
        std::fs::set_permissions(path, perm).unwrap();
    }

    #[cfg(not(unix))]
    pub(crate) fn make_executable(_path: &Path) {
        // The contract tests are Unix-only today (stub shell scripts); the
        // schema tests cover the parse contract everywhere.
    }

    /// A Python that can import THIS checkout's vco_lib: the RT-4 ladder's
    /// venv if one resolves (PYTHONPATH below re-points it at the public
    /// checkout), else a plain `python3` (runtime_reconcile is stdlib-only).
    #[cfg(unix)]
    pub(crate) fn test_python() -> PathBuf {
        crate::python_resolve::resolve_python_for_vco_lib()
            .unwrap_or_else(|| PathBuf::from("python3"))
    }

    /// The child env every contract-style test hands the CLI: stubs-only
    /// PATH, an empty HOME, `VCT_TOOL_SEARCH_DIRS=""` (an EMPTY value
    /// REPLACES the search table — the child then sees only the stubs, never
    /// the host's real podman/docker) and PYTHONPATH at the public checkout
    /// (the temp roots carry no vco_lib).
    pub(crate) fn isolation_env(bin_dir: &Path, home: &Path) -> Vec<(String, String)> {
        vec![
            ("HOME".to_string(), home.display().to_string()),
            ("VCT_TOOL_SEARCH_DIRS".to_string(), String::new()),
            ("PYTHONPATH".to_string(), repo_root().display().to_string()),
            ("PATH".to_string(), bin_dir.display().to_string()),
        ]
    }

    /// One stub runtime answering the decide fixture's argv grammar
    /// (`_comment`): `version` prints `version_out` and exits `version_ok`,
    /// `info` exits `daemon_ok`, `compose version` exits `compose_subcommand`,
    /// the listings print `ps_a` / `ps` / `volume_ls` (and `ps_a_ids` for
    /// the `--no-trunc` full-ID form) gated on the daemon being up.
    pub(crate) fn write_stub(bin_dir: &Path, name: &str, spec: &serde_json::Value) {
        let str_list = |key: &str, default: &[&str]| -> Vec<String> {
            match spec.get(key).and_then(serde_json::Value::as_array) {
                Some(a) => a
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect(),
                None => default.iter().map(|s| s.to_string()).collect(),
            }
        };
        let bool_or = |key: &str, default: bool| {
            spec.get(key)
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(default)
        };
        // `echo` appends the newline itself; `printf %s 'x\n'` would emit a
        // LITERAL backslash-n and fuse every listing into one name.
        let emit = |xs: &[String]| -> String {
            xs.iter()
                .map(|n| format!("echo {}; ", sh_quote(n)))
                .collect::<String>()
        };
        let body = if !bool_or("on_path", true) {
            "#!/bin/sh\nexit 1\n".to_string()
        } else {
            let daemon_rc = if bool_or("daemon_ok", true) { 0 } else { 1 };
            let compose_rc = if bool_or("compose_subcommand", true) { 0 } else { 1 };
            let version_rc = if bool_or("version_ok", true) { 0 } else { 1 };
            let vout = spec
                .get("version_out")
                .and_then(serde_json::Value::as_str)
                .map(|s| s.trim_end().to_string())
                .unwrap_or_else(|| format!("{name} version 5.0.0"));
            let ps_a = str_list("ps_a", &["someone_elses_ollama"]);
            let ps = str_list("ps", &[]);
            let vols = str_list("volume_ls", &["someone_elses_data"]);
            let ids = str_list("ps_a_ids", &[]);
            format!(
                "#!/bin/sh\ncase \"$1\" in\n  version) echo {}; exit {version_rc} ;;\n  \
                 info) exit {daemon_rc} ;;\nesac\ncase \"$1 $2\" in\n  \"compose version\") exit \
                 {compose_rc} ;;\n  \"volume ls\") {vol_cmd}exit {daemon_rc} ;;\nesac\ncase \"$1 \
                 $2 $3\" in\n  \"ps -a --no-trunc\") {ids_cmd}exit {daemon_rc} ;;\nesac\ncase \
                 \"$1 $2\" in\n  \"ps -a\") {ps_a_cmd}exit {daemon_rc} ;;\n  \"ps \
                 --format\") {ps_cmd}exit {daemon_rc} ;;\nesac\nexit 0\n",
                sh_quote(&vout),
                vol_cmd = emit(&vols),
                ids_cmd = emit(&ids),
                ps_a_cmd = emit(&ps_a),
                ps_cmd = emit(&ps),
            )
        };
        let path = bin_dir.join(name);
        std::fs::write(&path, body).expect("stub written");
        make_executable(&path);
    }

    /// Apply a fixture `expect` subset to the parsed verdict. `_contains` /
    /// `_not_contains` rows are substring asserts on string fields
    /// (`_not_contains` FIRST — it is the only suffix pair whose keys
    /// overlap).
    pub(crate) fn assert_subset(case: &str, verdict: &RuntimeVerdict, exp: &serde_json::Value) {
        let obj = exp.as_object().expect("expect is an object");
        for (key, want) in obj {
            if let Some(field) = key.strip_suffix("_not_contains") {
                let hay = verdict_field(verdict, field)
                    .unwrap_or_else(|| panic!("{case}: no field {field}"));
                for needle in want.as_array().expect("list") {
                    let needle = needle.as_str().unwrap();
                    assert!(
                        !hay.contains(needle),
                        "{case}: {field} must NOT contain {needle:?} — {hay:?}"
                    );
                }
            } else if let Some(field) = key.strip_suffix("_contains") {
                let hay = verdict_field(verdict, field)
                    .unwrap_or_else(|| panic!("{case}: no field {field}"));
                for needle in want.as_array().expect("list") {
                    let needle = needle.as_str().unwrap();
                    assert!(
                        hay.contains(needle),
                        "{case}: {field} must contain {needle:?} — {hay:?}"
                    );
                }
            } else {
                let got = verdict_json_field(verdict, key)
                    .unwrap_or_else(|| panic!("{case}: no field {key}"));
                assert_eq!(got, *want, "{case}: field {key}");
            }
        }
    }

    /// The string fields `_contains` / `_not_contains` rows may target.
    fn verdict_field(v: &RuntimeVerdict, field: &str) -> Option<String> {
        Some(match field {
            "refusal" => v.refusal.clone()?,
            "reason" => v.reason.clone(),
            "not_switched" => v.not_switched.clone()?,
            _ => return None,
        })
    }

    /// Every schema-1 key a fixture `expect` may equality-assert, as the
    /// JSON the CLI printed (so `null` round-trips).
    fn verdict_json_field(v: &RuntimeVerdict, field: &str) -> Option<serde_json::Value> {
        Some(match field {
            "state" => serde_json::json!(v.state),
            "runtime" => serde_json::json!(v.runtime),
            "compose" => serde_json::json!(v.compose),
            "compose_form" => serde_json::json!(v.compose_form),
            "requested" => serde_json::json!(v.requested),
            "requested_via" => serde_json::json!(v.requested_via),
            "requested_installed" => serde_json::json!(v.requested_installed),
            "alternative_usable" => serde_json::json!(v.alternative_usable),
            "record_reconciled" => serde_json::json!(v.record_reconciled),
            "outcome" => serde_json::json!(v.outcome),
            "not_switched_key" => serde_json::json!(v.not_switched_key),
            "same_engine" => serde_json::json!(v.same_engine),
            "refused" => serde_json::json!(v.refused),
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verdict_parses_the_documented_schema() {
        let v = RuntimeVerdict::parse(serde_json::json!({
            "schema": 1, "state": "absent", "runtime": null, "compose": null,
            "compose_form": null, "binary_path": null, "search_path": null,
            "installed": "podman", "requested": "docker", "requested_via": "record",
            "requested_installed": false, "alternative_usable": "podman",
            "record_reconciled": false, "outcome": "unusable",
            "not_switched_key": "no_data", "not_switched": "no data anywhere",
            "same_engine": false, "refused": true,
            "refusal": "refused (not switched: no data anywhere)", "reason": "why"
        }))
        .expect("parses");
        assert!(v.refused);
        assert_eq!(v.not_switched_key.as_deref(), Some("no_data"));
        assert_eq!(v.pin_source().map(|(_, s)| s), Some(super::super::container_runtime::RuntimePinSource::RuntimeTxt));
        assert_eq!(v.pin_source().map(|(n, _)| n), Some("docker"));
    }

    #[test]
    fn a_wrong_schema_is_a_loud_error() {
        let err = RuntimeVerdict::parse(serde_json::json!({"schema": 2})).unwrap_err();
        assert!(err.contains("schema"), "{err}");
    }

    // -----------------------------------------------------------------------
    // The decide contract, through THIS client, against the REAL CLI.
    // `tests/fixtures/runtime_decide_cases.json`'s `_comment` spells out the
    // protocol these tests follow: a temp install root per case, stub
    // podman/docker scripts on a PATH handed to the child (never the
    // process's), HOME at an empty temp dir, and the same `expect` subset
    // match the Python lane applies (`_not_contains` FIRST — it is the only
    // suffix pair whose keys overlap).
    // -----------------------------------------------------------------------

    use super::contract_support::*;

    fn fixture_cases() -> serde_json::Value {
        super::contract_support::fixture_json("tests/fixtures/runtime_decide_cases.json")
    }


    #[cfg(unix)]
    #[tokio::test]
    async fn decide_contract_fixture_cases_pass_through_the_client() {
        let cases = fixture_cases()["cases"].as_array().unwrap().clone();
        assert!(!cases.is_empty(), "fixture loaded");
        let python = test_python();
        for case in &cases {
            let name = case["name"].as_str().unwrap().to_string();
            let dir = tempfile::TempDir::new().unwrap();
            let root = dir.path().join("install-root");
            std::fs::create_dir_all(root.join("state/install")).unwrap();
            if let Some(record) = case.get("record").and_then(|v| v.as_str()) {
                std::fs::write(root.join("state/install/runtime.txt"), format!("{record}\n"))
                    .unwrap();
            }
            if let Some(confirmed) = case.get("confirmed").and_then(|v| v.as_str()) {
                std::fs::write(
                    root.join("state/install/runtime.confirmed"),
                    format!("{confirmed}\n"),
                )
                .unwrap();
            }
            let data_dir = dir.path().join("data");
            std::fs::create_dir_all(&data_dir).unwrap();
            if let Some(infra_env) = case.get("infra_env").and_then(|v| v.as_str()) {
                std::fs::create_dir_all(root.join("infrastructure")).unwrap();
                std::fs::write(
                    root.join("infrastructure/.env"),
                    infra_env.replace("{DATA}", &data_dir.display().to_string()),
                )
                .unwrap();
            }
            for folder in case
                .get("folders")
                .and_then(|v| v.as_array())
                .into_iter()
                .flatten()
            {
                let p = std::path::PathBuf::from(
                    folder["path"]
                        .as_str()
                        .unwrap()
                        .replace("{DATA}", &data_dir.display().to_string()),
                );
                std::fs::create_dir_all(&p).unwrap();
                for entry in folder
                    .get("entries")
                    .and_then(|v| v.as_array())
                    .into_iter()
                    .flatten()
                    .filter_map(|e| e.as_str())
                {
                    std::fs::write(p.join(entry), "x").unwrap();
                }
            }
            // The fake machine: stub runtimes on a PATH the child alone sees.
            let bin_dir = dir.path().join("bin");
            std::fs::create_dir_all(&bin_dir).unwrap();
            for (rt, spec) in case["runtimes"].as_object().expect("runtimes map") {
                write_stub(&bin_dir, rt, spec);
                if spec.get("standalone").and_then(|v| v.as_bool()) == Some(true) {
                    let p = bin_dir.join(format!("{rt}-compose"));
                    std::fs::write(&p, "#!/bin/sh\nexit 0\n").unwrap();
                    make_executable(&p);
                }
            }
            let home = dir.path().join("home");
            std::fs::create_dir_all(&home).unwrap();
            let mut env_pairs = vec![
                ("HOME".to_string(), home.display().to_string()),
                // An EMPTY value REPLACES the tool-search table — the child's
                // probes then see only the stubs, never the host's real
                // podman/docker (`VCT_TOOL_SEARCH_DIRS` is an override, not
                // an "unset means default" key for this test).
                ("VCT_TOOL_SEARCH_DIRS".to_string(), String::new()),
                // The temp root has no vco_lib and the ladder's venv may be
                // another checkout's; the public repo's vco_lib is the code
                // under contract (PYTHONPATH precedes site-packages).
                ("PYTHONPATH".to_string(), repo_root().display().to_string()),
            ];
            let mut unset_keys = Vec::new();
            match case.get("env_pin").and_then(|v| v.as_str()) {
                Some(pin) => {
                    env_pairs.push(("VCT_CONTAINER_RUNTIME".to_string(), pin.to_string()));
                }
                None => unset_keys.push("VCT_CONTAINER_RUNTIME".to_string()),
            }
            let mode = match case.get("mode").and_then(|v| v.as_str()) {
                Some("install") => Mode::Install,
                _ => Mode::ReadOnly,
            };
            let purpose = match case.get("purpose").and_then(|v| v.as_str()) {
                Some("module") => Purpose::Module,
                _ => Purpose::Infra,
            };
            let verdict = decide_uncached(VerdictOpts {
                install_root: Some(&root),
                mode: Some(mode),
                purpose: Some(purpose),
                python: Some(python.as_path()),
                path_env: Some(&OsString::from(bin_dir.display().to_string())),
                env_pairs,
                unset_keys,
            })
            .await
            .unwrap_or_else(|e| panic!("{name}: decide failed: {e}"));
            // The refusal contract the Python lane pins for every case.
            assert_eq!(verdict.refused, !verdict.resolved, "{name}");
            assert!(!verdict.reason.is_empty(), "{name}");
            if verdict.refused {
                assert_eq!(verdict.refusal.as_deref(), Some(verdict.reason.as_str()), "{name}");
            }
            assert_subset(&name, &verdict, &case["expect"]);
            // READ-ONLY is a promise: the record never moved.
            if mode == Mode::ReadOnly {
                if let Some(record) = case.get("record").and_then(|v| v.as_str()) {
                    let on_disk =
                        std::fs::read_to_string(root.join("state/install/runtime.txt"))
                            .unwrap_or_default();
                    assert_eq!(on_disk.trim(), record, "{name}: read-only rewrote the record");
                }
            }
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn the_ttl_cache_serves_repeat_ticks_without_respawning_python() {
        // A stub "python" that counts spawns and prints a valid verdict.
        let dir = tempfile::TempDir::new().unwrap();
        let counter = dir.path().join("spawns");
        let stub = dir.path().join("counting-python");
        std::fs::write(
            &stub,
            format!(
                "#!/bin/sh\nn=$(cat {counter} 2>/dev/null || echo 0)\necho $((n + 1)) > \
                 {counter}\nprintf '%s' {verdict_json}\nexit 0\n",
                counter = counter.display(),
                verdict_json = sh_quote(
                    &serde_json::json!({
                        "schema": 1, "state": "resolved", "runtime": "podman",
                        "compose": ["podman", "compose"], "compose_form": "subcommand",
                        "binary_path": "/usr/bin/podman", "search_path": null,
                        "installed": "podman", "requested": null, "requested_via": "auto",
                        "requested_installed": false, "alternative_usable": null,
                        "record_reconciled": false, "outcome": "", "not_switched_key": null,
                        "not_switched": null, "same_engine": false, "refused": false,
                        "refusal": null, "reason": "stub"
                    })
                    .to_string(),
                ),
            ),
        )
        .unwrap();
        make_executable(&stub);
        let root = dir.path().join("install-root");
        std::fs::create_dir_all(&root).unwrap();
        let opts = || VerdictOpts {
            install_root: Some(&root),
            mode: Some(Mode::ReadOnly),
            purpose: Some(Purpose::Infra),
            python: Some(stub.as_path()),
            path_env: None,
            env_pairs: Vec::new(),
            unset_keys: Vec::new(),
        };
        let spawns = || {
            std::fs::read_to_string(&counter)
                .ok()
                .and_then(|s| s.trim().parse::<u32>().ok())
                .unwrap_or(0)
        };
        invalidate();
        // Two asks inside the TTL (the hub watchdog's 45s tick), then one
        // after invalidate(): the counter must read 2, not 3.
        let a = decide_cached(opts()).await.expect("first ask");
        let b = decide_cached(opts()).await.expect("cached ask");
        assert_eq!(a, b);
        assert_eq!(spawns(), 1, "second ask inside the TTL must not respawn");
        invalidate();
        decide_cached(opts()).await.expect("post-invalidate ask");
        assert_eq!(spawns(), 2, "invalidate() must force a fresh spawn");
    }
}
