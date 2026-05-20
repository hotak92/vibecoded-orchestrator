<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { ProjectHook } from '$lib/types/project-state';
  import Dropdown from '$lib/components/Dropdown.svelte';

  let { projectId }: { projectId: string } = $props();

  let hooks = $state<ProjectHook[]>([]);
  let loading = $state(true);
  let showAdd = $state(false);

  let nEvent = $state('PostToolUse');
  let nMatcher = $state('');
  let nCommand = $state('');
  let nTimeout = $state('');

  const COMMON_EVENTS = [
    'SessionStart', 'SessionEnd', 'UserPromptSubmit',
    'PreToolUse', 'PostToolUse', 'Stop', 'StopFailure',
    'PreCompact', 'PostCompact', 'TeammateIdle', 'TaskCompleted',
  ];
  const EVENT_OPTIONS = COMMON_EVENTS.map((e) => ({ value: e, label: e }));

  // PR-6 (v0.2.11): per-project lean-ctx toggle. Three logical states map
  // to two on-disk states for `<project>/.claude/env::VCO_LEAN_CTX_DEFAULT`:
  //   * 'default' → key absent (the PR-1 hook treats absence as "on")
  //   * 'on'      → key present, value 'on'
  //   * 'off'     → key present, value 'off'
  type LeanCtxChoice = 'default' | 'on' | 'off';
  const LEAN_CTX_KEY = 'VCO_LEAN_CTX_DEFAULT';
  const LEAN_CTX_OPTIONS: Array<{ value: LeanCtxChoice; label: string }> = [
    { value: 'default', label: 'Default (on)' },
    { value: 'on', label: 'Per-project: on' },
    { value: 'off', label: 'Per-project: off' },
  ];
  let leanCtxChoice = $state<LeanCtxChoice>('default');
  let leanCtxLoading = $state(true);
  let leanCtxSaving = $state(false);

  async function load() {
    loading = true;
    try {
      hooks = await invoke<ProjectHook[]>('list_project_hooks', { projectId });
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  // v0.2.22 item #17: "Re-scan from disk" button — see AgentsTab.svelte
  // for the full rationale. Hooks live in `.claude/settings.json`'s
  // `hooks` block; the populate scanner mirrors them into project_hooks.
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

  async function loadLeanCtx() {
    leanCtxLoading = true;
    try {
      const v = await invoke<string | null>('get_claude_env_value', {
        projectId,
        key: LEAN_CTX_KEY,
      });
      if (v === null || v === undefined) leanCtxChoice = 'default';
      else if (v === 'off') leanCtxChoice = 'off';
      else if (v === 'on') leanCtxChoice = 'on';
      // Any other value (manual edit) is rendered as "default" in the UI;
      // the user keeps the on-disk override until they actively change the
      // toggle, at which point we overwrite cleanly.
      else leanCtxChoice = 'default';
    } catch (e) {
      toast.error(e);
    } finally {
      leanCtxLoading = false;
    }
  }

  async function setLeanCtx(next: LeanCtxChoice) {
    if (next === leanCtxChoice) return;
    leanCtxSaving = true;
    const previous = leanCtxChoice;
    leanCtxChoice = next; // optimistic
    try {
      const value = next === 'default' ? null : next;
      await invoke('set_claude_env_value', {
        projectId,
        key: LEAN_CTX_KEY,
        value,
      });
      toast.success(
        next === 'default'
          ? 'Reverted to default (compression on)'
          : `Per-project compression set to ${next}`,
      );
    } catch (e) {
      leanCtxChoice = previous;
      toast.error(e);
    } finally {
      leanCtxSaving = false;
    }
  }

  async function toggle(id: number, enabled: boolean) {
    try {
      await invoke('set_project_hook_enabled', { hookId: id, enabled });
      await load();
    } catch (e) { toast.error(e); }
  }

  async function del(id: number) {
    if (!confirm('Delete this hook from the registry? Script file untouched.')) return;
    try {
      await invoke('unregister_project_hook', { hookId: id });
      toast.success('Removed');
      await load();
    } catch (e) { toast.error(e); }
  }

  async function add() {
    if (!nEvent.trim() || !nCommand.trim()) {
      toast.error('event + command required');
      return;
    }
    try {
      await invoke('register_project_hook', {
        projectId,
        req: {
          event: nEvent.trim(),
          matcher: nMatcher.trim(),
          command: nCommand.trim(),
          source: 'project',
          source_module: null,
          timeout_ms: nTimeout.trim() ? Number(nTimeout) : null,
          config: {},
        },
      });
      toast.success('Hook registered');
      nMatcher = ''; nCommand = ''; nTimeout = '';
      showAdd = false;
      await load();
    } catch (e) { toast.error(e); }
  }

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Hooks</h3>
    <button class="ps-btn-primary" onclick={() => (showAdd = !showAdd)}>{showAdd ? 'Cancel' : '+ Register'}</button>
  </header>

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Event</span>
          <Dropdown options={EVENT_OPTIONS} bind:value={nEvent} />
        </label>
        <label><span>Matcher</span><input bind:value={nMatcher} placeholder="Edit(*) or *" /></label>
        <label class="ps-span2"><span>Command</span>
          <input bind:value={nCommand} placeholder="bash .claude/hooks/my-hook.sh" />
        </label>
        <label><span>Timeout (ms)</span><input bind:value={nTimeout} placeholder="optional" inputmode="numeric" /></label>
      </div>
      <button class="ps-btn-primary" onclick={add}>Register hook</button>
    </div>
  {/if}

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if hooks.length === 0}
    <div class="ps-empty-state">
      <p class="ps-empty">No hooks registered.</p>
      <p class="ps-empty-hint">
        Has the project's <code>.claude/settings.json</code> already?
        Click Re-scan to populate from disk (idempotent — preserves toggles).
      </p>
      <button class="ps-btn-primary" disabled={rescanning} onclick={rescan}>
        {rescanning ? 'Re-scanning…' : 'Re-scan from disk'}
      </button>
    </div>
  {:else}
    <table class="ps-table">
      <thead><tr><th>Event</th><th>Matcher</th><th>Command</th><th>Source</th><th>Enabled</th><th></th></tr></thead>
      <tbody>
        {#each hooks as h (h.id)}
          <tr>
            <td><code>{h.event}</code></td>
            <td><code>{h.matcher || '*'}</code></td>
            <td class="ps-cmd"><code>{h.command}</code></td>
            <td><span class="ps-tag ps-tag-{h.source}">{h.source}</span></td>
            <td>
              <input type="checkbox" checked={h.enabled}
                onchange={(e) => toggle(h.id, (e.target as HTMLInputElement).checked)} />
            </td>
            <td><button class="ps-btn-link" onclick={() => del(h.id)}>Delete</button></td>
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
  .ps-span2 { grid-column: span 2; }
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
  .ps-cmd { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ps-tag { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: #ccc; }
  .ps-tag-bundled { background: rgba(0,191,166,0.15); color: #0fc; }
  .ps-tag-project { background: rgba(58,163,255,0.15); color: #6cf; }
  .ps-tag-paid-module { background: rgba(255,200,70,0.15); color: #fc6; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }
</style>
