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

import { describe, it, expect } from 'vitest';
import {
  chipFor,
  indexCensus,
  summaryLine,
  needsAttentionCount,
  undeterminedCensus,
  shouldRecensusOnUpdaterEdge,
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
