<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { AccessMode } from '$lib/types/project-state';
  import type { ProjectView } from '$lib/types/launcher';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let {
    targetLabel,
    initial,
    onSave,
    onClose,
  }: {
    targetLabel: string;
    initial: AccessMode | null;
    onSave: (mode: AccessMode) => Promise<void>;
    onClose: () => void;
  } = $props();

  let mode = $state<'shared' | 'projects' | 'private'>(
    (initial?.mode as any) ?? 'private',
  );
  let selected = $state<Set<string>>(new Set(initial?.project_ids ?? []));
  let allProjects = $state<ProjectView[]>([]);
  let saving = $state(false);

  onMount(async () => {
    try {
      allProjects = await invoke<ProjectView[]>('list_projects_v2');
    } catch (e) {
      toast.error(e);
    }
  });

  function toggle(id: string) {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    selected = new Set(selected);
  }

  async function save() {
    saving = true;
    try {
      await onSave({
        mode,
        project_ids: mode === 'projects' ? [...selected] : [],
        owner_project_id: initial?.owner_project_id ?? null,
      });
      toast.success('Access updated');
      onClose();
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }
</script>

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot. -->
<DialogRoot open={true} width="480px" onClose={onClose}>
  {#snippet header()}
    <div class="access-header-row">
      <div class="access-header-text">
        <h3>Access scope</h3>
        <p class="access-target">{targetLabel}</p>
      </div>
      <button class="access-close" onclick={onClose} aria-label="Close">×</button>
    </div>
  {/snippet}
  {#snippet body()}
    <div class="access-options">
      <label class="access-opt" class:active={mode === 'shared'}>
        <input type="radio" name="mode" value="shared" bind:group={mode} />
        <div>
          <strong>Shared (all projects)</strong>
          <p>Visible to every project on this machine.</p>
        </div>
      </label>

      <label class="access-opt" class:active={mode === 'projects'}>
        <input type="radio" name="mode" value="projects" bind:group={mode} />
        <div>
          <strong>Specific projects</strong>
          <p>Restrict to a chosen list of projects.</p>
        </div>
      </label>

      <label class="access-opt" class:active={mode === 'private'}>
        <input type="radio" name="mode" value="private" bind:group={mode} />
        <div>
          <strong>This project only</strong>
          <p>Only the owner project can access.</p>
        </div>
      </label>
    </div>

    {#if mode === 'projects'}
      <div class="access-projects">
        <h4>Allowed projects</h4>
        {#if allProjects.length === 0}
          <p class="access-empty">No other projects to choose from.</p>
        {:else}
          <ul>
            {#each allProjects as p}
              <li>
                <label>
                  <input type="checkbox" checked={selected.has(p.id)} onchange={() => toggle(p.id)} />
                  <span>{p.name}</span>
                  <small>{p.host}</small>
                </label>
              </li>
            {/each}
          </ul>
          <p class="access-count">{selected.size} project(s) selected</p>
        {/if}
      </div>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="access-footer-row">
      <button class="access-btn" onclick={onClose}>Cancel</button>
      <button class="access-btn access-btn-primary" disabled={saving} onclick={save}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  {/snippet}
</DialogRoot>

<style>
  /* Bug 26: backdrop / sizing / shell now handled by DialogRoot. */
  .access-header-row {
    display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  }
  .access-header-text h3 { margin: 0; font-size: 14px; }
  .access-target { margin: 4px 0 0; font-size: 12px; color: #888; font-family: ui-monospace, monospace; }
  .access-close {
    background: none; border: none; color: #888; cursor: pointer; font-size: 20px;
    line-height: 1; padding: 4px 8px;
  }
  .access-close:hover { color: #fff; }
  .access-options { display: flex; flex-direction: column; gap: 8px; }
  .access-opt {
    display: flex; gap: 10px; padding: 10px 12px;
    background: rgba(255,255,255,0.03); border-radius: 6px; cursor: pointer;
    border: 1px solid transparent;
  }
  .access-opt:hover { background: rgba(255,255,255,0.06); }
  .access-opt.active { border-color: rgb(0,191,166); }
  .access-opt input { margin-top: 3px; }
  .access-opt strong { font-size: 13px; }
  .access-opt p { font-size: 11px; color: #888; margin: 2px 0 0; }
  .access-projects { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.06); }
  .access-projects h4 { font-size: 12px; margin: 0 0 8px; color: #888; text-transform: uppercase; }
  .access-projects ul { list-style: none; padding: 0; margin: 0; max-height: 200px; overflow-y: auto; }
  .access-projects li label {
    display: flex; align-items: center; gap: 8px; padding: 4px 8px;
    border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .access-projects li label:hover { background: rgba(255,255,255,0.04); }
  .access-projects li small { color: #666; margin-left: auto; }
  .access-empty { color: #888; font-size: 12px; }
  .access-count { font-size: 11px; color: #888; margin: 8px 0 0; }
  .access-footer-row {
    display: flex; justify-content: flex-end; gap: 8px;
  }
  .access-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 6px 16px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .access-btn:hover { background: rgba(255,255,255,0.1); }
  .access-btn-primary { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600; }
  .access-btn-primary:hover { background: rgb(0,210,180); }
  .access-btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
