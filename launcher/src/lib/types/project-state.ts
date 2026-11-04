// Mirrors of Rust types in:
//   launcher/src-tauri/src/db/project_state.rs
//   launcher/src-tauri/src/commands/project_state_cmd.rs
//   launcher/src-tauri/src/commands/kg.rs
//   launcher/src-tauri/src/commands/codegraph.rs
//   launcher/src-tauri/src/commands/coordination.rs
//   launcher/src-tauri/src/commands/telemetry_cmd.rs
//
// Field naming follows serde defaults (snake_case).

// ─── Project state rows ──────────────────────────────────────────────────

import type { StorePresence } from '$lib/stores/secrets';

export interface ProjectAgent {
  project_id: string;
  agent_name: string;
  source: string; // bundled | user | paid-module | project
  source_module: string | null;
  model: string | null;
  enabled: boolean;
  file_path: string | null;
  config: Record<string, unknown>;
  installed_at: number;
  updated_at: number;
}

export interface ProjectSkill {
  project_id: string;
  skill_name: string;
  source: string;
  source_module: string | null;
  model: string | null;
  enabled: boolean;
  file_path: string | null;
  config: Record<string, unknown>;
  installed_at: number;
  updated_at: number;
}

export interface ProjectHook {
  id: number;
  project_id: string;
  event: string;
  matcher: string;
  command: string;
  source: string;
  source_module: string | null;
  enabled: boolean;
  timeout_ms: number | null;
  config: Record<string, unknown>;
  installed_at: number;
  updated_at: number;
}

export interface ProjectPermission {
  id: number;
  project_id: string;
  subject: string;
  kind: string; // write_scope | allowed_tool | denied_tool | mcp_server | permission_mode
  value: string;
  config: Record<string, unknown>;
  granted_at: number;
}

export interface ProjectSecretRef {
  project_id: string;
  secret_key: string;
  resolution: string; // keychain-per-project | keychain-shared | keychain-global | file | env
  file_path: string | null;
  env_name: string | null;
  source_module: string | null;
  required_for: string[];
  description: string;
  is_set: boolean;
  updated_at: number;
}

export interface ProjectKgBinding {
  project_id: string;
  role: string; // primary | shared | archive
  collection_name: string;
  // v0.2.102 W6-J: no `embedding_model` — the per-project KG model pick was
  // retired (one global embedding setting). The DB column survives; nothing
  // reads or writes it.
  embedding_dim: number | null;
  kg_dir_path: string | null;
  weaviate_url: string | null;
  config: Record<string, unknown>;
  updated_at: number;
}

export interface ProjectCodegraphBinding {
  project_id: string;
  collection_prefix: string;
  embedding_model: string | null;
  embedding_dim: number | null;
  last_analyzed_commit: string | null;
  last_analyzed_at: number | null;
  enabled: boolean;
  config: Record<string, unknown>;
  updated_at: number;
}

export interface ProjectStateSnapshot {
  project_id: string;
  agents: ProjectAgent[];
  skills: ProjectSkill[];
  hooks: ProjectHook[];
  permissions: ProjectPermission[];
  secret_refs: ProjectSecretRef[];
  kg_bindings: ProjectKgBinding[];
  codegraph_binding: ProjectCodegraphBinding | null;
}

// ─── Packs (v0.2.101 catalogue plan §3.6) ─────────────────────────────────
//
// Mirrors the Rust struct `PackInfo` in
//   launcher/src-tauri/src/commands/packs_cmd.rs
// (the wiring test `src/lib/packs.wiring.test.ts` pins the two stay in
// lockstep). Backed by the Python SSOT (`vco_lib.packs status`) — the
// launcher only bridges and parses JSON.

export interface PackInfo {
  name: string;
  description: string;
  /** Member names: agent file stems + skill directory names. */
  members: string[];
  /** True iff the pack is recorded in the project manifest's `packs` map. */
  installed: boolean;
}

// ─── Access scoping (UI-side aggregate, mapped to backend kg/codegraph access) ──

export interface AccessMode {
  /**
   * UI-level access mode.
   * - shared: visible to every project (kg: 'write' for all + sharedVCT marker)
   * - projects: only listed project IDs (kg: 'read' rows for each)
   * - private: only owner project (kg: no rows except owner's 'write')
   */
  mode: 'shared' | 'projects' | 'private';
  project_ids: string[];
  owner_project_id: string | null;
}

// ─── KG dashboard ────────────────────────────────────────────────────────

export interface KgCollectionAccess {
  name: string;
  /** v0.2.105 lazy-load counts: null = never counted (render "—" via
   * formatCount). Live only for this project's own/shared/granted
   * collections; everything else carries the last cached value. */
  node_count: number | null;
  /** Epoch-ms moment node_count was measured (null with a null count). */
  counted_at: number | null;
  access: 'read' | 'write' | 'none' | string;
  is_shared: boolean;
}

export interface KgNode {
  id: string;
  title: string;
  node_type: string;
  tags: string[];
  collection: string;
  excerpt: string;
  file_path: string | null;
}

export interface KgEdge {
  from_id: string;
  to_id: string;
  relationship_type: string;
}

export interface KgGraph {
  nodes: KgNode[];
  edges: KgEdge[];
  total_nodes_in_collection: number;
  truncated: boolean;
}

export interface KgNodeFull {
  id: string;
  title: string;
  node_type: string;
  tags: string[];
  collection: string;
  content: string;
  file_path: string | null;
  outgoing_links: KgEdge[];
  /** Optional: list of project IDs this node is shared with (cross_project_access). */
  cross_project_access?: string[];
}

// ─── Codegraph ───────────────────────────────────────────────────────────

export interface ProjectRef {
  id: string;
  name: string;
}

export interface CodegraphAccessMatrix {
  project_id: string;
  can_read_from: ProjectRef[];
  readable_by: ProjectRef[];
}

// ─── Coordination ────────────────────────────────────────────────────────

export interface CoordinationConfig {
  project_id: string;
  installed: boolean;
  enabled: boolean;
  supabase_url: string | null;
  /** Tri-state, not a boolean: a locked/erroring keychain reports
   * `'unknown'`, never `'absent'`. Rendering "not set" for a store we
   * could not read is what prompts a user to re-paste a secret they
   * already have. Same vocabulary as `StorePresence` in
   * `$lib/stores/secrets`. */
  supabase_key_presence: StorePresence;
  telegram_bot_token_presence: StorePresence;
  username: string | null;
  user_aliases: string[];
  channels_enabled: string[];
  telegram_group_id: string | null;
}

export interface CoordinationConfigUpdate {
  supabase_url?: string;
  supabase_key?: string;
  telegram_bot_token?: string;
  username?: string;
  user_aliases?: string[];
  channels_enabled?: string[];
  telegram_group_id?: string;
}

export interface ConnectionTestResult {
  reachable: boolean;
  latency_ms: number | null;
  auth_ok: boolean;
  schema_applied: boolean;
  error: string | null;
}

export interface TeamMember {
  username: string;
  display_name: string;
  role: string;
}

export interface PresenceEntry {
  username: string;
  source: string;
  status: string;
  last_seen: string;
}

export interface TeamStatus {
  members: TeamMember[];
  presence: PresenceEntry[];
  recent_messages_count: number;
  online_now: number;
  connection_ok: boolean;
}

// ─── Telemetry ───────────────────────────────────────────────────────────

export interface ConsentFlags {
  consent_version: string;
  granted_at: string | null;
  always_on: boolean;
  rl_data: boolean;
  routing_data: boolean;
  instinct_data: boolean;
  hardware: boolean;
}

export interface TelemetryStatus {
  consent: ConsentFlags;
  queue_size: number;
  last_upload_at: number | null;
  last_upload_error: string | null;
  disabled_via_env: boolean;
}

export interface TelemetryEventView {
  id: number;
  event_type: string;
  created_at: number;
  uploaded_at: number | null;
  payload_summary: string;
}

// (v0.2.102) The Diagrams tab and every diagram type it alone used
// (`DiagramType`, `DiagramRow`, `DiagramSnapshotRow`, `DiagramChangedPayload`,
// `SnapshotTrigger`) were removed with the dropped mermaid/excalidraw
// integrations (owner ruling 2026-10-07). The launcher DB tables behind them
// (migration 022) stay — user data is never dropped.
//
// (v0.2.101) `McpToolGrant` removed: its only consumer was the PermissionsTab
// "MCP Tools" sub-section, removed with the retired diagram wrapper MCPs.

