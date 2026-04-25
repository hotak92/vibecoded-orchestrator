<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    ProjectStateSnapshot,
    ProjectKgBinding,
    ProjectCodegraphBinding,
  } from '$lib/types/project-state';

  let { projectId }: { projectId: string } = $props();

  let snapshot = $state<ProjectStateSnapshot | null>(null);
  let loading = $state(true);

  // KG form
  let kgRole = $state<'primary' | 'shared' | 'archive'>('primary');
  let kgCollection = $state('');
  let kgEmbedding = $state('');
  let kgWeaviateUrl = $state('');

  // Codegraph form
  let cgPrefix = $state('');
  let cgEmbedding = $state('');
  let cgEnabled = $state(true);

  async function load() {
    loading = true;
    try {
      snapshot = await invoke<ProjectStateSnapshot>('get_project_state_snapshot', { projectId });
      const primary = snapshot.kg_bindings.find((b) => b.role === kgRole) ?? snapshot.kg_bindings[0];
      if (primary) {
        kgRole = (primary.role as any) ?? 'primary';
        kgCollection = primary.collection_name;
        kgEmbedding = primary.embedding_model ?? '';
        kgWeaviateUrl = primary.weaviate_url ?? '';
      }
      if (snapshot.codegraph_binding) {
        cgPrefix = snapshot.codegraph_binding.collection_prefix;
        cgEmbedding = snapshot.codegraph_binding.embedding_model ?? '';
        cgEnabled = snapshot.codegraph_binding.enabled;
      }
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function saveKg() {
    if (!kgCollection.trim()) {
      toast.error('Collection name required');
      return;
    }
    try {
      await invoke<ProjectKgBinding>('set_project_kg_binding', {
        projectId,
        req: {
          role: kgRole,
          collection_name: kgCollection.trim(),
          embedding_model: kgEmbedding.trim() || null,
          weaviate_url: kgWeaviateUrl.trim() || null,
          config: {},
        },
      });
      toast.success('KG binding saved');
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  async function saveCg() {
    if (!cgPrefix.trim()) {
      toast.error('Collection prefix required');
      return;
    }
    try {
      await invoke<ProjectCodegraphBinding>('set_project_codegraph_binding', {
        projectId,
        req: {
          collection_prefix: cgPrefix.trim(),
          embedding_model: cgEmbedding.trim() || null,
          enabled: cgEnabled,
          config: {},
        },
      });
      toast.success('Codegraph binding saved');
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(load);
  $effect(() => {
    if (projectId) void load();
  });
</script>

<section class="ps-tab">
  {#if loading}
    <p class="ps-loading">Loading…</p>
  {:else}
    <header class="ps-tab-header">
      <h3>KG / Codegraph bindings</h3>
    </header>

    <div class="ps-section">
      <h4>Knowledge graph</h4>
      <div class="ps-form-grid">
        <label>
          <span>Role</span>
          <select bind:value={kgRole}>
            <option value="primary">primary</option>
            <option value="shared">shared</option>
            <option value="archive">archive</option>
          </select>
        </label>
        <label>
          <span>Collection</span>
          <input bind:value={kgCollection} placeholder="ClaudeKnowledgeGraph" />
        </label>
        <label>
          <span>Embedding model</span>
          <input bind:value={kgEmbedding} placeholder="qwen3-embedding:0.6b" />
        </label>
        <label>
          <span>Weaviate URL</span>
          <input bind:value={kgWeaviateUrl} placeholder="http://localhost:8081" />
        </label>
      </div>
      <button class="ps-btn-primary" onclick={saveKg}>Save KG binding</button>
    </div>

    <div class="ps-section">
      <h4>Code graph</h4>
      <div class="ps-form-grid">
        <label>
          <span>Collection prefix</span>
          <input bind:value={cgPrefix} placeholder="MyProject_" />
        </label>
        <label>
          <span>Embedding model</span>
          <input bind:value={cgEmbedding} placeholder="codesage-large-v2" />
        </label>
        <label class="ps-checkbox">
          <input type="checkbox" bind:checked={cgEnabled} />
          <span>Enabled</span>
        </label>
        {#if snapshot?.codegraph_binding?.last_analyzed_commit}
          <div class="ps-meta">
            Last analyzed:
            <code>{snapshot.codegraph_binding.last_analyzed_commit.slice(0, 8)}</code>
          </div>
        {/if}
      </div>
      <button class="ps-btn-primary" onclick={saveCg}>Save codegraph binding</button>
    </div>

    <div class="ps-section">
      <h4>Snapshot summary</h4>
      <div class="ps-snapshot">
        <div><strong>Agents:</strong> {snapshot?.agents.length ?? 0}</div>
        <div><strong>Skills:</strong> {snapshot?.skills.length ?? 0}</div>
        <div><strong>Hooks:</strong> {snapshot?.hooks.length ?? 0}</div>
        <div><strong>Permissions:</strong> {snapshot?.permissions.length ?? 0}</div>
        <div><strong>Secret refs:</strong> {snapshot?.secret_refs.length ?? 0}</div>
      </div>
    </div>
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-loading { color: #888; padding: 24px; text-align: center; }
  .ps-section { margin-bottom: 20px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 6px; }
  .ps-section h4 { font-size: 13px; margin: 0 0 12px; color: #c4b3ff; }
  .ps-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input, .ps-form-grid select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 6px 10px; border-radius: 4px; font-size: 13px;
  }
  .ps-form-grid .ps-checkbox { flex-direction: row; align-items: center; gap: 8px; }
  .ps-form-grid .ps-checkbox input { width: auto; }
  .ps-meta { font-size: 11px; color: #888; align-self: end; }
  .ps-meta code { background: rgba(255,255,255,0.05); padding: 1px 5px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-snapshot { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; font-size: 13px; }
  .ps-snapshot div { padding: 8px 12px; background: rgba(255,255,255,0.04); border-radius: 4px; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600; }
</style>
