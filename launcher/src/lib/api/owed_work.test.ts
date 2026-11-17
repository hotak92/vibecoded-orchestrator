// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.107 (A-003) — Preferences → Services → "Owed background work": the
// line's decisions (owed_work.ts) and its wiring (the component calls them,
// the page mounts it in the Services section, the command is registered).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadOwedWorkStatus, projectLabel, queueLine, type OwedWorkStatus } from './owed_work';

const here = dirname(fileURLToPath(import.meta.url));

function st(over: Partial<OwedWorkStatus> = {}): OwedWorkStatus {
  return { ok: true, queued: 0, worker_running: false, projects: [], ...over };
}

const two = [
  { key: 'a-1', folder: '/home/u/Projects/alpha', enqueued_at: 1, tries: 0 },
  { key: 'b-2', folder: 'C:\\Users\\u\\beta', enqueued_at: 2, tries: 1 },
];

describe('queueLine', () => {
  it('says nothing is waiting on an empty queue', () => {
    expect(queueLine(st())).toBe('Nothing queued — no skipped background work is waiting.');
  });

  it('names the queued projects and a running worker', () => {
    expect(queueLine(st({ queued: 2, worker_running: true, projects: two }))).toBe(
      '2 projects queued (alpha, beta) — the background worker is running them one at a time.',
    );
  });

  it('says when they will run while no worker runs', () => {
    expect(queueLine(st({ queued: 1, projects: [two[0]] }))).toContain(
      '1 project queued (alpha) — they run once Weaviate is ready',
    );
  });

  it('reports an unreadable queue instead of "nothing queued"', () => {
    expect(queueLine(st({ ok: false, error: 'boom' }))).toBe(
      'Could not read the owed-work queue: boom.',
    );
  });

  it('labels POSIX and Windows folders', () => {
    expect(projectLabel('/a/b/c/')).toBe('c');
    expect(projectLabel('C:\\x\\y')).toBe('y');
  });
});

describe('wiring', () => {
  const component = readFileSync(resolve(here, '../preferences/OwedWorkQueue.svelte'), 'utf-8');
  const page = readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf-8');
  const lib = readFileSync(resolve(here, '../../../src-tauri/src/lib.rs'), 'utf-8');

  it('the component reads through the tested functions', () => {
    expect(component).toContain('loadOwedWorkStatus({ invoke })');
    expect(component).toContain('queueLine(st)');
  });

  it('the page mounts it inside the Services section', () => {
    const section = page.slice(page.indexOf('id="pr-services-watcher-title"'));
    const end = section.indexOf('</section>');
    expect(section.slice(0, end)).toContain('<OwedWorkQueue />');
  });

  it('the command it invokes is registered', async () => {
    expect(lib).toContain('commands::owed_work::get_owed_work_status');
    const calls: string[] = [];
    await loadOwedWorkStatus({
      invoke: async <T>(cmd: string): Promise<T> => {
        calls.push(cmd);
        return st() as T;
      },
    });
    expect(calls).toEqual(['get_owed_work_status']);
  });
});
