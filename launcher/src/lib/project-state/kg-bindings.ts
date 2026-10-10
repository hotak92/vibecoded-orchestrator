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

import type { ProjectCodegraphBinding, ProjectKgBinding } from '$lib/types/project-state';

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

// ─── v0.2.102 W6-B2: the code-graph binding save keeps the model RECORD ─────
//
// `project_codegraph_bindings.embedding_model` is the RECORD of what the
// project's code graph is indexed with (vco_lib/code_embed_model.py reads it;
// searches use it until a newly selected model's slot is complete). Saving the
// Codegraph tab used to send `last_analyzed_*: undefined` and `config: {}`,
// which NULLed the build provenance and wiped `config_json`, and it wrote the
// SELECTION over the record. Now the record and provenance are kept and the
// selection goes to `config.code_model_target` (MUST MATCH
// code_embed_model.TARGET_CONFIG_KEY): the next code-graph run adds that
// model's vectors beside the old ones and switches searches when complete
// (owner ruling 2026-10-10 — applied automatically, nothing dropped).

/** MUST MATCH vco_lib/code_embed_model.py TARGET_CONFIG_KEY. */
export const CODE_MODEL_TARGET_KEY = 'code_model_target';

export interface CodegraphBindingEdits {
  collection_prefix: string;
  /** The project's own code-model selection; null = follow the global pick. */
  target_model: string | null;
  enabled: boolean;
}

/** The model the tab shows as selected: the project's target, else the record. */
export function codegraphSelectedModel(current: ProjectCodegraphBinding | null | undefined): string {
  const t = current?.config?.[CODE_MODEL_TARGET_KEY];
  return (typeof t === 'string' && t.trim()) ? t : (current?.embedding_model ?? '');
}

export function codegraphBindingSaveReq(
  current: ProjectCodegraphBinding | null | undefined,
  edits: CodegraphBindingEdits,
): Record<string, unknown> {
  const config: Record<string, unknown> = { ...(current?.config ?? {}) };
  const target = edits.target_model?.trim() || null;
  if (target && target !== (current?.embedding_model ?? null)) config[CODE_MODEL_TARGET_KEY] = target;
  else delete config[CODE_MODEL_TARGET_KEY];
  return {
    collection_prefix: edits.collection_prefix,
    embedding_model: current?.embedding_model ?? null,
    embedding_dim: current?.embedding_dim ?? null,
    last_analyzed_commit: current?.last_analyzed_commit ?? null,
    last_analyzed_at: current?.last_analyzed_at ?? null,
    enabled: edits.enabled,
    config,
  };
}
