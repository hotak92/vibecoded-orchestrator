<script lang="ts">
  // Secrets manager.
  //
  // ─── Three scope tabs ────────────────────────────────────────────
  //  - Per-project: tied to a specific registered project. The form
  //    surfaces a project DROPDOWN (not a free-text field) so the user
  //    can only target a project that exists. Read at runtime by any
  //    module running in that project's context.
  //  - Shared (this user): not tied to any project. Visible to ALL of
  //    this user's projects. Form is just KEY + value.
  //  - Global (this machine): machine-wide. Visible to all users + all
  //    projects on this machine. Form is just KEY + value.
  //
  // ─── Read-time resolution order (modules) ────────────────────────
  //  A module running in project P resolves `getSecret(K)` by checking
  //    1. Per-project bag for P
  //    2. Shared (this user)
  //    3. Global (this machine)
  //  First hit wins. A module in project P never sees project Q's
  //  per-project bag — backend enforces this via
  //  `enforce_scope_invariants` rejecting unregistered project_ids.
  //
  // ─── Per-secret lifecycle ────────────────────────────────────────
  //  - Set / Update: write a value to keychain.
  //  - Unset: clear the value from keychain BUT keep the entry in the
  //    UI registry. Useful for token rotation.
  //  - Remove: drop the entry from the registry entirely.
  //
  // ─── Module identity (legacy) ────────────────────────────────────
  //  The keychain key shape includes a module_id segment for backward
  //  compat with pre-existing entries (e.g. the seeded
  //  `licensing/VIBECODED_LICENSE_KEY` global entry). New entries
  //  added through this panel default to module_id="user". The UI
  //  does NOT surface module_id to the user — it is an
  //  implementation detail for v0.1.x. Listing entries flattens the
  //  visual grouping and keys solely by KEY name.

  import { onMount } from 'svelte';
  import { secrets, lifecycleOf, type SecretEntry, type SecretScope } from '$lib/stores/secrets';
  import { selectedProject, projects } from '$lib/stores/projects';

  let scope = $state<SecretScope>('per_project');

  // Add-form state. module_id is no longer a UI field — entries created
  // through this panel use a fixed "user" module bucket (see header
  // comment). Per-project uses `formProjectId` to choose the target.
  let formProjectId = $state<string | null>(null);
  let newKey = $state('');
  let newValue = $state('');
  let newSensitive = $state(true);
  let newError = $state<string | null>(null);
  let busy = $state(false);

  // The module bucket new UI-created entries land in. Pre-existing
  // entries keep their original module_id (e.g. "licensing"). This
  // sentinel is purely an implementation detail; the user does not
  // see it.
  const UI_MODULE_BUCKET = 'user';

  // Edit-row state — keyed by the entry's hash
  let editingKey = $state<string | null>(null);
  let editValue = $state('');
  let editShowValue = $state(false);

  // Confirm-modal state for Remove (destructive)
  let removeConfirm = $state<SecretEntry | null>(null);

  const project = $derived($selectedProject);
  const sState = $derived($secrets);
  const allProjects = $derived($projects.projects);

  // When the user opens the panel, default the per-project form to the
  // currently-selected project (matches their expectation when arriving
  // via a project's "Open secrets panel" button).
  $effect(() => {
    if (formProjectId === null && project) formProjectId = project.id;
  });

  // Group entries by scope. We no longer group by module_id; everything
  // for a given scope renders as a flat list keyed by KEY only. For
  // per-project we filter to entries that match `formProjectId` (so
  // switching the project dropdown updates the visible list).
  function listForScope(
    s: typeof sState,
    scopeFilter: SecretScope,
    projectId: string | null,
  ): SecretEntry[] {
    const out: SecretEntry[] = [];
    for (const e of s.entries.values()) {
      if (e.scope !== scopeFilter) continue;
      if (scopeFilter === 'per_project' && e.project_id !== projectId) continue;
      out.push(e);
    }
    // Stable sort: by KEY name.
    out.sort((a, b) => a.key.localeCompare(b.key));
    return out;
  }

  const visibleEntries = $derived(listForScope(sState, scope, formProjectId));

  // Seed the store with the orchestrator-tier license key so users
  // always see "is the orchestrator license keychain entry set?". Other
  // module-specific keys come from manifests / "Add secret" form.
  function seedKnownKeys() {
    secrets.register({
      project_id: '_global_',
      module_id: 'licensing',
      scope: 'global',
      key: 'VIBECODED_LICENSE_KEY',
      sensitive: true,
    });
  }

  onMount(async () => {
    seedKnownKeys();
    // Pull the registered-projects list so the per-project dropdown
    // populates immediately (other parts of the app may not have
    // loaded it yet — load() is idempotent).
    await projects.load();
    secrets.refreshAll();
  });

  $effect(() => {
    // When project filter changes, refresh per-project + shared
    // entries so the "is_set" state matches the new scope.
    void formProjectId;
    secrets.refreshAll();
  });

  function entryDomKey(e: SecretEntry): string {
    return `${e.scope}::${e.module_id}::${e.key}`;
  }

  function projectIdForScope(s: SecretScope): string {
    if (s === 'global') return '_global_';
    if (s === 'shared') return '_user_shared_';
    return formProjectId ?? '';
  }

  async function handleAdd() {
    newError = null;
    if (!newKey.trim() || !newValue) {
      newError = 'KEY and value are required';
      return;
    }
    if (scope === 'per_project' && !formProjectId) {
      newError = 'Pick a project first';
      return;
    }
    busy = true;
    try {
      const entry = {
        project_id: projectIdForScope(scope),
        module_id: UI_MODULE_BUCKET,
        scope,
        key: newKey.trim(),
        sensitive: newSensitive,
      };
      secrets.register(entry);
      await secrets.setValue(entry, newValue);
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

  async function handleUnset(e: SecretEntry) {
    busy = true;
    try {
      await secrets.unsetValue(e);
    } catch (err) {
      console.error('unset secret failed', err);
    } finally {
      busy = false;
    }
  }

  async function handleReactivate(e: SecretEntry) {
    busy = true;
    try {
      await secrets.reactivateValue(e);
    } catch (err) {
      console.error('reactivate secret failed', err);
    } finally {
      busy = false;
    }
  }

  async function handleRemoveConfirmed() {
    if (!removeConfirm) return;
    busy = true;
    try {
      await secrets.removeEntry(removeConfirm);
      removeConfirm = null;
    } catch (err) {
      console.error('remove secret failed', err);
    } finally {
      busy = false;
    }
  }

  function startEdit(e: SecretEntry) {
    editingKey = entryDomKey(e);
    editValue = '';
    editShowValue = false;
  }

  async function refreshProjects() {
    await projects.load();
  }
</script>

<div class="secrets-panel">
  <h3 class="section-title">Secrets</h3>
  <p class="section-desc">
    Keys and tokens used by orchestrator modules. Values are stored in the OS
    keychain — never in plain files.
  </p>

  <!-- Scope toggle. Note `disabled={!project}` was removed for shared:
       shared is per-USER (not per-project), so it's always usable. -->
  <div class="scope-toggle">
    <button
      class="scope-btn"
      class:active={scope === 'per_project'}
      onclick={() => (scope = 'per_project')}
    >
      Per-project
    </button>
    <button
      class="scope-btn"
      class:active={scope === 'shared'}
      onclick={() => (scope = 'shared')}
    >
      Shared (this user)
    </button>
    <button
      class="scope-btn"
      class:active={scope === 'global'}
      onclick={() => (scope = 'global')}
    >
      Global (this machine)
    </button>
  </div>

  <!-- Scope description: explain semantics so the user knows what
       lifetime each tab implies.
       Subagent G (2026-05-08): per-project tab gets an extra line
       calling out the env-var auto-emission contract. Closes the "GUI
       says secret is set, but I can't actually use it" gap from the
       user's perspective — they now understand that adding a key here
       makes it appear as $KEY in their next Claude Code session for
       that project. -->
  <p class="scope-desc">
    {#if scope === 'per_project'}
      Tied to a specific project. Visible only to modules running in that project's context.
      Auto-emitted as <code>$KEY</code> env vars in the project's Claude Code session
      (no session restart needed).
    {:else if scope === 'shared'}
      Shared across <em>all</em> of your projects. Not visible to other users on this machine.
    {:else}
      Machine-wide. Visible to all users and all projects on this machine.
    {/if}
  </p>

  <!-- Per-project: project dropdown selector + refresh -->
  {#if scope === 'per_project'}
    <div class="project-picker">
      <label for="secrets-project-picker" class="project-picker-label">Project</label>
      {#if allProjects.length === 0}
        <div class="msg msg-warning">
          No projects registered yet. Register a project first via the Library page.
        </div>
      {:else}
        <select
          id="secrets-project-picker"
          class="form-input form-input-sm"
          bind:value={formProjectId}
        >
          <option value={null}>— pick a project —</option>
          {#each allProjects as p (p.id)}
            <option value={p.id}>{p.name}{p.folder_path ? ` (${p.folder_path})` : ''}</option>
          {/each}
        </select>
        <button
          class="btn-3d btn-3d-ghost btn-3d-sm"
          onclick={refreshProjects}
          title="Refresh project list"
          disabled={busy || $projects.loading}
          type="button"
        >
          ↻
        </button>
      {/if}
    </div>
  {/if}

  {#if scope === 'per_project' && !formProjectId}
    <div class="empty">
      <p class="empty-text">Pick a project above to view or add per-project secrets.</p>
    </div>
  {:else}
    <!-- Existing entries: flat list, no module-id grouping. -->
    {#if visibleEntries.length === 0}
      <div class="empty">
        <p class="empty-text">No secrets registered for this scope yet.</p>
      </div>
    {:else}
      <div class="entries-list">
        {#each visibleEntries as entry (entryDomKey(entry))}
          {@const lifecycle = lifecycleOf(entry)}
          <div class="entry-row" class:row-inactive={lifecycle === 'inactive'}>
            <div class="entry-info">
              <span class="entry-key mono">{entry.key}</span>
              <span class="entry-meta">
                <!-- Lifecycle badge:
                       active   → "set"     (teal, value readable)
                       inactive → "unset"   (amber, value preserved but gated)
                       empty    → "not set" (grey, no saved value)
                     The value-still-in-keychain part of inactive is NOT
                     surfaced as a preview — readers are gated on active. -->
                {#if lifecycle === 'active'}
                  <span class="badge badge-set">set</span>
                {:else if lifecycle === 'inactive'}
                  <span class="badge badge-inactive" title="Value preserved in keychain. Readers gated until you Reactivate.">unset</span>
                {:else}
                  <span class="badge badge-unset">not set</span>
                {/if}
                {#if lifecycle === 'active' && entry.preview}
                  <span class="entry-preview mono">{entry.preview}</span>
                {:else if lifecycle === 'active' && entry.sensitive}
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
              {:else if lifecycle === 'inactive'}
                <!-- Inactive: one-click Reactivate (no value-input row).
                     The value is still in the keychain from before Unset
                     so we just flip the active flag. "Set as new value"
                     is the escape hatch when the user wants to replace
                     the saved value while currently paused. -->
                <button
                  class="btn-3d btn-3d-primary btn-3d-sm"
                  onclick={() => handleReactivate(entry)}
                  title="Re-activate using the saved value. No re-entry required."
                  disabled={busy}
                >
                  Reactivate
                </button>
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm"
                  onclick={() => startEdit(entry)}
                  title="Replace the saved value with a new one (keeps the entry; sets active)."
                  disabled={busy}
                >
                  Set as new value
                </button>
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm danger"
                  onclick={() => (removeConfirm = entry)}
                  title="Drop this entry from the registry entirely (forgets the entry exists, deletes from keychain)."
                  disabled={busy}
                >
                  Remove
                </button>
              {:else}
                <!-- Active or Empty: classic flow. The button label
                     swaps between Update and Set based on whether the
                     keychain has a value. Unset only appears when there
                     is something to pause. -->
                <button
                  class="btn-3d btn-3d-primary btn-3d-sm"
                  onclick={() => startEdit(entry)}
                  title={lifecycle === 'active' ? 'Update the stored value' : 'Set a value'}
                >
                  {lifecycle === 'active' ? 'Update' : 'Set'}
                </button>
                {#if lifecycle === 'active'}
                  <button
                    class="btn-3d btn-3d-ghost btn-3d-sm"
                    onclick={() => handleUnset(entry)}
                    title="Mark inactive. The value stays securely in your OS keychain but readers won't see it. Use to rotate tokens — pause an old one while validating a new one."
                    disabled={busy}
                  >
                    Unset
                  </button>
                {/if}
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm danger"
                  onclick={() => (removeConfirm = entry)}
                  title="Drop this entry from the registry entirely (forgets the entry exists, deletes from keychain)."
                  disabled={busy}
                >
                  Remove
                </button>
              {/if}
            </div>
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
        <!-- Sensitive checkbox + tooltip (Bug 2 follow-up to PR #60).
             Default is true — most secrets ARE sensitive. The (?) icon
             carries the explanation as a native tooltip; same wording as
             the inline help below for screen-readers / pointer hover. -->
        <label class="sensitive-toggle">
          <input type="checkbox" bind:checked={newSensitive} />
          <span>Sensitive</span>
          <span
            class="help-icon"
            title="Tokens, API keys, passwords. When checked, the launcher refuses to display the value anywhere — even masked. Uncheck only for non-secret config strings (e.g. URLs, usernames) you want to verify in audit logs."
            aria-label="Help: what does Sensitive do?"
            role="img"
          >?</span>
        </label>
        <button
          class="btn-3d btn-3d-primary btn-3d-sm"
          onclick={handleAdd}
          disabled={busy || (scope === 'per_project' && !formProjectId)}
        >
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

  <!-- Confirm modal: Remove is destructive (drops the registry entry,
       requires re-add). Unset is the non-destructive alternative when
       the user just wants to clear the current value. -->
  {#if removeConfirm}
    <div
      class="confirm-overlay"
      role="button"
      tabindex="-1"
      onclick={() => (removeConfirm = null)}
      onkeydown={(e) => { if (e.key === 'Escape') removeConfirm = null; }}
    >
      <div
        class="confirm-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-title"
        tabindex="-1"
        onclick={(e) => e.stopPropagation()}
        onkeydown={(e) => e.stopPropagation()}
      >
        <h4 id="remove-title" class="confirm-title">Remove secret entry?</h4>
        <p class="confirm-body">
          Remove the entry for <code>{removeConfirm.key}</code>? This forgets the
          entry exists; you'll need to re-add it.
        </p>
        <p class="confirm-hint">
          Use <strong>Unset</strong> instead if you just want to clear the current value
          (the entry stays visible so you can re-set it).
        </p>
        <div class="confirm-actions">
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm"
            onclick={() => (removeConfirm = null)}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            class="btn-3d btn-3d-primary btn-3d-sm danger"
            onclick={handleRemoveConfirmed}
            disabled={busy}
          >
            Remove
          </button>
        </div>
      </div>
    </div>
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
    margin-bottom: 8px;
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

  .scope-desc {
    font-size: 11px;
    color: var(--color-muted);
    margin-bottom: 16px;
    font-style: italic;
  }

  .scope-desc em {
    color: var(--color-teal);
    font-style: normal;
    font-weight: 600;
  }

  /* Subagent G (2026-05-08): inline `<code>` for the env-var hint in
   * the per-project scope description. Mono so it's visually distinct
   * from prose, matches the entry-key + entry-preview mono treatment
   * elsewhere in this panel. */
  .scope-desc code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 10px;
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 5px;
    border-radius: 3px;
    color: var(--color-text);
    font-style: normal;
  }

  .project-picker {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
  }

  .project-picker-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--color-mid);
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

  .entries-list {
    display: flex;
    flex-direction: column;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    margin-bottom: 18px;
    overflow: hidden;
  }

  .entry-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
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

  /* Inactive (Lifecycle B): value is paused in keychain. Amber so it's
   * visually distinct from "set" (teal) and "not set" (grey) — readers
   * are gated, but the saved value still exists. */
  .badge-inactive {
    color: #ffc800;
    background: rgba(255, 200, 0, 0.12);
  }

  .badge-unset {
    color: var(--color-muted);
    background: rgba(255, 255, 255, 0.04);
  }

  /* Subtle de-emphasis on inactive rows so the lifecycle state reads at
   * a glance. Buttons themselves stay full-opacity — the action is
   * still available, the row is just paused. */
  .row-inactive .entry-key {
    color: var(--color-mid);
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
    align-items: center;
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

  /* Project-picker dropdown: the global app.css rule sets `color-scheme:
   * dark` on :root so WebKitGTK paints the popup in dark mode (Bug 1).
   * We layer explicit colors here as defence in depth — any browser /
   * webview that ignores `color-scheme` for whatever reason still gets
   * a legible control. The `option` rule is critical on Chromium /
   * WebView2 where the popup honors per-option styling.
   * MDN: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/select#styling_with_css */
  select.form-input {
    cursor: pointer;
    background: rgba(255, 255, 255, 0.06);
    color: var(--color-text);
    /* color-scheme on the element itself reinforces the :root rule for
     * environments where the user-agent only consults the nearest
     * ancestor (rare, but cheap insurance). */
    color-scheme: dark;
  }

  select.form-input option {
    background: var(--color-bg2);
    color: var(--color-text);
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

  /* Tooltip-bearing question mark next to the Sensitive label. We use a
   * native `title` attribute (rendered as the OS tooltip by every
   * webview) rather than a custom popover — keeps the bundle small and
   * matches the rest of the panel's tooltip pattern. */
  .help-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.08);
    color: var(--color-mid);
    font-size: 9px;
    font-weight: 700;
    cursor: help;
    user-select: none;
  }
  .help-icon:hover {
    background: rgba(0, 191, 166, 0.2);
    color: var(--color-teal);
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

  /* Confirm modal for destructive Remove */
  .confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 16px;
    border: none;
  }

  .confirm-card {
    max-width: 420px;
    width: 100%;
    padding: 18px;
    background: rgba(20, 22, 28, 0.98);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }

  .confirm-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 8px;
  }

  .confirm-body {
    font-size: 12px;
    color: var(--color-mid);
    margin-bottom: 8px;
  }

  .confirm-body code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
  }

  .confirm-hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-bottom: 14px;
  }

  .confirm-hint strong {
    color: var(--color-teal);
  }

  .confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
