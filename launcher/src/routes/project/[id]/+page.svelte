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
  import CodeGraphBuildPill from '$lib/components/CodeGraphBuildPill.svelte';
  import type { ProjectView } from '$lib/types/launcher';

  let projectId = $derived($page.params.id);
  let project = $state<ProjectView | null>(null);
  let activeTab = $state<'agents' | 'skills' | 'hooks' | 'permissions' | 'secrets' | 'kg' | 'settings'>('agents');

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

  const tabs = [
    { id: 'agents', label: 'Agents' },
    { id: 'skills', label: 'Skills' },
    { id: 'hooks', label: 'Hooks' },
    { id: 'permissions', label: 'Permissions' },
    { id: 'secrets', label: 'Secret refs' },
    { id: 'kg', label: 'KG / Codegraph' },
    { id: 'settings', label: 'Settings' },
  ] as const;
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
          <CodeGraphBuildPill projectId={project.id} />
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
    {/if}
  </header>

  {#if orchState && orchState.installed && orchState.version_status === 'outdated' && orchState.bundled_version}
    <div class="orch-banner">
      <span class="orch-banner-text">
        Orchestrator
        {#if orchState.version}v{orchState.version}{/if}
        — update to v{orchState.bundled_version} available
      </span>
      <button class="orch-banner-btn" onclick={runUpdate} disabled={updating}>
        {updating ? 'Updating…' : 'Update this project'}
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
    {:else if activeTab === 'agents'}
      <AgentsTab projectId={project.id} />
    {:else if activeTab === 'skills'}
      <SkillsTab projectId={project.id} />
    {:else if activeTab === 'hooks'}
      <HooksTab projectId={project.id} />
    {:else if activeTab === 'permissions'}
      <PermissionsTab projectId={project.id} />
    {:else if activeTab === 'secrets'}
      <SecretsTab projectId={project.id} />
    {:else if activeTab === 'kg'}
      <KgCodegraphTab projectId={project.id} />
    {:else if activeTab === 'settings'}
      <a href="/project/{project.id}/settings" class="settings-link">Open project settings →</a>
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
  .settings-link { color: #0fc; padding: 24px; display: block; text-decoration: none; }
</style>
