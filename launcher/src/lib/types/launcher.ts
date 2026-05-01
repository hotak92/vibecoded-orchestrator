// Type mirrors of Rust command return values. Mirrors live in:
//   launcher/src-tauri/src/commands/projects_v2.rs   ProjectView, SwitchHostResult
//   launcher/src-tauri/src/commands/secrets_cmd.rs   SecretMetadata, SettingEntry
//   launcher/src-tauri/src/commands/licensing.rs     TierCacheView
//   launcher/src-tauri/src/commands/modules.rs       ModuleCatalogEntry, ModuleStatusView
//   launcher/src-tauri/src/db/models.rs              ModuleInstallRow, ProjectHost, ModuleStatus
//
// Keep these in sync. Field naming follows serde defaults (snake_case).

export type ProjectHost = 'base' | 'mao';

export type ModuleStatus = 'installing' | 'installed' | 'running' | 'stopped' | 'error';

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
 * Also reused as the return type of `set_shared_kg_opt_out` (MEDIUM-1).
 */
export interface RenameProjectResult {
  project: ProjectView;
  warnings: string[];
}

export interface TierCacheView {
  orchestrator_tier: LicenseTier | string;
  module_licenses: Record<string, unknown>;
  last_validated: number;
  last_error: string | null;
  grace_period_remaining_ms: number | null;
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
  kind: 'bundled' | 'available' | 'installed' | 'subcomponent' | 'coming_soon';
  parent_id: string;
  cta_route: string;
  /** For `kind === 'coming_soon'`: which tier this will ship under (e.g. 'pro'). */
  coming_soon_tier?: string;
  /** For `kind === 'coming_soon'`: optional public target window (e.g. 'Q3 2026'). */
  coming_soon_target?: string;
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
