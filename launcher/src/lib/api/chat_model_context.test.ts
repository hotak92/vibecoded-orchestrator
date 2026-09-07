// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-11 — unit tests for the chat-model context API wrapper.
//
// Two things are pinned here:
//   1. The WIRE SHAPE of every call — command name and argument names. A
//      renamed Tauri command or a camelCase/snake_case slip is a runtime
//      "command not found" the type checker cannot see, so it gets a test.
//   2. The pane's VALIDATION, which is the client half of the three-layer
//      citation gate (client → Rust validator → SQL CHECK). The rule that
//      matters most: a blank `context_window` must never reach `upsert`.
//
// Pure unit tests: `$lib/tauri` is mocked, no Tauri runtime required.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
}));

import { invoke } from '$lib/tauri';
import {
  deleteChatModelContext,
  describeDelete,
  describeExport,
  describeReseed,
  draftFromRow,
  emptyDraft,
  exportChatModelContext,
  getChatModelContextStatus,
  listChatModelContext,
  reseedChatModelContext,
  upsertChatModelContext,
  validateDraft,
  type ChatModelContextDraft,
} from './chat_model_context';
import type {
  ChatModelContextMutation,
  ChatModelContextRow,
  ExportReport,
} from '$lib/types/chat-model-context';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function makeRow(over: Partial<ChatModelContextRow> = {}): ChatModelContextRow {
  return {
    model_id: 'glm-5.2',
    vendor: 'zai',
    context_window: 1_000_000,
    max_output: 128_000,
    window_1m: true,
    source: 'https://docs.z.ai/guides/llm/glm-5.2',
    source_note: '',
    user_edited: false,
    updated_at: '2026-09-02T18:04:11Z',
    ...over,
  };
}

function makeExport(over: Partial<ExportReport> = {}): ExportReport {
  return {
    ok: true,
    path: '/home/u/.vct/model-gateway/chat_model_context.json',
    path_overridden_by_env: false,
    models: 10,
    generated_at: '2026-09-02T18:04:11Z',
    error: null,
    ...over,
  };
}

function makeMutation(over: Partial<ChatModelContextMutation> = {}): ChatModelContextMutation {
  return {
    row: null,
    deleted: false,
    reseed: null,
    export: makeExport(),
    ...over,
  };
}

function goodDraft(over: Partial<ChatModelContextDraft> = {}): ChatModelContextDraft {
  return {
    model_id: 'glm-5.2',
    vendor: 'zai',
    context_window: '1000000',
    max_output: '128000',
    window_1m: true,
    source: 'https://docs.z.ai/guides/llm/glm-5.2',
    source_note: '',
    ...over,
  };
}

beforeEach(() => {
  mockInvoke.mockReset();
});

describe('command wire shapes', () => {
  it('lists with no arguments', async () => {
    mockInvoke.mockResolvedValueOnce([makeRow()]);
    const rows = await listChatModelContext();
    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_list');
    expect(rows[0].model_id).toBe('glm-5.2');
  });

  it('reads status with no arguments', async () => {
    mockInvoke.mockResolvedValueOnce({ rows: 10 });
    await getChatModelContextStatus();
    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_status');
  });

  it('upserts by passing the row under the `input` argument name', async () => {
    mockInvoke.mockResolvedValueOnce(makeMutation({ row: makeRow() }));
    const parsed = validateDraft(goodDraft());
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;

    await upsertChatModelContext(parsed.input);

    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_upsert', {
      input: {
        model_id: 'glm-5.2',
        vendor: 'zai',
        context_window: 1_000_000,
        max_output: 128_000,
        window_1m: true,
        source: 'https://docs.z.ai/guides/llm/glm-5.2',
        source_note: '',
      },
    });
  });

  it('deletes by camelCase `modelId` (Tauri maps it to the Rust snake_case arg)', async () => {
    mockInvoke.mockResolvedValueOnce(makeMutation({ deleted: true }));
    await deleteChatModelContext('glm-5.2');
    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_delete', {
      modelId: 'glm-5.2',
    });
  });

  it('reseeds and exports with no arguments', async () => {
    mockInvoke.mockResolvedValueOnce(makeMutation());
    await reseedChatModelContext();
    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_reseed');

    mockInvoke.mockResolvedValueOnce(makeExport());
    await exportChatModelContext();
    expect(mockInvoke).toHaveBeenCalledWith('chat_model_context_export');
  });
});

describe('validateDraft — the client half of the citation gate', () => {
  it('accepts a complete row and coerces the numeric fields exactly once', () => {
    const result = validateDraft(goodDraft());
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.input.context_window).toBe(1_000_000);
    expect(result.input.max_output).toBe(128_000);
    expect(typeof result.input.context_window).toBe('number');
  });

  it('REFUSES an uncited row and says why', () => {
    for (const blank of ['', '   ', '\t\n']) {
      const result = validateDraft(goodDraft({ source: blank }));
      expect(result.ok).toBe(false);
      if (result.ok) return;
      const err = result.errors.find((e) => e.field === 'source');
      expect(err?.message).toContain('uncited window is a guess');
    }
  });

  it('REFUSES a blank context window rather than sending 0', () => {
    const result = validateDraft(goodDraft({ context_window: '' }));
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors.map((e) => e.field)).toContain('context_window');
  });

  it('refuses non-integer, zero and negative token counts', () => {
    for (const bad of ['0', '-1', '1e6', '1.5', '1 000', 'lots', ' ']) {
      const result = validateDraft(goodDraft({ context_window: bad }));
      expect(result.ok, `"${bad}" must be refused`).toBe(false);
    }
  });

  it('refuses a blank model id and a blank vendor', () => {
    const result = validateDraft(goodDraft({ model_id: '  ', vendor: '' }));
    expect(result.ok).toBe(false);
    if (result.ok) return;
    const fields = result.errors.map((e) => e.field);
    expect(fields).toContain('model_id');
    expect(fields).toContain('vendor');
  });

  it('reports EVERY problem at once, not just the first', () => {
    const result = validateDraft(
      emptyDraft() as ChatModelContextDraft,
    );
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors.map((e) => e.field).sort()).toEqual(
      ['context_window', 'max_output', 'model_id', 'source', 'vendor'].sort(),
    );
  });

  it('trims, so a padded id is not stored as a different key', () => {
    const result = validateDraft(
      goodDraft({ model_id: '  glm-5.2 ', source: ' https://docs.z.ai/x  ', source_note: ' n ' }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.input.model_id).toBe('glm-5.2');
    expect(result.input.source).toBe('https://docs.z.ai/x');
    expect(result.input.source_note).toBe('n');
  });

  it('does NOT require the citation to be a URL', () => {
    // The gateway's reader only requires non-empty; a pane stricter than the
    // file format would refuse rows the format accepts.
    const result = validateDraft(goodDraft({ source: 'vendor PDF, page 3' }));
    expect(result.ok).toBe(true);
  });

  it('keeps an empty source_note empty rather than inventing one', () => {
    const result = validateDraft(goodDraft({ source_note: '' }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.input.source_note).toBe('');
  });
});

describe('draft helpers', () => {
  it('round-trips a row into an editable draft without losing a field', () => {
    const row = makeRow({ source_note: 'cited from the glm-4.5 card', user_edited: true });
    const draft = draftFromRow(row);
    expect(draft.context_window).toBe('1000000');
    expect(draft.source_note).toBe('cited from the glm-4.5 card');

    const result = validateDraft(draft);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.input).toEqual({
      model_id: row.model_id,
      vendor: row.vendor,
      context_window: row.context_window,
      max_output: row.max_output,
      window_1m: row.window_1m,
      source: row.source,
      source_note: row.source_note,
    });
  });

  it('starts a new row 1M-off, so a new entry never claims 1M by default', () => {
    expect(emptyDraft().window_1m).toBe(false);
  });
});

describe('summaries', () => {
  it('names the preserved user edits, which is the invisible half of reseed', () => {
    expect(
      describeReseed({ inserted: 1, updated: 2, unchanged: 3, preserved_user_edits: 1 }),
    ).toBe('1 added, 2 refreshed, 3 already current, 1 of your edit kept.');
    expect(
      describeReseed({ inserted: 0, updated: 0, unchanged: 0, preserved_user_edits: 2 }),
    ).toBe('2 of your edits kept.');
    expect(
      describeReseed({ inserted: 0, updated: 0, unchanged: 0, preserved_user_edits: 0 }),
    ).toBe('Nothing to reseed.');
  });

  it('surfaces an export failure verbatim instead of a success sentence', () => {
    const failed = makeExport({
      ok: false,
      error: 'could not write /x: denied. The table is saved, but the model gateway…',
    });
    expect(describeExport(failed)).toContain('could not write /x');
    expect(describeExport(makeExport({ models: 1 }))).toBe(
      'Exported 1 model to /home/u/.vct/model-gateway/chat_model_context.json ' +
        'at 2026-09-02T18:04:11Z.',
    );
  });

  it('carries the stamp actually written, and omits it when there is none', () => {
    expect(describeExport(makeExport())).toContain('at 2026-09-02T18:04:11Z');
    expect(describeExport(makeExport({ generated_at: null }))).not.toContain(' at ');
  });

  it('distinguishes a real delete from an already-gone row', () => {
    // A double-click on Remove must not look like an error and must not
    // claim to have deleted the row twice.
    expect(describeDelete('glm-5.2', true)).toBe('Removed glm-5.2.');
    expect(describeDelete('glm-5.2', false)).toBe('glm-5.2 was already gone.');
  });
});
