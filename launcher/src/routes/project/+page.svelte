<script lang="ts">
  // Project landing page.
  //
  // The sidebar's "Project" link points here when nothing is selected; if a
  // project IS selected, we hop straight to /project/<id>. Without this
  // file, /project would 404 (the route must exist for the redirect to
  // dispatch).

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { projectColor } from '$lib/project-color';
  import CodeGraphBuildPill from '$lib/components/CodeGraphBuildPill.svelte';
  import KgSyncPill from '$lib/components/KgSyncPill.svelte';
  import KgSummaryPill from '$lib/components/KgSummaryPill.svelte';

  const pState = $derived($projects);

  onMount(() => {
    projects.load();
  });

  // If a project is already selected, redirect into its settings view.
  $effect(() => {
    const sel = $selectedProject;
    if (sel) {
      goto(`/project/${sel.id}`, { replaceState: true });
    }
  });

  function pick(id: string) {
    projects.select(id);
    goto(`/project/${id}`);
  }
</script>

<div class="page">
  <header class="page-header">
    <h1>Projects</h1>
    <p class="lede">
      Pick a project to view its agents, skills, hooks, permissions, and
      secrets. Use the project selector in the top bar to create a new one.
    </p>
  </header>

  {#if pState.loading}
    <p class="empty">Loading…</p>
  {:else if pState.projects.length === 0}
    <div class="empty-card">
      <p class="empty-title">No projects yet</p>
      <p class="empty-text">
        Open the project selector in the top bar and click <strong>+ New</strong>
        to create your first project.
      </p>
    </div>
  {:else}
    <ul class="grid">
      {#each pState.projects as p (p.id)}
        <li>
          <button class="card" onclick={() => pick(p.id)}>
            <span class="dot" style:background={projectColor(p.id)} aria-hidden="true"></span>
            <span class="card-body">
              <span class="name-row">
                <span class="name">{p.name}</span>
                <CodeGraphBuildPill projectId={p.id} compact />
                <KgSyncPill projectId={p.id} compact />
                <KgSummaryPill projectId={p.id} compact />
              </span>
              <span class="meta">
                <span>{p.host}</span>
                <span>· {p.module_count} module{p.module_count === 1 ? '' : 's'}</span>
              </span>
              <span class="path" title={p.folder_path}>{p.folder_path}</span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if pState.error}
    <p class="error">{pState.error}</p>
  {/if}
</div>

<style>
  .page { padding: 24px 28px 60px; }
  .page-header { margin-bottom: 18px; }
  h1 {
    font-size: 22px; font-weight: 800; color: var(--color-text);
    letter-spacing: -0.5px; margin-bottom: 4px;
  }
  .lede { font-size: 13px; color: var(--color-mid); max-width: 720px; }
  .empty {
    padding: 40px; text-align: center; color: var(--color-muted); font-size: 13px;
  }
  .empty-card {
    padding: 36px 28px; text-align: center;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px;
    max-width: 520px; margin: 0 auto;
  }
  .empty-title { font-size: 15px; font-weight: 700; color: var(--color-text); }
  .empty-text { font-size: 12px; color: var(--color-mid); margin-top: 6px; line-height: 1.5; }
  .grid {
    list-style: none; padding: 0; margin: 0;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }
  .card {
    width: 100%; text-align: left; cursor: pointer;
    display: flex; gap: 12px; align-items: flex-start;
    padding: 14px 16px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
    color: inherit;
    transition: all 0.15s ease;
  }
  .card:hover {
    background: rgba(255,255,255,0.05);
    border-color: rgba(255,255,255,0.12);
    transform: translateY(-1px);
  }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    flex-shrink: 0; margin-top: 5px;
  }
  .card-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
  .name-row {
    display: flex; align-items: center; gap: 8px;
    justify-content: space-between;
  }
  .name { font-size: 14px; font-weight: 700; color: var(--color-text); }
  .meta { font-size: 11px; color: var(--color-muted); display: flex; gap: 4px; }
  .path {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 10px; color: var(--color-muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    margin-top: 4px;
  }
  .error {
    margin: 12px 0; font-size: 12px; color: var(--color-pink);
    padding: 8px 12px; background: rgba(255,79,160,0.08);
    border: 1px solid rgba(255,79,160,0.2); border-radius: 8px;
  }
</style>
