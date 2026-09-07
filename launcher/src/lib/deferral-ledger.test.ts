// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 WP-I — tests for the deferral-ledger panel's rendering decisions.
//
// Fixtures mirror the Rust `DeferralLedgerView` wire shape exactly (see
// `launcher/src-tauri/src/commands/deferral_ledger.rs`); the Rust unit tests
// cover the parse/resolve half, these cover grouping, badge scoping, the retry
// sentence, and the scope-naming the decision-#6 UX rider requires.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  unbackedRetryableCount,
  actionGroupNote,
  AUTO_RETRY_BACKED_CONDITIONS,
  autoRetryIsBacked,
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
        // A REAL backed cid: `retryingCount` counts only rows a mechanism
        // stands behind, so an arbitrary id here would assert the
        // over-claim rather than the behaviour (v0.2.92 MAJOR-14).
        entry({ condition_id: 'kg_sync_no_embedding_backend', disposition: 'auto_retryable' }),
        entry({ condition_id: 'c', disposition: 'informational_record' }),
      ],
    });
    expect(badgeCount(v)).toBe(1);
    // The FE derivation and the Rust-computed BADGE field must never disagree.
    // (`actionable_count` is the wider partition and is deliberately larger.)
    expect(badgeCount(v)).toBe(v.action_required_count);
    expect(v.actionable_count).toBe(2);
    // The group still renders both — membership and nagging are two questions.
    expect(groupEntries(v).actionNeeded.map((e) => e.condition_id)).toEqual([
      'a',
      'kg_sync_no_embedding_backend',
    ]);
    expect(retryingCount(v)).toBe(1);
  });

  it('does not badge a ledger whose only open work is auto_retryable', () => {
    const v = view({
      entries: [
        entry({ condition_id: 'kg_sync_no_embedding_backend', disposition: 'auto_retryable' }),
        entry({ condition_id: 'r', disposition: 'informational_record' }),
      ],
    });
    expect(badgeCount(v)).toBe(0);
    // …but the entry is still visible, so nothing is hidden by not badging it.
    expect(groupEntries(v).actionNeeded).toHaveLength(1);
    expect(actionGroupNote(v)).toContain('VCO retries itself');
    expect(actionGroupNote(v)).toContain('not counted in the badge');
  });

  it('does NOT claim a retry for an auto_retryable row nothing retries', () => {
    // `podman_daemon_start_failed` is classed auto_retryable in the registry
    // but declares no retry_action. Saying "VCO retries this itself" about it
    // is a promise with no mechanism — the defect MAJOR-14 was raised for.
    const v = view({
      entries: [
        entry({ condition_id: 'podman_daemon_start_failed', disposition: 'auto_retryable' }),
      ],
    });
    expect(retryingCount(v)).toBe(0);
    expect(unbackedRetryableCount(v)).toBe(1);
    const note = actionGroupNote(v);
    expect(note).not.toContain('VCO retries itself');
    expect(note).toContain('no automatic retry');
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

  it('tells an auto_retryable entry with a real retry handler that VCO handles it', () => {
    const e = entry({
      // A cid the registry gives `retry_action = "retry:py:kg_seed"` — the
      // claim below is TRUE for it. (Pre-v0.2.92 this test used a made-up id,
      // which is how the unconditional claim passed review: the fixture had
      // no relationship to whether a mechanism existed.)
      condition_id: 'kg_sync_no_embedding_backend',
      disposition: 'auto_retryable',
    });
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

// ── v0.2.92 review MAJOR-14 — never claim a retry that has no mechanism ────
//
// `auto_retryable` is a CLASSIFICATION. The panel used to read it as an
// implementation and told every such row "VCO retries this itself", including
// four registry rows with no `retry_action` and nothing scheduled behind them
// (`kg_summary_no_backend`, `podman_daemon_start_failed`,
// `weaviate_unreachable_at_bootstrap` — whose clear_probe is `manual-dismiss`,
// i.e. it says outright that only a human clears it — and
// `weaviate_unreachable_at_update`). The Python ledger renders retry history
// honestly; this makes the GUI match.
describe('auto_retryable honesty (review MAJOR-14)', () => {
  it('claims a retry only when a mechanism exists', () => {
    const backed = entry({
      condition_id: 'code_graph_no_embedding_backend',
      disposition: 'auto_retryable',
    });
    expect(autoRetryIsBacked(backed)).toBe(true);
    expect(dispositionExplanation(backed)).toContain('VCO retries this itself');
  });

  it('does NOT claim a retry for an auto_retryable row with no mechanism', () => {
    // THE DEFECT. Every one of these is classed `auto_retryable` and none has
    // a `retry_action`; the reader was told to sit and wait for a retry that
    // no code performs.
    for (const cid of [
      'kg_summary_no_backend',
      'podman_daemon_start_failed',
      'weaviate_unreachable_at_bootstrap',
      'weaviate_unreachable_at_update',
    ]) {
      const e = entry({ condition_id: cid, disposition: 'auto_retryable' });
      expect(autoRetryIsBacked(e)).toBe(false);
      const text = dispositionExplanation(e);
      expect(text).not.toContain('VCO retries this itself');
      expect(text).toContain('no automatic retry');
      // Still honest about how it DOES end, so the arm is not a dead end.
      expect(text).toMatch(/dismiss/i);
    }
  });

  it('an unregistered auto_retryable cid gets the cautious arm, not the promise', () => {
    // Direction of the default matters: a new row that ships without reaching
    // this list understates rather than lying, and the pin below turns the
    // omission into a failing test.
    const e = entry({ condition_id: 'some_future_cid', disposition: 'auto_retryable' });
    expect(autoRetryIsBacked(e)).toBe(false);
    expect(dispositionExplanation(e)).toContain('no automatic retry');
  });

  it('the question does not apply to other dispositions', () => {
    for (const d of ['action_required', 'environmental', 'informational_record']) {
      // Even for a cid that IS in the backed set — the disposition governs.
      const e = entry({ condition_id: 'kg_sync_no_embedding_backend', disposition: d });
      expect(autoRetryIsBacked(e)).toBe(false);
    }
  });
});

// The shipped list above is only honest while it matches the registry. This
// pins it to `vco_lib/deferral_conditions.toml` — READ-ONLY ground truth,
// owned elsewhere — so a row that gains or loses a `retry_action` fails here
// instead of silently changing what the GUI promises.
describe('AUTO_RETRY_BACKED_CONDITIONS is pinned to the deferral registry', () => {
  /** launcher/src/lib → launcher/src → launcher → repo root. */
  const REGISTRY = fileURLToPath(
    new URL('../../../vco_lib/deferral_conditions.toml', import.meta.url),
  );

  interface RegistryRow {
    cid: string;
    klass: string | null;
    retryAction: string | null;
  }

  /**
   * Minimal TOML read: `[conditions.<id>]` tables and the two scalar keys we
   * care about. Triple-quoted `notes` are stripped FIRST so their prose (which
   * discusses `retry_action` at length) can never be mistaken for a key.
   */
  function readRegistry(): RegistryRow[] {
    const raw = readFileSync(REGISTRY, 'utf-8').replace(/"""[\s\S]*?"""/g, '""');
    const lines = raw.split('\n').filter((l) => !/^\s*#/.test(l));
    const rows: RegistryRow[] = [];
    let cur: RegistryRow | null = null;
    for (const line of lines) {
      const header = /^\[conditions\.(?:"([^"]+)"|([^\]"]+))\]/.exec(line);
      if (header) {
        cur = { cid: header[1] ?? header[2], klass: null, retryAction: null };
        rows.push(cur);
        continue;
      }
      if (/^\[/.test(line)) {
        cur = null;
        continue;
      }
      if (!cur) continue;
      const klass = /^\s*class\s*=\s*"([^"]*)"/.exec(line);
      if (klass) cur.klass = klass[1];
      const retry = /^\s*retry_action\s*=\s*"([^"]*)"/.exec(line);
      if (retry) cur.retryAction = retry[1];
    }
    return rows;
  }

  const rows = readRegistry();
  const byCid = new Map(rows.map((r) => [r.cid, r]));
  const autoRetryable = rows.filter((r) => r.klass === 'auto_retryable');

  it('parsed a plausible registry (guards the reader itself)', () => {
    expect(rows.length).toBeGreaterThan(100);
    expect(autoRetryable.length).toBeGreaterThanOrEqual(5);
    expect(autoRetryable.filter((r) => r.retryAction).length).toBeGreaterThanOrEqual(4);
  });

  it('every row with a retry_action is claimed as backed', () => {
    const withHandler = autoRetryable.filter((r) => r.retryAction).map((r) => r.cid);
    const unclaimed = withHandler.filter((c) => !AUTO_RETRY_BACKED_CONDITIONS.has(c));
    expect(unclaimed).toEqual([]);
  });

  it('the only backed row WITHOUT a retry_action is the documented exception', () => {
    // `project_move_codegraph_reanalyze_pending` declares none on purpose: the
    // launcher's build runner already consumes the pending row it enqueued,
    // and a WP-H handler would be a second scheduler racing it.
    const noHandler = [...AUTO_RETRY_BACKED_CONDITIONS].filter(
      (c) => !byCid.get(c)?.retryAction,
    );
    expect(noHandler).toEqual(['project_move_codegraph_reanalyze_pending']);
  });

  it('every claimed cid exists in the registry and is classed auto_retryable', () => {
    for (const cid of AUTO_RETRY_BACKED_CONDITIONS) {
      expect(byCid.get(cid)?.klass).toBe('auto_retryable');
    }
  });

  it('the rows the review named are still unbacked in the registry', () => {
    for (const cid of [
      'kg_summary_no_backend',
      'podman_daemon_start_failed',
      'weaviate_unreachable_at_bootstrap',
      'weaviate_unreachable_at_update',
    ]) {
      expect(byCid.get(cid)?.klass).toBe('auto_retryable');
      expect(byCid.get(cid)?.retryAction).toBeNull();
      expect(AUTO_RETRY_BACKED_CONDITIONS.has(cid)).toBe(false);
    }
  });
});
