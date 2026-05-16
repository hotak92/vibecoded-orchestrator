// Type mirrors of Rust types in `launcher/src-tauri/src/commands/project_identity.rs`.
// PR-8 (v0.2.11 / 2026-05-15): per-project Identity tab + legacy code-graph
// collection cleanup. Field naming follows serde defaults (snake_case).

export interface ProjectIdentity {
  project_id: string;
  name: string;
  folder_path: string;
  /** Raw host string. May be 'base' | 'mao' | 'orchestrator_root' (PR-3-v2). */
  host: string;
  slug: string;
  /** True when this row is the orchestrator-root project. Computed by the
   *  Rust side via slug=='orchestrator-root' OR host=='orchestrator_root'. */
  is_orchestrator_root: boolean;
  kg_collection: string;
  code_graph_project: string;
  /** Source-of-truth file the launcher reads for re-detection
   *  ('.vscode/settings.json' for user projects,
   *  '.claude/settings.json' for the orchestrator root). */
  identity_source: string;
  /** Only populated for the orchestrator root: bundled launcher version
   *  exported by `vct-module.json::version`. */
  vct_module_version: string | null;
}

export interface UpdateProjectIdentityRequest {
  kg_collection?: string | null;
  code_graph_project?: string | null;
}

export interface UpdateProjectIdentityResult {
  identity: ProjectIdentity;
  warnings: string[];
}

export interface LegacyCodegraphCollection {
  /** Full Weaviate class name, e.g. 'ClaudeOrchestrator_CodeFunction'. */
  class: string;
  /** Suffix: 'CodeModule' | 'CodeClass' | 'CodeFunction' | 'CodeAPI' | 'CodeInteraction'. */
  suffix: string;
  object_count: number;
}

export interface AffectedProject {
  project_id: string;
  name: string;
  current_prefix: string;
}

export interface LegacyCodegraphReport {
  collections: LegacyCodegraphCollection[];
  affected_projects: AffectedProject[];
  /** True when the launcher should surface the one-time notification. */
  action_recommended: boolean;
}

export interface CleanupLegacyFailure {
  class: string;
  error: string;
}

export interface CleanupLegacyReport {
  deleted: string[];
  failed: CleanupLegacyFailure[];
}
