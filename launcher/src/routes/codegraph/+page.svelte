<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import Term from '$lib/components/Term.svelte';
  import SigmaGraph from '$lib/kg/SigmaGraph.svelte';
  import type { VizNode, VizEdge } from '$lib/kg/graph-types';
  import type { ProjectView } from '$lib/types/launcher';
  import type { CodegraphSummary, CodegraphAccessMatrix } from '$lib/types/project-state';

  const ENTITY_COLORS: Record<string, string> = {
    CodeModule: '#3aa3ff',
    CodeClass: '#9b59b6',
    CodeFunction: '#1abc9c',
    CodeAPI: '#ff9b3d',
    CodeInteraction: '#ff6f9e',
  };

  let targetId = $state<string>('');
  let summary = $state<CodegraphSummary | null>(null);
  let matrix = $state<CodegraphAccessMatrix | null>(null);
  let nodes = $state<VizNode[]>([]);
  let edges = $state<VizEdge[]>([]);
  let loading = $state(false);
  let truncated = $state(false);

  const acting = $derived($selectedProject);

  onMount(async () => {
    await projects.load();
    if (acting && !targetId) targetId = acting.id;
  });

  async function loadAccess() {
    if (!acting) return;
    try {
      matrix = await invoke<CodegraphAccessMatrix>('codegraph_list_access', { projectId: acting.id });
    } catch (e) {
      toast.error(e);
    }
  }

  async function loadGraph() {
    if (!acting || !targetId) return;
    loading = true;
    try {
      const viz = await invoke<{ nodes: any[]; edges: any[]; truncated: boolean }>(
        'codegraph_load_graph',
        { actingProjectId: acting.id, targetProjectId: targetId, maxNodes: 150 },
      );
      nodes = viz.nodes.map((n) => ({
        id: n.id,
        label: n.label,
        type: n.entity_type,
        meta: { project: n.project, file_path: n.file_path },
      }));
      edges = viz.edges.map((e) => ({
        from: e.from_id,
        to: e.to_id,
        type: e.edge_type,
      }));
      truncated = viz.truncated;
      summary = await invoke<CodegraphSummary>('codegraph_summary', {
        actingProjectId: acting.id,
        targetProjectId: targetId,
      });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (acting) void loadAccess();
  });

  let pinned = $state<VizNode | null>(null);

  // Available targets: own + projects this one can read from
  const availableTargets = $derived.by<ProjectView[]>(() => {
    if (!acting) return [];
    const list: ProjectView[] = [acting];
    const ids = new Set<string>([acting.id]);
    for (const r of matrix?.can_read_from ?? []) {
      const p = $projects.projects.find((x) => x.id === r.id);
      if (p && !ids.has(p.id)) {
        list.push(p);
        ids.add(p.id);
      }
    }
    return list;
  });
</script>

<div class="cg-page">
  <header class="cg-header">
    <button class="cg-back" onclick={() => goto('/')}>← Back</button>
    <h1>Code Graph</h1>
    {#if !acting}
      <span class="cg-warn">No project selected.</span>
    {:else}
      <label class="cg-target">
        <span>Target:</span>
        <select bind:value={targetId}>
          {#each availableTargets as p}<option value={p.id}>{p.name}</option>{/each}
        </select>
      </label>
      <button class="cg-load" onclick={loadGraph} disabled={loading}>
        {loading ? 'Loading…' : 'Load graph'}
      </button>
    {/if}
  </header>

  {#if summary}
    <div class="cg-summary">
      <span class="cg-stat" style="border-color:{ENTITY_COLORS.CodeModule}"><strong>{summary.module_count}</strong> modules</span>
      <span class="cg-stat" style="border-color:{ENTITY_COLORS.CodeClass}"><strong>{summary.class_count}</strong> classes</span>
      <span class="cg-stat" style="border-color:{ENTITY_COLORS.CodeFunction}"><strong>{summary.function_count}</strong> functions</span>
      <span class="cg-stat" style="border-color:{ENTITY_COLORS.CodeAPI}"><strong>{summary.api_count}</strong> APIs</span>
      <span class="cg-stat" style="border-color:{ENTITY_COLORS.CodeInteraction}"><strong>{summary.interaction_count}</strong> interactions</span>
      {#if truncated}<span class="cg-stat cg-trunc">truncated</span>{/if}
    </div>
  {/if}

  <div class="cg-graph-host">
    {#if nodes.length === 0 && !loading}
      <p class="cg-empty">Pick a project and click <em>Load graph</em>.</p>
    {:else}
      <SigmaGraph
        {nodes}
        {edges}
        typeColors={ENTITY_COLORS}
        onNodeClick={(n) => (pinned = n)}
      />
      {#if pinned}
        <aside class="cg-side">
          <header>
            <strong>{pinned.label}</strong>
            <button onclick={() => (pinned = null)} aria-label="Close">×</button>
          </header>
          <p class="cg-side-meta">
            <span class="cg-stat" style="border-color:{ENTITY_COLORS[pinned.type] ?? '#888'}">{pinned.type}</span>
          </p>
          {#if pinned.meta?.['file_path']}
            <p class="cg-side-path"><code>{pinned.meta['file_path']}</code></p>
          {/if}
        </aside>
      {/if}
    {/if}
  </div>

  <footer class="cg-legend">
    {#each Object.entries(ENTITY_COLORS) as [type, color]}
      {@const glossKey = type === 'CodeModule' ? 'code-module'
        : type === 'CodeClass' ? 'code-class'
        : type === 'CodeFunction' ? 'code-function'
        : type === 'CodeAPI' ? 'code-api'
        : 'code-interaction'}
      <span><i style="background:{color}"></i><Term key={glossKey}>{type}</Term></span>
    {/each}
  </footer>
</div>

<Toast />

<style>
  .cg-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); display: flex; flex-direction: column; }
  .cg-header {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .cg-header h1 { font-size: 16px; margin: 0; flex: 0; }
  .cg-back, .cg-load {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .cg-load { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600; }
  .cg-load:disabled { opacity: 0.5; cursor: not-allowed; }
  .cg-target { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #888; }
  .cg-target select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 3px 8px; border-radius: 4px; font-size: 12px;
  }
  .cg-warn { color: #fa8; }
  .cg-summary { display: flex; gap: 8px; flex-wrap: wrap; padding: 8px 24px; }
  .cg-stat {
    font-size: 11px; padding: 2px 8px; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.15);
    color: #ccc; background: rgba(255,255,255,0.04);
  }
  .cg-stat strong { color: #fff; }
  .cg-trunc { color: #fa8; border-color: rgba(255,170,68,0.5); }
  .cg-graph-host { flex: 1; min-height: 480px; position: relative; }
  .cg-empty { padding: 40px; text-align: center; color: #888; }
  .cg-side {
    position: absolute; top: 12px; left: 12px; width: 260px;
    background: rgba(20,20,28,0.95); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 12px; z-index: 8; font-size: 12px;
  }
  .cg-side header { display: flex; justify-content: space-between; gap: 6px; }
  .cg-side header button { background: none; border: none; color: #888; cursor: pointer; font-size: 16px; }
  .cg-side-meta { margin: 6px 0; }
  .cg-side-path code { font-size: 10px; color: #888; word-break: break-all; }
  .cg-legend {
    display: flex; gap: 12px; flex-wrap: wrap; padding: 6px 24px;
    border-top: 1px solid rgba(255,255,255,0.06);
    font-size: 10px; color: #888;
  }
  .cg-legend span { display: inline-flex; align-items: center; gap: 4px; }
  .cg-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
</style>
