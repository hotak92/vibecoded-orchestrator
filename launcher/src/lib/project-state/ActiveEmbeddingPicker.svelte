<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.71 T-B-emb — per-project ACTIVE_EMBEDDING profile picker.
  //
  // First-class per-project embedding CHOICE. Distinct from the
  // KgCodegraphTab per-binding model picker (which sets
  // project_kg_bindings.embedding_model + triggers enrichment) — THIS picker
  // sets the `module_settings/orchestrator-core/active_embedding` PROFILE
  // (qwen3 / arctic / openai) that the hub resolver + config_projection
  // stamp into .claude/{settings.json,env} as ACTIVE_EMBEDDING.
  //
  // Writing here marks the row source="user" (sticky), so the choice
  // SURVIVES bundle/orchestrator updates (no update path overwrites a
  // source=user row; the startup backfill only seeds source=auto rows).
  //
  // A project with no user pick shows the EFFECTIVE value inherited from the
  // machine-global default (app_state[embedding.active_profile]) with an
  // "inherited" badge; saving a pick flips it to "this project" (sticky).

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    EmbeddingCatalog,
    ModelChoice,
  } from '$lib/types/embedding-catalog';

  let { projectId }: { projectId: string } = $props();

  // Resolved state from the backend: { effective, source }.
  interface ActiveEmbeddingState {
    effective: string;
    source: string; // "user" | "auto"
  }

  let loading = $state(true);
  let saving = $state(false);
  let effective = $state('qwen3');
  let source = $state('auto'); // "user" (sticky) | "auto" (inherited)
  let selected = $state('qwen3');
  let options = $state<Array<{ value: string; label: string; available: boolean }>>([]);

  // Catalog text-models map to profiles. The `slot` field encodes which
  // named-vector slot the model writes to; we derive the profile id (the
  // value stored in active_embedding) from the model id — mirror of the
  // backend model→profile maps (project_env_settings.rs::active_profile_for_model
  // et al.). Kept tiny + local; the backend is the source of truth, this
  // only shapes the dropdown labels + availability.
  function profileForModelId(id: string): string | null {
    switch (id) {
      case 'qwen3-embedding:0.6b':
        return 'qwen3';
      case 'snowflake-arctic-embed2:latest':
        return 'arctic';
      case 'openai-text-embedding-3-small':
      case 'text-embedding-3-small':
        return 'openai';
      default:
        return null;
    }
  }

  function labelForProfile(profile: string, model?: ModelChoice): string {
    const base =
      profile === 'qwen3'
        ? 'qwen3 (local, 1024-dim)'
        : profile === 'arctic'
          ? 'arctic (local, 1024-dim)'
          : profile === 'openai'
            ? 'openai (text-embedding-3-small, 1536-dim)'
            : profile;
    return model && !model.available_now
      ? `${base} — unavailable`
      : base;
  }

  function buildOptions(catalog: EmbeddingCatalog | null): void {
    // Always offer the three canonical text profiles; mark availability
    // from the catalog when present (a profile whose backend is down is
    // still selectable but flagged, matching the KgCodegraphTab UX).
    const seen = new Map<string, { value: string; label: string; available: boolean }>();
    for (const m of catalog?.text_models ?? []) {
      const profile = profileForModelId(m.id);
      if (!profile) continue;
      // First model that maps to a profile wins the availability flag.
      if (!seen.has(profile)) {
        seen.set(profile, {
          value: profile,
          label: labelForProfile(profile, m),
          available: m.available_now,
        });
      }
    }
    // Guarantee qwen3 is always present (the floor) even if the catalog
    // failed to load.
    if (!seen.has('qwen3')) {
      seen.set('qwen3', { value: 'qwen3', label: labelForProfile('qwen3'), available: true });
    }
    // Always ensure the currently-effective value is selectable even if the
    // catalog doesn't list it (e.g. codesage as a text profile, or a stale
    // value) so the dropdown can render the current selection.
    if (effective && !seen.has(effective)) {
      seen.set(effective, {
        value: effective,
        label: labelForProfile(effective),
        available: true,
      });
    }
    options = [...seen.values()];
  }

  async function load(): Promise<void> {
    loading = true;
    try {
      const state = await invoke<ActiveEmbeddingState>(
        'get_project_active_embedding',
        { projectId },
      );
      effective = state.effective;
      source = state.source;
      selected = state.effective;
    } catch (e) {
      toast.error(e);
    }
    // Catalog is best-effort — the picker still works (qwen3 floor) if it
    // fails to load.
    let catalog: EmbeddingCatalog | null = null;
    try {
      catalog = await invoke<EmbeddingCatalog>('get_embedding_catalog');
    } catch {
      catalog = null;
    }
    buildOptions(catalog);
    loading = false;
  }

  async function save(): Promise<void> {
    if (!selected) return;
    saving = true;
    try {
      await invoke('set_project_active_embedding', {
        projectId,
        profile: selected,
      });
      // Re-project .claude/{settings.json,env} so ACTIVE_EMBEDDING reflects
      // the new pick immediately (no session restart needed).
      try {
        await invoke('refresh_project_env', { projectId });
      } catch (e) {
        // The DB write succeeded; surface the projection hiccup but don't
        // claim failure of the choice itself.
        toast.error(
          `Saved the embedding choice, but re-projecting env files failed: ${e}`,
        );
      }
      toast.success('Project embedding set (sticky across updates)');
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }

  const dirty = $derived(selected !== effective || source !== 'user');

  onMount(load);
  $effect(() => {
    if (projectId) void load();
  });
</script>

<section class="ps-section">
  <h2>Embedding model (this project)</h2>
  <p class="ps-hint">
    Sets <code>ACTIVE_EMBEDDING</code> for this project — the named-vector slot
    the KG + code-graph index and search against. A choice here is
    <strong>sticky</strong>: it survives bundle / orchestrator updates. Leave it
    inherited to track the machine-global default. This is the embedding
    <em>profile</em>; the per-collection model + re-embedding lives in the
    KG / Code Graph tab.
  </p>

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else}
    <div class="ps-grid">
      <label>
        <span>Profile</span>
        <select bind:value={selected} aria-label="Active embedding profile">
          {#each options as opt (opt.value)}
            <option value={opt.value}>{opt.label}</option>
          {/each}
        </select>
      </label>
      <div class="ps-meta">
        <p>
          <span>Effective:</span> <code>{effective}</code>
          {#if source === 'user'}
            <span class="ps-badge ps-badge-user">this project</span>
          {:else}
            <span class="ps-badge ps-badge-auto">inherited</span>
          {/if}
        </p>
      </div>
    </div>
    <button
      class="ps-btn-primary"
      onclick={save}
      disabled={saving || !dirty}
      title="Pin this project's ACTIVE_EMBEDDING (sticky across updates)"
    >
      {saving ? 'Saving…' : 'Save embedding'}
    </button>
  {/if}
</section>

<style>
  .ps-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-size: 0.72rem;
    vertical-align: middle;
  }
  .ps-badge-user {
    background: rgba(0, 191, 166, 0.18);
    color: #00bfa6;
  }
  .ps-badge-auto {
    background: rgba(123, 95, 255, 0.16);
    color: #7b5fff;
  }
</style>
