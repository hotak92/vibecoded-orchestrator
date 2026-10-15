// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import {
  tailEvents,
  copyText,
  summaryLine,
  eventLine,
  eventKey,
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

describe('eventKey (v0.2.103)', () => {
  // Fixture verbatim from the owner's real state/logs/install.jsonl
  // (2026-10-13 session): four events where the (ts, step, phase) triple
  // repeats within a single second — two different actors finishing the
  // same step, and the same actor emitting twice for one step. This is
  // NORMAL log content, not corruption.
  const duplicateTripleEvents: InstallLogView['events'] = [
    { ts: '2026-10-13T23:51:07Z', actor: 'install.py', step: '8/10', phase: 'ok', detail: 'vct-hub binary swapped' },
    { ts: '2026-10-13T23:51:07Z', actor: 'install.py', step: '8/10', phase: 'ok', detail: 'vct-hub /health responded' },
    { ts: '2026-10-13T23:51:18Z', actor: 'post-install-launcher.sh', step: 'desktop-icon', phase: 'ok', detail: 'Linux desktop entry created' },
    { ts: '2026-10-13T23:51:18Z', actor: 'install.py', step: 'desktop-icon', phase: 'ok', detail: 'shortcut helper completed' },
  ];

  it('the naive (ts, step, phase) key COLLIDES on real logs — the defect this pins', () => {
    const naive = duplicateTripleEvents.map((e) => `${e.ts}${e.step}${e.phase}`);
    expect(new Set(naive).size, 'fixture must contain duplicate triples').toBeLessThan(naive.length);
  });

  it('eventKey is unique for every event, including duplicate (ts, step, phase) triples', () => {
    const keys = duplicateTripleEvents.map((e, i) => eventKey(e, i));
    expect(new Set(keys).size).toBe(duplicateTripleEvents.length);
  });

  it('eventKey embeds the event identity (reads as "this event" in devtools)', () => {
    const e = duplicateTripleEvents[0]!;
    expect(eventKey(e, 0)).toContain(e.ts);
    expect(eventKey(e, 0)).toContain(e.step);
    expect(eventKey(e, 0)).toContain(e.phase);
  });

  // v0.2.103 (review R8 m1): the strongest form of the property — a log
  // of IDENTICAL events. A mutation that stops using the index (e.g.
  // `eventKey(e, 0)`) must fail this, whatever the template looks like.
  it('eventKey stays unique over a log of IDENTICAL events (the index is load-bearing)', () => {
    const identical = Array.from({ length: 12 }, () =>
      ev('2026-10-13T23:51:07Z', '8/10', 'ok'),
    );
    const tail = tailEvents(log({ events: identical }));
    expect(tail).toHaveLength(12);
    const keys = tail.map((e, i) => eventKey(e, i));
    expect(new Set(keys).size).toBe(12);
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
