// SPDX-License-Identifier: AGPL-3.0-or-later
// TS shapes for `enrich_collection_vectors` (v0.2.18, Commit 9).
//
// Mirror of the Rust + Python types in
// `launcher/src-tauri/src/commands/embedding_enrichment.rs` and
// `vco_lib/embedding_enrichment.py`. Keep all three in sync — drift
// causes silent deserialise failures at the Tauri boundary.

/**
 * Per-batch progress event payload. Emitted on the
 * `vct-enrichment-progress` Tauri event while the Python subprocess
 * streams `--stream-progress` lines. The Svelte modal binds the progress
 * bar to `progress` ∈ [0, 1] and displays `message` as the sub-text.
 */
export interface EnrichmentProgress {
  /** Weaviate class being enriched (echoed back so the UI can sanity-
   *  check the event is for the modal it's showing). */
  collection: string;
  /** Named-vector slot being populated. */
  new_slot: string;
  /** Fractional progress in [0, 1]. May exceed 1 by tiny float drift —
   *  the UI should `Math.min(progress, 1)` when computing the width. */
  progress: number;
  /** Human-readable sub-text: e.g. "Enriched 420/1000 (5 skipped, 0 failed)". */
  message: string;
}

/**
 * One row in the final-report `failures` array.
 *
 * Carries either:
 *   - `{uuid, error}` for per-object failures (embed call raised, write
 *     failed, etc.), OR
 *   - `{dry_run_count}` for the dry-run sentinel (failures[0] in
 *     dry-run mode lists the would-have-enriched count).
 *
 * The Python side flattens both shapes into the same array slot; the TS
 * type accepts a union via optional fields so both render paths work.
 */
export interface EnrichmentFailure {
  uuid?: string;
  error?: string;
  /** Dry-run sentinel: when present, this row is NOT a per-object
   *  failure but the count of objects that WOULD have been enriched. */
  dry_run_count?: number;
}

/**
 * Final report returned by `enrich_collection_vectors`. The modal
 * switches from "running" to "complete" state when this resolves.
 */
export interface EnrichmentReport {
  collection: string;
  new_slot: string;
  /** Total objects walked. May be 0 (empty collection). */
  total: number;
  /** Objects gained a new-slot vector this run. */
  enriched: number;
  /** Objects already had the slot populated (idempotency counter). */
  skipped: number;
  /** Objects where embed OR write failed. Detail in `failures` (capped
   *  at MAX_FAILURE_DETAILS=20). A `failed > failures.length` value
   *  means the detail cap was hit. */
  failed: number;
  failures: EnrichmentFailure[];
}
