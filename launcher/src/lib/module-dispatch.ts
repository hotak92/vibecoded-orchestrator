// Shared dispatcher for module-contributed GUI controls.
//
// v0.2.26 introduced the declarative HTTP-action contract: an `ActionRef`
// is either a legacy Tauri command name (string) OR a structured
// `ActionDescriptor` (object). The Rust side's generic
// `module_dispatch_action` command executes descriptors without any
// per-module Rust code.
//
// This module centralises the dispatch decision so the renderer and
// individual control components (`StatusDisplayControl`, …) can both
// invoke an `ActionRef` without duplicating the type check.

import { invoke } from '$lib/tauri';
import { isActionDescriptor, type ActionRef } from '$lib/types/manifest';

export interface DispatchContext {
  moduleId: string;
  projectId: string;
}

/**
 * Invoke an `ActionRef`. Returns the response (descriptor) or the
 * legacy command result.
 *
 *  - If `action` is a string → `invoke(action, { moduleId, projectId, value })`
 *    (legacy path; preserves the v0.2.20-v0.2.25 wire contract).
 *  - If `action` is an `ActionDescriptor` → `invoke('module_dispatch_action', {
 *    moduleId, projectId, action, value })` (v0.2.26 declarative path).
 *
 * For polling actions the dispatcher returns the *kick* response
 * immediately; the renderer or control component is responsible for
 * subscribing to `polling.progress_event` and `polling.failed_event` to
 * observe progress.
 *
 * Caller is responsible for catching errors (the helper rethrows so the
 * caller can show a toast or set an inline error).
 */
export async function dispatchAction<T = unknown>(
  ctx: DispatchContext,
  action: ActionRef,
  value: unknown = null,
): Promise<T> {
  if (isActionDescriptor(action)) {
    return invoke<T>('module_dispatch_action', {
      moduleId: ctx.moduleId,
      projectId: ctx.projectId,
      action,
      value,
    });
  }
  // Legacy form — preserve the existing argument shape (moduleId +
  // projectId + value). Some legacy commands ignore `value`; that's OK,
  // Tauri's arg deserialiser only consumes the keys it declares.
  return invoke<T>(action, {
    moduleId: ctx.moduleId,
    projectId: ctx.projectId,
    value,
  });
}

/**
 * Substitute `{{field}}` placeholders in a template with values from a
 * response object's top-level fields. Used by `StatusDisplayControl`.
 *
 *  - Missing fields render as `""` (empty) — matches the Rust-side
 *    behaviour and avoids `{{undefined}}` leaks.
 *  - Non-string values (numbers, booleans) are coerced via `String()`.
 *  - Objects / arrays are JSON-stringified (defensive — most callers
 *    should template scalar fields).
 *  - The template is treated as plain text; the renderer inserts the
 *    output as a text node so XSS via the response payload is impossible.
 */
export function renderTemplate(
  template: string,
  data: Record<string, unknown> | null | undefined,
): string {
  if (!data) return template.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, '');
  return template.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_, key: string) => {
    const v = (data as Record<string, unknown>)[key];
    if (v === undefined || v === null) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try {
      return JSON.stringify(v);
    } catch {
      return '';
    }
  });
}
