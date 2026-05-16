<script lang="ts">
  // PR-26 / Group E (v0.2.12 / 2026-05-16): shared KG canonical-name picker.
  //
  // Surfaces all orchestrator-shaped KG classes detected on Weaviate (via
  // the existing `hub::cli_api::detect_orchestrator_kg_collections` probe
  // wrapped by the new `list_orchestrator_kg_collections` Tauri command)
  // and lets the user pick which one is the canonical shared KG for this
  // install.
  //
  // Use case: a v0.2.11 user who already has a populated
  // `VibeCodedTools_KnowledgeGraph` class (the pre-rename canonical name)
  // sees the new v0.2.12 default `VibecodedOrchestrator_KnowledgeGraph`
  // doesn't match anything on disk. Picker lets them keep the existing
  // data without manual env-file editing or running the migration script.
  //
  // Behaviour rules:
  //   - READ-ONLY: picking a name only updates the persisted canonical
  //     name (`app_state[shared_kg.collection_name]`); no class is
  //     created, renamed, or deleted on Weaviate.
  //   - One-click confirm: this isn't a destructive op, so no extra
  //     checkbox gate (unlike the legacy-collections cleanup modal).
  //   - Soft-fail: persist errors surface as toasts via the parent; the
  //     modal stays open until the parent closes it after a successful
  //     pick (so the user sees the saving spinner).

  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let {
    candidates,
    currentName,
    onPick,
    onClose,
  }: {
    candidates: string[];
    currentName: string;
    onPick: (picked: string) => void | Promise<void>;
    onClose: () => void;
  } = $props();

  let pickInProgress = $state<string | null>(null);

  async function handlePick(name: string) {
    if (pickInProgress) return;
    pickInProgress = name;
    try {
      await onPick(name);
    } finally {
      pickInProgress = null;
    }
  }
</script>

<DialogRoot open={true} width="560px" onClose={onClose}>
  {#snippet header()}
    <div class="skp-header">
      <h3>Pick canonical shared KG collection</h3>
      <p>
        Your Weaviate has <strong>{candidates.length}</strong> orchestrator-shaped
        KG classes. The current canonical name
        (<code>{currentName}</code>) doesn't match any of them — pick which
        one this install should treat as the shared KG.
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if candidates.length === 0}
      <p class="skp-empty">
        No orchestrator-shaped KG classes detected. Either Weaviate is
        unreachable or no class has the required marker properties
        (<code>title</code>, <code>node_type</code>, <code>tags</code>,
        <code>typed_links</code>).
      </p>
    {:else}
      <section class="skp-section">
        <h4>Detected collections</h4>
        <ul class="skp-list">
          {#each candidates as cand (cand)}
            <li class="skp-item">
              <code class="skp-name" class:is-current={cand === currentName}>
                {cand}
              </code>
              <button
                class="skp-pick-btn"
                onclick={() => handlePick(cand)}
                disabled={pickInProgress !== null}
              >
                {pickInProgress === cand ? 'Saving…' : 'Use as canonical'}
              </button>
            </li>
          {/each}
        </ul>
        <p class="skp-hint">
          Picking a name updates the launcher's persisted canonical
          (<code>app_state.shared_kg.collection_name</code>) and re-emits
          the <code>SHARED_KG_COLLECTION</code> env value to every
          project's three env surfaces on next refresh. No class is
          created, renamed, or deleted on Weaviate.
        </p>
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="skp-footer">
      <button class="skp-btn" onclick={onClose} disabled={pickInProgress !== null}>
        Cancel
      </button>
    </div>
  {/snippet}
</DialogRoot>

<style>
  .skp-header h3 { margin: 0; font-size: 14px; }
  .skp-header p {
    margin: 6px 0 0;
    font-size: 12px;
    color: #aaa;
    line-height: 1.5;
  }
  .skp-header code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .skp-empty {
    color: #888;
    padding: 24px;
    text-align: center;
    font-size: 12px;
  }
  .skp-empty code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
  }
  .skp-section {
    margin-bottom: 12px;
  }
  .skp-section h4 {
    font-size: 12px;
    margin: 0 0 8px;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .skp-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .skp-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 10px;
    background: rgba(255,255,255,0.03);
    border-radius: 4px;
  }
  .skp-name {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #ddd;
    word-break: break-all;
  }
  .skp-name.is-current {
    color: #0fc;
    border: 1px dashed rgba(0,191,166,0.4);
    padding: 1px 6px;
    border-radius: 3px;
  }
  .skp-hint {
    margin: 10px 0 0;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .skp-hint code {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .skp-pick-btn {
    background: rgba(0,191,166,0.15);
    border: 1px solid rgba(0,191,166,0.35);
    color: #0fc;
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    flex-shrink: 0;
  }
  .skp-pick-btn:hover:not(:disabled) {
    background: rgba(0,191,166,0.22);
  }
  .skp-pick-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .skp-footer {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .skp-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .skp-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .skp-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
