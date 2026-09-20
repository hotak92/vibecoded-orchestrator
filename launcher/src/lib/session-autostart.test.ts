// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SESSION_AUTOSTART,
  resolveSessionAutostart,
  sessionAutostartHint,
} from './session-autostart';

describe('session autostart preference (v0.2.95, R2)', () => {
  it('renders ON before the backend answers', () => {
    // An install that has never opened Preferences has no app_state row, and
    // the backend resolves that to ON. A checkbox that flashed OFF while
    // loading would misreport shipped behaviour.
    expect(resolveSessionAutostart(null)).toBe(true);
    expect(resolveSessionAutostart(undefined)).toBe(true);
    expect(DEFAULT_SESSION_AUTOSTART).toBe(true);
  });

  it('renders ON when the command is unreachable', () => {
    // Browser mode / partial install: the catch branch passes null. Same
    // answer — the hook will still start the launcher, so say so.
    expect(resolveSessionAutostart(null)).toBe(DEFAULT_SESSION_AUTOSTART);
  });

  it('honours an explicit stored value in both directions', () => {
    expect(resolveSessionAutostart(false)).toBe(false);
    expect(resolveSessionAutostart(true)).toBe(true);
  });

  it('says what happens in each state, including the no-focus promise', () => {
    const on = sessionAutostartHint(true);
    expect(on).toMatch(/tray/i);
    expect(on).toMatch(/focus/i);
    const off = sessionAutostartHint(false);
    expect(off).not.toEqual(on);
    expect(off).toMatch(/not be started/i);
  });
});
