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
  // v0.2.92 WP-D (GUI half): per-project bundle-staleness chip + the count
  // surfaced when an orchestrator update completes.
  import BundleStalenessChip from '$lib/components/BundleStalenessChip.svelte';
  import {
    summaryLine,
    needsAttentionCount,
    createCensusController,
    type CensusView,
  } from '$lib/bundle-staleness';
  import type { BundleStalenessCensus } from '$lib/types/launcher';
  import { updater } from '$lib/stores/updater';
  import { projectSetup } from '$lib/stores/project-setup';

  // Declared BEFORE the effects below on purpose: `projects.load()` flips the
  // store to `loading` synchronously, so the projects-set effect's first
  // settled observation is the loaded list (its baseline), not an empty
  // pre-load list that would read as "projects were added" and cost a
  // second census on every mount.
  onMount(() => {
    void projects.load();
    void loadFolderHealth();
    void censusCtl.load();
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

  // ─── Bundle-staleness census (v0.2.92 WP-D) ────────────────────────
  //
  // Updating the ORCHESTRATOR does not update the bundles installed into
  // each project — that gap is exactly what let 12 of one user's 13
  // projects sit on June bundles while the launcher said "up to date".
  // So the census is re-taken after EVERY action that changes a bundle:
  // page mount, an orchestrator update completing (the moment the gap is
  // created), "Update all" finishing (success, partial or failed), the
  // Refresh button, a project added or removed from any surface, and a new
  // project's background bundle install finishing. A per-project "Update
  // bundle" runs on /project/[id]/settings, a different route: coming back
  // here re-mounts this page and the mount census covers it.
  //
  // Every trigger — and the rule that an OLDER census response never
  // overwrites a newer one — lives in `createCensusController`, where each
  // is unit-tested. This page only feeds it and renders its view.
  //
  // `null` census means "not determined" — never "all fine". The helpers in
  // `$lib/bundle-staleness` hold that distinction; this page just renders
  // whatever they return. `attempted` stays false until the FIRST census
  // resolves, so "not yet asked" never renders as "asked and could not
  // tell". `postUpdateNotice` promotes the summary to a call-out from the
  // completion of an orchestrator update, because that is the moment the
  // projects fell behind.
  const censusCtl = createCensusController({
    fetchCensus: () => invoke<BundleStalenessCensus>('bundle_staleness_census'),
    onChange: (v) => (censusView = v),
    enabled: tauriAvailable,
  });
  let censusView = $state<CensusView>(censusCtl.view());

  // Falling edge of the orchestrator updater only — the controller holds
  // the previous tick, so the naive `!updating` form (true on every tick →
  // a subprocess poll loop) cannot creep back in here.
  const upd = $derived($updater);
  $effect(() => {
    censusCtl.updaterTick(upd.updating);
  });

  // Adds and deletes happen from the MenuBar project selector while this
  // page is mounted. The controller fires only when the SET of ids changes.
  $effect(() => {
    censusCtl.projectsChanged(
      store.projects.map((p) => p.id),
      store.loading,
    );
  });

  // A new project's bundle is installed by a detached background phase
  // AFTER `create_project_v2` returns; its row only becomes readable when
  // that phase reaches a terminal status.
  const setup = $derived($projectSetup.active);
  $effect(() => {
    censusCtl.setupObserved(setup);
  });

  function refreshAll() {
    void projects.load();
    void censusCtl.refresh();
  }

  const census = $derived(censusView.census);
  const attention = $derived(needsAttentionCount(census));
  const censusSummary = $derived(summaryLine(census));

  function open(id: string) {
    projects.select(id);
    goto(`/project/${id}`);
  }
</script>

<svelte:head>
  <title>Projects — VCT Launcher</title>
</svelte:head>

<Toast />
<!-- `onFinished` fires when a run reaches its done phase (success, partial or
     failure) and never for a dialog cancelled before running: a run changes
     bundle state, so the census on this page is re-taken. -->
<UpdateAllProjectsModal bind:open={updateAllOpen} onFinished={censusCtl.updateAllFinished} />

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
    <!-- Refreshes the project list AND re-takes the bundle census: a
         Refresh that left the chips as they were would repeat the stale
         "Bundle stale" lie this button is the user's remedy for. -->
    <button
      class="pl-refresh"
      onclick={refreshAll}
      disabled={store.loading}
      title="Reload the project list and re-check every project's bundle"
    >
      {store.loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  <!-- v0.2.92 WP-D: population-level bundle-staleness line. Promoted to a
       call-out right after an orchestrator update completes, because that
       is when the projects fell behind the orchestrator. Informational
       only — the remedy is the user choosing "Update all".
       `role="status"` makes the re-taken summary reach a screen reader
       politely; `aria-busy` marks the shown figures as about to change. -->
  {#if censusView.attempted}
    <div
      class="pl-census"
      class:notice={censusView.postUpdateNotice}
      class:undetermined={attention === null}
      class:attention={attention !== null && attention > 0}
      data-testid="bundle-census-summary"
      role="status"
      aria-busy={censusView.checking}
    >
      {#if censusView.postUpdateNotice}
        <strong>Orchestrator updated.</strong>
        Project bundles are not updated with it —
      {/if}
      <span>{censusSummary}</span>
      {#if attention !== null && attention > 0}
        <span class="pl-census-remedy">Use “Update all” to bring them forward.</span>
      {/if}
      {#if censusView.checking}
        <span class="pl-census-checking">Re-checking…</span>
      {/if}
      {#if censusView.postUpdateNotice}
        <button class="pl-census-dismiss" onclick={censusCtl.dismissNotice}>
          Dismiss
        </button>
      {/if}
    </div>
  {:else if censusView.checking}
    <!-- First census still running: say so, rather than rendering nothing
         (or, worse, "could not determine" for a question not yet answered). -->
    <div class="pl-census" role="status" aria-busy="true" data-testid="bundle-census-summary">
      <span class="pl-census-checking">Checking project bundles…</span>
    </div>
  {/if}

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
        <!-- The chip lives in the grid CELL rather than inside ProjectCard
             so the card component keeps a single responsibility (and so
             every other ProjectCard call-site is unaffected). Negative
             top margin + the cell's flex column make it read as a footer
             strip attached to the card. -->
        <div class="pl-cell">
          <ProjectCard
            project={p}
            active={active?.id === p.id}
            folderMissing={folderMissingMap[p.id] === true}
            onOpen={open}
          />
          <div class="pl-cell-chips">
            <BundleStalenessChip {census} projectId={p.id} />
          </div>
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

  /* v0.2.92 WP-D: population summary line. Three visual weights —
     quiet (all current), amber (something needs attention), grey-dashed
     (could not be determined; NEVER the healthy treatment). */
  .pl-census {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: -8px 0 20px;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
    color: #aaa;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
  }
  .pl-census.attention {
    color: rgb(255, 190, 80);
    background: rgba(255, 176, 32, 0.08);
    border-color: rgba(255, 176, 32, 0.28);
  }
  .pl-census.undetermined {
    color: #b3b3b3;
    border-style: dashed;
    border-color: rgba(255, 255, 255, 0.28);
    font-style: italic;
  }
  .pl-census.notice {
    border-width: 1px;
    box-shadow: 0 0 0 1px rgba(123, 95, 255, 0.25);
  }
  .pl-census strong { color: inherit; font-style: normal; }
  .pl-census-remedy { opacity: 0.85; }
  .pl-census-checking { opacity: 0.75; font-style: italic; }
  .pl-census-dismiss {
    margin-left: auto;
    padding: 2px 10px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: inherit;
    font-size: 11px;
    cursor: pointer;
  }
  .pl-cell { display: flex; flex-direction: column; }
  .pl-cell-chips {
    display: flex;
    gap: 6px;
    padding: 6px 16px 0;
  }

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
