// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Unit tests for the error-notification (bell) store. Covers the mixed
// auto-resolution strategy: dedup-by-key, auto-clear-on-success, manual
// dismiss/clear.

import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { notifications, errorCount } from './notifications';

function snapshot() {
  return get(notifications);
}

describe('notifications store', () => {
  beforeEach(() => {
    notifications.clearAll();
  });

  it('records an error as a single entry with count 1', () => {
    notifications.record('boom', undefined, 1000);
    const list = snapshot();
    expect(list).toHaveLength(1);
    expect(list[0].message).toBe('boom');
    expect(list[0].count).toBe(1);
    expect(get(errorCount)).toBe(1);
  });

  it('dedups by explicit key, bumping count instead of stacking', () => {
    notifications.record('install failed', 'module:rl:install', 1000);
    notifications.record('install failed again', 'module:rl:install', 2000);
    const list = snapshot();
    expect(list).toHaveLength(1);
    expect(list[0].count).toBe(2);
    expect(list[0].lastSeen).toBe(2000);
    // Latest message text wins.
    expect(list[0].message).toBe('install failed again');
    expect(get(errorCount)).toBe(2);
  });

  it('dedups untagged errors by identical message text', () => {
    notifications.record('same error', undefined, 1000);
    notifications.record('same error', undefined, 1500);
    notifications.record('different error', undefined, 1600);
    const list = snapshot();
    expect(list).toHaveLength(2);
    const same = list.find((n) => n.message === 'same error');
    expect(same?.count).toBe(2);
  });

  it('auto-resolves: a success for the same key clears the stored error', () => {
    notifications.record('install failed', 'module:rl:install', 1000);
    expect(snapshot()).toHaveLength(1);
    notifications.resolve('module:rl:install');
    expect(snapshot()).toHaveLength(0);
    expect(get(errorCount)).toBe(0);
  });

  it('resolve with a non-matching or empty key is a no-op', () => {
    notifications.record('install failed', 'module:rl:install', 1000);
    notifications.resolve('module:other:install');
    notifications.resolve(undefined);
    notifications.resolve('');
    expect(snapshot()).toHaveLength(1);
  });

  it('manual dismiss removes one entry by id', () => {
    notifications.record('a', 'k1', 1000);
    notifications.record('b', 'k2', 1000);
    const [first] = snapshot();
    notifications.dismiss(first.id);
    const list = snapshot();
    expect(list).toHaveLength(1);
    expect(list[0].key).toBe('k2');
  });

  it('clearAll empties the inbox', () => {
    notifications.record('a', 'k1', 1000);
    notifications.record('b', 'k2', 1000);
    notifications.clearAll();
    expect(snapshot()).toHaveLength(0);
    expect(get(errorCount)).toBe(0);
  });

  it('errorCount sums dedup counts across entries', () => {
    notifications.record('a', 'k1', 1000);
    notifications.record('a', 'k1', 1100); // k1 count = 2
    notifications.record('b', 'k2', 1200); // k2 count = 1
    expect(get(errorCount)).toBe(3);
  });
});
