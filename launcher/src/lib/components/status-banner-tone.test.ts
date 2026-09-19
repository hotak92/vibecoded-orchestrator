// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — the tone vocabulary the shared banner shell paints from.
// The semantic decisions worth pinning: work that was SKIPPED or DEFERRED is
// amber/informational, never the failure pink; a `partial` code-graph build
// (inserts succeeded, stale-row prune didn't) is likewise amber.

import { describe, it, expect } from 'vitest';
import type {
  CodeGraphBuildStatus,
  KgSummaryStatus,
  KgSyncStatus,
  ProjectSetupStatus,
} from '$lib/types/launcher';
import {
  toneForCodeGraphBuildStatus,
  toneForKgSummaryStatus,
  toneForKgSyncStatus,
  toneForProjectSetupStatus,
} from './status-banner-tone';

const TONES = ['pending', 'running', 'success', 'warning', 'failed'];

describe('project setup', () => {
  it('shows a queued add as running — the user is already waiting', () => {
    expect(toneForProjectSetupStatus('pending')).toBe('running');
    expect(toneForProjectSetupStatus('running')).toBe('running');
  });

  it('paints a deferral amber, never the failure pink', () => {
    expect(toneForProjectSetupStatus('deferred')).toBe('warning');
    expect(toneForProjectSetupStatus('failed')).toBe('failed');
    expect(toneForProjectSetupStatus('done')).toBe('success');
  });
});

describe('code graph build', () => {
  it('treats `partial` as a warning, not a failure', () => {
    expect(toneForCodeGraphBuildStatus('partial')).toBe('warning');
    expect(toneForCodeGraphBuildStatus('skipped')).toBe('warning');
    expect(toneForCodeGraphBuildStatus('failed')).toBe('failed');
    expect(toneForCodeGraphBuildStatus('success')).toBe('success');
    expect(toneForCodeGraphBuildStatus('pending')).toBe('pending');
    expect(toneForCodeGraphBuildStatus('running')).toBe('running');
  });
});

describe('kg sync / kg summary', () => {
  it('maps the shared lifecycle identically for both', () => {
    const statuses: KgSyncStatus[] = [
      'pending',
      'running',
      'success',
      'failed',
      'skipped',
    ];
    for (const s of statuses) {
      expect(toneForKgSyncStatus(s)).toBe(toneForKgSummaryStatus(s as KgSummaryStatus));
    }
    expect(toneForKgSyncStatus('skipped')).toBe('warning');
    expect(toneForKgSyncStatus('failed')).toBe('failed');
  });
});

describe('every status resolves to a known tone', () => {
  it('leaves no status unmapped', () => {
    const setup: ProjectSetupStatus[] = ['pending', 'running', 'done', 'deferred', 'failed'];
    const build: CodeGraphBuildStatus[] = [
      'pending',
      'running',
      'success',
      'partial',
      'failed',
      'skipped',
    ];
    const sync: KgSyncStatus[] = ['pending', 'running', 'success', 'failed', 'skipped'];
    const summary: KgSummaryStatus[] = ['pending', 'running', 'success', 'failed', 'skipped'];

    for (const s of setup) expect(TONES).toContain(toneForProjectSetupStatus(s));
    for (const s of build) expect(TONES).toContain(toneForCodeGraphBuildStatus(s));
    for (const s of sync) expect(TONES).toContain(toneForKgSyncStatus(s));
    for (const s of summary) expect(TONES).toContain(toneForKgSummaryStatus(s));
  });
});
