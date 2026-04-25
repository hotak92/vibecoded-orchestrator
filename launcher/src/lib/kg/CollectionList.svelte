<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgCollectionAccess } from '$lib/types/project-state';

  let {
    projectId,
    onBrowse,
    onAccess,
  }: {
    projectId: string;
    onBrowse: (collection: string) => void;
    onAccess: (collection: string) => void;
  } = $props();

  let collections = $state<KgCollectionAccess[]>([]);
  let loading = $state(true);

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
      {#each collections as col}
        <article class="cl-card">
          <header class="cl-card-h">
            <strong>{col.name}</strong>
            {#if col.is_shared}<span class="cl-badge cl-badge-shared">shared</span>{/if}
            <span class="cl-access cl-access-{col.access}">{col.access}</span>
          </header>
          <p class="cl-meta">{col.node_count} nodes</p>
          <div class="cl-actions">
            <button onclick={() => onBrowse(col.name)} disabled={col.access === 'none'}>Browse</button>
            <button onclick={() => onAccess(col.name)}>Access…</button>
          </div>
        </article>
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
  .cl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
  .cl-card {
    background: rgba(255,255,255,0.04); padding: 10px 12px; border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.08);
  }
  .cl-card-h { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .cl-card-h strong { font-size: 13px; flex: 1; min-width: 100px; }
  .cl-badge {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(0,191,166,0.15); color: #0fc;
  }
  .cl-access {
    font-size: 10px; padding: 1px 6px; border-radius: 8px; text-transform: uppercase;
  }
  .cl-access-read { background: rgba(123,95,255,0.2); color: #c4b3ff; }
  .cl-access-write { background: rgba(0,191,166,0.2); color: #0fc; }
  .cl-access-none { background: rgba(255,99,99,0.15); color: #f99; }
  .cl-meta { font-size: 11px; color: #888; margin: 4px 0 8px; }
  .cl-actions { display: flex; gap: 6px; }
  .cl-actions button {
    flex: 1;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;
  }
  .cl-actions button:hover { background: rgba(255,255,255,0.12); }
  .cl-actions button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
