// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Who owns a container, read from its REAL labels — the ONE Rust home
//! (v0.2.100 WP-06, AD-5 / AD-12).
//!
//! ## Why this module exists (field incident 2026-09-29)
//!
//! The hub's infra watchdog decided ownership from the launcher.db
//! `service_endpoints` row alone (`vco_managed` + `enabled` ⇒ "VCO's compose
//! owns it"). On the incident machine the stopped `vco_code_embed` had been
//! created by ANOTHER compose project; the watchdog composed against it, the
//! container disappeared, the create failed, and the watchdog logged
//! "restart issued successfully" and reset its crash-loop budget. Real user
//! data (a 110 GB model bind, a 7.4 GB cache) sits behind these containers.
//!
//! So every Rust surface that acts on an existing container asks THIS module:
//!
//! * [`read_identity`] — state + compose project + label family from the
//!   container's own labels (`com.docker.compose.project`,
//!   `io.podman.compose.project`, `com.docker.compose.config-hash`); a name
//!   `inspect` cannot see is looked up in `podman ps -a --external`, so a
//!   storage-only leftover is never mistaken for "missing".
//! * [`ownership`] — `Owned` only when the label names the project the row
//!   records (or, with no recorded project, the installer's own project);
//!   `Foreign` otherwise; `Unknown` when it cannot be decided. Callers act only
//!   on `Owned`.
//! * [`verify_running`] — "issued" is never success: the post-condition is
//!   read back from the runtime.
//! * the launcher's OWN creation label ([`LAUNCHER_LABEL`]) that module
//!   containers carry from creation, and [`launcher_label_verdict`], which the
//!   orphan reaper requires before it may remove anything (L2-F17).
//! * [`guarded_up`] — the ONE Rust caller of the guarded compose verb
//!   `python -m vco_lib.service_lifecycle up` (data-identity guard first; a
//!   refused service is never removed or composed).
//!
//! This is the only Rust file that names the label keys or parses them
//! (grep proof in the WP-06 report). Cross-language: the Python side is
//! `vco_lib/containers.py` (`compose_identity_of`, `compose_label_family`,
//! `foreign_compose_identity`, `compose_project_name`); both execute
//! `tests/fixtures/container_ownership_parity.json` (Python:
//! `tests/test_v02100_container_ownership_parity.py`; Rust: this file's tests).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use crate::db::service_endpoints::ServiceEndpointRow;
use crate::process::CommandExt as _;

// ─── label keys (the one Rust home) ──────────────────────────────────────

/// Set by the launcher on every module container it creates; the value is
/// the install-root id ([`owner_id_for_root`]). MUST MATCH nothing else — no
/// other file may spell it.
pub const LAUNCHER_LABEL: &str = "io.vibecoded.vct.launcher";
/// The compose project a container was created under (docker compose AND
/// podman-compose write it). MUST MATCH `vco_lib/containers.py::COMPOSE_PROJECT_LABEL`.
pub const COMPOSE_PROJECT_LABEL: &str = "com.docker.compose.project";
/// podman-compose's own project label — its presence names the podman family.
/// MUST MATCH `vco_lib/containers.py::PODMAN_COMPOSE_PROJECT_LABEL`.
pub const PODMAN_COMPOSE_PROJECT_LABEL: &str = "io.podman.compose.project";
/// docker compose v2's config hash — podman-compose never writes it.
/// MUST MATCH `vco_lib/containers.py::DOCKER_COMPOSE_CONFIG_HASH_LABEL`.
pub const DOCKER_COMPOSE_CONFIG_HASH_LABEL: &str = "com.docker.compose.config-hash";

/// A container's labels.
pub type Labels = HashMap<String, String>;

/// Labels from the JSON a runtime prints: an object (`inspect`, `podman ps
/// --format json`), a `k=v,k2=v2` string (`docker ps --format json`), or
/// `null`/absent (no labels).
pub fn labels_from_json(v: &serde_json::Value) -> Labels {
    match v {
        serde_json::Value::Object(map) => map
            .iter()
            .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
            .collect(),
        serde_json::Value::String(s) => s
            .split(',')
            .filter_map(|kv| {
                let (k, v) = kv.split_once('=')?;
                let k = k.trim();
                (!k.is_empty()).then(|| (k.to_string(), v.trim().to_string()))
            })
            .collect(),
        _ => Labels::new(),
    }
}

/// The labels of one `ps --format json` row (its `Labels` field).
pub fn labels_from_ps_row(row: &serde_json::Value) -> Labels {
    row.get("Labels").map(labels_from_json).unwrap_or_default()
}

fn label<'a>(labels: &'a Labels, key: &str) -> Option<&'a str> {
    labels.get(key).map(|s| s.trim()).filter(|s| !s.is_empty())
}

/// The compose tool family whose labels a container carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Provider {
    Docker,
    Podman,
    /// The labels do not say (not compose-created, or an old tool).
    None,
}

impl Provider {
    /// The word `vco_lib.containers.ComposeIdentity.provider` uses.
    pub fn as_str(self) -> &'static str {
        match self {
            Provider::Docker => "docker",
            Provider::Podman => "podman",
            Provider::None => "",
        }
    }
}

/// Which compose tool's labels these are. MUST MATCH
/// `vco_lib/containers.py::compose_label_family`.
pub fn label_family(labels: &Labels) -> Provider {
    if label(labels, PODMAN_COMPOSE_PROJECT_LABEL).is_some() {
        Provider::Podman
    } else if label(labels, DOCKER_COMPOSE_CONFIG_HASH_LABEL).is_some() {
        Provider::Docker
    } else {
        Provider::None
    }
}

/// The compose project of a container (`com.docker.compose.project`), as
/// `vco_lib.containers.compose_identity_of` reads it.
pub fn compose_project_of(labels: &Labels) -> Option<String> {
    label(labels, COMPOSE_PROJECT_LABEL).map(str::to_string)
}

// ─── the compose project VCO's own stack runs under ──────────────────────

/// The project compose derives for `compose_dir` from its compose file's
/// text: a top-level `name:` key wins, else the directory basename
/// lower-cased with every character outside `[a-z0-9_-]` dropped and leading
/// `-`/`_` trimmed. Rule-C mirror — MUST MATCH
/// `vco_lib/containers.py::compose_project_name` (the `project_name_cases`
/// of the parity table pin both). `None` when nothing is left.
pub fn compose_project_name(compose_dir: &Path, compose_text: &str) -> Option<String> {
    for line in compose_text.lines() {
        if let Some(rest) = line.strip_prefix("name:") {
            let rest = rest.trim_start();
            let rest = rest.strip_prefix(['\'', '"']).unwrap_or(rest);
            let name: String = rest
                .chars()
                .take_while(|c| !matches!(c, '\'' | '"' | '#') && !c.is_whitespace())
                .collect();
            if !name.is_empty() {
                return Some(name);
            }
        }
    }
    let raw = compose_dir.file_name()?.to_string_lossy().to_lowercase();
    let kept: String = raw
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_' || *c == '-')
        .collect();
    let trimmed = kept.trim_start_matches(['-', '_']).to_string();
    (!trimmed.is_empty()).then_some(trimmed)
}

/// The installer's own compose project: [`compose_project_name`] of
/// `<infra_dir>/docker-compose.yml` (an unreadable file derives from the
/// directory alone, as the Python side does).
pub fn installer_compose_project(infra_dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(infra_dir.join("docker-compose.yml")).unwrap_or_default();
    compose_project_name(infra_dir, &text)
}

// ─── running a runtime command (injectable) ──────────────────────────────

/// One finished runtime command.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CmdOutput {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
}

/// Runs `<runtime> <args>`. The production impl is [`RuntimeRunner`]; tests
/// script a fake so every decision below is exercised without a runtime.
#[allow(async_fn_in_trait)]
pub trait ContainerRunner {
    /// `"podman"` or `"docker"` — decides whether `--external` exists.
    fn runtime_name(&self) -> &str;
    /// `Err` = could not run / timed out (never a verdict about the
    /// container).
    async fn run(&self, args: &[&str], timeout: Duration) -> Result<CmdOutput, String>;
}

/// The real runtime binary.
#[derive(Debug, Clone)]
pub struct RuntimeRunner {
    pub binary: PathBuf,
    pub runtime: String,
}

impl ContainerRunner for RuntimeRunner {
    fn runtime_name(&self) -> &str {
        &self.runtime
    }

    async fn run(&self, args: &[&str], timeout: Duration) -> Result<CmdOutput, String> {
        let mut cmd = tokio::process::Command::new(&self.binary).silent();
        cmd.args(args)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);
        match tokio::time::timeout(timeout, cmd.output()).await {
            Ok(Ok(o)) => Ok(CmdOutput {
                success: o.status.success(),
                stdout: String::from_utf8_lossy(&o.stdout).into_owned(),
                stderr: String::from_utf8_lossy(&o.stderr).into_owned(),
            }),
            Ok(Err(e)) => Err(format!("spawn {} {}: {}", self.runtime, args.join(" "), e)),
            Err(_) => Err(format!(
                "{} {} did not answer within {} s",
                self.runtime,
                args.first().copied().unwrap_or(""),
                timeout.as_secs()
            )),
        }
    }
}

// ─── identity ────────────────────────────────────────────────────────────

/// What the runtime says about a container name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContainerState {
    Running,
    /// Needs `unpause`, never `start`/`up`.
    Paused,
    /// Exists and is not running (`exited`, `created`, `stopped`, `dead`, …).
    Stopped(String),
    /// Mid-transition (`restarting`, `removing`, `stopping`): the runtime is
    /// already acting on it — nothing to do this round.
    Transitioning(String),
    /// Neither `inspect` nor (podman) `ps -a --external` knows the name.
    Missing,
    /// Only podman's storage knows the name (the leftover of a failed
    /// unmount): it can be neither started nor re-created under that name.
    StorageOnly,
    /// Could not be read (daemon down, timeout, unparseable) — never
    /// collapsed to Missing.
    Unknown(String),
}

/// A container's identity from its real labels (AD-5).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub state: ContainerState,
    /// `com.docker.compose.project`, when the container carries it.
    pub project: Option<String>,
    pub provider: Provider,
    pub storage_only: bool,
    pub labels: Labels,
}

impl Identity {
    fn bare(state: ContainerState) -> Self {
        let storage_only = state == ContainerState::StorageOnly;
        Identity { state, project: None, provider: Provider::None, storage_only, labels: Labels::new() }
    }

    /// Identity of an existing container whose labels were read.
    pub fn with_labels(state: ContainerState, labels: Labels) -> Self {
        Identity {
            project: compose_project_of(&labels),
            provider: label_family(&labels),
            storage_only: false,
            state,
            labels,
        }
    }
}

const INSPECT_TIMEOUT: Duration = Duration::from_secs(10);
const EXTERNAL_TIMEOUT: Duration = Duration::from_secs(15);
/// Go template both runtimes accept: the status and the labels, as JSON.
const INSPECT_FORMAT: &str = "{{json .State.Status}}\t{{json .Config.Labels}}";

/// Did `inspect` fail because the name does not exist? Exactly the two
/// runtime phrasings — nothing looser (a `crun: executable file … not found`
/// from some other failure must never read as "missing").
pub fn inspect_says_missing(stderr: &str) -> bool {
    let lc = stderr.to_lowercase();
    lc.contains("no such container") || lc.contains("no such object")
}

/// Map a `.State.Status` word to a [`ContainerState`].
pub fn state_of_status(status: &str) -> ContainerState {
    match status.trim() {
        "running" => ContainerState::Running,
        "paused" => ContainerState::Paused,
        s @ ("exited" | "created" | "dead" | "stopped" | "configured" | "initialized") => {
            ContainerState::Stopped(s.to_string())
        }
        s @ ("restarting" | "removing" | "stopping") => ContainerState::Transitioning(s.to_string()),
        other => ContainerState::Unknown(format!("unrecognised container status {:?}", other)),
    }
}

/// Parse `inspect --format INSPECT_FORMAT` output.
pub fn parse_inspect_output(stdout: &str) -> Identity {
    let line = stdout.lines().find(|l| !l.trim().is_empty()).unwrap_or("");
    let (status_json, labels_json) = line.split_once('\t').unwrap_or((line, "null"));
    let status = match serde_json::from_str::<serde_json::Value>(status_json.trim()) {
        Ok(serde_json::Value::String(s)) => s,
        _ => return Identity::bare(ContainerState::Unknown(format!("unreadable inspect output {:?}", line))),
    };
    let labels = serde_json::from_str::<serde_json::Value>(labels_json.trim())
        .map(|v| labels_from_json(&v))
        .unwrap_or_default();
    Identity::with_labels(state_of_status(&status), labels)
}

/// Does `ps -a --external --format json` output list `name`? `None` when
/// the output cannot be read.
pub fn external_lists_name(json: &str, name: &str) -> Option<bool> {
    let trimmed = json.trim();
    if trimmed.is_empty() || trimmed == "null" {
        return Some(false);
    }
    let rows: serde_json::Value = serde_json::from_str(trimmed).ok()?;
    let rows = rows.as_array()?;
    Some(rows.iter().any(|row| {
        let names = row.get("Names");
        let listed: Vec<String> = match names {
            Some(serde_json::Value::Array(a)) => {
                a.iter().filter_map(|v| v.as_str()).map(|s| s.trim_start_matches('/').to_string()).collect()
            }
            Some(serde_json::Value::String(s)) => vec![s.trim_start_matches('/').to_string()],
            _ => Vec::new(),
        };
        listed.iter().any(|n| n == name)
    }))
}

/// Read `name`'s identity: `inspect` for state + labels; on the runtime's
/// exact "no such container" a podman name is looked up in `ps -a
/// --external` (docker has no storage-only state, so its "missing" is
/// final). Anything unreadable is `Unknown`, never `Missing`.
pub async fn read_identity<R: ContainerRunner>(runner: &R, name: &str) -> Identity {
    let out = match runner
        .run(&["inspect", "--type", "container", "--format", INSPECT_FORMAT, name], INSPECT_TIMEOUT)
        .await
    {
        Ok(o) => o,
        Err(e) => return Identity::bare(ContainerState::Unknown(e)),
    };
    if out.success {
        return parse_inspect_output(&out.stdout);
    }
    if !inspect_says_missing(&out.stderr) {
        return Identity::bare(ContainerState::Unknown(format!(
            "inspect {} failed: {}",
            name,
            out.stderr.trim()
        )));
    }
    if runner.runtime_name() != "podman" {
        return Identity::bare(ContainerState::Missing);
    }
    match runner.run(&["ps", "-a", "--external", "--format", "json"], EXTERNAL_TIMEOUT).await {
        Ok(o) if o.success => match external_lists_name(&o.stdout, name) {
            Some(true) => Identity::bare(ContainerState::StorageOnly),
            Some(false) => Identity::bare(ContainerState::Missing),
            None => Identity::bare(ContainerState::Unknown("unreadable `ps -a --external` output".into())),
        },
        Ok(o) => Identity::bare(ContainerState::Unknown(format!("ps -a --external failed: {}", o.stderr.trim()))),
        Err(e) => Identity::bare(ContainerState::Unknown(e)),
    }
}

// ─── ownership ───────────────────────────────────────────────────────────

/// May VCO act on this existing container?
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Ownership {
    Owned,
    Foreign { why: String },
    Unknown { why: String },
}

impl Ownership {
    /// The parity table's word.
    pub fn as_str(&self) -> &'static str {
        match self {
            Ownership::Owned => "owned",
            Ownership::Foreign { .. } => "foreign",
            Ownership::Unknown { .. } => "unknown",
        }
    }
}

/// The project a container must carry to be VCO's: the row's recorded
/// `compose_project`, else the installer's own.
pub fn expected_project<'a>(row: Option<&'a ServiceEndpointRow>, installer_project: Option<&'a str>) -> Option<&'a str> {
    row.and_then(|r| r.compose_project.as_deref())
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .or_else(|| installer_project.map(str::trim).filter(|p| !p.is_empty()))
}

/// AD-5: `Owned` iff the container's compose project label equals the row's
/// recorded `compose_project` (or, when the row records none, the
/// installer's project). A container without a compose label was not created
/// by compose — `Foreign`, as `vco_lib.containers.foreign_compose_identity`
/// says. `Unknown` when the container could not be read or the expected
/// project is not known.
pub fn ownership(row: Option<&ServiceEndpointRow>, identity: &Identity, installer_project: Option<&str>) -> Ownership {
    match &identity.state {
        ContainerState::Unknown(why) => return Ownership::Unknown { why: why.clone() },
        ContainerState::Missing | ContainerState::StorageOnly => {
            return Ownership::Unknown { why: "no container to read labels from".into() }
        }
        _ => {}
    }
    let Some(expected) = expected_project(row, installer_project) else {
        return Ownership::Unknown {
            why: "the installer's compose project is not known and the row records none".into(),
        };
    };
    match identity.project.as_deref() {
        None => Ownership::Foreign { why: "carries no compose project label — it was not created by compose".into() },
        Some(p) if p != expected => Ownership::Foreign {
            why: format!(
                "was created by compose project '{}'{}, not by project '{}'",
                p,
                match identity.provider {
                    Provider::None => String::new(),
                    other => format!(" ({}-compose)", other.as_str()),
                },
                expected
            ),
        },
        Some(_) => Ownership::Owned,
    }
}

// ─── acting on an owned container, and proving the result ────────────────

/// Bound for [`verify_running`] (AD-5: 30 s).
pub const VERIFY_TIMEOUT: Duration = Duration::from_secs(30);
const VERIFY_INTERVAL: Duration = Duration::from_millis(1500);
const ACTION_TIMEOUT: Duration = Duration::from_secs(120);

/// How long / how often [`verify_running`] polls (tests shrink it).
#[derive(Debug, Clone, Copy)]
pub struct VerifyPolicy {
    pub timeout: Duration,
    pub interval: Duration,
}

impl Default for VerifyPolicy {
    fn default() -> Self {
        VerifyPolicy { timeout: VERIFY_TIMEOUT, interval: VERIFY_INTERVAL }
    }
}

/// `Ok` only once the runtime reports `name` running; `Err` names the last
/// state seen when the bound elapses. "Issued" is never success.
pub async fn verify_running<R: ContainerRunner>(runner: &R, name: &str, policy: VerifyPolicy) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + policy.timeout;
    loop {
        let id = read_identity(runner, name).await;
        if id.state == ContainerState::Running {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!(
                "container '{}' is not running {} s after the action (last seen: {:?})",
                name,
                policy.timeout.as_secs(),
                id.state
            ));
        }
        tokio::time::sleep(policy.interval).await;
    }
}

/// The by-name verb for an existing container: `unpause` a paused one,
/// `start` a stopped one; `None` for any other state.
pub fn by_name_verb(state: &ContainerState) -> Option<&'static str> {
    match state {
        ContainerState::Paused => Some("unpause"),
        ContainerState::Stopped(_) => Some("start"),
        _ => None,
    }
}

/// Run `<runtime> <verb> <name>`; `Err` carries the runtime's stderr.
pub async fn act_by_name<R: ContainerRunner>(runner: &R, verb: &str, name: &str) -> Result<(), String> {
    let out = runner.run(&[verb, name], ACTION_TIMEOUT).await?;
    if out.success {
        Ok(())
    } else {
        Err(format!("{} {} {} failed: {}", runner.runtime_name(), verb, name, out.stderr.trim()))
    }
}

/// Stop `name` with a bounded grace period (`stop --time <secs>`); the
/// command itself is bounded too, so a hung stop cannot hang the caller.
pub async fn stop_by_name<R: ContainerRunner>(runner: &R, name: &str, grace_secs: u32) -> Result<(), String> {
    let grace = grace_secs.to_string();
    let out = runner
        .run(&["stop", "--time", &grace, name], Duration::from_secs(u64::from(grace_secs) + 30))
        .await?;
    if out.success {
        Ok(())
    } else {
        Err(format!("{} stop {} failed: {}", runner.runtime_name(), name, out.stderr.trim()))
    }
}

// ─── the launcher's own creation label (module containers, L2-F17) ───────

/// The install-root id: the first 16 hex chars of sha256 over the root's
/// canonical path (Windows verbatim prefix stripped). Stable per install;
/// two installs on one machine differ.
pub fn owner_id_for_root(root: &Path) -> String {
    use sha2::{Digest, Sha256};
    let canonical = dunce::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let normalized = super::install_root::strip_windows_verbatim_prefix(&canonical.to_string_lossy());
    let mut h = Sha256::new();
    h.update(normalized.as_bytes());
    hex::encode(h.finalize())[..16].to_string()
}

/// THIS process's install-root id, or why it has none (resolved once).
fn launcher_owner_resolution() -> &'static Result<String, String> {
    static ID: OnceLock<Result<String, String>> = OnceLock::new();
    ID.get_or_init(|| {
        super::install_root::resolve_current_exe_without_db()
            .map(|r| owner_id_for_root(&r.path))
            .map_err(|e| e.to_string())
    })
}

/// THIS process's install-root id (`None` when no install root resolves —
/// then nothing is labelled and nothing is reaped).
pub fn launcher_owner_id() -> Option<String> {
    launcher_owner_resolution().as_ref().ok().cloned()
}

/// Why THIS launcher creates module containers WITHOUT its ownership label
/// (W4R-09): `Some(reason)` when its install root did not resolve from its
/// own executable — a bootstrap binary run from outside an install, a test
/// binary — else `None`.
pub fn launcher_label_omitted_cause() -> Option<String> {
    label_omitted_cause_for(launcher_owner_resolution())
}

/// [`launcher_label_omitted_cause`] for a given resolution. Pure.
pub fn label_omitted_cause_for(resolution: &Result<String, String>) -> Option<String> {
    resolution.as_ref().err().map(|why| {
        format!("this launcher's install root did not resolve from its executable ({why})")
    })
}

/// `--label io.vibecoded.vct.launcher=<id>` for a `run` argv, given the id.
pub fn launcher_label_args_for(owner_id: Option<&str>) -> Vec<String> {
    match owner_id {
        Some(id) if !id.is_empty() => vec!["--label".into(), format!("{}={}", LAUNCHER_LABEL, id)],
        _ => Vec::new(),
    }
}

/// [`launcher_label_args_for`] this process's id — called at every module
/// container CREATE. When the id is unknown the container is created
/// unlabelled; that is logged ONCE per process with the cause (W4R-09), so a
/// later `module_container_unlabelled` row has its explanation in the log of
/// the launcher that created the container.
pub fn launcher_label_args() -> Vec<String> {
    if let Some(cause) = launcher_label_omitted_cause() {
        if first_time("launcher-label-omitted") {
            tracing::warn!(
                "[container_ownership] module containers are created WITHOUT the {} label: {}. \
                 A launcher whose install root resolves will report them as unlabelled and \
                 never remove them.",
                LAUNCHER_LABEL,
                cause
            );
        }
    }
    launcher_label_args_for(launcher_owner_id().as_deref())
}

/// Whose creation label a container carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LabelVerdict {
    /// Created by THIS install's launcher.
    Ours,
    /// No launcher label (pre-0.2.100, or not ours at all).
    Unlabelled,
    /// Created by another install's launcher.
    OtherInstall(String),
}

/// Classify `labels` against `owner_id`. With no `owner_id` nothing is
/// "ours" (a label is proof only when both sides are known).
pub fn launcher_label_verdict(labels: &Labels, owner_id: Option<&str>) -> LabelVerdict {
    match (label(labels, LAUNCHER_LABEL), owner_id) {
        (None, _) => LabelVerdict::Unlabelled,
        (Some(v), Some(id)) if v == id => LabelVerdict::Ours,
        (Some(v), _) => LabelVerdict::OtherInstall(v.to_string()),
    }
}

/// `true` the first time `key` is seen in this process — for "logged once"
/// lines that would otherwise repeat every pass/tick.
pub fn first_time(key: &str) -> bool {
    static SEEN: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    let mut g = SEEN.get_or_init(|| Mutex::new(HashSet::new())).lock().unwrap_or_else(|p| p.into_inner());
    g.insert(key.to_string())
}

// ─── the guarded compose verb (the one Rust caller) ──────────────────────

/// Bound on one `service_lifecycle up` run (its own compose call is bounded
/// at 900 s; the guard's reads come first).
const GUARDED_UP_TIMEOUT: Duration = Duration::from_secs(960);

/// What `python -m vco_lib.service_lifecycle up --shell` reported.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct UpReply {
    /// 0 ok, 1 compose failed, 3 some refused (the verb's contract).
    pub code: Option<i32>,
    pub cleared: Vec<String>,
    pub refused: Vec<String>,
    pub removed: Vec<String>,
    /// The verb's human lines (without the `vco_up_*` assignments).
    pub output: String,
}

/// Parse the verb's `--shell` output (`vco_up_cleared='a b'` …).
pub fn parse_up_reply(stdout: &str, code: Option<i32>) -> UpReply {
    let mut reply = UpReply { code, ..UpReply::default() };
    let mut human = Vec::new();
    for line in stdout.lines() {
        let Some(rest) = line.strip_prefix("vco_up_") else {
            if !line.trim().is_empty() {
                human.push(line);
            }
            continue;
        };
        let Some((key, value)) = rest.split_once('=') else { continue };
        let words: Vec<String> = shlex::split(value)
            .unwrap_or_default()
            .join(" ")
            .split_whitespace()
            .map(str::to_string)
            .collect();
        match key {
            "cleared" => reply.cleared = words,
            "refused" => reply.refused = words,
            "removed" => reply.removed = words,
            _ => {}
        }
    }
    reply.output = human.join("\n");
    reply
}

/// One guarded-verb request.
#[derive(Debug, Clone)]
pub struct UpRequest<'a> {
    pub services: &'a [&'a str],
    /// Zombies removed ONLY after their guard passed.
    pub recreate: &'a [&'a str],
    /// Guard (and remove cleared zombies) only; the caller composes.
    pub guard_only: bool,
    /// Rebuild images (`--build`, code_embed).
    pub build: bool,
    pub compose_dir: &'a Path,
    /// `"podman"` / `"docker"`; `None` lets the verb resolve it.
    pub runtime: Option<&'a str>,
}

/// The argv after the interpreter (pure, pinned by a test).
pub fn guarded_up_args(req: &UpRequest<'_>) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "-m".into(),
        "vco_lib.service_lifecycle".into(),
        "up".into(),
        "--shell".into(),
        "--services".into(),
        req.services.join(" "),
        "--compose-dir".into(),
        req.compose_dir.to_string_lossy().into_owned(),
    ];
    if !req.recreate.is_empty() {
        args.push("--recreate".into());
        args.push(req.recreate.join(" "));
    }
    if req.guard_only {
        args.push("--guard-only".into());
    }
    if req.build {
        args.push("--build".into());
    }
    if let Some(rt) = req.runtime {
        args.push("--runtime".into());
        args.push(rt.to_string());
    }
    args
}

/// Run the guarded compose verb for `req` from the install `root`. `Err`
/// only when the verb could not run at all; its verdict is in the reply.
pub async fn guarded_up(python: &Path, root: &Path, req: &UpRequest<'_>) -> Result<UpReply, String> {
    if req.services.is_empty() {
        return Ok(UpReply { code: Some(0), ..UpReply::default() });
    }
    let mut cmd = tokio::process::Command::new(python).silent();
    cmd.args(guarded_up_args(req))
        .current_dir(root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    let out = match tokio::time::timeout(GUARDED_UP_TIMEOUT, cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("cannot run {} for service_lifecycle up: {}", python.display(), e)),
        Err(_) => {
            return Err(format!(
                "vco_lib.service_lifecycle up did not finish within {} s",
                GUARDED_UP_TIMEOUT.as_secs()
            ))
        }
    };
    let mut reply = parse_up_reply(&String::from_utf8_lossy(&out.stdout), out.status.code());
    let stderr = String::from_utf8_lossy(&out.stderr);
    if !stderr.trim().is_empty() {
        reply.output = format!("{}\n{}", reply.output, stderr.trim()).trim().to_string();
    }
    Ok(reply)
}

// ─── a scripted runtime (test support, used by the hub's tests too) ─────

/// A scripted [`ContainerRunner`] for tests in this crate AND in its
/// dependents (the hub's watchdog tests) — which is why it is not
/// `#[cfg(test)]` (that gate does not apply across crates; the same reason
/// `crate::test_env` is public). Nothing in a release path constructs it.
pub mod fake {
    use super::{CmdOutput, ContainerRunner};
    use std::cell::RefCell;
    use std::collections::{HashMap, VecDeque};
    use std::time::Duration;

    /// A scripted runtime: each call pops the next reply for its verb
    /// (`args[0]`); every call is recorded.
    pub struct FakeRunner {
        pub runtime: String,
        pub replies: RefCell<HashMap<String, VecDeque<Result<CmdOutput, String>>>>,
        pub calls: RefCell<Vec<Vec<String>>>,
    }

    impl FakeRunner {
        pub fn new(runtime: &str) -> Self {
            FakeRunner { runtime: runtime.into(), replies: RefCell::default(), calls: RefCell::default() }
        }
        pub fn on(&self, verb: &str, reply: Result<CmdOutput, String>) -> &Self {
            self.replies.borrow_mut().entry(verb.into()).or_default().push_back(reply);
            self
        }
        pub fn verbs(&self) -> Vec<String> {
            self.calls.borrow().iter().map(|c| c[0].clone()).collect()
        }
    }

    impl ContainerRunner for FakeRunner {
        fn runtime_name(&self) -> &str {
            &self.runtime
        }
        async fn run(&self, args: &[&str], _t: Duration) -> Result<CmdOutput, String> {
            self.calls.borrow_mut().push(args.iter().map(|s| s.to_string()).collect());
            let mut r = self.replies.borrow_mut();
            let q = r.get_mut(args[0]).unwrap_or_else(|| panic!("unscripted runtime call: {:?}", args));
            // The last scripted reply repeats (a poll keeps seeing it).
            if q.len() > 1 {
                q.pop_front().unwrap()
            } else {
                q.front().cloned().unwrap_or_else(|| panic!("no reply left for {:?}", args))
            }
        }
    }

    pub fn ok(stdout: &str) -> Result<CmdOutput, String> {
        Ok(CmdOutput { success: true, stdout: stdout.into(), stderr: String::new() })
    }
    pub fn fail(stderr: &str) -> Result<CmdOutput, String> {
        Ok(CmdOutput { success: false, stdout: String::new(), stderr: stderr.into() })
    }
    pub fn inspect_line(status: &str, labels: &str) -> String {
        format!("\"{}\"\t{}\n", status, labels)
    }
}

// ─── tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::fake::*;
    use super::*;
    use crate::db::service_endpoints::EndpointMode;

    fn fixture() -> serde_json::Value {
        let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/container_ownership_parity.json");
        serde_json::from_str(&std::fs::read_to_string(&p).expect("parity fixture")).expect("parity fixture parses")
    }

    fn labels_of(v: &serde_json::Value) -> Labels {
        labels_from_json(v)
    }

    fn row_of(v: &serde_json::Value, service: &str) -> Option<ServiceEndpointRow> {
        if v.is_null() {
            return None;
        }
        let mode = match v["mode"].as_str().unwrap() {
            "vco_managed" => EndpointMode::VcoManaged,
            "adopted_container" => EndpointMode::AdoptedContainer,
            _ => EndpointMode::AdoptedExternal,
        };
        let mut r = ServiceEndpointRow::new(service, mode, "localhost", 1);
        r.compose_project = v.get("compose_project").and_then(|p| p.as_str()).map(str::to_string);
        r.container_name = v.get("container_name").and_then(|p| p.as_str()).map(str::to_string);
        if let Some(e) = v.get("enabled").and_then(|e| e.as_bool()) {
            r.enabled = e;
        }
        if let Some(a) = v.get("autostart").and_then(|a| a.as_bool()) {
            r.autostart = a;
        }
        Some(r)
    }

    #[test]
    fn label_constants_match_the_parity_table() {
        let t = fixture();
        assert_eq!(t["labels"]["compose_project"], COMPOSE_PROJECT_LABEL);
        assert_eq!(t["labels"]["podman_compose_project"], PODMAN_COMPOSE_PROJECT_LABEL);
        assert_eq!(t["labels"]["docker_config_hash"], DOCKER_COMPOSE_CONFIG_HASH_LABEL);
        assert_eq!(t["labels"]["launcher"], LAUNCHER_LABEL);
    }

    #[test]
    fn identity_cases_match_the_parity_table() {
        for case in fixture()["identity_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let id = Identity::with_labels(ContainerState::Stopped("exited".into()), labels_of(&case["labels"]));
            assert_eq!(id.provider.as_str(), case["expect_provider"].as_str().unwrap(), "{name}");
            assert_eq!(id.project.as_deref(), case["expect_project"].as_str(), "{name}");
        }
    }

    #[test]
    fn project_name_cases_match_the_parity_table() {
        for case in fixture()["project_name_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let got = compose_project_name(Path::new(case["dir"].as_str().unwrap()), case["compose_text"].as_str().unwrap());
            assert_eq!(got.unwrap_or_default(), case["expect"].as_str().unwrap(), "{name}");
        }
    }

    /// The ownership rule AND the zombie verb given ownership
    /// (`expect_on_foreign_project`), executed from the shared table.
    #[test]
    fn ownership_cases_match_the_parity_table() {
        for case in fixture()["ownership_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let svc = case["service"].as_str().unwrap();
            let row = row_of(&case["row"], svc);
            let id = Identity::with_labels(ContainerState::Stopped("exited".into()), labels_of(&case["labels"]));
            let own = ownership(row.as_ref(), &id, case["installer_project"].as_str());
            assert_eq!(own.as_str(), case["expect_ownership"].as_str().unwrap(), "{name}: {own:?}");
            let zombie = super::super::service_endpoints::zombie_action_given(row.as_ref(), &own);
            assert_eq!(zombie.as_str(), case["expect_on_zombie"].as_str().unwrap(), "{name} zombie");
        }
    }

    #[test]
    fn unreadable_or_absent_containers_are_never_owned() {
        let row = ServiceEndpointRow::new("ollama", EndpointMode::VcoManaged, "localhost", 1);
        for state in [ContainerState::Unknown("daemon down".into()), ContainerState::Missing, ContainerState::StorageOnly] {
            let own = ownership(Some(&row), &Identity::bare(state.clone()), Some("infrastructure"));
            assert_eq!(own.as_str(), "unknown", "{state:?}");
        }
        // No expected project at all → Unknown, not Owned.
        let id = Identity::with_labels(ContainerState::Stopped("exited".into()), labels_from_json(&serde_json::json!({COMPOSE_PROJECT_LABEL: "infrastructure"})));
        assert_eq!(ownership(None, &id, None).as_str(), "unknown");
    }

    #[test]
    fn labels_parse_from_every_runtime_shape() {
        let obj = labels_from_json(&serde_json::json!({"a": "1", "b": "two"}));
        assert_eq!(obj.get("b").map(String::as_str), Some("two"));
        let s = labels_from_json(&serde_json::json!("com.docker.compose.project=infrastructure,x=y"));
        assert_eq!(compose_project_of(&s).as_deref(), Some("infrastructure"));
        assert!(labels_from_json(&serde_json::Value::Null).is_empty());
    }

    #[test]
    fn missing_is_only_the_runtimes_exact_phrasing() {
        assert!(inspect_says_missing("Error: no such container vco_ollama"));
        assert!(inspect_says_missing("Error response from daemon: No such object: vco_ollama"));
        assert!(!inspect_says_missing("crun: executable file `x` not found in $PATH"));
        assert!(!inspect_says_missing("Error: image not found"));
        assert!(!inspect_says_missing("Cannot connect to the Docker daemon"));
    }

    #[tokio::test]
    async fn read_identity_distinguishes_every_state() {
        let labels = r#"{"com.docker.compose.project":"infrastructure","io.podman.compose.project":"infrastructure"}"#;
        let r = FakeRunner::new("podman");
        r.on("inspect", ok(&inspect_line("paused", labels)));
        let id = read_identity(&r, "vco_ollama").await;
        assert_eq!(id.state, ContainerState::Paused);
        assert_eq!(id.project.as_deref(), Some("infrastructure"));
        assert_eq!(id.provider, Provider::Podman);

        // podman: inspect says missing, --external still lists it → StorageOnly.
        let r = FakeRunner::new("podman");
        r.on("inspect", fail("Error: no such container vco_code_embed"));
        r.on("ps", ok(r#"[{"Names":["vco_code_embed"],"State":"storage"}]"#));
        assert_eq!(read_identity(&r, "vco_code_embed").await.state, ContainerState::StorageOnly);

        // podman: nowhere → Missing.
        let r = FakeRunner::new("podman");
        r.on("inspect", fail("Error: no such container vco_code_embed"));
        r.on("ps", ok("[]"));
        assert_eq!(read_identity(&r, "vco_code_embed").await.state, ContainerState::Missing);

        // podman: --external could not be read → Unknown, never Missing.
        let r = FakeRunner::new("podman");
        r.on("inspect", fail("Error: no such container vco_code_embed"));
        r.on("ps", fail("Error: database is locked"));
        assert!(matches!(read_identity(&r, "vco_code_embed").await.state, ContainerState::Unknown(_)));

        // docker: missing is final (no --external call).
        let r = FakeRunner::new("docker");
        r.on("inspect", fail("Error: No such container: vco_ollama"));
        assert_eq!(read_identity(&r, "vco_ollama").await.state, ContainerState::Missing);
        assert_eq!(r.verbs(), vec!["inspect"]);

        // Any other failure → Unknown.
        let r = FakeRunner::new("docker");
        r.on("inspect", fail("permission denied while trying to connect to the Docker daemon socket"));
        assert!(matches!(read_identity(&r, "vco_ollama").await.state, ContainerState::Unknown(_)));
        let r = FakeRunner::new("docker");
        r.on("inspect", Err("timed out".into()));
        assert!(matches!(read_identity(&r, "vco_ollama").await.state, ContainerState::Unknown(_)));
    }

    #[tokio::test]
    async fn verify_running_needs_the_runtime_to_say_running() {
        let quick = VerifyPolicy { timeout: Duration::from_millis(30), interval: Duration::from_millis(5) };
        let r = FakeRunner::new("docker");
        r.on("inspect", ok(&inspect_line("created", "null")));
        r.on("inspect", ok(&inspect_line("running", "null")));
        assert!(verify_running(&r, "c", quick).await.is_ok());
        // Gone after the action: never success.
        let r = FakeRunner::new("docker");
        r.on("inspect", fail("Error: No such container: c"));
        let err = verify_running(&r, "c", quick).await.unwrap_err();
        assert!(err.contains("not running"), "{err}");
    }

    #[test]
    fn by_name_verbs_follow_the_state() {
        assert_eq!(by_name_verb(&ContainerState::Paused), Some("unpause"));
        assert_eq!(by_name_verb(&ContainerState::Stopped("exited".into())), Some("start"));
        assert_eq!(by_name_verb(&ContainerState::Running), None);
        assert_eq!(by_name_verb(&ContainerState::Missing), None);
        assert_eq!(by_name_verb(&ContainerState::StorageOnly), None);
    }

    #[test]
    fn launcher_label_is_ours_only_with_both_ids_equal() {
        let ours = labels_from_json(&serde_json::json!({LAUNCHER_LABEL: "abc"}));
        assert_eq!(launcher_label_verdict(&ours, Some("abc")), LabelVerdict::Ours);
        assert_eq!(launcher_label_verdict(&ours, Some("def")), LabelVerdict::OtherInstall("abc".into()));
        assert_eq!(launcher_label_verdict(&ours, None), LabelVerdict::OtherInstall("abc".into()));
        assert_eq!(launcher_label_verdict(&Labels::new(), Some("abc")), LabelVerdict::Unlabelled);
        // W4R-09: an unresolved root is a NAMED cause, a resolved one none.
        let cause = label_omitted_cause_for(&Err("no vct-module.json above /tmp/x".into())).expect("cause");
        assert!(cause.contains("did not resolve") && cause.contains("/tmp/x"), "{cause}");
        assert_eq!(label_omitted_cause_for(&Ok("0123456789abcdef".into())), None);
        assert_eq!(launcher_label_args_for(Some("abc")), vec!["--label", "io.vibecoded.vct.launcher=abc"]);
        assert!(launcher_label_args_for(None).is_empty());
    }

    #[test]
    fn owner_ids_differ_per_install_and_are_stable() {
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        assert_eq!(owner_id_for_root(a.path()), owner_id_for_root(a.path()));
        assert_ne!(owner_id_for_root(a.path()), owner_id_for_root(b.path()));
        assert_eq!(owner_id_for_root(a.path()).len(), 16);
    }

    #[test]
    fn guarded_up_argv_and_reply() {
        let dir = Path::new("/opt/vco/infrastructure");
        let args = guarded_up_args(&UpRequest {
            services: &["code_embed"],
            recreate: &[],
            guard_only: false,
            build: false,
            compose_dir: dir,
            runtime: Some("podman"),
        });
        assert_eq!(
            args,
            vec![
                "-m", "vco_lib.service_lifecycle", "up", "--shell", "--services", "code_embed",
                "--compose-dir", "/opt/vco/infrastructure", "--runtime", "podman"
            ]
        );
        let reply = parse_up_reply(
            "  [code_embed] recreate refused — x\nvco_up_cleared=''\nvco_up_refused=code_embed\nvco_up_removed=''\n",
            Some(3),
        );
        assert_eq!(reply.refused, vec!["code_embed"]);
        assert!(reply.cleared.is_empty());
        assert!(reply.output.contains("recreate refused"));
        let reply = parse_up_reply("vco_up_cleared='ollama weaviate'\n", Some(0));
        assert_eq!(reply.cleared, vec!["ollama", "weaviate"]);
    }
}
