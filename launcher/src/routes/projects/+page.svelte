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
  import { invoke, tauriAvailable } from '$lib/tauri';
  import Toast from '$lib/components/Toast.svelte';
  import ProjectCard from '$lib/components/ProjectCard.svelte';
  import UpdateAllProjectsModal from '$lib/components/UpdateAllProjectsModal.svelte';
  import {
    buildFolderMissingMap,
    type ProjectFolderFlag,
  } from '$lib/project-folder-health';

  onMount(() => {
    void projects.load();
    void loadFolderHealth();
  });

  const store = $derived($projects);
  const active = $derived($selectedProject);

  // 0.2.x backlog #4 (2026-05-10): "Update all" modal state. Driven by
  // a $state boolean — see UpdateAllProjectsModal for the lifecycle.
  let updateAllOpen = $state(false);

  // v0.2.49 Phase 6 S-4 — boot-probe verdict per project. Populated on
  // mount via `read_project_folder_missing_flags`. The boot probe itself
  // runs once per launcher boot in lib.rs setup, so this is just a
  // cheap one-shot read; we don't poll. Soft-fail: when the command
  // is unavailable (CLI / pre-v0.2.49 launcher) we render every card
  // as healthy and skip the banner.
  let folderMissingMap = $state<Record<string, boolean>>({});

  async function loadFolderHealth() {
    if (!tauriAvailable()) return;
    try {
      const flags = await invoke<ProjectFolderFlag[]>('read_project_folder_missing_flags');
      folderMissingMap = buildFolderMissingMap(flags);
    } catch {
      // Soft-fail: no banner is correct fallback when the command
      // is missing or the DB read fails. The eprintln side of the
      // probe will surface the issue server-side.
      folderMissingMap = {};
    }
  }

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

<div class="pl-page">
  <header class="pl-header">
    <button class="pl-back" onclick={() => goto('/')}>← Home</button>
    <h1>Projects</h1>
    <button class="pl-add" onclick={() => ui.openCreateProject()}>
      + Add Project
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
        <ProjectCard
          project={p}
          active={active?.id === p.id}
          folderMissing={folderMissingMap[p.id] === true}
          onOpen={open}
        />
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
  .pl-back, .pl-refresh, .pl-add, .pl-update-all {
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit; font-size: 13px;
  }
  .pl-back:hover, .pl-refresh:hover:not(:disabled), .pl-add:hover,
  .pl-update-all:hover:not(:disabled) {
    background: rgba(255,255,255,0.1);
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
  /* v0.2.49 Phase 6 S-4: card chrome moved into ProjectCard.svelte
     (component scope, with the folder-missing warning banner). The
     .pl-card* selectors that used to live here are gone — the page
     just provides the grid layout now. */
</style>
