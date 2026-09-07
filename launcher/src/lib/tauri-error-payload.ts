// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Tolerant parser for the structured JSON payloads the Rust side returns
// as a Tauri `Err` string (`{"event":"orchestrator_update_conflict",...}`,
// `{"kind":"install_conflict",...}`, ...).
//
// Field incident 2026-09-07: `merge_orchestrator_with_upstream` returned the
// `orchestrator_update_conflict` payload and the divergence modal showed
// NOTHING — its local parser required `raw.startsWith('{')`, and the string
// it received did not start with `{` (a leading label / whitespace). The
// merge looked hung. This module is the ONE home for that parsing so every
// site tolerates the same shapes:
//
//   - leading whitespace                       → trimmed
//   - a leading label such as `Error: {...}`   → stripped up to the first `{`
//   - an `Error` instance                      → its `.message` is parsed
//   - a non-JSON string                        → null (caller keeps it raw)
//   - a JSON string whose tag doesn't match    → null
//
// Detection is by tag substring (`"<key>":"<value>"`, spaces tolerated), NOT
// by a `{` prefix, so the presence check can never be defeated by wrapping.

/**
 * Parse `raw` as a JSON object tagged `"<tagKey>": "<tagValue>"`.
 * Returns the typed object or `null` for any other shape. Never throws.
 */
export function parseTaggedErrorPayload<T extends object>(
  raw: unknown,
  tagKey: string,
  tagValue: string,
): T | null {
  const text = raw instanceof Error ? raw.message : raw;
  if (typeof text !== 'string') return null;
  const s = text.trimStart();
  if (!s) return null;
  // Cheap presence check before any JSON.parse — the vast majority of error
  // strings are plain prose and must stay plain prose.
  const tagRe = new RegExp(`"${escapeRe(tagKey)}"\\s*:\\s*"${escapeRe(tagValue)}"`);
  if (!tagRe.test(s)) return null;

  const candidates = [s];
  // Some Tauri runtimes prefix a label (`Error: {...}`); strip to the first
  // `{` and retry with the same object check.
  const braceAt = s.indexOf('{');
  if (braceAt > 0) candidates.push(s.slice(braceAt));

  for (const candidate of candidates) {
    const parsed = tryParseObject(candidate);
    if (parsed && parsed[tagKey] === tagValue) return parsed as T;
  }
  return null;
}

function tryParseObject(s: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // not JSON
  }
  return null;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Human-readable text for a Tauri rejection (Error or string). */
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ---------------------------------------------------------------------------
// Orchestrator-update conflict payload — ONE declaration, shared by the
// divergence modal, the conflict modal, the updater store and the badge's
// "stalled merge" path (`get_pending_conflict_payload` returns this shape).
// ---------------------------------------------------------------------------

/** Mirrors Rust `serialize_orchestrator_conflict_error`. */
export type OrchestratorConflictPayload = {
  event: 'orchestrator_update_conflict';
  operation: 'merge' | 'rebase';
  branch: string;
  conflicted_files: string[];
  git_stderr: string;
};

/** Parse a Tauri Err as the merge/rebase conflict payload; null otherwise. */
export function parseOrchestratorConflictError(raw: unknown): OrchestratorConflictPayload | null {
  return parseTaggedErrorPayload<OrchestratorConflictPayload>(
    raw,
    'event',
    'orchestrator_update_conflict',
  );
}
