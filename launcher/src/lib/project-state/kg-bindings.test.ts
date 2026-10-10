// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { canRemoveKgBinding, codegraphBindingSaveReq, codegraphSelectedModel, kgBindingRemoveMessage, removeKgBinding } from './kg-bindings';

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

describe('v0.2.102 W6-B2 — the Codegraph-tab save keeps the record; the choice is a target', () => {
  const built = {
    project_id: 'p1', collection_prefix: 'Acme', embedding_model: 'codesage-large-v2',
    embedding_dim: 2048, last_analyzed_commit: 'abc', last_analyzed_at: 123, enabled: true,
    config: { code_model_fill: { target: 'x' } }, updated_at: 1,
  };

  it('an unchanged model keeps record, provenance and config (red on 2cc19a9c: config {} + NULLs)', () => {
    const req = codegraphBindingSaveReq(built, {
      collection_prefix: 'Acme', target_model: 'codesage-large-v2', enabled: false,
    });
    expect(req).toEqual({
      collection_prefix: 'Acme', embedding_model: 'codesage-large-v2', embedding_dim: 2048,
      last_analyzed_commit: 'abc', last_analyzed_at: 123, enabled: false,
      config: { code_model_fill: { target: 'x' } },
    });
  });

  it('a different model becomes config.code_model_target — the record is NOT overwritten', () => {
    const req = codegraphBindingSaveReq(built, {
      collection_prefix: 'Acme', target_model: 'qwen3-embedding:0.6b', enabled: true,
    });
    expect(req.embedding_model).toBe('codesage-large-v2');
    expect(req.embedding_dim).toBe(2048);
    expect(req.config).toEqual({ code_model_fill: { target: 'x' }, code_model_target: 'qwen3-embedding:0.6b' });
    const back = codegraphBindingSaveReq({ ...built, config: req.config as Record<string, unknown> }, {
      collection_prefix: 'Acme', target_model: 'codesage-large-v2', enabled: true,
    });
    expect(back.config).not.toHaveProperty('code_model_target');
  });

  it('the tab shows the target when set, else the record', () => {
    expect(codegraphSelectedModel(built)).toBe('codesage-large-v2');
    expect(codegraphSelectedModel({ ...built, config: { code_model_target: 'jina' } })).toBe('jina');
  });

  it('the tab sends exactly that request and no longer enriches the code slot', () => {
    const tab = readFileSync(fileURLToPath(new URL('./KgCodegraphTab.svelte', import.meta.url)), 'utf8');
    expect(tab).toMatch(/req: codegraphBindingSaveReq\(snapshot\?\.codegraph_binding, \{/);
    const saveCg = tab.slice(tab.indexOf('async function saveCg()'), tab.indexOf('async function saveCg()') + 2500);
    expect(saveCg).not.toMatch(/config: \{\},/);
    expect(saveCg).not.toContain('enrichmentTarget');
  });
});
