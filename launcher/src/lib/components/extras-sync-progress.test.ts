// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 F-W2-05: the Rust comment on `vct-codegraph-extras-progress`
// promised a "Syncing…" progress modal fed by the event; this pins that the
// modal is now fed by it.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  EXTRAS_PROGRESS_EVENT,
  applyExtrasProgress,
  progressPercent,
  type ExtrasProgressPayload,
} from './extras-sync-progress';

const ev = (p: Partial<ExtrasProgressPayload>): ExtrasProgressPayload => ({
  project_id: 'p1',
  label: 'clone',
  progress: 0,
  message: '',
  file: '',
  lang: '',
  ...p,
});

describe('applyExtrasProgress', () => {
  it('a running run takes the event: fraction + message', () => {
    const p = applyExtrasProgress(null, ev({ progress: 0.4, message: 'Parsing' }), 'p1', true);
    expect(p).toEqual({ fraction: 0.4, line: 'Parsing' });
    expect(progressPercent(p!)).toBe(40);
  });

  it('falls back to the file, clamps, and never goes backwards', () => {
    let p = applyExtrasProgress(null, ev({ progress: 1.7, file: 'a.py' }), 'p1', true);
    expect(p).toEqual({ fraction: 1, line: 'Indexing a.py' });
    p = applyExtrasProgress({ fraction: 0.5, line: 'x' }, ev({ progress: 0.2 }), 'p1', true);
    expect(p).toEqual({ fraction: 0.5, line: 'x' });
  });

  it('ignores another project and events outside a run', () => {
    const prev = { fraction: 0.3, line: 'y' };
    expect(applyExtrasProgress(prev, ev({ project_id: 'p2', progress: 0.9 }), 'p1', true)).toBe(prev);
    expect(applyExtrasProgress(prev, ev({ progress: 0.9 }), 'p1', false)).toBe(prev);
  });

  it('the event name matches the Rust emitter, and the panel + modal consume it', () => {
    const rs = readFileSync(
      fileURLToPath(new URL('../../../src-tauri/src/commands/project_codegraph_extras.rs', import.meta.url)),
      'utf8',
    );
    expect(rs).toContain(`const PROGRESS_EVENT: &str = "${EXTRAS_PROGRESS_EVENT}";`);
    const panel = readFileSync(
      fileURLToPath(new URL('../project-state/ExtraCodegraphPathsPanel.svelte', import.meta.url)),
      'utf8',
    );
    expect(panel).toMatch(/listen<ExtrasProgressPayload>\(\s*EXTRAS_PROGRESS_EVENT/);
    expect(panel).toContain('progress={syncProgress}');
    const modal = readFileSync(fileURLToPath(new URL('./ExtrasSyncProgressModal.svelte', import.meta.url)), 'utf8');
    expect(modal).toContain('role="progressbar"');
  });
});
