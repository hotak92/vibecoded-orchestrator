// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.103 (review R8 M1): the Settings tab's "Project env vars (notes
// only)" scratch list as pure logic. `addEnv` used to append blindly, so
// typing the same key twice created two rows keyed `e.key` — the SAME
// `each_key_duplicate` throw that broke the install-log tail, one a user
// can trigger with two clicks. Adding now REFUSES a duplicate key with a
// message the component surfaces; the rows themselves stay component
// state (scratch space, never persisted — see the panel's own disclaimer).

/** One scratch row. `key` is stored uppercased + trimmed. */
export interface EnvNote {
  key: string;
  value: string;
}

/** The stored form of a raw key: trimmed, uppercased. */
export function normalizeEnvKey(raw: string): string {
  return raw.trim().toUpperCase();
}

export interface AddEnvNoteResult {
  /** The new list on success; the UNCHANGED list on refusal. */
  entries: EnvNote[];
  /** Non-null when the add was refused — a one-line, user-facing reason. */
  refused: string | null;
}

/** Add one note. An empty/whitespace key is the caller's no-op (unchanged
 *  list, no refusal). A key that already exists is REFUSED: the row is
 *  already there to edit, and a second row would duplicate the each key. */
export function addEnvNote(
  entries: EnvNote[],
  rawKey: string,
  value: string,
): AddEnvNoteResult {
  const key = normalizeEnvKey(rawKey);
  if (key === '') return { entries, refused: null };
  if (entries.some((e) => e.key === key)) {
    return {
      entries,
      refused: `${key} is already in the list — edit its existing row instead of adding it twice.`,
    };
  }
  return { entries: [...entries, { key, value }], refused: null };
}
