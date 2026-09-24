//! Settings struct + populate helper for per-project env-file writers.
//!
//! Background: Until 2026-05-06, the Rust env writer and the Rust `.env`
//! template writer (both retired v0.2.97) accepted a hand-crafted argument list of
//! `(folder, project_name, write_disabled)` and derived every other value
//! from hardcoded constants. The launcher's adopted service ports,
//! `ACTIVE_EMBEDDING` choice, and shared-KG name were all invisible to
//! the create-project path — see `launcher-settings-propagation-audit-2026-05-06.md`
//! for the full inventory of "values that should propagate but don't".
//!
//! This module introduces `ProjectEnvSettings` as a single named bundle —
//! today consumed by the project-root `.env` write (its service ports go to
//! `vco_lib.env_template` through the bridge), the kg-sync / kg-summary
//! spawns, and the access-list values
//! `refresh_project_env_with_db` reports; the canonical env SURFACES are
//! written by `vco_lib.config_projection` alone since the Rust writer's
//! retirement — plus a `populate` helper that reads the
//! launcher's current state (app_state k/v + services.toml + canonical
//! defaults) once per `create_project_v2` / rename / shared-KG-toggle
//! call. Future launcher-state values can be added here without churning
//! every call site.
//!
//! Key invariants:
//!   * Defaults match the canonical hardcoded values (`localhost:8081`,
//!     `localhost:11435`, `localhost:11440`, "qwen3",
//!     "VibeCodedOrchestrator_KnowledgeGraph" — flipped to capital-C in
//!     v0.2.23 B1 from the v0.2.12–v0.2.22 lowercase-c casing
//!     "VibecodedOrchestrator_KnowledgeGraph", itself renamed from
//!     "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 — etc.) so a
//!     launcher with no custom settings produces identical output to the
//!     pre-refactor code modulo the shared-KG rename.
//!   * Reads are best-effort: a missing app_state row or unreadable
//!     services.toml falls through to defaults. The write path must NEVER
//!     fail because state lookup hiccupped.
//!   * Adopted services (mode = `Adopt` / `Parallel`) override default
//!     ports. Refused / Unresolved fall back to canonical defaults.

// Canonical home for the `default_text_embedding` app_state key — reuse it
// here rather than re-declaring the magic string a third time (it is also
// privately re-declared in `embedding_catalog.rs`, with a sync comment).
use crate::commands::openai_cmd::APP_STATE_DEFAULT_TEXT_EMBED;
use crate::commands::projects_v2::sanitize_kg_collection;
use crate::db::Db;

/// `app_state` key for the active embedding profile (qwen3 / openai / arctic / codesage).
/// Default: `"qwen3"` (matches install.py's default and the MCP server's fallback).
pub const APP_STATE_KEY_ACTIVE_EMBEDDING: &str = "embedding.active_profile";

/// `module_settings` identifiers for the per-project ACTIVE_EMBEDDING profile.
///
/// `(project_id, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)` is
/// the per-project row that the hub resolver (`config_api.rs`) + the Python
/// `config_projection` writer read to stamp `ACTIVE_EMBEDDING` into
/// `.claude/{settings.json,env}`. v0.2.71 T-B-emb adds a companion
/// `ACTIVE_EMBEDDING_SOURCE_SETTING_KEY` marker row so a deliberate user pick
/// (`"user"`) becomes sticky-across-updates while an auto-seed (`"auto"`) and a
/// legacy NO-marker row both inherit the machine-global default.
pub const ORCHESTRATOR_CORE_MODULE_ID: &str = "orchestrator-core";
pub const ACTIVE_EMBEDDING_SETTING_KEY: &str = "active_embedding";
pub const ACTIVE_EMBEDDING_SOURCE_SETTING_KEY: &str = "active_embedding_source";

/// Provenance marker values for `active_embedding_source`.
///
/// * `"user"` — written by the Settings-tab per-project embedding picker
///   (`set_project_active_embedding`). STICKY: the resolver returns the
///   per-project `active_embedding` row verbatim and NO update path may
///   overwrite it.
/// * `"auto"` — written by the startup backfill (`project_backfill.rs`). The
///   resolver treats it as "inherit the machine-global default".
///
/// A LEGACY per-project `active_embedding` row with NO `active_embedding_source`
/// companion (written before v0.2.71) is treated identically to `"auto"` —
/// inherit the global default. This is a LOCKED decision (v0.2.71 MASTER PLAN
/// §Sweep B): a legacy deliberate qwen3 pick getting overridden by a global
/// arctic default is the accepted cost of fixing the far-more-common
/// auto-seeded qwen3 case (the backfill stamped qwen3 with no provenance).
pub const ACTIVE_EMBEDDING_SOURCE_USER: &str = "user";
pub const ACTIVE_EMBEDDING_SOURCE_AUTO: &str = "auto";

/// `app_state` key for an override of the cross-project shared KG class name.
/// Default: `"VibeCodedOrchestrator_KnowledgeGraph"` (since v0.2.23 B1; was
/// `"VibecodedOrchestrator_KnowledgeGraph"` v0.2.12–v0.2.22, itself renamed
/// from `"VibeCodedTools_KnowledgeGraph"` in v0.2.12 PR-26 / Group E).
/// White-label / fork installs can swap this without recompiling.
pub const APP_STATE_KEY_SHARED_KG_NAME: &str = "shared_kg.collection_name";

/// v0.2.73 Concern-A/C: machine-GLOBAL RL telemetry opt-out `app_state` keys.
///
/// Written by the launcher's GLOBAL Preferences page via the generic
/// `app_state_set_bool` command ("true"/"false"), read back by the Python
/// `config_projection` writer (`APP_STATE_KEY_RL_*_GLOBAL`) and projected into
/// every project's `.claude/settings.json` env as `RL_LOCAL_LOGGING_DISABLED_GLOBAL`
/// / `RL_ONLINE_TRAINING_DISABLED_GLOBAL`. These are the GLOBAL leg of a
/// two-level gate: the RL resolver OR's the global env with the per-project
/// `.claude/env` flag, so a GLOBAL disable overrides ALL projects while a
/// global-enabled state still lets one project opt out locally.
///
/// MUST MATCH the Python constants in `vco_lib/config_projection.py`
/// (`APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL` /
/// `APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL`). Both keys are listed in
/// `app_state_key_triggers_env_reprojection` below so a GUI write refreshes
/// every registered project's env.
pub const APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL: &str =
    "rl.local_logging_disabled_global";
pub const APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL: &str =
    "rl.online_training_disabled_global";

/// Where the core services are reached: `vct_launcher_core::services::
/// service_endpoints` — the launcher.db `service_endpoints` row, else the
/// compiled default — the resolver the hub's `/config` shares with
/// [`populate`] and the Python projection mirrors (v0.2.97).
use vct_launcher_core::services::service_endpoints::{self as endpoints, CoreService};

/// `app_state` boolean for the GPU toggle. Used by callers that need to
/// know whether the launcher's current install runs in GPU mode (for
/// future per-project compose overrides). Today consumed only as
/// `cpu_only = !use_gpu` for env_file plumbing.
pub const APP_STATE_KEY_USE_GPU: &str = "launcher.use_gpu";

/// Canonical default ports — declared in `service_endpoints` (see above) and
/// kept in lockstep with `commands::installer`'s private copies via a unit
/// test below.
pub use vct_launcher_core::services::service_endpoints::{
    DEFAULT_CODE_EMBED_PORT, DEFAULT_OLLAMA_PORT, DEFAULT_WEAVIATE_PORT,
};
pub const DEFAULT_ACTIVE_EMBEDDING: &str = "qwen3";

/// Text model id → ACTIVE_EMBEDDING profile.
///
/// must match install.py::_TEXT_MODEL_ACTIVE_EMBEDDING (the Python side is
/// the canonical home; this is the Rust mirror so the GUI chooser can write
/// the same profile key install.py would derive). Drift here re-introduces
/// the v0.2.68 Defect D bug: the GUI writes only the model id, the canonical
/// `embedding.active_profile` key stays empty, and `populate` falls back to
/// "qwen3" for every project even when the user picked arctic.
///
/// v0.2.69 FIX 1: made `pub(crate)` so `project_backfill.rs` reuses the
/// same map (single source) when deriving the per-project
/// `module_settings/active_embedding` seed from the hardware pick.
pub(crate) fn active_profile_for_model(model_id: &str) -> Option<&'static str> {
    match model_id.trim() {
        "qwen3-embedding:0.6b" => Some("qwen3"),
        "snowflake-arctic-embed2:latest" => Some("arctic"),
        "openai-text-embedding-3-small" => Some("openai"),
        "text-embedding-3-small" => Some("openai"),
        _ => None,
    }
}

/// Write the new-project default TEXT embedding model id AND its derived
/// canonical profile (`app_state[embedding.active_profile]`) in one place.
///
/// Before v0.2.68 the GUI/onboarding chooser wrote only the model id key
/// (`default_text_embedding`). The canonical profile key — the one
/// `populate` (below) and `embedding_service.py::_resolve_active_embedding`
/// actually read — was written by NO GUI path, only by install.py's
/// `_reconcile_install_active_embedding` during a full install run. So a
/// user who picked arctic in the launcher still got `ACTIVE_EMBEDDING=qwen3`
/// stamped into every project's `.claude/settings.json` + `.claude/env`.
///
/// All four GUI write sites for `default_text_embedding`
/// (`embedding_catalog::set_default_embedding_models` +
/// `openai_cmd`'s register / recovery-fallback / recovery-restore paths)
/// funnel through this helper so the two keys can never diverge again.
///
/// The profile is only written when `model_id` maps to a known profile via
/// `active_profile_for_model`; an unrecognised id leaves the canonical key
/// untouched (conservative: don't stamp a guessed profile that could index
/// the KG against the wrong vector slot).
///
/// F5 (v0.2.72): both keys feed the machine-global leg of the
/// ACTIVE_EMBEDDING cascade — every project WITHOUT a sticky per-project
/// user pick inherits the new value in its projected env. After the DB
/// write we therefore re-project `.claude/{settings.json,env}` for ALL
/// projects so the settings watcher's diff-guard can fire the guarded MCP
/// reload (see `projects_v2::reproject_env_soft` for the mechanism note).
/// Soft-fail: the returned report carries per-project outcomes; a
/// projection hiccup never rolls back the app_state write.
pub fn set_text_embedding_and_profile(
    db: &Db,
    model_id: &str,
) -> Result<crate::commands::projects_v2::RefreshAllProjectsEnvResult, String> {
    let model_id = model_id.trim();
    db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, model_id)
        .map_err(|e| format!("app_state_set default_text_embedding: {e}"))?;
    if let Some(profile) = active_profile_for_model(model_id) {
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, profile)
            .map_err(|e| format!("app_state_set embedding.active_profile: {e}"))?;
    }
    Ok(crate::commands::projects_v2::refresh_all_projects_env_with_db(db))
}

/// F5 (v0.2.72): `app_state` keys whose value changes MCP-relevant
/// per-project env (they feed the machine-global leg of the
/// ACTIVE_EMBEDDING cascade that `config_projection.py` projects into
/// every project's `.claude/settings.json`). The generic `app_state_set`
/// / `app_state_set_bool` Tauri commands consult this predicate to decide
/// whether a write must trigger a machine-global env re-projection — the
/// launcher GUI's Preferences page writes `embedding.active_profile`
/// through the GENERIC command, not a dedicated setter.
///
/// Deliberately NOT listed:
///   * `APP_STATE_KEY_SHARED_KG_NAME` — written only by the dedicated
///     `set_shared_kg_collection_name` command, which (v0.2.72 R1)
///     already refreshes ALL projects itself via
///     `set_shared_kg_collection_name_with_db`. (Since R1 the Python
///     projection + hub resolver DO honor this override as Priority 1 —
///     same precedence as `populate()` below — so the refresh is no
///     longer inert; it just lives at the dedicated setter, mirroring
///     the `set_codegraph_floors` pattern.)
///   * `codegraph.retrieval_floor` / `codegraph.post_rerank_floor` —
///     written only by `set_codegraph_floors`, which already refreshes.
///
/// v0.2.97: the service endpoints are NOT app_state keys. They are
/// `service_endpoints` rows, whose one writer (`vco_lib.service_endpoints`)
/// re-projects every project itself after a change; the retired
/// `*.port_override` keys feed nothing and so trigger nothing.
pub fn app_state_key_triggers_env_reprojection(key: &str) -> bool {
    matches!(
        key,
        APP_STATE_KEY_ACTIVE_EMBEDDING
            | APP_STATE_DEFAULT_TEXT_EMBED
            // v0.2.73 Concern-A/C: the GLOBAL RL telemetry opt-outs are written
            // through the GENERIC app_state_set_bool command (the Preferences
            // page has no dedicated setter for them), so they MUST be listed
            // here to trigger the machine-global env re-projection that stamps
            // RL_LOCAL_LOGGING_DISABLED_GLOBAL / RL_ONLINE_TRAINING_DISABLED_GLOBAL
            // into every project's .claude/settings.json env.
            | APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL
            | APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL
    )
}

/// Read the machine-global active-embedding profile from `app_state`.
///
/// Same shape as the first two arms of `populate`'s `active_embedding`
/// resolution: canonical `app_state[embedding.active_profile]` →
/// `app_state[default_text_embedding]` mapped via `active_profile_for_model`
/// → `None`. Returns `None` (caller falls to `"qwen3"`) when neither is set
/// or the hardware pick maps to no known profile. Soft-fail on any DB error.
///
/// This is the GLOBAL leg of the active-embedding cascade — shared by
/// `resolve_active_embedding_cascade` (below) and mirrored in
/// `config_api.rs` (hub) + `config_projection.py` (projection writer).
fn global_active_embedding(db: &Db) -> Option<String> {
    db.app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED)
                .ok()
                .flatten()
                .and_then(|model_id| active_profile_for_model(&model_id))
                .map(|profile| profile.to_string())
        })
}

/// The ONE active-embedding resolution cascade (v0.2.71 T-B-emb).
///
/// Resolution order (LOCKED — must match the hub `config_api.rs` resolver and
/// the Python `config_projection.py` writer EXACTLY):
///
///   1. Per-project `module_settings/orchestrator-core/active_embedding`
///      WHERE the companion `active_embedding_source` row == `"user"`
///      (a deliberate Settings-tab pick) → returned VERBATIM (sticky).
///   2. Machine-global `app_state[embedding.active_profile]` (then the
///      hardware-pick derive) — when the per-project row is `"auto"`,
///      a legacy NO-marker row, or absent. This is the two-DB-location
///      BRIDGE (B1): a per-project row that is NOT a user pick yields to the
///      global default, so a GUI write to `app_state` (Identity tab) and a
///      hub read can never disagree on a non-user project.
///   3. `"qwen3"` — final fallback.
///
/// Soft-fail: every DB read is best-effort; a hiccup falls through to the
/// next leg (never panics, never blocks an env render).
///
/// `project_id == None` (test / DB-less contexts) skips leg 1 and resolves
/// from the global default only — matching the `populate(None)` contract.
pub fn resolve_active_embedding_cascade(db: &Db, project_id: Option<&str>) -> String {
    // Leg 1: sticky per-project user pick.
    if let Some(pid) = project_id {
        let source = db
            .get_setting(pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .ok()
            .flatten()
            .and_then(|v| v.as_str().map(String::from));
        if source.as_deref() == Some(ACTIVE_EMBEDDING_SOURCE_USER) {
            if let Some(value) = db
                .get_setting(pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
                .ok()
                .flatten()
                .and_then(|v| v.as_str().map(String::from))
                .filter(|s| !s.is_empty())
            {
                return value;
            }
            // source=user but the value row is missing/empty — fall through
            // to the global default rather than returning an empty string.
        }
    }
    // Legs 2 + 3: machine-global default, else qwen3.
    global_active_embedding(db).unwrap_or_else(|| DEFAULT_ACTIVE_EMBEDDING.to_string())
}

/// Persist a deliberate per-project active-embedding pick from the
/// Settings-tab picker. Writes BOTH the value row AND the
/// `active_embedding_source = "user"` marker so the cascade treats it as
/// sticky across updates.
///
/// `profile` is a normalised profile id (`qwen3` / `arctic` / `openai` /
/// `codesage`) — the picker resolves the chosen model's slot to a profile
/// before calling this. The pair is written atomically enough for our
/// purposes (two `set_setting` upserts; a crash between them leaves the
/// value row without a user marker, which the cascade treats as auto =
/// inherit global — the conservative outcome, never a wrong sticky slot).
pub fn write_project_active_embedding_user(
    db: &Db,
    project_id: &str,
    profile: &str,
) -> Result<(), String> {
    let profile = profile.trim();
    if profile.is_empty() {
        return Err("write_project_active_embedding_user: empty profile".into());
    }
    db.set_setting(
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        ACTIVE_EMBEDDING_SETTING_KEY,
        &serde_json::Value::String(profile.to_string()),
    )?;
    db.set_setting(
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
        &serde_json::Value::String(ACTIVE_EMBEDDING_SOURCE_USER.to_string()),
    )?;
    Ok(())
}

/// Free-function core of `set_project_active_embedding` — write the sticky
/// per-project pick, then re-project the env files so
/// `.claude/{settings.json,env}` reflect the new ACTIVE_EMBEDDING and the
/// settings watcher's diff-guard can fire the guarded MCP reload.
///
/// F5 (v0.2.72): the re-projection moved INTO the command (previously the
/// doc comment delegated it to "the caller" — and one caller, the
/// model-switch modal's "keep previous model" path, never did it, leaving
/// the live MCP on a stale ACTIVE_EMBEDDING until an unrelated refresh).
/// Soft-fail: a projection hiccup lands in the returned result's
/// `warnings`; it never rolls back the DB write.
pub fn set_project_active_embedding_with_db(
    db: &Db,
    project_id: &str,
    profile: &str,
) -> Result<crate::commands::projects_v2::RefreshProjectEnvResult, String> {
    if project_id.is_empty() {
        return Err("set_project_active_embedding: project_id required".into());
    }
    write_project_active_embedding_user(db, project_id, profile)?;
    Ok(crate::commands::projects_v2::reproject_env_soft(db, project_id))
}

/// Tauri command — Settings-tab per-project embedding picker WRITE path.
///
/// Records a deliberate user pick (`source = "user"`, sticky). The frontend
/// passes the resolved PROFILE id (qwen3 / arctic / openai / codesage), not
/// the raw model id — the picker maps the chosen catalog model's slot to its
/// profile before invoking. The command re-projects the env files itself
/// (F5, v0.2.72) — callers no longer need a follow-up
/// `refresh_project_env` invoke (a duplicate one is a harmless idempotent
/// no-op: the watcher diff-guard hash-matches and skips the reload).
#[tauri::command]
pub async fn set_project_active_embedding(
    project_id: String,
    profile: String,
    db: tauri::State<'_, Db>,
) -> Result<(), String> {
    set_project_active_embedding_with_db(&db, &project_id, &profile).map(|_| ())
}

/// Resolved per-project active-embedding profile + its provenance, for the
/// Settings-tab picker to render its current selection and source badge.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ActiveEmbeddingState {
    /// The EFFECTIVE profile the cascade resolves to (what lands in
    /// `.claude/{settings.json,env}`). Always non-empty (qwen3 floor).
    pub effective: String,
    /// Provenance of the effective value: `"user"` (sticky per-project pick),
    /// or `"auto"` (inherited from the machine-global default — covers
    /// `source=auto`, a legacy NO-marker row, or no per-project row at all).
    pub source: String,
}

/// Tauri command — Settings-tab per-project embedding picker READ path.
///
/// Returns the EFFECTIVE active-embedding profile (post-cascade) plus its
/// provenance so the picker can show the current selection and whether it's
/// a sticky user pick or inherited from the global default.
#[tauri::command]
pub async fn get_project_active_embedding(
    project_id: String,
    db: tauri::State<'_, Db>,
) -> Result<ActiveEmbeddingState, String> {
    if project_id.is_empty() {
        return Err("get_project_active_embedding: project_id required".into());
    }
    let is_user = db
        .get_setting(
            &project_id,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
        )
        .ok()
        .flatten()
        .and_then(|v| v.as_str().map(String::from))
        .as_deref()
        == Some(ACTIVE_EMBEDDING_SOURCE_USER);
    let effective = resolve_active_embedding_cascade(&db, Some(&project_id));
    Ok(ActiveEmbeddingState {
        effective,
        source: if is_user {
            ACTIVE_EMBEDDING_SOURCE_USER.to_string()
        } else {
            ACTIVE_EMBEDDING_SOURCE_AUTO.to_string()
        },
    })
}

/// Canonical shared-KG class name — LAST-RESORT FALLBACK.
///
/// **v0.2.40 W40-C rename** (was `DEFAULT_SHARED_KG_COLLECTION`): renamed
/// to `LAST_RESORT_*` so call sites that bypass the DB-read chain become
/// audit-able via `grep LAST_RESORT_SHARED_KG_COLLECTION`. The const
/// value is unchanged; the rename is purely a discipline signal that
/// this value is the END of the resolution chain, not the first choice.
///
/// **Resolution chain** (highest to lowest, all roads end here only if
/// every higher-priority source is empty):
///
///   1. `app_state[shared_kg.collection_name]` — explicit GUI override.
///   2. The orchestrator-root project's PRIMARY KG binding — reads
///      `project_kg_bindings(slug='orchestrator-root', role='primary').
///      collection_name`. This is the SOURCE OF TRUTH for the shared-KG
///      name on every machine where the orchestrator-root project is
///      registered (which is every machine that has run the launcher
///      at least once).
///   3. `LAST_RESORT_SHARED_KG_COLLECTION` (this const). Only fires on
///      a totally-fresh-fresh first boot before any project is created,
///      OR in tests with an empty in-memory DB. In production, callers
///      should essentially never see this value.
///
/// Legs 1+2 are NOT implemented here: they live in
/// [`crate::commands::project_state_populate::shared_kg_binding::
/// resolve_shared_kg_collection`], which is the single home for the
/// DB-backed chain (v0.2.92 W12 wiring). This module owns only leg 3 —
/// the reader-side last resort that turns that function's `None` into a
/// name. That split is the whole point of the const: a WRITER must never
/// materialise leg 3 into a binding row (see the W12 header in
/// `shared_kg_binding.rs`), so the DB-backed resolver deliberately has no
/// access to it.
///
/// Must stay in lockstep with:
///   * `vco_lib/project_init.py::_SHARED_KG_NAME`
///   * `claude_mcp_servers/weaviate_mcp/server.py::_SHARED_KG_DEFAULT`
///   * `scripts/migrate-shared-kg-schema.{sh,ps1}` defaults
///
/// Cross-language invariant test
/// `tests/test_shared_kg_constant_consistency.py` pins these together so
/// any drift fails CI loudly. The test parses this `.rs` file by const
/// name; renaming required updating the test in lockstep (which v0.2.40
/// W40-C did).
///
/// v0.2.23 B1 (2026-05-21): casing flipped from lowercase-c "Vibecoded"
/// (the v0.2.12–v0.2.22 default) back to capital-C "VibeCoded" to match
/// the brand spelling. Case-insensitive adoption in
/// `install.py::_ensure_collections` plus the binding-row self-heal step
/// in `install.py::_self_heal_kg_bindings_on_update` ensure existing
/// installs with the lowercase-c class are adopted in place — no rename,
/// no data loss, no re-embedding.
pub const LAST_RESORT_SHARED_KG_COLLECTION: &str = "VibeCodedOrchestrator_KnowledgeGraph";

/// Legacy shared-KG class name (pre-v0.2.12 PR-26 rename). Used ONLY by
/// migration-detection paths (e.g., `commands::kg::list_kg_collections`
/// recognizing a pre-rename class still living on disk). DO NOT use as a
/// default for new writes — picker-driven migration is the consent
/// mechanism for renaming the on-disk class.
///
/// v0.2.53 (Track E, DC-1): live Rust readers were all migrated to
/// `is_shared_kg_class_name` (extracted in v0.2.24 B4); the constant
/// itself is now consumed only by `is_shared_kg_class_name` (kept for
/// future migration-detection paths) and by the cross-language pin in
/// `tests/test_shared_kg_constant_consistency.py` (regex-parses this
/// `.rs` file by const name to guarantee Python `_LEGACY_SHARED_KG_NAME`
/// stays in lockstep). The Rust-side `#[allow(dead_code)]` silences the
/// cargo warning without breaking that cross-language contract.
/// Detection-only consumers in `vco_lib/`, `install.py`, `kg.rs`, and
/// `access.rs` use inline string literals (intentionally — the cross-
/// language pin protects against drift) rather than this constant; if
/// you find yourself wanting to delete this entirely, also remove the
/// Python lockstep test in the same commit.
#[allow(dead_code)]
pub const LEGACY_SHARED_KG_COLLECTION: &str = "VibeCodedTools_KnowledgeGraph";

/// Lowercase-c variant of the canonical name (PR-34 / v0.2.12 default
/// through v0.2.22). v0.2.23 B1 flipped the canonical to capital-C to
/// match the brand spelling; this constant pins the prior default as a
/// legacy alias so case-insensitive-adoption code recognises a user
/// Weaviate that still carries the lowercase-c class.
///
/// Same DO-NOT-USE-FOR-WRITES contract as `LEGACY_SHARED_KG_COLLECTION`:
/// detection only. Install.py's case-insensitive adoption logic rebinds
/// the resolved `SHARED_KG_COLLECTION` env value to whatever the live
/// class actually is, so downstream writes always target the on-disk
/// casing.
///
/// v0.2.53 (Track E, DC-2): same `#[allow(dead_code)]` treatment as
/// `LEGACY_SHARED_KG_COLLECTION` — see the rationale on that constant
/// for why the constant is retained rather than deleted.
#[allow(dead_code)]
pub const LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C: &str =
    "VibecodedOrchestrator_KnowledgeGraph";

/// Returns `true` iff `name` is recognised as a shared-KG class name,
/// accounting for legacy casing variants.
///
/// Recognises:
/// * `canonical` (case-insensitive) — the active canonical shared-KG name
///   for this install. Production call sites pass
///   [`LAST_RESORT_SHARED_KG_COLLECTION`]; tests and white-label forks may pass
///   a different value (e.g. `"AcmeOrchestrator_KnowledgeGraph"`).
/// * [`LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C`] — the v0.2.12–v0.2.22
///   lowercase-c default. Always recognised so pre-v0.2.23-B1 installs are
///   still detected even when the user has flipped to a custom canonical.
/// * [`LEGACY_SHARED_KG_COLLECTION`] (`VibeCodedTools_KnowledgeGraph`) —
///   the pre-v0.2.12-PR-26 default. Recognised for back-compat with
///   installs that never ran the PR-26 rename.
///
/// v0.2.24 B4 (2026-05-22): extracted from inline match logic that lived
/// in `commands/kg.rs::kg_list_collections` (strict `==`, MISSED case-
/// folded canonical) and `commands/maintenance.rs::parse_schema_response`
/// (case-insensitive on canonical, strict `==` on legacy). The unified
/// helper applies case-insensitive matching to ALL three names — strictly
/// a widening of recognition, never narrowing. See peer-review-B HIGH-2
/// (v0.2.23) for the original maintenance.rs fix this consolidates.
///
/// v0.2.53 (Track E, DC-3): live Rust call sites (`commands/kg.rs::
/// kg_list_collections` + `commands/maintenance.rs::parse_schema_response`)
/// were swept to inline literal-match logic during the v0.2.24-v0.2.40
/// refactor cycle, leaving this helper with only its own unit tests as
/// consumers. Cargo reports it as dead. Retained behind
/// `#[allow(dead_code)]` because:
///   1. The 7 unit tests at the bottom of this file are the canonical
///      reference for the case-folded matching semantics — deleting the
///      helper means deleting them too, losing the executable spec.
///   2. Future call sites (vct-hub migration paths, white-label class
///      detection) are likely to need this same matcher; rewriting it
///      from the inline literals would be a regression.
/// If a future cycle confirms no caller will ever resurrect, delete the
/// fn + its 7 unit tests + this comment block in one commit.
#[allow(dead_code)]
pub fn is_shared_kg_class_name(name: &str, canonical: &str) -> bool {
    name.eq_ignore_ascii_case(canonical)
        || name.eq_ignore_ascii_case(LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C)
        || name.eq_ignore_ascii_case(LEGACY_SHARED_KG_COLLECTION)
}

/// Populated once per project-env write call. Its ports are forwarded to
/// the project-root `.env` writer (`vco_lib.env_template`, and its safe-add
/// sidecar) so future launcher-state values can be added here without
/// re-threading every call site.
///
/// String-typed for trivial JSON / TOML serialisation in tests; the fields
/// are typed numerically only where a u16 is unambiguously a port.
#[derive(Debug, Clone)]
pub struct ProjectEnvSettings {
    /// Embedding profile (`qwen3` / `openai` / `arctic` / `codesage`).
    /// Read from `app_state` key `embedding.active_profile`; default `"qwen3"`.
    pub active_embedding: String,

    /// Per-service URLs. Composed from the resolved port + the
    /// canonical scheme/host. (v0.2.97: no `code_embed_url` — its one
    /// reader, the retired Rust `.env` renderer, is superseded by
    /// `vco_lib.env_template`, which composes `CODE_EMBED_URL` from the
    /// forwarded `code_embed_port` the same way.)
    pub weaviate_url: String,
    pub ollama_url: String,

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
    /// `"VibeCodedOrchestrator_KnowledgeGraph"` (since v0.2.23 B1; was
    /// `"VibecodedOrchestrator_KnowledgeGraph"` v0.2.12–v0.2.22, itself
    /// renamed from `"VibeCodedTools_KnowledgeGraph"` in v0.2.12 PR-26);
    /// overridable via app_state.
    pub shared_kg_collection: String,

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
            weaviate_port: DEFAULT_WEAVIATE_PORT,
            ollama_port: DEFAULT_OLLAMA_PORT,
            code_embed_port: DEFAULT_CODE_EMBED_PORT,
            container_runtime: None,
            // v0.2.84 PLAN-v0284 D1 (review F4): name-derived KG/dev names here are the
            // SANCTIONED last resort — `with_defaults` has NO `Db` handle, so there is no
            // `project_kg_bindings` row to honor. D1's rule ("primary = binding when a row
            // exists; else `sanitize(name)_KnowledgeGraph`") reduces to the else-branch when
            // no binding is reachable; `dev` is the `_KnowledgeGraph`→`_Development` suffix
            // swap of that basename. Any DB-backed caller goes through `populate` →
            // `collection_naming::resolve_project_collections` (the binding-first path); this
            // default-only constructor never overrides a binding because it can't see one.
            kg_collection: format!("{}_KnowledgeGraph", kg_basename),
            dev_collection: format!("{}_Development", kg_basename),
            shared_kg_collection: LAST_RESORT_SHARED_KG_COLLECTION.to_string(),
            cpu_only: true,
            use_gpu: false,
            project_name: project_name.to_string(),
            kg_access_list: Vec::new(),
            code_graph_access_list: Vec::new(),
        }
    }
}

/// The code-embedding service port exactly as [`populate`] resolves it for
/// the project env projection (its `service_endpoints` row, else 11440).
/// v0.2.97: the Core module settings panel shows it read-only for
/// `vct-code-embedding`'s `CODE_EMBED_PORT`.
pub fn resolve_code_embed_port(db: &Db) -> u16 {
    endpoints::machine_port(db, CoreService::CodeEmbed)
}

/// Detect the container runtime synchronously without spawning child
/// processes. Returns `Some("podman")`, `Some("docker")`, or `None`.
/// Synchronous because populate runs from non-async callers
/// (the env writers); a runtime probe via `which` is sufficient
/// — a full-fledged version check happens later via `detect_system`.
///
/// Honors `VCT_CONTAINER_RUNTIME=podman|docker|auto` env var as the
/// user's explicit preference (v0.2.14 Bug #3 fix). If set to a
/// recognized value AND that runtime is on PATH, returns it directly;
/// else falls through to auto-detect (podman first, docker second).
/// This matches the contract honored by `services/runtime.rs::resolve_runtime`,
/// `install.py::_runtime_preference_from_env`, the hook scripts, and
/// the boot wrapper.
fn detect_runtime_sync() -> Option<String> {
    if let Ok(raw) = std::env::var("VCT_CONTAINER_RUNTIME") {
        let pref = raw.trim().to_ascii_lowercase();
        if pref == "podman" || pref == "docker" {
            if which_cmd(&pref).is_some() {
                return Some(pref);
            }
            // Preference set but not installed — fall through to auto-detect.
            // (Lenient: don't strand the user on a misconfigured env var.)
        }
        // "auto" / "" / unknown → fall through.
    }
    if which_cmd("podman").is_some() {
        return Some("podman".to_string());
    }
    if which_cmd("docker").is_some() {
        return Some("docker".to_string());
    }
    None
}

/// Minimal `which` — walk `PATH` and look for an executable file.
///
/// v0.2.77 (Part 7c task 3): delegates to the shared
/// `vct_launcher_core::paths::which_on_path` (one home). Behaviour is
/// preserved on POSIX and upgraded on Windows (the shared form also probes
/// `.cmd`/`.bat`, not just `.exe`). Kept the local name so the two
/// `.is_some()` call-sites are undisturbed.
fn which_cmd(name: &str) -> Option<std::path::PathBuf> {
    vct_launcher_core::paths::which_on_path(name)
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
    // v0.2.97: the Weaviate URL comes from the ONE resolver the hub's
    // `/config` also calls (`service_endpoints::machine_weaviate_url`: the
    // launcher.db `service_endpoints` row, else `http://localhost:8081`). The
    // port is the one that URL addresses, so `WEAVIATE_PORT` cannot disagree
    // with it.
    let weaviate_url = endpoints::machine_weaviate_url(db);
    let weaviate_port = endpoints::weaviate_port_for_url(&weaviate_url);
    let ollama_port = endpoints::machine_port(db, CoreService::Ollama);
    let code_embed_port = endpoints::machine_port(db, CoreService::CodeEmbed);

    // v0.2.71 T-B-emb: resolve via the ONE shared cascade. Sticky per-project
    // user pick (module_settings/orchestrator-core/active_embedding WHERE
    // source=user) → machine-global default (app_state[embedding.active_profile]
    // → hardware-pick derive) → qwen3. The same fn backs the hub resolver +
    // the Settings-tab picker; `config_projection.py` mirrors it for the
    // canonical .claude/{settings.json,env} writer.
    //
    // v0.2.69 FIX 1 (Defect D add-path gap) carried forward inside
    // `global_active_embedding`: when the canonical `embedding.active_profile`
    // key is empty/absent (the usual state on a fresh add — only the GUI
    // Identity-tab chooser writes it), derive the profile from the machine's
    // hardware pick (`app_state[default_text_embedding]`) BEFORE falling to
    // "qwen3" (conservative: never stamp a guessed profile → wrong vector slot).
    //
    // NOTE: this Rust populate() value feeds the `.env` template +
    // SecretsPanel surfaces, NOT the canonical `.claude/{settings.json,env}`
    // (those come from the Python `config_projection` writer, which mirrors
    // this cascade). Both are fixed in lockstep so the two surfaces agree.
    let active_embedding = resolve_active_embedding_cascade(db, project_id);

    // PR-9 (v0.2.11): shared KG resolution with three-tier priority.
    //
    // v0.2.72 R1 (F5 residual): the Priority-1 app_state override MUST
    // MATCH the other two SHARED_KG_COLLECTION resolvers —
    // `vco_lib/config_projection.py::project_env_from_db` (the canonical
    // .claude/{settings.json,env} writer) and the hub resolver in
    // `launcher/src-tauri/vct-hub/src/config_api.rs` — all three honor a
    // non-empty `app_state[shared_kg.collection_name]` first. Pre-R1 only
    // this populate() did, so the three surfaces disagreed whenever the
    // SharedKgPicker override was set.
    //
    // Priority 1: explicit user override in `app_state` (preserves any
    //             manually-set value via the GUI's existing setting).
    // Priority 2: Orchestrator Project's primary KG binding from
    //             `project_kg_bindings`. Seeded by
    //             `orchestrator_root::ensure_orchestrator_root_kg_binding`
    //             on launcher boot whenever the orchestrator clone is
    //             detected. This makes every project on the machine
    //             derive the shared KG from the same source of truth:
    //             the Orchestrator Project itself.
    // Priority 3: `LAST_RESORT_SHARED_KG_COLLECTION` const fallback. Kept
    //             for two scenarios:
    //               (a) standalone-binary install (no clone → no row
    //                   → no binding);
    //               (b) tests with an empty in-memory DB.
    //
    // Explicit empty string (`SHARED_KG_COLLECTION=""`) handling: a
    // user who has explicitly set `app_state[shared_kg.collection_name]`
    // to "" gets back LAST_RESORT_SHARED_KG_COLLECTION here. That's fine —
    // the per-project gate `SHARED_KG_WRITE_DISABLED` (resolved below)
    // is the right knob for "opt out of shared KG writes". Forcing
    // SHARED_KG_COLLECTION to be empty would break the read path too,
    // which the asymmetric-access model since 2026-05-01 explicitly
    // says must never be empty.
    //
    // v0.2.92 W12 (Task 3) — legs 1+2 are NOT re-implemented here. They live
    // in `shared_kg_binding::resolve_shared_kg_collection`, the single home
    // for the DB-backed chain; this call site adds ONLY leg 3, the
    // reader-side last resort. Before the unification the same two legs were
    // spelled out here and again in `shared_kg_binding` — two copies of a
    // priority order that MUST agree, which is precisely the drift CLAUDE.md's
    // "one concern, one home" rule exists to prevent.
    let shared_kg_collection =
        crate::commands::project_state_populate::shared_kg_binding::
            resolve_shared_kg_collection(db)
            .unwrap_or_else(|| LAST_RESORT_SHARED_KG_COLLECTION.to_string());

    let use_gpu = db
        .app_state_get_bool(APP_STATE_KEY_USE_GPU)
        .ok()
        .flatten()
        .unwrap_or(false);

    // v0.2.84 D1 (P2): KG + dev collection names come from the ONE rule in
    // `vct_launcher_core::collection_naming::resolve_project_collections` —
    // binding-first (`project_kg_bindings(role='primary')`) with a
    // name-derived last resort, dev/diagrams by suffix-swap off the
    // resolved KG (slug fallback for a custom-rename primary). This is the
    // SAME rule the hub's `config_api` and the python `config_projection`
    // delegate to, closing the v0.2.83 dogfood P2 drift where `populate()`
    // name-derived the dev name from the DISPLAY name and stranded the
    // binding-paired `<KG>_Development` docs store (was
    // `sanitize_kg_collection(project_name)` here — the exact violation
    // the file's own SSOT comment below flags for CODE_GRAPH_PROJECT).
    //
    // Slug: resolve from the project row when we have a project_id (drives
    // the dev/diagrams non-canonical fallback, matching the hub's
    // `project.slug`); None in test contexts → the rule seeds the fallback
    // off the name (never reached when the primary is canonical).
    let project_slug: Option<String> = project_id
        .and_then(|pid| db.get_project(pid).ok().flatten())
        .map(|row| row.slug);
    let collections = crate::collection_naming::resolve_project_collections(
        db,
        project_id,
        project_name,
        project_slug.as_deref(),
    );
    let own_kg = collections.kg;
    let own_dev = collections.dev;

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

    ProjectEnvSettings {
        active_embedding,
        weaviate_url,
        ollama_url: format!("http://localhost:{}", ollama_port),
        weaviate_port,
        ollama_port,
        code_embed_port,
        container_runtime: detect_runtime_sync(),
        kg_collection: own_kg,
        dev_collection: own_dev,
        shared_kg_collection,
        cpu_only: !use_gpu,
        use_gpu,
        project_name: project_name.to_string(),
        kg_access_list,
        code_graph_access_list,
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
/// project NAMES. Sorted for deterministic env output. Soft-fail to empty
/// list on any DB error.
///
/// v0.2.80 (GAP-CG-1, third producer): this writer previously emitted the
/// SANITIZED PREFIX (`sanitize_kg_collection(name)`) — neither the slug the
/// hub/Python producers used to emit nor the NAME the consumers expect. The
/// `VCT_CODE_GRAPH_ACCESS_LIST` contract is grantor NAMES (what the analyzer
/// stamped in the `project` property; consumers derive the class prefix
/// themselves) — converging with `config_api.rs::
/// list_codegraph_grantor_names_for_grantee` and
/// `config_projection.py::_fetch_code_graph_access_list`. A prefix-form entry
/// made the MCP's `project`-property filter miss every peer row (silent
/// zeros) and kept REGRESSING the env value on every launcher-side refresh
/// after install.py's name-form re-projection.
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
        match db.get_project(&grantor_id) {
            Ok(Some(row)) => {
                let name = row.name.trim().to_string();
                if !name.is_empty() {
                    peers.insert(name);
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

// v0.2.92 W12 (Task 3) — `resolve_shared_kg_from_orchestrator_root` USED TO
// LIVE HERE (PR-9, v0.2.11). It was a private copy of legs 1+2 of the
// shared-KG resolution chain; `shared_kg_binding::resolve_shared_kg_collection`
// (v0.2.92 W12) was a second copy, written because this one was private and
// unreachable from the new module. Two implementations of one priority order
// is the duplication CLAUDE.md forbids, so the copies were collapsed onto the
// `shared_kg_binding` one and this function deleted; `populate` now calls it
// and appends `LAST_RESORT_SHARED_KG_COLLECTION` for its reader-side answer.
//
// A THIRD implementation survives on purpose in
// `launcher/src-tauri/vct-hub/src/config_api.rs`: `vct-hub` is a separate
// crate that depends only on `vct-launcher-core`, NOT on this binary crate,
// so it cannot call this code at all. That boundary is real, not laziness —
// it is pinned behaviourally instead, by the shared truth table in
// `shared_kg_binding::tests::shared_resolution_truth_table` and its hub-side
// twin `config_api::tests::shared_resolution_truth_table_matches_launcher`.

/// W40-B (v0.2.40): decide whether a project's env files need
/// regeneration based on binding-row freshness vs env-file mtime.
///
/// Returns `true` iff the most recent `updated_at` across the
/// project's KG + codegraph binding rows is strictly newer than the
/// env file's modification time. Used by the launcher boot path to
/// auto-refresh per-project `.claude/settings.json` + `.claude/env`
/// after a binding has been adopted to a different collection name
/// (the `adopt_populated_collections_at_boot` self-heal in
/// `vct-launcher-core`).
///
/// Soft-fail contract:
///   * No bindings for the project → `false` (nothing to compare).
///   * Env file missing → `false`. Caller should NOT trigger a
///     refresh on a project that's never had env files written —
///     the regular create-project / populate path owns that. The
///     boot regen is strictly a "stale env" healer, not a first-time
///     creator.
///   * mtime unreadable → `false`. Better to skip a refresh than
///     to spam regeneration on every boot for a project whose
///     filesystem timestamps are flaky.
///
/// Performance: 1 SQLite read (bounded set of binding rows per
/// project) + 1 `metadata()` call. Bounded; safe to call once per
/// project at boot.
pub fn should_regenerate_env_for_project(
    db: &Db,
    project_id: &str,
    env_file_path: &std::path::Path,
) -> bool {
    // Collect the latest binding update timestamp from KG + codegraph.
    let kg_bindings = match db.list_project_kg_bindings(project_id) {
        Ok(v) => v,
        Err(_) => return false,
    };
    let codegraph = db.get_project_codegraph_binding(project_id).ok().flatten();

    let mut db_max_ms: Option<i64> = None;
    for b in &kg_bindings {
        db_max_ms = Some(db_max_ms.map_or(b.updated_at, |m| m.max(b.updated_at)));
    }
    if let Some(cb) = &codegraph {
        db_max_ms = Some(db_max_ms.map_or(cb.updated_at, |m| m.max(cb.updated_at)));
    }
    let Some(db_max_ms) = db_max_ms else {
        // No bindings at all — nothing has been written that the env
        // could be lagging behind.
        return false;
    };

    let meta = match std::fs::metadata(env_file_path) {
        Ok(m) => m,
        Err(_) => return false, // env file missing or unreadable
    };
    let mtime = match meta.modified() {
        Ok(t) => t,
        Err(_) => return false,
    };
    // Convert env-file mtime to epoch milliseconds for comparison.
    let env_ms = match mtime.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_millis() as i64,
        Err(_) => return false, // mtime before UNIX epoch — improbable, skip
    };

    db_max_ms > env_ms
}

#[cfg(test)]
mod tests {
    use super::*;

    /// v0.2.97 (lane T): the bundled code-embedding manifest's health URL
    /// names the port THIS resolver answers (it said 11438, a port nothing
    /// serves). Lane W: it names it through `{code_embed_port}`, so the two
    /// agree under an override as well as on a machine with none.
    #[test]
    fn code_embedding_health_url_is_the_resolvers_default() {
        let _state = vct_launcher_core::test_env::state_dir_guard();
        let db = Db::open().unwrap();
        let (_, body) = vct_launcher_core::bundled_manifests::BUNDLED_MANIFESTS
            .iter()
            .find(|(name, _)| *name == "vct-code-embedding.json")
            .unwrap();
        let manifest: serde_json::Value = serde_json::from_str(body).unwrap();
        let url = manifest["runtime"]["health_check"]["url"].as_str().unwrap().to_string();
        let ctx = vct_launcher_core::manifest::PlaceholderCtx::new("vct-code-embedding");
        let expect = || format!("http://localhost:{}/health", resolve_code_embed_port(&db));
        assert_eq!(ctx.resolve(&url), expect());
        assert_eq!(resolve_code_embed_port(&db), DEFAULT_CODE_EMBED_PORT);
        db.app_state_set(endpoints::APP_STATE_KEY_CODE_EMBED_PORT, "21441").unwrap();
        assert_eq!(resolve_code_embed_port(&db), DEFAULT_CODE_EMBED_PORT, "retired key");
        db.service_endpoint_seed_for_tests(&vct_launcher_core::db::service_endpoints::ServiceEndpointRow::new(
            "code_embed",
            vct_launcher_core::db::service_endpoints::EndpointMode::VcoManaged,
            "localhost",
            21440,
        ))
        .unwrap();
        assert_eq!(ctx.resolve(&url), expect());
        assert_eq!(resolve_code_embed_port(&db), 21440);
    }

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
        assert_eq!(s.shared_kg_collection, "VibeCodedOrchestrator_KnowledgeGraph");
        assert_eq!(s.weaviate_url, "http://localhost:8081");
        assert_eq!(s.ollama_url, "http://localhost:11435");
        assert_eq!(s.code_embed_port, 11440);
        assert_eq!(s.active_embedding, "qwen3");
        assert!(!s.use_gpu);
        assert!(s.cpu_only);
    }

    #[test]
    fn resolve_code_graph_access_peers_emits_grantor_names_not_prefixes() {
        // v0.2.80 GAP-CG-1 third producer: the env writer must emit the
        // grantor's NAME — not the sanitized class prefix it used to emit,
        // and not the slug the hub/Python producers used to emit. Fixture
        // deliberately has name ≠ slug ≠ prefix ("Client Alpha" /
        // "client-alpha" / "ClientAlpha") so any regression to either wrong
        // form fails. Act + leave-alone: non-read grants and self-grants are
        // excluded.
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().unwrap();
        db.insert_project("me", "My_Project", "/tmp/me", ProjectHost::Base, "my-project")
            .unwrap();
        db.insert_project(
            "peer",
            "Client Alpha",
            "/tmp/peer",
            ProjectHost::Base,
            "client-alpha",
        )
        .unwrap();
        db.insert_project("none", "No Grant", "/tmp/none", ProjectHost::Base, "no-grant")
            .unwrap();
        db.codegraph_grant("peer", "me", "read").unwrap();
        db.codegraph_grant("none", "me", "none").unwrap();

        let peers = resolve_code_graph_access_peers(&db, "me");
        assert_eq!(peers, vec!["Client Alpha".to_string()]);
        // Explicit negatives: neither the prefix nor the slug form.
        assert!(!peers.contains(&"ClientAlpha".to_string()));
        assert!(!peers.contains(&"client-alpha".to_string()));
    }

    // The override / parallel / adopt / refuse port legs these tests used to
    // pin one by one are cases in `tests/fixtures/service_endpoint_parity.json`
    // now, executed against the ONE resolver in
    // `vct_launcher_core::services::service_endpoints` (and its Python mirror).

    /// v0.2.97: the projection's Weaviate URL IS the machine resolver's —
    /// the value the hub's `/config` serves, from the `service_endpoints`
    /// row. An adopted external Weaviate keeps its host, and `WEAVIATE_PORT`
    /// is that URL's port. The retired statement env var and app_state
    /// override move nothing.
    #[test]
    fn populate_weaviate_url_is_the_machine_resolvers() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let _state = vct_launcher_core::test_env::state_dir_guard_with(&[
            (endpoints::STATEMENT_ENV, Some("http://statement.invalid:1")),
        ]);
        let db = Db::open_in_memory().unwrap();
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedExternal, "weaviate.lan", 8090);
        row.grpc_port = Some(50051);
        db.service_endpoint_seed_for_tests(&row).unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.weaviate_url, "http://weaviate.lan:8090");
        assert_eq!(s.weaviate_url, endpoints::machine_weaviate_url(&db));
        assert_eq!(s.weaviate_port, 8090);

        db.app_state_set(endpoints::APP_STATE_KEY_OLLAMA_PORT, "21436").unwrap();
        assert_eq!(populate(&db, "Acme", None).ollama_port, DEFAULT_OLLAMA_PORT);
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "ollama",
            EndpointMode::AdoptedExternal,
            "localhost",
            21435,
        ))
        .unwrap();
        assert_eq!(populate(&db, "Acme", None).ollama_port, 21435);
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
        assert_eq!(s.shared_kg_collection, "VibeCodedOrchestrator_KnowledgeGraph");
    }

    #[test]
    fn populate_honors_active_embedding_override() {
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "openai").unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "openai");
    }

    #[test]
    fn active_profile_for_model_maps_known_ids() {
        // Mirror of install.py::_TEXT_MODEL_ACTIVE_EMBEDDING — must stay in
        // lockstep with the Python map.
        assert_eq!(active_profile_for_model("qwen3-embedding:0.6b"), Some("qwen3"));
        assert_eq!(
            active_profile_for_model("snowflake-arctic-embed2:latest"),
            Some("arctic")
        );
        assert_eq!(
            active_profile_for_model("openai-text-embedding-3-small"),
            Some("openai")
        );
        assert_eq!(
            active_profile_for_model("text-embedding-3-small"),
            Some("openai")
        );
        // Unknown id → no profile (conservative: leave canonical key untouched).
        assert_eq!(active_profile_for_model("some-future-model"), None);
    }

    #[test]
    fn chooser_stamps_canonical_profile_so_populate_resolves_arctic() {
        // v0.2.68 Defect D regression guard. The chooser writes only the
        // model id (`default_text_embedding`); the canonical profile key
        // (`embedding.active_profile`) starts ABSENT. Before the fix,
        // populate() fell through to "qwen3" for an arctic pick. After the
        // fix the helper stamps the derived profile so the ENV output is
        // "arctic".
        let db = Db::open_in_memory().unwrap();

        // Pre-condition: canonical key genuinely unset.
        assert!(db
            .app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
            .unwrap()
            .is_none());

        // Simulate the GUI chooser selecting arctic.
        set_text_embedding_and_profile(&db, "snowflake-arctic-embed2:latest").unwrap();

        // The canonical key is now populated with the derived profile...
        assert_eq!(
            db.app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING).unwrap().as_deref(),
            Some("arctic")
        );
        // ...and the model id key is written too.
        assert_eq!(
            db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).unwrap().as_deref(),
            Some("snowflake-arctic-embed2:latest")
        );

        // The ENV that lands in .claude/settings.json + .claude/env is
        // "arctic", NOT the "qwen3" fallback.
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "arctic");
    }

    #[test]
    fn chooser_unknown_model_leaves_canonical_key_untouched() {
        // An id with no profile mapping must NOT stamp a guessed profile —
        // populate() then keeps its canonical "qwen3" fallback.
        let db = Db::open_in_memory().unwrap();
        set_text_embedding_and_profile(&db, "some-future-model").unwrap();
        assert!(db
            .app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
            .unwrap()
            .is_none());
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "qwen3");
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
    fn populate_derives_arctic_from_default_text_embedding_when_canonical_absent() {
        // v0.2.69 FIX 1 (Defect D add-path gap). A fresh add never writes
        // the canonical `embedding.active_profile` key (only the GUI
        // Identity-tab chooser does). install.py's hardware chooser DOES
        // write `default_text_embedding` (the model id). Before the fix
        // populate() fell straight to "qwen3" on an arctic host; after the
        // fix it derives "arctic" from the hardware pick.
        let db = Db::open_in_memory().unwrap();

        // Canonical profile key genuinely ABSENT...
        assert!(db
            .app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING)
            .unwrap()
            .is_none());
        // ...but the hardware pick (arctic) IS present.
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();

        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "arctic");
    }

    #[test]
    fn populate_unknown_default_text_embedding_stays_qwen3() {
        // Conservative guard: an unmapped hardware pick must NOT stamp a
        // guessed profile — populate() keeps the qwen3 fallback.
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "some-future-model")
            .unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, DEFAULT_ACTIVE_EMBEDDING);
    }

    // ─── F5 (v0.2.72): env re-projection after MCP-relevant DB writes ──

    /// `set_project_active_embedding_with_db` writes the sticky pick AND
    /// re-projects the project's env. Proof of the refresh: the returned
    /// `RefreshProjectEnvResult` carries the access list only the refresh
    /// path (populate) computes — seeded here so the value is
    /// deterministic. Previously the refresh was delegated to "the
    /// caller"; the model-switch modal never did it → stale MCP env.
    #[test]
    fn set_project_active_embedding_persists_and_reprojects() {
        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "p-f5-emb",
            "F5Emb",
            "/nonexistent/f5-emb",
            crate::db::models::ProjectHost::Base,
            "f5emb",
        )
        .unwrap();
        db.kg_set_access("p-f5-emb", "PeerProj_KnowledgeGraph", "read")
            .unwrap();

        let result = set_project_active_embedding_with_db(&db, "p-f5-emb", "arctic")
            .expect("setter must succeed");

        // The sticky user pick landed (cascade leg 1).
        assert_eq!(
            resolve_active_embedding_cascade(&db, Some("p-f5-emb")),
            "arctic",
        );
        // The env re-projection ran (populate resolved the seeded peer).
        assert_eq!(
            result.kg_access_list,
            vec!["PeerProj".to_string()],
            "the setter must re-project env after the write",
        );
        // Empty project_id keeps its precondition error.
        assert!(set_project_active_embedding_with_db(&db, "", "arctic").is_err());
    }

    /// `set_text_embedding_and_profile` writes the machine-global default
    /// keys AND re-projects every project's env (all auto projects inherit
    /// the new value via the cascade's global leg). Proof of the
    /// refresh-all: a registered project with a missing folder lands in
    /// the returned report's `skipped` list — only the refresh path
    /// computes that.
    #[test]
    fn set_text_embedding_and_profile_reprojects_all_projects() {
        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "p-f5-glob",
            "F5Glob",
            "/nonexistent/f5-glob",
            crate::db::models::ProjectHost::Base,
            "f5glob",
        )
        .unwrap();

        let report =
            set_text_embedding_and_profile(&db, "snowflake-arctic-embed2:latest")
                .expect("global setter must succeed");

        // Both keys written (pre-existing contract)...
        assert_eq!(
            db.app_state_get(APP_STATE_KEY_ACTIVE_EMBEDDING).unwrap().as_deref(),
            Some("arctic"),
        );
        // ...and the machine-global refresh iterated the projects.
        assert!(
            report.skipped.contains(&"F5Glob".to_string()),
            "refresh-all must have run over registered projects; got {:?}",
            report,
        );
    }

    /// The generic-app_state-write predicate: the ACTIVE_EMBEDDING cascade keys
    /// AND the v0.2.73 machine-global RL telemetry opt-outs trigger a
    /// machine-global re-projection; launcher-state flags and the
    /// (projection-inert) shared-KG-name override do not.
    #[test]
    fn app_state_reprojection_predicate_covers_cascade_keys_only() {
        assert!(app_state_key_triggers_env_reprojection(
            APP_STATE_KEY_ACTIVE_EMBEDDING
        ));
        assert!(app_state_key_triggers_env_reprojection(
            APP_STATE_DEFAULT_TEXT_EMBED
        ));
        // v0.2.73 Concern-A/C: the GLOBAL RL opt-outs are written via the
        // generic app_state_set_bool command, so they MUST trigger re-projection.
        assert!(app_state_key_triggers_env_reprojection(
            APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL
        ));
        assert!(app_state_key_triggers_env_reprojection(
            APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL
        ));
        assert!(!app_state_key_triggers_env_reprojection(
            APP_STATE_KEY_SHARED_KG_NAME
        ));
        // v0.2.97: the retired port-override keys feed no projection.
        for key in [
            endpoints::APP_STATE_KEY_WEAVIATE_PORT,
            endpoints::APP_STATE_KEY_OLLAMA_PORT,
            endpoints::APP_STATE_KEY_CODE_EMBED_PORT,
        ] {
            assert!(!app_state_key_triggers_env_reprojection(key), "{key}");
        }
        assert!(!app_state_key_triggers_env_reprojection("onboarding.complete"));
        assert!(!app_state_key_triggers_env_reprojection(APP_STATE_KEY_USE_GPU));
    }

    #[test]
    fn populate_canonical_profile_wins_over_default_text_embedding() {
        // An explicit canonical pick is authoritative — the derive only
        // fires when the canonical key is empty/absent.
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "openai").unwrap();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.active_embedding, "openai");
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

    // ─── PR-9 (v0.2.11): shared KG opzione A — derive from
    //     Orchestrator Project's primary KG binding ─────────────────

    #[test]
    fn pr9_shared_kg_resolves_from_orchestrator_root_primary_binding() {
        use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;
        use crate::db::models::ProjectHost;

        let db = Db::open_in_memory().unwrap();
        let root_id = "00000000-0000-0000-0000-000000000099";
        db.insert_project(
            root_id,
            "VibeCoded Orchestrator",
            "/tmp/orchestrator-root-fake",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();
        db.set_project_kg_binding(
            root_id,
            "primary",
            "MyOrchestratorBrand_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();

        let s = populate(&db, "SomeUserProject", None);
        assert_eq!(s.shared_kg_collection, "MyOrchestratorBrand_KnowledgeGraph");
    }

    #[test]
    fn pr9_shared_kg_app_state_override_wins_over_root_binding() {
        use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;
        use crate::db::models::ProjectHost;

        let db = Db::open_in_memory().unwrap();
        let root_id = "00000000-0000-0000-0000-000000000098";
        db.insert_project(
            root_id,
            "VibeCoded Orchestrator",
            "/tmp/orchestrator-root-fake-2",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();
        db.set_project_kg_binding(
            root_id,
            "primary",
            "ShouldBeIgnored_KG",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();
        // User explicitly sets a different name via the GUI.
        db.app_state_set(APP_STATE_KEY_SHARED_KG_NAME, "UserOverride_KG").unwrap();

        let s = populate(&db, "Acme", None);
        assert_eq!(s.shared_kg_collection, "UserOverride_KG");
    }

    #[test]
    fn pr9_shared_kg_no_root_falls_back_to_default_const() {
        // Standalone-binary install scenario: migration 013 ran but
        // ensure_orchestrator_root found no clone on disk, so no
        // projects row + no primary binding. Caller must get the const.
        let db = Db::open_in_memory().unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.shared_kg_collection, LAST_RESORT_SHARED_KG_COLLECTION);
    }

    #[test]
    fn pr9_shared_kg_root_without_binding_falls_back_to_default_const() {
        // Edge case: row exists but binding never seeded (e.g. a
        // pre-PR-9 orchestrator install raced its first boot post-
        // upgrade). The resolver returns None → caller falls through.
        use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;
        use crate::db::models::ProjectHost;

        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "00000000-0000-0000-0000-000000000097",
            "VibeCoded Orchestrator",
            "/tmp/orchestrator-root-fake-3",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();
        // No binding set.
        let s = populate(&db, "Acme", None);
        assert_eq!(s.shared_kg_collection, LAST_RESORT_SHARED_KG_COLLECTION);
    }

    #[test]
    fn pr9_shared_kg_empty_binding_collection_name_falls_back_to_default() {
        // Defensive: an empty `collection_name` in the binding must not
        // propagate (would break env resolution downstream). Filter
        // empties out and fall through to const.
        use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;
        use crate::db::models::ProjectHost;

        let db = Db::open_in_memory().unwrap();
        let root_id = "00000000-0000-0000-0000-000000000096";
        db.insert_project(
            root_id,
            "VibeCoded Orchestrator",
            "/tmp/orchestrator-root-fake-4",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();
        db.set_project_kg_binding(
            root_id, "primary", "",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();
        let s = populate(&db, "Acme", None);
        assert_eq!(s.shared_kg_collection, LAST_RESORT_SHARED_KG_COLLECTION);
    }

    // ─── is_shared_kg_class_name unit tests (B4) ────────────────────────
    //
    // Pin the helper's recognition contract: the canonical name is
    // matched case-insensitively, both legacy aliases are matched
    // case-insensitively, and unrelated KG / Development collection
    // names return false. Mirrors the test list in the v0.2.24 B4
    // refactor task spec.

    #[test]
    fn is_shared_kg_class_name_recognises_canonical_casing() {
        assert!(is_shared_kg_class_name(
            "VibeCodedOrchestrator_KnowledgeGraph",
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
    }

    #[test]
    fn is_shared_kg_class_name_recognises_case_folded_canonical() {
        // Fully lowercased canonical → still a match (the v0.2.23 HIGH-2
        // fix in maintenance.rs that this helper consolidates).
        assert!(is_shared_kg_class_name(
            "vibecodedorchestrator_knowledgegraph",
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
    }

    #[test]
    fn is_shared_kg_class_name_recognises_lowercase_c_legacy_alias() {
        // The v0.2.12–v0.2.22 lowercase-c default. Detected regardless
        // of which canonical the caller passes — pre-flip installs must
        // be picked up even on a white-label fork.
        assert!(is_shared_kg_class_name(
            LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C,
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
        // Custom canonical → legacy still recognised.
        assert!(is_shared_kg_class_name(
            LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C,
            "AcmeOrchestrator_KnowledgeGraph",
        ));
    }

    #[test]
    fn is_shared_kg_class_name_recognises_pre_pr26_legacy_alias() {
        // `VibeCodedTools_KnowledgeGraph` — pre-v0.2.12 PR-26 default.
        assert!(is_shared_kg_class_name(
            LEGACY_SHARED_KG_COLLECTION,
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
        // Case-folded legacy → still detected.
        assert!(is_shared_kg_class_name(
            "vibecodedtools_knowledgegraph",
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
    }

    #[test]
    fn is_shared_kg_class_name_rejects_random_kg_collection() {
        assert!(!is_shared_kg_class_name(
            "RandomProject_KnowledgeGraph",
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
    }

    #[test]
    fn is_shared_kg_class_name_rejects_development_collection() {
        assert!(!is_shared_kg_class_name(
            "MyProject_Development",
            LAST_RESORT_SHARED_KG_COLLECTION,
        ));
    }

    #[test]
    fn is_shared_kg_class_name_accepts_custom_canonical_for_white_label() {
        // White-label forks set their own canonical. Match is
        // case-insensitive against whatever canonical the caller passes.
        assert!(is_shared_kg_class_name(
            "AcmeOrchestrator_KnowledgeGraph",
            "AcmeOrchestrator_KnowledgeGraph",
        ));
        assert!(is_shared_kg_class_name(
            "acmeorchestrator_knowledgegraph",
            "AcmeOrchestrator_KnowledgeGraph",
        ));
        // ... but a name that's neither the custom canonical NOR a
        // documented legacy alias is rejected.
        assert!(!is_shared_kg_class_name(
            "OtherTool_KnowledgeGraph",
            "AcmeOrchestrator_KnowledgeGraph",
        ));
    }

    // ─── W40-B (v0.2.40): should_regenerate_env_for_project ──────────

    /// Seed a project + a primary KG binding with `updated_at = now`.
    fn seed_project_with_kg_binding(db: &Db, project_id: &str, folder: &str) {
        use crate::db::models::ProjectHost;
        db.insert_project(
            project_id,
            project_id,
            folder,
            ProjectHost::Base,
            project_id,
        )
        .unwrap();
        db.set_project_kg_binding(
            project_id,
            "primary",
            "VCODev_KnowledgeGraph",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();
    }

    /// T8: DB binding `updated_at` is NEWER than the env file mtime →
    /// regen needed (boot-time adoption just rewrote the binding;
    /// env file is now stale).
    #[test]
    fn should_regen_returns_true_when_binding_newer_than_env_file() {
        use std::io::Write;
        let tmp = tempfile::tempdir().unwrap();
        let env_path = tmp.path().join("env");
        // Write the env file FIRST so its mtime is older than the
        // upcoming binding write.
        let mut f = std::fs::File::create(&env_path).unwrap();
        writeln!(f, "KG_COLLECTION=OldName").unwrap();
        drop(f);

        // Sleep just enough so the binding's updated_at (set to
        // chrono::now() inside set_project_kg_binding) is strictly
        // greater than the env file mtime.
        std::thread::sleep(std::time::Duration::from_millis(50));

        let db = Db::open_in_memory().unwrap();
        seed_project_with_kg_binding(&db, "p-stale", tmp.path().to_str().unwrap());

        assert!(
            should_regenerate_env_for_project(&db, "p-stale", &env_path),
            "expected true: binding is newer than env file"
        );
    }

    /// T9: env file is NEWER than the binding → no regen.
    #[test]
    fn should_regen_returns_false_when_env_file_newer_than_binding() {
        let tmp = tempfile::tempdir().unwrap();
        let env_path = tmp.path().join("env");

        let db = Db::open_in_memory().unwrap();
        seed_project_with_kg_binding(&db, "p-fresh", tmp.path().to_str().unwrap());

        // Now write the env file LATER. Ensures the env mtime > binding.updated_at.
        std::thread::sleep(std::time::Duration::from_millis(50));
        std::fs::write(&env_path, b"KG_COLLECTION=CurrentName").unwrap();

        assert!(
            !should_regenerate_env_for_project(&db, "p-fresh", &env_path),
            "expected false: env file is newer than binding"
        );
    }

    /// T10: env file missing → false (don't regen on a project that
    /// has never had env files; the regular populate path owns that).
    #[test]
    fn should_regen_returns_false_when_env_file_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let env_path = tmp.path().join("nonexistent-env");

        let db = Db::open_in_memory().unwrap();
        seed_project_with_kg_binding(&db, "p-nofile", tmp.path().to_str().unwrap());

        assert!(
            !should_regenerate_env_for_project(&db, "p-nofile", &env_path),
            "expected false: env file missing, refresh path not appropriate"
        );
    }

    // ─── v0.2.71 T-B-emb: active-embedding cascade + marker ─────────────

    /// Seed a project row so module_settings writes have a valid FK.
    fn seed_bare_project(db: &Db, name: &str) -> String {
        use crate::db::models::ProjectHost;
        let id = uuid::Uuid::new_v4().to_string();
        let folder = format!("/tmp/test-{}", id);
        db.insert_project(&id, name, &folder, ProjectHost::Base, name)
            .unwrap();
        id
    }

    /// Leg 1: a per-project row marked source=user is STICKY — returned
    /// verbatim even when the machine-global default says otherwise.
    #[test]
    fn cascade_source_user_is_sticky_over_global() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "Sticky");
        // Machine-global default is arctic.
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "arctic").unwrap();
        // But this project's user pick is openai.
        write_project_active_embedding_user(&db, &pid, "openai").unwrap();

        assert_eq!(
            resolve_active_embedding_cascade(&db, Some(&pid)),
            "openai",
            "source=user pick must win over the global default"
        );
    }

    /// Leg 2: a per-project row marked source=auto INHERITS the global
    /// default (it does NOT pin its own stored value).
    #[test]
    fn cascade_source_auto_inherits_global() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "AutoSeed");
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "arctic").unwrap();
        // Backfill-style auto seed: value qwen3 + source=auto.
        db.set_setting(
            &pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY,
            &serde_json::Value::String("qwen3".to_string()),
        ).unwrap();
        db.set_setting(
            &pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
            &serde_json::Value::String(ACTIVE_EMBEDDING_SOURCE_AUTO.to_string()),
        ).unwrap();

        assert_eq!(
            resolve_active_embedding_cascade(&db, Some(&pid)),
            "arctic",
            "source=auto must inherit the machine-global default (arctic), not pin its stored qwen3"
        );
    }

    /// Leg 2 (auto-seeded-qwen3 case): a LEGACY per-project row with NO source
    /// marker inherits the global default. This is the locked decision that
    /// fixes the auto-qwen3 bug — the brittle pre-v0.2.71 "==qwen3" heuristic
    /// is gone; provenance, not value, decides.
    #[test]
    fn cascade_legacy_no_marker_inherits_global_auto_seeded_case() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "LegacyAutoSeeded");
        // Global hardware pick is arctic.
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();
        // Legacy backfill stamped qwen3 with NO source companion.
        db.set_setting(
            &pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY,
            &serde_json::Value::String("qwen3".to_string()),
        ).unwrap();

        assert_eq!(
            resolve_active_embedding_cascade(&db, Some(&pid)),
            "arctic",
            "legacy no-marker row must inherit the global default (auto-qwen3 fix)"
        );
    }

    /// Leg 3: nothing set anywhere → qwen3 floor.
    #[test]
    fn cascade_empty_resolves_qwen3() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "Empty");
        assert_eq!(resolve_active_embedding_cascade(&db, Some(&pid)), "qwen3");
        // And with no project at all.
        assert_eq!(resolve_active_embedding_cascade(&db, None), "qwen3");
    }

    /// BRIDGE (B1): a GUI write to the global app_state profile (Identity
    /// tab) is what a non-user project's cascade resolves to — so a hub read
    /// (which uses the SAME cascade) can never disagree with the populate /
    /// projection value. Here: no user pick on the project, global=openai.
    #[test]
    fn cascade_bridge_global_app_state_reaches_non_user_project() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "Bridge");
        // GUI Identity-tab style global write.
        db.app_state_set(APP_STATE_KEY_ACTIVE_EMBEDDING, "openai").unwrap();
        // No per-project user pick → inherits global.
        assert_eq!(resolve_active_embedding_cascade(&db, Some(&pid)), "openai");
        // populate() (the .env-template surface) agrees.
        let s = populate(&db, "Bridge", Some(&pid));
        assert_eq!(s.active_embedding, "openai");
    }

    /// The writer stamps source=user, and the read command reports it.
    #[test]
    fn picker_write_then_get_reports_user_source() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "Picker");
        write_project_active_embedding_user(&db, &pid, "arctic").unwrap();
        // Stored marker is exactly "user".
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .unwrap();
        assert_eq!(src.as_str(), Some("user"));
        // Cascade returns the picked value.
        assert_eq!(resolve_active_embedding_cascade(&db, Some(&pid)), "arctic");
    }

    /// SURVIVES UPDATE: a source=user row + the projected populate() value
    /// are unchanged after a simulated update that re-runs the backfill
    /// (the auto-seed must NOT overwrite a user pick) and re-projects env.
    #[test]
    fn source_user_survives_simulated_update() {
        let db = Db::open_in_memory().unwrap();
        let pid = seed_bare_project(&db, "Survivor");
        // Machine-global default is qwen3 (the auto-seed would write qwen3).
        // The user deliberately picked openai.
        write_project_active_embedding_user(&db, &pid, "openai").unwrap();
        let before = populate(&db, "Survivor", Some(&pid)).active_embedding;
        assert_eq!(before, "openai");

        // Simulate an update: re-run the startup backfill (which writes
        // source=auto seeds for NON-user projects but must leave a user pick
        // alone) and re-project the env (populate re-derives).
        let report = crate::project_backfill::backfill_all_projects(&db);
        assert!(report.errors.is_empty(), "backfill errors: {:?}", report.errors);

        // The user pick + its marker survived...
        let value = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .unwrap();
        assert_eq!(value.as_str(), Some("openai"), "user value must survive backfill");
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .unwrap();
        assert_eq!(src.as_str(), Some("user"), "user marker must survive backfill");
        // ...and the re-projected env value is identical (not stale, re-derived).
        let after = populate(&db, "Survivor", Some(&pid)).active_embedding;
        assert_eq!(after, before, "projected ACTIVE_EMBEDDING must be unchanged across update");
    }

    /// Edge: no bindings at all → false (nothing to compare against).
    #[test]
    fn should_regen_returns_false_when_no_bindings() {
        let tmp = tempfile::tempdir().unwrap();
        let env_path = tmp.path().join("env");
        std::fs::write(&env_path, b"KG_COLLECTION=X").unwrap();

        let db = Db::open_in_memory().unwrap();
        // Note: project not inserted; list_project_kg_bindings returns
        // empty for unknown project_id.
        assert!(
            !should_regenerate_env_for_project(&db, "ghost", &env_path),
            "expected false: no binding rows → nothing to regenerate against"
        );
    }

    // ─── v0.2.84 WP-2 (P2 D1): populate() delegates to the ONE rule ─────

    /// FAIL-WITHOUT-FIX PIN (P2 no-name-derivation-when-binding-resolves):
    /// a project whose primary KG binding is `VCODev_KnowledgeGraph` but
    /// whose DISPLAY NAME name-derives to `VibeCodedOrchestrator_*` ⇒
    /// populate() yields dev `VCODev_Development` (suffix-swap off the
    /// BINDING), NOT `VibeCodedOrchestrator_Development`.
    ///
    /// Fails on the pre-fix tree where populate did
    /// `own_dev = format!("{}_Development", sanitize_kg_collection(name))`.
    #[test]
    fn populate_dev_suffix_swaps_from_binding_not_display_name() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "vco-root",
            "VibeCoded Orchestrator",
            "/tmp/vct-test-vco-root",
            ProjectHost::Base,
            "vibecoded-orchestrator",
        )
        .unwrap();
        db.set_project_kg_binding(
            "vco-root",
            "primary",
            "VCODev_KnowledgeGraph",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        let s = populate(&db, "VibeCoded Orchestrator", Some("vco-root"));
        assert_eq!(s.kg_collection, "VCODev_KnowledgeGraph");
        assert_eq!(
            s.dev_collection, "VCODev_Development",
            "populate dev must suffix-swap off the resolved binding, NOT \
             name-derive from the display name (P2 regression was \
             VibeCodedOrchestrator_Development)"
        );
    }

    /// Non-`_KnowledgeGraph` primary (custom-rename) ⇒ populate dev falls
    /// back to the slug-sanitized name, byte-matching the hub + python.
    #[test]
    fn populate_dev_slug_fallback_for_non_canonical_primary() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "p-weird",
            "Weird Project",
            "/tmp/vct-test-p-weird",
            ProjectHost::Base,
            "weirdproject",
        )
        .unwrap();
        db.set_project_kg_binding(
            "p-weird",
            "primary",
            "WeirdName_Custom",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        let s = populate(&db, "Weird Project", Some("p-weird"));
        assert_eq!(s.kg_collection, "WeirdName_Custom");
        assert_eq!(s.dev_collection, "Weirdproject_Development");
    }

    // ─── v0.2.84 WP-2 (P7 D8.1): populate() reads ZERO keychain values ──

    /// FAIL-WITHOUT-FIX PIN (P7 read-count): with the mock keychain guard,
    /// populate() for a project with an active user secret + a PAT present
    /// performs ZERO keychain VALUE reads.
    ///
    /// Counter mechanism (no secrets.rs edit): register a one-shot
    /// `fail_next_get` on the PAT slot AND the active user-secret slot.
    /// If populate READ either value, the mock consumes that fail inside
    /// populate. After populate we probe each slot with a direct
    /// `secrets::get` — a returned `Err` proves the fail was STILL pending
    /// (populate never read it → zero reads); an `Ok(value)` would prove
    /// populate consumed it (a read happened → the pin fails).
    ///
    /// Fails on the pre-fix tree: pre-D8.1 populate called
    /// `github_pat_for_env` (1 PAT read; renamed `resolve_github_pat` in
    /// v0.2.97, now read only by the PAT status surfaces) + `resolve_user_secret_state`
    /// (one `secrets::get` per active key), so both probes would return
    /// `Ok` and the assertions flip.
    #[test]
    fn populate_reads_zero_keychain_values() {
        use crate::db::models::ProjectHost;
        use crate::secrets::{self, SecretScope};

        // 2026-09-17: this test seeds the PRODUCTION GitHub-PAT tuple
        // (`shared/_user_shared_/user/github_pat`). The `MockGuard` below keeps
        // the value in a thread-local map, but the keychain baton is what makes
        // that a guarantee rather than a coincidence: it installs the hermetic
        // `vct-test-<pid>` namespace, so even a future assertion here that
        // dropped the mock could not reach the developer's real PAT.
        // `secrets::for_tests::assert_not_production_pat_slot` enforces it.
        let _kc_lock = secrets::test_serialize::keychain_serialize_lock();
        let _g = secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().unwrap();
        db.insert_project(
            "p-secrets",
            "SecretsProject",
            "/tmp/vct-test-p-secrets",
            ProjectHost::Base,
            "secretsproject",
        )
        .unwrap();
        db.set_project_kg_binding(
            "p-secrets",
            "primary",
            "SecretsProject_KnowledgeGraph",
            None, None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        // Seed a PAT value (shared-scope) + mark active. `SENTINEL_SHARED`
        // is a private installer const; use its literal value (matches
        // `config_projection._USER_SECRET_PROJECT_ID_SHARED`).
        let pat_scope = SecretScope::Shared {
            project_id: "_user_shared_",
        };
        secrets::set(
            pat_scope,
            crate::commands::installer::GITHUB_PAT_MODULE_ID,
            crate::commands::installer::GITHUB_PAT_KEY,
            "ghp_canary_value",
        )
        .unwrap();
        db.mark_secret_active(
            "shared",
            "_user_shared_",
            crate::commands::installer::GITHUB_PAT_MODULE_ID,
            crate::commands::installer::GITHUB_PAT_KEY,
        )
        .unwrap();

        // Seed an ACTIVE per-project user secret (value + active flag) so
        // pre-D8.1 populate WOULD have read its value.
        let user_scope = SecretScope::PerProject {
            project_id: "p-secrets",
        };
        secrets::set(user_scope, "user", "MY_API_KEY", "secret-value").unwrap();
        db.mark_secret_active("per_project", "p-secrets", "user", "MY_API_KEY")
            .unwrap();

        // Arm one-shot read-failures on BOTH value slots. If populate reads
        // either, it consumes the fail inside populate.
        secrets::for_tests::fail_next_get(
            crate::commands::installer::GITHUB_PAT_KEY,
        );
        secrets::for_tests::fail_next_get("MY_API_KEY");

        // v0.2.97: the settings struct no longer carries secret fields at
        // all (their only reader, the retired Rust env writer, is gone), so
        // the pending-fail probe below is the whole contract.
        let _ = populate(&db, "SecretsProject", Some("p-secrets"));

        // Probe: the fails must be STILL PENDING (populate read neither
        // value). A pending fail ⇒ direct get returns Err; a consumed fail
        // ⇒ get returns Ok (which would mean populate read it — pin fails).
        assert!(
            secrets::get(
                pat_scope,
                crate::commands::installer::GITHUB_PAT_MODULE_ID,
                crate::commands::installer::GITHUB_PAT_KEY,
            )
            .is_err(),
            "PAT fail-get was consumed → populate READ the PAT value \
             (regression: D8.1 must eliminate that read)"
        );
        assert!(
            secrets::get(user_scope, "user", "MY_API_KEY").is_err(),
            "user-secret fail-get was consumed → populate READ the value \
             (regression: D8.1 must eliminate that read)"
        );
    }

    // ─── v0.2.84 WP-2 (P7 D8.1): known-keys parity across the split ─────

}
