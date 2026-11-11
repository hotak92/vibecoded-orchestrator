// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SERVICES_WATCHER,
  resolveServicesWatcher,
  servicesWatcherHint,
} from './services-watcher';

describe('services watcher preference (v0.2.106)', () => {
  it('renders ON before the backend answers', () => {
    // An install that has never opened Preferences has no app_state row,
    // and the backend resolves that to ON. A checkbox that flashed OFF
    // while loading would misreport shipped behaviour.
    expect(resolveServicesWatcher(null)).toBe(true);
    expect(resolveServicesWatcher(undefined)).toBe(true);
    expect(DEFAULT_SERVICES_WATCHER).toBe(true);
  });

  it('renders ON when the command is unreachable', () => {
    // Browser mode / partial install: the catch branch passes null. The
    // watcher will still self-heal, so the toggle must say so.
    expect(resolveServicesWatcher(null)).toBe(DEFAULT_SERVICES_WATCHER);
  });

  it('honours an explicit stored value in both directions', () => {
    expect(resolveServicesWatcher(false)).toBe(false);
    expect(resolveServicesWatcher(true)).toBe(true);
  });

  it('the hint names both restarters and the Stop pause, in both states', () => {
    const on = servicesWatcherHint(true);
    expect(on).toContain('restarts it automatically');
    expect(on).toContain('hub watchdog');
    expect(on).toContain('left down');
    const off = servicesWatcherHint(false);
    expect(off).toContain('neither');
    expect(off).toContain('hub watchdog');
  });
});
