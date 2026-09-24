// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The unregister stop + "Unregister anyway — leave these values" (owner
// ruling, v0.2.97 review R5 F39). The flow the settings tab and the project
// selector run.

import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import {
  UNREGISTER_ANYWAY_LABEL,
  UNREGISTER_STOPPED_PREFIX,
  isUnregisterStopped,
  runUnregister,
} from './unregister-escape';
import type { UnregisterOptions, UnregisterReport } from '$lib/types/launcher';

const here = dirname(fileURLToPath(import.meta.url));
const STOP =
  'Unregister stopped — the project is still registered (…). These values VCO wrote could not ' +
  'be removed: OPENAI_API_KEY in .claude/env. … choose "Unregister anyway — leave these values" …';

function report(extra: Partial<UnregisterReport> = {}): UnregisterReport {
  return {
    projectId: 'p1',
    projectName: 'Gamma',
    filesPurged: [],
    keysPurgedFromEnv: [],
    collectionsDropped: [],
    warnings: [],
    leftInPlace: [],
    leftoversNote: null,
    ...extra,
  };
}

describe('the unregister stop and its escape', () => {
  it('matches the Rust prefix and label exactly', () => {
    const rust = readFileSync(
      resolve(here, '../../src-tauri/src/commands/projects_v2/unregister_strip.rs'),
      'utf8',
    );
    expect(rust).toContain(`pub(crate) const UNREGISTER_STOPPED_PREFIX: &str = "${UNREGISTER_STOPPED_PREFIX}";`);
    expect(rust).toContain(`choose \\"${UNREGISTER_ANYWAY_LABEL}\\"`);
    expect(isUnregisterStopped(STOP)).toBe(true);
    expect(isUnregisterStopped(new Error(STOP))).toBe(true);
    expect(isUnregisterStopped('project p1 not found')).toBe(false);
  });

  it('a clean unregister never asks and never sends the escape', async () => {
    const del = vi.fn(async (_o: UnregisterOptions | null) => report());
    const ask = vi.fn(async () => true);
    const out = await runUnregister(del, { purgeLauncherFiles: true }, ask);
    expect(out).toEqual({ kind: 'done', report: report(), leftAnyway: false });
    expect(ask).not.toHaveBeenCalled();
    expect(del).toHaveBeenCalledTimes(1);
    expect(del.mock.calls[0][0]).toEqual({ purgeLauncherFiles: true, leaveUnremovable: false });
  });

  it('the stop stands when the user keeps the project', async () => {
    const del = vi.fn(async (_o: UnregisterOptions | null) => {
      throw STOP;
    });
    const ask = vi.fn(async () => false);
    const out = await runUnregister(del, null, ask);
    expect(out).toEqual({ kind: 'kept', message: STOP });
    expect(ask).toHaveBeenCalledWith(STOP);
    expect(del).toHaveBeenCalledTimes(1);
    expect(del.mock.calls[0][0]).toBeNull();
  });

  it('"Unregister anyway" re-runs with leaveUnremovable and keeps the other options', async () => {
    const left = report({ leftInPlace: ['OPENAI_API_KEY in .claude/env'], leftoversNote: '/p/.claude/VCO-UNREGISTER-LEFTOVERS.md' });
    const del = vi
      .fn(async (_o: UnregisterOptions | null) => left)
      .mockImplementationOnce(async () => {
        throw STOP;
      });
    const out = await runUnregister(del, { purgeLauncherFiles: true, purgeCollections: true }, async () => true);
    expect(out).toEqual({ kind: 'done', report: left, leftAnyway: true });
    expect(del.mock.calls[1][0]).toEqual({
      purgeLauncherFiles: true,
      purgeCollections: true,
      leaveUnremovable: true,
    });
  });

  it('any other failure is re-thrown, never offered the escape', async () => {
    const del = vi.fn(async () => {
      throw 'project p1 not found';
    });
    const ask = vi.fn(async () => true);
    await expect(runUnregister(del, null, ask)).rejects.toBe('project p1 not found');
    expect(ask).not.toHaveBeenCalled();
  });
});
