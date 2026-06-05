<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgCollectionAccess } from '$lib/types/project-state';
  import { groupCollections } from './collection-grouping';
  import CollectionGroupCard from './CollectionGroupCard.svelte';

  let {
    projectId,
    onBrowse,
    onAccess,
  }: {
    projectId: string;
    onBrowse: (collection: string) => void;
    /** Open the access modal for one or more collections at once. */
    onAccess: (collections: string[]) => void;
  } = $props();

  let collections = $state<KgCollectionAccess[]>([]);
  let loading = $state(true);

  // Three sibling collections per project ({Prefix}_KnowledgeGraph /
  // _Development / _Diagrams) collapse into one stacked card whose access
  // applies to all members at once.
  const groups = $derived(groupCollections(collections));

  async function load() {
    loading = true;
    try {
      collections = await invoke<KgCollectionAccess[]>('kg_list_collections', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
</script>

<div class="cl-host">
  <header class="cl-header">
    <h2>KG Collections</h2>
    <button class="cl-refresh" onclick={load}>Refresh</button>
  </header>
  {#if loading}
    <p class="cl-empty">Loading…</p>
  {:else if collections.length === 0}
    <p class="cl-empty">No Weaviate collections found.</p>
  {:else}
    <div class="cl-grid">
      {#each groups as group (group.prefix)}
        <CollectionGroupCard {group} {onBrowse} {onAccess} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .cl-host { padding: 16px 24px; }
  .cl-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .cl-header h2 { font-size: 16px; margin: 0; }
  .cl-refresh {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .cl-empty { color: #888; padding: 24px; text-align: center; }
  .cl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
</style>
