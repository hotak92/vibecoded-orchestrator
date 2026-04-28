<script lang="ts">
  // Projects list — top-level page that just enumerates every registered
  // project as a clickable card. Used as the canonical Back target from
  // per-project routes (`/project/[id]/...`) and from cross-project
  // dashboards like `/kg` and `/codegraph`. The home page (`/`) renders
  // the module catalog, not a project list — they're different concepts
  // (modules = "what tools are installed" vs projects = "which workspaces
  // do they manage"). Reported 2026-04-28: the per-project Back button
  // was sending users to the catalog, which felt like a dead-end.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { projects, selectedProject } from '$lib/stores/projects';
  import Toast from '$lib/components/Toast.svelte';

  onMount(() => {
    void projects.load();
  });

  const state = $derived($projects);
  const active = $derived($selectedProject);

  function open(id: string) {
    projects.select(id);
    goto(`/project/${id}`);
  }
</script>

<Toast />

<div class="pl-page">
  <header class="pl-header">
    <button class="pl-back" onclick={() => goto('/')}>← Home</button>
    <h1>Projects</h1>
    <button class="pl-refresh" onclick={() => projects.load()} disabled={state.loading}>
      {state.loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if state.loading && state.projects.length === 0}
    <p class="pl-empty">Loading…</p>
  {:else if state.projects.length === 0}
    <p class="pl-empty">
      No projects registered yet. Create one from the home dashboard's
      project picker.
    </p>
  {:else}
    <div class="pl-grid">
      {#each state.projects as p (p.id)}
        <article
          class="pl-card"
          class:active={active?.id === p.id}
          onclick={() => open(p.id)}
        >
          <header class="pl-card-head">
            <h3>{p.name}</h3>
            {#if active?.id === p.id}
              <span class="pl-card-badge">ACTIVE</span>
            {/if}
          </header>
          <p class="pl-card-path"><code>{p.folder_path}</code></p>
          <p class="pl-card-meta">
            <span>{p.host?.toUpperCase() ?? 'BASE'}</span>
          </p>
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .pl-page { padding: 24px; max-width: 1200px; margin: 0 auto; }
  .pl-header {
    display: flex; align-items: center; gap: 12px; margin-bottom: 24px;
  }
  .pl-header h1 { margin: 0; font-size: 22px; flex: 1; }
  .pl-back, .pl-refresh {
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit; font-size: 13px;
  }
  .pl-back:hover, .pl-refresh:hover:not(:disabled) {
    background: rgba(255,255,255,0.1);
  }
  .pl-refresh:disabled { opacity: 0.5; cursor: default; }
  .pl-empty { color: #888; padding: 40px; text-align: center; }

  .pl-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 12px;
  }
  .pl-card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 14px 16px;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    display: flex; flex-direction: column; gap: 6px;
  }
  .pl-card:hover {
    background: rgba(255,255,255,0.08);
    border-color: rgba(255,255,255,0.15);
  }
  .pl-card.active {
    border-color: rgba(0,191,166,0.4);
    background: rgba(0,191,166,0.05);
  }
  .pl-card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .pl-card-head h3 { margin: 0; font-size: 15px; }
  .pl-card-badge {
    background: rgba(0,191,166,0.15); color: rgb(0,191,166);
    border: 1px solid rgba(0,191,166,0.3);
    padding: 2px 8px; border-radius: 10px; font-size: 10px;
    font-weight: 600; letter-spacing: 0.04em;
  }
  .pl-card-path {
    margin: 0; font-size: 11px; color: #888;
    word-break: break-all;
  }
  .pl-card-path code {
    background: rgba(255,255,255,0.05); padding: 1px 5px;
    border-radius: 3px; font-family: ui-monospace, monospace;
  }
  .pl-card-meta { margin: 0; font-size: 11px; color: #aaa; }
  .pl-card-meta span {
    background: rgba(255,255,255,0.05); padding: 2px 8px;
    border-radius: 10px; font-size: 10px; font-weight: 600;
    letter-spacing: 0.04em;
  }
</style>
