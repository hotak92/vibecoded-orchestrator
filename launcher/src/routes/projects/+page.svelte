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
    undeterminedCensus,
    shouldRecensusOnUpdaterEdge,
  } from '$lib/bundle-staleness';
  import type { BundleStalenessCensus } from '$lib/types/launcher';
  import { updater } from '$lib/stores/updater';

  onMount(() => {
    void projects.load();
    void loadFolderHealth();
    void loadBundleCensus();
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
  // So the census runs on mount AND again the moment an orchestrator
  // update finishes, which is the moment the gap is created.
  //
  // `null` means "not determined" — never "all fine". The helpers in
  // `$lib/bundle-staleness` hold that distinction; this page just renders
  // whatever they return.
  let census = $state<BundleStalenessCensus | null>(null);
  // False until the FIRST census attempt has resolved. Gates the summary
  // line so a page that is still asking never renders "could not determine"
  // — "not yet asked" and "asked and could not tell" are different states,
  // and conflating them is the same class of error this feature exists to
  // prevent, one level down.
  let censusAttempted = $state(false);
  // True from the completion of an orchestrator update until the user
  // dismisses the notice — it promotes the summary from a quiet line to a
  // call-out, because that is the moment the projects fell behind.
  let postUpdateNotice = $state(false);

  async function loadBundleCensus() {
    if (!tauriAvailable()) return;
    try {
      census = await invoke<BundleStalenessCensus>('bundle_staleness_census');
    } catch (e) {
      // The command is contracted to never reject, so this path means the
      // command is missing entirely (older launcher binary). Report it as
      // undetermined — NOT as an absence of findings.
      census = undeterminedCensus(e instanceof Error ? e.message : String(e));
    } finally {
      censusAttempted = true;
    }
  }

  const upd = $derived($updater);
  // Plain `let`, NOT `$state`: the effect both reads and writes it, and a
  // reactive cell in that position re-triggers its own effect. This is a
  // memo of the previous tick, never rendered, so it must not be tracked.
  let prevUpdating = false;
  $effect(() => {
    const isUpdating = upd.updating;
    // Falling edge only: an orchestrator update just completed. Re-census
    // and call the result out — this is the moment project bundles fell
    // behind. The predicate is pure + unit-tested so the naive `!updating`
    // form (true on every tick → a subprocess poll loop) can't creep back.
    const fire = shouldRecensusOnUpdaterEdge(prevUpdating, isUpdating);
    prevUpdating = isUpdating;
    if (fire) {
      postUpdateNotice = true;
      void loadBundleCensus();
    }
  });

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

  <!-- v0.2.92 WP-D: population-level bundle-staleness line. Promoted to a
       call-out right after an orchestrator update completes, because that
       is when the projects fell behind the orchestrator. Informational
       only — the remedy is the user choosing "Update all". -->
  {#if censusAttempted}
    <div
      class="pl-census"
      class:notice={postUpdateNotice}
      class:undetermined={attention === null}
      class:attention={attention !== null && attention > 0}
      data-testid="bundle-census-summary"
    >
      {#if postUpdateNotice}
        <strong>Orchestrator updated.</strong>
        Project bundles are not updated with it —
      {/if}
      <span>{censusSummary}</span>
      {#if attention !== null && attention > 0}
        <span class="pl-census-remedy">Use “Update all” to bring them forward.</span>
      {/if}
      {#if postUpdateNotice}
        <button class="pl-census-dismiss" onclick={() => (postUpdateNotice = false)}>
          Dismiss
        </button>
      {/if}
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
