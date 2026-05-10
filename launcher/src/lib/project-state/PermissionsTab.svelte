<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { ProjectPermission } from '$lib/types/project-state';
  import Dropdown from '$lib/components/Dropdown.svelte';

  let { projectId }: { projectId: string } = $props();

  // 0.2.x backlog #5 (2026-05-10): per-project MCP toggle UI.
  //
  // The backend ships `list_project_mcp_permissions(project_id, server_ids)`
  // and `set_project_mcp_permission(project_id, server_id, enabled)`. The
  // canonical list of server IDs comes from `get_mcp_servers()` —
  // SAME source the dashboard uses, so the toggle list always matches what
  // the user sees elsewhere. No hardcoded server names here.
  //
  // Default-enabled semantic: if there's no `project_permissions` row for
  // (project, server), the server runs. Disabling writes an explicit
  // `config.enabled=false` row; re-enabling DELETEs the row.
  type McpServerConfig = {
    id: string;
    name: string;
    description: string;
    enabled: boolean;       // global enabled flag from get_mcp_servers
    // ...other fields exist but aren't surfaced here
  };
  type ProjectMcpPermission = {
    server_id: string;
    enabled: boolean;       // per-project resolved state
    explicit: boolean;      // true iff a per-project row exists
  };
  type McpRow = McpServerConfig & {
    project_enabled: boolean;
    explicit: boolean;
  };

  let mcpRows = $state<McpRow[]>([]);
  let mcpLoading = $state(true);

  async function loadMcp() {
    mcpLoading = true;
    try {
      const servers = await invoke<McpServerConfig[]>('get_mcp_servers');
      const ids = servers.map((s) => s.id);
      const perms = ids.length
        ? await invoke<ProjectMcpPermission[]>('list_project_mcp_permissions', {
            projectId,
            serverIds: ids,
          })
        : [];
      const permsById = new Map(perms.map((p) => [p.server_id, p]));
      mcpRows = servers.map((s) => {
        const p = permsById.get(s.id);
        return {
          ...s,
          project_enabled: p?.enabled ?? true,
          explicit: p?.explicit ?? false,
        };
      });
    } catch (e) {
      toast.error(e);
    } finally {
      mcpLoading = false;
    }
  }

  async function toggleMcp(row: McpRow) {
    const next = !row.project_enabled;
    // Optimistic UI: flip locally, revert on backend error. The
    // server_enabled (global) state is independent — we only flip the
    // per-project gate.
    const prev = { project_enabled: row.project_enabled, explicit: row.explicit };
    row.project_enabled = next;
    row.explicit = next ? false : true; // re-enable DELETEs the row → no longer explicit
    try {
      await invoke('set_project_mcp_permission', {
        projectId,
        serverId: row.id,
        enabled: next,
      });
    } catch (e) {
      toast.error(e);
      row.project_enabled = prev.project_enabled;
      row.explicit = prev.explicit;
    }
  }

  const KIND_HELP: Record<string, string> = {
    write_scope: 'Glob the agent is allowed to write to (e.g. src/**).',
    allowed_tool: 'Tool name on the allow list (e.g. Read, Edit, Bash).',
    denied_tool: 'Tool name on the deny list (overrides allowed_tool).',
    mcp_server: 'MCP server scoped to this subject (e.g. weaviate-kg).',
    permission_mode: 'default | acceptEdits | dontAsk | bypassPermissions | plan',
  };
  const KINDS = Object.keys(KIND_HELP);
  const KIND_OPTIONS = KINDS.map((k) => ({ value: k, label: k }));

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

  onMount(() => {
    void load();
    void loadMcp();
  });
  $effect(() => {
    if (projectId) {
      void load();
      void loadMcp();
    }
  });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Permissions</h3>
    <button class="ps-btn-primary" onclick={() => (showAdd = !showAdd)}>{showAdd ? 'Cancel' : '+ Add'}</button>
  </header>

  <!-- 0.2.x backlog #5: per-project MCP server toggle.
       Listed before the generic permissions table because it's the
       most common "I want to silence X for this project" surface. -->
  <div class="ps-mcp-section">
    <h4 class="ps-group-h">MCP servers <small>(per-project)</small></h4>
    {#if mcpLoading}
      <p class="ps-empty">Loading MCP servers…</p>
    {:else if mcpRows.length === 0}
      <p class="ps-empty">No MCP servers configured.</p>
    {:else}
      <table class="ps-table">
        <thead>
          <tr>
            <th>Server</th>
            <th>Description</th>
            <th class="ps-mcp-state-col">State</th>
            <th class="ps-mcp-toggle-col">Project</th>
          </tr>
        </thead>
        <tbody>
          {#each mcpRows as row (row.id)}
            <tr class:ps-mcp-row-globally-off={!row.enabled}>
              <td><code>{row.id}</code></td>
              <td class="ps-mcp-desc" title={row.description}>{row.name}</td>
              <td>
                {#if !row.enabled}
                  <span class="ps-tag ps-tag-warn" title="This MCP server is globally disabled in the orchestrator config — toggling per-project has no effect until it's globally enabled.">global off</span>
                {:else if row.explicit && !row.project_enabled}
                  <span class="ps-tag ps-tag-off" title="Disabled for this project via an explicit row in project_permissions.">project off</span>
                {:else if row.explicit && row.project_enabled}
                  <span class="ps-tag" title="Explicit row enables this MCP server for this project.">project on</span>
                {:else}
                  <span class="ps-tag ps-tag-default" title="No per-project row; defaults to enabled.">default</span>
                {/if}
              </td>
              <td>
                <label class="ps-toggle" title={row.project_enabled ? 'Click to disable this MCP server for this project.' : 'Click to enable this MCP server for this project.'}>
                  <input
                    type="checkbox"
                    checked={row.project_enabled}
                    disabled={!row.enabled}
                    onchange={() => toggleMcp(row)}
                  />
                  <span class="ps-toggle-slider"></span>
                </label>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="ps-mcp-hint">
        Toggling here flips a row in <code>project_permissions</code> with
        <code>kind=mcp_server</code>. The launcher's env-writer mirrors this
        into <code>.claude/settings.json::disabledMcpjsonServers</code> so
        Claude Code skips spawning the server in this project. Default state
        (no row) is <em>enabled</em>.
      </p>
    {/if}
  </div>

  <h4 class="ps-group-h">Other permissions</h4>

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Subject</span><input bind:value={nSubject} placeholder="agent:planner or @global" /></label>
        <label><span>Kind</span>
          <Dropdown options={KIND_OPTIONS} bind:value={nKind} />
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

  /* 0.2.x backlog #5: per-project MCP toggle section. */
  .ps-mcp-section { margin-bottom: 20px; }
  .ps-mcp-state-col { width: 90px; }
  .ps-mcp-toggle-col { width: 72px; text-align: right; }
  .ps-mcp-desc { color: #aaa; }
  .ps-mcp-hint { font-size: 11px; color: #888; margin: 8px 0 0; line-height: 1.4; }
  .ps-mcp-hint code { font-family: ui-monospace, monospace; font-size: 10px; background: rgba(255,255,255,0.05); padding: 0 4px; border-radius: 2px; }
  .ps-mcp-row-globally-off { opacity: 0.55; }
  .ps-tag-warn { background: rgba(255,170,80,0.15); color: #ffb060; }
  .ps-tag-off  { background: rgba(255,120,120,0.15); color: #ff8888; }
  .ps-tag-default { background: rgba(120,180,255,0.10); color: #88aacc; }

  /* Compact iOS-style toggle. */
  .ps-toggle { position: relative; display: inline-block; width: 36px; height: 18px; cursor: pointer; }
  .ps-toggle input { opacity: 0; width: 0; height: 0; }
  .ps-toggle-slider {
    position: absolute; inset: 0; background: rgba(255,255,255,0.15);
    border-radius: 9px; transition: background 0.15s ease;
  }
  .ps-toggle-slider::before {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 14px; height: 14px; background: #ddd; border-radius: 50%;
    transition: transform 0.15s ease;
  }
  .ps-toggle input:checked + .ps-toggle-slider { background: rgb(0,191,166); }
  .ps-toggle input:checked + .ps-toggle-slider::before { transform: translateX(18px); }
  .ps-toggle input:disabled + .ps-toggle-slider { opacity: 0.4; cursor: not-allowed; }
</style>
