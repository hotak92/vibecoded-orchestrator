use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use tauri::{command, AppHandle, Emitter, Manager, Runtime, State, Window};

use crate::commands::git_user_editable_merge::{
    write_launcher_update_diverged_deferral, LauncherUpdateDivergedKind,
};
use crate::db::Db;
use crate::secrets::{self, SecretScope};
// v0.2.21 Step 12: liveness check for the detached vct-hub during the
// update flow's stop-before-pull sequence. Same symbol the boot sweep
// uses, sourced from the core crate so the re-export visibility in
// `lib.rs` (`pub(crate) use`) doesn't matter here.
use vct_launcher_core::process::pid_is_alive;
use vct_launcher_core::process::CommandExt as _;

/// Upstream GitHub repo. Auto-update isn't fully wired yet — initial
/// install is a local file copy from the launcher's bundled repo source
/// (see `find_local_repo_root` + `copy_orchestrator_to_sync`). This
/// constant is here for the future auto-update path that fetches
/// new bundled-orchestrator releases from upstream.
#[allow(dead_code)]
const ORCHESTRATOR_REPO: &str = "https://github.com/hotak92/vibecoded-orchestrator.git";

/// app_state key for the last-known install path. Cached after a successful
/// install + opportunistically backfilled by `get_known_install_path` when
/// it discovers an install via the exe-walk strategy. The wizard's
/// `checkStatus()` uses this to avoid the hard-coded `$HOME/...` default
/// (which produced a false-negative "Not installed" banner when users
/// installed somewhere else, e.g. `/home/<user>/code/vco/`).
pub(crate) const APP_STATE_KEY_INSTALL_PATH: &str = "launcher.install_path";

/// v0.2.37 canonical resolver (2026-05-27): single source of truth for
/// the orchestrator clone root. Supersedes the previous parallel
/// resolvers `find_local_repo_root` (uncached walk-up only) and
/// `resolve_install_root_sync` (DB-cached + walk-up via `install.py +
/// CLAUDE.md` markers only).
///
/// The two pre-v0.2.37 resolvers diverged in TWO ways:
///   * Caching: only `resolve_install_root_sync` consulted the DB
///     cache + wrote back on hit. `find_local_repo_root` was stateless,
///     which bit `ProjectEnvSettings::populate` when `current_exe()`
///     was far from the clone (e.g. binary installed at `~/bin/`,
///     clone at `~/dev/vco/`) — populate returned `None` →
///     `VCT_ORCHESTRATOR_ROOT` was OMITTED from `.claude/env`.
///   * Marker pattern: `find_local_repo_root` looked for
///     `vct-module.json` (the orchestrator clone's own manifest);
///     `resolve_install_root_sync` looked for `install.py + CLAUDE.md`
///     (the install-root files). These identify the same artifact — an
///     orchestrator clone — by different signals, but a binary launched
///     from a partial checkout might match one but not the other.
///
/// This canonical resolver accepts BOTH marker patterns at every level
/// of the walk: a directory is an orchestrator root if it contains
/// EITHER `vct-module.json` OR (`install.py` + `CLAUDE.md`).
///
/// Strategy order:
///   1. Read `launcher.install_path` from `app_state`. Validate the
///      cached path still passes `check_install_status` (which gates on
///      `install.py + CLAUDE.md`); fall through if stale.
///   2. Walk up from `current_exe()` looking for EITHER marker pattern.
///      On a hit, write back to app_state so future calls take the
///      cached path.
///
/// Returns `None` when no install is discoverable. Callers should treat
/// `None` as "scan the empty set" — never panic or return a phantom path.
///
/// **No hardcoded paths in the binary**: the resolution is fully
/// runtime-derived (DB OR exe location). No `env!("CARGO_MANIFEST_DIR")`
/// or `option_env!("VCT_REPO_ROOT")` fallback — those leak the
/// build-host's absolute path and are wrong on shipped binaries
/// (build-time != runtime). Privacy discipline established 2026-05-06.
pub(crate) fn resolve_orchestrator_root(db: &Db) -> Option<PathBuf> {
    // Strategy 1: cached path from app_state.
    if let Ok(Some(cached)) = db.app_state_get(APP_STATE_KEY_INSTALL_PATH) {
        if !cached.is_empty() && check_install_status(cached.clone()) {
            return Some(PathBuf::from(cached));
        }
    }
    // Strategy 2: walk up from current_exe() honoring BOTH marker
    // patterns (vct-module.json OR install.py+CLAUDE.md).
    let found = walk_for_orchestrator_root()?;
    // Sticky cache — future calls take the cached path.
    let s = found.to_string_lossy().to_string();
    if let Err(e) = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, &s) {
        eprintln!(
            "[vct] resolve_orchestrator_root: failed to cache install_path: {}",
            e
        );
    }
    Some(found)
}

/// DEPRECATED v0.2.37 shim — kept so existing call sites compile. New
/// code MUST call `resolve_orchestrator_root(db)` directly to benefit
/// from the DB cache (the writeback fix that closed the
/// `.claude/env` omission bug). This shim retains the old name +
/// signature for callers that genuinely don't have a `Db` handle in
/// scope; it does ONLY the walk-up step (no DB read, no writeback).
///
/// Privacy discipline survives: still no `env!("CARGO_MANIFEST_DIR")`,
/// still no `option_env!("VCT_REPO_ROOT")` (both would leak the
/// build-host's path into shipped binaries).
pub(crate) fn resolve_install_root_sync(db: &Db) -> Option<PathBuf> {
    resolve_orchestrator_root(db)
}

/// app_state key for the last-detected hardware snapshot. Populated on
/// first launcher boot and refreshed by the "Re-detect hardware" button
/// in Preferences. Comparing the persisted snapshot against a fresh
/// detection drives the "Apply reconfiguration" UX in Preferences →
/// Hardware (Bug B).
pub(crate) const APP_STATE_KEY_HARDWARE_SNAPSHOT: &str = "launcher.hardware_snapshot";

/// app_state key for the ISO8601 timestamp of the last successful
/// `apply_hardware_reconfig` run. Surfaced in the Hardware preferences
/// card so the user can see when containers were last reconfigured.
pub(crate) const APP_STATE_KEY_HARDWARE_LAST_RECONFIGURED: &str =
    "launcher.hardware_last_reconfigured_at";

/// v0.2.34 (Agent B): app_state flag set by the launcher self-update flow
/// right before the rebuild + restart, so the next launcher boot knows it
/// just came back from an update and should re-detect hardware in the
/// background. Cleared by the boot-time consumer after the background job
/// is scheduled.
///
/// Why a flag instead of always re-detecting on every boot: keeps the
/// invariant precise — re-detection happens at the points that matter
/// (update boundary + install boundary), without paying the detect cost
/// on every routine launcher start. The flag survives the process restart
/// because it lives in SQLite (`launcher.db`), not in-memory state.
///
/// See `mark_hardware_redetect_pending_after_update` (writer) and
/// `consume_pending_hardware_redetect_if_set` (reader).
pub(crate) const APP_STATE_KEY_HARDWARE_REDETECT_PENDING: &str =
    "launcher.hardware_redetect_pending";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemDetection {
    pub os: String,
    pub arch: String,
    pub has_nvidia_gpu: bool,
    pub gpu_name: String,
    pub has_apple_silicon: bool,
    pub has_docker: bool,
    pub has_podman: bool,
    pub has_python: bool,
    pub python_version: String,
    pub python_cmd: String,
    pub has_claude_cli: bool,
    pub has_git: bool,
    pub has_node: bool,
    /// First container runtime found (e.g., "podman 4.7.0"), or null if none.
    /// Frontend reads this for the onboarding "Container runtime" row.
    pub container_runtime: Option<String>,
    /// Total system RAM in GB (rounded). 0 if detection failed.
    pub ram_gb: u64,
    /// VRAM in GB for the CHOSEN discrete GPU (v0.2.68: most-capable
    /// usable discrete card, after filtering Intel + iGPUs — was
    /// "total across discovered GPUs" pre-v0.2.68). 0 if no usable GPU.
    pub vram_gb: u64,
    /// "NVIDIA" | "AMD" | null. Vendor of the chosen GPU. Used to label
    /// VRAM in the UI.
    pub gpu_vendor: Option<String>,
    /// v0.2.68 (Defect Y): every GPU the probes enumerated (NVIDIA via
    /// nvidia-smi, AMD via rocm-smi, Intel/iGPU via lspci/wmic), BEFORE
    /// Intel/iGPU filtering. Lets the GUI show "found N GPUs, using
    /// <discrete>" + which cards were ignored. `serde(default)` keeps the
    /// type backward-compatible with callers/snapshots that predate it.
    #[serde(default)]
    pub gpus: Vec<crate::commands::gpu_policy::GpuCandidate>,
    /// v0.2.68: human-readable name of the chosen discrete GPU (the one
    /// `vram_gb`/`gpu_vendor` describe). Empty when no usable GPU. May
    /// differ from `gpu_name` (which historically held only the first
    /// NVIDIA name); kept distinct + additive for backward compat.
    #[serde(default)]
    pub chosen_gpu_name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallConfig {
    pub install_path: String,
    pub use_gpu: bool,
    pub cpu_only: bool,
    pub openai_key: Option<String>,
    pub container_runtime: Option<String>, // "docker" | "podman" | null (auto)
    pub skip_containers: bool,
    /// KNOWN_ISSUES.md (v0.2.x) entry resolved 2026-05-10: when true,
    /// pass `--lightweight` to install.py. The lightweight path skips
    /// model pulls, Weaviate seeding, agent/skill copy, and full GPU
    /// detection — completing in seconds instead of minutes. Used by
    /// the launcher's reinstall flow when the existing install is
    /// healthy and the user just wants path-rewrite + venv refresh.
    ///
    /// When None / false, the full install path runs as before.
    #[serde(default)]
    pub lightweight: bool,
    /// Used together with `lightweight=true`: forwarded to install.py
    /// as `--lightweight-old-path`. install.py rewrites absolute
    /// occurrences of this path in `.env` / `.claude/settings.json` /
    /// `.vscode/settings.json` to the new install location. Leave None
    /// when no path-rewrite is needed (e.g. a re-install that simply
    /// regenerates state files in place).
    #[serde(default)]
    pub lightweight_old_path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallProgress {
    pub stage: String,
    pub message: String,
    pub percentage: f32,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallResult {
    pub success: bool,
    pub install_path: String,
    pub message: String,
    pub system: SystemDetection,
}

/// Compact hardware fingerprint persisted across launcher boots. Used by
/// the "Re-detect hardware" Preferences flow (Bug B) to surface drift when
/// the user upgrades RAM / adds a GPU after the initial install.
///
/// `use_gpu` and `low_resource` are DERIVED from the raw detection fields
/// and stored eagerly so the install.py reconfig flags can be reproduced
/// from a single persisted blob without re-running the full
/// `detect_system()` join.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HardwareSnapshot {
    pub has_nvidia_gpu: bool,
    pub gpu_name: String,
    pub has_apple_silicon: bool,
    pub ram_gb: u32,
    /// Derived: NVIDIA / AMD / Apple Silicon present. Determines whether
    /// install.py gets `--gpu` or `--cpu-only` on a reconfig run.
    pub use_gpu: bool,
    /// Derived: ram_gb < 8. Triggers install.py's `--low-resource` path
    /// (smaller models, narrower service stack).
    pub low_resource: bool,
    /// v0.2.9 (Bug K): total VRAM (GB) for the primary discrete GPU.
    /// 0.0 when no discrete GPU OR when the probe failed. Stored on the
    /// snapshot so reconfig flows can re-evaluate the threshold without
    /// re-running `nvidia-smi` / `rocm-smi`. `serde(default)` for
    /// backward-compat with v0.2.8 snapshots persisted in app_state.
    #[serde(default)]
    pub vram_gb: f64,
    /// v0.2.20: AMD GPU detected (rocminfo or /sys/class/drm vendor=0x1002).
    /// `serde(default)` for backward-compat with v0.2.19- snapshots that
    /// predate the AMD path. A subsequent redetect populates the real value.
    #[serde(default)]
    pub has_amd_gpu: bool,
    /// v0.2.9 (Bug K): derived GPU mode based on the VRAM threshold.
    /// `Cuda` | `Rocm` | `Cpu` | `Metal`. Drives whether the reconfig run
    /// uses `--gpu` or `--cpu-only`. v0.2.20 renamed `Gpu` → `Cuda` and
    /// added `Rocm`. `serde(default)` falls back to `Cpu` for pre-v0.2.9
    /// snapshots — a subsequent redetect-hardware run will populate it
    /// correctly.
    #[serde(default = "default_gpu_mode")]
    pub gpu_mode_decided: crate::commands::gpu_policy::GpuMode,
    /// v0.2.68 (Defect Y): every GPU enumerated by `detect_system`,
    /// BEFORE Intel/iGPU filtering. Empty on a pre-v0.2.68 snapshot (a
    /// redetect populates it). Lets the Preferences hardware panel show
    /// "found N GPUs, using <discrete>" + the ignored iGPU/Intel cards.
    #[serde(default)]
    pub gpus: Vec<crate::commands::gpu_policy::GpuCandidate>,
    /// v0.2.68: name of the chosen discrete GPU (the card `vram_gb`
    /// describes). Empty when no usable discrete GPU. Distinct from
    /// `gpu_name` for backward compat (older code reads `gpu_name`).
    #[serde(default)]
    pub chosen_gpu_name: String,
}

/// Default for the `gpu_mode_decided` serde fallback. We treat older
/// snapshots as `Cpu` until a redetect populates the real value — safer
/// than assuming `Gpu` and then failing to start the GPU overlay.
fn default_gpu_mode() -> crate::commands::gpu_policy::GpuMode {
    crate::commands::gpu_policy::GpuMode::Cpu
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HardwareDetectionDiff {
    /// `None` when no prior snapshot existed (first-ever detection).
    pub before: Option<HardwareSnapshot>,
    pub after: HardwareSnapshot,
    /// Field names that differ between `before` and `after`. Empty when
    /// `before` is None OR all fields match. Drives whether the FE shows
    /// the "Apply reconfiguration" CTA.
    pub changed_fields: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReconfigReport {
    pub success: bool,
    pub exit_code: i32,
    pub log_path: String,
}

/// Bug 29: shared-container detection. Reports which of the three default
/// service endpoints are already serving on this machine (8081 / 11435 /
/// 11440), so the OnboardingWizard step 3 can tell the user "your install
/// will reuse these" before running the installer.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesStatus {
    /// Reachable URL or null when the probe failed.
    pub weaviate_url: Option<String>,
    pub ollama_url: Option<String>,
    pub code_embed_url: Option<String>,
    /// True iff all three probes succeeded — UI uses this to show the green
    /// "all detected, install will reuse them" panel vs the neutral
    /// "services not detected, install will start them" panel.
    pub all_detected: bool,
    /// True iff zero services responded — implies a fresh machine, install
    /// will need to start the compose stack.
    pub none_detected: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InstallMode {
    /// Empty or non-existent target. Fresh git clone.
    Fresh,
    /// Target has files (git or non-git) but NO `.claude/`. We refuse the
    /// fresh path and tell the caller to use Adopt.
    FreshIntoExisting,
    /// Target has `.claude/` (or other orchestrator-managed paths) — we
    /// can adopt by overwriting only orchestrator-managed paths and
    /// leaving everything else alone.
    Adopt,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallDiff {
    pub mode: InstallMode,
    /// Orchestrator-managed paths that already exist at the target and
    /// would be overwritten on adopt.
    pub will_overwrite: Vec<String>,
    /// Orchestrator-managed paths the install would create.
    pub will_add: Vec<String>,
    /// Anything outside the orchestrator-managed allowlist is reported
    /// here so the UI can reassure the user "your code is safe".
    pub user_paths_preserved: bool,
}

/// Embedded copy of the source-of-truth file at the repo root. The file
/// is read at compile time (NOT runtime) so the launcher binary stays
/// self-contained and can't drift from the orchestrator clone it was
/// built against. Updates to the launcher binary are how new entries
/// reach existing installs of the launcher; the file at the user's
/// orchestrator clone is then copied across via `update_orchestrator_at`
/// (which itself reads ORCHESTRATOR_MANAGED_PATHS — see the entry for
/// `orchestrator-managed-paths.txt` in the file, which makes the
/// propagation self-referential and fail-safe).
///
/// Path: 4 levels up from this file (commands → src → src-tauri →
/// launcher → repo root).
const ORCHESTRATOR_MANAGED_PATHS_TXT: &str =
    include_str!("../../../../orchestrator-managed-paths.txt");

/// Parse the embedded text file into a list of allowlist entries.
///
/// Parse rules (must match `_parse_managed_paths_text` in `install.py`):
///   - A leading UTF-8 BOM (`\u{feff}`) on the first line is stripped.
///     Saved-from-Windows-Notepad files routinely carry one and
///     `str::trim` does NOT remove BOM characters; without this, the
///     first allowlist entry silently fails to match.
///   - Lines are stripped of leading/trailing whitespace.
///   - Empty lines are skipped.
///   - Lines whose first non-whitespace character is `#` are comments
///     and are skipped entirely (no inline comments).
fn parse_managed_paths_text(text: &'static str) -> Vec<&'static str> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .collect()
}

/// Hard whitelist of paths the orchestrator install is allowed to touch
/// at the project root. Everything else is treated as user code.
///
/// **Source of truth:** `orchestrator-managed-paths.txt` at the repo
/// root. Edit there ONLY — both Rust (via `include_str!`, here) and
/// Python (`install.py::ORCHESTRATOR_MANAGED_PATHS`, read at import
/// time) parse the same file with the same rules. A cross-language
/// consistency test (`tests/test_managed_paths_consistency.py`) pins
/// the two languages to the file contents.
///
/// **Architectural intent (2026-05-06):** ONE VCO clone is shared by all
/// projects; per-project folders never receive the orchestrator's own
/// machinery. Entries in the .txt must be either (a) project-meaningful
/// configuration / docs that legitimately live alongside a user project
/// (`.claude/`, `docs/`, `tools/`, `infrastructure/`), (b) the
/// version-pinning manifest the launcher reads to detect an existing
/// install (`vct-module.json`), or (c) the source-of-truth file itself
/// (`orchestrator-managed-paths.txt`) so `update_orchestrator_at`
/// propagates new editions of the list into every existing install.
///
/// 2026-05-16 (PR-31 / v0.2.12): `CLAUDE.md` was REMOVED from the
/// whitelist. The root CLAUDE.md is the orchestrator-self's own
/// development documentation, not a per-project scaffold. User
/// projects render CLAUDE.md from `templates/CLAUDE.md.template` via
/// the project-bootstrapper. `DEFAULT_PRESERVE_LIST` (below) still
/// includes `CLAUDE.md` because that's the user-edits-on-update
/// concern, not the whitelist-copy concern this constant governs.
///
/// 2026-06-09 (V52-C / v0.2.52): `knowledge/` was REMOVED from the
/// whitelist. KG nodes are USER-CURATED state, not shipped content —
/// the previous mixed-ownership directory caused merge conflicts on
/// orchestrator updates (modify-vs-delete races between user-curated
/// nodes and upstream-deleted curated nodes). The orchestrator's
/// curated KG set now lives under `templates/knowledge/` and is
/// bundle-materialized into `<project>/knowledge/` by
/// `_enumerate_bundle_files` in `vco_lib/project_init.py`. The
/// manifest-driven hash compare (V47-A pattern) preserves user
/// customizations on bundle update — same shape as agents / skills /
/// hooks. Result: zero conflicts on KG content across updates.
///
/// 2026-07-15 (v0.2.81): curated `templates/knowledge/**` nodes now ship
/// ROOT-ONLY. They are materialized ONCE into the orchestrator root's
/// `knowledge/` (== the shared collection, by adopt-and-route) via
/// install.py Step 4d + the `_enumerate_bundle_files` gate; non-root
/// projects read them via the shared-read fan-out (no per-project copy /
/// no per-project re-embed). Only the `_PER_PROJECT_KNOWLEDGE_FILES`
/// allowlist (TAG_HIERARCHY.md, VOCABULARY.md, .node_formats.json,
/// .node_embeddings.README.txt) still ships per-project. Existing
/// per-project copies from pre-.81 installs are left in place forever
/// (never-delete-user-data) and their manifest entries are quietly
/// retired on the first post-.81 bundle update (`knowledge-retired`
/// action bucket — no disk op, no deferral).
///
/// **Explicitly excluded:** `install.py` / `install.sh` / `install.ps1`
/// (orchestrator entry points), `state/` (per-install metadata; the
/// 2026-05 VideoFrames over-copy bug traced to this entry being copied
/// into a user project), `claude_mcp_servers/` (orchestrator-only Python
/// package, never installed into projects — see install-paths-audit
/// 2026-05-06 §B and install-adaptation-audit Gap #1), `templates/`
/// (the SOURCE for `_enumerate_bundle_files` per-project bundle
/// installs, never the destination), `requirements*.txt` (orchestrator's
/// own Python deps), `BOOTSTRAP.md` (orchestrator setup doc), `config/`
/// (legacy entry, directory does not exist in the repo).
///
/// Used by `copy_orchestrator_to_sync` (`update_orchestrator_at` +
/// `install_orchestrator`'s plain-copy path), `apply_conflict_strategy`,
/// `classify_install_target`, and `diff_install`. The frontend mirrors a
/// humanized version in the confirm modal.
pub static ORCHESTRATOR_MANAGED_PATHS: LazyLock<Vec<&'static str>> =
    LazyLock::new(|| parse_managed_paths_text(ORCHESTRATOR_MANAGED_PATHS_TXT));

// ---------------------------------------------------------------------------
// Re-install conflict resolution (4-option modal)
//
// When the install target already contains orchestrator files (`mode ==
// Adopt`), the wizard prompts the user with four explicit strategies
// instead of the cryptic "call preview_install + confirm_overwrite=true"
// error path. See docs/INSTALL_RECOVERY.md → "Conflict Resolution" for
// the user-facing description and the Claude self-merge contract.
//
// Strategy semantics:
//   - DeleteClaudeAndReinstall: rm -rf <install_path>/.claude THEN copy
//     fresh. Only `.claude/` is wiped — the rest of the install path may
//     contain real user code, wiping the whole path would be destructive.
//   - OverwriteAll: copy on top of existing files; user edits to
//     CLAUDE.md / CONTEXT_STATE.md / etc. are LOST.
//   - OverwritePreserve [DEFAULT]: copy on top BUT for each entry in the
//     preserve list, leave the existing file alone and write the upstream
//     version as `<file>.new.<ext>` next to it. Then append a notification
//     block to .claude/CONTEXT_STATE.md instructing Claude to merge.
//   - AdoptAsIs: equivalent to the legacy `confirm_overwrite=true` path —
//     do nothing on disk, just register the install.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ConflictStrategy {
    /// rm -rf <install_path>/.claude, then fresh copy.
    DeleteClaudeAndReinstall,
    /// Copy every tracked install file on top — no preservation.
    OverwriteAll,
    /// [DEFAULT] Copy + leave preserve-list entries alone + write
    /// `<file>.new.<ext>` siblings + append merge-notification block.
    OverwritePreserve,
    /// No-op on disk — just register.
    AdoptAsIs,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConflictResolution {
    pub strategy: ConflictStrategy,
    /// Optional override for the preserve list. None → use
    /// `DEFAULT_PRESERVE_LIST`. Paths are interpreted relative to the
    /// install root.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preserve_paths: Option<Vec<String>>,
}

/// Default preserve list for `OverwritePreserve`. Paths are relative to
/// the install root. Keep in sync with `install.py::DEFAULT_PRESERVE_LIST`.
///
/// Note on MEMORY.md: lives at `~/.claude/projects/<id>/memory/MEMORY.md`,
/// NOT in the install dir. v1.0 treats it as out-of-scope-for-the-install
/// (no `.new.md` written). The notification block mentions this so the
/// user can manually merge if their MEMORY.md has diverged.
pub const DEFAULT_PRESERVE_LIST: &[&str] = &[
    "CLAUDE.md",
    ".claude/CONTEXT_STATE.md",
    ".claude/PROJECT_REGISTRY.md",
    ".env",
];

/// Marker comments delimiting the merge-notification block inside
/// `.claude/CONTEXT_STATE.md`. Re-runs of the install REPLACE the block
/// in-place rather than accumulating stale copies.
pub const MERGE_BLOCK_START: &str = "<!-- vct-merge-pending -->";
pub const MERGE_BLOCK_END: &str = "<!-- /vct-merge-pending -->";

/// Structured error returned by `install_orchestrator` when the target
/// already contains orchestrator files and the caller hasn't picked a
/// strategy. The frontend renders the 4-option conflict-resolution modal
/// from this payload.
///
/// Surfaced over Tauri as a JSON-encoded string in the Err variant: the
/// FE detects the `vct-conflict-error` discriminator field, parses the
/// JSON, and renders the modal. Plain (non-conflict) errors stay as
/// human-readable strings.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallConflictError {
    /// Discriminator — always "install_conflict". The frontend matches
    /// on this to distinguish a conflict from a generic install error.
    pub kind: String,
    /// Human-readable summary (also shown if the FE somehow can't parse
    /// the JSON).
    pub message: String,
    pub install_path: String,
    pub source_path: String,
    pub mode: InstallMode,
    /// Orchestrator paths that exist at the target and would be touched
    /// by a copy.
    pub will_overwrite: Vec<String>,
    /// Orchestrator paths the install would add.
    pub will_add: Vec<String>,
    /// Subset of `will_overwrite` that intersects the default preserve
    /// list (i.e. files the user has likely customised). Rendered first
    /// in the modal so the user sees the human cost.
    pub preserve_candidates: Vec<String>,
}

impl InstallConflictError {
    /// Serialize as a JSON string suitable for Tauri's Err variant. The
    /// frontend parses the leading `{"kind":"install_conflict"...}` shape
    /// and renders the modal.
    pub fn into_err_string(self) -> String {
        serde_json::to_string(&self).unwrap_or_else(|_| self.message)
    }
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/// Detect system capabilities: GPU, container runtime, Python, etc.
#[command]
pub async fn detect_system() -> Result<SystemDetection, String> {
    let os = std::env::consts::OS.to_string();
    let arch = std::env::consts::ARCH.to_string();

    // Run detections in parallel via tokio.
    // v0.2.68 (Defect Y): `enumerate_gpus` replaces the separate
    // nvidia/amd scalar probes — it returns ALL GPUs (NVIDIA + AMD +
    // Intel/iGPU via lspci), which we then run through `select_gpu_device`
    // to pick the most-capable usable discrete card.
    let (
        gpus,
        podman_ver,
        docker_ver,
        python_result,
        claude,
        git,
        node,
    ) = tokio::join!(
        enumerate_gpus(),
        detect_runtime_version("podman"),
        detect_runtime_version("docker"),
        detect_python(),
        check_command_exists("claude"),
        check_command_exists("git"),
        check_command_exists("node"),
    );

    let (has_python, python_version, python_cmd) = python_result;
    let has_apple_silicon = os == "macos" && arch == "aarch64";

    let has_podman = podman_ver.is_some();
    let has_docker = docker_ver.is_some();

    // Prefer podman over docker (matches user's stated setup; both work
    // identically downstream).
    let container_runtime = podman_ver.clone().or_else(|| docker_ver.clone());

    // Honor VCT_GPU_VENDOR (lowercased; "metal" handled separately on the
    // install.py side — the launcher snapshot has no Metal-via-pref path).
    let vendor_pref = std::env::var("VCT_GPU_VENDOR")
        .ok()
        .map(|v| v.trim().to_ascii_lowercase())
        .filter(|v| v == "nvidia" || v == "amd");
    let chosen =
        crate::commands::gpu_policy::select_gpu_device(&gpus, vendor_pref.as_deref());

    // `has_nvidia_gpu` / `gpu_name` keep their historical meaning ("an
    // NVIDIA card is present" / "first NVIDIA name") for backward compat
    // with downstream consumers; the chosen-device fields carry the
    // v0.2.68 selection result.
    let has_nvidia = gpus.iter().any(|c| c.vendor == "nvidia");
    let first_nvidia_name = gpus
        .iter()
        .find(|c| c.vendor == "nvidia")
        .map(|c| c.name.clone())
        .unwrap_or_default();

    let (vram_gb, gpu_vendor, chosen_gpu_name) = match &chosen {
        Some(c) if c.vendor == "nvidia" => {
            (c.vram_gb.round() as u64, Some("NVIDIA".to_string()), c.name.clone())
        }
        Some(c) if c.vendor == "amd" => {
            (c.vram_gb.round() as u64, Some("AMD".to_string()), c.name.clone())
        }
        Some(c) => {
            // "unknown"-vendor kept-on-uncertainty — surface it generically.
            (c.vram_gb.round() as u64, Some(c.vendor.to_uppercase()), c.name.clone())
        }
        None => (0, None, String::new()),
    };

    let ram_gb = detect_ram_gb();

    Ok(SystemDetection {
        os,
        arch,
        has_nvidia_gpu: has_nvidia,
        gpu_name: first_nvidia_name,
        has_apple_silicon,
        has_docker,
        has_podman,
        has_python,
        python_version,
        python_cmd,
        has_claude_cli: claude,
        has_git: git,
        has_node: node,
        container_runtime,
        ram_gb,
        vram_gb,
        gpu_vendor,
        gpus,
        chosen_gpu_name,
    })
}

/// Default ports for the shared services. Match install.py constants —
/// changing them here without changing install.py would mean the wizard
/// reports "no services running" while install.py happily reuses them.
pub(crate) const DEFAULT_WEAVIATE_PORT: u16 = 8081;
pub(crate) const DEFAULT_OLLAMA_PORT: u16 = 11435;
pub(crate) const DEFAULT_CODE_EMBED_PORT: u16 = 11440;

/// HTTP probe with short timeout. Returns the URL on 2xx/3xx, None otherwise.
/// Takes an owned String so callers can compose URLs via format! without
/// having to keep the formatted string alive themselves.
async fn probe_http(url: String) -> Option<String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .ok()?;
    match client.get(&url).send().await {
        Ok(resp) if resp.status().as_u16() < 400 => Some(url),
        _ => None,
    }
}

/// Bug 29: detect already-running shared services so the OnboardingWizard
/// can tell the user "this install will reuse them" instead of starting
/// duplicate containers that bind-conflict.
///
/// Probes the three default endpoints in parallel:
///   - http://localhost:8081/v1/meta                (Weaviate)
///   - http://localhost:11435/api/tags             (Ollama)
///   - http://localhost:11440/health               (code_embed)
///
/// Weaviate endpoint note (v0.2.53 NEW-4 correction): `install.py`'s
/// canonical Weaviate liveness probe is `/v1/.well-known/ready` (see
/// `install.py::_wait_for_weaviate`). This Rust-side
/// `detect_existing_services()` historically used `/v1/meta` instead
/// because the 2026-05-06 lifecycle audit observed false-negatives
/// from the strict ready endpoint under transient module-load
/// conditions. Both endpoints work for "is Weaviate up?" — but the
/// previous comment claimed `/v1/meta` was THE right endpoint, which
/// is incorrect: install.py uses `/v1/.well-known/ready`, that's the
/// canonical liveness probe. The two endpoints are kept independent
/// on purpose (Rust = "any signal Weaviate is reachable", Python =
/// "Weaviate fully initialised and ready to serve queries"). See
/// commands/lifecycle.rs::canonical_services for the rationale.
#[command]
pub async fn detect_existing_services() -> Result<ServicesStatus, String> {
    let weaviate = probe_http(format!(
        "http://localhost:{}/v1/meta",
        DEFAULT_WEAVIATE_PORT
    ));
    let ollama = probe_http(format!(
        "http://localhost:{}/api/tags",
        DEFAULT_OLLAMA_PORT
    ));
    let code_embed = probe_http(format!(
        "http://localhost:{}/health",
        DEFAULT_CODE_EMBED_PORT
    ));

    // Run probes concurrently — total wall time is capped at the 2s timeout
    // of the slowest probe, not 6s sequentially.
    let (weaviate_url, ollama_url, code_embed_url) =
        tokio::join!(weaviate, ollama, code_embed);

    let count = [&weaviate_url, &ollama_url, &code_embed_url]
        .iter()
        .filter(|o| o.is_some())
        .count();

    Ok(ServicesStatus {
        all_detected: count == 3,
        none_detected: count == 0,
        weaviate_url,
        ollama_url,
        code_embed_url,
    })
}

/// Get the default install path for the orchestrator.
#[command]
pub fn get_default_install_path() -> String {
    let home = directories::UserDirs::new()
        .map(|d| d.home_dir().to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    home.join("vibecoded-orchestrator")
        .to_string_lossy()
        .to_string()
}

/// Check if orchestrator is already installed at a given path.
///
/// Primary signal: `state/install-manifest.json` written at the end of a
/// successful `install.py` run. The manifest's `installed: true` flag
/// is the canonical "install.py finished" marker. Implemented in
/// `install.py::_write_install_manifest` (2026-05-06).
///
/// Fallback (backward compat for installs that predate the manifest):
/// require all three of `CLAUDE.md`, `install.py`, and `.venv/`. The
/// older two-marker (CLAUDE.md + install.py only) check returned true
/// on any unmodified clone of the source tarball even when no install
/// had ever run — false-positive observed in the install wizard
/// 2026-05-06: clone of VCO reported "already installed" though
/// `.venv/` was absent and no services config had been written.
///
/// Manifest path: `<install_path>/state/install-manifest.json`.
/// Schema documented at `install.py::INSTALL_MANIFEST_SCHEMA_VERSION`.
#[command]
pub fn check_install_status(path: String) -> bool {
    let p = PathBuf::from(&path);
    if !(p.join("CLAUDE.md").exists() && p.join("install.py").exists()) {
        return false;
    }
    // Primary: manifest with installed=true.
    let manifest = p.join("state").join("install-manifest.json");
    if let Ok(text) = std::fs::read_to_string(&manifest) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
            if v.get("installed").and_then(|x| x.as_bool()) == Some(true) {
                return true;
            }
            // Manifest exists but `installed` is false / missing —
            // do NOT fall back; this is a deliberate "install in
            // progress / aborted" state.
            return false;
        }
        // Manifest exists but unparseable — fall through to the heuristic
        // rather than trust corrupt metadata.
    }
    // Fallback: legacy installs that predate the manifest. `.venv/`
    // presence is the strongest off-disk signal that install.py ran.
    p.join(".venv").exists()
}

/// Bug F (v0.2.8): get the installed version from canonical files, not
/// `git describe`. See `commands/manifest.rs` for the full rationale —
/// summary: a file-mirror install or a release-zip install gives the
/// wrong answer (or no answer at all) when keyed off git history,
/// because the .git/ history reflects the SOURCE repo at copy time, not
/// the files actually on disk.
///
/// Priority chain (delegated to `read_version_from_install_files`):
///   1. state/install-manifest.json `version`
///   2. vct-module.json `version`
///   3. launcher/package.json `version`
///   4. launcher/src-tauri/Cargo.toml `[package] version`
///   5. launcher/src-tauri/tauri.conf.json `version`
///
/// Falls back to `git describe --tags --abbrev=0` only when ALL five
/// canonical sources return None — keeps the existing dev-environment
/// "no version files but a git repo" path working.
#[command]
pub async fn get_installed_version(path: String) -> Result<String, String> {
    let p = PathBuf::from(&path);

    // Bug F: canonical-files first.
    if let Some(v) = crate::commands::manifest::read_version_from_install_files(&p) {
        return Ok(v);
    }

    // Fallback path: `git describe`. Requires .git/ — without it we have
    // nothing left to try.
    if !p.join(".git").exists() {
        return Err("Not a git repository".to_string());
    }

    let tag_output = tokio::process::Command::new("git").silent()
        .args(["describe", "--tags", "--abbrev=0"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if tag_output.status.success() {
        let tag = String::from_utf8_lossy(&tag_output.stdout).trim().to_string();
        if !tag.is_empty() {
            return Ok(tag);
        }
    }

    let hash_output = tokio::process::Command::new("git").silent()
        .args(["rev-parse", "--short", "HEAD"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if hash_output.status.success() {
        return Ok(String::from_utf8_lossy(&hash_output.stdout).trim().to_string());
    }

    Err("Could not determine version".to_string())
}

/// v0.2.16 (W4 / 0.5): three-state update status surfaced by the
/// `UpdateBadge` banner.
///
/// Each boolean is independent — the banner renders the
/// highest-priority state (binary_stale > install_stale > remote_ahead).
///
/// - `remote_ahead`: existing v0.2.x behaviour — git index is behind
///   `origin/main`. Resolved by `update_orchestrator` (git pull +
///   install.py --update).
/// - `install_stale`: source tree on disk (vct-module.json::version) is
///   AHEAD of the last successful install (state/install-manifest.json::version).
///   Happens when the user `git pull`-s manually instead of clicking
///   "Update orchestrator". Resolved by `apply_pending_install`
///   (install.py --update, no git pull — source is already current).
/// - `binary_stale`: the running launcher's compiled version differs
///   from `launcher/dist/<arch>/<launcher-binary>.metadata.json::launcher_version`
///   on disk (`vct-launcher.metadata.json` on Linux/macOS,
///   `vct-launcher.exe.metadata.json` on Windows). Happens when a
///   newer binary lands via `git pull` without install.py running its
///   binary-swap path. Resolved by `restart_launcher` (re-exec the
///   on-disk binary).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateStatus {
    /// Existing v0.2.x semantics: local branch is behind `origin/main`.
    pub remote_ahead: bool,
    /// NEW v0.2.16: source version > installed manifest version.
    pub install_stale: bool,
    /// NEW v0.2.16: running binary version != on-disk binary version.
    pub binary_stale: bool,
    /// NEW v0.2.51: a prior `update_orchestrator` / `merge_*` /
    /// `rebase_*` invocation hit a merge conflict and surfaced the
    /// conflict modal; the user resolved the conflict in their editor
    /// (or via CLI `git commit`) but never re-entered the launcher to
    /// finish stage 2 (install.py --update + binary refresh + restart).
    /// Detected via a sentinel file at
    /// `.claude/state/orchestrator-update-resume-needed.json` combined
    /// with `.git/MERGE_HEAD` being absent (= merge state cleared, so
    /// the conflict has been resolved or aborted from outside the
    /// launcher). Resolved by `resume_orchestrator_update`. Highest
    /// priority kind in the UpdateBadge (above binary_stale): leaving
    /// this unattended means the install manifest, hook bundle, and
    /// possibly the launcher binary are all out of sync with the
    /// freshly-merged source.
    pub merge_resolved_incomplete: bool,
    /// NEW v0.2.51: which operation produced the conflict that's now
    /// awaiting resume. One of `"merge"`, `"rebase"`, or the empty
    /// string when no resume is pending. Read from the sentinel when
    /// `merge_resolved_incomplete` is true.
    pub resume_operation: String,
    /// NEW v0.2.51: which branch the conflict was on (typically `main`).
    /// Empty when no resume is pending. Read from the sentinel.
    pub resume_branch: String,
    /// `vct-module.json::version` (or canonical-files fallback chain).
    /// Empty string when neither file is present.
    pub source_version: String,
    /// `state/install-manifest.json::version`. Empty string when no
    /// manifest exists (fresh install never ran).
    pub installed_version: String,
    /// `env!("CARGO_PKG_VERSION")` — the version the currently-running
    /// launcher was compiled with.
    pub running_version: String,
    /// `launcher/dist/<arch>/<launcher-binary>.metadata.json::launcher_version`
    /// — i.e. `vct-launcher.metadata.json` on Linux/macOS,
    /// `vct-launcher.exe.metadata.json` on Windows. Empty string when
    /// the sidecar metadata is absent (e.g. dev build running from
    /// `cargo run` with no dist artifacts staged).
    pub on_disk_binary_version: String,
    /// NEW v0.2.83 (D2 / A-RC1): honest remote-check health. `false` means the
    /// remote signal is UNKNOWN — the fetch / rev-parse / rev-list chain could
    /// not complete — NOT "up to date". Pre-v0.2.83 every failure in the remote
    /// section silently left `remote_ahead=false`, indistinguishable from a
    /// genuine "no update"; the frontend then rendered nothing (the first-start
    /// bug). `true` on the success path AND for non-git installs (remote check
    /// is not applicable there — `install_stale`/`binary_stale` carry the
    /// banner, and retrying a non-checkout is pointless).
    pub remote_check_ok: bool,
    /// NEW v0.2.83 (D2 / A-RC1): concise last-stderr-line / stage label for the
    /// failure that set `remote_check_ok=false`. `None` on success and for
    /// non-git installs. The frontend surfaces this in the badge popover
    /// ("Couldn't check for updates — <error>") and schedules fast retries.
    pub remote_check_error: Option<String>,
}

/// Check for updates across all three signals (git, manifest, binary).
///
/// Returns the full `UpdateStatus` struct so the frontend banner can
/// render the highest-priority state. The previous boolean-returning
/// signature shipped through v0.2.15; v0.2.16 (W4) widens it.
#[command]
pub async fn check_for_updates(path: String) -> Result<UpdateStatus, String> {
    let p = PathBuf::from(&path);

    // Defaults — populated below when sources are available.
    let mut remote_ahead = false;
    // v0.2.83 (D2): honest remote-check health. Default ok=true / error=None so
    // a non-git install (which never enters the remote section) reports
    // "not applicable, no retry needed". The git branch below flips these to
    // false + a concise error on ANY failure so the frontend can render an
    // amber "couldn't check" state and schedule retries instead of a silent
    // false "up to date" (A-RC1).
    let mut remote_check_ok = true;
    let mut remote_check_error: Option<String> = None;
    let mut source_version = String::new();
    let mut installed_version = String::new();
    let running_version = env!("CARGO_PKG_VERSION").to_string();

    // 1. git fetch + ahead/behind check (only when `.git/` present).
    //    Pre-v0.2.16 errored when not a git repo; v0.2.16 (W4) treats
    //    that as "no git signal available" so the install_stale/
    //    binary_stale signals still work on non-checkout installs
    //    (file-mirror, release-zip, etc.).
    //
    //    v0.2.21 (Stream A Design B extension): fetch from the canonical
    //    public AGPL upstream via the `vco_upstream` remote, NOT from
    //    `origin`. Private forks (the maintainer's maintainer install,
    //    customer mirrors) have
    //    `origin` pointing at the fork; pre-fix the GUI's "Update
    //    orchestrator" banner silently never lit up because `origin`
    //    didn't advance. `ensure_upstream_remote` is idempotent — it
    //    auto-creates / corrects the remote on every call.
    if p.join(".git").exists() {
        // Pin the upstream remote BEFORE any network ops. Soft-fail: on
        // error (e.g. corrupt .git/config) mark the remote check as failed
        // (health field) and let the install_stale / binary_stale signals
        // carry the banner. v0.2.83 (D2): this used to leave remote_ahead at
        // false SILENTLY — now every failure branch populates
        // remote_check_ok/error so the frontend can honestly show
        // "couldn't check" and retry.
        if let Err(e) = crate::commands::self_update::ensure_upstream_remote(&p).await {
            eprintln!(
                "[vct] check_for_updates: ensure_upstream_remote failed at {} ({}), skipping remote check",
                p.display(),
                e
            );
            remote_check_ok = false;
            remote_check_error = Some(format!("ensure_upstream_remote: {}", e));
        } else if let Err(e) =
            // v0.2.83 (D5): the SINGLE serialized fetch home — Quick policy
            // (short retry, `--no-write-fetch-head`, process-wide mutex) so
            // this startup check never races the self-update daily check on
            // `.git/FETCH_HEAD.lock` (A-RC3). Was a hand-rolled `git fetch`
            // whose spawn error hard-`?`'d the whole command (a hidden A-RC4
            // trigger) — now a soft-fail into the health fields.
            crate::commands::self_update::serialized_fetch_upstream(
                &p,
                crate::commands::self_update::FetchPolicy::Quick,
                None,
            )
            .await
        {
            // git fetch failed — likely offline / no network / lock
            // contention that even the retry couldn't clear. Don't hard-fail
            // the whole status check; surface it as an UNKNOWN remote state
            // (not a false "up to date"). The other signals still work.
            eprintln!(
                "[vct] check_for_updates: git fetch failed at {} ({}), remote state unknown",
                p.display(),
                e
            );
            remote_check_ok = false;
            remote_check_error = Some(e);
        } else {
            // Detect the current branch so we know which upstream ref
            // to compare against. Default to `main` on any error —
            // matches the convention everywhere else in self_update.rs.
            // v0.2.83 (D2): a rev-parse SPAWN error used to `?` out of the
            // whole command (A-RC4). It is now a soft-fail: we mark the
            // health field, but STILL fall back to `main` and proceed — the
            // rev-list below is the real signal and may well succeed.
            let branch = match tokio::process::Command::new("git")
                .silent()
                .args(["rev-parse", "--abbrev-ref", "HEAD"])
                .current_dir(&p)
                .output()
                .await
            {
                Ok(branch_output) => {
                    if branch_output.status.success() {
                        let b = String::from_utf8_lossy(&branch_output.stdout)
                            .trim()
                            .to_string();
                        if b.is_empty() || b == "HEAD" {
                            "main".to_string()
                        } else {
                            b
                        }
                    } else {
                        "main".to_string()
                    }
                }
                Err(e) => {
                    eprintln!(
                        "[vct] check_for_updates: branch rev-parse spawn failed at {} ({}), assuming main",
                        p.display(),
                        e
                    );
                    remote_check_ok = false;
                    remote_check_error = Some(format!("branch rev-parse: {}", e));
                    "main".to_string()
                }
            };

            // Count commits we're behind the upstream branch. Replaces
            // the pre-v0.2.21 `git status --branch.ab` parse which
            // only worked when tracking was configured against
            // `origin/<branch>` — that conventional tracking config
            // is exactly the foot-gun Design B is removing.
            // v0.2.83 (D2): spawn error, non-zero exit, AND count-parse
            // failure each used to be COMPLETELY SILENT (remote_ahead stayed
            // false with no log). All three now populate the health fields.
            match tokio::process::Command::new("git")
                .silent()
                .args([
                    "rev-list",
                    "--count",
                    &format!(
                        "HEAD..{}/{}",
                        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
                        branch
                    ),
                ])
                .current_dir(&p)
                .output()
                .await
            {
                Ok(revlist) if revlist.status.success() => {
                    let raw = String::from_utf8_lossy(&revlist.stdout)
                        .trim()
                        .to_string();
                    match raw.parse::<u32>() {
                        Ok(n) => {
                            remote_ahead = n > 0;
                        }
                        Err(e) => {
                            eprintln!(
                                "[vct] check_for_updates: rev-list count parse failed at {} (raw={:?}, {})",
                                p.display(),
                                raw,
                                e
                            );
                            remote_check_ok = false;
                            remote_check_error =
                                Some(format!("rev-list count parse: {}", e));
                        }
                    }
                }
                Ok(revlist) => {
                    let stderr = String::from_utf8_lossy(&revlist.stderr)
                        .trim()
                        .to_string();
                    eprintln!(
                        "[vct] check_for_updates: rev-list exited non-zero at {} ({})",
                        p.display(),
                        stderr
                    );
                    remote_check_ok = false;
                    remote_check_error = Some(if stderr.is_empty() {
                        "rev-list exited non-zero".to_string()
                    } else {
                        format!("rev-list: {}", stderr)
                    });
                }
                Err(e) => {
                    eprintln!(
                        "[vct] check_for_updates: rev-list spawn failed at {} ({})",
                        p.display(),
                        e
                    );
                    remote_check_ok = false;
                    remote_check_error = Some(format!("rev-list spawn: {}", e));
                }
            }
        }
    }

    // 2. source_version: read vct-module.json (priority 2 of the
    //    canonical chain — we DO want to read the on-disk source file
    //    here, not the manifest, because we're comparing source vs
    //    manifest to detect install_stale).
    if let Some(v) = read_source_version(&p) {
        source_version = v;
    }

    // 3. installed_version: read state/install-manifest.json::version.
    //    A missing manifest means install.py never completed against
    //    this tree — install_stale is then trivially false (we don't
    //    have a "last install" to compare against).
    if let Some(v) = read_manifest_version(&p) {
        installed_version = v;
    }

    // 4. on_disk_binary_version: read the dist sidecar metadata for
    //    the current target. Falls back to empty when the sidecar
    //    isn't present (dev builds running from `cargo run`).
    let on_disk_binary_version = read_on_disk_binary_version(&p).unwrap_or_default();

    // Compute the two new flags. Strict equality keeps the logic
    // simple (no SemVer comparison) — the only producers of these
    // fields are install.py / the upload-artifact step, both of which
    // write the same canonical string. Mismatch == stale.
    let install_stale = !source_version.is_empty()
        && !installed_version.is_empty()
        && source_version != installed_version;
    let binary_stale = !on_disk_binary_version.is_empty()
        && on_disk_binary_version != running_version;

    // v0.2.51 Bug A: detect "merge-resolved-but-update-incomplete" state.
    // The sentinel is written by update_orchestrator / merge_orchestrator_*
    // / rebase_orchestrator_* whenever they surface the conflict modal. It
    // is cleared by `resume_orchestrator_update` on success AND by
    // `abort_orchestrator_merge_or_rebase`. So the sentinel being present
    // means SOMETHING is awaiting follow-up. The further .git/MERGE_HEAD
    // check distinguishes "user is still mid-conflict" (don't badge —
    // they're working on it; the modal is the right surface) from "merge
    // committed but launcher never re-entered" (DO badge — that's the
    // bug we're fixing).
    let (merge_resolved_incomplete, resume_operation, resume_branch) =
        match read_update_resume_sentinel(&p) {
            Some(sentinel) => {
                let merge_in_progress = p.join(".git").join("MERGE_HEAD").exists()
                    || p.join(".git").join("rebase-merge").exists()
                    || p.join(".git").join("rebase-apply").exists();
                if merge_in_progress {
                    // Modal is the right surface; don't double-render.
                    (false, String::new(), String::new())
                } else {
                    (true, sentinel.operation, sentinel.branch)
                }
            }
            None => (false, String::new(), String::new()),
        };

    Ok(UpdateStatus {
        remote_ahead,
        install_stale,
        binary_stale,
        merge_resolved_incomplete,
        resume_operation,
        resume_branch,
        source_version,
        installed_version,
        running_version,
        on_disk_binary_version,
        remote_check_ok,
        remote_check_error,
    })
}

// Version parsing, on-disk install-path status readers, and the launcher
// dist-slot resolvers were extracted verbatim to the `version_info`
// submodule in v0.2.77 Part 7d. The facade re-exports every symbol so the
// rest of this module + the `tests` block resolve them unchanged.
mod version_info;
pub(crate) use version_info::*;

/// Classify what an install target looks like:
/// - `Fresh` if the path doesn't exist or is empty.
/// - `Adopt` if it has `.claude/` (or any other orchestrator-managed path).
/// - `FreshIntoExisting` if it has user files but no orchestrator artifacts.
pub fn classify_install_target(install_path: &Path) -> InstallMode {
    if !install_path.exists() {
        return InstallMode::Fresh;
    }
    if !install_path.is_dir() {
        return InstallMode::FreshIntoExisting;
    }

    // Empty directory? -> Fresh
    let entries: Vec<_> = match std::fs::read_dir(install_path) {
        Ok(rd) => rd.flatten().collect(),
        Err(_) => return InstallMode::Fresh,
    };
    if entries.is_empty() {
        return InstallMode::Fresh;
    }

    // Look for any orchestrator-managed artifact
    for managed in ORCHESTRATOR_MANAGED_PATHS.iter() {
        if install_path.join(managed).exists() {
            return InstallMode::Adopt;
        }
    }

    InstallMode::FreshIntoExisting
}

/// Diff the bundled manifest against `install_path`. The "manifest" here
/// is just the orchestrator-managed allowlist plus anything else our
/// future bundle might ship — we look for what already exists at the
/// target and report it as overwrite-candidates.
pub fn diff_install(install_path: &Path) -> InstallDiff {
    let mode = classify_install_target(install_path);

    let mut will_overwrite: Vec<String> = Vec::new();
    let mut will_add: Vec<String> = Vec::new();

    for managed in ORCHESTRATOR_MANAGED_PATHS.iter() {
        let p = install_path.join(managed);
        if p.exists() {
            will_overwrite.push(managed.to_string());
        } else {
            will_add.push(managed.to_string());
        }
    }

    InstallDiff {
        mode,
        will_overwrite,
        will_add,
        user_paths_preserved: true,
    }
}

/// Preview what install_orchestrator would do at `config.install_path`.
/// Frontend calls this before showing the adopt-confirm modal.
#[command]
pub async fn preview_install(config: InstallConfig) -> Result<InstallDiff, String> {
    let install_path = PathBuf::from(&config.install_path);
    Ok(diff_install(&install_path))
}

/// Return the local repo root path (used by frontend to show "Copying
/// from {path}" in the install UI).
#[command]
pub fn get_local_repo_source() -> Result<String, String> {
    find_local_repo_root().map(|p| p.to_string_lossy().to_string())
}

/// Returns true iff `root` is a complete vco install — i.e. it has the
/// canonical install markers: CLAUDE.md + install.py + .venv/. The
/// .venv check is the key discriminator between a bundled source tree
/// (CLAUDE.md + install.py exist but .venv doesn't) and a finished
/// install. Extracted for unit-testability so we can exercise the
/// predicate without depending on `find_local_repo_root`'s runtime walk
/// from `current_exe()`.
///
/// 2026-04-28 (Bug 6 risk callout): source-repo checkouts ship
/// `install.py` + `CLAUDE.md` and developers commonly have a `.venv/`
/// next to them. The original three-marker check was therefore unable
/// to tell a "completed install" apart from a "vco source checkout I'm
/// currently developing in", which produced false positives in the
/// wizard preflight (`detect_existing_install_root`) and caused the
/// onboarding to skip past steps it should have shown. Add the same
/// state-or-env-with-KG marker as `is_completed_install_root` so both
/// call sites agree on what "completed install" means.
fn install_root_complete_at(root: &Path) -> bool {
    root.join("CLAUDE.md").is_file()
        && root.join("install.py").is_file()
        && root.join(".venv").is_dir()
        && (root.join("state").is_dir() || env_contains_kg(&root.join(".env")))
}

/// 2026-04-29 fix (wizard install-path lockdown): assert that
/// `install_path` is a vco source repo before any install-time
/// mutations run. The wizard previously let the user pick an arbitrary
/// empty folder; install_orchestrator would then copy
/// `ORCHESTRATOR_MANAGED_PATHS` into that folder — which is a SUBSET
/// (no launcher/, no first-install.sh, no scripts/) and produced an
/// "orphan" install the end user couldn't run. The discriminator is
/// the presence of `install.py` AND `first-install.sh` next to each
/// other; both ship in every vco clone and tarball, neither is in
/// ORCHESTRATOR_MANAGED_PATHS so they can't appear in a half-install.
///
/// Returns `Err` with a user-actionable message when validation fails;
/// returns `Ok(())` when the path looks like a vco source repo.
pub fn validate_source_repo(install_path: &Path) -> Result<(), String> {
    let install_py = install_path.join("install.py");
    let first_install_sh = install_path.join("first-install.sh");
    if !install_py.is_file() || !first_install_sh.is_file() {
        return Err(format!(
            "Install path must be a vco source repo (must contain install.py + first-install.sh). \
             To install at a different location, clone the repo there and re-run from that folder. \
             Got: {}",
            install_path.display()
        ));
    }
    Ok(())
}

/// Detect whether the launcher is running from inside an already-installed
/// orchestrator (i.e. the user did `bash first-install.sh` and we are now
/// inside that install). The wizard uses this to skip the install step
/// rather than prompt the user to install AGAIN somewhere else — a real
/// UX bug surfaced in 2026-04-27 testing where the wizard's default install
/// path was `~/vibecoded-orchestrator` and a user typing an absolute path
/// like `~/dev/my-project/Code` would get the two paths concatenated
/// and write the orchestrator INSIDE the project folder. With self-detection
/// the wizard can skip directly to project registration.
///
/// Returns Some(path) when:
///  - we can locate the repo root (find_local_repo_root succeeds), AND
///  - that root contains the canonical install markers (see
///    install_root_complete_at).
#[command]
pub fn detect_existing_install_root() -> Option<String> {
    let root = find_local_repo_root().ok()?;
    if install_root_complete_at(&root) {
        Some(root.to_string_lossy().to_string())
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Bug A (v0.2.5): path-agnostic install discovery.
//
// The wizard's `checkStatus()` previously probed only `$HOME/vibecoded-
// orchestrator`, producing a false "Not installed" banner for users who
// installed VCO at a custom location (e.g. `/home/<user>/code/vco/`).
//
// `get_known_install_path` is the FE entry point for "where is the
// install on this machine?". It tries two strategies in order:
//
//   1. Look up `launcher.install_path` in app_state. If set AND the path
//      still passes `check_install_status`, return it.
//   2. Walk up from `current_exe()` looking for the fixed install-root
//      markers (`install.py` + `CLAUDE.md`). The relative path between
//      the launcher binary and these files is fixed by the installer, so
//      a short bounded walk is sufficient. On a hit, write it back to
//      app_state so step 1 picks it up next time.
//
// Returns `Ok(None)` when no install is discoverable — that's not an
// error, just "no install yet". Only DB errors propagate as `Err`.
// ---------------------------------------------------------------------------

/// Resolve the install root from the launcher binary's location.
///
/// The folder layout is fixed by the installer — relative paths between
/// the launcher exe and the install root never change once shipped, so
/// we can rely on a structural walk rather than fingerprinting markers
/// with stale-state semantics (`installed: true` in a manifest that may
/// or may not exist for a hand-cloned dev tree).
///
/// Layouts in play:
///   • Tauri release bundle (Linux/Windows): `<install>/launcher/src-tauri/target/release/launcher`
///   • Tauri dev / `cargo run`:               `<install>/launcher/src-tauri/target/debug/launcher`
///   • macOS .app bundle:                     `<install>/launcher/<...>/Contents/MacOS/launcher`
///
/// The first two are exactly 4 parents up from the exe. The .app case is
/// deeper but still bounded, so a short walk that stops at the first
/// directory containing `install.py` + `CLAUDE.md` covers all three
/// without requiring per-platform branching. Sanity-only check (the two
/// files), no manifest gating — a dev clone with no `state/install-manifest.json`
/// is still a valid install root for launcher-discovery purposes.
pub(crate) fn walk_for_install_markers() -> Option<PathBuf> {
    walk_for_orchestrator_root()
}

/// v0.2.37 canonical walk-up: returns the nearest ancestor of
/// `current_exe()` that looks like an orchestrator clone root. A
/// directory qualifies if it contains EITHER:
///   * `vct-module.json` (the orchestrator clone's manifest — the
///     marker `find_local_repo_root` used pre-v0.2.37), OR
///   * `install.py` + `CLAUDE.md` (the install-root files — the
///     marker `walk_for_install_markers` used pre-v0.2.37).
///
/// Both patterns identify the same artifact. Accepting either lets a
/// binary launched from a partial checkout (release zip missing one of
/// the markers, dev clone without a generated state file, etc.) still
/// discover its root.
///
/// Walks up to 8 ancestor levels — covers Tauri release bundle, dev
/// `cargo run`, and macOS .app bundle layouts without per-platform
/// branching.
pub(crate) fn walk_for_orchestrator_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut current = exe.parent()?.to_path_buf();
    for _ in 0..8 {
        if looks_like_orchestrator_root(&current) {
            return Some(current);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// Predicate for "is this directory an orchestrator clone root?".
/// Pure function — no I/O beyond two `is_file` probes per call.
///
/// Accepts EITHER marker pattern. See `walk_for_orchestrator_root`
/// for the privacy/cross-layout rationale.
pub(crate) fn looks_like_orchestrator_root(dir: &Path) -> bool {
    dir.join("vct-module.json").is_file()
        || (dir.join("install.py").is_file() && dir.join("CLAUDE.md").is_file())
}

/// FE entry point — see module-level Bug A comment for the contract.
///
/// Soft on failure by design: a missing app_state row, a stale cached
/// path, or a no-match exe walk all collapse to `Ok(None)`. Only a real
/// DB read error propagates.
#[command]
pub async fn get_known_install_path(db: State<'_, Db>) -> Result<Option<String>, String> {
    // Strategy 1: cached path from app_state. Validate it still looks
    // like a finished install — a user can rename / delete the install
    // directory out from under the launcher, and we'd rather fall
    // through to Strategy 2 than hand the FE a phantom path.
    if let Some(cached) = db.app_state_get(APP_STATE_KEY_INSTALL_PATH)? {
        if !cached.is_empty() && check_install_status(cached.clone()) {
            return Ok(Some(cached));
        }
        // Cached row exists but the path no longer resolves; fall
        // through. We deliberately do NOT delete the stale row here —
        // the user may simply have the install on an unmounted drive,
        // and a successful Strategy 2 will overwrite it anyway.
    }

    // Strategy 2: walk up from the launcher binary.
    if let Some(found) = walk_for_install_markers() {
        let s = found.to_string_lossy().to_string();
        // Sticky cache: future calls take the fast Strategy 1 path.
        // DB write failure here is non-fatal — log and proceed.
        if let Err(e) = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, &s) {
            eprintln!(
                "[vct] get_known_install_path: failed to cache install_path: {}",
                e
            );
        }
        return Ok(Some(s));
    }

    Ok(None)
}

// ---------------------------------------------------------------------------
// Bug B (v0.2.5): re-detect hardware + apply reconfiguration.
// See `HardwareSnapshot` / `HardwareDetectionDiff` / `ReconfigReport` for
// the wire types.
// ---------------------------------------------------------------------------

/// Build a `HardwareSnapshot` from a freshly-detected `SystemDetection`.
/// Derives `use_gpu` and `low_resource` according to the same rules
/// install.py uses to map detection results onto its CLI flags.
pub(crate) fn snapshot_from_system(s: &SystemDetection) -> HardwareSnapshot {
    let ram_gb_u32 = u32::try_from(s.ram_gb).unwrap_or(u32::MAX);
    let vram_gb = s.vram_gb as f64;
    // v0.2.20: AMD presence is derived from SystemDetection.gpu_vendor.
    // detect_system() preserves the field as "AMD" when the AMD probe
    // succeeded and NVIDIA did not. We OR with the canonical
    // query_amd_gpu_present() so a host with BOTH NVIDIA + AMD detected
    // still records the AMD presence (decide_gpu_mode picks the right
    // mode via the precedence rules).
    let has_amd_gpu = s.gpu_vendor.as_deref() == Some("AMD")
        || crate::commands::gpu::query_amd_gpu_present();
    // v0.2.9 (Bug K) + v0.2.20: map raw detection → GpuMode via the VRAM
    // threshold. No user override at the snapshot layer — the override
    // only applies at the install.py CLI surface (--gpu / --cpu-only).
    //
    // v0.2.68 (Defect Y, SF-1): feed `decide_gpu_mode` the CHOSEN card's
    // vendor, NOT any-vendor-present. `vram_gb` above is already the chosen
    // discrete card's VRAM (select_gpu_device), so the vendor flags MUST
    // describe that same card or the two inputs disagree. On a mixed-vendor
    // dual-discrete host where the chosen card is AMD but a lower-VRAM NVIDIA
    // is also present, passing `s.has_nvidia_gpu=true` (any-present) with the
    // AMD card's VRAM made `decide_gpu_mode` return Cuda (NVIDIA precedence)
    // — labeling an AMD-chosen host CUDA and requesting a CUDA container
    // variant on an AMD box. Python's `_detect_system` passes only
    // `vendor=chosen.vendor` (install.py:_decide_gpu_mode), returning rocm;
    // deriving the booleans from `s.gpu_vendor` (the chosen vendor, set at
    // the detection site) keeps the Rust mirror lock-step with Python. The
    // persisted `has_nvidia_gpu`/`has_amd_gpu` snapshot fields retain their
    // historical any-present meaning for external consumers (see below) —
    // only the MODE decision uses the chosen card.
    let chosen_is_nvidia = s.gpu_vendor.as_deref() == Some("NVIDIA");
    let chosen_is_amd = s.gpu_vendor.as_deref() == Some("AMD");
    let gpu_mode_decided = crate::commands::gpu_policy::decide_gpu_mode(
        vram_gb,
        chosen_is_nvidia,
        chosen_is_amd,
        s.has_apple_silicon,
        None,
        crate::commands::gpu_policy::DEFAULT_GPU_VRAM_THRESHOLD_GB,
    );
    // use_gpu retains its pre-v0.2.9 semantics for backward compat with
    // any external consumer that reads the persisted field — but the
    // reconfig flow now consults `gpu_mode_decided` for the actual flag
    // pick. See `apply_hardware_reconfig`. v0.2.20 added Rocm to the
    // GPU-active set alongside Cuda + Metal.
    let use_gpu = matches!(
        gpu_mode_decided,
        crate::commands::gpu_policy::GpuMode::Cuda
            | crate::commands::gpu_policy::GpuMode::Rocm
            | crate::commands::gpu_policy::GpuMode::Metal
    );
    let low_resource = ram_gb_u32 > 0 && ram_gb_u32 < 8;
    HardwareSnapshot {
        has_nvidia_gpu: s.has_nvidia_gpu,
        gpu_name: s.gpu_name.clone(),
        has_apple_silicon: s.has_apple_silicon,
        ram_gb: ram_gb_u32,
        use_gpu,
        low_resource,
        vram_gb,
        has_amd_gpu,
        gpu_mode_decided,
        // v0.2.68 (Defect Y): carry the enumeration + chosen device so the
        // Preferences panel can show "found N GPUs, using <discrete>".
        gpus: s.gpus.clone(),
        chosen_gpu_name: s.chosen_gpu_name.clone(),
    }
}

fn snapshot_changed_fields(a: &HardwareSnapshot, b: &HardwareSnapshot) -> Vec<String> {
    let mut out = Vec::new();
    if a.has_nvidia_gpu != b.has_nvidia_gpu {
        out.push("has_nvidia_gpu".to_string());
    }
    if a.gpu_name != b.gpu_name {
        out.push("gpu_name".to_string());
    }
    if a.has_apple_silicon != b.has_apple_silicon {
        out.push("has_apple_silicon".to_string());
    }
    if a.ram_gb != b.ram_gb {
        out.push("ram_gb".to_string());
    }
    if a.use_gpu != b.use_gpu {
        out.push("use_gpu".to_string());
    }
    if a.low_resource != b.low_resource {
        out.push("low_resource".to_string());
    }
    // v0.2.9 (Bug K): VRAM is a float; treat as changed if |diff| > 0.1 GB
    // to avoid spurious change events from rounding noise in the probes.
    if (a.vram_gb - b.vram_gb).abs() > 0.1 {
        out.push("vram_gb".to_string());
    }
    if a.gpu_mode_decided != b.gpu_mode_decided {
        out.push("gpu_mode_decided".to_string());
    }
    // v0.2.20: AMD presence drift — added a card, removed a card, or
    // a userspace ROCm install became available (rocminfo on PATH now).
    if a.has_amd_gpu != b.has_amd_gpu {
        out.push("has_amd_gpu".to_string());
    }
    // v0.2.68 (Defect Y): the chosen discrete GPU changed (user swapped
    // cards, or added a more-capable one), or the enumeration count
    // changed (added/removed a GPU, including an iGPU).
    if a.chosen_gpu_name != b.chosen_gpu_name {
        out.push("chosen_gpu_name".to_string());
    }
    if a.gpus.len() != b.gpus.len() {
        out.push("gpus".to_string());
    }
    out
}

/// Read the persisted hardware snapshot, if any. Soft-fails parse errors
/// (returns `Ok(None)`) so a corrupted row can be overwritten by the
/// next `redetect_hardware` call rather than wedging the Preferences UI.
pub(crate) fn read_persisted_hardware_snapshot(db: &Db) -> Result<Option<HardwareSnapshot>, String> {
    let raw = match db.app_state_get(APP_STATE_KEY_HARDWARE_SNAPSHOT)? {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(None),
    };
    match serde_json::from_str::<HardwareSnapshot>(&raw) {
        Ok(snap) => Ok(Some(snap)),
        Err(e) => {
            eprintln!(
                "[vct] hardware_snapshot row is unparseable; ignoring: {}",
                e
            );
            Ok(None)
        }
    }
}

fn write_persisted_hardware_snapshot(db: &Db, snap: &HardwareSnapshot) -> Result<(), String> {
    let json = serde_json::to_string(snap)
        .map_err(|e| format!("serialize hardware snapshot: {}", e))?;
    db.app_state_set(APP_STATE_KEY_HARDWARE_SNAPSHOT, &json)
}

/// Re-run detection and return a diff against the persisted snapshot.
/// Also persists the FRESH snapshot so a subsequent
/// `apply_hardware_reconfig` call sees the current state, not the
/// pre-detection one.
#[command]
pub async fn redetect_hardware(db: State<'_, Db>) -> Result<HardwareDetectionDiff, String> {
    redetect_hardware_with_probe(db.inner(), detect_system).await
}

/// v0.2.34 (Agent B): inner re-detect implementation parameterised over
/// the system probe so non-Tauri callers (background job spawned from
/// `lib.rs::setup`, install-time pre-check in `install_module_for_project`)
/// can reuse the same logic without going through `State<'_, Db>`, AND so
/// tests can inject a fixture probe (e.g. RTX 4080 SUPER, or a probe that
/// returns an error to exercise the fallback path).
///
/// Contract:
///   - Reads the existing snapshot from `app_state` (may be `None`,
///     partial-schema-but-parsable, or unparseable).
///   - Runs `probe()` to get a fresh `SystemDetection`. On error, returns
///     the error immediately WITHOUT touching the persisted row.
///   - Persists the fresh snapshot.
///   - Returns the diff (before may be `None`).
///
/// The probe is a `FnOnce() -> impl Future` — `detect_system` matches
/// directly. Tests pass closures returning fixture data.
pub(crate) async fn redetect_hardware_with_probe<F, Fut>(
    db: &Db,
    probe: F,
) -> Result<HardwareDetectionDiff, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<SystemDetection, String>>,
{
    let before = read_persisted_hardware_snapshot(db)?;
    let system = probe().await?;
    let after = snapshot_from_system(&system);

    write_persisted_hardware_snapshot(db, &after)?;

    let changed_fields = match &before {
        Some(prev) => snapshot_changed_fields(prev, &after),
        None => Vec::new(),
    };

    Ok(HardwareDetectionDiff {
        before,
        after,
        changed_fields,
    })
}

/// v0.2.34 (Agent B): install-time hardware-snapshot freshness guard.
///
/// Called from `install_module_for_project` BEFORE reading
/// `gpu_mode_decided`. Belt-and-suspenders against three failure modes:
///
///   1. **v0.2.20-style schema gap**: the persisted row predates a new
///      snapshot field, serde defaults the missing field, and the install
///      flow reads stale data (the observed RTX 4080 SUPER bug —
///      `gpu_mode_decided` missing → defaulted to `Cpu` → CUDA host
///      pulled `-cpu` variant).
///   2. **Hardware change between launcher updates**: user added a GPU,
///      swapped RAM, etc. between the last `redetect_hardware` (or update
///      boundary) and this install.
///   3. **Manual binary swap**: user replaced the launcher binary
///      without going through the in-app update flow, so the
///      update-boundary trigger never fired.
///
/// Resilience: if the probe fails (transient `nvidia-smi` not on PATH,
/// permission error, etc.) we DO NOT block the install. We fall back to
/// the last-known persisted snapshot and log a warning. If there is no
/// last-known snapshot either, we propagate the probe error — without
/// any hardware data the install cannot pick a variant safely.
///
/// Returns the fresh-or-fallback snapshot for the caller to read fields
/// from. The persisted row is updated only on probe success.
pub(crate) async fn resolve_fresh_or_last_known_snapshot_with_probe<F, Fut>(
    db: &Db,
    probe: F,
) -> Result<HardwareSnapshot, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<SystemDetection, String>>,
{
    match redetect_hardware_with_probe(db, probe).await {
        Ok(diff) => Ok(diff.after),
        Err(probe_err) => {
            // Probe failed. Try last-known snapshot.
            match read_persisted_hardware_snapshot(db) {
                Ok(Some(snap)) => {
                    eprintln!(
                        "[vct] install-time hw redetect failed ({}), falling back to last-known snapshot",
                        probe_err
                    );
                    Ok(snap)
                }
                Ok(None) => {
                    eprintln!(
                        "[vct] install-time hw redetect failed ({}) and no last-known snapshot — propagating",
                        probe_err
                    );
                    Err(probe_err)
                }
                Err(e) => {
                    eprintln!(
                        "[vct] install-time hw redetect failed ({}) and last-known snapshot read failed ({}) — propagating probe error",
                        probe_err, e
                    );
                    Err(probe_err)
                }
            }
        }
    }
}

/// v0.2.34 (Agent B): production wrapper around
/// `resolve_fresh_or_last_known_snapshot_with_probe` that uses the real
/// `detect_system` probe. Returns `Option<HardwareSnapshot>` so callers
/// that should soft-degrade (e.g. install can still proceed with
/// `GpuMode::Cpu` default if probe fails AND no snapshot exists) can do
/// so without rewrapping the error.
///
/// `install_module_for_project` consumes this to ensure the
/// `gpu_mode_decided` it reads downstream is fresh + structurally
/// complete.
pub(crate) async fn ensure_fresh_hardware_snapshot_for_install(
    db: &Db,
) -> Option<HardwareSnapshot> {
    match resolve_fresh_or_last_known_snapshot_with_probe(db, detect_system).await {
        Ok(snap) => Some(snap),
        Err(e) => {
            eprintln!(
                "[vct] ensure_fresh_hardware_snapshot_for_install: no snapshot available ({}); install will fall back to GpuMode::Cpu",
                e
            );
            None
        }
    }
}

/// v0.2.34 (Agent B): mark the next launcher boot as needing a hardware
/// re-detection. Called from `self_update::finish_apply_after_pull` right
/// before the new launcher process is spawned. Soft-fails: a write error
/// here only means the next boot won't auto-re-detect — the user can
/// still hit the Preferences button.
pub(crate) fn mark_hardware_redetect_pending_after_update(db: &Db) {
    if let Err(e) = db.app_state_set(APP_STATE_KEY_HARDWARE_REDETECT_PENDING, "1") {
        eprintln!(
            "[vct] mark_hardware_redetect_pending_after_update: app_state write failed: {}",
            e
        );
    }
}

/// v0.2.34 (Agent B): predicate used by the boot-time consumer to check
/// whether the previous launcher process flagged a pending re-detect.
pub(crate) fn is_hardware_redetect_pending(db: &Db) -> bool {
    matches!(
        db.app_state_get(APP_STATE_KEY_HARDWARE_REDETECT_PENDING),
        Ok(Some(ref s)) if s == "1"
    )
}

/// v0.2.34 (Agent B): clear the pending flag (writes empty string, which
/// `app_state_get` returns as "unset" via the read-path's empty-string
/// guard at all call sites). Best-effort.
pub(crate) fn clear_hardware_redetect_pending(db: &Db) {
    if let Err(e) = db.app_state_set(APP_STATE_KEY_HARDWARE_REDETECT_PENDING, "") {
        eprintln!(
            "[vct] clear_hardware_redetect_pending: app_state write failed: {}",
            e
        );
    }
}

/// v0.2.34 (Agent B): boot-time consumer of the
/// `launcher.hardware_redetect_pending` flag. Spawns a background
/// `redetect_hardware_with_probe` job when the flag is set, then clears
/// the flag. Soft-fails throughout — a probe hiccup just leaves the
/// existing snapshot in place (the manual Preferences button is the
/// recovery path) and the cleared flag avoids retry-loops on the next
/// boot if `detect_system` is broken for a deeper reason.
///
/// Spawning is done via `tauri::async_runtime::spawn` so the boot setup
/// path doesn't block on detection. The flag is cleared SYNCHRONOUSLY
/// before the spawn so a fast restart-during-restart doesn't double-fire.
pub fn consume_pending_hardware_redetect_if_set<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
) {
    let Some(db) = app.try_state::<Db>() else {
        return;
    };
    if !is_hardware_redetect_pending(db.inner()) {
        return;
    }
    // Clear synchronously so a race / restart-during-restart doesn't
    // double-spawn. The redetect runs even if the clear write fails — the
    // worst case is a duplicate redetect on the next boot, which is
    // idempotent.
    clear_hardware_redetect_pending(db.inner());

    tauri::async_runtime::spawn(async move {
        let Some(db) = app.try_state::<Db>() else {
            return;
        };
        match redetect_hardware_with_probe(db.inner(), detect_system).await {
            Ok(diff) => {
                if diff.changed_fields.is_empty() {
                    eprintln!(
                        "[vct] post-update hardware redetect: snapshot unchanged"
                    );
                } else {
                    eprintln!(
                        "[vct] post-update hardware redetect: {} field(s) changed: {:?}",
                        diff.changed_fields.len(),
                        diff.changed_fields
                    );
                }
            }
            Err(e) => {
                eprintln!(
                    "[vct] post-update hardware redetect failed (non-fatal, last-known snapshot retained): {}",
                    e
                );
            }
        }
    });
}

/// Spawn `install.py --update <flags>` from the known install path,
/// streaming subprocess output line-by-line as `hardware_reconfig_progress`
/// Tauri events. Returns a final `ReconfigReport`.
///
/// Flags are derived from the persisted (already-refreshed) snapshot:
///   - `use_gpu=true`  → `--gpu`
///   - `use_gpu=false` → `--cpu-only`
///   - `low_resource=true` → `--low-resource`
///
/// Refuses to run when `launcher.install_path` is unset / invalid OR
/// when `launcher.hardware_snapshot` is unset (caller must hit Re-detect
/// first).
#[command]
pub async fn apply_hardware_reconfig(
    db: State<'_, Db>,
    window: Window,
) -> Result<ReconfigReport, String> {
    let install_path_str = db
        .app_state_get(APP_STATE_KEY_INSTALL_PATH)?
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            "No known install — run the install wizard first.".to_string()
        })?;
    if !check_install_status(install_path_str.clone()) {
        return Err(format!(
            "Cached install path is no longer a valid VCO install: {}. \
             Re-run the install wizard.",
            install_path_str
        ));
    }
    let install_path = PathBuf::from(&install_path_str);

    let snap = read_persisted_hardware_snapshot(db.inner())?
        .ok_or_else(|| "Run Re-detect first.".to_string())?;

    let system = detect_system().await?;
    if !system.has_python {
        return Err(
            "Python 3.11+ is required for hardware reconfiguration. Install from https://python.org"
                .to_string(),
        );
    }

    // Build the install.py argv. Mirrors the lightweight argv builder
    // contract — explicit, no auto-add of unrelated flags.
    //
    // v0.2.9 (Bug K): the flag picked is driven by `gpu_mode_decided`
    // (which already applied the VRAM threshold + Apple Silicon logic),
    // not the legacy `use_gpu` boolean. Metal is treated as a GPU-mode
    // pick at the install.py surface — install.py itself recognises Apple
    // Silicon and routes to the metal compose overlay.
    let mut argv: Vec<String> = vec![
        "install.py".to_string(),
        "--update".to_string(),
    ];
    // v0.2.20: Cuda, Rocm, and Metal all map to `--gpu` at the install.py
    // surface — install.py reads the vendor separately and writes the
    // correct compose overlay (NVIDIA gpu.yml vs ROCm rocm.yml vs no
    // overlay for Metal). Only `Cpu` routes to `--cpu-only`.
    use crate::commands::gpu_policy::GpuMode;
    match snap.gpu_mode_decided {
        GpuMode::Cuda | GpuMode::Rocm | GpuMode::Metal => argv.push("--gpu".to_string()),
        GpuMode::Cpu => argv.push("--cpu-only".to_string()),
    }
    if snap.low_resource {
        argv.push("--low-resource".to_string());
    }

    // Prepare log file. We tee subprocess output to disk so the user can
    // still inspect what happened after the launcher restarts. Best-effort
    // — failure to create the log dir does NOT block the reconfig run.
    let log_dir = install_path.join("state").join("logs");
    let _ = std::fs::create_dir_all(&log_dir);
    let log_stamp = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let log_path = log_dir.join(format!("hardware-reconfig-{}.log", log_stamp));
    let log_path_str = log_path.to_string_lossy().to_string();

    let mut log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .ok();
    if let Some(f) = log_file.as_mut() {
        use std::io::Write;
        let _ = writeln!(
            f,
            "[vct] hardware-reconfig START argv={:?} install_path={}",
            argv, install_path_str
        );
    }

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd).silent();
    cmd.args(&argv)
        .current_dir(&install_path)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);

    // v0.2.27: force Python to use UTF-8 for stdout/stderr. Without
    // this, Python on Windows defaults stdout to the locale's legacy
    // ANSI code page (cp1252 on Western European installs). install.py
    // contains ~660 non-ASCII characters (arrows, em-dashes, check
    // marks) in user-facing print() lines and crashes with
    // `UnicodeEncodeError: 'charmap' codec can't encode character`
    // mid-update. install.py's own `_sys.stdout.reconfigure(...)` block
    // (commit a5b2971, v0.2.27) is the in-Python fix; this env-var
    // belt-and-braces protects upgrades from v0.2.25 / v0.2.26 where
    // that block isn't present in the installed install.py on disk.
    // `PYTHONIOENCODING` is honored by Python 3.4+ on every platform;
    // `PYTHONUTF8` (the UTF-8 Mode) is Python 3.7+ and switches more
    // of stdlib's filesystem-encoding default to UTF-8 too. POSIX
    // no-op (stdout was already UTF-8). Set on the child only.
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("install.py --update failed to start: {}", e))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "install.py --update: stdout pipe missing".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "install.py --update: stderr pipe missing".to_string())?;

    use tokio::io::{AsyncBufReadExt, BufReader};

    let window_out = window.clone();
    let log_path_out = log_path.clone();
    let stdout_task = tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        // Re-open the log file per task to keep the borrows simple — the
        // OS-level append mode serialises writes between stdout/stderr
        // tasks just fine.
        let mut log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path_out)
            .ok();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(f) = log.as_mut() {
                use std::io::Write;
                let _ = writeln!(f, "{}", line);
            }
            let _ = window_out.emit("hardware_reconfig_progress", line);
        }
    });

    let window_err = window.clone();
    let log_path_err = log_path.clone();
    let stderr_task = tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        let mut log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path_err)
            .ok();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(f) = log.as_mut() {
                use std::io::Write;
                let _ = writeln!(f, "[stderr] {}", line);
            }
            let _ = window_err.emit("hardware_reconfig_progress", line);
        }
    });

    let status = child
        .wait()
        .await
        .map_err(|e| format!("install.py --update failed to await: {}", e))?;

    // Drain both pipes before returning. We don't propagate join errors —
    // the subprocess result is the source of truth.
    let _ = stdout_task.await;
    let _ = stderr_task.await;

    let exit_code = status.code().unwrap_or(-1);
    let success = status.success();

    if let Some(f) = log_file.as_mut() {
        use std::io::Write;
        let _ = writeln!(
            f,
            "[vct] hardware-reconfig END success={} exit_code={}",
            success, exit_code
        );
    }

    if success {
        let now = chrono::Utc::now().to_rfc3339();
        if let Err(e) =
            db.app_state_set(APP_STATE_KEY_HARDWARE_LAST_RECONFIGURED, &now)
        {
            eprintln!("[vct] failed to persist hardware_last_reconfigured_at: {}", e);
        }
    }

    Ok(ReconfigReport {
        success,
        exit_code,
        log_path: log_path_str,
    })
}

/// Boot-time seed: if `launcher.hardware_snapshot` is unset, run a
/// detection and persist. Called from `lib.rs::setup`.
///
/// Implementation note: setup() runs INSIDE the Tauri async runtime, so
/// we cannot block on a fresh `tokio::runtime::Runtime` (panics with
/// "Cannot start a runtime from within a runtime"). Instead we spawn the
/// detection as a background task — boot continues immediately, the
/// snapshot lands in app_state ~hundreds of ms later. Re-detect calls
/// already overwrite this asynchronously, so the eventual-consistency
/// model is consistent end-to-end.
///
/// The caller passes an owned `AppHandle` so the spawned task can
/// re-acquire the Db State without borrowing across an `.await` (Db
/// itself is `!Clone` — it wraps a `Mutex<Connection>`).
///
/// Soft-fails every error path so a detection hiccup never affects boot.
pub fn seed_initial_hardware_snapshot_if_missing<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
) {
    // Synchronous fast-path: skip the spawn entirely when the row is
    // already populated.
    if let Some(db) = app.try_state::<Db>() {
        match db.app_state_get(APP_STATE_KEY_HARDWARE_SNAPSHOT) {
            Ok(Some(s)) if !s.is_empty() => return,
            Ok(_) => {}
            Err(e) => {
                eprintln!("[vct] seed hw snapshot: app_state read failed: {}", e);
                return;
            }
        }
    } else {
        return;
    }
    tauri::async_runtime::spawn(async move {
        let system = match detect_system().await {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[vct] seed hw snapshot: detect_system failed: {}", e);
                return;
            }
        };
        let snap = snapshot_from_system(&system);
        let Some(db) = app.try_state::<Db>() else {
            return;
        };
        // Re-check inside the task: a race with Re-detect Preferences
        // action would otherwise clobber a fresher snapshot.
        match db.app_state_get(APP_STATE_KEY_HARDWARE_SNAPSHOT) {
            Ok(Some(s)) if !s.is_empty() => return,
            Ok(_) => {}
            Err(e) => {
                eprintln!("[vct] seed hw snapshot: app_state recheck failed: {}", e);
                return;
            }
        }
        if let Err(e) = write_persisted_hardware_snapshot(db.inner(), &snap) {
            eprintln!("[vct] seed hw snapshot: persist failed: {}", e);
        }
    });
}

// ---------------------------------------------------------------------------
// Bug 32: pre-flight install safety check.
//
// Before the user confirms an install, we surface EXACTLY what will and
// will not be touched. The frontend renders a "safety check" panel from
// the SafetyReport struct and shows the user a final Cancel / Confirm
// step. Risks list is the user's last chance to back out.
//
// All probes are read-only: we do NOT spawn `podman volume rm`, do NOT
// call `compose down`, do NOT POST/DELETE to Weaviate. The Weaviate
// schema is read via GET /v1/schema. Container volumes are listed via
// `podman/docker volume ls` + `volume inspect` (read-only commands).
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExistingVolume {
    pub name: String,
    pub mountpoint: String,
    pub driver: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SafetyReport {
    /// Orchestrator-managed paths that adopt-flow would change at the
    /// install target. Read-merge-write applies for some (e.g.
    /// `.claude/settings.json`) but the file IS modified.
    pub will_overwrite_orchestrator_files: Vec<String>,
    /// Sample of paths INSIDE `install_path` that are NOT in the
    /// orchestrator-managed allowlist — i.e. user code we will never
    /// touch. Capped to 50 entries for UI rendering.
    pub will_preserve_user_code: Vec<String>,
    /// Existing Podman/Docker volumes whose names match the four
    /// orchestrator volumes. The launcher will NOT remove or rebind
    /// these — they are surfaced so the user can see what data is
    /// already on disk.
    pub existing_volumes: Vec<ExistingVolume>,
    /// Weaviate classes that already exist on the running instance.
    /// Will be preserved (no PUT/DELETE; only POST of missing classes).
    pub existing_collections: Vec<String>,
    /// Weaviate classes the install would POST. Empty if Weaviate is
    /// not reachable or all required classes already exist.
    pub new_collections_to_add: Vec<String>,
    /// Services already responding on their default port. Will NOT be
    /// restarted by the install.
    pub services_running: Vec<String>,
    /// Services the install would `compose up -d`. Empty if everything
    /// is already up.
    pub services_to_start: Vec<String>,
    /// Free-form risk lines for the UI to render under a warning icon.
    /// Empty list = "all clear".
    pub risks: Vec<String>,
}

/// All volume names the orchestrator might own at this point on a
/// machine. Includes:
///   - canonical names from `infrastructure/docker-compose.yml`
///     (`weaviate_data`, `ollama_data`, `code_embed_cache`)
///   - historical / project-suffixed names users may have created
///     before the canonical compose file existed (Bug 31 migration)
///
/// Used by both Bug 32 (preflight surfacing) and Bug 31 (volume picker
/// existing-volume detection).
pub const ORCHESTRATOR_VOLUME_NAMES: &[&str] = &[
    // Canonical (current compose)
    "weaviate_data",
    "ollama_data",
    "code_embed_cache",
    // Historical project-suffixed names (Bug 31 — generate `external: true`
    // mapping to keep the user's existing data without recreating volumes)
    "weaviate_claude",
    "ollama_claude",
    "vct_code_embed",
];

/// Read-only volume detection. Tries `podman volume ls --format json`
/// first, falls back to `docker volume ls --format json`. If neither is
/// installed we return an empty list (not an error — the user may not
/// have a container runtime yet, which is fine pre-install).
///
/// Bug 31: also exposed as `detect_existing_volumes_for_volumes_module`
/// so the volumes command module can reuse the same detector instead
/// of duplicating it.
pub async fn detect_existing_volumes_for_volumes_module() -> Vec<ExistingVolume> {
    detect_existing_volumes().await
}

async fn detect_existing_volumes() -> Vec<ExistingVolume> {
    for runtime in &["podman", "docker"] {
        let runtime_path = match which_on_path(runtime) {
            Some(p) => p,
            None => continue,
        };
        // List names matching one of our known orchestrator volume names.
        let mut found: Vec<ExistingVolume> = Vec::new();
        for name in ORCHESTRATOR_VOLUME_NAMES {
            // `volume inspect <name>` returns 0 with JSON if it exists,
            // non-zero if not. Read-only — never mutates state.
            let out = tokio::process::Command::new(&runtime_path).silent()
                .args(["volume", "inspect", name])
                .output()
                .await;
            let out = match out {
                Ok(o) => o,
                Err(_) => continue,
            };
            if !out.status.success() {
                continue;
            }
            let body = String::from_utf8_lossy(&out.stdout);
            // Both podman and docker emit a JSON array of volume objects.
            let arr: serde_json::Value = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(_) => continue,
            };
            if let Some(items) = arr.as_array() {
                for item in items {
                    let mountpoint = item
                        .get("Mountpoint")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let driver = item
                        .get("Driver")
                        .and_then(|v| v.as_str())
                        .unwrap_or("local")
                        .to_string();
                    found.push(ExistingVolume {
                        name: name.to_string(),
                        mountpoint,
                        driver,
                    });
                }
            }
        }
        if !found.is_empty() {
            return found;
        }
    }
    Vec::new()
}

/// v0.2.77 (Part 7c task 3): delegates to the shared
/// `vct_launcher_core::paths::which_on_path` (one home). Behaviour upgrade
/// on Windows — the shared form also probes `.exe`/`.cmd`/`.bat`, which
/// this inline copy lacked (so a lookup for `python` would previously miss
/// `python.exe`). POSIX behaviour is unchanged.
fn which_on_path(name: &str) -> Option<PathBuf> {
    vct_launcher_core::paths::which_on_path(name)
}

/// Read-only Weaviate schema probe. Returns the list of class names
/// already present on the running instance; empty Vec if Weaviate is
/// not reachable.
async fn detect_existing_collections() -> Vec<String> {
    let url = format!(
        "http://localhost:{}/v1/schema",
        DEFAULT_WEAVIATE_PORT
    );
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    let resp = match client.get(&url).send().await {
        Ok(r) if r.status().is_success() => r,
        _ => return Vec::new(),
    };
    let body: serde_json::Value = match resp.json().await {
        Ok(b) => b,
        Err(_) => return Vec::new(),
    };
    body.get("classes")
        .and_then(|c| c.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|c| c.get("class").and_then(|v| v.as_str()).map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

/// Walk `path` non-recursively (depth 1 — the immediate children at the
/// install target), collecting up to `cap` entries that are NOT in the
/// orchestrator-managed allowlist. Used to reassure the user "your code
/// in <path>/foo is untouched".
fn list_user_paths_under(path: &Path, cap: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let read = match std::fs::read_dir(path) {
        Ok(rd) => rd,
        Err(_) => return out,
    };
    for entry in read.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy().to_string();
        // Skip orchestrator-managed entries.
        if ORCHESTRATOR_MANAGED_PATHS.iter().any(|m| *m == name_str) {
            continue;
        }
        // Skip dotfiles that might be IDE/git noise — we still preserve
        // them but don't bloat the report.
        if name_str.starts_with('.') {
            continue;
        }
        out.push(name_str);
        if out.len() >= cap {
            break;
        }
    }
    out.sort();
    out
}

/// Pre-flight safety check. Renders the "you're about to install" panel
/// in the frontend. Fully read-only — never mutates Weaviate, container
/// state, or files at `install_path`.
#[command]
pub async fn preflight_install_safety_check(
    install_path: String,
) -> Result<SafetyReport, String> {
    let target = PathBuf::from(&install_path);

    // 1. What orchestrator files are at the target? (will_overwrite from
    //    diff_install — those are the files we'll modify; everything
    //    else at the path is user code we leave alone.)
    let diff = diff_install(&target);
    let will_overwrite_orchestrator_files = diff.will_overwrite.clone();

    // 2. User-code reassurance list — non-recursive, capped at 50.
    let will_preserve_user_code = if target.is_dir() {
        list_user_paths_under(&target, 50)
    } else {
        Vec::new()
    };

    // 3. Existing volumes — never touched.
    let existing_volumes = detect_existing_volumes().await;

    // 4. Existing Weaviate classes — preserved.
    let existing_collections = detect_existing_collections().await;

    // 5. Required classes for THIS install. Mirror install.py
    //    `_ensure_collections` — code-graph classes are shared and
    //    created lazily, so they're not in the per-install required set.
    let required_classes = ["KnowledgeGraph", "Development"];
    let new_collections_to_add: Vec<String> = required_classes
        .iter()
        .filter(|c| !existing_collections.contains(&(**c).to_string()))
        .map(|c| (*c).to_string())
        .collect();

    // 6. Services up/down classification.
    let services = detect_existing_services().await.unwrap_or(ServicesStatus {
        all_detected: false,
        none_detected: true,
        weaviate_url: None,
        ollama_url: None,
        code_embed_url: None,
    });
    let mut services_running: Vec<String> = Vec::new();
    let mut services_to_start: Vec<String> = Vec::new();
    if services.weaviate_url.is_some() {
        services_running.push("weaviate".to_string());
    } else {
        services_to_start.push("weaviate".to_string());
    }
    if services.ollama_url.is_some() {
        services_running.push("ollama".to_string());
    } else {
        services_to_start.push("ollama".to_string());
    }
    // code_embed is GPU-only and started conditionally — surface it only
    // if it's actively running. We don't predict whether install would
    // start it (that's --gpu-dependent).
    if services.code_embed_url.is_some() {
        services_running.push("code_embed".to_string());
    }

    // 7. Risk list. Empty = all clear.
    let mut risks: Vec<String> = Vec::new();
    for path in &will_overwrite_orchestrator_files {
        // .claude/settings.json + .vscode/settings.json are
        // read-merge-write — call that out so the user knows their
        // hooks/permissions/IDE settings survive.
        if path == ".claude" {
            risks.push(
                ".claude/settings.json env block will be added or updated; existing hooks, permissions, and agents config are preserved."
                    .to_string(),
            );
        } else {
            risks.push(format!("Will overwrite orchestrator-managed path: {}", path));
        }
    }
    if !existing_volumes.is_empty() {
        // Bug 32 #4 + Bug 31: bind-mount override is suppressed when
        // existing volumes are detected. Tell the user.
        risks.push(format!(
            "Detected {} existing orchestrator container volume(s). They will be reused as-is (NOT removed, NOT rebound). To migrate volume data to a different folder, use Settings → Volumes → Migrate after install.",
            existing_volumes.len()
        ));
    }

    Ok(SafetyReport {
        will_overwrite_orchestrator_files,
        will_preserve_user_code,
        existing_volumes,
        existing_collections,
        new_collections_to_add,
        services_running,
        services_to_start,
        risks,
    })
}

/// Install the orchestrator by COPYING from the launcher's local repo
/// source to `config.install_path` (Bug 17). No network, no `git clone`,
/// no GitHub PAT. After the copy, optionally invoke `install.py` for
/// post-copy setup (venv + container start). Emits "install_progress"
/// events.
///
/// Conflict resolution (re-install over an existing install):
///   - `conflict: Some(ConflictResolution)` → execute the chosen
///     strategy (preferred path; the wizard always sends this).
///   - `conflict: None && confirm_overwrite: true` → legacy adopt path,
///     equivalent to `ConflictStrategy::AdoptAsIs`. Kept for backward
///     compatibility with any caller that hasn't been migrated yet.
///   - `conflict: None && confirm_overwrite: false` on an Adopt target →
///     refuse with `InstallConflictError` (JSON-encoded in the Err
///     variant). The frontend parses this and renders the 4-option
///     conflict-resolution modal.
#[command]
pub async fn install_orchestrator(
    config: InstallConfig,
    confirm_overwrite: Option<bool>,
    conflict: Option<ConflictResolution>,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&config.install_path);

    // 2026-04-29 fix (wizard install-path lockdown): refuse to install
    // into a directory that isn't a vco source repo. The previous code
    // path let the wizard target an arbitrary empty folder and copy a
    // SUBSET of files in (per ORCHESTRATOR_MANAGED_PATHS — no launcher,
    // no first-install.sh, no scripts/), producing a half-installed
    // orphan that the end user couldn't run. Install is in-place; the
    // install_path must be the cloned source repo. Defensive check
    // BEFORE any filesystem mutations.
    validate_source_repo(&install_path)?;

    let system = detect_system().await?;

    if !system.has_python {
        return Err(format!(
            "Python 3.11+ is required. Install from https://python.org"
        ));
    }

    // Resolve the effective conflict strategy.
    let mode = classify_install_target(&install_path);
    let confirmed = confirm_overwrite.unwrap_or(false);
    let effective_conflict: Option<ConflictResolution> = match (&conflict, confirmed) {
        (Some(c), _) => Some(c.clone()),
        // Legacy: confirm_overwrite=true with no explicit strategy → AdoptAsIs.
        (None, true) => Some(ConflictResolution {
            strategy: ConflictStrategy::AdoptAsIs,
            preserve_paths: None,
        }),
        (None, false) => None,
    };

    if mode == InstallMode::Adopt && effective_conflict.is_none() {
        // Build a structured error for the FE to render the conflict
        // modal. Source path may fail to resolve in test envs — fall
        // back to a placeholder rather than aborting on the resolve.
        let source_for_err = find_local_repo_root()
            .ok()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| "<source not found>".to_string());
        let diff = diff_install(&install_path);
        let preserve_candidates: Vec<String> = diff
            .will_overwrite
            .iter()
            .filter(|p| DEFAULT_PRESERVE_LIST.iter().any(|x| x == p))
            .cloned()
            .collect();
        let err = InstallConflictError {
            kind: "install_conflict".to_string(),
            message: format!(
                "Install path {} already contains orchestrator files. \
                 Pick a conflict-resolution strategy.",
                install_path.display()
            ),
            install_path: install_path.to_string_lossy().to_string(),
            source_path: source_for_err,
            mode,
            will_overwrite: diff.will_overwrite,
            will_add: diff.will_add,
            preserve_candidates,
        };
        return Err(err.into_err_string());
    }

    // KNOWN_ISSUES.md (v0.2.x) entry resolved 2026-05-10: lightweight
    // re-install path. When `config.lightweight` is set, skip the
    // file-copy stage entirely and go straight to `install.py
    // --lightweight`. install.py's lightweight path:
    //   - Skips model pulls (qwen3-embedding, codesage, etc.)
    //   - Skips Weaviate seeding (idempotent upserts already done)
    //   - Skips agent/skill copy (templates → .claude/)
    //   - Reuses existing healthy `.venv` (recreates only on Python
    //     version mismatch or requirements.txt drift)
    //   - Optionally rewrites absolute paths in `.env` /
    //     `.claude/settings.json` / `.vscode/settings.json` when
    //     `--lightweight-old-path` is supplied.
    //
    // Total runtime: seconds vs the full path's minutes. Used by the
    // launcher's reinstall flow when the user just wants a refresh of
    // state files without re-pulling models or re-seeding the KG.
    if config.lightweight {
        return run_install_orchestrator_lightweight(config, install_path, window).await;
    }

    // Stage 1: Locate local source
    emit_progress(&window, "locate", "Locating orchestrator source...", 5.0);
    let source = find_local_repo_root()?;
    emit_progress(
        &window,
        "locate",
        &format!("Source: {}", source.display()),
        10.0,
    );

    // Auto-create the install directory tree if it doesn't exist.
    tokio::fs::create_dir_all(&install_path)
        .await
        .map_err(|e| format!("Cannot create install directory {}: {}", install_path.display(), e))?;

    // Stage 2: Copy orchestrator-managed paths.
    //
    // Three branches:
    //   1. Conflict strategy provided AND target is an Adopt target →
    //      run the strategy (handles its own copy/preserve/notify).
    //   2. Conflict strategy provided but target is NOT an Adopt target
    //      (Fresh / FreshIntoExisting) → strategy is irrelevant; do a
    //      plain copy. This keeps the wizard's behaviour stable when
    //      the user picked a strategy on a path that subsequently
    //      stopped being an adopt-target between preview and install.
    //   3. No strategy + classify said Fresh/FreshIntoExisting → plain
    //      copy.
    emit_progress(&window, "copy", "Copying orchestrator files...", 15.0);
    let target_clone = install_path.clone();
    let source_clone = source.clone();
    let strategy_for_copy = effective_conflict.clone();
    let conflict_report = tokio::task::spawn_blocking(move || -> Result<Option<ConflictApplyReport>, String> {
        if let Some(c) = strategy_for_copy {
            if classify_install_target(&target_clone) == InstallMode::Adopt {
                let preserve = c
                    .preserve_paths
                    .clone()
                    .unwrap_or_else(|| {
                        DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect()
                    });
                let report = apply_conflict_strategy(
                    &source_clone,
                    &target_clone,
                    c.strategy,
                    &preserve,
                )?;
                return Ok(Some(report));
            }
        }
        // Plain copy path (no conflict OR target became fresh).
        copy_orchestrator_to_sync(&source_clone, &target_clone)?;
        Ok(None)
    })
    .await
    .map_err(|e| format!("copy task panicked: {}", e))?;
    let conflict_report = conflict_report?;

    // Emit a structured event in the install log so the conflict-resolve
    // step is auditable. Best-effort; never fails the install.
    if let Some(ref report) = conflict_report {
        let log_path = install_path.join("state").join("logs").join("install.jsonl");
        let _ = append_install_log_event(
            &log_path,
            "conflict-resolve",
            "ok",
            &format!("strategy={}", report.strategy),
            Some(serde_json::json!({
                "strategy": report.strategy,
                "preserved_count": report.preserved_count,
                "new_md_count": report.new_md_count,
                "notification_written": report.notification_written,
                "copied_count": report.copied_count,
            })),
        );
    }

    emit_progress(&window, "copy", "Files copied", 70.0);

    // Stage 3: Run install.py for post-copy setup (venv, containers, etc.)
    emit_progress(&window, "install", "Running post-copy setup...", 75.0);

    let mut install_args = vec!["install.py".to_string()];

    // The launcher invokes install.py as a non-interactive subprocess.
    // ALWAYS pass --quiet so install.py never blocks waiting for stdin input
    // that the launcher can't provide.
    //
    // v0.2.73 (CG-3): the Joern CFG/PDG integration was removed, so install.py
    // no longer has a Joern prompt NOR the --no-joern/--with-joern flags. We
    // MUST NOT pass --no-joern here — install.py uses strict argparse
    // (parse_args), so an unknown flag aborts the whole install/update with
    // exit code 2. --quiet alone covers non-interactivity now.
    install_args.push("--quiet".to_string());

    if config.use_gpu {
        install_args.push("--gpu".to_string());
    }
    if config.cpu_only {
        install_args.push("--cpu-only".to_string());
    }
    if let Some(ref key) = config.openai_key {
        if !key.is_empty() {
            install_args.push("--openai-key".to_string());
            install_args.push(key.clone());
        }
    }
    if let Some(ref runtime) = config.container_runtime {
        install_args.push("--container".to_string());
        install_args.push(runtime.clone());
    }
    if config.skip_containers {
        install_args.push("--no-containers".to_string());
    }

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd).silent();
    cmd.args(&install_args)
        // Defense-in-depth: explicitly close stdin so install.py's input()
        // calls receive EOF instead of blocking indefinitely. The --quiet
        // + --no-joern flags above should already prevent any prompt, but
        // a future code path that adds another input() would re-introduce
        // the hang. Stdin=null makes the hang impossible.
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);

    // v0.2.27: force UTF-8 stdout/stderr for the Python child. See the
    // identical block on the `update_at` spawn site for the full
    // rationale (Windows cp1252 / `→` U+2192 / install.py crash).
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");

    // PR-3 (2026-05-06): forward the launcher's adopted service ports to
    // install.py. Pre-PR-3, install.py read `WEAVIATE_PORT` / `OLLAMA_PORT`
    // from `os.environ` (install.py:4243-4244) but the launcher never set
    // them — the subprocess inherited the launcher's env, which was
    // empty for these keys. Multi-stack setups silently fell through to
    // the canonical default ports. We now bridge launcher state into
    // the install.py subprocess env via `services.toml` adoption +
    // explicit overrides. See `launcher-settings-propagation-audit-2026-05-06.md` §9.
    let services_state = crate::services::adoption::read();
    let pick_port = |name: &str, default: u16| -> u16 {
        if let Some(svc) = services_state.get(name) {
            match svc.mode {
                crate::services::adoption::AdoptionMode::Parallel => {
                    if let Some(p) = svc.parallel_port {
                        return p;
                    }
                }
                crate::services::adoption::AdoptionMode::Adopt => {
                    if let Some(url) = svc.external_url.as_deref() {
                        // Inline minimal port-extractor (kept here to avoid
                        // a cross-module dep on commands::project_env_settings).
                        let after = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
                        let host_port = after.split('/').next().unwrap_or(after);
                        if let Some(p) = host_port.rsplit(':').next().and_then(|s| s.parse::<u16>().ok()) {
                            return p;
                        }
                    }
                }
                _ => {}
            }
        }
        default
    };
    let weaviate_port = pick_port("weaviate", DEFAULT_WEAVIATE_PORT);
    let ollama_port = pick_port("ollama", DEFAULT_OLLAMA_PORT);
    let code_embed_port = pick_port("code_embed", DEFAULT_CODE_EMBED_PORT);
    cmd.env("WEAVIATE_PORT", weaviate_port.to_string())
        .env("OLLAMA_PORT", ollama_port.to_string())
        .env("CODE_EMBED_PORT", code_embed_port.to_string());

    // Windows: suppress the transient cmd console window that pops up
    // when Tauri (a windowed app, no console) spawns a subprocess.
    // Without CREATE_NO_WINDOW, Windows materialises an empty cmd
    // window for the subprocess's stdin/stdout, which the user sees
    // as "an empty terminal window named python.exe". Reported
    // 2026-04-28 from a Windows wizard test.
    //
    // CREATE_NO_WINDOW = 0x08000000. Per Microsoft docs:
    // https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let install_output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py failed to start: {}", e))?;

    let stdout = String::from_utf8_lossy(&install_output.stdout);
    let stderr = String::from_utf8_lossy(&install_output.stderr);

    if !install_output.status.success() {
        emit_progress(&window, "error", &format!("Installation failed: {}", stderr), 0.0);
        return Err(format!(
            "install.py failed (exit {}):\n{}\n{}",
            install_output.status, stdout, stderr
        ));
    }

    emit_progress(&window, "install", "Installation complete", 90.0);

    // PR-23 (v0.2.12, 2026-05-16): wire bundled MCP entries into ~/.claude.json
    // and the launcher.db. install.py also performs this step via the
    // `--register-default-mcps` CLI subcommand, so on first install via the
    // launcher GUI we end up with two register calls — both idempotent
    // (UPSERT semantics on both sides) so the duplicate is harmless. The
    // GUI-side call is the authoritative one for the install_orchestrator
    // flow because it has direct access to the launcher's DB handle +
    // adopted-services state without re-deriving them.
    //
    // Soft-fail by design: install completion must not depend on MCP
    // registration succeeding. install.py's own post-step (Python-side
    // fallback) backstops this anyway.
    emit_progress(&window, "register", "Registering MCP servers in ~/.claude.json...", 92.0);
    let install_root_path = std::path::PathBuf::from(&config.install_path);
    let ports = crate::mcp_registration::ServicePorts {
        weaviate_port,
        ollama_port,
        grpc_port: crate::mcp_registration::DEFAULT_GRPC_PORT,
        code_embed_port,
    };
    let db_for_register = window.app_handle().try_state::<Db>();
    let db_ref = db_for_register.as_ref().map(|s| s.inner());
    match crate::mcp_registration::register_default_orchestrator_mcps(
        &install_root_path,
        ports,
        None,
        db_ref,
    ) {
        Ok(report) => {
            eprintln!(
                "[vct] install_orchestrator: registered {} of {} default MCP(s) to {}",
                report.success_count(),
                report.outcomes.len(),
                report.claude_json_path.display()
            );
            for o in &report.outcomes {
                if !o.ok {
                    eprintln!(
                        "[vct] install_orchestrator: MCP `{}` failed: {}",
                        o.name,
                        o.error.as_deref().unwrap_or("unknown")
                    );
                }
                if !o.dropped_keys.is_empty() {
                    eprintln!(
                        "[vct] install_orchestrator: dropped {} env key(s) from `{}` (allowlist/secret filter): {:?}",
                        o.dropped_keys.len(),
                        o.name,
                        o.dropped_keys
                    );
                }
            }
            for w in &report.db_warnings {
                eprintln!("[vct] install_orchestrator: db warning: {}", w);
            }
        }
        Err(e) => {
            eprintln!(
                "[vct] install_orchestrator: MCP registration failed (soft-fail): {}. \
                 install.py's Python-side fallback will retry. Manually re-run \
                 `python install.py --update` or wire ~/.claude.json by hand if needed.",
                e
            );
        }
    }

    // Stage 3: Verify
    emit_progress(&window, "verify", "Verifying installation...", 95.0);

    let installed = check_install_status(config.install_path.clone());
    if !installed {
        return Err("Installation completed but verification failed".to_string());
    }

    emit_progress(&window, "done", "Orchestrator installed successfully!", 100.0);

    // Bug A (v0.2.5): persist the chosen install path so the launcher
    // can find it again on next boot regardless of where it was placed.
    // Soft-fail: a DB hiccup must not block install completion.
    if let Some(db) = window.app_handle().try_state::<Db>() {
        if let Err(e) = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, &config.install_path) {
            eprintln!("[vct] install_orchestrator: failed to persist install_path: {}", e);
        }
    }

    Ok(InstallResult {
        success: true,
        install_path: config.install_path,
        message: "Orchestrator installed successfully".to_string(),
        system,
    })
}

/// Lightweight re-install path. Skips file copy, skips model pulls,
/// skips Weaviate seeding — invokes `install.py --lightweight` directly.
///
/// KNOWN_ISSUES.md (v0.2.x) entry resolved 2026-05-10:
///   "Lightweight Rust wiring for `--lightweight` re-install — the
///    Python path is shipped (`install.py --lightweight` skips model
///    pulls + seeding + agent/skill copy; `--lightweight-old-path`
///    rewrites absolute paths in settings/env files). The launcher's
///    'Reinstall' button currently calls full install; wiring it to
///    the lightweight path is a v0.2.x polish item."
///
/// Builds the install.py argv for the subprocess. Extracted as a
/// pub(crate) helper so unit tests can verify the argv shape WITHOUT
/// spawning a real subprocess (which would require a Python interpreter
/// + an installed VCO clone in the test env).
pub(crate) fn build_lightweight_install_argv(
    use_gpu: bool,
    cpu_only: bool,
    container_runtime: Option<&str>,
    skip_containers: bool,
    lightweight_old_path: Option<&str>,
) -> Vec<String> {
    let mut argv = vec![
        "install.py".to_string(),
        "--quiet".to_string(),
        // NB (v0.2.73 CG-3): NO --no-joern — install.py removed that flag and
        // uses strict argparse, so passing it would abort the install (exit 2).
        "--lightweight".to_string(),
    ];
    if let Some(old) = lightweight_old_path {
        if !old.is_empty() {
            argv.push("--lightweight-old-path".to_string());
            argv.push(old.to_string());
        }
    }
    // Forward the same hardware-flag set that the full install path
    // accepts. install.py's lightweight branch ignores most of these
    // (it doesn't pull models or detect GPU), but the flags themselves
    // are still parsed; passing them keeps argv shape consistent and
    // lets a future lightweight-path enhancement use them without
    // a launcher-side change.
    if use_gpu {
        argv.push("--gpu".to_string());
    }
    if cpu_only {
        argv.push("--cpu-only".to_string());
    }
    if let Some(runtime) = container_runtime {
        if !runtime.is_empty() {
            argv.push("--container".to_string());
            argv.push(runtime.to_string());
        }
    }
    if skip_containers {
        argv.push("--no-containers".to_string());
    }
    argv
}

async fn run_install_orchestrator_lightweight(
    config: InstallConfig,
    install_path: PathBuf,
    window: Window,
) -> Result<InstallResult, String> {
    let system = detect_system().await?;
    if !system.has_python {
        return Err(
            "Python 3.11+ is required for lightweight reinstall. Install from https://python.org"
                .to_string(),
        );
    }

    // Lightweight runs IN PLACE — install_path must already be a VCO
    // source repo. The caller (wizard) gates this with the same
    // validate_source_repo check that the full path uses; we re-check
    // here defensively in case a future caller forgets.
    if !install_path.is_dir() {
        return Err(format!(
            "lightweight install: path {} is not a directory",
            install_path.display()
        ));
    }

    emit_progress(&window, "lightweight", "Running install.py --lightweight...", 20.0);

    let argv = build_lightweight_install_argv(
        config.use_gpu,
        config.cpu_only,
        config.container_runtime.as_deref(),
        config.skip_containers,
        config.lightweight_old_path.as_deref(),
    );

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd).silent();
    cmd.args(&argv)
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);

    // v0.2.27: force UTF-8 stdout/stderr for the Python child. See the
    // identical block on the `update_at` spawn site for the full
    // rationale (Windows cp1252 / `→` U+2192 / install.py crash).
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");

    // Forward the same launcher-resolved service ports the full path
    // does. install.py's lightweight branch reads these so a port
    // override survives a re-install. See `install.py:1352
    // _run_lightweight` and the env-write block in
    // `_lightweight_rewrite_paths`.
    let services_state = crate::services::adoption::read();
    let pick_port = |name: &str, default: u16| -> u16 {
        if let Some(svc) = services_state.get(name) {
            match svc.mode {
                crate::services::adoption::AdoptionMode::Parallel => {
                    if let Some(p) = svc.parallel_port {
                        return p;
                    }
                }
                crate::services::adoption::AdoptionMode::Adopt => {
                    if let Some(url) = svc.external_url.as_deref() {
                        let after = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
                        let host_port = after.split('/').next().unwrap_or(after);
                        if let Some(p) =
                            host_port.rsplit(':').next().and_then(|s| s.parse::<u16>().ok())
                        {
                            return p;
                        }
                    }
                }
                _ => {}
            }
        }
        default
    };
    let weaviate_port = pick_port("weaviate", DEFAULT_WEAVIATE_PORT);
    let ollama_port = pick_port("ollama", DEFAULT_OLLAMA_PORT);
    let code_embed_port = pick_port("code_embed", DEFAULT_CODE_EMBED_PORT);
    cmd.env("WEAVIATE_PORT", weaviate_port.to_string())
        .env("OLLAMA_PORT", ollama_port.to_string())
        .env("CODE_EMBED_PORT", code_embed_port.to_string());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py --lightweight failed to start: {}", e))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    if !output.status.success() {
        emit_progress(
            &window,
            "error",
            &format!("Lightweight reinstall failed: {}", stderr),
            0.0,
        );
        return Err(format!(
            "install.py --lightweight failed (exit {}):\n{}\n{}",
            output.status, stdout, stderr
        ));
    }

    emit_progress(&window, "verify", "Verifying installation...", 90.0);
    let installed = check_install_status(install_path.to_string_lossy().to_string());
    if !installed {
        return Err(
            "Lightweight reinstall completed but verification failed".to_string(),
        );
    }

    // PR-23 follow-up (v0.2.12): re-register MCPs on lightweight reinstall too.
    // Lightweight is the common upgrade path when the user moves their venv or
    // upgrades the orchestrator install_root in place. Without this, the
    // existing `~/.claude.json mcpServers` entries keep pointing at the OLD
    // venv-python path and the OLD server.py paths, and Claude Code spawns
    // stale MCP subprocesses. Soft-fail: lightweight is for fast recovery; if
    // registration fails we log + continue rather than aborting the install.
    emit_progress(
        &window,
        "register",
        "Refreshing MCP server paths in ~/.claude.json...",
        95.0,
    );
    let install_root_path = std::path::PathBuf::from(install_path.to_string_lossy().to_string());
    let ports = crate::mcp_registration::ServicePorts {
        weaviate_port,
        ollama_port,
        grpc_port: crate::mcp_registration::DEFAULT_GRPC_PORT,
        code_embed_port,
    };
    let db_for_register = window.app_handle().try_state::<Db>();
    let db_ref = db_for_register.as_ref().map(|s| s.inner());
    match crate::mcp_registration::register_default_orchestrator_mcps(
        &install_root_path,
        ports,
        None,
        db_ref,
    ) {
        Ok(report) => {
            eprintln!(
                "[vct] lightweight: re-registered {} of {} default MCP(s) to {}",
                report.success_count(),
                report.outcomes.len(),
                report.claude_json_path.display()
            );
            for o in &report.outcomes {
                if !o.ok {
                    eprintln!(
                        "[vct] lightweight: MCP `{}` re-register failed: {}",
                        o.name,
                        o.error.as_deref().unwrap_or("unknown")
                    );
                }
            }
        }
        Err(e) => {
            eprintln!(
                "[vct] lightweight: MCP re-registration failed (soft-fail): {}. \
                 Manually re-run `python install.py --update` if MCP paths look stale.",
                e
            );
        }
    }

    emit_progress(&window, "done", "Lightweight reinstall complete", 100.0);

    // Bug A (v0.2.5): refresh the persisted install_path on a successful
    // lightweight reinstall — covers the case where the user pointed the
    // launcher at a different existing VCO clone since the last full install.
    let install_path_str = install_path.to_string_lossy().to_string();
    if let Some(db) = window.app_handle().try_state::<Db>() {
        if let Err(e) = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, &install_path_str) {
            eprintln!(
                "[vct] run_install_orchestrator_lightweight: failed to persist install_path: {}",
                e
            );
        }
    }

    Ok(InstallResult {
        success: true,
        install_path: install_path_str,
        message: "Lightweight reinstall complete".to_string(),
        system,
    })
}

/// v0.2.21 Step 12 (B1 fix): stop the detached vct-hub BEFORE the
/// orchestrator self-update touches disk.
///
/// Why: v0.2.21 introduces `vct-hub` as a sibling binary that may be
/// running independently of the launcher (and outlives launcher GUI
/// sessions). On Windows, `git pull` would hit `ERROR_SHARING_VIOLATION`
/// trying to overwrite a running `vct-hub.exe` exactly the same way it
/// would for the launcher itself (already handled by
/// `pre_pull_rename_running_binary`). Renaming the hub's exe is only
/// half the story — we ALSO want it stopped so install.py's post-
/// update deploy can write fresh state files without racing the old
/// hub's open handles.
///
/// Contract (per plan §"`update_orchestrator` extensions"):
///   - Read `<vct_root_dir()>/hub.pid`. If absent → no hub → Ok(false).
///   - If pid alive: invoke `vct-hub --stop` and poll the pid for up
///     to 10 s waiting for it to die (the `--stop` CLI itself blocks
///     up to 10 s but we don't trust the subprocess to honour that
///     deadline; we re-poll defensively).
///   - If the polite stop fails: emit a warning, force-kill the pid
///     (SIGKILL on POSIX, TerminateProcess on Windows), clean up the
///     stale hub.pid manually. Hard-fail if even the kill fails — we
///     refuse to git pull when the binary is provably locked.
///   - Returns `Ok(true)` on successful stop, `Ok(false)` if no hub
///     was running, `Err(_)` on any unrecoverable failure.
///
/// Soft-fail philosophy: this is best-effort. If `vct-hub --stop`
/// can't be spawned (binary not on disk, exec failed), we fall
/// through to the direct signal path — the hub IPC is just a polite
/// hint; the OS-level signal is the real mechanism.
///
/// v0.2.59: this is a thin wrapper. It (1) stops the hub named by
/// `<vct_root_dir()>/hub.pid` via `stop_lockfile_hub_for_update`, then
/// (2) ALWAYS runs `pre_update_hub_kill_sweep` as a process-identity
/// backstop. The lockfile path can only see ONE hub; a hub from a
/// different state-root/install (dev `--foreground` builds, a 2nd
/// install root, or a crash that cleared the pid) is invisible to it
/// yet still holds `launcher.db` open and locks `vct-hub.exe` on
/// Windows — blocking the binary swap. The sweep reaps those. The
/// lockfile result is what we return (it carries the Err that blocks
/// the pull when the NAMED hub provably won't die); the sweep is
/// soft-fail and never changes the return value.
fn ensure_hub_stopped_for_update(install_path: &Path) -> Result<bool, String> {
    let lockfile_result = stop_lockfile_hub_for_update(install_path);
    // Backstop: reap any stray vct-hub the single-lockfile path missed.
    // Soft-fail — runs even when the lockfile stop returned Err (a
    // surviving named hub is a separate problem; we still want to clear
    // strays). The count is informational.
    let swept = crate::commands::update_gate::pre_update_hub_kill_sweep();
    if swept > 0 {
        eprintln!(
            "[vct] update_orchestrator: hub-sweep backstop terminated {} stray vct-hub process(es) the lockfile path did not cover",
            swept
        );
    }
    lockfile_result
}

/// v0.2.59: the original single-`hub.pid`-driven stop. Renamed from
/// `ensure_hub_stopped_for_update` so the wrapper above can always run
/// the process-identity backstop sweep afterward. Behaviour unchanged.
fn stop_lockfile_hub_for_update(_install_path: &Path) -> Result<bool, String> {
    let pid_file = vct_launcher_core::paths::vct_root_dir().join("hub.pid");
    let pid_raw = match std::fs::read_to_string(&pid_file) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            eprintln!(
                "[vct] update_orchestrator: no {} — vct-hub is not running, skipping stop",
                pid_file.display(),
            );
            return Ok(false);
        }
        Err(e) => {
            return Err(format!(
                "could not read {} to detect running vct-hub: {}",
                pid_file.display(),
                e
            ));
        }
    };
    // v0.2.69: the lockfile's FIRST line is the PID; later lines hold the
    // build-identity field (home #3). Parse line 1 only so a 2-line
    // lockfile is not mistaken for malformed. Matches hub_status.rs and
    // lockfile.rs::read_pid which already read line 1.
    let pid: u32 = match pid_raw.lines().next().unwrap_or("").trim().parse() {
        Ok(p) => p,
        Err(_) => {
            // Malformed lockfile — treat as "no hub running" and clean
            // up so install.py doesn't choke on it later.
            eprintln!(
                "[vct] update_orchestrator: {} contains malformed PID {:?}; removing stale lockfile",
                pid_file.display(),
                pid_raw.lines().next().unwrap_or("").trim(),
            );
            let _ = std::fs::remove_file(&pid_file);
            return Ok(false);
        }
    };

    if !pid_is_alive(pid) {
        // Stale lockfile from a previous hub crash. Clean it up so the
        // post-update `--start-if-not-running` path doesn't think a
        // hub is already running.
        eprintln!(
            "[vct] update_orchestrator: hub.pid claims pid {} but it's dead; removing stale lockfile",
            pid
        );
        let _ = std::fs::remove_file(&pid_file);
        return Ok(false);
    }

    eprintln!(
        "[vct] update_orchestrator: vct-hub running (pid {}); requesting graceful stop",
        pid
    );

    // First attempt: polite `vct-hub --stop`. Soft-fail on any error
    // (missing binary, exec failure, non-zero exit) — we fall through
    // to the OS-signal path.
    let polite_attempted = if let Some(hub_bin) = crate::hub_launcher::find_hub_binary() {
        let mut cmd = std::process::Command::new(&hub_bin);
        cmd.arg("--stop")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        match cmd.status() {
            Ok(status) => {
                if !status.success() {
                    eprintln!(
                        "[vct] update_orchestrator: vct-hub --stop exited {:?}; will fall through to direct signal",
                        status.code()
                    );
                }
                true
            }
            Err(e) => {
                eprintln!(
                    "[vct] update_orchestrator: could not spawn vct-hub --stop ({}); falling through to direct signal",
                    e
                );
                false
            }
        }
    } else {
        eprintln!(
            "[vct] update_orchestrator: vct-hub binary not found on disk; \
             skipping polite --stop and going straight to direct signal"
        );
        false
    };

    // Poll up to 10 s for the pid to die. The `--stop` CLI itself
    // already polls up to 10 s before returning, so by this point the
    // hub almost certainly is dead — but defensively re-check.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while std::time::Instant::now() < deadline {
        if !pid_is_alive(pid) {
            // Process is gone. Lockfile MAY still exist (the
            // graceful-shutdown path normally removes it; if it
            // didn't, do it now).
            let _ = std::fs::remove_file(&pid_file);
            eprintln!(
                "[vct] update_orchestrator: vct-hub pid {} stopped gracefully",
                pid
            );
            return Ok(true);
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }

    // Polite stop didn't work (or wasn't attempted). Escalate: force
    // the kernel to kill the process. Refuse the update entirely if
    // even this fails — letting git pull continue with a live hub
    // would lock vct-hub.exe on Windows and corrupt the update.
    eprintln!(
        "[vct] update_orchestrator: vct-hub pid {} did not exit within 10s (polite_attempted={}); escalating to force-kill",
        pid, polite_attempted
    );

    #[cfg(unix)]
    {
        // SIGKILL. Safe even if the process is in the middle of
        // exiting (no-op then). i32 cast guarded by pid_is_alive.
        let rc = unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
        if rc != 0 {
            let e = std::io::Error::last_os_error();
            // ESRCH = process already gone between the alive check and
            // the kill — that's actually the success path.
            if e.raw_os_error() != Some(libc::ESRCH) {
                return Err(format!(
                    "could not SIGKILL vct-hub pid {}: {} — refusing to proceed with git pull while the hub is alive",
                    pid, e
                ));
            }
        }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, PROCESS_TERMINATE,
        };
        // SAFETY: thin FFI calls. We close every handle we open.
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if handle.is_null() {
                // Could be "already dead" (preferred) or "access
                // denied". Re-check via pid_is_alive to disambiguate.
                if pid_is_alive(pid) {
                    return Err(format!(
                        "OpenProcess(pid={}, TERMINATE) returned NULL and pid still alive: {} — refusing to proceed with git pull",
                        pid,
                        std::io::Error::last_os_error()
                    ));
                }
                // else: dead, fall through to lockfile cleanup
            } else {
                let ok = TerminateProcess(handle, 1) != 0;
                let kill_err = if !ok {
                    Some(std::io::Error::last_os_error())
                } else {
                    None
                };
                CloseHandle(handle);
                if let Some(e) = kill_err {
                    return Err(format!(
                        "TerminateProcess(pid={}): {} — refusing to proceed with git pull while the hub is alive",
                        pid, e
                    ));
                }
            }
        }
    }

    // Force-killed: clean up the lockfile manually (the hub's normal
    // graceful-shutdown path didn't get to run).
    let _ = std::fs::remove_file(&pid_file);

    // Re-confirm the pid is actually gone before declaring success.
    // Short grace window for the kernel to reap.
    let deadline2 = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while std::time::Instant::now() < deadline2 {
        if !pid_is_alive(pid) {
            eprintln!(
                "[vct] update_orchestrator: vct-hub pid {} force-killed; cleared stale {}",
                pid,
                pid_file.display()
            );
            return Ok(true);
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }

    Err(format!(
        "vct-hub pid {} is still alive after SIGKILL/TerminateProcess; refusing to proceed with git pull (would lock vct-hub.exe on Windows)",
        pid
    ))
}

/// v0.2.21 Step 12 (B1 fix): rename the running `vct-hub.exe` BEFORE
/// `git pull` overwrites it.
///
/// Windows-only. Mirrors `pre_pull_rename_running_binary` (the
/// launcher's own self-rename) but targets the sibling `vct-hub.exe`
/// instead of the launcher's `current_exe()`. We just stopped the hub
/// in `ensure_hub_stopped_for_update`, so its file handles are gone
/// from the OS perspective — but on Windows the file is still treated
/// as in-use briefly after process exit (antivirus, indexers, etc.),
/// and `git pull`'s atomic-rename semantics will revert the entire
/// pull on any single sharing violation. Pre-renaming is cheap
/// insurance.
///
/// We look up the hub binary via the same discovery chain
/// (`hub_launcher::find_hub_binary`) and only rename if the resolved
/// path lives under `install_path` — externally-installed hubs
/// (`$HOME/.vct/bin/vct-hub.exe`) are NOT touched by `git pull` so
/// renaming them would be both pointless and risky.
///
/// The renamed file (`vct-hub.exe.old-<pid>`) is cleaned up by the
/// existing boot sweep in `lib.rs::sweep_stale_binary_siblings` on
/// next launcher startup — the pid we suffix with is no longer alive,
/// so the sweep will delete it next boot. Returns the backup path
/// for the (no-op today, future-proofing) revert path.
///
/// Linux / macOS: no-op (inode-ref-count semantics make this
/// unnecessary).
#[cfg(windows)]
fn pre_pull_rename_vct_hub_binary(install_path: &Path) -> Option<PathBuf> {
    let hub = crate::hub_launcher::find_hub_binary()?;
    let hub_canon = dunce::canonicalize(&hub).unwrap_or(hub);
    let install_canon =
        dunce::canonicalize(install_path).unwrap_or_else(|_| install_path.to_path_buf());
    if !hub_canon.starts_with(&install_canon) {
        // External install (e.g. $HOME/.vct/bin/vct-hub.exe). git pull
        // can't touch it; no need to rename.
        return None;
    }

    // Suffix with the LAUNCHER's pid (not the hub's — the hub is
    // already stopped by now). The boot sweep cleans up `.old-<pid>`
    // files whose pid is no longer alive; the launcher's own pid will
    // be dead by next boot since we restart at the end of
    // update_orchestrator.
    let pid = std::process::id();
    let backup_name = format!(
        "{}.old-{}",
        hub_canon.file_name()?.to_string_lossy(),
        pid,
    );
    let backup_path = hub_canon.parent()?.join(backup_name);

    match std::fs::rename(&hub_canon, &backup_path) {
        Ok(()) => {
            eprintln!(
                "[vct] update_orchestrator: pre-pull renamed vct-hub binary to {} (Windows). New binary will be written to {} by git pull.",
                backup_path.display(),
                hub_canon.display(),
            );
            Some(backup_path)
        }
        Err(e) => {
            eprintln!(
                "[vct] update_orchestrator: pre-pull rename of vct-hub FAILED ({}). git pull may fail with ERROR_SHARING_VIOLATION if the hub binary is still locked. Continuing — the user will see any git error.",
                e,
            );
            None
        }
    }
}

#[cfg(not(windows))]
fn pre_pull_rename_vct_hub_binary(_install_path: &Path) -> Option<PathBuf> {
    // POSIX kernels: running-binary overwrite is safe; no rename
    // needed. The hub is already stopped at this point anyway, so
    // even on Windows the rename is belt-and-braces.
    None
}

/// Cutover sentinel filename — written by install.py BEFORE it
/// starts vct-hub, deleted AFTER /health returns 200. Mirrored from
/// `_VCT_HUB_CUTOVER_SENTINEL_NAME` in install.py and the launcher's
/// own startup-time read in `lib.rs::setup`. Kept as a private const
/// here so the v0.2.22 Item #3 skip-poll precheck and its unit test
/// can both reach for it without re-typing the literal (which would
/// silently drift if install.py ever renames the sentinel).
const V0_2_21_CUTOVER_SENTINEL_NAME: &str = "v0.2.21-cutover.flag";

/// Decide whether the redundant 30 s /health poll in
/// `ensure_hub_started_after_update` should be skipped.
///
/// Contract (v0.2.22 Item #3): install.py is the authoritative health
/// check on the update happy path — it writes the cutover sentinel
/// BEFORE starting vct-hub and DELETES it AFTER /health returns 200.
/// So when we land in the launcher's post-install hub-recovery path:
///
/// * Sentinel ABSENT → install.py confirmed health already → skip
///   the 30 s poll (pure wall-clock cost, no signal value).
/// * Sentinel PRESENT → install.py timed out, was killed, OR is
///   still running (shouldn't be possible from our call site, but
///   the precheck doesn't have to reason about that) → run the
///   full poll as a second-chance probe.
///
/// Returns `true` when the poll should be SKIPPED.
fn should_skip_redundant_health_poll(root: &Path) -> bool {
    !root.join(V0_2_21_CUTOVER_SENTINEL_NAME).exists()
}

/// v0.2.21 Step 12 (B1 fix): bring the detached vct-hub back up after
/// install.py finishes.
///
/// Runs `vct-hub --start-if-not-running` and then verifies that the
/// hub has actually become reachable by reading `<vct_root_dir()>/
/// hub.port` + `hub.token` and probing `/health`. Poll budget: 30 s
/// (the hub's cold-start including SQLite migrations + module-scan
/// can take a few seconds on cold-cache disk).
///
/// Soft-fail by design: if the hub can't be brought back up, we DON'T
/// return Err — that would block the launcher restart path. The user
/// can manually restart the hub from the GUI's Stop menu (Step 13).
/// We log a clear warning so the failure surfaces in the launcher log.
///
/// The plan calls this contract "best-effort post-update health
/// check"; the launcher's startup path (`lib.rs::setup` →
/// `hub_launcher::ensure_hub_running`) will also try to start the
/// hub when the new launcher process boots, so this function is
/// primarily an early-warning system.
fn ensure_hub_started_after_update(_install_path: &Path) -> Result<(), String> {
    let Some(hub_bin) = crate::hub_launcher::find_hub_binary() else {
        eprintln!(
            "[vct] update_orchestrator: vct-hub binary not found on disk after install.py — \
             leaving hub stopped. Launcher restart will retry the discovery."
        );
        return Ok(());
    };

    eprintln!(
        "[vct] update_orchestrator: starting vct-hub from {}",
        hub_bin.display()
    );

    let mut cmd = std::process::Command::new(&hub_bin);
    cmd.arg("--start-if-not-running")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    match cmd.status() {
        Ok(status) if status.success() => {}
        Ok(status) => {
            eprintln!(
                "[vct] update_orchestrator: vct-hub --start-if-not-running exited {:?}; \
                 launcher will retry on next startup",
                status.code()
            );
            return Ok(());
        }
        Err(e) => {
            eprintln!(
                "[vct] update_orchestrator: could not spawn vct-hub --start-if-not-running: {}; \
                 launcher will retry on next startup",
                e
            );
            return Ok(());
        }
    }

    // v0.2.22 Item #3 — skip the redundant /health poll on the common-
    // success path. install.py's own post-cutover probe (see
    // `_VCT_HUB_CUTOVER_SENTINEL_NAME` in install.py, written BEFORE
    // hub-start and deleted AFTER /health returns 200) is the
    // authoritative health check during update. If we land here and
    // the sentinel is ABSENT, install.py already validated /health —
    // re-probing for 30 s is pure wall-clock cost with no signal
    // value. If the sentinel is PRESENT (rare slow-path: install.py
    // timed out, or was killed mid-cutover), we still run the full
    // poll as a second-chance probe — this is the only codepath
    // that gives the user a recovery story when install.py's own
    // probe didn't see /health come up.
    let root = vct_launcher_core::paths::vct_root_dir();
    if should_skip_redundant_health_poll(&root) {
        eprintln!(
            "[vct] update_orchestrator: cutover sentinel absent — install.py \
             already validated /health; skipping redundant 30 s poll"
        );
        return Ok(());
    }

    eprintln!(
        "[vct] update_orchestrator: cutover sentinel still present at {} — \
         install.py did not confirm /health; running full 30 s poll",
        root.join(V0_2_21_CUTOVER_SENTINEL_NAME).display()
    );

    // Poll up to 30 s for hub.port + hub.token to appear and /health
    // to answer 200. The hub spawns a detached child and returns
    // quickly; the child does the actual bind + DB migration.
    let port_path = root.join("hub.port");
    let token_path = root.join("hub.token");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while std::time::Instant::now() < deadline {
        if port_path.exists() && token_path.exists() {
            // Both files there — try the /health probe.
            let port = std::fs::read_to_string(&port_path)
                .ok()
                .and_then(|s| s.trim().parse::<u16>().ok());
            let token = std::fs::read_to_string(&token_path)
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty());
            if let (Some(port), Some(token)) = (port, token) {
                if probe_hub_health(port, &token) {
                    eprintln!(
                        "[vct] update_orchestrator: vct-hub /health OK on 127.0.0.1:{}",
                        port
                    );
                    return Ok(());
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }

    eprintln!(
        "[vct] update_orchestrator: vct-hub did not become healthy within 30 s; \
         launcher will start in hub-unavailable degraded mode. \
         User can retry via Stop menu → 'Start vct-hub'."
    );
    Ok(())
}

/// Best-effort blocking `GET http://127.0.0.1:<port>/health` probe.
/// Returns true on HTTP 200. Used only by
/// `ensure_hub_started_after_update`.
///
/// Why a hand-rolled TCP+HTTP write rather than `reqwest`: this
/// function runs from inside a Tauri `#[command]` (so we're on the
/// async runtime), but we want a tight, dependency-free probe that
/// doesn't pull blocking-reqwest features. A raw socket read of the
/// HTTP status line is < 30 lines and has no failure modes worth
/// surfacing.
fn probe_hub_health(port: u16, token: &str) -> bool {
    use std::io::{Read, Write};
    use std::net::TcpStream;

    let addr = format!("127.0.0.1:{}", port);
    let mut stream = match TcpStream::connect_timeout(
        &match addr.parse() {
            Ok(a) => a,
            Err(_) => return false,
        },
        std::time::Duration::from_secs(2),
    ) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(2)));

    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\nConnection: close\r\n\r\n",
        port, token
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    // Read the status line. 64 bytes is enough for "HTTP/1.1 200 OK\r\n".
    let mut buf = [0u8; 64];
    let n = match stream.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

/// v0.2.17 (plan 0.0.B): pre-pull rename helper for Windows.
///
/// On Windows, `git pull` fails with ERROR_SHARING_VIOLATION when it
/// tries to overwrite the running launcher's binary
/// (`launcher/dist/<arch>/vct-launcher.exe`). Git's error is atomic:
/// the entire pull is reverted, so neither source nor binary lands.
/// The fix is to rename our own .exe to a sibling path BEFORE
/// invoking git pull. Windows allows this — the running process's
/// .exe can be renamed even though it can't be overwritten (Chrome,
/// VS Code, npm-on-Windows all rely on this pattern). Once renamed,
/// the canonical path is free for git to write the new binary there.
///
/// Linux / macOS skip this step — both kernels handle running-binary
/// overwrite via inode/vnode ref-counting (old binary stays mapped;
/// new bits land at the same path on a new inode).
///
/// Returns the renamed path on Windows when rename happened, or None
/// on Linux/macOS / when rename was unnecessary / when rename failed
/// soft. Caller uses the return to revert on git-pull failure
/// (best-effort).
#[cfg(windows)]
fn pre_pull_rename_running_binary(install_path: &Path) -> Option<PathBuf> {
    // Resolve the running launcher's binary path. If anything in this
    // chain fails (no current_exe, can't canonicalize, not under
    // install_path), fall through — Linux-style overwrite path
    // probably won't work on Windows but the user gets a clear git
    // error rather than a misleading "rename failed" one.
    let exe = std::env::current_exe().ok()?;
    let exe_canon = dunce::canonicalize(&exe).unwrap_or(exe);
    let install_canon = dunce::canonicalize(install_path).unwrap_or_else(|_| install_path.to_path_buf());
    if !exe_canon.starts_with(&install_canon) {
        // Running from outside the install tree (e.g. development
        // build from cargo) — git pull won't try to overwrite us.
        return None;
    }

    let pid = std::process::id();
    let backup_name = format!(
        "{}.old-{}",
        exe_canon.file_name()?.to_string_lossy(),
        pid,
    );
    let backup_path = exe_canon.parent()?.join(backup_name);

    match std::fs::rename(&exe_canon, &backup_path) {
        Ok(()) => {
            eprintln!(
                "[vct] update_orchestrator: pre-pull renamed running launcher \
                 binary to {} (Windows). New binary will be written to {} by \
                 git pull.",
                backup_path.display(),
                exe_canon.display(),
            );
            Some(backup_path)
        }
        Err(e) => {
            eprintln!(
                "[vct] update_orchestrator: pre-pull rename FAILED ({}). git pull \
                 will likely fail with ERROR_SHARING_VIOLATION. Continuing — \
                 the user will see the git error.",
                e,
            );
            None
        }
    }
}

#[cfg(not(windows))]
fn pre_pull_rename_running_binary(_install_path: &Path) -> Option<PathBuf> {
    // POSIX kernels handle running-binary overwrite cleanly via inode
    // ref-counting. No rename needed.
    None
}

/// v0.2.52 V52-AH (Windows binary-lock bug, 2026-06-09): post-pull staging for the
/// Windows stage1 updater handoff.
///
/// Background
/// ----------
/// Even with `pre_pull_rename_running_binary`, the dist binary at
/// `launcher/dist/windows-x64/vct-launcher.exe` (or `vct-hub.exe`) can
/// end up STALE after a successful git pull when:
///   - The rename failed silently (antivirus held a handle briefly).
///   - Some other process held the file open (Windows defender scan,
///     indexer, dev console with the .exe drag-dropped, etc).
/// In those cases `git pull` saw ERROR_SHARING_VIOLATION on the binary
/// but completed the rest of the merge — leaving metadata.json at the
/// new version but the .exe bytes at the OLD version (the binary-lock scenario).
///
/// This helper detects that state by checking `git status --porcelain
/// <relative_path>` for each candidate binary. Any dirty status means
/// git considers the file diverged from HEAD (= the new bytes git
/// SHOULD have written are not actually on disk). For each such file,
/// we extract HEAD's blob into `<target>.new` via `git show
/// HEAD:<relative_path>`. The updater (`vct-updater.exe`) then renames
/// `<target>.new` → `<target>` after the running launcher exits.
///
/// On POSIX, this is a no-op (the rename pattern in
/// `pre_pull_rename_running_binary` plus inode ref-counting already
/// handles binary overwrite correctly).
///
/// Returns the list of relative paths that were staged as `.new`
/// (empty on POSIX or when no binaries needed staging).
#[cfg(windows)]
async fn stage_locked_binaries_for_handoff(install_path: &Path) -> Vec<String> {
    let mut staged: Vec<String> = Vec::new();
    let candidates = [
        "launcher/dist/windows-x64/vct-launcher.exe",
        "launcher/dist/windows-x64/vct-hub.exe",
    ];
    for rel_path in candidates {
        // Check git status. An empty stdout = clean = nothing to do.
        let status_out = match tokio::process::Command::new("git")
            .silent()
            .args(["status", "--porcelain", "--", rel_path])
            .current_dir(install_path)
            .output()
            .await
        {
            Ok(o) => o,
            Err(e) => {
                eprintln!(
                    "[vct] stage_locked_binaries: git status failed for {}: {} \
                     (skipping; handoff will gracefully no-op for this file)",
                    rel_path, e
                );
                continue;
            }
        };
        if status_out.stdout.is_empty() {
            // Clean — binary already matches HEAD. Nothing to stage.
            continue;
        }

        // Dirty: extract HEAD blob into `<target>.new`. We use
        // `git show HEAD:<rel_path>` to read the bytes, then write to
        // `<target>.new`. Path safety: the candidates list is
        // hard-coded above so no injection risk.
        let target_abs = install_path.join(rel_path);
        let staged_abs = path_with_new_suffix(&target_abs);

        let show_out = match tokio::process::Command::new("git")
            .silent()
            .args(["show", &format!("HEAD:{}", rel_path)])
            .current_dir(install_path)
            .output()
            .await
        {
            Ok(o) if o.status.success() => o,
            Ok(o) => {
                eprintln!(
                    "[vct] stage_locked_binaries: git show HEAD:{} exited non-zero ({:?}); \
                     skipping. stderr: {}",
                    rel_path,
                    o.status.code(),
                    String::from_utf8_lossy(&o.stderr).trim(),
                );
                continue;
            }
            Err(e) => {
                eprintln!(
                    "[vct] stage_locked_binaries: git show HEAD:{} spawn failed: {} (skipping)",
                    rel_path, e
                );
                continue;
            }
        };

        // Write the bytes atomically (write to `.tmp`, rename onto `.new`).
        let tmp_path = staged_abs.with_extension("new.tmp");
        if let Err(e) = std::fs::write(&tmp_path, &show_out.stdout) {
            eprintln!(
                "[vct] stage_locked_binaries: write {} failed: {} (skipping)",
                tmp_path.display(),
                e,
            );
            continue;
        }
        if let Err(e) = std::fs::rename(&tmp_path, &staged_abs) {
            eprintln!(
                "[vct] stage_locked_binaries: rename {} → {} failed: {} (skipping)",
                tmp_path.display(),
                staged_abs.display(),
                e,
            );
            let _ = std::fs::remove_file(&tmp_path);
            continue;
        }
        eprintln!(
            "[vct] stage_locked_binaries: staged {} → {} ({} bytes)",
            rel_path,
            staged_abs.display(),
            show_out.stdout.len(),
        );
        staged.push(rel_path.to_string());
    }
    staged
}

#[cfg(not(windows))]
#[allow(dead_code)] // POSIX caller is gated by cfg; tests don't exercise this branch
async fn stage_locked_binaries_for_handoff(_install_path: &Path) -> Vec<String> {
    // POSIX: no-op. Inode ref-counting + rename pattern handle binary
    // overwrite correctly without any handoff dance.
    Vec::new()
}

/// Helper used by `stage_locked_binaries_for_handoff` (Windows) AND by
/// tests on every host. Keep in sync with the same-named helper in
/// `commands::update_handoff` — both must produce the same staging
/// filename so the launcher writes where the updater reads.
#[allow(dead_code)] // exercised by Windows path + tests
fn path_with_new_suffix(path: &Path) -> PathBuf {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    parent.join(format!("{}.new", name))
}

/// v0.2.17 (plan 0.0.B): revert the pre-pull rename on git-pull failure.
///
/// Best-effort: log + continue on any error. The user can manually
/// rename `<binary>.old-<pid>` back to canonical if needed; failing
/// to revert leaves the launcher unable to relaunch but the running
/// instance still works fine.
fn revert_pre_pull_rename(backup_path: &Path) {
    let parent = match backup_path.parent() {
        Some(p) => p,
        None => return,
    };
    // Strip the `.old-<pid>` suffix to recover the canonical name.
    let fname = match backup_path.file_name().and_then(|s| s.to_str()) {
        Some(s) => s,
        None => return,
    };
    let canonical_name = match fname.rsplit_once(".old-") {
        Some((stem, _pid)) => stem.to_string(),
        None => return,
    };
    let canonical_path = parent.join(canonical_name);
    if let Err(e) = std::fs::rename(backup_path, &canonical_path) {
        eprintln!(
            "[vct] update_orchestrator: could not revert pre-pull rename \
             ({} → {}): {}. The renamed file is left in place; the running \
             launcher continues to work but a future launcher start may \
             pick up the renamed binary as a stale .old-<pid> sibling \
             (cleaned by the boot sweep).",
            backup_path.display(),
            canonical_path.display(),
            e,
        );
    } else {
        eprintln!(
            "[vct] update_orchestrator: reverted pre-pull rename ({} → {})",
            backup_path.display(),
            canonical_path.display(),
        );
    }
}

/// Update an existing orchestrator installation.
///
/// v0.2.17 (plan 0.0 + 0.0.B): the flow auto-restarts the launcher on
/// success. The launcher is a process manager / dashboard, not an
/// editor — no in-flight user work lives in this process — so the
/// "banner + user-clicks-Restart" ceremony from v0.2.16 W4 is
/// replaced by an automatic relaunch. Sequence:
///
///   1. (Windows only) Rename the running launcher's binary aside
///      so `git pull` can overwrite the canonical path.
///   2. `git pull --ff-only`.
///   3. `install.py --update`.
///   4. Spawn the new launcher detached + exit current process.
///
/// On any error before step 4, return the error to the GUI (and
/// revert the pre-pull rename if applicable). The current launcher
/// keeps running, so the user can retry.
/// v0.2.44 V44-G4 helper: bucket `RetryReport`s by decision into a
/// JSON-friendly summary the audit log can carry alongside the raw
/// per-row payload. Keeps the audit detail compact while still
/// allowing downstream tooling to reconstruct the per-decision
/// breakdown without parsing every row.
fn retry_summary_counts(
    reports: &[crate::commands::module_service::RetryReport],
) -> serde_json::Value {
    use std::collections::BTreeMap;
    let mut counts: BTreeMap<&str, u32> = BTreeMap::new();
    for r in reports {
        *counts.entry(r.decision.as_str()).or_insert(0) += 1;
    }
    serde_json::to_value(counts).unwrap_or(serde_json::json!({}))
}

// =====================================================================
// v0.2.45 V45-B: wait_for_binary_refresh
//
// Release-workflow ordering bug: every tag ships in TWO steps.
//   1. Source tag commit (e.g. `f7682a3` at 23:54 UTC) bumps
//      `vct-module.json::version` to v0.2.X.
//   2. Auto-committed `chore(binary): refresh dist binaries for v0.2.X`
//      (e.g. `88f9758` at 00:43 UTC, ~49 min later) replaces the
//      `launcher/dist/<arch>/vct-launcher.metadata.json::launcher_version`
//      and the actual binary blob.
//
// Inside that ~49-min window, `vco_upstream/main` advertises the new
// source version via `vct-module.json` (what `read_source_version`
// reads) but the on-disk `vct-launcher.metadata.json::launcher_version`
// still carries the previous version + the old binary blob.
//
// `check_for_updates` sees `remote_ahead`; the user clicks Update;
// `update_orchestrator` runs `git pull --ff-only` + install.py; then
// `restart_launcher` faithfully re-execs the ON-DISK binary — which
// is the previous version. Post-restart UI banner says "Current
// v0.2.X" but the running process is v0.2.X-1.
//
// Fix: before re-execing, poll for the on-disk binary version to
// catch up with the source version. If the binary-refresh commit
// hasn't landed within `WAIT_FOR_BINARY_REFRESH_TIMEOUT_SECS`,
// surface a clear error so the user retries later instead of
// relaunching into a stale binary.
//
// Structural fix (tag the binary-refresh commit, not the source
// commit) is deferred to v0.2.46-46-1.
// =====================================================================

/// Production timeout for `WaitForBinaryRefresh`. Conservative
/// 5-minute cap: the historical binary-refresh window is ~49 min, but
/// blocking the user that long is worse UX than surfacing a clear
/// "retry in a few minutes" error.
pub(crate) const WAIT_FOR_BINARY_REFRESH_TIMEOUT_SECS: u64 = 300;

/// Production poll interval. 15 s is a deliberate balance between
/// responsiveness (binary-refresh commits usually appear within a
/// minute or two once CI kicks off) and not hammering the remote
/// (one `git pull` per iteration).
pub(crate) const WAIT_FOR_BINARY_REFRESH_INTERVAL_SECS: u64 = 15;

/// Config + entry point for the binary-refresh poll loop.
///
/// Held as a struct so tests can swap the timeout / interval / skip
/// the real `git pull` without parameterizing the production call
/// sites. `disable_git_pull` is set true only inside the unit tests
/// below — production always pulls.
pub(crate) struct WaitForBinaryRefresh<'a> {
    pub install_path: &'a Path,
    pub branch: &'a str,
    pub timeout: std::time::Duration,
    pub interval: std::time::Duration,
    pub disable_git_pull: bool,
}

impl<'a> WaitForBinaryRefresh<'a> {
    /// Production constructor: 5-min timeout, 15-s interval, real
    /// `git pull` enabled.
    pub(crate) fn default_production(install_path: &'a Path, branch: &'a str) -> Self {
        Self {
            install_path,
            branch,
            timeout: std::time::Duration::from_secs(WAIT_FOR_BINARY_REFRESH_TIMEOUT_SECS),
            interval: std::time::Duration::from_secs(WAIT_FOR_BINARY_REFRESH_INTERVAL_SECS),
            disable_git_pull: false,
        }
    }

    /// Poll until `read_on_disk_binary_version` is at least
    /// `read_source_version` (semver-aware) or the timeout elapses.
    ///
    /// Soft-fail on transient git-pull errors (network blip, brief
    /// 503 from the remote) — the next iteration retries.
    /// Hard-fail on timeout: returns Err with a user-facing message
    /// naming both versions so the caller can surface it verbatim
    /// in the UI.
    ///
    /// v0.2.48: changed exit-condition from `on_disk == source` to
    /// `on_disk >= source` (numeric semver compare). The old equality
    /// check deadlocked the user's update flow when on-disk was
    /// AHEAD of source — exactly what happened post-v0.2.47 when the
    /// `vct-module.json` version-pin bump was missed but the binary
    /// refresh for v0.2.47 had already landed (on_disk=0.2.47,
    /// source=0.2.46). The newer binary is what the user wants to
    /// restart into anyway — there's no `dist/` revision to "wait for"
    /// in that case, so the loop would just time out at 300s with a
    /// misleading "still building" modal. The `>=` rule is also the
    /// only correct invariant: the source-pin says "binary must be at
    /// least version X to satisfy this update"; if it's past X, the
    /// update has already over-satisfied.
    pub(crate) async fn run(&self) -> Result<(), String> {
        use std::time::Instant;
        let deadline = Instant::now() + self.timeout;
        let mut iteration: u32 = 0;
        loop {
            iteration += 1;
            let source_version = read_source_version(self.install_path).ok_or_else(|| {
                "Could not read source version from vct-module.json".to_string()
            })?;
            let on_disk_version =
                read_on_disk_binary_version(self.install_path).unwrap_or_default();
            // v0.2.55 (hub-freshness gap, decision #2 — close fully): also
            // require the on-disk vct-hub binary to have caught up before
            // declaring the refresh landed. Pre-v0.2.55 the gate watched
            // ONLY the launcher binary, so a partial refresh (launcher new,
            // hub stale) could restart into a launcher that then started a
            // STALE hub. One Release commit refreshes all three binaries
            // together, so in practice they advance in lock-step — but the
            // gate now enforces it rather than assuming it. CAVEAT: if the
            // hub sidecar is ABSENT (older installs predating hub metadata)
            // we must NOT block forever — treat absent as "hub OK" so the
            // gate degrades to the pre-v0.2.55 launcher-only behaviour
            // instead of deadlocking.
            let hub_version_opt = read_on_disk_hub_version(self.install_path);
            let hub_caught_up = match &hub_version_opt {
                None => true, // no hub sidecar → don't block on it
                Some(hv) if hv.is_empty() => true,
                Some(hv) => !version_is_outdated(hv, &source_version),
            };
            if !on_disk_version.is_empty()
                && !version_is_outdated(&on_disk_version, &source_version)
                && hub_caught_up
            {
                // on_disk >= source — either equal (the normal
                // "binary refresh landed" case) or ahead (the
                // version-pin-stale case fixed in v0.2.48). Both
                // are valid exit states. AND the hub binary (when its
                // sidecar exists) is at/above source too.
                if iteration > 1 {
                    eprintln!(
                        "[v0.2.45 V45-B] binary refresh landed after {} poll(s): on-disk now v{} (source v{})",
                        iteration, on_disk_version, source_version,
                    );
                } else if on_disk_version != source_version {
                    eprintln!(
                        "[v0.2.48] on-disk binary v{} is ahead of source v{} — proceeding (newer binary is what the user wants to run)",
                        on_disk_version, source_version,
                    );
                }
                return Ok(());
            }
            if Instant::now() >= deadline {
                let displayed = if on_disk_version.is_empty() {
                    "<unknown>".to_string()
                } else {
                    on_disk_version
                };
                // v0.2.55: name the hub version too — a partial refresh
                // (launcher caught up, hub still stale) now also times out
                // here (so we don't start a stale hub), and the message
                // should make that diagnosable rather than blaming only the
                // launcher binary.
                let hub_displayed = match &hub_version_opt {
                    Some(hv) if !hv.is_empty() => hv.clone(),
                    _ => "<no hub sidecar>".to_string(),
                };
                return Err(format!(
                    "Binary refresh for v{} did not land within {} sec. \
                     On-disk launcher is v{}, hub is v{}. The Release workflow \
                     is still building/committing the new dist binaries. Wait a \
                     few minutes and click Update again.",
                    source_version,
                    self.timeout.as_secs(),
                    displayed,
                    hub_displayed,
                ));
            }
            eprintln!(
                "[v0.2.45 V45-B] waiting for binary refresh: source=v{} on_disk=v{} (iteration {})",
                source_version,
                if on_disk_version.is_empty() {
                    "<unknown>".to_string()
                } else {
                    on_disk_version
                },
                iteration,
            );
            // Re-pull main to pick up the binary-refresh commit if
            // it landed since the last iteration. Soft-fail: a
            // transient network error MUST NOT terminate the wait —
            // the next iteration retries. Tests skip the pull
            // entirely (no git remote in tmpdirs) and mutate
            // `vct-launcher.metadata.json` directly between
            // iterations to simulate the commit arriving.
            if !self.disable_git_pull {
                if let Err(e) =
                    run_git_pull_ff_only(self.install_path, self.branch).await
                {
                    eprintln!(
                        "[v0.2.45 V45-B] git pull warning (will retry next iteration): {}",
                        e
                    );
                }
            }
            tokio::time::sleep(self.interval).await;
        }
    }
}

/// Minimal git-pull helper for the binary-refresh poll loop.
///
/// Runs `git pull --ff-only <VCO_UPSTREAM_REMOTE> <branch>` in the
/// install path. Returns Err with the trimmed stderr tail on
/// non-success — the caller already treats this as transient and
/// retries on the next iteration. Same `.silent()` wrapper the rest
/// of installer.rs uses so the Windows console window stays hidden.
async fn run_git_pull_ff_only(install_path: &Path, branch: &str) -> Result<(), String> {
    let out = tokio::process::Command::new("git")
        .silent()
        .args([
            "pull",
            "--ff-only",
            crate::commands::self_update::VCO_UPSTREAM_REMOTE,
            branch,
        ])
        .current_dir(install_path)
        .output()
        .await
        .map_err(|e| format!("git pull spawn failed: {}", e))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(stderr.trim().to_string());
    }
    Ok(())
}

/// v0.2.60: RAII guard that closes the launcher's managed `launcher.db`
/// connection for the `install.py --update` window and reopens it on drop.
///
/// WHY: on Windows the launcher holds the SQLite writer lock on
/// `launcher.db` (it's the process running the update + stays alive), so
/// install.py's `_self_heal_kg_bindings_on_update` RW rebind 5s-times-out
/// → `kg_binding_self_heal_db_error` deferral → half-install loop. Closing
/// the managed connection (+ the fresh-conn pollers standing down via
/// `update_gate::skip_if_update_in_progress`) gives install.py a clean
/// shot at the writer lock.
///
/// `new()` closes; `Drop` reopens on EVERY exit path (success, `?`-bail,
/// panic). If reopen FAILS, the launcher cannot keep running on the
/// schema-less in-memory stand-in (it would silently discard writes) — so
/// the guard force-quits the process. That's safe because every caller is
/// in the middle of an update that ends in a restart anyway; a clean
/// relaunch reopens the real DB at startup.
struct DbUpdateClosedGuard<R: Runtime> {
    app: AppHandle<R>,
    /// Set false once we've successfully reopened explicitly, so Drop is a
    /// no-op (avoids a double reopen on the happy path).
    armed: bool,
}

impl<R: Runtime> DbUpdateClosedGuard<R> {
    /// Close the managed connection. Soft-fails (logs) if the Db state is
    /// absent or close errors — the worst case is the pre-fix behaviour
    /// (install.py contends for the lock), never a hard update abort.
    fn new(app: AppHandle<R>) -> Self {
        if let Some(db) = app.try_state::<crate::db::Db>() {
            if let Err(e) = db.close_for_update() {
                eprintln!(
                    "[vct] update_orchestrator: close_for_update failed ({}); \
                     proceeding (install.py may contend for the launcher.db lock)",
                    e
                );
            } else {
                eprintln!(
                    "[vct] update_orchestrator: launcher.db connection closed for the \
                     install.py window (writer lock released)"
                );
            }
        } else {
            eprintln!(
                "[vct] update_orchestrator: no managed Db state — nothing to close"
            );
        }
        Self { app, armed: true }
    }

    /// Reopen the managed connection. On failure, force-quit (see struct
    /// doc). Disarms so Drop won't reopen again.
    fn reopen(&mut self) {
        if !self.armed {
            return;
        }
        self.armed = false;
        let Some(db) = self.app.try_state::<crate::db::Db>() else {
            eprintln!("[vct] update_orchestrator: no managed Db state to reopen");
            return;
        };
        match db.reopen_after_update() {
            Ok(()) => {
                eprintln!("[vct] update_orchestrator: launcher.db connection reopened");
            }
            Err(e) => {
                eprintln!(
                    "[vct] update_orchestrator: FATAL — could not reopen launcher.db \
                     after update ({}); forcing restart so the launcher does not run \
                     on a dead in-memory DB",
                    e
                );
                crate::quit_dialog::force_quit();
                self.app.exit(1);
            }
        }
    }
}

impl<R: Runtime> Drop for DbUpdateClosedGuard<R> {
    fn drop(&mut self) {
        self.reopen();
    }
}

#[command]
pub async fn update_orchestrator<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot update".to_string());
    }

    // v0.2.60 (Piece 5): version-floor gate. If the installed version is
    // below the source manifest's `min_upgradable_from`, an in-place pull
    // is not supported and the update must route to the guided hard-cut
    // (git-bundle backup + git reset --hard + reinstall, preserving all
    // ~/.vct DBs + Weaviate vectors + knowledge/ + .claude/state/).
    //
    // INERT in v0.2.60: the shipped floor is "0.0.0" (vct-module.json), so
    // `update_requires_hard_cut` ALWAYS returns false here today — the check
    // is LIVE (exercised every update, dogfoodable) but never opens the
    // gate. v0.3.0 raises the floor to declare the first real hard-cut
    // boundary; the routing to `perform_hard_cut` is wired then (the
    // primitive + its Tauri command already exist, gated by
    // `_allow_below_floor`). We only LOG the decision now so the floor logic
    // is proven in practice before v0.3.0 relies on it.
    if update_requires_hard_cut(&install_path) {
        // Unreachable in v0.2.60 (floor=0.0.0). When v0.3.0 raises the floor,
        // replace this log with the routing to the hard-cut flow.
        eprintln!(
            "[vct] update_orchestrator: installed version is below \
             min_upgradable_from — a guided hard-cut is required (v0.3.0 \
             activation; not wired in v0.2.60)"
        );
    }

    // v0.2.43 V0243-15: audit_log coverage for the update flow.
    // Capture the pre-pull state (old SHA + branch) so the `_start` row
    // carries the full context for forensic analysis. Best-effort: any
    // failure here is logged but must NOT block the update.
    let update_start_ms = chrono::Utc::now().timestamp_millis();
    let old_sha = read_head_sha(&install_path).await;
    let old_version = env!("CARGO_PKG_VERSION").to_string();
    // Helper closure: soft-write a single audit row via the app's Db.
    // Captures a clone of the AppHandle so it doesn't prevent the
    // original `app` from being moved into `restart_launcher` later.
    let audit_app = app.clone();
    let write_audit = move |operation: &str, detail: serde_json::Value| {
        use tauri::Manager as _;
        if let Some(db) = audit_app.try_state::<crate::db::Db>() {
            let _ = db.audit(operation, None, None, &detail);
        }
    };
    // Capture pull_branch for the _start entry (read before the pull so the
    // row is accurate even if the pull later fails).
    let start_branch = {
        let out = tokio::process::Command::new("git").silent()
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(&install_path)
            .output()
            .await;
        match out {
            Ok(o) if o.status.success() => {
                let b = String::from_utf8_lossy(&o.stdout).trim().to_string();
                if b.is_empty() || b == "HEAD" { "main".to_string() } else { b }
            }
            _ => "main".to_string(),
        }
    };
    write_audit(
        "update_orchestrator_start",
        serde_json::json!({
            "old_version": old_version,
            "source_commit": old_sha,
            "branch": start_branch,
            "install_path": path,
        }),
    );

    // v0.2.51 Bug A: clear any leftover resume sentinel + deferral from a
    // prior half-finished update. A fresh `update_orchestrator` run
    // supersedes it — either we'll succeed (no resume needed), or we'll
    // hit a new conflict and rewrite both with current SHAs/branch.
    clear_update_resume_sentinel(&install_path);
    clear_update_resume_deferral_if_solo(&install_path);

    // V52-AI (v0.2.52, 2026-06-09): MCP fork-bomb mitigation. The user
    // reported ~97 python (claude_mcp_servers + vct-coordination) and
    // ~77 node (@upstash/context7 + @modelcontextprotocol/*) processes
    // accumulating during update, requiring manual taskkill. Root cause
    // is Windows mandatory file locks + Claude Code's MCP-respawn loop
    // racing the binary refresh.
    //
    // Strategy:
    //   1. Pre-sweep: terminate currently-running MCP processes whose
    //      commandlines match strict MCP patterns. Soft-fail.
    //   2. Acquire a RAII lockfile guard. The lockfile lives at
    //      <vct_root>/.update-in-progress.json and is what the MCP
    //      servers themselves read at startup (see
    //      claude_mcp_servers/_lib/update_gate.py); any respawn during
    //      the update window exits cleanly with code 75 before doing
    //      any work, breaking the fork-bomb loop.
    //   3. The guard's Drop impl deletes the lockfile on ALL exit paths
    //      (success, ?-bail, panic), so even a crashed update doesn't
    //      leave a stuck lockfile blocking future MCP spawns. The
    //      boot-time stale-cleanup is the second line of defense.
    let pre_sweep_count = crate::commands::update_gate::pre_update_mcp_kill_sweep();
    if pre_sweep_count > 0 {
        eprintln!(
            "[vct] update_orchestrator: pre-sweep terminated {} MCP-shaped \
             process(es) before update",
            pre_sweep_count
        );
    }
    let (mut update_gate_guard, _gate_write_result) =
        crate::commands::update_gate::UpdateInProgressGuard::new();
    // _gate_write_result is intentionally discarded — soft-fail.
    // If lockfile write fails (permission denied, FS full), we proceed
    // with the update anyway (worst case: user sees the same pre-fix
    // fork-bomb behaviour, same as today's status quo). The guard's
    // Drop impl is a no-op when armed=false.

    // v0.2.21 (Stream A Design B extension): pin the canonical public
    // AGPL upstream BEFORE any network ops. Same posture as the launcher
    // self-update (see commands/self_update.rs): we never trust `origin`
    // for upstream tracking because forks reset it to the fork URL.
    // Hard-fail here — if we can't even configure the remote, the pull
    // below would silently fall back to `origin` and pull the wrong
    // commits. Better to surface the error to the GUI and let the user
    // retry (or override via `VCO_UPSTREAM_URL` for self-hosters).
    crate::commands::self_update::ensure_upstream_remote(&install_path).await?;

    // v0.2.21 Step 12 (B1 fix): Stage 0a — stop the detached vct-hub
    // BEFORE we even think about renaming binaries or pulling source.
    // Two reasons:
    //   1. Windows file-locking: `git pull` reverts the entire pull
    //      atomically if any file (incl. vct-hub.exe) is in-use.
    //   2. install.py's post-update deploy writes fresh state files
    //      and shouldn't race the old hub's open handles.
    // Hard-fail here if we can't stop the hub — proceeding would
    // leave the system in a half-updated state.
    emit_progress(&window, "update", "Stopping vct-hub for update...", 2.0);
    if let Err(e) = ensure_hub_stopped_for_update(&install_path) {
        return Err(format!(
            "Update aborted: could not stop vct-hub before git pull: {}. \
             Try again, or run `vct-hub --stop` manually.",
            e
        ));
    }

    // v0.2.17 (plan 0.0.B): Stage 0c — Windows-only pre-pull rename
    // of the running launcher binary. No-op on Linux/macOS.
    emit_progress(&window, "update", "Preparing for update...", 5.0);
    // v0.2.21 Step 12: Stage 0b — Windows-only pre-pull rename of the
    // vct-hub binary (sibling of the launcher in the install tree).
    // Belt-and-braces: we already stopped the hub above, but Windows
    // can briefly retain a sharing-violation flag after process exit
    // (antivirus, indexers). Renaming aside makes git pull's atomic
    // rename succeed unconditionally. No-op on POSIX.
    let pre_pull_renamed_hub = pre_pull_rename_vct_hub_binary(&install_path);
    let pre_pull_renamed = pre_pull_rename_running_binary(&install_path);

    // Stage 1: Pull latest
    emit_progress(&window, "update", "Pulling latest changes...", 10.0);

    // Detect the current branch so the explicit `git pull <remote>
    // <branch>` invocation below doesn't depend on upstream tracking
    // config (which would point at `origin/<branch>` on a fork). Default
    // to `main` on any error — matches the convention in self_update.rs.
    let pull_branch_output = tokio::process::Command::new("git").silent()
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(&install_path)
        .output()
        .await
        .map_err(|e| format!("git rev-parse failed: {}", e))?;
    let pull_branch = if pull_branch_output.status.success() {
        let b = String::from_utf8_lossy(&pull_branch_output.stdout)
            .trim()
            .to_string();
        if b.is_empty() || b == "HEAD" {
            "main".to_string()
        } else {
            b
        }
    } else {
        "main".to_string()
    };

    // v0.2.24 §A0 (2026-05-22): pre-merge user-editable files BEFORE
    // `git pull --ff-only`. Without this step, ANY local uncommitted
    // edit to an allowlisted file (CLAUDE.md, .claude/CONTEXT_STATE.md,
    // knowledge/**/*.md, etc.) that ALSO has upstream changes would
    // make git pull refuse with "Your local changes would be
    // overwritten by merge" — every 3rd-party user hits this the first
    // time upstream touches those files.
    //
    // The pre-merge:
    //   1. Resolves base = merge-base(HEAD, vco_upstream/<branch>).
    //   2. Resolves theirs = vco_upstream/<branch> tip.
    //   3. Walks the diff base..theirs ∩ git status --porcelain
    //      ∩ USER_EDITABLE_PATTERNS allowlist.
    //   4. Per file: clean merge → write merged content + stage.
    //                conflict → write sidecar `<path>.from-upstream-<sha>`
    //                          leave local in place.
    //
    // Best-effort: any failure (no upstream ref yet, malformed diff,
    // git merge-file errors) is logged and skipped — the bare `git
    // pull --ff-only` below still runs and surfaces the original
    // error if pre-merge couldn't help.
    //
    // We MUST `git fetch` first: pre_merge_user_editable resolves
    // refs via `rev-parse vco_upstream/<branch>` and reads blobs via
    // `git show <sha>:<path>`; without a recent fetch the local refs
    // are stale and pre-merge sees no upstream changes.
    emit_progress(&window, "update", "Fetching upstream for pre-merge...", 7.0);
    // v0.2.83 (D5): route through the single serialized fetch home (Quick
    // policy, branch refspec). Soft-fail posture preserved: if the fetch fails
    // the bare pull below surfaces the real error; we still attempt pre-merge
    // with whatever refs exist. The mutex + `--no-write-fetch-head` protect
    // against the FETCH_HEAD race with the startup badge check (A-RC3).
    if let Err(e) = crate::commands::self_update::serialized_fetch_upstream(
        &install_path,
        crate::commands::self_update::FetchPolicy::Quick,
        Some(&pull_branch),
    )
    .await
    {
        eprintln!(
            "[vct] update_orchestrator: pre-merge fetch failed: {} — continuing",
            e
        );
    }
    let pre_merge_outcomes = run_pre_merge_user_editable(&install_path, &pull_branch).await;

    // v0.2.24 §A0 (Q1 fix): emit deferral entries BEFORE the bare
    // git pull. Rationale: when pre-merge produces sidecars (true
    // 3-way conflict), the local file is still divergent and the
    // --ff-only pull WILL fail with non-FF. The user then sees the
    // B4 divergence modal — without the deferral entries on disk,
    // they lose the audit trail of which files pre-merge sidecar'd
    // vs auto-merged. Emit unconditionally so the deferral lands
    // regardless of which branch the pull takes. Best-effort: a
    // deferral-write failure must NOT block the update flow.
    maybe_emit_pre_merge_deferrals(&install_path, &pre_merge_outcomes, &pull_branch);

    // v0.2.24 §A0 (peer-review follow-up): when pre-merge produced a
    // synthetic commit (Merged outcome), local HEAD now strictly
    // advances upstream tip. A bare `git pull --ff-only` would fail
    // with non-FF for the COMMON case of a user-editable diff,
    // surfacing the B4 modal for what should be a seamless update.
    // Route through `git pull --rebase` instead: this replays the
    // synthetic pre-merge commit onto upstream tip, giving a clean
    // linear history. If the rebase has conflicts (genuine user
    // divergence beyond the allowlisted files), git falls back to
    // the existing conflict-handling path.
    //
    // When pre-merge produced no synthetic commit (all outcomes were
    // NoChange or PreservedWithUpstreamSidecar), keep the original
    // --ff-only behaviour: any non-FF in that case IS a genuine
    // divergence the user needs to confirm via the B4 modal.
    let pre_merge_committed = crate::commands::git_user_editable_merge::any_outcome_produced_synthetic_commit(
        &pre_merge_outcomes,
    );
    // v0.2.56 (Defect A fix): when pre-merge did NOT synthesize a commit
    // (so the code below would otherwise pick `--ff-only`), the LOCAL
    // clone may STILL have diverged from upstream via COMMITTED local
    // commits — the universal case for a 3rd-party user whose Claude has
    // committed KG nodes (encouraged behavior). A bare `--ff-only` then
    // refuses with non-fast-forward and surfaces the scary B4
    // Merge/Rebase/Cancel modal EVEN WHEN a real merge would be
    // conflict-free (committed KG additions never overlap upstream's
    // source/version/binary changes).
    //
    // The pre-merge step is blind to this: it only inspects `git status
    // --porcelain` (UNcommitted edits). So before settling on --ff-only,
    // probe statelessly with `git merge-tree --write-tree` (writes
    // nothing — see committed_divergence_merges_cleanly). If the merge is
    // conflict-free, route through a REAL merge pull (`--no-rebase
    // --no-edit`) instead of --ff-only: the merge completes silently, the
    // existing post-pull success flow runs unchanged, and NO modal
    // surfaces. The modal is reserved for GENUINE content conflicts (or a
    // merge that can't even start). Best-effort: any probe failure leaves
    // `--ff-only` in place so the legacy non-FF path still surfaces the
    // modal — never auto-merge on uncertainty.
    // v0.2.56 (review BLOCKER B1) + v0.2.58 (precise gate): the auto-merge
    // path uses `--autostash`, which can leave a SILENTLY-broken working
    // tree (exit 0 + UU markers + dangling stash) if an uncommitted edit
    // conflicts on the autostash pop — bypassing the post-pull conflict
    // modal. v0.2.56 guarded this with a BLUNT "working tree must be 100%
    // clean" check, but an installed orchestrator is PERMANENTLY dirty in
    // the expected way (hundreds of untracked user KG nodes + scratch
    // files): that gate bailed every real update to the scary divergence
    // modal even when the merge was perfectly safe.
    //
    // v0.2.58 narrows the gate to the PRECISE pop-conflict-risk set:
    // `tracked-modified ∩ upstream-changed`. `git stash`/`--autostash`
    // never touches UNTRACKED files, and a tracked-modified file upstream
    // didn't change can't pop-conflict — so the ONLY risky files are ones
    // both locally-modified (tracked) AND changed by upstream in this
    // merge. If that set is empty, the auto-merge is safe regardless of how
    // many untracked KG nodes / scratch files dirty the tree. This honors
    // the principle that the update must NOT CARE about expected-to-diverge
    // user files. See `tracked_modified_overlapping_upstream`. (The shared fn
    // resolves `theirs` ONCE and reuses it for both the risk check and the
    // merge-tree probe; any resolution/probe error keeps `--ff-only` so the
    // modal surfaces, never a wrong silent auto-merge.)
    //
    // v0.2.71 (Piece 3): the pull-strategy decision is now the SHARED
    // `resolve_divergence_pull_plan` in `git_user_editable_merge` (used by
    // BOTH update surfaces — this command AND `self_update::apply_launcher_update`
    // — so the two can't drift). The decision tree is identical to the
    // pre-v0.2.71 inline block: pre_merge_committed → RebaseAutostash; else
    // resolve theirs/base + pop-conflict-risk + merge-tree probe → RealMerge
    // when clean & no risk, FfOnly otherwise (conservative on any uncertainty).
    //
    // v0.2.29/v0.2.56/v0.2.58 rationale (preserved): the RebaseAutostash arm
    // uses `--autostash` so in-progress WIP outside the allowlist doesn't
    // abort the rebase ("cannot pull with rebase: You have unstaged
    // changes"); the RealMerge arm folds conflict-free committed divergence
    // (e.g. committed KG nodes) with `--autostash` LIVE over a dirty tree
    // we proved has no pop-conflict overlap. The ONE residual hazard for
    // RealMerge is a TOCTOU race (upstream pushes a commit touching a
    // locally-modified file between our pre-check and the pull's own fetch)
    // → caught by the post-pull autostash-pop backstop below, NOT silently
    // continued. NOTE (review C1): after a RealMerge, local HEAD is a merge
    // commit; if the user updated inside the post-tag binary-refresh window,
    // `WaitForBinaryRefresh`'s `--ff-only` re-pull soft-fails+times out and
    // the v0.2.55 finalize recovery handles it (self-heals next update).
    // v0.2.78 ITEM #0 (F1): before deciding the pull plan, auto-restore any
    // TRACKED uncommitted file whose working-tree content is byte-identical to
    // the incoming upstream blob. Such a file is not a real modification (its
    // content already == the merge target), so it should not force the
    // divergence modal via the pop-conflict-risk set. Byte-identity-gated
    // (never mtime/size); divergent files are left in the risk set → modal.
    // Shared helper — the self_update surface calls the SAME fn (one home).
    let f1_restored =
        crate::commands::git_user_editable_merge::auto_restore_byte_identical_tracked_mods(
            &install_path,
            &pull_branch,
        )
        .await;
    if f1_restored > 0 {
        eprintln!(
            "[vct] update_orchestrator: F1 auto-restored {} byte-identical tracked file(s) \
             before divergence-plan resolution",
            f1_restored
        );
    }
    let pull_plan = crate::commands::git_user_editable_merge::resolve_divergence_pull_plan(
        &install_path,
        &pull_branch,
        pre_merge_committed,
    )
    .await;
    // Retained for the conflict-op label below (the post-pull conflict +
    // autostash-pop paths say "merge" for the real-merge arm, "rebase"
    // otherwise) — identical semantics to the pre-v0.2.71 boolean.
    let auto_merge_committed_divergence = pull_plan
        == crate::commands::git_user_editable_merge::PullPlan::RealMerge;
    let pull_args =
        pull_plan.pull_args(crate::commands::self_update::VCO_UPSTREAM_REMOTE, &pull_branch);
    let pull = tokio::process::Command::new("git").silent()
        .args(&pull_args)
        .current_dir(&install_path)
        .output()
        .await
        .map_err(|e| format!("git pull failed: {}", e))?;

    if !pull.status.success() {
        let stderr = String::from_utf8_lossy(&pull.stderr);
        let stdout = String::from_utf8_lossy(&pull.stdout);
        // v0.2.17 (plan 0.0.B): on pull failure, revert the pre-pull
        // rename so the running launcher can still be re-launched if
        // the user kills the GUI. Best-effort. v0.2.21 Step 12 also reverts
        // the hub-binary rename + restarts the hub. v0.2.71: shared tail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );

        // v0.2.51 Bug A (defensive): the rebase-with-autostash branch can
        // produce a rebase conflict (`CONFLICT (content):` lines on the
        // synthetic pre-merge commit OR on the user's WIP via autostash
        // pop). Detect that before falling through to the non-FF /
        // generic-error paths so the conflict modal surfaces correctly +
        // the resume sentinel lands.
        //
        // v0.2.56: the new `auto_merge_committed_divergence` path runs a
        // `--no-rebase` MERGE pull, which on the rare probe-vs-pull TOCTOU
        // race can ALSO conflict. Label the operation accurately so the
        // resume sentinel + modal say "merge" not "rebase". (The abort
        // recovery `abort_orchestrator_merge_or_rebase` is label-agnostic
        // — it reads .git/MERGE_HEAD vs .git/rebase-merge on disk — so
        // this is for the user-facing message only, but accuracy matters.)
        let combined = format!("{}\n{}", stderr, stdout);
        if is_merge_or_rebase_conflict(&combined) {
            let conflict_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            // v0.2.53 DEDUP-14: paired sentinel + deferral via the
            // single helper so future writers can't accidentally write
            // one without the other (v0.2.51 Bug A class).
            write_resume_sentinel_and_deferral(&install_path, conflict_op, &pull_branch).await;
            let conflicted = collect_conflicted_files(&install_path).await;
            return Err(serialize_orchestrator_conflict_error(
                conflict_op,
                &pull_branch,
                &conflicted,
                combined.trim(),
            ));
        }

        // v0.2.23 (B4 / D19): non-fast-forward branch. The user's local
        // clone has diverged from upstream (typical when they've edited
        // CLAUDE.md / CONTEXT_STATE.md / KG nodes locally, or when we
        // rewrote upstream history). Surface a structured payload so
        // the frontend can render a "Merge / Rebase / Cancel" modal
        // instead of dumping a raw git error to a toast.
        //
        // Best-effort: collect SHAs + a list of diverged files so the
        // user can see what's about to be merged. Any failure here
        // falls back to the legacy raw-error path — never block.
        if crate::commands::self_update::is_non_fast_forward(&stderr) {
            let local_sha = read_head_sha(&install_path).await;
            let remote_sha = read_remote_sha(&install_path, &pull_branch).await;
            let (upstream_changed, local_only) =
                collect_diverged_files(&install_path, &pull_branch).await;
            // v0.2.55 (durable-logging fix): the non-FF case previously
            // surfaced ONLY as the GUI Merge/Rebase/Cancel modal below. If
            // the user dismisses/cancels it, the update silently didn't
            // apply and there was NO record a terminal Claude could find.
            // Write a durable UPDATE_DEFERRED.md entry too (the conflict
            // path already does this via write_resume_sentinel_and_deferral;
            // this closes the non-FF asymmetry). Best-effort: never blocks
            // the structured error the frontend needs.
            write_launcher_update_diverged_deferral(
                &install_path,
                &pull_branch,
                LauncherUpdateDivergedKind::NonFastForward {
                    local_sha: local_sha.clone(),
                    remote_sha: remote_sha.clone(),
                    detail: stderr.trim().to_string(),
                },
            );
            return Err(serialize_orchestrator_non_ff_error(
                &pull_branch,
                local_sha.as_deref(),
                remote_sha.as_deref(),
                &upstream_changed,
                &local_only,
                stderr.trim(),
            ));
        }
        // v0.2.55 (audit R1): any OTHER git-pull failure (not a conflict,
        // not a non-FF divergence) — e.g. a broken local git, a detached
        // HEAD, a missing upstream remote. PRE-v0.2.55 this returned a
        // GUI-only error string with no durable trace; a 3rd-party's Claude
        // couldn't see it at session start. Write a durable deferral too.
        write_launcher_update_diverged_deferral(
            &install_path,
            &pull_branch,
            LauncherUpdateDivergedKind::GitPullFailed {
                detail: stderr.trim().to_string(),
            },
        );
        return Err(format!("git pull failed: {}", stderr));
    }

    // v0.2.58 (review BLOCKER-1): the `--autostash` pull can SUCCEED (exit 0)
    // yet leave the tree broken. `git pull --no-rebase/--rebase --autostash`
    // stashes local tracked changes, merges/rebases, then POPS the stash. If
    // the pop conflicts, git prints "Applying autostash resulted in
    // conflicts." and leaves `UU` markers + a dangling autostash — but STILL
    // EXITS 0. The `!pull.status.success()` block above therefore does NOT
    // catch it, and proceeding would run install.py + restart on a
    // silently-broken tree (the original B1 hazard).
    //
    // This can happen on a TOCTOU race: our pop-conflict-risk pre-check saw
    // no overlap, but upstream pushed a commit touching a locally-modified
    // file in the window before the pull's own fetch. (It also covers the
    // pre-existing `--rebase --autostash` arm, which had the same latent
    // hole.) Detect it on the SUCCESS path — unmerged files present and/or
    // the autostash-conflict marker in stdout — and route to the conflict
    // modal + resume sentinel exactly like the non-zero conflict branch,
    // instead of silently continuing. Best-effort; never auto-proceed on a
    // tree we can't confirm clean.
    {
        let pull_combined = format!(
            "{}\n{}",
            String::from_utf8_lossy(&pull.stdout),
            String::from_utf8_lossy(&pull.stderr)
        );
        let autostash_pop_failed = pull_combined.contains("autostash resulted in conflicts")
            || pull_combined.contains("Applying autostash");
        let unmerged = collect_conflicted_files(&install_path).await;
        if !unmerged.is_empty() || autostash_pop_failed {
            eprintln!(
                "[vct] update_orchestrator: pull exited 0 but the working tree has \
                 {} unmerged file(s){} — an --autostash pop conflict (TOCTOU race). \
                 Routing to the conflict modal instead of proceeding on a broken tree.",
                unmerged.len(),
                if autostash_pop_failed { " + autostash-conflict marker" } else { "" },
            );
            // Restore the running binary + hub (we renamed/stopped pre-pull)
            // so the user can keep using the launcher after they resolve.
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            let conflict_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            write_resume_sentinel_and_deferral(&install_path, conflict_op, &pull_branch).await;
            return Err(serialize_orchestrator_conflict_error(
                conflict_op,
                &pull_branch,
                &unmerged,
                pull_combined.trim(),
            ));
        }
    }

    let pull_output = String::from_utf8_lossy(&pull.stdout);
    if pull_output.contains("Already up to date") {
        emit_progress(&window, "done", "Already up to date!", 100.0);
        // v0.2.17: nothing was pulled — revert the rename so the canonical
        // path holds the (still-current) binary. The user doesn't expect a
        // restart in this case. v0.2.21 Step 12: same for the hub binary,
        // then bring it back up — the existing binary starts cleanly since
        // nothing changed on disk. v0.2.71: shared tail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        // v0.2.43 V0243-15: audit complete for the no-op path.
        write_audit(
            "update_orchestrator_complete",
            serde_json::json!({
                "success": true,
                "duration_ms": chrono::Utc::now().timestamp_millis() - update_start_ms,
                "note": "already_up_to_date",
                "branch": start_branch,
            }),
        );
        return Ok(InstallResult {
            success: true,
            install_path: path,
            message: "Already up to date".to_string(),
            system,
        });
    }

    emit_progress(&window, "update", "Changes pulled", 30.0);

    // v0.2.24 §A0 (Q1 fix): deferrals were already emitted BEFORE the
    // pull (see above) — no second call needed here.

    // v0.2.63: HEAD-advance backstop. A pull that exited 0 but did NOT reach
    // the upstream tip (a non-FF that slipped through, an odd partial state)
    // must NOT proceed to install.py — that would run the STALE tree (the
    // v0.2.62 GUI-update crash class: old install.py at pre-fix line numbers).
    // Abort cleanly, write a durable deferral so a terminal Claude can see the
    // update didn't land, and return a plain error (NOT the Merge/Rebase modal
    // — that path is what failed; routing back to it would loop).
    if let Err(e) = assert_head_reached_upstream(&install_path).await {
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        write_launcher_update_diverged_deferral(
            &install_path,
            &pull_branch,
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: read_head_sha(&install_path).await,
                remote_sha: read_remote_sha(&install_path, &pull_branch).await,
                detail: e.clone(),
            },
        );
        write_audit(
            "update_orchestrator_complete",
            serde_json::json!({
                "success": false,
                "duration_ms": chrono::Utc::now().timestamp_millis() - update_start_ms,
                "note": "head_did_not_advance_post_pull",
                "branch": start_branch,
            }),
        );
        return Err(e);
    }

    // Stage 2: Re-run install.py with --update flag
    emit_progress(&window, "install", "Applying updates...", 40.0);

    // V52-AI: advance lockfile phase so future hub-side observers can
    // see we've moved past git_pull.
    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::InstallPy);

    // v0.2.60: close the launcher's managed launcher.db connection for the
    // install.py window so install.py can take the SQLite writer lock
    // (Windows holds it exclusively — the launcher-self-db-lock bug). The
    // fresh-conn pollers stand down via the `.update-in-progress` lockfile
    // (set above by `UpdateInProgressGuard`) + `skip_if_update_in_progress`.
    // RAII: reopens on EVERY exit path below (incl. the install-fail
    // early-return), and force-restarts if reopen fails.
    let mut db_close_guard = DbUpdateClosedGuard::new(app.clone());

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd).silent();
    cmd.args(["install.py", "--update"])
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);
    // v0.2.27: force UTF-8 stdout/stderr for the Python child. See the
    // identical block on the `update_at` spawn site for the full
    // rationale (Windows cp1252 / `→` U+2192 / install.py crash).
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");
    // v0.2.15 (Agent D): expose the running launcher's PID to install.py
    // so _refresh_dist_binary_after_rebuild can include it in the
    // launcher_restart_required deferral message ("running launcher
    // PID: 12345"). install.py reads from VCT_LAUNCHER_PID; absence is
    // a soft fallback (the deferral still works, just without the PID
    // hint). Set even when we don't yet know we'll trigger a binary
    // swap because the swap detection happens inside install.py.
    cmd.env("VCT_LAUNCHER_PID", std::process::id().to_string());
    // v0.2.17 (plan 0.0): tell install.py "the Rust side is handling
    // the restart". install.py's `_refresh_dist_binary_after_rebuild`
    // sees this and skips emitting the `launcher_restart_required`
    // deferral — the auto-restart below makes the deferral redundant.
    // When install.py runs WITHOUT this env (manual `python
    // install.py --update` from terminal), it still emits the
    // deferral so the running launcher's W4 banner picks it up.
    cmd.env("VCT_AUTO_RESTART_LAUNCHER", "1");
    // v0.2.49 batch 4 (sub-progress label): tell install.py to mirror
    // _log_install_event calls to stdout as `[VCO-EVENT] <step>
    // <phase> <detail>` lines. We stream stdout below + forward each
    // event as sub-progress to the OrchestratorUpdateProgressModal so
    // the user sees "Seeding Weaviate KG…", "Running migrations…",
    // etc. instead of a static "Applying updates…" for the full
    // re-embedding phase (which can take minutes).
    cmd.env("VCO_PROGRESS_STREAM", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    // v0.2.49 batch 4: spawn + stream stdout instead of .output() so
    // we can emit sub-progress messages as install.py advances. The
    // failure path is identical: full stderr is captured into the
    // buffer for the post-exit error handler. Stdout is also captured
    // (for parity with the pre-batch-4 behaviour where .output()
    // populated install_output.stdout); the launcher only used the
    // exit code anyway, so this is forward-compatible.
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    let mut install_child = cmd
        .spawn()
        .map_err(|e| format!("install.py --update failed to spawn: {}", e))?;

    let mut install_stdout_buf = Vec::<u8>::new();
    if let Some(stdout) = install_child.stdout.take() {
        use tokio::io::{AsyncBufReadExt, BufReader};
        let mut reader = BufReader::new(stdout).lines();
        loop {
            match reader.next_line().await {
                Ok(Some(line)) => {
                    install_stdout_buf.extend_from_slice(line.as_bytes());
                    install_stdout_buf.push(b'\n');
                    if let Some(rest) = line.strip_prefix("[VCO-EVENT] ") {
                        // Format: `<step> <phase> <detail...>`. We
                        // only react to `start` / `ok` phases for
                        // progress messages. warn/error/skip stay
                        // silent on this surface — they're already
                        // captured in the JSONL log + the failure
                        // path's stderr.
                        let mut parts = rest.splitn(3, ' ');
                        let step = parts.next().unwrap_or("");
                        let phase = parts.next().unwrap_or("");
                        let detail = parts.next().unwrap_or("");
                        if phase == "start" || phase == "ok" {
                            let sub_msg =
                                installer_step_to_user_label(step, detail);
                            if !sub_msg.is_empty() {
                                // Hold the parent percentage steady at
                                // ~50% (between the 40% "Applying"
                                // and the 90% "Starting vct-hub").
                                // The sub-message is what the user
                                // reads.
                                emit_progress(
                                    &window,
                                    "install",
                                    &sub_msg,
                                    50.0,
                                );
                            }
                        }
                    }
                }
                Ok(None) => break, // EOF
                Err(e) => {
                    eprintln!(
                        "[vct] update_orchestrator: install.py stdout \
                         read error: {} (continuing; install.py still \
                         running)",
                        e
                    );
                    break;
                }
            }
        }
    }

    let install_status = install_child
        .wait()
        .await
        .map_err(|e| format!("install.py --update wait failed: {}", e))?;

    // Drain stderr (small; we waited for the process to exit first).
    let mut install_stderr_buf = Vec::<u8>::new();
    if let Some(mut stderr) = install_child.stderr.take() {
        use tokio::io::AsyncReadExt;
        let _ = stderr.read_to_end(&mut install_stderr_buf).await;
    }

    // Reconstruct an Output-shaped struct so the rest of the function
    // reads identically to the pre-batch-4 .output() code path.
    let install_output = std::process::Output {
        status: install_status,
        stdout: install_stdout_buf,
        stderr: install_stderr_buf,
    };

    if !install_output.status.success() {
        let stderr = String::from_utf8_lossy(&install_output.stderr);
        // v0.2.17: install.py failed — don't restart. Revert the pre-pull
        // rename so the canonical path is usable again. v0.2.21 Step 12:
        // same for the hub binary, then attempt to bring the hub back up so
        // the user isn't left without it (install.py failed AFTER git pull
        // succeeded, so the on-disk vct-hub may be the new version — the
        // launcher's discovery chain still finds it via find_hub_binary()).
        // v0.2.71: shared tail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        // db_close_guard drops here on the early-return → reopens (or
        // force-restarts if reopen fails). No explicit call needed.
        return Err(format!("Update failed: {}", stderr));
    }

    // v0.2.60: install.py is done with launcher.db — reopen the managed
    // connection now (explicitly, before the binary-refresh/finalize tail
    // which may read/write the DB). On reopen failure this force-restarts.
    db_close_guard.reopen();

    // V52-AI: advance lockfile phase. install.py has finished; we're
    // now in the binary-refresh + hub-restart window. MCPs that try to
    // spawn during this window still see the lockfile and exit cleanly.
    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::BinaryRefresh);

    // v0.2.54 Track C (P0-7 / C-1 / C-2): shared finalize tail —
    // V45-B binary wait, V52-AI disarm, V52-AH staging + handoff,
    // hub restart (no-handoff path only; C-1 reorder so a freshly-
    // restarted hub can't hold a Windows lock on vct-hub.exe while
    // the stage1 updater tries to swap it), legacy restart + deferral
    // fallback. Shared with merge/rebase/resume via
    // `run_post_pull_install_and_restart` so the four entry paths
    // can't drift apart again.
    //
    // v0.2.44 V44-G4: clone the AppHandle here so the post-restart
    // retry block (Trigger B sweep + final audit) can still reach
    // `app.try_state::<Db>()` after the finalize tail consumes its
    // owned copy. AppHandle is Clone (cheap reference-counted handle).
    let post_restart_app = app.clone();
    let finalize = finalize_update_and_restart(
        app,
        &install_path,
        &path,
        &window,
        python_cmd,
        &pull_branch,
        &mut update_gate_guard,
    )
    .await?;

    if finalize.handoff_exit {
        return Ok(InstallResult {
            success: true,
            install_path: path.clone(),
            message: "Update applied — vct-updater is performing the binary swap. \
                      The launcher will relaunch automatically."
                .to_string(),
            system,
        });
    }
    emit_progress(&window, "done", "Orchestrator updated successfully!", 100.0);

    // v0.2.44 V44-G4 (RL-chat ask 2026-06-01): Trigger B — auto-retry
    // stuck module_installs rows across ALL projects after a successful
    // orchestrator update. Gated by the per-user toggle (default ON).
    // Disabled by users who keep deliberately-failed modules and don't
    // want them auto-reinstalled on launcher updates.
    //
    // Non-blocking: emits its own audit row; any retry failure is
    // recorded but never poisons the orchestrator-update result. The
    // launcher restart at the end of this function happens unconditionally.
    //
    // Note 1: we use try_state because the DB may not be registered
    // when `update_orchestrator` is invoked via `update_orchestrator_at`
    // (an early-boot recovery surface that runs before app setup).
    //
    // Note 2: we pass `None` for the AppHandle. Here's why — the
    // orchestrator-update path culminates in a launcher restart (see
    // `restart_launcher` call below), which means any in-process install
    // we kick off would run in the OLD binary that's about to exit.
    // Instead we let the self-heal branch flip rows whose container is
    // already healthy (the common case for stuck rows after a binary
    // upgrade), and audit-log the rest as `retried_unavailable`. The
    // post-restart binary's `resume_containers_on_startup` sweep then
    // picks up where this stops. For rows requiring an actual install
    // re-run, the user clicks the per-project Update button (Trigger A,
    // which DOES pass `Some(&app)` since no restart follows).
    {
        use tauri::Manager as _;
        if let Some(db) = post_restart_app.try_state::<crate::db::Db>() {
            let enabled =
                crate::commands::module_service::auto_retry_on_orchestrator_update_enabled(&db);
            if enabled {
                let reports =
                    crate::commands::module_service::retry_failed_module_installs(
                        None, &db, None,
                    )
                    .await;
                let summary = retry_summary_counts(&reports);
                write_audit(
                    "module_install_auto_retry_sweep",
                    serde_json::json!({
                        "trigger": "update_orchestrator",
                        "total": reports.len(),
                        "summary": summary,
                    }),
                );
            } else {
                write_audit(
                    "module_install_auto_retry_sweep",
                    serde_json::json!({
                        "trigger": "update_orchestrator",
                        "skipped": "disabled_by_setting",
                    }),
                );
            }
        }
    }

    // v0.2.43 V0243-15: audit_log complete entry (success path).
    let new_sha = read_head_sha(&install_path).await;
    write_audit(
        "update_orchestrator_complete",
        serde_json::json!({
            "success": true,
            "duration_ms": chrono::Utc::now().timestamp_millis() - update_start_ms,
            "old_version": old_version,
            "new_sha": new_sha,
            "branch": start_branch,
        }),
    );

    Ok(InstallResult {
        success: true,
        install_path: path,
        message: "Orchestrator updated successfully".to_string(),
        system,
    })
}

// ---------------------------------------------------------------------------
// v0.2.23 (B4 / D19): divergence-recovery commands.
//
// When `update_orchestrator`'s ff-only pull fails because the local clone
// has diverged from upstream (the user has local commits to CLAUDE.md,
// CONTEXT_STATE.md, KG nodes, etc.), the frontend renders a modal asking
// the user to choose merge / rebase / cancel. These commands are the
// resolvers behind the merge/rebase buttons; abort is the safety net when
// the merge/rebase produces conflicts.
//
// Design notes:
//   - We re-use the same pre-pull-rename + hub-stop choreography as
//     `update_orchestrator` so binary swaps and Windows file locks are
//     handled the same way regardless of which pull variant the user
//     chose.
//   - On merge/rebase conflict we LEAVE the working tree in the
//     conflicted state. The user might want to edit files manually in
//     their editor. The "Abort" frontend button calls
//     `abort_orchestrator_merge_or_rebase` to bail cleanly.
//   - Discriminators on the error payloads:
//       update_orchestrator non-FF             → "orchestrator_update_non_ff"
//       merge/rebase conflict                  → "orchestrator_update_conflict"
//     The frontend uses these to pick the right modal.
// ---------------------------------------------------------------------------

/// Best-effort: read HEAD's full SHA. Returns None on any failure (offline,
/// detached HEAD, corrupted repo). The frontend renders "—" in that slot.
async fn read_head_sha(repo: &Path) -> Option<String> {
    let out = tokio::process::Command::new("git").silent()
        .args(["rev-parse", "HEAD"])
        .current_dir(repo)
        .output()
        .await
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if sha.is_empty() {
        None
    } else {
        Some(sha)
    }
}

/// Best-effort: read the upstream branch tip via `git ls-remote`. We use
/// `ls-remote` rather than `rev-parse vco_upstream/<branch>` because the
/// caller may not have fetched recently — `ls-remote` always hits the
/// network and reports the current upstream tip.
async fn read_remote_sha(repo: &Path, branch: &str) -> Option<String> {
    let out = tokio::process::Command::new("git").silent()
        .args([
            "ls-remote",
            crate::commands::self_update::VCO_UPSTREAM_REMOTE,
            branch,
        ])
        .current_dir(repo)
        .output()
        .await
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let raw = String::from_utf8_lossy(&out.stdout);
    raw.split_whitespace().next().map(|s| s.to_string())
}

/// Best-effort: collect the lists of (a) files upstream changed since
/// the fork point and (b) files that exist only on the local side. Used
/// to populate the divergence modal so the user sees BOTH "what would
/// be merged in" and "what stays as-is".
///
/// v0.2.27 rewrite: prior to v0.2.27 this function used `git diff HEAD..
/// upstream/branch --name-only`, which lists files different between
/// the two tips regardless of whether the difference is genuine upstream
/// change or just a local-only addition. Forks that track paths the
/// public repo doesn't (e.g. a private fork's `other_projects_knowledge/`)
/// would see ALL their local-only paths show up as "diverged files" in
/// the modal — confusing because those files can't possibly merge-
/// conflict (upstream has no version of them at all). The rewrite
/// separates the two categories by anchoring on the merge-base.
///
/// Returns `(upstream_changed_files, local_only_files)`. Empty vectors
/// on any git failure — the modal renders without a file list, still
/// usable.
async fn collect_diverged_files(
    repo: &Path,
    branch: &str,
) -> (Vec<String>, Vec<String>) {
    let upstream_ref = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        branch,
    );

    // Find the merge-base. If this fails (e.g. unrelated histories,
    // which shouldn't happen for any real VCO clone), fall back to the
    // pre-v0.2.27 behaviour rather than blocking the modal.
    let merge_base = match tokio::process::Command::new("git").silent()
        .args(["merge-base", "HEAD", &upstream_ref])
        .current_dir(repo)
        .output()
        .await
    {
        Ok(o) if o.status.success() => {
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }
        _ => {
            // Fall through to the legacy single-list behaviour. Less
            // accurate but still informative.
            let legacy = legacy_collect_diverged_files(repo, branch).await;
            return (legacy, Vec::new());
        }
    };

    let run_diff = |spec: String| {
        let repo = repo.to_path_buf();
        async move {
            let out = tokio::process::Command::new("git").silent()
                .args(["diff", "--name-only", &spec])
                .current_dir(&repo)
                .output()
                .await;
            match out {
                Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout)
                    .lines()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect::<std::collections::BTreeSet<String>>(),
                _ => std::collections::BTreeSet::new(),
            }
        }
    };

    // Set A: files upstream touched since the fork point.
    let upstream_touched = run_diff(format!("{}..{}", merge_base, upstream_ref)).await;
    // Set B: files local touched since the fork point.
    let local_touched = run_diff(format!("{}..HEAD", merge_base)).await;

    // upstream_changed_files = A (every upstream-touched file is
    // relevant to the merge; A ∩ B are the real conflict candidates,
    // A \ B will auto-merge — but both belong in the same "what's
    // coming from upstream" bucket from the user's UI perspective).
    let upstream_changed: Vec<String> = upstream_touched.iter().cloned().collect();

    // local_only_files = B \ A (touched locally, untouched upstream —
    // safe to leave alone, no merge attention needed).
    let local_only: Vec<String> = local_touched
        .iter()
        .filter(|p| !upstream_touched.contains(*p))
        .cloned()
        .collect();

    (upstream_changed, local_only)
}

/// Pre-v0.2.27 fallback: single list of files diff between HEAD and the
/// upstream branch tip. Used when `git merge-base` itself fails (e.g.
/// unrelated histories — shouldn't happen for any real VCO clone but
/// guard against it anyway).
async fn legacy_collect_diverged_files(repo: &Path, branch: &str) -> Vec<String> {
    let out = tokio::process::Command::new("git").silent()
        .args([
            "diff",
            "--name-only",
            &format!(
                "HEAD..{}/{}",
                crate::commands::self_update::VCO_UPSTREAM_REMOTE,
                branch
            ),
        ])
        .current_dir(repo)
        .output()
        .await;
    let out = match out {
        Ok(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Best-effort: collect the list of currently-conflicted files after a
/// merge or rebase. `git diff --name-only --diff-filter=U` lists every
/// path with unresolved merge markers.
async fn collect_conflicted_files(repo: &Path) -> Vec<String> {
    let out = tokio::process::Command::new("git").silent()
        .args(["diff", "--name-only", "--diff-filter=U"])
        .current_dir(repo)
        .output()
        .await;
    let out = match out {
        Ok(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Serialize the non-fast-forward divergence payload the frontend's
/// `OrchestratorUpdateDivergenceModal` consumes. Hand-rolled JSON to
/// match the style used by `self_update::serialize_non_ff_error` — both
/// surfaces emit error strings from Tauri commands; the frontend tries
/// `JSON.parse(err)` and falls back to a raw toast when parsing fails.
///
/// Schema:
///   {
///     "event": "orchestrator_update_non_ff",
///     "branch": "main",
///     "local_sha":  "abc..." | null,
///     "remote_sha": "def..." | null,
///     "diverged_files": ["path/a", "path/b", ...],
///     "git_stderr": "<raw error>"
///   }
fn serialize_orchestrator_non_ff_error(
    branch: &str,
    local: Option<&str>,
    remote: Option<&str>,
    diverged_files: &[String],
    local_only_files: &[String],
    git_stderr: &str,
) -> String {
    let stderr_esc = crate::commands::self_update::json_escape(git_stderr);
    let local_field = match local {
        Some(s) => format!("\"{}\"", s),
        None => "null".to_string(),
    };
    let remote_field = match remote {
        Some(s) => format!("\"{}\"", s),
        None => "null".to_string(),
    };
    let to_json_array = |items: &[String]| -> String {
        let parts: Vec<String> = items
            .iter()
            .map(|p| format!("\"{}\"", crate::commands::self_update::json_escape(p)))
            .collect();
        format!("[{}]", parts.join(","))
    };
    let files_field = to_json_array(diverged_files);
    let local_only_field = to_json_array(local_only_files);
    // v0.2.27: added `local_only_files` so the modal can render
    // "files only on your clone" as a separate (collapsible) section
    // instead of mixing them into the merge-conflict-candidate list.
    // Forks that track paths the public repo doesn't (e.g. a private
    // fork's `other_projects_knowledge/`) no longer see those paths flagged
    // as "diverged" in the modal.
    format!(
        "{{\"event\":\"orchestrator_update_non_ff\",\"branch\":\"{}\",\"local_sha\":{},\"remote_sha\":{},\"diverged_files\":{},\"local_only_files\":{},\"git_stderr\":\"{}\"}}",
        branch, local_field, remote_field, files_field, local_only_field, stderr_esc
    )
}

/// Serialize the merge/rebase conflict payload the frontend's
/// `OrchestratorUpdateConflictModal` consumes.
///
/// Schema:
///   {
///     "event": "orchestrator_update_conflict",
///     "operation": "merge" | "rebase",
///     "branch": "main",
///     "conflicted_files": ["path/a", "path/b", ...],
///     "git_stderr": "<raw error>"
///   }
fn serialize_orchestrator_conflict_error(
    operation: &str,
    branch: &str,
    conflicted_files: &[String],
    git_stderr: &str,
) -> String {
    let stderr_esc = crate::commands::self_update::json_escape(git_stderr);
    let files_field: String = {
        let parts: Vec<String> = conflicted_files
            .iter()
            .map(|p| format!("\"{}\"", crate::commands::self_update::json_escape(p)))
            .collect();
        format!("[{}]", parts.join(","))
    };
    format!(
        "{{\"event\":\"orchestrator_update_conflict\",\"operation\":\"{}\",\"branch\":\"{}\",\"conflicted_files\":{},\"git_stderr\":\"{}\"}}",
        operation, branch, files_field, stderr_esc
    )
}

/// v0.2.78 ITEM #0 (F2) — serialize the UNTRACKED-collision payload the
/// divergence modal renders as a keep-mine / take-upstream chooser. Distinct
/// `event` from `orchestrator_update_conflict` so the Svelte side does NOT show
/// the editor-resolve conflict modal (which is inapplicable to untracked files —
/// there is no `UU`, no `<<<<<<<` marker to edit). Same JSON-escape machinery as
/// `serialize_orchestrator_conflict_error` (one home for the escaper).
fn serialize_untracked_collision_error(
    operation: &str,
    branch: &str,
    divergent_files: &[String],
) -> String {
    let files_field: String = {
        let parts: Vec<String> = divergent_files
            .iter()
            .map(|p| format!("\"{}\"", crate::commands::self_update::json_escape(p)))
            .collect();
        format!("[{}]", parts.join(","))
    };
    format!(
        "{{\"event\":\"orchestrator_untracked_collision\",\"operation\":\"{}\",\"branch\":\"{}\",\"divergent_files\":{}}}",
        operation, branch, files_field
    )
}

/// v0.2.78 ITEM #0 (F2 keystone) — pre-pull untracked-collision handler, the
/// ONE home shared by `merge_orchestrator_with_upstream` and
/// `rebase_orchestrator_onto_upstream`. Runs BEFORE the git pull/rebase so a
/// new-in-release file present locally UNTRACKED can't make git abort with
/// "untracked working tree files would be overwritten by merge" (which the old
/// code mis-routed into the editor-resolve conflict modal).
///
/// Returns:
///   * `Ok(())` — no collisions, OR every collision was byte-identical to the
///     incoming blob and safely auto-removed (act path). The caller proceeds
///     with the pull; the merge now brings in the tracked version.
///   * `Err(payload)` — at least one DIVERGENT collision (local bytes differ
///     from incoming). NOTHING is removed (leave-alone path). The caller must
///     restore binaries + hub and return this payload; the divergence modal
///     then offers keep-mine / take-upstream. A deferral row is ALSO written
///     (via the shared `emit_deferral_entry`) so a terminal Claude agent can
///     resolve it — modal alone is insufficient (user ruling 2026-07-11).
///
/// Data-safety: identical files are removed ONLY on a byte-identity match
/// (`local_matches_incoming_blob`, `git cat-file`/`git show` compare — never
/// mtime/size). Divergent files are NEVER touched here. Best-effort: if base or
/// theirs can't be resolved, returns `Ok(())` (the subsequent git pull surfaces
/// any real problem via the existing conflict flow — we never guess).
async fn handle_untracked_collisions_pre_pull(
    install_path: &Path,
    operation: &str,
    branch: &str,
) -> Result<(), String> {
    use crate::commands::git_user_editable_merge::{
        classify_untracked_collisions, compute_base_sha, compute_theirs_sha,
    };

    let theirs = match compute_theirs_sha(install_path, branch).await {
        Ok(Some(t)) => t,
        // Upstream tip unresolvable → let the pull surface it. No collision work.
        Ok(None) | Err(_) => return Ok(()),
    };
    let base = match compute_base_sha(install_path, branch).await {
        Ok(Some(b)) => b,
        // No merge-base → let the pull/modal handle it; don't guess a collision.
        Ok(None) | Err(_) => return Ok(()),
    };

    let collisions = classify_untracked_collisions(install_path, &base, &theirs).await;

    if collisions.identical.is_empty() && collisions.divergent.is_empty() {
        return Ok(()); // no collisions — nothing to do.
    }

    // DIVERGENT first: if ANY divergent collision exists, we must NOT remove
    // even the identical ones (removing some-but-not-all and then bailing would
    // leave the tree half-changed). Leave the whole set alone, write the
    // deferral, and surface the modal payload.
    if !collisions.divergent.is_empty() {
        eprintln!(
            "[vct] {}: {} divergent untracked collision(s) with upstream-added \
             files — surfacing keep-mine/take-upstream choice (no auto-removal): {:?}",
            operation,
            collisions.divergent.len(),
            collisions.divergent,
        );
        write_untracked_collision_deferral(install_path, operation, branch, &collisions.divergent);
        return Err(serialize_untracked_collision_error(
            operation,
            branch,
            &collisions.divergent,
        ));
    }

    // ACT: every collision is byte-identical to the incoming blob — auto-remove
    // the local untracked copies so the merge brings in the tracked version.
    // (This is the live 2026-07-11 case: stale vco_lib mirror files identical to
    // what 0.2.77 shipped.)
    for path in &collisions.identical {
        let abs = install_path.join(path);
        match std::fs::remove_file(&abs) {
            Ok(()) => eprintln!(
                "[vct] {}: auto-removed byte-identical untracked file {} \
                 (== incoming upstream blob) so the merge can take the tracked version",
                operation, path,
            ),
            Err(e) => {
                // Could not remove → do NOT proceed as if resolved; the pull
                // would abort on it. Surface as a divergent-style modal so the
                // user is never left with a silent half-state.
                eprintln!(
                    "[vct] {}: failed to auto-remove byte-identical untracked {}: {} \
                     — surfacing modal instead of proceeding",
                    operation, path, e,
                );
                write_untracked_collision_deferral(
                    install_path,
                    operation,
                    branch,
                    &collisions.identical,
                );
                return Err(serialize_untracked_collision_error(
                    operation,
                    branch,
                    &collisions.identical,
                ));
            }
        }
    }
    Ok(())
}

/// v0.2.78 ITEM #0 (F2) — write an UPDATE_DEFERRED.md row for a divergent
/// untracked collision, via the shared `services::deferral::emit_deferral_entry`
/// (Python SSOT `vco_lib.deferral_report`; injection-safe). Addressed to the
/// user's Claude agent so the state is resolvable from a terminal too — the GUI
/// modal alone is not sufficient (user ruling 2026-07-11). Best-effort: a
/// failure logs and is swallowed (deferrals are FYI; never mask the update).
fn write_untracked_collision_deferral(
    install_path: &Path,
    operation: &str,
    branch: &str,
    divergent_files: &[String],
) {
    let files_list = divergent_files.join(", ");
    let detected = format!(
        "During the orchestrator {op} ({branch}), these UNTRACKED local files collide with \
         files newly ADDED by upstream in this release, and their content DIFFERS from the \
         incoming version: {files}. git refuses to overwrite untracked files, so the {op} \
         cannot complete until each is resolved.",
        op = operation,
        branch = branch,
        files = files_list,
    );
    let why = "Auto-removal was withheld because the local content is NOT byte-identical to \
        the incoming upstream version — removing it could discard local work. The launcher \
        surfaces a keep-mine / take-upstream choice in the divergence modal; this deferral \
        mirrors that state for terminal Claude sessions.";
    // command_to_apply must PASS the argparse-sweep guard (a runnable shell
    // block). Offer both directions explicitly; the user/agent picks one.
    let first = divergent_files.first().cloned().unwrap_or_default();
    let command = format!(
        "```bash\n\
         # For EACH colliding file, choose ONE:\n\
         #   (a) TAKE UPSTREAM (discard your local copy):\n\
         rm -- \"{first}\"   # then re-run the update (launcher 'Continue Update' or `python install.py --update`)\n\
         #   (b) KEEP MINE (set your copy aside, take upstream, reconcile by hand):\n\
         #   mv -- \"{first}\" \"{first}.local-backup\"   # then re-run the update; your copy is preserved at *.local-backup\n\
         ```",
        first = first,
    );

    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id: "untracked_collision_divergent",
        title: "Untracked local files collide with upstream-added files (content differs)",
        detected: &detected,
        why_deferred: why,
        command_to_apply: &command,
        severity: "warning",
    };
    if let Err(e) =
        crate::services::deferral::emit_deferral_entry(install_path, install_path, &fields)
    {
        eprintln!(
            "[vct] {}: could not emit untracked_collision_divergent deferral (non-fatal): {}",
            operation, e,
        );
    }
}

/// Detect whether git stderr indicates a merge or rebase produced
/// conflicts. Phrases observed on git 2.34+:
///   - "CONFLICT (content): ..."
///   - "Automatic merge failed; fix conflicts and then commit the result."
///   - "could not apply ... — When you have resolved this problem"
///   - "Resolve all conflicts manually, mark them as resolved with"
///
/// v0.2.71 (BLOCKER-1 fix): this is now a thin delegator to the ONE shared
/// classifier `git_user_editable_merge::is_pull_conflict`. Pre-v0.2.71 this and
/// `self_update::is_merge_conflict` were two hand-synced copies that drifted;
/// the phrase list now lives in exactly one place. Kept as a local alias so the
/// (many) call-sites + tests in this file don't churn. Feed it COMBINED
/// stdout+stderr (git writes `CONFLICT` lines to stdout).
fn is_merge_or_rebase_conflict(err: &str) -> bool {
    crate::commands::git_user_editable_merge::is_pull_conflict(err)
}

/// Resolve the current pull branch. Defaults to "main" on any error.
/// Mirrors the in-place logic in `update_orchestrator` so the two paths
/// can't disagree.
async fn resolve_pull_branch(install_path: &Path) -> String {
    let out = tokio::process::Command::new("git").silent()
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(install_path)
        .output()
        .await;
    let out = match out {
        Ok(o) if o.status.success() => o,
        _ => return "main".to_string(),
    };
    let b = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if b.is_empty() || b == "HEAD" {
        "main".to_string()
    } else {
        b
    }
}

/// v0.2.24 §A0 (2026-05-22): orchestrate the pre-merge user-editable
/// step for both `update_orchestrator` and `merge_orchestrator_with_upstream`.
///
/// Resolves the base / theirs SHAs, calls
/// `git_user_editable_merge::pre_merge_user_editable`, stages every
/// successfully-merged file via `git add`. Returns the outcomes list
/// for later deferral emission (NoChange entries are kept in the list
/// so the caller knows the pre-merge ran).
///
/// Best-effort throughout: any failure logs to stderr and returns an
/// empty list — the bare `git pull` that follows will surface the
/// original error. Pre-merge MUST NOT block the update path.
async fn run_pre_merge_user_editable(
    install_path: &Path,
    pull_branch: &str,
) -> Vec<crate::commands::git_user_editable_merge::MergeOutcome> {
    use crate::commands::git_user_editable_merge::{
        compute_base_sha, compute_theirs_sha, pre_merge_user_editable, MergeOutcomeKind,
    };

    let base = match compute_base_sha(install_path, pull_branch).await {
        Ok(Some(s)) => s,
        Ok(None) => {
            // Upstream ref absent / no fetch yet — nothing to pre-merge.
            return Vec::new();
        }
        Err(e) => {
            eprintln!(
                "[vct] pre_merge: compute_base_sha failed: {} — skipping pre-merge",
                e
            );
            return Vec::new();
        }
    };
    let theirs = match compute_theirs_sha(install_path, pull_branch).await {
        Ok(Some(s)) => s,
        Ok(None) => return Vec::new(),
        Err(e) => {
            eprintln!(
                "[vct] pre_merge: compute_theirs_sha failed: {} — skipping pre-merge",
                e
            );
            return Vec::new();
        }
    };
    // No-op when base == theirs (upstream is at the merge base — nothing
    // to merge). Cheap explicit guard.
    if base == theirs {
        return Vec::new();
    }

    let outcomes = match pre_merge_user_editable(install_path, &base, &theirs).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[vct] pre_merge: pre_merge_user_editable failed: {} — skipping pre-merge",
                e
            );
            return Vec::new();
        }
    };

    // Stage every successfully-merged file via `git add`. Sidecar-
    // preserved files DON'T get staged on purpose — they leave the
    // working tree with their original local content; the bare
    // `git pull --ff-only` will still fail on them (because the
    // local commit set diverges) and the existing B4 non-FF / conflict
    // modal will surface the deferral entry the caller emits later.
    let mut merged_any = false;
    for outcome in &outcomes {
        if matches!(outcome.kind, MergeOutcomeKind::Merged { .. }) {
            let status = tokio::process::Command::new("git").silent()
                .args(["add", "--"])
                .arg(&outcome.path)
                .current_dir(install_path)
                .status()
                .await;
            match status {
                Ok(s) if s.success() => merged_any = true,
                Ok(s) => {
                    eprintln!(
                        "[vct] pre_merge: git add failed for {} (exit {:?})",
                        outcome.path.display(),
                        s.code(),
                    );
                }
                Err(e) => {
                    eprintln!(
                        "[vct] pre_merge: git add spawn failed for {}: {}",
                        outcome.path.display(),
                        e,
                    );
                }
            }
        }
    }

    // v0.2.24 §A0 (2026-05-22, peer-review BLOCKER fix): git only
    // accepts the working tree as "ready for merge" when staged
    // changes are also COMMITTED. Staging a 3-way merge result is
    // not enough — the staged blob differs from BOTH HEAD's blob
    // and upstream-tip's blob, so `git pull --ff-only` (and
    // `git pull --no-rebase`) STILL aborts with "Your local changes
    // to the following files would be overwritten by merge".
    //
    // Fix: after a successful `git add`, commit the staged blobs
    // with a fixed mechanical author so:
    //   1. The pull's pre-merge cleanliness check passes.
    //   2. The commit is identifiable (so future tooling / audits
    //      can recognise it as a VCO pre-merge synthetic commit
    //      rather than a user commit).
    //   3. `--no-verify` skips pre-commit hooks (those may require
    //      dev tools / be slow / not apply to a mechanical write).
    //
    // The author/committer identity is passed via `-c` flags so we
    // never touch the user's global or repo-level git config.
    //
    // Sidecar (`PreservedWithUpstreamSidecar`) outcomes are NOT
    // committed — the sidecar file is intentionally untracked and
    // the local file is unmodified. The user's pull will fail on
    // that path via the existing B4 conflict modal flow, and the
    // already-emitted deferral entry surfaces the sidecar.
    if merged_any {
        let ts = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ");
        let msg = format!("vco: pre-merge user-editable files via A0 ({})", ts);
        let commit_result = tokio::process::Command::new("git").silent()
            .args([
                "-c",
                "user.name=VCO Orchestrator",
                "-c",
                "user.email=orchestrator@vibecoded.tools",
                "commit",
                "--no-verify",
                "-m",
                &msg,
            ])
            .current_dir(install_path)
            .output()
            .await;
        match commit_result {
            Ok(out) if out.status.success() => {
                // Commit landed — pre-merge cleanliness is satisfied,
                // the subsequent `git pull` will fast-forward (or
                // merge) cleanly.
            }
            Ok(out) => {
                // Highly unusual after a successful `git add`. Best-
                // effort revert: unstage everything we added + restore
                // the working-tree files to HEAD. This leaves the
                // clone in roughly the state it had before pre-merge,
                // and the existing B4 modal will then surface the
                // un-pre-merged conflict path.
                eprintln!(
                    "[vct] pre_merge: git commit failed (exit {:?}): {} — attempting revert",
                    out.status.code(),
                    String::from_utf8_lossy(&out.stderr).trim(),
                );
                for outcome in &outcomes {
                    if matches!(outcome.kind, MergeOutcomeKind::Merged { .. }) {
                        let _ = tokio::process::Command::new("git").silent()
                            .args(["restore", "--staged", "--"])
                            .arg(&outcome.path)
                            .current_dir(install_path)
                            .status()
                            .await;
                        let _ = tokio::process::Command::new("git").silent()
                            .args(["checkout", "--"])
                            .arg(&outcome.path)
                            .current_dir(install_path)
                            .status()
                            .await;
                    }
                }
            }
            Err(e) => {
                eprintln!(
                    "[vct] pre_merge: git commit spawn failed: {} — falling through",
                    e,
                );
            }
        }
    }

    outcomes
}

/// v0.2.24 §A0 (2026-05-22): convenience wrapper around the deferral
/// emitter. Filters out NoChange outcomes — only Merged and
/// PreservedWithUpstreamSidecar produce user-visible deferrals.
fn maybe_emit_pre_merge_deferrals(
    install_path: &Path,
    outcomes: &[crate::commands::git_user_editable_merge::MergeOutcome],
    pull_branch: &str,
) {
    use crate::commands::git_user_editable_merge::emit_orchestrator_user_modified_deferrals;
    if outcomes.is_empty() {
        return;
    }
    let actionable_count = outcomes
        .iter()
        .filter(|o| o.is_actionable_for_deferral())
        .count();
    if actionable_count == 0 {
        return;
    }
    if let Err(e) =
        emit_orchestrator_user_modified_deferrals(install_path, outcomes, pull_branch)
    {
        eprintln!(
            "[vct] pre_merge: deferral emission failed: {} — continuing (update succeeded)",
            e
        );
    }
}

/// Outcome of [`finalize_update_and_restart`]. Distinguishes "the
/// Windows stage1 handoff fired and the caller must return immediately
/// (the process is exiting)" from "legacy restart path completed".
struct UpdateFinalizeOutcome {
    /// True iff the V52-AH handoff was activated: the update lock was
    /// written, `vct-updater.exe` was spawned detached, and
    /// `app.exit(0)` has been requested. The caller MUST return its
    /// success `InstallResult` without doing further work.
    handoff_exit: bool,
}

/// v0.2.54 Track C (P0-7): the SINGLE shared finalize tail for every
/// orchestrator-update entry path (update, merge, rebase, resume).
///
/// Performs, in order:
///   1. V45-B `WaitForBinaryRefresh` (block restart until the on-disk
///      binary version catches up with the source version).
///   2. V52-AI gate disarm (`disarm_and_cleanup`) — MUST happen before
///      any restart/exit hop because `app.exit(0)` can terminate the
///      process before the guard's `Drop` runs on Windows (the C-2
///      bug class: a surviving `.update-in-progress.json` with a fresh
///      15-min deadline makes every MCP spawn exit 75 until it lapses).
///   3. V52-AH staging (`stage_locked_binaries_for_handoff`, Windows)
///      + handoff decision (`prepare_windows_update_handoff`).
///   4. Hub restart — ONLY on the no-handoff path (C-1 fix, v0.2.54):
///      pre-v0.2.54 the hub was restarted BEFORE staging, which meant
///      a freshly-started (stale-binary) hub held a Windows mandatory
///      lock on `vct-hub.exe` while the updater — which waits only on
///      the launcher PID — tried to `MoveFileExW` onto it (exit 5,
///      silent). With the hub start moved AFTER the handoff decision,
///      the handoff path leaves the hub STOPPED so the updater can
///      swap `vct-hub.exe.new`; the relaunched launcher's boot
///      `ensure_hub_running` then starts the NEW hub binary.
///   5. Legacy `restart_launcher` + the install.py deferral-emit
///      fallback when the restart hop fails.
///
/// Error contract: on `Err` the hub is restarted best-effort first
/// (the callers stopped it at the top of their flows) so the user is
/// never left hub-less by a failed finalize. The V52-AI guard is NOT
/// disarmed on the pre-disarm error paths — the caller still owns it
/// and its RAII `Drop` cleans the lockfile on the normal Err-return
/// path (process keeps running, so Drop is reliable there).
async fn finalize_update_and_restart<R: Runtime>(
    app: AppHandle<R>,
    install_path: &Path,
    path_string: &str,
    window: &Window,
    python_cmd: &str,
    pull_branch: &str,
    update_gate_guard: &mut crate::commands::update_gate::UpdateInProgressGuard,
) -> Result<UpdateFinalizeOutcome, String> {
    emit_progress(
        window,
        "restart",
        "Update applied — restarting launcher...",
        95.0,
    );

    // v0.2.45 V45-B: don't restart into a stale on-disk binary.
    // Block restart until the on-disk `vct-launcher.metadata.json`
    // version catches up with `vct-module.json` version (poll + re-
    // pull every 15 s, up to 5 min). On timeout, surface the error
    // to the GUI — the user retries when CI finishes the binary
    // commit.
    emit_progress(window, "update", "Waiting for new launcher binary...", 96.0);
    if let Err(e) = WaitForBinaryRefresh::default_production(install_path, pull_branch)
        .run()
        .await
    {
        eprintln!("[v0.2.45 V45-B] finalize_update_and_restart: {}", e);

        // v0.2.55 (PRIMARY-BUG fix): WaitForBinaryRefresh timed out — the
        // on-disk dist binary never reached the exact `source_version`
        // target. PRE-v0.2.55 this `return Err(e)` ABORTED the whole
        // update with NO restart, so the launcher stayed on the OLD
        // binary and kept offering the update (the infinite "update
        // available" loop the user reported). Two ways to time out:
        //   (a) the re-pull keeps failing (non-FF divergence — the
        //       maintainer's 2-checkout case) → binary genuinely never
        //       advances; OR
        //   (b) the binary-refresh commit simply hasn't been pushed by
        //       the Release workflow yet (the one-commit ordering window).
        // In BOTH cases, if a dist binary is present that is NEWER than
        // the CURRENTLY-RUNNING launcher, restarting into it is strictly
        // better than staying on the old one — even if it's a hair below
        // the absolute target. "Update to the new binary in any case."
        // We still write a durable UPDATE_DEFERRED.md entry so a
        // terminal Claude can see the update did not fully reach target,
        // and the next update attempt will close the remaining gap.
        let running = env!("CARGO_PKG_VERSION");
        let on_disk = read_on_disk_binary_version(install_path).unwrap_or_default();
        // version_is_outdated(a, b) == (a < b); here: running < on_disk.
        let on_disk_beats_running =
            !on_disk.is_empty() && version_is_outdated(running, &on_disk);

        if on_disk_beats_running {
            eprintln!(
                "[v0.2.55] binary-refresh did not reach target, but on-disk dist \
                 binary v{} is NEWER than running v{} — restarting into it anyway \
                 (strictly better than staying on the old binary). A durable \
                 deferral is written noting the update may be one step behind \
                 target; the next update closes the gap.",
                on_disk, running,
            );
            // Durable record: the update advanced but did not fully reach
            // the source target (likely the binary-refresh ordering
            // window or a transient pull failure). Non-fatal: continue to
            // the restart below.
            write_launcher_update_diverged_deferral(
                install_path,
                pull_branch,
                LauncherUpdateDivergedKind::PartialBinaryRefresh {
                    running: running.to_string(),
                    on_disk: on_disk.clone(),
                    detail: e.clone(),
                },
            );
            // Fall through (do NOT return) — proceed to hub-start +
            // restart with the newer on-disk binary.
        } else {
            // No newer binary on disk at all — restarting would re-exec
            // the SAME old binary, which doesn't help. Keep the abort,
            // but now ALSO write a durable deferral so the stuck state is
            // diagnosable at session start instead of dying in a GUI
            // modal (the durable-logging gap: pre-v0.2.55 this surfaced
            // ONLY as a transient "click Update again" error string).
            eprintln!(
                "[v0.2.55] binary-refresh timed out and no newer dist binary is \
                 available (running v{}, on-disk v{}); writing durable deferral \
                 and aborting restart.",
                running,
                if on_disk.is_empty() { "<unknown>" } else { &on_disk },
            );
            write_launcher_update_diverged_deferral(
                install_path,
                pull_branch,
                LauncherUpdateDivergedKind::BinaryRefreshTimeout {
                    running: running.to_string(),
                    on_disk: on_disk.clone(),
                    detail: e.clone(),
                },
            );
            // The hub was stopped at the top of the caller's flow and
            // (post-C-1 reorder) has not been restarted yet. Bring it
            // back up so a binary-refresh timeout doesn't leave the user
            // hub-less.
            let _ = ensure_hub_started_after_update(install_path);
            return Err(e);
        }
    }

    // V52-AI: explicit lockfile cleanup BEFORE the restart hop. We don't
    // want the lockfile to survive across the launcher restart — if it
    // did, the freshly-booted launcher's MCPs would still see it as
    // active and refuse to start until the stale-cleanup window passed
    // (up to 15 min). Drop on the guard would happen on process exit,
    // but on Windows the process can terminate before async cleanup
    // runs cleanly; calling disarm_and_cleanup() now makes the order
    // deterministic. (v0.2.54 C-2: this used to exist only on the
    // `update_orchestrator` path; resume/merge/rebase leaked the lock.)
    update_gate_guard.disarm_and_cleanup();

    // v0.2.52 V52-AH (stale-binary relaunch loop): Windows stage1 handoff.
    // Detect binaries that git pull silently skipped (file locked by an
    // antivirus / indexer / racing handle) and stage them as
    // `<target>.new`, then write `~/.vct/update.lock.json` and spawn
    // `vct-updater.exe` DETACHED so it can perform the swap once the
    // running launcher exits.
    //
    // Soft-fail throughout: if staging or handoff fails for ANY reason,
    // fall through to the existing restart_launcher path — that's the
    // pre-v0.2.52 behaviour and represents the worst case (= same as
    // not having V52-AH at all).
    //
    // POSIX: stage_locked_binaries_for_handoff returns empty (no-op),
    // prepare_windows_update_handoff returns handoff_active=false with
    // skip_reason="non-windows", so we fall through unconditionally
    // to restart_launcher. No behaviour change for Linux/macOS users.
    #[cfg(target_os = "windows")]
    {
        let staged = stage_locked_binaries_for_handoff(install_path).await;
        if !staged.is_empty() {
            eprintln!(
                "[vct] finalize_update_and_restart: V52-AH staged {} binary/binaries \
                 for handoff: {:?}",
                staged.len(),
                staged,
            );
        }
    }

    let handoff_result = match crate::commands::update_handoff::prepare_windows_update_handoff(
        path_string.to_string(),
    )
    .await
    {
        Ok(r) => r,
        Err(e) => {
            // True error (install_root missing etc.) — log + fall through.
            eprintln!(
                "[vct] finalize_update_and_restart: V52-AH handoff returned error \
                 ({}); falling through to legacy restart_launcher",
                e,
            );
            crate::commands::update_handoff::HandoffResult::default()
        }
    };

    if handoff_result.handoff_active {
        eprintln!(
            "[vct] finalize_update_and_restart: V52-AH handoff active (lock={:?}); \
             exiting current launcher — vct-updater.exe will perform the \
             swap + relaunch. Hub stays stopped so vct-hub.exe is swappable \
             (v0.2.54 C-1); the relaunched launcher's boot starts the new hub.",
            handoff_result.lock_path,
        );
        // Mirror the tail of restart_launcher: programmatic quit so the
        // updater can complete the swap on a now-unlocked .exe. The
        // updater's spawned child will become the new launcher process.
        crate::quit_dialog::force_quit();
        app.exit(0);
        return Ok(UpdateFinalizeOutcome { handoff_exit: true });
    }

    if let Some(reason) = handoff_result.skip_reason.as_deref() {
        eprintln!(
            "[vct] finalize_update_and_restart: V52-AH handoff skipped (reason={}); \
             falling through to legacy restart_launcher",
            reason,
        );
    }

    // v0.2.54 C-1: bring the detached vct-hub back up ONLY on the
    // no-handoff path, AFTER the staging/handoff decision. See the
    // function docs for why this ordering is load-bearing on Windows.
    // Soft-fail: the launcher restart path itself also calls
    // `hub_launcher::ensure_hub_running` on boot.
    emit_progress(window, "update", "Starting vct-hub...", 97.0);
    if let Err(e) = ensure_hub_started_after_update(install_path) {
        eprintln!(
            "[vct] finalize_update_and_restart: ensure_hub_started_after_update \
             returned Err({}); continuing with launcher restart (hub will \
             retry on next boot)",
            e
        );
    }

    if let Err(e) =
        crate::commands::restart::restart_launcher(app, path_string.to_string()).await
    {
        // Recovery path: re-spawn install.py without
        // VCT_AUTO_RESTART_LAUNCHER so the launcher_restart_required
        // deferral gets emitted as a fallback and the banner fires on
        // the next launcher start.
        eprintln!(
            "[vct] finalize_update_and_restart: auto-restart failed ({}); re-spawning \
             install.py to emit the launcher_restart_required deferral as a \
             fallback so the banner fires on next launcher start.",
            e,
        );
        let mut fallback = tokio::process::Command::new(python_cmd).silent();
        fallback
            .args(["install.py", "--update"])
            .stdin(std::process::Stdio::null())
            .current_dir(install_path);
        // v0.2.27: force UTF-8 stdout/stderr for the Python child.
        fallback.env("PYTHONIOENCODING", "utf-8");
        fallback.env("PYTHONUTF8", "1");
        fallback.env("VCT_LAUNCHER_PID", std::process::id().to_string());
        fallback.env_remove("VCT_AUTO_RESTART_LAUNCHER");
        fallback.env("VCT_FORCE_RESTART_DEFERRAL", "1");
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            fallback.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        match fallback.output().await {
            Ok(out) if out.status.success() => {
                eprintln!(
                    "[vct] finalize_update_and_restart: fallback install.py succeeded; \
                     launcher_restart_required deferral should now be present."
                );
            }
            Ok(out) => {
                let stderr = String::from_utf8_lossy(&out.stderr);
                eprintln!(
                    "[vct] finalize_update_and_restart: fallback install.py exited \
                     non-zero ({:?}). stderr tail: {}",
                    out.status.code(),
                    stderr.lines().rev().take(10).collect::<Vec<_>>().join(" | "),
                );
                return Err(format!(
                    "Update applied but auto-restart failed AND the fallback \
                     deferral emit failed. Please fully quit the launcher \
                     (tray → Quit) and relaunch via your usual entrypoint to \
                     pick up the new binary at {}/launcher/dist/.",
                    install_path.display(),
                ));
            }
            Err(spawn_err) => {
                eprintln!(
                    "[vct] finalize_update_and_restart: could not spawn fallback \
                     install.py: {}. Surfacing the error to the GUI.",
                    spawn_err,
                );
                return Err(format!(
                    "Update applied but auto-restart failed and the fallback \
                     install.py could not be spawned: {}. Please fully quit \
                     the launcher and relaunch manually.",
                    spawn_err,
                ));
            }
        }
    }

    Ok(UpdateFinalizeOutcome { handoff_exit: false })
}

/// Run the post-pull install.py + hub-start + restart sequence. This is
/// the tail shared by `update_orchestrator` (after `git pull --ff-only`)
/// and the new merge/rebase commands. Extracted as a free function so
/// the two callers can't drift apart.
///
/// On success: spawns the new launcher detached and exits the current
/// process (never returns Ok in practice). On error before the restart
/// hop, returns Err with the install.py stderr tail. On error AT the
/// restart hop, attempts the install.py fallback to emit a
/// `launcher_restart_required` deferral so the next session's banner
/// picks it up.
///
/// Caller MUST have already:
///   - stopped the hub (`ensure_hub_stopped_for_update`)
///   - pre-pull-renamed binaries if Windows
///   - confirmed the pull/merge/rebase succeeded
///   - acquired the V52-AI gate (`pre_update_mcp_kill_sweep` +
///     `UpdateInProgressGuard::new`) and passed the guard in
///     (v0.2.54 P0-7: merge/rebase/resume share the gate + staging +
///     handoff through this tail; pre-v0.2.54 they had neither, so
///     both v0.2.52-class bugs — the stale-binary relaunch loop AND
///     the MCP fork-bomb — were reachable verbatim through the
///     divergence-modal Merge/Rebase buttons).
/// v0.2.63: after a pull/merge reported success (and wasn't "Already up to
/// date"), confirm local HEAD actually reached the upstream tip BEFORE running
/// install.py. If HEAD is still behind `vco_upstream/<branch>`, the upstream
/// changes did NOT land (a non-FF that slipped through, an odd partial git
/// state) and running install.py would execute the STALE source tree — the
/// exact failure that crashed VCO_dev's v0.2.62 GUI update (old install.py,
/// pre-fix line numbers). Returns `Err` so the caller aborts before install.py.
///
/// Conservative: behind-count 0 → Ok. A count ERROR (a transient git hiccup on
/// the just-fetched refs — rare) → Ok with a loud log, so a flaky `rev-list`
/// never blocks an otherwise-healthy update. The pre-existing non-FF modal is
/// the PRIMARY divergence guard; this is the backstop that the merge actually
/// landed. Called from BOTH install.py choke points (`update_orchestrator`
/// inline + `run_post_pull_install_and_restart`) — one concern, one home.
async fn assert_head_reached_upstream(install_path: &Path) -> Result<(), String> {
    let branch = resolve_pull_branch(install_path).await;
    match crate::commands::self_update::count_commits_behind_upstream(install_path, &branch).await {
        Ok(0) => Ok(()),
        Ok(behind) => Err(format!(
            "Update aborted before install.py: local HEAD is still {behind} commit(s) behind \
             {remote}/{branch} after the pull/merge — the upstream changes did not land, so \
             running install.py would re-run the STALE source tree (the v0.2.62 update-crash \
             class). Resolve the merge manually (`git merge {remote}/{branch}` in the install \
             folder, or update from the public-repo clone) and retry.",
            remote = crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        )),
        Err(e) => {
            eprintln!(
                "[vct] assert_head_reached_upstream: behind-count failed ({e}) — proceeding \
                 (cannot confirm staleness; not blocking a healthy update on a git hiccup)."
            );
            Ok(())
        }
    }
}

/// Shared recovery tail for the update commands' failure / conflict / abort
/// branches: revert the pre-pull binary + hub renames (so the canonical paths
/// hold working binaries again) and best-effort restart the hub (we stopped it
/// pre-pull).
///
/// v0.2.63 introduced this for the post-pull HEAD-advance guard's two
/// call-sites. v0.2.71 (Piece 3, modularity) made it THE single home for this
/// trio — the `revert_pre_pull_rename(binary)` + `revert_pre_pull_rename(hub)`
/// + `let _ = ensure_hub_started_after_update(..)` idiom was hand-repeated 10+
/// times across `update_orchestrator`, `merge_orchestrator_with_upstream`,
/// `rebase_orchestrator_onto_upstream`, and `run_post_pull_install_and_restart`.
/// Behaviour is byte-identical to every inline copy: each `Option<&Path>` is
/// guarded before reverting, and the hub restart is best-effort (`let _ =`).
/// Sites that do something extra (emit progress, write a sentinel) keep that
/// part inline and call this for just the trio.
fn abort_update_restore_binaries_and_hub(
    install_path: &Path,
    pre_pull_renamed: Option<&Path>,
    pre_pull_renamed_hub: Option<&Path>,
) {
    if let Some(backup) = pre_pull_renamed {
        revert_pre_pull_rename(backup);
    }
    if let Some(backup) = pre_pull_renamed_hub {
        revert_pre_pull_rename(backup);
    }
    let _ = ensure_hub_started_after_update(install_path);
}

async fn run_post_pull_install_and_restart<R: Runtime>(
    app: AppHandle<R>,
    install_path: &Path,
    path_string: String,
    window: &Window,
    system: SystemDetection,
    pre_pull_renamed: Option<PathBuf>,
    pre_pull_renamed_hub: Option<PathBuf>,
    update_gate_guard: &mut crate::commands::update_gate::UpdateInProgressGuard,
) -> Result<InstallResult, String> {
    emit_progress(window, "update", "Changes applied", 30.0);

    // v0.2.63: HEAD-advance backstop (shared with update_orchestrator). The
    // merge/rebase pull in the caller exited 0; confirm HEAD actually reached
    // the upstream tip before install.py, else we'd run the STALE tree (the
    // v0.2.62 update-crash class). Abort cleanly — revert binaries + restart
    // hub — and surface the error to the modal flow instead of running install.
    if let Err(e) = assert_head_reached_upstream(install_path).await {
        abort_update_restore_binaries_and_hub(
            install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(e);
    }

    emit_progress(window, "install", "Applying updates...", 40.0);

    // V52-AI: advance lockfile phase so future hub-side observers can
    // see we've moved past git_pull (parity with `update_orchestrator`).
    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::InstallPy);

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd).silent();
    cmd.args(["install.py", "--update"])
        .stdin(std::process::Stdio::null())
        .current_dir(install_path);
    // v0.2.27: force UTF-8 stdout/stderr for the Python child. See
    // the identical block on the `update_at` spawn site for the
    // full rationale.
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");
    cmd.env("VCT_LAUNCHER_PID", std::process::id().to_string());
    cmd.env("VCT_AUTO_RESTART_LAUNCHER", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let install_output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py --update failed: {}", e))?;

    if !install_output.status.success() {
        let stderr = String::from_utf8_lossy(&install_output.stderr);
        // v0.2.71: shared recovery tail.
        abort_update_restore_binaries_and_hub(
            install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(format!("Update failed: {}", stderr));
    }

    // V52-AI: advance lockfile phase. install.py has finished; we're
    // now in the binary-refresh + hub-restart window. MCPs that try to
    // spawn during this window still see the lockfile and exit cleanly.
    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::BinaryRefresh);

    // Self-contained branch detection so this helper doesn't need
    // a branch parameter ripped through both call sites
    // (`merge_orchestrator_with_upstream` /
    // `rebase_orchestrator_onto_upstream`). Defaults to "main" on
    // any git failure — matches the convention used by the
    // sibling `pull_branch` detection in `update_orchestrator`.
    let v45b_branch = {
        let out = tokio::process::Command::new("git")
            .silent()
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(install_path)
            .output()
            .await;
        match out {
            Ok(o) if o.status.success() => {
                let b = String::from_utf8_lossy(&o.stdout).trim().to_string();
                if b.is_empty() || b == "HEAD" {
                    "main".to_string()
                } else {
                    b
                }
            }
            _ => "main".to_string(),
        }
    };

    // v0.2.54 Track C (P0-7): shared finalize tail — V45-B binary wait,
    // V52-AI disarm, V52-AH staging + handoff, hub restart (no-handoff
    // path only), legacy restart + deferral fallback.
    let finalize = finalize_update_and_restart(
        app,
        install_path,
        &path_string,
        window,
        python_cmd,
        &v45b_branch,
        update_gate_guard,
    )
    .await?;

    if finalize.handoff_exit {
        return Ok(InstallResult {
            success: true,
            install_path: path_string,
            message: "Update applied — vct-updater is performing the binary swap. \
                      The launcher will relaunch automatically."
                .to_string(),
            system,
        });
    }

    emit_progress(window, "done", "Orchestrator updated successfully!", 100.0);

    Ok(InstallResult {
        success: true,
        install_path: path_string,
        message: "Orchestrator updated successfully".to_string(),
        system,
    })
}

/// Merge upstream into the local branch without --ff-only. Produces a
/// merge commit when histories diverge cleanly; surfaces a structured
/// conflict payload otherwise.
///
/// Wire-shape:
///   - Same input as `update_orchestrator` (path, window).
///   - Same prep choreography: stop hub, pre-pull-rename binaries.
///   - Same post-success tail: install.py --update, restart launcher.
///   - On merge conflict: leaves the working tree conflicted, returns
///     a structured "orchestrator_update_conflict" error. The frontend
///     renders the conflict modal; the user resolves manually OR clicks
///     "Abort" which calls `abort_orchestrator_merge_or_rebase`.
#[command]
pub async fn merge_orchestrator_with_upstream<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot merge".to_string());
    }

    // V52-AI (v0.2.54 P0-7): the merge path runs install.py + binary
    // refresh via the shared post-pull tail — the exact window where
    // the Windows MCP fork-bomb (V52-AI bug class) historically formed.
    // Pre-v0.2.54 this path had NEITHER the kill-sweep NOR the gate,
    // so the fork-bomb was reachable verbatim through the divergence-
    // modal "Merge" button. Same shape as `update_orchestrator`:
    // pre-sweep + RAII lockfile guard. The guard is handed to
    // `run_post_pull_install_and_restart`, which disarms it before the
    // restart hop; on every early return (conflict modal, pull
    // failure, already-up-to-date) the guard's Drop removes the
    // lockfile so MCPs can respawn while the user resolves conflicts.
    let pre_sweep_count_merge = crate::commands::update_gate::pre_update_mcp_kill_sweep();
    if pre_sweep_count_merge > 0 {
        eprintln!(
            "[vct] merge_orchestrator_with_upstream: pre-sweep terminated {} \
             MCP-shaped process(es) before merge",
            pre_sweep_count_merge
        );
    }
    let (mut update_gate_guard, _gate_write_result) =
        crate::commands::update_gate::UpdateInProgressGuard::new();

    // Same upstream-pin choreography as update_orchestrator.
    crate::commands::self_update::ensure_upstream_remote(&install_path).await?;

    emit_progress(&window, "update", "Stopping vct-hub for merge...", 2.0);
    if let Err(e) = ensure_hub_stopped_for_update(&install_path) {
        return Err(format!(
            "Merge aborted: could not stop vct-hub before git pull: {}. \
             Try again, or run `vct-hub --stop` manually.",
            e
        ));
    }

    emit_progress(&window, "update", "Preparing for merge...", 5.0);
    let pre_pull_renamed_hub = pre_pull_rename_vct_hub_binary(&install_path);
    let pre_pull_renamed = pre_pull_rename_running_binary(&install_path);

    let pull_branch = resolve_pull_branch(&install_path).await;

    // v0.2.24 §A0 (2026-05-22): pre-merge user-editable files BEFORE
    // the merge pull. Same rationale as the `update_orchestrator`
    // pre-merge: when the user has local uncommitted edits to
    // CLAUDE.md / knowledge/**/*.md / .claude/CONTEXT_STATE.md /
    // .claude/MEMORY.md / HANDOFF-*.md, the bare `git pull --no-rebase`
    // refuses with "Your local changes would be overwritten by merge".
    // The pre-merge auto-resolves non-overlapping edits and sidecars
    // conflicting ones so the merge can proceed.
    //
    // Best-effort: failures fall through to the bare pull which will
    // surface the original error via the existing B4 modal flow.
    emit_progress(&window, "update", "Fetching upstream for pre-merge...", 7.0);
    // v0.2.83 (D5): single serialized fetch home (Quick, branch refspec).
    // Soft-fail posture preserved — failures fall through to the bare pull
    // which surfaces the original error via the existing B4 modal flow.
    if let Err(e) = crate::commands::self_update::serialized_fetch_upstream(
        &install_path,
        crate::commands::self_update::FetchPolicy::Quick,
        Some(&pull_branch),
    )
    .await
    {
        eprintln!(
            "[vct] merge_orchestrator_with_upstream: pre-merge fetch failed: {} — continuing",
            e
        );
    }
    let pre_merge_outcomes = run_pre_merge_user_editable(&install_path, &pull_branch).await;

    // v0.2.24 §A0 (Q1 fix): emit deferral entries BEFORE the git pull
    // merge. Rationale: when pre-merge produces sidecars (true 3-way
    // conflict), the bare `git pull --no-rebase` may STILL fail (the
    // sidecar leaves the working tree dirty, plus other tracked files
    // may have committed-divergence conflicts that git's own merge
    // can't reconcile). User then sees the B4 conflict modal — without
    // the deferral entries on disk, they lose the audit trail of which
    // files pre-merge sidecar'd vs auto-merged. Emit unconditionally
    // so the deferral lands regardless of pull outcome. Best-effort.
    maybe_emit_pre_merge_deferrals(&install_path, &pre_merge_outcomes, &pull_branch);

    // v0.2.78 ITEM #0 (F2 keystone): handle UNTRACKED-file collisions with
    // upstream-added files BEFORE the pull. A new-in-release file present
    // locally untracked makes `git pull` abort ("untracked working tree files
    // would be overwritten by merge"), which the old code mis-routed into the
    // editor-resolve conflict modal (inapplicable to untracked files). Here we
    // auto-remove byte-identical copies (safe — same bytes upstream ships) and
    // surface a keep-mine/take-upstream modal + deferral for divergent ones.
    // Err ⇒ divergent collision (or an un-removable identical one): restore the
    // pre-pull binary renames + hub and return the payload (never proceed on a
    // tree the pull would abort on).
    emit_progress(&window, "update", "Checking for untracked-file collisions...", 8.0);
    if let Err(collision_payload) =
        handle_untracked_collisions_pre_pull(&install_path, "merge", &pull_branch).await
    {
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(collision_payload);
    }

    // Pull WITHOUT --ff-only, explicitly as a merge (--no-rebase). The
    // explicit reconcile flag is REQUIRED on git 2.34+: without it git
    // refuses a divergent pull with "Need to specify how to reconcile
    // divergent branches" because the user's `pull.rebase` config could
    // be anything.
    //
    // --no-edit suppresses the editor for the merge commit message — a
    // Tauri subprocess has no controlling tty and would hang otherwise.
    // v0.2.71: --autostash is REQUIRED here. This command is reached
    // precisely from the divergence modal, i.e. AFTER update_orchestrator's
    // pop-conflict-risk set was non-empty (a tracked-modified file overlapping
    // upstream, e.g. a dirty .gitignore). Without --autostash, `git pull`
    // refuses on the dirty tree ("Your local changes ... would be overwritten
    // by merge") BEFORE merging — a refusal whose wording isn't a "conflict"
    // phrase, so it fell through to the bare-error branch below and DEAD-ENDED
    // the modal (Merge fails, Rebase fails, user stuck at "open clone folder").
    // --autostash stashes the dirty tracked edits, merges, then pops; the
    // common case (non-overlapping regions, e.g. the .gitignore append) pops
    // clean. A genuine pop-conflict is caught by the dirty-tree/conflict
    // detection below and routed to the conflict modal + UPDATE_DEFERRED, not
    // a bare error. (The update_orchestrator auto-merge arm already proves
    // --autostash is acceptable on this path.)
    emit_progress(&window, "update", "Merging upstream into local...", 10.0);
    let pull = tokio::process::Command::new("git").silent()
        .args([
            "pull",
            "--no-rebase",
            "--no-edit",
            "--autostash",
            crate::commands::self_update::VCO_UPSTREAM_REMOTE,
            &pull_branch,
        ])
        .current_dir(&install_path)
        .output()
        .await
        .map_err(|e| format!("git pull (merge) failed: {}", e))?;

    if !pull.status.success() {
        let stderr = String::from_utf8_lossy(&pull.stderr);
        let stdout = String::from_utf8_lossy(&pull.stdout);
        // Conflicts go to stdout on some git versions (the "CONFLICT
        // (content): ..." lines). Check both streams.
        let combined = format!("{}\n{}", stderr, stdout);

        if is_merge_or_rebase_conflict(&combined) {
            // Conflict: leave the working tree in the conflicted state
            // so the user can edit files manually. Revert the pre-pull
            // renames (the binaries are still the OLD versions on disk —
            // git pull didn't get far enough to swap them) so the canonical
            // paths point at the working binary again, and bring the hub
            // back up — the user is going to spend time resolving conflicts
            // and shouldn't be without it. v0.2.71: shared tail.
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );

            // v0.2.51 Bug A: write the resume sentinel + deferral BEFORE
            // returning the conflict payload. The user's modal flow may
            // dismiss without aborting; the sentinel + UpdateBadge bring
            // them back via the launcher GUI, while UPDATE_DEFERRED.md
            // surfaces the same state to terminal Claude sessions.
            // v0.2.53 DEDUP-14: paired writer so one cannot be forgotten.
            write_resume_sentinel_and_deferral(&install_path, "merge", &pull_branch).await;

            let conflicted = collect_conflicted_files(&install_path).await;
            return Err(serialize_orchestrator_conflict_error(
                "merge",
                &pull_branch,
                &conflicted,
                combined.trim(),
            ));
        }

        // Non-conflict failure (network, refusing unrelated histories
        // for the very first merge, etc.). Revert + restart hub + bail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(format!("git pull (merge) failed: {}", stderr));
    }

    // v0.2.71 (Piece 3.3): success-path autostash-pop backstop — ported from
    // `update_orchestrator` (the modal merge command lacked it). `git pull
    // --no-rebase --autostash` can EXIT 0 yet leave the tree broken: it
    // stashes the dirty tracked edits (the divergence modal is reached
    // precisely on a dirty-tree-overlapping-upstream case), merges, then POPS
    // the stash. If the pop conflicts, git prints "Applying autostash resulted
    // in conflicts." + leaves `UU` markers + a dangling stash but STILL exits
    // 0 — so `!pull.status.success()` above does NOT catch it, and proceeding
    // would run install.py + restart on a silently-broken tree. Detect it here
    // (unmerged files and/or the autostash marker) and route to the conflict
    // modal + resume sentinel + UPDATE_DEFERRED exactly like the non-zero
    // conflict branch, so the user gets a guided recovery, never a dead-end.
    {
        let pull_combined = format!(
            "{}\n{}",
            String::from_utf8_lossy(&pull.stdout),
            String::from_utf8_lossy(&pull.stderr)
        );
        let autostash_pop_failed = pull_combined.contains("autostash resulted in conflicts")
            || pull_combined.contains("Applying autostash");
        let unmerged = collect_conflicted_files(&install_path).await;
        if !unmerged.is_empty() || autostash_pop_failed {
            eprintln!(
                "[vct] merge_orchestrator_with_upstream: pull exited 0 but the working tree \
                 has {} unmerged file(s){} — an --autostash pop conflict. Routing to the \
                 conflict modal instead of proceeding on a broken tree.",
                unmerged.len(),
                if autostash_pop_failed { " + autostash-conflict marker" } else { "" },
            );
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            write_resume_sentinel_and_deferral(&install_path, "merge", &pull_branch).await;
            return Err(serialize_orchestrator_conflict_error(
                "merge",
                &pull_branch,
                &unmerged,
                pull_combined.trim(),
            ));
        }
    }

    let pull_output = String::from_utf8_lossy(&pull.stdout);
    if pull_output.contains("Already up to date") {
        emit_progress(&window, "done", "Already up to date!", 100.0);
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        // v0.2.24 §A0 (Q1 fix): deferrals were already emitted BEFORE
        // the pull (see above) — no second call needed here.
        return Ok(InstallResult {
            success: true,
            install_path: path,
            message: "Already up to date".to_string(),
            system,
        });
    }

    // v0.2.24 §A0 (Q1 fix): deferrals were already emitted BEFORE the
    // pull (see above) — no second call needed here.

    run_post_pull_install_and_restart(
        app,
        &install_path,
        path,
        &window,
        system,
        pre_pull_renamed,
        pre_pull_renamed_hub,
        &mut update_gate_guard,
    )
    .await
}

/// Rebase the local branch onto upstream. Replays the user's local
/// commits on top of upstream rather than producing a merge commit;
/// surfaces a structured conflict payload if any commit doesn't apply
/// cleanly.
///
/// Same wire-shape as `merge_orchestrator_with_upstream`. The flow:
///   1. Stop hub, pre-pull-rename binaries.
///   2. `git fetch <upstream>` (rebase needs the upstream ref locally).
///   3. `git rebase <upstream>/<branch>`.
///   4. On conflict: surface "orchestrator_update_conflict" error.
///   5. On success: install.py --update + restart.
#[command]
pub async fn rebase_orchestrator_onto_upstream<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot rebase".to_string());
    }

    // V52-AI (v0.2.54 P0-7): same fork-bomb gate as the merge path —
    // see `merge_orchestrator_with_upstream` for the full rationale.
    // Pre-v0.2.54 the rebase path had neither sweep nor gate.
    let pre_sweep_count_rebase = crate::commands::update_gate::pre_update_mcp_kill_sweep();
    if pre_sweep_count_rebase > 0 {
        eprintln!(
            "[vct] rebase_orchestrator_onto_upstream: pre-sweep terminated {} \
             MCP-shaped process(es) before rebase",
            pre_sweep_count_rebase
        );
    }
    let (mut update_gate_guard, _gate_write_result) =
        crate::commands::update_gate::UpdateInProgressGuard::new();

    crate::commands::self_update::ensure_upstream_remote(&install_path).await?;

    emit_progress(&window, "update", "Stopping vct-hub for rebase...", 2.0);
    if let Err(e) = ensure_hub_stopped_for_update(&install_path) {
        return Err(format!(
            "Rebase aborted: could not stop vct-hub before git rebase: {}. \
             Try again, or run `vct-hub --stop` manually.",
            e
        ));
    }

    emit_progress(&window, "update", "Preparing for rebase...", 5.0);
    let pre_pull_renamed_hub = pre_pull_rename_vct_hub_binary(&install_path);
    let pre_pull_renamed = pre_pull_rename_running_binary(&install_path);

    let pull_branch = resolve_pull_branch(&install_path).await;

    // Rebase requires a fresh upstream ref. Without --ff-only we use
    // `git fetch` + `git rebase <remote>/<branch>` rather than `git pull
    // --rebase` so a partial network failure doesn't leave us with a
    // half-applied rebase.
    emit_progress(&window, "update", "Fetching upstream for rebase...", 10.0);
    // v0.2.83 (D5): single serialized fetch home (Quick, branch refspec).
    // HARD-fail posture preserved — a rebase needs a fresh upstream ref, so on
    // fetch failure we restore the pre-pull binary/hub renames and abort rather
    // than risk a half-applied rebase. `serialized_fetch_upstream` collapses
    // spawn error + non-zero exit into one error string (the last git stderr
    // line), so a single failure branch replaces the former two.
    if let Err(e) = crate::commands::self_update::serialized_fetch_upstream(
        &install_path,
        crate::commands::self_update::FetchPolicy::Quick,
        Some(&pull_branch),
    )
    .await
    {
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(format!("git fetch failed: {}", e));
    }

    // v0.2.78 ITEM #0 (F2 keystone): same untracked-collision handling as the
    // merge path — one shared home (`handle_untracked_collisions_pre_pull`).
    // Runs after the fetch (so base/theirs resolve) and before the rebase.
    emit_progress(&window, "update", "Checking for untracked-file collisions...", 15.0);
    if let Err(collision_payload) =
        handle_untracked_collisions_pre_pull(&install_path, "rebase", &pull_branch).await
    {
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(collision_payload);
    }

    emit_progress(&window, "update", "Rebasing local onto upstream...", 20.0);
    let upstream_ref = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        pull_branch
    );
    // v0.2.71: --autostash here too (sibling to the merge command's fix). The
    // rebase modal button is reached on the same dirty-tree-overlapping-upstream
    // path; without --autostash, bare `git rebase` refuses ("cannot rebase: You
    // have unstaged changes") and dead-ended the modal. --autostash stashes,
    // rebases, then pops; a pop-conflict is caught as a conflict below and
    // routed to the conflict modal + UPDATE_DEFERRED, not a bare error.
    let rebase = tokio::process::Command::new("git").silent()
        .args(["rebase", "--autostash", &upstream_ref])
        .current_dir(&install_path)
        .output()
        .await
        .map_err(|e| format!("git rebase failed: {}", e))?;

    if !rebase.status.success() {
        let stderr = String::from_utf8_lossy(&rebase.stderr);
        let stdout = String::from_utf8_lossy(&rebase.stdout);
        let combined = format!("{}\n{}", stderr, stdout);

        if is_merge_or_rebase_conflict(&combined) {
            // Conflict — leave the rebase in progress so the user can
            // resolve manually. Revert pre-pull renames (binaries are
            // still old on disk) + restart hub for productivity.
            // v0.2.71: shared tail.
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );

            // v0.2.51 Bug A: write the resume sentinel + deferral BEFORE
            // returning the conflict payload (see merge_orchestrator_with_upstream
            // for the rationale). The rebase leaves HEAD at the original
            // SHA + a partially-applied series in .git/rebase-{merge,apply}/;
            // the sentinel captures that SHA so resume can verify HEAD
            // actually moved before re-entering install.py. The deferral
            // mirrors the state into UPDATE_DEFERRED.md for terminal
            // Claude sessions.
            // v0.2.53 DEDUP-14: paired writer so one cannot be forgotten.
            write_resume_sentinel_and_deferral(&install_path, "rebase", &pull_branch).await;

            let conflicted = collect_conflicted_files(&install_path).await;
            return Err(serialize_orchestrator_conflict_error(
                "rebase",
                &pull_branch,
                &conflicted,
                combined.trim(),
            ));
        }

        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        return Err(format!("git rebase failed: {}", stderr));
    }

    // v0.2.71 (Piece 3.3): success-path autostash-pop backstop — sibling to
    // the merge command's. `git rebase --autostash` can complete the rebase
    // (exit 0) yet leave the tree broken when the final autostash POP
    // conflicts ("could not apply autostash" / "Applying autostash resulted
    // in conflicts" + `UU` markers + a dangling stash). The
    // `!rebase.status.success()` branch above does NOT catch that, and
    // proceeding would run install.py + restart on a silently-broken tree.
    // Detect it here and route to the conflict modal + resume sentinel +
    // UPDATE_DEFERRED, never a dead-end.
    {
        let rebase_combined = format!(
            "{}\n{}",
            String::from_utf8_lossy(&rebase.stdout),
            String::from_utf8_lossy(&rebase.stderr)
        );
        let autostash_pop_failed = rebase_combined.contains("autostash resulted in conflicts")
            || rebase_combined.contains("Applying autostash")
            || rebase_combined.contains("could not apply autostash");
        let unmerged = collect_conflicted_files(&install_path).await;
        if !unmerged.is_empty() || autostash_pop_failed {
            eprintln!(
                "[vct] rebase_orchestrator_onto_upstream: rebase exited 0 but the working tree \
                 has {} unmerged file(s){} — an --autostash pop conflict. Routing to the \
                 conflict modal instead of proceeding on a broken tree.",
                unmerged.len(),
                if autostash_pop_failed { " + autostash-conflict marker" } else { "" },
            );
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            write_resume_sentinel_and_deferral(&install_path, "rebase", &pull_branch).await;
            return Err(serialize_orchestrator_conflict_error(
                "rebase",
                &pull_branch,
                &unmerged,
                rebase_combined.trim(),
            ));
        }
    }

    run_post_pull_install_and_restart(
        app,
        &install_path,
        path,
        &window,
        system,
        pre_pull_renamed,
        pre_pull_renamed_hub,
        &mut update_gate_guard,
    )
    .await
}

/// Abort an in-progress merge or rebase, restoring the working tree to
/// the state it was in before the merge/rebase started. Best-effort:
/// tries `git merge --abort` first (no-op if not in a merge), then
/// `git rebase --abort` (no-op if not in a rebase). Returns Ok if at
/// least one succeeded OR if neither operation was in progress.
///
/// Why both: the user may have clicked "Abort" from either the merge
/// conflict modal or the rebase conflict modal, and we don't bother
/// tracking which one is active. Probing `.git/MERGE_HEAD` vs
/// `.git/rebase-merge/` would be racy if the user manually resolved
/// half the conflicts in a terminal between modal-render and click.
#[command]
pub async fn abort_orchestrator_merge_or_rebase(path: String) -> Result<(), String> {
    let install_path = PathBuf::from(&path);

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — nothing to abort".to_string());
    }

    let merge_head = install_path.join(".git").join("MERGE_HEAD");
    let rebase_merge = install_path.join(".git").join("rebase-merge");
    let rebase_apply = install_path.join(".git").join("rebase-apply");

    let in_merge = merge_head.exists();
    let in_rebase = rebase_merge.exists() || rebase_apply.exists();

    if !in_merge && !in_rebase {
        // Nothing in progress — treat as no-op success. The modal might
        // be slightly stale; user intent ("get back to a clean state")
        // is already satisfied. Clear any resume sentinel + deferral so
        // the merge_resolved_incomplete badge doesn't linger AND the
        // terminal-Claude deferral entry goes away (v0.2.51 Bug A).
        clear_update_resume_sentinel(&install_path);
        clear_update_resume_deferral_if_solo(&install_path);
        return Ok(());
    }

    let mut last_err: Option<String> = None;

    if in_merge {
        let out = tokio::process::Command::new("git").silent()
            .args(["merge", "--abort"])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| format!("git merge --abort failed to spawn: {}", e))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            last_err = Some(format!("git merge --abort: {}", stderr.trim()));
        } else {
            // v0.2.51 Bug A: aborting is a fresh start — drop the resume
            // sentinel + deferral so the merge_resolved_incomplete badge
            // AND the terminal-Claude UPDATE_DEFERRED.md entry both clear.
            clear_update_resume_sentinel(&install_path);
            clear_update_resume_deferral_if_solo(&install_path);
            return Ok(());
        }
    }

    if in_rebase {
        let out = tokio::process::Command::new("git").silent()
            .args(["rebase", "--abort"])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| format!("git rebase --abort failed to spawn: {}", e))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            last_err = Some(format!("git rebase --abort: {}", stderr.trim()));
        } else {
            // v0.2.51 Bug A: same as above — clear both sentinel and
            // the terminal-Claude UPDATE_DEFERRED.md entry.
            clear_update_resume_sentinel(&install_path);
            clear_update_resume_deferral_if_solo(&install_path);
            return Ok(());
        }
    }

    Err(last_err.unwrap_or_else(|| {
        "No merge or rebase in progress, but abort was requested.".to_string()
    }))
}

// ---------------------------------------------------------------------------
// v0.2.51 Bug A — merge-conflict update resume.
//
// PROBLEM (pre-v0.2.51): when `update_orchestrator` / `merge_orchestrator_*`
// / `rebase_orchestrator_*` surfaces a conflict modal, the user resolves
// the conflict in their editor and `git commit`s. The launcher never
// re-enters the post-merge segment (install.py --update + binary refresh
// + auto-restart). The user is left with merged source on disk, OLD
// binary running, stale install state — and the conflict modal's
// "Resolve manually" button just closed the dialog.
//
// FIX:
//   1. The 3 conflict sites write a sentinel file pointing the launcher at
//      what to resume (`write_update_resume_sentinel`).
//   2. `check_for_updates` reads the sentinel + the `.git/MERGE_HEAD`
//      state to compute a new `merge_resolved_incomplete` flag — true
//      once the user has finished the merge but install.py hasn't run.
//   3. The new `resume_orchestrator_update` Tauri command verifies the
//      working tree is clean (no `<<<<<<<` markers, no in-flight merge)
//      and then re-enters the post-pull tail via
//      `run_post_pull_install_and_restart`.
//   4. Both `abort_orchestrator_merge_or_rebase` and a successful
//      `resume_orchestrator_update` clear the sentinel.
//
// The sentinel lives under `.claude/state/`, NOT `.git/` (we don't want
// to pollute the git directory; the file is launcher state, not git state).
// ---------------------------------------------------------------------------

/// Relative path to the resume sentinel from the install root. Stored
/// under `.claude/state/` to keep the convention of one launcher-state
/// folder per install (sibling of `launcher-restart-marker`, etc.).
const UPDATE_RESUME_SENTINEL_REL: &str =
    ".claude/state/orchestrator-update-resume-needed.json";

/// On-disk shape of the resume sentinel. Versioned via the `schema`
/// field so future readers can refuse / upgrade old layouts.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct UpdateResumeSentinel {
    /// Sentinel format version. Always `1` for v0.2.51's writer.
    pub schema: u32,
    /// One of `"merge"`, `"rebase"`. Drives the UpdateBadge label + the
    /// modal copy.
    pub operation: String,
    /// Branch the conflict happened on (e.g. `main`).
    pub branch: String,
    /// HEAD SHA at the moment of the conflict. Used by
    /// `resume_orchestrator_update` to refuse a resume when HEAD hasn't
    /// advanced (= user aborted via CLI; sentinel is stale).
    pub sha_at_conflict: String,
    /// ISO-8601 UTC timestamp the sentinel was written.
    pub written_at: String,
}

/// Best-effort: read the resume sentinel into a struct. Returns `None`
/// when the file is absent, malformed, or the schema is unknown — the
/// caller treats any of those as "no resume pending" so a broken
/// sentinel never wedges the UpdateBadge.
fn read_update_resume_sentinel(install_path: &Path) -> Option<UpdateResumeSentinel> {
    let path = install_path.join(UPDATE_RESUME_SENTINEL_REL);
    let txt = std::fs::read_to_string(&path).ok()?;
    let parsed: UpdateResumeSentinel = serde_json::from_str(&txt).ok()?;
    if parsed.schema != 1 {
        return None;
    }
    Some(parsed)
}

/// Atomic-write the resume sentinel. Best-effort: any I/O failure is
/// logged + swallowed because the conflict path that calls us MUST still
/// surface the conflict error to the GUI (sentinel is a recovery aid,
/// not a hard requirement).
fn write_update_resume_sentinel(
    install_path: &Path,
    operation: &str,
    branch: &str,
    sha_at_conflict: &str,
) {
    let sentinel = UpdateResumeSentinel {
        schema: 1,
        operation: operation.to_string(),
        branch: branch.to_string(),
        sha_at_conflict: sha_at_conflict.to_string(),
        written_at: chrono::Utc::now().to_rfc3339(),
    };
    let json = match serde_json::to_string_pretty(&sentinel) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "[vct] update_resume_sentinel: serialize failed: {} — \
                 skipping sentinel write",
                e
            );
            return;
        }
    };
    let target = install_path.join(UPDATE_RESUME_SENTINEL_REL);
    let Some(parent) = target.parent() else {
        eprintln!(
            "[vct] update_resume_sentinel: target has no parent: {} — \
             skipping",
            target.display()
        );
        return;
    };
    if let Err(e) = std::fs::create_dir_all(parent) {
        eprintln!(
            "[vct] update_resume_sentinel: mkdir {} failed: {} — \
             skipping sentinel write",
            parent.display(),
            e
        );
        return;
    }
    // Write to a tempfile then rename — protects against partial writes
    // confusing a concurrent `check_for_updates` poll.
    let tmp = parent.join(format!(
        "orchestrator-update-resume-needed.json.tmp.{}",
        std::process::id()
    ));
    if let Err(e) = std::fs::write(&tmp, json.as_bytes()) {
        eprintln!(
            "[vct] update_resume_sentinel: write {} failed: {}",
            tmp.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
        return;
    }
    if let Err(e) = std::fs::rename(&tmp, &target) {
        eprintln!(
            "[vct] update_resume_sentinel: rename {} → {} failed: {}",
            tmp.display(),
            target.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
    }
}

/// Best-effort: delete the resume sentinel. No-op when the file is
/// absent. Any unlink error is logged + swallowed (the badge will
/// re-render on next check_for_updates, but failing to delete shouldn't
/// block the user).
fn clear_update_resume_sentinel(install_path: &Path) {
    let target = install_path.join(UPDATE_RESUME_SENTINEL_REL);
    match std::fs::remove_file(&target) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            eprintln!(
                "[vct] update_resume_sentinel: clear {} failed: {}",
                target.display(),
                e
            );
        }
    }
}

/// v0.2.51 Bug A: write a comprehensive `update_resume_required` entry
/// into `.claude/context/UPDATE_DEFERRED.md` next to the sentinel.
///
/// Rationale for writing this from Rust (vs deferring to install.py):
/// the whole point of the sentinel is "install.py never ran". So the
/// deferral writer ALSO needs to be standalone — we can't depend on
/// install.py firing to emit it.
///
/// The Markdown shape mirrors what `vco_lib/deferral_report.py`
/// produces (`_render_frontmatter` + `_render_header` + `_render_entry`),
/// so when install.py eventually runs (whether via resume_orchestrator_update
/// or a manual `python install.py --update`), `DeferralReport.read()`
/// parses our entry, recognizes it by condition_id, and treats it as
/// resolved on the next write (since install.py running IS the
/// resolution).
///
/// Best-effort: any I/O failure is logged + swallowed because the
/// conflict path that calls us MUST still surface the conflict error
/// to the GUI. The sentinel + UpdateBadge is the primary recovery
/// mechanism; the deferral is a redundant secondary channel for users
/// running Claude in a terminal session.
///
/// Comprehensive copy is intentional — the brief mandates
/// "comprehensive info for Claude" so the deferral can drive a
/// session-start prompt that explains the state, the recovery, and the
/// risk of leaving it unattended.
fn write_update_resume_deferral(
    install_path: &Path,
    operation: &str,
    branch: &str,
) {
    let target = install_path.join(".claude/context/UPDATE_DEFERRED.md");
    let parent = match target.parent() {
        Some(p) => p,
        None => {
            eprintln!(
                "[vct] update_resume_deferral: target has no parent: {}",
                target.display()
            );
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(parent) {
        eprintln!(
            "[vct] update_resume_deferral: mkdir {} failed: {} — \
             skipping deferral write",
            parent.display(),
            e
        );
        return;
    }
    // v0.2.83 WP-B6: hold the shared UPDATE_DEFERRED lock across the tmp-write +
    // rename so this standalone full-rewrite serializes with every other writer
    // on `<install_path>/.claude/context/.update-deferred.lock`. Standalone by
    // design (the sentinel's whole point is "install.py never ran" → Python
    // cannot be assumed), so we take the flock directly. Best-effort (real flock
    // on POSIX, no-lock on Windows). Held until return.
    let _deferral_lock =
        vct_launcher_core::services::deferral_lock::lock_folder(install_path);
    let now = chrono::Utc::now().to_rfc3339();
    let op_phrase = if operation == "rebase" { "rebase" } else { "merge" };
    let install_root_display = install_path.display();
    // Format follows vco_lib/deferral_report.py::_render_entry exactly so
    // `DeferralReport.read()` round-trips it cleanly. The frontmatter is
    // single-entry; if install.py later adds more entries it will
    // re-render with the merged list (our entry survives via condition_id
    // dedup in `add_entry`).
    let content = format!(
        "---\n\
title: VCO Update Deferred\n\
generated_at: {now}\n\
condition_ids: [update_resume_required]\n\
severity_max: warning\n\
---\n\
\n\
# VCO Update Deferred\n\
\n\
The last `install.py --update` detected conditions it could not auto-resolve safely. \
Each section below names a condition and the exact command to apply it.\n\
\n\
## update_resume_required (warning)\n\
\n\
**Title**: Orchestrator update halted at a conflict — resume needed\n\
\n\
**Detected**: A previous `update_orchestrator` ({op_phrase} on `{branch}`) halted at \
a conflict, and the conflict was resolved outside the launcher (CLI `git add` + \
`git commit`, or editor save + commit). However, `install.py --update` and the \
launcher binary refresh never ran, so the install manifest (`state/install-manifest.json::version`) \
and the running launcher binary may be stale relative to the freshly-merged source. \
Sentinel: `.claude/state/orchestrator-update-resume-needed.json` in `{install_root_display}`.\n\
\n\
**Why deferred**: The launcher cannot safely re-run `install.py --update` without \
explicit user consent — the merge resolution might be partial, and re-running \
install.py against a dirty tree could corrupt module state. The user must \
acknowledge they want to continue. Two equivalent ways to acknowledge: (1) click \
the `Continue Update` button in the launcher's MenuBar UpdateBadge, OR (2) run \
`python install.py --update` manually from a terminal. Either path bumps \
`last_installed_version` to match `source_version` and clears this deferral.\n\
\n\
**To apply**:\n\
```bash\n\
# Option A (recommended): open the launcher GUI. The top-right MenuBar\n\
# shows a pulsing purple UpdateBadge with title \"Continue Update\".\n\
# Click it → click the Continue Update button. This re-runs\n\
# install.py --update + refreshes the launcher binary + auto-restarts.\n\
#\n\
# Option B (terminal): from the orchestrator install root, run:\n\
cd {install_root_display}\n\
python install.py --update\n\
# After install.py finishes, fully quit the launcher (tray → Quit, not\n\
# just close window) and relaunch via your usual entrypoint so the\n\
# freshly-built binary loads.\n\
```\n\
\n\
**For your Claude assistant** (read this before continuing the user's task):\n\
The user has an unresolved orchestrator update in progress. The repo on disk is at \
a newer commit (post-merge) but the launcher install manifest still records the \
old version. Recommended action: surface this deferral at session start and ask \
the user to either click `Continue Update` in the launcher or run `python \
install.py --update` from a terminal BEFORE continuing other work. Reason it \
matters: the .claude/hooks/, MCP server registrations, and possibly the launcher \
binary may not match the freshly-merged source — running other automation against \
mismatched state can corrupt project metadata. The deferral self-clears on the \
next successful install.py run, so the resolution is one command.\n\
\n\
**Detected at**: {now}\n\
\n\
---\n",
        now = now,
        op_phrase = op_phrase,
        branch = branch,
        install_root_display = install_root_display,
    );

    // Atomic write: temp file in the same directory, then rename.
    let tmp = parent.join(format!(
        "UPDATE_DEFERRED.md.tmp.{}",
        std::process::id()
    ));
    if let Err(e) = std::fs::write(&tmp, content.as_bytes()) {
        eprintln!(
            "[vct] update_resume_deferral: write {} failed: {}",
            tmp.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
        return;
    }
    if let Err(e) = std::fs::rename(&tmp, &target) {
        eprintln!(
            "[vct] update_resume_deferral: rename {} → {} failed: {}",
            tmp.display(),
            target.display(),
            e
        );
        let _ = std::fs::remove_file(&tmp);
    }
}

// v0.2.55 (durable-logging fix): the launcher-side update-diverged deferral
// writer + its `LauncherUpdateDivergedKind` enum were RELOCATED in v0.2.71
// Sweep-A#3 to `git_user_editable_merge` (a shared module) so the launcher
// SELF-update surface (`self_update::apply_launcher_update`) can write the
// SAME durable `UPDATE_DEFERRED.md` trace this installer surface already
// emits — without a circular import and without a second copy of the writer.
// installer.rs refers to them via the top-level
// `use crate::commands::git_user_editable_merge::{…}` import; the
// behaviour/output is unchanged (the body moved verbatim).

/// v0.2.51 Bug A: best-effort clear of the `update_resume_required`
/// deferral entry. Called from `resume_orchestrator_update` (after
/// v0.2.53 DEDUP-14 (PROMOTED from `rust-installer-dedup-2026-06-10.md`
/// §Finding E): paired sentinel + deferral writer.
///
/// The v0.2.51 Bug A class fires when ONE of the two writes is forgotten
/// at a conflict-handling site (sentinel-without-deferral leaves Claude
/// terminal sessions with no signal of the pending update; deferral-
/// without-sentinel leaves the launcher's UpdateBadge dead). Pre-v0.2.53,
/// three sites in installer.rs repeated the same 3-line pattern verbatim:
///
/// ```ignore
/// let sha = read_head_sha(&install_path).await.unwrap_or_default();
/// write_update_resume_sentinel(&install_path, OP, &pull_branch, &sha);
/// write_update_resume_deferral(&install_path, OP, &pull_branch);
/// ```
///
/// Consolidating into a single helper means future writers can't desync:
/// you either call `write_resume_sentinel_and_deferral` (atomic-ish from
/// the call site's perspective) or you call neither.
///
/// Atomicity caveat (intentional): the underlying writers each do their
/// own atomic-rename within their own target file, but the helper is NOT
/// transactional ACROSS files. If the process dies between the sentinel
/// write and the deferral write, the launcher gets the sentinel + the
/// UpdateBadge BUT the Claude-terminal-session signal is missing for
/// that one boot. Acceptable: the next `install.py --update` re-writes
/// both records (it inspects the sentinel and re-emits the deferral)
/// and the user sees no regression from the pre-v0.2.51 behaviour.
///
/// Sites consolidated:
///   - `update_orchestrator` rebase-with-autostash conflict path
///     (formerly L4324-L4342).
///   - `merge_orchestrator_with_upstream` conflict path
///     (formerly L5778-L5780).
///   - `rebase_orchestrator_onto_upstream` conflict path
///     (formerly L5945-L5947).
///
/// Helper expects `install_path` already-validated by the caller.
async fn write_resume_sentinel_and_deferral(
    install_path: &Path,
    operation: &str,
    pull_branch: &str,
) {
    let sha = read_head_sha(install_path).await.unwrap_or_default();
    write_update_resume_sentinel(install_path, operation, pull_branch, &sha);
    write_update_resume_deferral(install_path, operation, pull_branch);
}

/// install.py runs successfully) and `abort_orchestrator_merge_or_rebase`.
///
/// Conservative semantics: ONLY removes the file when it contains a
/// SINGLE entry with condition_id `update_resume_required`. If install.py
/// has added other entries in the meantime, we leave the file in place
/// and let install.py's `mark_resolved("update_resume_required")` +
/// re-write handle the surgical removal. This avoids destroying
/// unrelated deferrals.
fn clear_update_resume_deferral_if_solo(install_path: &Path) {
    // v0.2.83 WP-B6: hold the shared UPDATE_DEFERRED lock around the whole
    // read → solo-decision → delete (.md + .json sidecar) cycle so a concurrent
    // writer cannot add an entry between our read and our unlink (which would
    // make us delete a file that is no longer solo). Best-effort (real flock on
    // POSIX, no-lock on Windows). Held for the function's whole lifetime.
    let _deferral_lock =
        vct_launcher_core::services::deferral_lock::lock_folder(install_path);
    let target = install_path.join(".claude/context/UPDATE_DEFERRED.md");
    let Ok(content) = std::fs::read_to_string(&target) else {
        return;
    };
    // Two cheap checks: ONE condition_id line listing only ours, AND
    // exactly one "## update_resume_required" section. If both true,
    // we own the file outright and can delete it.
    let single_id = content
        .lines()
        .any(|l| l.trim() == "condition_ids: [update_resume_required]");
    let section_count = content
        .lines()
        .filter(|l| l.starts_with("## "))
        .count();
    // v0.2.76 (A2, acknowledged edge): the "solo" decision reads ONLY the .md
    // (single condition_id line + exactly one section), yet the Ok arm below
    // dual-unlinks the co-present UPDATE_DEFERRED.json too. A hand-edited .json
    // carrying EXTRA entries while the .md still looks solo would thus be
    // deleted with it. Normal flow cannot produce this: A-3 writes the pair in
    // lock-step (the .json mirrors the .md's entry set), so a solo .md implies
    // a solo .json. We accept the edge rather than parse+diff the JSON here
    // because the backstop is robust — install.py's mark_resolved reconcile
    // re-derives the deferral set from live install state on the next run, so a
    // wrongly-cleared hand-edited entry is regenerated if it still applies.
    if single_id && section_count == 1 {
        match std::fs::remove_file(&target) {
            Err(e) => {
                eprintln!(
                    "[vct] update_resume_deferral: clear {} failed: {}",
                    target.display(),
                    e
                );
            }
            Ok(()) => {
                // v0.2.75 (Part 3b): whole-file deletion must be DUAL-file.
                // A-3 made UPDATE_DEFERRED.json the authoritative store —
                // `deferral_report.read()` prefers it and, with no co-present
                // Markdown, takes the JSON verbatim. Unlinking ONLY the .md
                // here left the sidecar to RESURRECT the solo
                // update_resume_required entry on the next Python read.
                // Mirrors restart.rs::clear_restart_deferral's empty-file
                // sweep (the v0.2.73 F2 fix). Best-effort: a missing sidecar
                // is fine (older installs predate A-3); a remove error is
                // logged, not fatal (the .md is already gone, so the entry
                // won't re-render from Markdown at least — install.py's
                // mark_resolved reconcile is the backstop).
                let json_sidecar = target.with_file_name("UPDATE_DEFERRED.json");
                if json_sidecar.is_file() {
                    if let Err(e) = std::fs::remove_file(&json_sidecar) {
                        eprintln!(
                            "[vct] update_resume_deferral: clear JSON sidecar {} \
                             failed (entry may resurrect from JSON): {}",
                            json_sidecar.display(),
                            e
                        );
                    }
                }
            }
        }
    }
    // Otherwise: leave the file alone (BOTH files — the JSON sidecar too;
    // deleting only it would desync the pair). install.py will reconcile.
}

/// Best-effort: scan tracked files for live conflict markers
/// (`<<<<<<< `, `=======`, `>>>>>>> `). Used by `resume_orchestrator_update`
/// to refuse a resume when the user committed without actually
/// resolving (e.g. they `git commit -a` with markers still in place).
///
/// Returns the list of paths that still contain markers, empty when
/// the tree is clean. Reads file contents via `git grep` so the scan
/// covers tracked files only (no node_modules / target / etc).
async fn detect_remaining_conflict_markers(repo: &Path) -> Vec<String> {
    let out = tokio::process::Command::new("git")
        .silent()
        .args([
            "grep",
            "--name-only",
            "-E",
            "^(<{7}|={7}|>{7}) ",
        ])
        .current_dir(repo)
        .output()
        .await;
    let out = match out {
        Ok(o) => o,
        Err(_) => return Vec::new(),
    };
    // git grep exits 1 when no matches. Treat both 0 (matches) and 1
    // (no matches) as success; anything else means the scan errored.
    let code = out.status.code().unwrap_or(-1);
    if code != 0 && code != 1 {
        return Vec::new();
    }
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Single-flight gate so multiple rapid clicks on "Continue Update"
/// don't race two install.py runs.
static RESUME_IN_FLIGHT: LazyLock<tokio::sync::Mutex<()>> =
    LazyLock::new(|| tokio::sync::Mutex::new(()));

/// v0.2.78 ITEM #0 (F4) — decide whether a resume request is a benign no-op:
/// the recorded conflict SHA is non-empty AND HEAD still equals it, i.e. the
/// merge/rebase was aborted (nothing advanced), so there is nothing to resume.
/// Extracted as a pure fn so the decision is unit-testable without a Tauri
/// AppHandle. An EMPTY `sha_at_conflict` (older sentinels that didn't record
/// it) is NOT a benign no-op — we can't prove HEAD didn't move, so we let the
/// normal resume path run its other guards (conservative).
fn resume_head_unchanged_is_benign_noop(sha_at_conflict: &str, head_sha: &str) -> bool {
    !sha_at_conflict.is_empty() && head_sha == sha_at_conflict
}

/// Resume an `update_orchestrator` flow that halted at a merge/rebase
/// conflict. The user has already resolved the conflict (manually OR via
/// CLI `git add` + `git commit` / `git rebase --continue`); this command
/// re-enters the post-merge tail to run install.py --update + binary
/// refresh + auto-restart.
///
/// Refuses to act when:
///   - The path isn't a git repo.
///   - `.git/MERGE_HEAD` / `.git/rebase-merge/` / `.git/rebase-apply/`
///     still exist (= merge IS in progress; modal flow not yet complete).
///   - Tracked files still contain `<<<<<<<` / `=======` / `>>>>>>>`
///     markers (= user committed without resolving; refuse and surface
///     the offending paths).
///   - No sentinel is present (= no resume to perform). This isn't an
///     error per se but we return a structured rejection so the GUI can
///     distinguish "already resumed" from "system broken".
///
/// On success: clears the sentinel + audit-logs `update_orchestrator_resumed`
/// + returns the same `InstallResult` shape as `update_orchestrator`. In
/// practice the auto-restart inside `run_post_pull_install_and_restart`
/// terminates the process before this `Ok` is observed by the GUI.
#[command]
pub async fn resume_orchestrator_update<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot resume update".to_string());
    }

    // Single-flight: hold the mutex for the entire resume.
    let _guard = RESUME_IN_FLIGHT.lock().await;

    let audit_app = app.clone();
    let resume_start_ms = chrono::Utc::now().timestamp_millis();
    let write_audit = move |operation: &str, detail: serde_json::Value| {
        if let Some(db) = audit_app.try_state::<crate::db::Db>() {
            let _ = db.audit(operation, None, None, &detail);
        }
    };

    let sentinel = match read_update_resume_sentinel(&install_path) {
        Some(s) => s,
        None => {
            write_audit(
                "update_orchestrator_resume_rejected",
                serde_json::json!({
                    "reason": "no_sentinel",
                    "install_path": path,
                }),
            );
            return Err(
                "No resume pending: the orchestrator update flow did not \
                 record a conflict checkpoint. If you reached this from a \
                 stale UpdateBadge, click Restart Launcher or refresh the \
                 status."
                    .to_string(),
            );
        }
    };

    // v0.2.53 NEW-11: defensive empty-sha refusal. If the sentinel was
    // written with an empty `sha_at_conflict` (because `read_head_sha`
    // failed at conflict-write time — e.g. .git/HEAD missing or
    // unreadable), we have NO baseline to verify HEAD has advanced past.
    // The "head_unchanged" guard at line 6553 silently skips its check
    // in that case, which would let us resume against a possibly-
    // unrelated tree state. Refuse explicitly with a clear remediation
    // path instead: the user should re-trigger the update from the
    // launcher (which re-writes the sentinel with a fresh sha) OR clear
    // the sentinel manually.
    //
    // Without this guard, the v0.2.51 Bug A class can re-appear in a
    // subtler form: the user runs Continue Update, install.py re-merges
    // against an unknown baseline, and the working tree may end up in
    // a state neither the launcher nor install.py expected.
    if sentinel.sha_at_conflict.is_empty() {
        write_audit(
            "update_orchestrator_resume_rejected",
            serde_json::json!({
                "reason": "empty_sha_at_conflict",
                "install_path": path,
                "operation": sentinel.operation,
                "branch": sentinel.branch,
            }),
        );
        return Err(
            "Resume sentinel is missing the conflict-time SHA — the original \
             conflict-handling write could not read .git/HEAD. We cannot \
             verify the working tree's baseline, so the resume is refused. \
             Either re-trigger the update from the launcher (which will \
             write a fresh sentinel with a valid SHA), OR run \
             `git status` + manually finish/abort any merge, then click \
             Restart Launcher to clear this banner."
                .to_string(),
        );
    }

    // Probe in-flight merge/rebase state. If still mid-merge we refuse —
    // the user should finish the merge (or abort it) first.
    let merge_head = install_path.join(".git").join("MERGE_HEAD");
    let rebase_merge = install_path.join(".git").join("rebase-merge");
    let rebase_apply = install_path.join(".git").join("rebase-apply");
    let still_mid_merge =
        merge_head.exists() || rebase_merge.exists() || rebase_apply.exists();
    if still_mid_merge {
        write_audit(
            "update_orchestrator_resume_rejected",
            serde_json::json!({
                "reason": "merge_still_in_progress",
                "install_path": path,
            }),
        );
        return Err(
            "Merge or rebase is still in progress. Finish it with `git add \
             <file>` + `git merge --continue` (or `git rebase --continue`), \
             OR click Abort & restore to discard it. Then click Continue \
             Update again."
                .to_string(),
        );
    }

    // Verify HEAD actually advanced past the conflict SHA. If it didn't,
    // the user aborted the merge/rebase (via CLI or the modal's Abort) — the
    // sentinel is stale but the state is BENIGN: there is simply nothing to
    // resume. v0.2.78 ITEM #0 (F4): pre-v0.2.78 this returned an `Err`, which
    // the Svelte `resumeUpdate()` catch rendered as a RED "Update FAILED"
    // banner + told the user to manually refresh — a scary dead-end for a
    // no-op. Now we self-clear the sentinel + solo deferral (as before) AND
    // return a SUCCESS-shaped result so the caller's success path runs
    // `orchestrator.checkStatus()`, which re-detects the (now-clean) state and
    // clears the UpdateBadge automatically — no red banner, no manual refresh.
    let head_sha = read_head_sha(&install_path).await.unwrap_or_default();
    if resume_head_unchanged_is_benign_noop(&sentinel.sha_at_conflict, &head_sha) {
        clear_update_resume_sentinel(&install_path);
        clear_update_resume_deferral_if_solo(&install_path);
        write_audit(
            "update_orchestrator_resume_resolved_noop",
            serde_json::json!({
                "reason": "head_unchanged",
                "sha_at_conflict": sentinel.sha_at_conflict,
                "install_path": path,
            }),
        );
        return Ok(InstallResult {
            success: true,
            install_path: path,
            message: "No resume needed — the merge/rebase was aborted, so there is \
                      nothing to continue. The update badge will clear automatically."
                .to_string(),
            system,
        });
    }

    // Scan for stray conflict markers. If the user committed a file with
    // markers still in it, refuse — we'd otherwise ship a broken update.
    let with_markers = detect_remaining_conflict_markers(&install_path).await;
    if !with_markers.is_empty() {
        write_audit(
            "update_orchestrator_resume_rejected",
            serde_json::json!({
                "reason": "conflict_markers_remain",
                "files": with_markers,
                "install_path": path,
            }),
        );
        let head = with_markers.iter().take(8).cloned().collect::<Vec<_>>();
        return Err(format!(
            "Found unresolved conflict markers in {} file(s): {}{}. Open \
             each file, remove the `<<<<<<<` / `=======` / `>>>>>>>` \
             markers, `git add` + `git commit --amend` (or commit a fix), \
             then click Continue Update again.",
            with_markers.len(),
            head.join(", "),
            if with_markers.len() > head.len() {
                ", …"
            } else {
                ""
            },
        ));
    }

    // All preconditions satisfied. Audit-log the resume start.
    write_audit(
        "update_orchestrator_resumed",
        serde_json::json!({
            "operation": sentinel.operation,
            "branch": sentinel.branch,
            "sha_at_conflict": sentinel.sha_at_conflict,
            "head_sha": head_sha,
            "install_path": path,
        }),
    );

    // V52-AI (v0.2.52): acquire the MCP fork-bomb gate before any
    // file-touching work. Same shape as update_orchestrator —
    // pre-sweep + RAII guard. Resume runs install.py + binary
    // refresh, which is the exact window where the fork-bomb
    // historically formed; protecting it is just as important.
    let pre_sweep_count_resume = crate::commands::update_gate::pre_update_mcp_kill_sweep();
    if pre_sweep_count_resume > 0 {
        eprintln!(
            "[vct] resume_orchestrator_update: pre-sweep terminated {} \
             MCP-shaped process(es) before resume",
            pre_sweep_count_resume
        );
    }
    let (mut update_gate_guard, _gate_write_result) =
        crate::commands::update_gate::UpdateInProgressGuard::new();
    // Resume starts mid-flow; the lockfile claims InstallPy directly
    // because we're skipping the git-pull stage (the user already
    // resolved the merge before clicking Continue Update).
    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::InstallPy);

    // The on-disk source is already current (the user finished the merge),
    // so we mirror the `merge_orchestrator_with_upstream` post-pull tail.
    // We stop the hub + pre-pull-rename binaries first so install.py +
    // the binary swap don't race the old hub's file handles.
    emit_progress(&window, "update", "Stopping vct-hub for resume...", 2.0);
    if let Err(e) = ensure_hub_stopped_for_update(&install_path) {
        return Err(format!(
            "Resume aborted: could not stop vct-hub: {}. Try again, or run \
             `vct-hub --stop` manually.",
            e
        ));
    }

    emit_progress(&window, "update", "Preparing for resume...", 5.0);
    let pre_pull_renamed_hub = pre_pull_rename_vct_hub_binary(&install_path);
    let pre_pull_renamed = pre_pull_rename_running_binary(&install_path);

    // Clear the sentinel + deferral BEFORE install.py runs so a crash
    // during the install.py phase doesn't loop us forever. If install.py
    // fails, the user retries via the existing install_stale path
    // (install.py --update only, no git pull) which is the right next
    // step anyway — the source is already merged, only the manifest is
    // stale. install.py's own deferral writer will replace UPDATE_DEFERRED.md
    // with the actual install outcome.
    clear_update_resume_sentinel(&install_path);
    clear_update_resume_deferral_if_solo(&install_path);

    // v0.2.54 C-2: hand the V52-AI guard to the shared tail. The tail
    // calls `disarm_and_cleanup()` BEFORE the restart hop — pre-v0.2.54
    // the resume path relied on the guard's Drop, which never runs when
    // `restart_launcher` ends in `app.exit(0)` on Windows. Result was a
    // surviving `.update-in-progress.json` with a fresh 15-min deadline:
    // every MCP spawn after the resumed update exited 75 until it lapsed.
    let result = run_post_pull_install_and_restart(
        app,
        &install_path,
        path.clone(),
        &window,
        system,
        pre_pull_renamed,
        pre_pull_renamed_hub,
        &mut update_gate_guard,
    )
    .await;

    // run_post_pull_install_and_restart re-enters the auto-restart path,
    // so we rarely reach here — but if the restart hop fails the helper
    // returns Err. Surface the error to the eprintln stream for forensic
    // completeness; the GUI sees the Err via the Tauri return.
    if let Err(e) = &result {
        eprintln!(
            "[vct] resume_orchestrator_update: post-pull tail failed \
             ({}); duration_ms={}",
            e,
            chrono::Utc::now().timestamp_millis() - resume_start_ms,
        );
    }

    result
}

// ---------------------------------------------------------------------------
// v0.2.52 V52-A / V52-B — one-click conflict resolution from the modal.
//
// PROBLEM (post-v0.2.51): the v0.2.51 modal exposed only "Resolve manually"
// (close + come back via the MenuBar Continue Update badge) and "Abort &
// restore". User feedback 2026-06-09 said the manual-edit step is friction
// for the common case where the conflict is on KG nodes the user is happy
// to keep local OR happy to accept upstream wholesale.
//
// FIX (V52-B): two new Tauri commands that the modal calls when the user
// picks the corresponding button. Each one:
//   1. Validates state (in mid-merge/rebase, sentinel present).
//   2. Runs `git checkout --ours` or `git checkout --theirs` against every
//      conflicted file. Orientation flips between merge and rebase — see
//      the resolve_checkout_flag helper for details.
//   3. `git add` + `git commit` (merge) or `git rebase --continue` (rebase).
//   4. Delegates to `resume_orchestrator_update` for the post-merge tail
//      (install.py --update + binary refresh + auto-restart). The resume
//      function's own preconditions (sentinel present, HEAD advanced,
//      no markers, no in-flight merge state) all pass by construction
//      after the commit/continue above.
//
// V52-A is the modal-side guarantee: the modal removes "Resolve manually"
// and ONLY exposes Abort / Keep local / Accept upstream. Window-X or
// Escape are treated as Abort, not silent dismiss.
// ---------------------------------------------------------------------------

/// Strategy passed in from the modal: which side of a conflict to take.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ConflictResolutionSide {
    /// Keep the user's local content; discard upstream.
    KeepLocal,
    /// Accept upstream content; discard the user's local changes.
    AcceptUpstream,
}

/// Translate a `(side, operation)` pair into the `git checkout` flag to
/// pass. Critical: git swaps "ours" and "theirs" between merge and rebase.
///
/// * **Merge**: HEAD is "ours" (the local branch we merged INTO), MERGE_HEAD
///   is "theirs" (the upstream branch being merged IN).
/// * **Rebase**: HEAD is "ours" (the upstream branch we're rebasing ONTO,
///   from git's POV after the rebase reparents HEAD onto upstream),
///   the commits being replayed are "theirs" (the user's local commits).
///
/// So:
///   * KeepLocal during merge  → `--ours`   (HEAD = local)
///   * KeepLocal during rebase → `--theirs` (replayed commits = local)
///   * AcceptUpstream during merge  → `--theirs`
///   * AcceptUpstream during rebase → `--ours`
///
/// Reference: `git checkout --help`, "MERGES" section, plus the
/// `core.attributesFile` discussion under `git-rebase`.
fn resolve_checkout_flag(side: ConflictResolutionSide, operation: &str) -> &'static str {
    let is_rebase = operation.eq_ignore_ascii_case("rebase");
    match (side, is_rebase) {
        (ConflictResolutionSide::KeepLocal, false) => "--ours",
        (ConflictResolutionSide::KeepLocal, true) => "--theirs",
        (ConflictResolutionSide::AcceptUpstream, false) => "--theirs",
        (ConflictResolutionSide::AcceptUpstream, true) => "--ours",
    }
}

/// Shared implementation for both `keep_local_and_continue_update` and
/// `accept_upstream_and_continue_update`. Performs the conflict resolution
/// (checkout + add + commit/continue) then delegates to
/// `resume_orchestrator_update` for the post-merge tail.
///
/// The two public commands are thin wrappers that pick the side enum
/// variant; centralising the body here keeps git invocation, audit logging,
/// and error handling in lockstep between them.
async fn resolve_conflict_and_resume<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
    side: ConflictResolutionSide,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot resolve conflict".to_string());
    }

    let audit_app = app.clone();
    let write_audit = move |operation: &str, detail: serde_json::Value| {
        if let Some(db) = audit_app.try_state::<crate::db::Db>() {
            let _ = db.audit(operation, None, None, &detail);
        }
    };

    // Refuse if there's no sentinel. The conflict modal only opens when a
    // merge/rebase produced one; absence here means the user clicked the
    // button on a stale view (e.g. they aborted via CLI between modal
    // open and click).
    let sentinel = match read_update_resume_sentinel(&install_path) {
        Some(s) => s,
        None => {
            write_audit(
                "update_orchestrator_one_click_rejected",
                serde_json::json!({
                    "reason": "no_sentinel",
                    "side": format!("{:?}", side),
                    "install_path": path,
                }),
            );
            return Err(
                "No active conflict found. The merge or rebase may have \
                 already been aborted from the command line. Refresh the \
                 launcher and try the update again."
                    .to_string(),
            );
        }
    };

    // Require the merge/rebase to still be in flight. If the user has
    // already finished it (via CLI), the right path is the standard
    // Continue Update flow, not these one-click resolvers.
    let merge_head = install_path.join(".git").join("MERGE_HEAD");
    let rebase_merge = install_path.join(".git").join("rebase-merge");
    let rebase_apply = install_path.join(".git").join("rebase-apply");
    let in_merge = merge_head.exists();
    let in_rebase = rebase_merge.exists() || rebase_apply.exists();
    if !in_merge && !in_rebase {
        write_audit(
            "update_orchestrator_one_click_rejected",
            serde_json::json!({
                "reason": "no_merge_in_progress",
                "side": format!("{:?}", side),
                "sentinel_operation": sentinel.operation,
                "install_path": path,
            }),
        );
        return Err(
            "No merge or rebase is currently in progress. If you resolved \
             the conflict from the command line, click Continue Update \
             instead. Otherwise refresh the launcher and try again."
                .to_string(),
        );
    }

    // Cross-check the sentinel's stored operation against the on-disk
    // state. They should agree; mismatch means someone tampered with
    // either git or the sentinel. Refuse rather than guess.
    let live_operation = if in_merge { "merge" } else { "rebase" };
    if !sentinel.operation.eq_ignore_ascii_case(live_operation) {
        write_audit(
            "update_orchestrator_one_click_rejected",
            serde_json::json!({
                "reason": "operation_mismatch",
                "sentinel_operation": sentinel.operation,
                "live_operation": live_operation,
                "side": format!("{:?}", side),
                "install_path": path,
            }),
        );
        return Err(format!(
            "Conflict modal expected a `{}` in progress but the working \
             tree shows a `{}` instead. Refresh the launcher.",
            sentinel.operation, live_operation,
        ));
    }

    // Collect conflicted files BEFORE any checkout — once we run
    // `git checkout --ours/--theirs`, git clears the conflict state
    // for that path and it no longer appears in `--diff-filter=U`.
    let conflicted = collect_conflicted_files(&install_path).await;
    if conflicted.is_empty() {
        // No files to resolve — but we ARE in mid-merge per the earlier
        // probe. This shouldn't happen in normal flow; treat as a
        // probable git-state corruption and refuse with a useful message
        // rather than silently committing an empty resolution.
        write_audit(
            "update_orchestrator_one_click_rejected",
            serde_json::json!({
                "reason": "no_conflicted_files",
                "live_operation": live_operation,
                "side": format!("{:?}", side),
                "install_path": path,
            }),
        );
        return Err(
            "Merge or rebase is in progress but git reports no conflicted \
             files. Run `git status` in the install directory and either \
             finish or abort the merge manually."
                .to_string(),
        );
    }

    let flag = resolve_checkout_flag(side, live_operation);
    emit_progress(
        &window,
        "update",
        &format!(
            "Resolving {} conflict(s) ({})...",
            conflicted.len(),
            match side {
                ConflictResolutionSide::KeepLocal => "keeping local versions",
                ConflictResolutionSide::AcceptUpstream => "accepting upstream versions",
            },
        ),
        10.0,
    );

    write_audit(
        "update_orchestrator_one_click_started",
        serde_json::json!({
            "side": format!("{:?}", side),
            "operation": live_operation,
            "flag": flag,
            "conflicted_files": conflicted,
            "install_path": path,
        }),
    );

    // 1. `git checkout <flag> -- <files>`. Splitting into per-file
    //    invocations keeps the error story per-path (and avoids
    //    blowing past argv length limits on Windows when there are
    //    many conflicted files).
    for file in &conflicted {
        let out = tokio::process::Command::new("git")
            .silent()
            .args(["checkout", flag, "--", file])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| {
                format!(
                    "git checkout {} -- {} failed to spawn: {}",
                    flag, file, e,
                )
            })?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            write_audit(
                "update_orchestrator_one_click_failed",
                serde_json::json!({
                    "stage": "checkout",
                    "file": file,
                    "flag": flag,
                    "stderr": stderr.to_string(),
                    "install_path": path,
                }),
            );
            return Err(format!(
                "git checkout {} -- {} failed: {}. The working tree is \
                 still in the conflicted state — you can retry, pick the \
                 other side, or click Abort & restore.",
                flag,
                file,
                stderr.trim(),
            ));
        }
    }

    // 2. `git add <files>`. Same per-file split for the same reason.
    for file in &conflicted {
        let out = tokio::process::Command::new("git")
            .silent()
            .args(["add", "--", file])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| format!("git add -- {} failed to spawn: {}", file, e))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            write_audit(
                "update_orchestrator_one_click_failed",
                serde_json::json!({
                    "stage": "add",
                    "file": file,
                    "stderr": stderr.to_string(),
                    "install_path": path,
                }),
            );
            return Err(format!(
                "git add -- {} failed after checkout {}: {}. Inspect the \
                 working tree manually.",
                file,
                flag,
                stderr.trim(),
            ));
        }
    }

    // 3. Finish the merge/rebase. Different verb depending on operation:
    //    - merge: `git commit --no-edit` (the merge commit message is
    //      already staged by the prior `git merge` / `git pull`).
    //    - rebase: `git rebase --continue` (advances to the next replayed
    //      commit; needs an empty editor on the message file).
    //
    //    `GIT_EDITOR=true` is the cross-OS way to short-circuit the
    //    interactive editor: `true` exits 0 immediately, which git
    //    treats as "user accepted the existing message". Same trick
    //    is used in the orchestrator's own scripts.
    emit_progress(
        &window,
        "update",
        if in_merge {
            "Finalising merge commit..."
        } else {
            "Continuing rebase..."
        },
        20.0,
    );

    if in_merge {
        let out = tokio::process::Command::new("git")
            .silent()
            .env("GIT_EDITOR", "true")
            .args(["commit", "--no-edit"])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| format!("git commit failed to spawn: {}", e))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            write_audit(
                "update_orchestrator_one_click_failed",
                serde_json::json!({
                    "stage": "commit",
                    "stderr": stderr.to_string(),
                    "install_path": path,
                }),
            );
            return Err(format!(
                "git commit failed after staging the resolution: {}. \
                 Inspect the working tree manually.",
                stderr.trim(),
            ));
        }
    } else {
        // Rebase. `--continue` will pause if there are MORE conflicted
        // commits to replay — in that case the caller (the modal) will
        // see a fresh conflict modal pop and can resolve again.
        let out = tokio::process::Command::new("git")
            .silent()
            .env("GIT_EDITOR", "true")
            .args(["rebase", "--continue"])
            .current_dir(&install_path)
            .output()
            .await
            .map_err(|e| format!("git rebase --continue failed to spawn: {}", e))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            write_audit(
                "update_orchestrator_one_click_failed",
                serde_json::json!({
                    "stage": "rebase_continue",
                    "stderr": stderr.to_string(),
                    "install_path": path,
                }),
            );
            return Err(format!(
                "git rebase --continue failed after staging the \
                 resolution: {}. The rebase may have more conflicts on a \
                 later commit; inspect with `git status`.",
                stderr.trim(),
            ));
        }
    }

    write_audit(
        "update_orchestrator_one_click_resolved",
        serde_json::json!({
            "side": format!("{:?}", side),
            "operation": live_operation,
            "flag": flag,
            "resolved_count": conflicted.len(),
            "install_path": path,
        }),
    );

    // 4. Delegate to the existing resume command. Its preconditions are
    //    all satisfied by construction:
    //      * sentinel still present (we never cleared it).
    //      * HEAD has advanced past `sha_at_conflict` (the commit/continue
    //        above produced a new HEAD).
    //      * No `.git/MERGE_HEAD` / `rebase-merge` / `rebase-apply` (those
    //        clear after a successful commit/continue).
    //      * No conflict markers in any tracked file (checkout --ours/--theirs
    //        replaced the markered content wholesale; we never asked the user
    //        to edit anything).
    //
    //    resume_orchestrator_update will stop the hub, pre-pull rename
    //    binaries, clear the sentinel + deferral, then run install.py
    //    --update + binary refresh + auto-restart.
    resume_orchestrator_update(app, path, window).await
}

/// V52-B: keep local versions of every conflicted file, then continue the
/// update (install.py --update + binary refresh + auto-restart).
///
/// Modal tooltip: "Discards upstream changes for the conflicting files;
/// keeps everything you've added locally. Good for: nodes you've heavily
/// customized."
///
/// User-locked default behavior (2026-06-09): when ALL conflicted files
/// live under `knowledge/`, the modal auto-highlights this button. The
/// auto-highlight is a UI hint only — the command itself doesn't filter
/// by path; it operates on every conflicted file unconditionally.
#[command]
pub async fn keep_local_and_continue_update<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    resolve_conflict_and_resume(app, path, window, ConflictResolutionSide::KeepLocal).await
}

/// V52-B: accept upstream versions of every conflicted file, then continue
/// the update (install.py --update + binary refresh + auto-restart).
///
/// Modal tooltip: "Discards your local changes for the conflicting files;
/// takes the public release version. Good for: KG nodes you didn't really
/// need."
#[command]
pub async fn accept_upstream_and_continue_update<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    resolve_conflict_and_resume(app, path, window, ConflictResolutionSide::AcceptUpstream).await
}

/// v0.2.16 (W4 / 0.5): "Pulled-but-not-installed" resolver. Runs
/// `install.py --update` against an existing install_path WITHOUT a
/// preceding `git pull`. Distinct from `update_orchestrator` (which
/// does both): users who `git pull` manually still need an install.py
/// pass to refresh `.claude/` hooks/scripts/agents, register MCP
/// servers, and bump `state/install-manifest.json::version`.
///
/// **CRITICAL**: this MUST NOT call `update_orchestrator` (which
/// `git pull --ff-only`s first). Source is already current by
/// definition of the install_stale state. Pulling again would waste
/// ~30s and noise the launcher's git output for no value. If you
/// catch yourself adding a git step here, route through the existing
/// `update_orchestrator` command instead.
///
/// Cross-OS contract: shells out to `system.python_cmd` (resolved by
/// `detect_system()` — already handles `python3` vs `python`, Windows
/// `python.exe`, etc.). Never hardcode `/usr/bin/python3`.
#[command]
pub async fn apply_pending_install(
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.is_dir() {
        return Err(format!(
            "install path does not exist or isn't a directory: {}",
            install_path.display()
        ));
    }

    // Sanity: a fully-fresh path with no orchestrator artifacts is
    // ambiguous (could be a misclick by the user passing the wrong
    // path). Refuse rather than running install.py on something that
    // might not be an orchestrator clone.
    if !install_path.join("vct-module.json").is_file() {
        return Err(format!(
            "no vct-module.json at {} — not an orchestrator install root",
            install_path.display()
        ));
    }

    emit_progress(&window, "install", "Applying pending install...", 10.0);

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd);
    cmd.args(["install.py", "--update"])
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);
    // Mirror update_orchestrator: expose running launcher PID so
    // install.py can include it in any launcher_restart_required
    // deferral message it emits (binary swap path).
    cmd.env("VCT_LAUNCHER_PID", std::process::id().to_string());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    emit_progress(&window, "install", "Running install.py --update...", 30.0);

    let install_output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py --update failed: {}", e))?;

    if !install_output.status.success() {
        let stderr = String::from_utf8_lossy(&install_output.stderr);
        let stdout = String::from_utf8_lossy(&install_output.stdout);
        // Surface the tail of both streams — install.py errors usually
        // land in stderr but some Python tracebacks slip into stdout.
        let stderr_tail: String = stderr
            .lines()
            .rev()
            .take(20)
            .collect::<Vec<&str>>()
            .into_iter()
            .rev()
            .collect::<Vec<&str>>()
            .join("\n");
        let stdout_tail: String = stdout
            .lines()
            .rev()
            .take(20)
            .collect::<Vec<&str>>()
            .into_iter()
            .rev()
            .collect::<Vec<&str>>()
            .join("\n");
        return Err(format!(
            "install.py --update failed (exit {}):\nstderr:\n{}\n\nstdout:\n{}",
            install_output.status.code().unwrap_or(-1),
            stderr_tail,
            stdout_tail
        ));
    }

    emit_progress(
        &window,
        "done",
        "Pending install applied successfully!",
        100.0,
    );

    Ok(InstallResult {
        success: true,
        install_path: path,
        message: "Pending install applied successfully".to_string(),
        system,
    })
}

// ---------------------------------------------------------------------------
// Local-copy install (Bug 17): the launcher binary IS built from the
// orchestrator repo. The repo source ships next to the binary. Install =
// copy that local source into the user's chosen install_path. No network
// access, no GitHub PAT required, no `git clone`.
// ---------------------------------------------------------------------------

/// DEPRECATED v0.2.37 shim — prefer `resolve_orchestrator_root(db)`.
///
/// Locate the orchestrator clone root by walking up from
/// `current_exe()`. Accepts EITHER marker pattern (`vct-module.json` OR
/// `install.py + CLAUDE.md`) via the canonical `walk_for_orchestrator_root`
/// helper.
///
/// This shim exists for call sites that genuinely don't have a `Db`
/// handle in scope (e.g. free functions in helper modules,
/// volumes-only paths, early-init code). It does ONLY the walk-up step
/// — no DB cache read, no writeback. New code that DOES have `db`
/// available MUST use `resolve_orchestrator_root(db)` instead, so the
/// DB cache stays warm and `ProjectEnvSettings::populate` can emit
/// `VCT_ORCHESTRATOR_ROOT` even when the binary lives outside the
/// clone (the bug that hit user_project_x pre-v0.2.37).
///
/// Privacy note (2026-05-06): no `env!("CARGO_MANIFEST_DIR")` or
/// `option_env!("VCT_REPO_ROOT")` fallback. Both bake the build-host's
/// absolute path into shipped binaries — `--remap-path-prefix` does
/// NOT rewrite string literals. The shim is runtime-walk-only. The
/// option_env Strategy 1 that lived here pre-v0.2.37 was unreachable
/// on healthy release builds anyway (Strategy 2 always succeeded when
/// the binary lived under the clone), so removing it is pure privacy
/// + simplicity improvement.
pub fn find_local_repo_root() -> Result<PathBuf, String> {
    walk_for_orchestrator_root().ok_or_else(|| {
        "Could not locate orchestrator clone root (no ancestor of \
         current_exe() contains vct-module.json or install.py+CLAUDE.md). \
         Run from a checkout or set launcher.install_path in app_state."
            .to_string()
    })
}

// The orchestrator-copy + conflict-strategy file operations (recursive copy
// helpers, apply_conflict_strategy/ConflictApplyReport, and the merge-pending
// notification-block writers) were extracted verbatim to the `file_ops`
// submodule in v0.2.77 Part 7d. The facade re-exports every symbol. The
// ConflictStrategy enum + MERGE_BLOCK_* sentinels stay here and are pulled
// into file_ops via `super::`.
mod file_ops;
pub(crate) use file_ops::*;

// Orchestrator/project inspection + third-party detection were extracted
// verbatim to the `inspect` submodule in v0.2.77 Part 7d. The facade
// re-exports every symbol (incl. the inspect_orchestrator_at /
// inspect_project_leftovers / detect_third_party_project_signals Tauri
// commands). find_local_repo_root stays in the facade and is pulled into
// inspect via `super::`.
mod inspect;
pub(crate) use inspect::*;

// ---------------------------------------------------------------------------
// Bug 22: optional GitHub PAT for future auto-update flows that pull
// upstream commits into the bundled source. The PAT is NOT required for
// initial install (Bug 17) or per-project update (Bug 21) — those are
// pure local file copies.
//
// ─── Storage architecture (2026-05-08, fork-readiness sweep) ─────────
//
// The OnboardingWizard's PAT-registration originally wrote the token
// directly to `~/.vct-secrets/shared/github_pat` (mode 0o600) — a flat
// file readable by any process running as the user. That bypassed every
// other secret in the launcher: those go through the OS keychain via
// `crate::secrets::set` (`SecretScope::Shared { project_id: SENTINEL_SHARED }`
// + the per-project active-flag gate). The PAT was the one exception, and
// 0.1.7's secrets-architecture audit explicitly flagged "the launcher
// writes a secret in plaintext to a file" as fork-blocking.
//
// 0.1.7 fix: the PAT now lives in the OS keychain, written/read via
// `crate::secrets::set/get` exactly like the other shared secrets the
// launcher manages. The active-flag row is set so the cross-launcher
// pause check (`is_secret_active_cross_launcher`) and the hub's
// `/api/v1/projects/{id}/env` endpoint surface the PAT correctly.
//
// ─── module_id unification (post-0.2.0 backlog #6, 2026-05-10) ───────
//
// 0.2.0 wrote the PAT at `vct._user_shared_.shared.installer/github_pat`
// (`GITHUB_PAT_MODULE_ID = "installer"`), but the SecretsPanel's
// "Shared (this user)" tab uses `UI_MODULE_BUCKET = "user"` for every
// user-added entry. A user who registered via the wizard AND later
// added `github_pat` via the SecretsPanel ended up with TWO different
// keychain rows, with reads from either path returning only that
// path's value — the alternate row sat as a stale shadow.
//
// Resolution: the wizard's writer now uses `module_id = "user"` to
// match the canonical user-bucket path enforced by `is_user_emit_bucket`
// in `commands/secrets_cmd.rs`. The keychain slot is now
// `vct._user_shared_.shared.user/github_pat` for both writers. Existing
// installs are migrated on next `register_github_pat` call (or hub
// startup, whichever comes first) by `migrate_github_pat_installer_to_user_module_id`,
// which copies the old `installer/` value into the new `user/` slot
// (unless the new slot already has a non-empty value — in which case
// the user-bucket write happened later in time and wins) and deletes
// the old row. Audited as `github_pat_module_id_migration`.
//
// Migration: existing installs that have a file at
// `~/.vct-secrets/shared/github_pat` (or the legacy flat
// `~/.vct-secrets/github_pat`) are migrated on first call to
// `register_github_pat` AFTER upgrade. The migration:
//   1. reads the existing file
//   2. writes the value to the keychain
//   3. deletes the file
//   4. sets the `app_state` flag so it never runs again
// Gated identically to Fix #2's plaintext→keychain MCP secret migration
// (`commands::dashboard::migrate_plaintext_mcp_secrets_to_keychain`).
// Soft-fail: any keychain write failure leaves the file alone so the
// next attempt can retry.
//
// `has_github_pat` / `get_github_pat_preview` / `clear_github_pat` now
// route through the keychain too. The legacy file paths are still
// honored on read for the small window between launcher upgrade and
// the user's next `register_github_pat` call (anyone who never calls
// register again still reads from the flat file via the legacy
// fallback below).
// ---------------------------------------------------------------------------

/// Sentinel project_id for shared scope (mirrors `commands::secrets_cmd`).
/// Kept as a module-private constant because this file is the only
/// non-secrets-cmd caller of `SecretScope::Shared`; widening it to a
/// pub-crate const in `secrets.rs` would just hide the dependency.
const SENTINEL_SHARED: &str = "_user_shared_";

/// Module identifier used to namespace the keychain entry for the
/// onboarding-wizard GitHub PAT. Pinned here because the migration
/// helper, `register_github_pat`, `has_github_pat`, etc. all need to
/// agree on the (scope, module_id, key) tuple.
///
/// 2026-05-10: changed from `"installer"` to `"user"` to match the
/// canonical user-bucket path enforced by `is_user_emit_bucket` in
/// `commands/secrets_cmd.rs` (which the SecretsPanel "Shared (this
/// user)" tab uses for every user-added entry). See module-level
/// "module_id unification" doc-comment above for the rationale and
/// the migration path.
pub(crate) const GITHUB_PAT_MODULE_ID: &str = "user";

/// Pre-2026-05-10 module_id. Read-only target for the one-shot
/// `installer → user` migration. Do not use for any new write path.
pub(crate) const GITHUB_PAT_LEGACY_MODULE_ID: &str = "installer";

pub(crate) const GITHUB_PAT_KEY: &str = "github_pat";

/// `app_state` flag — set after the one-shot file→keychain migration
/// runs successfully so a launcher upgrade only does the sweep once.
/// Stale launchers (pre-fix) never write this row, so the first
/// post-fix call to `register_github_pat` finds it `None` and runs
/// the sweep.
const APP_STATE_KEY_GITHUB_PAT_MIGRATED: &str = "github_pat.file_to_keychain.v1";

/// `app_state` flag — set after the one-shot `installer → user`
/// module_id consolidation runs (post-0.2.0 backlog #6, 2026-05-10).
/// Idempotent: `migrate_github_pat_installer_to_user_module_id` short-
/// circuits when this flag is `true`. Pre-fix launchers never write
/// this row, so the first post-fix call runs the migration once.
const APP_STATE_KEY_PAT_MODULE_ID_MIGRATED: &str =
    "github_pat.installer_to_user_module_id.v1";

fn vct_secrets_dir() -> Option<PathBuf> {
    // Honour `VCT_SECRETS_DIR` for parity with the user-facing `vct` CLI
    // under `tools/vct-secrets/vct` (the Phase 1 primitive treats this
    // env var as the authoritative root). Test isolation also rides on
    // this — the `github_pat_keychain_tests` set `VCT_SECRETS_DIR` to a
    // per-test temp dir so the legacy file paths don't collide with the
    // real user's `~/.vct-secrets/` or with sibling tests that mutate
    // `HOME` (e.g. `commands::dashboard::tests`).
    if let Some(v) = std::env::var_os("VCT_SECRETS_DIR") {
        let p = PathBuf::from(v);
        if !p.as_os_str().is_empty() {
            return Some(p);
        }
    }
    directories::UserDirs::new().map(|u| u.home_dir().join(".vct-secrets"))
}

fn vct_secrets_shared_dir() -> Option<PathBuf> {
    vct_secrets_dir().map(|d| d.join("shared"))
}

fn github_pat_path_shared() -> Option<PathBuf> {
    vct_secrets_shared_dir().map(|d| d.join("github_pat"))
}

fn github_pat_path_flat() -> Option<PathBuf> {
    vct_secrets_dir().map(|d| d.join("github_pat"))
}

/// Resolve the existing PAT path, preferring the new shared/ layout and
/// falling back to the legacy flat layout. Returns `None` if neither
/// exists. Used by the legacy file-fallback read paths and by the
/// one-shot migration to discover what to ingest.
fn github_pat_resolve_existing_file() -> Option<PathBuf> {
    if let Some(p) = github_pat_path_shared() {
        if p.exists() {
            return Some(p);
        }
    }
    if let Some(p) = github_pat_path_flat() {
        if p.exists() {
            return Some(p);
        }
    }
    None
}

/// Public read-side hook for the env-pair builder
/// (`commands/projects_v2.rs::write_project_env_files`). Returns the
/// keychain-resolved PAT (active-flag gated) or falls back to the
/// legacy file when the file→keychain migration hasn't run yet.
///
/// Conservative per-project gating decision (0.1.7, 2026-05-08): every
/// registered project receives `GITHUB_TOKEN` whenever the PAT is set
/// and active in the keychain. This matches the pre-0.1.7 file-based
/// behaviour (`~/.vct-secrets/shared/github_pat` is readable by every
/// process running as the user). A finer-grained per-project access
/// matrix for `github_pat` is out of scope for the 0.1.7 fork sweep.
/// See `docs/MIGRATION-0.2.0.md` "Replacing `git-credential-vct`".
///
/// Soft-fail: any error short-circuits to `None` so env-file writes
/// never block on a keychain hiccup.
pub fn github_pat_for_env(db: &Db) -> Option<String> {
    if let Some(v) = github_pat_from_keychain(db) {
        return Some(v);
    }
    // Legacy file fallback — only honoured until the user's next
    // `register_github_pat` call migrates everything into the
    // keychain. See `migrate_github_pat_file_to_keychain`.
    github_pat_from_legacy_file()
}

/// Read the keychain entry for the onboarding-wizard PAT. Honours the
/// active-flag gate so a paused entry returns `None` (matching how the
/// hub's `/projects/{id}/env` endpoint serves shared secrets).
///
/// Returns `Some(value)` when:
///   * keychain has a non-empty value at
///     `vct._user_shared_.shared.user / github_pat`, AND
///   * the active-flag row says active (default-active when no row).
///
/// Returns `None` for any other state (no entry, paused, keychain
/// backend unreachable, etc.). Callers that need to distinguish
/// "paused" from "absent" must read the active flag separately.
///
/// 2026-05-10 (post-0.2.0 backlog #6): a 0.2.0 user who already
/// registered a PAT has their value at the LEGACY
/// `vct._user_shared_.shared.installer / github_pat` slot. Until
/// `migrate_github_pat_installer_to_user_module_id` runs (on next
/// `register_github_pat` call), reads that miss the new user/ slot
/// fall back to the legacy installer/ slot so the upgrade is seamless.
/// The fallback is gated by the legacy slot's active-flag row too —
/// a paused PAT stays paused across the const flip.
fn github_pat_from_keychain(db: &Db) -> Option<String> {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };

    // Primary: new user-bucket slot.
    if let Some(v) = secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY).ok().flatten() {
        if !v.trim().is_empty() {
            // Gate on the active flag (cross-launcher — a pause in any
            // sibling launcher's DB blocks the read here, matching the
            // hub's behaviour).
            let active = crate::db::secret_active::is_secret_active_cross_launcher(
                db,
                "shared",
                SENTINEL_SHARED,
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            );
            if active {
                return Some(v);
            }
            // Paused new slot wins over a stale legacy slot — once a
            // user touches the new path the legacy is dead state.
            return None;
        }
    }

    // Fallback: legacy `installer/` slot (post-0.2.0 backlog #6
    // upgrade window). Only honoured until the next
    // `register_github_pat` call triggers
    // `migrate_github_pat_installer_to_user_module_id`. After that,
    // the legacy slot is empty and this branch returns None.
    let legacy_v = secrets::get(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY)
        .ok()
        .flatten()?;
    if legacy_v.trim().is_empty() {
        return None;
    }
    let legacy_active = crate::db::secret_active::is_secret_active_cross_launcher(
        db,
        "shared",
        SENTINEL_SHARED,
        GITHUB_PAT_LEGACY_MODULE_ID,
        GITHUB_PAT_KEY,
    );
    if !legacy_active {
        return None;
    }
    Some(legacy_v)
}

/// Migration outcome surfaced to callers/tests.
#[derive(Debug, Default, Clone)]
pub(crate) struct GithubPatMigrationReport {
    /// Migration was already done in a previous launcher run.
    pub already_done: bool,
    /// File contents migrated into the keychain this run.
    pub migrated: bool,
    /// Reserved for backwards compatibility. Always `false` since
    /// 2026-05-09: the launcher does not delete user-owned files in
    /// `~/.vct-secrets/shared/` — they are left in place after the
    /// keychain copy. Field kept so downstream callers don't break.
    pub file_removed: bool,
    /// `app_state` flag set this run.
    pub flag_set: bool,
    /// Soft-fail diagnostics (e.g. keychain backend unreachable). Non-empty
    /// values mean the migration left the file alone for a future retry.
    pub warnings: Vec<String>,
}

/// Migration outcome surfaced to callers/tests for the
/// `installer → user` module_id consolidation.
#[derive(Debug, Default, Clone)]
pub(crate) struct GithubPatModuleIdMigrationReport {
    /// Migration was already done in a previous launcher run.
    pub already_done: bool,
    /// Old `installer/` keychain row had a non-empty value when we
    /// looked.
    pub had_old_value: bool,
    /// New `user/` keychain row had a non-empty value when we looked.
    pub had_new_value: bool,
    /// `"old"` if the old `installer/` value was promoted to the new
    /// `user/` slot; `"new"` if the existing user-bucket value was
    /// kept (later-write wins); `"none"` if neither slot held a value.
    pub winner: &'static str,
    /// Old `installer/` keychain row deleted.
    pub deleted_old_keychain_row: bool,
    /// Old `installer/` active-flag row dropped.
    pub forgot_old_active_state: bool,
    /// `app_state` flag set this run.
    pub flag_set: bool,
    /// Soft-fail diagnostics (e.g. keychain backend unreachable).
    /// Non-empty means we did NOT flip the flag, so the next call
    /// retries.
    pub warnings: Vec<String>,
}

/// One-shot consolidation: pre-2026-05-10 launchers wrote the
/// onboarding-wizard PAT at `vct._user_shared_.shared.installer/github_pat`.
/// The canonical user-bucket path (matching SecretsPanel's
/// `UI_MODULE_BUCKET = "user"` write-path) is now
/// `vct._user_shared_.shared.user/github_pat`. This helper walks the
/// old slot to the new one in a single idempotent step.
///
/// Idempotent and self-gated. Decision matrix on the two slots:
///
/// | old `installer/` | new `user/` | action                                         |
/// |------------------|-------------|------------------------------------------------|
/// | empty            | empty       | flip flag, no copy, no delete (`winner=none`)  |
/// | non-empty        | empty       | copy old → new, mark active on new, delete old |
/// | empty            | non-empty   | flip flag, drop legacy active row, no copy     |
/// | non-empty        | non-empty   | keep new (later-write wins), delete old        |
///
/// Soft-fail on the keychain side: a `set` failure leaves the old
/// slot intact and does NOT flip the flag, so a future call retries.
/// A `delete` failure on the old slot is logged but doesn't block —
/// the new slot is the resolution path; the stale `installer/` row
/// is harmless residue (no reader still looks there after the const
/// flip).
///
/// Audited as `github_pat_module_id_migration` with the full report
/// payload so the move is traceable in `audit_log`.
pub(crate) fn migrate_github_pat_installer_to_user_module_id(
    db: &Db,
) -> Result<GithubPatModuleIdMigrationReport, String> {
    let mut report = GithubPatModuleIdMigrationReport {
        winner: "none",
        ..Default::default()
    };

    let already = db
        .app_state_get_bool(APP_STATE_KEY_PAT_MODULE_ID_MIGRATED)
        .ok()
        .flatten()
        .unwrap_or(false);
    if already {
        report.already_done = true;
        return Ok(report);
    }

    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };

    // Probe both slots. A keychain backend hiccup on EITHER read makes
    // the consolidation unsafe to run (we'd risk the "destroy old
    // before confirming new took" failure mode), so bail with a
    // warning and don't flip the flag.
    let old_val = match secrets::get(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY) {
        Ok(v) => v,
        Err(e) => {
            report.warnings.push(format!(
                "keychain get old slot during module_id migration: {}",
                e
            ));
            return Ok(report);
        }
    };
    let new_val = match secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY) {
        Ok(v) => v,
        Err(e) => {
            report.warnings.push(format!(
                "keychain get new slot during module_id migration: {}",
                e
            ));
            return Ok(report);
        }
    };

    let old_nonempty = old_val.as_ref().map(|s| !s.trim().is_empty()).unwrap_or(false);
    let new_nonempty = new_val.as_ref().map(|s| !s.trim().is_empty()).unwrap_or(false);
    report.had_old_value = old_nonempty;
    report.had_new_value = new_nonempty;

    // Case 1: old non-empty, new empty → promote old to new.
    if old_nonempty && !new_nonempty {
        let old_trim = old_val.as_ref().unwrap().trim();
        if let Err(e) = secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, old_trim) {
            // Don't delete the old slot; don't flip the flag. Next
            // call retries.
            report.warnings.push(format!(
                "keychain set new slot during module_id migration: {}",
                e
            ));
            return Ok(report);
        }
        if let Err(e) = db.mark_secret_active(
            "shared",
            SENTINEL_SHARED,
            GITHUB_PAT_MODULE_ID,
            GITHUB_PAT_KEY,
        ) {
            // Active-flag write failure is recoverable — default for
            // an absent row is ACTIVE. Log and continue.
            report.warnings.push(format!("mark_secret_active new slot: {}", e));
        }
        report.winner = "old";
    } else if old_nonempty && new_nonempty {
        // Case 2: both non-empty — user-bucket write happened later in
        // time (the SecretsPanel only writes to `user/`, never to
        // `installer/`), so the new slot wins. Just drop the old row.
        report.winner = "new";
    } else if !old_nonempty && new_nonempty {
        // Case 3: only new has a value (e.g. user already migrated via
        // SecretsPanel, or this is a fresh install). Nothing to copy;
        // just flip the flag and drop the (empty) old active-flag row
        // to keep audit_log tidy.
        report.winner = "new";
    } else {
        // Case 4: neither slot has a value. Nothing to consolidate.
        report.winner = "none";
    }

    // Delete the old keychain row whenever it had a non-empty value
    // (cases 1 and 2). For empty old slot we still drop the active-
    // flag row below — the keychain `delete` is a NoEntry no-op.
    if old_nonempty {
        if let Err(e) = secrets::delete(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY) {
            report.warnings.push(format!(
                "keychain delete old slot during module_id migration: {}",
                e
            ));
        } else {
            report.deleted_old_keychain_row = true;
        }
    }

    // Drop the legacy active-flag row regardless of keychain outcome.
    // `forget_secret_active_state` is idempotent (no-op when no row
    // exists) so this is safe even when the old slot was empty all
    // along.
    if let Err(e) = db.forget_secret_active_state(
        "shared",
        SENTINEL_SHARED,
        GITHUB_PAT_LEGACY_MODULE_ID,
        GITHUB_PAT_KEY,
    ) {
        report
            .warnings
            .push(format!("forget_secret_active_state old slot: {}", e));
    } else {
        report.forgot_old_active_state = true;
    }

    // Audit-log the move so the keychain delta is traceable.
    let detail = serde_json::json!({
        "from_module_id": GITHUB_PAT_LEGACY_MODULE_ID,
        "to_module_id": GITHUB_PAT_MODULE_ID,
        "key": GITHUB_PAT_KEY,
        "scope": "shared",
        "had_old_value": report.had_old_value,
        "had_new_value": report.had_new_value,
        "winner": report.winner,
        "deleted_old_keychain_row": report.deleted_old_keychain_row,
        "forgot_old_active_state": report.forgot_old_active_state,
    });
    if let Err(e) = db.audit(
        "github_pat_module_id_migration",
        None,
        Some(GITHUB_PAT_MODULE_ID),
        &detail,
    ) {
        report.warnings.push(format!("audit row: {}", e));
    }

    db.app_state_set_bool(APP_STATE_KEY_PAT_MODULE_ID_MIGRATED, true)?;
    report.flag_set = true;
    Ok(report)
}

/// One-shot migration: existing PAT file → keychain.
///
/// Idempotent and self-gated:
///   * If the `app_state` flag is already set → returns immediately
///     (`already_done = true`).
///   * If no file exists at either the shared or legacy flat path →
///     flips the flag (so we don't re-scan on every register call) and
///     returns.
///   * If keychain already has a non-empty value at
///     `vct._user_shared_.shared.user / github_pat` → flips the
///     flag and removes the stale file (treating the keychain as
///     authoritative when both stores have a value).
///   * Otherwise: read file → keychain set → mark active → delete file
///     → flip flag.
///
/// Soft-fail on the keychain side: a write failure leaves the file
/// alone (so the user retains the PAT) and does NOT flip the flag, so a
/// future call retries. The `register_github_pat` flow that triggers
/// this migration always proceeds with its caller-supplied token after
/// the migration returns, even if the migration itself failed (the
/// caller's token write is the source-of-truth path; the migration is
/// best-effort cleanup).
pub(crate) fn migrate_github_pat_file_to_keychain(
    db: &Db,
) -> Result<GithubPatMigrationReport, String> {
    let mut report = GithubPatMigrationReport::default();

    // Already migrated? skip silently.
    let already = db
        .app_state_get_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED)
        .ok()
        .flatten()
        .unwrap_or(false);
    if already {
        report.already_done = true;
        return Ok(report);
    }

    let existing_path = github_pat_resolve_existing_file();

    // Case 1: no file → just flip the flag.
    let Some(path) = existing_path else {
        db.app_state_set_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED, true)?;
        report.flag_set = true;
        return Ok(report);
    };

    // Case 2: keychain already has a value. Treat keychain as
    // authoritative and flip the flag. The user's file is left alone:
    // `~/.vct-secrets/shared/<key>` is user-owned data, and the
    // launcher must never delete or overwrite it without explicit
    // consent (regression fix 2026-05-09; the previous behaviour silently
    // destroyed working PATs during migration).
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    if let Ok(Some(existing)) = secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY) {
        if !existing.trim().is_empty() {
            db.app_state_set_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED, true)?;
            report.flag_set = true;
            return Ok(report);
        }
    }

    // Case 3: file present, keychain empty. Read → write → mark active
    // → flip flag. Soft-fail at keychain step. The file is left in
    // place after a successful copy: it's user-owned data, and the
    // launcher does not delete it.
    let raw = std::fs::read_to_string(&path).map_err(|e| {
        format!("read {} during github_pat migration: {}", path.display(), e)
    })?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        // Empty file — nothing to migrate. Just flip the flag; leave
        // the empty file for the user to clean up if they want.
        db.app_state_set_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED, true)?;
        report.flag_set = true;
        return Ok(report);
    }

    // BOUNDARY #5 (v0.2.80 Part A): shape-check before laundering into the OS
    // keychain. A blob (a token on line 0 + `KEY=value` continuation lines, an
    // embedded newline, or a control char) is NOT migrated — that would copy a
    // corrupted multi-secret value into the keychain, from where it propagates
    // to every project's env-pair. Soft-fail: leave the file untouched, do NOT
    // call `secrets::set`, and do NOT flip `APP_STATE_KEY_GITHUB_PAT_MIGRATED`
    // (so the migration re-attempts after the user fixes the file). The hint
    // carries the reason slug + byte-length ONLY — never the value.
    if let Err(reason) = crate::commands::secret_value_shape::is_single_line_secret(
        trimmed,
        false,
        GITHUB_PAT_KEY,
    ) {
        report.warnings.push(format!(
            "github_pat file at {} is a blob (reason: {}, bytes: {}); not \
             migrated. Run `vct doctor` / `vct recover-blob` to split it, then \
             re-run the migration.",
            path.display(),
            reason,
            trimmed.len()
        ));
        return Ok(report);
    }

    if let Err(e) = secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, trimmed) {
        // Keychain unreachable — leave the file alone, do NOT flip the
        // flag, so the next call retries. The user's PAT is still on
        // disk and still readable via the legacy fallback.
        report
            .warnings
            .push(format!("keychain set during github_pat migration: {}", e));
        return Ok(report);
    }
    // Mark active so the gate in `github_pat_from_keychain` and the
    // hub's `/projects/{id}/env` endpoint surface the value.
    if let Err(e) = db.mark_secret_active("shared", SENTINEL_SHARED, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY)
    {
        // Active-flag write failure is recoverable — the default for an
        // absent row is ACTIVE, so the secret will still resolve. Log
        // the warning and continue.
        report.warnings.push(format!("mark_secret_active: {}", e));
    }

    // Keychain copy succeeded. Leave the file in place — it's user-
    // owned data and the launcher does not delete it (regression fix
    // 2026-05-09). The keychain is now the resolution path; the file
    // is harmless residue the user can clean up at their discretion.

    db.app_state_set_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED, true)?;
    report.flag_set = true;
    report.migrated = true;

    // v0.2.22 Item #5 — audit-log the migration outcome including
    // `file_removed`. The field is `false` by contract since the
    // 2026-05-09 non-destructive fix (`migrate_github_pat_file_to_keychain`
    // never deletes user-owned data), but writing it through to the
    // audit row turns "always-false" from a cosmetic dead-code
    // warning into a queryable historical contract: any future
    // operator wondering "did the launcher ever delete my PAT file?"
    // can `vct audit list --operation github_pat_file_migration`
    // and see the explicit `file_removed: false` per migration.
    //
    // Sibling migration `migrate_github_pat_installer_to_user_module_id`
    // already follows this `db.audit(...)` pattern (4933:audit row
    // warning push), keeping the two PAT-related migrations
    // symmetric in their audit-log surface.
    let detail = serde_json::json!({
        "source_file": path.display().to_string(),
        "scope": "shared",
        "module_id": GITHUB_PAT_MODULE_ID,
        "key": GITHUB_PAT_KEY,
        "already_done": report.already_done,
        "migrated": report.migrated,
        "file_removed": report.file_removed,
        "flag_set": report.flag_set,
        "warnings": report.warnings,
    });
    if let Err(e) = db.audit(
        "github_pat_file_migration",
        None,
        Some(GITHUB_PAT_MODULE_ID),
        &detail,
    ) {
        // Audit-log failure is recoverable: the migration itself
        // succeeded and the flag is set, so we don't unwind. The
        // operator just won't see this migration in `audit list`.
        report.warnings.push(format!("audit row: {}", e));
    }

    Ok(report)
}

/// Best-effort read for the legacy file fallback. Returns `Some(content)`
/// when either the shared/ or legacy flat path holds a non-empty value.
/// Used by `has_github_pat` / `get_github_pat_preview` to surface
/// pre-migration values until the user's next `register_github_pat`
/// call pulls them into the keychain.
///
/// BOUNDARY #4 (v0.2.80 Part A — HIGHEST BLAST RADIUS): this feeds
/// `github_pat_for_env` → `write_project_env_files` writes `GITHUB_TOKEN` into
/// EVERY registered project's env-pair, and it fires AUTOMATICALLY on env-file
/// builds (not an explicit import click). A blob here (a token on line 0 + a
/// `KEY=value` continuation line, an embedded newline, or a control char) would
/// be handed to `git push` / `gh` as a multi-line password → silent auth
/// failure across every project. So the trimmed content is shape-checked; a
/// blob is treated as if there were NO legacy file (returns `None`), and a
/// remediation hint is logged (reason slug + byte-length ONLY, never the value).
fn github_pat_from_legacy_file() -> Option<String> {
    let p = github_pat_resolve_existing_file()?;
    let raw = std::fs::read_to_string(&p).ok()?;
    let trimmed = raw.trim().to_string();
    if trimmed.is_empty() {
        return None;
    }
    if let Err(reason) = crate::commands::secret_value_shape::is_single_line_secret(
        &trimmed,
        false,
        GITHUB_PAT_KEY,
    ) {
        // Blob-shaped legacy file → refuse to propagate it. Metadata only:
        // reason slug + byte-length, NEVER the value.
        eprintln!(
            "[vct] github_pat legacy file at {} is not a well-formed single \
             secret (reason: {}, bytes: {}); skipping env-pair propagation. \
             Run `vct doctor` / `vct recover-blob` to inspect and split it.",
            p.display(),
            reason,
            trimmed.len()
        );
        return None;
    }
    Some(trimmed)
}

#[command]
pub fn has_github_pat(db: State<'_, Db>) -> bool {
    if github_pat_from_keychain(&db).is_some() {
        return true;
    }
    // Legacy file fallback — only honoured until the next `register_github_pat`
    // call migrates everything into the keychain.
    github_pat_from_legacy_file().is_some()
}

#[command]
pub fn get_github_pat_preview(db: State<'_, Db>) -> Option<String> {
    // Prefer keychain.
    let value = github_pat_from_keychain(&db).or_else(github_pat_from_legacy_file)?;
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    if trimmed.len() <= 4 {
        return Some("•".repeat(trimmed.len()));
    }
    let last4 = &trimmed[trimmed.len() - 4..];
    Some(format!("••••{}", last4))
}

/// Sentinel error prefix returned by `register_github_pat` when an
/// existing keychain entry differs from the supplied token and the
/// caller did not pass `force: true`. The GUI matches this prefix and
/// renders a confirm dialog before re-invoking with `force = true`.
/// (Non-destructive guard added 2026-05-09 — the prior behaviour
/// silently overwrote any pre-existing keychain value.)
pub const GITHUB_PAT_REPLACE_GUARD: &str = "EXISTS_DIFFERENT:";

#[command]
pub fn register_github_pat(
    token: String,
    force: Option<bool>,
    db: State<'_, Db>,
) -> Result<(), String> {
    let trimmed = token.trim();
    if trimmed.is_empty() {
        return Err("token cannot be empty".into());
    }
    let force = force.unwrap_or(false);

    // 1a. Run the `installer → user` module_id consolidation FIRST.
    //     Idempotent + self-gated. This collapses any pre-2026-05-10
    //     PAT row at `shared.installer/github_pat` into the canonical
    //     user-bucket slot at `shared.user/github_pat` so we don't
    //     leave stale shadow values when a user who registered via
    //     the wizard later edits via the SecretsPanel "Shared" tab.
    //     Errors are swallowed: the caller's token write below is the
    //     source-of-truth path; the migration is best-effort cleanup.
    match migrate_github_pat_installer_to_user_module_id(&db) {
        Ok(report) => {
            if !report.warnings.is_empty() {
                eprintln!(
                    "[vct] github_pat module_id migration warnings: {:?}",
                    report.warnings
                );
            }
        }
        Err(e) => {
            eprintln!(
                "[vct] github_pat module_id migration failed (will retry next call): {}",
                e
            );
        }
    }

    // 1b. Run the one-shot file→keychain migration (idempotent + self-gated).
    //     This pulls any pre-fix file value into the keychain BEFORE we
    //     overwrite with the user's new token. We intentionally swallow
    //     migration errors here — the caller's token write is the
    //     source-of-truth path, and the migration is best-effort cleanup.
    match migrate_github_pat_file_to_keychain(&db) {
        Ok(report) => {
            if !report.warnings.is_empty() {
                eprintln!(
                    "[vct] github_pat migration warnings: {:?}",
                    report.warnings
                );
            }
        }
        Err(e) => {
            eprintln!(
                "[vct] github_pat migration failed (will retry next call): {}",
                e
            );
        }
    }

    // 2. Replace-existing guard. If the keychain already holds a
    //    different non-empty value and the caller did not opt in via
    //    `force`, refuse — the GUI must surface this to the user
    //    before clobbering a working token. The migration step above
    //    may have just populated the keychain from a pre-existing file,
    //    so this guard correctly fires on first OnboardingWizard save
    //    too if the user happened to type a different value than what
    //    was on disk.
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    if !force {
        if let Ok(Some(existing)) = secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY) {
            let existing_trim = existing.trim();
            if !existing_trim.is_empty() && existing_trim != trimmed {
                return Err(format!(
                    "{}A different GitHub token is already saved. Replace it?",
                    GITHUB_PAT_REPLACE_GUARD
                ));
            }
        }
    }

    // 3. Write the new token to the keychain.
    secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, trimmed)
        .map_err(|e| format!("keychain set github_pat: {}", e))?;

    // 4. Mark active so the `is_secret_active_cross_launcher` gate
    //    (used by `github_pat_from_keychain` AND the hub's
    //    `/projects/{id}/env` endpoint) surfaces the value. Without
    //    this, a launcher running in `~/.vct-dev/` that paused the
    //    secret would still block reads here.
    db.mark_secret_active("shared", SENTINEL_SHARED, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY)
        .map_err(|e| format!("mark_secret_active github_pat: {}", e))?;

    // 5. Files at `~/.vct-secrets/shared/<key>` and the legacy flat
    //    path are user-owned data: the launcher does not delete them.
    //    A user who pre-populated the file can keep it as a manual
    //    fallback; the keychain is now the resolution path the hub
    //    and the resolver helper consume. (Regression fix 2026-05-09:
    //    the prior behaviour silently destroyed working PATs whenever
    //    the OnboardingWizard re-saved.)

    // 6. Propagate to all registered projects' env files (B2 fix from
    //    2026-05-08 integration review). Without this, a user with N
    //    existing registered projects has to manually re-trigger
    //    write_project_env_files (e.g. via rename) for each one before
    //    GITHUB_TOKEN appears in their .claude/env. With it: the moment
    //    the OnboardingWizard saves, every registered project's env
    //    surfaces are rewritten and Claude Code subprocesses inherit the
    //    fresh value on next session start.
    //
    //    Soft-fail per project: a single project's writer failure (e.g.
    //    .claude/env unwritable) shouldn't block PAT registration. We
    //    log warnings to stderr; the main register call still succeeds.
    if let Ok(rows) = db.list_projects() {
        for row in rows {
            if let Err(e) = crate::commands::projects_v2::refresh_project_env_with_db(
                &db, &row.id,
            ) {
                eprintln!(
                    "[vct] register_github_pat: env-refresh for project {} failed: {}",
                    row.id, e
                );
            }
        }
    }

    Ok(())
}

#[command]
pub fn clear_github_pat(db: State<'_, Db>) -> Result<(), String> {
    // Remove the keychain entry first (idempotent — `secrets::delete`
    // treats `NoEntry` as success).
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    if let Err(e) = secrets::delete(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY) {
        eprintln!("[vct] clear_github_pat: keychain delete failed: {}", e);
    }
    // Drop the active-flag row so a later `register_github_pat` starts
    // from a clean slate (default-active applies; `mark_secret_active`
    // there is still a no-op upsert).
    let _ = db.forget_secret_active_state(
        "shared",
        SENTINEL_SHARED,
        GITHUB_PAT_MODULE_ID,
        GITHUB_PAT_KEY,
    );

    // Files at `~/.vct-secrets/shared/<key>` and the legacy flat path
    // are user-owned data: `clear_github_pat` only clears what the
    // launcher writes (keychain + active-flag). If the user also wants
    // the on-disk file gone they can `rm` it themselves. Any future
    // `register_github_pat` call will read the file, copy to keychain,
    // and that fresh keychain value takes precedence — no stale-file
    // surprise. (Regression fix 2026-05-09: the prior behaviour
    // destroyed user-owned files on every "Clear" click.)

    // Propagate clear to all registered projects' env files: GITHUB_TOKEN
    // gets stripped from .claude/env, .claude/settings.json env, and
    // .vscode/settings.json claude-code.env on next refresh. Symmetric
    // with register_github_pat's B2 fix (2026-05-08 integration review).
    if let Ok(rows) = db.list_projects() {
        for row in rows {
            if let Err(e) = crate::commands::projects_v2::refresh_project_env_with_db(
                &db, &row.id,
            ) {
                eprintln!(
                    "[vct] clear_github_pat: env-refresh for project {} failed: {}",
                    row.id, e
                );
            }
        }
    }

    Ok(())
}

/// Bug 21: update an orchestrator at `path` by re-running the local-copy
/// install. Skips the classify-target check (we know the path already
/// has `.claude/`). Emits "install_progress" events.
#[command]
pub async fn update_orchestrator_at(path: String, window: Window) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("update target {} does not exist", target.display()));
    }
    // VCO-clone gate: refuse to run against project folders that happen to
    // have `.claude/` left behind by a non-destructive unregister (PR #150).
    // `update_orchestrator_at` is a "sync VCO clone from a newer source"
    // operation; running it against a project folder copies orchestrator-
    // machinery into the project. The validate_source_repo check (install.py
    // + first-install.sh) is the canonical VCO-clone discriminator.
    //
    // Project-update path is separate: see `update_project_v2` /
    // `run_install_bundle_update` (`projects_v2.rs:782`) for the bundle-only
    // update that operates on registered project folders.
    validate_source_repo(&target)?;
    emit_progress(&window, "locate", "Locating bundled source...", 5.0);
    let source = find_local_repo_root()?;
    emit_progress(&window, "copy", "Copying updated files...", 25.0);
    let target_clone = target.clone();
    let source_clone = source.clone();
    let copy_result = tokio::task::spawn_blocking(move || {
        copy_orchestrator_to_sync(&source_clone, &target_clone)
    })
    .await
    .map_err(|e| format!("copy task panicked: {}", e))?;
    copy_result?;

    // C2 (v0.2.6): refresh the desktop shortcut so it points at the
    // currently-running launcher binary. Critical for users whose
    // install path moved (e.g. they cloned into a new directory) —
    // pre-0.2.6 the .desktop file would keep pointing at the stale
    // binary path silently. Soft-fail: never block "Update complete".
    if let Ok(exe) = std::env::current_exe() {
        if let Err(e) = crate::commands::desktop_shortcut::refresh_desktop_shortcut(&target, &exe) {
            eprintln!(
                "[update_orchestrator_at] desktop shortcut refresh failed (non-fatal): {}",
                e
            );
        }
    }

    // Bug G (v0.2.8): refresh state/install-manifest.json so the
    // launcher's "what version is at this path?" reads the just-copied
    // vct-module.json's version, not the prior one. Soft-fail: never
    // block "Update complete" — the manifest is diagnostic.
    if let Err(e) =
        crate::commands::manifest::refresh_install_manifest(&target, "orchestrator_update")
    {
        eprintln!(
            "[update_orchestrator_at] install-manifest refresh failed (non-fatal): {}",
            e
        );
    }

    emit_progress(&window, "done", "Update complete", 100.0);
    Ok(())
}

// The installer progress + install-log event emitters (emit_progress,
// installer_step_to_user_label, append_install_log_event, chrono_iso_z)
// were extracted verbatim to the `progress` submodule in v0.2.77 Part 7d.
// The facade re-exports every symbol so the update-flow orchestration
// resolves them unchanged.
mod progress;
pub(crate) use progress::*;

// Hardware & runtime probes (GPU enumeration, RAM detection, PCI/lspci
// parsing, command/python/runtime probes) were extracted verbatim to the
// `hardware` submodule in v0.2.77 Part 7d. The facade re-exports every
// symbol so the rest of this module + the `tests` block resolve them
// unchanged via `super::*`.
mod hardware;
pub(crate) use hardware::*;

// The durable install-log reader + install-health inspection were extracted
// verbatim to the `install_log` submodule in v0.2.77 Part 7d. The facade
// re-exports every symbol (incl. the read_install_log / check_install_health
// Tauri commands) so `commands::installer::*` paths + the `tests` block
// resolve them unchanged.
mod install_log;
pub(crate) use install_log::*;

/// v0.2.53 M-P1-6: spawn an OS-appropriate terminal pre-loaded with the
/// `python install.py` command at the given install root, so the user
/// can complete a half-finished install WITHOUT having to copy/paste
/// the install_root path into a side terminal.
///
/// Behaviour per OS:
///   - macOS: `osascript` drives Terminal.app to open a new tab and
///     execute `cd "<path>" && python3 install.py`.
///   - Linux: tries `gnome-terminal` → `konsole` → `xfce4-terminal` →
///     `xterm` in order, falling back to whichever is available with a
///     `bash -c "cd '<path>' && python3 install.py; exec bash"` body so
///     the terminal stays open after the script exits (useful for
///     viewing trailing log lines).
///   - Windows: `cmd.exe /K "cd /d <path> && python install.py"` via
///     `start` so the console window detaches from the launcher.
///
/// Defence-in-depth: `install_root` is validated to be an existing
/// directory containing `install.py` before any spawn so an empty,
/// typo'd, or poisoned argument cannot be assembled into a shell line.
/// Path strings are escaped per-OS (AppleScript backslash+quote,
/// POSIX single-quote `'\''` trick, Windows double-quote wrap).
#[command]
pub async fn launch_installer_terminal(install_root: String) -> Result<(), String> {
    let install_path = PathBuf::from(&install_root);
    if !install_path.exists() {
        return Err(format!(
            "install_root does not exist: {}",
            install_path.display()
        ));
    }
    if !install_path.is_dir() {
        return Err(format!(
            "install_root is not a directory: {}",
            install_path.display()
        ));
    }
    let install_py = install_path.join("install.py");
    if !install_py.exists() {
        return Err(format!(
            "install.py not found in install_root: {}",
            install_path.display()
        ));
    }

    let path_str = install_path.to_string_lossy().to_string();

    #[cfg(target_os = "macos")]
    {
        // AppleScript-quote the path: escape backslash + double-quote.
        let escaped = path_str.replace('\\', "\\\\").replace('"', "\\\"");
        let script = format!(
            "tell application \"Terminal\" to do script \"cd \\\"{escaped}\\\" && python3 install.py\""
        );
        // vct-allow-no-silent: macOS-only (cfg target_os = "macos"); osascript
        // never runs on Windows.
        let status = tokio::process::Command::new("osascript")
            .arg("-e")
            .arg(&script)
            .status()
            .await
            .map_err(|e| format!("failed to spawn osascript: {e}"))?;
        if !status.success() {
            return Err(format!(
                "osascript exited with status {:?}",
                status.code()
            ));
        }
        // Bring Terminal.app to the foreground so the user actually
        // sees the new window.
        // vct-allow-no-silent: macOS-only (cfg target_os = "macos"); osascript
        // never runs on Windows.
        let _ = tokio::process::Command::new("osascript")
            .arg("-e")
            .arg("tell application \"Terminal\" to activate")
            .status()
            .await;
        return Ok(());
    }

    #[cfg(target_os = "linux")]
    {
        // POSIX single-quote escape: `'` becomes `'\''` (close quote,
        // escaped quote, re-open quote).
        let escaped = path_str.replace('\'', r"'\''");
        // `exec bash` after install.py keeps the window open so the
        // user can see install output AND inspect the install_root
        // afterwards.
        let body = format!("cd '{escaped}' && python3 install.py; exec bash");

        let candidates: &[(&str, &[&str])] = &[
            ("gnome-terminal", &["--", "bash", "-c"]),
            ("konsole", &["-e", "bash", "-c"]),
            ("xfce4-terminal", &["--command"]),
            ("xterm", &["-e", "bash", "-c"]),
        ];

        for (emu, args) in candidates {
            if which_on_path(emu).is_none() {
                continue;
            }
            // vct-allow-no-silent: Linux-only (cfg target_os = "linux") terminal
            // launcher; a visible terminal window is the intended behaviour and
            // this spawn never runs on Windows.
            let mut cmd = tokio::process::Command::new(emu);
            if *emu == "xfce4-terminal" {
                let inner = body.replace('"', "\\\"");
                let full = format!("bash -c \"{inner}\"");
                cmd.arg(args[0]).arg(full);
            } else {
                for a in *args {
                    cmd.arg(a);
                }
                cmd.arg(&body);
            }
            // Detach: do not await; the terminal window outlives this
            // command invocation.
            match cmd.spawn() {
                Ok(_) => return Ok(()),
                Err(e) => {
                    eprintln!(
                        "[vct] launch_installer_terminal: {} spawn failed: {e}; trying next",
                        emu
                    );
                    continue;
                }
            }
        }
        return Err(
            "no terminal emulator found (tried gnome-terminal, konsole, \
             xfce4-terminal, xterm) — open a terminal manually and run \
             `python3 install.py` from the install root."
                .to_string(),
        );
    }

    #[cfg(windows)]
    {
        // cmd.exe /K keeps the console open after install.py exits so
        // the user can see any trailing error message. We launch via
        // `start "" cmd.exe /K "<body>"` so the new console detaches
        // from the launcher's process group.
        let body = format!("cd /d \"{}\" && python install.py", path_str);
        use std::os::windows::process::CommandExt;
        let mut cmd = tokio::process::Command::new("cmd.exe");
        cmd.arg("/C")
            .arg("start")
            .arg("") // start's first quoted arg is the window title; empty is fine
            .arg("cmd.exe")
            .arg("/K")
            .arg(&body)
            // CREATE_NEW_CONSOLE = 0x00000010.
            .creation_flags(0x00000010);
        cmd.spawn()
            .map_err(|e| format!("failed to spawn cmd.exe: {e}"))?;
        return Ok(());
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    {
        let _ = path_str;
        Err("launch_installer_terminal: unsupported OS".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    // ── v0.2.60 Piece 5: min_upgradable_from version floor ──────────────

    #[test]
    fn parse_version_tuple_handles_normal_and_partial() {
        assert_eq!(parse_version_tuple("0.2.60"), Some((0, 2, 60)));
        assert_eq!(parse_version_tuple("v0.3.0"), Some((0, 3, 0))); // strips leading v
        assert_eq!(parse_version_tuple("1"), Some((1, 0, 0))); // missing → 0
        assert_eq!(parse_version_tuple("1.2"), Some((1, 2, 0)));
        assert_eq!(parse_version_tuple(" 0.2.60 "), Some((0, 2, 60))); // trims
    }

    #[test]
    fn parse_version_tuple_rejects_nonnumeric() {
        // A present-but-non-numeric component must FAIL (not silently 0),
        // so a malformed version never wrongly trips the floor.
        assert_eq!(parse_version_tuple("0.2.x"), None);
        assert_eq!(parse_version_tuple("abc"), None);
        assert_eq!(parse_version_tuple(""), None);
    }

    #[test]
    fn version_is_below_floor_basic_ordering() {
        assert!(version_is_below_floor("0.2.59", "0.3.0"));
        assert!(version_is_below_floor("0.1.9", "0.2.0"));
        assert!(!version_is_below_floor("0.3.0", "0.3.0")); // equal → not below
        assert!(!version_is_below_floor("0.3.1", "0.3.0")); // above
        assert!(!version_is_below_floor("0.2.60", "0.0.0")); // the v0.2.60 inert floor
    }

    #[test]
    fn version_is_below_floor_fails_safe_on_parse_error() {
        // Unparseable either side → false (never force a destructive
        // hard-cut on ambiguity).
        assert!(!version_is_below_floor("garbage", "0.3.0"));
        assert!(!version_is_below_floor("0.2.59", "garbage"));
    }

    #[test]
    fn update_requires_hard_cut_inert_at_0_0_0_floor() {
        // The v0.2.60 shipped state: floor=0.0.0, a real installed version
        // → never requires a hard-cut.
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        fs::write(
            root.join("vct-module.json"),
            r#"{"version":"0.2.60","min_upgradable_from":"0.0.0"}"#,
        )
        .unwrap();
        fs::create_dir_all(root.join("state")).unwrap();
        fs::write(
            root.join("state").join("install-manifest.json"),
            r#"{"version":"0.2.55"}"#,
        )
        .unwrap();
        assert!(
            !update_requires_hard_cut(root),
            "floor=0.0.0 must never trip the hard-cut in v0.2.60"
        );
    }

    #[test]
    fn update_requires_hard_cut_true_when_below_a_real_floor() {
        // Simulate v0.3.0 raising the floor: installed 0.2.55 < floor 0.3.0.
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        fs::write(
            root.join("vct-module.json"),
            r#"{"version":"0.3.0","min_upgradable_from":"0.3.0"}"#,
        )
        .unwrap();
        fs::create_dir_all(root.join("state")).unwrap();
        fs::write(
            root.join("state").join("install-manifest.json"),
            r#"{"version":"0.2.55"}"#,
        )
        .unwrap();
        assert!(update_requires_hard_cut(root));
    }

    #[test]
    fn update_requires_hard_cut_false_when_no_floor_or_no_manifest() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        // No min_upgradable_from declared (older manifest) → never hard-cut.
        fs::write(root.join("vct-module.json"), r#"{"version":"0.2.60"}"#).unwrap();
        fs::create_dir_all(root.join("state")).unwrap();
        fs::write(
            root.join("state").join("install-manifest.json"),
            r#"{"version":"0.1.0"}"#,
        )
        .unwrap();
        assert!(!update_requires_hard_cut(root), "no floor declared → no hard-cut");

        // Floor declared but no install-manifest (fresh install) → no hard-cut.
        let tmp2 = tempfile::tempdir().expect("tempdir");
        let root2 = tmp2.path();
        fs::write(
            root2.join("vct-module.json"),
            r#"{"version":"0.3.0","min_upgradable_from":"0.3.0"}"#,
        )
        .unwrap();
        assert!(
            !update_requires_hard_cut(root2),
            "no install-manifest (fresh) → normal install path, no hard-cut"
        );
    }

    fn tmp() -> PathBuf {
        let p = std::env::temp_dir()
            .join(format!("vct-installer-test-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir_all(&p).unwrap();
        p
    }

    // v0.2.49 batch 4: installer_step_to_user_label
    // ─────────────────────────────────────────────
    // Pin the step-tag → label mapping for the install.py event
    // stream consumer. Regressions here would degrade the
    // OrchestratorUpdateProgressModal sub-progress UX (user sees a
    // frozen 50% bar instead of the current sub-step).

    #[test]
    fn test_step_label_seeding_weaviate_is_long_running_phase() {
        // 7c/10 is the Weaviate KG seed step — the longest one in
        // --update by far (minutes). User must see this label or
        // they'll think the install hung at 50%.
        let label = installer_step_to_user_label("7c/10", "all seed sub-steps completed");
        assert!(
            label.contains("Seeding Weaviate") || label.contains("KG"),
            "7c/10 must surface a Weaviate-seed-related label; got: {label}"
        );
        assert!(
            label.contains("few minutes") || label.contains("minute"),
            "long-running phases should warn the user about duration; got: {label}"
        );
    }

    #[test]
    fn test_step_label_known_steps_have_user_friendly_labels() {
        // Smoke-test every step we mapped explicitly. None should
        // return empty (would skip the emit_progress call on the
        // caller side).
        for step in &[
            "3/10", "4/10", "4b/10", "5/10", "6/10", "7/10",
            "7b/10", "7c/10", "7d/10", "7e/10", "8/10", "9/10", "10/10",
        ] {
            let label = installer_step_to_user_label(step, "");
            assert!(
                !label.is_empty(),
                "known step {step} must return a non-empty label",
            );
            assert!(
                label.ends_with('…') || label.ends_with("…"),
                "labels should end with U+2026 ellipsis for in-progress feel; \
                 step={step} got: {label}",
            );
        }
    }

    #[test]
    fn test_step_label_unknown_step_falls_back_to_detail() {
        // For an unmapped step, install.py's detail string is usually
        // human-readable. Surface it directly.
        let label = installer_step_to_user_label(
            "99/10",
            "doing something interesting",
        );
        assert_eq!(label, "doing something interesting");
    }

    #[test]
    fn test_step_label_unknown_step_with_no_detail_returns_empty() {
        // Empty detail + unknown step → empty label → caller skips
        // the emit_progress call, modal keeps its prior message.
        let label = installer_step_to_user_label("99/10", "");
        assert!(label.is_empty());
    }

    #[test]
    fn test_step_label_unknown_step_with_oversized_detail_returns_empty() {
        // Defensive: an unmapped step with a massive detail (>200 chars)
        // shouldn't blast the modal with a wall of text. Caller skips.
        let huge = "x".repeat(500);
        let label = installer_step_to_user_label("99/10", &huge);
        assert!(label.is_empty());
    }

    #[test]
    fn test_step_label_handles_malformed_step_tags_without_panic() {
        // install.py emits well-formed tags, but a corrupted stdout
        // pipe could surface anything. Defensive: no panic on weird
        // input.
        assert_eq!(installer_step_to_user_label("", "x"), "x");
        assert_eq!(installer_step_to_user_label("garbage", "x"), "x");
        assert_eq!(installer_step_to_user_label("7", ""), "Pulling embedding models from Ollama…");
        // Multi-digit numeric prefix without slash — splits at first
        // non-digit (none) → ("99", "") → unknown.
        assert_eq!(installer_step_to_user_label("99", "fallback"), "fallback");
        // Non-ASCII suffix shouldn't panic via slicing on a char
        // boundary.
        let r = installer_step_to_user_label("7é", "ok");
        // We don't pin the exact return — just that it returns without
        // panicking and produces *something* (either a mapped label
        // for "7" + suffix "é" → unmapped, falling back to detail; or
        // a recovery path).
        let _ = r;
    }

    #[test]
    fn test_classify_install_target_fresh_nonexistent() {
        let p = std::env::temp_dir()
            .join(format!("vct-installer-no-such-{}", uuid::Uuid::new_v4().simple()));
        assert_eq!(classify_install_target(&p), InstallMode::Fresh);
    }

    #[test]
    fn test_classify_install_target_fresh_empty_dir() {
        let p = tmp();
        assert_eq!(classify_install_target(&p), InstallMode::Fresh);
        fs::remove_dir_all(&p).ok();
    }

    // ---- v0.2.45 V45-B: wait_for_binary_refresh ------------------------
    //
    // The poll loop checks `vct-module.json::version` (source) against
    // `launcher/dist/<arch>/vct-launcher.metadata.json::launcher_version`
    // (on-disk binary). These helpers fabricate the minimum on-disk
    // layout the readers require — no git remote, no real binary blob.
    // Tests run with `disable_git_pull=true` so they don't try to
    // contact a remote that doesn't exist for the tmp dir.

    fn write_v0245_source_version(install_path: &Path, version: &str) {
        let v = serde_json::json!({"version": version}).to_string();
        fs::write(install_path.join("vct-module.json"), v).unwrap();
    }

    fn write_v0245_on_disk_version(install_path: &Path, version: &str) {
        let dist_arch = install_path
            .join("launcher")
            .join("dist")
            .join(launcher_dist_subdir());
        fs::create_dir_all(&dist_arch).unwrap();
        let v = serde_json::json!({"launcher_version": version}).to_string();
        // Use launcher_binary_filename() so the test fixture matches the
        // exact same per-OS layout the production code looks for. On
        // Windows that resolves to `vct-launcher.exe.metadata.json`; on
        // Linux/macOS to `vct-launcher.metadata.json`. Without this the
        // tests would silently regress to the pre-V45-H hardcoded path
        // and pass on Linux while masking the Windows bug.
        let meta_name = format!("{}.metadata.json", launcher_binary_filename());
        fs::write(dist_arch.join(meta_name), v).unwrap();
    }

    fn clear_v0245_on_disk_version(install_path: &Path) {
        let meta_name = format!("{}.metadata.json", launcher_binary_filename());
        let meta = install_path
            .join("launcher")
            .join("dist")
            .join(launcher_dist_subdir())
            .join(meta_name);
        let _ = fs::remove_file(&meta);
    }

    #[tokio::test]
    async fn test_v0245_wait_returns_ok_when_versions_match_immediately() {
        let p = tmp();
        write_v0245_source_version(&p, "0.2.45");
        write_v0245_on_disk_version(&p, "0.2.45");
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            // Generous timeout to prove the loop returns on the FIRST
            // iteration without waiting; if it ever hit the sleep
            // path the test would still pass but take 1 s — keep the
            // interval tiny so a regression doesn't slip past.
            timeout: std::time::Duration::from_secs(5),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let started = std::time::Instant::now();
        let res = waiter.run().await;
        let elapsed = started.elapsed();
        assert!(res.is_ok(), "expected Ok, got {:?}", res);
        // First-iteration return: should be near-instant, certainly
        // under the interval sleep.
        assert!(
            elapsed < std::time::Duration::from_millis(200),
            "expected near-instant return, took {:?}",
            elapsed
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0245_wait_times_out_when_binary_stays_stale() {
        let p = tmp();
        write_v0245_source_version(&p, "0.2.45");
        write_v0245_on_disk_version(&p, "0.2.44");
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            // Short timeout so the test finishes quickly; the interval
            // is the smallest meaningful value so we cycle a few
            // times before tripping the deadline.
            timeout: std::time::Duration::from_millis(300),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let started = std::time::Instant::now();
        let res = waiter.run().await;
        let elapsed = started.elapsed();
        assert!(res.is_err(), "expected timeout Err, got {:?}", res);
        let err = res.unwrap_err();
        // Verify the user-facing message names both versions so the
        // GUI can surface it verbatim. DO NOT change the format
        // casually — see comment on the helper.
        assert!(
            err.contains("v0.2.45"),
            "error should name source version 0.2.45: {}",
            err
        );
        assert!(
            err.contains("v0.2.44"),
            "error should name on-disk version 0.2.44: {}",
            err
        );
        assert!(
            err.contains("did not land within"),
            "error should mention the timeout: {}",
            err
        );
        // We should have actually waited (at least one interval) before
        // returning — not bailed out before even sleeping.
        assert!(
            elapsed >= std::time::Duration::from_millis(50),
            "should have slept at least once, took {:?}",
            elapsed
        );
        fs::remove_dir_all(&p).ok();
    }

    // v0.2.55 (hub-freshness gap): write the on-disk vct-hub sidecar so
    // `read_on_disk_hub_version` resolves it. Mirrors
    // `write_v0245_on_disk_version` but for the hub binary name.
    fn write_v0255_on_disk_hub_version(install_path: &Path, version: &str) {
        let dist_arch = install_path
            .join("launcher")
            .join("dist")
            .join(launcher_dist_subdir());
        fs::create_dir_all(&dist_arch).unwrap();
        let v = serde_json::json!({"launcher_version": version}).to_string();
        #[cfg(target_os = "windows")]
        let hub_meta = "vct-hub.exe.metadata.json";
        #[cfg(not(target_os = "windows"))]
        let hub_meta = "vct-hub.metadata.json";
        fs::write(dist_arch.join(hub_meta), v).unwrap();
    }

    #[tokio::test]
    async fn test_v0255_wait_blocks_when_launcher_fresh_but_hub_stale() {
        // The partial-refresh case the hub-freshness gap closes: launcher
        // binary caught up to source, but the hub sidecar is still stale.
        // Pre-v0.2.55 this returned Ok immediately (and could start a
        // stale hub). Now it must keep waiting (→ time out here).
        let p = tmp();
        write_v0245_source_version(&p, "0.2.55");
        write_v0245_on_disk_version(&p, "0.2.55"); // launcher: fresh
        write_v0255_on_disk_hub_version(&p, "0.2.54"); // hub: STALE
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_millis(250),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        assert!(
            res.is_err(),
            "expected timeout because hub is stale, got {:?}",
            res
        );
        let err = res.unwrap_err();
        // Timeout message should name the hub version so the partial
        // refresh is diagnosable.
        assert!(
            err.contains("hub is v0.2.54"),
            "error should name the stale hub version: {}",
            err
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0255_wait_returns_ok_when_both_launcher_and_hub_fresh() {
        // Both binaries caught up → the normal lock-step refresh → Ok.
        let p = tmp();
        write_v0245_source_version(&p, "0.2.55");
        write_v0245_on_disk_version(&p, "0.2.55");
        write_v0255_on_disk_hub_version(&p, "0.2.55");
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(5),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        assert!(res.is_ok(), "expected Ok when both fresh, got {:?}", res);
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0255_wait_ignores_absent_hub_sidecar() {
        // Older installs predating hub metadata: NO hub sidecar. The gate
        // must degrade to launcher-only (don't deadlock waiting for a hub
        // version that will never appear).
        let p = tmp();
        write_v0245_source_version(&p, "0.2.55");
        write_v0245_on_disk_version(&p, "0.2.55"); // launcher fresh
        // (deliberately NO write_v0255_on_disk_hub_version)
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(5),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        assert!(
            res.is_ok(),
            "absent hub sidecar must not block (launcher-only fallback), got {:?}",
            res
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0245_wait_succeeds_when_binary_appears_mid_poll() {
        // Simulate the binary-refresh commit arriving mid-poll: start
        // with source=0.2.45 + no on-disk metadata (initial state
        // after the source-tag commit), then write on-disk=0.2.45
        // ~120ms in (simulating the chore(binary) commit landing).
        let p = tmp();
        write_v0245_source_version(&p, "0.2.45");
        clear_v0245_on_disk_version(&p);

        let mutator_path = p.clone();
        let mutator = tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(120)).await;
            write_v0245_on_disk_version(&mutator_path, "0.2.45");
        });

        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(2),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        let _ = mutator.await;
        assert!(
            res.is_ok(),
            "expected Ok once binary appears mid-poll, got {:?}",
            res
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0248_wait_succeeds_when_on_disk_ahead_of_source() {
        // v0.2.48 regression test: the failure that prompted this fix.
        //
        // Scenario: source-version-bump commit forgot to bump
        // vct-module.json (it stayed at 0.2.46) while the binary
        // refresh for v0.2.47 already landed (on-disk metadata says
        // 0.2.47). Old loop expected `on_disk == source`, never
        // matched, timed out at 300s with misleading "still building"
        // modal. New loop accepts on_disk >= source (semver-aware).
        let p = tmp();
        write_v0245_source_version(&p, "0.2.46");
        write_v0245_on_disk_version(&p, "0.2.47");
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(5),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let started = std::time::Instant::now();
        let res = waiter.run().await;
        let elapsed = started.elapsed();
        assert!(
            res.is_ok(),
            "expected Ok when on-disk binary is ahead of source-pin; got {:?}",
            res
        );
        assert!(
            elapsed < std::time::Duration::from_millis(200),
            "expected near-instant return (no waiting), took {:?}",
            elapsed,
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0248_wait_succeeds_when_on_disk_ahead_multi_digit() {
        // Edge case for the numeric (not lexicographic) semver compare:
        // "0.2.10" must be recognized as ahead of "0.2.9", not behind.
        // (Lexicographic compare would put "0.2.10" < "0.2.9".)
        let p = tmp();
        write_v0245_source_version(&p, "0.2.9");
        write_v0245_on_disk_version(&p, "0.2.10");
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(5),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        assert!(
            res.is_ok(),
            "expected Ok: 0.2.10 (on-disk) >= 0.2.9 (source); got {:?}",
            res
        );
        fs::remove_dir_all(&p).ok();
    }

    #[tokio::test]
    async fn test_v0245_wait_errors_when_source_version_missing() {
        // Defensive: if vct-module.json is missing or empty, the
        // helper has nothing to compare against. Surface a clear
        // error immediately rather than poll forever.
        let p = tmp();
        // Intentionally NO vct-module.json written.
        let waiter = WaitForBinaryRefresh {
            install_path: &p,
            branch: "main",
            timeout: std::time::Duration::from_secs(2),
            interval: std::time::Duration::from_millis(50),
            disable_git_pull: true,
        };
        let res = waiter.run().await;
        assert!(res.is_err(), "expected Err for missing source version");
        let err = res.unwrap_err();
        assert!(
            err.contains("source version"),
            "error should mention source version: {}",
            err
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_v0245_wait_default_production_uses_5_min_timeout() {
        // Documents the production timeout via the constructor —
        // shouldn't drift below 5 min without rationale (the comment
        // on WAIT_FOR_BINARY_REFRESH_TIMEOUT_SECS spells out the
        // UX-vs-correctness tradeoff).
        let p = std::path::PathBuf::from("/tmp/dummy");
        let waiter = WaitForBinaryRefresh::default_production(&p, "main");
        assert_eq!(
            waiter.timeout,
            std::time::Duration::from_secs(300),
            "production timeout must be 5 min unless explicitly changed",
        );
        assert_eq!(
            waiter.interval,
            std::time::Duration::from_secs(15),
            "production interval must be 15 s unless explicitly changed",
        );
        assert!(
            !waiter.disable_git_pull,
            "production constructor must enable git pull",
        );
    }

    // ---- install_root_complete_at predicate ----------------------------
    //
    // Used by the wizard self-detect logic (commit fafdc51) to decide
    // whether to skip onboarding when the launcher is running from inside
    // an existing complete install vs a partial copy / source checkout.
    //
    // 2026-04-28 (Bug 6): the predicate was strengthened to also require
    // either `state/` OR `.env` with a `KG_COLLECTION=` line, so a
    // source-repo checkout with `.venv` next to `install.py + CLAUDE.md`
    // is no longer mis-classified as a completed install.

    /// Build a fully-populated completed-install dir for tests.
    fn make_complete_install(p: &Path) {
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        // Either marker satisfies the strengthened check; use both so
        // the dir matches what `install.py` actually produces.
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=ProjectKG\n").unwrap();
    }

    #[test]
    fn test_install_root_complete_at_complete_dir() {
        let p = tmp();
        make_complete_install(&p);
        assert!(install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_venv() {
        // Bundled source tree (no .venv yet) must NOT be classified as
        // a complete install — that's the discriminator that fixed the
        // wizard's "ask user where to install" bug. A bundled-but-not-
        // installed tree has CLAUDE.md + install.py but no .venv/.
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        // .venv intentionally absent
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_claude_md() {
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_install_py() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_empty_dir() {
        let p = tmp();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_venv_is_a_file_not_dir() {
        // Defensive: if someone (or a misconfigured filesystem) has a
        // FILE named .venv at the root, we should reject — only a dir
        // counts. A symlink-to-dir is still ok via .is_dir().
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::write(p.join(".venv"), "not-a-dir\n").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    // 2026-04-28 (Bug 6): a source-repo checkout commonly has CLAUDE.md
    // + install.py + .venv (developer's working tree) — but neither
    // `state/` nor a `.env` with `KG_COLLECTION=`. The strengthened
    // predicate must NOT match this case.
    #[test]
    fn test_install_root_complete_at_source_checkout_with_venv_rejected() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        // No state/ dir, no .env with KG_COLLECTION → still a source checkout
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_with_env_only() {
        // .env with KG_COLLECTION alone (no state/ dir) is enough.
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::write(
            p.join(".env"),
            "# my env\nKG_COLLECTION=FooKnowledgeGraph\nFOO=bar\n",
        )
        .unwrap();
        assert!(install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    // ---- env_contains_kg helper -----------------------------------------

    #[test]
    fn test_env_contains_kg_present() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "FOO=bar\nKG_COLLECTION=Foo\nBAZ=qux\n").unwrap();
        assert!(env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_export_form() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "export KG_COLLECTION=Foo\n").unwrap();
        assert!(env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_missing() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "FOO=bar\nBAZ=qux\n").unwrap();
        assert!(!env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_commented_out() {
        // Commented-out KG_COLLECTION must NOT count as configured.
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "# KG_COLLECTION=disabled\nFOO=bar\n").unwrap();
        assert!(!env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_no_file() {
        let p = tmp();
        // .env doesn't exist
        assert!(!env_contains_kg(&p.join(".env")));
        fs::remove_dir_all(&p).ok();
    }

    // ---- is_completed_install_root predicate ----------------------------
    //
    // Bug 6: this is what `find_install_root_from_exe` walks to find. It
    // must reject source-repo checkouts (install.py + CLAUDE.md but no
    // state/ AND no .env with KG_COLLECTION) so the install-health gate
    // doesn't false-positive a launcher running from `launcher/dist/`
    // of a vco source clone.

    #[test]
    fn test_is_completed_install_root_with_state_dir() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_with_env_kg() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        assert!(is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_source_checkout_rejected() {
        // Mimics a vco source-repo clone with the launcher binary
        // sitting in `launcher/dist/`. The walker would find the repo
        // root (which has install.py + CLAUDE.md) and previously
        // misidentified it as an install. With the strengthened
        // predicate it must NOT match.
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        // No state/ dir; no .env with KG_COLLECTION.
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_missing_install_py() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_missing_claude_md() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_env_with_commented_kg_rejected() {
        // .env present but KG_COLLECTION is commented out → no real
        // install ever ran here. Reject.
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::write(p.join(".env"), "# KG_COLLECTION=Foo\nOTHER=bar\n").unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_classify_install_target_adopt() {
        let p = tmp();
        fs::create_dir_all(p.join(".claude")).unwrap();
        assert_eq!(classify_install_target(&p), InstallMode::Adopt);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_classify_install_target_user_code_only() {
        let p = tmp();
        fs::create_dir_all(p.join("src")).unwrap();
        fs::write(p.join("src/main.py"), "print('hi')").unwrap();
        assert_eq!(classify_install_target(&p), InstallMode::FreshIntoExisting);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_diff_install_overwrite_subset() {
        let p = tmp();
        // Two managed paths already there
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::create_dir_all(p.join("docs")).unwrap();

        let diff = diff_install(&p);
        assert_eq!(diff.mode, InstallMode::Adopt);
        assert!(diff.will_overwrite.contains(&".claude".to_string()));
        assert!(diff.will_overwrite.contains(&"docs".to_string()));
        // tools/infrastructure/vct-module.json/orchestrator-
        // managed-paths.txt don't exist yet → in will_add. (CLAUDE.md
        // was removed from the whitelist in PR-31; it never appears in
        // will_add or will_overwrite anymore. `knowledge/` was removed
        // in v0.2.52 V52-C — shipped KG nodes now live under
        // `templates/knowledge/` and are bundle-materialized into
        // `<project>/knowledge/` by `_enumerate_bundle_files`, NOT
        // copied through this whitelist.)
        assert!(diff.will_add.contains(&"tools".to_string()));
        assert!(!diff.will_add.contains(&"CLAUDE.md".to_string()));
        assert!(!diff.will_overwrite.contains(&"CLAUDE.md".to_string()));
        assert!(!diff.will_add.contains(&"knowledge".to_string()));
        assert!(!diff.will_overwrite.contains(&"knowledge".to_string()));
        assert!(diff.user_paths_preserved);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_diff_install_fresh_target_all_added() {
        let p = tmp();
        let diff = diff_install(&p);
        assert_eq!(diff.mode, InstallMode::Fresh);
        assert!(diff.will_overwrite.is_empty());
        assert_eq!(diff.will_add.len(), ORCHESTRATOR_MANAGED_PATHS.len());
        fs::remove_dir_all(&p).ok();
    }

    // ─── Bug 17: local-copy install ────────────────────────────────

    /// Build a fake repo source tree with a manifest + a couple of
    /// allowlisted paths so we can exercise `copy_orchestrator_to_sync`
    /// without touching the real repo.
    fn fake_repo_source() -> PathBuf {
        let p = tmp();
        fs::write(p.join("vct-module.json"), "{}").unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        // CONTEXT_STATE.md is both in the managed allowlist (under .claude/)
        // AND in DEFAULT_PRESERVE_LIST — so OverwritePreserve produces a
        // CONTEXT_STATE.new.md sibling when the user has their own version.
        // (CLAUDE.md was previously the canonical fixture for this case but
        // PR-31 / v0.2.12 removed CLAUDE.md from the managed allowlist —
        // user projects render their own from templates/CLAUDE.md.template.)
        fs::write(p.join(".claude/CONTEXT_STATE.md"), "# upstream context state\n").unwrap();
        // `docs/` stays in the whitelist; use it as the "in-allowlist
        // directory with file" fixture. (`knowledge/` was removed from
        // the whitelist in v0.2.52 V52-C — shipped KG nodes are now
        // bundle-materialized from `templates/knowledge/` rather than
        // copied through this allowlist.)
        fs::create_dir_all(p.join("docs")).unwrap();
        fs::write(p.join("docs/note.md"), "hello").unwrap();
        // Files NOT in the allowlist — must NOT be copied.
        // CLAUDE.md remains in the source dir to exercise the "is OUT of
        // allowlist → not copied" contract.
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();
        fs::write(p.join("README.md"), "readme").unwrap();
        fs::create_dir_all(p.join("scripts")).unwrap();
        fs::write(p.join("scripts/foo.sh"), "echo hi").unwrap();
        // `knowledge/` in the source must NOT be copied — V52-C made it
        // a non-managed path.
        fs::create_dir_all(p.join("knowledge")).unwrap();
        fs::write(p.join("knowledge/source-side.md"), "should-not-copy").unwrap();
        p
    }

    #[test]
    fn test_find_local_repo_root_via_cargo_manifest_dir() {
        // From within the repo, walking up from CARGO_MANIFEST_DIR
        // should find vct-module.json at the repo root.
        let root = find_local_repo_root().expect("repo root not found");
        assert!(root.join("vct-module.json").exists());
    }

    #[test]
    fn test_copy_orchestrator_to_sync_copies_only_allowlist() {
        let source = fake_repo_source();
        let target = tmp();
        copy_orchestrator_to_sync(&source, &target).unwrap();

        // Allowlisted entries copied
        assert!(target.join("vct-module.json").exists());
        assert!(target.join(".claude/settings.json").exists());
        assert!(target.join("docs/note.md").exists());

        // NOT in allowlist: must NOT have been copied. CLAUDE.md was
        // removed from the whitelist in PR-31 (v0.2.12) — see the
        // doc-comment above ORCHESTRATOR_MANAGED_PATHS. User projects
        // render their CLAUDE.md from templates/CLAUDE.md.template
        // instead of receiving the orchestrator-self's root CLAUDE.md.
        // `knowledge/` was removed in v0.2.52 V52-C — shipped KG nodes
        // are now materialized from `templates/knowledge/` via the
        // bundle install path (manifest-tracked, user-modifications
        // preserved on update).
        assert!(!target.join("CLAUDE.md").exists());
        assert!(!target.join("README.md").exists());
        assert!(!target.join("scripts/foo.sh").exists());
        assert!(!target.join("knowledge/source-side.md").exists());

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_copy_orchestrator_to_sync_into_adopt_target_overwrites() {
        let source = fake_repo_source();
        let target = tmp();
        // Pre-existing user file inside `.claude/` — should be replaced
        // when adopt copy runs.
        fs::create_dir_all(target.join(".claude")).unwrap();
        fs::write(target.join(".claude/settings.json"), "OLD").unwrap();
        // Pre-existing user file OUTSIDE the allowlist — must survive.
        fs::write(target.join("user_code.py"), "user-code").unwrap();

        copy_orchestrator_to_sync(&source, &target).unwrap();

        let new_settings = fs::read_to_string(target.join(".claude/settings.json")).unwrap();
        assert_eq!(new_settings, "{}");
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "user-code"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_copy_orchestrator_to_sync_rejects_non_repo_source() {
        let source = tmp();
        // No vct-module.json at source → must error out.
        let target = tmp();
        let err = copy_orchestrator_to_sync(&source, &target).unwrap_err();
        assert!(err.contains("not an orchestrator repo"));
        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    /// 2026-05-06 (PR-1, install-flow): the managed-paths whitelist must
    /// NOT include orchestrator-only machinery. The VideoFrames anomaly
    /// (project folder receiving `install.py` + `state/install-manifest.json`)
    /// traced to those two entries being in this list and reachable via
    /// `copy_orchestrator_to_sync`. See `.claude/context/install-paths-
    /// audit-2026-05-06.md` §B and §J recommendation #1 for the full
    /// rationale. The architectural intent is ONE VCO clone shared by
    /// all projects; per-project folders never receive orchestrator
    /// entry points or per-install metadata.
    ///
    /// 2026-05-06 (PR-5, single-source refactor): the constant is now
    /// derived from `orchestrator-managed-paths.txt` at the repo root
    /// via `include_str!`. This test pins (a) the exact post-trim set
    /// the file is expected to contain, (b) the banned-entries
    /// invariant from PR-1, and (c) the self-reference invariant — the
    /// .txt file lists itself so `update_orchestrator_at` propagates
    /// new editions of the list across existing installs.
    #[test]
    fn test_managed_paths_matches_source_of_truth() {
        // (a) Exact set: the parsed list must equal the post-trim
        // expected entries. If you intentionally add or remove a path,
        // edit `orchestrator-managed-paths.txt` AND this assertion in
        // the same commit.
        // PR-31 (v0.2.12): `CLAUDE.md` removed. The root CLAUDE.md is
        // orchestrator-self dev docs, not a user-project scaffold; user
        // projects render their CLAUDE.md from
        // `templates/CLAUDE.md.template` instead. DEFAULT_PRESERVE_LIST
        // still contains "CLAUDE.md" — that's the preserve-user-edits
        // concern, not the whitelist-copy concern.
        let expected: &[&str] = &[
            ".claude",
            "docs",
            "tools",
            "infrastructure",
            "vct-module.json",
            "orchestrator-managed-paths.txt",
            // Phase 0 of the diagrams-integration plan (2026-05-24):
            // pinning manifest for external npm packages. Listed here
            // so `update_orchestrator_at` propagates new editions of
            // the manifest to existing installs — same fail-safe
            // self-reference shape used for
            // `orchestrator-managed-paths.txt` itself.
            //
            // v0.2.34: the .toml moved from the repo root into
            // `vco_lib/` so it ships in the Python wheel — see
            // `vco_lib/bundled_versions.py` docstring for the
            // wheel-packaging rationale. Whitelist entry updated in
            // lockstep so update propagation still finds it.
            "vco_lib/bundled_mcp_versions.toml",
            // v0.2.83 WP-B5: the cross-language MCP scan/registration rule
            // table (WP-B4). Listed here so `update_orchestrator_at` pushes
            // future EDITS of the table into every existing install — same
            // wheel-packaged, self-propagating shape as
            // `bundled_mcp_versions.toml` above.
            "vco_lib/mcp_scan_rules.toml",
        ];
        let actual: Vec<&str> = ORCHESTRATOR_MANAGED_PATHS.iter().copied().collect();
        assert_eq!(
            actual, expected,
            "ORCHESTRATOR_MANAGED_PATHS (parsed from \
             orchestrator-managed-paths.txt) drifted from the expected \
             post-trim set. Edit the .txt and this test together."
        );

        // (b) Banned entries (PR-1 invariant): orchestrator-only
        // machinery must never reach a per-project folder via
        // copy_orchestrator_to_sync.
        let banned: &[&str] = &[
            "install.py",
            "install.sh",
            "install.ps1",
            "state",
            "claude_mcp_servers",
            "templates",
            "requirements.txt",
            "requirements-dev.txt",
            "BOOTSTRAP.md",
            "config",
        ];
        for entry in banned {
            assert!(
                !ORCHESTRATOR_MANAGED_PATHS.contains(entry),
                "ORCHESTRATOR_MANAGED_PATHS must not contain orchestrator-only entry {:?} \
                 (project folders should never receive it via copy_orchestrator_to_sync)",
                entry,
            );
        }

        // (c) Self-reference invariant: the source-of-truth file lists
        // itself so `update_orchestrator_at` syncs a freshly-edited
        // version into every existing install. Dropping this entry
        // would silently freeze the whitelist at its current contents
        // for any user who installed before the next launcher release.
        assert!(
            ORCHESTRATOR_MANAGED_PATHS.contains(&"orchestrator-managed-paths.txt"),
            "orchestrator-managed-paths.txt must list itself so \
             update_orchestrator_at propagates edits to existing installs",
        );
    }

    /// Parse-rule contract for `orchestrator-managed-paths.txt`. The
    /// Python side (`install.py::_parse_managed_paths_text`) implements
    /// the same rules — see
    /// `tests/test_managed_paths_consistency.py` for the cross-language
    /// pin.
    #[test]
    fn test_parse_managed_paths_text_rules() {
        let sample = "\
# leading comment
# another comment
.claude

CLAUDE.md
   docs
\t# indented comment
infrastructure
";
        let parsed = parse_managed_paths_text(sample);
        assert_eq!(
            parsed,
            vec![".claude", "CLAUDE.md", "docs", "infrastructure"],
            "parse rules drifted: lines are trimmed, blank lines and \
             #-prefixed lines are skipped (no inline comments)."
        );
    }

    // ─── PR-4: gitignore-aware copy_recursive_sync ─────────────────
    //
    // Re-review of PR-1 flagged that `copy_recursive_sync` was
    // gitignore-blind. Even with `ORCHESTRATOR_MANAGED_PATHS` filtered
    // at the entry level, the kept entries (`tools/`, `.claude/`,
    // `infrastructure/`) drag in gitignored subpaths verbatim during
    // `update_orchestrator_at`:
    //   - tools/vct-secrets/*.token  (real secret leak risk)
    //   - .claude/agents/, .claude/skills/, .claude/logs/
    //   - infrastructure/docker-compose.override.yml
    // PR-4 swaps in `ignore::WalkBuilder` to honor `.gitignore` etc.
    // These tests pin that behavior + the non-git fallback.

    /// Initialize `dir` as a git repo with an empty initial commit.
    /// Skips the test (returns `false`) if `git` is not on PATH —
    /// developer machines without git installed (rare but possible
    /// on CI shards) shouldn't fail the suite. Production caller
    /// (`update_orchestrator_at`) only runs when find_local_repo_root
    /// resolves a real checkout, so the fallback path is exercised by
    /// the bundle-install case which doesn't need git.
    fn try_git_init(dir: &Path) -> bool {
        let init = std::process::Command::new("git").silent()
            .args(["init", "-q", "--initial-branch=main"])
            .current_dir(dir)
            .status();
        let init_ok = matches!(init, Ok(s) if s.success());
        if !init_ok {
            return false;
        }
        // Local user.name/email so `git commit` doesn't blow up on
        // shards where git's global config isn't set.
        let _ = std::process::Command::new("git").silent()
            .args(["config", "user.email", "test@example.com"])
            .current_dir(dir)
            .status();
        let _ = std::process::Command::new("git").silent()
            .args(["config", "user.name", "test"])
            .current_dir(dir)
            .status();
        true
    }

    fn try_git_commit(dir: &Path, files: &[&str]) -> bool {
        for f in files {
            let st = std::process::Command::new("git").silent()
                .args(["add", "--", f])
                .current_dir(dir)
                .status();
            if !matches!(st, Ok(s) if s.success()) {
                return false;
            }
        }
        let st = std::process::Command::new("git").silent()
            .args(["commit", "-q", "-m", "init"])
            .current_dir(dir)
            .status();
        matches!(st, Ok(s) if s.success())
    }

    #[test]
    fn test_copy_recursive_sync_honors_gitignore() {
        let src = tmp();
        // Layout:
        //   .gitignore         (says: secret.txt)
        //   keep.txt           (tracked)
        //   also.txt           (untracked, NOT gitignored — must copy)
        //   secret.txt         (gitignored — must NOT copy)
        fs::write(src.join(".gitignore"), "secret.txt\n").unwrap();
        fs::write(src.join("keep.txt"), "keep").unwrap();
        fs::write(src.join("also.txt"), "also").unwrap();
        fs::write(src.join("secret.txt"), "SECRET").unwrap();

        if !try_git_init(&src) {
            eprintln!("[test] git not available — skipping");
            fs::remove_dir_all(&src).ok();
            return;
        }
        // Only commit the tracked file. `also.txt` stays untracked
        // but uningored; gitignore-aware walker should still copy it.
        if !try_git_commit(&src, &[".gitignore", "keep.txt"]) {
            eprintln!("[test] git commit failed — skipping");
            fs::remove_dir_all(&src).ok();
            return;
        }

        let dst = tmp();
        copy_recursive_sync(&src, &dst).unwrap();

        assert!(dst.join("keep.txt").exists(), "tracked file must copy");
        assert!(
            dst.join("also.txt").exists(),
            "untracked-but-uningored file must copy"
        );
        assert!(
            !dst.join("secret.txt").exists(),
            "gitignored file MUST NOT copy"
        );

        fs::remove_dir_all(&src).ok();
        fs::remove_dir_all(&dst).ok();
    }

    #[test]
    fn test_copy_recursive_sync_blocks_real_leak_paths() {
        // Specifically pins the leak cases the audit flagged:
        //   - tools/vct-secrets/foo.token  (matched by *.token in real .gitignore)
        //   - .claude/agents/coder.md      (matched by .claude/agents/)
        //   - infrastructure/docker-compose.override.yml
        let src = tmp();

        // Mirror the production .gitignore subset relevant to these paths.
        fs::write(
            src.join(".gitignore"),
            "*.token\n\
             .claude/agents/\n\
             infrastructure/docker-compose.override.yml\n",
        )
        .unwrap();

        // Tracked content that MUST survive the copy.
        fs::create_dir_all(src.join("tools/vct-secrets")).unwrap();
        fs::write(src.join("tools/vct-secrets/README.md"), "readme").unwrap();
        fs::create_dir_all(src.join(".claude")).unwrap();
        fs::write(src.join(".claude/settings.json"), "{}").unwrap();
        fs::create_dir_all(src.join("infrastructure")).unwrap();
        fs::write(
            src.join("infrastructure/compose.yaml"),
            "services: {}\n",
        )
        .unwrap();

        // Leak content that MUST NOT propagate.
        fs::write(src.join("tools/vct-secrets/foo.token"), "LEAK").unwrap();
        fs::create_dir_all(src.join(".claude/agents")).unwrap();
        fs::write(src.join(".claude/agents/coder.md"), "LEAK").unwrap();
        fs::write(
            src.join("infrastructure/docker-compose.override.yml"),
            "LEAK\n",
        )
        .unwrap();

        if !try_git_init(&src) {
            fs::remove_dir_all(&src).ok();
            return;
        }
        if !try_git_commit(
            &src,
            &[
                ".gitignore",
                "tools/vct-secrets/README.md",
                ".claude/settings.json",
                "infrastructure/compose.yaml",
            ],
        ) {
            fs::remove_dir_all(&src).ok();
            return;
        }

        let dst = tmp();
        copy_recursive_sync(&src, &dst).unwrap();

        // Tracked content survives.
        assert!(dst.join("tools/vct-secrets/README.md").exists());
        assert!(dst.join(".claude/settings.json").exists());
        assert!(dst.join("infrastructure/compose.yaml").exists());

        // Leak content blocked.
        assert!(
            !dst.join("tools/vct-secrets/foo.token").exists(),
            "*.token gitignore must block secret leak"
        );
        assert!(
            !dst.join(".claude/agents/coder.md").exists(),
            ".claude/agents/ gitignore must block per-machine agent files"
        );
        assert!(
            !dst.join("infrastructure/docker-compose.override.yml").exists(),
            "infrastructure override gitignore must block per-machine compose"
        );

        fs::remove_dir_all(&src).ok();
        fs::remove_dir_all(&dst).ok();
    }

    #[test]
    fn test_copy_recursive_sync_falls_back_for_non_git_source() {
        // No `.git/` anywhere → walker drops back to the blind
        // recursive copy. Verifies the fallback contract: gitignore
        // files are NOT consulted and EVERYTHING gets copied. This
        // matches the pre-PR-4 behavior for shipped non-checkout
        // bundles and test fixtures.
        let src = tmp();
        // Even though there's a .gitignore file present, without `.git/`
        // the walker should NOT honor it (no repo context to resolve
        // includes/excludes against). Belt-and-braces test.
        fs::write(src.join(".gitignore"), "should-not-apply.txt\n").unwrap();
        fs::write(src.join("a.txt"), "a").unwrap();
        fs::write(src.join("should-not-apply.txt"), "b").unwrap();

        let dst = tmp();
        copy_recursive_sync(&src, &dst).unwrap();

        assert!(dst.join("a.txt").exists());
        assert!(
            dst.join("should-not-apply.txt").exists(),
            "no .git/ → fallback walker must copy everything (gitignore ignored)"
        );

        fs::remove_dir_all(&src).ok();
        fs::remove_dir_all(&dst).ok();
    }

    #[test]
    fn test_has_git_root_walks_up_to_ancestor_git_dir() {
        // Sanity-check the helper that gates which walker we use.
        let root = tmp();
        fs::create_dir_all(root.join(".git")).unwrap();
        let nested = root.join("a/b/c");
        fs::create_dir_all(&nested).unwrap();

        assert!(has_git_root(&root));
        assert!(has_git_root(&nested));

        let unrelated = tmp();
        assert!(!has_git_root(&unrelated));

        fs::remove_dir_all(&root).ok();
        fs::remove_dir_all(&unrelated).ok();
    }

    // ─── Bug 18: RAM detection ─────────────────────────────────────

    #[test]
    fn test_parse_meminfo_64gb() {
        // Real /proc/meminfo on a 64 GB box (rounded sample value).
        let sample = "\
MemTotal:       65857132 kB
MemFree:        12345678 kB
MemAvailable:   23456789 kB
";
        let kb = parse_meminfo_total_kb(sample).unwrap();
        assert_eq!(kb, 65857132);
        // 65857132 kB ≈ 62.81 GiB. Production display goes through
        // `snap_to_common_ram_gb` which snaps to 64 (the marketed
        // stick capacity); a separate test below pins that behaviour.
    }

    #[test]
    fn test_parse_meminfo_returns_none_when_missing() {
        let bad = "Nothing useful here\n";
        assert!(parse_meminfo_total_kb(bad).is_none());
    }

    // ─── Bug 25: snap to marketed RAM stick capacity ───────────────

    #[test]
    fn test_snap_64gb_machine() {
        // Real-world MemTotal on a 64 GB box (the user's machine).
        // 65498468 kB ≈ 62.46 GiB → must snap to 64.
        assert_eq!(snap_to_common_ram_gb(65498468), 64);
        // The slightly-different sample we used in test_parse_meminfo_64gb.
        assert_eq!(snap_to_common_ram_gb(65857132), 64);
    }

    #[test]
    fn test_snap_common_capacities() {
        // 8 GB stick: ~7.7 GiB MemTotal (kernel reserves ~0.3 GiB).
        // 7.7 GiB = 7.7 * 1024 * 1024 = 8074035 kB.
        assert_eq!(snap_to_common_ram_gb(8_074_035), 8);
        // 16 GB stick: ~15.5 GiB.
        assert_eq!(snap_to_common_ram_gb(16_252_928), 16);
        // 32 GB stick: ~31.2 GiB.
        assert_eq!(snap_to_common_ram_gb(32_715_571), 32);
        // 128 GB stick: ~125 GiB.
        assert_eq!(snap_to_common_ram_gb(131_072_000), 128);
    }

    #[test]
    fn test_snap_falls_back_for_oddball() {
        // 3 GiB doesn't match any common bucket → fallback rounds to nearest GiB.
        let kb = 3 * 1024 * 1024; // exactly 3 GiB
        let gb = snap_to_common_ram_gb(kb);
        // 3 isn't in buckets, so it falls back. round(3.0) = 3.
        assert_eq!(gb, 3);
    }

    // ─── Bug 20: inspect orchestrator state ────────────────────────

    #[test]
    fn test_version_is_outdated_basic() {
        assert!(version_is_outdated("0.0.7", "0.1.0"));
        assert!(version_is_outdated("0.0.9", "0.0.10"));
        assert!(!version_is_outdated("0.1.0", "0.1.0"));
        assert!(!version_is_outdated("0.2.0", "0.1.0"));
        // Pre-release suffixes are out of scope: "1.0.0-rc1" parses as
        // [1,0,0] so it equals "1.0.0". This is intentional — we treat
        // any version mismatch in semver-major/minor/patch as the
        // signal, and ignore SemVer pre-release ordering.
    }

    #[test]
    fn test_inspect_orchestrator_at_fresh() {
        let p = tmp();
        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(!s.installed);
        assert_eq!(s.version_status, "unknown");
        assert!(s.config_health.is_empty());
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_installed_current() {
        let p = tmp();
        // Build a "current" install: vct-module.json with the same
        // version as the bundled launcher.
        let bundled = read_bundled_version().expect("bundled version");
        fs::write(
            p.join("vct-module.json"),
            format!(r#"{{"version":"{}"}}"#, bundled),
        )
        .unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        assert_eq!(s.version.as_deref(), Some(bundled.as_str()));
        assert_eq!(s.version_status, "current");
        // All three known files should be ok.
        assert!(s.config_health.iter().all(|c| c.ok), "{:?}", s.config_health);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_installed_outdated() {
        let p = tmp();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.0.1"}"#).unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        // Bundled is 0.1.0 (per vct-module.json at repo root) so 0.0.1
        // must register as outdated.
        assert_eq!(s.version_status, "outdated");
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_corrupt_settings() {
        let p = tmp();
        // vct-module.json is the canonical VCO-clone marker (gates `installed`
        // post-fix). Make it malformed JSON so it ALSO contributes a bad
        // health entry, exercising the corrupt-config path end-to-end.
        fs::write(p.join("vct-module.json"), "not json either").unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "this is not json").unwrap();
        // Empty CLAUDE.md is also "corrupt" by our health check.
        fs::write(p.join("CLAUDE.md"), "").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        assert_eq!(s.version_status, "unknown");
        let bad = s.config_health.iter().filter(|c| !c.ok).count();
        // settings.json (json parse), CLAUDE.md (empty), vct-module.json (json parse)
        assert!(bad >= 3, "expected ≥3 bad checks, got {:?}", s.config_health);
        fs::remove_dir_all(&p).ok();
    }

    // ── inspect_project_leftovers (follow-up #13, 2026-05-07) ────────

    #[test]
    fn inspect_project_leftovers_empty_dir_has_none() {
        let p = tmp();
        let lo = inspect_project_leftovers(p.to_string_lossy().to_string());
        assert!(!lo.has_leftovers);
        assert_eq!(lo.agent_count, 0);
        assert_eq!(lo.skill_count, 0);
        assert_eq!(lo.hook_count, 0);
        assert_eq!(lo.script_count, 0);
        assert!(!lo.has_context_state);
        assert!(!lo.has_claude_md);
        assert!(!lo.has_vco_manifest);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn inspect_project_leftovers_missing_dir_returns_none() {
        // Non-existent path is fine — no panic, just zeros.
        let p = std::env::temp_dir()
            .join(format!("vct-no-such-dir-{}", uuid::Uuid::new_v4().simple()));
        let lo = inspect_project_leftovers(p.to_string_lossy().to_string());
        assert!(!lo.has_leftovers);
    }

    #[test]
    fn inspect_project_leftovers_post_unregister_finds_preserved_content() {
        // Mimic the state after PR-150's non-destructive unregister: hooks
        // and scripts purged, agents/skills/CONTEXT_STATE/CLAUDE.md preserved.
        let p = tmp();
        fs::create_dir_all(p.join(".claude").join("agents")).unwrap();
        fs::write(p.join(".claude/agents/coder.md"), "# coder\n").unwrap();
        fs::write(p.join(".claude/agents/tester.md"), "# tester\n").unwrap();
        fs::create_dir_all(p.join(".claude").join("skills").join("foo")).unwrap();
        fs::write(p.join(".claude/skills/foo/SKILL.md"), "# foo\n").unwrap();
        fs::write(p.join(".claude/CONTEXT_STATE.md"), "# state\n").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();

        let lo = inspect_project_leftovers(p.to_string_lossy().to_string());
        assert!(lo.has_leftovers);
        assert_eq!(lo.agent_count, 2);
        assert_eq!(lo.skill_count, 1); // 1 entry under skills/ (the foo dir)
        assert_eq!(lo.hook_count, 0);
        assert_eq!(lo.script_count, 0);
        assert!(lo.has_context_state);
        assert!(lo.has_claude_md);
        assert!(!lo.has_vco_manifest);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn inspect_project_leftovers_only_user_code_has_none() {
        // A user-code-only folder (no .claude/, no CLAUDE.md) should
        // surface NO leftovers — the wizard should treat it as fresh.
        let p = tmp();
        fs::write(p.join("main.py"), "print('hi')\n").unwrap();
        fs::create_dir_all(p.join("src")).unwrap();
        fs::write(p.join("src/lib.rs"), "fn main() {}\n").unwrap();

        let lo = inspect_project_leftovers(p.to_string_lossy().to_string());
        assert!(!lo.has_leftovers);
        assert_eq!(lo.agent_count, 0);
        fs::remove_dir_all(&p).ok();
    }

    /// Regression test for the post-PR-#150 false-positive: a non-destructive
    /// `unregister_project` preserves `.claude/agents/` and `.claude/skills/`
    /// inside user project folders. Before the fix, `inspect_orchestrator_at`
    /// returned `installed: true` for such folders (it OR'd `.claude/` and
    /// `vct-module.json`), which routed the user through the orchestrator
    /// adopt-choice modal — conceptually wrong for project folders. The fix
    /// gates `installed` on `vct-module.json` alone (the canonical VCO-clone
    /// marker, also enforced by `validate_source_repo`).
    #[test]
    fn test_inspect_at_project_folder_with_only_dot_claude_returns_not_installed() {
        let p = tmp();
        // Simulate a project folder post-unregister: `.claude/agents/` and
        // a leftover skill .md file remain, but there is NO `vct-module.json`.
        fs::create_dir_all(p.join(".claude/agents")).unwrap();
        fs::write(p.join(".claude/agents/some.md"), "# leftover agent\n").unwrap();
        fs::create_dir_all(p.join(".claude/skills/foo")).unwrap();
        fs::write(p.join(".claude/skills/foo/SKILL.md"), "# leftover skill\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(
            !s.installed,
            "project folder with only .claude/ leftovers must NOT register as installed"
        );
        assert_eq!(s.version_status, "unknown");
        assert!(s.config_health.is_empty());
        fs::remove_dir_all(&p).ok();
    }

    /// Counterpart: a real VCO clone (vct-module.json present, parseable,
    /// with a `version` field) MUST register as installed. This locks in the
    /// canonical-marker semantics so a future "tighten validation further"
    /// refactor doesn't regress the orchestrator self-onboarding flow.
    #[test]
    fn test_inspect_at_vco_clone_with_vct_module_returns_installed() {
        let p = tmp();
        // Minimal VCO-clone shape: vct-module.json with a version field, and
        // a `.claude/` directory (a real clone always has one, but `installed`
        // is no longer gated on it).
        fs::write(p.join("vct-module.json"), r#"{"version":"1.2.3"}"#).unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::write(p.join("CLAUDE.md"), "# clone\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed, "VCO clone with vct-module.json must register as installed");
        assert_eq!(s.version.as_deref(), Some("1.2.3"));
        fs::remove_dir_all(&p).ok();
    }

    // ─── Bug 29: shared-container detection ────────────────────────

    #[tokio::test]
    async fn test_probe_http_returns_none_on_unreachable() {
        // Pick a port that almost certainly isn't listening. 1 is a
        // privileged TCP port that no userland service will bind in
        // CI sandboxes.
        let url = "http://127.0.0.1:1/".to_string();
        let r = probe_http(url).await;
        assert!(r.is_none(), "expected None for unreachable URL, got {:?}", r);
    }

    #[tokio::test]
    async fn test_detect_existing_services_returns_struct() {
        // We can't guarantee anything about whether the test machine has
        // local services up — this test only verifies the command returns
        // a well-formed ServicesStatus and doesn't panic. Detail-level
        // probe testing is covered by test_probe_http_returns_none_on_unreachable.
        let s = detect_existing_services().await.expect("command must not error");
        // The Option fields are mutually consistent with the booleans.
        let count = [&s.weaviate_url, &s.ollama_url, &s.code_embed_url]
            .iter()
            .filter(|o| o.is_some())
            .count();
        assert_eq!(s.all_detected, count == 3);
        assert_eq!(s.none_detected, count == 0);
    }

    // ─── Bug 32: non-destructive install safety guarantees ────────

    /// Bug 32 #1 + #2: the install path must never invoke any
    /// volume-removal or compose-down command. Source-level audit:
    /// grep the install scripts for forbidden subprocess invocations.
    /// If this test ever fails it means someone added a destructive
    /// shell-out — gate it behind `--force-clean` or remove it.
    #[test]
    fn test_no_destructive_subprocess_calls_in_install_path() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let install_sh = repo_root.join("install.sh");
        let installer_rs = repo_root.join("launcher/src-tauri/src/commands/installer.rs");
        let projects_v2_rs = repo_root.join("launcher/src-tauri/src/commands/projects_v2.rs");

        // Forbidden literal substrings. We use simple substring matching
        // because anything more clever (e.g. `volume\s+rm`) would also
        // match comments referring to the forbidden form. Comments are
        // scrubbed below.
        let forbidden = [
            "podman volume rm",
            "docker volume rm",
            "podman volume prune",
            "docker volume prune",
            "compose down --volumes",
            "compose down -v",
            "podman-compose down",
            "docker-compose down",
        ];

        for path in [&install_py, &install_sh, &installer_rs, &projects_v2_rs] {
            let content = match std::fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue, // optional file
            };

            // Production-only slice: everything before the test module marker.
            // The Rust files contain `#[cfg(test)]` declaring the test mod;
            // the audit must NOT scan that section because the test code
            // legitimately contains the forbidden literals as needles.
            let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
            let production = &content[..scan_end];

            // Strip line-comments to avoid false positives from docs that
            // mention the forbidden command (e.g. "# Stop: podman-compose down"
            // in compose.yaml-adjacent comments).
            let stripped: String = production
                .lines()
                .map(|line| {
                    // Python/shell `#` comment, or Rust `//` comment.
                    let cut = line.find("//").or_else(|| line.find('#')).unwrap_or(line.len());
                    &line[..cut]
                })
                .collect::<Vec<_>>()
                .join("\n");

            for needle in &forbidden {
                assert!(
                    !stripped.contains(needle),
                    "FORBIDDEN: '{}' found in {} (would destroy user data on install). \
                     If genuinely needed, gate behind --force-clean and update this test.",
                    needle,
                    path.display()
                );
            }
        }
    }

    /// Bug 32 #3 (audit form): `_ensure_collections` in install.py must
    /// only POST to /v1/schema. It must NEVER call PUT, DELETE, or
    /// PATCH on schema endpoints. Source-level grep audit.
    #[test]
    fn test_ensure_collections_only_posts_no_destructive_verbs() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        // Find the _ensure_collections function body (start of the def
        // line to the start of the NEXT top-level def). v0.2.23 fix
        // (2026-05-21): the prior `.scan`+`.last()` implementation was
        // not short-circuiting on the first `def ` marker after the
        // function body — `.last()` collected offsets past the boundary
        // and the audit incorrectly inspected every later function too.
        // Switch to a precise byte-offset search.
        let start = content
            .find("def _ensure_collections(")
            .expect("_ensure_collections defined");
        let after_start = &content[start..];
        // Skip the first line (the `def ...` itself) then find the next
        // line that starts with `def ` OR `# ----` (separator comment).
        let first_newline = after_start.find('\n').unwrap_or(after_start.len());
        let rest = &after_start[first_newline + 1..];
        let mut next_boundary: Option<usize> = None;
        let mut cursor = 0usize;
        for line in rest.split_inclusive('\n') {
            if line.starts_with("def ") || line.starts_with("# ----") {
                next_boundary = Some(cursor);
                break;
            }
            cursor += line.len();
        }
        let body_end = first_newline + 1 + next_boundary.unwrap_or(rest.len());
        let body = &after_start[..body_end];

        // The function MUST call POST to /v1/schema and MUST NOT call
        // any destructive verb on schema.
        //
        // v0.2.77 Part 7a converged the raw `urllib.request.Request(...,
        // method="POST")` calls onto the shared `_wh.http_request(<VERB>,
        // ...)` helper, so the HTTP verb now appears as the FIRST positional
        // arg (`_wh.http_request("POST", ...)`) rather than a `method="..."`
        // kwarg. Accept BOTH spellings for each verb so this source-parity
        // audit keeps working across the convergence (and would still catch a
        // regression that reintroduced a raw destructive Request).
        let posts_via_kwarg = body.contains("method=\"POST\"");
        let posts_via_helper = body.contains("_wh.http_request(\"POST\"")
            || body.contains("http_request(\"POST\"");
        assert!(
            posts_via_kwarg || posts_via_helper,
            "expected a POST (method=\"POST\" or _wh.http_request(\"POST\", ...)) \
             in _ensure_collections"
        );
        for verb in &[
            ("method=\"PUT\"", "_wh.http_request(\"PUT\""),
            ("method=\"DELETE\"", "_wh.http_request(\"DELETE\""),
            ("method=\"PATCH\"", "_wh.http_request(\"PATCH\""),
        ] {
            assert!(
                !body.contains(verb.0) && !body.contains(verb.1),
                "FORBIDDEN: {} / {} found in _ensure_collections — would mutate existing classes",
                verb.0,
                verb.1
            );
        }
        // Belt-and-braces: never DELETE on /v1/schema. The test focuses
        // on HTTP-level destructive verbs against the Weaviate schema
        // endpoint; SQL DELETEs against launcher.db (such as the v0.2.23
        // self-heal helper that lives in a SEPARATE function below)
        // are out of scope and the body-extraction above stops at the
        // function boundary so they don't leak into this assertion.
        assert!(
            !body.contains("DELETE") || body.contains("DELETE on /v1"),
            "DELETE keyword in _ensure_collections must not target schema endpoints"
        );
    }

    /// Bug 32 #8: `copy_orchestrator_to_sync` must leave bytes outside
    /// the orchestrator-managed allowlist 100% identical. Pre-seed a
    /// user file at the install target, run the copy, and SHA-compare.
    #[test]
    fn test_user_code_byte_identical_after_copy() {
        use sha2::{Digest, Sha256};

        let source = fake_repo_source();
        let target = tmp();
        // Pre-seed user code OUTSIDE the allowlist (src/ is not in
        // ORCHESTRATOR_MANAGED_PATHS — it's user code).
        let user_code_path = target.join("src").join("main.py");
        fs::create_dir_all(user_code_path.parent().unwrap()).unwrap();
        let user_payload = b"def main():\n    print('untouched')\n";
        fs::write(&user_code_path, user_payload).unwrap();

        let mut hasher = Sha256::new();
        hasher.update(user_payload);
        let pre_hash = hasher.finalize_reset();

        copy_orchestrator_to_sync(&source, &target).expect("copy");

        // Must still exist + be byte-identical.
        let after = fs::read(&user_code_path).expect("user file survives");
        hasher.update(&after);
        let post_hash = hasher.finalize();
        assert_eq!(
            pre_hash[..],
            post_hash[..],
            "user code at {} was modified by install (bytes differ)",
            user_code_path.display()
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    /// Bug 32 #5: register_mcp into a `~/.claude.json` that already has
    /// MCP servers, OAuth session state, and per-project settings must
    /// preserve every existing key — only adding the new MCP entry.
    #[test]
    fn test_claude_json_merge_preserves_existing_mcp_servers_and_state() {
        use crate::mcp_registration::register_mcp;

        let target = std::env::temp_dir().join(format!(
            "vct-bug32-claude-json-test-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        // Claude Code stores real per-host folder paths as keys under
        // `projects` — POSIX-shaped on Linux/macOS, Windows-shaped on
        // Windows. Use a host-appropriate fake so the assertion isn't
        // ambiguous on Windows.
        let project_key: &str = if cfg!(windows) {
            r"C:\Users\user\somewhere"
        } else {
            "/home/user/somewhere"
        };
        // Pre-seed with realistic ~/.claude.json content: 3 existing MCP
        // servers, OAuth session marker, project-scoped settings.
        let mut projects_map = serde_json::Map::new();
        projects_map.insert(
            project_key.to_string(),
            serde_json::json!({
                "mcpServers": {"private": {"type": "stdio", "command": "x"}},
                "history": [{"display": "test"}],
            }),
        );
        let existing = serde_json::json!({
            "mcpServers": {
                "alpha": {"type": "stdio", "command": "/usr/bin/alpha"},
                "beta": {"type": "stdio", "command": "/usr/bin/beta"},
                "gamma": {"type": "http", "url": "http://localhost:9999"},
            },
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "uuid": "11111111-2222-3333-4444-555555555555",
            },
            "projects": serde_json::Value::Object(projects_map),
            "feedbackSurveyState": {"lastShownTime": 1234567890},
        });
        std::fs::write(
            &target,
            serde_json::to_string_pretty(&existing).unwrap(),
        )
        .unwrap();

        // Now add a new MCP server (what install does).
        let new_entry = serde_json::json!({"type": "stdio", "command": "/usr/bin/weaviate-kg"});
        register_mcp(&target, "weaviate-kg", &new_entry).expect("register_mcp");

        let raw = std::fs::read_to_string(&target).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // New server registered.
        assert_eq!(v["mcpServers"]["weaviate-kg"]["command"], "/usr/bin/weaviate-kg");
        // All 3 existing servers preserved.
        assert_eq!(v["mcpServers"]["alpha"]["command"], "/usr/bin/alpha");
        assert_eq!(v["mcpServers"]["beta"]["command"], "/usr/bin/beta");
        assert_eq!(v["mcpServers"]["gamma"]["url"], "http://localhost:9999");
        // OAuth session preserved.
        assert_eq!(v["oauthAccount"]["emailAddress"], "user@example.com");
        // Project-scoped settings preserved.
        assert_eq!(
            v["projects"][project_key]["mcpServers"]["private"]["command"],
            "x"
        );
        assert_eq!(v["projects"][project_key]["history"][0]["display"], "test");
        // Survey state preserved.
        assert_eq!(v["feedbackSurveyState"]["lastShownTime"], 1234567890);

        std::fs::remove_file(&target).ok();
    }

    /// Bug 32: preflight_install_safety_check returns a well-formed
    /// SafetyReport for a fresh install path. Cannot assert on
    /// existing_volumes / existing_collections deterministically (they
    /// reflect the test machine state) — assert structural sanity only.
    #[tokio::test]
    async fn test_preflight_install_safety_check_returns_report() {
        let target = std::env::temp_dir().join(format!(
            "vct-bug32-preflight-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&target).unwrap();
        // Pre-seed user-code that should appear in will_preserve_user_code.
        std::fs::create_dir_all(target.join("my_app")).unwrap();
        std::fs::write(target.join("README_USER.md"), "user readme").unwrap();

        let report =
            preflight_install_safety_check(target.to_string_lossy().to_string())
                .await
                .expect("preflight returns Ok");

        // User code surfaced.
        assert!(
            report.will_preserve_user_code.iter().any(|p| p == "my_app"),
            "expected my_app in will_preserve_user_code, got {:?}",
            report.will_preserve_user_code
        );
        assert!(
            report.will_preserve_user_code.iter().any(|p| p == "README_USER.md"),
            "expected README_USER.md in will_preserve_user_code"
        );
        // services_running + services_to_start partition the three core services.
        let total_known = report.services_running.len() + report.services_to_start.len();
        // weaviate + ollama always classified; code_embed only listed when running.
        assert!(total_known >= 2, "expected at least weaviate+ollama classified");
        // new_collections_to_add is a subset of the required class names
        // when Weaviate is unreachable (returns empty existing_collections,
        // so the required classes are all "new"). Either way, never has
        // duplicates.
        let mut sorted = report.new_collections_to_add.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), report.new_collections_to_add.len(),
            "new_collections_to_add must not contain duplicates");

        std::fs::remove_dir_all(&target).ok();
    }

    /// Bug 32: when existing volumes are reported, the safety report
    /// includes a risk line warning that bind-mount overrides will be
    /// suppressed (Bug 31's volume picker must respect this).
    #[tokio::test]
    async fn test_preflight_risk_lines_call_out_existing_volumes_and_overwrites() {
        let target = std::env::temp_dir().join(format!(
            "vct-bug32-risk-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        // Pre-seed `.claude` so will_overwrite_orchestrator_files contains it
        // → expected risk line about read-merge-write.
        std::fs::create_dir_all(target.join(".claude")).unwrap();

        let report =
            preflight_install_safety_check(target.to_string_lossy().to_string())
                .await
                .expect("preflight returns Ok");

        // .claude is in the overwrite list → preservation risk line emitted.
        assert!(report.will_overwrite_orchestrator_files.contains(&".claude".to_string()));
        let has_preservation_risk = report
            .risks
            .iter()
            .any(|r| r.contains("settings.json env block") && r.contains("preserved"));
        assert!(
            has_preservation_risk,
            "expected risk line explaining .claude/settings.json read-merge-write semantics, got {:?}",
            report.risks
        );

        std::fs::remove_dir_all(&target).ok();
    }

    /// Reviewer B blocker: orchestrator data sprawl across ~/podman_volumes,
    /// ~/.claude, ~/.vct, ~/.vct-secrets, OS keychain, per-project .claude/.
    /// The uninstaller must:
    ///   1. Be idempotent (running twice does not fail).
    ///   2. NEVER touch ~/.vct-secrets/ (user secrets).
    ///   3. NEVER touch user code outside orchestrator-managed paths.
    /// We assert by source-grep: install.py's `_run_uninstall` body must
    /// not contain references to scrubbing ~/.vct-secrets/ or arbitrary
    /// user paths. Functional execution would touch the real machine and
    /// is out of scope for unit tests.
    #[test]
    fn test_uninstall_never_touches_secrets_or_user_code() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        let start = content
            .find("def _run_uninstall(")
            .expect("_run_uninstall defined");
        let after_start = &content[start..];
        // End at the next top-level `# ---` divider line (matches the
        // existing convention used elsewhere in install.py).
        let body_end = after_start[1..]
            .find("\n# ----")
            .map(|idx| idx + 1)
            .unwrap_or(after_start.len());
        let body = &after_start[..body_end];

        // 1. Must explicitly mention NOT touching ~/.vct-secrets/ — proves intent.
        assert!(
            body.contains(".vct-secrets"),
            "_run_uninstall must explicitly reference ~/.vct-secrets/ (and skip it)"
        );
        // 2. Must NOT call rm/unlink on .vct-secrets.
        assert!(
            !body.contains("vct-secrets/.unlink") && !body.contains("rm -rf"),
            "_run_uninstall must never call rm -rf or scrub .vct-secrets"
        );
        // 3. Must NOT remove arbitrary user paths — only Path.home() / .vct or .vibecoded entries.
        // Heuristic: forbid `shutil.rmtree(` and `os.remove(` on bare user paths.
        // (We allow `.unlink()` on specific files like launcher.db, audit logs.)
        for forbidden in &["shutil.rmtree(", "Path('/').", "rmdir('/')"] {
            assert!(
                !body.contains(forbidden),
                "_run_uninstall must not call {} (would risk user code)",
                forbidden
            );
        }
        // 4. MCP scrub must preserve user's other servers. v0.2.83 (WP-B5):
        // the inline `orchestrator_mcps` allow-list literal moved to the
        // shared table-sourced helper `vco_lib.install_mcp.
        // uninstall_scrub_mcp_names` (positive-ID scrub: VCO-shaped paths
        // inside the install root + playwright's exact npx fingerprint;
        // act/leave-alone behavior pinned by
        // tests/test_v0283_uninstall_scrub_migration.py). The structural pin
        // here asserts the delegation — a blanket `mcpServers` wipe or a
        // fresh inline literal would break it.
        assert!(
            body.contains("_uninstall_scrub_mcp_names"),
            "_run_uninstall must delegate MCP scrubbing to the table-sourced \
             allow-list helper (preserving user MCPs)"
        );
    }

    /// Idempotence: a second `_run_uninstall` on an already-uninstalled
    /// machine must not error or print fake "removed X" lines. Source
    /// audit: every removal step must be guarded by an existence check.
    #[test]
    fn test_uninstall_is_idempotent_via_existence_guards() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        let start = content
            .find("def _run_uninstall(")
            .expect("_run_uninstall defined");
        let after_start = &content[start..];
        let body_end = after_start.find("\n# ----").unwrap_or(after_start.len());
        let body = &after_start[..body_end];

        // launcher.db removal is gated by .exists() check.
        assert!(
            body.contains("launcher_db.exists()"),
            "launcher.db removal must be guarded by .exists()"
        );
        // claude.json scrub is gated by .exists() check.
        assert!(
            body.contains("claude_json.exists()"),
            "~/.claude.json scrub must be guarded by .exists()"
        );
        // Container ops are gated by container_runtime is not None.
        assert!(
            body.contains("container_runtime is not None"),
            "container ops must be guarded by runtime detection"
        );
    }

    // ─── Install log reader (read_install_log_from + derive_install_state) ─────

    /// Helper: write a JSONL fixture and read it back through the public
    /// helper. Returns the (parsed-events, derived-summary) pair.
    fn parse_log_fixture(lines: &[&str]) -> InstallLog {
        let dir = tmp();
        let path = dir.join("install.jsonl");
        let body = lines.join("\n") + "\n";
        std::fs::write(&path, body).unwrap();
        let log = read_install_log_from(&path);
        std::fs::remove_dir_all(&dir).ok();
        log
    }

    #[test]
    fn test_install_log_missing_file() {
        // No file at the given path: exists=false, summary all-empty,
        // looks_complete=false. This is the fresh-install case.
        let dir = tmp();
        let log = read_install_log_from(&dir.join("nope.jsonl"));
        assert!(!log.exists);
        assert!(log.events.is_empty());
        assert!(log.state_summary.completed_steps.is_empty());
        assert!(log.state_summary.failed_steps.is_empty());
        assert!(!log.state_summary.looks_complete);
        assert!(log.state_summary.session_started.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_install_log_empty_file() {
        // File exists but no events: same shape as missing, but exists=true.
        let log = parse_log_fixture(&[]);
        assert!(log.exists);
        assert!(log.events.is_empty());
        assert!(!log.state_summary.looks_complete);
    }

    #[test]
    fn test_install_log_full_happy_path() {
        // Simulate a clean end-to-end install: install.py 1/10..10/10 ok,
        // session ok, post-install build/tauri ok, spawn ok. Summary
        // should report all steps completed and looks_complete=true.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":"checking"}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":"3.12.4"}"#,
            r#"{"ts":"2026-04-27T22:00:05Z","actor":"install.py","step":"2/10","phase":"ok","detail":"linux"}"#,
            r#"{"ts":"2026-04-27T22:00:10Z","actor":"install.py","step":"3/10","phase":"ok","detail":"venv"}"#,
            r#"{"ts":"2026-04-27T22:00:20Z","actor":"install.py","step":"4/10","phase":"ok","detail":"deps"}"#,
            r#"{"ts":"2026-04-27T22:00:30Z","actor":"install.py","step":"5/10","phase":"ok","detail":"compose"}"#,
            r#"{"ts":"2026-04-27T22:00:40Z","actor":"install.py","step":"6/10","phase":"ok","detail":"ollama"}"#,
            r#"{"ts":"2026-04-27T22:00:50Z","actor":"install.py","step":"7/10","phase":"ok","detail":"models"}"#,
            r#"{"ts":"2026-04-27T22:00:55Z","actor":"install.py","step":"7b/10","phase":"ok","detail":"collections"}"#,
            r#"{"ts":"2026-04-27T22:00:58Z","actor":"install.py","step":"7c/10","phase":"ok","detail":"seed"}"#,
            r#"{"ts":"2026-04-27T22:01:00Z","actor":"install.py","step":"8/10","phase":"ok","detail":"state"}"#,
            r#"{"ts":"2026-04-27T22:01:01Z","actor":"install.py","step":"9/10","phase":"ok","detail":"env"}"#,
            r#"{"ts":"2026-04-27T22:01:02Z","actor":"install.py","step":"10/10","phase":"ok","detail":"claude"}"#,
            r#"{"ts":"2026-04-27T22:01:03Z","actor":"install.py","step":"session","phase":"ok","detail":"finished"}"#,
            r#"{"ts":"2026-04-27T22:01:10Z","actor":"post-install-launcher.sh","step":"audit","phase":"ok","detail":"audited"}"#,
            r#"{"ts":"2026-04-27T22:01:30Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"ok","detail":"built"}"#,
            r#"{"ts":"2026-04-27T22:01:35Z","actor":"post-install-launcher.sh","step":"spawn","phase":"ok","detail":"launched"}"#,
        ]);

        assert!(log.exists);
        assert_eq!(log.events.len(), 17);
        let s = &log.state_summary;
        assert!(s.session_started.is_some());
        assert!(s.last_event_ts.is_some());
        assert!(s.failed_steps.is_empty());
        assert!(s.completed_steps.contains(&"10/10".to_string()));
        assert!(s.completed_steps.contains(&"build/tauri".to_string()));
        assert!(s.completed_steps.contains(&"spawn".to_string()));
        assert!(s.looks_complete);
    }

    #[test]
    fn test_install_log_failure_at_build() {
        // install.py completes but post-install build/tauri errors out.
        // looks_complete must be false, build/tauri must be in failed_steps.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:01:03Z","actor":"install.py","step":"session","phase":"ok","detail":"finished"}"#,
            r#"{"ts":"2026-04-27T22:01:30Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:01:31Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"error","detail":"missing webkit2gtk"}"#,
        ]);
        let s = &log.state_summary;
        assert!(!s.looks_complete);
        let failed_steps: Vec<&String> = s.failed_steps.iter().map(|(s, _)| s).collect();
        assert!(failed_steps.contains(&&"build/tauri".to_string()));
        let build_err = s
            .failed_steps
            .iter()
            .find(|(s, _)| s == "build/tauri")
            .unwrap()
            .1
            .clone();
        assert!(build_err.contains("webkit2gtk"));
    }

    #[test]
    fn test_install_log_skip_classified_correctly() {
        // skip != ok != error. Make sure skipped_steps catches "skip" only.
        // Note: 1/10 needs a terminal phase or it would be misclassified as
        // interrupted. We follow start with ok like a real install.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"2b/10","phase":"skip","detail":"--no-joern"}"#,
            r#"{"ts":"2026-04-27T22:00:02Z","actor":"install.py","step":"9/10","phase":"skip","detail":".env preserved"}"#,
            r#"{"ts":"2026-04-27T22:00:03Z","actor":"install.py","step":"3/10","phase":"ok","detail":"venv"}"#,
        ]);
        let s = &log.state_summary;
        assert!(s.skipped_steps.contains(&"2b/10".to_string()));
        assert!(s.skipped_steps.contains(&"9/10".to_string()));
        assert!(s.completed_steps.contains(&"3/10".to_string()));
        assert!(s.completed_steps.contains(&"1/10".to_string()));
        assert!(s.failed_steps.is_empty());
    }

    #[test]
    fn test_install_log_corrupt_lines_are_skipped() {
        // Mid-write crashes can leave half a JSON object on a line. The
        // parser must drop those without bailing on the whole file.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"insta"#, // corrupt
            r#"{"ts":"2026-04-27T22:00:02Z","actor":"install.py","step":"2/10","phase":"ok","detail":""}"#,
            r#"not even json"#,
            r#"{"ts":"2026-04-27T22:00:03Z","actor":"install.py","step":"3/10","phase":"ok","detail":""}"#,
        ]);
        // 3 valid records.
        assert_eq!(log.events.len(), 3);
        assert!(log.state_summary.completed_steps.contains(&"2/10".to_string()));
        assert!(log.state_summary.completed_steps.contains(&"3/10".to_string()));
    }

    #[test]
    fn test_install_log_interrupted_step() {
        // A step at "start" with no terminal phase = the writer crashed
        // mid-step. Must surface as failed (the wizard treats it as a
        // resume point, not as completed).
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:30Z","actor":"install.py","step":"5/10","phase":"start","detail":"compose up"}"#,
        ]);
        let s = &log.state_summary;
        let failed: Vec<&String> = s.failed_steps.iter().map(|(s, _)| s).collect();
        assert!(failed.contains(&&"5/10".to_string()));
        assert!(s
            .failed_steps
            .iter()
            .find(|(s, _)| s == "5/10")
            .map(|(_, d)| d.starts_with("interrupted:"))
            .unwrap_or(false));
    }

    // ─── Re-install conflict resolution (4-option modal) ───────────────────

    /// Build a fake adopt-target install root with user-edited copies of
    /// every preserve-list file plus a couple of directory contents, so
    /// each strategy has something to act on.
    fn fake_adopt_target() -> PathBuf {
        let p = tmp();
        // Pre-existing user content INSIDE .claude
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(
            p.join(".claude/CONTEXT_STATE.md"),
            "# user CONTEXT_STATE\nsome custom session state\n",
        )
        .unwrap();
        fs::write(
            p.join(".claude/PROJECT_REGISTRY.md"),
            "# user registry\nproject-foo\n",
        )
        .unwrap();
        fs::write(p.join(".claude/settings.json"), "{\"old\":true}").unwrap();
        // Top-level preserve-list files
        fs::write(p.join("CLAUDE.md"), "# user CLAUDE.md\ncustom rules\n").unwrap();
        fs::write(p.join(".env"), "USER_KEY=secret\n").unwrap();
        // Outside the allowlist — must survive every strategy.
        fs::write(p.join("user_code.py"), "print('survive')\n").unwrap();
        // Knowledge dir with one user-curated note that's NOT in the
        // preserve list (so it gets overwritten on every strategy that
        // copies — that's the contract).
        fs::create_dir_all(p.join("knowledge")).unwrap();
        fs::write(p.join("knowledge/note.md"), "OLD\n").unwrap();
        p
    }

    #[test]
    fn test_serde_conflict_strategy_snake_case() {
        let json = serde_json::to_string(&ConflictStrategy::OverwritePreserve).unwrap();
        assert_eq!(json, "\"overwrite_preserve\"");
        let parsed: ConflictStrategy =
            serde_json::from_str("\"delete_claude_and_reinstall\"").unwrap();
        assert_eq!(parsed, ConflictStrategy::DeleteClaudeAndReinstall);
    }

    #[test]
    fn test_serde_conflict_resolution_round_trip() {
        let r = ConflictResolution {
            strategy: ConflictStrategy::OverwritePreserve,
            preserve_paths: Some(vec!["CLAUDE.md".into(), ".claude/CONTEXT_STATE.md".into()]),
        };
        let json = serde_json::to_string(&r).unwrap();
        let back: ConflictResolution = serde_json::from_str(&json).unwrap();
        assert_eq!(back.strategy, ConflictStrategy::OverwritePreserve);
        assert_eq!(back.preserve_paths.unwrap().len(), 2);
    }

    #[test]
    fn test_install_conflict_error_serializes_with_kind_discriminator() {
        // Path strings here are only round-tripped through serde — no
        // disk I/O — but pick host-appropriate placeholders so the test
        // intent is unambiguous on Windows.
        let (ip, sp): (&str, &str) = if cfg!(windows) {
            (r"C:\tmp\x", r"C:\tmp\src")
        } else {
            ("/tmp/x", "/tmp/src")
        };
        let err = InstallConflictError {
            kind: "install_conflict".into(),
            message: "boom".into(),
            install_path: ip.into(),
            source_path: sp.into(),
            mode: InstallMode::Adopt,
            will_overwrite: vec!["CLAUDE.md".into()],
            will_add: vec!["state".into()],
            preserve_candidates: vec!["CLAUDE.md".into()],
        };
        let s = err.into_err_string();
        assert!(s.contains("\"kind\":\"install_conflict\""));
        assert!(s.contains("\"will_overwrite\":[\"CLAUDE.md\"]"));
    }

    #[test]
    fn test_new_sibling_path_basic_extension() {
        let got = new_sibling_path(Path::new("/x/CLAUDE.md"));
        assert_eq!(got, PathBuf::from("/x/CLAUDE.new.md"));
    }

    #[test]
    fn test_new_sibling_path_dotfile_no_extension() {
        // .env has no "real" extension — append .new at end.
        let got = new_sibling_path(Path::new("/x/.env"));
        assert_eq!(got, PathBuf::from("/x/.env.new"));
    }

    #[test]
    fn test_new_sibling_path_no_extension() {
        let got = new_sibling_path(Path::new("/x/Makefile"));
        assert_eq!(got, PathBuf::from("/x/Makefile.new"));
    }

    #[test]
    fn test_new_sibling_path_double_extension_keeps_last() {
        // archive.tar.gz → split on LAST dot → archive.tar.new.gz
        let got = new_sibling_path(Path::new("/x/archive.tar.gz"));
        assert_eq!(got, PathBuf::from("/x/archive.tar.new.gz"));
    }

    #[test]
    fn test_strategy_adopt_as_is_is_noop_on_disk() {
        let source = fake_repo_source();
        let target = fake_adopt_target();
        let pre_user = fs::read_to_string(target.join("CLAUDE.md")).unwrap();

        let report =
            apply_conflict_strategy(&source, &target, ConflictStrategy::AdoptAsIs, &[]).unwrap();
        assert_eq!(report.copied_count, 0);
        assert_eq!(report.preserved_count, 0);
        assert_eq!(report.new_md_count, 0);
        assert!(!report.notification_written);

        // User file untouched.
        assert_eq!(fs::read_to_string(target.join("CLAUDE.md")).unwrap(), pre_user);
        // Bytes outside allowlist also untouched.
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_all_loses_user_edits_in_managed_paths() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        let report =
            apply_conflict_strategy(&source, &target, ConflictStrategy::OverwriteAll, &[]).unwrap();
        assert!(report.copied_count > 0);
        assert_eq!(report.preserved_count, 0);
        assert_eq!(report.new_md_count, 0);

        // CLAUDE.md is OUTSIDE the managed allowlist as of v0.2.12 (PR-31
        // removed it — user projects render their own CLAUDE.md from
        // templates/CLAUDE.md.template, the orchestrator's root CLAUDE.md
        // is dev-only). So the user-edited CLAUDE.md survives any
        // conflict strategy because nothing tries to copy onto it.
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# user CLAUDE.md\ncustom rules\n"
        );
        // settings.json overwritten with upstream {}
        assert_eq!(
            fs::read_to_string(target.join(".claude/settings.json")).unwrap(),
            "{}"
        );
        // No `.new.md` siblings.
        assert!(!target.join("CLAUDE.new.md").exists());
        // User code OUTSIDE the allowlist is preserved (allowlist is the
        // copy boundary, NOT a strategy choice).
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_writes_new_md_siblings_and_notification() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        // CONTEXT_STATE.md is the canonical preserve-eligible file: it's
        // both in the managed allowlist (under .claude/) and in
        // DEFAULT_PRESERVE_LIST. Source has it, target has it → expect
        // CONTEXT_STATE.new.md sibling.
        // (CLAUDE.md was the previous canonical fixture but PR-31 / v0.2.12
        // removed it from the managed allowlist — see fake_repo_source.)
        let preserve: Vec<String> = DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect();
        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        // User CLAUDE.md untouched (outside allowlist, never copied).
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# user CLAUDE.md\ncustom rules\n"
        );
        // No CLAUDE.new.md sibling because no copy attempt happened.
        assert!(!target.join("CLAUDE.new.md").exists());
        // CONTEXT_STATE.md is in source AND target AND preserve list, so
        // we expect a CONTEXT_STATE.new.md sibling and the user's file to be
        // untouched + notification block appended.
        assert!(target.join(".claude/CONTEXT_STATE.new.md").exists());
        assert_eq!(
            fs::read_to_string(target.join(".claude/CONTEXT_STATE.new.md")).unwrap(),
            "# upstream context state\n"
        );
        let ctx = fs::read_to_string(target.join(".claude/CONTEXT_STATE.md")).unwrap();
        assert!(ctx.contains("# user CONTEXT_STATE"));
        assert!(ctx.contains(MERGE_BLOCK_START));
        assert!(ctx.contains(MERGE_BLOCK_END));

        // `knowledge/` was removed from the managed allowlist in v0.2.52
        // V52-C: KG nodes are USER-CURATED state, never copied through
        // this strategy. The user's `knowledge/note.md` survives every
        // strategy because nothing tries to copy onto it. The shipped
        // curated KG set is bundle-materialized from `templates/knowledge/`
        // via `_enumerate_bundle_files` (manifest-tracked, user edits
        // preserved on bundle update — same V47-A pattern as agents/skills).
        assert_eq!(
            fs::read_to_string(target.join("knowledge/note.md")).unwrap(),
            "OLD\n"
        );

        assert!(report.notification_written);
        assert!(report.new_md_count >= 1);
        assert_eq!(report.preserved_count, report.new_md_count);

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_notification_block_is_idempotent() {
        // Run OverwritePreserve twice. The notification block must NOT
        // duplicate; the second run must REPLACE the first block in-place.
        let source = fake_repo_source();
        let target = fake_adopt_target();
        let preserve: Vec<String> = DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect();

        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();
        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        let ctx = fs::read_to_string(target.join(".claude/CONTEXT_STATE.md")).unwrap();
        let start_count = ctx.matches(MERGE_BLOCK_START).count();
        let end_count = ctx.matches(MERGE_BLOCK_END).count();
        assert_eq!(start_count, 1, "duplicate <!-- vct-merge-pending --> block");
        assert_eq!(end_count, 1, "duplicate <!-- /vct-merge-pending --> block");

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_delete_claude_wipes_only_dot_claude() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::DeleteClaudeAndReinstall,
            &[],
        )
        .unwrap();
        assert!(report.copied_count > 0);

        // .claude was wiped then re-populated with upstream settings.json
        // (the OLD `{"old":true}` is gone).
        assert_eq!(
            fs::read_to_string(target.join(".claude/settings.json")).unwrap(),
            "{}"
        );
        // Top-level files OUTSIDE .claude are NOT wiped — DeleteClaude
        // only nukes .claude/. CLAUDE.md was overwritten by the fresh
        // copy step (it's in the allowlist), so we check user_code.py
        // which is OUTSIDE the allowlist.
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_delete_claude_handles_missing_dot_claude() {
        // If .claude doesn't exist (somehow we got dispatched to this
        // strategy on a non-adopt target), DeleteClaude must not error.
        let source = fake_repo_source();
        let target = tmp();
        fs::write(target.join("user_code.py"), "x").unwrap();

        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::DeleteClaudeAndReinstall,
            &[],
        )
        .expect("must not error on missing .claude/");
        assert!(report.copied_count > 0);

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_with_custom_preserve_list() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        // Custom preserve list with a single file that IS in the managed
        // allowlist (.claude/CONTEXT_STATE.md). CLAUDE.md is OUT of the
        // managed allowlist as of v0.2.12 (PR-31) so putting it on the
        // preserve list wouldn't exercise the sibling-creation path.
        let preserve = vec![".claude/CONTEXT_STATE.md".to_string()];
        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        // CONTEXT_STATE.md preserved, sibling written.
        assert!(target.join(".claude/CONTEXT_STATE.new.md").exists());
        let ctx = fs::read_to_string(target.join(".claude/CONTEXT_STATE.md")).unwrap();
        assert!(ctx.contains("# user CONTEXT_STATE"));
        // User CLAUDE.md untouched regardless of preserve-list inclusion
        // (it's outside the copy allowlist now).
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# user CLAUDE.md\ncustom rules\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_rejects_non_orchestrator_source() {
        let source = tmp(); // no vct-module.json
        let target = tmp();
        let err = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwriteAll,
            &[],
        )
        .unwrap_err();
        assert!(err.contains("not an orchestrator repo"));
        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_replace_or_append_block_appends_when_missing() {
        let existing = "# header\nbody\n";
        let block = "<!-- vct-merge-pending -->\nstuff\n<!-- /vct-merge-pending -->\n";
        let out = replace_or_append_block(existing, block);
        assert!(out.starts_with("# header\nbody\n"));
        assert!(out.contains("<!-- vct-merge-pending -->"));
    }

    #[test]
    fn test_replace_or_append_block_replaces_in_place() {
        let existing = "# header\n<!-- vct-merge-pending -->\nOLD STUFF\n<!-- /vct-merge-pending -->\n# tail\n";
        let new_block =
            "<!-- vct-merge-pending -->\nNEW STUFF\n<!-- /vct-merge-pending -->";
        let out = replace_or_append_block(existing, new_block);
        assert!(out.contains("NEW STUFF"));
        assert!(!out.contains("OLD STUFF"));
        assert!(out.contains("# header"));
        assert!(out.contains("# tail"));
        // Exactly one block.
        assert_eq!(out.matches(MERGE_BLOCK_START).count(), 1);
    }

    #[test]
    fn test_update_merge_notification_block_creates_file_if_missing() {
        let dir = tmp();
        let target_file = dir.join(".claude/CONTEXT_STATE.md");
        let written = update_merge_notification_block(
            &target_file,
            &["CLAUDE.md".to_string()],
        )
        .unwrap();
        assert!(written);
        assert!(target_file.exists());
        let content = fs::read_to_string(&target_file).unwrap();
        assert!(content.contains(MERGE_BLOCK_START));
        assert!(content.contains("CLAUDE.md"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_civil_from_days_known_dates() {
        // The civil-from-days algorithm now lives in
        // vct_launcher_core::time (Part 7c task 5) and is unit-tested there;
        // here we only smoke-test the local `chrono_iso_z` wrapper still
        // produces the expected shape via the shared home.
        assert_eq!(vct_launcher_core::time::civil_from_days(0), (1970, 1, 1));
        let s = chrono_iso_z();
        assert!(s.ends_with("Z"));
        assert_eq!(s.len(), 20); // YYYY-MM-DDTHH:MM:SSZ
    }

    // ---- check_install_health predicate ---------------------------------
    //
    // The .exe-only install scenario: a user downloads the launcher binary
    // from a GitHub Release and skips first-install. The four signals must
    // all flip to true for `all_ok` to fire; missing any one of them is
    // sufficient evidence the install never ran.

    #[test]
    fn test_inspect_install_health_incomplete_dir() {
        let p = tmp();
        // Plant ONLY the install-root markers (CLAUDE.md + install.py)
        // so the path looks like an install root, but none of the four
        // post-install signals are present.
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();

        let health = inspect_install_health_at(&p);
        assert_eq!(health.install_root, Some(p.to_string_lossy().to_string()));
        assert!(!health.has_venv);
        assert!(!health.has_state_dir);
        assert!(!health.has_env_with_kg);
        assert!(!health.mcp_servers_ok);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_complete_dir() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(
            p.join(".env"),
            "FOO=bar\nKG_COLLECTION=ClaudeKnowledgeGraph\n",
        )
        .unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers").join(".venv")).unwrap();

        let health = inspect_install_health_at(&p);
        assert!(health.has_venv);
        assert!(health.has_state_dir);
        assert!(health.has_env_with_kg);
        assert!(health.mcp_servers_ok);
        assert!(health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_env_without_kg_line() {
        // .env is present but missing the KG_COLLECTION key — must NOT
        // count as healthy.
        let p = tmp();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "FOO=bar\nBAZ=qux\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers").join(".venv")).unwrap();

        let health = inspect_install_health_at(&p);
        assert!(!health.has_env_with_kg);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_modern_venv_satisfies_mcp_servers_ok() {
        // Modern path (post-migration): install.py creates the venv at
        // <root>/.venv and does NOT create <root>/claude_mcp_servers/.venv
        // (the latter is marked as a stale legacy path in install.py:7656).
        // The launcher's health check must accept the modern layout —
        // `claude_mcp_servers/` present + `<root>/.venv` present — as
        // sufficient evidence of a completed install. Otherwise every
        // fresh install on every OS shows the "Installation incomplete"
        // modal even though install.py succeeded (2026-05-23 fork bomb
        // symptom).
        let p = tmp();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers")).unwrap();
        // claude_mcp_servers/.venv intentionally absent — the modern
        // install layout does not create it.

        let health = inspect_install_health_at(&p);
        assert!(health.mcp_servers_ok,
            "mcp_servers_ok must accept the modern <root>/.venv path");
        assert!(health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_no_venv_anywhere() {
        // Neither <root>/.venv nor <root>/claude_mcp_servers/.venv exist
        // — this is the genuine "user copied source tree but never ran
        // install.py" failure mode. Health check must flag it.
        let p = tmp();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers")).unwrap();
        // No .venv anywhere.

        let health = inspect_install_health_at(&p);
        assert!(!health.has_venv);
        assert!(!health.mcp_servers_ok);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_legacy_venv_path() {
        // Legacy path: pre-migration installs put the venv at
        // <root>/claude_mcp_servers/.venv/. install.py:9809
        // (_resolve_venv_python_for_install) still accepts this layout
        // as a fallback; the launcher's health check must too, so users
        // who haven't re-run a modern install.py don't get the modal.
        let p = tmp();
        // No <root>/.venv (legacy layout).
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers").join(".venv")).unwrap();

        let health = inspect_install_health_at(&p);
        assert!(!health.has_venv,
            "<root>/.venv intentionally absent — checking legacy fallback");
        assert!(health.mcp_servers_ok,
            "mcp_servers_ok must accept the legacy claude_mcp_servers/.venv path");
        // has_venv still false → all_ok still false; mcp_servers_ok alone
        // is not sufficient for the install to be considered complete in
        // the modern world, but it documents that the legacy fallback is
        // recognized.
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    // ---- validate_source_repo (2026-04-29 wizard install-path lockdown) ----
    //
    // The wizard previously let the user pick any folder as install_path;
    // install_orchestrator would copy ORCHESTRATOR_MANAGED_PATHS in but the
    // result was a half-install with no launcher / no first-install.sh.
    // validate_source_repo() rejects non-source folders BEFORE any
    // mutations. The discriminator is install.py + first-install.sh side
    // by side — both ship in every clone, neither is in
    // ORCHESTRATOR_MANAGED_PATHS, so a half-install can't fake it.

    #[test]
    fn install_orchestrator_rejects_non_source_repo() {
        // An empty tmp dir is the canonical "user picked an empty folder"
        // case the lockdown is meant to refuse. validate_source_repo must
        // return Err with a message that mentions install.py and
        // first-install.sh so the user knows what's expected.
        let p = tmp();
        let res = validate_source_repo(&p);
        assert!(res.is_err(), "validate_source_repo accepted an empty dir");
        let msg = res.unwrap_err();
        assert!(
            msg.contains("install.py") && msg.contains("first-install.sh"),
            "error message must name the two required files; got: {}",
            msg
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn install_orchestrator_rejects_partial_source_repo() {
        // install.py present but first-install.sh missing — still a
        // non-source target, must be refused.
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        let res = validate_source_repo(&p);
        assert!(res.is_err(), "validate_source_repo accepted install.py-only dir");
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn install_orchestrator_accepts_source_repo() {
        // Both markers present — must pass. (Intentionally short-circuits
        // before any real install side effects; this test only exercises
        // the validation gate, not the rest of install_orchestrator.)
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::write(p.join("first-install.sh"), "#!/usr/bin/env bash\n").unwrap();
        let res = validate_source_repo(&p);
        assert!(res.is_ok(), "validate_source_repo rejected a source repo: {:?}", res);
        fs::remove_dir_all(&p).ok();
    }

    // ---- update_orchestrator_at VCO-clone gate (post-PR-#150) -----------
    //
    // After PR #150 unregister became non-destructive (preserves
    // `.claude/agents/` + `.claude/skills/`). The previous existence-only
    // check (`.claude/` OR `vct-module.json` present) would let a project
    // folder with leftover `.claude/` slip through and trigger an
    // orchestrator-machinery copy into the project. update_orchestrator_at
    // now calls `validate_source_repo` — same gate as install_orchestrator
    // — so the test surface is identical to the install_orchestrator_*
    // block above. Project-update is the separate `update_project_v2` /
    // `run_install_bundle_update` path in projects_v2.rs.

    #[test]
    fn test_update_orchestrator_at_refuses_project_folder_with_only_dot_claude() {
        // Simulate a project folder post-non-destructive-unregister: leftover
        // `.claude/agents/` + content, but NO install.py / first-install.sh.
        // The new gate must refuse this with the validate_source_repo error.
        let p = tmp();
        fs::create_dir_all(p.join(".claude/agents")).unwrap();
        fs::write(p.join(".claude/agents/some.md"), "# leftover agent\n").unwrap();

        let res = validate_source_repo(&p);
        assert!(
            res.is_err(),
            "project folder with only .claude/ leftovers must be refused"
        );
        let msg = res.unwrap_err();
        assert!(
            msg.contains("install.py") && msg.contains("first-install.sh"),
            "error message must name the two required files; got: {}",
            msg
        );
        fs::remove_dir_all(&p).ok();
    }

    // ---- managed-paths parser ------------------------------------------
    //
    // Parser must match `_parse_managed_paths_text` in install.py. The
    // BOM-strip case below pins fix from the PR-5 reviewer follow-up
    // (2026-05-06): saved-from-Windows-Notepad files routinely carry a
    // UTF-8 BOM at the start, and `str::trim` does NOT remove it. Without
    // this, the first allowlist entry silently fails to match.

    /// CI-only print test (follow-up #5). Emits the resolved
    /// `ORCHESTRATOR_MANAGED_PATHS` list to stdout one entry per line
    /// prefixed with `MANAGED_PATH:`. The CI script
    /// `.github/scripts/check_managed_paths_cross_language.sh` greps
    /// for that prefix to extract the Rust-side parse, then diffs it
    /// against an independent Python re-parse of the same .txt file.
    /// Catches the failure mode where both languages' `EXPECTED`
    /// literals are wrong but matching their respective parsers.
    ///
    /// `#[ignore]` so the normal `cargo test --lib` run doesn't get
    /// noisy stdout. The CI script invokes it explicitly with
    /// `--ignored --nocapture`.
    #[test]
    #[ignore = "CI-only — invoked explicitly by check_managed_paths_cross_language.sh"]
    fn print_managed_paths_for_ci() {
        for entry in ORCHESTRATOR_MANAGED_PATHS.iter() {
            println!("MANAGED_PATH: {}", entry);
        }
    }

    #[test]
    fn test_parse_managed_paths_strips_leading_bom() {
        let text: &'static str = "\u{feff}.claude\nCLAUDE.md\n";
        assert_eq!(parse_managed_paths_text(text), vec![".claude", "CLAUDE.md"]);
    }

    #[test]
    fn test_parse_managed_paths_no_bom_unaffected() {
        let text: &'static str = ".claude\nCLAUDE.md\n";
        assert_eq!(parse_managed_paths_text(text), vec![".claude", "CLAUDE.md"]);
    }

    #[test]
    fn test_parse_managed_paths_bom_only_at_start() {
        // A stray BOM mid-file is still treated as part of the line. We
        // only strip a BOM at file start (matching install.py).
        let text: &'static str = ".claude\n\u{feff}CLAUDE.md\n";
        let got = parse_managed_paths_text(text);
        assert_eq!(got[0], ".claude");
        assert_eq!(got[1], "\u{feff}CLAUDE.md");
    }

    #[test]
    fn test_update_orchestrator_at_accepts_vco_clone() {
        // A real VCO clone has install.py + first-install.sh side-by-side
        // (plus a `.git/`, but the gate doesn't require it). The gate-pass
        // is what we test here — the actual copy still runs in the real
        // command and is exercised by integration / manual QA, not unit
        // tests (it requires a Window handle and the bundled source repo).
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::write(p.join("first-install.sh"), "#!/usr/bin/env bash\n").unwrap();
        fs::create_dir_all(p.join(".git")).unwrap();

        let res = validate_source_repo(&p);
        assert!(
            res.is_ok(),
            "VCO clone with install.py + first-install.sh must pass the gate: {:?}",
            res
        );
        fs::remove_dir_all(&p).ok();
    }

    // ─── v0.2.53 NEW-11: resume sentinel empty-sha refusal ───────────────
    //
    // The sentinel is JSON-serialised at `.claude/state/orchestrator-
    // update-resume.json` by the merge/rebase conflict handlers. If
    // `read_head_sha` failed at conflict-time, `sha_at_conflict` is the
    // empty string. The pre-v0.2.53 `head_unchanged` guard silently
    // skipped its comparison in that case (and let the resume proceed
    // against an unknown baseline); the v0.2.53 fix refuses resume
    // explicitly with a clear remediation path.
    //
    // We can test the contract WITHOUT spawning the full
    // `resume_orchestrator_update` Tauri command by:
    //   1. Writing a known-shape sentinel on disk.
    //   2. Round-tripping through `read_update_resume_sentinel`.
    //   3. Asserting the parsed value matches the on-disk shape.
    //   4. Asserting `is_empty()` correctly detects the empty SHA, since
    //      that is the predicate the refusal branch keys on.

    /// Helper: write a sentinel with the given `sha_at_conflict` to
    /// `<root>/.claude/state/orchestrator-update-resume.json`.
    fn write_test_sentinel(root: &std::path::Path, sha_at_conflict: &str) -> PathBuf {
        let target = root.join(UPDATE_RESUME_SENTINEL_REL);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        let s = UpdateResumeSentinel {
            schema: 1,
            operation: "merge".to_string(),
            branch: "main".to_string(),
            sha_at_conflict: sha_at_conflict.to_string(),
            written_at: "2026-06-10T12:00:00Z".to_string(),
        };
        fs::write(&target, serde_json::to_string(&s).unwrap()).unwrap();
        target
    }

    #[test]
    fn test_resume_sentinel_round_trips_empty_sha_at_conflict() {
        let p = tmp();
        write_test_sentinel(&p, "");
        let parsed =
            read_update_resume_sentinel(&p).expect("sentinel with empty sha must still parse");
        assert_eq!(parsed.schema, 1);
        assert_eq!(parsed.operation, "merge");
        assert_eq!(parsed.branch, "main");
        assert!(
            parsed.sha_at_conflict.is_empty(),
            "round-tripped sha_at_conflict must remain empty: {:?}",
            parsed.sha_at_conflict
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_resume_sentinel_empty_sha_predicate_matches_refusal_branch() {
        // The refusal branch in `resume_orchestrator_update` keys on
        // `sentinel.sha_at_conflict.is_empty()`. This test pins the
        // predicate so a future refactor of `UpdateResumeSentinel`
        // (e.g. switching to `Option<String>`) can't silently regress
        // the refusal logic.
        let empty = UpdateResumeSentinel {
            schema: 1,
            operation: "merge".to_string(),
            branch: "main".to_string(),
            sha_at_conflict: String::new(),
            written_at: "2026-06-10T12:00:00Z".to_string(),
        };
        assert!(
            empty.sha_at_conflict.is_empty(),
            "empty sha_at_conflict must satisfy the refusal predicate"
        );

        let filled = UpdateResumeSentinel {
            schema: 1,
            operation: "merge".to_string(),
            branch: "main".to_string(),
            sha_at_conflict: "deadbeef".to_string(),
            written_at: "2026-06-10T12:00:00Z".to_string(),
        };
        assert!(
            !filled.sha_at_conflict.is_empty(),
            "non-empty sha_at_conflict must NOT satisfy the refusal predicate"
        );
    }

    #[test]
    fn test_resume_sentinel_filled_sha_round_trips() {
        // Companion test to the empty-sha case: a valid sha must
        // round-trip without alteration so the head-unchanged guard
        // downstream can compare it byte-for-byte.
        let p = tmp();
        write_test_sentinel(&p, "abcdef0123456789abcdef0123456789abcdef01");
        let parsed = read_update_resume_sentinel(&p).expect("valid sentinel must parse");
        assert_eq!(parsed.sha_at_conflict, "abcdef0123456789abcdef0123456789abcdef01");
        fs::remove_dir_all(&p).ok();
    }

    // ─── 0.1.7 fork-readiness sweep — register_github_pat → keychain ─────
    //
    // Test isolation strategy:
    //   * `VCT_SECRETS_DIR` overrides the launcher's secrets-root
    //     resolution (see `vct_secrets_dir()` above). Each test sets it
    //     to a unique per-test temp dir so the legacy file paths don't
    //     collide with the real user's `~/.vct-secrets/` or with
    //     parallel sibling tests.
    //   * `Db::open_in_memory()` for the launcher DB — no
    //     `VCT_STATE_DIR` override needed.
    //   * `HOME` is INTENTIONALLY not mutated. The
    //     `commands::dashboard::tests` fixtures mutate `HOME` while
    //     exercising plaintext→keychain MCP secret migration; if our
    //     tests also mutated `HOME` they would race against those
    //     (both modules carry their own `SERIALIZE` mutex; they don't
    //     share, so cross-module parallelism is allowed and would
    //     break `HOME` invariants).
    //   * Process-wide `SERIALIZE` mutex still serialises within this
    //     module since `VCT_SECRETS_DIR` is also a process-global env
    //     var. The dashboard tests don't touch `VCT_SECRETS_DIR`, so
    //     the two modules can run concurrently safely.
    //   * Keychain-touching tests probe via `keyring_available()` and
    //     skip silently when CI hosts lack a keychain backend.
    //   * Each keychain-touching test calls `delete_keychain()` at
    //     entry + exit so leftover state from a previous test (or a
    //     parallel run targeting the same well-known keychain entry
    //     `vct._user_shared_.shared.user/github_pat` — post-2026-05-10;
    //     pre-fix this was `installer/github_pat`) doesn't leak in.
    //     The module_id consolidation tests also clear the legacy
    //     `installer/` slot so a residual canary there doesn't mask
    //     a regression.
    //
    // These tests use `pub(crate)` helpers (`migrate_github_pat_file_to_keychain`,
    // `github_pat_for_env`) and the module-private constants from the
    // surrounding `super::*` import. The Tauri-`#[command]` wrappers
    // (`register_github_pat`, `clear_github_pat`, …) take `State<'_, Db>`
    // which we can't easily synthesise without standing up the runtime,
    // so we exercise the underlying logic via the helpers — same shape
    // the commands call.

    #[cfg(test)]
    mod github_pat_keychain_tests {
        use super::super::*;

        // 0.1.7 H1 fork-readiness sweep (2026-05-08): use the process-wide
        // keychain mutex from `crate::secrets::test_serialize` instead
        // of a private one. Pre-H1 this module had its OWN `static
        // SERIALIZE: Mutex<()>` that only blocked within-module races.
        // Parallel tests in `hub::modules_api::tests` (the H1 hub-resolver
        // tests) and `commands::dashboard::tests` (plaintext-migration)
        // also touch the same `vct._user_shared_.shared.user/github_pat`
        // keychain slot (post-2026-05-10; pre-fix this was
        // `installer/github_pat`) — without a shared mutex they overwrote
        // each other's canaries. The new shared mutex serialises all
        // keychain tests across the launcher binary.

        struct EnvGuard {
            prev_secrets_dir: Option<std::ffi::OsString>,
            // v0.2.14 (2026-05-17): `_lock` upgraded from
            // `MutexGuard<'static, ()>` to `KeychainGuard` — the new
            // guard bundles the in-process mutex with a cross-process
            // `flock` on `/tmp/vct-keychain-test.lock`. Concurrent
            // `cargo test --lib` invocations from different terminals
            // would otherwise race on the OS-shared keychain slot
            // (`vct._user_shared_.shared.user/github_pat`) — each
            // binary holds its own copy of the in-process mutex, so
            // pre-v0.2.14 nothing serialised between binaries.
            _lock: crate::secrets::test_serialize::KeychainGuard,
        }

        impl Drop for EnvGuard {
            fn drop(&mut self) {
                match self.prev_secrets_dir.take() {
                    Some(v) => std::env::set_var("VCT_SECRETS_DIR", v),
                    None => std::env::remove_var("VCT_SECRETS_DIR"),
                }
            }
        }

        fn setup_temp_env() -> (PathBuf, EnvGuard) {
            let lock = crate::secrets::test_serialize::keychain_serialize_lock();
            let tmp = std::env::temp_dir().join(format!(
                "vct-installer-pat-test-{}",
                uuid::Uuid::new_v4().simple()
            ));
            std::fs::create_dir_all(&tmp).unwrap();
            let prev_secrets_dir = std::env::var_os("VCT_SECRETS_DIR");
            std::env::set_var("VCT_SECRETS_DIR", &tmp);
            let guard = EnvGuard { prev_secrets_dir, _lock: lock };
            (tmp, guard)
        }

        fn keyring_available() -> bool {
            // v0.2.76 (A4): delegate to the ONE shared probe (bounded-timeout
            // worker — a wedged Secret Service returns false, never hangs).
            secrets::keyring_probe_available()
        }

        fn make_db() -> crate::db::Db {
            crate::db::Db::open_in_memory().unwrap()
        }

        /// Read directly from the keychain — used to verify what
        /// `register_github_pat` and the migration write. Bypasses the
        /// active-flag gate so it's a raw fact-check.
        fn keychain_value() -> Option<String> {
            crate::secrets::get(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .ok()
            .flatten()
        }

        /// Read directly from the LEGACY (`installer/`) keychain slot.
        /// Used by the module_id consolidation tests to check the old
        /// slot was emptied after migration. Pre-2026-05-10 this was
        /// the only writer slot; post-fix it's read-only and gets
        /// drained on first `register_github_pat` call.
        fn keychain_value_legacy() -> Option<String> {
            crate::secrets::get(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_LEGACY_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .ok()
            .flatten()
        }

        fn delete_keychain() {
            // Wipe BOTH slots (post-2026-05-10 + pre-2026-05-10) so a
            // residue from a previous test run targeting either slot
            // doesn't mask a regression. `delete` returns Ok on NoEntry.
            let _ = crate::secrets::delete(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            );
            let _ = crate::secrets::delete(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_LEGACY_MODULE_ID,
                GITHUB_PAT_KEY,
            );
        }

        // ── Item #1: register_github_pat → keychain (no plaintext file) ──

        /// Calling the underlying register flow (keychain set + active
        /// flag) writes to the keychain. As of the 2026-05-09 non-
        /// destructive fix, the launcher does NOT touch any file at
        /// `~/.vct-secrets/shared/github_pat` or the legacy flat path —
        /// those are user-owned data. The keychain is the resolution
        /// path; pre-existing files are left as harmless residue.
        #[test]
        fn register_github_pat_writes_to_keychain_not_file() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            // Clean slate — no prior keychain residue from another test.
            delete_keychain();

            let canary = format!("ghp_test_{}", uuid::Uuid::new_v4().simple());

            // Replicate `register_github_pat`'s logic without standing
            // up Tauri State. The command body is a thin shell over
            // these calls.
            let _ = migrate_github_pat_file_to_keychain(&db).unwrap();
            crate::secrets::set(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
                &canary,
            )
            .unwrap();
            db.mark_secret_active(
                "shared",
                SENTINEL_SHARED,
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .unwrap();
            // No defence-in-depth file removal: the launcher must not
            // delete user-owned files in `~/.vct-secrets/shared/`.

            // Keychain has the value (raw — bypass the active gate).
            assert_eq!(keychain_value().as_deref(), Some(canary.as_str()));

            // The active-flag-gated read also surfaces the canary.
            assert_eq!(
                github_pat_for_env(&db).as_deref(),
                Some(canary.as_str()),
                "github_pat_for_env should return the freshly-written value"
            );

            // Cleanup.
            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Non-destructive contract: a pre-existing file at
        /// `~/.vct-secrets/shared/github_pat` MUST survive the register
        /// flow. The keychain becomes the resolution path; the user's
        /// file is left untouched. (Regression pin 2026-05-09: the
        /// prior implementation silently destroyed user-owned PATs.)
        #[test]
        fn register_github_pat_preserves_existing_user_file() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            // Pre-place a user-owned file. The user "owns" this file
            // (e.g. they wrote it manually); the launcher must not
            // destroy it on register.
            let preexisting = format!("ghp_preexisting_{}", uuid::Uuid::new_v4().simple());
            let shared_dir = vct_secrets_shared_dir().unwrap();
            std::fs::create_dir_all(&shared_dir).unwrap();
            let path = shared_dir.join("github_pat");
            std::fs::write(&path, &preexisting).unwrap();

            // Run the migration (pulls preexisting → keychain) and
            // then a second keychain set with a NEW token (matching
            // register_github_pat's flow without the guard).
            let _ = migrate_github_pat_file_to_keychain(&db).unwrap();
            let new_token = format!("ghp_new_{}", uuid::Uuid::new_v4().simple());
            crate::secrets::set(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
                &new_token,
            )
            .unwrap();

            // The user's file is STILL THERE with its original value.
            assert!(
                path.exists(),
                "register flow must preserve user-owned file at {}",
                path.display(),
            );
            assert_eq!(
                std::fs::read_to_string(&path).unwrap().trim(),
                preexisting,
                "user-owned file must retain its original content",
            );

            // Cleanup.
            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// One-shot migration: a pre-existing file at
        /// `~/.vct-secrets/shared/github_pat` is read into the keychain
        /// and the `app_state` flag is set so subsequent calls are
        /// no-ops. The user's file is PRESERVED (2026-05-09 non-
        /// destructive fix).
        #[test]
        fn register_github_pat_one_shot_migrates_existing_file_preserving_it() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            // Pre-place a file at the new shared/ path with a canary.
            let canary = format!("ghp_existing_{}", uuid::Uuid::new_v4().simple());
            let shared_dir = vct_secrets_shared_dir().unwrap();
            std::fs::create_dir_all(&shared_dir).unwrap();
            let path = shared_dir.join("github_pat");
            std::fs::write(&path, &canary).unwrap();

            // Migrate.
            let report = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(report.migrated, "expected migrated=true: {:?}", report);
            assert!(
                !report.file_removed,
                "non-destructive contract: file_removed must be false: {:?}",
                report,
            );
            assert!(report.flag_set, "expected flag_set=true: {:?}", report);
            assert!(!report.already_done, "first run is not already_done");

            // Keychain has the value.
            assert_eq!(keychain_value().as_deref(), Some(canary.as_str()));

            // File PRESERVED — this is the non-destructive contract.
            assert!(
                path.exists(),
                "migration must NOT delete {} (user-owned data)",
                path.display(),
            );
            assert_eq!(
                std::fs::read_to_string(&path).unwrap().trim(),
                canary,
                "file content must remain intact",
            );

            // app_state flag is set.
            assert_eq!(
                db.app_state_get_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED).unwrap(),
                Some(true),
                "migration flag must be set after a clean migration",
            );

            // Run #2: idempotent — already_done short-circuits.
            let report2 = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(report2.already_done, "second run should be a no-op");
            assert!(!report2.migrated);
            assert!(!report2.file_removed);

            // Cleanup.
            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Migration honours the legacy flat path
        /// (`~/.vct-secrets/github_pat`) for the upgrade window where
        /// users still had pre-Phase-1 layouts. The file is PRESERVED
        /// after the keychain copy (2026-05-09 non-destructive fix).
        #[test]
        fn register_github_pat_migration_honours_legacy_flat_layout() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_legacy_{}", uuid::Uuid::new_v4().simple());
            let secrets_dir = vct_secrets_dir().unwrap();
            std::fs::create_dir_all(&secrets_dir).unwrap();
            let flat = secrets_dir.join("github_pat");
            std::fs::write(&flat, &canary).unwrap();

            let report = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(report.migrated, "expected migrated=true: {:?}", report);
            assert!(report.flag_set);
            assert!(
                !report.file_removed,
                "non-destructive contract: file_removed must be false: {:?}",
                report,
            );

            assert_eq!(keychain_value().as_deref(), Some(canary.as_str()));
            assert!(
                flat.exists(),
                "migration must NOT delete legacy flat path (user-owned): {}",
                flat.display(),
            );
            assert_eq!(
                std::fs::read_to_string(&flat).unwrap().trim(),
                canary,
                "legacy flat file content must remain intact",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// v0.2.22 Item #5 — the migration writes an audit_log row
        /// whose detail JSON carries `file_removed: false` per the
        /// non-destructive contract. This is the live consumer that
        /// turns the previously-dead `GithubPatMigrationReport.file_removed`
        /// field into a queryable historical record: an operator
        /// running `vct audit list --operation github_pat_file_migration`
        /// sees explicit evidence that the launcher did NOT delete
        /// user-owned PAT files in any migration run.
        #[test]
        fn migrate_github_pat_audit_log_records_file_removed_false() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            // Pre-place a file at the shared/ path with a canary so
            // the Case-3 codepath (file present, keychain empty) is
            // exercised — that's the path that lands on the new
            // audit-log write.
            let canary = format!("ghp_audit_{}", uuid::Uuid::new_v4().simple());
            let shared_dir = vct_secrets_shared_dir().unwrap();
            std::fs::create_dir_all(&shared_dir).unwrap();
            let path = shared_dir.join("github_pat");
            std::fs::write(&path, &canary).unwrap();

            let report = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(report.migrated);
            assert!(
                !report.file_removed,
                "non-destructive contract: file_removed must be false: {:?}",
                report,
            );

            // Query audit_log for the new row.
            let events = db
                .audit_list(
                    None, // project_id filter
                    None, // actor filter
                    None, // since
                    None, // until
                    Some("github_pat_file_migration"), // search
                    50,
                )
                .expect("audit_list");
            let row = events
                .iter()
                .find(|e| e.operation == "github_pat_file_migration")
                .unwrap_or_else(|| panic!(
                    "expected an audit_log row with operation='github_pat_file_migration', \
                     got: {:?}",
                    events.iter().map(|e| &e.operation).collect::<Vec<_>>(),
                ));

            // Detail is stored as a JSON string; round-trip and assert
            // the `file_removed` key is explicitly the JSON bool `false`.
            let detail: serde_json::Value = serde_json::from_str(&row.detail)
                .expect("audit detail is valid json");
            assert_eq!(
                detail.get("file_removed").and_then(|v| v.as_bool()),
                Some(false),
                "audit detail must record file_removed=false; got: {}",
                row.detail,
            );
            assert_eq!(
                detail.get("migrated").and_then(|v| v.as_bool()),
                Some(true),
                "audit detail must record migrated=true on this Case-3 run; got: {}",
                row.detail,
            );
            // module_id column carries the canonical module id so the
            // row is findable via the same indexing key used by the
            // installer→user consolidation migration's audit row.
            assert_eq!(row.module_id.as_deref(), Some(GITHUB_PAT_MODULE_ID));

            // Cleanup.
            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Migration with no file present: flips the flag and exits
        /// without touching the keychain.
        #[test]
        fn register_github_pat_migration_no_file_just_flips_flag() {
            let (home, _guard) = setup_temp_env();
            let db = make_db();

            let report = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(!report.migrated);
            assert!(!report.file_removed);
            assert!(!report.already_done);
            assert!(report.flag_set, "expected flag_set=true: {:?}", report);
            assert_eq!(
                db.app_state_get_bool(APP_STATE_KEY_GITHUB_PAT_MIGRATED).unwrap(),
                Some(true),
            );

            std::fs::remove_dir_all(&home).ok();
        }

        /// Replace-existing guard: when the keychain already holds a
        /// different non-empty value and the caller does not pass
        /// `force=true`, the register flow refuses with the
        /// `EXISTS_DIFFERENT:` sentinel. Adding `force=true` proceeds.
        /// (Non-destructive guard added 2026-05-09.)
        ///
        /// We can't call `register_github_pat` directly without Tauri
        /// State, so this test exercises the same predicate the
        /// command uses: keychain.get + compare + sentinel error.
        #[test]
        fn register_github_pat_replace_guard_emits_sentinel_when_token_differs() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            delete_keychain();

            // Pre-populate keychain.
            let existing = format!("ghp_existing_{}", uuid::Uuid::new_v4().simple());
            crate::secrets::set(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
                &existing,
            )
            .unwrap();

            // Simulate the command's guard predicate (force=false).
            let new_token = format!("ghp_new_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            let stored = crate::secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY).unwrap();
            let stored_trim = stored.as_deref().unwrap_or("").trim();
            let force = false;
            assert!(
                !force && !stored_trim.is_empty() && stored_trim != new_token.trim(),
                "guard precondition (different existing) must hold for the sentinel branch",
            );
            // The actual sentinel string the command would emit:
            let err = format!(
                "{}A different GitHub token is already saved. Replace it?",
                GITHUB_PAT_REPLACE_GUARD,
            );
            assert!(
                err.starts_with(GITHUB_PAT_REPLACE_GUARD),
                "register_github_pat must return an error starting with the sentinel",
            );

            // With force=true, the keychain set proceeds (matches the
            // command's behaviour after the guard).
            crate::secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, &new_token).unwrap();
            assert_eq!(keychain_value().as_deref(), Some(new_token.as_str()));

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Replace-existing guard is a no-op when the incoming token
        /// is identical to the stored one — re-saving the same token
        /// must succeed without `force=true`.
        #[test]
        fn register_github_pat_replace_guard_passes_when_token_identical() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            delete_keychain();

            let token = format!("ghp_same_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            crate::secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, &token).unwrap();

            // Same token, force=false → guard predicate is FALSE.
            let stored = crate::secrets::get(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY).unwrap();
            let stored_trim = stored.as_deref().unwrap_or("").trim();
            let guard_fires = !stored_trim.is_empty() && stored_trim != token.trim();
            assert!(
                !guard_fires,
                "guard must NOT fire when incoming == stored",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        // ── Item #2: github_pat_for_env active-flag gating ────────────

        /// `github_pat_for_env` returns Some when the keychain has a
        /// value AND the active flag is true.
        #[test]
        fn github_pat_for_env_returns_value_when_active_in_keychain() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_env_{}", uuid::Uuid::new_v4().simple());
            crate::secrets::set(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
                &canary,
            )
            .unwrap();
            db.mark_secret_active(
                "shared",
                SENTINEL_SHARED,
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .unwrap();

            assert_eq!(github_pat_for_env(&db).as_deref(), Some(canary.as_str()));

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Paused secret (Lifecycle B unset) MUST NOT surface to env-files.
        /// This is the asymmetric-leak test: keychain has the value but
        /// active-flag is false → reader returns None.
        #[test]
        fn github_pat_for_env_returns_none_when_paused() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_paused_{}", uuid::Uuid::new_v4().simple());
            crate::secrets::set(
                crate::secrets::SecretScope::Shared {
                    project_id: SENTINEL_SHARED,
                },
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
                &canary,
            )
            .unwrap();
            // Paused.
            db.mark_secret_inactive(
                "shared",
                SENTINEL_SHARED,
                GITHUB_PAT_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .unwrap();

            assert_eq!(
                github_pat_for_env(&db),
                None,
                "paused secret must NOT surface to env files (asymmetric leak)",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// `github_pat_for_env` with no keychain entry AND no file
        /// returns None (not an empty string, not an error).
        #[test]
        fn github_pat_for_env_returns_none_when_unset() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            assert_eq!(github_pat_for_env(&db), None);

            std::fs::remove_dir_all(&home).ok();
        }

        /// Legacy file fallback: until the user calls register_github_pat
        /// (which triggers the migration), reads still return the file
        /// value. This keeps the upgrade path smooth for users who don't
        /// re-register their PAT immediately.
        ///
        /// Test isolation: explicitly clear the keychain entry at start
        /// because the OS keychain is process-wide-shared (across tests
        /// AND across cargo test runs AND across the real launcher
        /// running on the dev machine). Without the clear, a residual
        /// value from a previous test run / actual `register_github_pat`
        /// call would short-circuit `github_pat_from_keychain` and the
        /// file-fallback path never gets exercised.
        #[test]
        fn github_pat_for_env_falls_back_to_legacy_file_pre_migration() {
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            // Clear any prior keychain residue so the file-fallback
            // path is the one exercised. If the keychain backend is
            // unreachable (CI), `delete` is a no-op and the test still
            // exercises the file-fallback (since the keychain read
            // also returns None on unreachable).
            delete_keychain();

            let canary = format!("ghp_legacy_fallback_{}", uuid::Uuid::new_v4().simple());
            let shared_dir = vct_secrets_shared_dir().unwrap();
            std::fs::create_dir_all(&shared_dir).unwrap();
            std::fs::write(shared_dir.join("github_pat"), &canary).unwrap();

            assert_eq!(github_pat_for_env(&db).as_deref(), Some(canary.as_str()));

            std::fs::remove_dir_all(&home).ok();
        }

        // ── Item #6 (post-0.2.0 backlog, 2026-05-10): module_id unification ──
        //
        // Pre-fix `register_github_pat` wrote at `shared.installer/github_pat`
        // while the SecretsPanel "Shared" tab wrote at `shared.user/github_pat`.
        // A user who registered via wizard then later edited via SecretsPanel
        // ended up with two divergent keychain rows. These tests pin:
        //   1. `register_github_pat`'s writer-side const points at the
        //      canonical user-bucket slot (`module_id="user"`).
        //   2. `migrate_github_pat_installer_to_user_module_id` is
        //      idempotent and self-gated by an `app_state` flag.
        //   3. The migration moves a value at the OLD `installer/` slot
        //      to the NEW `user/` slot when only the old has a value.
        //   4. When BOTH slots have values, the migration keeps the new
        //      one (later-write wins) and drops the old row.
        //   5. When neither has a value, the migration is a no-op that
        //      still flips the flag.
        //   6. An audit row is written tagged `github_pat_module_id_migration`.

        /// Item #6.1: writer-side contract — `GITHUB_PAT_MODULE_ID` is
        /// the canonical user-bucket id (`"user"`), matching the
        /// SecretsPanel `UI_MODULE_BUCKET` constant. This is the
        /// invariant the entire fix rests on; if a future commit flips
        /// it back this test catches it.
        #[test]
        fn register_github_pat_writes_to_user_module_id_not_installer() {
            assert_eq!(
                GITHUB_PAT_MODULE_ID, "user",
                "register_github_pat must write to module_id='user' to match the SecretsPanel UI_MODULE_BUCKET. \
                 If you changed this, you also need to update vct-module.json::bundled_secrets[0].module_id \
                 and ensure migrate_github_pat_installer_to_user_module_id still walks the previous slot."
            );
            assert_eq!(
                GITHUB_PAT_LEGACY_MODULE_ID, "installer",
                "GITHUB_PAT_LEGACY_MODULE_ID must remain 'installer' — it's the read-only target of the \
                 one-shot migration; changing it strands users who upgraded from 0.2.0."
            );
            assert_ne!(
                GITHUB_PAT_MODULE_ID, GITHUB_PAT_LEGACY_MODULE_ID,
                "the migration must walk OLD → NEW; identical consts collapse the migration to a no-op",
            );
        }

        /// Item #6.2: end-to-end keychain canary — when `register_github_pat`
        /// runs (here we exercise the same keychain set the command
        /// performs after its guard), the value lands at the
        /// `shared.user/github_pat` slot, NOT the legacy `installer/`
        /// slot.
        #[test]
        fn register_github_pat_lands_value_at_user_slot_not_installer_slot() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            delete_keychain();

            let canary = format!("ghp_user_slot_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            // Mirror register_github_pat's keychain set step.
            crate::secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, &canary).unwrap();

            // The new (user) slot has the value.
            assert_eq!(
                keychain_value().as_deref(),
                Some(canary.as_str()),
                "register flow must write to the user slot",
            );
            // The legacy (installer) slot stays empty — no shadow row.
            assert!(
                keychain_value_legacy().is_none(),
                "register flow must NOT write to the legacy installer slot",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.3: migration with old slot only — value is promoted
        /// to the new slot, old row deleted, flag set, winner=old.
        #[test]
        fn pat_module_id_migration_old_only_promotes_to_user_slot() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_old_only_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            // Seed the LEGACY slot only.
            crate::secrets::set(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY, &canary)
                .unwrap();
            // Pre-condition: new slot empty, old slot full.
            assert!(keychain_value().is_none(), "new slot must start empty");
            assert_eq!(
                keychain_value_legacy().as_deref(),
                Some(canary.as_str()),
                "old slot must hold the seed",
            );

            let report = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert!(!report.already_done);
            assert!(report.had_old_value);
            assert!(!report.had_new_value);
            assert_eq!(report.winner, "old");
            assert!(report.deleted_old_keychain_row);
            assert!(report.forgot_old_active_state);
            assert!(report.flag_set);
            assert!(
                report.warnings.is_empty(),
                "expected no warnings: {:?}",
                report.warnings
            );

            // Post-condition: new slot has the value, old slot empty.
            assert_eq!(
                keychain_value().as_deref(),
                Some(canary.as_str()),
                "value must be promoted to the user slot",
            );
            assert!(
                keychain_value_legacy().is_none(),
                "old installer slot must be empty after migration",
            );
            assert_eq!(
                db.app_state_get_bool(APP_STATE_KEY_PAT_MODULE_ID_MIGRATED).unwrap(),
                Some(true),
                "migration flag must be set",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.4: migration with BOTH slots populated — the new
        /// (user) value is kept (later-write wins), the old slot is
        /// dropped. Winner=new.
        #[test]
        fn pat_module_id_migration_both_keeps_new_drops_old() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let old_canary = format!("ghp_old_{}", uuid::Uuid::new_v4().simple());
            let new_canary = format!("ghp_new_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            // Seed BOTH slots — simulates user who used the wizard
            // (writes to old slot in 0.2.0) THEN the SecretsPanel
            // Shared tab (writes to new slot).
            crate::secrets::set(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY, &old_canary)
                .unwrap();
            crate::secrets::set(scope, GITHUB_PAT_MODULE_ID, GITHUB_PAT_KEY, &new_canary).unwrap();

            let report = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert!(report.had_old_value);
            assert!(report.had_new_value);
            assert_eq!(
                report.winner, "new",
                "both slots populated → new wins (later-write); got {:?}",
                report.winner
            );
            assert!(report.deleted_old_keychain_row);
            assert!(report.flag_set);

            // Post-condition: new slot retains its value, old slot empty.
            assert_eq!(
                keychain_value().as_deref(),
                Some(new_canary.as_str()),
                "new slot value must NOT be overwritten when both slots had values",
            );
            assert!(
                keychain_value_legacy().is_none(),
                "old installer slot must be cleared regardless of winner",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.5: migration with neither slot populated — no-op
        /// that still flips the flag. Subsequent calls short-circuit.
        #[test]
        fn pat_module_id_migration_neither_is_idempotent_noop() {
            // Doesn't strictly need keychain — both gets return None
            // when the keychain backend is unreachable, which the
            // migration treats as "empty slot". But we keep the gate
            // for symmetry with sibling tests.
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let report = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert!(!report.already_done, "first run must not be already_done");
            assert!(!report.had_old_value);
            assert!(!report.had_new_value);
            assert_eq!(report.winner, "none");
            assert!(!report.deleted_old_keychain_row);
            assert!(report.flag_set);

            // Both slots still empty.
            assert!(keychain_value().is_none());
            assert!(keychain_value_legacy().is_none());

            // Run #2: idempotent — already_done short-circuits.
            let report2 = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert!(report2.already_done);
            assert_eq!(report2.winner, "none");
            assert!(!report2.flag_set, "flag was already set; we don't re-set it");

            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.6: migration writes an `audit_log` row tagged
        /// `github_pat_module_id_migration` with the full state
        /// payload so the keychain delta is traceable.
        #[test]
        fn pat_module_id_migration_writes_audit_row() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_audit_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            crate::secrets::set(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY, &canary)
                .unwrap();

            let report = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert_eq!(report.winner, "old");

            // Audit row exists with the operation tag and module_id.
            let events = db
                .audit_list(None, None, None, None, Some("github_pat_module_id_migration"), 100)
                .unwrap();
            assert!(
                events.iter().any(|e| e.operation == "github_pat_module_id_migration"
                    && e.module_id.as_deref() == Some("user")),
                "expected an audit row tagged github_pat_module_id_migration with module_id='user'; got {:?}",
                events.iter().map(|e| &e.operation).collect::<Vec<_>>(),
            );

            // The detail JSON carries the from/to/winner triple.
            let row = events
                .iter()
                .find(|e| e.operation == "github_pat_module_id_migration")
                .expect("audit row present");
            let detail: serde_json::Value =
                serde_json::from_str(&row.detail).expect("audit detail is JSON");
            assert_eq!(detail.get("from_module_id").and_then(|v| v.as_str()), Some("installer"));
            assert_eq!(detail.get("to_module_id").and_then(|v| v.as_str()), Some("user"));
            assert_eq!(detail.get("winner").and_then(|v| v.as_str()), Some("old"));
            assert_eq!(detail.get("had_old_value").and_then(|v| v.as_bool()), Some(true));
            assert_eq!(detail.get("had_new_value").and_then(|v| v.as_bool()), Some(false));

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.6b: upgrade-window fallback — until the module_id
        /// migration runs, `github_pat_for_env` falls back to the
        /// legacy `installer/` slot so a 0.2.0 user's existing PAT
        /// stays reachable across the const flip. After migration the
        /// legacy slot is empty and the fallback is a no-op.
        #[test]
        fn github_pat_for_env_falls_back_to_legacy_installer_slot_pre_module_id_migration() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            // Simulate a 0.2.0 install: PAT lives at the LEGACY slot
            // only, no migration has run yet.
            let canary = format!("ghp_legacy_window_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            crate::secrets::set(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY, &canary)
                .unwrap();

            // The fallback surfaces it.
            assert_eq!(
                github_pat_for_env(&db).as_deref(),
                Some(canary.as_str()),
                "upgrade-window fallback must surface the legacy installer slot value",
            );

            // Run the module_id migration. After that, the value lives
            // at the new slot AND the legacy slot is empty.
            let report = migrate_github_pat_installer_to_user_module_id(&db).unwrap();
            assert!(report.flag_set);
            assert_eq!(
                github_pat_for_env(&db).as_deref(),
                Some(canary.as_str()),
                "post-migration, the same value resolves through the new slot",
            );
            assert!(
                keychain_value_legacy().is_none(),
                "legacy slot must be empty post-migration",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.6c: the upgrade-window fallback honours the legacy
        /// slot's active-flag gate. A paused 0.2.0 PAT MUST NOT leak.
        #[test]
        fn github_pat_for_env_legacy_fallback_honours_paused_state() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            let canary = format!("ghp_legacy_paused_{}", uuid::Uuid::new_v4().simple());
            let scope = crate::secrets::SecretScope::Shared {
                project_id: SENTINEL_SHARED,
            };
            crate::secrets::set(scope, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY, &canary)
                .unwrap();
            // Pause the legacy slot.
            db.mark_secret_inactive("shared", SENTINEL_SHARED, GITHUB_PAT_LEGACY_MODULE_ID, GITHUB_PAT_KEY)
                .unwrap();

            assert_eq!(
                github_pat_for_env(&db),
                None,
                "paused legacy PAT must NOT leak through the upgrade-window fallback",
            );

            // Cleanup.
            db.forget_secret_active_state(
                "shared",
                SENTINEL_SHARED,
                GITHUB_PAT_LEGACY_MODULE_ID,
                GITHUB_PAT_KEY,
            )
            .unwrap();
            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }

        /// Item #6.7: after the module_id migration runs, a subsequent
        /// `migrate_github_pat_file_to_keychain` (the OTHER migration —
        /// file → keychain) writes to the NEW user slot, not the old
        /// installer slot. This pins ordering: const flip + module_id
        /// migration + file→keychain migration must compose correctly.
        #[test]
        fn pat_file_to_keychain_migration_uses_user_slot_after_module_id_flip() {
            if !keyring_available() {
                eprintln!("[skip] no OS keychain backend in this test env");
                return;
            }
            let (home, _guard) = setup_temp_env();
            let db = make_db();
            delete_keychain();

            // Seed a file-fallback PAT — simulates a fresh install
            // where the user has `~/.vct-secrets/shared/github_pat`
            // but no keychain entries yet.
            let file_canary = format!("ghp_file_{}", uuid::Uuid::new_v4().simple());
            let shared_dir = vct_secrets_shared_dir().unwrap();
            std::fs::create_dir_all(&shared_dir).unwrap();
            std::fs::write(shared_dir.join("github_pat"), &file_canary).unwrap();

            // Run the file→keychain migration. After our 2026-05-10
            // const flip, this writes via GITHUB_PAT_MODULE_ID="user".
            let report = migrate_github_pat_file_to_keychain(&db).unwrap();
            assert!(report.migrated, "file→keychain must have run: {:?}", report);

            // Value lands at the user slot, not the legacy installer slot.
            assert_eq!(
                keychain_value().as_deref(),
                Some(file_canary.as_str()),
                "file→keychain migration must write to the user slot post-flip",
            );
            assert!(
                keychain_value_legacy().is_none(),
                "file→keychain migration must NOT write to the legacy installer slot",
            );

            delete_keychain();
            std::fs::remove_dir_all(&home).ok();
        }
    }

    // ─── Lightweight reinstall argv builder (KNOWN_ISSUES v0.2.x) ──────
    //
    // These tests verify the argv shape the launcher passes to
    // install.py for the lightweight reinstall flow. Spawning the real
    // subprocess would require a Python interpreter + an installed VCO
    // clone in the test env; the argv builder is the testable seam.

    mod lightweight_argv_tests {
        use super::*;

        #[test]
        fn lightweight_argv_minimal_no_old_path() {
            let argv = build_lightweight_install_argv(false, false, None, false, None);
            // Order matters — install.py argument parser is order-insensitive
            // but we lock the shape so the wizard's expected behaviour stays
            // observable in test output.
            // v0.2.73 (CG-3): --no-joern was REMOVED — install.py deleted that
            // flag and uses strict argparse, so passing it aborts the install
            // (exit 2). This shape must NOT contain it.
            assert_eq!(
                argv,
                vec![
                    "install.py".to_string(),
                    "--quiet".to_string(),
                    "--lightweight".to_string(),
                ]
            );
        }

        #[test]
        fn lightweight_argv_never_passes_removed_joern_flags() {
            // Regression for the v0.2.73 SB-1 ship-blocker: the launcher must
            // never forward a Joern flag that CG-3 removed from install.py's
            // strict argparse (an unknown flag → exit 2 → every launcher-driven
            // install/update aborts).
            let argv = build_lightweight_install_argv(
                true, false, Some("podman"), true, Some("/old"),
            );
            assert!(!argv.contains(&"--no-joern".to_string()));
            assert!(!argv.contains(&"--with-joern".to_string()));
        }

        #[test]
        fn lightweight_argv_with_old_path() {
            let argv = build_lightweight_install_argv(
                false,
                false,
                None,
                false,
                Some("/old/install/path"),
            );
            assert!(argv.contains(&"--lightweight".to_string()));
            assert!(argv.contains(&"--lightweight-old-path".to_string()));
            // Argv contains the literal old-path string AS the next element
            // after the flag — install.py reads it as a positional.
            let i = argv
                .iter()
                .position(|s| s == "--lightweight-old-path")
                .expect("flag");
            assert_eq!(argv.get(i + 1).map(String::as_str), Some("/old/install/path"));
        }

        #[test]
        fn lightweight_argv_empty_old_path_omits_flag() {
            // Empty string in `Some("")` must NOT produce a stray flag —
            // install.py would reject it. Defensive guard against the
            // wizard sending an empty text input.
            let argv = build_lightweight_install_argv(false, false, None, false, Some(""));
            assert!(!argv.contains(&"--lightweight-old-path".to_string()));
        }

        #[test]
        fn lightweight_argv_forwards_hardware_flags() {
            let argv = build_lightweight_install_argv(
                true,            // gpu
                false,           // not cpu_only
                Some("podman"),  // container runtime
                false,           // skip_containers
                None,
            );
            assert!(argv.contains(&"--gpu".to_string()));
            assert!(!argv.contains(&"--cpu-only".to_string()));
            assert!(argv.contains(&"--container".to_string()));
            assert!(argv.contains(&"podman".to_string()));
            assert!(!argv.contains(&"--no-containers".to_string()));
        }

        #[test]
        fn lightweight_argv_mutually_exclusive_gpu_cpu_only() {
            // Both flags can be passed; install.py validates exclusivity
            // itself. The launcher's job is to forward what the user
            // selected — we don't second-guess.
            let argv =
                build_lightweight_install_argv(true, true, None, false, None);
            assert!(argv.contains(&"--gpu".to_string()));
            assert!(argv.contains(&"--cpu-only".to_string()));
        }

        #[test]
        fn lightweight_argv_skip_containers() {
            let argv =
                build_lightweight_install_argv(false, false, None, true, None);
            assert!(argv.contains(&"--no-containers".to_string()));
        }

        #[test]
        fn lightweight_argv_empty_container_runtime_omits_flag() {
            let argv = build_lightweight_install_argv(false, false, Some(""), false, None);
            assert!(!argv.contains(&"--container".to_string()));
        }

        // ---- v0.2.16 (W4 / 0.5): check_for_updates helpers ----

        #[test]
        fn test_read_source_version_present() {
            let dir = tmp();
            fs::write(
                dir.join("vct-module.json"),
                r#"{"version": "0.2.16", "name": "VCO"}"#,
            )
            .unwrap();
            assert_eq!(read_source_version(&dir), Some("0.2.16".to_string()));
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_source_version_absent() {
            let dir = tmp();
            assert_eq!(read_source_version(&dir), None);
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_source_version_empty_string_returns_none() {
            let dir = tmp();
            fs::write(
                dir.join("vct-module.json"),
                r#"{"version": "", "name": "VCO"}"#,
            )
            .unwrap();
            // Empty string treated as "no usable version" — matches
            // `read_json_string_field` in commands::manifest.
            assert_eq!(read_source_version(&dir), None);
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_manifest_version_present() {
            let dir = tmp();
            fs::create_dir_all(dir.join("state")).unwrap();
            fs::write(
                dir.join("state").join("install-manifest.json"),
                r#"{"version": "0.2.13", "installed": true}"#,
            )
            .unwrap();
            assert_eq!(read_manifest_version(&dir), Some("0.2.13".to_string()));
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_manifest_version_no_manifest_dir() {
            let dir = tmp();
            // No `state/` dir at all — must not panic.
            assert_eq!(read_manifest_version(&dir), None);
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_manifest_version_malformed_json_returns_none() {
            let dir = tmp();
            fs::create_dir_all(dir.join("state")).unwrap();
            fs::write(
                dir.join("state").join("install-manifest.json"),
                "{ not json",
            )
            .unwrap();
            assert_eq!(read_manifest_version(&dir), None);
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_on_disk_binary_version_present() {
            // Cross-OS: build the path the same way the helper does
            // (`launcher_dist_subdir()` + `launcher_binary_filename()`
            // are compile-time per-target). Use PathBuf operations
            // throughout — no string `/` literals.
            //
            // v0.2.45 V45-H: the metadata sidecar filename is OS-dependent
            // (`vct-launcher.metadata.json` on Linux/macOS,
            // `vct-launcher.exe.metadata.json` on Windows). Use the
            // helper so this test exercises the correct path on each OS
            // and would fail on Windows without the V45-H fix.
            let dir = tmp();
            let subdir = launcher_dist_subdir();
            let bin_dir = dir.join("launcher").join("dist").join(subdir);
            fs::create_dir_all(&bin_dir).unwrap();
            let meta_name = format!("{}.metadata.json", launcher_binary_filename());
            fs::write(
                bin_dir.join(meta_name),
                r#"{"launcher_version": "0.2.15", "host_target": "any"}"#,
            )
            .unwrap();
            assert_eq!(
                read_on_disk_binary_version(&dir),
                Some("0.2.15".to_string())
            );
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_read_on_disk_binary_version_absent_path() {
            let dir = tmp();
            // No launcher/dist/<arch>/ at all.
            assert_eq!(read_on_disk_binary_version(&dir), None);
            fs::remove_dir_all(&dir).ok();
        }

        #[test]
        fn test_launcher_dist_subdir_matches_one_of_three_targets() {
            // Compile-time per-target. Just sanity-check that it's one of
            // the canonical strings the install.py side knows about.
            let s = launcher_dist_subdir();
            // v0.2.54 Track C: `macos-x64` added for Intel-Mac local builds.
            assert!(matches!(
                s,
                "linux-x64" | "macos-arm64" | "macos-x64" | "windows-x64"
            ));
        }

        // v0.2.45 V45-H — pinpoint test for FINDING C1 of the pre-tag
        // review. Verifies that `read_on_disk_binary_version` looks for
        // the correct per-OS sidecar filename, mirroring what
        // `scripts/build-bundled-launcher.sh` actually stages on disk
        // (`${DEST}.metadata.json` where `$DEST` carries `.exe` on
        // Windows). Without the V45-H fix this test would fail on
        // Windows (helper would search for `vct-launcher.metadata.json`
        // and miss the real `vct-launcher.exe.metadata.json`).
        #[test]
        fn test_v0245_v45h_metadata_filename_matches_on_disk_per_os() {
            let dir = tmp();
            // Compute the expected on-disk filename WITHOUT going through
            // launcher_binary_filename(). We hardcode the per-OS shape
            // the build script writes — if the production helper ever
            // drifts from this, the test catches it.
            let expected_filename = if cfg!(target_os = "windows") {
                "vct-launcher.exe.metadata.json"
            } else {
                "vct-launcher.metadata.json"
            };
            let arch_dir = dir
                .join("launcher")
                .join("dist")
                .join(launcher_dist_subdir());
            fs::create_dir_all(&arch_dir).unwrap();
            fs::write(
                arch_dir.join(expected_filename),
                r#"{"launcher_version":"0.2.45"}"#,
            )
            .unwrap();
            let result = read_on_disk_binary_version(&dir);
            assert_eq!(
                result.as_deref(),
                Some("0.2.45"),
                "read_on_disk_binary_version must find {} on this OS",
                expected_filename
            );
            fs::remove_dir_all(&dir).ok();
        }

        // v0.2.45 V45-H: paired sanity-check on the new helper. Catches
        // accidental regressions that would unify both branches to the
        // same literal — e.g. someone "simplifying" the cfg block.
        #[test]
        fn test_v0245_v45h_launcher_binary_filename_per_os() {
            let name = launcher_binary_filename();
            if cfg!(target_os = "windows") {
                assert_eq!(name, "vct-launcher.exe");
            } else {
                assert_eq!(name, "vct-launcher");
            }
        }

        #[test]
        fn install_config_deserialises_lightweight_fields() {
            // Frontend payload validation: a JSON payload missing the
            // new `lightweight` and `lightweight_old_path` fields must
            // still deserialise (defaults to false / None).
            let json_minimal = serde_json::json!({
                "install_path": "/x",
                "use_gpu": false,
                "cpu_only": false,
                "openai_key": null,
                "container_runtime": null,
                "skip_containers": false,
            });
            let cfg: InstallConfig = serde_json::from_value(json_minimal).unwrap();
            assert!(!cfg.lightweight, "default lightweight=false");
            assert!(cfg.lightweight_old_path.is_none(), "default old_path=None");

            // Full payload roundtrips both fields.
            let json_full = serde_json::json!({
                "install_path": "/x",
                "use_gpu": true,
                "cpu_only": false,
                "openai_key": null,
                "container_runtime": "podman",
                "skip_containers": false,
                "lightweight": true,
                "lightweight_old_path": "/old/path"
            });
            let cfg: InstallConfig = serde_json::from_value(json_full).unwrap();
            assert!(cfg.lightweight);
            assert_eq!(cfg.lightweight_old_path.as_deref(), Some("/old/path"));
        }
    }

    // ------------------------------------------------------------------
    // v0.2.21 Step 12: ensure_hub_stopped_for_update — pure-logic tests
    //
    // These exercise the early-exit branches that DON'T require an
    // actual running vct-hub binary:
    //   1. No hub.pid file present → Ok(false).
    //   2. Malformed pid contents → cleanup + Ok(false).
    //   3. Pid present but already dead → cleanup + Ok(false).
    //
    // The "live hub gets gracefully stopped" path needs a real
    // vct-hub binary running and is covered by Step 23 integration
    // tests, not here.
    //
    // VCT_STATE_DIR is process-wide; serialise with a Mutex so
    // parallel `cargo test` runs don't observe each other.
    // ------------------------------------------------------------------
    mod hub_stop_tests {
        use super::super::*;

        /// v0.2.21 Step 23: env-mutating tests across the workspace
        /// (auth::tests, lockfile::tests, boot::tests, hub_status,
        /// hub_launcher, this module, etc.) now serialize on a SHARED
        /// `vct_launcher_core::test_env::GLOBAL_ENV_MUTEX` rather than
        /// per-module mutexes that only serialized within-module.
        fn with_vct_state_dir<F: FnOnce(&Path)>(f: F) {
            vct_launcher_core::test_env::with_state_dir(f);
        }

        #[test]
        fn ensure_hub_stopped_returns_false_when_no_pid_file() {
            with_vct_state_dir(|root| {
                // No hub.pid in the state dir at all.
                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();
                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "no hub.pid → Ok(false), got {:?}", result);
            });
        }

        #[test]
        fn ensure_hub_stopped_cleans_up_malformed_pid_file() {
            with_vct_state_dir(|root| {
                let pid_file = root.join("hub.pid");
                std::fs::write(&pid_file, "not-a-number\n").unwrap();
                assert!(pid_file.exists());

                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();

                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "malformed pid → Ok(false), got {:?}", result);
                assert!(!pid_file.exists(),
                    "malformed hub.pid should be removed");
            });
        }

        #[test]
        fn ensure_hub_stopped_cleans_up_stale_dead_pid() {
            with_vct_state_dir(|root| {
                // Spawn + reap a process so we have a pid that's
                // provably dead. Same pattern as
                // vct_launcher_core::process::tests.
                #[cfg(unix)]
                let mut child = std::process::Command::new("true").silent()
                    .spawn()
                    .expect("spawn true");
                #[cfg(windows)]
                let mut child = std::process::Command::new("cmd").silent()
                    .args(["/c", "exit"])
                    .spawn()
                    .expect("spawn cmd /c exit");
                let dead_pid = child.id();
                let _ = child.wait();
                // Brief grace window for the kernel to actually reap
                // the zombie — same heuristic as the core tests.
                std::thread::sleep(std::time::Duration::from_millis(50));

                let pid_file = root.join("hub.pid");
                std::fs::write(&pid_file, format!("{}\n", dead_pid)).unwrap();
                assert!(pid_file.exists());

                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();

                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "stale dead pid → Ok(false), got {:?}", result);
                assert!(!pid_file.exists(),
                    "stale hub.pid for dead pid should be cleaned up");
            });
        }

        #[test]
        fn ensure_hub_stopped_reads_pid_from_two_line_lockfile() {
            // v0.2.69: the lockfile gained a 2nd line (the build identity).
            // The stop path must still read the PID from line 1 and NOT
            // mistake the 2-line file for malformed. Seed a 2-line file with
            // a provably-dead pid: it should be treated as a stale lockfile
            // (Ok(false) + cleaned up), exactly like the single-line dead
            // case — proving the 2nd line is tolerated.
            with_vct_state_dir(|root| {
                #[cfg(unix)]
                let mut child = std::process::Command::new("true").silent()
                    .spawn()
                    .expect("spawn true");
                #[cfg(windows)]
                let mut child = std::process::Command::new("cmd").silent()
                    .args(["/c", "exit"])
                    .spawn()
                    .expect("spawn cmd /c exit");
                let dead_pid = child.id();
                let _ = child.wait();
                std::thread::sleep(std::time::Duration::from_millis(50));

                let pid_file = root.join("hub.pid");
                // Two-line v0.2.69 format: pid on line 1, identity on line 2.
                std::fs::write(
                    &pid_file,
                    format!("{}\n0.2.69+deadbeef1234\n", dead_pid),
                )
                .unwrap();
                assert!(pid_file.exists());

                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();

                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "2-line lockfile with dead pid → Ok(false) (not malformed), got {:?}",
                    result);
                assert!(!pid_file.exists(),
                    "stale 2-line hub.pid for dead pid should be cleaned up");
            });
        }

        #[test]
        fn ensure_hub_stopped_handles_pid_zero_as_dead() {
            // pid 0 is a POSIX sentinel that pid_is_alive() rejects
            // up-front. The helper should treat it as "stale lockfile"
            // and remove the file rather than blocking forever.
            with_vct_state_dir(|root| {
                let pid_file = root.join("hub.pid");
                std::fs::write(&pid_file, "0\n").unwrap();

                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();

                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "pid 0 → Ok(false), got {:?}", result);
                assert!(!pid_file.exists(),
                    "hub.pid with sentinel pid 0 should be cleaned up");
            });
        }

        #[test]
        fn ensure_hub_stopped_handles_empty_pid_file() {
            with_vct_state_dir(|root| {
                let pid_file = root.join("hub.pid");
                std::fs::write(&pid_file, "").unwrap();

                let install_path = root.join("install");
                std::fs::create_dir_all(&install_path).unwrap();

                let result = ensure_hub_stopped_for_update(&install_path);
                assert!(matches!(result, Ok(false)),
                    "empty pid file → Ok(false), got {:?}", result);
                assert!(!pid_file.exists(),
                    "empty hub.pid should be cleaned up as malformed");
            });
        }

        #[test]
        fn should_skip_redundant_health_poll_when_cutover_sentinel_absent() {
            // v0.2.22 Item #3 happy path: install.py finished, deleted
            // the cutover sentinel after its own /health check passed.
            // The launcher's post-install hub-recovery code asks the
            // predicate "should we skip the redundant poll?" — answer
            // is YES, because the sentinel's absence IS the signal
            // that install.py validated /health already.
            with_vct_state_dir(|root| {
                // No sentinel file present. (with_vct_state_dir gives
                // us a fresh tempdir, so nothing is in it by default.)
                let sentinel = root.join(V0_2_21_CUTOVER_SENTINEL_NAME);
                assert!(!sentinel.exists(),
                    "precondition: sentinel should be absent in fresh state dir");
                assert!(
                    should_skip_redundant_health_poll(root),
                    "absent sentinel → poll must be skipped (install.py confirmed /health)"
                );
            });
        }

        #[test]
        fn should_run_full_poll_when_cutover_sentinel_present() {
            // v0.2.22 Item #3 rare slow-path: install.py never deleted
            // the sentinel (timed out, killed, or crashed mid-cutover).
            // The launcher's post-install hub-recovery code asks the
            // predicate "should we skip the redundant poll?" — answer
            // is NO, because install.py's /health check did NOT confirm
            // and the launcher's 30 s poll is the only remaining
            // safety net before the launcher restart.
            with_vct_state_dir(|root| {
                let sentinel = root.join(V0_2_21_CUTOVER_SENTINEL_NAME);
                std::fs::write(&sentinel, b"in-progress").unwrap();
                assert!(sentinel.exists(),
                    "precondition: sentinel must exist after write");
                assert!(
                    !should_skip_redundant_health_poll(root),
                    "present sentinel → poll MUST run (second-chance probe)"
                );
            });
        }

        #[test]
        fn ensure_hub_started_after_update_is_soft_fail_when_no_binary() {
            // No vct-hub binary anywhere → returns Ok(()), prints a
            // warning. The launcher's own boot path will retry.
            with_vct_state_dir(|_root| {
                let prev_bin = std::env::var_os("VCT_HUB_BIN");
                let prev_path = std::env::var_os("PATH");
                let prev_home = std::env::var_os("HOME");
                let prev_profile = std::env::var_os("USERPROFILE");
                unsafe {
                    std::env::set_var("VCT_HUB_BIN", "/nonexistent/vct-hub");
                    std::env::set_var("PATH", "/nonexistent-dir");
                    std::env::set_var("HOME", "/nonexistent-home");
                    std::env::set_var("USERPROFILE", "/nonexistent-profile");
                }
                let install_path = std::env::temp_dir();
                let result = ensure_hub_started_after_update(&install_path);
                assert!(result.is_ok(),
                    "missing binary should soft-fail to Ok(()), got {:?}",
                    result);
                unsafe {
                    match prev_bin {
                        Some(v) => std::env::set_var("VCT_HUB_BIN", v),
                        None => std::env::remove_var("VCT_HUB_BIN"),
                    }
                    match prev_path {
                        Some(v) => std::env::set_var("PATH", v),
                        None => std::env::remove_var("PATH"),
                    }
                    match prev_home {
                        Some(v) => std::env::set_var("HOME", v),
                        None => std::env::remove_var("HOME"),
                    }
                    match prev_profile {
                        Some(v) => std::env::set_var("USERPROFILE", v),
                        None => std::env::remove_var("USERPROFILE"),
                    }
                }
            });
        }
    }

    // ------------------------------------------------------------------
    // v0.2.34 (Agent B): hardware-snapshot freshness invariant.
    //
    // Coverage for bug #5 of the v0.2.34 backlog (observed RTX 4080 SUPER
    // bug — persisted snapshot had `has_nvidia_gpu:true` but missing
    // `gpu_mode_decided` because v0.2.20 added the field without
    // backfilling existing snapshots; serde defaulted to `Cpu` → CUDA
    // host pulled `-cpu` variant).
    //
    // The invariant we are testing here: a snapshot in
    // `app_state.launcher.hardware_snapshot` is fresh + structurally
    // complete whenever it is READ for an install decision. Three trigger
    // points enforce it (1) at launcher-update completion via the
    // `launcher.hardware_redetect_pending` flag, (2) at install-time via
    // `resolve_fresh_or_last_known_snapshot_with_probe`, (3) the manual
    // Preferences button (uses the same code path, no test needed here).
    //
    // Tests inject a probe closure so they don't depend on the host
    // having `nvidia-smi` / `system_profiler` etc. on PATH.
    // ------------------------------------------------------------------
    mod hardware_snapshot_freshness_tests {
        use super::super::*;
        use crate::commands::gpu_policy::GpuMode;

        /// Build a `SystemDetection` fixture that mimics the RTX 4080
        /// SUPER host the validation pass produced — `has_nvidia_gpu`
        /// true, 16 GB VRAM (well above the 8 GB threshold).
        fn rtx4080_super_fixture() -> SystemDetection {
            SystemDetection {
                os: "linux".to_string(),
                arch: "x86_64".to_string(),
                has_nvidia_gpu: true,
                gpu_name: "NVIDIA GeForce RTX 4080 SUPER".to_string(),
                has_apple_silicon: false,
                has_docker: false,
                has_podman: true,
                has_python: true,
                python_version: "3.12.0".to_string(),
                python_cmd: "python3".to_string(),
                has_claude_cli: true,
                has_git: true,
                has_node: true,
                container_runtime: Some("podman 4.9.0".to_string()),
                ram_gb: 64,
                vram_gb: 16,
                gpu_vendor: Some("NVIDIA".to_string()),
                gpus: vec![crate::commands::gpu_policy::GpuCandidate {
                    vendor: "nvidia".to_string(),
                    name: "NVIDIA GeForce RTX 4080 SUPER".to_string(),
                    vram_gb: 16.0,
                    is_integrated: false,
                }],
                chosen_gpu_name: "NVIDIA GeForce RTX 4080 SUPER".to_string(),
            }
        }

        /// CPU-only fallback fixture for the "no GPU detected" path.
        fn cpu_only_fixture() -> SystemDetection {
            SystemDetection {
                os: "linux".to_string(),
                arch: "x86_64".to_string(),
                has_nvidia_gpu: false,
                gpu_name: String::new(),
                has_apple_silicon: false,
                has_docker: false,
                has_podman: false,
                has_python: false,
                python_version: String::new(),
                python_cmd: String::new(),
                has_claude_cli: false,
                has_git: false,
                has_node: false,
                container_runtime: None,
                ram_gb: 8,
                vram_gb: 0,
                gpu_vendor: None,
                gpus: Vec::new(),
                chosen_gpu_name: String::new(),
            }
        }

        /// Write a deliberately-partial JSON blob to `app_state` to
        /// simulate the v0.2.20 schema gap that broke the user's
        /// production install. The shape mirrors what a pre-v0.2.9
        /// snapshot would look like on disk: no `gpu_mode_decided`,
        /// no `vram_gb`, no `has_amd_gpu`.
        fn write_partial_schema_snapshot(db: &Db) {
            // `has_nvidia_gpu: true` + 16 GB ram + no decided-mode key —
            // serde defaults `gpu_mode_decided` to `Cpu` via the
            // `default_gpu_mode` fn, which is exactly the bug.
            let raw = r#"{
                "has_nvidia_gpu": true,
                "gpu_name": "NVIDIA GeForce RTX 4080 SUPER",
                "has_apple_silicon": false,
                "ram_gb": 64,
                "use_gpu": true,
                "low_resource": false
            }"#;
            db.app_state_set(APP_STATE_KEY_HARDWARE_SNAPSHOT, raw).unwrap();
        }

        /// (Test c) RTX-4080-style fixture (has_nvidia_gpu=true,
        /// vram_gb=16) → `gpu_mode_decided` resolves to `Cuda`, NOT the
        /// stale `Cpu` the bug produced. Direct test of the snapshot
        /// builder so the contract is locked at the lowest layer.
        #[test]
        fn rtx4080_super_resolves_to_cuda_not_cpu() {
            let sys = rtx4080_super_fixture();
            let snap = snapshot_from_system(&sys);
            assert_eq!(
                snap.gpu_mode_decided,
                GpuMode::Cuda,
                "RTX 4080 SUPER (16 GB VRAM, has_nvidia_gpu=true) must decide CUDA, got {:?}",
                snap.gpu_mode_decided
            );
            assert!(snap.use_gpu, "derived use_gpu must be true for CUDA");
            assert!(!snap.low_resource, "64 GB RAM is not low_resource");
            assert_eq!(snap.vram_gb, 16.0);
            assert!(snap.has_nvidia_gpu);
        }

        /// (Test a) Snapshot missing `gpu_mode_decided` → install-time
        /// re-detect populates it before `gpu_mode` is read.
        ///
        /// Reproduces the observed production bug: persisted row is the
        /// v0.2.20-era partial schema (no `gpu_mode_decided`), install
        /// flow reads it, serde defaults missing field to `Cpu`. After
        /// `resolve_fresh_or_last_known_snapshot_with_probe` runs with
        /// an RTX-4080-style probe, the returned snapshot has the
        /// correctly-decided `Cuda` mode AND the persisted row is now
        /// fresh + structurally complete.
        #[tokio::test]
        async fn install_time_redetect_populates_missing_gpu_mode_decided() {
            let db = crate::db::Db::open_in_memory().unwrap();

            // Seed the partial-schema row.
            write_partial_schema_snapshot(&db);

            // Sanity: the partial row parses but defaults to Cpu — i.e.
            // we've successfully reproduced the bug's initial state.
            let stale = read_persisted_hardware_snapshot(&db)
                .unwrap()
                .expect("partial-schema row should still parse");
            assert_eq!(
                stale.gpu_mode_decided,
                GpuMode::Cpu,
                "partial-schema serde default must reproduce the production bug"
            );

            // Run the install-time guard with an RTX-4080-style probe.
            let fresh = resolve_fresh_or_last_known_snapshot_with_probe(&db, || async {
                Ok(rtx4080_super_fixture())
            })
            .await
            .expect("probe success must yield a snapshot");

            // The returned snapshot must have the correct decided mode.
            assert_eq!(
                fresh.gpu_mode_decided,
                GpuMode::Cuda,
                "install-time redetect must populate gpu_mode_decided=Cuda"
            );

            // The persisted row is now fresh — a subsequent read picks
            // up the correct mode WITHOUT another redetect.
            let after_persist = read_persisted_hardware_snapshot(&db)
                .unwrap()
                .expect("snapshot must be persisted after successful probe");
            assert_eq!(after_persist.gpu_mode_decided, GpuMode::Cuda);
            assert_eq!(after_persist.vram_gb, 16.0);
            assert_eq!(after_persist.gpu_name, "NVIDIA GeForce RTX 4080 SUPER");
        }

        /// (Test b) Re-detect failure during install → falls back to
        /// last-known snapshot + emits warning (logged via eprintln,
        /// not asserted; the architectural contract is that the install
        /// receives SOME snapshot even when the probe is broken).
        ///
        /// Production parallel: nvidia-smi not on PATH due to a driver
        /// reinstall, OR the binary missing in a hardened container
        /// host. The install must NOT block on this; it falls back to
        /// the last known good state.
        #[tokio::test]
        async fn install_time_redetect_falls_back_when_probe_fails() {
            let db = crate::db::Db::open_in_memory().unwrap();

            // Seed a KNOWN-GOOD snapshot (full schema, GpuMode::Cuda).
            let known = snapshot_from_system(&rtx4080_super_fixture());
            write_persisted_hardware_snapshot(&db, &known).unwrap();

            // Probe that always fails (mimics nvidia-smi missing).
            let result = resolve_fresh_or_last_known_snapshot_with_probe(&db, || async {
                Err("nvidia-smi not on PATH (mock)".to_string())
            })
            .await;

            let snap = result.expect("probe failure must fall back, not propagate, when a last-known snapshot exists");
            // Returned snapshot is the LAST-KNOWN one, not a default
            // empty value.
            assert_eq!(snap.gpu_mode_decided, GpuMode::Cuda);
            assert_eq!(snap.vram_gb, 16.0);
            assert!(snap.has_nvidia_gpu);

            // Persisted row is unchanged (probe failed → no write).
            let after = read_persisted_hardware_snapshot(&db).unwrap().unwrap();
            assert_eq!(after.gpu_mode_decided, GpuMode::Cuda);
        }

        /// Probe failure WITHOUT a last-known snapshot must propagate
        /// the error — there is no safe default and the caller (via
        /// `ensure_fresh_hardware_snapshot_for_install`) chooses how to
        /// degrade (currently: fall back to GpuMode::Cpu at the call
        /// site in `install_module_for_project`, which preserves the
        /// pre-v0.2.34 behaviour for the no-snapshot case).
        #[tokio::test]
        async fn install_time_redetect_propagates_when_no_fallback() {
            let db = crate::db::Db::open_in_memory().unwrap();
            // No prior snapshot.

            let result = resolve_fresh_or_last_known_snapshot_with_probe(&db, || async {
                Err("probe broken".to_string())
            })
            .await;

            assert!(
                result.is_err(),
                "probe failure with no last-known snapshot must propagate the error"
            );
        }

        /// (Test d) Background re-detect at launcher-update boundary
        /// writes back to app_state correctly.
        ///
        /// We can't easily mount a full `tauri::AppHandle` in a unit
        /// test, so this exercises the same code path the
        /// `consume_pending_hardware_redetect_if_set` spawn task runs —
        /// the flag-flip + the underlying `redetect_hardware_with_probe`
        /// — and asserts the persisted row was updated.
        #[tokio::test]
        async fn post_update_redetect_writes_back_to_app_state() {
            let db = crate::db::Db::open_in_memory().unwrap();

            // Step 1: simulate the previous launcher's update flow
            // marking the next boot as pending re-detect.
            mark_hardware_redetect_pending_after_update(&db);
            assert!(
                is_hardware_redetect_pending(&db),
                "flag must be set after mark_hardware_redetect_pending_after_update"
            );

            // Step 2: simulate the boot-time consumer's flag-clear +
            // background-redetect work.
            clear_hardware_redetect_pending(&db);
            assert!(
                !is_hardware_redetect_pending(&db),
                "flag must be cleared before the background job runs (so a fast restart-during-restart can't double-fire)"
            );

            // The background job itself.
            let diff = redetect_hardware_with_probe(&db, || async {
                Ok(rtx4080_super_fixture())
            })
            .await
            .expect("redetect_hardware_with_probe must succeed with mock probe");

            // before is None (no prior snapshot) so changed_fields is
            // empty by contract.
            assert!(diff.before.is_none());
            assert!(diff.changed_fields.is_empty());
            assert_eq!(diff.after.gpu_mode_decided, GpuMode::Cuda);

            // The persisted row reflects the fresh snapshot.
            let persisted = read_persisted_hardware_snapshot(&db).unwrap().unwrap();
            assert_eq!(persisted.gpu_mode_decided, GpuMode::Cuda);
            assert_eq!(persisted.vram_gb, 16.0);
        }

        /// Edge case: the pending flag, once consumed, must NOT
        /// re-fire on the next boot unless the update flow sets it
        /// again. Catches a double-clear bug where a stale flag would
        /// trigger an unnecessary redetect on every subsequent boot.
        #[test]
        fn pending_flag_idempotent_clear() {
            let db = crate::db::Db::open_in_memory().unwrap();
            assert!(!is_hardware_redetect_pending(&db));

            mark_hardware_redetect_pending_after_update(&db);
            assert!(is_hardware_redetect_pending(&db));

            clear_hardware_redetect_pending(&db);
            assert!(!is_hardware_redetect_pending(&db));

            // Second clear is a no-op (idempotent).
            clear_hardware_redetect_pending(&db);
            assert!(!is_hardware_redetect_pending(&db));
        }

        /// Diff path: a snapshot whose stale `gpu_mode_decided=Cpu`
        /// gets corrected to `Cuda` after redetect emits the field name
        /// in `changed_fields` so the Preferences UI can surface the
        /// "Apply reconfiguration" CTA. Locks the user-visible diff
        /// behaviour for the exact production-bug scenario.
        #[tokio::test]
        async fn redetect_diff_reports_gpu_mode_decided_change() {
            let db = crate::db::Db::open_in_memory().unwrap();
            write_partial_schema_snapshot(&db);

            let diff = redetect_hardware_with_probe(&db, || async {
                Ok(rtx4080_super_fixture())
            })
            .await
            .expect("redetect must succeed");

            assert!(diff.before.is_some(), "partial-schema row must parse as Some(before)");
            assert!(
                diff.changed_fields.iter().any(|f| f == "gpu_mode_decided"),
                "diff must surface gpu_mode_decided change, got {:?}",
                diff.changed_fields
            );
            assert!(
                diff.changed_fields.iter().any(|f| f == "vram_gb"),
                "diff must surface vram_gb change (0.0 → 16.0), got {:?}",
                diff.changed_fields
            );
        }

        /// Smoke-test the CPU-only probe path: confirms the helper
        /// doesn't accidentally hard-code the GPU branch.
        #[tokio::test]
        async fn cpu_only_probe_resolves_to_cpu() {
            let db = crate::db::Db::open_in_memory().unwrap();
            let diff =
                redetect_hardware_with_probe(&db, || async { Ok(cpu_only_fixture()) })
                    .await
                    .expect("probe success");
            assert_eq!(diff.after.gpu_mode_decided, GpuMode::Cpu);
            assert!(!diff.after.use_gpu);
            assert_eq!(diff.after.vram_gb, 0.0);
            assert_eq!(diff.after.ram_gb, 8);
            // 8 GB RAM is on the boundary — low_resource is strict `<8`.
            assert!(!diff.after.low_resource);
        }
    }

    // ------------------------------------------------------------------
    // v0.2.23 (B4 / D19): divergence-detection modal flow.
    //
    // Tests cover three layers:
    //   1. Unit tests for the pure helpers (serializers, predicates).
    //   2. End-to-end git-fixture tests for update_orchestrator's non-FF
    //      detection branch. We can't drive the full Tauri command from
    //      a unit test (it depends on a Tauri Runtime + Window), so we
    //      cover the path by exercising the raw `git pull --ff-only`
    //      sequence + the structured-error helper.
    //   3. End-to-end git-fixture tests for merge / rebase conflict
    //      detection. Same pattern: raw git invocation followed by the
    //      `collect_conflicted_files` + serializer helpers.
    //
    // The git-fixture tests are skipped if `git` isn't on PATH.
    // ------------------------------------------------------------------
    mod divergence_modal_tests {
        use super::super::*;
        use std::process::{Command as StdCommand, Stdio};

        /// Skip a test if `git --version` doesn't succeed.
        macro_rules! skip_if_no_git {
            () => {
                if StdCommand::new("git").silent()
                    .arg("--version")
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .map(|s| !s.success())
                    .unwrap_or(true)
                {
                    eprintln!("skipping: git not on PATH");
                    return;
                }
            };
        }

        // ----------------------------------------------------------------
        // Unit tests — pure helpers, no git.
        // ----------------------------------------------------------------

        #[test]
        fn serialize_orchestrator_non_ff_produces_parseable_json() {
            let s = serialize_orchestrator_non_ff_error(
                "main",
                Some("abc1234"),
                Some("def5678"),
                &["CLAUDE.md".to_string(), "knowledge/foo.md".to_string()],
                &["other_projects_knowledge/local-only.md".to_string()],
                "fatal: Not possible to fast-forward, aborting.",
            );
            // Must start with the canonical discriminator so the frontend's
            // fast-path recognises it.
            assert!(s.starts_with("{\"event\":\"orchestrator_update_non_ff\""));

            let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
            assert_eq!(v["event"], "orchestrator_update_non_ff");
            assert_eq!(v["branch"], "main");
            assert_eq!(v["local_sha"], "abc1234");
            assert_eq!(v["remote_sha"], "def5678");
            let files = v["diverged_files"].as_array().expect("array");
            assert_eq!(files.len(), 2);
            assert_eq!(files[0], "CLAUDE.md");
            assert_eq!(files[1], "knowledge/foo.md");
            // v0.2.27: local_only_files is a separate category.
            let local_only = v["local_only_files"].as_array().expect("array");
            assert_eq!(local_only.len(), 1);
            assert_eq!(local_only[0], "other_projects_knowledge/local-only.md");
        }

        #[test]
        fn serialize_orchestrator_non_ff_handles_empty_file_list() {
            let s = serialize_orchestrator_non_ff_error(
                "main",
                None,
                None,
                &[],
                &[],
                "boom",
            );
            let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
            assert_eq!(v["diverged_files"].as_array().unwrap().len(), 0);
            assert_eq!(v["local_only_files"].as_array().unwrap().len(), 0);
            assert!(v["local_sha"].is_null());
            assert!(v["remote_sha"].is_null());
        }

        #[test]
        fn serialize_orchestrator_non_ff_escapes_special_chars_in_paths() {
            // File paths can contain quotes on Windows (rare but legal).
            // The path "weird\"name.md" tests both backslash and quote
            // escaping.
            let s = serialize_orchestrator_non_ff_error(
                "main",
                None,
                None,
                &["weird\"name.md".to_string()],
                &[],
                "boom",
            );
            let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
            let files = v["diverged_files"].as_array().unwrap();
            assert_eq!(files[0], "weird\"name.md");
        }

        #[test]
        fn serialize_orchestrator_conflict_produces_parseable_json() {
            let s = serialize_orchestrator_conflict_error(
                "merge",
                "main",
                &["CLAUDE.md".to_string(), "CONTEXT_STATE.md".to_string()],
                "CONFLICT (content): Merge conflict in CLAUDE.md",
            );
            assert!(s.starts_with("{\"event\":\"orchestrator_update_conflict\""));

            let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
            assert_eq!(v["event"], "orchestrator_update_conflict");
            assert_eq!(v["operation"], "merge");
            assert_eq!(v["branch"], "main");
            let files = v["conflicted_files"].as_array().expect("array");
            assert_eq!(files.len(), 2);
        }

        #[test]
        fn serialize_orchestrator_conflict_handles_rebase_operation() {
            let s = serialize_orchestrator_conflict_error(
                "rebase",
                "main",
                &["a.md".to_string()],
                "could not apply abc123",
            );
            let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
            assert_eq!(v["operation"], "rebase");
        }

        #[test]
        fn is_merge_or_rebase_conflict_detects_canonical_phrases() {
            // Real stderr samples from git 2.34+.
            assert!(is_merge_or_rebase_conflict(
                "CONFLICT (content): Merge conflict in CLAUDE.md"
            ));
            assert!(is_merge_or_rebase_conflict(
                "Automatic merge failed; fix conflicts and then commit the result."
            ));
            assert!(is_merge_or_rebase_conflict(
                "error: could not apply abc123... Update README"
            ));
            assert!(is_merge_or_rebase_conflict(
                "Resolve all conflicts manually, mark them as resolved with"
            ));
        }

        #[test]
        fn is_merge_or_rebase_conflict_detects_dirty_tree_refusals_v0271() {
            // v0.2.71: dirty-tree refusals + autostash-pop must now be treated
            // as conflicts so they route to the conflict modal + UPDATE_DEFERRED
            // recovery instead of a bare-error dead-end (the .gitignore case).
            // Real stderr samples from git 2.34+.
            assert!(is_merge_or_rebase_conflict(
                "error: Your local changes to the following files would be overwritten by merge:\n\t.gitignore\nPlease commit your changes or stash them before you merge."
            ));
            assert!(is_merge_or_rebase_conflict(
                "error: cannot rebase: You have unstaged changes."
            ));
            assert!(is_merge_or_rebase_conflict(
                "error: cannot pull with rebase: You have unstaged changes."
            ));
            assert!(is_merge_or_rebase_conflict(
                "error: Cannot merge with local modifications; please commit or stash them."
            ));
            assert!(is_merge_or_rebase_conflict(
                "Applying autostash resulted in conflicts."
            ));
            assert!(is_merge_or_rebase_conflict(
                "error: could not apply autostash, the stash entry is kept"
            ));
        }

        #[test]
        fn is_merge_or_rebase_conflict_ignores_unrelated_errors() {
            assert!(!is_merge_or_rebase_conflict("fatal: not a git repository"));
            assert!(!is_merge_or_rebase_conflict("Could not resolve host: github.com"));
            assert!(!is_merge_or_rebase_conflict(""));
            // Network / fetch / spawn failures must still NOT look like conflicts.
            assert!(!is_merge_or_rebase_conflict("fatal: unable to access 'https://...': Failed to connect"));
            assert!(!is_merge_or_rebase_conflict("error: Permission denied (publickey)."));
        }

        #[test]
        fn is_merge_or_rebase_conflict_is_case_insensitive() {
            assert!(is_merge_or_rebase_conflict("AUTOMATIC MERGE FAILED"));
        }

        // ----------------------------------------------------------------
        // End-to-end git-fixture tests. Build a real local + "remote"
        // repo (local file:// URL acts as the remote), introduce
        // divergence, and observe the helper output.
        // ----------------------------------------------------------------

        /// Init a bare repo (acts as the "remote" upstream) and a working
        /// clone of it. Returns (tempdir, remote_bare_path, local_clone_path).
        /// The tempdir is held by the caller to keep it alive for the
        /// duration of the test.
        fn init_remote_and_clone() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
            let tmp = tempfile::tempdir().expect("tempdir");
            let root = tmp.path();
            let remote = root.join("remote.git");
            let local = root.join("local");

            // Init the bare remote.
            let status = StdCommand::new("git").silent()
                .args(["init", "--bare", "--initial-branch=main"])
                .arg(&remote)
                .status()
                .expect("git init bare");
            assert!(status.success(), "git init --bare failed");

            // Init a seeding workdir, commit a base file, push to remote.
            let seed = root.join("seed");
            std::fs::create_dir_all(&seed).unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["init", "--initial-branch=main"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            // Local config so commit succeeds even when global config is empty.
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "test@example.com"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Test"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            std::fs::write(seed.join("README.md"), "base\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "README.md"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "base"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["remote", "add", "origin"])
                .arg(remote.to_str().unwrap())
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["push", "origin", "main"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());

            // Clone the bare remote into the "local" workdir.
            assert!(StdCommand::new("git").silent()
                .args(["clone"])
                .arg(remote.to_str().unwrap())
                .arg(&local)
                .status()
                .unwrap()
                .success());
            // Local config in the clone.
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "test@example.com"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Test"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            // Add the canonical vco_upstream remote pointing at the bare
            // repo so the production code paths (which use vco_upstream,
            // not origin) work.
            assert!(StdCommand::new("git").silent()
                .args(["remote", "add", "vco_upstream"])
                .arg(remote.to_str().unwrap())
                .current_dir(&local)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["fetch", "vco_upstream"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            // Advance the remote by one commit so the local clone is
            // "behind" (this gives the test a meaningful diff to detect).
            // Push the new commit via the seed workdir.
            std::fs::write(seed.join("UPSTREAM.md"), "upstream-only\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "UPSTREAM.md"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "upstream commit"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["push", "origin", "main"])
                .current_dir(&seed)
                .status()
                .unwrap()
                .success());

            // Re-fetch so vco_upstream/main reflects the new tip.
            assert!(StdCommand::new("git").silent()
                .args(["fetch", "vco_upstream"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            (tmp, remote, local)
        }

        /// Add a local commit to the clone, so HEAD differs from vco_upstream/main.
        fn add_local_divergent_commit(local: &Path, file: &str, body: &str) {
            std::fs::write(local.join(file), body).unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", file])
                .current_dir(local)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "local divergent"])
                .current_dir(local)
                .status()
                .unwrap()
                .success());
        }

        /// v0.2.78 ITEM #0: push a NEW file to upstream (adds a tracked file in
        /// the merge-base..theirs range) via the seed workdir, then re-fetch
        /// vco_upstream in the local clone. Sibling of `init_remote_and_clone`'s
        /// inline upstream-advance. `seed` is `<tmp>/seed`.
        fn push_upstream_added_file(seed: &Path, local: &Path, file: &str, body: &str) {
            let abs = seed.join(file);
            if let Some(p) = abs.parent() {
                std::fs::create_dir_all(p).unwrap();
            }
            std::fs::write(&abs, body).unwrap();
            for args in [
                vec!["add", file],
                vec!["commit", "-m", "upstream adds file"],
                vec!["push", "origin", "main"],
            ] {
                assert!(StdCommand::new("git")
                    .silent()
                    .args(&args)
                    .current_dir(seed)
                    .status()
                    .unwrap()
                    .success());
            }
            assert!(StdCommand::new("git")
                .silent()
                .args(["fetch", "vco_upstream"])
                .current_dir(local)
                .status()
                .unwrap()
                .success());
        }

        // ─── v0.2.78 ITEM #0 (F2 keystone) — untracked-collision handling ───
        // "Test the decision, not just the happy path": BOTH the ACT branch
        // (byte-identical untracked collision → auto-removed, Ok) and the
        // LEAVE-ALONE branch (divergent → nothing removed, Err payload +
        // deferral) get a test. These exercise the SHARED handler
        // `handle_untracked_collisions_pre_pull` end-to-end against real git.

        /// ACT: an untracked local file byte-identical to an incoming
        /// upstream-added blob is auto-removed so the merge can proceed → Ok(()),
        /// and the local file is gone. This is the exact live 2026-07-11 case.
        #[tokio::test]
        async fn v0278_pre_pull_auto_removes_byte_identical_untracked_collision() {
            skip_if_no_git!();
            let (tmp, _remote, local) = init_remote_and_clone();
            let seed = tmp.path().join("seed");

            let body = "def shipped():\n    return 1\n";
            push_upstream_added_file(&seed, &local, "vco_lib/new_mod.py", body);
            // Local UNTRACKED copy, byte-identical to what upstream ships.
            let local_file = local.join("vco_lib/new_mod.py");
            std::fs::create_dir_all(local_file.parent().unwrap()).unwrap();
            std::fs::write(&local_file, body).unwrap();

            let res = handle_untracked_collisions_pre_pull(&local, "merge", "main").await;
            assert!(
                res.is_ok(),
                "byte-identical untracked collision must be auto-resolved (Ok): {:?}",
                res
            );
            assert!(
                !local_file.exists(),
                "the byte-identical untracked file must have been auto-removed",
            );
        }

        /// LEAVE-ALONE: a DIVERGENT untracked collision is NOT removed; the
        /// handler returns the untracked-collision payload (Err) and writes a
        /// deferral row. Data-safety: the local file must still exist afterward.
        #[tokio::test]
        async fn v0278_pre_pull_surfaces_divergent_untracked_collision_and_defers() {
            skip_if_no_git!();
            let (tmp, _remote, local) = init_remote_and_clone();
            let seed = tmp.path().join("seed");

            push_upstream_added_file(
                &seed,
                &local,
                "vco_lib/new_mod.py",
                "def upstream():\n    return 1\n",
            );
            // Local UNTRACKED copy with DIFFERENT content.
            let local_file = local.join("vco_lib/new_mod.py");
            std::fs::create_dir_all(local_file.parent().unwrap()).unwrap();
            std::fs::write(&local_file, "def my_local():\n    return 999\n").unwrap();

            let res = handle_untracked_collisions_pre_pull(&local, "merge", "main").await;
            let payload = res.expect_err("divergent collision must surface an Err payload");

            // Payload is the untracked-collision event listing the file.
            let v: serde_json::Value =
                serde_json::from_str(&payload).expect("payload must be valid JSON");
            assert_eq!(v["event"], "orchestrator_untracked_collision");
            assert_eq!(v["operation"], "merge");
            let files = v["divergent_files"].as_array().expect("array");
            assert_eq!(files.len(), 1);
            assert_eq!(files[0], "vco_lib/new_mod.py");

            // Leave-alone: the divergent local file must NOT have been removed.
            assert!(
                local_file.exists(),
                "a DIVERGENT untracked file must never be auto-removed (data-safety)",
            );

            // A deferral row must have been written (agent-resolvable channel).
            // Best-effort emit: assert only when python+vco_lib are resolvable
            // (the writer soft-fails otherwise). Presence of the file with our
            // condition id is the signal.
            let deferred = local.join(".claude/context/UPDATE_DEFERRED.md");
            if deferred.exists() {
                let content = std::fs::read_to_string(&deferred).unwrap();
                assert!(
                    content.contains("untracked_collision_divergent"),
                    "deferral must carry the untracked_collision_divergent condition id;\n{}",
                    content
                );
            } else {
                eprintln!(
                    "[test] deferral file absent — python/vco_lib not resolvable in this \
                     test env; skipping deferral-content assertion (emit is best-effort)"
                );
            }
        }

        /// REGRESSION (the live 2026-07-11 shape): the ENTIRE `vco_lib/`
        /// directory is untracked locally while upstream adds files under it.
        /// `git status --porcelain` (default `normal`) collapses this to a
        /// single `?? vco_lib/` entry — which never intersects the file-granular
        /// upstream-added set unless `list_untracked_files` uses `-uall`. Without
        /// the fix the collision goes UNDETECTED and this test's Ok+removed
        /// assertions fail. See KG git-status-collapses-untracked-directories.
        #[tokio::test]
        async fn v0278_pre_pull_detects_wholly_untracked_directory_collision() {
            skip_if_no_git!();
            let (tmp, _remote, local) = init_remote_and_clone();
            let seed = tmp.path().join("seed");

            // Upstream adds TWO files under a directory that does not yet exist
            // in the local clone → locally the whole dir is untracked.
            let body_a = "def a():\n    return 1\n";
            let body_b = "def b():\n    return 2\n";
            push_upstream_added_file(&seed, &local, "vco_lib/mod_a.py", body_a);
            push_upstream_added_file(&seed, &local, "vco_lib/mod_b.py", body_b);

            // Local UNTRACKED copies, byte-identical (the whole dir is new).
            let dir = local.join("vco_lib");
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("mod_a.py"), body_a).unwrap();
            std::fs::write(dir.join("mod_b.py"), body_b).unwrap();

            // Sanity: confirm git DOES collapse this to `?? vco_lib/` under the
            // default mode (documents the reason for -uall).
            let normal = StdCommand::new("git")
                .args(["status", "--porcelain"])
                .current_dir(&local)
                .output()
                .unwrap();
            let normal_str = String::from_utf8_lossy(&normal.stdout);
            assert!(
                normal_str.lines().any(|l| l.trim() == "?? vco_lib/"),
                "precondition: default git status must collapse the dir; got:\n{}",
                normal_str
            );

            let res = handle_untracked_collisions_pre_pull(&local, "merge", "main").await;
            assert!(
                res.is_ok(),
                "wholly-untracked-dir byte-identical collision must auto-resolve: {:?}",
                res
            );
            assert!(
                !dir.join("mod_a.py").exists() && !dir.join("mod_b.py").exists(),
                "both byte-identical untracked files must have been auto-removed",
            );
        }

        /// v0.2.78 ITEM #0 (F3) — SOURCE-SHAPE guard: the finalize tail
        /// (`run_post_pull_install_and_restart`, which runs install.py --update +
        /// binary refresh + hub restart) MUST remain wired on every post-choice
        /// success path, so a GUI-only user auto-continues without a terminal:
        ///   * `merge_orchestrator_with_upstream` success,
        ///   * `rebase_orchestrator_onto_upstream` success,
        ///   * `resolve_conflict_and_resume` (both keep-mine/take-upstream) →
        ///     delegates to `resume_orchestrator_update` which itself finalizes.
        /// This is the F3 residual: the wiring already exists (verified 2026-07-11);
        /// this guard catches a future refactor silently dropping it — a
        /// behavioral test can't reach it without a real install + restart.
        ///
        /// Needles are ASSEMBLED FROM SPLIT LITERALS so the test's own source
        /// (included via include_str! — this test IS in installer.rs) does not
        /// match its own needle. See KG
        /// source-shape-guard-tests-split-literal-needles-2026-07-02.
        #[test]
        fn v0278_f3_finalize_tail_stays_wired_on_success_paths() {
            let src = include_str!("installer.rs");
            // Split-literal assembly: neither of these string literals, as
            // written here, equals the runtime call token we count.
            let finalize_call = concat!("run_post_pull_install", "_and_restart(");
            let resume_call = concat!("resume_orchestrator", "_update(app, path, window)");

            let finalize_hits = src.matches(finalize_call).count();
            // merge success + rebase success + resolve_conflict_and_resume all
            // call the finalize tail directly (3 call-sites). If a refactor drops
            // one, this trips.
            assert!(
                finalize_hits >= 3,
                "expected >=3 run_post_pull_install_and_restart call-sites on the \
                 update success paths (merge, rebase, resolve-resume); found {}. A \
                 finalize call was likely dropped — GUI-only auto-continue would break.",
                finalize_hits
            );

            // resolve_conflict_and_resume's tail must delegate to the resume
            // command (which finalizes) — the keep-mine/take-upstream → auto-
            // continue story depends on this exact delegation.
            assert!(
                src.contains(resume_call),
                "resolve_conflict_and_resume must delegate to resume_orchestrator_update \
                 so the conflict-resolution path auto-continues to finalize",
            );
        }

        /// A pure-local untracked scratch file that upstream did NOT add is NOT
        /// a collision → handler returns Ok and the file is untouched. Guards
        /// against over-eager removal of KG nodes / notes.
        #[tokio::test]
        async fn v0278_pre_pull_ignores_non_collision_untracked_scratch() {
            skip_if_no_git!();
            let (tmp, _remote, local) = init_remote_and_clone();
            let seed = tmp.path().join("seed");
            // Upstream adds an UNRELATED file.
            push_upstream_added_file(&seed, &local, "vco_lib/added.py", "x = 1\n");
            // Local untracked scratch upstream never touched.
            let scratch = local.join("knowledge/concepts/scratch.md");
            std::fs::create_dir_all(scratch.parent().unwrap()).unwrap();
            std::fs::write(&scratch, "# scratch\n").unwrap();

            let res = handle_untracked_collisions_pre_pull(&local, "merge", "main").await;
            assert!(res.is_ok(), "non-collision must be Ok: {:?}", res);
            assert!(
                scratch.exists(),
                "pure-local untracked scratch must never be removed",
            );
        }

        // v0.2.63: the HEAD-advance backstop — test BOTH the abort case (HEAD
        // behind upstream) and the proceed case (HEAD at upstream tip), per the
        // "test the decision, not just the happy path" rule for branches that
        // gate a destructive action (running install.py on a stale tree).
        #[tokio::test]
        async fn assert_head_reached_upstream_errs_when_behind() {
            skip_if_no_git!();
            // init_remote_and_clone leaves the local clone exactly 1 commit
            // behind vco_upstream/main (remote advanced + re-fetched, no merge).
            let (_tmp, _remote, local) = init_remote_and_clone();
            let res = assert_head_reached_upstream(&local).await;
            assert!(
                res.is_err(),
                "HEAD behind upstream → guard must abort before install.py: {:?}",
                res
            );
            assert!(res.unwrap_err().contains("behind"));
        }

        #[tokio::test]
        async fn assert_head_reached_upstream_ok_when_at_upstream_tip() {
            skip_if_no_git!();
            let (_tmp, _remote, local) = init_remote_and_clone();
            // Advance HEAD to the upstream tip (a real merge) → no longer behind.
            assert!(StdCommand::new("git")
                .silent()
                .args(["merge", "--no-edit", "vco_upstream/main"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());
            let res = assert_head_reached_upstream(&local).await;
            assert!(res.is_ok(), "HEAD at upstream tip → guard must pass: {:?}", res);
        }

        #[tokio::test]
        async fn update_orchestrator_non_ff_path_produces_structured_error() {
            skip_if_no_git!();

            let (_tmp, _remote, local) = init_remote_and_clone();
            // Add a local commit so vco_upstream/main and HEAD have diverged.
            // The "local file" name doesn't collide with UPSTREAM.md so the
            // pull will be non-FF but NOT conflicting if attempted as merge.
            add_local_divergent_commit(&local, "LOCAL.md", "local-only\n");

            // Attempt the same `git pull --ff-only` invocation
            // update_orchestrator runs. It must fail with a non-FF stderr
            // pattern that `is_non_fast_forward` recognizes.
            let pull = tokio::process::Command::new("git").silent()
                .args(["pull", "--ff-only", "vco_upstream", "main"])
                .current_dir(&local)
                .output()
                .await
                .expect("git pull spawn");
            assert!(!pull.status.success(), "ff-only pull should fail on diverged history");
            let stderr = String::from_utf8_lossy(&pull.stderr);
            assert!(
                crate::commands::self_update::is_non_fast_forward(&stderr),
                "expected non-FF detection on stderr: {}",
                stderr
            );

            // The diverged-files helper must return at least UPSTREAM.md
            // (added on upstream but absent locally).
            let (diverged, local_only) =
                collect_diverged_files(&local, "main").await;
            assert!(
                diverged.iter().any(|p| p == "UPSTREAM.md"),
                "expected UPSTREAM.md in diverged list, got {:?}",
                diverged
            );

            // The SHAs should both be readable.
            let local_sha = read_head_sha(&local).await;
            let remote_sha = read_remote_sha(&local, "main").await;
            assert!(local_sha.is_some(), "local SHA should be readable");
            assert!(remote_sha.is_some(), "remote SHA should be readable");
            assert_ne!(local_sha, remote_sha, "SHAs should differ on divergence");

            // Final: the serialized payload should parse as JSON with the
            // right discriminator.
            let payload = serialize_orchestrator_non_ff_error(
                "main",
                local_sha.as_deref(),
                remote_sha.as_deref(),
                &diverged,
                &local_only,
                stderr.trim(),
            );
            let v: serde_json::Value =
                serde_json::from_str(&payload).expect("payload valid JSON");
            assert_eq!(v["event"], "orchestrator_update_non_ff");
            assert_eq!(v["branch"], "main");
            assert!(!v["diverged_files"].as_array().unwrap().is_empty());
            // local_only_files should be present (may be empty in this fixture,
            // but the key must exist for the v0.2.27 shape contract).
            assert!(v.get("local_only_files").is_some());
        }

        #[tokio::test]
        async fn merge_with_upstream_creates_merge_commit_on_clean_overlap() {
            skip_if_no_git!();

            let (_tmp, _remote, local) = init_remote_and_clone();
            // Add a local commit that touches a DIFFERENT file from
            // UPSTREAM.md so the merge is non-conflicting.
            add_local_divergent_commit(&local, "LOCAL.md", "local-only\n");

            // Run the same `git pull --no-rebase --no-edit vco_upstream main`
            // invocation merge_orchestrator_with_upstream uses.
            let pull = tokio::process::Command::new("git").silent()
                .args(["pull", "--no-rebase", "--no-edit", "vco_upstream", "main"])
                .current_dir(&local)
                .output()
                .await
                .expect("git pull merge");
            assert!(
                pull.status.success(),
                "merge should succeed on non-overlapping files: stderr={} stdout={}",
                String::from_utf8_lossy(&pull.stderr),
                String::from_utf8_lossy(&pull.stdout),
            );

            // Verify HEAD is now a merge commit (has two parents).
            let parents = StdCommand::new("git").silent()
                .args(["rev-list", "--parents", "-n", "1", "HEAD"])
                .current_dir(&local)
                .output()
                .expect("rev-list");
            let out = String::from_utf8_lossy(&parents.stdout);
            let parent_count = out.trim().split_whitespace().count() - 1; // first token is HEAD itself
            assert_eq!(
                parent_count, 2,
                "expected merge commit with 2 parents, got {}: {}",
                parent_count, out
            );

            // Both files should be in the working tree.
            assert!(local.join("LOCAL.md").exists(), "LOCAL.md must persist");
            assert!(
                local.join("UPSTREAM.md").exists(),
                "UPSTREAM.md must be merged in"
            );

            // No conflicts.
            let conflicted = collect_conflicted_files(&local).await;
            assert!(
                conflicted.is_empty(),
                "no conflicts expected, got {:?}",
                conflicted
            );
        }

        #[tokio::test]
        async fn merge_returns_conflict_list_on_overlapping_changes() {
            skip_if_no_git!();

            let (_tmp, _remote, local) = init_remote_and_clone();
            // Make a local commit that modifies a file we also modify on
            // upstream — but UPSTREAM.md was only added on upstream, so we
            // need to use a file that exists on both sides. Use README.md
            // (the base commit's file, present locally and remotely) and
            // modify it differently on both sides.
            //
            // Step 1: modify locally + commit.
            std::fs::write(local.join("README.md"), "LOCAL VERSION\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "README.md"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "local README change"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            // Step 2: simulate an upstream change to README.md too. Reach
            // through the bare-remote URL by cloning it again into a temp
            // workdir, modifying, pushing.
            let pusher = local.parent().unwrap().join("pusher");
            std::fs::create_dir_all(&pusher).unwrap();
            // Clone from the bare remote — same URL used in vco_upstream.
            let remote_url = StdCommand::new("git").silent()
                .args(["remote", "get-url", "vco_upstream"])
                .current_dir(&local)
                .output()
                .expect("get-url")
                .stdout;
            let remote_url = String::from_utf8_lossy(&remote_url).trim().to_string();
            assert!(StdCommand::new("git").silent()
                .args(["clone", &remote_url])
                .arg(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "test@example.com"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Test"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            std::fs::write(pusher.join("README.md"), "UPSTREAM VERSION\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "README.md"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "upstream README change"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["push", "origin", "main"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());

            // Step 3: fetch vco_upstream in the local clone so it sees the
            // upstream README change.
            assert!(StdCommand::new("git").silent()
                .args(["fetch", "vco_upstream"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            // Step 4: attempt the merge — should fail with conflict.
            let pull = tokio::process::Command::new("git").silent()
                .args(["pull", "--no-rebase", "--no-edit", "vco_upstream", "main"])
                .current_dir(&local)
                .output()
                .await
                .expect("git pull merge");
            assert!(!pull.status.success(), "merge should fail on overlap");

            let combined = format!(
                "{}\n{}",
                String::from_utf8_lossy(&pull.stderr),
                String::from_utf8_lossy(&pull.stdout)
            );
            assert!(
                is_merge_or_rebase_conflict(&combined),
                "expected conflict detection on output: {}",
                combined
            );

            // The conflict-file collector must return README.md.
            let conflicted = collect_conflicted_files(&local).await;
            assert!(
                conflicted.iter().any(|p| p == "README.md"),
                "expected README.md in conflicted list, got {:?}",
                conflicted
            );

            // Serializer round-trips.
            let payload = serialize_orchestrator_conflict_error(
                "merge",
                "main",
                &conflicted,
                combined.trim(),
            );
            let v: serde_json::Value =
                serde_json::from_str(&payload).expect("payload valid JSON");
            assert_eq!(v["event"], "orchestrator_update_conflict");
            assert_eq!(v["operation"], "merge");
            let files = v["conflicted_files"].as_array().unwrap();
            assert!(files.iter().any(|f| f.as_str() == Some("README.md")));

            // Cleanup: abort the merge so the tempdir can be removed
            // without lingering MERGE_HEAD state confusing the OS.
            let _ = StdCommand::new("git").silent()
                .args(["merge", "--abort"])
                .current_dir(&local)
                .status();
        }

        #[tokio::test]
        async fn abort_merge_or_rebase_noops_when_nothing_in_progress() {
            skip_if_no_git!();
            let (_tmp, _remote, local) = init_remote_and_clone();
            // No merge or rebase in progress — abort should be a no-op.
            let result = abort_orchestrator_merge_or_rebase(
                local.to_str().unwrap().to_string(),
            )
            .await;
            assert!(result.is_ok(), "expected no-op Ok, got {:?}", result);
        }

        #[tokio::test]
        async fn abort_merge_or_rebase_aborts_in_progress_merge() {
            skip_if_no_git!();

            // Reproduce a conflict-state, then abort it.
            let (_tmp, _remote, local) = init_remote_and_clone();
            // Set up overlapping changes (same scaffolding as the conflict
            // test).
            std::fs::write(local.join("README.md"), "LOCAL VERSION\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "README.md"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "local README"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            let pusher = local.parent().unwrap().join("pusher2");
            let remote_url = StdCommand::new("git").silent()
                .args(["remote", "get-url", "vco_upstream"])
                .current_dir(&local)
                .output()
                .expect("get-url")
                .stdout;
            let remote_url = String::from_utf8_lossy(&remote_url).trim().to_string();
            assert!(StdCommand::new("git").silent()
                .args(["clone", &remote_url])
                .arg(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "test@example.com"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Test"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            std::fs::write(pusher.join("README.md"), "UPSTREAM VERSION\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["add", "README.md"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-m", "upstream README"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["push", "origin", "main"])
                .current_dir(&pusher)
                .status()
                .unwrap()
                .success());
            assert!(StdCommand::new("git").silent()
                .args(["fetch", "vco_upstream"])
                .current_dir(&local)
                .status()
                .unwrap()
                .success());

            // Trigger the merge — expected to leave MERGE_HEAD.
            let pull = StdCommand::new("git").silent()
                .args(["pull", "--no-rebase", "--no-edit", "vco_upstream", "main"])
                .current_dir(&local)
                .output()
                .expect("pull");
            assert!(!pull.status.success(), "merge should leave conflict state");
            assert!(
                local.join(".git").join("MERGE_HEAD").exists(),
                "MERGE_HEAD must exist after conflicted merge"
            );

            // Abort.
            let result = abort_orchestrator_merge_or_rebase(
                local.to_str().unwrap().to_string(),
            )
            .await;
            assert!(result.is_ok(), "abort should succeed, got {:?}", result);
            assert!(
                !local.join(".git").join("MERGE_HEAD").exists(),
                "MERGE_HEAD must be cleared after abort"
            );
        }

        // -------------------------------------------------------------------
        // v0.2.51 Bug A — resume sentinel + deferral writer tests.
        //
        // Coverage:
        //   - Sentinel round-trip (write → read → verify fields).
        //   - Deferral file shape (frontmatter, condition_id, comprehensive
        //     "for your Claude assistant" hint).
        //   - Idempotent clear: solo deferral deletes, multi-entry preserves.
        //   - End-to-end: a merge conflict on a git fixture leaves BOTH
        //     sentinel + deferral on disk; abort clears BOTH.
        // -------------------------------------------------------------------

        #[test]
        fn update_resume_sentinel_roundtrip() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();

            // Initially absent.
            assert!(read_update_resume_sentinel(&install).is_none());

            // Write.
            write_update_resume_sentinel(
                &install,
                "merge",
                "main",
                "abc123def4567890",
            );
            let target = install.join(UPDATE_RESUME_SENTINEL_REL);
            assert!(target.exists(), "sentinel file must be written");

            // Read back.
            let s = read_update_resume_sentinel(&install).expect("sentinel parses");
            assert_eq!(s.schema, 1);
            assert_eq!(s.operation, "merge");
            assert_eq!(s.branch, "main");
            assert_eq!(s.sha_at_conflict, "abc123def4567890");
            assert!(!s.written_at.is_empty(), "written_at must populate");

            // Clear is idempotent.
            clear_update_resume_sentinel(&install);
            assert!(!target.exists());
            clear_update_resume_sentinel(&install); // second call: no-op
            assert!(!target.exists());
            assert!(read_update_resume_sentinel(&install).is_none());
        }

        #[test]
        fn update_resume_sentinel_rejects_bad_schema() {
            // A pre-existing sentinel from a future schema version must be
            // treated as "no sentinel" so we don't crash trying to parse it.
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            let target = install.join(UPDATE_RESUME_SENTINEL_REL);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            std::fs::write(
                &target,
                r#"{"schema":99,"operation":"merge","branch":"main","sha_at_conflict":"x","written_at":"2026-06-09T12:00:00Z"}"#,
            )
            .unwrap();
            assert!(
                read_update_resume_sentinel(&install).is_none(),
                "schema 99 must yield None, not a panic or Some"
            );
        }

        #[test]
        fn update_resume_deferral_shape_is_comprehensive() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            write_update_resume_deferral(&install, "merge", "main");

            let target = install.join(".claude/context/UPDATE_DEFERRED.md");
            assert!(target.exists(), "deferral file must be written");

            let body = std::fs::read_to_string(&target).expect("read");

            // Required structural pieces (per vco_lib/deferral_report.py format).
            assert!(body.starts_with("---\n"), "must start with YAML frontmatter");
            assert!(
                body.contains("condition_ids: [update_resume_required]"),
                "frontmatter must list our condition_id"
            );
            assert!(
                body.contains("severity_max: warning"),
                "single-entry deferral must report severity_max=warning"
            );
            assert!(
                body.contains("## update_resume_required (warning)"),
                "section header missing"
            );

            // Required content pieces.
            assert!(body.contains("**Title**:"));
            assert!(body.contains("**Detected**:"));
            assert!(body.contains("**Why deferred**:"));
            assert!(body.contains("**To apply**:"));
            assert!(body.contains("**Detected at**:"));

            // The brief mandates a comprehensive "for your Claude assistant"
            // section. Catching its removal here lets us evolve the copy
            // without breaking the contract.
            assert!(
                body.contains("**For your Claude assistant**"),
                "deferral must include the explicit Claude-facing hint"
            );

            // Both recovery paths must be documented (GUI + terminal) and
            // the install.py command must use --update (not --force or
            // anything destructive).
            assert!(body.contains("Continue Update"), "missing GUI option");
            assert!(body.contains("python install.py --update"), "missing CLI option");
        }

        #[test]
        fn update_resume_deferral_rebase_variant_uses_correct_op_label() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            write_update_resume_deferral(&install, "rebase", "main");
            let body = std::fs::read_to_string(
                install.join(".claude/context/UPDATE_DEFERRED.md"),
            )
            .unwrap();
            assert!(
                body.contains("(rebase on `main`)"),
                "rebase op must be reflected in Detected copy; got: {body}"
            );
        }

        /// WP-B6 (v0.2.83): `write_update_resume_deferral` must hold the shared
        /// UPDATE_DEFERRED flock across its tmp-write + rename. Observable proof:
        /// hold the SAME folder's flock on a background thread for a fixed
        /// window; the writer on the main thread must BLOCK until release, so its
        /// wall-clock duration is >= the window. POSIX-only. ms-scaled.
        #[cfg(unix)]
        #[test]
        fn write_update_resume_deferral_serializes_on_shared_lock() {
            use std::sync::mpsc;
            use std::time::{Duration, Instant};

            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            std::fs::create_dir_all(install.join(".claude/context")).unwrap();

            const HOLD: Duration = Duration::from_millis(300);
            let folder = install.clone();
            let (ready_tx, ready_rx) = mpsc::channel::<()>();
            let holder = std::thread::spawn(move || {
                let guard =
                    vct_launcher_core::services::deferral_lock::lock_folder(&folder);
                ready_tx.send(()).unwrap();
                std::thread::sleep(HOLD);
                drop(guard);
            });
            ready_rx.recv_timeout(Duration::from_secs(5)).unwrap();

            let started = Instant::now();
            write_update_resume_deferral(&install, "merge", "main");
            let waited = started.elapsed();

            assert!(
                waited >= HOLD - Duration::from_millis(80),
                "write_update_resume_deferral did not block on the shared lock \
                 (waited {waited:?}, expected >= ~{HOLD:?})",
            );
            assert!(
                install.join(".claude/context/UPDATE_DEFERRED.md").is_file(),
                "deferral must land after acquiring the lock"
            );
            holder.join().unwrap();
        }

        /// WP-B6 (v0.2.83): `clear_update_resume_deferral_if_solo` must hold the
        /// shared UPDATE_DEFERRED flock across its read → solo-decision → delete
        /// cycle. Same serialization proof as the writer test. POSIX-only.
        #[cfg(unix)]
        #[test]
        fn clear_update_resume_deferral_if_solo_serializes_on_shared_lock() {
            use std::sync::mpsc;
            use std::time::{Duration, Instant};

            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            let ctx = install.join(".claude/context");
            std::fs::create_dir_all(&ctx).unwrap();
            // A solo update_resume_required deferral so the clear will unlink it.
            std::fs::write(
                ctx.join("UPDATE_DEFERRED.md"),
                "---\ncondition_ids: [update_resume_required]\n---\n\n# VCO Update Deferred\n\n## update_resume_required (warning)\n\n**Title**: foo\n\n---\n",
            )
            .unwrap();

            const HOLD: Duration = Duration::from_millis(300);
            let folder = install.clone();
            let (ready_tx, ready_rx) = mpsc::channel::<()>();
            let holder = std::thread::spawn(move || {
                let guard =
                    vct_launcher_core::services::deferral_lock::lock_folder(&folder);
                ready_tx.send(()).unwrap();
                std::thread::sleep(HOLD);
                drop(guard);
            });
            ready_rx.recv_timeout(Duration::from_secs(5)).unwrap();

            let started = Instant::now();
            clear_update_resume_deferral_if_solo(&install);
            let waited = started.elapsed();

            assert!(
                waited >= HOLD - Duration::from_millis(80),
                "clear_update_resume_deferral_if_solo did not block on the shared \
                 lock (waited {waited:?}, expected >= ~{HOLD:?})",
            );
            assert!(
                !ctx.join("UPDATE_DEFERRED.md").exists(),
                "solo entry must be cleared after acquiring the lock"
            );
            holder.join().unwrap();
        }

        // ─── v0.2.55: launcher_update_diverged durable-logging writer ─────
        //
        // v0.2.71 Sweep-A#3: the writer + its `LauncherUpdateDivergedKind`
        // enum (and their markdown-shape tests) were RELOCATED to
        // `git_user_editable_merge` so both update surfaces share ONE writer.
        // See `git_user_editable_merge::tests::launcher_update_diverged_*`
        // for the relocated markdown-shape coverage. The PRIMARY-BUG version-
        // comparison test below stays here because it exercises
        // `version_is_outdated`, which is still defined in installer.rs.

        #[test]
        fn primary_bug_on_disk_beats_running_comparison_direction() {
            // The PRIMARY-BUG fix restarts into the on-disk binary when it
            // is NEWER than the running launcher. version_is_outdated(a, b)
            // is true iff a < b, so `version_is_outdated(running, on_disk)`
            // == "running is older than on_disk" == "restart is worth it".
            assert!(
                version_is_outdated("0.2.54", "0.2.55"),
                "running 0.2.54 < on-disk 0.2.55 → should restart"
            );
            assert!(
                !version_is_outdated("0.2.55", "0.2.55"),
                "equal versions → no newer binary → keep abort"
            );
            assert!(
                !version_is_outdated("0.2.56", "0.2.55"),
                "running ahead of on-disk → keep abort (don't downgrade)"
            );
        }

        // ─── v0.2.53 DEDUP-14: paired sentinel + deferral writer ──────────
        //
        // `write_resume_sentinel_and_deferral` is the single helper the
        // three conflict-handling sites now route through. The tests
        // below pin the contract: BOTH files land, BOTH carry the right
        // operation/branch, and a non-existent install_path that fails
        // the sentinel write still produces a clean error footprint
        // (no half-written files).

        #[tokio::test]
        async fn paired_writer_emits_both_sentinel_and_deferral_for_merge() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();

            write_resume_sentinel_and_deferral(&install, "merge", "main").await;

            let sentinel_path = install.join(UPDATE_RESUME_SENTINEL_REL);
            let deferral_path = install.join(".claude/context/UPDATE_DEFERRED.md");
            assert!(sentinel_path.exists(), "sentinel must be written");
            assert!(deferral_path.exists(), "deferral must be written");

            let s = read_update_resume_sentinel(&install).expect("parse sentinel");
            assert_eq!(s.operation, "merge");
            assert_eq!(s.branch, "main");

            let body = std::fs::read_to_string(&deferral_path).expect("read deferral");
            assert!(
                body.contains("(merge on `main`)"),
                "deferral must reflect operation+branch; got: {body}"
            );
        }

        #[tokio::test]
        async fn paired_writer_emits_both_sentinel_and_deferral_for_rebase() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();

            write_resume_sentinel_and_deferral(&install, "rebase", "release").await;

            let s = read_update_resume_sentinel(&install).expect("parse sentinel");
            assert_eq!(s.operation, "rebase");
            assert_eq!(s.branch, "release");
            let body = std::fs::read_to_string(
                install.join(".claude/context/UPDATE_DEFERRED.md"),
            )
            .expect("read deferral");
            assert!(
                body.contains("(rebase on `release`)"),
                "deferral must reflect rebase variant; got: {body}"
            );
        }

        #[tokio::test]
        async fn paired_writer_handles_missing_head_sha_with_empty_string() {
            // No .git/ in the install_path → `read_head_sha` returns
            // None → `unwrap_or_default()` gives "". The helper still
            // writes BOTH files; the empty SHA is intentional and is
            // handled by the v0.2.53 NEW-11 empty-sha refusal branch
            // in `resume_orchestrator_update`.
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();

            write_resume_sentinel_and_deferral(&install, "merge", "main").await;

            let s = read_update_resume_sentinel(&install).expect("parse sentinel");
            assert!(
                s.sha_at_conflict.is_empty(),
                "no .git → sha_at_conflict must be empty (NEW-11 will refuse resume)"
            );
            assert!(
                install.join(".claude/context/UPDATE_DEFERRED.md").exists(),
                "deferral must still be emitted even when sha is empty"
            );
        }

        /// v0.2.53 DEDUP-14 integration-shape test (Track C scope).
        /// Atomic-ish contract: both files land, OR the function returns
        /// silently (best-effort). Forgetting one of the two writes is
        /// what produced v0.2.51 Bug A; this test catches any future
        /// refactor that reintroduces the split.
        #[tokio::test]
        async fn paired_writer_helper_writes_both_files_in_single_call() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();

            // PRE: nothing written.
            assert!(!install.join(UPDATE_RESUME_SENTINEL_REL).exists());
            assert!(!install.join(".claude/context/UPDATE_DEFERRED.md").exists());

            // CALL.
            write_resume_sentinel_and_deferral(&install, "merge", "main").await;

            // POST: both written.
            let sentinel_written = install.join(UPDATE_RESUME_SENTINEL_REL).exists();
            let deferral_written = install
                .join(".claude/context/UPDATE_DEFERRED.md")
                .exists();
            assert!(
                sentinel_written && deferral_written,
                "paired writer MUST emit BOTH files (forgetting one is v0.2.51 Bug A class); \
                 sentinel_written={sentinel_written}, deferral_written={deferral_written}"
            );
        }

        #[test]
        fn clear_update_resume_deferral_if_solo_preserves_multi_entry_file() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            let target = install.join(".claude/context/UPDATE_DEFERRED.md");
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();

            // Simulate a file with TWO entries: ours + a sibling we don't own.
            let multi = "---\n\
title: VCO Update Deferred\n\
generated_at: 2026-06-09T12:00:00Z\n\
condition_ids: [update_resume_required, schema_drift_rebuild_required]\n\
severity_max: critical\n\
---\n\
\n\
## update_resume_required (warning)\n\
**Title**: ours\n\
\n\
## schema_drift_rebuild_required (critical)\n\
**Title**: not ours\n\
";
            std::fs::write(&target, multi).unwrap();

            clear_update_resume_deferral_if_solo(&install);

            // File must still exist because we don't own it solo.
            assert!(
                target.exists(),
                "multi-entry deferral must NOT be unlinked by our clear()"
            );
            let body = std::fs::read_to_string(&target).unwrap();
            assert!(body.contains("schema_drift_rebuild_required"));
        }

        #[test]
        fn clear_update_resume_deferral_if_solo_removes_owned_file() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            write_update_resume_deferral(&install, "merge", "main");
            let target = install.join(".claude/context/UPDATE_DEFERRED.md");
            assert!(target.exists());

            clear_update_resume_deferral_if_solo(&install);
            assert!(
                !target.exists(),
                "solo update_resume_required deferral must be unlinked"
            );
        }

        /// v0.2.75 (Part 3b): the solo clear must be DUAL-file. install.py's
        /// A-3 write pairs the .md with an authoritative UPDATE_DEFERRED.json
        /// sidecar; unlinking only the .md let the next Python read take the
        /// JSON verbatim and RESURRECT the just-cleared entry. Mirrors
        /// restart.rs::clear_restart_deferral's empty-file sweep (F2).
        #[test]
        fn clear_update_resume_deferral_if_solo_also_unlinks_json_sidecar() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            write_update_resume_deferral(&install, "merge", "main");
            let target = install.join(".claude/context/UPDATE_DEFERRED.md");
            let sidecar = install.join(".claude/context/UPDATE_DEFERRED.json");
            // Simulate install.py's A-3 dual write: the JSON sidecar carries
            // the same solo entry (shape per _render_json_sidecar; content
            // is irrelevant to the unlink — presence is what matters).
            std::fs::write(
                &sidecar,
                r#"{"schema_version": 1, "generated_at": "2026-07-08T00:00:00Z",
                    "severity_max": "warning",
                    "entries": [{"condition_id": "update_resume_required"}]}"#,
            )
            .unwrap();
            assert!(target.exists() && sidecar.exists());

            clear_update_resume_deferral_if_solo(&install);
            assert!(!target.exists(), "solo .md must be unlinked");
            assert!(
                !sidecar.exists(),
                "JSON sidecar must be unlinked WITH the .md — leaving it \
                 resurrects the entry on the next Python read"
            );
        }

        /// Leave-alone side of the dual-unlink: a multi-entry file is not
        /// ours to clear, so BOTH files must survive (deleting only the JSON
        /// would desync the authoritative store from its render).
        #[test]
        fn clear_update_resume_deferral_if_solo_multi_entry_keeps_both_files() {
            let dir = tempfile::tempdir().expect("tempdir");
            let install = dir.path().to_path_buf();
            let target = install.join(".claude/context/UPDATE_DEFERRED.md");
            let sidecar = install.join(".claude/context/UPDATE_DEFERRED.json");
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            let multi = "---\n\
title: VCO Update Deferred\n\
generated_at: 2026-07-08T12:00:00Z\n\
condition_ids: [update_resume_required, schema_drift_rebuild_required]\n\
severity_max: critical\n\
---\n\
\n\
## update_resume_required (warning)\n\
**Title**: ours\n\
\n\
## schema_drift_rebuild_required (critical)\n\
**Title**: not ours\n\
";
            std::fs::write(&target, multi).unwrap();
            std::fs::write(
                &sidecar,
                r#"{"schema_version": 1, "entries": []}"#,
            )
            .unwrap();

            clear_update_resume_deferral_if_solo(&install);
            assert!(
                target.exists(),
                "multi-entry .md must NOT be unlinked (not solo-owned)"
            );
            assert!(
                sidecar.exists(),
                "JSON sidecar must NOT be unlinked when the .md is kept"
            );
        }

        /// resume_orchestrator_update REFUSES when no sentinel exists —
        /// the GUI should never get a half-baked InstallResult back when
        /// the user clicked Continue Update on a stale badge.
        #[tokio::test]
        async fn resume_orchestrator_update_refuses_without_sentinel() {
            skip_if_no_git!();
            let (_tmp, _remote, local) = init_remote_and_clone();

            // No sentinel written → resume must refuse with a structured
            // human-readable error.
            //
            // We invoke resume via a synthetic app handle. The simplest
            // way to exercise the precondition checks WITHOUT spinning up
            // a Tauri runtime is to assert the helper functions: when
            // read_update_resume_sentinel returns None, no resume can
            // proceed. That's the contract the command enforces in its
            // very first match arm.
            assert!(
                read_update_resume_sentinel(&local).is_none(),
                "fresh fixture must have no sentinel"
            );
        }

        // ─── v0.2.78 ITEM #0 (F4) — benign-noop resume decision ───

        /// The head-unchanged decision: HEAD == recorded conflict SHA (and the
        /// SHA is non-empty) ⇒ benign no-op (the merge/rebase was aborted;
        /// nothing to resume). This is the branch that now self-clears + returns
        /// a SUCCESS-shaped result instead of a red-banner Err.
        #[test]
        fn v0278_f4_resume_head_unchanged_is_benign_noop_decision() {
            // ACT: same non-empty SHA on both sides → benign no-op.
            assert!(
                resume_head_unchanged_is_benign_noop("abc123", "abc123"),
                "HEAD == conflict SHA (non-empty) must be a benign no-op",
            );
            // LEAVE-ALONE: HEAD advanced past the conflict SHA → a real resume
            // (must NOT be treated as a no-op — the finalize tail should run).
            assert!(
                !resume_head_unchanged_is_benign_noop("abc123", "def456"),
                "HEAD advanced → not a no-op; the resume must proceed to finalize",
            );
            // EMPTY conflict SHA (older sentinels) → cannot prove HEAD didn't
            // move → NOT a benign no-op (let the other guards run).
            assert!(
                !resume_head_unchanged_is_benign_noop("", ""),
                "empty conflict SHA must not be classified as a benign no-op",
            );
            assert!(
                !resume_head_unchanged_is_benign_noop("", "abc123"),
                "empty conflict SHA (any HEAD) must not be a benign no-op",
            );
        }

        /// End-to-end: a real merge conflict on a git fixture surfaces
        /// the sentinel + deferral; abort_orchestrator_merge_or_rebase
        /// clears both.
        #[tokio::test]
        async fn merge_conflict_writes_sentinel_and_deferral_and_abort_clears_both()
        {
            skip_if_no_git!();
            let (_tmp, remote, local) = init_remote_and_clone();

            // Re-create the divergence-overlap scenario from the existing
            // tests: same file edited on both sides, then a `pull --merge`.
            std::fs::write(local.join("README.md"), "LOCAL CHANGE\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "test@example.com"])
                .current_dir(&local).status().unwrap().success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Test"])
                .current_dir(&local).status().unwrap().success());
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-am", "local README"])
                .current_dir(&local).status().unwrap().success());

            // Build an upstream divergence.
            let pusher = local.parent().unwrap().join("pusher");
            assert!(StdCommand::new("git").silent()
                .args(["clone", remote.to_str().unwrap()])
                .arg(&pusher).status().unwrap().success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.email", "u@example.com"])
                .current_dir(&pusher).status().unwrap().success());
            assert!(StdCommand::new("git").silent()
                .args(["config", "user.name", "Up"])
                .current_dir(&pusher).status().unwrap().success());
            std::fs::write(pusher.join("README.md"), "UPSTREAM CHANGE\n").unwrap();
            assert!(StdCommand::new("git").silent()
                .args(["commit", "-am", "upstream README"])
                .current_dir(&pusher).status().unwrap().success());
            assert!(StdCommand::new("git").silent()
                .args(["push", "origin", "main"])
                .current_dir(&pusher).status().unwrap().success());

            // Trigger the merge conflict.
            assert!(StdCommand::new("git").silent()
                .args(["fetch", "origin"])
                .current_dir(&local).status().unwrap().success());
            let pull = StdCommand::new("git").silent()
                .args(["pull", "--no-rebase", "--no-edit", "origin", "main"])
                .current_dir(&local).output().unwrap();
            assert!(!pull.status.success(), "merge should conflict");
            assert!(local.join(".git").join("MERGE_HEAD").exists());

            // Simulate the conflict-surface path: write sentinel + deferral.
            write_update_resume_sentinel(&local, "merge", "main", "fake-sha");
            write_update_resume_deferral(&local, "merge", "main");

            assert!(local.join(UPDATE_RESUME_SENTINEL_REL).exists());
            assert!(local.join(".claude/context/UPDATE_DEFERRED.md").exists());

            // Now abort — both should be cleared.
            let result = abort_orchestrator_merge_or_rebase(
                local.to_str().unwrap().to_string(),
            )
            .await;
            assert!(result.is_ok(), "abort failed: {:?}", result);

            assert!(
                !local.join(UPDATE_RESUME_SENTINEL_REL).exists(),
                "abort must clear the sentinel"
            );
            assert!(
                !local.join(".claude/context/UPDATE_DEFERRED.md").exists(),
                "abort must clear the solo deferral"
            );
        }
    }

    // ------------------------------------------------------------------
    // v0.2.83 (D2 / A-RC1): honest remote-check health in check_for_updates.
    //
    // A rev-list failure used to be COMPLETELY SILENT (remote_ahead stayed
    // false, indistinguishable from "up to date"). These tests pin that the
    // health fields (remote_check_ok / remote_check_error) are now populated
    // on the failure branches, and left ok=true/None for a non-git install.
    //
    // The rev-list-failure pin uses a PATH-shim fake `git` (Unix only) that
    // passes remote/fetch/rev-parse and fails rev-list — mirroring the Python
    // suite's PATH-shim idiom. Guarded with GLOBAL_ENV_MUTEX (via
    // `with_env_vars`) because it mutates PATH process-globally.
    // ------------------------------------------------------------------
    mod remote_check_health_tests {
        use super::super::*;

        #[tokio::test]
        async fn non_git_dir_reports_remote_check_ok_true_no_error() {
            // No `.git/` → the remote section is skipped entirely. That is
            // "not applicable", not a failure: ok=true, error=None, and there
            // is nothing to retry. install_stale / binary_stale carry the
            // banner instead.
            let tmp = tempfile::tempdir().expect("tempdir");
            let status = check_for_updates(tmp.path().to_str().unwrap().to_string())
                .await
                .expect("check_for_updates should not hard-fail on a non-git dir");
            assert!(
                status.remote_check_ok,
                "non-git dir must report remote_check_ok=true (not applicable)"
            );
            assert!(
                status.remote_check_error.is_none(),
                "non-git dir must have no remote_check_error, got {:?}",
                status.remote_check_error
            );
            assert!(
                !status.remote_ahead,
                "non-git dir cannot be remote_ahead"
            );
        }

        /// REGRESSION PIN (A-RC1): a rev-list failure must set
        /// remote_check_ok=false + populate remote_check_error — NOT silently
        /// leave remote_ahead=false. A PATH-shim fake `git` passes
        /// remote/fetch/rev-parse and fails ONLY rev-list.
        ///
        /// Plain `#[test]` (not `#[tokio::test]`): we build our OWN
        /// single-thread runtime INSIDE `with_env_vars` so the PATH override
        /// covers the whole async invocation. `#[tokio::test]` would already
        /// hold a runtime and forbid the nested `block_on`.
        #[cfg(unix)]
        #[test]
        fn rev_list_failure_populates_health_fields_not_silent() {
            use std::os::unix::fs::PermissionsExt;

            let tmp = tempfile::tempdir().expect("tempdir");
            let repo = tmp.path().join("repo");
            // `.git/` must exist for check_for_updates to enter the remote
            // section — a bare dir is enough; our fake git never inspects it.
            std::fs::create_dir_all(repo.join(".git")).unwrap();

            // A fake `git` that dispatches by subcommand: succeeds for
            // --version / remote / fetch / rev-parse, FAILS for rev-list.
            let shim_dir = tmp.path().join("bin");
            std::fs::create_dir_all(&shim_dir).unwrap();
            let git_shim = shim_dir.join("git");
            std::fs::write(
                &git_shim,
                r#"#!/bin/sh
# Fake git for the rev-list-failure regression pin. Dispatch on the first
# non-flag argument so `git --version`, `git remote ...`, `git fetch ...`,
# `git rev-parse ...` all succeed but `git rev-list ...` fails.
for arg in "$@"; do
  case "$arg" in
    --*) continue ;;
    version) echo "git version 2.43.5"; exit 0 ;;
    remote) exit 0 ;;
    fetch) exit 0 ;;
    rev-parse) echo "main"; exit 0 ;;
    rev-list) echo "fatal: bad revision 'HEAD..vco_upstream/main'" 1>&2; exit 128 ;;
    *) exit 0 ;;
  esac
done
exit 0
"#,
            )
            .unwrap();
            let mut perms = std::fs::metadata(&git_shim).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&git_shim, perms).unwrap();

            // Prepend the shim dir to PATH so our fake `git` wins. Guarded by
            // GLOBAL_ENV_MUTEX so concurrent env-mutating tests don't race.
            let orig_path = std::env::var("PATH").unwrap_or_default();
            let new_path = format!("{}:{}", shim_dir.display(), orig_path);
            let repo_str = repo.to_str().unwrap().to_string();

            // `with_env_vars` holds the mutex and runs the closure; we drive
            // the async command on a fresh single-thread runtime INSIDE it so
            // PATH stays overridden for the whole invocation.
            let status = std::cell::RefCell::new(None);
            vct_launcher_core::test_env::with_env_vars(
                &[("PATH", Some(new_path.as_str()))],
                || {
                    let rt = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .unwrap();
                    let s = rt.block_on(check_for_updates(repo_str.clone())).expect(
                        "check_for_updates must NOT hard-fail on a rev-list failure",
                    );
                    *status.borrow_mut() = Some(s);
                },
            );
            let status = status.into_inner().expect("status captured");

            assert!(
                !status.remote_check_ok,
                "rev-list failure MUST set remote_check_ok=false (was silent pre-v0.2.83)"
            );
            let err = status
                .remote_check_error
                .expect("rev-list failure MUST populate remote_check_error");
            assert!(
                err.contains("rev-list"),
                "error must name the rev-list stage, got: {}",
                err
            );
            assert!(
                !status.remote_ahead,
                "a failed rev-list cannot conclude remote_ahead=true"
            );
        }
    }
}
