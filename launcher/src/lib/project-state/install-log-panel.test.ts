// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import {
  tailEvents,
  copyText,
  summaryLine,
  eventLine,
  TAIL_SIZE,
  type InstallLogView,
} from './install-log-panel';

function ev(ts: string, step: string, phase: string): InstallLogView['events'][number] {
  return { ts, actor: 'install.py', step, phase, detail: `${step} ${phase}` };
}

function log(overrides: Partial<InstallLogView> = {}): InstallLogView {
  return {
    events: [ev('2026-10-09T01:00:00Z', 'venv', 'ok')],
    state_summary: {
      session_started: '2026-10-09T01:00:00Z',
      completed_steps: ['venv'],
      skipped_steps: [],
      failed_steps: [],
      last_event_ts: '2026-10-09T01:01:00Z',
      looks_complete: true,
    },
    log_path: '/opt/vco/state/logs/install.jsonl',
    exists: true,
    ...overrides,
  };
}

describe('tailEvents (W5-B C8)', () => {
  it('returns the LAST TAIL_SIZE events, oldest first', () => {
    const events = Array.from({ length: TAIL_SIZE + 10 }, (_, i) =>
      ev(`t${String(i).padStart(3, '0')}`, `step${i}`, 'ok'),
    );
    const tail = tailEvents(log({ events }));
    expect(tail).toHaveLength(TAIL_SIZE);
    expect(tail[0].step).toBe(`step${10}`);
    expect(tail[tail.length - 1].step).toBe(`step${TAIL_SIZE + 9}`);
  });

  it('empty for a null log or an empty one', () => {
    expect(tailEvents(null)).toEqual([]);
    expect(tailEvents(log({ events: [] }))).toEqual([]);
  });
});

describe('copyText (W5-B C8)', () => {
  it('carries the path, the verdict, the failed steps, and EVERY event line', () => {
    const text = copyText(
      log({
        events: [ev('a', 'venv', 'ok'), ev('b', 'compose', 'error')],
        state_summary: {
          session_started: 's1',
          completed_steps: ['venv'],
          skipped_steps: [],
          failed_steps: [['compose', 'podman not found']],
          last_event_ts: 'b',
          looks_complete: false,
        },
      }),
    );
    const lines = text.split('\n');
    expect(lines[0]).toContain('/opt/vco/state/logs/install.jsonl');
    expect(text).toContain('failed steps: compose (podman not found)');
    // Every event, not just the tail — the copy is for a bug report.
    expect(text).toContain(eventLine(ev('a', 'venv', 'ok')));
    expect(text).toContain(eventLine(ev('b', 'compose', 'error')));
  });

  it('empty string for a null log', () => {
    expect(copyText(null)).toBe('');
  });
});

describe('summaryLine (W5-B C8)', () => {
  it('distinguishes no-log / failed / complete / incomplete', () => {
    expect(summaryLine(null)).toBe('Install log not loaded.');
    expect(summaryLine(log({ exists: false }))).toContain('No install log yet');
    expect(
      summaryLine(
        log({ state_summary: { ...log().state_summary, failed_steps: [['x', 'y']] } }),
      ),
    ).toContain('1 failed step');
    expect(summaryLine(log())).toBe('Last install session completed.');
    expect(
      summaryLine(
        log({ state_summary: { ...log().state_summary, looks_complete: false } }),
      ),
    ).toBe('Last install session did not reach a complete state.');
  });
});
