// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.75 P2d (C-11b) — tests for the prune-failure escalation decision +
// drop-and-recreate command builder used by CodeGraphBuildBanner.

import { describe, it, expect } from 'vitest';
import {
  isPruneFailurePartial,
  buildDropRecreateCommand,
  buildDetailLine,
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
    expect(buildDropRecreateCommand()).toBe(
      'code-graph-analyze . --from-resolver --force-recreate',
    );
  });

  // v0.2.92 (BLOCKER-2): the DISPLAY name must never reach --project here.
  // The analyzer sanitizes --project into the class prefix it DROPS, so a
  // display name that differs from the project's bound `collection_prefix`
  // rebuilt the wrong family (and could drop another project's). Identity
  // now comes from the resolver, which is what the hooks already use.
  it('never derives the dropped family from a display name', () => {
    const cmd = buildDropRecreateCommand();
    expect(cmd).toContain('--from-resolver');
    expect(cmd).not.toContain('--project');
  });

  it('always uses the real --force-recreate flag (never a bogus --force)', () => {
    const cmd = buildDropRecreateCommand();
    expect(cmd).toContain('--force-recreate');
    expect(cmd).not.toMatch(/--force(?!-recreate)/);
  });
});

// v0.2.96 (L-4): a FAILED build must say WHY on the banner itself. Pre-fix
// `detailLine` appended `error_message` only for `partial`, so the one state
// that never auto-hides showed the user a dead end.
describe('buildDetailLine', () => {
  it('surfaces the reason on a FAILED build', () => {
    const line = buildDetailLine(
      view({
        status: 'failed',
        languages: [],
        duration_ms: null,
        error_message: 'code-graph-analyze exited 1: ModuleNotFoundError: weaviate',
      }),
    );
    expect(line).toContain('code-graph-analyze exited 1');
    expect(line).toContain('ModuleNotFoundError: weaviate');
  });

  it('keeps the reason FIRST, ahead of languages and duration', () => {
    const line = buildDetailLine(
      view({
        status: 'failed',
        languages: ['py', 'rs'],
        duration_ms: 2_500,
        error_message: 'scan folder failed: permission denied',
      }),
    );
    expect(line).toBe(
      'scan folder failed: permission denied · Languages: py, rs · Took 2.5s',
    );
  });

  it('still surfaces the partial warning it always did', () => {
    const line = buildDetailLine(
      view({
        status: 'partial',
        languages: [],
        duration_ms: null,
        error_message: '3 stale row(s) could not be pruned; inserts succeeded',
      }),
    );
    expect(line).toContain('could not be pruned');
  });

  it('adds nothing when a failed row persisted no message (leave-alone)', () => {
    const line = buildDetailLine(
      view({ status: 'failed', languages: [], duration_ms: null, error_message: null }),
    );
    expect(line).toBe('');
  });

  it('does NOT put a message on a SUCCESS row', () => {
    // Nothing writes one today; if something starts to, "indexed N files"
    // must not grow an error clause without a deliberate decision.
    const line = buildDetailLine(
      view({
        status: 'success',
        languages: ['py'],
        duration_ms: 1_000,
        error_message: 'leftover',
      }),
    );
    expect(line).toBe('Languages: py · Took 1.0s');
  });
});
