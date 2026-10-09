// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 (W5-B): the decisions behind the Preferences → Storage
// "Migrate volume data" panel. Both migration commands (`migrate_to_named_
// volume`, `migrate_to_bind_path`) COPY with `cp -a` and never delete the
// source — the modal's copy states exactly that, and this module is the
// one home of that sentence so the component and the tests cannot drift.
// The panel NEVER auto-runs: the confirm modal is the only path to the
// invoke, and every arm here is pure.

export type MigrationDirection = 'to_named' | 'to_bind';

/** The three canonical compose volume names (storage_ux::LOGICAL_SERVICES'
 *  volumes — the only names the migration commands accept). */
export const CANONICAL_VOLUMES = ['weaviate_data', 'ollama_data', 'code_embed_cache'] as const;

export type CanonicalVolume = (typeof CANONICAL_VOLUMES)[number];

/** Mirrors `commands::storage_ux::MigrationOutcome`. */
export interface MigrationOutcome {
  success: boolean;
  bytes_copied: number;
  source: string;
  target: string;
  message: string;
  deferral_emitted: boolean;
}

/** The invoke argument payload for a direction. `null` when the inputs do
 *  not validate (see `validationError`). */
export function migrationArgs(
  direction: MigrationDirection,
  volume: string,
  path: string,
): Record<string, string> | null {
  if (validationError(direction, volume, path) !== null) return null;
  return direction === 'to_named'
    ? { sourceBindPath: path.trim(), targetNamedVolume: volume.trim() }
    : { sourceNamedVolume: volume.trim(), targetBindPath: path.trim() };
}

/** Why the Migrate button cannot run yet; null when it can. */
export function validationError(
  direction: MigrationDirection,
  volume: string,
  path: string,
): string | null {
  if (!volume.trim()) return 'Pick a volume.';
  if (!path.trim()) {
    return direction === 'to_named'
      ? 'Enter the bind-path folder to copy FROM.'
      : 'Enter the folder to copy INTO.';
  }
  if (!path.trim().startsWith('/') && !/^[A-Za-z]:[\\/]/.test(path.trim())) {
    return 'The path must be absolute.';
  }
  return null;
}

/** The confirm modal's copy. States WHAT moves WHERE and that NOTHING is
 *  deleted — the two facts a destructive-adjacent confirm must carry. */
export function modalCopy(
  direction: MigrationDirection,
  volume: string,
  path: string,
): { title: string; body: string } {
  const from = direction === 'to_named' ? path.trim() : `named volume ${volume.trim()}`;
  const into = direction === 'to_named' ? `named volume ${volume.trim()}` : path.trim();
  return {
    title: 'Migrate volume data?',
    body:
      `Copy the current contents of ${from} into ${into} (a full copy; existing ` +
      'files at the destination are overwritten by same-named ones). ' +
      'Nothing is deleted — the source stays exactly where it is, and you ' +
      'can re-run or undo this by migrating the other way. Stop the ' +
      'containers that use this volume before continuing for a consistent ' +
      'copy, and restart them afterwards.',
  };
}
