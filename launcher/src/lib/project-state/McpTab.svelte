<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  // v0.2.102 (W5-B C4): per-project MCP registry tab.
  //
  // The registry surface the migration-010 commands were built for and no
  // screen ever invoked: every MCP row registered for THIS project
  // (mirrored from `.claude/settings.json::mcpServers` and `.mcp.json` by
  // the populate step), with the per-row toggle and the user-added
  // Unregister action. `PermissionsTab` is the OTHER half of the story —
  // the per-project Claude Code gate over the orchestrator's configured
  // servers; this tab is the registry of what the project itself declares.
  //
  // The toggle invokes `set_project_mcp_server_enabled`, which flips the
  // registry row AND routes through the ONE existing writer of
  // `~/.claude.json projects[<path>].disabledMcpServers` — see
  // `set_project_mcp_server_enabled_with_claude_json` in
  // project_state_cmd.rs. This component never writes that file itself.
  //
  // Unregister deletes THIS project's registry row only (confirm-gated —
  // destructive-adjacent); the row comes back on the next populate if the
  // entry still exists in the project's files, which is the honest story
  // and is told in the confirm text.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  let { projectId }: { projectId: string } = $props();

  // Mirrors `vct_launcher_core::db::project_mcp_servers::ProjectMcpServer`.
  interface ProjectMcpServer {
    project_id: string;
    mcp_name: string;
    is_user_added: boolean;
    source: string;
    source_file: string | null;
    enabled: boolean;
    command: string | null;
  }

  let rows = $state<ProjectMcpServer[]>([]);
  let loading = $state(true);
  let toggling = $state<string | null>(null);
  let unregistering = $state<string | null>(null);

  async function load() {
    loading = true;
    try {
      rows = await invoke<ProjectMcpServer[]>('list_project_mcp_servers', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  // Toggle: the backend command flips the registry row and mirrors the
  // project's disabled set into ~/.claude.json through the existing
  // writer. Optimistic flip, revert on error (PermissionsTab pattern).
  async function toggleMcp(row: ProjectMcpServer) {
    const next = !row.enabled;
    const prev = row.enabled;
    row.enabled = next;
    toggling = row.mcp_name;
    try {
      await invoke('set_project_mcp_server_enabled', {
        projectId,
        mcpName: row.mcp_name,
        enabled: next,
      });
    } catch (e) {
      toast.error(e);
      row.enabled = prev;
    } finally {
      toggling = null;
    }
  }

  // Unregister (user-added rows only): confirm-gated, THIS project's row
  // only. Cancel leaves everything untouched.
  async function unregisterMcp(row: ProjectMcpServer) {
    const sure = confirm(
      `Remove the "${row.mcp_name}" registration from this project's MCP list? ` +
        'This removes the launcher row for THIS project only — other projects are ' +
        'not affected and nothing is deleted from your settings files; the next ' +
        're-scan re-adds the entry if it is still declared there.',
    );
    if (!sure) return;
    unregistering = row.mcp_name;
    try {
      await invoke('unregister_project_mcp_server', {
        projectId,
        mcpName: row.mcp_name,
      });
      toast.success(`Unregistered ${row.mcp_name} from this project`);
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      unregistering = null;
    }
  }

  onMount(() => {
    void load();
  });
  $effect(() => {
    if (projectId) void load();
  });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>MCP servers</h3>
  </header>
  <p class="ps-mcp-hint">
    Every MCP this project declares (<code>.claude/settings.json</code> and
    <code>.mcp.json</code>), as registered by the populate step. Toggling flips
    the registry row and mirrors the project's disabled set into
    <code>~/.claude.json</code> →
    <code>projects[&lt;this project&gt;].disabledMcpServers</code> — the channel
    Claude Code honours for user-scope servers (a server defined in the
    project's own <code>.mcp.json</code> is governed by Claude Code's
    project-scope rules, not that key).
  </p>

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if rows.length === 0}
    <p class="ps-empty">
      No MCP servers registered for this project. Registered entries appear
      after the project is re-scanned.
    </p>
  {:else}
    <table class="ps-table">
      <thead>
        <tr>
          <th>Server</th>
          <th>Origin</th>
          <th class="ps-mcp-state-col">Registry</th>
          <th class="ps-mcp-toggle-col">Enabled</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.mcp_name)}
          <tr>
            <td><code>{row.mcp_name}</code></td>
            <td>
              <span
                class="ps-tag"
                title={row.source_file ?? row.source}
              >{row.is_user_added ? 'user-added' : 'bundled'}</span>
              {#if row.command}<span class="ps-cmd" title={row.command}>{row.command}</span>{/if}
            </td>
            <td>
              {#if !row.enabled}
                <span class="ps-tag ps-tag-off" title="Disabled on the registry row; the name is mirrored into this project's disabledMcpServers set.">off</span>
              {:else}
                <span class="ps-tag ps-tag-default" title="Enabled on the registry row (default).">on</span>
              {/if}
            </td>
            <td>
              <label
                class="ps-toggle"
                title={row.enabled ? 'Click to disable this MCP for this project.' : 'Click to enable this MCP for this project.'}
              >
                <input
                  type="checkbox"
                  checked={row.enabled}
                  disabled={toggling === row.mcp_name}
                  onchange={() => toggleMcp(row)}
                  aria-label="{row.enabled ? 'Disable' : 'Enable'} MCP server {row.mcp_name} for this project"
                />
                <span class="ps-toggle-slider"></span>
              </label>
            </td>
            <td>
              {#if row.is_user_added}
                <button
                  class="ps-btn-link"
                  onclick={() => unregisterMcp(row)}
                  disabled={unregistering === row.mcp_name}
                  title="Remove this project's registry row for this user-added MCP (confirm first)"
                >
                  {unregistering === row.mcp_name ? 'Removing…' : 'Unregister'}
                </button>
              {/if}
            </td>
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
  .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-cmd {
    font-family: ui-monospace, monospace; font-size: 10px; color: #888;
    margin-left: 6px; word-break: break-all; max-width: 220px; display: inline-block;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
  }
  .ps-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(255,255,255,0.08); color: #ccc; cursor: help;
  }
  .ps-tag-off  { background: rgba(255,120,120,0.15); color: #ff8888; }
  .ps-tag-default { background: rgba(0,191,166,0.12); color: rgb(0,212,184); }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover:not(:disabled) { text-decoration: underline; }
  .ps-btn-link:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-mcp-hint { font-size: 11px; color: #888; margin: 0 0 14px; line-height: 1.5; }
  .ps-mcp-hint code { font-family: ui-monospace, monospace; font-size: 10px; background: rgba(255,255,255,0.05); padding: 0 4px; border-radius: 2px; }
  .ps-mcp-state-col { width: 80px; }
  .ps-mcp-toggle-col { width: 72px; text-align: right; }

  /* Compact iOS-style toggle (PermissionsTab recipe). */
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
