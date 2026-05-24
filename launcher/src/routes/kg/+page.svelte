<script lang="ts">
  import { selectedProject } from '$lib/stores/projects';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import Term from '$lib/components/Term.svelte';
  import NoProjectBanner from '$lib/components/NoProjectBanner.svelte';
  import CollectionList from '$lib/kg/CollectionList.svelte';
  import CollectionViewer from '$lib/kg/CollectionViewer.svelte';
  import AccessModal from '$lib/access/AccessModal.svelte';
  import type { AccessMode } from '$lib/types/project-state';
  import type { KgNode } from '$lib/types/project-state';

  let view = $state<'list' | 'viewer'>('list');
  let activeCollection = $state<string | null>(null);

  // Access modal state
  let modalOpen = $state(false);
  let modalLabel = $state('');
  let modalInitial = $state<AccessMode | null>(null);
  let modalKind = $state<'collection' | 'node' | 'node-bulk'>('collection');
  let modalCollection = $state('');
  let modalNodeId = $state<string | null>(null);
  let modalNodeIds = $state<string[]>([]);

  const project = $derived($selectedProject);

  function onBrowse(c: string) {
    activeCollection = c;
    view = 'viewer';
  }

  function onAccessCollection(c: string) {
    if (!project) return;
    modalKind = 'collection';
    modalCollection = c;
    modalNodeId = null;
    modalLabel = `Collection: ${c}`;
    modalInitial = { mode: 'private', project_ids: [], owner_project_id: project.id };
    modalOpen = true;
  }

  function onShareNode(node: KgNode) {
    if (!project) return;
    modalKind = 'node';
    modalCollection = node.collection;
    modalNodeId = node.id;
    modalNodeIds = [];
    modalLabel = `Node: ${node.title}`;
    modalInitial = { mode: 'private', project_ids: [], owner_project_id: project.id };
    modalOpen = true;
  }

  function onBulkShareNodes(nodeIds: string[]) {
    if (!project || !activeCollection || nodeIds.length === 0) return;
    modalKind = 'node-bulk';
    modalCollection = activeCollection;
    modalNodeId = null;
    modalNodeIds = nodeIds;
    modalLabel = `${nodeIds.length} nodes in ${activeCollection}`;
    modalInitial = { mode: 'private', project_ids: [], owner_project_id: project.id };
    modalOpen = true;
  }

  async function handleSave(mode: AccessMode) {
    if (!project) throw new Error('no project selected');
    if (modalKind === 'collection') {
      await invoke('kg_set_collection_access_mode', {
        req: {
          owner_project_id: project.id,
          collection: modalCollection,
          mode: mode.mode,
          project_ids: mode.project_ids,
        },
      });
    } else if (modalKind === 'node-bulk' && modalNodeIds.length > 0) {
      // Ensure schema first (best effort).
      try {
        await invoke('kg_ensure_node_access_schema', { collection: modalCollection });
      } catch (e) {
        console.warn('[kg_ensure_node_access_schema]', e);
      }
      const result = await invoke<{ succeeded: number; failed: number; failures: { id: string; error: string }[] }>(
        'kg_set_node_access_bulk',
        {
          req: {
            project_id: project.id,
            collection: modalCollection,
            node_ids: modalNodeIds,
            mode: mode.mode,
            project_ids: mode.project_ids,
          },
        },
      );
      if (result.failed > 0) {
        toast.error(`${result.succeeded} updated, ${result.failed} failed (first: ${result.failures[0]?.error ?? 'unknown'})`);
      } else {
        toast.success(`${result.succeeded} node${result.succeeded === 1 ? '' : 's'} updated`);
      }
    } else if (modalNodeId) {
      // Ensure schema first (best effort, ignore "already exists")
      try {
        await invoke('kg_ensure_node_access_schema', { collection: modalCollection });
      } catch (e) {
        console.warn('[kg_ensure_node_access_schema]', e);
      }
      await invoke('kg_set_node_access', {
        req: {
          project_id: project.id,
          collection: modalCollection,
          node_id: modalNodeId,
          mode: mode.mode,
          project_ids: mode.project_ids,
        },
      });
    }
  }

  function backToList() {
    view = 'list';
    activeCollection = null;
  }
</script>

<svelte:head>
  <title>Knowledge Graph — VCT Launcher</title>
</svelte:head>

<div class="kg-page">
  <header class="kg-pageheader">
    <button class="kg-back" onclick={() => goto('/')}>← Back</button>
    <h1><Term key="kg">Knowledge Graph</Term></h1>
    {#if project}
      <span class="kg-project">acting as <code>{project.name}</code></span>
    {:else}
      <span class="kg-warn">No project selected — select one to enable access enforcement.</span>
    {/if}
  </header>

  <main class="kg-main">
    {#if !project}
      <NoProjectBanner section="this project's Knowledge Graph" />
    {:else if view === 'list'}
      <CollectionList
        projectId={project.id}
        onBrowse={onBrowse}
        onAccess={onAccessCollection}
      />
    {:else if activeCollection}
      <CollectionViewer
        projectId={project.id}
        collection={activeCollection}
        onBack={backToList}
        onShareNode={onShareNode}
        onBulkShareNodes={onBulkShareNodes}
      />
    {/if}
  </main>

  {#if modalOpen}
    <AccessModal
      targetLabel={modalLabel}
      initial={modalInitial}
      onSave={handleSave}
      onClose={() => (modalOpen = false)}
    />
  {/if}
</div>

<Toast />

<style>
  .kg-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); display: flex; flex-direction: column; }
  .kg-pageheader {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .kg-pageheader h1 { font-size: 16px; margin: 0; }
  .kg-back {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .kg-project { color: #888; font-size: 12px; }
  .kg-project code { color: #c4b3ff; font-family: ui-monospace, monospace; }
  .kg-warn { color: #fa8; font-size: 12px; }
  .kg-main { flex: 1; min-height: 0; }
</style>
