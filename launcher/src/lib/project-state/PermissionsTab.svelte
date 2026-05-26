<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    ProjectPermission,
    McpToolGrant,
  } from '$lib/types/project-state';
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

  // ─── MCP Tools sub-section (diagrams-integration plan Phase 1 item 7) ──
  //
  // Per-tool allowlist surface: for each MCP server registered for this
  // project, render a collapsible group showing per-tool enabled/disabled
  // toggles. Backed by `project_mcp_tool_grants` (Phase 1.1 DB migration).
  //
  // Decision recap (plan §3 Phase 1.2): we ship per-tool granularity for
  // all MCPs in Phase 4, but Phase 1 only needs the Mermaid wrapper's
  // tools controllable. The UI generalises from the start so Phase 4
  // doesn't reshape this section — just adds rows for more MCPs.
  //
  // Default state (no rows in DB for an MCP): all tools enabled. The
  // "Customize" button populates the table with all-enabled rows so the
  // user can then toggle individual tools off.

  type McpInfo = {
    mcp_name: string;
    project_enabled: boolean;
    explicit: boolean;
  };
  type McpToolsGroup = {
    info: McpInfo;
    expanded: boolean;
    loading: boolean;
    loaded: boolean;
    customized: boolean; // true iff any row exists in project_mcp_tool_grants
    tools: McpToolGrant[];
  };
  let mcpToolGroups = $state<McpToolsGroup[]>([]);

  // v0.2.35 Agent L fix (a): atomic MCPs (weaviate-kg / search / playwright)
  // ship no `tool_allowlist` — they're on/off-only, not per-tool gated. The
  // generic MCP Tools section rendered an empty "No tools exposed" placeholder
  // for them which looked like a bug. We now filter atomic MCPs out of the
  // per-tool section entirely (they remain in the MCP servers toggle table
  // above).
  //
  // The frontend mirrors the Rust-side `fallback_default_allowlist` in
  // `launcher/src-tauri/src/commands/diagrams_cmd.rs` — see the integration
  // test `fallback_default_allowlist_matches_hub_constants` for the sync
  // contract with `vct-hub`. If new wrapper MCPs ship with default tool
  // allowlists, add their IDs here.
  const PER_TOOL_CAPABLE_MCPS = new Set(['mermaid', 'excalidraw']);

  async function loadMcpToolGroups() {
    // Reuse the same MCP list that drives the per-server toggle table
    // above. Derive a thin Info[] from `mcpRows` once it's loaded.
    if (!mcpRows.length) {
      mcpToolGroups = [];
      return;
    }
    // Filter to per-tool-capable MCPs only (fix (a) above). Atomic MCPs
    // still appear in the MCP servers section above with on/off toggles.
    mcpToolGroups = mcpRows
      .filter((row) => PER_TOOL_CAPABLE_MCPS.has(row.id))
      .map((row) => ({
        info: {
          mcp_name: row.id,
          project_enabled: row.project_enabled,
          explicit: row.explicit,
        },
        expanded: false,
        loading: false,
        loaded: false,
        customized: false,
        tools: [],
      }));
  }

  async function expandMcpToolGroup(group: McpToolsGroup) {
    group.expanded = !group.expanded;
    if (!group.expanded || group.loaded) return;
    group.loading = true;
    try {
      const tools = await invoke<McpToolGrant[]>('list_project_mcp_tools', {
        projectId,
        mcpName: group.info.mcp_name,
      });
      group.tools = tools;
      group.customized = tools.length > 0;
      group.loaded = true;
    } catch (e) {
      // Defensive — `list_project_mcp_tools` is wired since v0.2.33,
      // so this branch only fires on a real Tauri-IPC error (DB lock,
      // schema migration mid-flight). No toast: each MCP would fire
      // its own and the user can't act on the failure anyway.
      console.warn('[permissions] list_project_mcp_tools failed:', e);
      group.tools = [];
      group.customized = false;
      group.loaded = true;
    } finally {
      group.loading = false;
    }
  }

  async function toggleMcpTool(group: McpToolsGroup, tool: McpToolGrant) {
    const next = !tool.enabled;
    const prev = tool.enabled;
    tool.enabled = next; // optimistic
    try {
      await invoke('set_project_mcp_tool_enabled', {
        projectId,
        mcpName: group.info.mcp_name,
        toolName: tool.tool_name,
        enabled: next,
      });
      group.customized = true;
    } catch (e) {
      toast.error(e);
      tool.enabled = prev; // revert
    }
  }

  async function customizeMcpToolGroup(group: McpToolsGroup) {
    // "Customize" pre-populates the grant table with all-enabled rows
    // by calling the backend's seed helper. From there the user can
    // toggle off the ones they don't want.
    group.loading = true;
    try {
      const tools = await invoke<McpToolGrant[]>('seed_project_mcp_tool_grants', {
        projectId,
        mcpName: group.info.mcp_name,
      });
      group.tools = tools;
      group.customized = true;
    } catch (e) {
      toast.error(e);
    } finally {
      group.loading = false;
    }
  }

  function enabledCount(group: McpToolsGroup): string {
    if (!group.loaded) return '?';
    if (!group.customized) return 'all';
    const on = group.tools.filter((t) => t.enabled).length;
    return `${on}/${group.tools.length}`;
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
  // Re-derive the per-MCP tool-grant groups whenever the MCP server list
  // refreshes. Cheap (just builds metadata; tools are lazy-loaded on
  // expand) so safe to run on every refresh.
  $effect(() => {
    if (mcpRows.length >= 0) {
      void loadMcpToolGroups();
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

  <!--
    diagrams-integration plan Phase 1 item 7: per-MCP tool grants.
    Each collapsible group shows "<enabled>/<total>" once expanded; a
    project that has never customised the allowlist for an MCP shows
    "all" + a Customize button (pre-populates an all-enabled grant
    table the user can then trim).
  -->
  <div class="ps-mcp-tools-section">
    <h4 class="ps-group-h">MCP Tools <small>(per-MCP allowlist)</small></h4>
    {#if mcpLoading}
      <p class="ps-empty">Loading MCP tools…</p>
    {:else if mcpToolGroups.length === 0}
      <p class="ps-empty">
        No per-tool-gated MCPs registered. Atomic MCPs (e.g.
        <code>weaviate-kg</code>, <code>search</code>, <code>playwright</code>)
        are controlled by the on/off toggle above.
      </p>
    {:else}
      <ul class="ps-mcp-tool-groups">
        {#each mcpToolGroups as group (group.info.mcp_name)}
          <li class="ps-mcp-tool-group">
            <button
              class="ps-mcp-tool-summary"
              onclick={() => expandMcpToolGroup(group)}
              aria-expanded={group.expanded}
              aria-controls="mcp-tools-{group.info.mcp_name}"
            >
              <span class="ps-mcp-tool-chevron" aria-hidden="true">
                {group.expanded ? '▾' : '▸'}
              </span>
              <code class="ps-mcp-tool-name">{group.info.mcp_name}</code>
              <span class="ps-mcp-tool-count">
                {enabledCount(group)} tools enabled
              </span>
              {#if !group.info.project_enabled}
                <span class="ps-tag ps-tag-off" title="The MCP server itself is disabled for this project; per-tool grants have no effect until you re-enable the server above.">server off</span>
              {/if}
            </button>
            {#if group.expanded}
              <div
                id="mcp-tools-{group.info.mcp_name}"
                class="ps-mcp-tool-body"
              >
                {#if group.loading}
                  <p class="ps-empty">Loading tools…</p>
                {:else if !group.loaded}
                  <p class="ps-empty">Failed to load tools.</p>
                {:else if !group.customized}
                  <p class="ps-mcp-tool-default">
                    Using defaults: <strong>all tools enabled</strong>.
                  </p>
                  <button class="ps-btn-primary" onclick={() => customizeMcpToolGroup(group)}>
                    Customize
                  </button>
                {:else if group.tools.length === 0}
                  <p class="ps-empty">No tools exposed by this MCP.</p>
                {:else}
                  <ul class="ps-mcp-tool-grid" role="list">
                    {#each group.tools as tool (tool.tool_name)}
                      <li class="ps-mcp-tool-cell">
                        <label
                          class="ps-mcp-tool-label"
                          title={tool.description ?? '(no description)'}
                        >
                          <input
                            type="checkbox"
                            checked={tool.enabled}
                            disabled={!group.info.project_enabled}
                            onchange={() => toggleMcpTool(group, tool)}
                            aria-label="{tool.tool_name} {tool.enabled ? 'enabled' : 'disabled'}"
                          />
                          <code>{tool.tool_name}</code>
                          <span class="ps-mcp-tool-help" aria-hidden="true">?</span>
                        </label>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
      <p class="ps-mcp-hint">
        Per-tool toggles flip rows in
        <code>project_mcp_tool_grants</code>. Wrapper MCPs read these on
        each <code>tools/list</code> request and filter the response — the
        upstream MCP never sees the call for disabled tools.
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
  .ps-form-grid input {
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

  /* MCP Tools sub-section (diagrams-integration plan Phase 1 item 7). */
  .ps-mcp-tools-section { margin-bottom: 20px; }
  .ps-mcp-tool-groups { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .ps-mcp-tool-group {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 4px;
  }
  .ps-mcp-tool-summary {
    width: 100%;
    background: none;
    border: none;
    color: inherit;
    padding: 8px 12px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    text-align: left;
  }
  .ps-mcp-tool-summary:hover { background: rgba(255,255,255,0.04); }
  .ps-mcp-tool-chevron { color: #888; width: 12px; display: inline-block; }
  .ps-mcp-tool-name { font-family: ui-monospace, monospace; font-size: 12px; flex: 1; }
  .ps-mcp-tool-count { color: #888; font-size: 11px; }
  .ps-mcp-tool-body {
    padding: 10px 12px 12px;
    border-top: 1px solid rgba(255,255,255,0.04);
  }
  .ps-mcp-tool-default { font-size: 12px; color: #aaa; margin: 0 0 8px; }
  .ps-mcp-tool-grid {
    list-style: none; margin: 0; padding: 0;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 6px;
  }
  .ps-mcp-tool-cell { display: flex; }
  .ps-mcp-tool-label {
    display: flex; align-items: center; gap: 6px;
    padding: 4px 6px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 11px;
    width: 100%;
  }
  .ps-mcp-tool-label:hover { background: rgba(255,255,255,0.03); }
  .ps-mcp-tool-label code { font-family: ui-monospace, monospace; font-size: 11px; flex: 1; }
  .ps-mcp-tool-help {
    color: #666; font-size: 10px;
    border: 1px solid #444; border-radius: 50%;
    width: 14px; height: 14px;
    display: inline-flex; align-items: center; justify-content: center;
    cursor: help;
  }
</style>
