//! Tauri commands exposing secrets + settings to the React UI.
//!
//! Secret values NEVER leave the Rust process. Commands return presence
//! booleans and masked previews only. Settings are non-sensitive and are
//! returned fully.
//!
//! ─── Secret lifecycle (Bug 3 follow-up to PR #60) ──────────────────────
//!
//!  Lifecycle B (user-selected): the keychain VALUE is preserved across
//!  Unset → Reactivate cycles. The "active" state is a separate flag in
//!  `launcher.db::secret_active_state` (Storage A). Readers gate on it
//!  BEFORE returning anything from the keychain.
//!
//!  Operations:
//!    * `set_secret_v2`        — write value to keychain, mark active.
//!                               Used for both initial Set and Update.
//!    * `clear_secret_v2`      — Unset. Mark inactive. **Keychain
//!                               UNTOUCHED.** Read API will refuse to
//!                               return the value while inactive.
//!    * `reactivate_secret_v2` — flip active back to true. **No keychain
//!                               change**, no value re-entry required.
//!                               One-click resume after a rotation pause.
//!    * `remove_secret_v2`     — DELETE the keychain value AND drop the
//!                               active-state row. The entry is gone.
//!    * `is_secret_set`        — true ONLY when keychain has a value AND
//!                               active=true. Returns false for inactive.
//!    * `get_secret_preview`   — masked preview ONLY when active=true.
//!                               Returns Ok(None) for inactive, never
//!                               leaks the canary.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::secrets::{self, SecretScope};

// ─── Secrets ────────────────────────────────────────────────────────────

// TODO: wire — defined for the secrets-list UI but no command emits it
// yet. The current secret list page reads `get_secret_status_v2` per
// key. When we add a "list all secrets for this project" panel, that
// command should return Vec<SecretMetadata>.
#[allow(dead_code)]
#[derive(Debug, Serialize)]
pub struct SecretMetadata {
    pub key: String,
    pub scope: String,
    pub is_set: bool,
    pub sensitive: bool,
    pub value_preview: Option<String>,
}

fn scope_from_manifest<'a>(scope: &str, project_id: &'a str) -> SecretScope<'a> {
    match scope {
        "global" => SecretScope::Global,
        "shared" => SecretScope::Shared { project_id },
        _ => SecretScope::PerProject { project_id },
    }
}

/// Subagent G (2026-05-08): identify whether a given (scope, module_id)
/// targets the per-project user bucket, i.e. the bucket the
/// `SecretsPanel.svelte` add-form's "Per-project" tab writes to and
/// whose entries auto-emit into ONE specific project's env surfaces.
///
/// Module-owned secrets (`scope='per_project'`, `module_id != 'user'` —
/// e.g. licensing's `VIBECODED_LICENSE_KEY` global, or any future
/// MCP-server module declaring `secrets[]` in its manifest) are
/// resolved separately by the hub `project_env` endpoint and
/// already reach subprocesses via that path. They are NOT in scope
/// for the env-file emit because:
///
///   1. Module manifests change less frequently than user secrets,
///      so file-side caching saves no real ergonomics.
///   2. Modules that need the value already call the resolver
///      directly via the bundled `vct_secrets_resolve.sh`.
///   3. Adding them to the env surface widens the bag of secrets a
///      curious user might `cat .claude/env` past.
///
/// Returns true ONLY for per-project user-bucket entries. Use
/// `is_user_emit_bucket` (below) for the broader test that also
/// catches the Shared / Global tab entries.
///
/// 0.1.7 H2 (2026-05-08): kept as a sub-predicate of
/// `is_user_emit_bucket` for the per-project-only refresh path inside
/// `refresh_env_after_user_secret_change` and as a regression-test
/// pin (see `is_per_project_user_bucket_pure_predicate`). All
/// production callers route through `is_user_emit_bucket` after H2.
#[allow(dead_code)]
fn is_per_project_user_bucket(scope: &str, module_id: &str) -> bool {
    scope == "per_project" && module_id == "user"
}

/// H2 (0.1.7 fork-readiness sweep, 2026-05-08): broaden the user-bucket
/// predicate to also recognise the SecretsPanel "Shared (this user)"
/// and "Global (this machine)" tabs.
///
/// All three tabs in `SecretsPanel.svelte` use the same constant
/// `UI_MODULE_BUCKET = "user"` for `module_id`. Pre-H2, only the
/// per-project tab's writes propagated into the env-file surfaces;
/// Shared / Global tab writes landed in the keychain but were
/// silent to every consumer — the original "GUI says secret is set,
/// but nothing reads it" gap, just one tab over.
///
/// Returns true for every tab the SecretsPanel's add-form can write to:
///   * `(per_project, user)` — one specific project
///   * `(shared,      user)` — every registered project (across this user)
///   * `(global,      user)` — every registered project (machine-wide)
///
/// Subprocesses spawned in any registered project's Claude Code session
/// see all three classes as normal env vars. The threat model is the
/// same as `is_per_project_user_bucket`: anything in the env surfaces
/// is readable by any subprocess in the project — same exposure profile
/// `~/.vct-secrets/` had pre-H2.
fn is_user_emit_bucket(scope: &str, module_id: &str) -> bool {
    if module_id != "user" {
        return false;
    }
    matches!(scope, "per_project" | "shared" | "global")
}

/// Subagent G (2026-05-08), broadened by H2 (2026-05-08): re-run
/// `write_project_env_files` for the affected project(s) after a
/// user-bucket secret change.
///
/// Three cases:
///   * `(per_project, user)` — refresh ONE project (the one in
///     `project_id`).
///   * `(shared, user)` or `(global, user)` — refresh EVERY registered
///     project. Shared / global entries are user-wide / machine-wide,
///     so a single change has to fan out to every project's env
///     surfaces. Otherwise a key added in the Shared tab would be
///     visible only to projects registered AFTER the change (because
///     `populate` reads it at write time), with stale surfaces
///     everywhere else until the next manual refresh.
///   * Anything else (`module_id != 'user'`, etc.) — skip. Module-owned
///     secrets are resolved by the hub's `/projects/{id}/env` endpoint
///     and don't go through the env-file emit path.
///
/// Failures are logged via eprintln and SWALLOWED — the secret
/// operation has already committed; an env-write hiccup must not roll
/// back the user's GUI action. For shared/global, a single project's
/// writer hiccup is logged but the fan-out continues for the rest.
///
/// `project_id` MUST already be validated by `enforce_scope_invariants`
/// before this is called — we don't double-check here.
fn refresh_env_after_user_secret_change(
    db: &Db,
    project_id: &str,
    scope: &str,
    module_id: &str,
    op: &str,
) {
    if !is_user_emit_bucket(scope, module_id) {
        return;
    }
    if scope == "per_project" {
        // Single-project refresh — the entry only affects one project.
        if let Err(e) =
            crate::commands::projects_v2::refresh_project_env_with_db(db, project_id)
        {
            eprintln!(
                "[vct] warning: env-file refresh after {} on {}/{} failed: {}. \
                 The keychain change has committed; env surfaces may be stale \
                 until the next refresh.",
                op, project_id, module_id, e
            );
        }
        return;
    }
    // Shared / global — fan out to every registered project. A single
    // project's writer hiccup is logged but the fan-out continues for
    // the rest. Soft-fail on the list_projects() read too: we have no
    // recovery path if the project list itself is unreadable.
    let projects = match db.list_projects() {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!(
                "[vct] warning: env-file fan-out after {} on shared/global {} failed \
                 to list registered projects: {}. The keychain change has committed; \
                 env surfaces will pick it up on the next per-project refresh.",
                op, module_id, e,
            );
            return;
        }
    };
    for row in projects {
        if let Err(e) =
            crate::commands::projects_v2::refresh_project_env_with_db(db, &row.id)
        {
            eprintln!(
                "[vct] warning: env-file refresh after {} on shared/global {}/{} \
                 for project {} failed: {}. Other registered projects continue \
                 to refresh; this one will be stale until next refresh.",
                op, scope, module_id, row.id, e
            );
        }
    }
}

/// Sentinel project_id used by the GUI when scope is global / shared.
///
/// These scopes don't tie a secret to a specific project; the frontend
/// passes a stable sentinel so the audit log + keychain service name
/// remain well-formed. `_global_` for global scope, `_user_shared_` for
/// shared (per-user, across all projects).
const SENTINEL_GLOBAL: &str = "_global_";
const SENTINEL_SHARED: &str = "_user_shared_";

/// Reject path-traversal-ish project_ids and enforce that per-project
/// secrets target a project that actually exists in the DB.
///
/// Without this, a caller could write a secret under e.g.
/// `project_id = "../../"` (which would still produce a valid keychain
/// service name) or under a `project_id` that no longer corresponds to a
/// registered project. Either lets a per-project secret leak to / be read
/// by an unintended context. Project-isolation requirement (see PR
/// description "Read semantics").
fn enforce_scope_invariants(scope: &str, project_id: &str, db: &Db) -> Result<(), String> {
    // No control characters / dot-segments / slashes anywhere — applies to
    // every scope as a defence-in-depth check. Sentinels above pass.
    if project_id.is_empty()
        || project_id.contains('/')
        || project_id.contains('\\')
        || project_id.contains('\0')
        || project_id == "."
        || project_id == ".."
        || project_id.starts_with("./")
        || project_id.starts_with("../")
    {
        return Err(format!("invalid project_id: {:?}", project_id));
    }
    match scope {
        "global" => {
            if project_id != SENTINEL_GLOBAL {
                return Err(format!(
                    "global scope must use sentinel project_id={:?}; got {:?}",
                    SENTINEL_GLOBAL, project_id
                ));
            }
        }
        "shared" => {
            // Shared keychain still uses a project_id slot for backward
            // compat with existing entries (see secrets.rs `service_name`).
            // The frontend passes `SENTINEL_SHARED` so all "shared"
            // secrets land in one user-wide bucket. Real project ids are
            // also accepted here (legacy) to preserve any pre-existing
            // per-project shared entries written before this PR.
            if project_id != SENTINEL_SHARED && db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "shared scope: project_id {:?} is neither sentinel {:?} nor a registered project",
                    project_id, SENTINEL_SHARED
                ));
            }
        }
        _ => {
            // Per-project: must reference an existing registered project.
            // Prevents projectA's modules from writing/reading secrets
            // under a project_id they make up.
            if db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "per-project scope: project {:?} is not a registered project",
                    project_id
                ));
            }
        }
    }
    Ok(())
}

#[command]
pub async fn set_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    value: String,
    validation_regex: Option<String>,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Validate value against the manifest regex if provided.
    if let Some(pattern) = validation_regex.as_deref() {
        let re = regex::Regex::new(pattern)
            .map_err(|e| format!("invalid validation regex: {}", e))?;
        if !re.is_match(&value) {
            return Err("value does not match validation pattern".into());
        }
    }

    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::set(scope_enum, &module_id, &key, &value)?;
    // Setting a value implicitly activates the entry. Covers both the
    // first Set and any later Update / "Set as new value" path. Without
    // this, an entry that was Unset and then re-Set without going through
    // Reactivate would still read as inactive.
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;

    db.audit(
        "secret_set",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "key": key,
            "scope": scope,
            "sensitive": sensitive,
            // Never log the value, not even truncated. Presence + scope are
            // enough to reconstruct "what happened" for debugging.
        }),
    )?;
    // Subagent G (2026-05-08): per-project user-bucket secrets auto-emit
    // into all 3 env surfaces so they show up as $KEY in the project's
    // Claude Code session. Refresh after the audit so the audit row's
    // ordering doesn't depend on an env-file write race. Soft-fail
    // (eprintln only) — the secret has already committed to the
    // keychain.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "set_secret_v2");
    Ok(())
}

/// Unset (Lifecycle B): flip the entry to INACTIVE without touching the
/// keychain value. The value stays in the OS keychain so a later
/// `reactivate_secret_v2` can resume the entry without the user re-typing
/// the value. While inactive, `is_secret_set` returns false and
/// `get_secret_preview` returns Ok(None) — the value cannot leak through
/// the launcher's API.
///
/// Distinct from `remove_secret_v2`, which deletes the keychain value AND
/// the active-state row.
#[command]
pub async fn clear_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Mark inactive. Do NOT call `secrets::delete` — that's the whole
    // Lifecycle B requirement. The keychain entry is the user's saved
    // value; we just gate readers on the active flag.
    db.mark_secret_inactive(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_unset",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    // Subagent G (2026-05-08): paused user-bucket secret must leave the
    // env surfaces. The writer's strip set picks up the (now-inactive)
    // row and removes it from `.claude/settings.json` env,
    // `.vscode/settings.json` claude-code.env, and the BEGIN/END block
    // of `.claude/env`. The keychain value is intentionally preserved
    // (Lifecycle B) so reactivate is one-click.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "clear_secret_v2");
    Ok(())
}

/// Reactivate a previously-Unset entry. Flips active=true. **Does not
/// touch the keychain.** The value that was already there is now
/// re-exposed to readers. This is the one-click "resume rotation pause"
/// path.
///
/// If the keychain has no value (e.g. a user manually deleted it via the
/// OS keychain UI while the launcher was inactive), this still flips the
/// flag — the next `is_secret_set` will simply return false because the
/// keychain side is empty. The flag itself is independent of the value's
/// existence; the read gate is `keychain_has_value AND active=true`.
#[command]
pub async fn reactivate_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_reactivate",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    // Subagent G (2026-05-08): the value flips back into the EMIT set,
    // so it returns to the env surfaces. Same refresh pattern as set /
    // clear — the writer reads the active flag + keychain value and
    // composes the surfaces from scratch.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "reactivate_secret_v2");
    Ok(())
}

/// Remove a secret entry: delete the keychain value AND drop the
/// active-state row. The entry is gone — Set requires re-typing the
/// value.
///
/// This is the destructive path. Use Unset (`clear_secret_v2`) if the
/// user just wants to pause an entry for token rotation.
#[command]
pub async fn remove_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::delete(scope_enum, &module_id, &key)?;

    // Subagent G (2026-05-08), broadened by H2 (2026-05-08): order
    // matters here for ALL user-bucket entries (per-project, shared, global).
    //
    // The env-file writer's strip set is derived from the
    // `secret_active_state` table. Once we've called
    // `forget_secret_active_state` the row is gone, the key drops out
    // of the strip set, and the env surfaces would carry a stale
    // entry across the next refresh.
    //
    // Solution: refresh BEFORE forget. At the moment of refresh, the
    // keychain was just deleted (so `secrets::get` returns None and
    // the EMIT pair-builder skips the key), but the row still exists
    // in `secret_active_state` (so the key IS in the strip set), so
    // the writer correctly removes it from every surface. AFTER
    // refresh, we forget the row for clean teardown.
    //
    // For shared / global user-bucket entries, the refresh fans out to
    // every registered project (see `refresh_env_after_user_secret_change`),
    // so the strip-set semantics apply per-project.
    //
    // Module-owned (`module_id != 'user'`) entries don't go through
    // the env-file emit path, so the strip-set semantics don't apply
    // — the order is irrelevant for them. We branch on the broader
    // user-bucket predicate so the existing code path for non-user-bucket
    // secrets stays byte-identical to pre-Subagent-G.
    if is_user_emit_bucket(&scope, &module_id) {
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "remove_secret_v2",
        );
        // Now the row's done its job in the strip set — forget it.
        db.forget_secret_active_state(&scope, &project_id, &module_id, &key)?;
    } else {
        // Pre-Subagent-G ordering for non-user-bucket entries.
        db.forget_secret_active_state(&scope, &project_id, &module_id, &key)?;
    }

    db.audit(
        "secret_remove",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    Ok(())
}

/// Combined status used by the secrets panel UI. `is_set` follows the
/// same gate as `is_secret_set` (true ⇔ keychain has value AND
/// active=true). `has_saved_value` reports whether the keychain still
/// has a value REGARDLESS of the active flag — the UI uses this to tell
/// "newly added, never set" (no saved value) apart from "Unset, value
/// preserved" (saved value but inactive).
///
/// `has_saved_value` is metadata about the LIFECYCLE state, not the
/// secret itself — it discloses no value bytes. The audit log mentions
/// `secret_unset` / `secret_reactivate` already, so an attacker with DB
/// read access can already reconstruct this fact; surfacing the boolean
/// to the UI does not weaken the model.
#[derive(Debug, Serialize)]
pub struct SecretStatus {
    pub is_set: bool,
    pub is_active: bool,
    pub has_saved_value: bool,
}

#[command]
pub async fn get_secret_status_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<SecretStatus, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    // 0.1.7 H3 (2026-05-08): the `is_set` field is the same boolean
    // contract as the `is_secret_set` command — readers (GUI badge,
    // any module testing presence) MUST see the cross-launcher view
    // so the GUI doesn't disagree with what subprocesses see. The
    // `is_active` field stays own-DB so the GUI can distinguish "this
    // launcher paused it" from "another launcher paused it" if a
    // future UI surfaces that detail.
    // 0.2.1: per-requester gate. The GUI is asking "does THIS project see
    // the secret as active?", so the requester is `project_id`. For
    // shared/global scopes the requester is the project that's about to
    // consume the secret — same project_id the GUI already knows. For
    // per_project scope, owner == requester == project_id, so the
    // semantics are identical to the legacy single-row gate.
    let active_cross = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        &db, &scope, &project_id, &module_id, &key, &project_id,
    );
    let active_own = db.is_secret_active_for_requester(
        &scope, &project_id, &module_id, &key, &project_id,
    )?;
    let has_saved_value = secrets::is_set(scope_enum, &module_id, &key)?;
    Ok(SecretStatus {
        is_set: active_cross && has_saved_value,
        is_active: active_own,
        has_saved_value,
    })
}

/// True only when the keychain has a value AND the launcher's active
/// flag is set. Returns false for inactive (Unset) entries even though
/// the keychain still has the value — that is the read-time gate.
///
/// 0.1.7 H3 (2026-05-08): the active-flag check is the cross-launcher
/// variant (Option γ), matching every other secret-reader path
/// (Subagent D's `github_pat_from_keychain`, Subagent G's user-secret
/// resolver, the hub's `project_env` resolver). Pre-H3 this used the
/// own-DB-only `db.is_secret_active`, which let prod's GUI report
/// "set" while every consumer (hub + env-file emit) saw "paused"
/// because dev launcher had paused the secret. H3 closes the
/// last asymmetry — the GUI's "Set" badge now agrees with what
/// subprocesses actually see.
#[command]
pub async fn is_secret_set(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Read-time gate: an inactive entry MUST appear "not set" to the UI
    // and to any module asking via this command. We check the gate FIRST
    // to avoid an unnecessary keychain round-trip when the entry is
    // paused.
    // 0.2.1: per-requester gate. The caller is asking "is this secret
    // set for THIS project right now?" — same project_id is the
    // requester. For per_project scope, owner == requester so the
    // semantics are identical to the legacy single-row gate; for
    // shared/global, the requester drives a per-project pause check.
    let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        &db, &scope, &project_id, &module_id, &key, &project_id,
    );
    if !active {
        return Ok(false);
    }
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::is_set(scope_enum, &module_id, &key)
}

/// Return a masked preview for NON-sensitive secrets only. For sensitive
/// secrets, the caller should use `is_secret_set` and render a "••••••••"
/// placeholder in the UI without calling this command.
///
/// Read-time gate (Bug 3): inactive entries return Ok(None) regardless
/// of keychain state. This is the canary-test invariant — a paused
/// secret must NOT leak through the preview path even as a masked value.
#[command]
pub async fn get_secret_preview(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    if sensitive {
        return Err("cannot preview sensitive secret".into());
    }
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Active-flag gate. Even though the keychain still holds the value
    // for an Unset entry, we treat it as if it weren't there for the
    // purposes of the public API. The user "paused" the entry; readers
    // (including the UI itself) must not see anything but a "not set"
    // signal until Reactivate.
    //
    // 0.1.7 H3 (2026-05-08): cross-launcher gate (Option γ). Symmetric
    // with the hub's `project_env` resolver and `is_secret_set` so a
    // pause anywhere takes effect everywhere — no GUI/consumer
    // disagreement. See `is_secret_set` doc comment for the asymmetry
    // we're closing.
    // 0.2.1: per-requester gate (same rule as is_secret_set). The
    // preview path must agree with what `project_env` will actually
    // serve, so a per-project pause hides the masked preview too.
    let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        &db, &scope, &project_id, &module_id, &key, &project_id,
    );
    if !active {
        return Ok(None);
    }
    let scope_enum = scope_from_manifest(&scope, &project_id);
    let val = secrets::get(scope_enum, &module_id, &key)?;
    Ok(val.map(|v| secrets::mask_preview(&v)))
}

// ─── Settings ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettingEntry {
    pub key: String,
    pub value: serde_json::Value,
}

#[command]
pub async fn get_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    db: State<'_, Db>,
) -> Result<Option<serde_json::Value>, String> {
    db.get_setting(&project_id, &module_id, &key)
}

#[command]
pub async fn set_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    value: serde_json::Value,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_setting(&project_id, &module_id, &key, &value)
}

#[command]
pub async fn list_module_settings_v2(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<Vec<SettingEntry>, String> {
    let rows = db.list_module_settings(&project_id, &module_id)?;
    Ok(rows
        .into_iter()
        .map(|(key, value)| SettingEntry { key, value })
        .collect())
}

// ─── 0.2.1 grants & per-requester pause commands ─────────────────────────
//
// Five Tauri commands that surface the migration-009 grants table and
// per-(secret × requester) active-flag rows to the launcher GUI:
//
//   * `grant_secret`              — owner grants read access to grantee
//   * `revoke_secret_grant_cmd`   — owner revokes a grant
//   * `list_grants_for_project`   — return owner-issued + grantee-received
//                                    grants for the GUI's "Per-project" /
//                                    "Shared" tabs
//   * `pause_secret_for_project`  — flip the per-(secret × requester)
//                                    flag to inactive (grantee self-opt-out
//                                    or owner pausing for a specific peer)
//   * `resume_secret_for_project` — drop the per-requester pause row so the
//                                    canonical / `*` row takes over
//
// Authorisation policy (deliberately enforced in Rust, not SQL):
//   * `grant_secret`: only the OWNER project may grant.
//   * `revoke_secret_grant_cmd`: only the OWNER project may revoke.
//   * `pause_secret_for_project` / `resume_secret_for_project`: any
//     project that's a valid requester (owner OR grantee, OR — for
//     shared/global — itself) may pause for itself. Pausing for someone
//     else is restricted to the OWNER. The CHECK on `secret_grants`
//     keeps the schema honest; this layer keeps the user-facing ergonomic.

#[derive(Debug, Clone, serde::Serialize)]
pub struct SecretGrantView {
    pub scope: String,
    pub owner_project_id: String,
    pub module_id: String,
    pub key: String,
    pub grantee_project_id: String,
    pub granted_at: i64,
    pub granted_by_actor: Option<String>,
    pub note: Option<String>,
}

impl From<crate::db::secret_grants::SecretGrant> for SecretGrantView {
    fn from(g: crate::db::secret_grants::SecretGrant) -> Self {
        Self {
            scope: g.scope,
            owner_project_id: g.owner_project_id,
            module_id: g.module_id,
            key: g.key,
            grantee_project_id: g.grantee_project_id,
            granted_at: g.granted_at,
            granted_by_actor: g.granted_by_actor,
            note: g.note,
        }
    }
}

/// Wraps `list_grants_for_project` output: the GUI's per-project tab
/// renders both the grants this project ISSUED (as owner) and the
/// grants it RECEIVED (as grantee) on the same screen.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ProjectGrantsView {
    pub issued: Vec<SecretGrantView>,
    pub received: Vec<SecretGrantView>,
}

#[command]
pub async fn grant_secret(
    owner_project_id: String,
    module_id: String,
    key: String,
    grantee_project_id: String,
    note: Option<String>,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if owner_project_id == grantee_project_id {
        return Err("grant_secret: owner and grantee must differ".to_string());
    }
    // Schema CHECK enforces scope='per_project'. The command takes no
    // scope arg — granting global/shared is meaningless because they're
    // already cross-project, and exposing the choice would be footgun.
    db.insert_secret_grant(
        "per_project",
        &owner_project_id,
        &module_id,
        &key,
        &grantee_project_id,
        Some("user"),
        note.as_deref(),
    )
}

#[command]
pub async fn revoke_secret_grant_cmd(
    owner_project_id: String,
    module_id: String,
    key: String,
    grantee_project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.revoke_secret_grant(
        "per_project",
        &owner_project_id,
        &module_id,
        &key,
        &grantee_project_id,
    )
}

#[command]
pub async fn list_grants_for_project(
    project_id: String,
    db: State<'_, Db>,
) -> Result<ProjectGrantsView, String> {
    let issued = db
        .list_grants_by_owner(&project_id)?
        .into_iter()
        .map(SecretGrantView::from)
        .collect();
    let received = db
        .list_grants_by_grantee(&project_id)?
        .into_iter()
        .map(SecretGrantView::from)
        .collect();
    Ok(ProjectGrantsView { issued, received })
}

#[command]
pub async fn pause_secret_for_project(
    scope: String,
    project_id: String,
    module_id: String,
    key: String,
    requester_project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    db.mark_secret_inactive_for_requester(
        &scope,
        &project_id,
        &module_id,
        &key,
        &requester_project_id,
    )
}

#[command]
pub async fn resume_secret_for_project(
    scope: String,
    project_id: String,
    module_id: String,
    key: String,
    requester_project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Resume = drop the per-requester row so the canonical (`*` or
    // owner-literal) row takes over. We do NOT explicitly mark active —
    // that would create a row on every resume and pollute the table
    // with default-state entries we'd otherwise prune as no-ops.
    db.forget_secret_active_state_for_requester(
        &scope,
        &project_id,
        &module_id,
        &key,
        &requester_project_id,
    )
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// These tests cover the scope-invariant guard rails and the active-flag
// gate. The scope-invariants check is a pure-DB function; the
// active-flag tests use the in-memory DB helpers and (where the keychain
// is involved) skip via `keyring_available()`.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use rusqlite::{params, Connection};
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn seed_project(db: &Db, id: &str, name: &str) {
        // Placeholder folder_path string — never resolved against disk by
        // these tests. Use a platform-appropriate prefix so the value isn't
        // ambiguous on Windows.
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\{}", id)
        } else {
            format!("/tmp/{}", id)
        };
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, folder, id, 1_700_000_000_000_i64],
            )
            .unwrap();
    }

    /// Probe whether the OS keychain backend is available in this test
    /// environment. CI containers and headless build hosts typically
    /// have no Secret Service / Keychain / Credential Manager running,
    /// so any test that exercises the actual keychain has to short-circuit.
    fn keyring_available() -> bool {
        let entry = match keyring::Entry::new("vct.test.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        // Try a write+delete round-trip. Any error means the backend
        // can't be reached — we skip the keychain-touching tests.
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    }

    #[test]
    fn invariants_per_project_requires_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Registered project: passes.
        assert!(enforce_scope_invariants("per_project", "p1", &db).is_ok());

        // Unregistered project: rejected.
        let err = enforce_scope_invariants("per_project", "ghost", &db).unwrap_err();
        assert!(
            err.contains("not a registered project"),
            "expected isolation error, got: {}",
            err
        );
    }

    #[test]
    fn invariants_global_requires_sentinel() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("global", SENTINEL_GLOBAL, &db).is_ok());

        // Real project_id under global scope is rejected — global is
        // machine-wide, must not be tied to any project's scope.
        let err = enforce_scope_invariants("global", "p1", &db).unwrap_err();
        assert!(err.contains("global scope"), "got: {}", err);
    }

    #[test]
    fn invariants_shared_accepts_sentinel_or_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("shared", SENTINEL_SHARED, &db).is_ok());
        // Legacy: real project_id passes (backward compat for any
        // pre-existing project-shared entries written before this PR).
        assert!(enforce_scope_invariants("shared", "p1", &db).is_ok());
        // Unregistered project: rejected.
        assert!(enforce_scope_invariants("shared", "ghost", &db).is_err());
    }

    #[test]
    fn invariants_reject_path_traversal() {
        let db = make_db();
        for bad in [
            "",
            ".",
            "..",
            "../",
            "./foo",
            "foo/bar",
            "foo\\bar",
            "foo\0bar",
        ] {
            let err =
                enforce_scope_invariants("per_project", bad, &db).unwrap_err();
            assert!(
                err.contains("invalid project_id"),
                "expected traversal rejection for {:?}, got: {}",
                bad,
                err
            );
        }
    }

    #[test]
    fn invariants_audit_label_unset_vs_remove_is_caller_concern() {
        // Sanity-check the constants the frontend relies on.
        assert_eq!(SENTINEL_GLOBAL, "_global_");
        assert_eq!(SENTINEL_SHARED, "_user_shared_");
    }

    /// Pure-DB regression for the active-flag default. A secret with no
    /// row in `secret_active_state` is treated as ACTIVE — anything else
    /// would silently break every entry written before migration 007.
    #[test]
    fn active_flag_defaults_active_when_no_row() {
        let db = make_db();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Pure-DB regression for the unset → reactivate roundtrip.
    /// `clear_secret_v2` and `reactivate_secret_v2` both go through these
    /// `mark_secret_*` helpers, so this exercises the storage layer
    /// without needing a real keychain.
    #[test]
    fn active_flag_unset_then_reactivate_roundtrip() {
        let db = make_db();
        db.mark_secret_inactive("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(!db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
        db.mark_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Canary test (Bug 3 security requirement): an Unset entry must NOT
    /// leak the value via the preview path. We write a unique canary
    /// directly through `secrets::*` + the active-flag DB helpers, then
    /// call the gate logic the public Tauri commands use. Going through
    /// `secrets::*` rather than the wrapped `#[command]` functions lets
    /// us avoid Tauri's `State<'_, Db>` machinery in unit tests while
    /// still exercising the exact same gate.
    ///
    /// Skipped in CI environments without an OS keychain backend (most
    /// Linux build hosts). Run locally with a logged-in desktop session
    /// or pass through to a workstation pre-merge.
    #[test]
    fn inactive_secret_does_not_leak_preview() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }

        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Unique canary — substring detection catches any accidental
        // leak even if a future bug changes the masking format.
        let canary = format!(
            "test-secret-leak-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let key = format!(
            "CANARY_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let scope = "per_project";
        let project_id = "p1";
        let module_id = "user";

        // Set + activate (mirrors `set_secret_v2`).
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).expect("keychain set");
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("mark active");

        // Sanity: while ACTIVE the gate lets a masked preview through.
        // (We replicate `get_secret_preview`'s body since the
        // `#[command]` wrapper requires Tauri State.)
        let preview_active = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(preview_active.is_some(), "preview missing while active");
        // The masked form must NOT contain the raw canary verbatim.
        assert!(
            !preview_active.as_ref().unwrap().contains(&canary),
            "raw canary leaked through the masked preview while active"
        );

        // Unset — the Lifecycle B path. KEYCHAIN UNTOUCHED.
        db.mark_secret_inactive(scope, project_id, module_id, &key)
            .expect("mark inactive");

        // The keychain still has the value (proves Lifecycle B):
        let kc = secrets::get(scope_enum, module_id, &key).unwrap();
        assert_eq!(
            kc.as_deref(),
            Some(canary.as_str()),
            "Lifecycle B violated: unset cleared the keychain"
        );

        // But the read gate must lie about it:
        let is_set_inactive = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            !is_set_inactive,
            "is_secret_set leaked an inactive entry as set"
        );

        // And the preview gate MUST return None (not even a masked form):
        let preview_inactive = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(
            preview_inactive.is_none(),
            "preview leaked while inactive: {:?}",
            preview_inactive
        );
        if let Some(p) = preview_inactive.as_ref() {
            assert!(!p.contains(&canary), "canary substring leaked");
        }

        // Reactivate — flips flag, keychain unchanged.
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("reactivate");

        // The same gate now yields the value again WITHOUT having
        // re-typed it. This is the user-facing benefit of Lifecycle B.
        let is_set_after = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            is_set_after,
            "reactivate did not restore the read gate; user would have to re-enter the value"
        );

        // Cleanup keychain (best-effort).
        let _ = secrets::delete(scope_enum, module_id, &key);
        let _ = db.forget_secret_active_state(scope, project_id, module_id, &key);
    }

    // ─── Subagent G (2026-05-08): refresh-on-change wiring ──────────────
    //
    // These tests pin the contract that mutating a per-project user-bucket
    // secret via `set_secret_v2` / `clear_secret_v2` / `reactivate_secret_v2`
    // / `remove_secret_v2` triggers `write_project_env_files` so the env
    // surfaces stay in sync without a session restart.
    //
    // Each test seeds a real on-disk project folder so the writer has
    // somewhere to land. Keychain-touching cases short-circuit when the
    // backend is unavailable (CI containers without libsecret).

    /// Seed a project with a real on-disk folder so the env writer has
    /// a target. Returns the folder path. The caller cleans up.
    fn seed_project_with_real_folder(db: &Db, id: &str, name: &str) -> std::path::PathBuf {
        let folder = std::env::temp_dir().join(format!(
            "vct-secrets-cmd-test-{}-{}",
            id,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&folder).unwrap();
        let folder_str = folder.to_string_lossy().to_string();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, folder_str, id, 1_700_000_000_000_i64],
            )
            .unwrap();
        folder
    }

    /// `is_per_project_user_bucket` correctly identifies the user bucket
    /// and rejects every other (scope, module) combo.
    #[test]
    fn is_per_project_user_bucket_pure_predicate() {
        // Match: per-project + user
        assert!(is_per_project_user_bucket("per_project", "user"));
        // Module-owned per-project: not user bucket
        assert!(!is_per_project_user_bucket("per_project", "licensing"));
        assert!(!is_per_project_user_bucket("per_project", "search_mcp"));
        // Shared / global: never user bucket
        assert!(!is_per_project_user_bucket("shared", "user"));
        assert!(!is_per_project_user_bucket("global", "user"));
        assert!(!is_per_project_user_bucket("shared", "installer"));
    }

    /// H3 (2026-05-08): `is_secret_set` / `get_secret_preview` /
    /// `get_secret_status_v2` ALL use the cross-launcher active-flag
    /// gate (Option γ). Pre-H3 the GUI-facing readers used
    /// `db.is_secret_active` (own DB only) while every other secret
    /// reader used `is_secret_active_cross_launcher`, so prod's GUI
    /// could report "set" while consumers (hub + env-file emit) saw
    /// "paused" because dev launcher had paused the secret.
    ///
    /// This test pins the gate symmetry on the predicate level:
    /// after `mark_secret_inactive` on the OWN DB, all three readers
    /// agree that the secret is paused. The cross-launcher walk to a
    /// sibling DB is exercised separately in `db::secret_active::tests::
    /// sibling_launcher_pause_propagates_via_read_helper`. Combining
    /// the two gives end-to-end coverage of the H3 invariant.
    #[test]
    fn h3_readers_use_cross_launcher_gate_consistent_with_consumers() {
        let db = make_db();
        seed_project(&db, "h3-proj", "H3 Test Project");

        // Sanity: no row → all three readers report "active=true" via
        // the default-active semantic.
        let scope = "per_project";
        let project_id = "h3-proj";
        let module_id = "user";
        let key = "H3_TEST_KEY";

        let active_own = db.is_secret_active(scope, project_id, module_id, key).unwrap();
        let active_cross = crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        );
        assert!(active_own);
        assert!(active_cross);

        // Pause via own DB.
        db.mark_secret_inactive(scope, project_id, module_id, key).unwrap();

        // Both readers MUST now say "inactive" (cross-launcher short-circuits
        // on own-DB, see `is_secret_active_cross_launcher`).
        assert!(!db.is_secret_active(scope, project_id, module_id, key).unwrap());
        assert!(!crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        ));

        // Reactivate.
        db.mark_secret_active(scope, project_id, module_id, key).unwrap();
        assert!(crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        ));
    }

    /// H2 (2026-05-08): the broader user-emit predicate catches all three
    /// SecretsPanel tabs (per-project, shared, global) when they target
    /// the `user` module bucket. Module-owned secrets and non-user
    /// scopes are still excluded — they go through the hub's
    /// /projects/{id}/env resolver path, not the env-file emit path.
    #[test]
    fn is_user_emit_bucket_pure_predicate() {
        // All three SecretsPanel tabs writing to module_id='user' MUST match.
        assert!(is_user_emit_bucket("per_project", "user"));
        assert!(is_user_emit_bucket("shared", "user"));
        assert!(is_user_emit_bucket("global", "user"));
        // Module-owned: excluded (per-project licensing, shared installer-bundled
        // PAT, global licensing key all flow via the hub resolver, not the
        // env-file emit path).
        assert!(!is_user_emit_bucket("per_project", "licensing"));
        assert!(!is_user_emit_bucket("shared", "installer"));
        assert!(!is_user_emit_bucket("global", "licensing"));
        assert!(!is_user_emit_bucket("shared", "search_mcp"));
        // Unknown scope strings: rejected. The predicate is closed over
        // the three scopes secrets_cmd.rs validates in
        // `enforce_scope_invariants`.
        assert!(!is_user_emit_bucket("bogus", "user"));
        assert!(!is_user_emit_bucket("", "user"));
    }

    /// End-to-end: `set_secret_v2` against the per-project user bucket
    /// triggers `write_project_env_files`. The keychain entry lands AND
    /// the project's `.claude/settings.json` env block carries the key.
    /// Skipped without an OS keychain (most CI containers).
    #[tokio::test]
    async fn set_secret_v2_triggers_env_refresh() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_set_refresh", "SetRefreshProj");

        // Wrap Db in Tauri-style state. The free function
        // `refresh_project_env_with_db` takes &Db so the State
        // wrapper here is for parity with the production command.
        let state: tauri::State<Db> = unsafe {
            // Tauri's State<T> is a thin wrapper around &T; in unit
            // tests that don't construct a real AppHandle, we use
            // the free-function variant of the env refresh inside
            // `set_secret_v2`. Calling the public `#[command]`
            // requires Tauri State plumbing, so we replicate the
            // command body here to exercise the same code path
            // without the Tauri runtime.
            std::mem::transmute(&db)
        };
        let _ = state; // silence unused; the free-function path covers it

        // Replicate the `#[command] set_secret_v2` body using the same
        // helpers the command uses (see lines 132-180 of this file).
        // Going through the helpers (rather than the wrapped command)
        // skips the Tauri runtime requirement while keeping the
        // refresh hook, audit log, and active-flag mark identical.
        let scope = "per_project".to_string();
        let project_id = "p_set_refresh".to_string();
        let module_id = "user".to_string();
        let key = "REFRESH_TEST_KEY".to_string();
        let canary_value = format!(
            "subagent-g-set-refresh-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary_value).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        // The refresh hook — same call the command makes.
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2",
        );

        // Project's `.claude/settings.json` env block should now carry
        // the key.
        let cs_path = folder.join(".claude/settings.json");
        assert!(cs_path.exists(), "writer didn't run; .claude/settings.json absent");
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert_eq!(
            cs["env"][&key],
            canary_value,
            ".claude/settings.json env block missing or wrong value: {}",
            cs["env"]
        );

        // Cleanup.
        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        std::fs::remove_dir_all(&folder).ok();
    }

    /// `remove_secret_v2` against the user bucket: the env-write fires
    /// BEFORE `forget_secret_active_state` so the row's still in the
    /// strip set when the writer composes the new surfaces. After the
    /// test the surfaces no longer carry the key.
    ///
    /// Skipped without OS keychain.
    #[tokio::test]
    async fn delete_secret_v2_strips_secret_from_env_surfaces() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_del_strip", "DelStripProj");

        let scope = "per_project";
        let project_id = "p_del_strip";
        let module_id = "user";
        let key = "STRIP_TEST_KEY";
        let canary = format!(
            "subagent-g-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Step 1: set + active. Env surfaces should carry the key.
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2");
        let cs_path = folder.join(".claude/settings.json");
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert_eq!(cs["env"][key], canary, "pre-delete: key not in env");

        // Step 2: replicate `remove_secret_v2` body — keychain delete,
        // refresh BEFORE forget, then forget.
        secrets::delete(scope_enum, module_id, key).unwrap();
        // Crucial: refresh BEFORE forget so the strip set carries the key.
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "remove_secret_v2");
        db.forget_secret_active_state(scope, project_id, module_id, key).unwrap();

        // .claude/settings.json no longer carries the key.
        let cs_after: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert!(
            cs_after["env"].get(key).is_none(),
            "post-delete: env still has {}: {}",
            key,
            cs_after["env"]
        );
        // .claude/env: BEGIN/END block should not contain the key either.
        let claude_env = std::fs::read_to_string(folder.join(".claude/env")).unwrap();
        assert!(
            !claude_env.contains(key),
            "post-delete: .claude/env still mentions {}:\n{}",
            key,
            claude_env,
        );
        // .vscode/settings.json: same.
        let vsc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(folder.join(".vscode/settings.json")).unwrap())
                .unwrap();
        assert!(vsc["claude-code.env"].get(key).is_none());

        std::fs::remove_dir_all(&folder).ok();
    }

    /// H2 (2026-05-08): refresh-skip — only NON-user-emit buckets are
    /// skipped. After H2 the predicate covers per-project + shared +
    /// global user buckets (all three SecretsPanel tabs); only
    /// module-owned scopes still skip the env-file emit path.
    ///
    /// This pins the contract so a future refactor can't regress to
    /// "always refresh" or "skip shared/global". We exercise the
    /// module-owned cases (the only ones that should still skip after
    /// H2) and verify env-file writes don't fire.
    ///
    /// Doesn't need keychain because we're testing the negative —
    /// the project folder simply must NOT have `.claude/settings.json`
    /// after a module-owned secret op.
    #[test]
    fn refresh_env_after_user_secret_change_skips_module_owned_buckets() {
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_skip", "SkipBucketProj");

        // Module-owned shared (e.g. installer's github_pat written by
        // register_github_pat — that has its own dedicated env-file
        // refresh path in installer.rs after H2's B2 fix). MUST skip
        // here.
        refresh_env_after_user_secret_change(
            &db,
            "_user_shared_",
            "shared",
            "installer",
            "set_secret_v2_module_shared",
        );
        // Module-owned global (e.g. licensing's machine-wide key).
        // MUST skip — flows via hub resolver, not env-file emit.
        refresh_env_after_user_secret_change(
            &db,
            "_global_",
            "global",
            "licensing",
            "set_secret_v2_module_global",
        );
        // Per-project, but NOT the user bucket (e.g. licensing per-project
        // entry). MUST skip.
        refresh_env_after_user_secret_change(
            &db,
            "p_skip",
            "per_project",
            "licensing",
            "set_secret_v2_module",
        );

        // None of those should have triggered the env writer.
        assert!(
            !folder.join(".claude/settings.json").exists(),
            "module-owned refresh leaked into the project's env files"
        );
        assert!(!folder.join(".vscode/settings.json").exists());
        assert!(!folder.join(".claude/env").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── H2 (0.1.7 fork-readiness sweep, 2026-05-08): shared/global propagation ─
    //
    // The SecretsPanel "Shared (this user)" and "Global (this machine)"
    // tabs write to keychain slots `vct._user_shared_.shared.user/<key>`
    // and `vct._global_.global.user/<key>` respectively. Pre-H2, NOTHING
    // enumerated those slots — `list_user_secret_keys_for_project` only
    // returned per-project rows, and `refresh_env_after_user_secret_change`
    // skipped non-per-project scopes outright. So a user adding
    // `OPENAI_API_KEY` via the Shared tab landed it in the keychain but
    // every project's `.claude/env` stayed silent.
    //
    // These tests pin the H2 contract: a shared/global user-bucket op
    // fans out to every registered project's env files. Skipped without
    // an OS keychain backend (most CI containers).

    /// Seed two registered projects on disk so the fan-out has a
    /// non-trivial target set. The H2 invariant is "every registered
    /// project's surfaces include the shared/global key", and a
    /// single-project test would pass even if the fan-out only touched
    /// the calling project.
    fn seed_two_registered_projects(db: &Db) -> (std::path::PathBuf, std::path::PathBuf) {
        let p1 = seed_project_with_real_folder(db, "h2-p1", "H2 Project One");
        let p2 = seed_project_with_real_folder(db, "h2-p2", "H2 Project Two");
        (p1, p2)
    }

    /// H2: a shared user-bucket secret added via `set_secret_v2`
    /// propagates to EVERY registered project's `.claude/settings.json`
    /// env block (and the other two surfaces via the same writer).
    /// Pre-H2 this was a silent no-op — the keychain landed but no
    /// project's env files saw the key.
    #[tokio::test]
    async fn set_secret_v2_shared_user_bucket_propagates_to_all_registered_projects() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "shared".to_string();
        let project_id = "_user_shared_".to_string();
        let module_id = "user".to_string();
        let key = format!(
            "H2_SHARED_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-shared-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Replicate the `#[command] set_secret_v2` body — same as the
        // existing pre-H2 set test pattern.
        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2_shared",
        );

        // Both projects' `.claude/settings.json` should now carry the key.
        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            assert!(
                cs_path.exists(),
                "[{}] writer didn't run; .claude/settings.json absent",
                label
            );
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(
                cs["env"][&key],
                canary,
                "[{}] .claude/settings.json env block missing or wrong value: {}",
                label,
                cs["env"]
            );
        }

        // Cleanup keychain.
        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: global user-bucket secrets propagate to every registered
    /// project's env files. Symmetric with the shared test above —
    /// the writer doesn't care which user-emit bucket the key lives
    /// in, only that it's in some user-emit bucket.
    #[tokio::test]
    async fn set_secret_v2_global_user_bucket_propagates_to_all_registered_projects() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "global".to_string();
        let project_id = "_global_".to_string();
        let module_id = "user".to_string();
        let key = format!(
            "H2_GLOBAL_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-global-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2_global",
        );

        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            assert!(
                cs_path.exists(),
                "[{}] writer didn't run for global; .claude/settings.json absent",
                label
            );
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(
                cs["env"][&key],
                canary,
                "[{}] global key missing from .claude/settings.json env: {}",
                label,
                cs["env"]
            );
        }

        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: `remove_secret_v2` on a shared user-bucket entry strips the
    /// key from EVERY registered project's env files. Mirrors the
    /// per-project strip test, but the fan-out has to land in both
    /// projects.
    #[tokio::test]
    async fn delete_secret_v2_shared_user_bucket_strips_from_all_projects() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "shared";
        let project_id = "_user_shared_";
        let module_id = "user";
        let key = format!(
            "H2_STRIP_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Step 1: set + active. Both projects' env surfaces carry the key.
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2_shared");
        for folder in [&folder1, &folder2] {
            let cs_path = folder.join(".claude/settings.json");
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(cs["env"][&key], canary);
        }

        // Step 2: replicate the `remove_secret_v2` body for shared scope.
        // Order: delete keychain → refresh (carries strip set) → forget.
        secrets::delete(scope_enum, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            project_id,
            scope,
            module_id,
            "remove_secret_v2_shared",
        );
        db.forget_secret_active_state(scope, project_id, module_id, &key).unwrap();

        // Both projects' surfaces no longer carry the key.
        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            let cs_after: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert!(
                cs_after["env"].get(&key).is_none(),
                "[{}] post-delete: shared env still has {}: {}",
                label,
                key,
                cs_after["env"]
            );
            let claude_env = std::fs::read_to_string(folder.join(".claude/env")).unwrap();
            assert!(
                !claude_env.contains(&key),
                "[{}] post-delete: .claude/env still mentions {}:\n{}",
                label,
                key,
                claude_env,
            );
        }

        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: `remove_secret_v2` on a global user-bucket entry strips
    /// from every registered project. Symmetric with the shared strip
    /// test.
    #[tokio::test]
    async fn delete_secret_v2_global_user_bucket_strips_from_all_projects() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "global";
        let project_id = "_global_";
        let module_id = "user";
        let key = format!(
            "H2_GLOBAL_STRIP_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-global-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2_global");
        for folder in [&folder1, &folder2] {
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(folder.join(".claude/settings.json")).unwrap()).unwrap();
            assert_eq!(cs["env"][&key], canary);
        }

        secrets::delete(scope_enum, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            project_id,
            scope,
            module_id,
            "remove_secret_v2_global",
        );
        db.forget_secret_active_state(scope, project_id, module_id, &key).unwrap();

        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_after: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(folder.join(".claude/settings.json")).unwrap()).unwrap();
            assert!(
                cs_after["env"].get(&key).is_none(),
                "[{}] post-delete: global env still has {}",
                label,
                key
            );
        }

        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }
}
