// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-J — the "a project's KG may need a duplicate re-scan" alert in
// the launcher's notification bell (owner 2026-10-11: the duplicate scan's
// surfaces are the project's KG tab AND a launcher notification listing the
// projects that may need a re-scan, with a button that runs it).
//
// "May need a re-scan" is computed from signals that ALREADY exist — the
// `get_kg_embed_model_status` payload (`python -m vco_lib.kg_embed_model
// status`). A project qualifies when any of its KG collections:
//
//   * is FILLING (a newly selected model's vectors are being added): a scan
//     run before or during the fill could not compare the objects that were
//     still missing the vector — a fresh scan finds their duplicates; or
//   * has NO recorded model yet while it HOLDS objects (`objects > 0`): a
//     record is stamped off a cursor scan only when one slot is complete on
//     every object, so a stored collection without one is either a
//     pre-v0.2.102 collection whose stamp failed (Weaviate down at the
//     bundle update) or one with no complete slot — objects missing the
//     searched vector, exactly what the scan's `objects_missing_vector`
//     reports. An EMPTY or absent collection (a fresh project, an unused
//     development collection) also has no record and never qualifies —
//     it would otherwise alert forever (W6-J review of the partial lane).
//
// A collection whose target is OpenAI-REFUSED (the embeddings consent is
// off) never qualifies on its own — that is a consent state the owner must
// change, not a scan deficiency, and alerting on it would nag forever.
//
// "A fill COMPLETED since the last scan" is deliberately NOT a signal: the
// every-10-edits hook runs most scans and records no timestamp anywhere, so
// no honest "last scanned at" exists to compare the flip time against.
//
// The store and the invoke are injectable so vitest pins every branch; the
// production wiring is +layout.svelte's startup check.

import type { NotificationAction } from '$lib/stores/notifications';
import {
  loadKgEmbedStatus,
  type KgEmbedStatusPayload,
} from '$lib/preferences/kg-embed-status';

/** The alert's dedup/replace key in the notifications store. */
export const KG_RESCAN_ALERT_KEY = 'kg-duplicate-rescan';

export interface RescanCandidate {
  projectId: string;
  name: string;
}

/** The projects whose KG may need a fresh duplicate scan (see the header). */
export function findRescanCandidates(payload: KgEmbedStatusPayload): RescanCandidate[] {
  const out: RescanCandidate[] = [];
  for (const p of payload.projects) {
    if (!p.project_id) continue;
    const needs = p.collections.some(
      (c) =>
        c.filling || (!c.recorded_model && !c.target_refused && (c.objects ?? 0) > 0),
    );
    if (needs) out.push({ projectId: p.project_id, name: p.name });
  }
  return out;
}

/** The alert's message + one scan action per candidate project. */
export function rescanAlertEntry(candidates: RescanCandidate[]): {
  key: string;
  message: string;
  actions: NotificationAction[];
} {
  return {
    key: KG_RESCAN_ALERT_KEY,
    message:
      candidates.length === 1
        ? `The knowledge graph of ${candidates[0].name} may need a fresh duplicate scan (a new embedding model is being filled in, or some stored objects may lack the vector the scan compares).`
        : `${candidates.length} projects' knowledge graphs may need a fresh duplicate scan (a new embedding model is being filled in, or some stored objects may lack the vector the scan compares).`,
    actions: candidates.map((c) => ({
      label: `Scan ${c.name}`,
      // Identity = projectId: two registered projects may share a display
      // name, and the bell keys/drops actions by this, not the label.
      key: c.projectId,
      command: 'kg_check_duplicates',
      args: { projectId: c.projectId },
    })),
  };
}

export interface SyncDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  store: {
    upsertAlert: (entry: { key: string; message: string; actions?: NotificationAction[] }, now: number) => void;
    resolve: (key: string) => void;
  };
  now?: () => number;
}

/** Re-evaluate candidacy and make the bell entry match: upsert while any
 *  project qualifies, clear when none does. A failed status read changes
 *  nothing (an alert already shown stays; none is invented). */
export async function syncKgRescanAlert(deps: SyncDeps): Promise<void> {
  let payload: KgEmbedStatusPayload;
  try {
    payload = await loadKgEmbedStatus({ invoke: deps.invoke });
  } catch {
    return; // the status read is best-effort — never invent an alert
  }
  const candidates = findRescanCandidates(payload);
  if (candidates.length === 0) {
    deps.store.resolve(KG_RESCAN_ALERT_KEY);
    return;
  }
  deps.store.upsertAlert(rescanAlertEntry(candidates), (deps.now ?? Date.now)());
}
