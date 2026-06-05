<script lang="ts">
  // Projects list — top-level page that just enumerates every registered
  // project as a clickable card. Used as the canonical Back target from
  // per-project routes (`/project/[id]/...`) and from cross-project
  // dashboards like `/kg` and `/codegraph`. The home page (`/`) renders
  // the module catalog, not a project list — they're different concepts
  // (modules = "what tools are installed" vs projects = "which workspaces
  // do they manage"). The per-project Back button targets this page so
  // users land somewhere meaningful instead of the module catalog.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { ui } from '$lib/stores/ui';
  import Toast from '$lib/components/Toast.svelte';
  import UpdateAllProjectsModal from '$lib/components/UpdateAllProjectsModal.svelte';
  import BulkImportProjectsModal from '$lib/components/BulkImportProjectsModal.svelte';

  onMount(() => {
    void projects.load();
  });

  const store = $derived($projects);
  const active = $derived($selectedProject);

  // 0.2.x backlog #4 (2026-05-10): "Update all" modal state. Driven by
  // a $state boolean — see UpdateAllProjectsModal for the lifecycle.
  let updateAllOpen = $state(false);
  // Scan-and-import-from-a-root-folder modal.
  let bulkImportOpen = $state(false);

  function open(id: string) {
    projects.select(id);
    goto(`/project/${id}`);
  }
</script>

<svelte:head>
  <title>Projects — VCT Launcher</title>
</svelte:head>

<Toast />
<UpdateAllProjectsModal bind:open={updateAllOpen} />
<BulkImportProjectsModal bind:open={bulkImportOpen} />

<div class="pl-page">
  <header class="pl-header">
    <button class="pl-back" onclick={() => goto('/')}>← Home</button>
    <h1>Projects</h1>
    <button class="pl-add" onclick={() => ui.openCreateProject()}>
      + Add Project
    </button>
    <!-- Scan a root folder and bulk-import the projects found inside. -->
    <button
      class="pl-scan"
      onclick={() => (bulkImportOpen = true)}
      title="Scan a folder for projects and import several at once"
    >
      ⤓ Scan &amp; import
    </button>
    <!-- 0.2.x backlog #4: power-user "Update all" button. Sequential
         iteration; the modal shows per-project status. Disabled when
         no projects are registered (nothing to update). -->
    <button
      class="pl-update-all"
      onclick={() => (updateAllOpen = true)}
      disabled={store.loading || store.projects.length === 0}
      title="Re-run bundle install on every registered project, sequentially"
    >
      ⟳ Update all
    </button>
    <button class="pl-refresh" onclick={() => projects.load()} disabled={store.loading}>
      {store.loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if store.loading && store.projects.length === 0}
    <p class="pl-empty">Loading…</p>
  {:else if store.projects.length === 0}
    <div class="pl-empty">
      <p>No projects registered yet.</p>
      <button class="pl-add" onclick={() => ui.openCreateProject()}>
        + Add your first project
      </button>
    </div>
  {:else}
    <div class="pl-grid">
      {#each store.projects as p (p.id)}
        <div
          class="pl-card"
          class:active={active?.id === p.id}
          role="button"
          tabindex="0"
          onclick={() => open(p.id)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              open(p.id);
            }
          }}
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
        </div>
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
  .pl-back, .pl-refresh, .pl-add, .pl-update-all, .pl-scan {
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit; font-size: 13px;
  }
  .pl-back:hover, .pl-refresh:hover:not(:disabled), .pl-add:hover,
  .pl-update-all:hover:not(:disabled), .pl-scan:hover {
    background: rgba(255,255,255,0.1);
  }
  .pl-scan {
    border-color: rgba(0,191,166,0.3);
    color: rgb(0,191,166);
  }
  /* 0.2.x backlog #4: distinct teal accent so the power-user action
   * reads as an action button, not a chrome control. Matches the Add
   * button's accent treatment. */
  .pl-update-all {
    border-color: rgba(0,191,166,0.3);
    color: rgb(0,191,166);
  }
  .pl-update-all:disabled {
    opacity: 0.4;
    cursor: not-allowed;
    color: var(--color-mid, #aaa);
  }
  .pl-add {
    border-color: rgba(0,191,166,0.4);
    color: rgb(0,191,166);
  }
  .pl-add:hover {
    background: rgba(0,191,166,0.08);
    border-color: rgba(0,191,166,0.6);
  }
  .pl-refresh:disabled { opacity: 0.5; cursor: default; }
  .pl-empty { color: #888; padding: 40px; text-align: center; }
  .pl-empty p { margin: 0 0 16px; }

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
  .pl-card-head h3 {
    margin: 0; font-size: 15px;
    flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pl-card-badge {
    background: rgba(0,191,166,0.15); color: rgb(0,191,166);
    border: 1px solid rgba(0,191,166,0.3);
    padding: 2px 8px; border-radius: 10px; font-size: 10px;
    font-weight: 600; letter-spacing: 0.04em;
    flex-shrink: 0;
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
