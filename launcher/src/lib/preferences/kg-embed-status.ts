// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-I — the KG embedding-model status for Preferences → Embedding
// profile. (W6-J moved the duplicate scan to the project's KG tab — see
// project-state/kg-duplicates.ts — and the launcher's notifications alert;
// this module keeps only the per-collection model/fill status.)
//
// The answers are computed in Python (`python -m vco_lib.kg_embed_model
// status`, transported verbatim by the Rust `get_kg_embed_model_status`).
// This module only invokes it (injected `invoke`, so tests drive the real
// call) and turns the payload into sentences — no model logic is re-derived
// here.

export interface KgEmbedCollection {
  collection: string;
  recorded_model: string | null;
  /** Objects stored — counted only for an UNRECORDED collection (null when
   *  recorded, not counted, or Weaviate unreachable; 0 = empty/absent). */
  objects: number | null;
  target_model: string;
  filling: boolean;
  filled: number | null;
  total: number | null;
  complete: boolean;
  cost_bearing: boolean;
  target_refused: boolean;
}

export interface KgEmbedProject {
  project_id?: string | null;
  name: string;
  folder: string;
  target_model: string;
  collections: KgEmbedCollection[];
}

export interface KgEmbedStatusPayload {
  target: string;
  projects: KgEmbedProject[];
}

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

export function loadKgEmbedStatus(deps: InvokeDeps): Promise<KgEmbedStatusPayload> {
  return deps.invoke<KgEmbedStatusPayload>('get_kg_embed_model_status');
}

/** What one collection's searches use and where the selected model stands. */
export function kgEmbedFillText(c: KgEmbedCollection): string {
  if (c.target_refused) {
    return 'the selected model is an OpenAI model and "Allow OpenAI for embeddings" is off — it is not filled and no API call is made';
  }
  if (!c.recorded_model) return 'not recorded yet — recorded on the next sync or bundle update';
  if (c.complete) return 'complete';
  if (c.filling && c.filled != null && c.total != null) {
    return `filling: ${c.filled} of ${c.total} objects have the new vector`;
  }
  if (c.filling) {
    const n = c.total != null ? ` (up to ${c.total} objects)` : '';
    return `fill pending${n} — runs on the next whole-tree sync, which VCO starts by itself`;
  }
  return 'complete';
}
