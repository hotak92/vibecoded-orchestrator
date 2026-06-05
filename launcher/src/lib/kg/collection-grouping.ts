// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Group the flat `kg_list_collections` result into per-project groups.
//
// Each VCO project owns up to three sibling collections that share a common
// prefix and differ only by suffix:
//   {Prefix}_KnowledgeGraph   — primary per-project KG
//   {Prefix}_Development      — verbose project docs
//   {Prefix}_Diagrams         — Mermaid / Excalidraw diagram references
//
// These three back the same project and are meant to share one access policy
// (it makes little sense to let another project read your KG but not your
// docs/diagrams). The UI groups them into a single stacked card whose
// "Access…" action applies to all members at once. We still keep per-member
// identity so the expanded view can Browse each collection individually.

import type { KgCollectionAccess } from '$lib/types/project-state';

// Ordered so the primary KG renders on top of the stack.
const SUFFIXES = ['_KnowledgeGraph', '_Development', '_Diagrams'] as const;
type Suffix = (typeof SUFFIXES)[number];

const ROLE_LABEL: Record<Suffix, string> = {
  _KnowledgeGraph: 'Knowledge',
  _Development: 'Docs',
  _Diagrams: 'Diagrams',
};

export interface CollectionGroup {
  /** Shared prefix = the project name (e.g. "Test", "SD15"). */
  prefix: string;
  /** Member collections present in Weaviate, ordered KG → Docs → Diagrams. */
  members: GroupMember[];
  /** Total nodes across all members. */
  totalNodes: number;
  /**
   * Effective access shown on the collapsed card. 'mixed' when members
   * disagree (a legacy state the unified "Access…" action heals).
   */
  access: 'read' | 'write' | 'none' | 'mixed' | string;
  /** Any member shared. */
  isShared: boolean;
}

export interface GroupMember extends KgCollectionAccess {
  /** Human label for the member's role within the project. */
  roleLabel: string;
}

function matchSuffix(name: string): Suffix | null {
  return SUFFIXES.find((s) => name.endsWith(s)) ?? null;
}

/**
 * Group collections by project prefix. Collections that don't match the
 * known suffix convention (or whose prefix is empty) are returned as their
 * own single-member group so nothing is ever hidden.
 */
export function groupCollections(collections: KgCollectionAccess[]): CollectionGroup[] {
  const groups = new Map<string, GroupMember[]>();
  const ungrouped: GroupMember[] = [];

  for (const col of collections) {
    const suffix = matchSuffix(col.name);
    const prefix = suffix ? col.name.slice(0, -suffix.length) : '';
    const member: GroupMember = {
      ...col,
      roleLabel: suffix ? ROLE_LABEL[suffix] : col.name,
    };
    if (!suffix || prefix.length === 0) {
      ungrouped.push(member);
      continue;
    }
    const existing = groups.get(prefix);
    if (existing) existing.push(member);
    else groups.set(prefix, [member]);
  }

  const result: CollectionGroup[] = [];

  for (const [prefix, members] of groups) {
    members.sort((a, b) => suffixRank(a.name) - suffixRank(b.name));
    result.push(buildGroup(prefix, members));
  }
  // Each ungrouped collection becomes its own single-member group.
  for (const m of ungrouped) {
    result.push(buildGroup(m.name, [m]));
  }

  result.sort((a, b) => a.prefix.localeCompare(b.prefix));
  return result;
}

function suffixRank(name: string): number {
  const s = matchSuffix(name);
  return s ? SUFFIXES.indexOf(s) : SUFFIXES.length;
}

function buildGroup(prefix: string, members: GroupMember[]): CollectionGroup {
  const totalNodes = members.reduce((sum, m) => sum + (m.node_count ?? 0), 0);
  const levels = new Set(members.map((m) => m.access));
  const access = levels.size === 1 ? [...levels][0] : 'mixed';
  return {
    prefix,
    members,
    totalNodes,
    access,
    isShared: members.some((m) => m.is_shared),
  };
}
