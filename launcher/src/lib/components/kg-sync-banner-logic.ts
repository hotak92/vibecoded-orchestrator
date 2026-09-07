// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.92 WP-B1 — pure decision logic for the KG-sync progress surfaces
// (KgSyncBanner + KgSyncPill). Extracted for the same reason as
// `codegraph-build-banner-logic.ts`: the running-phase label decision was
// inline in the Svelte components, untestable without mounting them.
//
// The phase VOCABULARY lives here, once — both components classify through
// `kgSyncPhaseKind`, so a new Rust stage string needs exactly one edit and
// can never be "known" to one surface and "unknown" to the other.

import type { KgSyncView } from '$lib/types/launcher';

/**
 * Kind of live phase a running sync is in.
 *
 * - 'scan'     — pre-flight (launcher-emitted before the script starts)
 * - 'queued'   — waiting for the process-global embed lane (v0.2.71)
 * - 'embed'    — knowledge/ embedding (phases 'knowledge', 'embed', null)
 * - 'docs'     — docs/ embedding
 * - 'finalize' — post-summary `.node_formats.json` regen (v0.2.92 WP-B1);
 *   the script prints its `📝 Refreshing …` stage marker AFTER the final
 *   `📊` counts and can legitimately spend up to 600 s there — the GUI
 *   must show it instead of a stalled "embedding (N/N)".
 * - 'unknown'  — a phase string this build does not recognize. Must render
 *   NEUTRALLY: pre-fix, an unknown phase fell through to the generic
 *   branch and rendered a confident, WRONG "embedding (N/N)" label.
 */
export type KgSyncPhaseKind =
  | 'scan'
  | 'queued'
  | 'finalize'
  | 'embed'
  | 'docs'
  | 'unknown';

export function kgSyncPhaseKind(phase: string | null | undefined): KgSyncPhaseKind {
  switch (phase) {
    case 'scan':
      return 'scan';
    case 'queued':
      return 'queued';
    case 'finalize':
      return 'finalize';
    case 'docs':
      return 'docs';
    // 'embed' is the launcher's own pre-script phase; 'knowledge' is the
    // script's per-node phase; null is the historical/unknown-absent shape.
    // All three mean "embedding knowledge/".
    case 'embed':
    case 'knowledge':
    case null:
    case undefined:
      return 'embed';
    default:
      return 'unknown';
  }
}

/** Files fully accounted for so far: written OR intentionally skipped. */
export function kgSyncDoneCount(
  v: Pick<KgSyncView, 'kg_succeeded' | 'kg_skipped' | 'docs_succeeded' | 'docs_skipped'>,
): number {
  return (
    v.kg_succeeded +
    (v.kg_skipped ?? 0) +
    v.docs_succeeded +
    (v.docs_skipped ?? 0)
  );
}

/** Total files the script reported (knowledge/ + docs/). */
export function kgSyncTotalCount(
  v: Pick<KgSyncView, 'kg_total' | 'docs_total'>,
): number {
  return v.kg_total + v.docs_total;
}

/**
 * Full-sentence running label for KgSyncBanner.
 *
 * `counter` is the banner's "done / total" string ('' when totals are
 * unknown). Behavior is identical to the pre-v0.2.92 inline mapping for
 * every KNOWN phase; the two deliberate changes are the new 'finalize'
 * stage and the neutral rendering of unknown phases (see
 * `KgSyncPhaseKind`).
 */
export function kgSyncBannerRunningLabel(
  counter: string,
  phase: string | null | undefined,
): string {
  const kind = kgSyncPhaseKind(phase);
  if (kind === 'scan') return 'KG sync: scanning knowledge/ and docs/…';
  if (kind === 'queued') return 'KG sync: waiting for the embed lane…';
  if (kind === 'finalize') return 'KG sync: finalizing summaries…';
  if (kind === 'unknown') {
    return counter ? `KG sync: ${phase} (${counter})` : `KG sync: ${phase}…`;
  }
  if (!counter) return 'KG sync: embedding…';
  if (kind === 'docs') return `KG sync: embedding docs (${counter})`;
  return `KG sync: embedding (${counter})`;
}

/**
 * Success label for KgSyncBanner — counts files actually indexed, not the
 * discovered total. v0.2.92 WP-B1 / D12: pre-fix, archived and other
 * intentionally-skipped nodes were counted as "succeeded" by the script,
 * so "indexed 117 nodes" could include 4 that are deliberately absent
 * from Weaviate; now the skip count is surfaced instead of hidden.
 */
export function kgSyncBannerSuccessLabel(v: KgSyncView): string {
  const total = kgSyncTotalCount(v);
  if (total === 0) return 'KG sync: complete';
  const indexed = v.kg_succeeded + v.docs_succeeded;
  const skipped = (v.kg_skipped ?? 0) + (v.docs_skipped ?? 0);
  const base = `KG sync: indexed ${indexed} node${indexed === 1 ? '' : 's'}`;
  return skipped > 0 ? `${base} (${skipped} skipped)` : base;
}
