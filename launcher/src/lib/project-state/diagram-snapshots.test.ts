// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { deleteSnapshotWithConfirm, snapshotDeleteMessage } from './diagram-snapshots';
import type { DiagramSnapshotRow } from '$lib/types/project-state';

const snap = (over: Partial<DiagramSnapshotRow> = {}) =>
  ({ id: 42, label: 'before refactor', trigger: 'manual', created_at: 1, ...over }) as DiagramSnapshotRow;

describe('per-snapshot delete (destructive: act AND leave-alone)', () => {
  it('ACT: a confirmed delete calls delete_diagram_snapshot with the snapshot id', async () => {
    const remove = vi.fn(async () => undefined);
    const out = await deleteSnapshotWithConfirm(snap(), 'today', { confirm: () => true, remove });
    expect(out).toBe('deleted');
    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith(42);
  });

  it('LEAVE ALONE: a declined confirm never reaches the backend', async () => {
    const remove = vi.fn(async () => undefined);
    const out = await deleteSnapshotWithConfirm(snap(), 'today', { confirm: () => false, remove });
    expect(out).toBe('cancelled');
    expect(remove).not.toHaveBeenCalled();
  });

  it('a backend failure propagates so the tab can toast it', async () => {
    const remove = vi.fn(async () => {
      throw new Error('db locked');
    });
    await expect(
      deleteSnapshotWithConfirm(snap(), 'today', { confirm: () => true, remove }),
    ).rejects.toThrow('db locked');
  });

  it('the confirm names the snapshot and says the file is untouched but the snapshot is gone', () => {
    const m = snapshotDeleteMessage(snap(), '1 Oct');
    expect(m).toContain('"before refactor"');
    expect(m).toContain('1 Oct');
    expect(m).toMatch(/file itself is not touched/);
    expect(m).toMatch(/cannot be recovered/);
    expect(snapshotDeleteMessage(snap({ label: null }), 'x')).toContain('the manual snapshot');
  });
});

describe('the timeline wires it', () => {
  const src = readFileSync(fileURLToPath(new URL('./DiagramsTab.svelte', import.meta.url)), 'utf8');
  it('each chip has a delete control that runs the confirmed flow', () => {
    expect(src).toContain('deleteSnapshotWithConfirm(snap');
    expect(src).toContain("invoke('delete_diagram_snapshot', { snapshotId: id })");
    expect(src).toContain('data-testid="diagrams-snap-delete"');
  });
});
