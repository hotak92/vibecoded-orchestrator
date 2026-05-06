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
  import { isTauriRuntime, invoke } from '$lib/tauri';
  import { projectColor } from '$lib/project-color';
  import { ui } from '$lib/stores/ui';
  import type { ProjectHost, ProjectView } from '$lib/types/launcher';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  // When the user picks a folder, run inspect_orchestrator_at to
  // detect whether an orchestrator is already there and what shape it's
  // in. The status panel below the path input shows the result and
  // gives the user three explicit choices (use as-is / update / install
  // fresh).
  type ConfigHealth = { file: string; ok: boolean; error: string | null };
  type OrchestratorState = {
    installed: boolean;
    version: string | null;
    version_status: 'current' | 'outdated' | 'unknown';
    bundled_version: string | null;
    config_health: ConfigHealth[];
  };
  let orchestratorState = $state<OrchestratorState | null>(null);
  let inspecting = $state(false);
  // 'use_as_is' | 'update' | 'install_fresh' — only meaningful when a
  // pre-existing install was detected at the chosen project folder
  // (i.e. the user is pointing the wizard at a folder that already
  // contains a VCO clone, an orchestrator self-onboarding scenario).
  // 2026-05-06 (PR-1): the "no orchestrator → install it here" branch
  // and its `installAfterCreate` checkbox were removed; the launcher
  // architecture is ONE shared VCO clone, never installed INTO project
  // folders. See `.claude/context/install-paths-audit-2026-05-06.md` §J.
  let adoptChoice = $state<'use_as_is' | 'update' | 'install_fresh'>('use_as_is');
  let inspectDebounce: ReturnType<typeof setTimeout> | null = null;

  // Host options used by the create modal. Bug 3d: MAO is hidden until it
  // ships as a managed module.
  const HOST_OPTIONS: { value: ProjectHost; label: string }[] = [
    { value: 'base', label: 'Standard — Claude Code only' },
  ];

  let open = $state(false);
  let wrapperEl: HTMLDivElement;

  // Create modal state
  let showCreate = $state(false);
  let createName = $state('');
  let createPath = $state('');
  // Bug 3d: default host is always 'base' (Standard). MAO is currently
  // hidden from the dropdown until it ships as a real managed module.
  let createHost = $state<ProjectHost>('base');
  let creating = $state(false);
  let createError = $state<string | null>(null);
  let showHostHelp = $state(false);
  // Once the user manually edits the path field we stop overwriting it.
  let pathTouched = $state(false);
  // Resolved root for the path autosuggest (e.g. /home/martino/code or
  // ~/code as a literal fallback in browser mode).
  let suggestedRoot = $state('~/code');
  const inTauri = isTauriRuntime();

  /** kebab-case slug for the folder name, fallback 'my-project'. */
  function slugify(s: string): string {
    return s
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 64);
  }

  async function openCreate() {
    showCreate = true;
    open = false;
    pathTouched = createPath !== '' && createPath !== undefined;
    // Resolve ~/code once per modal open. Browser mode keeps the literal
    // tilde so the user sees a recognizable shape.
    const suggested = await suggestProjectFolder();
    suggestedRoot = suggested || '~/code';
    if (!pathTouched) {
      createPath = `${suggestedRoot}/${slugify(createName) || 'my-project'}`;
    }
  }

  // Reactively keep the path in sync with the name until the user edits it.
  $effect(() => {
    if (!showCreate || pathTouched) return;
    const root = suggestedRoot || '~/code';
    createPath = `${root}/${slugify(createName) || 'my-project'}`;
  });

  function onPathInput() {
    pathTouched = true;
    scheduleInspect();
  }

  /**
   * Bug 20: debounce calls to inspect_orchestrator_at so we don't hammer
   * the FS while the user is typing. 300 ms is short enough to feel
   * instant after they pause.
   */
  function scheduleInspect() {
    if (inspectDebounce) clearTimeout(inspectDebounce);
    if (!inTauri) return;
    inspectDebounce = setTimeout(() => {
      void runInspect();
    }, 300);
  }

  async function runInspect() {
    const path = (createPath || '').trim();
    if (!path) {
      orchestratorState = null;
      return;
    }
    inspecting = true;
    try {
      orchestratorState = await invoke<OrchestratorState>('inspect_orchestrator_at', { path });
      if (orchestratorState.installed) {
        // Pre-existing orchestrator at the chosen folder — default the
        // adopt action based on whether the bundled launcher ships a
        // newer version.
        adoptChoice = orchestratorState.version_status === 'outdated' ? 'update' : 'use_as_is';
      }
      // No-orchestrator-at-path branch is intentionally a no-op: the
      // launcher never installs into project folders, so there is no
      // toggle to default. The status panel below simply hides the
      // existing-install UI in that case.
    } catch (e) {
      orchestratorState = null;
      console.error('inspect_orchestrator_at failed', e);
    } finally {
      inspecting = false;
    }
  }

  // Re-run inspection when the path changes via the auto-suggest effect
  // above (otherwise we'd only inspect on manual edits / browse).
  $effect(() => {
    if (showCreate && createPath) scheduleInspect();
  });

  // Cross-component trigger: when a route (e.g. /projects list page's
  // "+ Add Project" button) calls ui.openCreateProject(), open our modal
  // here. We immediately clear the store flag so subsequent close→reopen
  // cycles work — the store is purely a one-shot signal.
  $effect(() => {
    if ($ui.showCreateProject && !showCreate) {
      ui.closeCreateProject();
      void openCreate();
    }
  });

  function closeCreate() {
    showCreate = false;
    pathTouched = false;
    createError = null;
    orchestratorState = null;
    if (inspectDebounce) {
      clearTimeout(inspectDebounce);
      inspectDebounce = null;
    }
  }

  async function browseFolder() {
    const picked = await pickDirectory({
      defaultPath: createPath || undefined,
      title: 'Select project folder',
    });
    if (picked) {
      createPath = picked;
      pathTouched = true;
      scheduleInspect();
    }
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
  // When no projects exist at all, the trigger button is a one-click
  // "create your first project" instead of opening an empty dropdown.
  // Joint Round 3 verdict flagged the previous behavior (open empty
  // panel that contains a CTA inside) as confusing.
  const hasNoProjects = $derived(!pState.loading && pState.projects.length === 0);

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

  function handleTriggerClick(e: MouseEvent) {
    e.stopPropagation();
    if (hasNoProjects) {
      // Skip the empty dropdown — open the create modal directly.
      openCreate();
      return;
    }
    open = !open;
  }

  async function handleCreate() {
    createError = null;
    if (!createName.trim() || !createPath.trim()) {
      createError = 'Name and folder path are required';
      return;
    }
    // Expand a leading ~ to the resolved home dir (best-effort). Tauri's
    // own commands won't expand ~, so do it here before submitting.
    let submitPath = createPath.trim();
    if (submitPath.startsWith('~')) {
      const root = suggestedRoot || '';
      // suggestedRoot looks like "/home/you/code" or "~/code"; recover the
      // home portion by stripping the trailing "/code" if present.
      const home = root.replace(/[\\/]code$/, '');
      if (home && !home.startsWith('~')) {
        submitPath = home + submitPath.slice(1);
      }
    }
    creating = true;
    try {
      // Bug 20: handle pre-existing orchestrator state before creating
      // the project record so the user's choice is honored. This branch
      // covers the orchestrator self-onboarding flow only — when the
      // user points the wizard at a folder that already contains a VCO
      // clone. 2026-05-06 (PR-1): the "no orchestrator at path → install
      // it here" branch was removed because the architecture is ONE
      // shared VCO clone, never installed INTO project folders. See
      // `.claude/context/install-paths-audit-2026-05-06.md`.
      if (orchestratorState?.installed) {
        if (adoptChoice === 'update') {
          await invoke('update_orchestrator_at', { path: submitPath });
        } else if (adoptChoice === 'install_fresh') {
          await invoke('install_orchestrator', {
            config: {
              install_path: submitPath,
              use_gpu: false,
              cpu_only: false,
              openai_key: null,
              container_runtime: null,
              skip_containers: true,
            },
            confirmOverwrite: true,
          });
        }
        // 'use_as_is' → no action, just register.
      }

      await projects.create(createName.trim(), submitPath, createHost);
      showCreate = false;
      createName = '';
      createPath = '';
      createHost = 'base';
      pathTouched = false;
      orchestratorState = null;
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
      // Pass null → backend defaults apply: purgeLauncherFiles=true,
      // purgeCollections=false. The selector's quick-trash flow keeps
      // the original UX: "remove project from launcher" with sensible
      // defaults. The settings-tab Danger zone exposes the full options.
      await projects.delete(deletingProject.id, null);
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
    class:project-trigger-active={!!current}
    class:project-trigger-empty={hasNoProjects}
    style:--project-accent={current ? projectColor(current.id) : 'transparent'}
    onclick={handleTriggerClick}
    title={hasNoProjects ? 'Create your first project' : 'Switch project'}
  >
    <span
      class="project-dot"
      style:background={current ? projectColor(current.id) : 'transparent'}
      style:border-color={current ? 'transparent' : 'rgba(255,255,255,0.2)'}
      aria-hidden="true"
    ></span>
    <span class="project-name">
      {#if current}
        {current.name}
      {:else if hasNoProjects}
        + New project
      {:else}
        No project
      {/if}
    </span>
    {#if !hasNoProjects}
      <svg class="chevron" class:open width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="6 9 12 15 18 9"/>
      </svg>
    {/if}
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
<DialogRoot bind:open={showCreate} onClose={closeCreate}>
  {#snippet header()}
    <div class="modal-header-row">
      <h2>New Project</h2>
      <button class="modal-close" onclick={closeCreate} aria-label="Close">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
        </svg>
      </button>
    </div>
  {/snippet}
  {#snippet body()}
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
              oninput={onPathInput}
              placeholder="~/code/my-project"
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
            Absolute path. The folder will be created if it doesn't exist yet.
            {#if !inTauri} (Browse requires the desktop app — type the path manually here.){/if}
          </p>
        </div>

        {#if inTauri && createPath.trim() && orchestratorState?.installed}
          <!--
            2026-05-06 (PR-1): the "no orchestrator at this path → install
            into this folder" branch + checkbox were removed. The
            launcher architecture is ONE shared VCO clone; project
            folders never receive the orchestrator. The branch below
            only fires when the user has pointed the wizard at a folder
            that ALREADY contains a VCO clone (orchestrator
            self-onboarding flow). See
            `.claude/context/install-paths-audit-2026-05-06.md` §J for
            the rationale.
          -->
          <div class="orch-status orch-existing">
            <p class="orch-row">
              <span class="orch-icon orch-ok">✓</span>
              <span>
                Existing orchestrator detected
                {#if orchestratorState.version}
                  — installed: <strong>v{orchestratorState.version}</strong>
                {/if}
                {#if orchestratorState.version_status === 'outdated' && orchestratorState.bundled_version}
                  <span class="orch-mid">(bundled v{orchestratorState.bundled_version} available)</span>
                {:else if orchestratorState.version_status === 'unknown'}
                  <span class="orch-mid">(version unknown)</span>
                {/if}
              </span>
            </p>
            {#if orchestratorState.config_health.length > 0}
              {@const bad = orchestratorState.config_health.filter((c) => !c.ok)}
              {#if bad.length === 0}
                <p class="orch-row orch-mid">
                  Config: all {orchestratorState.config_health.length} files parse OK
                </p>
              {:else}
                <p class="orch-row orch-warn">
                  Config: {orchestratorState.config_health.length - bad.length} of
                  {orchestratorState.config_health.length} files parse OK; flagged:
                  {bad.map((c) => c.file).join(', ')}
                </p>
              {/if}
            {/if}
            <div class="orch-choices" role="radiogroup">
              <label class="orch-radio" class:active={adoptChoice === 'use_as_is'}>
                <input type="radio" name="adopt-choice" value="use_as_is" bind:group={adoptChoice} />
                <span>Use as-is</span>
              </label>
              <label class="orch-radio" class:active={adoptChoice === 'update'}>
                <input type="radio" name="adopt-choice" value="update" bind:group={adoptChoice} />
                <span>
                  Update
                  {#if orchestratorState.bundled_version}
                    to v{orchestratorState.bundled_version}
                  {/if}
                </span>
              </label>
              <label class="orch-radio" class:active={adoptChoice === 'install_fresh'}>
                <input type="radio" name="adopt-choice" value="install_fresh" bind:group={adoptChoice} />
                <span>Install fresh (overwrite)</span>
              </label>
            </div>
          </div>
        {:else if inTauri && createPath.trim() && inspecting}
          <p class="form-hint orch-inspecting">Inspecting folder…</p>
        {/if}

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
          <!-- Bug 12: native <select> on Linux/Tauri WebKitGTK ignores CSS
               on the OS-level dropdown popup and renders white-on-white.
               Replaced with custom Dropdown component. Bug 3d: MAO option
               still hidden — re-add a row to HOST_OPTIONS when ready. -->
          <Dropdown
            id="project-host"
            options={HOST_OPTIONS}
            bind:value={createHost}
          />
          <p class="form-hint">
            MAO (Multi-Agent Orchestrator) is coming soon — opt-in via the
            Modules tab once available.
          </p>
          {#if showHostHelp}
            <div class="host-help">
              <p>
                <strong>Standard (base):</strong> the standard Orchestrator install
                — Knowledge Graph, Code Graph, and 16 hooks. Pick this if you're
                unsure.
              </p>
            </div>
          {/if}
        </div>
        {#if createError}
          <div class="msg msg-error">{createError}</div>
        {/if}
        <div class="form-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={closeCreate} disabled={creating}>
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
  {/snippet}
</DialogRoot>

<!-- Delete confirm modal -->
{#if deletingProject}
{@const proj = deletingProject}
<DialogRoot
  open={true}
  onClose={() => (deletingProject = null)}
>
  {#snippet header()}
    <div class="modal-header-row">
      <h2>Delete Project</h2>
    </div>
  {/snippet}
  {#snippet body()}
        <p class="modal-desc">
          This removes the project from the launcher and uninstalls its modules.
          Your project folder on disk is <strong>not</strong> deleted.
        </p>
        <p class="modal-desc">
          Type <strong class="mono">{proj.name}</strong> to confirm.
        </p>
        <input
          type="text"
          class="form-input mono"
          bind:value={deleteConfirmText}
          placeholder={proj.name}
        />
        <div class="form-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => (deletingProject = null)} disabled={deleting}>
            Cancel
          </button>
          <button
            class="btn-3d btn-3d-accent btn-3d-sm"
            onclick={confirmDelete}
            disabled={deleting || deleteConfirmText !== proj.name}
          >
            {#if deleting}
              <span class="spinner-sm"></span>
            {:else}
              Delete
            {/if}
          </button>
        </div>
  {/snippet}
</DialogRoot>
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

  /* Second accent surface (paired with the MenuBar header strip) — tints
     the project-name pill background and its border with the project's
     hue. Solves the "color-only single-channel cue" finding from the
     joint power-user verification. color-mix has 95%+ browser support
     in WebKit / Chrome / Firefox 117+; Tauri ships modern WebKit so this
     is safe in the desktop runtime. */
  .project-trigger-active {
    background: color-mix(in srgb, var(--project-accent) 14%, rgba(255,255,255,0.04));
    border-color: color-mix(in srgb, var(--project-accent) 45%, rgba(255,255,255,0.08));
  }

  .project-trigger:hover {
    border-color: rgba(0, 191, 166, 0.3);
    background: rgba(255, 255, 255, 0.06);
  }
  /* Empty-state pill: dashed border + teal text to telegraph "create",
     not "switch". Click goes straight to the create modal. */
  .project-trigger-empty {
    border-style: dashed;
    border-color: rgba(0, 191, 166, 0.4);
    color: var(--color-teal, #0fc);
  }
  .project-trigger-empty:hover {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.6);
  }
  .project-trigger-active:hover {
    background: color-mix(in srgb, var(--project-accent) 22%, rgba(255,255,255,0.06));
    border-color: color-mix(in srgb, var(--project-accent) 60%, rgba(255,255,255,0.1));
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
  /* Bug 26: modals now use the native <dialog> top layer via DialogRoot.
     This component only owns the header row layout + close button styling.
     Backdrop / centering / max-height are handled by DialogRoot.svelte. */
  .modal-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-header-row h2 {
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
  /* Bug 12: native <select> styling is upstream-broken on Tauri/WebKitGTK
     (see https://github.com/tauri-apps/tauri/issues/11755). The HOST field
     now uses the custom Dropdown component instead. */
  .form-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }
  .form-hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 4px;
  }

  /* Bug 20: orchestrator state panel inside project-create modal.
     2026-05-06 (PR-1): `.orch-fresh` + `.orch-toggle` removed along with
     the "install orchestrator into this folder" checkbox; only the
     existing-install adopt-choice panel remains. */
  .orch-status {
    margin: 14px 0;
    padding: 10px 12px;
    border: 1px solid rgba(0,191,166,0.25);
    background: rgba(0,191,166,0.05);
    border-radius: 8px;
    font-size: 12px;
  }
  .orch-row {
    display: flex; align-items: flex-start; gap: 8px;
    margin: 0 0 6px;
    color: var(--color-text);
    line-height: 1.5;
  }
  .orch-icon { font-weight: 700; color: rgb(0,191,166); }
  .orch-icon.orch-ok { color: rgb(0,191,166); }
  .orch-mid { color: var(--color-mid); }
  .orch-warn { color: #f5b342; }
  .orch-inspecting { color: var(--color-mid); margin-top: 8px; }
  .orch-choices {
    display: flex; flex-direction: column; gap: 4px;
    margin-top: 8px;
  }
  .orch-radio {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px;
    border: 1px solid transparent;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    color: var(--color-text);
  }
  .orch-radio:hover { background: rgba(255,255,255,0.04); }
  .orch-radio.active { border-color: rgba(0,191,166,0.4); background: rgba(0,191,166,0.08); }

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
