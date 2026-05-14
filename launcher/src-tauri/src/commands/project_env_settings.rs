//! Settings struct + populate helper for per-project env-file writers.
//!
//! Background: Until 2026-05-06, `write_project_env_files` and
//! `ensure_project_env_template` accepted a hand-crafted argument list of
//! `(folder, project_name, write_disabled)` and derived every other value
//! from hardcoded constants. The launcher's adopted service ports,
//! `ACTIVE_EMBEDDING` choice, and shared-KG name were all invisible to
//! the create-project path — see `launcher-settings-propagation-audit-2026-05-06.md`
//! for the full inventory of "values that should propagate but don't".
//!
//! This module introduces `ProjectEnvSettings` as a single named bundle
//! plumbed through both writers, plus a `populate` helper that reads the
//! launcher's current state (app_state k/v + services.toml + canonical
//! defaults) once per `create_project_v2` / rename / shared-KG-toggle
//! call. Future launcher-state values can be added here without churning
//! every call site.
//!
//! Key invariants:
//!   * Defaults match the canonical hardcoded values (`localhost:8081`,
//!     `localhost:11435`, `localhost:11440`, "qwen3", "VibeCodedTools_KnowledgeGraph",
//!     etc.) so a launcher with no custom settings produces identical
//!     output to the pre-refactor code.
//!   * Reads are best-effort: a missing app_state row or unreadable
//!     services.toml falls through to defaults. The write path must NEVER
//!     fail because state lookup hiccupped.
//!   * Adopted services (mode = `Adopt` / `Parallel`) override default
//!     ports. Refused / Unresolved fall back to canonical defaults.

use std::path::PathBuf;

use crate::commands::installer::find_local_repo_root;
use crate::commands::projects_v2::{get_shared_kg_write_disabled, sanitize_kg_collection};
use crate::db::Db;
use crate::services::adoption::{self, AdoptionMode};

/// `app_state` key for the active embedding profile (qwen3 / openai / arctic / codesage).
/// Default: `"qwen3"` (matches install.py's default and the MCP server's fallback).
pub const APP_STATE_KEY_ACTIVE_EMBEDDING: &str = "embedding.active_profile";

/// `app_state` key for an override of the cross-project shared KG class name.
/// Default: `"VibeCodedTools_KnowledgeGraph"`. White-label / fork installs
/// can swap this without recompiling.
pub const APP_STATE_KEY_SHARED_KG_NAME: &str = "shared_kg.collection_name";

/// `app_state` keys for explicit port overrides. When set, these win over
/// services.toml adoption + the canonical defaults.
pub const APP_STATE_KEY_WEAVIATE_PORT: &str = "weaviate.port_override";
pub const APP_STATE_KEY_OLLAMA_PORT: &str = "ollama.port_override";
pub const APP_STATE_KEY_CODE_EMBED_PORT: &str = "code_embed.port_override";

/// `app_state` boolean for the GPU toggle. Used by callers that need to
/// know whether the launcher's current install runs in GPU mode (for
/// future per-project compose overrides). Today consumed only as
/// `cpu_only = !use_gpu` for env_file plumbing.
pub const APP_STATE_KEY_USE_GPU: &str = "launcher.use_gpu";

/// Canonical defaults — duplicated from `commands::installer` (private constants).
/// Kept in lockstep via a unit test below.
pub const DEFAULT_WEAVIATE_PORT: u16 = 8081;
pub const DEFAULT_OLLAMA_PORT: u16 = 11435;
pub const DEFAULT_CODE_EMBED_PORT: u16 = 11440;
pub const DEFAULT_ACTIVE_EMBEDDING: &str = "qwen3";
pub const DEFAULT_SHARED_KG_COLLECTION: &str = "VibeCodedTools_KnowledgeGraph";

/// Populated once per project-env write call. Plumbed through
/// `write_project_env_files` and `ensure_project_env_template` so future
/// launcher-state values can be added here without re-threading every
/// call site.
///
/// String-typed for trivial JSON / TOML serialisation in tests; the fields
/// are typed numerically only where a u16 is unambiguously a port.
#[derive(Debug, Clone)]
pub struct ProjectEnvSettings {
    /// Embedding profile (`qwen3` / `openai` / `arctic` / `codesage`).
    /// Read from `app_state` key `embedding.active_profile`; default `"qwen3"`.
    pub active_embedding: String,

    /// Per-service URLs. Composed from the resolved port + the
    /// canonical scheme/host.
    pub weaviate_url: String,
    pub ollama_url: String,
    pub code_embed_url: String,

    pub weaviate_port: u16,
    pub ollama_port: u16,
    pub code_embed_port: u16,

    /// Container runtime detected at populate-time (`"podman"` / `"docker"`)
    /// or `None` if neither is on PATH. Hooks re-probe at exec time;
    /// this value is informational for future compose-override generation
    /// (PR-3 currently only carries it for symmetry — the hook templates
    /// stay runtime-detected on purpose).
    #[allow(dead_code)]
    pub container_runtime: Option<String>,

    /// Per-project KG collection name (`<sanitized>_KnowledgeGraph`).
    pub kg_collection: String,

    /// Per-project development collection (`<sanitized>_Development`).
    pub dev_collection: String,

    /// Cross-project shared KG class name. Default
    /// `"VibeCodedTools_KnowledgeGraph"`; overridable via app_state.
    pub shared_kg_collection: String,

    /// Asymmetric write-gate (read of shared KG is unconditional).
    /// True ⇒ project carries `SHARED_KG_WRITE_DISABLED=true`.
    pub shared_kg_write_disabled: bool,

    /// CPU-only flag (mirror of `!use_gpu`). True when the launcher's
    /// install was configured for CPU-only. Reserved for future per-
    /// project compose-override generation.
    #[allow(dead_code)]
    pub cpu_only: bool,

    /// GPU mode (mirror of `use_gpu`). Reserved for future per-project
    /// compose-override generation.
    #[allow(dead_code)]
    pub use_gpu: bool,

    /// Project's display name (raw, not sanitized — for `PROJECT_NAME`).
    pub project_name: String,

    /// Orchestrator clone root (PR-2 portability, 2026-05-06). `Some` when
    /// `find_local_repo_root()` succeeds at populate time; `None` falls
    /// through to the older behaviour where `VCT_ORCHESTRATOR_ROOT` /
    /// `VCT_INFRASTRUCTURE_DIR` are simply omitted from `.claude/env` (the
    /// in-tree hooks have a fallback resolution path). Routed via the
    /// settings struct so PR-2's value flows through PR-3's plumbing
    /// rather than being a side-channel `find_local_repo_root()` call
    /// inside the writer body.
    pub orchestrator_root: Option<PathBuf>,

    /// Multi-source KG access list (P1-D, 2026-05-08). Sorted, deduped list
    /// of peer project names (sanitized — i.e. the prefix used in the
    /// peer's `<Name>_KnowledgeGraph` collection) the current project has
    /// READ access to via the launcher's access matrix. Empty when the
    /// project only has access to its own + the shared KG (the default).
    /// Emitted as `VCT_KG_ACCESS_LIST=Foo,Bar,Baz` to all three install
    /// surfaces; consumed by `weaviate_mcp/server.py::_kg_collections_to_search`
    /// and the bundled `rl_kg_search.py` to fan-out searches across peers.
    pub kg_access_list: Vec<String>,

    /// Multi-source code-graph access list (P1-D, 2026-05-08). Sorted,
    /// deduped list of peer project names whose code graph the current
    /// project has READ access to (`codegraph_access` table, where the
    /// current project is `grantee` and `access_level == 'read'`). Each
    /// peer maps to 5 prefixed Weaviate collections (`<Name>_CodeFunction`,
    /// `<Name>_CodeClass`, etc.). Empty by default. Emitted as
    /// `VCT_CODE_GRAPH_ACCESS_LIST=Foo,Bar,Baz`.
    pub code_graph_access_list: Vec<String>,

    /// GitHub PAT (0.1.7 fork-readiness sweep, 2026-05-08). Resolved at
    /// `populate` time from the OS keychain entry the OnboardingWizard
    /// writes via `commands::installer::register_github_pat`
    /// (`vct._user_shared_.shared.user / github_pat` — post-2026-05-10
    /// module_id unification with the SecretsPanel UI_MODULE_BUCKET).
    /// Honours the active-flag gate (`is_secret_active_cross_launcher`)
    /// so a paused secret in any sibling launcher's DB returns `None`
    /// here too.
    ///
    /// Replaces the pre-0.1.7 `git-credential-vct` helper: instead of
    /// having git's credential protocol invoke a per-project
    /// helper (incompatible with the active-flag gate), the launcher
    /// now writes `GITHUB_TOKEN=<value>` to each registered project's
    /// env files. Users configure git's credential helper once
    /// (`gh auth setup-git`, or a thin shell helper that reads
    /// `$GITHUB_TOKEN`) and the launcher takes over the per-project
    /// gating via the env var.
    ///
    /// `None` means: no keychain entry, OR entry paused via Lifecycle B,
    /// OR keychain backend unreachable. The pair-builder filter omits
    /// the key from all 3 surfaces in that case (matching the
    /// `VCT_ORCHESTRATOR_ROOT` / `VCT_KG_ACCESS_LIST` semantics).
    ///
    /// Conservative per-project gating: today, every registered project
    /// receives `GITHUB_TOKEN` whenever the keychain has it active. That
    /// matches the pre-0.1.7 file-based behaviour
    /// (`~/.vct-secrets/shared/github_pat` is readable by every process
    /// running as the user). A finer-grained per-project access matrix
    /// for `github_pat` is out of scope for the 0.1.7 fork sweep — see
    /// `docs/MIGRATION-0.2.0.md` "Replacing `git-credential-vct`".
    pub github_token: Option<String>,

    /// Subagent G (2026-05-08): per-project user-bucket secrets resolved
    /// at populate time. Pairs of (KEY, VALUE) for entries that are both
    /// (a) active under the cross-launcher gate, and (b) currently
    /// keychain-present. Emitted alongside the canonical keys into all 3
    /// launcher-managed env surfaces (`.claude/env`,
    /// `.claude/settings.json` `env`, `.vscode/settings.json`
    /// `claude-code.env`).
    ///
    /// Closes the "GUI says secret is set, but I can't actually use it"
    /// gap: a user adding `MY_PROJECT_KEY` in the SecretsPanel now sees
    /// it appear as a normal env var in their next Claude Code session
    /// for that project (no session restart, courtesy of the
    /// `refresh_project_env_with_db` hook in the secret-mutation
    /// commands).
    ///
    /// Threat model note: any subprocess spawned in the project's
    /// Claude Code session can read these as normal env vars —
    /// including bundled MCP servers + hooks. Same exposure profile
    /// `~/.vct-secrets/` already had pre-Subagent A.
    ///
    /// Disjoint from `github_token` (Subagent D): that resolves the
    /// SHARED-scope keychain entry written by the OnboardingWizard
    /// (`scope='shared'`, `module_id='installer'`). User-bucket secrets
    /// here are at `scope='per_project'`, `module_id='user'`. The two
    /// flows never enumerate each other's rows.
    pub user_secret_pairs: Vec<(String, String)>,

    /// Subagent G (2026-05-08): every KEY name the launcher has ever
    /// observed for this project's user-bucket (`scope='per_project'`,
    /// `module_id='user'`), regardless of active flag. ASCII-sorted by
    /// key for deterministic env diffs.
    ///
    /// Used by the env writer as the STRIP set: any key in this list
    /// that is NOT in `user_secret_pairs` is removed from every env
    /// surface on the next write. This is how "paused" / "removed"
    /// secrets get out of the surfaces — without this, a previously-
    /// emitted user secret would persist stale even after the GUI says
    /// it's off.
    ///
    /// Invariant: superset of the keys in `user_secret_pairs`. The
    /// difference set is exactly the inactive / pending-removal
    /// entries. Keys here that the user added BY HAND directly to a
    /// JSON env block (never through `set_secret_v2`) DO NOT appear —
    /// those are tracked solely in the JSON files and the writer
    /// preserves them via the existing `merge_env_object_canonical`
    /// deep-merge.
    pub user_secret_known_keys: Vec<String>,
}

impl ProjectEnvSettings {
    /// Construct a defaults-only settings struct for a project name. Used
    /// by tests and by callers that lack a `Db` handle. All ports / URLs
    /// land at canonical localhost values.
    #[allow(dead_code)]
    pub fn with_defaults(project_name: &str) -> Self {
        let kg_basename = sanitize_kg_collection(project_name);
        Self {
            active_embedding: DEFAULT_ACTIVE_EMBEDDING.to_string(),
            weaviate_url: format!("http://localhost:{}", DEFAULT_WEAVIATE_PORT),
            ollama_url: format!("http://localhost:{}", DEFAULT_OLLAMA_PORT),
            code_embed_url: format!("http://localhost:{}", DEFAULT_CODE_EMBED_PORT),
            weaviate_port: DEFAULT_WEAVIATE_PORT,
            ollama_port: DEFAULT_OLLAMA_PORT,
            code_embed_port: DEFAULT_CODE_EMBED_PORT,
            container_runtime: None,
            kg_collection: format!("{}_KnowledgeGraph", kg_basename),
            dev_collection: format!("{}_Development", kg_basename),
            shared_kg_collection: DEFAULT_SHARED_KG_COLLECTION.to_string(),
            shared_kg_write_disabled: false,
            cpu_only: true,
            use_gpu: false,
            project_name: project_name.to_string(),
            orchestrator_root: None,
            kg_access_list: Vec::new(),
            code_graph_access_list: Vec::new(),
            // Tests use `with_defaults`; they get an absent token so the
            // pair-builder omits `GITHUB_TOKEN` from the surfaces. Tests
            // that exercise the GITHUB_TOKEN propagation path construct
            // a settings struct directly and override this field.
            github_token: None,
            // Subagent G (2026-05-08): tests using `with_defaults` get
            // empty user-secret state (no active pairs, no known keys).
            // Tests that exercise the user-secret propagation path
            // construct a settings struct directly + override.
            user_secret_pairs: Vec::new(),
            user_secret_known_keys: Vec::new(),
        }
    }

    /// `"true"` / `"false"` string form for env writers.
    pub fn shared_kg_write_disabled_str(&self) -> &'static str {
        if self.shared_kg_write_disabled { "true" } else { "false" }
    }
}

/// Resolve a port: app_state override > services.toml adoption > default.
///
/// `services.toml` adoption is honored only for the `Adopt` and `Parallel`
/// modes. `Refuse` and `Unresolved` fall through to the default.
fn resolve_port(
    db: &Db,
    state_key: &str,
    services_state: &adoption::AdoptionState,
    service_name: &str,
    default: u16,
) -> u16 {
    // 1. Explicit user override via app_state.
    if let Ok(Some(s)) = db.app_state_get(state_key) {
        if let Ok(p) = s.parse::<u16>() {
            if p > 0 {
                return p;
            }
        }
    }
    // 2. services.toml adoption (Parallel uses `parallel_port`; Adopt
    //    parses the external_url for the port).
    if let Some(svc) = services_state.get(service_name) {
        match svc.mode {
            AdoptionMode::Parallel => {
                if let Some(p) = svc.parallel_port {
                    return p;
                }
            }
            AdoptionMode::Adopt => {
                if let Some(url) = svc.external_url.as_deref() {
                    if let Some(p) = parse_port_from_url(url) {
                        return p;
                    }
                }
            }
            AdoptionMode::Refuse | AdoptionMode::Unresolved => {}
        }
    }
    default
}

/// Extract the port from a URL like `http://localhost:8081/v1/meta`.
/// Returns `None` for unparseable / missing-port inputs.
fn parse_port_from_url(url: &str) -> Option<u16> {
    // Strip scheme.
    let after_scheme = url
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(url);
    // Slice up to first `/`.
    let host_port = after_scheme.split('/').next().unwrap_or(after_scheme);
    let port_str = host_port.rsplit(':').next()?;
    port_str.parse::<u16>().ok()
}

/// Detect the container runtime synchronously without spawning child
/// processes. Returns `Some("podman")`, `Some("docker")`, or `None`.
/// Synchronous because populate runs from non-async callers
/// (`write_project_env_files`); a runtime probe via `which` is sufficient
/// — a full-fledged version check happens later via `detect_system`.
fn detect_runtime_sync() -> Option<String> {
    if which_cmd("podman").is_some() {
        return Some("podman".to_string());
    }
    if which_cmd("docker").is_some() {
        return Some("docker".to_string());
    }
    None
}

/// Minimal `which` — walk `PATH` and look for an executable file.
/// Avoids pulling in the `which` crate just for this one synchronous use.
fn which_cmd(name: &str) -> Option<std::path::PathBuf> {
    let path_env = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_env) {
        let candidate = dir.join(name);
        // Linux/macOS: just check for a regular file. Windows: try
        // `<name>.exe` too. We stay platform-portable by trying both.
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let with_ext = dir.join(format!("{}.exe", name));
            if with_ext.is_file() {
                return Some(with_ext);
            }
        }
    }
    None
}

/// Populate `ProjectEnvSettings` for a project from launcher state.
///
/// Inputs:
///   * `db` — launcher.db handle. Used to read app_state overrides +
///     `shared_kg_write_disabled` k/v.
///   * `project_name` — project's display name (used for KG collection
///     derivation + `PROJECT_NAME`).
///   * `project_id` — when known, used to read `shared_kg_write_disabled`
///     from `module_settings`. `None` for callers that don't have the row
///     yet (e.g. test contexts).
///
/// Soft-fail policy: every read is wrapped in `unwrap_or` of the canonical
/// default. A poisoned mutex / corrupt JSON / missing services.toml falls
/// through silently. The whole point is that env-file writes must not be
/// blocked by a state-read hiccup.
pub fn populate(
    db: &Db,
    project_name: &str,
    project_id: Option<&str>,
) -> ProjectEnvSettings {
    let services_state = adoption::read();

    let weaviate_port = resolve_port(
        db,
        APP_STATE_KEY_WEAVIATE_PORT,
        &services_state,
        "weaviate",
        DEFAULT_WEAVIATE_PORT,
    );
    let ollama_port = resolve_port(
        db,
        APP_STATE_KEY_OLLAMA_PORT,
        &services_state,
        "ollama",
        DEFAULT_OLLAMA_PORT,
    );
    let code_embed_port = resolve_port(
        db,
        APP_STATE_KEY_CODE_EMBED_PORT,
        &services_state,
        "code_embed",
        DEFAULT_CODE_EMBED_PORT,
    );

    let active_embedding = db
        .app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_ACTIVE_EMBEDDING.to_string());

    let shared_kg_collection = db
        .app_state_get(APP_STATE_KEY_SHARED_KG_NAME)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_SHARED_KG_COLLECTION.to_string());

    let shared_kg_write_disabled = match project_id {
        Some(pid) => get_shared_kg_write_disabled(db, pid).unwrap_or(false),
        None => false,
    };

    let use_gpu = db
        .app_state_get_bool(APP_STATE_KEY_USE_GPU)
        .ok()
        .flatten()
        .unwrap_or(false);

    let kg_basename = sanitize_kg_collection(project_name);
    let own_kg = format!("{}_KnowledgeGraph", kg_basename);
    let own_dev = format!("{}_Development", kg_basename);

    // P1-D (2026-05-08): resolve cross-project KG + codegraph access lists
    // from the launcher's access matrix. These flow into env vars on the
    // 3 surfaces and are consumed by `weaviate_mcp/server.py` + the
    // bundled `rl_kg_search.py` to fan-out searches across peers. Soft-fail
    // (empty list) on any DB error — env-file writes must never block on a
    // matrix-read hiccup.
    let kg_access_list = match project_id {
        Some(pid) => resolve_kg_access_peers(db, pid, &own_kg, &own_dev, &shared_kg_collection),
        None => Vec::new(),
    };
    let code_graph_access_list = match project_id {
        Some(pid) => resolve_code_graph_access_peers(db, pid),
        None => Vec::new(),
    };

    // 0.1.7 fork-readiness sweep (2026-05-08): the OnboardingWizard's
    // GitHub PAT is now in the OS keychain (replaces the legacy
    // `~/.vct-secrets/shared/github_pat` file). Resolve here so the
    // env-pair builder in `write_project_env_files` can emit
    // `GITHUB_TOKEN=<value>` to all 3 install surfaces. Soft-fail
    // (None) on keychain unreachable / no entry / paused — the
    // pair-builder omits the key in that case, matching the
    // VCT_ORCHESTRATOR_ROOT / VCT_KG_ACCESS_LIST semantics.
    //
    // See `commands::installer::github_pat_from_keychain` for the
    // (scope, module_id, key) tuple + active-flag gate.
    let github_token = crate::commands::installer::github_pat_for_env(db);

    // Subagent G (2026-05-08): resolve user-set per-project secrets so
    // they auto-emit into all 3 launcher-managed env surfaces.
    //
    // Two parallel outputs:
    //   * `user_secret_pairs`: (KEY, VALUE) for entries that are both
    //     active under the cross-launcher gate AND keychain-present.
    //     The env writer EMITS these.
    //   * `user_secret_known_keys`: every KEY ever observed in the
    //     per-project user-bucket regardless of active flag. Used as
    //     the STRIP set so paused / removed secrets get out of the
    //     surfaces (otherwise a previously-emitted secret persists
    //     stale even after the GUI says it's off).
    //
    // Without `project_id` (test contexts where the project row hasn't
    // been inserted yet) we skip the resolution — empty pairs + empty
    // known set means the writer behaves identically to pre-Subagent-G.
    let (user_secret_pairs, user_secret_known_keys) = match project_id {
        Some(pid) => resolve_user_secret_state(db, pid),
        None => (Vec::new(), Vec::new()),
    };

    ProjectEnvSettings {
        active_embedding,
        weaviate_url: format!("http://localhost:{}", weaviate_port),
        ollama_url: format!("http://localhost:{}", ollama_port),
        code_embed_url: format!("http://localhost:{}", code_embed_port),
        weaviate_port,
        ollama_port,
        code_embed_port,
        container_runtime: detect_runtime_sync(),
        kg_collection: own_kg,
        dev_collection: own_dev,
        shared_kg_collection,
        shared_kg_write_disabled,
        cpu_only: !use_gpu,
        use_gpu,
        project_name: project_name.to_string(),
        // PR-2 portability: best-effort orchestrator clone root. Soft-fail
        // (None) so a launcher running outside a git checkout still
        // produces a usable `.claude/env` (the bundled hooks' in-tree
        // fallback path takes over).
        orchestrator_root: find_local_repo_root().ok(),
        kg_access_list,
        code_graph_access_list,
        github_token,
        user_secret_pairs,
        user_secret_known_keys,
    }
}

/// Extract peer project names from the launcher's `kg_collection_access`
/// matrix for a given project. Returns the SANITIZED prefix of every
/// `<X>_KnowledgeGraph` collection the project has read/write access to,
/// excluding the project's own KG/dev collections and the cross-project
/// shared collection. Sorted + deduped for deterministic env output.
///
/// Soft-fail: any DB error → empty list (the access-list feature is a
/// strict opt-in extension; a populate-time read failure must never
/// degrade the basic env write).
///
/// Naming round-trip: `kg_set_access` writes `<Sanitized>_KnowledgeGraph`
/// from `populate_kg_collection_access`; we strip the trailing
/// `_KnowledgeGraph` (or `_Development`) and feed the prefix back to the
/// MCP server, which re-applies its own `_sanitize_collection_prefix`
/// (idempotent for already-sanitized inputs) before resolving the full
/// collection name. This keeps the env-var contract project-name-shaped
/// rather than collection-name-shaped, matching the design in
/// `vco-multi-source-kg-access-design.md`.
fn resolve_kg_access_peers(
    db: &Db,
    project_id: &str,
    own_kg: &str,
    own_dev: &str,
    shared_kg: &str,
) -> Vec<String> {
    let rows = match db.kg_list_access(project_id) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };
    let mut peers: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for (collection_name, access_level) in rows {
        if access_level != "read" && access_level != "write" {
            continue;
        }
        if collection_name == own_kg
            || collection_name == own_dev
            || collection_name == shared_kg
        {
            continue;
        }
        if let Some(stripped) = collection_name
            .strip_suffix("_KnowledgeGraph")
            .or_else(|| collection_name.strip_suffix("_Development"))
        {
            if !stripped.is_empty() {
                peers.insert(stripped.to_string());
            }
        }
    }
    peers.into_iter().collect()
}

/// Extract peer project names whose code graph the given project can read.
/// Reads the `codegraph_access` table for rows where `grantee_project_id =
/// project_id` and `access_level = 'read'`, then resolves grantor IDs to
/// human-readable project names. Sorted by sanitized peer name for
/// deterministic env output. Soft-fail to empty list on any DB error.
fn resolve_code_graph_access_peers(db: &Db, project_id: &str) -> Vec<String> {
    let rows = match db.codegraph_list_grants_to(project_id) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };
    let mut peers: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for (grantor_id, access_level) in rows {
        if access_level != "read" {
            continue;
        }
        if grantor_id == project_id {
            continue;
        }
        // Resolve grantor's name → sanitized prefix (matches the per-project
        // code-graph collection naming `<Sanitized>_CodeFunction` etc.).
        match db.get_project(&grantor_id) {
            Ok(Some(row)) => {
                let sanitized = sanitize_kg_collection(&row.name);
                if !sanitized.is_empty() {
                    peers.insert(sanitized);
                }
            }
            // Dangling grantor (project deleted): skip silently.
            Ok(None) => {}
            // DB error per row: skip the row, keep going.
            Err(_) => {}
        }
    }
    peers.into_iter().collect()
}

/// Subagent G (2026-05-08), broadened by H2 (2026-05-08): resolve the
/// user-bucket secret state for the env-pair builder. Covers all THREE
/// SecretsPanel tabs:
///
///   * Per-project tab → `(scope='per_project', project_id, module_id='user')`
///   * Shared tab      → `(scope='shared',      '_user_shared_', 'user')`
///   * Global tab      → `(scope='global',      '_global_',      'user')`
///
/// Pre-H2 only the per-project bucket flowed into env surfaces. Shared
/// and Global rows existed in the keychain + active-flag DB but no
/// consumer enumerated them, so a key the user added via the Shared
/// tab was silent to every project's `.claude/env`. H2 closes that
/// gap by merging all three buckets at populate time.
///
/// Returns `(active_pairs, known_keys)`:
///
///   * `active_pairs`: `(KEY, VALUE)` for every key (across all three
///     buckets) where the cross-launcher active gate says active AND
///     the OS keychain currently holds a value. Order is per-project
///     keys first (ASCII-sorted), then shared (ASCII-sorted), then
///     global (ASCII-sorted) — bucket-stable so env-surface diffs
///     stay readable.
///
///   * `known_keys`: every KEY ever observed in any of the three
///     buckets regardless of active flag. Same ordering as
///     `active_pairs`. Superset of the keys in `active_pairs`.
///
/// The env writer uses the difference set (`known_keys` − keys-in-`active_pairs`)
/// as its STRIP set: any of those keys still present in the env
/// surfaces from a prior write get removed on this write. That is how
/// paused / pending-removal secrets exit the surfaces — without it, a
/// previously-emitted user secret would persist stale even after the
/// GUI toggles it off.
///
/// Bucket-collision handling: if the same KEY exists in two buckets
/// (e.g. a user adds `MY_KEY` per-project AND in Shared), the
/// per-project value wins by virtue of bucket order — it lands in
/// `active_pairs` first, and the writer's pair-canonicalization keeps
/// the first occurrence. This matches the SecretsPanel's read-time
/// resolution comment ("Per-project bag for P → Shared → Global,
/// first hit wins").
///
/// Soft-fail: keychain backend unreachable / DB hiccup → empty pairs
/// (the key vanishes from EMIT but stays in the strip set if its row
/// exists). The env-file writes must never block on a metadata-read
/// failure.
///
/// Disjoint from `github_pat_for_env` (Subagent D): that one targets
/// the SHARED-scope `github_pat` keychain entry under
/// `module_id='installer'`. This function only enumerates
/// `module_id='user'` rows. The two flows never enumerate each
/// other's entries — there is zero overlap.
fn resolve_user_secret_state(db: &Db, project_id: &str) -> (Vec<(String, String)>, Vec<String>) {
    // Per-project bucket (existing behaviour, byte-identical to pre-H2).
    let per_project_keys = db.list_user_secret_keys_for_project(project_id);
    // Shared bucket — applies to every registered project for this user.
    let shared_keys = db.list_shared_user_secret_keys();
    // Global bucket — applies machine-wide.
    let global_keys = db.list_global_user_secret_keys();

    let mut pairs: Vec<(String, String)> =
        Vec::with_capacity(per_project_keys.len() + shared_keys.len() + global_keys.len());
    let mut known_keys: Vec<String> =
        Vec::with_capacity(per_project_keys.len() + shared_keys.len() + global_keys.len());

    // Helper closure: resolve one bucket. `scope_str` drives the active-flag
    // gate; `slot_project_id` drives both the active-flag gate AND the
    // keychain lookup (matches the writer's slot — SENTINEL_SHARED for
    // shared, SENTINEL_GLOBAL for global, real UUID for per-project).
    // Shared and global use module_id='user' across the board.
    fn resolve_one_bucket(
        db: &Db,
        keys: &[String],
        scope_str: &str,
        slot_project_id: &str,
        keychain_scope: crate::secrets::SecretScope<'_>,
        out_pairs: &mut Vec<(String, String)>,
        out_known: &mut Vec<String>,
        already_emitted: &std::collections::HashSet<String>,
    ) {
        for key in keys {
            // The known-keys list always carries the key (drives strip
            // set on the writer side). De-duplication on `out_known`
            // prevents the same key showing up twice if it lives in
            // multiple buckets.
            if !out_known.iter().any(|k| k == key) {
                out_known.push(key.clone());
            }
            // Skip emit if a previous bucket already populated this key.
            // Bucket order = per-project → shared → global, so
            // per-project wins (matches SecretsPanel's read order).
            if already_emitted.contains(key) {
                continue;
            }
            let active = crate::db::secret_active::is_secret_active_cross_launcher(
                db,
                scope_str,
                slot_project_id,
                "user",
                key,
            );
            if !active {
                continue;
            }
            match crate::secrets::get(keychain_scope, "user", key) {
                Ok(Some(v)) => {
                    out_pairs.push((key.clone(), v));
                }
                // Keychain has no value for this row (e.g. user removed
                // via the OS keychain UI directly) — skip emit. The
                // strip set still carries the key.
                Ok(None) => {}
                // Keychain backend unreachable — soft-fail.
                Err(_) => {}
            }
        }
    }

    // Track keys already emitted so collisions across buckets resolve
    // first-bucket-wins.
    let mut emitted: std::collections::HashSet<String> = std::collections::HashSet::new();

    // 1. Per-project bucket — wins on collisions with shared/global.
    resolve_one_bucket(
        db,
        &per_project_keys,
        "per_project",
        project_id,
        crate::secrets::SecretScope::PerProject { project_id },
        &mut pairs,
        &mut known_keys,
        &emitted,
    );
    for (k, _) in pairs.iter() {
        emitted.insert(k.clone());
    }

    // 2. Shared bucket.
    let shared_pairs_start = pairs.len();
    resolve_one_bucket(
        db,
        &shared_keys,
        "shared",
        "_user_shared_",
        crate::secrets::SecretScope::Shared {
            project_id: "_user_shared_",
        },
        &mut pairs,
        &mut known_keys,
        &emitted,
    );
    for (k, _) in &pairs[shared_pairs_start..] {
        emitted.insert(k.clone());
    }

    // 3. Global bucket.
    resolve_one_bucket(
        db,
        &global_keys,
        "global",
        "_global_",
        crate::secrets::SecretScope::Global,
        &mut pairs,
        &mut known_keys,
        &emitted,
    );

    (pairs, known_keys)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::adoption::ServiceAdoption;

    #[test]
    fn defaults_match_installer_constants() {
        // Pinned by name to keep this module decoupled from
        // `commands::installer`'s private constants. If installer.rs ever
        // changes a default port, both places must change — this test
        // documents the contract.
        assert_eq!(DEFAULT_WEAVIATE_PORT, 8081);
        assert_eq!(DEFAULT_OLLAMA_PORT, 11435);
        assert_eq!(DEFAULT_CODE_EMBED_PORT, 11440);
    }

    #[test]
    fn with_defaults_produces_canonical_output() {
        let s = ProjectEnvSettings::with_defaults("My Project");
        assert_eq!(s.kg_collection, "MyProject_KnowledgeGraph");
        assert_eq!(s.dev_collection, "MyProject_Development");
        assert_eq!(s.shared_kg_collection, "VibeCodedTools_KnowledgeGraph");
        assert_eq!(s.weaviate_url, "http://localhost:8081");
        assert_eq!(s.ollama_url, "http://localhost:11435");
        assert_eq!(s.code_embed_url, "http://localhost:11440");
        assert_eq!(s.active_embedding, "qwen3");
        assert_eq!(s.shared_kg_write_disabled_str(), "false");
        assert!(!s.shared_kg_write_disabled);
        assert!(!s.use_gpu);
        assert!(s.cpu_only);
    }

    #[test]
    fn parse_port_from_url_handles_canonical_shapes() {
        assert_eq!(parse_port_from_url("http://localhost:8081"), Some(8081));
        assert_eq!(parse_port_from_url("http://localhost:8081/v1/meta"), Some(8081));
        assert_eq!(parse_port_from_url("https://host:11445/path"), Some(11445));
        assert_eq!(parse_port_from_url("http://localhost"), None);
        assert_eq!(parse_port_from_url("not-a-url"), None);
    }

    #[test]
    fn resolve_port_app_state_override_wins() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_WEAVIATE_PORT, "9999").unwrap();
        let services = adoption::AdoptionState::default();
        let p = resolve_port(
            &db,
            APP_STATE_KEY_WEAVIATE_PORT,
            &services,
            "weaviate",
            DEFAULT_WEAVIATE_PORT,
        );
        assert_eq!(p, 9999);
    }

    #[test]
    fn resolve_port_services_toml_parallel_used() {
        let db = Db::open_in_memory().unwrap();
        let mut services = adoption::AdoptionState::default();
        services.upsert(ServiceAdoption {
            name: "ollama".into(),
            mode: AdoptionMode::Parallel,
            external_url: Some("http://localhost:11435".into()),
            parallel_port: Some(11445),
            container_name: None,
        });
        let p = resolve_port(
            &db,
            APP_STATE_KEY_OLLAMA_PORT,
            &services,
            "ollama",
            DEFAULT_OLLAMA_PORT,
        );
        assert_eq!(p, 11445);
    }

    #[test]
    fn resolve_port_services_toml_adopt_url_used() {
        let db = Db::open_in_memory().unwrap();
        let mut services = adoption::AdoptionState::default();
        services.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8090".into()),
            parallel_port: None,
            container_name: None,
        });
        let p = resolve_port(
            &db,
            APP_STATE_KEY_WEAVIATE_PORT,
            &services,
            "weaviate",
            DEFAULT_WEAVIATE_PORT,
        );
        assert_eq!(p, 8090);
    }

    #[test]
    fn resolve_port_refused_falls_through_to_default() {
        let db = Db::open_in_memory().unwrap();
        let mut services = adoption::AdoptionState::default();
        services.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Refuse,
            external_url: Some("http://localhost:9999".into()),
            parallel_port: None,
            container_name: None,
        });
        let p = resolve_port(
            &db,
            APP_STATE_KEY_WEAVIATE_PORT,
            &services,
            "weaviate",
            DEFAULT_WEAVIATE_PORT,
        );
        assert_eq!(p, DEFAULT_WEAVIATE_PORT);
    }

    #[test]
    fn populate_with_no_state_returns_canonical_defaults() {
        let db = Db::open_in_memory().unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "qwen3");
        assert_eq!(s.weaviate_port, DEFAULT_WEAVIATE_PORT);
        assert_eq!(s.ollama_port, DEFAULT_OLLAMA_PORT);
        assert_eq!(s.code_embed_port, DEFAULT_CODE_EMBED_PORT);
        assert_eq!(s.kg_collection, "Acme_KnowledgeGraph");
        assert_eq!(s.shared_kg_collection, "VibeCodedTools_KnowledgeGraph");
        assert!(!s.shared_kg_write_disabled);
    }

    #[test]
    fn populate_honors_active_embedding_override() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "openai").unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "openai");
    }

    #[test]
    fn populate_honors_shared_kg_name_override() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_SHARED_KG_NAME, "WhitelabelCorp_KG").unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.shared_kg_collection, "WhitelabelCorp_KG");
    }

    #[test]
    fn populate_honors_use_gpu_toggle() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set_bool(APP_STATE_KEY_USE_GPU, true).unwrap();
        let s = populate(&db, "Acme", None);
        assert!(s.use_gpu);
        assert!(!s.cpu_only);
    }

    #[test]
    fn populate_empty_string_app_state_falls_through_to_default() {
        // Defensive: an `app_state_set` with an empty value must not
        // override the default — empty strings would silently break env
        // resolution downstream.
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "").unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, DEFAULT_ACTIVE_EMBEDDING);
    }
}
