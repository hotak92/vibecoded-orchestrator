// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// Pure helpers backing `ModuleConfigTab.svelte`'s v0.2.32 L3 + L7 work.
//
//   * L3 (auto-wrap project picker) — `sectionUsesProjectId` inspects
//     every control's action / on_change / options_source / source /
//     apply_action ActionRef looking for `{{project_id}}`. Sections that
//     reference it get a per-section project picker rendered above their
//     controls, overriding the global `selectedProject` store.
//
//   * L7 (embedding-source placeholder) — `substituteEmbeddingSource`
//     walks an ActionDescriptor's `path` + `body` and replaces every
//     `{{embedding_source_from_project_kg_binding}}` with the active
//     project's resolved text-embedding source (e.g. `"qwen3"`,
//     `"arctic"`, `"openai"`).
//
// Substitution is intentionally client-side for v0.2.32: the alternative
// (adding a placeholder to the Rust dispatcher in
// `commands::module_dispatch::resolve_token`) would require the
// dispatcher to grow a per-project DB lookup, expanding its trust
// surface. Client-side substitution at dispatch time keeps the
// dispatcher unchanged — the wire it sees is already-substituted JSON.
//
// Keep these helpers PURE — no Tauri imports, no Svelte runtime. They
// exist so the renderer logic can be reasoned about (and trivially
// unit-tested once vitest lands; today the launcher has no JS test
// runner configured).

import {
  isActionDescriptor,
  isChainedAction,
  type ActionDescriptor,
  type ActionRef,
  type ChainedActionDescriptor,
  type ConfigControl,
  type ConfigSection,
  type HttpActionDescriptor,
} from '$lib/types/manifest';

/** Literal placeholder token, including the curly braces. */
export const PROJECT_ID_TOKEN = '{{project_id}}';
export const EMBEDDING_SOURCE_TOKEN =
  '{{embedding_source_from_project_kg_binding}}';

/**
 * Collect every ActionRef that a single control might dispatch. Controls
 * carry different action-shaped fields depending on `kind`; this
 * normalises them so the substitution-site scan can be uniform.
 *
 * Returns an array (possibly empty) — never `undefined` — so callers can
 * iterate without null-checks.
 */
function collectControlActionRefs(control: ConfigControl): ActionRef[] {
  const refs: ActionRef[] = [];
  // Discriminated union — TypeScript narrows each branch.
  switch (control.kind) {
    case 'checkbox':
      if (control.on_change) refs.push(control.on_change);
      break;
    case 'multi_select':
      refs.push(control.options_source);
      if (control.on_change) refs.push(control.on_change);
      break;
    case 'button':
      refs.push(control.action);
      break;
    case 'select':
      if (control.on_change) refs.push(control.on_change);
      break;
    case 'text_input':
      if (control.apply_action) refs.push(control.apply_action);
      break;
    case 'number_input':
      if (control.on_change) refs.push(control.on_change);
      break;
    case 'status_display':
      refs.push(control.source);
      break;
    case 'file_picker':
      if (control.on_change) refs.push(control.on_change);
      break;
    // `info` + `link` have no action surface.
    // v0.2.33 (Agent D): the `Unsupported` forward-compat fallback
    // has no recognisable action surface either — the renderer
    // can't know which fields of the raw payload are actions, so
    // we skip it entirely. Worst case: a future control kind that
    // referenced `{{project_id}}` won't trigger the auto-picker
    // on a launcher version that hasn't grown the variant yet.
    // Acceptable — the user will see the Unsupported placeholder
    // and update.
  }
  return refs;
}

/**
 * True iff the ActionRef references the literal `{{project_id}}` token.
 *
 * Policy:
 *   - Legacy string actions (Tauri command names) ALWAYS receive
 *     `projectId` via the renderer's `invoke()` wrapper. Whether the
 *     command actually consumes the arg is internal — but since the
 *     legacy convention is "every action is per-project", legacy strings
 *     count as project-id-dependent for L3.
 *   - ActionDescriptor (kind:"http"): inspect `path` and JSON-serialised
 *     `body`. Any occurrence of `{{project_id}}` (whole-string OR
 *     embedded) flags the section.
 */
export function actionRefUsesProjectId(ref: ActionRef): boolean {
  if (typeof ref === 'string') {
    // Legacy actions: see policy above.
    return true;
  }
  if (!isActionDescriptor(ref)) return false;
  // v0.2.32 (CHAINED_ACTION): a chained_action carries no path/body of
  // its own — recurse into every step + the (rarely-used) polling block.
  if (isChainedAction(ref)) {
    for (const step of ref.steps) {
      if (actionRefUsesProjectId(step as ActionRef)) return true;
    }
    return false;
  }
  // v0.2.33 (Agent D): tauri_command descriptor — inspect the args
  // (analogue of Http's body) for `{{project_id}}`. The command name
  // itself never carries placeholders.
  if (ref.kind === 'tauri_command') {
    if (ref.args !== undefined && ref.args !== null) {
      try {
        const serialised = JSON.stringify(ref.args);
        if (serialised.includes(PROJECT_ID_TOKEN)) return true;
      } catch {
        // Same defensive policy as Http.body — non-serialisable
        // payloads count as non-matching.
      }
    }
    return false;
  }
  // ref is HttpActionDescriptor here.
  if (ref.path && ref.path.includes(PROJECT_ID_TOKEN)) return true;
  if (ref.body !== undefined && ref.body !== null) {
    try {
      const serialised = JSON.stringify(ref.body);
      if (serialised.includes(PROJECT_ID_TOKEN)) return true;
    } catch {
      // Defensive: if body has a non-serialisable shape (circular,
      // BigInt) we treat it as non-matching rather than crashing the
      // renderer. The dispatcher would reject the body too.
    }
  }
  // Http descriptors can also chain via `next_action` — walk the chain
  // so a section whose top-level action doesn't reference project_id
  // but whose next_action does still triggers the picker.
  if (ref.next_action) {
    return actionRefUsesProjectId(ref.next_action as ActionRef);
  }
  return false;
}

/**
 * True iff ANY control in the section references `{{project_id}}` in any
 * of its action surfaces. Drives the L3 per-section picker render.
 *
 * O(controls × actions-per-control) — bounded by the manifest size; no
 * memoisation needed.
 */
export function sectionUsesProjectId(section: ConfigSection): boolean {
  for (const control of section.controls) {
    for (const ref of collectControlActionRefs(control)) {
      if (actionRefUsesProjectId(ref)) return true;
    }
  }
  return false;
}

/**
 * Replace every `{{embedding_source_from_project_kg_binding}}` token
 * inside a string with `embeddingSource`. Returns the input verbatim
 * when no token is present (fast path) so legacy manifests pay zero
 * substitution cost.
 */
function substituteEmbeddingSourceInString(
  s: string,
  embeddingSource: string,
): string {
  if (!s.includes(EMBEDDING_SOURCE_TOKEN)) return s;
  // Plain string-replace-all — the token has no regex meta-chars when
  // taken literally, so this is safe and zero-dependency.
  return s.split(EMBEDDING_SOURCE_TOKEN).join(embeddingSource);
}

/**
 * Walk an arbitrary JSON value and substitute every embedding-source
 * token in every string leaf. Objects + arrays recurse; numbers /
 * booleans / null pass through. Keys are NOT substituted (mirrors the
 * Rust dispatcher's substitute() rule).
 */
function substituteEmbeddingSourceInValue(
  value: unknown,
  embeddingSource: string,
): unknown {
  if (typeof value === 'string') {
    return substituteEmbeddingSourceInString(value, embeddingSource);
  }
  if (Array.isArray(value)) {
    return value.map((v) => substituteEmbeddingSourceInValue(v, embeddingSource));
  }
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = substituteEmbeddingSourceInValue(v, embeddingSource);
    }
    return out;
  }
  // Primitives / null pass through.
  return value;
}

/**
 * Return a clone of `descriptor` with every occurrence of
 * `{{embedding_source_from_project_kg_binding}}` (in `path`, `body`, or
 * nested `next_action`) replaced by `embeddingSource`.
 *
 * `embeddingSource` is the SHORT identifier (e.g. `"qwen3"`, `"arctic"`,
 * `"openai"`) returned by the `get_project_embedding_source` Tauri
 * command — NOT a model id (`"qwen3-embedding:0.6b"`).
 *
 * When `embeddingSource` is the empty string, this returns the
 * descriptor unchanged — better to fail loudly on the dispatcher side
 * with "unknown placeholder" than to substitute an empty string and
 * silently mis-route the action.
 */
export function substituteEmbeddingSource(
  descriptor: ActionDescriptor,
  embeddingSource: string,
): ActionDescriptor {
  if (!embeddingSource) return descriptor;
  // v0.2.32 (CHAINED_ACTION): map across the steps array; the wrapper
  // carries no path/body of its own.
  if (isChainedAction(descriptor)) {
    const out: ChainedActionDescriptor = {
      ...descriptor,
      steps: descriptor.steps.map(
        (step) => substituteEmbeddingSource(step, embeddingSource),
      ),
    };
    return out;
  }
  // v0.2.33 (Agent D): tauri_command descriptor. The command name is
  // never substituted (it's the whitelist key); only the `args`
  // payload walks the value-substitution helper, same as Http.body.
  if (descriptor.kind === 'tauri_command') {
    return {
      ...descriptor,
      args: substituteEmbeddingSourceInValue(descriptor.args, embeddingSource),
    };
  }
  // descriptor is HttpActionDescriptor here.
  const out: HttpActionDescriptor = {
    ...descriptor,
    path: substituteEmbeddingSourceInString(descriptor.path, embeddingSource),
  };
  if (descriptor.body !== undefined && descriptor.body !== null) {
    out.body = substituteEmbeddingSourceInValue(descriptor.body, embeddingSource);
  }
  if (descriptor.next_action) {
    out.next_action = substituteEmbeddingSource(
      descriptor.next_action,
      embeddingSource,
    );
  }
  return out;
}

/**
 * Substitute the embedding-source token inside ANY ActionRef. Legacy
 * string actions are returned verbatim — the substitution happens on
 * the `extraArgs.embedding_source` field that `dispatchAction` already
 * appends to the `invoke()` call when the action is a string.
 *
 * Exposed so the renderer can pre-process descriptors before they
 * reach `module_dispatch_action`.
 */
export function substituteEmbeddingSourceInAction(
  action: ActionRef,
  embeddingSource: string,
): ActionRef {
  if (typeof action === 'string') return action;
  if (!isActionDescriptor(action)) return action;
  return substituteEmbeddingSource(action, embeddingSource);
}
