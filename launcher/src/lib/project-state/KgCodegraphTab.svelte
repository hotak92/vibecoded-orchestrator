<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    ProjectStateSnapshot,
    ProjectKgBinding,
    ProjectCodegraphBinding,
  } from '$lib/types/project-state';
  import type {
    EmbeddingCatalog,
    ModelChoice,
  } from '$lib/types/embedding-catalog';
  import Dropdown from '$lib/components/Dropdown.svelte';

  const ROLE_OPTIONS = [
    { value: 'primary', label: 'primary' },
    { value: 'shared', label: 'shared' },
    { value: 'archive', label: 'archive' },
  ];

  let { projectId }: { projectId: string } = $props();

  let snapshot = $state<ProjectStateSnapshot | null>(null);
  let loading = $state(true);

  // KG form
  let kgRole = $state<'primary' | 'shared' | 'archive'>('primary');
  let kgCollection = $state('');
  let kgEmbedding = $state<string>('');
  let kgWeaviateUrl = $state('');

  // Codegraph form
  let cgPrefix = $state('');
  let cgEmbedding = $state<string>('');
  let cgEnabled = $state(true);

  // v0.2.18 (Commit 8): embedding catalog populates both dropdowns.
  // Loaded once on mount; refreshed on retry. Errors fall back to
  // empty lists — the Dropdown component handles that gracefully by
  // showing the placeholder.
  let catalog = $state<EmbeddingCatalog | null>(null);
  let catalogError = $state<string | null>(null);

  // Track the previously-saved binding model so the change-detected
  // event only fires on actual changes (not on the initial population).
  let kgEmbeddingInitial = '';
  let cgEmbeddingInitial = '';

  function buildOptions(models: ModelChoice[]) {
    // Each option carries its label + an optional disabled flag mapped
    // from `available_now`. Tooltip text on the trigger comes from the
    // reason_unavailable field via the option's title attribute.
    return models.map((m) => ({
      value: m.id,
      label: m.available_now ? m.label : `${m.label} (unavailable)`,
      disabled: !m.available_now,
      title: m.reason_unavailable ?? undefined,
    }));
  }

  async function loadCatalog() {
    catalogError = null;
    try {
      catalog = await invoke<EmbeddingCatalog>('get_embedding_catalog', {
        projectId,
      });
      // Surface non-fatal errors (e.g. one provider failed but others
      // worked) inline so the user knows what's missing.
      if (catalog.errors && catalog.errors.length > 0) {
        catalogError = catalog.errors.join('; ');
      }
    } catch (e) {
      catalog = null;
      catalogError = String(e);
    }
  }

  async function load() {
    loading = true;
    try {
      snapshot = await invoke<ProjectStateSnapshot>('get_project_state_snapshot', { projectId });
      const primary = snapshot.kg_bindings.find((b) => b.role === kgRole) ?? snapshot.kg_bindings[0];
      if (primary) {
        kgRole = (primary.role as any) ?? 'primary';
        kgCollection = primary.collection_name;
        kgEmbedding = primary.embedding_model ?? '';
        kgEmbeddingInitial = kgEmbedding;
        kgWeaviateUrl = primary.weaviate_url ?? '';
      }
      if (snapshot.codegraph_binding) {
        cgPrefix = snapshot.codegraph_binding.collection_prefix;
        cgEmbedding = snapshot.codegraph_binding.embedding_model ?? '';
        cgEmbeddingInitial = cgEmbedding;
        cgEnabled = snapshot.codegraph_binding.enabled;
      }
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
    // Catalog load is independent — don't let a snapshot failure block
    // it, and vice versa.
    await loadCatalog();
  }

  /** Detect whether the user changed the model and warn them about the
   *  enrichment migration (Commit 9). For v0.2.18 we only emit a Tauri
   *  event the UI logs — the enrichment runner itself is the next commit.
   *  Returns true if the user confirms (or no change was detected). */
  function confirmModelChange(
    kind: 'kg' | 'codegraph',
    fromModel: string,
    toModel: string,
  ): boolean {
    if (fromModel === toModel) return true;
    // Empty → set: no migration needed (fresh binding).
    if (!fromModel) return true;
    const msg = `Changing the ${kind === 'kg' ? 'KG' : 'code graph'} embedding model from
"${fromModel}" to "${toModel}" will require re-embedding all existing
nodes into the new vector slot. The previous slot's vectors will be
preserved (you can revert without data loss).

Continue?`;
    return confirm(msg);
  }

  async function saveKg() {
    if (!kgCollection.trim()) {
      toast.error('Collection name required');
      return;
    }
    if (!confirmModelChange('kg', kgEmbeddingInitial, kgEmbedding)) {
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
      // Placeholder for Commit 9 (enrichment migration): emit a hint
      // the UI can later subscribe to. Today this is logged only.
      if (kgEmbedding && kgEmbeddingInitial && kgEmbedding !== kgEmbeddingInitial) {
        console.log(
          '[vct] KG embedding model changed — enrichment migration pending (Commit 9)',
          { projectId, from: kgEmbeddingInitial, to: kgEmbedding },
        );
      }
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
    if (!confirmModelChange('codegraph', cgEmbeddingInitial, cgEmbedding)) {
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
      if (cgEmbedding && cgEmbeddingInitial && cgEmbedding !== cgEmbeddingInitial) {
        console.log(
          '[vct] Codegraph embedding model changed — enrichment migration pending (Commit 9)',
          { projectId, from: cgEmbeddingInitial, to: cgEmbedding },
        );
      }
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  const textOptions = $derived(
    catalog ? buildOptions(catalog.text_models) : [],
  );
  const codeOptions = $derived(
    catalog ? buildOptions(catalog.code_models) : [],
  );

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

    {#if catalogError}
      <p class="ps-catalog-warn">
        Embedding catalog warning: {catalogError}
        <button class="ps-link-btn" onclick={() => void loadCatalog()}>retry</button>
      </p>
    {/if}

    <div class="ps-section">
      <h4>Knowledge graph</h4>
      <div class="ps-form-grid">
        <label>
          <span>Role</span>
          <Dropdown options={ROLE_OPTIONS} bind:value={kgRole} />
        </label>
        <label>
          <span>Collection</span>
          <input bind:value={kgCollection} placeholder="ClaudeKnowledgeGraph" />
        </label>
        <label>
          <span>Embedding model</span>
          <Dropdown
            options={textOptions}
            bind:value={kgEmbedding}
            placeholder={catalog ? 'Select model…' : 'Loading…'}
            ariaLabel="KG embedding model"
          />
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
          <Dropdown
            options={codeOptions}
            bind:value={cgEmbedding}
            placeholder={catalog ? 'Select model…' : 'Loading…'}
            ariaLabel="Codegraph embedding model"
          />
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
  .ps-catalog-warn {
    margin: 0 0 12px; padding: 8px 12px;
    background: rgba(255,200,80,0.08); border: 1px solid rgba(255,200,80,0.2);
    border-radius: 4px; color: rgb(255,200,120); font-size: 11px;
  }
  .ps-link-btn {
    background: none; border: none; color: rgb(0,191,166); cursor: pointer;
    padding: 0; margin-left: 8px; text-decoration: underline; font-size: 11px;
  }
</style>
