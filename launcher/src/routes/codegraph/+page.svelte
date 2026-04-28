<script lang="ts">
  // Codegraph dashboard — card grid mirroring /kg.
  //
  // Replaced 2026-04-28: the prior implementation tried to render a
  // force-directed graph of every codegraph entity, which (a) didn't
  // scale past a few hundred nodes and (b) was useless for the task
  // users actually have ("which projects have a code graph and how
  // big is each one"). New layout: one card per project that has
  // codegraph data, with the five entity counts (modules/classes/
  // functions/APIs/interactions) and a Browse button that drills into
  // an entity table view. Drill-in TBD; for v0 the Browse button is
  // a placeholder that toasts "viewer coming soon" so we ship the
  // dashboard now and iterate.
  //
  // Source: codegraph_list_projects (Rust). Returns one row per
  // <prefix>_CodeFunction-style class group, with the five counts
  // pre-aggregated server-side.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import NoProjectBanner from '$lib/components/NoProjectBanner.svelte';

  interface CodegraphProjectSummary {
    project_name: string;
    prefix: string;
    module_count: number;
    class_count: number;
    function_count: number;
    api_count: number;
    interaction_count: number;
    access: 'read' | 'write' | 'none';
  }

  let summaries = $state<CodegraphProjectSummary[]>([]);
  let loading = $state(true);
  const acting = $derived($selectedProject);

  async function load() {
    if (!acting) {
      loading = false;
      return;
    }
    loading = true;
    try {
      summaries = await invoke<CodegraphProjectSummary[]>('codegraph_list_projects', {
        projectId: acting.id,
      });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
  $effect(() => {
    if (acting) void load();
  });

  function totalEntities(s: CodegraphProjectSummary): number {
    return (
      s.module_count +
      s.class_count +
      s.function_count +
      s.api_count +
      s.interaction_count
    );
  }

  function browseProject(s: CodegraphProjectSummary) {
    // v0 placeholder — entity-table viewer is the next iteration.
    // For now the per-project tab "KG / Codegraph" is the canonical
    // place to see + manage one project's codegraph; this dashboard
    // is the cross-project overview.
    toast.info(`Codegraph viewer for ${s.project_name} — coming soon. See the project's KG / Codegraph tab.`);
  }
</script>

<Toast />

<div class="cg-page">
  <header class="cg-header">
    <button class="cg-back" onclick={() => history.back()}>← Back</button>
    <h1>Code Graph</h1>
    <button class="cg-refresh" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if !acting}
    <NoProjectBanner />
  {:else if loading && summaries.length === 0}
    <p class="cg-empty">Loading…</p>
  {:else if summaries.length === 0}
    <p class="cg-empty">
      No codegraph data found in Weaviate. Run <code>code-graph-analyze</code>
      on a project to generate the entity classes (or wait for the auto-build
      to finish — see the status pill in the project header).
    </p>
  {:else}
    <div class="cg-grid">
      {#each summaries as s (s.prefix)}
        <article class="cg-card" class:owned={s.access === 'write'}>
          <header class="cg-card-head">
            <h3>{s.project_name}</h3>
            <span class="cg-card-access cg-card-access-{s.access}">{s.access.toUpperCase()}</span>
          </header>
          <p class="cg-card-prefix"><code>{s.prefix}_*</code></p>
          <div class="cg-card-stats">
            <span class="cg-stat" style="--c:#3aa3ff"><strong>{s.module_count}</strong> modules</span>
            <span class="cg-stat" style="--c:#9b59b6"><strong>{s.class_count}</strong> classes</span>
            <span class="cg-stat" style="--c:#1abc9c"><strong>{s.function_count}</strong> functions</span>
            {#if s.api_count > 0}
              <span class="cg-stat" style="--c:#ff9b3d"><strong>{s.api_count}</strong> APIs</span>
            {/if}
            {#if s.interaction_count > 0}
              <span class="cg-stat" style="--c:#ff6f9e"><strong>{s.interaction_count}</strong> interactions</span>
            {/if}
          </div>
          <p class="cg-card-total">{totalEntities(s)} entities total</p>
          <div class="cg-card-actions">
            <button
              class="cg-btn"
              onclick={() => browseProject(s)}
              disabled={s.access === 'none'}
            >
              Browse
            </button>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .cg-page { padding: 24px; max-width: 1200px; margin: 0 auto; }
  .cg-header { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
  .cg-header h1 { margin: 0; font-size: 22px; flex: 1; }
  .cg-back, .cg-refresh {
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; font-size: 13px;
  }
  .cg-back:hover, .cg-refresh:hover:not(:disabled) {
    background: rgba(255,255,255,0.1);
  }
  .cg-refresh:disabled { opacity: 0.5; cursor: default; }
  .cg-empty { color: #888; padding: 40px; text-align: center; }
  .cg-empty code {
    background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 3px;
    font-family: ui-monospace, monospace; font-size: 12px;
  }

  .cg-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }
  .cg-card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .cg-card.owned {
    border-color: rgba(0,191,166,0.35);
    background: rgba(0,191,166,0.05);
  }
  .cg-card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .cg-card-head h3 { margin: 0; font-size: 15px; }
  .cg-card-access {
    padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600;
    letter-spacing: 0.04em;
  }
  .cg-card-access-write {
    background: rgba(0,191,166,0.15); color: rgb(0,191,166);
    border: 1px solid rgba(0,191,166,0.3);
  }
  .cg-card-access-read {
    background: rgba(155,89,182,0.15); color: rgb(155,89,182);
    border: 1px solid rgba(155,89,182,0.3);
  }
  .cg-card-access-none {
    background: rgba(255,255,255,0.05); color: #888;
    border: 1px solid rgba(255,255,255,0.12);
  }
  .cg-card-prefix {
    margin: 0; font-size: 11px; color: #888;
  }
  .cg-card-prefix code {
    background: rgba(255,255,255,0.05); padding: 1px 5px; border-radius: 3px;
    font-family: ui-monospace, monospace;
  }
  .cg-card-stats {
    display: flex; flex-wrap: wrap; gap: 4px;
    margin: 4px 0;
  }
  .cg-stat {
    padding: 2px 8px; border-radius: 10px; font-size: 11px;
    border: 1px solid var(--c, #888);
    background: color-mix(in srgb, var(--c, #888) 8%, transparent);
    color: var(--c, #ccc);
  }
  .cg-stat strong { font-weight: 700; color: #fff; }
  .cg-card-total {
    margin: 0; font-size: 11px; color: #aaa;
  }
  .cg-card-actions {
    display: flex; justify-content: flex-end; gap: 6px;
    margin-top: 4px;
  }
  .cg-btn {
    padding: 4px 12px; border-radius: 4px; font-size: 12px;
    background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
    color: inherit; cursor: pointer;
  }
  .cg-btn:hover:not(:disabled) { background: rgba(255,255,255,0.12); }
  .cg-btn:disabled { opacity: 0.4; cursor: default; }
</style>
