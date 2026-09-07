// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-D (GUI half) — pure presentation logic for the per-project
// bundle-staleness chip and the post-orchestrator-update count.
//
// Why the logic lives here rather than inline in the Svelte component:
// the same reason `project-folder-health.ts` does — the project's vitest
// config is deliberately minimal (pure node, no jsdom / testing-library),
// so the gating contract is pinned by testing pure functions and the
// component stays a thin renderer over them.
//
// ─── The one invariant this module exists to hold ────────────────────────
//
// A check that cannot distinguish "I could not determine this" from "this
// is fine" is not a check. Three consequences, each pinned by a test in
// `bundle-staleness.test.ts`:
//
//   1. `unknown` is its OWN verdict. It is never rendered as `current`
//      (which would repeat the exact five-week lie WP-D exists to stop)
//      and never folded into `stale` (which would make the remedy look
//      applicable when it is not — you cannot bundle-update a project
//      whose folder is gone).
//   2. A census that could not RUN yields `determined: false` from the
//      backend, and every project then renders `unknown`, not `current`.
//   3. The count is `null`, not `0`, when nothing could be determined.
//      `0` is a claim; `null` is the absence of one.

import type {
  BundleStalenessCensus,
  BundleStalenessProject,
  BundleVerdict,
} from '$lib/types/launcher';

/** Synthetic reason for "the census itself could not run". Not a Python
 *  reason — the backend reports that through `determined: false`, and this
 *  is how a per-project chip names it. */
export const CENSUS_UNAVAILABLE_REASON = 'census_unavailable';

/** Synthetic reason for "the census ran but has no row for this project"
 *  (registered after the census, or filtered out). Still `unknown`: a
 *  missing row proves nothing about the bundle. */
export const NOT_IN_CENSUS_REASON = 'not_in_census';

/** Tone hook for the chip's CSS class. One tone per verdict — the test
 *  pins that the set has three members, so a future edit cannot quietly
 *  paint `unknown` with the `current` colour. */
export type BundleChipTone = 'ok' | 'warn' | 'unknown';

export interface BundleChip {
  verdict: BundleVerdict;
  tone: BundleChipTone;
  /** Short user-visible label rendered in the chip. */
  label: string;
  /** One-sentence cause, shown as the chip's title/tooltip. */
  detail: string;
  /** Machine reason behind `detail` (Python's, or one of the two
   *  synthetic reasons above). Kept so the UI can key off the cause. */
  reason: string;
}

/** Human copy for every reason the census can emit. Centralised so the
 *  wording is a deliberate UX decision, and so an unrecognised reason
 *  still produces an honest sentence rather than an empty tooltip. */
const REASON_COPY: Record<string, string> = {
  // current
  noop: 'A bundle update would change nothing in this project.',
  // stale
  files_changed: 'A bundle update would change files in this project.',
  // unknown
  folder_missing: 'The project folder could not be found on disk.',
  manifest_missing: 'The project has no .claude/.vco-manifest.json to compare against.',
  manifest_unparseable: 'The project manifest could not be parsed.',
  engine_error: 'The bundle engine could not classify this project.',
  self_check_failed: 'The post-update self-check could not prove the bundle landed.',
  [CENSUS_UNAVAILABLE_REASON]: 'The bundle census could not run, so this project was not checked.',
  [NOT_IN_CENSUS_REASON]: 'This project was not covered by the last bundle census.',
};

function copyFor(reason: string): string {
  return REASON_COPY[reason] ?? `Undetermined (${reason}).`;
}

/**
 * Index a census's rows by project id.
 *
 * An UNDETERMINED census yields an EMPTY map on purpose: when the probe
 * failed, no row may be treated as known, and the caller must fall through
 * to the `unknown` chip rather than reading a stale/partial `projects`
 * array that a future backend change might leave populated.
 */
export function indexCensus(
  census: BundleStalenessCensus | null | undefined,
): Map<string, BundleStalenessProject> {
  const out = new Map<string, BundleStalenessProject>();
  if (!census || !census.determined) return out;
  for (const row of census.projects ?? []) out.set(row.id, row);
  return out;
}

/**
 * The chip to render for one project.
 *
 * Falls to `unknown` — never `current` — for all three "we don't know"
 * cases: no census, an undetermined census, and a determined census with
 * no row for this project.
 */
export function chipFor(
  census: BundleStalenessCensus | null | undefined,
  projectId: string,
): BundleChip {
  if (!census || !census.determined) {
    return unknownChip(CENSUS_UNAVAILABLE_REASON);
  }
  const row = indexCensus(census).get(projectId);
  if (!row) return unknownChip(NOT_IN_CENSUS_REASON);

  if (row.verdict === 'current') {
    return {
      verdict: 'current',
      tone: 'ok',
      label: 'Bundle current',
      detail: copyFor(row.reason),
      reason: row.reason,
    };
  }
  if (row.verdict === 'stale') {
    const n = (row.changed_files ?? []).length;
    const files = `${n} file${n === 1 ? '' : 's'}`;
    return {
      verdict: 'stale',
      tone: 'warn',
      label: 'Bundle stale',
      detail: `A bundle update would change ${files} in this project.`,
      reason: row.reason,
    };
  }
  // Anything the backend did not positively prove current-or-stale.
  return unknownChip(row.reason || CENSUS_UNAVAILABLE_REASON);
}

function unknownChip(reason: string): BundleChip {
  return {
    verdict: 'unknown',
    tone: 'unknown',
    label: 'Bundle unknown',
    detail: copyFor(reason),
    reason,
  };
}

/**
 * How many projects need the user's attention — stale PLUS undetermined.
 *
 * `null` (not `0`) when the census could not run. Callers must render the
 * null case as "could not determine", never as a zero badge.
 */
export function needsAttentionCount(
  census: BundleStalenessCensus | null | undefined,
): number | null {
  if (!census || !census.determined || !census.summary) return null;
  return census.summary.stale + census.summary.unknown;
}

/**
 * The one-line summary rendered on the Projects page (and after an
 * orchestrator update completes — the moment the gap is created, since
 * updating the orchestrator does NOT update the projects installed from
 * it).
 *
 * Names the two populations separately, exactly as the ledger entry does,
 * so a user can clear them cause by cause.
 */
export function summaryLine(
  census: BundleStalenessCensus | null | undefined,
): string {
  if (!census || !census.determined || !census.summary) {
    const why = census?.error ? ` (${census.error})` : '';
    return `Could not determine the bundle state of your projects${why}.`;
  }
  const { current, stale, unknown } = census.summary;
  const total = current + stale + unknown;
  if (total === 0) {
    return 'No projects are registered, so there are no bundles to check.';
  }
  if (stale === 0 && unknown === 0) {
    return `All ${total} project bundle${total === 1 ? ' is' : 's are'} up to date.`;
  }
  const parts: string[] = [];
  if (stale > 0) parts.push(`${stale} stale`);
  if (unknown > 0) parts.push(`${unknown} could not be determined`);
  return `${parts.join(', ')} of ${total} project bundle${total === 1 ? '' : 's'}.`;
}

/**
 * Build the UNDETERMINED census payload for a client-side failure — the
 * `bundle_staleness_census` command is contracted never to reject, so a
 * rejection means the command is absent entirely (an older launcher
 * binary). Reported as "could not determine", never as an absence of
 * findings.
 */
export function undeterminedCensus(error: string): BundleStalenessCensus {
  return {
    determined: false,
    error,
    registry: null,
    running_version: null,
    projects: [],
    summary: null,
    remedy_gui: null,
    remedy_cli: null,
  };
}

/**
 * Should the page re-run the census on this updater tick?
 *
 * ONLY on the falling edge (an orchestrator update just completed) — the
 * moment the gap between orchestrator and project bundles is created.
 * Pinned as a pure predicate because the naive form (`!updating`) is true
 * on every reactive tick and would turn the page into a subprocess poll
 * loop against the census.
 */
export function shouldRecensusOnUpdaterEdge(
  prevUpdating: boolean,
  updating: boolean,
): boolean {
  return prevUpdating && !updating;
}
