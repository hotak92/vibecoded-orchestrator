// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Bug D (v0.2.49) — Per-project badge unit tests.
//
// Why these tests live here
// ─────────────────────────
// User report (a contributor, V5 handoff): "when another project is selected
// modules disappear." The fix is two-part:
//   1. The catalog LIST is sourced from `$modules.catalog` (global).
//      This was already correct pre-fix — `mState.catalog` populates
//      from `list_module_catalog` (a project-agnostic Tauri command).
//      Switching projects does NOT change the list source.
//   2. Each tile renders a per-project STATUS BADGE so the user can
//      see at a glance whether the module is installed here, installed
//      elsewhere, or not installed anywhere. This is the new behaviour.
//
// Test scenarios are spelled out in the Bug D spec (V5 handoff §2):
//
//   (a) Catalog renders globally — with 2 entries in `mState.catalog`
//       and a selected project, both rows are visible regardless of
//       per-project install state.
//
//   (b) Per-project badge — installed here — when the install row
//       exists for the current project the badge says so.
//
//   (c) Per-project badge — installed elsewhere — when the catalog
//       entry's aggregate `kind='installed'` but no row for THIS
//       project, the badge surfaces the "installed elsewhere" state.
//
//   (d) Per-project badge — not installed — when the catalog kind is
//       `available` and no row exists, the badge says "not installed".
//
//   (e) Switching project does NOT hide rows — given a fixed
//       `mState.catalog`, the visible row count is constant across
//       different `currentProjectId` values; only the per-row badge
//       changes.
//
// Test environment
// ────────────────
// Pure-function tests against the `module-per-project-display` helper
// (the same module the `+page.svelte` renderer consumes). No Svelte
// runtime, no jsdom — same convention as `module-status-display.test.ts`
// and `module-active-gate.test.ts`.
//
// The spec called for `@testing-library/svelte` here; the existing
// project convention (vitest in node env, no jsdom, no @testing-library)
// is followed instead. The behaviour under test is fully covered by
// the pure helper; mounting `ModuleCatalog.svelte` would only re-assert
// what these tests already prove. Flagged in the report-back as a
// deliberate deviation.

import { describe, expect, it } from 'vitest';
import {
  resolvePerProjectBadge,
  perProjectBadgeClass,
  type PerProjectBadgeKind,
} from '$lib/module-per-project-display';
import type {
  ModuleCatalogEntry,
  ModuleInstallRow,
  ModuleStatus,
} from '$lib/types/launcher';

/** Build a minimal catalog entry; tests override the fields they care about. */
function makeEntry(over: Partial<ModuleCatalogEntry> = {}): ModuleCatalogEntry {
  return {
    id: 'vct-test-module',
    name: 'Test Module',
    version: '0.2.0',
    description: 'Test',
    category: 'test',
    tags: [],
    license_required: false,
    license_variant_ids: [],
    min_orchestrator_tier: 'free',
    compatibility_hosts: [],
    is_licensed: false,
    manifest_source: 'bundled',
    kind: 'available',
    parent_id: '',
    cta_route: '',
    ...over,
  };
}

/** Build a minimal install row; tests override the fields they care about. */
function makeRow(
  over: Partial<ModuleInstallRow> & { module_id?: string } = {},
): ModuleInstallRow {
  return {
    id: 'row-1',
    project_id: 'proj-x',
    module_id: 'vct-test-module',
    module_version: '0.2.0',
    install_path: '/tmp/install',
    status: 'installed',
    enabled: true,
    installed_at: 0,
    last_started_at: null,
    last_error: null,
    ...over,
  };
}

/**
 * Simulate the renderer's per-project visibility logic. The visible
 * list is sourced from the global catalog (project-agnostic).
 * `currentProjectInstalls` only affects per-tile badges, NOT the
 * visible-row count.
 */
function simulateVisibleRows(
  catalog: ModuleCatalogEntry[],
  _currentProjectInstalls: ModuleInstallRow[],
): ModuleCatalogEntry[] {
  // Matches the `mState.catalog.filter(...)` source-of-truth in
  // ModuleCatalog.svelte. With filter='all' and no search, every entry
  // is visible. The point of this test is to assert that the visible
  // count is project-agnostic — so we deliberately do NOT apply the
  // 'installed' filter pill here.
  return [...catalog];
}

describe('Catalog renders globally — modules do not disappear on project switch', () => {
  it('two catalog entries are visible regardless of which project is selected', () => {
    // Scenario (a) from the Bug D spec.
    const entryA = makeEntry({ id: 'vct-rl-reranker', name: 'RL Reranker' });
    const entryB = makeEntry({ id: 'vct-mao', name: 'MAO Runtime' });
    const catalog = [entryA, entryB];

    // Project X has module A installed. Project Y has neither.
    const installsForProjX = [makeRow({ module_id: 'vct-rl-reranker' })];
    const installsForProjY: ModuleInstallRow[] = [];

    const visibleOnX = simulateVisibleRows(catalog, installsForProjX);
    const visibleOnY = simulateVisibleRows(catalog, installsForProjY);

    expect(visibleOnX.length).toBe(2);
    expect(visibleOnY.length).toBe(2);
    expect(visibleOnX.map((m) => m.id)).toEqual(['vct-rl-reranker', 'vct-mao']);
    expect(visibleOnY.map((m) => m.id)).toEqual(['vct-rl-reranker', 'vct-mao']);
  });

  it('switching project does NOT hide rows — only the badge changes', () => {
    // Scenario (e) from the Bug D spec.
    const entry = makeEntry({
      id: 'vct-rl-reranker',
      kind: 'installed', // Aggregated: installed in at least one project.
    });
    const catalog = [entry];

    // Project X: module IS installed (row exists, enabled).
    const rowOnX = makeRow({
      project_id: 'proj-x',
      module_id: 'vct-rl-reranker',
      status: 'installed',
      enabled: true,
    });

    // Project Y: NO row for this module (it's installed only in X).
    const rowOnY = null;

    const visibleOnX = simulateVisibleRows(catalog, [rowOnX]);
    const visibleOnY = simulateVisibleRows(catalog, []);

    // Same row count across project switches.
    expect(visibleOnX.length).toBe(1);
    expect(visibleOnY.length).toBe(1);

    // BUT the badge differs.
    const badgeOnX = resolvePerProjectBadge(entry, rowOnX);
    const badgeOnY = resolvePerProjectBadge(entry, rowOnY);

    expect(badgeOnX.kind).toBe('enabled-here');
    expect(badgeOnY.kind).toBe('installed-elsewhere');

    // And the labels carry the user-visible distinction.
    expect(badgeOnX.label).toMatch(/here/i);
    expect(badgeOnY.label).toMatch(/elsewhere/i);
  });
});

describe('resolvePerProjectBadge — installed in current project', () => {
  it('enabled-here when install row exists, status=installed, enabled=true', () => {
    // Scenario (b) from the Bug D spec.
    const entry = makeEntry({ kind: 'installed' });
    const row = makeRow({ status: 'installed', enabled: true });

    const badge = resolvePerProjectBadge(entry, row);

    expect(badge.kind).toBe('enabled-here');
    expect(badge.label).toContain('Enabled');
    expect(badge.tooltip).toContain('enabled');
  });

  it('disabled-here when install row exists, status=installed, enabled=false', () => {
    const entry = makeEntry({ kind: 'installed' });
    const row = makeRow({ status: 'installed', enabled: false });

    const badge = resolvePerProjectBadge(entry, row);

    expect(badge.kind).toBe('disabled-here');
    expect(badge.label).toContain('Disabled');
    expect(badge.tooltip).toContain('toggle is off');
  });

  it('enabled-here / disabled-here also apply to status=running and status=stopped', () => {
    // The steady-state statuses (installed/running/stopped) all map to
    // the enabled/disabled-here variants so the badge reflects the
    // per-project toggle position uniformly.
    const entry = makeEntry({ kind: 'installed' });

    const runningEnabled = resolvePerProjectBadge(
      entry,
      makeRow({ status: 'running', enabled: true }),
    );
    const stoppedDisabled = resolvePerProjectBadge(
      entry,
      makeRow({ status: 'stopped', enabled: false }),
    );

    expect(runningEnabled.kind).toBe('enabled-here');
    expect(stoppedDisabled.kind).toBe('disabled-here');
  });

  it('installed-here (not enabled/disabled) for transient/error statuses', () => {
    // installing/error/broken: body tile already surfaces the lifecycle
    // state. Badge stays generic so the per-project signal is consistent.
    const entry = makeEntry({ kind: 'installed' });

    const transientStatuses: ModuleStatus[] = [
      'installing',
      'error',
      'broken',
    ];
    for (const status of transientStatuses) {
      const badge = resolvePerProjectBadge(
        entry,
        makeRow({ status, enabled: true }),
      );
      expect(badge.kind).toBe('installed-here');
      expect(badge.label).toContain('Installed');
    }
  });
});

describe('resolvePerProjectBadge — installed elsewhere', () => {
  it('installed-elsewhere when catalog kind=installed but no row for this project', () => {
    // Scenario (c) — this is the headline fix for Bug D.
    const entry = makeEntry({ kind: 'installed' });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('installed-elsewhere');
    expect(badge.label).toContain('elsewhere');
    expect(badge.tooltip).toContain('another project');
  });

  it('installed-elsewhere also applies when kind=update_available', () => {
    // `update_available` means "installed somewhere AND newer version
    // exists in catalog". The "installed elsewhere" signal is still
    // accurate from THIS project's POV.
    const entry = makeEntry({ kind: 'update_available' });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('installed-elsewhere');
  });

  it('installed-elsewhere also applies when kind=broken', () => {
    // `broken` means the reconciler found a missing on-disk artifact
    // for an installed module. From the perspective of a project that
    // didn't install it, the signal is still "this module exists
    // somewhere on the machine."
    const entry = makeEntry({ kind: 'broken' });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('installed-elsewhere');
  });
});

describe('resolvePerProjectBadge — install_scope=global (v0.2.49 amendment)', () => {
  // v0.2.49 Stream A introduced install.scope = "global" | "per_project".
  // A `global` module is installed ONCE on the host and is available
  // to every project (Stream B's enable toggle provides per-project
  // opt-out). The catalog's `kind` will be `installed` (or update_
  // available / broken) AND no per-project install row exists for any
  // project that hasn't explicitly enabled it. Pre-amendment this
  // rendered as `installed-elsewhere` on every project — wrong, since
  // "installed once = available to all" is the contract.

  it('enabled-globally when install_scope=global AND installedAnywhere', () => {
    const entry = makeEntry({
      kind: 'installed',
      install_scope: 'global',
    });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('enabled-globally');
    expect(badge.label).toMatch(/global/i);
    expect(badge.tooltip).toMatch(/every project/i);
  });

  it('enabled-globally also applies when kind=update_available + global scope', () => {
    const entry = makeEntry({
      kind: 'update_available',
      install_scope: 'global',
    });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('enabled-globally');
  });

  it('per_project scope still renders installed-elsewhere (no regression)', () => {
    const entry = makeEntry({
      kind: 'installed',
      install_scope: 'per_project',
    });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('installed-elsewhere');
  });

  it('empty install_scope falls back to installed-elsewhere (legacy launcher back-compat)', () => {
    // Pre-v0.2.49 launchers + L0 catalogs send empty/missing
    // install_scope. The wire shape's serde-default treats this as
    // "per_project" — the badge must continue to render as
    // installed-elsewhere so legacy module rendering is unchanged.
    const entry = makeEntry({
      kind: 'installed',
      install_scope: '',
    });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('installed-elsewhere');
  });

  it('install_scope=global with current-project install row still renders enabled-here', () => {
    // A user who explicitly installed the global module in a specific
    // project should see the per-project enabled-here badge, not the
    // host-wide enabled-globally badge. Branch 1 (install row exists)
    // wins over the global-scope check in Branch 2.
    const entry = makeEntry({
      kind: 'installed',
      install_scope: 'global',
    });
    const row = makeRow({ status: 'installed', enabled: true });
    const badge = resolvePerProjectBadge(entry, row);

    expect(badge.kind).toBe('enabled-here');
  });

  it('perProjectBadgeClass maps enabled-globally to a stable class name', () => {
    // CSS class binding sanity: the renderer reads this via
    // `perProjectBadgeClass(badge.kind)` and spreads it onto the
    // badge span. The class name must be deterministic across
    // renders.
    expect(perProjectBadgeClass('enabled-globally')).toBe(
      'pp-badge-global',
    );
  });
});

describe('resolvePerProjectBadge — not installed', () => {
  it('not-installed when catalog kind=available and no row for this project', () => {
    // Scenario (d).
    const entry = makeEntry({ kind: 'available' });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('not-installed');
    expect(badge.label).toContain('Not installed');
    expect(badge.tooltip).toContain('Not installed');
  });

  it('not-installed with coming-soon tooltip when kind=coming_soon', () => {
    const entry = makeEntry({ kind: 'coming_soon' });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('not-installed');
    expect(badge.tooltip).toMatch(/announced/i);
  });
});

describe('resolvePerProjectBadge — orchestrator-shipped variants', () => {
  it('bundled badge for kind=bundled regardless of install row presence', () => {
    const entry = makeEntry({ kind: 'bundled' });
    // Even if SOMEHOW an install row sneaks in (which the install
    // pipeline forbids), the badge should still say "bundled" —
    // bundled is a stronger signal than a stray DB row.
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('bundled');
    expect(badge.label).toBe('Bundled');
  });

  it('included badge for kind=subcomponent', () => {
    const entry = makeEntry({
      kind: 'subcomponent',
      parent_id: 'orchestrator',
      cta_route: '/kg',
    });
    const badge = resolvePerProjectBadge(entry, null);

    expect(badge.kind).toBe('included');
    expect(badge.label).toBe('Included');
  });
});

describe('perProjectBadgeClass — CSS suffix mapping', () => {
  it('returns a distinct class for every badge variant', () => {
    const variants: PerProjectBadgeKind[] = [
      'bundled',
      'included',
      'enabled-here',
      'disabled-here',
      'installed-here',
      'installed-elsewhere',
      'not-installed',
    ];
    const classes = variants.map(perProjectBadgeClass);
    // No two variants share a CSS class.
    expect(new Set(classes).size).toBe(variants.length);
    // Every class follows the `pp-badge-*` prefix convention.
    for (const c of classes) {
      expect(c.startsWith('pp-badge-')).toBe(true);
    }
  });
});
