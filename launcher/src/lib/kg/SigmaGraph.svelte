<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // Generic Obsidian-like graph component used by both KG (Screen B) and
  // codegraph (Screen C). Caller passes nodes/edges in a normalized shape.
  import type { VizNode, VizEdge } from './graph-types';

  let {
    nodes,
    edges,
    onNodeClick,
    onNodeContextMenu,
    typeColors = {},
  }: {
    nodes: VizNode[];
    edges: VizEdge[];
    onNodeClick?: (node: VizNode) => void;
    onNodeContextMenu?: (node: VizNode, x: number, y: number) => void;
    typeColors?: Record<string, string>;
  } = $props();

  const DEFAULT_COLOR = '#7b5fff';
  const PALETTE = [
    '#00bfa6', '#7b5fff', '#ff6f9e', '#ffc846',
    '#3aa3ff', '#ff9b3d', '#9b59b6', '#1abc9c',
  ];

  function colorFor(type: string): string {
    if (typeColors[type]) return typeColors[type];
    // Stable pick based on string hash
    let h = 0;
    for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) | 0;
    return PALETTE[Math.abs(h) % PALETTE.length] ?? DEFAULT_COLOR;
  }

  let container: HTMLDivElement;
  let renderer: any = null;
  let graph: any = null;
  let layoutInterval: number | null = null;

  async function initGraph() {
    if (!container) return;
    if (renderer) {
      renderer.kill();
      renderer = null;
    }
    try {
      const Sigma = (await import('sigma')).default;
      const Graph = (await import('graphology')).default;

      graph = new Graph({ multi: true, type: 'directed' });

      // Initial random layout — we'll iterate force layout below.
      const N = nodes.length;
      for (let i = 0; i < N; i++) {
        const n = nodes[i];
        const angle = (i / Math.max(1, N)) * Math.PI * 2;
        const radius = Math.sqrt(N) * 8;
        graph.addNode(n.id, {
          x: Math.cos(angle) * radius + (Math.random() - 0.5) * 4,
          y: Math.sin(angle) * radius + (Math.random() - 0.5) * 4,
          size: 6,
          label: n.label,
          color: colorFor(n.type),
          // Visual indicator for shared/restricted nodes (addendum)
          borderColor:
            n.accessMode === 'shared' ? '#00bfa6'
              : n.accessMode === 'projects' ? '#ffc846'
              : undefined,
          _type: n.type,
          _tags: n.tags ?? [],
          _meta: n.meta ?? {},
          _accessMode: n.accessMode,
        });
      }
      for (const e of edges) {
        if (graph.hasNode(e.from) && graph.hasNode(e.to)) {
          try {
            graph.addEdge(e.from, e.to, {
              size: 1,
              color: 'rgba(255,255,255,0.18)',
              _type: e.type,
            });
          } catch {
            // Multi-edge can throw on identical (key, source, target) — ignore.
          }
        }
      }

      renderer = new Sigma(graph, container, {
        renderEdgeLabels: false,
        labelDensity: 0.07,
        labelGridCellSize: 60,
        labelRenderedSizeThreshold: 6,
        defaultEdgeColor: 'rgba(255,255,255,0.15)',
        defaultNodeColor: DEFAULT_COLOR,
        labelColor: { color: '#cdd6e0' },
      });

      // Hover: highlight neighborhood (Obsidian-like)
      renderer.on('enterNode', ({ node }: { node: string }) => {
        const neighbors = new Set<string>([node]);
        for (const n of graph.neighbors(node)) neighbors.add(n);
        graph.forEachNode((id: string, attrs: any) => {
          graph.setNodeAttribute(id, 'highlighted', neighbors.has(id));
          graph.setNodeAttribute(id, 'color', neighbors.has(id) ? colorFor(attrs._type) : '#3a3a45');
          graph.setNodeAttribute(id, 'label', neighbors.has(id) ? attrs.label : '');
        });
        graph.forEachEdge((id: string, attrs: any, src: string, tgt: string) => {
          const hot = neighbors.has(src) && neighbors.has(tgt);
          graph.setEdgeAttribute(id, 'color', hot ? '#00bfa6' : 'rgba(255,255,255,0.05)');
        });
      });
      renderer.on('leaveNode', () => {
        graph.forEachNode((id: string, attrs: any) => {
          graph.setNodeAttribute(id, 'highlighted', false);
          graph.setNodeAttribute(id, 'color', colorFor(attrs._type));
          graph.setNodeAttribute(id, 'label', attrs.label);
        });
        graph.forEachEdge((id: string) => {
          graph.setEdgeAttribute(id, 'color', 'rgba(255,255,255,0.18)');
        });
      });

      renderer.on('clickNode', ({ node }: { node: string }) => {
        const attrs = graph.getNodeAttributes(node);
        const v: VizNode = {
          id: node,
          label: attrs.label,
          type: attrs._type,
          tags: attrs._tags,
          meta: attrs._meta,
          accessMode: attrs._accessMode,
        };
        onNodeClick?.(v);
      });

      renderer.on('rightClickNode', (ev: any) => {
        ev.event.original?.preventDefault?.();
        const attrs = graph.getNodeAttributes(ev.node);
        const v: VizNode = {
          id: ev.node,
          label: attrs.label,
          type: attrs._type,
          tags: attrs._tags,
          meta: attrs._meta,
          accessMode: attrs._accessMode,
        };
        onNodeContextMenu?.(v, ev.event.x ?? 0, ev.event.y ?? 0);
      });

      // Mini force iteration: push connected nodes apart, attract neighbors.
      // Cheap O(N+E) per tick. Cap iterations.
      let ticks = 0;
      layoutInterval = window.setInterval(() => {
        if (!graph || ticks++ > 80) {
          if (layoutInterval !== null) {
            clearInterval(layoutInterval);
            layoutInterval = null;
          }
          return;
        }
        const k = 1.0;
        const positions = new Map<string, { x: number; y: number; vx: number; vy: number }>();
        graph.forEachNode((id: string, a: any) => {
          positions.set(id, { x: a.x, y: a.y, vx: 0, vy: 0 });
        });

        // Repulsion (sampled — full O(N²) is fine for ≤500 nodes)
        const ids = [...positions.keys()];
        for (let i = 0; i < ids.length; i++) {
          for (let j = i + 1; j < ids.length; j++) {
            const a = positions.get(ids[i])!;
            const b = positions.get(ids[j])!;
            const dx = a.x - b.x, dy = a.y - b.y;
            const dist2 = Math.max(0.01, dx * dx + dy * dy);
            const f = (k * k * 4) / dist2;
            const fx = (dx / Math.sqrt(dist2)) * f;
            const fy = (dy / Math.sqrt(dist2)) * f;
            a.vx += fx; a.vy += fy;
            b.vx -= fx; b.vy -= fy;
          }
        }
        // Attraction along edges
        graph.forEachEdge((_id: string, _attrs: any, src: string, tgt: string) => {
          const a = positions.get(src), b = positions.get(tgt);
          if (!a || !b) return;
          const dx = b.x - a.x, dy = b.y - a.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const f = (dist * dist) / (k * 30);
          a.vx += (dx / Math.max(0.01, dist)) * f;
          a.vy += (dy / Math.max(0.01, dist)) * f;
          b.vx -= (dx / Math.max(0.01, dist)) * f;
          b.vy -= (dy / Math.max(0.01, dist)) * f;
        });
        const damp = 0.85;
        positions.forEach((p, id) => {
          const max = 4;
          const vx = Math.max(-max, Math.min(max, p.vx)) * damp;
          const vy = Math.max(-max, Math.min(max, p.vy)) * damp;
          graph.setNodeAttribute(id, 'x', p.x + vx);
          graph.setNodeAttribute(id, 'y', p.y + vy);
        });
        renderer.refresh();
      }, 30);
    } catch (e) {
      console.error('[SigmaGraph] init failed', e);
      if (container) {
        container.innerHTML = `<div style="color:#888;padding:24px;text-align:center;font-size:13px;">Graph viz unavailable: ${(e as Error).message}<br/><small>Run <code>npm install</code> in launcher/ to install sigma + graphology.</small></div>`;
      }
    }
  }

  onMount(initGraph);
  onDestroy(() => {
    if (layoutInterval !== null) clearInterval(layoutInterval);
    if (renderer) renderer.kill();
  });

  // Re-init when nodes/edges change.
  $effect(() => {
    void nodes; void edges;
    if (renderer) {
      renderer.kill();
      renderer = null;
    }
    void initGraph();
  });
</script>

<div class="sigma-host" bind:this={container}></div>

<style>
  .sigma-host {
    width: 100%;
    height: 100%;
    background: #0e0e16;
    border-radius: 6px;
    position: relative;
    overflow: hidden;
  }
</style>
