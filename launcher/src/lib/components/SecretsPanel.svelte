<script lang="ts">
  // Secrets manager.
  //
  // - Scope toggle: per-project vs shared (per-project) vs global (machine).
  // - List of currently-known KEY entries with Set / Update / Clear + status.
  // - Form to add a new secret KEY+VALUE (the user picks the module_id and
  //   the key name; values never leave Rust process memory).
  // - Sensitive secrets show ••••••• when set; non-sensitive show a masked
  //   preview returned by `get_secret_preview`.
  //
  // The secrets backend is CRUD-by-key — there's no list endpoint. So we
  // track an in-memory set of KEYs in the secrets store. Default seed:
  // a few well-known keys so a fresh user sees something to fill in.

  import { onMount } from 'svelte';
  import { secrets, type SecretEntry, type SecretScope } from '$lib/stores/secrets';
  import { selectedProject } from '$lib/stores/projects';

  let scope = $state<SecretScope>('per_project');

  // Add-form state
  let newModuleId = $state('');
  let newKey = $state('');
  let newValue = $state('');
  let newSensitive = $state(true);
  let newError = $state<string | null>(null);
  let busy = $state(false);

  // Edit-row state — keyed by the entry's hash
  let editingKey = $state<string | null>(null);
  let editValue = $state('');
  let editShowValue = $state(false);

  const project = $derived($selectedProject);
  const sState = $derived($secrets);

  // Group entries by scope, then by module_id for display.
  function groupForScope(s: typeof sState, scopeFilter: SecretScope, projectId: string | null) {
    const out: Record<string, SecretEntry[]> = {};
    for (const e of s.entries.values()) {
      if (e.scope !== scopeFilter) continue;
      if (scopeFilter !== 'global' && e.project_id !== projectId) continue;
      out[e.module_id] = out[e.module_id] || [];
      out[e.module_id].push(e);
    }
    return out;
  }

  const grouped = $derived(groupForScope(sState, scope, project?.id ?? null));

  // Seed the store with the orchestrator-tier license key so users can
  // always see "is the orchestrator license keychain entry set?". Other
  // module-specific keys get added through the "Add Secret" form.
  function seedKnownKeys() {
    secrets.register({
      project_id: '_global_',
      module_id: 'licensing',
      scope: 'global',
      key: 'VIBECODED_LICENSE_KEY',
      sensitive: true,
    });
  }

  onMount(() => {
    seedKnownKeys();
    secrets.refreshAll();
  });

  $effect(() => {
    // When project changes, refresh per-project + shared entries so the
    // "is_set" state matches the new project context.
    void project?.id;
    secrets.refreshAll();
  });

  function entryDomKey(e: SecretEntry): string {
    return `${e.scope}::${e.module_id}::${e.key}`;
  }

  async function handleAdd() {
    newError = null;
    if (!newModuleId.trim() || !newKey.trim() || !newValue) {
      newError = 'Module, key and value are required';
      return;
    }
    if (scope !== 'global' && !project) {
      newError = 'Select a project first (or switch to Global scope)';
      return;
    }
    busy = true;
    try {
      const entry = {
        project_id: project?.id ?? '_global_',
        module_id: newModuleId.trim(),
        scope,
        key: newKey.trim(),
        sensitive: newSensitive,
      };
      secrets.register(entry);
      await secrets.setValue(entry, newValue);
      newModuleId = '';
      newKey = '';
      newValue = '';
    } catch (e) {
      newError = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function handleSave(e: SecretEntry) {
    if (!editValue) return;
    busy = true;
    try {
      await secrets.setValue(e, editValue);
      editingKey = null;
      editValue = '';
      editShowValue = false;
    } catch (err) {
      console.error('save secret failed', err);
    } finally {
      busy = false;
    }
  }

  async function handleClear(e: SecretEntry) {
    busy = true;
    try {
      await secrets.clearValue(e);
    } catch (err) {
      console.error('clear secret failed', err);
    } finally {
      busy = false;
    }
  }

  function startEdit(e: SecretEntry) {
    editingKey = entryDomKey(e);
    editValue = '';
    editShowValue = false;
  }
</script>

<div class="secrets-panel">
  <h3 class="section-title">Secrets</h3>
  <p class="section-desc">
    Keys and tokens used by orchestrator modules. Values are stored in the OS
    keychain — never in plain files.
  </p>

  <!-- Scope toggle -->
  <div class="scope-toggle">
    <button
      class="scope-btn"
      class:active={scope === 'per_project'}
      onclick={() => (scope = 'per_project')}
      disabled={!project}
    >
      Per-project
    </button>
    <button
      class="scope-btn"
      class:active={scope === 'shared'}
      onclick={() => (scope = 'shared')}
      disabled={!project}
    >
      Shared (project)
    </button>
    <button
      class="scope-btn"
      class:active={scope === 'global'}
      onclick={() => (scope = 'global')}
    >
      Global (machine)
    </button>
  </div>

  {#if scope !== 'global' && !project}
    <div class="msg msg-warning">
      Select a project from the menu bar to manage per-project secrets.
    </div>
  {:else}
    <!-- Existing entries -->
    {#if Object.keys(grouped).length === 0}
      <div class="empty">
        <p class="empty-text">No secrets registered for this scope yet.</p>
      </div>
    {:else}
      <div class="entries">
        {#each Object.entries(grouped) as [moduleId, list] (moduleId)}
          <div class="module-group">
            <div class="module-header">
              <span class="module-id mono">{moduleId}</span>
              <span class="module-count">{list.length} key{list.length !== 1 ? 's' : ''}</span>
            </div>
            {#each list as entry (entryDomKey(entry))}
              <div class="entry-row">
                <div class="entry-info">
                  <span class="entry-key mono">{entry.key}</span>
                  <span class="entry-meta">
                    <span class="badge" class:badge-set={entry.is_set} class:badge-unset={!entry.is_set}>
                      {entry.is_set ? 'set' : 'not set'}
                    </span>
                    {#if entry.is_set && entry.preview}
                      <span class="entry-preview mono">{entry.preview}</span>
                    {:else if entry.is_set && entry.sensitive}
                      <span class="entry-preview mono">••••••••</span>
                    {/if}
                    {#if entry.sensitive}
                      <span class="badge-hint">sensitive</span>
                    {/if}
                  </span>
                </div>
                <div class="entry-actions">
                  {#if editingKey === entryDomKey(entry)}
                    <input
                      type={editShowValue ? 'text' : 'password'}
                      class="form-input form-input-inline"
                      bind:value={editValue}
                      placeholder="Enter value"
                      autocomplete="off"
                    />
                    <button
                      class="row-action"
                      title={editShowValue ? 'Hide' : 'Show'}
                      onclick={() => (editShowValue = !editShowValue)}
                      type="button"
                    >
                      {editShowValue ? '🙈' : '👁'}
                    </button>
                    <button
                      class="btn-3d btn-3d-primary btn-3d-sm"
                      onclick={() => handleSave(entry)}
                      disabled={busy || !editValue}
                    >
                      Save
                    </button>
                    <button
                      class="btn-3d btn-3d-ghost btn-3d-sm"
                      onclick={() => { editingKey = null; editValue = ''; }}
                      disabled={busy}
                    >
                      Cancel
                    </button>
                  {:else}
                    <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => startEdit(entry)}>
                      {entry.is_set ? 'Update' : 'Set'}
                    </button>
                    {#if entry.is_set}
                      <button class="btn-3d btn-3d-ghost btn-3d-sm danger" onclick={() => handleClear(entry)}>
                        Clear
                      </button>
                    {/if}
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/each}
      </div>
    {/if}

    <!-- Add new -->
    <div class="add-form">
      <h4 class="add-title">Add secret</h4>
      <div class="add-row">
        <input
          type="text"
          class="form-input form-input-sm mono"
          placeholder="module_id (e.g. orchestrator)"
          bind:value={newModuleId}
          autocomplete="off"
        />
        <input
          type="text"
          class="form-input form-input-sm mono"
          placeholder="KEY (e.g. OPENAI_API_KEY)"
          bind:value={newKey}
          autocomplete="off"
        />
      </div>
      <div class="add-row">
        <input
          type="password"
          class="form-input form-input-sm"
          placeholder="value"
          bind:value={newValue}
          autocomplete="off"
        />
        <label class="sensitive-toggle">
          <input type="checkbox" bind:checked={newSensitive} />
          <span>Sensitive</span>
        </label>
        <button class="btn-3d btn-3d-primary btn-3d-sm" onclick={handleAdd} disabled={busy}>
          Add
        </button>
      </div>
      {#if newError}
        <div class="msg msg-error">{newError}</div>
      {/if}
    </div>
  {/if}

  {#if sState.error}
    <div class="msg msg-error">{sState.error}</div>
  {/if}
</div>

<style>
  .secrets-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .section-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 6px;
  }

  .section-desc {
    font-size: 12px;
    color: var(--color-mid);
    margin-bottom: 16px;
  }

  .scope-toggle {
    display: flex;
    gap: 4px;
    padding: 3px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    margin-bottom: 16px;
    width: fit-content;
  }

  .scope-btn {
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
    background: none;
    border: none;
    border-radius: 7px;
    color: var(--color-mid);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .scope-btn:hover:not(:disabled) {
    color: var(--color-text);
  }

  .scope-btn.active {
    background: rgba(0, 191, 166, 0.12);
    color: var(--color-teal);
  }

  .scope-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .empty {
    padding: 18px;
    background: rgba(255, 255, 255, 0.02);
    border: 1px dashed rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    text-align: center;
    margin-bottom: 16px;
  }

  .empty-text {
    font-size: 12px;
    color: var(--color-mid);
  }

  .entries {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 18px;
  }

  .module-group {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    overflow: hidden;
  }

  .module-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    background: rgba(255, 255, 255, 0.02);
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }

  .module-id {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-purple);
  }

  .module-count {
    font-size: 10px;
    color: var(--color-muted);
  }

  .entry-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.02);
  }
  .entry-row:last-child {
    border-bottom: none;
  }

  .entry-info {
    display: flex;
    flex-direction: column;
    gap: 3px;
    flex: 1;
    min-width: 0;
  }

  .entry-key {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-text);
  }

  .entry-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .entry-preview {
    font-size: 11px;
    color: var(--color-muted);
  }

  .badge {
    font-size: 10px;
    font-weight: 700;
    padding: 1px 8px;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .badge-set {
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.12);
  }

  .badge-unset {
    color: var(--color-muted);
    background: rgba(255, 255, 255, 0.04);
  }

  .badge-hint {
    font-size: 10px;
    color: var(--color-pink);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .entry-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }

  .row-action {
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    cursor: pointer;
    font-size: 12px;
  }

  .add-form {
    margin-top: 8px;
    padding: 14px;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
  }

  .add-title {
    font-size: 12px;
    font-weight: 700;
    color: var(--color-mid);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 10px;
  }

  .add-row {
    display: flex;
    gap: 6px;
    margin-bottom: 6px;
  }

  .form-input {
    width: 100%;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    color: var(--color-text);
    font-size: 12px;
    font-family: inherit;
    outline: none;
  }

  .form-input.mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
  }

  .form-input.form-input-sm {
    font-size: 11px;
  }

  .form-input-inline {
    width: 200px;
    padding: 6px 10px;
    font-size: 11px;
  }

  .form-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
  }

  .sensitive-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--color-mid);
    white-space: nowrap;
  }

  .sensitive-toggle input {
    accent-color: var(--color-teal);
  }

  .msg {
    margin-top: 10px;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 12px;
  }

  .msg-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    color: var(--color-pink);
  }

  .msg-warning {
    background: rgba(255, 200, 0, 0.08);
    border: 1px solid rgba(255, 200, 0, 0.2);
    color: #ffc800;
    margin-bottom: 12px;
  }

  .danger {
    color: var(--color-pink);
  }
  .danger:hover {
    border-color: var(--color-pink) !important;
  }

  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }
</style>
