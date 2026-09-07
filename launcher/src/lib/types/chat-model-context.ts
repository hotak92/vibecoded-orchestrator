// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-11 — wire types for the chat-model context table.
//
// Mirrors the serde shapes in
// `launcher/src-tauri/vct-launcher-core/src/db/chat_model_context.rs` and
// `launcher/src-tauri/src/commands/chat_model_context.rs`. Field names are
// snake_case because that is what serde emits; renaming them here would only
// move the impedance mismatch, not remove it.
//
// NOT the embedding-model token limits (`weaviate_mcp/chunking.py`
// `MODEL_TOKEN_LIMITS`): that table covers EMBEDDING models, sets Ollama's
// `num_ctx` for the chunker, and matches partially on purpose. This one
// covers CHAT models, is keyed by EXACT full model id, and only decides what
// the model gateway advertises to Claude Code.

/** One persisted row. */
export interface ChatModelContextRow {
  /** FULL vendor model id — never a family prefix. Primary key. */
  model_id: string;
  vendor: string;
  context_window: number;
  max_output: number;
  /** Advertise this id to Claude Code as `<id>[1m]`. */
  window_1m: boolean;
  /** The official page these numbers were read from. Never empty. */
  source: string;
  /** Optional caveat that travels with the citation. */
  source_note: string;
  /** A human edited this row; the next reseed will leave it alone. */
  user_edited: boolean;
  /** ISO-8601 UTC. */
  updated_at: string;
}

/** The caller-supplied half of a row (the store owns the other two fields). */
export interface ChatModelContextInput {
  model_id: string;
  vendor: string;
  context_window: number;
  max_output: number;
  window_1m: boolean;
  source: string;
  source_note: string;
}

/** What an export attempt did. `ok: false` means the table and the file the
 *  gateway reads have diverged — the pane says so rather than hiding it. */
export interface ExportReport {
  ok: boolean;
  path: string;
  path_overridden_by_env: boolean;
  models: number;
  generated_at: string | null;
  error: string | null;
}

/** What a reseed did. Four counters so the `user_edited` guard is visible. */
export interface ReseedOutcome {
  inserted: number;
  updated: number;
  unchanged: number;
  preserved_user_edits: number;
}

/** Every mutating command returns its own result AND the export outcome. */
export interface ChatModelContextMutation {
  row: ChatModelContextRow | null;
  deleted: boolean;
  reseed: ReseedOutcome | null;
  export: ExportReport;
}

/** Header status for the pane. */
export interface ChatModelContextStatus {
  rows: number;
  user_edited_rows: number;
  export_path: string;
  export_path_overridden_by_env: boolean;
  export_exists: boolean;
  /** `generated_at` read back OUT of the file — what the gateway sees now. */
  export_generated_at: string | null;
  export_models: number | null;
  /** Set when the file exists but the gateway will refuse it. */
  export_problem: string | null;
  seed_path: string | null;
  seed_available: boolean;
  seed_problem: string | null;
}
