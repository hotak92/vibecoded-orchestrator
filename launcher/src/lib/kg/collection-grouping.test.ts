// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, it, expect } from 'vitest';
import { groupCollections } from './collection-grouping';
import type { KgCollectionAccess } from '$lib/types/project-state';

function col(name: string, access = 'read', node_count = 0, is_shared = false): KgCollectionAccess {
  return { name, access, node_count, is_shared };
}

describe('groupCollections', () => {
  it('groups the three sibling collections of one project', () => {
    const groups = groupCollections([
      col('Test_Development', 'read', 2),
      col('Test_Diagrams', 'read', 1),
      col('Test_KnowledgeGraph', 'read', 5),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].prefix).toBe('Test');
    expect(groups[0].members).toHaveLength(3);
    expect(groups[0].totalNodes).toBe(8);
    expect(groups[0].access).toBe('read');
  });

  it('orders members KG → Docs → Diagrams', () => {
    const [g] = groupCollections([
      col('Test_Diagrams'),
      col('Test_KnowledgeGraph'),
      col('Test_Development'),
    ]);
    expect(g.members.map((m) => m.roleLabel)).toEqual(['Knowledge', 'Docs', 'Diagrams']);
  });

  it('marks access as mixed when members disagree', () => {
    const [g] = groupCollections([
      col('Test_KnowledgeGraph', 'write'),
      col('Test_Development', 'none'),
      col('Test_Diagrams', 'read'),
    ]);
    expect(g.access).toBe('mixed');
  });

  it('keeps an incomplete group with only the present members', () => {
    const [g] = groupCollections([col('Solo_KnowledgeGraph', 'read', 3)]);
    expect(g.prefix).toBe('Solo');
    expect(g.members).toHaveLength(1);
  });

  it('returns unknown-suffix collections as their own single-member group', () => {
    const groups = groupCollections([col('WeirdCollectionName', 'read', 4)]);
    expect(groups).toHaveLength(1);
    expect(groups[0].prefix).toBe('WeirdCollectionName');
    expect(groups[0].members[0].roleLabel).toBe('WeirdCollectionName');
  });

  it('separates two distinct projects', () => {
    const groups = groupCollections([
      col('Alpha_KnowledgeGraph'),
      col('Beta_KnowledgeGraph'),
      col('Alpha_Development'),
    ]);
    expect(groups.map((g) => g.prefix)).toEqual(['Alpha', 'Beta']);
    expect(groups[0].members).toHaveLength(2);
    expect(groups[1].members).toHaveLength(1);
  });

  it('propagates shared flag when any member is shared', () => {
    const [g] = groupCollections([
      col('Test_KnowledgeGraph', 'read', 0, true),
      col('Test_Development', 'read', 0, false),
    ]);
    expect(g.isShared).toBe(true);
  });
});
