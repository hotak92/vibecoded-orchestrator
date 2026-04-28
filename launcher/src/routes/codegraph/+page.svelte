<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import Term from '$lib/components/Term.svelte';
  import Dropdown from '$lib/components/Dropdown.svelte';
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

  // Multi-select state — shift-click adds, "Set access" opens bulk modal.
  // Each codegraph node carries an entity_class (CodeFunction etc) we need
  // to pass through to the bulk endpoint; we group selected IDs by class.
  let selectedNodeIds = $state<Set<string>>(new Set());
  let bulkModalOpen = $state(false);
  let bulkSaving = $state(false);
  let bulkMode = $state<'shared' | 'projects' | 'private'>('private');
  let bulkProjectIds = $state<Set<string>>(new Set());

  function toggleSelect(id: string) {
    if (selectedNodeIds.has(id)) selectedNodeIds.delete(id);
    else selectedNodeIds.add(id);
    selectedNodeIds = new Set(selectedNodeIds);
  }
  function clearSelection() {
    selectedNodeIds = new Set();
  }

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

  /** Group selected IDs by entity_class so we can dispatch one bulk
   * call per class (the backend command takes a single entity_class). */
  function groupSelectedByClass(): Map<string, string[]> {
    const out = new Map<string, string[]>();
    for (const n of nodes) {
      if (!selectedNodeIds.has(n.id)) continue;
      const klass = n.type;
      const arr = out.get(klass) ?? [];
      arr.push(n.id);
      out.set(klass, arr);
    }
    return out;
  }

  async function bulkApply() {
    if (!acting || selectedNodeIds.size === 0) return;
    bulkSaving = true;
    const groups = groupSelectedByClass();
    let totalSucceeded = 0;
    let totalFailed = 0;
    let firstError: string | null = null;
    try {
      for (const [klass, ids] of groups) {
        const result = await invoke<{ succeeded: number; failed: number; failures: { id: string; error: string }[] }>(
          'codegraph_set_entity_access_bulk',
          {
            req: {
              project_id: acting.id,
              entity_class: klass,
              entity_ids: ids,
              mode: bulkMode,
              project_ids: bulkMode === 'projects' ? [...bulkProjectIds] : [],
            },
          },
        );
        totalSucceeded += result.succeeded;
        totalFailed += result.failed;
        if (firstError === null && result.failures.length > 0) {
          firstError = result.failures[0]?.error ?? null;
        }
      }
      if (totalFailed > 0) {
        toast.error(`${totalSucceeded} updated, ${totalFailed} failed (first: ${firstError ?? 'unknown'})`);
      } else {
        toast.success(`${totalSucceeded} entit${totalSucceeded === 1 ? 'y' : 'ies'} updated`);
      }
      bulkModalOpen = false;
      clearSelection();
    } catch (e) {
      toast.error(e);
    } finally {
      bulkSaving = false;
    }
  }
</script>

<div class="cg-page">
  <header class="cg-header">
    <button class="cg-back" onclick={() => history.back()}>← Back</button>
    <h1>Code Graph</h1>
    {#if !acting}
      <span class="cg-warn">No project selected.</span>
    {:else}
      <!-- Target dropdown removed 2026-04-28: route is already
           project-scoped (active project = the only valid target);
           cross-project codegraph viewing is gated by the access
           control we already enforce server-side. If we ever
           reintroduce cross-project viewing, restore the Dropdown
           with availableTargets filtered to projects the user has
           explicit read access to. -->
      <span class="cg-target-label">{acting.name}</span>
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
      {#if selectedNodeIds.size > 0}
        <div class="cg-bulk-bar" role="toolbar" aria-label="Bulk actions">
          <span class="cg-bulk-count">{selectedNodeIds.size} selected</span>
          <button
            class="cg-bulk-btn cg-bulk-btn-primary"
            onclick={() => (bulkModalOpen = true)}
          >
            Set access for {selectedNodeIds.size} entit{selectedNodeIds.size === 1 ? 'y' : 'ies'}…
          </button>
          <button class="cg-bulk-btn" onclick={clearSelection}>Clear</button>
        </div>
      {/if}
      <SigmaGraph
        {nodes}
        {edges}
        typeColors={ENTITY_COLORS}
        onNodeClick={(n) => (pinned = n)}
        onSelectionToggle={toggleSelect}
        selectedIds={selectedNodeIds}
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

{#if bulkModalOpen}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="cg-modal-back" onclick={() => (bulkModalOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="cg-modal" onclick={(e) => e.stopPropagation()}>
      <header class="cg-modal-header">
        <h3>Set access for {selectedNodeIds.size} codegraph entit{selectedNodeIds.size === 1 ? 'y' : 'ies'}</h3>
        <button class="cg-modal-close" onclick={() => (bulkModalOpen = false)} aria-label="Close">×</button>
      </header>
      <div class="cg-modal-body">
        <p class="cg-modal-hint">
          Selection spans {groupSelectedByClass().size} entity class{groupSelectedByClass().size === 1 ? '' : 'es'}.
          Each class is updated in a separate batch.
        </p>
        <fieldset class="cg-mode-group">
          <legend>Access mode</legend>
          <label><input type="radio" name="bulkmode" value="shared" bind:group={bulkMode} /> Shared (all projects)</label>
          <label><input type="radio" name="bulkmode" value="projects" bind:group={bulkMode} /> Specific projects</label>
          <label><input type="radio" name="bulkmode" value="private" bind:group={bulkMode} /> This project only</label>
        </fieldset>
        {#if bulkMode === 'projects'}
          <div class="cg-mode-projects">
            <h4>Allowed projects</h4>
            {#each $projects.projects.filter((p) => p.id !== acting?.id) as p}
              <label class="cg-mode-project">
                <input
                  type="checkbox"
                  checked={bulkProjectIds.has(p.id)}
                  onchange={() => {
                    if (bulkProjectIds.has(p.id)) bulkProjectIds.delete(p.id);
                    else bulkProjectIds.add(p.id);
                    bulkProjectIds = new Set(bulkProjectIds);
                  }}
                />
                <span>{p.name}</span>
              </label>
            {/each}
          </div>
        {/if}
      </div>
      <footer class="cg-modal-footer">
        <button class="cg-bulk-btn" onclick={() => (bulkModalOpen = false)} disabled={bulkSaving}>Cancel</button>
        <button class="cg-bulk-btn cg-bulk-btn-primary" onclick={bulkApply} disabled={bulkSaving}>
          {bulkSaving ? 'Saving…' : 'Apply'}
        </button>
      </footer>
    </div>
  </div>
{/if}

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
  .cg-target-dd { width: 200px; }
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

  /* Bulk-action bar (graph overlay) */
  .cg-bulk-bar {
    position: absolute; top: 8px; right: 8px;
    z-index: 10;
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px;
    background: rgba(13,23,53,0.95);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(0,191,166,0.35);
    border-radius: 8px;
    font-size: 12px;
  }
  .cg-bulk-count {
    color: #0fc; font-weight: 700;
    padding: 2px 8px;
    background: rgba(0,191,166,0.12);
    border-radius: 999px;
  }
  .cg-bulk-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
  }
  .cg-bulk-btn:hover { background: rgba(255,255,255,0.1); }
  .cg-bulk-btn-primary {
    background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600;
  }
  .cg-bulk-btn-primary:hover { background: rgb(0,210,180); }
  .cg-bulk-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Bulk modal — Bug 19 systemic */
  .cg-modal-back {
    position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.6);
    display: flex; align-items: center; justify-content: center;
    padding: 2rem; overflow: hidden;
  }
  .cg-modal {
    background: #1a1a22; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px; width: 480px; max-width: min(92vw, 600px);
    max-height: calc(100vh - 4rem);
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,0.7);
  }
  .cg-modal-header {
    padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06);
    display: flex; justify-content: space-between; align-items: center;
  }
  .cg-modal-header h3 { margin: 0; font-size: 14px; }
  .cg-modal-close {
    background: none; border: none; color: #888; cursor: pointer; font-size: 20px;
    line-height: 1; padding: 0 4px;
  }
  .cg-modal-close:hover { color: #fff; }
  .cg-modal-body { padding: 14px 16px; }
  .cg-modal-hint { font-size: 11px; color: #888; margin: 0 0 12px; }
  .cg-mode-group { display: flex; flex-direction: column; gap: 6px; padding: 0; border: none; }
  .cg-mode-group legend { font-size: 11px; color: #888; text-transform: uppercase; padding: 0; margin-bottom: 6px; }
  .cg-mode-group label { font-size: 13px; cursor: pointer; }
  .cg-mode-projects { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.06); }
  .cg-mode-projects h4 { font-size: 11px; margin: 0 0 6px; color: #888; text-transform: uppercase; }
  .cg-mode-project {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 0; font-size: 12px; cursor: pointer;
  }
  .cg-modal-footer {
    padding: 10px 16px; border-top: 1px solid rgba(255,255,255,0.06);
    display: flex; justify-content: flex-end; gap: 8px;
  }
</style>
