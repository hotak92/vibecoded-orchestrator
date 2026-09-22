// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-11 — thin API wrapper over the six Tauri commands in
// `launcher/src-tauri/src/commands/chat_model_context.rs`, plus the row
// validation the pane runs BEFORE it calls them.
//
// Centralising the `invoke()` calls here keeps the pane testable without
// mocking `@tauri-apps/api/core`, and gives the validation a home that a
// `vitest` run can reach (the pane itself is Svelte; this project's vitest
// setup is a pure-node one with no component renderer).
//
// ## Why validate on the client at all, when Rust validates too
//
// Three layers, and none of them is redundant:
//   * HERE — turns a blank field into a sentence next to the input, before a
//     round trip. It is the only layer that can point at the field.
//   * Rust (`ChatModelContextInput::validated`) — the real gate; a future
//     caller that skips this file still cannot write a bad row.
//   * SQL `CHECK` — the backstop; nothing at all can write a bad row, not
//     even a hand-run `UPDATE`.
// Dropping the first makes the UI worse; dropping either of the others makes
// the DATA wrong.

import { invoke } from '$lib/tauri';
import type {
  ChatModelContextInput,
  ChatModelContextMutation,
  ChatModelContextRow,
  ChatModelContextStatus,
  ExportReport,
} from '$lib/types/chat-model-context';

/** All rows, ordered by `model_id`. */
export async function listChatModelContext(): Promise<ChatModelContextRow[]> {
  return invoke<ChatModelContextRow[]>('chat_model_context_list');
}

/** Header status: export path, what the gateway currently sees, seed state. */
export async function getChatModelContextStatus(): Promise<ChatModelContextStatus> {
  return invoke<ChatModelContextStatus>('chat_model_context_status');
}

/**
 * Insert or update one row. The backend marks it `user_edited`, which is what
 * protects it from the next "Reseed from shipped defaults".
 */
export async function upsertChatModelContext(
  input: ChatModelContextInput,
): Promise<ChatModelContextMutation> {
  return invoke<ChatModelContextMutation>('chat_model_context_upsert', { input });
}

export async function deleteChatModelContext(
  modelId: string,
): Promise<ChatModelContextMutation> {
  return invoke<ChatModelContextMutation>('chat_model_context_delete', { modelId });
}

/** Re-apply the shipped rows. Never touches a row the user edited. */
export async function reseedChatModelContext(): Promise<ChatModelContextMutation> {
  return invoke<ChatModelContextMutation>('chat_model_context_reseed');
}

/** Force an export without changing anything. */
export async function exportChatModelContext(): Promise<ExportReport> {
  return invoke<ExportReport>('chat_model_context_export');
}

// ─── Client-side validation ──────────────────────────────────────────────

/** A field name the pane can highlight, plus the message to show. */
export interface FieldError {
  field: keyof ChatModelContextInput;
  message: string;
}

/**
 * The draft a row editor holds. Numbers are strings while the user is typing
 * — an `<input type="number">` yields `''` for an empty box, and coercing
 * that to `0` early is how a blank field becomes a silently-zero window.
 */
export interface ChatModelContextDraft {
  model_id: string;
  vendor: string;
  context_window: string;
  max_output: string;
  window_1m: boolean;
  source: string;
  source_note: string;
}

export function draftFromRow(row: ChatModelContextRow): ChatModelContextDraft {
  return {
    model_id: row.model_id,
    vendor: row.vendor,
    context_window: String(row.context_window),
    // 0 = UNSTATED (the vendor publishes no figure) — the editor shows that
    // as a BLANK field, never as a "0" that reads like a real token count.
    max_output: row.max_output > 0 ? String(row.max_output) : '',
    window_1m: row.window_1m,
    source: row.source,
    source_note: row.source_note,
  };
}

export function emptyDraft(): ChatModelContextDraft {
  return {
    model_id: '',
    vendor: '',
    context_window: '',
    max_output: '',
    window_1m: false,
    source: '',
    source_note: '',
  };
}

/** Parse a token-count field. ONE home for both numeric fields of this pane
 *  (v0.2.96 D-10: they had drifted into two near-identical parsers differing
 *  only in the zero rule, which is the one thing a reader must not have to
 *  diff two functions to learn).
 *
 *  Shared in every case: trimmed, digits-only (`\d+` never matches a minus
 *  sign or a decimal point, so negatives and fractions are rejected without
 *  a second rule), and safe-integer.
 *
 *  `allowZero` is the ONLY axis:
 *   * `false` — CONTEXT WINDOW. Blank and 0 are refused; the DB's
 *     `context_window > 0` CHECK would refuse them anyway, later and less
 *     helpfully.
 *   * `true` — MAX OUTPUT, where blank or `0` means UNSTATED (blank
 *     normalises to the 0 marker). See `parseMaxOutputTokens` below for the
 *     cross-language contract that makes 0 the honest value. */
function parseTokenField(raw: string, opts: { allowZero: boolean }): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return opts.allowZero ? 0 : null;
  if (!/^\d+$/.test(trimmed)) return null;
  const n = Number(trimmed);
  if (!Number.isSafeInteger(n)) return null;
  return opts.allowZero || n > 0 ? n : null;
}

/** Parse the CONTEXT-WINDOW field: strictly positive, never unstated. An
 *  uncited-or-absent window is exactly what this table exists to prevent a
 *  client from acting on, so there is no marker value for "don't know". */
function parseTokens(raw: string): number | null {
  return parseTokenField(raw, { allowZero: false });
}

/** Parse the MAX-OUTPUT field, where blank or `0` means UNSTATED.
 *
 *  v0.2.96 cross-language contract: the vendor's docs may publish no
 *  per-model max-output figure (the shipped qwen Token-Plan rows store 0),
 *  and inventing a plausible number is forbidden by the same no-guessed-
 *  numbers rule that makes the citation mandatory. This mirrors the Rust
 *  gate (`ChatModelContextInput::validated` accepts 0, refuses negatives —
 *  backed by the SQL CHECK from migration 046) and the Python gateway's
 *  reader (`catalog::_positive` folds 0 to None, so an unstated row never
 *  publishes a token count). Negative and malformed stay rejected: `\d+`
 *  never matches a minus sign.
 *
 *  MUST MATCH — the `0 = UNSTATED` rule has FOUR homes with no shared
 *  runtime, so each names the other three (v0.2.96 D-3):
 *    1. SQL    — `CHECK (max_output >= 0)`, launcher/src-tauri/
 *                vct-launcher-core/src/db/migrations/046_chat_model_context_max_output_unstated.sql
 *    2. Rust   — `ChatModelContextInput::validated`, launcher/src-tauri/
 *                vct-launcher-core/src/db/chat_model_context.rs
 *    3. TS     — here.
 *    4. Python — `catalog.py::_positive`,
 *                claude_mcp_servers/model_router/catalog.py */
function parseMaxOutputTokens(raw: string): number | null {
  return parseTokenField(raw, { allowZero: true });
}

/**
 * Validate a draft and, when it is good, produce the exact payload the Tauri
 * command expects.
 *
 * The rules match `ChatModelContextInput::validated` on the Rust side — same
 * fields, same "non-empty after trimming" rule for `source`. Notably NOT
 * "source must be a URL": the gateway's reader applies exactly the non-empty
 * rule, and refusing something the file format accepts (a vendor PDF, an
 * internal doc reference) would make this pane stricter than the data.
 */
export function validateDraft(
  draft: ChatModelContextDraft,
): { ok: true; input: ChatModelContextInput } | { ok: false; errors: FieldError[] } {
  const errors: FieldError[] = [];

  const modelId = draft.model_id.trim();
  if (modelId === '') {
    errors.push({
      field: 'model_id',
      message: 'Enter the full model id, e.g. glm-5.2 — never a family pattern.',
    });
  }

  const vendor = draft.vendor.trim();
  if (vendor === '') {
    errors.push({ field: 'vendor', message: 'Enter the vendor id, e.g. zai.' });
  }

  const source = draft.source.trim();
  if (source === '') {
    errors.push({
      field: 'source',
      message:
        'Cite where these numbers come from. An uncited window is a guess, ' +
        'and Claude Code would act on it.',
    });
  }

  const contextWindow = parseTokens(draft.context_window);
  if (contextWindow === null) {
    errors.push({
      field: 'context_window',
      message: 'Enter the context window as a whole number of tokens, e.g. 1000000.',
    });
  }

  const maxOutput = parseMaxOutputTokens(draft.max_output);
  if (maxOutput === null) {
    errors.push({
      field: 'max_output',
      message:
        'Enter the max output as a whole number of tokens, e.g. 128000 — ' +
        'or leave it blank when the vendor does not publish a figure.',
    });
  }

  if (errors.length > 0) return { ok: false, errors };

  return {
    ok: true,
    input: {
      model_id: modelId,
      vendor,
      // Non-null by construction: `errors` was empty, so both parsed.
      context_window: contextWindow as number,
      max_output: maxOutput as number,
      window_1m: draft.window_1m,
      source,
      source_note: draft.source_note.trim(),
    },
  };
}

/**
 * One-line summary of a reseed, for a toast.
 *
 * Names the preserved edits explicitly, because "nothing happened to your
 * row" is the part of the guarantee a user cannot otherwise see.
 */
export function describeReseed(o: {
  inserted: number;
  updated: number;
  unchanged: number;
  preserved_user_edits: number;
}): string {
  const parts: string[] = [];
  if (o.inserted > 0) parts.push(`${o.inserted} added`);
  if (o.updated > 0) parts.push(`${o.updated} refreshed`);
  if (o.unchanged > 0) parts.push(`${o.unchanged} already current`);
  if (o.preserved_user_edits > 0) {
    parts.push(
      `${o.preserved_user_edits} of your edit${o.preserved_user_edits === 1 ? '' : 's'} kept`,
    );
  }
  return parts.length > 0 ? parts.join(', ') + '.' : 'Nothing to reseed.';
}

/**
 * One-line summary of an export, for a toast or the header.
 *
 * A FAILED export is not decoration: the row is saved but the model gateway
 * is still serving the old file, and the user has to know that.
 *
 * The success line carries `generated_at` — the stamp actually written into
 * the file — so the user can match what the toast said against what the
 * header shows after the next refresh, and against what the gateway reports
 * as its `context_table_path`.
 */
export function describeExport(report: ExportReport): string {
  if (!report.ok) {
    return report.error ?? `Could not write ${report.path}.`;
  }
  const stamp = report.generated_at ? ` at ${report.generated_at}` : '';
  return `Exported ${report.models} model${report.models === 1 ? '' : 's'} to ${report.path}${stamp}.`;
}

/**
 * One-line summary of a delete.
 *
 * "Already gone" is a distinct outcome from "removed", not a failure: a
 * double-click on Remove must not look like an error, and must not claim to
 * have deleted something twice.
 */
export function describeDelete(modelId: string, deleted: boolean): string {
  return deleted ? `Removed ${modelId}.` : `${modelId} was already gone.`;
}
