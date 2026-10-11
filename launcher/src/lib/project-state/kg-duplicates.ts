// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-J — the KG duplicate scan surface helpers, shared by the
// project KG tab (KgCodegraphTab.svelte — the "Check for duplicates" action
// and its `objects_missing_vector` list) and the launcher's notifications
// alert (kg-rescan-alerts.ts). Moved out of preferences/kg-embed-status.ts
// (W6-I), where the scan button lived before the owner placed it on the
// project's KG tab (2026-10-11).
//
// The scan itself is the shipped `detect_duplicates.py` via the
// `kg_check_duplicates` Tauri command; this module only invokes it (injected
// `invoke`, so tests drive the real call) and turns the payload into
// sentences — no scan logic is re-derived here.

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

export function runDuplicateScan(projectId: string, deps: InvokeDeps): Promise<DuplicateScanResult> {
  return deps.invoke<DuplicateScanResult>('kg_check_duplicates', { projectId });
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

/** One-line verdict of a finished scan — what the bell's scan button reports
 *  (its result is not kept anywhere else, so the line must carry it). */
export function duplicateScanSummary(label: string, r: DuplicateScanResult): string {
  let s = `${label}: ${r.pairs.length} likely duplicate pair(s) at threshold ${r.threshold}`;
  if (r.nodes_skipped > 0) s += `; ${r.nodes_skipped} node(s) re-synced during the scan were not compared`;
  if (r.objects_missing_vector.length > 0) {
    s += `; ${r.objects_missing_vector.length} object(s) lack the searched vector and were not compared`;
  }
  return s + '. Run "Check for duplicates" on the project\'s KG / Codegraph tab to list them.';
}

/** "has: a, b" / "has no vector at all" — the slots an object DOES carry. */
export function hasSlotsText(o: MissingVectorObject): string {
  return o.has_slots.length > 0 ? `has: ${o.has_slots.join(', ')}` : 'has no vector at all';
}
