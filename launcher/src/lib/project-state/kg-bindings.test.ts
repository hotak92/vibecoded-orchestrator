// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { canRemoveKgBinding, kgBindingRemoveMessage, removeKgBinding } from './kg-bindings';

const ok = { confirm: () => true };

describe('remove a shared/archive KG binding', () => {
  for (const role of ['shared', 'archive']) {
    it(`ACT: confirmed removal of the ${role} binding calls delete_project_kg_binding`, async () => {
      const remove = vi.fn(async () => undefined);
      const out = await removeKgBinding('p1', { role, collection_name: 'Extra_KG' }, { ...ok, remove });
      expect(out).toBe('removed');
      expect(remove).toHaveBeenCalledWith('p1', role);
    });
  }

  it('LEAVE ALONE: a declined confirm never reaches the backend', async () => {
    const remove = vi.fn(async () => undefined);
    const out = await removeKgBinding('p1', { role: 'shared', collection_name: 'X' }, { confirm: () => false, remove });
    expect(out).toBe('cancelled');
    expect(remove).not.toHaveBeenCalled();
  });

  it('LEAVE ALONE: the primary binding is never removable here (owner decision pending)', async () => {
    const remove = vi.fn(async () => undefined);
    const confirm = vi.fn(() => true);
    const out = await removeKgBinding('p1', { role: 'primary', collection_name: 'Main' }, { confirm, remove });
    expect(out).toBe('not_removable');
    expect(confirm).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    expect(canRemoveKgBinding('primary')).toBe(false);
    expect(canRemoveKgBinding('shared')).toBe(true);
  });

  it('a backend failure propagates so the tab can toast it', async () => {
    const remove = vi.fn(async () => {
      throw new Error('db locked');
    });
    await expect(
      removeKgBinding('p1', { role: 'archive', collection_name: 'A' }, { ...ok, remove }),
    ).rejects.toThrow('db locked');
  });

  it('the confirm says the collection itself survives', () => {
    const m = kgBindingRemoveMessage({ role: 'shared', collection_name: 'Extra_KG' });
    expect(m).toContain('"Extra_KG"');
    expect(m).toMatch(/not deleted/);
  });
});

describe('the tab wires it', () => {
  const src = readFileSync(fileURLToPath(new URL('./KgCodegraphTab.svelte', import.meta.url)), 'utf8');
  it('lists bindings with a remove control that runs the confirmed flow', () => {
    expect(src).toContain('removeKgBinding(projectId, b');
    expect(src).toContain("invoke('delete_project_kg_binding', { projectId: pid, role })");
    expect(src).toContain('data-testid="kg-binding-remove"');
  });
});
