// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 WP-I — tests for the deferral-ledger panel's rendering decisions.
//
// Fixtures mirror the Rust `DeferralLedgerView` wire shape exactly (see
// `launcher/src-tauri/src/commands/deferral_ledger.rs`); the Rust unit tests
// cover the parse/resolve half, these cover grouping, badge scoping, the retry
// sentence, and the scope-naming the decision-#6 UX rider requires.

import { describe, it, expect } from 'vitest';
import {
  actionGroupNote,
  badgeCount,
  dismissConfirmMessage,
  dismissResultMessage,
  dispositionExplanation,
  dispositionLabel,
  emptyStateMessage,
  findEntry,
  groupEntries,
  mountConfigError,
  panelTitle,
  retryingCount,
  retryLine,
  ROOT_SCOPE_LABEL,
  scopeNoun,
  sourceNotice,
  type DeferralLedgerView,
  type LedgerEntry,
  type RetrySummary,
} from './deferral-ledger';

function retries(partial: Partial<RetrySummary> = {}): RetrySummary {
  return {
    attempts: 0,
    cap: 3,
    cap_reached: false,
    succeeded: 0,
    failed: 0,
    inconclusive: 0,
    skipped: 0,
    outcomes: [],
    ...partial,
  };
}

function entry(partial: Partial<LedgerEntry> & { condition_id: string }): LedgerEntry {
  const disposition = partial.disposition ?? 'action_required';
  return {
    title: `T ${partial.condition_id}`,
    detected: 'D',
    why_deferred: 'W',
    command_to_apply: 'cmd',
    severity: 'warning',
    detected_at: '2026-08-27T00:00:00Z',
    kg_node_refs: [],
    disposition,
    disposition_source: 'registry',
    actionable: disposition === 'action_required' || disposition === 'auto_retryable',
    auto_retryable: disposition === 'auto_retryable',
    retries: retries(),
    ...partial,
  };
}

function view(partial: Partial<DeferralLedgerView> = {}): DeferralLedgerView {
  const entries = partial.entries ?? [];
  const actionable = entries.filter((e) => e.actionable).length;
  return {
    scope: 'project',
    scope_label: 'My Project',
    folder: '/home/u/proj',
    source: 'sidecar',
    actionable_count: actionable,
    action_required_count: entries.filter((e) => e.disposition === 'action_required')
      .length,
    record_count: entries.length - actionable,
    warnings: [],
    ...partial,
    entries,
  };
}

describe('groupEntries', () => {
  it('splits actionable work from records, ignoring severity', () => {
    // Both entries are `info` severity — the ONE thing that must not decide
    // the split (the conflation WP-B fixed).
    const v = view({
      entries: [
        entry({ condition_id: 'a', disposition: 'action_required', severity: 'info' }),
        entry({
          condition_id: 'b',
          disposition: 'informational_record',
          severity: 'info',
        }),
        entry({ condition_id: 'c', disposition: 'auto_retryable', severity: 'info' }),
        entry({ condition_id: 'd', disposition: 'environmental', severity: 'critical' }),
      ],
    });
    const { actionNeeded, records } = groupEntries(v);
    expect(actionNeeded.map((e) => e.condition_id)).toEqual(['a', 'c']);
    expect(records.map((e) => e.condition_id)).toEqual(['b', 'd']);
  });

  it('handles a null view without throwing', () => {
    expect(groupEntries(null)).toEqual({ actionNeeded: [], records: [] });
  });
});

describe('badgeCount', () => {
  // USER DECISION 2026-08-27: the badge counts `action_required` ONLY. An
  // `auto_retryable` condition stays in the "Action needed" GROUP (VCO is
  // already retrying it) but must never drive the number.
  it('counts action_required ONLY — not the whole actionable partition', () => {
    const v = view({
      entries: [
        entry({ condition_id: 'a', disposition: 'action_required' }),
        entry({ condition_id: 'b', disposition: 'auto_retryable' }),
        entry({ condition_id: 'c', disposition: 'informational_record' }),
      ],
    });
    expect(badgeCount(v)).toBe(1);
    // The FE derivation and the Rust-computed BADGE field must never disagree.
    // (`actionable_count` is the wider partition and is deliberately larger.)
    expect(badgeCount(v)).toBe(v.action_required_count);
    expect(v.actionable_count).toBe(2);
    // The group still renders both — membership and nagging are two questions.
    expect(groupEntries(v).actionNeeded.map((e) => e.condition_id)).toEqual(['a', 'b']);
    expect(retryingCount(v)).toBe(1);
  });

  it('does not badge a ledger whose only open work is auto_retryable', () => {
    const v = view({
      entries: [
        entry({ condition_id: 'k', disposition: 'auto_retryable' }),
        entry({ condition_id: 'r', disposition: 'informational_record' }),
      ],
    });
    expect(badgeCount(v)).toBe(0);
    // …but the entry is still visible, so nothing is hidden by not badging it.
    expect(groupEntries(v).actionNeeded).toHaveLength(1);
    expect(actionGroupNote(v)).toContain('VCO retries itself');
    expect(actionGroupNote(v)).toContain('not counted in the badge');
  });

  it('counts an UNREGISTERED condition, which the backend resolves to action_required', () => {
    const v = view({
      entries: [
        entry({
          condition_id: 'unknown_thing',
          disposition: 'action_required',
          disposition_source: 'default',
        }),
      ],
    });
    expect(badgeCount(v)).toBe(1);
  });

  it('adds no note when the group and the badge already agree', () => {
    const v = view({
      entries: [entry({ condition_id: 'a', disposition: 'action_required' })],
    });
    expect(badgeCount(v)).toBe(groupEntries(v).actionNeeded.length);
    expect(actionGroupNote(v)).toBeNull();
    expect(retryingCount(v)).toBe(0);
  });

  it('is zero for a records-only ledger (a badge must not nag about records)', () => {
    const v = view({
      entries: [entry({ condition_id: 'r', disposition: 'informational_record' })],
    });
    expect(badgeCount(v)).toBe(0);
  });

  it('counts ONLY the surface it is given — two scopes never aggregate', () => {
    const project = view({
      scope: 'project',
      entries: [entry({ condition_id: 'p1' }), entry({ condition_id: 'p2' })],
    });
    const root = view({
      scope: 'orchestrator_root',
      scope_label: ROOT_SCOPE_LABEL,
      folder: '/opt/vco',
      entries: [entry({ condition_id: 'r1' })],
    });
    expect(badgeCount(project)).toBe(2);
    expect(badgeCount(root)).toBe(1);
    // No helper exists that adds them — the separation is structural.
    expect(badgeCount(project)).not.toBe(badgeCount(root));
  });

  it('is zero for a null view', () => {
    expect(badgeCount(null)).toBe(0);
    expect(retryingCount(null)).toBe(0);
    expect(actionGroupNote(null)).toBeNull();
  });
});

describe('mountConfigError (a panel that could never load says so)', () => {
  it('names the wiring bug for a project mount with no project id', () => {
    const msg = mountConfigError('project', undefined)!;
    expect(msg).toContain('no project was given');
    expect(msg).toContain('Preferences → Updates');
    expect(mountConfigError('project', '')).toBe(msg);
  });

  it('leaves every loadable mount alone', () => {
    expect(mountConfigError('project', 'proj-1')).toBeNull();
    expect(mountConfigError('orchestrator_root', undefined)).toBeNull();
    expect(mountConfigError('orchestrator_root', 'ignored')).toBeNull();
  });
});

describe('scope naming (decision #6 rider)', () => {
  it('names the project for a project ledger', () => {
    const v = view({ scope: 'project', scope_label: 'WidgetApp' });
    expect(scopeNoun(v)).toContain('WidgetApp');
    expect(panelTitle(v)).toBe('Pending actions — WidgetApp');
  });

  it('names the orchestrator root for the global ledger', () => {
    const v = view({ scope: 'orchestrator_root', scope_label: ROOT_SCOPE_LABEL });
    expect(scopeNoun(v)).toBe('the orchestrator root');
    expect(panelTitle(v)).toBe('Pending actions — orchestrator root');
  });

  it('gives the two scopes DIFFERENT headings and empty states', () => {
    const p = view({ scope: 'project', scope_label: 'WidgetApp', entries: [] });
    const r = view({
      scope: 'orchestrator_root',
      scope_label: ROOT_SCOPE_LABEL,
      entries: [],
    });
    expect(panelTitle(p)).not.toBe(panelTitle(r));
    expect(emptyStateMessage(p)).not.toBe(emptyStateMessage(r));
    expect(emptyStateMessage(p)).toContain('WidgetApp');
    expect(emptyStateMessage(r)).toContain('orchestrator root');
  });
});

describe('dismiss messaging', () => {
  it('names the entry AND the scope AND the folder it will touch', () => {
    const v = view({ scope: 'project', scope_label: 'WidgetApp', folder: '/w/widgetapp' });
    const msg = dismissConfirmMessage(
      { condition_id: 'template_review_pending', title: 'Templates changed' },
      v,
    );
    expect(msg).toContain('template_review_pending');
    expect(msg).toContain('Templates changed');
    expect(msg).toContain('WidgetApp');
    expect(msg).toContain('/w/widgetapp');
  });

  it('names the ROOT scope when dismissing a global entry', () => {
    const v = view({
      scope: 'orchestrator_root',
      scope_label: ROOT_SCOPE_LABEL,
      folder: '/opt/vco',
    });
    const msg = dismissConfirmMessage(
      { condition_id: 'convergence_pending', title: 'Convergence' },
      v,
    );
    expect(msg).toContain('the orchestrator root');
    expect(msg).toContain('/opt/vco');
    expect(msg).not.toContain('My Project');
  });

  it('reports a real dismissal with the remaining count', () => {
    expect(
      dismissResultMessage({
        condition_id: 'x',
        scope: 'project',
        scope_label: 'WidgetApp',
        folder: '/w/widgetapp',
        dismissed: true,
        remaining: 2,
        reason: 'dismissed',
      }),
    ).toBe('Dismissed x for WidgetApp — 2 entries remain.');
  });

  it('is honest when the entry was already gone (idempotent no-op)', () => {
    const msg = dismissResultMessage({
      condition_id: 'x',
      scope: 'orchestrator_root',
      scope_label: ROOT_SCOPE_LABEL,
      folder: '/opt/vco',
      dismissed: false,
      remaining: 0,
      reason: 'no_match',
    });
    expect(msg).toContain('Nothing to dismiss');
    expect(msg).toContain('no_match');
    expect(msg).not.toContain('Dismissed x');
  });
});

describe('retryLine', () => {
  it('is null when nothing was ever tried (leave-alone: no retry UI)', () => {
    expect(retryLine(retries())).toBeNull();
    expect(retryLine(null)).toBeNull();
    expect(retryLine(undefined)).toBeNull();
  });

  it('reports inconclusive attempts as their OWN state, not as failures', () => {
    const line = retryLine(retries({ attempts: 2, inconclusive: 2 }))!;
    expect(line).toContain('2 times');
    expect(line).toContain('2 inconclusive');
    expect(line).toContain('ran, nothing proven');
    expect(line).not.toContain('failed');
  });

  it('distinguishes failed from inconclusive in the same sentence', () => {
    const line = retryLine(retries({ attempts: 2, failed: 1, inconclusive: 1 }))!;
    expect(line).toContain('1 failed');
    expect(line).toContain('1 inconclusive');
  });

  it('says the cap is reached only once it actually is', () => {
    expect(retryLine(retries({ attempts: 2, cap: 3, failed: 2 }))!).not.toContain(
      'stopped retrying',
    );
    const capped = retryLine(
      retries({ attempts: 3, cap: 3, cap_reached: true, failed: 3 }),
    )!;
    expect(capped).toContain('stopped retrying');
    expect(capped).toContain('attempt cap 3');
  });

  it('uses singular prose for a single attempt', () => {
    expect(retryLine(retries({ attempts: 1, skipped: 1 }))!).toContain('once');
  });
});

describe('disposition rendering', () => {
  it('labels every shipped tier distinctly', () => {
    const labels = [
      'action_required',
      'auto_retryable',
      'environmental',
      'informational_record',
    ].map(dispositionLabel);
    expect(new Set(labels).size).toBe(4);
  });

  it('tells an auto_retryable entry that VCO handles it', () => {
    const e = entry({ condition_id: 'k', disposition: 'auto_retryable' });
    expect(dispositionExplanation(e)).toContain('VCO retries this itself');
  });

  it('says a record needs nothing', () => {
    const e = entry({ condition_id: 'r', disposition: 'informational_record' });
    expect(dispositionExplanation(e)).toContain('No action needed');
  });

  it('flags an UNREGISTERED condition as a conservative default, not a verdict', () => {
    const e = entry({
      condition_id: 'unknown_thing',
      disposition: 'action_required',
      disposition_source: 'default',
    });
    expect(dispositionExplanation(e)).toContain('not in the deferral registry');
    expect(dispositionExplanation(e)).toContain('conservative');
  });
});

describe('sourceNotice', () => {
  it('says nothing for a healthy read', () => {
    expect(sourceNotice(view({ source: 'sidecar' }))).toBeNull();
  });

  it('says nothing for an ABSENT ledger — that is the all-clear case', () => {
    expect(sourceNotice(view({ source: 'absent' }))).toBeNull();
  });

  it('warns loudly for an unreadable ledger — never renders as all-clear', () => {
    const notice = sourceNotice(view({ source: 'unavailable' }))!;
    expect(notice).toContain('could not read');
    expect(notice).toContain('UPDATE_DEFERRED.json');
    expect(emptyStateMessage(view({ source: 'unavailable' }))).toContain('unreadable');
  });
});

describe('findEntry', () => {
  it('finds a condition by id and returns null when absent', () => {
    const v = view({ entries: [entry({ condition_id: 'convergence_pending' })] });
    expect(findEntry(v, 'convergence_pending')?.condition_id).toBe(
      'convergence_pending',
    );
    expect(findEntry(v, 'nope')).toBeNull();
    expect(findEntry(null, 'convergence_pending')).toBeNull();
  });
});
