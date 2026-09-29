// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (WP-03b, owner ruling F-W2-03): the divergence modal's "Reset to
// upstream" choice — the confirm dialog names BOTH backups before anything
// runs, the result names them exactly, and the action is the ONE store
// action with kind `ResetHard`.

import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  resetBundleDir,
  resetConfirmLines,
  resetResultText,
  runResetToUpstream,
} from './divergence-reset-logic';
import type { UpdateOutcome } from '$lib/stores/updater';

function outcome(partial: Partial<UpdateOutcome>): UpdateOutcome {
  return {
    kind: 'ResetHard',
    head_before: 'a',
    head_after: 'b',
    install_py_ran: true,
    restarted: false,
    log_path: null,
    ...partial,
  };
}

describe('confirm dialog', () => {
  it('names the backup branch AND the bundle directory before anything runs', () => {
    const text = resetConfirmLines('/home/u/.vct').join('\n');
    expect(text).toContain('vco-backup/<timestamp>');
    expect(text).toContain('vco-backup/<timestamp>-wip');
    expect(text).toContain('/home/u/.vct/backups/');
    expect(text).toContain('orchestrator-reset-<timestamp>.bundle');
    expect(text).toContain('If the backup cannot be written, nothing is reset.');
  });

  it('uses the platform separator of the state dir and never leaves the dir unnamed', () => {
    expect(resetBundleDir('C:\\Users\\u\\.vct')).toBe('C:\\Users\\u\\.vct\\backups\\');
    expect(resetBundleDir('/x/.vct/')).toBe('/x/.vct/backups/');
    expect(resetBundleDir(null)).toContain('backups');
  });
});

describe('result', () => {
  it('shows the backend message when it already names the backup', () => {
    const msg =
      'Orchestrator updated. Your 1 local commit(s) were saved to branch vco-backup/T and to /v/backups/orchestrator-reset-T.bundle.';
    const t = resetResultText(
      outcome({
        message: msg,
        reset_backup: { branch: 'vco-backup/T', uncommitted_branch: null, bundle: '/v/backups/orchestrator-reset-T.bundle', local_commits: 1 },
      }),
    );
    expect(t).toBe(msg);
  });

  it('builds the exact locations from the structured field when the message lacks them', () => {
    const t = resetResultText(
      outcome({
        message: 'Orchestrator updated.',
        reset_backup: {
          branch: 'vco-backup/T',
          uncommitted_branch: 'vco-backup/T-wip',
          bundle: '/v/backups/orchestrator-reset-T.bundle',
          local_commits: 2,
        },
      }),
    );
    expect(t).toContain('vco-backup/T');
    expect(t).toContain('vco-backup/T-wip');
    expect(t).toContain('/v/backups/orchestrator-reset-T.bundle');
  });

  it('a relaunch that ended the call still points at the backup branch', () => {
    expect(resetResultText(null)).toContain('vco-backup/<timestamp>');
  });
});

describe('the action', () => {
  it('runs the ONE store action with kind ResetHard', async () => {
    const run = vi.fn(async () => ({ ok: true as const, outcome: null }));
    await runResetToUpstream(run);
    expect(run).toHaveBeenCalledExactlyOnceWith('ResetHard');
  });

  it('the modal offers the choice and wires it through the confirm step', () => {
    const src = readFileSync(
      fileURLToPath(new URL('./OrchestratorUpdateDivergenceModal.svelte', import.meta.url)),
      'utf-8',
    );
    expect(src).toContain('Reset to upstream (discard local commits)');
    expect(src).toMatch(/onclick=\{askReset\}/);
    expect(src).toMatch(/onclick=\{confirmReset\}/);
    expect(src).toMatch(/runResetToUpstream\(\(kind\) => updater\.run\(kind\)\)/);
  });
});
