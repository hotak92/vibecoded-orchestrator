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
   * Bug 16: render hint:
   *   - 'bundled'      → always-installed, no Install button (e.g. the launcher itself)
   *   - 'available'    → catalog-listed, has Install action
   *   - 'installed'    → installed, can be configured / uninstalled
   *   - 'subcomponent' → ships with parent module, navigate to dashboard CTA
   */
  kind: 'bundled' | 'available' | 'installed' | 'subcomponent';
  parent_id: string;
  cta_route: string;
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
