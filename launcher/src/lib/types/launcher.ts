// Type mirrors of Rust command return values. Mirrors live in:
//   launcher/src-tauri/src/commands/projects_v2.rs   ProjectView, SwitchHostResult
//   launcher/src-tauri/src/commands/secrets_cmd.rs   SecretMetadata, SettingEntry
//   launcher/src-tauri/src/commands/licensing.rs     TierCacheView
//   launcher/src-tauri/src/commands/modules.rs       ModuleCatalogEntry, ModuleStatusView
//   launcher/src-tauri/src/db/models.rs              ModuleInstallRow, ProjectHost, ModuleStatus
//
// Keep these in sync. Field naming follows serde defaults (snake_case).

export type ProjectHost = 'base' | 'mao' | 'orchestrator_root';

// v0.2.35 (Agent J): added 'broken' to mirror the Rust enum
// (vct-launcher-core/src/db/models.rs::ModuleStatus::Broken — surfaced by
// the startup reconciler when ~/.vct/modules/<id>/ has gone missing).
// Previously this TS type drifted from the Rust source: the launcher would
// receive `"broken"` strings from `list_installed_modules` and silently
// type-cast them to nothing actionable. Now the catalog tile renders a
// distinct Retry-install + Uninstall pair for both error and broken rows.
export type ModuleStatus = 'installing' | 'installed' | 'running' | 'stopped' | 'error' | 'broken';

export type SecretScope = 'per_project' | 'shared' | 'global';

export type LicenseTier = 'free' | 'pro' | 'mao' | 'enterprise' | 'admin';

export interface ProjectView {
  id: string;
  name: string;
  folder_path: string;
  host: ProjectHost;
  /** URL-friendly slug (lowercase, dashes). Stable across renames only as
   *  long as the name does not change — renaming regenerates the slug.
   *  Use for /p/<slug>/... routes. */
  slug: string;
  created_at: number;
  updated_at: number;
  module_count: number;
}

export interface ModuleInstallRow {
  id: string;
  project_id: string;
  module_id: string;
  module_version: string;
  install_path: string;
  status: ModuleStatus;
  enabled: boolean;
  installed_at: number;
  last_started_at: number | null;
  last_error: string | null;
}

export interface SwitchHostResult {
  project: ProjectView;
  modules_removed: ModuleInstallRow[];
  modules_preserved: ModuleInstallRow[];
}

/**
 * Mirror of Rust `CreateProjectResult` (commands/projects_v2.rs).
 *
 * BLOCKER-2 (2026-05-01): PR 7's signature change wrapped ProjectView in
 * this {project, warnings} envelope, but the TS caller was still using
 * `<ProjectView>` as the invoke generic. Result: every newly-created
 * project landed in the store as the wrapper object, project.id was
 * undefined, and every downstream UI surface that keys off id broke.
 */
export interface CreateProjectResult {
  project: ProjectView;
  /** Non-fatal warnings (env-write failures, stale .env, etc.). */
  warnings: string[];
}

/**
 * Mirror of Rust `RenameProjectResult` (commands/projects_v2.rs).
 *
 * HIGH-7 (2026-05-01): rename mirrors create's warning surface so env
 * refresh failures during rename can be toasted instead of eprintln'd.
 * Also reused as the return type of `set_shared_kg_write_disabled`
 * (MEDIUM-1, refactored 2026-05-01) and its deprecated alias
 * `set_shared_kg_opt_out`.
 */
export interface RenameProjectResult {
  project: ProjectView;
  warnings: string[];
}

/**
 * Mirror of Rust `UpdateSummary` (commands/projects_v2.rs).
 *
 * PR 5 (2026-05-01): per-action counts produced by the bundle install
 * during an `update_project_v2` run. Drives the toast summary line
 * ("5 files updated, 2 user-modifications preserved") plus optional
 * detail breakdowns. Field naming mirrors the Rust struct (snake_case).
 */
export interface UpdateSummary {
  /** Newly-shipped orchestrator files that didn't exist before. */
  created: number;
  /** Files whose installed content matched the prior-shipped manifest hash;
   *  now overwritten with the new shipped version. */
  overwritten: number;
  /** Files where installed content diverged from the prior-shipped hash
   *  (= user-modified). Preserved on disk; surfaced via the
   *  `bundle_user_modified_preserved` deferral entry. */
  preserved: number;
  /** Files whose installed content already matches what we'd write. */
  noop: number;
  /** Files unconditionally overwritten (not user-customisable, e.g.
   *  `.claude/hooks/_lib/*`). */
  always_overwritten: number;
  /** First-install only — always 0 in update mode (kept for symmetry). */
  skipped_existing: number;
  /** Number of `errors[]` entries in the JSON envelope (per-file write
   *  failures). Each is also surfaced as a string in `warnings`. */
  errors_count: number;
}

/**
 * Mirror of Rust `UpdateProjectResult` (commands/projects_v2.rs).
 *
 * PR 5 (2026-05-01): structured envelope for the launcher's "Update bundle"
 * action. `warnings` flow as toasts; `summary` drives the one-line summary
 * toast.
 */
export interface UpdateProjectResult {
  project: ProjectView;
  warnings: string[];
  summary: UpdateSummary;
}

/**
 * Mirror of Rust `UpdateAllOptions` (commands/projects_v2.rs).
 *
 * 0.2.x backlog #4 (2026-05-10): drives the "Update all projects" power-
 * user button. `stop_on_error: true` (default) makes the launcher halt at
 * the first project that hard-fails (folder missing, project unregistered)
 * so the user sees the broken project promptly instead of chewing through
 * the remaining N-1 first. `false` continues past failures.
 */
export interface UpdateAllOptions {
  stop_on_error?: boolean;
}

/**
 * Mirror of Rust `UpdateAllProjectEntry` (commands/projects_v2.rs).
 *
 * Per-project outcome of an `update_all_projects` run. `status` is one of:
 *   - "succeeded": `update_project_v2` returned Ok (warnings may still
 *     populate `warnings[]` for soft-fail conditions).
 *   - "failed":    hard failure (project missing on disk / folder gone).
 *     `error` carries the explanatory message.
 *   - "skipped":   `stop_on_error=true` halted iteration before reaching
 *     this project. `error` is null, `summary` is null.
 */
export interface UpdateAllProjectEntry {
  project_id: string;
  project_name: string;
  status: 'succeeded' | 'failed' | 'skipped';
  error: string | null;
  warnings: string[];
  summary: UpdateSummary | null;
}

/**
 * Mirror of Rust `UpdateAllReport` (commands/projects_v2.rs).
 *
 * Aggregate counts after an `update_all_projects` run. The launcher
 * renders these in the progress modal's footer summary
 * ("3 updated, 1 failed, 0 skipped").
 */
export interface UpdateAllReport {
  updated: UpdateAllProjectEntry[];
  total_succeeded: number;
  total_failed: number;
  total_skipped: number;
}

/**
 * Mirror of Rust `UnregisterOptions` (commands/projects_v2.rs).
 *
 * 2026-05-06: drives the per-project "Unregister project" action. Both
 * fields are optional on the Tauri side (`#[serde(default)]`). Sending
 * `null` from the UI maps to backend defaults via `Option<UnregisterOptions>`.
 *
 *   - `purgeLauncherFiles` (default true): surgically remove launcher-
 *     managed files (.claude/hooks, .claude/scripts, .claude/env, infra
 *     compose YAMLs) AND strip canonical env keys from .env / .claude/env
 *     / .claude/settings.json env / .vscode/settings.json claude-code.env.
 *     User content (agents/skills/CONTEXT_STATE/CLAUDE.md/source code/
 *     user-added .env keys) is preserved.
 *   - `purgeCollections` (default false): drop the project's OWN Weaviate
 *     collections (`<Project>_KnowledgeGraph`, `<Project>_Development`).
 *     Shared collections never touched. OFF by default — collections can
 *     always be rebuilt from /knowledge + source code via
 *     install-bundle --update.
 */
export interface UnregisterOptions {
  purgeLauncherFiles?: boolean;
  purgeCollections?: boolean;
}

/**
 * Mirror of Rust `UnregisterReport` (commands/projects_v2.rs).
 *
 * Returned by `delete_project_v2`. The launcher's settings-tab toast uses
 * `filesPurged.length`, `keysPurgedFromEnv.length`, and
 * `collectionsDropped.length` for a one-line summary; `warnings[]` is
 * surfaced as additional error toasts when non-empty.
 */
export interface UnregisterReport {
  projectId: string;
  projectName: string;
  filesPurged: string[];
  keysPurgedFromEnv: string[];
  collectionsDropped: string[];
  warnings: string[];
}

export interface TierCacheView {
  orchestrator_tier: LicenseTier | string;
  module_licenses: Record<string, unknown>;
  last_validated: number;
  last_error: string | null;
  grace_period_remaining_ms: number | null;
}

/**
 * v0.2.32 §D1: row surface for the per-module license section in the
 * orchestrator-license dialog (`ActivationModal.svelte`).
 *
 * Backed by the `get_module_licenses` Tauri command, which flattens
 * `tier_cache.module_licenses` into rows. Mirrors
 * `launcher/src-tauri/src/commands/licensing.rs::ModuleLicenseRow`.
 *
 * Field semantics:
 *   - `module_id`: stable wire id (e.g. `"vct-rl-reranker"`).
 *   - `display_name`: human-readable name from `vct-module.json`; falls
 *     back to `module_id` when no catalog manifest is available.
 *   - `tier`: per-module tier the server granted (`"pro"` / `"mao"` /
 *     etc.). `"unknown"` when the server response was missing the field.
 *   - `activated_at`: optional activation timestamp. Server may send
 *     either ISO-8601 or numeric epoch — backend pre-stringifies both
 *     so the UI renders verbatim.
 */
export interface ModuleLicenseRow {
  module_id: string;
  display_name: string;
  tier: string;
  activated_at: string | null;
}

/**
 * v0.2.36: result shape for the admin-token machine-rebind command.
 *
 * Mirrors `launcher/src-tauri/src/commands/licensing.rs::AdminRebindResult`.
 * The Rust side orchestrates the full rebind (read license key from
 * keychain, compute machine_id_hash, POST to the edge function) so the
 * frontend never touches the secret directly — the value never crosses
 * the IPC boundary.
 *
 * On success: `success=true`, `user` and `rebound_at` populated.
 * On failure: `success=false`, `error` and `detail` describe the cause
 * (network failure, license_invalid, not_an_admin_token, no_license_key,
 * rebind_failed, service_misconfigured, license_key_invalid_format,
 * machine_id_hash_invalid_format).
 *
 * `machine_id_hash` is ALWAYS populated — useful for displaying the
 * "current machine" label in the dialog regardless of outcome.
 */
export interface AdminRebindResult {
  success: boolean;
  user: string | null;
  rebound_at: string | null;
  error: string | null;
  detail: string | null;
  machine_id_hash: string;
}

export interface ModuleCatalogEntry {
  id: string;
  name: string;
  version: string;
  description: string;
  category: string;
  tags: string[];
  license_required: boolean;
  license_variant_ids: string[];
  min_orchestrator_tier: string;
  compatibility_hosts: string[];
  is_licensed: boolean;
  manifest_source: string;
  /**
   * Bug 33: optional visibility hint. Public modules are visible to
   * everyone; `private-test` modules are visible only to users with
   * the server-classified `admin` tier. Missing field is treated as
   * `public` for backward compatibility with manifests written before
   * Bug 33.
   */
  visibility?: 'public' | 'private-test';
  /**
   * Bug 16 + Fix 8: render hint.
   *   - 'bundled'      → always-installed, no Install button (e.g. the launcher itself)
   *   - 'available'    → catalog-listed, has Install action
   *   - 'installed'    → installed, can be configured / uninstalled
   *   - 'subcomponent' → ships with parent module, navigate to dashboard CTA
   *   - 'coming_soon'  → announced, not yet shipped. Renders with a Coming Soon
   *                      badge + Learn-more CTA, no Install. Reserved for items
   *                      with a public roadmap commitment; do NOT use for vapor.
   */
  kind:
    | 'bundled'
    | 'available'
    | 'installed'
    | 'update_available'
    | 'broken'
    | 'subcomponent'
    | 'coming_soon';
  parent_id: string;
  cta_route: string;
  /** For `kind === 'coming_soon'`: which tier this will ship under (e.g. 'pro'). */
  coming_soon_tier?: string;
  /** For `kind === 'coming_soon'`: optional public target window (e.g. 'Q3 2026'). */
  coming_soon_target?: string;
  /**
   * v0.2.31 module-deprecation surface (Layer 1, GUI). When `true`, the
   * module card renders an amber `DEPRECATED` badge near the tier chip;
   * a `<DeprecationBanner>` may render at the top of the module's
   * dashboard (when one exists via `cta_route`). The module continues to
   * work normally — this is a "plan ahead for migration" signal, not a
   * hard block. Populated at catalog-build time once the v0.2.32 poller
   * lands; v0.2.31 defaults to `false` so the surface is forward-compatible.
   */
  deprecated?: boolean;
  /** Optional human-readable message rendered in badge tooltip + banner. */
  deprecation_message?: string;
  /** Optional ISO date (YYYY-MM-DD) for the module's end-of-life date. */
  deprecation_eol_date?: string;
  /** Optional URL pointing at the publisher's migration guide. */
  deprecation_migration_url?: string;
  /**
   * v0.2.33 (Agent B, L0a): set on installed module entries whose
   * `module_id` is NOT advertised by the L0 catalog. The renderer
   * shows a "No longer available in catalog" warning badge.
   */
  catalog_warning?: string;
}

/**
 * v0.2.33 (Agent B, L0a): L0 fetch status. Maps to the Rust `L0Status`
 * enum. Drives the catalog header banner (Agent E's scope).
 */
export type L0Status =
  | { kind: 'ok'; fetched_at: string; modules_count: number }
  | { kind: 'stale'; cached_fetched_at: string; last_error: string }
  | { kind: 'unavailable'; error: string };

/**
 * v0.2.33 (Agent B, L0a): one parse failure surfaced to the renderer
 * for the "1 module manifest couldn't be parsed" banner (Agent E).
 * `source` is either a file path (on-disk manifest) or `L0:<endpoint>`
 * (L0 envelope parse failure).
 */
export interface ManifestParseError {
  module_id: string;
  source: string;
  error: string;
}

/**
 * v0.2.33 (Agent B, L0a, review §10.c): emitted exactly when
 * `<install_root>/paid-modules/` exists AND
 * `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` is unset AND the user hasn't
 * dismissed the toast. The renderer surfaces this as a one-shot
 * "I see your dev paid-modules — opt in to render them" toast.
 */
export interface DevAffordanceHint {
  paid_modules_path: string;
  env_var_name: string;
}

/**
 * v0.2.33 (Agent B, L0a): the new `list_module_catalog` Tauri command
 * response shape. Replaces the v0.2.32-era bare `ModuleCatalogEntry[]`.
 *
 * The store unwraps `.modules` into the existing `catalog: ModuleCatalogEntry[]`
 * slot. `l0_status` + `parse_errors` + `dev_affordance_hint` flow into
 * Agent E's banner/toast surfaces.
 */
export interface CatalogResponse {
  modules: ModuleCatalogEntry[];
  l0_status: L0Status;
  parse_errors: ManifestParseError[];
  dev_affordance_hint: DevAffordanceHint | null;
}

export interface ModuleStatusView {
  status: string;
  enabled: boolean;
  installed_at: number;
  last_started_at: number | null;
  last_error: string | null;
}

export interface ModuleInstallCompleteEvent {
  project_id: string;
  module_id: string;
  success: boolean;
  error?: string;
}

/**
 * Gap 2: per-project initial code-graph build status.
 *
 * Mirrors `CodeGraphBuildView` in commands::codegraph (Rust). Fired on
 * the `code-graph-build-progress` Tauri event during a build, and
 * returned by `get_code_graph_build_status`.
 */
export type CodeGraphBuildStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped';

export interface CodeGraphBuildView {
  project_id: string;
  status: CodeGraphBuildStatus;
  /** ISO 8601 (RFC 3339); null until the build starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the build reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  files_analyzed: number;
  /** File-extension tags, e.g. `["py","ts"]`. */
  languages: string[];
  joern_used: boolean;
  error_message: string | null;
  /** Last ~4 KiB of analyzer stdout/stderr — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events (e.g. "scan", "analyze"). */
  current_phase: string | null;
}

/**
 * KG auto-sync (2026-05-12): per-project initial `kg-sync --all` status.
 *
 * Mirrors `KgSyncView` in commands::kg_sync (Rust). Fired on the
 * `kg-sync-progress` Tauri event during a sync, and returned by
 * `get_kg_sync_status`. Shape parallels `CodeGraphBuildView` — same
 * lifecycle states, same optional timestamps, same `current_phase`
 * field for live events.
 */
export type KgSyncStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped';

export interface KgSyncView {
  project_id: string;
  status: KgSyncStatus;
  /** ISO 8601 (RFC 3339); null until the sync starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the sync reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  /** Total `.md` files in knowledge/ (per the script's "📚 Found N" header). */
  kg_total: number;
  kg_succeeded: number;
  kg_failed: number;
  /** Total `.md` files in docs/ (per the script's "📚 Found N" header). */
  docs_total: number;
  docs_succeeded: number;
  docs_failed: number;
  error_message: string | null;
  /** Last ~4 KiB of subprocess stdout/stderr — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events ("scan" | "knowledge" | "docs" | "embed"). */
  current_phase: string | null;
}

/**
 * KG summary auto-backfill (v0.2.3 / 2026-05-12): per-project initial
 * `generate-kg-summary.py` pass status.
 *
 * Mirrors `KgSummaryView` in commands::kg_summary (Rust). Fired on the
 * `kg-summary-progress` Tauri event during a backfill, and returned by
 * `get_kg_summary_status`. Shape parallels `KgSyncView` — same lifecycle
 * states, same optional timestamps, same `current_phase` field for live
 * events. Adds `backend` (which fallback chain the summariser picked)
 * and per-node counters (succeeded / unchanged / failed / skipped)
 * specific to the per-file invocation pattern.
 */
export type KgSummaryStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped';

export interface KgSummaryView {
  project_id: string;
  status: KgSummaryStatus;
  /** ISO 8601 (RFC 3339); null until the backfill starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the backfill reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  /** Total `.md` files discovered under knowledge/. */
  nodes_total: number;
  /** Files where the summariser wrote a new entry. */
  nodes_succeeded: number;
  /** Files where the summariser detected an existing hash-match (no-op). */
  nodes_unchanged: number;
  /** Files where the summariser raised an exception (sub-fatal). */
  nodes_failed: number;
  /** Files where the summariser exited 0 with "no backend" or "no title". */
  nodes_skipped: number;
  /** Backend the summariser picked: "cli" | "ollama" | "api" | "skip" | null.
   *  null on terminal `skipped`/`failed` rows where nothing ran. */
  backend: string | null;
  error_message: string | null;
  /** Last ~4 KiB of aggregated subprocess output — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events ("scan" | "summarise"). */
  current_phase: string | null;
}

/** Mirrors `InstallHealth` in commands/installer.rs. Returned by
 *  `check_install_health` once at app startup. When `all_ok` is false the
 *  layout renders `InstallHealthGate.svelte` as a blocking modal. */
export interface InstallHealth {
  /** Resolved install-root path (null = developer mode, no install root
   *  found by walking up from the launcher binary). */
  install_root: string | null;
  has_venv: boolean;
  has_state_dir: boolean;
  has_env_with_kg: boolean;
  mcp_servers_ok: boolean;
  /** True when every signal passes OR when in developer mode. */
  all_ok: boolean;
}
