// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 WP-I (decision #6) — wire types + pure rendering decisions for the
// deferral-ledger panel.
//
// Extracted from the Svelte component for the same reason as
// `codegraph-build-banner-logic.ts` / `regenerate-modal-logic.ts`: the repo has
// no jsdom / testing-library (a deliberate scope decision, wave-2 verdict 2),
// so a component test would mean new devDeps and a lockfile regen for one file.
// Everything that DECIDES anything lives here and is unit-tested; the .svelte
// file is markup over these functions.
//
// The wire shapes mirror `launcher/src-tauri/src/commands/deferral_ledger.rs`.

/** Which ledger a view describes. Mirrors the Rust `LedgerScope`. */
export type LedgerScope = 'project' | 'orchestrator_root';

/** Where an entry's disposition came from. Mirrors `DispositionSource`. */
export type DispositionSource = 'entry' | 'registry' | 'default';

export interface RetryAttempt {
  ts: string;
  /** `started` | `retried` | `failed` | `inconclusive` | `skipped`. */
  status: string;
  detail: string;
}

export interface RetrySummary {
  attempts: number;
  cap: number;
  cap_reached: boolean;
  succeeded: number;
  failed: number;
  /** Ran, exited 0, condition still present — honest "unproven", NOT failed. */
  inconclusive: number;
  skipped: number;
  /** Outcome rows only, oldest first (the `started` bookkeeping is excluded). */
  outcomes: RetryAttempt[];
}

export interface LedgerEntry {
  condition_id: string;
  title: string;
  detected: string;
  why_deferred: string;
  /** Verbatim, multi-line, `#` comments intact — render in a `<pre>`. */
  command_to_apply: string;
  severity: string;
  detected_at: string;
  kg_node_refs: string[];
  disposition: string;
  disposition_source: DispositionSource;
  actionable: boolean;
  auto_retryable: boolean;
  retries: RetrySummary;
}

export interface DeferralLedgerView {
  scope: LedgerScope;
  scope_label: string;
  folder: string;
  /** `sidecar` | `absent` | `unavailable`. */
  source: string;
  entries: LedgerEntry[];
  /** The actionable partition (`action_required` + `auto_retryable`) — the
   *  group's membership, and what doctor / the CLAUDE.md reminder count. */
  actionable_count: number;
  /** `action_required` alone — what the BADGE counts (user decision
   *  2026-08-27). Mirrors the Rust `action_required_count`. */
  action_required_count: number;
  record_count: number;
  warnings: string[];
}

export interface DismissOutcome {
  condition_id: string;
  scope: LedgerScope;
  scope_label: string;
  folder: string;
  dismissed: boolean;
  remaining: number;
  reason: string;
}

/** The orchestrator-root ledger's fixed display name (matches the Rust side). */
export const ROOT_SCOPE_LABEL = 'Orchestrator root';

/**
 * The condition the convergence engine emits when a boot pass ends with real
 * pending work. Surfaced on the MCP maintenance page as well as in the ledger,
 * because "some project's MCP rows are not at the current defaults" is a fact a
 * user hunts for on the MCP page, not in a deferral list.
 *
 * MUST MATCH `convergence.rs::CID_CONVERGENCE_PENDING`.
 */
export const CID_CONVERGENCE_PENDING = 'convergence_pending';

/**
 * Human scope phrase for prose ("…is deferred for <noun>").
 *
 * Decision #6's rider requires every panel to state its scope and every Dismiss
 * to name it, so this is used in headers, empty states AND confirmations rather
 * than each site inventing wording.
 */
export function scopeNoun(view: Pick<DeferralLedgerView, 'scope' | 'scope_label'>): string {
  return view.scope === 'orchestrator_root'
    ? 'the orchestrator root'
    : `project “${view.scope_label}”`;
}

/** Panel heading, scope-explicit by construction. */
export function panelTitle(view: Pick<DeferralLedgerView, 'scope' | 'scope_label'>): string {
  return view.scope === 'orchestrator_root'
    ? 'Pending actions — orchestrator root'
    : `Pending actions — ${view.scope_label}`;
}

/**
 * The two render groups.
 *
 * "Action needed" is the ACTIONABLE partition (`action_required` +
 * `auto_retryable`) that the Rust side already computed per entry; everything
 * else is a record or a by-design skip and collapses. Severity is deliberately
 * NOT part of the split: conflating the two is the exact bug WP-B fixed — a
 * completed repair record and genuinely pending work both rendered `info`.
 *
 * This is the GROUP, not the badge: `badgeCount` counts `action_required`
 * alone. Membership and nagging are two questions, and `actionGroupNote` says
 * so on the surface rather than leaving the gap to look like a bug.
 */
export function groupEntries(view: DeferralLedgerView | null): {
  actionNeeded: LedgerEntry[];
  records: LedgerEntry[];
} {
  const entries = view?.entries ?? [];
  return {
    actionNeeded: entries.filter((e) => e.actionable),
    records: entries.filter((e) => !e.actionable),
  };
}

/**
 * The badge number for the surface this view is mounted on — and ONLY that
 * surface. A per-project panel never counts root entries and vice versa; the
 * separation is enforced upstream (two commands, two folders) and this function
 * simply never aggregates.
 *
 * WHAT it counts is deliberately narrower than what the "Action needed" group
 * RENDERS: `action_required` only (user decision 2026-08-27). An
 * `auto_retryable` condition is work VCO is already retrying by itself, so
 * badging it makes chrome nag about something the reader cannot usefully act
 * on. Those entries stay in the open group — visible, with their "VCO retries
 * this itself" line and their retry trail — they just do not drive the number.
 *
 * The entry's `disposition` arrives already resolved by the backend (explicit
 * sidecar field → registry → the conservative `action_required` default), so an
 * unregistered condition still counts here, which is the point of that default.
 */
export function badgeCount(view: DeferralLedgerView | null): number {
  return (view?.entries ?? []).filter((e) => e.disposition === 'action_required')
    .length;
}

/**
 * The part of the "Action needed" group the badge deliberately does not count:
 * conditions VCO retries itself.
 */
export function retryingCount(view: DeferralLedgerView | null): number {
  return groupEntries(view).actionNeeded.filter(
    (e) => e.disposition === 'auto_retryable',
  ).length;
}

/**
 * The honest note under the "Action needed" heading, or `null` when the group
 * and the badge happen to agree. Without it a group of 3 under a badge of 1
 * reads as a counting bug rather than as the intended tiering.
 */
export function actionGroupNote(view: DeferralLedgerView | null): string | null {
  const n = retryingCount(view);
  if (n === 0) return null;
  return n === 1
    ? '1 of these is a condition VCO retries itself — shown here, not counted in the badge.'
    : `${n} of these are conditions VCO retries itself — shown here, not counted in the badge.`;
}

/**
 * The reason a mount can never load, or `null` when it can.
 *
 * A `scope="project"` panel needs a project id: without one there is no folder
 * to read, `load()` has nothing to call, and the component would otherwise sit
 * on "Loading…" forever. No shipped mount does this (`SettingsTab` always
 * passes the id), so this is insurance against a future one — an explicit
 * sentence instead of an indefinite spinner.
 */
export function mountConfigError(
  scope: LedgerScope,
  projectId: string | null | undefined,
): string | null {
  if (scope === 'project' && !projectId) {
    return (
      'This panel was mounted for a project but no project was given, so ' +
      'there is no ledger folder to read. This is a wiring bug — the ' +
      'orchestrator root’s ledger lives under Preferences → Updates.'
    );
  }
  return null;
}

/** Short tier label for the chip. */
export function dispositionLabel(disposition: string): string {
  switch (disposition) {
    case 'action_required':
      return 'action needed';
    case 'auto_retryable':
      return 'VCO retries this';
    case 'environmental':
      return 'environment';
    case 'informational_record':
      return 'record';
    default:
      return disposition || 'unclassified';
  }
}

/** One line explaining what the tier means for the reader. */
export function dispositionExplanation(entry: LedgerEntry): string {
  if (entry.disposition_source === 'default') {
    return (
      'This condition is not in the deferral registry, so VCO is being ' +
      'conservative and treating it as work you need to look at.'
    );
  }
  switch (entry.disposition) {
    case 'action_required':
      return 'Run the command below (or dismiss it if it no longer applies).';
    case 'auto_retryable':
      return 'VCO retries this itself when the thing it needs comes back up.';
    case 'environmental':
      return 'A fact about this machine, not a task. Nothing to run.';
    case 'informational_record':
      return 'A record of something VCO already did. No action needed.';
    default:
      return '';
  }
}

/**
 * Retry sentence for one entry, or `null` when nothing has ever been tried.
 *
 * `inconclusive` is spelled out separately from `failed` on purpose: the driver
 * writes it when a handler ran, exited 0, and the condition is STILL in the
 * ledger. "Ran, unproven" and "failed" are different facts and the panel must
 * not merge them into a confident one.
 */
export function retryLine(retries: RetrySummary | null | undefined): string | null {
  if (!retries || retries.attempts === 0) return null;
  const times = retries.attempts === 1 ? 'once' : `${retries.attempts} times`;
  const parts: string[] = [];
  if (retries.succeeded > 0) parts.push(`${retries.succeeded} succeeded`);
  if (retries.failed > 0) parts.push(`${retries.failed} failed`);
  if (retries.inconclusive > 0) {
    parts.push(
      `${retries.inconclusive} inconclusive (ran, nothing proven)`,
    );
  }
  if (retries.skipped > 0) parts.push(`${retries.skipped} skipped`);
  const detail = parts.length ? ` — ${parts.join(', ')}` : '';
  const cap = retries.cap_reached
    ? ` VCO has stopped retrying (attempt cap ${retries.cap} reached).`
    : '';
  return `VCO retried this ${times}${detail}.${cap}`;
}

/**
 * Confirmation text for a Dismiss. Names the ENTRY and the SCOPE (decision #6
 * rider) plus the folder it will touch, so a dismissal can never be mistaken
 * for acting on the other ledger.
 */
export function dismissConfirmMessage(
  entry: Pick<LedgerEntry, 'condition_id' | 'title'>,
  view: Pick<DeferralLedgerView, 'scope' | 'scope_label' | 'folder'>,
): string {
  return (
    `Dismiss “${entry.title}” (${entry.condition_id}) for ` +
    `${scopeNoun(view)}?\n\n` +
    `This removes the entry from ${view.folder}. It comes back if VCO ` +
    `detects the same condition again.`
  );
}

/** Toast text after a dismissal — scope-named, and honest about a no-op. */
export function dismissResultMessage(outcome: DismissOutcome): string {
  const where =
    outcome.scope === 'orchestrator_root'
      ? ROOT_SCOPE_LABEL
      : outcome.scope_label;
  if (!outcome.dismissed) {
    return (
      `Nothing to dismiss: ${outcome.condition_id} is no longer in ` +
      `${where}'s ledger (${outcome.reason}).`
    );
  }
  const left =
    outcome.remaining === 1 ? '1 entry remains' : `${outcome.remaining} entries remain`;
  return `Dismissed ${outcome.condition_id} for ${where} — ${left}.`;
}

/**
 * A notice when the ledger could not be read, or `null` when it could.
 *
 * `absent` is the HEALTHY case (nothing deferred) and produces no notice;
 * `unavailable` means present-but-unreadable and must never render as "all
 * clear".
 */
export function sourceNotice(view: DeferralLedgerView | null): string | null {
  if (!view) return null;
  if (view.source === 'unavailable') {
    return (
      `VCO could not read this ledger, so the list below may be incomplete. ` +
      `The file is at ${view.folder}/.claude/context/UPDATE_DEFERRED.json.`
    );
  }
  return null;
}

/** Empty-state prose — scope-explicit, so two panels never read alike. */
export function emptyStateMessage(view: DeferralLedgerView | null): string {
  if (!view) return 'Loading…';
  if (view.source === 'unavailable') return 'Ledger unreadable — see the notice above.';
  return `Nothing deferred for ${scopeNoun(view)}.`;
}

/** Find one entry by condition id (used by the MCP page's cross-link). */
export function findEntry(
  view: DeferralLedgerView | null,
  conditionId: string,
): LedgerEntry | null {
  return view?.entries.find((e) => e.condition_id === conditionId) ?? null;
}
