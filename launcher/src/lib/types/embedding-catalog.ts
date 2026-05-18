// SPDX-License-Identifier: AGPL-3.0-or-later
// TS shapes for `get_embedding_catalog` (v0.2.18, Commit 8).
//
// Mirror of the Rust types in `launcher/src-tauri/src/commands/embedding_catalog.rs`
// — keep them in sync. The Rust derives `serde::Serialize` with default field
// naming, so the JSON shape is a direct 1:1 map.

export interface ModelChoice {
  /** Stable id used in KG/codegraph bindings + app_state defaults
   *  (e.g. "qwen3-embedding:0.6b", "openai-text-embedding-3-small"). */
  id: string;
  /** Human-readable label for the dropdown option. */
  label: string;
  /** Vector dim (1024 for qwen3, 1536 for OpenAI text-embedding-3-small,
   *  2048 for CodeSage Large v2, 768 for Jina v2 base code, etc.). */
  dim: number;
  /** Named-vector slot in the Weaviate schema this model writes to. */
  slot: string;
  /** Backend identifier ("ollama" | "codeembed" | "openai"). */
  backend: string;
  /** True iff the backend responded AND (for OpenAI) the key validates. */
  available_now: boolean;
  /** Reason for unavailability when `available_now=false` — e.g.
   *  "OPENAI_API_KEY not set", "Ollama unreachable at http://...". */
  reason_unavailable: string | null;
}

export interface EmbeddingCatalog {
  text_models: ModelChoice[];
  code_models: ModelChoice[];
  current_text_slot: string | null;
  current_code_slot: string | null;
  errors: string[];
}

/** Discriminated union returned by `validate_model_against_catalog`. */
export type ValidationResult =
  | { status: 'valid'; model_id: string; slot: string; backend: string }
  | { status: 'invalid'; reason: string };

export interface DefaultEmbeddingModels {
  text_model: string | null;
  code_model: string | null;
}
