<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { ProjectPermission } from '$lib/types/project-state';

  let { projectId }: { projectId: string } = $props();

  const KIND_HELP: Record<string, string> = {
    write_scope: 'Glob the agent is allowed to write to (e.g. src/**).',
    allowed_tool: 'Tool name on the allow list (e.g. Read, Edit, Bash).',
    denied_tool: 'Tool name on the deny list (overrides allowed_tool).',
    mcp_server: 'MCP server scoped to this subject (e.g. weaviate-kg).',
    permission_mode: 'default | acceptEdits | dontAsk | bypassPermissions | plan',
  };
  const KINDS = Object.keys(KIND_HELP);

  let perms = $state<ProjectPermission[]>([]);
  let loading = $state(true);
  let showAdd = $state(false);

  let nSubject = $state('');
  let nKind = $state<keyof typeof KIND_HELP>('allowed_tool');
  let nValue = $state('');

  async function load() {
    loading = true;
    try {
      perms = await invoke<ProjectPermission[]>('list_project_permissions', { projectId });
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  async function add() {
    if (!nSubject.trim() || !nValue.trim()) {
      toast.error('Subject + value required');
      return;
    }
    try {
      await invoke('add_project_permission', {
        projectId,
        req: { subject: nSubject.trim(), kind: nKind, value: nValue.trim(), config: {} },
      });
      toast.success('Permission added');
      nSubject = ''; nValue = ''; showAdd = false;
      await load();
    } catch (e) { toast.error(e); }
  }

  async function del(id: number) {
    if (!confirm('Delete this permission?')) return;
    try {
      await invoke('delete_project_permission', { permId: id });
      await load();
    } catch (e) { toast.error(e); }
  }

  // Group by subject
  const grouped = $derived.by(() => {
    const map = new Map<string, ProjectPermission[]>();
    for (const p of perms) {
      const arr = map.get(p.subject) ?? [];
      arr.push(p);
      map.set(p.subject, arr);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  });

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Permissions</h3>
    <button class="ps-btn-primary" onclick={() => (showAdd = !showAdd)}>{showAdd ? 'Cancel' : '+ Add'}</button>
  </header>

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Subject</span><input bind:value={nSubject} placeholder="agent:planner or @global" /></label>
        <label><span>Kind</span>
          <select bind:value={nKind}>
            {#each KINDS as k}<option value={k}>{k}</option>{/each}
          </select>
        </label>
        <label class="ps-span2"><span>Value</span>
          <input bind:value={nValue} placeholder="e.g. Read, src/**, weaviate-kg" />
        </label>
      </div>
      <p class="ps-hint" title={KIND_HELP[nKind]}>{KIND_HELP[nKind]}</p>
      <button class="ps-btn-primary" onclick={add}>Add permission</button>
    </div>
  {/if}

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if perms.length === 0}
    <p class="ps-empty">No permissions configured.</p>
  {:else}
    {#each grouped as [subject, items] (subject)}
      <div class="ps-group">
        <h4 class="ps-group-h">{subject} <small>({items.length})</small></h4>
        <table class="ps-table">
          <thead><tr><th>Kind</th><th>Value</th><th></th></tr></thead>
          <tbody>
            {#each items as p (p.id)}
              <tr>
                <td>
                  <span class="ps-tag" title={KIND_HELP[p.kind] ?? ''}>{p.kind}</span>
                </td>
                <td><code>{p.value}</code></td>
                <td><button class="ps-btn-link" onclick={() => del(p.id)}>Delete</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/each}
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-form { background: rgba(255,255,255,0.04); padding: 12px; border-radius: 6px; margin-bottom: 16px; }
  .ps-form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 8px; }
  .ps-span2 { grid-column: span 2; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input, .ps-form-grid select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }
  .ps-hint { font-size: 11px; color: #888; margin: 0 0 8px; cursor: help; }
  .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-group { margin-bottom: 16px; }
  .ps-group-h { font-size: 13px; margin: 0 0 4px; color: #c4b3ff; }
  .ps-group-h small { color: #888; font-weight: 400; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(255,255,255,0.08); color: #ccc; cursor: help;
  }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }
</style>
