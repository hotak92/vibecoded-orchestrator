<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.102 W5-A (M3): "Infrastructure this project owns".
  //
  // A project that runs its own Weaviate / Ollama next to the dev KG VCO
  // keeps for it declares those pieces here, and VCO leaves them alone:
  //   - external collections -> skipped by the doctor's unclaimed / fixture
  //     readings and by the binding-evidence heal;
  //   - unmanaged containers -> never started, healed, re-created, labelled
  //     or adopted (hub watchdog, compose recovery, service detection);
  //   - local ports -> projected as VCT_SSRF_EXTRA_LOCAL_PORTS (WebFetch may
  //     reach them from this project) and never probed for adoption.
  //
  // Backend: commands::infra_declarations::{get,set}_project_infra_declarations
  // (migration 049). Each save replaces ONE kind's list and re-projects the
  // project env; the panel re-renders from the stored result.

  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    EMPTY_DECLARATIONS,
    KIND_SPECS,
    addValue,
    removeValue,
    setPayload,
    type InfraDeclarations,
    type InfraDeclarationsWriteResult,
    type KindSpec,
  } from '$lib/project-state/infra-declarations';

  let { projectId }: { projectId: string } = $props();

  let declarations = $state<InfraDeclarations>({ ...EMPTY_DECLARATIONS });
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let saving = $state<string | null>(null);
  let drafts = $state<Record<string, string>>({});
  let errors = $state<Record<string, string>>({});

  async function load(pid: string) {
    loading = true;
    loadError = null;
    try {
      declarations = await invoke<InfraDeclarations>('get_project_infra_declarations', {
        projectId: pid,
      });
    } catch (e) {
      loadError = String(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void load(projectId);
  });

  async function save(spec: KindSpec, values: string[]) {
    saving = spec.kind;
    try {
      const result = await invoke<InfraDeclarationsWriteResult>(
        'set_project_infra_declarations',
        setPayload(projectId, spec.kind, values),
      );
      declarations = result.declarations;
      drafts = { ...drafts, [spec.kind]: '' };
      errors = { ...errors, [spec.kind]: '' };
      for (const w of result.warnings) toast.info(w);
      toast.success(`${spec.label} saved`);
    } catch (e) {
      errors = { ...errors, [spec.kind]: String(e) };
    } finally {
      saving = null;
    }
  }

  function onAdd(spec: KindSpec) {
    const next = addValue(spec.kind, declarations[spec.field], drafts[spec.kind] ?? '');
    if ('error' in next) {
      errors = { ...errors, [spec.kind]: next.error };
      return;
    }
    void save(spec, next.values);
  }

  function onRemove(spec: KindSpec, value: string) {
    void save(spec, removeValue(declarations[spec.field], value));
  }
</script>

<section class="ps-section infra-panel" data-testid="infra-declarations-panel">
  <h4>Infrastructure this project owns</h4>
  <p class="ps-form-hint">
    Declare what this project runs for itself, so VCO never mistakes it for its own. A
    container or a collection is one object on the machine, so a declaration here protects it
    everywhere.
  </p>

  {#if loading}
    <p class="ps-form-hint">Loading...</p>
  {:else if loadError}
    <p class="infra-error" role="alert">Could not load the declarations: {loadError}</p>
  {:else}
    {#each KIND_SPECS as spec (spec.kind)}
      <div class="infra-kind">
        <h5>{spec.label}</h5>
        <p class="ps-form-hint">{spec.hint}</p>
        {#if declarations[spec.field].length === 0}
          <p class="ps-form-hint infra-empty">None declared.</p>
        {:else}
          <ul class="infra-list">
            {#each declarations[spec.field] as value (value)}
              <li>
                <code>{value}</code>
                <button
                  class="ps-btn-secondary"
                  disabled={saving !== null}
                  aria-label={`Remove ${value}`}
                  onclick={() => onRemove(spec, value)}
                >
                  Remove
                </button>
              </li>
            {/each}
          </ul>
        {/if}
        <form
          class="infra-add"
          onsubmit={(e) => {
            e.preventDefault();
            onAdd(spec);
          }}
        >
          <input
            type="text"
            aria-label={`Add to ${spec.label}`}
            placeholder={spec.placeholder}
            value={drafts[spec.kind] ?? ''}
            oninput={(e) => (drafts = { ...drafts, [spec.kind]: (e.target as HTMLInputElement).value })}
          />
          <button class="ps-btn-primary" type="submit" disabled={saving !== null}>
            {saving === spec.kind ? 'Saving...' : 'Add'}
          </button>
        </form>
        {#if errors[spec.kind]}
          <p class="infra-error" role="alert">{errors[spec.kind]}</p>
        {/if}
      </div>
    {/each}
  {/if}
</section>

<style>
  .infra-panel { margin-top: 16px; }
  .infra-kind { margin-top: 12px; }
  .infra-kind h5 { margin: 0 0 4px; font-size: 0.95rem; }
  .infra-list { list-style: none; padding: 0; margin: 6px 0; }
  .infra-list li { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
  .infra-add { display: flex; gap: 8px; margin-top: 6px; }
  .infra-add input { flex: 1; min-width: 0; }
  .infra-empty { font-style: italic; }
  .infra-error { color: var(--color-pink); margin: 4px 0 0; }
</style>
