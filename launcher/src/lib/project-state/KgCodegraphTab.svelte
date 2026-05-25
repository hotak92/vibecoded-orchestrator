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
  import EnrichmentProgressModal from '$lib/components/EnrichmentProgressModal.svelte';
  // v0.2.18 (Plan C): Re-analyze code-graph modal. Forks the enrichment
  // modal's streaming pattern against analyze_code_graph.py --json-progress.
  // Always passes --prune-stale (authoritative refresh); --language is
  // optional and scopes the re-walk to one language at a time.
  import CodeGraphReanalysisModal from '$lib/components/CodeGraphReanalysisModal.svelte';

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

  // v0.2.18 Commit 9 (+ Commit 10 multi-class fix): enrichment-modal
  // driver state. Populated when a user changes the KG or codegraph
  // model + clicks Save; the EnrichmentProgressModal mounts, runs the
  // single-collection Tauri command sequentially across the list,
  // streams vct-enrichment-progress events, and closes on completion.
  //
  // The shape is multi-collection because the codegraph Save path
  // enrich-migrates 5 sibling Code* classes (CodeModule/CodeClass/
  // CodeFunction/CodeAPI/CodeInteraction) — see CODE_COLLECTION_SUFFIXES
  // below. The KG Save path wraps its single collection in a 1-element
  // list to keep the modal contract uniform.
  type CollectionTarget = { name: string; new_slot: string };
  type EnrichmentTarget = { collections: CollectionTarget[] };
  let enrichmentTarget = $state<EnrichmentTarget | null>(null);

  // Canonical code-class suffixes, ORDER-PRESERVING (Python's
  // `_CODE_COLLECTION_SUFFIXES` is a frozenset which has no canonical
  // order; we mirror the order used throughout vco_lib/weaviate_schema.py
  // docstrings + log messages: Module, Class, Function, API, Interaction.
  // The exact order affects only the UX (which class runs first); the
  // correctness guarantee — enriching all 5 — is order-independent.
  const CODE_COLLECTION_SUFFIXES = [
    'CodeModule',
    'CodeClass',
    'CodeFunction',
    'CodeAPI',
    'CodeInteraction',
  ] as const;

  // v0.2.18 (Plan C): Re-analyze code-graph modal driver state. Populated
  // when the user clicks "Re-analyze code graph"; the modal mounts on
  // `reanalysisTarget` population, invokes `reanalyze_code_graph`, streams
  // progress, and closes itself when the user clicks Close in the
  // done-state. The button passes `language: null` for a full
  // multi-language re-walk (Plan C: this prunes globally, the safe
  // primitive for explicit "authoritative refresh" clicks).
  type ReanalysisTarget = { projectName: string; language: string | null };
  let reanalysisTarget = $state<ReanalysisTarget | null>(null);

  function openReanalysisModal() {
    // ProjectStateSnapshot doesn't carry the project's display name; the
    // codegraph_binding's collection_prefix is the closest proxy (used for
    // the modal title only — the Tauri command resolves the canonical
    // project name itself via the DB lookup from projectId).
    const displayName =
      snapshot?.codegraph_binding?.collection_prefix ?? projectId;
    // Full multi-language re-walk by default. Per-language scoping is
    // exposed via the hook path (every file save passes --language for
    // the language-scoped prune); the button is the explicit-refresh
    // path that benefits most from a global walk + global prune.
    reanalysisTarget = { projectName: displayName, language: null };
  }

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
   *  enrichment migration. v0.2.18 Commit 9 wires the actual runner: a
   *  positive confirm here causes saveKg/saveCg to populate
   *  `enrichmentTarget`, which mounts <EnrichmentProgressModal>.
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

  /** Resolve the slot a given model id writes to, via the catalog. */
  function resolveSlot(
    kind: 'kg' | 'codegraph',
    modelId: string,
  ): string | null {
    if (!catalog) return null;
    const pool = kind === 'kg' ? catalog.text_models : catalog.code_models;
    const found = pool.find((m) => m.id === modelId);
    return found ? found.slot : null;
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
      // v0.2.18 Commit 9: when the model genuinely changed, kick the
      // enrichment migration. The modal mounts on `enrichmentTarget`
      // population, invokes the Tauri command, streams progress, and
      // closes itself when the user clicks Close in the done-state.
      if (kgEmbedding && kgEmbeddingInitial && kgEmbedding !== kgEmbeddingInitial) {
        const slot = resolveSlot('kg', kgEmbedding);
        if (slot && kgCollection.trim()) {
          // KG enrichment is single-collection; wrap in a 1-element
          // list to keep the modal's multi-collection contract uniform.
          enrichmentTarget = {
            collections: [
              { name: kgCollection.trim(), new_slot: slot },
            ],
          };
        } else {
          // No slot resolvable (catalog miss). Skip enrichment — the
          // binding write still succeeded, the seed pipeline will pick
          // up the new model on next sync.
          toast.error(
            `Saved binding but couldn't resolve a slot for ${kgEmbedding}; `
            + 'manual enrichment may be needed.',
          );
        }
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
        // Codegraph enrichment targets the per-class collections that
        // the user's prefix expands to. v0.2.18 (Commit 9 + Commit 10):
        // expand to ALL 5 sibling Code* classes (CodeModule, CodeClass,
        // CodeFunction, CodeAPI, CodeInteraction). Pre-Commit-10 we
        // only enriched CodeFunction, which silently left the 4 other
        // classes on the old slot and made search return empty against
        // them (because search_code_graph queries the active slot per
        // EmbeddingService.code_vector_slot). Enriching all 5 is a
        // correctness requirement, not a perf optimisation.
        const slot = resolveSlot('codegraph', cgEmbedding);
        const prefix = cgPrefix.trim();
        if (slot && prefix) {
          enrichmentTarget = {
            collections: CODE_COLLECTION_SUFFIXES.map((suffix) => ({
              name: `${prefix}${suffix}`,
              new_slot: slot,
            })),
          };
        } else {
          toast.error(
            `Saved binding but couldn't resolve a slot for ${cgEmbedding}; `
            + 'manual enrichment may be needed.',
          );
        }
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
      <div class="ps-btn-row">
        <button class="ps-btn-primary" onclick={saveCg}>Save codegraph binding</button>
        <!-- v0.2.18 (Plan C): Re-analyze button. Forces a full multi-
             language re-walk + global prune via analyze_code_graph.py.
             The hook does language-scoped incremental updates on every
             file save; this button is the explicit "authoritative
             refresh" path. -->
        <button
          class="ps-btn-secondary"
          onclick={openReanalysisModal}
          disabled={!cgEnabled}
          title={cgEnabled
            ? 'Force a full re-analysis: walks every supported language and prunes orphan rows.'
            : 'Enable the code graph above before re-analyzing.'}
        >
          Re-analyze code graph
        </button>
      </div>
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

{#if enrichmentTarget}
  <EnrichmentProgressModal
    collections={enrichmentTarget.collections}
    projectId={projectId}
    onClose={() => (enrichmentTarget = null)}
  />
{/if}

<!-- v0.2.18 (Plan C): Re-analyze modal mount. Same lifecycle pattern as
     EnrichmentProgressModal — mounts when reanalysisTarget is non-null,
     unmounts on close. -->
{#if reanalysisTarget}
  <CodeGraphReanalysisModal
    projectId={projectId}
    projectName={reanalysisTarget.projectName}
    language={reanalysisTarget.language}
    onClose={() => (reanalysisTarget = null)}
  />
{/if}

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-loading { color: #888; padding: 24px; text-align: center; }
  .ps-section { margin-bottom: 20px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 6px; }
  .ps-section h4 { font-size: 13px; margin: 0 0 12px; color: #c4b3ff; }
  .ps-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input {
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
  /* v0.2.18 (Plan C): row container for the codegraph save + re-analyze
     buttons so they sit on one line with a small gap. */
  .ps-btn-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .ps-btn-secondary {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.14);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
  }
  .ps-btn-secondary:hover:not(:disabled) { background: rgba(255,255,255,0.10); }
  .ps-btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
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
