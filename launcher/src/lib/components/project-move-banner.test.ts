// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  buildMoveBannerItems,
  finishMoveCommand,
  shellQuote,
  type LiveProjectMove,
} from './project-move-banner-logic';

const row = (over: Partial<LiveProjectMove> = {}): LiveProjectMove => ({
  id: 'm1',
  project_id: 'p1',
  src: '/old/alpha',
  dst: '/new/alpha',
  status: 'running',
  error: null,
  started_at: 1,
  flipped_at: null,
  finished_at: null,
  ...over,
});
const name = () => 'Alpha';

describe('interrupted-move banner', () => {
  it('no live moves: no banner', () => {
    expect(buildMoveBannerItems([], name)).toEqual([]);
  });

  it('running = interrupted BEFORE the flip: says it was not moved and offers no command', () => {
    const [it1] = buildMoveBannerItems([row()], name);
    expect(it1.kind).toBe('not_moved');
    expect(it1.title).toMatch(/was not moved/);
    expect(it1.detail).toContain('/old/alpha');
    expect(it1.detail).toMatch(/Nothing was changed/);
    expect(it1.command).toBeNull();
  });

  it('flipped = interrupted AFTER the flip: says where it lives now and gives the finish command', () => {
    const [it1] = buildMoveBannerItems([row({ status: 'flipped', flipped_at: 2 })], name);
    expect(it1.kind).toBe('finish_owed');
    expect(it1.title).toMatch(/needs finishing/);
    expect(it1.detail).toContain('/new/alpha');
    expect(it1.command).toBe("vco project move --verify --folder '/new/alpha'");
  });

  it('completed / failed rows are history, not interruptions', () => {
    expect(
      buildMoveBannerItems([row({ status: 'completed' }), row({ id: 'm2', status: 'failed' })], name),
    ).toEqual([]);
  });

  it('a dismissed move stays dismissed; others remain', () => {
    const items = buildMoveBannerItems([row(), row({ id: 'm2', status: 'flipped' })], name, new Set(['m1']));
    expect(items.map((i) => i.moveId)).toEqual(['m2']);
  });

  it('the printed command is safe for a path with a quote or space', () => {
    expect(shellQuote("/a b/it's")).toBe("'/a b/it'\\''s'");
    expect(finishMoveCommand('/x y')).toBe("vco project move --verify --folder '/x y'");
  });
});

describe('wiring', () => {
  const read = (rel: string) =>
    readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
  it('the banner reads the live-moves command and the shell mounts it', () => {
    expect(read('./ProjectMoveBanner.svelte')).toContain("invoke<LiveProjectMove[]>('list_live_project_moves_v2')");
    expect(read('../../routes/+layout.svelte')).toContain('<ProjectMoveBanner />');
  });
});
