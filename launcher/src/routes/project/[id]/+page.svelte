<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import AgentsTab from '$lib/project-state/AgentsTab.svelte';
  import SkillsTab from '$lib/project-state/SkillsTab.svelte';
  import HooksTab from '$lib/project-state/HooksTab.svelte';
  import PermissionsTab from '$lib/project-state/PermissionsTab.svelte';
  import SecretsTab from '$lib/project-state/SecretsTab.svelte';
  import KgCodegraphTab from '$lib/project-state/KgCodegraphTab.svelte';
  // PR-8 (v0.2.11 / 2026-05-15): per-project Identity tab + cross-project
  // access matrix tab. The existing PermissionsTab handles in-project
  // permissions (write scopes, tool allowlists, MCP toggles); these two
  // new tabs surface the cross-project access model (KG collections this
  // project can read, code-graph grants to/from other projects) and the
  // collection-identity fields (KG_COLLECTION / CODE_GRAPH_PROJECT) that
  // were previously only editable by hand-patching settings.json.
  import IdentityTab from '$lib/project-state/IdentityTab.svelte';
  import CrossProjectAccessTab from '$lib/project-state/CrossProjectAccessTab.svelte';
  // v0.2.22 — Item #14: Settings is now an inline tab body (extracted
  // from /project/[id]/settings into a reusable component). One click
  // from the tab strip to the settings form, no intermediate link page.
  // The /project/[id]/settings route still exists for direct URLs and
  // delegates to the same component.
  import SettingsTab from '$lib/project-state/SettingsTab.svelte';
  // v0.2.23 F2 (2026-05-21): when the active project is the
  // orchestrator-root row, the per-project tab strip surfaces an extra
  // "Orchestrator core" tab that renders the schema declared in the
  // repo-root vct-module.json. This consolidates the previously
  // duplicated "Orchestrator Core" sidebar entry — orchestrator-root
  // is treated as a special project rather than a separate sidebar
  // surface. See OrchestratorCoreTab.svelte for the rationale.
  import OrchestratorCoreTab from '$lib/project-state/OrchestratorCoreTab.svelte';
  import CodeGraphBuildBanner from '$lib/components/CodeGraphBuildBanner.svelte';
  import KgSyncBanner from '$lib/components/KgSyncBanner.svelte';
  import KgSummaryBanner from '$lib/components/KgSummaryBanner.svelte';
  import type { ProjectView } from '$lib/types/launcher';

  let projectId = $derived($page.params.id);
  let project = $state<ProjectView | null>(null);
  // PR-8 (v0.2.11): added 'identity' (project fundamentals: name, KG/code-graph
  // collection names) and 'access' (cross-project KG read levels +
  // code-graph grants). Position 'identity' first so it's the first
  // thing users see after agent listings; 'access' sits right after
  // 'permissions' (which handles intra-project permissions) so the two
  // permission models live next to each other in the tab strip.
  // v0.2.23 F2 (2026-05-21): added 'orchestrator_core' — visible only
  // when the active project's host is `orchestrator_root`. Pinned to
  // the end of the tab strip so it doesn't shift existing tab indices
  // for muscle-memory users on normal projects.
  let activeTab = $state<'identity' | 'agents' | 'skills' | 'hooks' | 'permissions' | 'access' | 'secrets' | 'kg' | 'settings' | 'orchestrator_core'>('agents');

  // v0.2.23 F2: the orchestrator-root project is treated as a special
  // project — same tab template, plus one extra "Orchestrator core"
  // tab. Normal projects don't see this tab. Derive from `project`
  // (not the global selectedProject store) so the comparison is
  // stable while a different project is loading.
  const isOrchestratorProject = $derived(project?.host === 'orchestrator_root');

  // Orchestrator update banner. Loads inspect_orchestrator_at on
  // mount; if version_status === 'outdated' a "Update this project"
  // button appears.
  type ConfigHealth = { file: string; ok: boolean; error: string | null };
  type OrchestratorState = {
    installed: boolean;
    version: string | null;
    version_status: 'current' | 'outdated' | 'unknown';
    bundled_version: string | null;
    config_health: ConfigHealth[];
  };
  let orchState = $state<OrchestratorState | null>(null);
  let updating = $state(false);
  let rebuilding = $state(false);
  let resyncingKg = $state(false);
  let rebuildingSummaries = $state(false);

  async function rebuildCodeGraph() {
    if (!project) return;
    rebuilding = true;
    try {
      await invoke('rebuild_code_graph', { projectId: project.id });
      toast.success('Code graph rebuild started');
    } catch (e) {
      toast.error(e);
    } finally {
      rebuilding = false;
    }
  }

  // Mirror of `rebuildCodeGraph` for the new KG-sync header button
  // (Decision 2026-05-12 — option B: keep both buttons in the project
  // header, smallest diff). `retry_kg_sync` already exists as a Tauri
  // command and is the same one invoked from the failure-state banner.
  async function resyncKg() {
    if (!project) return;
    resyncingKg = true;
    try {
      await invoke('retry_kg_sync', { projectId: project.id });
      toast.success('KG sync started');
    } catch (e) {
      toast.error(e);
    } finally {
      resyncingKg = false;
    }
  }

  // v0.2.3 (2026-05-12): third header button alongside "Re-build code
  // graph" / "Re-sync KG". Mirrors `resyncKg` end-to-end — same
  // `.rebuild-btn` style, same loading state, same `retry_kg_summary`
  // Tauri command the banner's Retry button calls. Triggers a full
  // re-walk of `knowledge/**/*.md` through `generate-kg-summary.py`
  // (which content-hashes nodes internally, so unchanged nodes are a
  // cheap no-op even on repeated clicks).
  async function rebuildKgSummaries() {
    if (!project) return;
    rebuildingSummaries = true;
    try {
      await invoke('retry_kg_summary', { projectId: project.id });
      toast.success('KG summaries rebuild started');
    } catch (e) {
      toast.error(e);
    } finally {
      rebuildingSummaries = false;
    }
  }

  async function loadProject() {
    try {
      project = await invoke<ProjectView | null>('get_project_v2', { id: projectId });
      if (!project) {
        toast.error(`Project ${projectId} not found`);
        return;
      }
      try {
        orchState = await invoke<OrchestratorState>('inspect_orchestrator_at', {
          path: project.folder_path,
        });
      } catch (e) {
        console.error('inspect_orchestrator_at failed', e);
      }
    } catch (e) {
      toast.error(e);
    }
  }

  async function runUpdate() {
    if (!project) return;
    updating = true;
    try {
      await invoke('update_orchestrator_at', { path: project.folder_path });
      orchState = await invoke<OrchestratorState>('inspect_orchestrator_at', {
        path: project.folder_path,
      });
      toast.success('Orchestrator updated');
    } catch (e) {
      toast.error(e);
    } finally {
      updating = false;
    }
  }

  onMount(loadProject);
  $effect(() => {
    if (projectId) void loadProject();
  });

  // v0.2.23 F2 (2026-05-21): converted from a static `const tabs` to a
  // `$derived` so the orchestrator-only "Orchestrator core" tab can be
  // conditionally appended for the orchestrator-root project. Normal
  // projects see the original 9 tabs in the same order — the new tab
  // is pinned to the END so existing muscle-memory tab indices are
  // preserved.
  type TabDef = { id: typeof activeTab; label: string };
  const tabs = $derived<TabDef[]>([
    // PR-8 (v0.2.11): Identity first — it's the project's
    // KG_COLLECTION / CODE_GRAPH_PROJECT identity (the "who am I?" before
    // anything else). Access is the cross-project counterpart to the
    // intra-project Permissions tab.
    { id: 'identity', label: 'Identity' },
    { id: 'agents', label: 'Agents' },
    { id: 'skills', label: 'Skills' },
    { id: 'hooks', label: 'Hooks' },
    { id: 'permissions', label: 'Permissions' },
    { id: 'access', label: 'Cross-project access' },
    { id: 'secrets', label: 'Secret refs' },
    { id: 'kg', label: 'KG / Codegraph' },
    { id: 'settings', label: 'Settings' },
    // Orchestrator-root-only tab — pinned to the end. Hosts the
    // schema-rendered controls from the repo-root vct-module.json
    // (previously surfaced as a duplicate "Orchestrator Core" sidebar
    // entry). Suppressed for normal projects.
    ...(isOrchestratorProject
      ? [{ id: 'orchestrator_core' as const, label: 'Orchestrator core' }]
      : []),
  ]);
</script>

<div class="project-page">
  <header class="project-header">
    <button
      class="back-btn"
      onclick={() => goto('/projects')}
      aria-label="Back to projects list"
    >
      ← Back
    </button>
    <div class="project-title">
      <h1>{project?.name ?? 'Project'}</h1>
      {#if project}
        <p class="project-meta">
          <code>{project.folder_path}</code>
          <span class="host-badge host-{project.host}">{project.host}</span>
        </p>
      {/if}
    </div>
    {#if project}
      <button
        class="rebuild-btn"
        onclick={rebuildCodeGraph}
        disabled={rebuilding}
        title="Re-run code-graph-analyze on this project's source folder"
      >
        {rebuilding ? 'Starting…' : 'Re-build code graph'}
      </button>
      <button
        class="rebuild-btn"
        onclick={resyncKg}
        disabled={resyncingKg}
        title="Re-run kg-sync --all on this project's knowledge/ and docs/ folders"
      >
        {resyncingKg ? 'Starting…' : 'Re-sync KG'}
      </button>
      <button
        class="rebuild-btn"
        onclick={rebuildKgSummaries}
        disabled={rebuildingSummaries}
        title="Re-run generate-kg-summary.py over knowledge/**/*.md for this project (regenerates .node_formats.json summaries)"
      >
        {rebuildingSummaries ? 'Starting…' : 'Re-build KG summaries'}
      </button>
    {/if}
  </header>

  {#if project}
    <!-- Background-task banners (KG summary v0.2.3; KG sync 2026-05-12;
         code-graph Gap 2). Stacked vertically below the header. Each
         banner self-manages its visibility: idle/old-terminal states
         unmount themselves so this region collapses to zero height when
         nothing's happening. Order: KG summary on top — newest task,
         per the v0.2.2 sort rule ("most recently started"; add-project
         spawns code-graph FIRST, then KG-sync, then KG-summary, so the
         summary banner is the newest one and renders above the older
         two). For the launcher-boot resume path the relative ordering
         is arbitrary since all three are re-spawned in the same setup()
         pass; keeping the spawn-order-on-add-project sort is the cheap
         rule. -->
    <KgSummaryBanner projectId={project.id} />
    <KgSyncBanner projectId={project.id} />
    <CodeGraphBuildBanner projectId={project.id} />
  {/if}

  <!--
    Orchestrator-update banner.

    Edge-case-only after PR-151 (2026-05-06): `inspect_orchestrator_at`
    only returns `installed: true` for actual VCO clones (gated by
    `vct-module.json` presence). Normal user-project folders return
    `installed: false`, so this banner stays hidden for them. The
    banner DOES appear when a user has registered the VCO clone
    itself as a project (a legitimate edge case for orchestrator
    self-development); in that case the banner offers the same
    `update_orchestrator_at` flow that lives on the Settings tab.

    Followup-#14 review (2026-05-07): banner kept rather than removed.
    `runUpdate` here calls the SAME backend command as
    `Settings → Update orchestrator`, so the two paths can't drift.
    Removing it would silently lose the edge-case ability to update
    from the project page; leaving it preserves it without breakage
    for normal projects.

    The guard chain that keeps this safe:
      1. `inspect_orchestrator_at` returns `installed: false` for
         project folders (PR-151).
      2. `update_orchestrator_at` (the handler) is gated by
         `validate_source_repo` (also PR-151) — refuses to run on
         non-VCO targets even if the GUI somehow tried.
  -->
  {#if orchState && orchState.installed && orchState.version_status === 'outdated' && orchState.bundled_version}
    <div class="orch-banner">
      <span class="orch-banner-text">
        Orchestrator clone at this path
        {#if orchState.version}v{orchState.version}{/if}
        — bundled launcher ships v{orchState.bundled_version}
      </span>
      <button class="orch-banner-btn" onclick={runUpdate} disabled={updating}>
        {updating ? 'Updating…' : 'Update orchestrator clone'}
      </button>
    </div>
  {/if}

  <nav class="tab-nav">
    {#each tabs as tab}
      <button
        class="tab-btn"
        class:active={activeTab === tab.id}
        onclick={() => (activeTab = tab.id)}
      >{tab.label}</button>
    {/each}
  </nav>

  <main class="tab-content">
    {#if !project}
      <p class="loading">Loading…</p>
    {:else if activeTab === 'identity'}
      <IdentityTab projectId={project.id} />
    {:else if activeTab === 'agents'}
      <AgentsTab projectId={project.id} />
    {:else if activeTab === 'skills'}
      <SkillsTab projectId={project.id} />
    {:else if activeTab === 'hooks'}
      <HooksTab projectId={project.id} />
    {:else if activeTab === 'permissions'}
      <PermissionsTab projectId={project.id} />
    {:else if activeTab === 'access'}
      <CrossProjectAccessTab projectId={project.id} />
    {:else if activeTab === 'secrets'}
      <SecretsTab projectId={project.id} />
    {:else if activeTab === 'kg'}
      <KgCodegraphTab projectId={project.id} />
    {:else if activeTab === 'settings'}
      <!-- v0.2.22 Item #14: inlined. SettingsTab is the extracted body of
           the /project/[id]/settings route, mounted directly so users
           reach the form on a single click. The standalone route is
           still valid and delegates to the same component. -->
      <SettingsTab projectId={project.id} />
    {:else if activeTab === 'orchestrator_core' && isOrchestratorProject}
      <!-- v0.2.23 F2 (2026-05-21): the previously-separate "Orchestrator
           Core" sidebar surface now lives here, as a tab on the
           orchestrator-root project. Reuses ModuleConfigTab.svelte —
           the same renderer paid modules go through — so the controls,
           tooltips, confirm prompts, and Tauri-command wiring stay
           single-sourced from vct-module.json. The `isOrchestratorProject`
           guard prevents a stale activeTab=`orchestrator_core` from
           rendering for a non-orchestrator project (defensive — the
           tab only ever appears in `tabs` for orchestrator-root). -->
      <OrchestratorCoreTab />
    {/if}
  </main>
</div>

<Toast />

<style>
  .project-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .project-header {
    display: flex; align-items: center; gap: 16px;
    padding: 14px 24px; border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .project-title { flex: 1; }
  .rebuild-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 6px 12px; border-radius: 4px;
    cursor: pointer; font-size: 12px;
    flex-shrink: 0;
  }
  .rebuild-btn:hover:not(:disabled) {
    background: rgba(0,191,166,0.10);
    border-color: rgba(0,191,166,0.3);
    color: rgb(0,191,166);
  }
  .rebuild-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .back-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 13px;
  }
  .back-btn:hover { background: rgba(255,255,255,0.1); }
  .project-title h1 { margin: 0; font-size: 18px; }
  .project-meta {
    margin: 4px 0 0; font-size: 12px; color: #888;
    display: flex; align-items: center; gap: 8px;
    flex-wrap: wrap;
  }
  .project-meta code { font-family: ui-monospace, monospace; }
  .host-badge {
    display: inline-block; padding: 1px 8px; border-radius: 10px;
    font-size: 10px; text-transform: uppercase; font-weight: 600;
    margin-left: 8px;
  }
  .host-base { background: rgba(0,191,166,0.15); color: #0fc; }
  .host-mao { background: rgba(123,95,255,0.15); color: #c4b3ff; }

  /* Bug 21: outdated-orchestrator banner */
  .orch-banner {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px;
    padding: 10px 24px;
    background: rgba(245,179,66,0.1);
    border-bottom: 1px solid rgba(245,179,66,0.25);
    color: #f5b342;
    font-size: 13px;
  }
  .orch-banner-text { line-height: 1.4; }
  .orch-banner-btn {
    background: rgba(0,191,166,0.9); border: 1px solid rgba(0,191,166,1);
    color: #000; font-weight: 600;
    padding: 6px 14px; border-radius: 6px; cursor: pointer;
    font-size: 12px;
  }
  .orch-banner-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .tab-nav { display: flex; padding: 0 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .tab-btn {
    background: none; border: none; color: #888; padding: 12px 16px;
    cursor: pointer; font-size: 13px;
    border-bottom: 2px solid transparent;
  }
  .tab-btn:hover { color: #ccc; }
  .tab-btn.active {
    color: #fff;
    border-bottom-color: rgb(0,191,166);
  }
  .tab-content { max-width: 1200px; margin: 0 auto; }
  .loading { padding: 40px; text-align: center; color: #888; }
</style>
