// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-I — the KG embedding-model status + the duplicate scan's
// "objects missing the vector" list, for Preferences → Embedding profile.
//
// The answers are computed in Python (`python -m vco_lib.kg_embed_model
// status`, transported verbatim by the Rust `get_kg_embed_model_status`) and
// by the shipped duplicate scanner (`kg_check_duplicates`). This module only
// invokes them (injected `invoke`, so tests drive the real calls) and turns
// the payloads into sentences — no model logic is re-derived here.

export interface KgEmbedCollection {
  collection: string;
  recorded_model: string | null;
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

/** One object the duplicate scan could not compare (`objects_missing_vector`). */
export interface MissingVectorObject {
  uuid: string;
  title: string;
  path: string;
  target_vector: string;
  has_slots: string[];
}

export interface DuplicateScanResult {
  threshold: number;
  pairs: unknown[];
  nodes_skipped: number;
  objects_missing_vector: MissingVectorObject[];
}

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

export function loadKgEmbedStatus(deps: InvokeDeps): Promise<KgEmbedStatusPayload> {
  return deps.invoke<KgEmbedStatusPayload>('get_kg_embed_model_status');
}

export function runDuplicateScan(projectId: string, deps: InvokeDeps): Promise<DuplicateScanResult> {
  return deps.invoke<DuplicateScanResult>('kg_check_duplicates', { projectId });
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

/** The short "why" shown above the duplicate scan's skip list. */
export function missingVectorExplanation(objs: MissingVectorObject[]): string {
  if (objs.length === 0) return '';
  const slot = objs[0].target_vector || 'the searched slot';
  return (
    `${objs.length} object(s) have no vector in ${slot}, so the scan could not compare them and ` +
    'any duplicate involving them is missing from the result. They were written by an older sync ' +
    'whose embedding backend failed, or while another embedding profile was active; the next ' +
    'whole-tree sync embeds them.'
  );
}

/** "has: a, b" / "has no vector at all" — the slots an object DOES carry. */
export function hasSlotsText(o: MissingVectorObject): string {
  return o.has_slots.length > 0 ? `has: ${o.has_slots.join(', ')}` : 'has no vector at all';
}
