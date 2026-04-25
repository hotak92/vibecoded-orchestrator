<script lang="ts">
  // Project selector dropdown for the MenuBar.
  //
  // - Shows current project name + chevron.
  // - Expanded panel lists projects with switch action.
  // - "+ New project" opens the create modal (handled inline here, simpler
  //   than a separate component for one form).
  // - Rename / Delete are inline per-row actions; delete requires a typed
  //   confirmation.

  import { onMount } from 'svelte';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { pickDirectory, suggestProjectFolder } from '$lib/dialog';
  import { isTauriRuntime } from '$lib/tauri';
  import { projectColor } from '$lib/project-color';
  import type { ProjectHost, ProjectView } from '$lib/types/launcher';

  let open = $state(false);
  let wrapperEl: HTMLDivElement;

  // Create modal state
  let showCreate = $state(false);
  let createName = $state('');
  let createPath = $state('');
  let createHost = $state<ProjectHost>('base');
  let creating = $state(false);
  let createError = $state<string | null>(null);
  let showHostHelp = $state(false);
  const inTauri = isTauriRuntime();

  async function openCreate() {
    showCreate = true;
    open = false;
    if (!createPath) {
      // Suggest a sane default the first time the modal opens.
      const suggested = await suggestProjectFolder();
      if (suggested) createPath = suggested;
    }
  }

  async function browseFolder() {
    const picked = await pickDirectory({
      defaultPath: createPath || undefined,
      title: 'Select project folder',
    });
    if (picked) createPath = picked;
  }

  // Rename inline state — keyed by project id
  let renamingId = $state<string | null>(null);
  let renameValue = $state('');

  // Delete confirm modal state
  let deletingProject = $state<ProjectView | null>(null);
  let deleteConfirmText = $state('');
  let deleting = $state(false);

  const pState = $derived($projects);
  const current = $derived($selectedProject);

  onMount(() => {
    projects.load();
  });

  function handleClickOutside(e: MouseEvent) {
    if (open && wrapperEl && !wrapperEl.contains(e.target as Node)) {
      open = false;
    }
  }

  function handleSelect(id: string) {
    projects.select(id);
    open = false;
  }

  async function handleCreate() {
    createError = null;
    if (!createName.trim() || !createPath.trim()) {
      createError = 'Name and folder path are required';
      return;
    }
    creating = true;
    try {
      await projects.create(createName.trim(), createPath.trim(), createHost);
      showCreate = false;
      createName = '';
      createPath = '';
      createHost = 'base';
      open = false;
    } catch (e) {
      createError = e instanceof Error ? e.message : String(e);
    } finally {
      creating = false;
    }
  }

  function startRename(p: ProjectView) {
    renamingId = p.id;
    renameValue = p.name;
  }

  async function commitRename(id: string) {
    if (!renameValue.trim()) {
      renamingId = null;
      return;
    }
    try {
      await projects.rename(id, renameValue.trim());
    } catch (e) {
      console.error('rename failed', e);
    } finally {
      renamingId = null;
    }
  }

  function startDelete(p: ProjectView) {
    deletingProject = p;
    deleteConfirmText = '';
  }

  async function confirmDelete() {
    if (!deletingProject) return;
    if (deleteConfirmText !== deletingProject.name) return;
    deleting = true;
    try {
      await projects.delete(deletingProject.id, false);
      deletingProject = null;
      deleteConfirmText = '';
    } catch (e) {
      console.error('delete failed', e);
    } finally {
      deleting = false;
    }
  }
</script>

<svelte:window onclick={handleClickOutside} />

<div class="project-wrapper" bind:this={wrapperEl}>
  <button
    class="project-trigger"
    onclick={(e) => {
      e.stopPropagation();
      open = !open;
    }}
    title="Switch project"
  >
    <span
      class="project-dot"
      style:background={current ? projectColor(current.id) : 'transparent'}
      style:border-color={current ? 'transparent' : 'rgba(255,255,255,0.2)'}
      aria-hidden="true"
    ></span>
    <span class="project-name">
      {current ? current.name : 'No project'}
    </span>
    <svg class="chevron" class:open width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="6 9 12 15 18 9"/>
    </svg>
  </button>

  {#if open}
    <div class="project-panel">
      <div class="panel-header">
        <span class="panel-title">Projects</span>
        <button class="panel-add" onclick={openCreate}>
          + New
        </button>
      </div>

      {#if pState.loading}
        <div class="panel-empty">Loading…</div>
      {:else if pState.projects.length === 0}
        <div class="panel-empty">
          <p class="empty-title">No projects yet</p>
          <p class="empty-text">Create your first project to start installing modules.</p>
          <button class="btn-3d btn-3d-primary btn-3d-sm" onclick={openCreate}>
            Create your first project
          </button>
        </div>
      {:else}
        <div class="panel-list">
          {#each pState.projects as p (p.id)}
            <div
              class="panel-row"
              class:active={current?.id === p.id}
            >
              {#if renamingId === p.id}
                <input
                  class="rename-input"
                  bind:value={renameValue}
                  onkeydown={(e) => {
                    if (e.key === 'Enter') commitRename(p.id);
                    if (e.key === 'Escape') { renamingId = null; }
                  }}
                  onblur={() => commitRename(p.id)}
                />
              {:else}
                <button class="row-main" onclick={() => handleSelect(p.id)}>
                  <span class="row-top">
                    <span class="row-dot" style:background={projectColor(p.id)} aria-hidden="true"></span>
                    <span class="row-name">{p.name}</span>
                  </span>
                  <span class="row-meta">
                    <span class="row-host">{p.host}</span>
                    <span class="row-count">{p.module_count} module{p.module_count !== 1 ? 's' : ''}</span>
                  </span>
                </button>
                <div class="row-actions">
                  <button class="row-action" title="Rename" onclick={() => startRename(p)}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"/>
                    </svg>
                  </button>
                  <button class="row-action row-action-danger" title="Delete" onclick={() => startDelete(p)}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="3 6 5 6 21 6"/>
                      <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
                    </svg>
                  </button>
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}

      {#if pState.error}
        <div class="panel-error">{pState.error}</div>
      {/if}
    </div>
  {/if}
</div>

<!-- Create project modal -->
{#if showCreate}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={() => (showCreate = false)} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal-content" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <div class="modal-header">
        <h2>New Project</h2>
        <button class="modal-close" onclick={() => (showCreate = false)} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label for="project-name">Name</label>
          <input
            id="project-name"
            type="text"
            class="form-input"
            bind:value={createName}
            placeholder="my-project"
          />
        </div>
        <div class="form-group">
          <label for="project-path">Folder Path</label>
          <div class="path-row">
            <input
              id="project-path"
              type="text"
              class="form-input mono path-input"
              bind:value={createPath}
              placeholder="/home/you/code/my-project"
            />
            <button
              type="button"
              class="btn-3d btn-3d-ghost btn-3d-sm browse-btn"
              onclick={browseFolder}
              disabled={!inTauri}
              title={inTauri ? 'Browse for folder' : 'Browse requires the desktop app'}
            >
              Browse…
            </button>
          </div>
          <p class="form-hint">
            Absolute path. Folder must already exist.
            {#if !inTauri} (Browse requires the desktop app — type the path manually here.){/if}
          </p>
        </div>
        <div class="form-group">
          <div class="label-row">
            <label for="project-host">Host</label>
            <button
              type="button"
              class="help-btn"
              onclick={() => (showHostHelp = !showHostHelp)}
              aria-label="What does host mean?"
              title="What does host mean?"
            >?</button>
          </div>
          <select id="project-host" class="form-input" bind:value={createHost}>
            <option value="base">Standard — Claude Code only</option>
            <option value="mao">MAO — Multi-Agent Orchestrator (beta)</option>
          </select>
          {#if showHostHelp}
            <div class="host-help">
              <p>
                <strong>Standard (base):</strong> the standard Orchestrator install
                — Knowledge Graph, Code Graph, and 16 hooks. Pick this if you're
                unsure.
              </p>
              <p>
                <strong>MAO:</strong> Multi-Agent Orchestrator — adds 10 specialist
                agents and a Maestro coordinator on top of Standard. Beta. Pick
                this if you want the extras and don't mind some rough edges.
              </p>
            </div>
          {/if}
        </div>
        {#if createError}
          <div class="msg msg-error">{createError}</div>
        {/if}
        <div class="form-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => (showCreate = false)} disabled={creating}>
            Cancel
          </button>
          <button class="btn-3d btn-3d-primary btn-3d-sm" onclick={handleCreate} disabled={creating}>
            {#if creating}
              <span class="spinner-sm"></span>
            {:else}
              Create
            {/if}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- Delete confirm modal -->
{#if deletingProject}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={() => (deletingProject = null)} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal-content" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <div class="modal-header">
        <h2>Delete Project</h2>
      </div>
      <div class="modal-body">
        <p class="modal-desc">
          This removes the project from the launcher and uninstalls its modules.
          Your project folder on disk is <strong>not</strong> deleted.
        </p>
        <p class="modal-desc">
          Type <strong class="mono">{deletingProject.name}</strong> to confirm.
        </p>
        <input
          type="text"
          class="form-input mono"
          bind:value={deleteConfirmText}
          placeholder={deletingProject.name}
        />
        <div class="form-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => (deletingProject = null)} disabled={deleting}>
            Cancel
          </button>
          <button
            class="btn-3d btn-3d-accent btn-3d-sm"
            onclick={confirmDelete}
            disabled={deleting || deleteConfirmText !== deletingProject.name}
          >
            {#if deleting}
              <span class="spinner-sm"></span>
            {:else}
              Delete
            {/if}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .project-wrapper {
    position: relative;
  }

  .project-trigger {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
    max-width: 260px;
  }

  .project-trigger:hover {
    border-color: rgba(0, 191, 166, 0.3);
    background: rgba(255, 255, 255, 0.06);
  }

  .project-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 180px;
  }
  .project-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    border: 1px solid;
    flex-shrink: 0;
  }
  .row-top {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .row-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .chevron {
    transition: transform 0.15s ease;
    color: var(--color-mid);
  }
  .chevron.open {
    transform: rotate(180deg);
  }

  .project-panel {
    position: absolute;
    top: 40px;
    left: 0;
    width: 320px;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 14px;
    padding: 6px;
    z-index: 200;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    animation: panel-appear 0.15s ease-out;
    max-height: 420px;
    overflow-y: auto;
  }

  @keyframes panel-appear {
    from { opacity: 0; transform: translateY(-8px) scale(0.97); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px 6px;
  }

  .panel-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--color-muted);
  }

  .panel-add {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-teal);
    background: none;
    border: none;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 6px;
  }
  .panel-add:hover {
    background: rgba(0, 191, 166, 0.08);
  }

  .panel-empty {
    padding: 18px 12px;
    text-align: center;
  }

  .empty-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 4px;
  }

  .empty-text {
    font-size: 12px;
    color: var(--color-mid);
    margin-bottom: 12px;
  }

  .panel-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .panel-row {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px;
    border-radius: 8px;
  }

  .panel-row.active {
    background: rgba(0, 191, 166, 0.08);
  }

  .row-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 6px 8px;
    background: none;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    text-align: left;
    color: var(--color-text);
  }

  .row-main:hover {
    background: rgba(255, 255, 255, 0.04);
  }

  .row-name {
    font-size: 13px;
    font-weight: 600;
  }

  .row-meta {
    display: flex;
    gap: 8px;
    font-size: 10px;
    color: var(--color-muted);
  }

  .row-host {
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--color-purple);
  }

  .row-actions {
    display: flex;
    gap: 2px;
  }

  .row-action {
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    border-radius: 6px;
    color: var(--color-muted);
    cursor: pointer;
  }
  .row-action:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }
  .row-action-danger:hover {
    color: var(--color-pink);
    background: rgba(255, 79, 160, 0.08);
  }

  .rename-input {
    flex: 1;
    padding: 6px 8px;
    background: rgba(0, 191, 166, 0.08);
    border: 1px solid rgba(0, 191, 166, 0.4);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 13px;
    font-weight: 600;
    outline: none;
  }

  .panel-error {
    margin: 8px 4px 4px;
    padding: 8px;
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    border-radius: 8px;
    color: var(--color-pink);
    font-size: 11px;
  }

  /* ── Modals ─────────────────────────────────────────────── */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 400;
    animation: fade-in 0.15s ease-out;
  }
  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .modal-content {
    width: 460px;
    max-width: 90vw;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 18px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 20px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }

  .modal-header h2 {
    font-size: 15px;
    font-weight: 700;
    color: var(--color-text);
  }

  .modal-close {
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    border-radius: 8px;
    color: var(--color-mid);
    cursor: pointer;
  }
  .modal-close:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }

  .modal-body {
    padding: 18px 20px;
  }

  .modal-desc {
    font-size: 13px;
    color: var(--color-mid);
    margin-bottom: 14px;
    line-height: 1.5;
  }

  .form-group {
    margin-bottom: 14px;
  }

  .form-group label {
    display: block;
    font-size: 11px;
    font-weight: 600;
    color: var(--color-mid);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .form-input {
    width: 100%;
    padding: 9px 12px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 13px;
    font-family: inherit;
    outline: none;
  }
  .form-input.mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 12px;
  }
  .form-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }
  .form-hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 4px;
  }

  .path-row {
    display: flex;
    gap: 6px;
    align-items: stretch;
  }
  .path-input {
    flex: 1;
  }
  .browse-btn {
    flex-shrink: 0;
    white-space: nowrap;
  }

  .label-row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }
  .label-row label {
    margin-bottom: 0;
  }
  .help-btn {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    border: 1px solid rgba(255, 255, 255, 0.18);
    background: rgba(255, 255, 255, 0.04);
    color: var(--color-mid);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
  .help-btn:hover {
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-teal);
  }

  .host-help {
    margin-top: 8px;
    padding: 10px 12px;
    background: rgba(0, 191, 166, 0.06);
    border: 1px solid rgba(0, 191, 166, 0.18);
    border-radius: 8px;
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
  }
  .host-help p {
    margin: 0;
  }
  .host-help p + p {
    margin-top: 6px;
  }
  .host-help strong {
    color: var(--color-text);
  }

  .form-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 16px;
  }

  .msg {
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 12px;
    margin-bottom: 12px;
  }
  .msg-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    color: var(--color-pink);
  }

  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .spinner-sm {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(0, 0, 0, 0.2);
    border-top-color: var(--color-bg);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
