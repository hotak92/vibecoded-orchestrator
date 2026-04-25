// Shared types for SigmaGraph + consumers (KG dashboard, codegraph viz).

export interface VizNode {
  id: string;
  label: string;
  type: string; // node_type or entity_class — used to color-code
  tags?: string[];
  meta?: Record<string, unknown>;
  accessMode?: string; // 'shared' | 'projects' | 'private' — visual halo
}

export interface VizEdge {
  from: string;
  to: string;
  type: string; // relationship_type or edge_type
}
