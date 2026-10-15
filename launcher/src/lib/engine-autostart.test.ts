// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_ENGINE_AUTOSTART,
  engineAutostartHint,
  resolveEngineAutostart,
} from './engine-autostart';

describe('container engine autostart preference (v0.2.103, review R19 F4)', () => {
  it('renders ON before the backend answers', () => {
    // An install that has never opened Preferences has no app_state row, and
    // the backend resolves that to ON. A checkbox that flashed OFF while
    // loading would misreport shipped behaviour.
    expect(resolveEngineAutostart(null)).toBe(true);
    expect(resolveEngineAutostart(undefined)).toBe(true);
    expect(DEFAULT_ENGINE_AUTOSTART).toBe(true);
  });

  it('renders ON when the command is unreachable', () => {
    // Browser mode / partial install: the catch branch passes null. Same
    // answer — the starter will still run, so say so.
    expect(resolveEngineAutostart(null)).toBe(DEFAULT_ENGINE_AUTOSTART);
  });

  it('honours an explicit stored value in both directions', () => {
    expect(resolveEngineAutostart(false)).toBe(false);
    expect(resolveEngineAutostart(true)).toBe(true);
  });

  it('says what happens in each state, and names the env override', () => {
    const on = engineAutostartHint(true);
    expect(on).toMatch(/only for the runtime this install recorded/i);
    expect(on).toMatch(/VCT_CONTAINER_ENGINE_AUTOSTART/);
    const off = engineAutostartHint(false);
    expect(off).not.toEqual(on);
    expect(off).toMatch(/not be started/i);
  });
});
