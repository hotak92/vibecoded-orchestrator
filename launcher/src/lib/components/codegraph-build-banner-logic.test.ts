// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.75 P2d (C-11b) — tests for the prune-failure escalation decision +
// drop-and-recreate command builder used by CodeGraphBuildBanner.

import { describe, it, expect } from 'vitest';
import {
  isPruneFailurePartial,
  buildDropRecreateCommand,
  PRUNE_FAILURE_SIGNATURE,
} from './codegraph-build-banner-logic';
import type { CodeGraphBuildView } from '$lib/types/launcher';

function view(partial: Partial<CodeGraphBuildView>): CodeGraphBuildView {
  return {
    project_id: 'p1',
    status: 'partial',
    started_at_iso: null,
    finished_at_iso: null,
    duration_ms: null,
    files_analyzed: 10,
    languages: ['py'],
    joern_used: false,
    error_message: null,
    log_tail: null,
    current_phase: null,
    ...partial,
  };
}

describe('isPruneFailurePartial', () => {
  it('is true for a partial whose message carries the prune-failure signature', () => {
    expect(
      isPruneFailurePartial(
        view({
          status: 'partial',
          error_message: '3 stale row(s) could not be pruned; inserts succeeded',
        }),
      ),
    ).toBe(true);
  });

  it('is FALSE for a partial WITHOUT the signature (leave-alone → plain rebuild)', () => {
    expect(
      isPruneFailurePartial(
        view({ status: 'partial', error_message: 'some other stale-row warning' }),
      ),
    ).toBe(false);
  });

  it('is false for a partial with no error message', () => {
    expect(isPruneFailurePartial(view({ status: 'partial', error_message: null }))).toBe(false);
  });

  it('is false for non-partial statuses even if the text matches', () => {
    for (const status of ['failed', 'success', 'running', 'pending', 'skipped'] as const) {
      expect(
        isPruneFailurePartial(
          view({ status, error_message: 'stale row(s) could not be pruned' }),
        ),
      ).toBe(false);
    }
  });

  it('is false for null/undefined view', () => {
    expect(isPruneFailurePartial(null)).toBe(false);
    expect(isPruneFailurePartial(undefined)).toBe(false);
  });

  it('the signature matches the Rust reader text', () => {
    // Guard: this substring MUST stay in the Rust partial error_message.
    expect('7 stale row(s) could not be pruned; inserts succeeded').toContain(
      PRUNE_FAILURE_SIGNATURE,
    );
  });
});

describe('buildDropRecreateCommand', () => {
  it('builds the analyzer drop+recreate command with --force-recreate', () => {
    expect(buildDropRecreateCommand('MyProject')).toBe(
      'code-graph-analyze . --project MyProject --force-recreate',
    );
  });

  it('degrades a blank/missing name to a <project> placeholder', () => {
    expect(buildDropRecreateCommand('')).toBe(
      'code-graph-analyze . --project <project> --force-recreate',
    );
    expect(buildDropRecreateCommand(null)).toBe(
      'code-graph-analyze . --project <project> --force-recreate',
    );
    expect(buildDropRecreateCommand('   ')).toBe(
      'code-graph-analyze . --project <project> --force-recreate',
    );
  });

  it('always uses the real --force-recreate flag (never a bogus --force)', () => {
    const cmd = buildDropRecreateCommand('X');
    expect(cmd).toContain('--force-recreate');
    expect(cmd).not.toMatch(/--force(?!-recreate)/);
  });
});
