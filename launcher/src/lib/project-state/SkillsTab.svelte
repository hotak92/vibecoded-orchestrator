<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { ProjectSkill } from '$lib/types/project-state';
  import Dropdown from '$lib/components/Dropdown.svelte';

  // Custom Dropdown — native <select> is unstyled on Linux/Tauri.
  const SOURCE_OPTIONS = [
    { value: 'user', label: 'user' },
    { value: 'bundled', label: 'bundled' },
    { value: 'paid-module', label: 'paid-module' },
    { value: 'project', label: 'project' },
  ];

  let { projectId }: { projectId: string } = $props();

  let skills = $state<ProjectSkill[]>([]);
  let loading = $state(true);
  let showAdd = $state(false);
  let newName = $state('');
  let newSource = $state<'bundled' | 'user' | 'paid-module' | 'project'>('user');
  let newModel = $state('');

  async function load() {
    loading = true;
    try {
      skills = await invoke<ProjectSkill[]>('list_project_skills', { projectId });
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  // v0.2.22 item #17: "Re-scan from disk" button — see AgentsTab.svelte
  // for the full rationale. Same command, idempotent, preserves toggles.
  let rescanning = $state(false);

  type RescanReport = {
    agents_inserted: number;
    skills_inserted: number;
    hooks_inserted: number;
    mcp_servers_inserted: number;
    kg_access_rows_inserted: number;
    warnings: string[];
  };

  async function rescan() {
    rescanning = true;
    try {
      const r = await invoke<RescanReport>('rescan_project_from_filesystem', {
        projectId,
      });
      const parts: string[] = [];
      if (r.agents_inserted > 0) parts.push(`${r.agents_inserted} agents`);
      if (r.skills_inserted > 0) parts.push(`${r.skills_inserted} skills`);
      if (r.hooks_inserted > 0) parts.push(`${r.hooks_inserted} hooks`);
      if (r.mcp_servers_inserted > 0) parts.push(`${r.mcp_servers_inserted} MCP servers`);
      toast.success(
        parts.length > 0
          ? `Re-scanned from disk: ${parts.join(', ')}`
          : 'Re-scan complete (nothing new to register)'
      );
      if (r.warnings.length > 0) {
        console.warn('[rescan] warnings:', r.warnings);
      }
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      rescanning = false;
    }
  }

  async function toggle(name: string, enabled: boolean) {
    try {
      await invoke('set_project_skill_enabled', { projectId, skillName: name, enabled });
      await load();
    } catch (e) { toast.error(e); }
  }

  async function unregister(name: string) {
    if (!confirm(`Unregister skill "${name}"? File NOT removed; only registry row.`)) return;
    try {
      await invoke('unregister_project_skill', { projectId, skillName: name });
      toast.success('Unregistered');
      await load();
    } catch (e) { toast.error(e); }
  }

  async function register() {
    if (!newName.trim()) { toast.error('Name required'); return; }
    try {
      await invoke('register_project_skill', {
        projectId,
        req: {
          skill_name: newName.trim(),
          source: newSource,
          source_module: null,
          model: newModel.trim() || null,
          file_path: null,
          config: {},
        },
      });
      toast.success('Registered');
      newName = ''; newModel = ''; showAdd = false;
      await load();
    } catch (e) { toast.error(e); }
  }

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Skills</h3>
    <button class="ps-btn-primary" onclick={() => (showAdd = !showAdd)}>{showAdd ? 'Cancel' : '+ Register'}</button>
  </header>

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Name</span><input bind:value={newName} placeholder="my-skill" /></label>
        <label><span>Source</span>
          <Dropdown options={SOURCE_OPTIONS} bind:value={newSource} />
        </label>
        <label><span>Model</span><input bind:value={newModel} placeholder="optional" /></label>
      </div>
      <button class="ps-btn-primary" onclick={register}>Register</button>
    </div>
  {/if}

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if skills.length === 0}
    <div class="ps-empty-state">
      <p class="ps-empty">No skills registered.</p>
      <p class="ps-empty-hint">
        Has the project's <code>.claude/skills/</code> already? Click
        Re-scan to populate from disk (idempotent — preserves toggles).
      </p>
      <button class="ps-btn-primary" disabled={rescanning} onclick={rescan}>
        {rescanning ? 'Re-scanning…' : 'Re-scan from disk'}
      </button>
    </div>
  {:else}
    <table class="ps-table">
      <thead><tr><th>Name</th><th>Source</th><th>Model</th><th>Enabled</th><th></th></tr></thead>
      <tbody>
        {#each skills as s (s.skill_name)}
          <tr>
            <td><code>{s.skill_name}</code></td>
            <td><span class="ps-tag ps-tag-{s.source}">{s.source}</span></td>
            <td>{s.model ?? '—'}</td>
            <td>
              <input type="checkbox" checked={s.enabled}
                onchange={(e) => toggle(s.skill_name, (e.target as HTMLInputElement).checked)}
                title="Toggles registry only — .md file untouched" />
            </td>
            <td><button class="ps-btn-link" onclick={() => unregister(s.skill_name)}>Unregister</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-form { background: rgba(255,255,255,0.04); padding: 12px; border-radius: 6px; margin-bottom: 16px; }
  .ps-form-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 10px; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input, .ps-form-grid select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }
  .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-empty-state { text-align: center; padding: 24px; }
  .ps-empty-state .ps-empty { padding: 0 0 8px; }
  .ps-empty-hint { color: #aaa; font-size: 12px; padding: 0 0 16px; max-width: 480px; margin: 0 auto; }
  .ps-empty-hint code { background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 6px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-tag { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: #ccc; }
  .ps-tag-bundled { background: rgba(0,191,166,0.15); color: #0fc; }
  .ps-tag-user { background: rgba(123,95,255,0.15); color: #c4b3ff; }
  .ps-tag-paid-module { background: rgba(255,200,70,0.15); color: #fc6; }
  .ps-tag-project { background: rgba(58,163,255,0.15); color: #6cf; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }
</style>
