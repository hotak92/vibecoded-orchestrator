<script lang="ts">
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgNode, KgNodeFull, KgGraph } from '$lib/types/project-state';
  import SigmaGraph from './SigmaGraph.svelte';
  import type { VizNode, VizEdge } from './graph-types';
  import GraphFilterPanel from './GraphFilterPanel.svelte';

  let {
    projectId,
    collection,
    onBack,
    onShareNode,
    onBulkShareNodes,
  }: {
    projectId: string;
    collection: string;
    onBack: () => void;
    onShareNode: (node: KgNode) => void;
    /** Open the bulk-access modal for a set of selected node IDs. */
    onBulkShareNodes?: (nodeIds: string[]) => void;
  } = $props();

  // Multi-select state — used by both search results and graph view.
  let selectedNodeIds = $state<Set<string>>(new Set());
  function toggleSelect(id: string) {
    if (selectedNodeIds.has(id)) selectedNodeIds.delete(id);
    else selectedNodeIds.add(id);
    selectedNodeIds = new Set(selectedNodeIds);
  }
  function selectAllVisible(ids: string[]) {
    selectedNodeIds = new Set(ids);
  }
  function clearSelection() {
    selectedNodeIds = new Set();
  }
  function openBulkModal() {
    if (!onBulkShareNodes || selectedNodeIds.size === 0) return;
    onBulkShareNodes([...selectedNodeIds]);
  }

  let viewMode = $state<'search' | 'graph'>('search');

  // Search mode
  let query = $state('');
  let results = $state<KgNode[]>([]);
  let searching = $state(false);
  let openNode = $state<KgNodeFull | null>(null);

  async function runSearch() {
    if (!query.trim()) {
      results = [];
      return;
    }
    searching = true;
    try {
      results = await invoke<KgNode[]>('kg_search', {
        projectId,
        collections: [collection],
        query: query.trim(),
        limit: 30,
      });
    } catch (e) {
      toast.error(e);
    } finally {
      searching = false;
    }
  }

  async function openDetail(node: KgNode) {
    try {
      openNode = await invoke<KgNodeFull>('kg_get_node', {
        projectId,
        collection,
        nodeId: node.id,
      });
    } catch (e) {
      toast.error(e);
    }
  }

  // Graph mode
  let graphLoading = $state(false);
  let vizNodes = $state<VizNode[]>([]);
  let vizEdges = $state<VizEdge[]>([]);
  let graphTotal = $state(0);
  let graphTruncated = $state(false);
  let pinnedNode = $state<VizNode | null>(null);

  // Filters
  let selectedTags = $state<string[]>([]);
  let selectedTypes = $state<string[]>([]);
  let selectedStatuses = $state<string[]>([]);
  let depthLimit = $state(2);
  let sharedOnly = $state(false);
  let nodeLimit = $state(500);

  let availableTags = $state<string[]>([]);
  let availableTypes = $state<string[]>([]);

  async function loadGraph() {
    graphLoading = true;
    try {
      const graph = await invoke<KgGraph>('kg_load_graph', {
        projectId,
        collection,
        tagFilter: selectedTags.length > 0 ? selectedTags : null,
        maxNodes: nodeLimit,
      });
      // Map filters
      let nodes = graph.nodes;
      if (selectedTypes.length > 0) {
        nodes = nodes.filter((n) => selectedTypes.includes(n.node_type));
      }
      const keep = new Set(nodes.map((n) => n.id));
      vizNodes = nodes.map((n) => ({
        id: n.id,
        label: n.title,
        type: n.node_type,
        tags: n.tags,
        meta: { excerpt: n.excerpt, file_path: n.file_path },
      }));
      vizEdges = graph.edges
        .filter((e) => keep.has(e.from_id) && keep.has(e.to_id))
        .map((e) => ({ from: e.from_id, to: e.to_id, type: e.relationship_type }));
      graphTotal = graph.total_nodes_in_collection;
      graphTruncated = graph.truncated;

      // Build filter chip lists from data
      const tagSet = new Set<string>();
      const typeSet = new Set<string>();
      for (const n of graph.nodes) {
        for (const t of n.tags ?? []) tagSet.add(t);
        if (n.node_type) typeSet.add(n.node_type);
      }
      availableTags = [...tagSet].sort();
      availableTypes = [...typeSet].sort();
    } catch (e) {
      toast.error(e);
    } finally {
      graphLoading = false;
    }
  }

  async function switchMode(m: 'search' | 'graph') {
    viewMode = m;
    if (m === 'graph' && vizNodes.length === 0) {
      await loadGraph();
    }
  }

  // Context menu state
  let ctxNode = $state<VizNode | null>(null);
  let ctxX = $state(0);
  let ctxY = $state(0);
  function onCtx(node: VizNode, x: number, y: number) {
    ctxNode = node;
    ctxX = x;
    ctxY = y;
  }

  async function promoteShared() {
    if (!ctxNode) return;
    try {
      await invoke('kg_promote_to_shared', {
        req: {
          project_id: projectId,
          source_collection: collection,
          node_id: ctxNode.id,
          shared_collection: null,
        },
      });
      toast.success('Promoted to shared');
    } catch (e) {
      toast.error(e);
    }
    ctxNode = null;
  }
  function shareNodeFromCtx() {
    if (!ctxNode) return;
    onShareNode({
      id: ctxNode.id,
      title: ctxNode.label,
      node_type: ctxNode.type,
      tags: ctxNode.tags ?? [],
      collection,
      excerpt: (ctxNode.meta?.['excerpt'] as string) ?? '',
      file_path: (ctxNode.meta?.['file_path'] as string | null) ?? null,
    });
    ctxNode = null;
  }
  async function getFullFromCtx() {
    if (!ctxNode) return;
    try {
      openNode = await invoke<KgNodeFull>('kg_get_node', {
        projectId,
        collection,
        nodeId: ctxNode.id,
      });
    } catch (e) {
      toast.error(e);
    }
    ctxNode = null;
  }
</script>

<div class="cv-host">
  <header class="cv-header">
    <button class="cv-back" onclick={onBack}>← Collections</button>
    <h2><code>{collection}</code></h2>
    <div class="cv-modes">
      <button class:active={viewMode === 'search'} onclick={() => switchMode('search')}>Search</button>
      <button class:active={viewMode === 'graph'} onclick={() => switchMode('graph')}>Graph</button>
    </div>
  </header>

  {#if viewMode === 'search'}
    <div class="cv-search">
      <form
        onsubmit={(e) => { e.preventDefault(); void runSearch(); }}
        class="cv-search-form"
      >
        <input
          bind:value={query}
          placeholder="Search nodes…"
          class="cv-input"
        />
        <button type="submit" disabled={searching} class="cv-btn">
          {searching ? 'Searching…' : 'Search'}
        </button>
      </form>
      {#if results.length > 0}
        {#if selectedNodeIds.size > 0}
          <div class="cv-bulk-bar" role="toolbar" aria-label="Bulk actions">
            <span class="cv-bulk-count">{selectedNodeIds.size} selected</span>
            {#if onBulkShareNodes}
              <button class="cv-bulk-btn cv-bulk-btn-primary" onclick={openBulkModal}>
                Set access for {selectedNodeIds.size} node{selectedNodeIds.size === 1 ? '' : 's'}…
              </button>
            {/if}
            <button class="cv-bulk-btn" onclick={clearSelection}>Clear</button>
          </div>
        {/if}
        <table class="cv-table">
          <thead>
            <tr>
              <th class="cv-col-check">
                <input
                  type="checkbox"
                  checked={results.length > 0 && results.every((r) => selectedNodeIds.has(r.id))}
                  onchange={(e) => {
                    if ((e.target as HTMLInputElement).checked) {
                      selectAllVisible(results.map((r) => r.id));
                    } else {
                      clearSelection();
                    }
                  }}
                  title="Select all visible"
                />
              </th>
              <th>Title</th><th>Type</th><th>Tags</th><th></th>
            </tr>
          </thead>
          <tbody>
            {#each results as r}
              <tr class:cv-row-selected={selectedNodeIds.has(r.id)}>
                <td class="cv-col-check">
                  <input
                    type="checkbox"
                    checked={selectedNodeIds.has(r.id)}
                    onchange={() => toggleSelect(r.id)}
                  />
                </td>
                <td>
                  <button class="cv-link" onclick={() => openDetail(r)}>{r.title}</button>
                </td>
                <td>{r.node_type}</td>
                <td>{(r.tags ?? []).join(', ')}</td>
                <td>
                  <button class="cv-row-share" onclick={() => onShareNode(r)} title="Share node access">⋯</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else if query && !searching}
        <p class="cv-empty">No results.</p>
      {/if}
    </div>
  {:else}
    <div class="cv-graph-host">
      {#if graphLoading}
        <p class="cv-empty">Loading graph…</p>
      {:else}
        {#if selectedNodeIds.size > 0}
          <div class="cv-bulk-bar cv-bulk-bar-graph" role="toolbar" aria-label="Bulk actions">
            <span class="cv-bulk-count">{selectedNodeIds.size} selected</span>
            {#if onBulkShareNodes}
              <button class="cv-bulk-btn cv-bulk-btn-primary" onclick={openBulkModal}>
                Set access for {selectedNodeIds.size} node{selectedNodeIds.size === 1 ? '' : 's'}…
              </button>
            {/if}
            <button class="cv-bulk-btn" onclick={clearSelection}>Clear</button>
          </div>
        {/if}
        <SigmaGraph
          nodes={vizNodes}
          edges={vizEdges}
          onNodeClick={(n) => (pinnedNode = n)}
          onNodeContextMenu={onCtx}
          onSelectionToggle={toggleSelect}
          selectedIds={selectedNodeIds}
        />
        <GraphFilterPanel
          tags={availableTags}
          types={availableTypes}
          statuses={[]}
          bind:selectedTags
          bind:selectedTypes
          bind:selectedStatuses
          bind:depthLimit
          bind:sharedOnly
          bind:nodeLimit
        />
        <div class="cv-graph-meta">
          {vizNodes.length} / {graphTotal} nodes{#if graphTruncated} (truncated){/if}
          <button class="cv-graph-reload" onclick={loadGraph}>Reload</button>
        </div>
        {#if pinnedNode}
          <aside class="cv-side">
            <header>
              <strong>{pinnedNode.label}</strong>
              <button onclick={() => (pinnedNode = null)} aria-label="Close">×</button>
            </header>
            <p class="cv-side-type">{pinnedNode.type}</p>
            {#if pinnedNode.tags && pinnedNode.tags.length > 0}
              <p class="cv-side-tags">{pinnedNode.tags.join(', ')}</p>
            {/if}
            {#if pinnedNode.meta?.['excerpt']}
              <p class="cv-side-excerpt">{pinnedNode.meta['excerpt']}</p>
            {/if}
            <div class="cv-side-actions">
              <button onclick={() => onShareNode({
                id: pinnedNode!.id,
                title: pinnedNode!.label,
                node_type: pinnedNode!.type,
                tags: pinnedNode!.tags ?? [],
                collection,
                excerpt: '',
                file_path: null,
              })}>Share</button>
              <button onclick={async () => {
                try {
                  openNode = await invoke<KgNodeFull>('kg_get_node', {
                    projectId,
                    collection,
                    nodeId: pinnedNode!.id,
                  });
                } catch (e) { toast.error(e); }
              }}>Get full</button>
            </div>
          </aside>
        {/if}
      {/if}
    </div>
  {/if}

  {#if ctxNode}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="cv-ctx-back" onclick={() => (ctxNode = null)}>
      <ul class="cv-ctx" style="left:{ctxX}px; top:{ctxY}px;">
        <li><button onclick={shareNodeFromCtx}>Share node…</button></li>
        <li><button onclick={getFullFromCtx}>Get full</button></li>
        <li><button onclick={promoteShared}>Promote to shared</button></li>
      </ul>
    </div>
  {/if}

  {#if openNode}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="cv-modal-back" onclick={() => (openNode = null)}>
      <div class="cv-modal" onclick={(e) => e.stopPropagation()}>
        <header><strong>{openNode.title}</strong>
          <button onclick={() => (openNode = null)} aria-label="Close">×</button>
        </header>
        <p class="cv-modal-meta">{openNode.node_type} · {(openNode.tags ?? []).join(', ')}</p>
        {#if openNode.file_path}<p class="cv-modal-path"><code>{openNode.file_path}</code></p>{/if}
        <pre class="cv-modal-body">{openNode.content}</pre>
      </div>
    </div>
  {/if}
</div>

<style>
  .cv-host { display: flex; flex-direction: column; height: 100%; }
  .cv-header {
    display: flex; align-items: center; gap: 12px;
    padding: 8px 16px; border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .cv-header h2 { font-size: 14px; margin: 0; flex: 1; }
  .cv-header h2 code { font-family: ui-monospace, monospace; }
  .cv-back, .cv-btn, .cv-modes button {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .cv-modes { display: flex; gap: 4px; }
  .cv-modes button.active { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; }
  .cv-search { padding: 14px 16px; }
  .cv-search-form { display: flex; gap: 6px; margin-bottom: 12px; }
  .cv-input {
    flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 6px 10px; border-radius: 4px; font-size: 13px;
  }
  .cv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .cv-table th { text-align: left; padding: 6px 8px; color: #888; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .cv-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .cv-col-check { width: 24px; padding: 6px 4px 6px 8px; }
  .cv-col-check input { cursor: pointer; }
  .cv-row-selected { background: rgba(0, 191, 166, 0.06); }
  .cv-bulk-bar {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px; margin-bottom: 8px;
    background: rgba(0,191,166,0.08);
    border: 1px solid rgba(0,191,166,0.3);
    border-radius: 8px;
    font-size: 12px;
  }
  .cv-bulk-bar-graph {
    position: absolute; top: 8px; right: 8px;
    z-index: 10; margin: 0;
    background: rgba(13,23,53,0.95);
    backdrop-filter: blur(10px);
  }
  .cv-bulk-count {
    color: #0fc; font-weight: 700;
    padding: 2px 8px;
    background: rgba(0,191,166,0.12);
    border-radius: 999px;
  }
  .cv-bulk-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
  }
  .cv-bulk-btn:hover { background: rgba(255,255,255,0.1); }
  .cv-bulk-btn-primary {
    background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600;
  }
  .cv-bulk-btn-primary:hover { background: rgb(0,210,180); }
  .cv-link { background: none; border: none; color: #0fc; cursor: pointer; padding: 0; text-align: left; }
  .cv-row-share {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 1px 8px; border-radius: 4px; cursor: pointer;
  }
  .cv-empty { color: #888; padding: 16px; text-align: center; }
  .cv-graph-host { position: relative; flex: 1; min-height: 500px; }
  .cv-graph-meta {
    position: absolute; bottom: 8px; left: 8px;
    background: rgba(20,20,28,0.85); padding: 4px 10px; border-radius: 4px;
    font-size: 11px; color: #aaa;
    display: flex; gap: 8px; align-items: center;
  }
  .cv-graph-reload {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 1px 6px; border-radius: 3px; cursor: pointer; font-size: 10px;
  }
  .cv-side {
    position: absolute; top: 12px; left: 12px;
    width: 260px; max-height: calc(100% - 24px); overflow-y: auto;
    background: rgba(20,20,28,0.95); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 12px; z-index: 8; font-size: 12px;
  }
  .cv-side header { display: flex; justify-content: space-between; align-items: flex-start; gap: 6px; }
  .cv-side header button {
    background: none; border: none; color: #888; cursor: pointer; font-size: 16px;
    line-height: 1; padding: 0 4px;
  }
  .cv-side-type { color: #c4b3ff; font-size: 11px; margin: 4px 0; }
  .cv-side-tags { color: #888; font-size: 11px; margin: 4px 0; }
  .cv-side-excerpt { color: #ccc; line-height: 1.4; margin: 6px 0; }
  .cv-side-actions { display: flex; gap: 6px; margin-top: 8px; }
  .cv-side-actions button {
    flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;
  }
  .cv-ctx-back { position: fixed; inset: 0; z-index: 1500; }
  .cv-ctx {
    position: fixed; list-style: none; margin: 0; padding: 4px;
    background: #1a1a22; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px; min-width: 160px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.6);
  }
  .cv-ctx li button {
    width: 100%; text-align: left; padding: 6px 10px;
    background: none; border: none; color: inherit; font-size: 12px; cursor: pointer;
    border-radius: 3px;
  }
  .cv-ctx li button:hover { background: rgba(255,255,255,0.08); }
  .cv-modal-back {
    position: fixed; inset: 0; z-index: 1100; background: rgba(0,0,0,0.6);
    display: flex; align-items: center; justify-content: center;
  }
  .cv-modal {
    background: #1a1a22; border-radius: 10px; width: 720px; max-width: 90vw;
    max-height: 80vh; padding: 16px; overflow-y: auto;
    border: 1px solid rgba(255,255,255,0.08);
  }
  .cv-modal header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
  .cv-modal header button {
    background: none; border: none; color: #888; cursor: pointer; font-size: 18px;
    line-height: 1; padding: 0 4px;
  }
  .cv-modal-meta { color: #888; font-size: 12px; margin: 0 0 4px; }
  .cv-modal-path { color: #888; font-size: 11px; margin: 0 0 8px; }
  .cv-modal-body {
    background: rgba(0,0,0,0.3); padding: 10px; border-radius: 4px;
    font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
    font-family: ui-monospace, monospace;
  }
</style>
