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

// ─── v0.2.91 decision #24 — the legacy path's security gate ──────────────
//
// A legacy `ActionRef` is a COMMAND NAME supplied by the module's manifest,
// which is third-party content. Until now the renderer passed it straight to
// `invoke()`, so a malformed or hostile manifest naming `delete_project_v2`
// (or any of the ~378 registered commands) was dispatched with the caller's
// full privileges. The declarative sibling has always been gated by
// `module_dispatch.rs::is_whitelisted_manifest_command`; this is the same
// gate for the string form.
//
// The policy lives in Rust and ONLY in Rust. This module asks; it does not
// keep a copy of the list, and it does not compose the refusal message — a
// second copy of a security policy is a second copy that drifts.

/**
 * Per-name memo of the backend's verdict. The allowlist is a compile-time
 * constant in the launcher binary, so an ALLOWLIST verdict cannot change
 * while the process lives; the cache turns a per-dispatch round trip into a
 * per-name one. Stores the refusal MESSAGE (not a boolean) so a repeat
 * refusal is as informative as the first.
 *
 * Only POLICY refusals are memoized here (wave-5 review NIT-6) — see
 * `isPolicyRefusal` below. A transient failure (IPC hiccup, backend
 * temporarily unreachable) is not a verdict about the allowlist at all, and
 * caching it identically to a real refusal would leave a first-party
 * control permanently blocked until the page reloads, even after the
 * transient condition clears. Fail-closed is still the right direction for
 * an unmemoized transient failure — it just isn't remembered.
 */
const legacyVerdictCache = new Map<string, string | null>();

/** Test seam — lets the vitest suite start from a clean memo. */
export function _resetLegacyVerdictCacheForTests(): void {
  legacyVerdictCache.clear();
}

/**
 * True when `message` is the backend's ALLOWLIST refusal shape — the
 * `module_manifest_command_allowed` denial text
 * (`module_dispatch.rs::module_manifest_command_allowed`) always contains
 * "not whitelisted" verbatim. Anything else (network/IPC failure, a
 * malformed response, the backend being mid-restart) is a transient
 * condition, not a verdict about whether `command` is allowed, and must not
 * be memoized as one.
 */
function isPolicyRefusal(message: string): boolean {
  return message.includes('not whitelisted');
}

/**
 * Throw unless the backend allows `command` as a manifest-dispatched name.
 *
 * Called by every renderer path that forwards a legacy `ActionRef` string:
 * this module's `dispatchAction`, and `ModuleConfigTab.svelte`'s local
 * `dispatchAction` (which adds `embeddingSource` + `extraArgs` and so cannot
 * simply delegate here).
 *
 * The thrown message is the backend's, verbatim. If the query itself fails
 * (IPC unavailable, backend error) the dispatch is REFUSED rather than
 * allowed: a gate that opens when it cannot check is not a gate. Outside the
 * Tauri runtime `invoke` already rejects, so browser-mode previews — which
 * cannot dispatch anything anyway — fail here first, with a clearer reason.
 * A transient failure of this kind is refused EVERY TIME it happens, but —
 * unlike a real allowlist refusal — is never memoized (`isPolicyRefusal`),
 * so a first-party control is not left permanently blocked by one IPC
 * hiccup.
 */
export async function assertLegacyActionAllowed(command: string): Promise<void> {
  const cached = legacyVerdictCache.get(command);
  if (cached !== undefined) {
    if (cached === null) return;
    throw new Error(cached);
  }
  try {
    await invoke<null>('module_manifest_command_allowed', { command });
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    if (isPolicyRefusal(message)) {
      legacyVerdictCache.set(command, message);
    }
    // Transient failures are rethrown but NOT memoized — see the cache's
    // doc comment. The caller (and a retried dispatch) sees the failure
    // every time until the backend actually answers the allowlist query.
    throw new Error(message);
  }
  legacyVerdictCache.set(command, null);
}

/**
 * Invoke an `ActionRef`. Returns the response (descriptor) or the
 * legacy command result.
 *
 *  - If `action` is a string → `invoke(action, { moduleId, projectId, value })`
 *    (legacy path; preserves the v0.2.20-v0.2.25 wire contract).
 *  - If `action` is an `ActionDescriptor` → `invoke('module_dispatch_action', {
 *    moduleId, projectId, action, value, siblingValues? })` (v0.2.26
 *    declarative path).
 *
 * `siblingValues` is an optional snapshot of other controls' current
 * values keyed by control id. The Rust dispatcher feeds it to its
 * `{{control:<id>}}` substitution resolver. When omitted, the
 * dispatcher falls back to reading from `module_settings` — fine for
 * widgets that own a single control or for cross-tab references.
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
  siblingValues?: Record<string, unknown>,
): Promise<T> {
  if (isActionDescriptor(action)) {
    return invoke<T>('module_dispatch_action', {
      moduleId: ctx.moduleId,
      projectId: ctx.projectId,
      action,
      value,
      siblingValues: siblingValues ?? null,
    });
  }
  // Legacy form — preserve the existing argument shape (moduleId +
  // projectId + value). Some legacy commands ignore `value`; that's OK,
  // Tauri's arg deserialiser only consumes the keys it declares.
  //
  // v0.2.91 decision #24: `action` here is a manifest-supplied command
  // NAME. Gate it against the backend allowlist before invoking (throws on
  // refusal; the caller's existing catch surfaces it).
  await assertLegacyActionAllowed(action);
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
