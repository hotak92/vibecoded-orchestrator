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

use std::path::PathBuf;

use crate::commands::installer::resolve_orchestrator_root;
// Canonical home for the `default_text_embedding` app_state key — reuse it
// here rather than re-declaring the magic string a third time (it is also
// privately re-declared in `embedding_catalog.rs`, with a sync comment).
use crate::commands::openai_cmd::APP_STATE_DEFAULT_TEXT_EMBED;
use crate::commands::projects_v2::{
    get_shared_kg_read_disabled, get_shared_kg_write_disabled, sanitize_kg_collection,
};
use crate::db::Db;
use crate::services::adoption::{self, AdoptionMode};

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
///   2. `resolve_shared_kg_from_orchestrator_root(db)` — reads
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
    /// `"VibeCodedOrchestrator_KnowledgeGraph"` (since v0.2.23 B1; was
    /// `"VibecodedOrchestrator_KnowledgeGraph"` v0.2.12–v0.2.22, itself
    /// renamed from `"VibeCodedTools_KnowledgeGraph"` in v0.2.12 PR-26);
    /// overridable via app_state.
    pub shared_kg_collection: String,

    /// Asymmetric write-gate (read of shared KG is unconditional).
    /// True ⇒ project carries `SHARED_KG_WRITE_DISABLED=true`.
    pub shared_kg_write_disabled: bool,

    /// v0.2.46 Decision B — symmetric READ gate. Mirror of
    /// `shared_kg_write_disabled` above. When `true`, the project's env
    /// surfaces carry `SHARED_KG_READ_DISABLED=true`, which the MCP's
    /// `_kg_collections_to_search` reads to drop `SHARED_KG_COLLECTION`
    /// from the hybrid_search / semantic_graph_search fan-out. Pre-
    /// v0.2.46 the read path was unconditional (asymmetric-by-default);
    /// v0.2.46 lets users opt OUT explicitly while keeping default ON.
    pub shared_kg_read_disabled: bool,

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
    /// `resolve_orchestrator_root(db)` succeeds at populate time; `None`
    /// falls through to the older behaviour where `VCT_ORCHESTRATOR_ROOT`
    /// / `VCT_INFRASTRUCTURE_DIR` are simply omitted from `.claude/env`
    /// (the in-tree hooks have a fallback resolution path). Routed via the
    /// settings struct so PR-2's value flows through PR-3's plumbing
    /// rather than being a side-channel resolver call inside the writer
    /// body.
    ///
    /// v0.2.37: switched from uncached `find_local_repo_root().ok()` to
    /// the canonical DB-cached `resolve_orchestrator_root(db)` resolver,
    /// so populate emits the orchestrator root even when `current_exe()`
    /// is far from the clone (binary in `~/bin/`, clone in `~/dev/`).
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
            shared_kg_collection: LAST_RESORT_SHARED_KG_COLLECTION.to_string(),
            shared_kg_write_disabled: false,
            // v0.2.46 Decision B — symmetric read gate. Default off
            // (reads allowed) on a fresh defaults-only struct.
            shared_kg_read_disabled: false,
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

    /// v0.2.46 Decision B — `"true"` / `"false"` string form for env
    /// writers. Mirrors `shared_kg_write_disabled_str` exactly so the
    /// pair-builder match arm has a symmetric helper to call.
    pub fn shared_kg_read_disabled_str(&self) -> &'static str {
        if self.shared_kg_read_disabled { "true" } else { "false" }
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
    let shared_kg_collection = db
        .app_state_get(APP_STATE_KEY_SHARED_KG_NAME)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .or_else(|| resolve_shared_kg_from_orchestrator_root(db))
        .unwrap_or_else(|| LAST_RESORT_SHARED_KG_COLLECTION.to_string());

    let shared_kg_write_disabled = match project_id {
        Some(pid) => get_shared_kg_write_disabled(db, pid).unwrap_or(false),
        None => false,
    };

    // v0.2.46 Decision B — symmetric READ gate. Same resolution shape
    // as the write gate above; default false (reads allowed) when the
    // row is absent OR no project_id was provided (test contexts).
    let shared_kg_read_disabled = match project_id {
        Some(pid) => get_shared_kg_read_disabled(db, pid).unwrap_or(false),
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
        shared_kg_read_disabled,
        cpu_only: !use_gpu,
        use_gpu,
        project_name: project_name.to_string(),
        // PR-2 portability: best-effort orchestrator clone root. Soft-fail
        // (None) so a launcher running outside a git checkout still
        // produces a usable `.claude/env` (the bundled hooks' in-tree
        // fallback path takes over).
        //
        // v0.2.37: was `find_local_repo_root().ok()` — the uncached
        // walk-up resolver. That bit user_project_x: when the launcher
        // binary lived at `~/bin/vct-launcher` and the clone at
        // `~/dev/vco/`, the walk-up returned None and
        // `VCT_ORCHESTRATOR_ROOT` was OMITTED from `.claude/env`
        // (per the omit-on-None semantic in
        // `write_project_env_files`). The canonical
        // `resolve_orchestrator_root(db)` checks the DB cache first
        // (`app_state['launcher.install_path']`, seeded at install
        // time by install.py + on the first launcher boot that hits
        // the walk-up), so populate succeeds even when
        // `current_exe()` is far from the clone.
        orchestrator_root: resolve_orchestrator_root(db),
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

/// PR-9 (v0.2.11): resolve the shared KG collection name from the
/// Orchestrator Project's primary `project_kg_bindings` entry.
///
/// Returns `Some(collection_name)` when:
///   - the orchestrator-root project row exists in `projects` (migration
///     013 has run AND `ensure_orchestrator_root` succeeded), AND
///   - that row has a `project_kg_bindings` entry with `role='primary'`
///     and a non-empty `collection_name`.
///
/// Returns `None` (so the caller falls through to
/// `LAST_RESORT_SHARED_KG_COLLECTION`) when:
///   - the row doesn't exist (standalone-binary install — no clone),
///   - the binding isn't seeded yet (rare — happens between
///     migration-013 run and the first `ensure_orchestrator_root` call),
///   - any DB error (we treat as "not derivable" and let the caller use
///     the safe fallback rather than crashing env resolution).
///
/// Soft-fail throughout. Never panics. The call site is on the hot path
/// of every project env render, so we use the cheapest possible
/// lookups (1 SELECT by slug + 1 SELECT bindings list).
fn resolve_shared_kg_from_orchestrator_root(db: &Db) -> Option<String> {
    use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;

    let root_row = db.get_project_by_slug(ORCHESTRATOR_ROOT_SLUG).ok().flatten()?;
    let bindings = db.list_project_kg_bindings(&root_row.id).ok()?;
    bindings
        .into_iter()
        .find(|b| b.role == "primary")
        .map(|b| b.collection_name)
        .filter(|s| !s.is_empty())
}

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
        assert_eq!(s.shared_kg_collection, "VibeCodedOrchestrator_KnowledgeGraph");
        assert_eq!(s.weaviate_url, "http://localhost:8081");
        assert_eq!(s.ollama_url, "http://localhost:11435");
        assert_eq!(s.code_embed_url, "http://localhost:11440");
        assert_eq!(s.active_embedding, "qwen3");
        assert_eq!(s.shared_kg_write_disabled_str(), "false");
        assert!(!s.shared_kg_write_disabled);
        // v0.2.46 Decision B — symmetric read gate defaults to off.
        assert_eq!(s.shared_kg_read_disabled_str(), "false");
        assert!(!s.shared_kg_read_disabled);
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
        assert_eq!(s.shared_kg_collection, "VibeCodedOrchestrator_KnowledgeGraph");
        assert!(!s.shared_kg_write_disabled);
        // v0.2.46 Decision B — symmetric read gate defaults off when
        // no project row exists (populate gets `None` for project_id).
        assert!(!s.shared_kg_read_disabled);
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
}
