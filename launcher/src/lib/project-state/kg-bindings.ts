// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: remove a `shared` / `archive` KG binding from the project page.
//
// `delete_project_kg_binding` had no caller, so a binding added by mistake (the
// role dropdown lets the user save a `shared` or `archive` one) could never be
// undone. The `primary` binding is deliberately NOT removable here: unbinding
// it needs a last-primary policy (what does a project with no KG of its own
// resolve to?) that is an open owner decision (0.2.102). Removal only drops
// the binding row; the Weaviate collection and its nodes stay, exactly as the
// command's own doc promises.
//
// Pure (confirm + command injected) so act / leave-alone are real asserts.

import type { ProjectKgBinding } from '$lib/types/project-state';

/** Roles the page may remove. `primary` is intentionally absent. */
export const REMOVABLE_KG_ROLES: readonly string[] = ['shared', 'archive'];

export function canRemoveKgBinding(role: string): boolean {
  return REMOVABLE_KG_ROLES.includes(role);
}

export type KgBindingRemoveOutcome = 'removed' | 'cancelled' | 'not_removable';

export interface KgBindingRemoveDeps {
  confirm: (message: string) => boolean;
  /** Performs the delete. The tab supplies the literal
   *  `invoke('delete_project_kg_binding', ...)` call so the command name stays
   *  visible to the invoke-name census (no dynamic `invoke(cmd)` here). */
  remove: (projectId: string, role: string) => Promise<unknown>;
}

export function kgBindingRemoveMessage(b: Pick<ProjectKgBinding, 'role' | 'collection_name'>): string {
  return (
    `Remove the ${b.role} KG binding to "${b.collection_name}" from this project? ` +
    'The collection and its nodes are not deleted, and you can bind it again later.'
  );
}

/**
 * Confirm, then remove. A `primary` binding never reaches the backend; a
 * declined confirm never does either. Rejects with the backend error if the
 * command itself fails.
 */
export async function removeKgBinding(
  projectId: string,
  b: Pick<ProjectKgBinding, 'role' | 'collection_name'>,
  deps: KgBindingRemoveDeps,
): Promise<KgBindingRemoveOutcome> {
  if (!canRemoveKgBinding(b.role)) return 'not_removable';
  if (!deps.confirm(kgBindingRemoveMessage(b))) return 'cancelled';
  await deps.remove(projectId, b.role);
  return 'removed';
}
