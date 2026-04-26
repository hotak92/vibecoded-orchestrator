<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { AccessMode } from '$lib/types/project-state';
  import type { ProjectView } from '$lib/types/launcher';

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

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="access-backdrop" onclick={onClose} onkeydown={() => {}}>
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="access-modal" onclick={(e) => e.stopPropagation()}>
    <header class="access-header">
      <h3>Access scope</h3>
      <p class="access-target">{targetLabel}</p>
      <button class="access-close" onclick={onClose}>×</button>
    </header>

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

    <footer class="access-footer">
      <button class="access-btn" onclick={onClose}>Cancel</button>
      <button class="access-btn access-btn-primary" disabled={saving} onclick={save}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </footer>
  </div>
</div>

<style>
  /* Bug 19 systemic */
  .access-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999;
    padding: 2rem; overflow: hidden;
  }
  .access-modal {
    background: #1a1a22; border-radius: 10px; width: 480px;
    max-width: min(92vw, 600px);
    max-height: calc(100vh - 4rem);
    display: flex; flex-direction: column; overflow: hidden;
    padding: 0;
    box-shadow: 0 20px 60px rgba(0,0,0,0.7); border: 1px solid rgba(255,255,255,0.08);
  }
  .access-header { padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); position: relative; flex: 0 0 auto; }
  .access-header h3 { margin: 0; font-size: 14px; }
  .access-target { margin: 4px 0 0; font-size: 12px; color: #888; font-family: ui-monospace, monospace; }
  .access-close {
    position: absolute; top: 10px; right: 10px;
    background: none; border: none; color: #888; cursor: pointer; font-size: 20px;
    line-height: 1; padding: 4px 8px;
  }
  .access-close:hover { color: #fff; }
  .access-options { padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; flex: 1 1 auto; min-height: 0; overflow-y: auto; }
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
  .access-projects { padding: 0 16px 12px; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 10px; }
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
  .access-footer {
    padding: 12px 16px; display: flex; justify-content: flex-end; gap: 8px;
    border-top: 1px solid rgba(255,255,255,0.06);
    flex: 0 0 auto;
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
