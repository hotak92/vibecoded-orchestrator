// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-D GUI half — pure-logic tests for the per-project bundle
// staleness chip and the post-orchestrator-update count.
//
// The governing thesis of the release, pinned here: a check that cannot
// distinguish "I could not determine this" from "this is fine" is not a
// check. Every assertion below exists to stop `unknown` collapsing into
// either `current` or `stale`, and to stop a FAILED census rendering as
// "all current" / "0 stale".

import { describe, it, expect, vi } from 'vitest';
import {
  chipFor,
  indexCensus,
  summaryLine,
  needsAttentionCount,
  undeterminedCensus,
  shouldRecensusOnUpdaterEdge,
  createCensusController,
  type CensusView,
  CENSUS_UNAVAILABLE_REASON,
  NOT_IN_CENSUS_REASON,
} from '$lib/bundle-staleness';
import type { BundleStalenessCensus } from '$lib/types/launcher';

/** A determined census carrying exactly one of each verdict. */
function threeVerdictCensus(): BundleStalenessCensus {
  return {
    determined: true,
    error: null,
    registry: 'launcher.db',
    running_version: '0.2.92',
    projects: [
      {
        id: 'p-current',
        name: 'Fresh',
        folder: '/tmp/fresh',
        verdict: 'current',
        reason: 'noop',
        changed_files: [],
        user_modified: 0,
      },
      {
        id: 'p-stale',
        name: 'Old',
        folder: '/tmp/old',
        verdict: 'stale',
        reason: 'files_changed',
        changed_files: ['.claude/hooks/a.sh', '.claude/agents/b.md'],
        user_modified: 1,
      },
      {
        id: 'p-unknown',
        name: 'Moved',
        folder: '/tmp/moved',
        verdict: 'unknown',
        reason: 'folder_missing',
        changed_files: [],
        user_modified: 0,
      },
    ],
    summary: { current: 1, stale: 1, unknown: 1 },
    remedy_gui: 'Projects → Update all bundles',
    remedy_cli: 'python -m vco_lib.project_init install-bundle --folder <project-folder> --update --json',
  };
}

/** What the Tauri command returns when the census could not run at all. */
function failedCensus(error: string): BundleStalenessCensus {
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

describe('chipFor — the three verdicts render distinctly', () => {
  it('renders current / stale / unknown with three distinct verdicts, tones and labels', () => {
    const census = threeVerdictCensus();
    const cur = chipFor(census, 'p-current');
    const stale = chipFor(census, 'p-stale');
    const unk = chipFor(census, 'p-unknown');

    expect(cur.verdict).toBe('current');
    expect(stale.verdict).toBe('stale');
    expect(unk.verdict).toBe('unknown');

    // Distinct tones — the CSS class hook must not collapse two states.
    const tones = new Set([cur.tone, stale.tone, unk.tone]);
    expect(tones.size).toBe(3);

    // Distinct user-visible labels.
    const labels = new Set([cur.label, stale.label, unk.label]);
    expect(labels.size).toBe(3);
  });

  it('never labels an unknown project as current or stale', () => {
    const unk = chipFor(threeVerdictCensus(), 'p-unknown');
    expect(unk.verdict).not.toBe('current');
    expect(unk.verdict).not.toBe('stale');
    expect(unk.label.toLowerCase()).not.toContain('current');
    expect(unk.label.toLowerCase()).not.toContain('stale');
    expect(unk.label.toLowerCase()).toContain('unknown');
  });

  it('carries the per-project reason on the unknown chip', () => {
    const unk = chipFor(threeVerdictCensus(), 'p-unknown');
    expect(unk.reason).toBe('folder_missing');
    // The detail sentence must name the cause, not be a generic shrug.
    expect(unk.detail.toLowerCase()).toContain('folder');
  });

  it('names the changed-file count on a stale chip', () => {
    const stale = chipFor(threeVerdictCensus(), 'p-stale');
    expect(stale.detail).toContain('2');
  });
});

describe('chipFor — a failed census is NOT "current"', () => {
  it('renders unknown (not current) for every project when the census could not run', () => {
    const chip = chipFor(failedCensus('no vco_lib-capable python interpreter resolved'), 'p-current');
    expect(chip.verdict).toBe('unknown');
    expect(chip.reason).toBe(CENSUS_UNAVAILABLE_REASON);
  });

  it('renders unknown (not current) when the census payload is absent entirely', () => {
    const chip = chipFor(null, 'anything');
    expect(chip.verdict).toBe('unknown');
    expect(chip.reason).toBe(CENSUS_UNAVAILABLE_REASON);
  });

  it('renders unknown for a project the determined census did not cover', () => {
    const chip = chipFor(threeVerdictCensus(), 'p-never-registered');
    expect(chip.verdict).toBe('unknown');
    expect(chip.reason).toBe(NOT_IN_CENSUS_REASON);
  });
});

describe('summaryLine — could-not-determine is distinguishable from all-current', () => {
  it('says "could not determine" and NOT "all current" on a failed census', () => {
    const line = summaryLine(failedCensus('census exited 2: orchestrator root not found'));
    expect(line.toLowerCase()).toContain('could not determine');
    expect(line.toLowerCase()).not.toContain('up to date');
    expect(line).not.toContain('0');
  });

  it('says "could not determine" when the census payload is absent', () => {
    expect(summaryLine(null).toLowerCase()).toContain('could not determine');
  });

  it('reports stale and undetermined populations separately', () => {
    const line = summaryLine(threeVerdictCensus());
    expect(line).toContain('1 stale');
    expect(line).toContain('1 could not be determined');
  });

  it('reports an all-current population as up to date', () => {
    const census = threeVerdictCensus();
    census.projects = census.projects.filter((p) => p.verdict === 'current');
    census.summary = { current: 1, stale: 0, unknown: 0 };
    const line = summaryLine(census);
    expect(line.toLowerCase()).toContain('up to date');
  });

  it('an EMPTY determined census is not a failure', () => {
    const census = failedCensus('unused');
    census.determined = true;
    census.error = null;
    census.registry = 'launcher.db';
    census.summary = { current: 0, stale: 0, unknown: 0 };
    const line = summaryLine(census);
    expect(line.toLowerCase()).not.toContain('could not determine');
  });
});

describe('needsAttentionCount — the count surfaced after an orchestrator update', () => {
  it('matches the census summary (stale + unknown)', () => {
    expect(needsAttentionCount(threeVerdictCensus())).toBe(2);
  });

  it('is null — NOT 0 — when the census could not run', () => {
    expect(needsAttentionCount(failedCensus('boom'))).toBeNull();
    expect(needsAttentionCount(null)).toBeNull();
  });

  it('is 0 for a genuinely all-current population', () => {
    const census = threeVerdictCensus();
    census.summary = { current: 3, stale: 0, unknown: 0 };
    expect(needsAttentionCount(census)).toBe(0);
  });

  it('derives from the summary the backend computed, so the badge count cannot drift from the rows', () => {
    const census = threeVerdictCensus();
    const rows = census.projects.filter((p) => p.verdict !== 'current').length;
    expect(needsAttentionCount(census)).toBe(rows);
  });
});

describe('indexCensus', () => {
  it('indexes rows by project id for a determined census', () => {
    const idx = indexCensus(threeVerdictCensus());
    expect(idx.get('p-stale')?.verdict).toBe('stale');
    expect(idx.size).toBe(3);
  });

  it('is empty for an undetermined census (no row may be treated as known)', () => {
    expect(indexCensus(failedCensus('x')).size).toBe(0);
  });
});

describe('undeterminedCensus — the client-side failure payload', () => {
  it('is undetermined with a null summary, not an empty determined census', () => {
    const c = undeterminedCensus('command bundle_staleness_census not found');
    expect(c.determined).toBe(false);
    expect(c.summary).toBeNull();
    expect(c.error).toContain('not found');
    expect(needsAttentionCount(c)).toBeNull();
    expect(summaryLine(c).toLowerCase()).toContain('could not determine');
    expect(chipFor(c, 'anything').verdict).toBe('unknown');
  });
});

describe('shouldRecensusOnUpdaterEdge — re-census when an update completes', () => {
  it('fires exactly on the true → false edge', () => {
    expect(shouldRecensusOnUpdaterEdge(true, false)).toBe(true);
  });

  it('does not fire while idle (otherwise every reactive tick spawns a census)', () => {
    expect(shouldRecensusOnUpdaterEdge(false, false)).toBe(false);
  });

  it('does not fire while the update is still running', () => {
    expect(shouldRecensusOnUpdaterEdge(false, true)).toBe(false);
    expect(shouldRecensusOnUpdaterEdge(true, true)).toBe(false);
  });
});

// ─── createCensusController — every trigger, one ordering rule ──────────
//
// Owner-reported (v0.2.97): after a successful "Update all" the Projects
// page kept showing "8 stale of 9" because the census was only re-taken on
// mount and on the orchestrator-update edge. Each trigger below is one
// place the page must re-take it; the ordering tests pin that an older
// response never overwrites a newer one.

/** A census where every one of `n` projects has the given verdict. */
function uniformCensus(verdict: 'current' | 'stale', n = 9): BundleStalenessCensus {
  const projects = Array.from({ length: n }, (_, i) => ({
    id: `p${i}`,
    name: `P${i}`,
    folder: `/tmp/p${i}`,
    verdict,
    reason: verdict === 'current' ? 'noop' : 'files_changed',
    changed_files: verdict === 'current' ? [] : ['.claude/hooks/x.sh'],
    user_modified: 0,
  }));
  return {
    determined: true,
    error: null,
    registry: 'launcher.db',
    running_version: '0.2.97',
    projects,
    summary: {
      current: verdict === 'current' ? n : 0,
      stale: verdict === 'stale' ? n : 0,
      unknown: 0,
    },
    remedy_gui: null,
    remedy_cli: null,
  };
}

interface Pending {
  resolve: (c: BundleStalenessCensus) => void;
  reject: (e: unknown) => void;
}

/** A controller whose census calls are held open until the test settles
 *  them, so response ORDER is under the test's control. */
function harness(opts: { enabled?: () => boolean } = {}) {
  const pending: Pending[] = [];
  const views: CensusView[] = [];
  const fetchCensus = vi.fn(
    () =>
      new Promise<BundleStalenessCensus>((resolve, reject) => {
        pending.push({ resolve, reject });
      }),
  );
  const ctl = createCensusController({
    fetchCensus,
    onChange: (v) => views.push(v),
    enabled: opts.enabled,
  });
  return { ctl, fetchCensus, pending, views };
}

/** Let resolved promise continuations run. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

describe('createCensusController — Update all finishing re-takes the census', () => {
  it('done-success: a completed run invokes the census and shows the fresh verdicts', async () => {
    const h = harness();
    const first = h.ctl.load();
    h.pending[0].resolve(uniformCensus('stale', 9));
    await first;
    expect(summaryLine(h.ctl.view().census)).toBe('9 stale of 9 project bundles.');

    const after = h.ctl.updateAllFinished('completed');
    expect(h.fetchCensus).toHaveBeenCalledTimes(2);
    h.pending[1].resolve(uniformCensus('current', 9));
    await after;
    expect(summaryLine(h.ctl.view().census)).toBe('All 9 project bundles are up to date.');
    expect(chipFor(h.ctl.view().census, 'p0').label).toBe('Bundle current');
  });

  it('done-failure: an errored run ALSO invokes the census (a partial run changed bundles)', async () => {
    const h = harness();
    void h.ctl.load();
    h.pending[0].resolve(uniformCensus('stale'));
    await flush();

    void h.ctl.updateAllFinished('errored');
    expect(h.fetchCensus).toHaveBeenCalledTimes(2);
  });

  it('a census call that rejects is shown as UNDETERMINED, never as the previous verdicts', async () => {
    const h = harness();
    void h.ctl.load();
    h.pending[0].resolve(uniformCensus('stale'));
    await flush();

    const after = h.ctl.updateAllFinished('completed');
    h.pending[1].reject(new Error('command bundle_staleness_census not found'));
    await after;
    expect(h.ctl.view().census?.determined).toBe(false);
    expect(needsAttentionCount(h.ctl.view().census)).toBeNull();
  });
});

describe('createCensusController — Refresh re-takes the census', () => {
  it('refresh invokes the census every time it is pressed', () => {
    const h = harness();
    void h.ctl.refresh();
    void h.ctl.refresh();
    expect(h.fetchCensus).toHaveBeenCalledTimes(2);
  });
});

describe('createCensusController — overlapping calls: newest wins', () => {
  it('an OLDER response arriving last does not overwrite the newer one', async () => {
    const h = harness();
    // Request 1 (e.g. a Refresh mid-run) and request 2 (Update all done)
    // are both in flight; request 2 answers first.
    const r1 = h.ctl.refresh();
    const r2 = h.ctl.updateAllFinished('completed');
    h.pending[1].resolve(uniformCensus('current'));
    await r2;
    expect(summaryLine(h.ctl.view().census)).toBe('All 9 project bundles are up to date.');

    h.pending[0].resolve(uniformCensus('stale'));
    await r1;
    expect(summaryLine(h.ctl.view().census)).toBe('All 9 project bundles are up to date.');
    expect(h.ctl.view().checking).toBe(false);
  });

  it('in-order responses both apply, the last one shown', async () => {
    const h = harness();
    const r1 = h.ctl.refresh();
    const r2 = h.ctl.refresh();
    h.pending[0].resolve(uniformCensus('stale'));
    await r1;
    // Request 2 is still out, so the page says the figures may change.
    expect(h.ctl.view().checking).toBe(true);
    expect(needsAttentionCount(h.ctl.view().census)).toBe(9);
    h.pending[1].resolve(uniformCensus('current'));
    await r2;
    expect(needsAttentionCount(h.ctl.view().census)).toBe(0);
    expect(h.ctl.view().checking).toBe(false);
  });

  it('an older REJECTION arriving last does not replace a newer determined census', async () => {
    const h = harness();
    const r1 = h.ctl.refresh();
    const r2 = h.ctl.refresh();
    h.pending[1].resolve(uniformCensus('current'));
    await r2;
    h.pending[0].reject(new Error('late failure'));
    await r1;
    expect(h.ctl.view().census?.determined).toBe(true);
  });

  it('attempted is false until the first response, then stays true', async () => {
    const h = harness();
    const r1 = h.ctl.load();
    expect(h.ctl.view().attempted).toBe(false);
    expect(h.ctl.view().checking).toBe(true);
    h.pending[0].resolve(uniformCensus('current'));
    await r1;
    expect(h.ctl.view().attempted).toBe(true);
  });
});

describe('createCensusController — no Tauri host', () => {
  it('takes no census when disabled', () => {
    const h = harness({ enabled: () => false });
    void h.ctl.load();
    void h.ctl.refresh();
    void h.ctl.updateAllFinished('completed');
    expect(h.fetchCensus).not.toHaveBeenCalled();
    expect(h.ctl.view().checking).toBe(false);
  });
});

describe('createCensusController — orchestrator update edge + the call-out', () => {
  it('fires on the falling edge only and raises the call-out', () => {
    const h = harness();
    h.ctl.updaterTick(false);
    h.ctl.updaterTick(true);
    expect(h.fetchCensus).not.toHaveBeenCalled();
    h.ctl.updaterTick(false);
    expect(h.fetchCensus).toHaveBeenCalledTimes(1);
    expect(h.ctl.view().postUpdateNotice).toBe(true);
    h.ctl.updaterTick(false);
    expect(h.fetchCensus).toHaveBeenCalledTimes(1);
  });

  it('keeps the call-out while bundles are stale; Update all bringing them current clears it', async () => {
    const h = harness();
    h.ctl.updaterTick(true);
    h.ctl.updaterTick(false);
    h.pending[0].resolve(uniformCensus('stale'));
    await flush();
    expect(h.ctl.view().postUpdateNotice).toBe(true);

    const after = h.ctl.updateAllFinished('completed');
    h.pending[1].resolve(uniformCensus('current'));
    await after;
    // "Project bundles are not updated with it" is no longer true.
    expect(h.ctl.view().postUpdateNotice).toBe(false);
  });

  it('an all-current answer to a request started BEFORE the update does not clear the call-out', async () => {
    const h = harness();
    const early = h.ctl.refresh(); // started before the update completed
    h.ctl.updaterTick(true);
    h.ctl.updaterTick(false); // request 2
    h.pending[0].resolve(uniformCensus('current'));
    await early;
    expect(h.ctl.view().postUpdateNotice).toBe(true);
  });

  it('an undetermined census never clears the call-out', async () => {
    const h = harness();
    h.ctl.updaterTick(true);
    h.ctl.updaterTick(false);
    h.pending[0].resolve(failedCensus('no interpreter'));
    await flush();
    expect(h.ctl.view().postUpdateNotice).toBe(true);
  });

  it('dismiss clears the call-out', () => {
    const h = harness();
    h.ctl.updaterTick(true);
    h.ctl.updaterTick(false);
    h.ctl.dismissNotice();
    expect(h.ctl.view().postUpdateNotice).toBe(false);
  });
});

describe('createCensusController — projects added or removed from any surface', () => {
  it('the first settled observation is a baseline, not a change', () => {
    const h = harness();
    h.ctl.projectsChanged([], true); // still loading: ignored
    h.ctl.projectsChanged(['a', 'b'], false);
    expect(h.fetchCensus).not.toHaveBeenCalled();
  });

  it('an added and a removed project each re-take the census', () => {
    const h = harness();
    h.ctl.projectsChanged(['a', 'b'], false);
    h.ctl.projectsChanged(['a', 'b', 'c'], false);
    expect(h.fetchCensus).toHaveBeenCalledTimes(1);
    h.ctl.projectsChanged(['a', 'c'], false);
    expect(h.fetchCensus).toHaveBeenCalledTimes(2);
  });

  it('a reorder or a re-load of the same set does not', () => {
    const h = harness();
    h.ctl.projectsChanged(['a', 'b'], false);
    h.ctl.projectsChanged(['b', 'a'], false);
    h.ctl.projectsChanged(['a', 'b'], true);
    h.ctl.projectsChanged(['a', 'b'], false);
    expect(h.fetchCensus).not.toHaveBeenCalled();
  });
});

describe("createCensusController — a new project's background bundle install finishing", () => {
  const s = (status: 'pending' | 'running' | 'done' | 'deferred' | 'failed', id = 'n', at = 1) =>
    ({ project_id: id, status, observed_at: at });

  it('fires when a setup reaches a terminal status (done / deferred / failed)', () => {
    for (const terminal of ['done', 'deferred', 'failed'] as const) {
      const h = harness();
      h.ctl.setupObserved(null);
      h.ctl.setupObserved(s('running'));
      expect(h.fetchCensus).not.toHaveBeenCalled();
      h.ctl.setupObserved(s(terminal));
      expect(h.fetchCensus).toHaveBeenCalledTimes(1);
    }
  });

  it('fires once per setup, not on every later tick of the same terminal state', () => {
    const h = harness();
    h.ctl.setupObserved(null);
    h.ctl.setupObserved(s('running'));
    h.ctl.setupObserved(s('done'));
    h.ctl.setupObserved(s('done'));
    expect(h.fetchCensus).toHaveBeenCalledTimes(1);
    h.ctl.setupObserved(null); // banner dismissed
    expect(h.fetchCensus).toHaveBeenCalledTimes(1);
    h.ctl.setupObserved(s('done', 'other', 2)); // a second add finishes
    expect(h.fetchCensus).toHaveBeenCalledTimes(2);
  });

  it('a setup already terminal when the page mounts is a baseline (the mount census covers it)', () => {
    const h = harness();
    h.ctl.setupObserved(s('done'));
    expect(h.fetchCensus).not.toHaveBeenCalled();
  });
});
