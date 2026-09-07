// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.92 WP-B1 — tests for the KG-sync progress label logic shared by
// KgSyncBanner + KgSyncPill (kg-sync-banner-logic.ts).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  kgSyncPhaseKind,
  kgSyncDoneCount,
  kgSyncTotalCount,
  kgSyncBannerRunningLabel,
  kgSyncBannerSuccessLabel,
} from './kg-sync-banner-logic';
import type { KgSyncView } from '$lib/types/launcher';

function view(partial: Partial<KgSyncView>): KgSyncView {
  return {
    project_id: 'p1',
    status: 'running',
    started_at_iso: null,
    finished_at_iso: null,
    duration_ms: null,
    kg_total: 17,
    kg_succeeded: 17,
    kg_failed: 0,
    kg_skipped: 0,
    docs_total: 0,
    docs_succeeded: 0,
    docs_failed: 0,
    docs_skipped: 0,
    error_message: null,
    log_tail: null,
    current_phase: null,
    ...partial,
  };
}

describe('kgSyncPhaseKind', () => {
  it('classifies every phase the Rust side emits today', () => {
    expect(kgSyncPhaseKind('scan')).toBe('scan');
    expect(kgSyncPhaseKind('queued')).toBe('queued');
    expect(kgSyncPhaseKind('embed')).toBe('embed');
    expect(kgSyncPhaseKind('knowledge')).toBe('embed');
    expect(kgSyncPhaseKind(null)).toBe('embed');
    expect(kgSyncPhaseKind(undefined)).toBe('embed');
    expect(kgSyncPhaseKind('docs')).toBe('docs');
    expect(kgSyncPhaseKind('finalize')).toBe('finalize');
  });

  it('classifies an unrecognized phase as unknown — never as embed', () => {
    // A future Rust stage string must not silently render as "embedding".
    expect(kgSyncPhaseKind('cooldown')).toBe('unknown');
    expect(kgSyncPhaseKind('')).toBe('unknown');
  });
});

describe('kgSyncBannerRunningLabel — finalize stage (v0.2.92 WP-B1)', () => {
  it('renders the finalize stage distinctly, with and without a counter', () => {
    expect(kgSyncBannerRunningLabel('17 / 17', 'finalize')).toBe(
      'KG sync: finalizing summaries…',
    );
    expect(kgSyncBannerRunningLabel('', 'finalize')).toBe('KG sync: finalizing summaries…');
  });

  // RED-PROOF companion: the pre-fix inline mapping (KgSyncBanner.svelte
  // statusLabel, pre-v0.2.92, transcribed verbatim from /tmp/wp-b1/pre/
  // KgSyncBanner.svelte:143-157) rendered an unrecognized phase — which
  // 'finalize' was, before this change — as a confident WRONG
  // "embedding (N/N)". Pinned here so the regression contract is
  // executable, not just narrated.
  function legacyRunningLabel(counter: string, phase: string | null): string {
    if (phase === 'scan') return 'KG sync: scanning knowledge/ and docs/…';
    if (phase === 'queued') return 'KG sync: waiting for the embed lane…';
    if (counter) {
      if (phase === 'docs') return `KG sync: embedding docs (${counter})`;
      return `KG sync: embedding (${counter})`;
    }
    return 'KG sync: embedding…';
  }

  it('legacy mapping produced the WRONG label for finalize — the defect this closes', () => {
    expect(legacyRunningLabel('17 / 17', 'finalize')).toBe('KG sync: embedding (17 / 17)');
    expect(legacyRunningLabel('17 / 17', 'cooldown')).toBe('KG sync: embedding (17 / 17)');
  });
});

describe('kgSyncBannerRunningLabel — known phases unchanged + neutral unknown', () => {
  it('preserves the pre-fix labels for every known phase', () => {
    expect(kgSyncBannerRunningLabel('', 'scan')).toBe('KG sync: scanning knowledge/ and docs/…');
    expect(kgSyncBannerRunningLabel('3 / 9', 'scan')).toBe('KG sync: scanning knowledge/ and docs/…');
    expect(kgSyncBannerRunningLabel('3 / 9', 'queued')).toBe('KG sync: waiting for the embed lane…');
    expect(kgSyncBannerRunningLabel('5 / 9', 'docs')).toBe('KG sync: embedding docs (5 / 9)');
    expect(kgSyncBannerRunningLabel('5 / 9', 'knowledge')).toBe('KG sync: embedding (5 / 9)');
    expect(kgSyncBannerRunningLabel('5 / 9', 'embed')).toBe('KG sync: embedding (5 / 9)');
    expect(kgSyncBannerRunningLabel('5 / 9', null)).toBe('KG sync: embedding (5 / 9)');
    expect(kgSyncBannerRunningLabel('', 'knowledge')).toBe('KG sync: embedding…');
  });

  it('renders an unknown phase NEUTRALLY (raw phase, never "embedding")', () => {
    expect(kgSyncBannerRunningLabel('5 / 9', 'cooldown')).toBe('KG sync: cooldown (5 / 9)');
    expect(kgSyncBannerRunningLabel('', 'cooldown')).toBe('KG sync: cooldown…');
  });
});

describe('kgSyncDoneCount / kgSyncTotalCount', () => {
  it('counts intentionally-skipped files as done (bar completes honestly)', () => {
    const v = view({ kg_total: 117, kg_succeeded: 113, kg_skipped: 4 });
    expect(kgSyncDoneCount(v)).toBe(117);
    expect(kgSyncTotalCount(v)).toBe(117);
  });

  it('treats missing skip fields (older payloads) as 0', () => {
    const v = view({ kg_total: 10, kg_succeeded: 10, kg_skipped: undefined });
    expect(kgSyncDoneCount(v)).toBe(10);
  });
});

describe('kgSyncBannerSuccessLabel — indexed count, not discovered total (D12)', () => {
  it('counts only files actually indexed and surfaces the skip count', () => {
    expect(
      kgSyncBannerSuccessLabel(view({ kg_total: 117, kg_succeeded: 113, kg_skipped: 4 })),
    ).toBe('KG sync: indexed 113 nodes (4 skipped)');
  });

  it('singularizes for exactly one indexed node', () => {
    expect(
      kgSyncBannerSuccessLabel(view({ kg_total: 1, kg_succeeded: 1, kg_skipped: 0 })),
    ).toBe('KG sync: indexed 1 node');
  });

  it('keeps the plain shape when nothing was skipped', () => {
    expect(
      kgSyncBannerSuccessLabel(view({ kg_total: 12, kg_succeeded: 12 })),
    ).toBe('KG sync: indexed 12 nodes');
  });

  it('keeps the empty-project shape', () => {
    expect(kgSyncBannerSuccessLabel(view({ kg_total: 0, kg_succeeded: 0 }))).toBe(
      'KG sync: complete',
    );
  });
});

// Source-level pin (the repo's established pattern for inline Svelte
// logic, cf. test_v0249_bug_k_kg_sync_venv_picker.py): both components
// must DELEGATE to the shared module so the phase vocabulary has one
// home — an inline copy in either component is the drift this closes.
describe('components delegate to the shared phase logic', () => {
  const here = fileURLToPath(new URL('.', import.meta.url));

  function source(name: string): string {
    return readFileSync(`${here}/${name}`, 'utf-8');
  }

  it('KgSyncBanner.svelte imports and uses the shared module', () => {
    const s = source('KgSyncBanner.svelte');
    // Imports sit indented inside <script> — anchor on the line start,
    // not column 0 (the word may also appear in prose; the import shape
    // is the contract).
    expect(s).toMatch(/^\s*import\s*\{[^}]*kgSyncBannerRunningLabel/m);
    expect(s).toMatch(/^\s*import\s*\{[^}]*kgSyncBannerSuccessLabel/m);
    expect(s).toMatch(/kgSyncBannerRunningLabel\(progressCounter\(v\), v\.current_phase\)/);
    expect(s).not.toMatch(/current_phase === 'scan'/);
  });

  it('KgSyncPill.svelte classifies through kgSyncPhaseKind (finalize included)', () => {
    const s = source('KgSyncPill.svelte');
    expect(s).toMatch(/^\s*import\s*\{[^}]*kgSyncPhaseKind/m);
    expect(s).toMatch(/kgSyncPhaseKind\(v\.current_phase\)/);
    expect(s).toMatch(/kind === 'finalize'/);
    expect(s).not.toMatch(/current_phase === 'scan'/);
  });
});
