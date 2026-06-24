// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.35 (Agent J): unit tests for the catalog-tile display contract.
//
// These tests exercise the pure helper at `module-status-display.ts`
// rather than mounting `ModuleCatalog.svelte` in a DOM. The component
// is a thin renderer over `resolveTileDisplay`'s discriminated union —
// covering every branch of the union at the helper level gives us
// type-checked coverage of the same gating decisions with zero DOM
// surface, no Tauri mock, and runs in <200 ms.
//
// The two cases called out in the v0.2.35 backlog are exercised
// explicitly (error tile → Retry + Uninstall + last_error; installed
// tile → Uninstall + no Retry). Additional cases cover the surrounding
// matrix (broken status mirrors error; bundled / subcomponent / unlicensed
// branches keep their pre-v0.2.35 shapes; in-flight install wins over
// every other state).

import { describe, expect, it } from 'vitest';
import {
  LAST_ERROR_TRUNCATE_BUDGET,
  detectModuleErrorAfterAction,
  installProgressLabel,
  moduleActionForKind,
  resolveTileDisplay,
  semverLess,
  statusBadgeLabel,
  truncateLastError,
} from './module-status-display';
import type { ModuleCatalogEntry, ModuleInstallRow } from './types/launcher';

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
function makeRow(over: Partial<ModuleInstallRow> = {}): ModuleInstallRow {
  return {
    id: 'row-1',
    project_id: 'proj-1',
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

describe('resolveTileDisplay', () => {
  it('error tile: renders Retry + Uninstall + visible last_error (the core v0.2.35 fix)', () => {
    const entry = makeEntry();
    const row = makeRow({
      status: 'error',
      last_error: 'pip install failed: ConnectionError to pypi.org',
    });

    const display = resolveTileDisplay(entry, row, /* isInstalling */ false);

    // Must NOT come back as 'installed' — that was the pre-v0.2.35 bug
    // where the tile only offered Uninstall and the user had to
    // uninstall first before retrying.
    expect(display.kind).toBe('errored');
    if (display.kind !== 'errored') return; // narrowing for TS

    // The retry path (button rendered by the tile from this branch)
    // shares the same dispatch as a fresh install via
    // `handleRetryInstall` → `install_module_for_project`. The
    // discriminator we test is whether the helper put us in the
    // 'errored' branch — the Svelte template renders both Retry and
    // Uninstall unconditionally inside it.
    expect(display.status).toBe('error');
    expect(display.install_row).toBe(row);
    expect(display.last_error).toBe(
      'pip install failed: ConnectionError to pypi.org',
    );
  });

  it('broken tile: mirrors error tile (same Retry + Uninstall contract)', () => {
    // status='broken' is the reconciler's signal that ~/.vct/modules/<id>
    // was found missing on startup. The recovery action is Reinstall,
    // same as the 'error' status — and the Tauri command is identical.
    const entry = makeEntry();
    const row = makeRow({
      status: 'broken',
      last_error: 'on-disk manifest missing at ~/.vct/modules/vct-test-module',
    });

    const display = resolveTileDisplay(entry, row, false);
    expect(display.kind).toBe('errored');
    if (display.kind !== 'errored') return;
    expect(display.status).toBe('broken');
    expect(display.last_error).toContain('on-disk manifest missing');
  });

  it('installed tile: renders Uninstall (NOT Retry); update flag falls out of semver', () => {
    // Installed-and-healthy is the happy path: the spec calls for
    // Uninstall but NOT Retry on this branch.
    const entry = makeEntry({ version: '0.2.5' });
    const row = makeRow({
      status: 'installed',
      module_version: '0.2.0',
      enabled: true,
      last_error: null,
    });

    const display = resolveTileDisplay(entry, row, false);
    expect(display.kind).toBe('installed');
    if (display.kind !== 'installed') return;

    // Update CTA gating: 0.2.0 < 0.2.5 so the template renders the
    // Update button alongside Enabled toggle + Uninstall.
    expect(display.can_update).toBe(true);
    expect(display.install_row.enabled).toBe(true);
  });

  it('installed-with-same-version: Uninstall renders but Update does not', () => {
    const entry = makeEntry({ version: '0.2.0' });
    const row = makeRow({ status: 'installed', module_version: '0.2.0' });

    const display = resolveTileDisplay(entry, row, false);
    expect(display.kind).toBe('installed');
    if (display.kind !== 'installed') return;
    expect(display.can_update).toBe(false);
  });

  it('installing-id wins over a stale error row (no double-button render mid-flight)', () => {
    // Edge case: the user clicked Retry, RPC is in flight, but the
    // row still says 'error' from the previous attempt. The spinner
    // must take priority — otherwise the Retry button would still be
    // clickable and the user could fire two installs in parallel.
    const entry = makeEntry();
    const row = makeRow({ status: 'error', last_error: 'previous error' });

    const display = resolveTileDisplay(entry, row, /* isInstalling */ true);
    expect(display.kind).toBe('installing');
  });

  it('available + unlicensed tier-gated: activate-license branch (no Install)', () => {
    const entry = makeEntry({
      license_required: true,
      is_licensed: false,
      min_orchestrator_tier: 'pro',
    });

    const display = resolveTileDisplay(entry, null, false);
    expect(display.kind).toBe('available');
    if (display.kind !== 'available') return;
    expect(display.needs_license).toBe(true);
  });

  it('available + licensed tier-gated: regular Install branch', () => {
    const entry = makeEntry({
      license_required: true,
      is_licensed: true,
      min_orchestrator_tier: 'pro',
    });

    const display = resolveTileDisplay(entry, null, false);
    expect(display.kind).toBe('available');
    if (display.kind !== 'available') return;
    expect(display.needs_license).toBe(false);
  });

  it('bundled kind: no install / uninstall affordance', () => {
    const entry = makeEntry({ kind: 'bundled' });
    const display = resolveTileDisplay(entry, null, false);
    expect(display.kind).toBe('bundled');
  });

  it('subcomponent kind: included-with-parent badge + optional dashboard CTA', () => {
    const entry = makeEntry({
      kind: 'subcomponent',
      parent_id: 'orchestrator',
      cta_route: '/kg',
    });
    const display = resolveTileDisplay(entry, null, false);
    expect(display.kind).toBe('included');
    if (display.kind !== 'included') return;
    expect(display.parent_id).toBe('orchestrator');
    expect(display.cta_route).toBe('/kg');
  });

  it('row.status="installing" without store flag still treats as installing', () => {
    // Defense against a race where the install-complete event fired
    // in another window but our store's `installingId` hasn't caught
    // up. The DB row is the canonical signal.
    const entry = makeEntry();
    const row = makeRow({ status: 'installing' });
    const display = resolveTileDisplay(entry, row, false);
    expect(display.kind).toBe('installing');
  });

  // ── v0.2.67: live install-progress is rendered on the installing branch ──

  it('installing branch carries a live progress label from a non-variant_fallback stage', () => {
    const entry = makeEntry();
    const display = resolveTileDisplay(entry, null, /* isInstalling */ true, {
      stage: 'pulling',
      percent: 40,
      message: 'Pulling image',
      failed: false,
    });
    expect(display.kind).toBe('installing');
    if (display.kind !== 'installing') return;
    expect(display.progress).toBe('Pulling image… 40%');
  });

  it('installing branch progress is null before any progress event arrives', () => {
    const entry = makeEntry();
    const display = resolveTileDisplay(entry, null, true, null);
    expect(display.kind).toBe('installing');
    if (display.kind !== 'installing') return;
    expect(display.progress).toBeNull();
  });

  it('a terminal failed stage flips the tile out of installing immediately', () => {
    // A fast 401 emits stage=failed; the user must see the error NOW, not
    // a hanging spinner waiting for the install RPC to propagate its Err.
    const entry = makeEntry();
    const row = makeRow({ status: 'error', last_error: 'unauthorized' });
    const display = resolveTileDisplay(entry, row, /* isInstalling */ true, {
      stage: 'failed',
      percent: 0,
      message: 'pull failed: unauthorized',
      failed: true,
    });
    expect(display.kind).toBe('errored');
  });
});

describe('installProgressLabel', () => {
  it('null/undefined → null (render bare spinner)', () => {
    expect(installProgressLabel(null)).toBeNull();
    expect(installProgressLabel(undefined)).toBeNull();
  });

  it('terminal done/failed stages → null (row status drives display)', () => {
    expect(
      installProgressLabel({ stage: 'done', percent: 100, message: 'Done', failed: false }),
    ).toBeNull();
    expect(
      installProgressLabel({ stage: 'failed', percent: 0, message: 'boom', failed: true }),
    ).toBeNull();
  });

  it('intermediate stage with mid-range percent → message + percent', () => {
    expect(
      installProgressLabel({ stage: 'clone', percent: 25, message: 'Fetching source', failed: false }),
    ).toBe('Fetching source… 25%');
  });

  it('intermediate stage with boundary percent (0 or 100) → message only', () => {
    expect(
      installProgressLabel({ stage: 'pulling', percent: 100, message: 'Pulling image', failed: false }),
    ).toBe('Pulling image…');
    expect(
      installProgressLabel({ stage: 'pulling', percent: 0, message: 'Pulling image', failed: false }),
    ).toBe('Pulling image…');
  });

  it('empty message → "Installing…" fallback', () => {
    expect(
      installProgressLabel({ stage: 'post_install', percent: 0, message: '', failed: false }),
    ).toBe('Installing…');
  });
});

describe('truncateLastError', () => {
  it('returns empty string for null/undefined/empty input', () => {
    expect(truncateLastError(null)).toEqual({ display: '', truncated: false });
    expect(truncateLastError(undefined)).toEqual({
      display: '',
      truncated: false,
    });
    expect(truncateLastError('')).toEqual({ display: '', truncated: false });
  });

  it('short message passes through untouched', () => {
    const msg = 'pip install failed: ConnectionError';
    const out = truncateLastError(msg);
    expect(out.display).toBe(msg);
    expect(out.truncated).toBe(false);
  });

  it('long message is truncated with ellipsis + truncated flag', () => {
    const msg = 'x'.repeat(LAST_ERROR_TRUNCATE_BUDGET + 50);
    const out = truncateLastError(msg);
    expect(out.truncated).toBe(true);
    expect(out.display.endsWith('…')).toBe(true);
    expect(out.display.length).toBeLessThanOrEqual(
      LAST_ERROR_TRUNCATE_BUDGET + 1,
    );
  });

  it('collapses internal whitespace runs to single spaces', () => {
    const msg = 'line1\n\n   line2\t\tline3';
    const out = truncateLastError(msg);
    expect(out.display).toBe('line1 line2 line3');
  });
});

describe('statusBadgeLabel', () => {
  it('maps every ModuleStatus variant to a non-empty human label', () => {
    // Sanity: every variant gets a label. If a new status is added
    // and someone forgets to extend this fn, the exhaustiveness check
    // would fire at type-check time, not here — but a regression
    // catch in tests is cheap.
    expect(statusBadgeLabel('installing')).toBe('Installing');
    expect(statusBadgeLabel('installed')).toBe('Installed');
    expect(statusBadgeLabel('running')).toBe('Running');
    expect(statusBadgeLabel('stopped')).toBe('Stopped');
    expect(statusBadgeLabel('error')).toBe('Install failed');
    expect(statusBadgeLabel('broken')).toBe('Files missing');
  });
});

describe('semverLess', () => {
  it('basic strict less-than', () => {
    expect(semverLess('0.2.0', '0.2.1')).toBe(true);
    expect(semverLess('0.2.1', '0.2.0')).toBe(false);
    expect(semverLess('0.2.0', '0.2.0')).toBe(false);
  });

  it('handles pre-release suffix by parsing leading integer', () => {
    // "0.2.4-dev" → 0.2.4; differs from strict semver but matches
    // the historic launcher behavior (we don't differentiate
    // pre-release from release for the "you have an update" CTA).
    expect(semverLess('0.2.0', '0.2.4-dev')).toBe(true);
    expect(semverLess('0.2.4-dev', '0.2.4')).toBe(false);
  });

  it('handles mismatched segment counts', () => {
    expect(semverLess('1', '1.0.1')).toBe(true);
    expect(semverLess('1.0', '1.0.0')).toBe(false);
  });
});

describe('moduleActionForKind', () => {
  it('broken → Reinstall via install', () => {
    expect(moduleActionForKind('broken')).toEqual({
      label: 'Reinstall',
      method: 'install',
    });
  });

  it('error → Retry install via install', () => {
    expect(moduleActionForKind('error')).toEqual({
      label: 'Retry install',
      method: 'install',
    });
  });

  it('update_available → Update via update', () => {
    expect(moduleActionForKind('update_available')).toEqual({
      label: 'Update',
      method: 'update',
    });
  });

  it('non-actionable kinds return null', () => {
    for (const kind of [
      'bundled',
      'installed',
      'available',
      'subcomponent',
      'coming_soon',
    ]) {
      expect(moduleActionForKind(kind)).toBeNull();
    }
  });

  it('null / undefined / unknown kinds return null', () => {
    expect(moduleActionForKind(null)).toBeNull();
    expect(moduleActionForKind(undefined)).toBeNull();
    expect(moduleActionForKind('totally-unknown')).toBeNull();
  });
});

describe('detectModuleErrorAfterAction', () => {
  const ID = 'vct-rl-reranker';

  it('detects error from catalog kind even when the row is misleadingly clean', () => {
    // The live-test case (2026-06-06): install resolves with a clean row
    // (status='installed', last_error=null) but the catalog kind is 'error'.
    const catalog = [{ id: ID, kind: 'error' }];
    const installed = [{ module_id: ID, last_error: null, status: 'installed' }];
    const msg = detectModuleErrorAfterAction(ID, catalog, installed);
    expect(msg).not.toBeNull();
    expect(msg).toContain('error');
  });

  it('prefers the row last_error message when present', () => {
    const catalog = [{ id: ID, kind: 'error' }];
    const installed = [
      { module_id: ID, last_error: 'docker run failed (exit 125): unauthorized', status: 'error' },
    ];
    expect(detectModuleErrorAfterAction(ID, catalog, installed)).toBe(
      'docker run failed (exit 125): unauthorized',
    );
  });

  it('detects broken kind', () => {
    const catalog = [{ id: ID, kind: 'broken' }];
    const installed = [{ module_id: ID, last_error: null }];
    expect(detectModuleErrorAfterAction(ID, catalog, installed)).not.toBeNull();
  });

  it('returns null on a genuine success (kind installed, no last_error)', () => {
    const catalog = [{ id: ID, kind: 'installed' }];
    const installed = [{ module_id: ID, last_error: null, status: 'installed' }];
    expect(detectModuleErrorAfterAction(ID, catalog, installed)).toBeNull();
  });

  it('detects error from the row even when the catalog entry is missing', () => {
    const catalog: Array<{ id: string; kind: string }> = [];
    const installed = [{ module_id: ID, last_error: 'boom', status: 'error' }];
    expect(detectModuleErrorAfterAction(ID, catalog, installed)).toBe('boom');
  });
});
